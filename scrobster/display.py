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
_idle = {}  # size -> JPEG bytes


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


# The Huffman tables of the JPEG standard, Annex K, as (code counts by length,
# symbols). Every decoder knows them, ESPHome's included.
_DC_LUMA = ((0, 1, 5, 1, 1, 1, 1, 1, 1, 0, 0, 0, 0, 0, 0, 0), range(12))
_DC_CHROMA = ((0, 3, 1, 1, 1, 1, 1, 1, 1, 1, 1, 0, 0, 0, 0, 0), range(12))
# Only end-of-block is ever written, so only the start of each AC table matters,
# but a decoder reads the whole table, so the whole table is sent.
_AC_LUMA = ((0, 2, 1, 3, 3, 2, 4, 3, 5, 5, 4, 4, 0, 0, 1, 0x7d), bytes.fromhex(
    "01020300041105122131410613516107227114328191a1082342b1c11552d1f02433627282"
    "090a161718191a25262728292a3435363738393a434445464748494a535455565758595a"
    "636465666768696a737475767778797a838485868788898a92939495969798999aa2a3a4"
    "a5a6a7a8a9aab2b3b4b5b6b7b8b9bac2c3c4c5c6c7c8c9cad2d3d4d5d6d7d8d9dae1e2e3"
    "e4e5e6e7e8e9eaf1f2f3f4f5f6f7f8f9fa"))
_AC_CHROMA = ((0, 2, 1, 2, 4, 4, 3, 4, 7, 5, 4, 4, 0, 1, 2, 0x77), bytes.fromhex(
    "000102031104052131061241510761711322328108144291a1b1c109233352f0156272d1"
    "0a162434e125f11718191a262728292a35363738393a434445464748494a535455565758"
    "595a636465666768696a737475767778797a82838485868788898a92939495969798999a"
    "a2a3a4a5a6a7a8a9aab2b3b4b5b6b7b8b9bac2c3c4c5c6c7c8c9cad2d3d4d5d6d7d8d9da"
    "e2e3e4e5e6e7e8e9eaf2f3f4f5f6f7f8f9fa"))
_QUANT = 8  # every coefficient divided by this; a black block has its DC alone


def _codes(table) -> dict:
    """symbol -> (code, length) of a JPEG Huffman table."""
    counts, symbols = table
    out, code, symbols = {}, 0, iter(symbols)
    for length, count in enumerate(counts, 1):
        for _ in range(count):
            out[next(symbols)] = (code, length)
            code += 1
        code <<= 1
    return out


def _segment(marker, data) -> bytes:
    return b"\xff" + bytes([marker]) + struct.pack(">H", len(data) + 2) + data


def idle_jpeg(size=400) -> bytes:
    """A black square, for a display that wants a picture even when nothing
    plays. Black, because a lit pixel that never changes burns into an OLED.

    A JPEG, like the covers, so a client that decodes one format, such as
    ESPHome's online_image, can show both. Each 8x8 block of a black picture
    is a constant: one DC value and no AC, so it is written here directly.
    """
    if size in _idle:
        return _idle[size]
    tables = (_DC_LUMA, _AC_LUMA, _DC_CHROMA, _AC_CHROMA)
    dc_y, ac_y, dc_c, ac_c = (_codes(t) for t in tables)
    # Black is Y 0, so -128 after the level shift, and a DC of 8 x -128.
    # Cb and Cr are 128, so 0. DC is coded as the difference from the
    # block before, so only the first block carries a value.
    first = -1024 // _QUANT
    bits = first.bit_length()
    stream = [dc_y[bits], (first + (1 << bits) - 1, bits), ac_y[0],
              dc_c[0], ac_c[0], dc_c[0], ac_c[0]]
    rest = [dc_y[0], ac_y[0], dc_c[0], ac_c[0], dc_c[0], ac_c[0]]
    blocks = (-(-size // 8)) ** 2
    acc = nbits = 0
    out = bytearray()
    for code, length in stream + rest * (blocks - 1) + [(0x7f, 7)]:  # pad with ones
        acc = (acc << length) | code
        nbits += length
        while nbits >= 8:
            nbits -= 8
            byte = (acc >> nbits) & 0xff
            out.append(byte)
            if byte == 0xff:
                out.append(0)  # stuffed, so it is not read as a marker
        acc &= (1 << nbits) - 1
    dht = b"".join(_segment(0xc4, bytes([kind]) + bytes(counts) + bytes(symbols))
                   for kind, (counts, symbols) in zip((0x00, 0x10, 0x01, 0x11), tables))
    image = (b"\xff\xd8"
             + _segment(0xe0, b"JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00")
             + _segment(0xdb, b"\x00" + bytes([_QUANT]) * 64)
             + _segment(0xc0, struct.pack(">BHHB", 8, size, size, 3)
                        + b"\x01\x11\x00\x02\x11\x00\x03\x11\x00")
             + dht
             + _segment(0xda, b"\x03\x01\x00\x02\x11\x03\x11\x00\x3f\x00")
             + bytes(out) + b"\xff\xd9")
    if len(_idle) >= CACHE_SIZE:
        _idle.clear()
    _idle[size] = image
    return image
