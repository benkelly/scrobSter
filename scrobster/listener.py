"""Capture loops: ffmpeg chunk -> shazamio match -> dedup -> store -> scrobble.

Each room runs its own loop. They share one Shazam queue, because every request
leaves from the same address and the rate limit counts them all.

CLI:
  python -m scrobster.listener song.wav   # match one file, print, exit
  python -m scrobster.listener            # run the capture loop headless
"""
import array
import asyncio
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

from . import accounts, config, db, enrich, rooms, scrobble

log = logging.getLogger("scrobster")

# Measured against known audio (Shazam matched 8-14s windows, failed at 16s and 20s).
# A longer capture is still useful, but never fingerprint more than this.
MAX_SEGMENT_SECONDS = 14
# Below this peak there is nothing to identify, so skip the request and save quota.
SILENT_DBFS = -70.0
# Give up on one Shazam request after this long.
SHAZAM_TIMEOUT_SECONDS = 30
# ffmpeg gets this long beyond the audio it records before it counts as stuck.
FFMPEG_GRACE_SECONDS = 20


class RateLimited(RuntimeError):
    """Shazam answered 429 Too Many Requests."""


class ShazamClient(HTTPClientInterface):
    """Ask Shazam once, with a timeout.

    shazamio's own client retries a 429 twenty times over about twelve minutes.
    That stalls capture and browser clips the whole time, makes the rate limit
    worse, and ends in "Failed to decode json". The capture loop backs off
    instead, see rate_limit_wait().
    """

    async def request(self, method, url, *args, **kwargs):
        timeout = aiohttp.ClientTimeout(total=SHAZAM_TIMEOUT_SECONDS)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.request(method, url, **kwargs) as r:
                if r.status == 429:
                    raise RateLimited("Shazam is limiting requests from this address")
                if r.status >= 400:
                    raise RuntimeError(f"Shazam answered HTTP {r.status}")
                return await r.json(content_type=None)


def rate_limit_wait(previous: float) -> float:
    """Seconds to wait after a 429: a minute, doubling to ten while it lasts."""
    return min(max(previous * 2, 60), 600)


def segment_seconds(chunk_seconds: int) -> int:
    """Fingerprint window to ask for. Capped because oversized signatures never match."""
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


def forget_old_plays(last_plays, now, fallback_cooldown_s):
    """Drop plays too old for should_scrobble() to answer no, so the duplicate
    filter does not keep every track ever heard until a restart.

    Takes the same cooldown as should_scrobble(). Adding a day rather than taking
    the longer of the two also covers a match judged with an earlier `now`,
    while a browser clip prunes during its network calls.
    """
    keep_s = fallback_cooldown_s + PLAY_MEMORY_SECONDS
    for key in [k for k, (ts, _) in last_plays.items() if now - ts >= keep_s]:
        del last_plays[key]


def peak_dbfs(wav: bytes) -> float:
    """Peak level of 16-bit WAV bytes, in dBFS. Digital silence returns -99.0.

    A silent input device matches nothing and looks identical to "music not
    recognised", so the loop reports this instead of failing quietly.
    """
    with wave.open(io.BytesIO(wav)) as w:
        frames = w.readframes(w.getnframes())
    samples = array.array("h")
    samples.frombytes(frames[: len(frames) - len(frames) % 2])
    peak = max(max(samples), -min(samples)) if samples else 0
    return round(20 * math.log10(peak / 32768), 1) if peak > 0 else -99.0


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
    with a star on the default:
        * default [Playback/recording through the PulseAudio sound server]
          hw:CARD=PCH,DEV=0 [HDA Intel PCH, ALC3232 Analog]
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
        m = re.match(r"^\s*(\*?)\s*(\S+)\s+\[(.*)\]\s*$", line)
        if m and not line.lower().startswith("auto-detected"):
            found.append({"device": m.group(2), "label": m.group(3).strip(),
                          "default": m.group(1) == "*"})
    return found


def parse_proc_asound_pcm(text: str) -> list[dict]:
    """Capture devices from /proc/asound/pcm, for a Linux box with no
    `ffmpeg -sources` support and no alsa-utils. One line per PCM:
        00-00: ALC3232 Analog : ALC3232 Analog : playback 1 : capture 1
    """
    found = []
    for line in text.splitlines():
        head, _, rest = line.partition(":")
        if "capture" not in rest or "-" not in head:
            continue
        card, _, dev = head.strip().partition("-")
        name = rest.split(":")[0].strip()
        found.append({"device": f"hw:{int(card)},{int(dev)}", "label": name})
    return found


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
    if not found and backend == "alsa":
        try:
            found = parse_proc_asound_pcm(pathlib.Path("/proc/asound/pcm").read_text())
        except OSError:
            pass
    return found


