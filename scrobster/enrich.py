"""Details a scrobble can carry beyond artist, title and album.

- MusicBrainz IDs, looked up from the ISRC that Shazam returns. ListenBrainz
  uses them to link a listen to its recording and release, and shows that
  release's cover from the Cover Art Archive. Maloja keeps them too.
- Cover art for Maloja, which accepts an uploaded picture. When Maloja shows a
  placeholder for an album, scrobSter sends it the cover that Shazam returned.

Last.fm and Libre.fm have no way to receive either, so they get neither.
Every step here is best effort: a failure logs at debug level and the scrobble
goes ahead without it.
"""
import asyncio
import base64
import logging
import time

import aiohttp

from . import __version__

log = logging.getLogger("scrobster")

MUSICBRAINZ_URL = "https://musicbrainz.org/ws/2"
# MusicBrainz asks every client to name itself and a way to reach its author.
USER_AGENT = f"scrobSter/{__version__} ( https://github.com/benkelly/scrobster )"
# A cover larger than this is not a cover.
MAX_ART_BYTES = 5 * 1024 * 1024
CACHE_SIZE = 500
MUSICBRAINZ_GAP_SECONDS = 1.1
# Recordings checked for the album's release. Each costs a second of lookup.
MAX_RECORDINGS = 3
# The whole lookup, all its requests together.
MUSICBRAINZ_LIMIT_SECONDS = 15

_mbids = {}  # isrc -> ids found, {} when MusicBrainz has none
_lengths = {}  # isrc -> seconds, for the recordings MusicBrainz knows the length of
_covers_tried = set()  # (maloja url, artist, album or title), once per run
_tasks = set()  # running cover uploads, kept so they are not collected mid-way
_mb_lock = asyncio.Lock()
_mb_last = 0.0  # when the last MusicBrainz request ended


def _same(a, b) -> bool:
    return bool(a and b) and a.strip().casefold() == b.strip().casefold()


def recordings_to_try(body: dict, title: str) -> list[dict]:
    """The recordings an ISRC lookup names, Shazam's title first.

    One ISRC can name several recordings, usually duplicates of one another.
    """
    recordings = body.get("recordings") or []
    return sorted(recordings, key=lambda r: not _same(r.get("title"), title))


def album_release(releases: list[dict], album: str | None) -> str | None:
    """The release whose title is Shazam's album, an official one first.

    None when no title matches. The same recording is on singles and
    compilations, and a guessed release would show the wrong cover.
    """
    found = [r for r in releases if _same(r.get("title"), album)]
    found.sort(key=lambda r: r.get("status") != "Official")
    return found[0]["id"] if found else None


def _ids(recording: dict, release: str | None) -> dict:
    ids = {"recording_mbid": recording["id"]}
    artists = [c["artist"]["id"] for c in recording.get("artist-credit") or []
               if (c.get("artist") or {}).get("id")]
    if artists:
        ids["artist_mbids"] = artists
    if release:
        ids["release_mbid"] = release
    return ids


async def _mb_get(session, path, **params):
    """One MusicBrainz request, at most one a second as their rules ask.
    None when MusicBrainz has no such thing."""
    global _mb_last
    async with _mb_lock:
        wait = _mb_last + MUSICBRAINZ_GAP_SECONDS - time.monotonic()
        if wait > 0:
            await asyncio.sleep(wait)
        try:
            async with session.get(f"{MUSICBRAINZ_URL}/{path}",
                                   params={**params, "fmt": "json"}) as r:
                if r.status == 404:
                    return None
                r.raise_for_status()
                return await r.json()
        finally:
            _mb_last = time.monotonic()


async def _lookup(isrc, title, album) -> dict:
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10),
                                     headers={"User-Agent": USER_AGENT}) as s:
        body = await _mb_get(s, f"isrc/{isrc}", inc="artist-credits") or {}
        recordings = recordings_to_try(body, title)
        ids = _ids(recordings[0], None) if recordings else {}
        if recordings and recordings[0].get("length"):
            if len(_lengths) >= CACHE_SIZE:
                _lengths.pop(next(iter(_lengths)))
            _lengths[isrc] = recordings[0]["length"] / 1000
        if album:
            for recording in recordings[:MAX_RECORDINGS]:
                detail = await _mb_get(s, f"recording/{recording['id']}",
                                       inc="releases") or {}
                release = album_release(detail.get("releases") or [], album)
                if release:
                    return _ids(recording, release)
    return ids


