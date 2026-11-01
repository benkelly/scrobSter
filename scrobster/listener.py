"""Capture loops: ffmpeg chunk -> shazamio match -> dedup -> store -> scrobble.

Each room runs its own loop. They share one Shazam queue, because every request
leaves from the same address and the rate limit counts them all.

CLI:
  python -m scrobster.listener song.wav   # match one file, print, exit
  python -m scrobster.listener            # run the capture loop headless
"""
import array
import asyncio
import collections
import io
import logging
import math
import pathlib
import re
import sys
import tempfile
import time
import wave

import aiohttp
from shazamio import Shazam
from shazamio.interfaces.client import HTTPClientInterface

from . import accounts, config, db, enrich, events, rooms, scrobble

log = logging.getLogger("scrobster")

# The window shazamio fingerprints. 12s is what the Shazam apps send, and
# shazamio 0.9 warns on any other length. Shazam answers no match from 15s up.
# A longer capture still works: shazamio-core fingerprints its middle 12s, and a
# shorter one whole.
MAX_SEGMENT_SECONDS = 12
# Below this peak there is nothing to identify, so skip the request and save quota.
SILENT_DBFS = -70.0
# A clip's floor is the median level of its windows this long. A click or a pop
# fills one window, so it raises the peak but not the floor. See QUIET_DBFS.
FLOOR_WINDOW_SECONDS = 0.05
# Give up on one Shazam request after this long.
SHAZAM_TIMEOUT_SECONDS = 30
# How far back the request count on the Rooms tab looks.
REQUEST_WINDOW_SECONDS = 600
# ffmpeg gets this long beyond the audio it records before it counts as stuck.
FFMPEG_GRACE_SECONDS = 20
# A room's live meter takes the peak of each block this long, while it records.
LEVEL_BLOCK_SECONDS = 0.25
# The meter shows the loudest block this recent, and nothing once blocks stop.
LEVEL_SHOW_SECONDS = 0.5
LEVEL_STALE_SECONDS = 1.5


class RateLimited(RuntimeError):
    """Shazam answered 429 Too Many Requests."""


class ShazamClient(HTTPClientInterface):
    """Ask Shazam once, with a timeout.

    shazamio's own client retries a 429 twenty times over about twelve minutes.
    That stalls capture and browser clips the whole time, makes the rate limit
    worse, and ends in "Failed to decode json". The capture loop backs off
    instead, see rate_limit_wait().

    Each request opens and closes its own session, so nothing is pooled and
    nothing is bound to an event loop. Shazam.close() leaves a client it was
    given alone, and this one has nothing to close.
    """

    async def request(self, method, url, *args, **kwargs):
        return await self._send(method, url, lambda r: r.json(content_type=None), **kwargs)

    async def request_text(self, url, *, content_type, **kwargs):
        # Required by shazamio>=0.9 for its chart endpoints. recognize() does not use it.
        return await self._send("GET", url, lambda r: r.text(), **kwargs)

    async def _send(self, method, url, read, **kwargs):
        timeout = aiohttp.ClientTimeout(total=SHAZAM_TIMEOUT_SECONDS)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.request(method, url, **kwargs) as r:
                if r.status == 429:
                    raise RateLimited("Shazam is limiting requests from this address")
                if r.status >= 400:
                    raise RuntimeError(f"Shazam answered HTTP {r.status}")
                return await read(r)


def rate_limit_wait(previous: float) -> float:
    """Seconds to wait after a 429: a minute, doubling to ten while it lasts."""
    return min(max(previous * 2, 60), 600)


def segment_seconds(chunk_seconds: int) -> int:
    """Seconds of a browser clip worth keeping: no more than is fingerprinted."""
    return min(chunk_seconds, MAX_SEGMENT_SECONDS)


# A match reports where it landed inside the track. Allow this much slack before
# calling a lower offset a restart: the sent window is ~12s and matches jitter.
REPEAT_TOLERANCE_SECONDS = 45
# Never count two plays of one track closer together than this.
MIN_REPEAT_GAP_SECONDS = 30
# A "now playing" mark expires after a few minutes, so refresh it while the same
# track keeps matching.
NOW_PLAYING_REFRESH_SECONDS = 120
# Remember a scrobbled play this long past the cooldown. It has to outlast one
# unbroken play of the longest track, because a play is recorded only when it is
# scrobbled, not each time it matches.
PLAY_MEMORY_SECONDS = 24 * 3600


def should_clear(now, last_match_at, now_playing, stop_after_s) -> bool:
    """True when a playing-now mark is stale and the music has stopped.

    Only a mark that exists can go stale. The test uses the last MATCH, not the
    last mark, because the mark is refreshed on a timer while a song plays.
    """
    if now_playing is None:
        return False
    if last_match_at is None:
        return True
    return now - last_match_at >= stop_after_s


def should_announce(track_key, now, prev) -> bool:
    """True when the service needs a fresh "playing now" mark.

    `prev` is (track_key, timestamp) of the last mark sent, or None. A new track
    is announced at once, and a track that keeps playing is refreshed so the mark
    does not expire.
    """
    if prev is None or prev[0] != track_key:
        return True
    return now - prev[1] >= NOW_PLAYING_REFRESH_SECONDS