async def _ffmpeg(*argv, data=None, timeout):
    """Run ffmpeg and return (returncode, stderr).

    An input that opens but never delivers audio would otherwise block for good.
    A cancelled capture would leave ffmpeg holding the device, so it is killed
    on the way out whatever happened.
    """
    proc = await asyncio.create_subprocess_exec(
        "ffmpeg", "-hide_banner", "-loglevel", "error", *argv,
        stdin=asyncio.subprocess.PIPE if data is not None else asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE,
    )
    try:
        _, err = await asyncio.wait_for(proc.communicate(data), timeout)
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


async def capture_chunk(seconds=None, backend=None, device=None, url=None) -> bytes:
    """Record one chunk from a device, or from a stream when `url` is set. Left
    out, the device is the configured one."""
    # ponytail: one ffmpeg spawn per chunk; self-heals when the device drops.
    # Write a temp file, never a pipe: piped WAV gets 0xFFFFFFFF RIFF sizes because
    # ffmpeg cannot seek back to patch them, and the decoder then reads no samples.
    with tempfile.TemporaryDirectory() as tmp:
        path = pathlib.Path(tmp) / "chunk.wav"
        seconds = seconds or config.CHUNK_SECONDS
        try:
            code, err = await _ffmpeg(
                *input_args(backend, device, url),
                "-t", str(seconds), "-ac", "1", "-ar", "16000", "-y", str(path),
                timeout=seconds + FFMPEG_GRACE_SECONDS)
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


class Room:
    """One input, and what it last heard. Everybody signed in may see this."""

    def __init__(self, row):
        self.id = row["id"]
        self.update(row)
        self.task = None
        self.started_at = None
        self.last_match = None
        self.reset_input()
        self.last_attempt = None
        self.attempts = 0

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
        self.last_level_db = None
        self.last_error = None
        self._warned_silent = False

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
        self._shazam = Shazam(segment_duration_seconds=segment_seconds(config.CHUNK_SECONDS),
                              http_client=ShazamClient())
        if config.CHUNK_SECONDS > MAX_SEGMENT_SECONDS:
            log.warning("CHUNK_SECONDS=%s, but only %ss is fingerprinted; longer windows"
                        " stop matching", config.CHUNK_SECONDS, MAX_SEGMENT_SECONDS)
        self.rooms = {}  # room id -> Room
        self.running = False  # an administrator started listening
        self.started_at = None
        # One queue to Shazam for every room and browser clip.
        self._shazam_lock = asyncio.Lock()
        self._last_request = 0.0  # monotonic
        self._blocked_until = 0.0  # monotonic, set by a 429
        self._backoff = 0  # seconds, grows while Shazam answers 429
        # (user_id, track_key) -> (ts, offset); in memory, a restart forgets
        self._last_plays = {}
        self._now_playing = {}  # user_id -> (track_key, ts) of the last mark
        self._last_match_at = {}  # user_id -> when their audio was last identified
        self.browser_matches = {}  # user_id -> their own last browser clip

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

    async def stop(self):
        self.running = False
        for room in self.rooms.values():
            await room.stop()
        await self._clear_all()

    async def _clear_all(self):
        """Stopping on purpose means nothing is playing."""
        marks, self._now_playing = list(self._now_playing), {}
        for user_id in marks:
            await scrobble.clear_now_playing_all(accounts.get_credentials(user_id))

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
            try:
                result = await self._shazam.recognize(wav)
            except RateLimited as e:
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
                wav = await capture_chunk(**room.capture_args())
                room.attempts += 1
                room.last_attempt = int(time.time())
                room.last_level_db = peak_dbfs(wav)
                if room.last_level_db < SILENT_DBFS:
                    # Nothing audible: skip the request instead of spending quota on silence.
                    if not room._warned_silent:
                        log.warning("%s is silent (%.0f dBFS), skipping recognition -"
                                    " is its input %s correct?",
                                    room.name, room.last_level_db, room.describe())
                        room._warned_silent = True
                else:
                    room._warned_silent = False
                    await self.match_bytes(wav, room=room)
                    room.last_error = None
                await self._clear_if_stopped()
            except asyncio.CancelledError:
                raise
            except RateLimited as e:
                room.last_error = str(e)
                log.warning("%s: %s", room.name, e)
                continue
            except Exception as e:
                room.last_error = rooms.redact(str(e), room.url)[:300]
                log.warning("%s: cycle failed: %s", room.name, room.last_error)
            elapsed = time.monotonic() - cycle_start
            await asyncio.sleep(max(0, config.MATCH_INTERVAL - elapsed))

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
            room.last_match = match
        for user in users:
            self._last_match_at[user["id"]] = now
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
            log.info("scrobbled for %s%s: %s - %s -> %s", user["username"],
                     f" from {room.name}" if room else "",
                     info["artist"], info["title"], results)


async def _main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    if len(sys.argv) > 1:
        data = pathlib.Path(sys.argv[1]).read_bytes()
        print(parse_track(await Shazam().recognize(data)) or "no match")
        return
    db.init()
    accounts.ensure_first_user()
    listener = Listener()
    await listener.sync_rooms()
    listener.start()
    await asyncio.gather(*(room.task for room in listener.rooms.values() if room.task))


if __name__ == "__main__":
    asyncio.run(_main())