async def musicbrainz_ids(isrc, title, album) -> dict:
    """MusicBrainz IDs for a track, from its ISRC. {} when there are none.

    The ISRC lookup names the recording. Its releases need a second lookup,
    because the ISRC endpoint leaves them out. Cached per ISRC, so a song heard
    every cycle is looked up once. A network failure or a rate limit is not
    cached, so the next match tries again.
    """
    if not isrc:
        return {}
    if isrc in _mbids:
        return _mbids[isrc]
    try:
        # Capture waits on this, so a slow MusicBrainz must not hold it long.
        ids = await asyncio.wait_for(_lookup(isrc, title, album), MUSICBRAINZ_LIMIT_SECONDS)
    except Exception as e:
        log.debug("MusicBrainz lookup of %s failed: %s", isrc, e or type(e).__name__)
        return {}
    if len(_mbids) >= CACHE_SIZE:
        _mbids.pop(next(iter(_mbids)))
    _mbids[isrc] = ids
    return ids


def track_length(isrc) -> float | None:
    """The track's length in seconds, if MusicBrainz said so already."""
    return _lengths.get(isrc) if isrc else None


def look_up_length(isrc, title, album):
    """Ask MusicBrainz in the background, so the room that needs the length
    does not wait for it. The answer is cached, see track_length()."""
    if not isrc or isrc in _mbids:
        return
    task = asyncio.create_task(musicbrainz_ids(isrc, title, album))
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)


def is_placeholder(status: int, location: str | None) -> bool | None:
    """Read Maloja's answer to GET /image. True means no picture, False means
    it has one, None means it is still looking and the answer comes later."""
    if status == 202:
        return None
    if status != 307:
        raise ValueError(f"Maloja answered HTTP {status} for the picture")
    return "placeholder" in (location or "")


async def fetch_image(session, url) -> tuple[str, bytes]:
    """(content type, bytes) of a cover. Refused when it is not an image, or
    is larger than MAX_ART_BYTES."""
    async with session.get(url) as r:
        r.raise_for_status()
        kind = r.headers.get("Content-Type", "").split(";")[0].strip()
        if not kind.startswith("image/"):
            raise ValueError(f"cover is {kind or 'untyped'}, not an image")
        # read(n) answers with what has arrived so far, which for a cover of
        # more than one packet is only its top, so read until the end.
        image = bytearray()
        while chunk := await r.content.read(65536):
            image += chunk
            if len(image) > MAX_ART_BYTES:
                raise ValueError("cover is too large")
    return kind, bytes(image)


async def _maloja_cover(url, key, artist, title, album, art_url) -> str:
    """Give Maloja Shazam's cover, unless it already shows a picture.

    The album gets the picture when there is one, because Maloja shows an
    album's picture for a track that has none of its own.
    """
    if album:
        entity, query = "album", {"albumtitle": album, "albumartist": artist}
    else:
        entity, query = "track", {"title": title, "trackartist": artist}
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30)) as s:
        # Maloja may have split or renamed the artist, and then this finds nothing.
        async with s.get(f"{url}/apis/mlj_1/{entity}info", params=query) as r:
            r.raise_for_status()
            entity_id = (await r.json())["id"]
        async with s.get(f"{url}/image", params={f"{entity}_id": entity_id},
                         allow_redirects=False) as r:
            missing = is_placeholder(r.status, r.headers.get("Location"))
        if missing is None:
            return "pending"
        if not missing:
            return "has one"
        kind, image = await fetch_image(s, art_url)
        b64 = f"data:{kind};base64,{base64.b64encode(image).decode()}"
        if entity == "album":
            body = {"albumtitle": album, "albumartist": [artist]}
        else:
            body = {"title": title, "trackartist": [artist]}
        async with s.post(f"{url}/apis/mlj_1/addpicture",
                          json={"key": key, "b64": b64, **body}) as r:
            r.raise_for_status()
    return "uploaded"


async def _try_maloja_cover(name, *args):
    try:
        outcome = await _maloja_cover(*args)
    except Exception as e:
        log.debug("cover for Maloja failed: %s", e)
        return
    if outcome == "pending":
        _covers_tried.discard(name)  # Maloja was still looking; ask next time
    elif outcome == "uploaded":
        log.info("gave Maloja the Shazam cover for %s - %s", args[2], args[4] or args[3])


def maloja_cover(data, artist, title, album, art_url):
    """Start a cover upload in the background, at most once per album per run.

    Called after a scrobble to Maloja succeeded, so the album exists there.
    """
    if not art_url:
        return
    url = data["url"].rstrip("/")
    name = (url, artist.casefold(), (album or title).casefold())
    if name in _covers_tried:
        return
    _covers_tried.add(name)
    task = asyncio.create_task(
        _try_maloja_cover(name, url, data["key"], artist, title, album, art_url))
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)
