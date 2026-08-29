"""Fan one matched track out to every configured service.

Two client paths cover four services:
- pylast: Last.fm and Libre.fm (Audioscrobbler protocol)
- ListenBrainz JSON POST: ListenBrainz and Maloja (same format, different URL)
"""
import asyncio
import logging

import aiohttp
import pylast

from . import config

log = logging.getLogger("scrobster")
_networks = {}  # pylast networks, built once


def _password_hash(plain, hashed):
    """Last.fm auth needs the md5 of the password. Accept the hash directly."""
    return hashed or (pylast.md5(plain) if plain else None)


def enabled_services() -> list[str]:
    s = []
    lastfm_login = config.LASTFM_SESSION_KEY or (
        config.LASTFM_USERNAME
        and _password_hash(config.LASTFM_PASSWORD, config.LASTFM_PASSWORD_HASH))
    if config.LASTFM_API_KEY and config.LASTFM_API_SECRET and lastfm_login:
        s.append("lastfm")
    if config.LIBREFM_USERNAME and _password_hash(config.LIBREFM_PASSWORD,
                                                  config.LIBREFM_PASSWORD_HASH):
        s.append("librefm")
    if config.LISTENBRAINZ_TOKEN:
        s.append("listenbrainz")
    if config.MALOJA_URL and config.MALOJA_KEY:
        s.append("maloja")
    return s


def _pylast_network(name):
    if name not in _networks:
        if name == "lastfm":
            _networks[name] = pylast.LastFMNetwork(
                api_key=config.LASTFM_API_KEY,
                api_secret=config.LASTFM_API_SECRET,
                # A session key alone is enough; the password path stays as a fallback.
                session_key=config.LASTFM_SESSION_KEY or "",
                username=config.LASTFM_USERNAME,
                password_hash=None if config.LASTFM_SESSION_KEY else _password_hash(
                    config.LASTFM_PASSWORD, config.LASTFM_PASSWORD_HASH),
            )
        else:
            _networks[name] = pylast.LibreFMNetwork(
                username=config.LIBREFM_USERNAME,
                password_hash=_password_hash(config.LIBREFM_PASSWORD,
                                             config.LIBREFM_PASSWORD_HASH),
            )
    return _networks[name]


def _pylast_scrobble(name, artist, title, album, ts):
    _pylast_network(name).scrobble(artist=artist, title=title, album=album, timestamp=ts)


async def _lb_submit(base_url, token, artist, title, album, ts):
    meta = {"artist_name": artist, "track_name": title}
    if album:
        meta["release_name"] = album
    payload = {"listen_type": "single",
               "payload": [{"listened_at": ts, "track_metadata": meta}]}
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15)) as s:
        async with s.post(f"{base_url.rstrip('/')}/1/submit-listens", json=payload,
                          headers={"Authorization": f"Token {token}"}) as r:
            r.raise_for_status()


async def scrobble_all(artist, title, album, ts) -> dict:
    """Returns {service: "ok" | "error: ..."}. Failures log and continue; no retry queue."""
    results = {}
    for name in enabled_services():
        try:
            if name in ("lastfm", "librefm"):
                await asyncio.to_thread(_pylast_scrobble, name, artist, title, album, ts)
            elif name == "listenbrainz":
                await _lb_submit(config.LISTENBRAINZ_URL, config.LISTENBRAINZ_TOKEN,
                                 artist, title, album, ts)
            elif name == "maloja":
                await _lb_submit(config.MALOJA_URL.rstrip("/") + "/apis/listenbrainz",
                                 config.MALOJA_KEY, artist, title, album, ts)
            results[name] = "ok"
        except Exception as e:
            log.warning("scrobble to %s failed: %s", name, e)
            results[name] = f"error: {e}"[:200]
    return results
