"""Capture loop: ffmpeg chunk -> shazamio match -> dedup -> store -> scrobble.

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
import sys
import tempfile
import time
import wave

from shazamio import Shazam

from . import accounts, config, db, scrobble

log = logging.getLogger("scrobster")

# Measured against known audio (Shazam matched 8-14s windows, failed at 16s and 20s).
# A longer capture is still useful, but never fingerprint more than this.
MAX_SEGMENT_SECONDS = 14
# Below this peak there is nothing to identify, so skip the request and save quota.
SILENT_DBFS = -70.0


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
        # position inside the track; used to tell a repeat from a continuing play
        "offset": matches[0].get("offset") if matches else None,
    }


async def capture_chunk() -> bytes:
    # ponytail: one ffmpeg spawn per chunk; self-heals when the device drops.
    # Write a temp file, never a pipe: piped WAV gets 0xFFFFFFFF RIFF sizes because
    # ffmpeg cannot seek back to patch them, and the decoder then reads no samples.
    with tempfile.TemporaryDirectory() as tmp:
        path = pathlib.Path(tmp) / "chunk.wav"
        proc = await asyncio.create_subprocess_exec(
            "ffmpeg", "-hide_banner", "-loglevel", "error",
            "-f", config.AUDIO_BACKEND, "-i", config.AUDIO_DEVICE,
            "-t", str(config.CHUNK_SECONDS), "-ac", "1", "-ar", "16000",
            "-y", str(path),
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE,
        )
        _, err = await proc.communicate()
        data = path.read_bytes() if path.exists() else b""
        if proc.returncode != 0 or not data:
            raise RuntimeError(f"ffmpeg capture failed: {err.decode(errors='replace')[-300:]}")
        return data


async def decode_to_wav(data: bytes) -> bytes:
    """Convert any browser/upload audio (webm, ogg, mp4) to the WAV the matcher wants.

    Output goes to a temp file, never a pipe: a piped WAV carries 0xFFFFFFFF RIFF
    sizes and decodes to nothing.
    """
    with tempfile.TemporaryDirectory() as tmp:
        path = pathlib.Path(tmp) / "in.wav"
        proc = await asyncio.create_subprocess_exec(
            "ffmpeg", "-hide_banner", "-loglevel", "error",
            "-i", "pipe:0", "-t", str(segment_seconds(config.CHUNK_SECONDS)),
            "-ac", "1", "-ar", "16000", "-y", str(path),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE,
        )
        _, err = await proc.communicate(data)
        wav = path.read_bytes() if path.exists() else b""
        if proc.returncode != 0 or not wav:
            raise ValueError(f"could not decode audio: {err.decode(errors='replace')[-200:]}")
        return wav


class Listener:
    def __init__(self):
        self._task = None
        self._shazam = Shazam(segment_duration_seconds=segment_seconds(config.CHUNK_SECONDS))
        if config.CHUNK_SECONDS > MAX_SEGMENT_SECONDS:
            log.warning("CHUNK_SECONDS=%s, but only %ss is fingerprinted; longer windows"
                        " stop matching", config.CHUNK_SECONDS, MAX_SEGMENT_SECONDS)
        # (user_id, track_key) -> (ts, offset); in memory, a restart forgets
        self._last_plays = {}
        self._now_playing = {}  # user_id -> (track_key, ts) of the last mark
        self._last_match_at = None  # when audio was last identified
        self._warned_silent = False
        self.last_match = None
        self.last_error = None
        self.last_level_db = None
        self.last_attempt = None
        self.attempts = 0
        self.started_at = None

    @property
    def listening(self) -> bool:
        return self._task is not None and not self._task.done()

    def start(self):
        if not self.listening:
            self.started_at = int(time.time())
            self._task = asyncio.create_task(self._loop())

    async def stop(self):
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        # Stopping on purpose means nothing is playing.
        for user_id in list(self._now_playing):
            await scrobble.clear_now_playing_all(accounts.get_credentials(user_id))
        self._now_playing.clear()

    async def _loop(self):
        log.info("listening: %s %s, one match per %ss",
                 config.AUDIO_BACKEND, config.AUDIO_DEVICE, config.MATCH_INTERVAL)
        while True:
            cycle_start = time.monotonic()
            try:
                wav = await capture_chunk()
                self.attempts += 1
                self.last_attempt = int(time.time())
                self.last_level_db = peak_dbfs(wav)
                if self.last_level_db < SILENT_DBFS:
                    # Nothing audible: skip the request instead of spending quota on silence.
                    if not self._warned_silent:
                        log.warning("input is silent (%.0f dBFS), skipping recognition -"
                                    " is AUDIO_DEVICE=%s correct?",
                                    self.last_level_db, config.AUDIO_DEVICE)
                        self._warned_silent = True
                else:
                    self._warned_silent = False
                    await self.match_bytes(wav)
                    self.last_error = None
                await self._clear_if_stopped()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                self.last_error = str(e)[:300]
                log.warning("cycle failed: %s", e)
            elapsed = time.monotonic() - cycle_start
            await asyncio.sleep(max(0, config.MATCH_INTERVAL - elapsed))

    async def _clear_if_stopped(self):
        """Drop the playing-now mark for every user once the music has stopped."""
        if not should_clear(int(time.time()), self._last_match_at, self._now_playing,
                            config.NOW_PLAYING_STOP_SECONDS):
            return
        log.info("no match for %ss, clearing the playing-now mark",
                 config.NOW_PLAYING_STOP_SECONDS)
        for user_id in list(self._now_playing):
            await scrobble.clear_now_playing_all(accounts.get_credentials(user_id))
        self._now_playing.clear()

    async def match_bytes(self, wav: bytes, source: str = "server",
                          users=None) -> dict | None:
        """Identify WAV bytes and scrobble for each given user.

        The capture loop passes the users who opted in to the room microphone.
        The browser endpoint passes only the user who recorded the clip, because
        that listen is theirs alone.
        """
        info = parse_track(await self._shazam.recognize(wav))
        if not (info and info["track_key"]):
            return None
        if users is None:
            users = accounts.room_mic_users()
        await self._on_match(info, source, users)
        return info

    async def _on_match(self, info, source, users):
        now = int(time.time())
        self.last_match = {**info, "ts": now, "source": source}
        self._last_match_at = now
        match_id = None  # written once, and only when somebody scrobbles

        for user in users:
            credentials = accounts.get_credentials(user["id"])
            if not scrobble.enabled_services(credentials):
                continue

            # Show it as playing now, even while the same play continues and no
            # new scrobble is due.
            if should_announce(info["track_key"], now, self._now_playing.get(user["id"])):
                self._now_playing[user["id"]] = (info["track_key"], now)
                await scrobble.now_playing_all(credentials, info["artist"],
                                               info["title"], info["album"])

            key = (user["id"], info["track_key"])
            if not should_scrobble(key, now, info.get("offset"), self._last_plays,
                                   config.RESCROBBLE_MINUTES * 60):
                continue
            self._last_plays[key] = (now, info.get("offset"))
            results = await scrobble.scrobble_all(credentials, info["artist"],
                                                  info["title"], info["album"], now)
            if match_id is None:
                match_id = db.add_match(now, info["artist"], info["title"],
                                        info["album"], info["track_key"],
                                        info["art_url"], source)
            db.add_scrobbles(match_id, user["id"], results)
            log.info("scrobbled for %s: %s - %s -> %s", user["username"],
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
    listener.start()
    await listener._task


if __name__ == "__main__":
    asyncio.run(_main())
