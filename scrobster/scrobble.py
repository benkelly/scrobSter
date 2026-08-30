"""Send one track to every service a user has connected.

Two client paths cover four services:
- pylast: Last.fm and Libre.fm (Audioscrobbler protocol)
- ListenBrainz JSON POST: ListenBrainz and Maloja (same format, different URL)

Credentials belong to a user and come from the database. The Last.fm API key and
secret are different: they identify this application, not a person, so one
registration serves every user and they stay in the environment.
"""
import asyncio
import logging

import aiohttp
import pylast

from . import config

log = logging.getLogger("scrobster")
_networks = {}  # (service, cache key) -> pylast network

LISTENBRAINZ_DEFAULT_URL = "https://api.listenbrainz.org"


def _password_hash(data):
    """Last.fm auth needs the md5 of the password. Accept the hash directly."""
    hashed = data.get("password_hash")
    plain = data.get("password")
    return hashed or (pylast.md5(plain) if plain else None)


def enabled_services(credentials: dict) -> list[str]:
    """Which services this user can actually scrobble to."""
    found = []
    lastfm = credentials.get("lastfm") or {}
    if (config.LASTFM_API_KEY and config.LASTFM_API_SECRET
            and (lastfm.get("session_key") or (lastfm.get("username")
                                               and _password_hash(lastfm)))):
        found.append("lastfm")
    librefm = credentials.get("librefm") or {}
    if librefm.get("username") and _password_hash(librefm):
        found.append("librefm")
    if (credentials.get("listenbrainz") or {}).get("token"):
        found.append("listenbrainz")
    maloja = credentials.get("maloja") or {}
    if maloja.get("url") and maloja.get("key"):
        found.append("maloja")
    return found


def _pylast_network(service, data):
    key = (service, data.get("session_key") or data.get("username"))
    if key not in _networks:
        if service == "lastfm":
            _networks[key] = pylast.LastFMNetwork(
                api_key=config.LASTFM_API_KEY,
                api_secret=config.LASTFM_API_SECRET,
                session_key=data.get("session_key") or "",
                username=data.get("username"),
                password_hash=None if data.get("session_key") else _password_hash(data),
            )
        else:
            _networks[key] = pylast.LibreFMNetwork(
                username=data.get("username"),
                password_hash=_password_hash(data),
            )
    return _networks[key]


def _pylast_scrobble(service, data, artist, title, album, ts):
    _pylast_network(service, data).scrobble(
        artist=artist, title=title, album=album, timestamp=ts)


def _pylast_now_playing(service, data, artist, title, album):
    _pylast_network(service, data).update_now_playing(
        artist=artist, title=title, album=album)


def _lb_endpoints(service, data):
    if service == "maloja":
        return data["url"].rstrip("/") + "/apis/listenbrainz", data["key"]
    return (data.get("url") or LISTENBRAINZ_DEFAULT_URL).rstrip("/"), data["token"]


async def _lb_submit(base_url, token, artist, title, album, ts=None):
    """Submit a listen. With ts=None this marks the track as playing now instead."""
    meta = {"artist_name": artist, "track_name": title}
    if album:
        meta["release_name"] = album
    listen = {"track_metadata": meta}
    if ts is None:
        payload = {"listen_type": "playing_now", "payload": [listen]}
    else:
        listen["listened_at"] = ts
        payload = {"listen_type": "single", "payload": [listen]}
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15)) as s:
        async with s.post(f"{base_url}/1/submit-listens", json=payload,
                          headers={"Authorization": f"Token {token}"}) as r:
            r.raise_for_status()


async def _lb_clear(base_url, token):
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15)) as s:
        async with s.post(f"{base_url}/1/playing-now/delete", json={},
                          headers={"Authorization": f"Token {token}"}) as r:
            r.raise_for_status()


async def _each_service(credentials, handle, on_error) -> dict:
    results = {}
    for service in enabled_services(credentials):
        data = credentials[service]
        try:
            await handle(service, data)
            results[service] = "ok"
        except Exception as e:
            on_error(service, e)
            results[service] = f"error: {e}"[:200]
    return results


async def scrobble_all(credentials, artist, title, album, ts) -> dict:
    """Returns {service: "ok" | "error: ..."}. A failure logs and does not stop
    the others, and there is no retry queue."""
    async def handle(service, data):
        if service in ("lastfm", "librefm"):
            await asyncio.to_thread(_pylast_scrobble, service, data,
                                    artist, title, album, ts)
        else:
            base, token = _lb_endpoints(service, data)
            await _lb_submit(base, token, artist, title, album, ts)

    return await _each_service(
        credentials, handle,
        lambda s, e: log.warning("scrobble to %s failed: %s", s, e))


async def now_playing_all(credentials, artist, title, album) -> dict:
    """Mark the track as playing now. Best effort: the scrobble is the record
    that matters, so a failure only logs at debug level."""
    async def handle(service, data):
        if service in ("lastfm", "librefm"):
            await asyncio.to_thread(_pylast_now_playing, service, data,
                                    artist, title, album)
        else:
            base, token = _lb_endpoints(service, data)
            await _lb_submit(base, token, artist, title, album)

    return await _each_service(
        credentials, handle,
        lambda s, e: log.debug("now playing on %s failed: %s", s, e))


async def clear_now_playing_all(credentials) -> dict:
    """Remove the playing-now mark once the music stops.

    ListenBrainz has an endpoint for this. Last.fm has none, so its mark stays
    until the service drops it a few minutes later.
    """
    results = {}
    for service in enabled_services(credentials):
        if service in ("lastfm", "librefm"):
            results[service] = "expires on its own"
            continue
        try:
            await _lb_clear(*_lb_endpoints(service, credentials[service]))
            results[service] = "cleared"
        except Exception as e:
            log.debug("clearing now playing on %s failed: %s", service, e)
            results[service] = f"error: {e}"[:200]
    return results