def should_scrobble(key, now, offset, last_plays, fallback_cooldown_s) -> bool:
    """True when this match is a NEW play rather than the same play continuing.

    Shazam reports no track duration, but it reports `offset`, the position inside
    the track. Through one play the offset advances with the clock. When the track
    restarts the offset drops back, and that drop is what marks a repeat.

    `last_plays` maps a key to (timestamp, offset) of the last SCROBBLED play. The
    key is per user, so one user hearing a track twice through two microphones
    still scrobbles it once.
    """
    prev = last_plays.get(key)
    if prev is None:
        return True
    prev_ts, prev_offset = prev
    elapsed = now - prev_ts
    if elapsed < MIN_REPEAT_GAP_SECONDS:
        return False
    if offset is None or prev_offset is None:
        return elapsed >= fallback_cooldown_s  # no offset: fall back to a cooldown
    # Where the track would be now if it never stopped playing.
    expected = prev_offset + elapsed
    return offset < expected - REPEAT_TOLERANCE_SECONDS


def play_memory_seconds(fallback_cooldown_s) -> float:
    return fallback_cooldown_s + PLAY_MEMORY_SECONDS


def forget_old_plays(last_plays, now, fallback_cooldown_s):
    """Drop plays too old for should_scrobble() to answer no, so the duplicate
    filter does not keep every track ever heard until a restart.

    Takes the same cooldown as should_scrobble(). Adding a day rather than taking
    the longer of the two also covers a match judged with an earlier `now`,
    while a browser clip prunes during its network calls.
    """
    keep_s = play_memory_seconds(fallback_cooldown_s)
    for key in [k for k, (ts, _) in last_plays.items() if now - ts >= keep_s]:
        del last_plays[key]


# Ask Shazam again this long before a known song should end, so the next one
# is caught as it starts.
SONG_END_MARGIN_SECONDS = 5


def known_for(prev_key, info, length, elapsed, cap) -> float:
    """How long a room may go without asking Shazam after this match.

    Only a song matched twice in a row counts as known, so one wrong match
    never silences a room. It stays known for at most `cap` seconds, which
    bounds how late a song changed half-way is noticed, and no longer than
    the song's end when MusicBrainz knows its `length`. `elapsed` is the time
    since the clip started, which is where `offset` points.
    """
    if cap <= 0 or not info or info["track_key"] != prev_key:
        return 0
    if length and info.get("offset") is not None:
        left = length - info["offset"] - elapsed - SONG_END_MARGIN_SECONDS
        return max(0.0, min(cap, left))
    return cap


def peak_dbfs(wav: bytes) -> float:
    """Peak level of 16-bit WAV bytes, in dBFS. Digital silence returns -99.0.

    A silent input device matches nothing and looks identical to "music not
    recognised", so the loop reports this instead of failing quietly.
    """
    with wave.open(io.BytesIO(wav)) as w:
        return peak_dbfs_pcm(w.readframes(w.getnframes()))


def _samples(frames: bytes) -> array.array:
    samples = array.array("h")
    samples.frombytes(frames[: len(frames) - len(frames) % 2])
    if sys.byteorder == "big":
        samples.byteswap()
    return samples


def peak_dbfs_pcm(frames: bytes) -> float:
    """Peak level of raw 16-bit samples, as peak_dbfs()."""
    samples = _samples(frames)
    peak = max(max(samples), -min(samples)) if samples else 0
    return round(20 * math.log10(peak / 32768), 1) if peak > 0 else -99.0


