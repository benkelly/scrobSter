"""What a display by the hi-fi shows: the song a room plays, and its cover.

A display asks often, so the cover is fetched once per song and kept in
memory, and each answer carries an ETag, so asking again costs a 304.
"""
import asyncio
import collections
import hashlib
import logging
import re
import struct
import time
import zlib

import aiohttp

from . import config, enrich
from .listener import SILENT_DBFS

log = logging.getLogger("scrobster")

# What ?size= may ask for, in pixels.
SIZE_RANGE = (32, 1200)
# Covers kept in memory. Each is a few hundred kB at most sizes.
CACHE_SIZE = 16
# A cover that could not be fetched is not asked for again this soon, so a
# display asking every few seconds does not ask Apple every few seconds too.
RETRY_SECONDS = 60

# Shazam's covers come from Apple, and the last part of the URL is the size:
# https://is1-ssl.mzstatic.com/image/thumb/Music123/v4/0c/72/a1/.../19UMGIM66006.rgb.jpg/400x400cc.jpg
_APPLE = re.compile(r"^(https://[a-z0-9-]+\.mzstatic\.com/image/thumb/.+/)"
                    r"\d+x\d+([a-z]{0,2}(?:-\d+)?\.(?:jpg|jpeg|png|webp))$")

_covers = collections.OrderedDict()  # url -> (content type, bytes, etag)
_failed = {}  # url -> monotonic time of the last failure
_fetching = {}  # url -> the task fetching it, shared by every display asking
_idle = {}  # size -> PNG bytes


def playing(room, now) -> bool:
    """True while a room plays what it last matched.

    The same rule as the room's MQTT `playing`, with two more ways to stop at
    once: the room stopped listening, or its last clip was silence, or below
    QUIET_DBFS, which is how a record side ends.
    """
    match = room.last_match
    if not (match and room.running):
        return False
    if room.quiet or (room.last_level_db is not None and room.last_level_db < SILENT_DBFS):
        return False
    # A song the room knows is not asked about, so its match is not renewed.
    return (now - match["ts"] < config.NOW_PLAYING_STOP_SECONDS
            or time.monotonic() < room.known_until)


def art_key(art_url) -> str | None:
    """Changes with the cover, so a page can tell a new one from the URL."""
    return hashlib.sha1(art_url.encode()).hexdigest()[:12] if art_url else None


def now_playing(room, now) -> dict:
    """What /api/rooms/{id}/now answers. The song only while it plays."""
    on = playing(room, now)
    match = room.last_match if on else None
    art = match.get("art_url") if match else None
    return {"room_id": room.id, "room": room.name, "playing": on,
            "artist": match["artist"] if match else None,
            "title": match["title"] if match else None,
            "album": match.get("album") if match else None,
            "since": room.song_since if match else None,
            "heard": match["ts"] if match else None,
            "art": f"/api/rooms/{room.id}/cover" if art else None,
            "art_key": art_key(art)}


def latest_room(rooms, now):
    """The room that plays and matched last, or None."""
    on = [room for room in rooms if playing(room, now)]
    return max(on, key=lambda room: room.last_match["ts"], default=None)


def sized_url(url, size):
    """Apple's cover URL at `size` pixels square. Another URL, or no size,
    comes back unchanged and is served as it is."""
    found = _APPLE.match(url or "") if size else None
    return f"{found[1]}{size}x{size}{found[2]}" if found else url


async def _fetch(url):
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15),
                                     headers={"User-Agent": enrich.USER_AGENT}) as s:
        kind, image = await enrich.fetch_image(s, url)
    etag = '"' + hashlib.sha1(image).hexdigest()[:16] + '"'
    return kind, image, etag


async def cover(url):
    """(content type, bytes, etag) of the cover at url, or None when it could
    not be fetched."""
    if url in _covers:
        _covers.move_to_end(url)
        return _covers[url]
    if time.monotonic() - _failed.get(url, -RETRY_SECONDS) < RETRY_SECONDS:
        return None
    task = _fetching.get(url)
    if task is None:
        task = _fetching[url] = asyncio.ensure_future(_fetch(url))
        task.add_done_callback(lambda _: _fetching.pop(url, None))
    try:
        found = await asyncio.shield(task)
    except Exception as e:
        log.debug("cover %s could not be fetched: %s", url, e or type(e).__name__)
        if len(_failed) > CACHE_SIZE * 4:
            _failed.clear()
        _failed[url] = time.monotonic()
        return None
    _failed.pop(url, None)
    _covers[url] = found
    while len(_covers) > CACHE_SIZE:
        _covers.popitem(last=False)
    return found


async def room_cover(art_url, size=None):
    """The cover at `size`, or as Shazam gave it when that size fails."""
    sized = sized_url(art_url, size)
    found = await cover(sized)
    if found is None and sized != art_url:
        found = await cover(art_url)
    return found


def _chunk(kind, data) -> bytes:
    return (struct.pack(">I", len(data)) + kind + data
            + struct.pack(">I", zlib.crc32(kind + data) & 0xffffffff))


def idle_png(size=400) -> bytes:
    """A black square, for a display that wants a picture even when nothing
    plays. Black, because a lit pixel that never changes burns into an OLED."""
    if size not in _idle:
        # One greyscale byte per pixel, after each row's filter byte.
        rows = (b"\x00" + b"\x00" * size) * size
        _idle[size] = (b"\x89PNG\r\n\x1a\n"
                       + _chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 0, 0, 0, 0))
                       + _chunk(b"IDAT", zlib.compress(rows, 9))
                       + _chunk(b"IEND", b""))
    return _idle[size]