def floor_dbfs(wav: bytes) -> float:
    """The level most of a clip sits at: the median RMS of its
    FLOOR_WINDOW_SECONDS windows, in dBFS. Digital silence returns -99.0.

    A record that has ended still sends surface noise, clicks and pops. Their
    peak is far above SILENT_DBFS, but the median ignores a few loud windows.
    """
    with wave.open(io.BytesIO(wav)) as w:
        size = max(1, int(w.getframerate() * FLOOR_WINDOW_SECONDS))
        samples = _samples(w.readframes(w.getnframes()))
    # A short last window would count as much as a whole one, so it is left
    # out, unless the clip is shorter than one window.
    windows = [samples[i:i + size] for i in range(0, max(1, len(samples) - size + 1), size)]
    levels = sorted(sum(s * s for s in w) / len(w) for w in windows if w)
    mean_square = levels[len(levels) // 2] if levels else 0
    return round(10 * math.log10(mean_square / 32768 ** 2), 1) if mean_square > 0 else -99.0


def is_quiet(floor_db, quiet_dbfs) -> bool:
    """True when a clip that is not silent is still too quiet to be music, such
    as a record between sides. Off while `quiet_dbfs` is None."""
    return quiet_dbfs is not None and floor_db < quiet_dbfs


def parse_track(result) -> dict | None:
    track = (result or {}).get("track")
    if not track:
        return None
    album = None
    for sec in track.get("sections", []):
        for m in sec.get("metadata", []):
            if m.get("title") == "Album":
                album = m.get("text")
    matches = (result or {}).get("matches") or []
    return {
        "track_key": track.get("key"),
        "title": track.get("title"),
        "artist": track.get("subtitle"),
        "album": album,
        "art_url": (track.get("images") or {}).get("coverart"),
        "isrc": track.get("isrc"),
        # position inside the track; used to tell a repeat from a continuing play
        "offset": matches[0].get("offset") if matches else None,
    }


# --- finding the audio device -------------------------------------------------

def parse_device_list(backend: str, text: str) -> list[dict]:
    """Turn ffmpeg's device listing into [{"device": ..., "label": ...}].

    avfoundation prints its list as a log, with a video section first:
        [AVFoundation indev @ 0x...] AVFoundation audio devices:
        [AVFoundation indev @ 0x...] [0] MacBook Pro Microphone
    `ffmpeg -sources <backend>` (alsa, pulse, and others) prints one per line,
    with a star on the default. ffmpeg 5.0 and later add the media types:
        * default [Playback/recording through the PulseAudio sound server] (none)
          hw:CARD=PCH,DEV=0 [HDA Intel PCH, ALC3232 Analog] (none)
    """
    found = []
    if backend == "avfoundation":
        in_audio = False
        for line in text.splitlines():
            msg = line.split("] ", 1)[-1].strip() if line.startswith("[") else line.strip()
            if msg.endswith("audio devices:"):
                in_audio = True
                continue
            if msg.endswith("devices:"):
                in_audio = False
                continue
            if in_audio and msg.startswith("[") and "]" in msg:
                index, _, name = msg[1:].partition("]")
                if index.strip().isdigit():
                    found.append({"device": f":{index.strip()}", "label": name.strip()})
        return found
    for line in text.splitlines():
        m = re.match(r"^\s*(\*?)\s*(\S+)\s+\[(.*)\](?:\s+\([^()]*\))?\s*$", line)
        if m and not line.lower().startswith("auto-detected"):
            found.append({"device": m.group(2), "label": m.group(3).strip(),
                          "default": m.group(1) == "*"})
    return found


def parse_proc_asound_cards(text: str) -> dict[int, tuple[str, str]]:
    """Card number -> (id, name) from /proc/asound/cards. Two lines per card:
         1 [CODEC          ]: USB-Audio - USB Audio CODEC
                              Burr-Brown from TI USB Audio CODEC at usb-0000:00:14.0-2
    The number follows the order the cards appeared at boot. The id stays.
    """
    cards = {}
    for line in text.splitlines():
        m = re.match(r"^\s*(\d+)\s+\[([^\]\s]+)\s*\]:.*? - (.*)$", line)
        if m:
            cards[int(m.group(1))] = (m.group(2), m.group(3).strip())
    return cards


def parse_proc_asound_pcm(text: str, cards=None) -> list[dict]:
    """Capture devices from /proc/asound/pcm, for a Linux box with no
    `ffmpeg -sources` support and no alsa-utils. One line per PCM:
        00-00: ALC3232 Analog : ALC3232 Analog : playback 1 : capture 1

    A card found in `cards`, from parse_proc_asound_cards(), is named by its id,
    as in hw:CARD=CODEC,DEV=0, because hw:1,0 can be another card after a reboot
    that found the USB devices in another order. `numeric` keeps the number, so
    the form can point a room saved with it at the name.
    """
    cards = cards or {}
    found = []
    for line in text.splitlines():
        head, _, rest = line.partition(":")
        if "capture" not in rest or "-" not in head:
            continue
        card, _, dev = head.strip().partition("-")
        card, dev = int(card), int(dev)
        name = rest.split(":")[0].strip()
        if card in cards:
            card_id, card_name = cards[card]
            found.append({"device": f"hw:CARD={card_id},DEV={dev}",
                          "label": f"{card_name}, {name}", "numeric": f"hw:{card},{dev}"})
        else:
            found.append({"device": f"hw:{card},{dev}", "label": name})
    return found


PROC_ASOUND = pathlib.Path("/proc/asound")


def _read_proc_asound(name) -> str:
    """A file under /proc/asound, or "" when it is missing or unreadable."""
    try:
        return (PROC_ASOUND / name).read_text()
    except OSError:
        return ""


async def _run(*argv, timeout=10) -> str:
    """Run a command and return whatever it printed on either stream."""
    proc = await asyncio.create_subprocess_exec(
        *argv, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        return ""
    return out.decode(errors="replace")


async def list_devices(backend: str) -> list[dict]:
    """Audio inputs ffmpeg can open with this backend. Empty when it cannot tell."""
    try:
        if backend == "avfoundation":
            text = await _run("ffmpeg", "-hide_banner", "-f", "avfoundation",
                              "-list_devices", "true", "-i", "")
        else:
            text = await _run("ffmpeg", "-hide_banner", "-sources", backend)
    except FileNotFoundError:
        return []
    found = parse_device_list(backend, text)
    if backend == "alsa":
        # ffmpeg always lists `null`, and can miss the cards in a container, so
        # /proc is read as well. Its entries also carry the numeric name that
        # the Rooms form needs to recognise an old `hw:1,0`.
        found = merge_alsa_devices(found, parse_proc_asound_pcm(
            _read_proc_asound("pcm"), parse_proc_asound_cards(_read_proc_asound("cards"))))
    return found


def merge_alsa_devices(listed: list[dict], proc: list[dict]) -> list[dict]:
    """ffmpeg's listing, then the /proc devices it left out. A device in both
    keeps ffmpeg's entry and gains the numeric name from /proc."""
    by_device = {d["device"]: d for d in proc}
    merged = []
    for d in listed:
        numeric = by_device.get(d["device"], {}).get("numeric")
        merged.append({**d, "numeric": numeric} if numeric else d)
    seen = {d["device"] for d in listed}
    return merged + [d for d in proc if d["device"] not in seen]


async def _ffmpeg(*argv, data=None, timeout, on_stdout=None):
    """Run ffmpeg and return (returncode, stderr).

    `on_stdout`, when given, is called with each piece of ffmpeg's standard
    output as it arrives.

    An input that opens but never delivers audio would otherwise block for good.
    A cancelled capture would leave ffmpeg holding the device, so it is killed
    on the way out whatever happened.
    """
    proc = await asyncio.create_subprocess_exec(
        "ffmpeg", "-hide_banner", "-loglevel", "error", *argv,
        stdin=asyncio.subprocess.PIPE if data is not None else asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE if on_stdout else asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
    )

    async def run():
        if on_stdout is None:
            return (await proc.communicate(data))[1]
        async def pump():
            while piece := await proc.stdout.read(65536):
                on_stdout(piece)
        err, _ = await asyncio.gather(proc.stderr.read(), pump())
        await proc.wait()
        return err

    try:
        err = await asyncio.wait_for(run(), timeout)
    except asyncio.TimeoutError:
        raise RuntimeError(f"ffmpeg gave nothing in {timeout:.0f}s; the input may have"
                           " stopped sending audio") from None
    finally:
        if proc.returncode is None:
            proc.kill()
            await proc.wait()
    return proc.returncode, err.decode(errors="replace")


def input_args(backend=None, device=None, url=None) -> list[str]:
    """ffmpeg's input options for a device, or for a stream when `url` is set."""
    if not url:
        return ["-f", backend or config.AUDIO_BACKEND, "-i", device or config.AUDIO_DEVICE]
    args = []
    if url.lower().startswith(("rtsp://", "rtsps://")):
        # TCP survives a lossy network, where UDP drops packets and with them
        # the fingerprint. Asking for audio only means the camera never sends
        # its video, which would be thrown away.
        args += ["-rtsp_transport", "tcp", "-allowed_media_types", "audio"]
    return args + ["-i", url, "-vn"]


class LevelBlocks:
    """Cut raw 16 kHz mono samples into blocks and report each block's peak."""

    def __init__(self, on_level):
        self.on_level = on_level
        self.size = int(16000 * LEVEL_BLOCK_SECONDS) * 2
        self.pending = b""

    def feed(self, piece: bytes):
        self.pending += piece
        while len(self.pending) >= self.size:
            block, self.pending = self.pending[:self.size], self.pending[self.size:]
            self.on_level(peak_dbfs_pcm(block))


async def capture_chunk(seconds=None, backend=None, device=None, url=None,
                        on_level=None) -> bytes:
    """Record one chunk from a device, or from a stream when `url` is set. Left
    out, the device is the configured one.

    `on_level`, when given, hears the peak of every LEVEL_BLOCK_SECONDS while
    the chunk records, for a live meter.
    """
    # ponytail: one ffmpeg spawn per chunk; self-heals when the device drops.
    # Write a temp file, never a pipe: piped WAV gets 0xFFFFFFFF RIFF sizes because
    # ffmpeg cannot seek back to patch them, and the decoder then reads no samples.
    # The meter's copy is raw samples, which have no header to get wrong.
    with tempfile.TemporaryDirectory() as tmp:
        path = pathlib.Path(tmp) / "chunk.wav"
        seconds = seconds or config.CHUNK_SECONDS
        meter = []
        if on_level:
            meter = ["-t", str(seconds), "-ac", "1", "-ar", "16000", "-f", "s16le", "pipe:1"]
        try:
            code, err = await _ffmpeg(
                *input_args(backend, device, url),
                "-t", str(seconds), "-ac", "1", "-ar", "16000", "-y", str(path), *meter,
                timeout=seconds + FFMPEG_GRACE_SECONDS,
                on_stdout=LevelBlocks(on_level).feed if on_level else None)
        except RuntimeError as e:
            raise RuntimeError(rooms.redact(str(e), url)) from None
        data = path.read_bytes() if path.exists() else b""
        if code != 0 or not data:
            if url:
                raise RuntimeError(rooms.redact(await _explain_stream_error(err, url), url))
            raise RuntimeError(f"ffmpeg capture failed: {short_error(err)}")
        return data


def short_error(err: str) -> str:
    """The line of ffmpeg's log that says what went wrong. The last lines only
    repeat that opening the input failed, and ALSA's own lines are noise."""
    lines = [re.sub(r"^\[[^\]]*\]\s*", "", line).strip()
             for line in err.splitlines() if line.strip()]
    useful = [line for line in lines
              if not line.startswith(("Error opening input", "ALSA lib"))]
    return ((useful or lines or ["no output"])[0])[:300]


# A refusal the camera named. Anything else may be a camera with its audio
# turned off: asked for audio only, it has nothing to play and fails strangely.
_NAMED_FAILURE = re.compile(r"\b(401|403|404)\b|refused|timed out|resolve|unreachable",
                            re.IGNORECASE)


async def _explain_stream_error(err: str, url: str) -> str:
    """One readable line for a stream that failed, rather than ffmpeg's log."""
    last = short_error(err)
    if _NAMED_FAILURE.search(err):
        return f"the camera refused: {last}"
    # Open the whole stream once and ask for its first audio track.
    args = [a for a in input_args(url=url) if a not in ("-allowed_media_types", "audio")]
    try:
        code, probe = await _ffmpeg(*args, "-map", "0:a:0", "-t", "0.5", "-f", "null", "-",
                                    timeout=FFMPEG_GRACE_SECONDS)
    except RuntimeError:
        return f"the stream failed: {last}"
    if code != 0 and "matches no streams" in probe:
        return "the stream has no audio track; turn on the camera's audio"
    return f"the stream failed: {last}"


async def probe_level(backend=None, device=None, url=None, seconds=3) -> float:
    """Record a short clip from a device or stream and report its peak, to check
    that it hears anything before committing to it."""
    return peak_dbfs(await capture_chunk(seconds, backend, device, url))


async def decode_to_wav(data: bytes) -> bytes:
    """Convert any browser/upload audio (webm, ogg, mp4) to the WAV the matcher wants.

    Output goes to a temp file, never a pipe: a piped WAV carries 0xFFFFFFFF RIFF
    sizes and decodes to nothing.
    """
    with tempfile.TemporaryDirectory() as tmp:
        path = pathlib.Path(tmp) / "in.wav"
        code, err = await _ffmpeg(
            "-i", "pipe:0", "-t", str(segment_seconds(config.CHUNK_SECONDS)),
            "-ac", "1", "-ar", "16000", "-y", str(path),
            data=data, timeout=FFMPEG_GRACE_SECONDS + 10)
        wav = path.read_bytes() if path.exists() else b""
        if code != 0 or not wav:
            raise ValueError(f"could not decode audio: {err[-200:]}")
        return wav


# While a room keeps failing, remind the log this often that it still is.
OUTAGE_REMINDER_SECONDS = 15 * 60
# ...and wait longer between tries, doubling up to this, so a camera that is
# rebooting is not asked every few seconds. A returning one is still found
# within this long.
OUTAGE_RETRY_MAX_SECONDS = 60


def _minutes(seconds) -> str:
    minutes = round(seconds / 60)
    return f"{minutes} min" if minutes else f"{round(seconds)} s"


class Outage:
    """A room whose cycles keep failing, such as a camera that is offline.

    The log gets one warning when it starts, a reminder every
    OUTAGE_REMINDER_SECONDS, a line for each new reason, and one line when the
    room works again, rather than a warning every cycle.
    """

    def __init__(self):
        self.since = None  # unix time of the first failure
        self.tries = 0
        self._reminded = None
        self._reasons = set()

    def failed(self, reason, now) -> tuple[str, str] | None:
        """(level, message) to log for this failure, or None to stay quiet."""
        self.tries += 1
        if self.since is None:
            self.since = self._reminded = now
            self._reasons = {reason}
            return "warning", f"failing: {reason}. Retrying quietly; will log when it works again"
        if reason not in self._reasons:
            self._reasons.add(reason)
            return "info", f"still failing, now: {reason}"
        if now - self._reminded >= OUTAGE_REMINDER_SECONDS:
            self._reminded = now
            return "warning", (f"still failing after {_minutes(now - self.since)}"
                               f" ({self.tries} tries)")
        return None

    def recovered(self, now) -> str | None:
        """The message for the first success after failures, or None."""
        if self.since is None:
            return None
        message = f"working again after {_minutes(now - self.since)}"
        self.since, self.tries, self._reminded, self._reasons = None, 0, None, set()
        return message

    def retry_after(self, interval) -> float:
        """Seconds from one try to the next: the usual interval at first,
        then doubling, up to OUTAGE_RETRY_MAX_SECONDS."""
        if self.tries <= 1:
            return interval
        return max(interval, min(interval * 2 ** (self.tries - 1), OUTAGE_RETRY_MAX_SECONDS))


class Room:
    """One input, and what it last heard. Everybody signed in may see this."""

    def __init__(self, row):
        self.id = row["id"]
        self.update(row)
        self.task = None
        self.started_at = None
        self.last_match = None
        self.song_since = None  # unix time the room started hearing last_match's song
        self.reset_input()
        self.last_attempt = None
        self.attempts = 0
        # Shazam is not asked again while the room hears a song it knows,
        # see known_for().
        self.known_key = None
        self.known_until = 0.0  # monotonic
        self.skipped = 0

    def update(self, row) -> bool:
        """Take the saved settings. True when the input itself changed."""
        before = getattr(self, "_input", None)
        self.name = row["name"]
        self.kind = row["kind"]
        self.enabled = bool(row["enabled"])
        self.url = row["url"] if row["kind"] == "stream" else None
        self.backend = row["backend"]
        self.device = row["device"]
        self._input = (self.kind, self.backend, self.device, self.url)
        return before is not None and before != self._input

    def reset_input(self):
        """Forget what the previous input reported, after it changed."""
        self.outage = Outage()
        self.known_key = None
        self.known_until = 0.0
        self.last_level_db = None
        self.last_floor_db = None
        self.quiet = False  # the last clip was noise below QUIET_DBFS
        self._live_levels = collections.deque()  # (monotonic, dBFS) per block
        self.last_error = None
        self._warned_silent = False

    def heard_level(self, db: float):
        now = time.monotonic()
        self._live_levels.append((now, db))
        while self._live_levels[0][0] < now - LEVEL_STALE_SECONDS:
            self._live_levels.popleft()

    def live_level_db(self) -> float | None:
        """The loudest block of the last LEVEL_SHOW_SECONDS while recording,
        or None between recordings."""
        now = time.monotonic()
        if not self._live_levels or self._live_levels[-1][0] < now - LEVEL_STALE_SECONDS:
            return None
        recent = [db for t, db in self._live_levels if t >= now - LEVEL_SHOW_SECONDS]
        return max(recent or [self._live_levels[-1][1]])

    def capture_args(self) -> dict:
        if self.kind == "stream":
            return {"url": self.url}
        return {"backend": self.backend or config.AUDIO_BACKEND,
                "device": self.device or config.AUDIO_DEVICE}

    def describe(self) -> str:
        if self.kind == "stream":
            return rooms.mask_url(self.url)
        args = self.capture_args()
        return f"{args['backend']} {args['device']}"

    @property
    def running(self) -> bool:
        return self.task is not None and not self.task.done()

    async def stop(self):
        if self.task:
            self.task.cancel()
            try:
                await self.task
            except asyncio.CancelledError:
                pass
            self.task = None


class Listener:
    def __init__(self):
        # Always the 12s window, even for a shorter CHUNK_SECONDS, which is then
        # fingerprinted whole. Any other window makes shazamio warn.
        self._shazam = Shazam(segment_duration_seconds=MAX_SEGMENT_SECONDS,
                              http_client=ShazamClient())
        if config.CHUNK_SECONDS > MAX_SEGMENT_SECONDS:
            log.warning("CHUNK_SECONDS=%s, but only the middle %ss is fingerprinted",
                        config.CHUNK_SECONDS, MAX_SEGMENT_SECONDS)
        self.rooms = {}  # room id -> Room
        self.running = False  # an administrator started listening
        self.started_at = None
        # One queue to Shazam for every room and browser clip.
        self._shazam_lock = asyncio.Lock()
        self._last_request = 0.0  # monotonic
        self._blocked_until = 0.0  # monotonic, set by a 429
        self._backoff = 0  # seconds, grows while Shazam answers 429
        self._requests = collections.deque()  # monotonic time of each request
        self.last_rate_limited = None  # unix time of the last 429
        # Set and cleared at once, to wake every room's wait after a rate change.
        self._rates_changed = asyncio.Event()
        # (user_id, track_key) -> (ts, offset), kept in the database too, see
        # restore_plays()
        self._last_plays = {}
        self._now_playing = {}  # user_id -> (track_key, ts) of the last mark
        self._last_match_at = {}  # user_id -> when their audio was last identified
        self.browser_matches = {}  # user_id -> their own last browser clip

    def restore_plays(self):
        """Read back the plays scrobbled before a restart. Without them, a
        restart in the middle of a song scrobbles it a second time."""
        keep_s = play_memory_seconds(config.RESCROBBLE_MINUTES * 60)
        self._last_plays = db.load_plays(int(time.time() - keep_s))

    @property
    def listening(self) -> bool:
        return any(room.running for room in self.rooms.values())

    def first_room(self):
        return self.rooms[min(self.rooms)] if self.rooms else None

    async def sync_rooms(self):
        """Bring the running loops in line with the saved rooms, after a change.

        A changed input takes effect at the room's next capture, so its loop
        keeps running. A removed or turned off room stops.
        """
        saved = {row["id"]: row for row in rooms.list_rooms()}
        for room_id in [r for r in self.rooms if r not in saved]:
            await self.rooms.pop(room_id).stop()
        for room_id, row in saved.items():
            room = self.rooms.get(room_id)
            if room is None:
                room = self.rooms[room_id] = Room(row)
            elif room.update(row):
                room.reset_input()
            if self.running and room.enabled:
                self._start_room(room)
            elif room.running:
                await room.stop()
        if not self.listening:
            await self._clear_all()
        events.emit("rooms")

    def _start_room(self, room):
        if not room.running:
            room.started_at = int(time.time())
            room.task = asyncio.create_task(self._loop(room))

    def start(self):
        """Listen in every room that is turned on."""
        if not self.running:
            self.started_at = int(time.time())
        self.running = True
        for room in self.rooms.values():
            if room.enabled:
                self._start_room(room)
        events.emit("listening", on=True)

    async def stop(self):
        self.running = False
        for room in self.rooms.values():
            await room.stop()
        await self._clear_all()
        events.emit("listening", on=False)

    async def _clear_all(self):
        """Stopping on purpose means nothing is playing."""
        marks, self._now_playing = list(self._now_playing), {}
        if marks:
            events.emit("cleared", users=marks)
        for user_id in marks:
            await scrobble.clear_now_playing_all(accounts.get_credentials(user_id))

    def rates_changed(self):
        """Wake every room between cycles, so a new interval applies now rather
        than after the old, possibly long, wait."""
        self._rates_changed.set()
        self._rates_changed.clear()
        events.emit("rates")

    def save_rates(self, gap=None, interval=None):
        """Keep rates chosen by an administrator, which outrank the environment,
        and apply them now. None keeps a value. The caller checks the ranges."""
        for key, value in (("recognition_gap_seconds", gap), ("match_interval", interval)):
            if value is not None:
                db.set_setting(key, value)
        config.set_rates(gap, interval)
        self.rates_changed()

    def reset_rates(self):
        """Forget the rates chosen by an administrator, back to the environment."""
        for key in ("recognition_gap_seconds", "match_interval"):
            db.set_setting(key, None)
        config.set_rates(config.ENV_RECOGNITION_GAP_SECONDS, config.ENV_MATCH_INTERVAL)
        self.rates_changed()

    def requests_recently(self) -> int:
        """Shazam requests in the last REQUEST_WINDOW_SECONDS, for tuning."""
        cutoff = time.monotonic() - REQUEST_WINDOW_SECONDS
        while self._requests and self._requests[0] < cutoff:
            self._requests.popleft()
        return len(self._requests)

    def rate_limited_for(self) -> float:
        """Seconds until Shazam may be asked again after a 429, or 0."""
        return max(0.0, self._blocked_until - time.monotonic())

    async def recognize(self, wav: bytes):
        """Ask Shazam, one request at a time and no closer together than
        RECOGNITION_GAP_SECONDS, so adding rooms does not add to the rate."""
        async with self._shazam_lock:
            wait = self.rate_limited_for()
            if wait:
                raise RateLimited(f"Shazam is limiting requests; trying again in {wait:.0f}s")
            gap = self._last_request + config.RECOGNITION_GAP_SECONDS - time.monotonic()
            if gap > 0:
                await asyncio.sleep(gap)
            self._last_request = time.monotonic()
            self._requests.append(self._last_request)
            self.requests_recently()  # keeps the window from growing
            try:
                result = await self._shazam.recognize(wav)
            except RateLimited as e:
                self.last_rate_limited = int(time.time())
                self._backoff = rate_limit_wait(self._backoff)
                self._blocked_until = time.monotonic() + self._backoff
                raise RateLimited(f"{e}; trying again in {self._backoff:.0f}s") from None
            self._backoff = 0
            return result

    async def _loop(self, room):
        log.info("listening in %s: %s, one match per %ss",
                 room.name, room.describe(), config.MATCH_INTERVAL)
        while True:
            cycle_start = time.monotonic()
            try:
                wait = self.rate_limited_for()
                if wait:
                    # Recording now would only be thrown away.
                    await asyncio.sleep(wait)
                    continue
                wav = await capture_chunk(**room.capture_args(), on_level=room.heard_level)
                room.attempts += 1
                room.last_attempt = int(time.time())
                room.last_level_db = peak_dbfs(wav)
                room.last_floor_db = floor_dbfs(wav)
                silent = room.last_level_db < SILENT_DBFS
                quiet = not silent and is_quiet(room.last_floor_db, config.QUIET_DBFS)
                if quiet and not room.quiet:
                    log.info("%s is quiet (floor %.0f dBFS), skipping recognition until"
                             " it plays again", room.name, room.last_floor_db)
                room.quiet = quiet
                if silent or quiet:
                    room.known_key, room.known_until = None, 0.0  # the song ended
                    # Nothing audible: skip the request instead of spending quota on silence.
                    if silent and not room._warned_silent:
                        log.warning("%s is silent (%.0f dBFS), skipping recognition -"
                                    " is its input %s correct?",
                                    room.name, room.last_level_db, room.describe())
                        room._warned_silent = True
                elif time.monotonic() < room.known_until:
                    # Still the song it matched twice: save the request.
                    room._warned_silent = False
                    room.skipped += 1
                else:
                    room._warned_silent = False
                    info = await self.match_bytes(wav, room=room)
                    room.last_error = None
                    self._remember_song(room, info, cycle_start)
                await self._clear_if_stopped()
                back = room.outage.recovered(int(time.time()))
                if back:
                    room.last_error = None
                    log.info("%s: %s", room.name, back)
                    events.emit("rooms")
            except asyncio.CancelledError:
                raise
            except RateLimited as e:
                room.last_error = str(e)
                log.warning("%s: %s", room.name, e)
                continue
            except Exception as e:
                room.last_error = rooms.redact(str(e), room.url)[:300]
                said = room.outage.failed(room.last_error, int(time.time()))
                if said:
                    getattr(log, said[0])("%s: %s", room.name, said[1])
                    events.emit("rooms")
                else:
                    log.debug("%s: cycle failed: %s", room.name, room.last_error)
                await self._until_next_cycle(
                    cycle_start, room.outage.retry_after(config.MATCH_INTERVAL))
                continue
            await self._until_next_cycle(cycle_start)

    def _remember_song(self, room, info, cycle_start):
        isrc = info.get("isrc") if info else None
        if config.SKIP_KNOWN_SECONDS > 0 and isrc and enrich.track_length(isrc) is None:
            enrich.look_up_length(isrc, info["title"], info["album"])
        known = known_for(room.known_key, info, enrich.track_length(isrc),
                          time.monotonic() - cycle_start, config.SKIP_KNOWN_SECONDS)
        room.known_key = info["track_key"] if info else None
        room.known_until = time.monotonic() + known if known else 0.0

    async def _until_next_cycle(self, cycle_start, interval=None):
        """Wait out MATCH_INTERVAL, or `interval`, from the start of the cycle.
        A rate change wakes the wait, which then measures again against the new
        interval."""
        while True:
            left = (interval or config.MATCH_INTERVAL) - (time.monotonic() - cycle_start)
            if left <= 0:
                return
            try:
                await asyncio.wait_for(self._rates_changed.wait(), left)
            except asyncio.TimeoutError:
                return

    def last_match_for(self, user_id):
        """The newest match this user may see: any room's, or their own clip.
        A browser clip belongs to the person who recorded it."""
        seen = [room.last_match for room in self.rooms.values() if room.last_match]
        if self.browser_matches.get(user_id):
            seen.append(self.browser_matches[user_id])
        return max(seen, key=lambda m: m["ts"], default=None)

    async def _clear_if_stopped(self):
        """Drop each user's playing-now mark once their music has stopped.

        Per user, because a browser clip keeps only its own user's mark alive.
        Every room calls this, so the stale marks are taken before the first
        network call, and a second room cannot clear them again.
        """
        now = int(time.time())
        stale = [u for u, mark in list(self._now_playing.items())
                 if should_clear(now, self._last_match_at.get(u), mark,
                                 config.NOW_PLAYING_STOP_SECONDS)]
        if not stale:
            return
        log.info("no match for %ss, clearing the playing-now mark",
                 config.NOW_PLAYING_STOP_SECONDS)
        for user_id in stale:
            self._now_playing.pop(user_id, None)
        events.emit("cleared", users=stale)
        for user_id in stale:
            await scrobble.clear_now_playing_all(accounts.get_credentials(user_id))

    async def match_bytes(self, wav: bytes, source: str = "server",
                          users=None, room=None) -> dict | None:
        """Identify WAV bytes and scrobble for each given user.

        A room passes the users who chose to receive it. The browser endpoint
        passes only the user who recorded the clip, because that listen is
        theirs alone.
        """
        info = parse_track(await self.recognize(wav))
        if not (info and info["track_key"]):
            return None
        if users is None:
            users = rooms.members(room.id) if room else []
        await self._on_match(info, source, users, room)
        return info

    async def _on_match(self, info, source, users, room=None):
        now = int(time.time())
        match = {**info, "ts": now, "source": source,
                 "room": room.name if room else None, "room_id": room.id if room else None}
        if room is None:
            for user in users:
                self.browser_matches[user["id"]] = match
        else:
            before = room.last_match
            if not (before and before["track_key"] == info["track_key"]
                    and now - before["ts"] < config.NOW_PLAYING_STOP_SECONDS):
                room.song_since = now
            room.last_match = match
        for user in users:
            self._last_match_at[user["id"]] = now
        events.emit("match", room_id=room.id if room else None,
                    users=[user["id"] for user in users], match=match)
        forget_old_plays(self._last_plays, now, config.RESCROBBLE_MINUTES * 60)
        match_id = None  # written once, and only when somebody scrobbles
        extra = None  # looked up once, and only for somebody who can receive it

        for user in users:
            credentials = accounts.get_credentials(user["id"])
            services = scrobble.enabled_services(credentials)
            if not services:
                continue
            if extra is None and {"listenbrainz", "maloja"} & set(services):
                extra = await enrich.musicbrainz_ids(info.get("isrc"), info["title"],
                                                     info["album"])
                if info.get("isrc"):
                    extra = {**extra, "isrc": info["isrc"]}

            # Show it as playing now, even while the same play continues and no
            # new scrobble is due.
            if should_announce(info["track_key"], now, self._now_playing.get(user["id"])):
                self._now_playing[user["id"]] = (info["track_key"], now)
                await scrobble.now_playing_all(credentials, info["artist"],
                                               info["title"], info["album"], extra)

            # Per user, not per room, so a song that two rooms hear at once is
            # still one play.
            key = (user["id"], info["track_key"])
            if not should_scrobble(key, now, info.get("offset"), self._last_plays,
                                   config.RESCROBBLE_MINUTES * 60):
                continue
            self._last_plays[key] = (now, info.get("offset"))
            db.save_play(user["id"], info["track_key"], now, info.get("offset"),
                         int(now - play_memory_seconds(config.RESCROBBLE_MINUTES * 60)))
            results = await scrobble.scrobble_all(credentials, info["artist"],
                                                  info["title"], info["album"], now,
                                                  extra)
            if results.get("maloja") == "ok":
                enrich.maloja_cover(credentials["maloja"], info["artist"],
                                    info["title"], info["album"], info["art_url"])
            if match_id is None:
                match_id = db.add_match(now, info["artist"], info["title"],
                                        info["album"], info["track_key"],
                                        info["art_url"], source,
                                        room.name if room else None)
            db.add_scrobbles(match_id, user["id"], results)
            events.emit("scrobble", user_id=user["id"], match_id=match_id, results=results)
            log.info("scrobbled for %s%s: %s - %s -> %s", user["username"],
                     f" from {room.name}" if room else "",
                     info["artist"], info["title"], results)


async def _main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    if len(sys.argv) > 1:
        data = pathlib.Path(sys.argv[1]).read_bytes()
        async with Shazam(http_client=ShazamClient()) as shazam:
            print(parse_track(await shazam.recognize(data)) or "no match")
        return
    db.init()
    accounts.ensure_first_user()
    listener = Listener()
    listener.restore_plays()
    await listener.sync_rooms()
    listener.start()
    await asyncio.gather(*(room.task for room in listener.rooms.values() if room.task))


if __name__ == "__main__":
    asyncio.run(_main())
