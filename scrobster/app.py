"""FastAPI app: web UI and JSON API.

A request is identified in this order: a bearer token, then a session cookie,
then Home Assistant ingress. Ingress is trusted because Home Assistant has
already signed the user in, and its documentation says an add-on must not ask
again.
"""
import asyncio
import contextlib
import json
import logging
import pathlib
import secrets

import pylast
import uvicorn
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse

from . import accounts, config, db, events, mqtt, rooms, scrobble
from . import __version__
from .listener import (REQUEST_WINDOW_SECONDS, SILENT_DBFS, Listener, RateLimited,
                       decode_to_wav, list_devices, peak_dbfs, probe_level)

MAX_UPLOAD_BYTES = 10 * 1024 * 1024
# /api/events sends a comment this often, so proxies keep the stream open.
EVENTS_KEEPALIVE_SECONDS = 15
COOKIE = "scrobster_session"

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("scrobster")
listener = Listener()
publisher = mqtt.Publisher(listener)


@contextlib.asynccontextmanager
async def _lifespan(app):
    db.init()
    accounts.ensure_first_user()
    accounts.sync_admin_password()
    removed = db.delete_orphaned_matches()
    if removed:
        log.info("Removed %d matches that no account scrobbled.", removed)
    # Rates chosen on the Rooms tab outrank the environment, see /api/recognition.
    config.set_rates(db.get_setting("recognition_gap_seconds"),
                     db.get_setting("match_interval"))
    listener.restore_plays()
    await listener.sync_rooms()
    if config.LISTEN_ON_START:
        listener.start()
    publishing = asyncio.create_task(publisher.run())
    yield
    publishing.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await publishing
    await listener.stop()


app = FastAPI(title="scrobSter", lifespan=_lifespan)
_static = pathlib.Path(__file__).parent / "static"


# --- identifying the caller -------------------------------------------------

def _from_ingress(request: Request) -> bool:
    client = request.client.host if request.client else None
    return bool(config.TRUST_INGRESS and client == config.INGRESS_IP)


def current_user(request: Request):
    header = request.headers.get("authorization", "")
    if header.startswith("Bearer "):
        token = header[7:]
        # The old shared token still works, as the owner, so an existing Home
        # Assistant REST sensor keeps reading the API.
        if config.API_TOKEN and secrets.compare_digest(token, config.API_TOKEN):
            return accounts.first_admin()
        user = accounts.get_user_by_token(token)
        if user:
            return user
    user = accounts.session_user(request.cookies.get(COOKIE))
    if user:
        return user
    if _from_ingress(request):
        return accounts.first_admin()
    return None


def require_user(request: Request) -> dict:
    user = current_user(request)
    if user is None:
        raise HTTPException(401, "sign in first")
    return user


def require_admin(request: Request) -> dict:
    user = require_user(request)
    if not user["is_admin"]:
        raise HTTPException(403, "this needs an administrator")
    return user


def _public(user) -> dict:
    return {"id": user["id"], "username": user["username"],
            "is_admin": bool(user["is_admin"]), "rooms": rooms.rooms_of(user["id"])}


# --- sign in ----------------------------------------------------------------

@app.post("/api/login")
async def login(request: Request, body: dict):
    user = accounts.get_user_by_name(str(body.get("username", "")))
    # Always compare, so a missing user and a wrong password take similar time.
    stored = user["password_hash"] if user else "scrypt$00$00"
    if not accounts.verify_password(str(body.get("password", "")), stored) or not user:
        raise HTTPException(401, "wrong user name or password")
    token = accounts.start_session(user["id"])
    forwarded = request.headers.get("x-forwarded-proto", request.url.scheme)
    response = JSONResponse({"user": _public(user)})
    response.set_cookie(COOKIE, token, httponly=True, samesite="lax",
                        secure=forwarded == "https", max_age=accounts.SESSION_DAYS * 86400)
    return response


@app.post("/api/logout")
async def logout(request: Request):
    accounts.end_session(request.cookies.get(COOKIE))
    response = JSONResponse({"ok": True})
    response.delete_cookie(COOKIE)
    return response


@app.get("/api/me")
async def me(user: dict = Depends(require_user)):
    return {**_public(user), "api_token": user["api_token"],
            "services": scrobble.enabled_services(accounts.get_credentials(user["id"]))}


@app.put("/api/me")
async def update_me(body: dict, user: dict = Depends(require_user)):
    """`room_mic` is from before rooms, when there was one. It now joins or
    leaves every room. Choose single rooms with /api/rooms/{id}/member."""
    if body.get("room_mic") is not None:
        for room in rooms.list_rooms():
            rooms.set_member(room["id"], user["id"], bool(body["room_mic"]))
    return _public(user)


@app.post("/api/me/password")
async def change_password(request: Request, body: dict, user: dict = Depends(require_user)):
    if not accounts.verify_password(str(body.get("current", "")), user["password_hash"]):
        raise HTTPException(403, "the current password is wrong")
    try:
        # Every other session ends; this one stays signed in.
        accounts.update_user(user["id"], password=str(body.get("password", "")),
                             keep_session=request.cookies.get(COOKIE))
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"ok": True}


@app.post("/api/me/token")
async def regenerate_token(user: dict = Depends(require_user)):
    return {"api_token": accounts.new_api_token(user["id"])}


# --- services ---------------------------------------------------------------

@app.get("/api/services")
async def services(user: dict = Depends(require_user)):
    """What is connected. Secrets are never sent back, only whether they exist."""
    credentials = accounts.get_credentials(user["id"])
    enabled = scrobble.enabled_services(credentials)
    profiles = scrobble.profile_urls(credentials)
    out = {}
    for name in accounts.SERVICES:
        data = credentials.get(name) or {}
        out[name] = {
            "connected": name in enabled,
            "username": data.get("username"),
            "url": data.get("url"),
            "profile_url": profiles.get(name),
            "has_secret": bool(data.get("token") or data.get("key")
                               or data.get("session_key") or data.get("password_hash")),
        }
    out["lastfm"]["app_registered"] = bool(config.LASTFM_API_KEY
                                           and config.LASTFM_API_SECRET)
    return out


@app.put("/api/services/{service}")
async def set_service(service: str, body: dict, user: dict = Depends(require_user)):
    """Set or update one service. Fields left out or empty keep their saved value,
    so the settings form can change a URL without asking for the secret again."""
    existing = accounts.get_credentials(user["id"]).get(service) or {}
    data = accounts.merge_credential(existing, body)
    if service == "listenbrainz" and data.get("token"):
        # Ask the server whose token it is. That both checks the token and gives
        # the user name for the profile link.
        try:
            username = await scrobble.listenbrainz_username(data.get("url"), data["token"])
        except Exception as e:
            log.warning("could not check the ListenBrainz token: %s", e)
            username = existing.get("username") if data["token"] == existing.get("token") else None
        else:
            if username is None:
                raise HTTPException(400, "ListenBrainz did not accept that token")
        data["username"] = username
    try:
        accounts.set_credential(user["id"], service, data)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"ok": True}


@app.delete("/api/services/{service}")
async def clear_service(service: str, user: dict = Depends(require_user)):
    accounts.delete_credential(user["id"], service)
    return {"ok": True}


# --- Last.fm authorization, without leaving the browser ----------------------

def _lastfm_generator():
    if not (config.LASTFM_API_KEY and config.LASTFM_API_SECRET):
        raise HTTPException(400, "set LASTFM_API_KEY and LASTFM_API_SECRET first")
    network = pylast.LastFMNetwork(api_key=config.LASTFM_API_KEY,
                                   api_secret=config.LASTFM_API_SECRET)
    return pylast.SessionKeyGenerator(network)


@app.post("/api/lastfm/start")
async def lastfm_start(user: dict = Depends(require_user)):
    url = await asyncio.to_thread(lambda: _lastfm_generator().get_web_auth_url())
    return {"url": url, "token": url.split("token=")[-1]}


@app.post("/api/lastfm/finish")
async def lastfm_finish(body: dict, user: dict = Depends(require_user)):
    token = str(body.get("token", ""))
    if not token:
        raise HTTPException(400, "no token")
    try:
        key, username = await asyncio.to_thread(
            lambda: _lastfm_generator().get_web_auth_session_key_username("", token))
    except Exception as e:
        raise HTTPException(400, f"Last.fm refused the token: {e}")
    accounts.set_credential(user["id"], "lastfm",
                            {"session_key": key, "username": username})
    return {"username": username}


# --- listener and history ---------------------------------------------------

def _room_public(row, user, mine) -> dict:
    """A room as the page shows it. Where it records from is for an
    administrator, and a camera password is never sent."""
    live = listener.rooms.get(row["id"])
    out = {"id": row["id"], "name": row["name"], "kind": row["kind"],
           "enabled": bool(row["enabled"]), "member": row["id"] in mine,
           "running": bool(live and live.running),
           "last_match": live.last_match if live else None,
           "last_error": live.last_error if live else None,
           "level_db": live.last_level_db if live else None,
           "live_db": live.live_level_db() if live else None,
           "attempts": live.attempts if live else 0,
           "last_attempt": live.last_attempt if live else None}
    if user["is_admin"]:
        out.update(backend=row["backend"], device=row["device"],
                   url=rooms.mask_url(row["url"]),
                   input=live.describe() if live else None)
    return out


def _rooms_for(user) -> list[dict]:
    mine = set(rooms.rooms_of(user["id"]))
    return [_room_public(row, user, mine) for row in rooms.list_rooms()]


@app.get("/api/status")
async def status(user: dict = Depends(require_user)):
    credentials = accounts.get_credentials(user["id"])
    # The flat fields describe the first room, as they described the single
    # microphone before rooms, so an existing Home Assistant sensor still reads.
    first = listener.first_room()
    room_list = _rooms_for(user)
    return {
        "version": __version__,
        "listening": listener.listening,
        "started_at": listener.started_at,
        "last_match": listener.last_match_for(user["id"]),
        "last_error": first.last_error if first else None,
        "level_db": first.last_level_db if first else None,
        "attempts": sum(r["attempts"] for r in room_list),
        "last_attempt": max((r["last_attempt"] or 0 for r in room_list), default=0) or None,
        "services": scrobble.enabled_services(credentials),
        "profiles": scrobble.profile_urls(credentials),
        "interval": config.MATCH_INTERVAL,
        "stop_seconds": config.NOW_PLAYING_STOP_SECONDS,
        "rate_limited_for": round(listener.rate_limited_for()),
        "mqtt": publisher.connected,
        "rooms": room_list,
    }


def visible_event(event: dict, user: dict) -> dict | None:
    """What one signed-in user may see of an event, or None.

    The same rules as the rest of the API: a room is everybody's, a browser
    clip, a scrobble and service credentials are their owner's, and the list
    of accounts is for an administrator.
    """
    kind, admin, me = event["kind"], bool(user["is_admin"]), user["id"]
    out = {"kind": kind, "ts": event["ts"]}
    if kind == "match":
        mine = me in event["users"]
        if event["room_id"] is None and not mine:
            return None
        out.update(room_id=event["room_id"], match=event["match"], mine=mine)
        if admin:
            out["users"] = event["users"]
    elif kind in ("scrobble", "services"):
        if event["user_id"] != me:
            return None
        out.update({k: v for k, v in event.items() if k not in ("kind", "ts", "user_id")})
    elif kind == "cleared":
        if me not in event["users"]:
            return None
    elif kind == "member":
        if event["user_id"] != me and not admin:
            return None
        out.update(room_id=event["room_id"], user_id=event["user_id"], on=event["on"])
    elif kind == "users":
        if not admin:
            return None
    elif kind == "listening":
        out["on"] = event["on"]
    elif kind not in ("rooms", "rates"):
        return None
    return out


def _sse(kind, data) -> str:
    return f"event: {kind}\ndata: {json.dumps(data, separators=(',', ':'))}\n\n"


@app.get("/api/events")
async def event_stream(request: Request, user: dict = Depends(require_user)):
    """What happens, as it happens, as server-sent events. Each event is named
    by its kind, see events.py. `resync` means some were lost, so read
    /api/status again."""
    async def stream():
        with events.subscribe() as sub:
            yield _sse("hello", {"version": __version__, "user_id": user["id"]})
            while not await request.is_disconnected():
                happened = await sub.wait(EVENTS_KEEPALIVE_SECONDS)
                if sub.dropped:
                    sub.dropped = 0
                    yield _sse("resync", {})
                    continue
                if not happened:
                    yield ": keepalive\n\n"
                for event in happened:
                    shown = visible_event(event, user)
                    if shown is not None:
                        yield _sse(shown["kind"], shown)
    # No buffering on the way, Home Assistant ingress and nginx included.
    return StreamingResponse(stream(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache",
                                      "X-Accel-Buffering": "no"})


@app.get("/api/levels")
async def levels(user: dict = Depends(require_user)):
    """How loud each recording room is right now, for the Rooms tab's meters.
    Small, because the page asks several times a second."""
    return {str(room_id): room.live_level_db()
            for room_id, room in listener.rooms.items() if room.running}


@app.post("/api/listen")
async def listen(body: dict, user: dict = Depends(require_admin)):
    """Only an administrator starts or stops listening, in every room that is
    turned on."""
    if body.get("on"):
        listener.start()
    else:
        await listener.stop()
    return {"listening": listener.listening}


@app.get("/api/recent")
async def recent(limit: int = 50, user: dict = Depends(require_user)):
    return db.recent(user["id"], min(limit, 500))


@app.post("/api/match")
async def match(request: Request, user: dict = Depends(require_user)):
    """Identify one clip recorded by a browser. It scrobbles for that user only."""
    declared = request.headers.get("content-length", "")
    if declared.isdigit() and int(declared) > MAX_UPLOAD_BYTES:
        raise HTTPException(413, "audio too large")
    data = await request.body()
    if not data:
        raise HTTPException(400, "empty body")
    if len(data) > MAX_UPLOAD_BYTES:
        raise HTTPException(413, "audio too large")
    try:
        wav = await decode_to_wav(data)
    except ValueError as e:
        raise HTTPException(415, str(e))
    try:
        found = await listener.match_bytes(wav, source="browser", users=[user])
    except RateLimited as e:
        raise HTTPException(429, str(e))
    except Exception as e:
        log.warning("browser clip failed: %s", e)
        raise HTTPException(502, f"recognition failed: {str(e) or type(e).__name__}")
    return {"match": found, "level_db": peak_dbfs(wav)}


# --- rooms ---------------------------------------------------------------------

def _room_fields(body: dict) -> dict:
    return {k: body.get(k) for k in ("name", "kind", "backend", "device", "url", "enabled")
            if k in body}


@app.get("/api/rooms")
async def list_rooms(user: dict = Depends(require_user)):
    return _rooms_for(user)


@app.post("/api/rooms")
async def add_room(body: dict, admin: dict = Depends(require_admin)):
    """Add a room. Everybody who chooses it receives what it hears, so it starts
    with nobody but the administrator who added it."""
    try:
        row = rooms.add_room(**_room_fields(body))
    except ValueError as e:
        raise HTTPException(400, str(e))
    rooms.set_member(row["id"], admin["id"], True)
    await listener.sync_rooms()
    log.info("room %r added by %s", row["name"], admin["username"])
    return _room_public(row, admin, set(rooms.rooms_of(admin["id"])))


@app.put("/api/rooms/{room_id}")
async def edit_room(room_id: int, body: dict, admin: dict = Depends(require_admin)):
    """Change a room. A new input is used from the room's next capture."""
    try:
        row = rooms.update_room(room_id, **_room_fields(body))
    except LookupError as e:
        raise HTTPException(404, str(e))
    except ValueError as e:
        raise HTTPException(400, str(e))
    await listener.sync_rooms()
    return _room_public(row, admin, set(rooms.rooms_of(admin["id"])))


@app.delete("/api/rooms/{room_id}")
async def remove_room(room_id: int, admin: dict = Depends(require_admin)):
    """The history keeps the room's name. Only listening there stops."""
    rooms.delete_room(room_id)
    await listener.sync_rooms()
    return {"ok": True}


@app.put("/api/rooms/{room_id}/member")
async def room_member(room_id: int, body: dict, user: dict = Depends(require_user)):
    """Each person chooses which rooms scrobble for them."""
    if rooms.get_room(room_id) is None:
        raise HTTPException(404, "no such room")
    rooms.set_member(room_id, user["id"], bool(body.get("on")))
    return {"rooms": rooms.rooms_of(user["id"])}


# --- how often to ask Shazam, for an administrator ----------------------------------

_RATES = {"gap": ("recognition_gap_seconds", config.RECOGNITION_GAP_RANGE, float),
          "interval": ("match_interval", config.MATCH_INTERVAL_RANGE, int)}


def _rates_state() -> dict:
    return {
        "gap": config.RECOGNITION_GAP_SECONDS, "interval": config.MATCH_INTERVAL,
        "env_gap": config.ENV_RECOGNITION_GAP_SECONDS,
        "env_interval": config.ENV_MATCH_INTERVAL,
        "saved": any(db.get_setting(key) is not None for key, _, _ in _RATES.values()),
        "ranges": {"gap": config.RECOGNITION_GAP_RANGE,
                   "interval": config.MATCH_INTERVAL_RANGE},
        # Rooms that would ask while music plays, which is what the rate must carry.
        "rooms_on": sum(1 for room in listener.rooms.values() if room.enabled),
        "requests": listener.requests_recently(),
        "window_seconds": REQUEST_WINDOW_SECONDS,
        "last_rate_limited": listener.last_rate_limited,
        "rate_limited_for": round(listener.rate_limited_for()),
    }


@app.get("/api/recognition")
async def recognition(admin: dict = Depends(require_admin)):
    """How often Shazam is asked, and how often it was asked lately."""
    return _rates_state()


@app.put("/api/recognition")
async def set_recognition(body: dict, admin: dict = Depends(require_admin)):
    """Change how often Shazam is asked. Kept in the database, it outranks the
    environment and the add-on options, and applies without a restart."""
    chosen = {}
    for name, (key, (low, high), kind) in _RATES.items():
        if body.get(name) is None:
            continue
        try:
            value = kind(body[name])
        except (TypeError, ValueError):
            raise HTTPException(400, f"{name} must be a number")
        if not low <= value <= high:
            raise HTTPException(400, f"{name} must be between {low} and {high} seconds")
        chosen[name] = value
    listener.save_rates(chosen.get("gap"), chosen.get("interval"))
    log.info("recognition rates changed by %s: %s s between requests, %s s per room",
             admin["username"], config.RECOGNITION_GAP_SECONDS, config.MATCH_INTERVAL)
    return _rates_state()


@app.delete("/api/recognition")
async def reset_recognition(admin: dict = Depends(require_admin)):
    """Forget the rates chosen here and go back to the environment."""
    listener.reset_rates()
    return _rates_state()


# --- audio inputs, for an administrator -------------------------------------------

@app.get("/api/audio")
async def audio(admin: dict = Depends(require_admin)):
    """The devices ffmpeg can see, and the device the environment names, which a
    device room with no device of its own records from."""
    return {"backend": config.AUDIO_BACKEND, "device": config.AUDIO_DEVICE,
            "devices": await list_devices(config.AUDIO_BACKEND)}


@app.post("/api/audio/test")
async def test_audio(body: dict, admin: dict = Depends(require_admin)):
    """Record three seconds from a device or a camera and report the peak, so a
    silent input is found before it is chosen.

    With `room_id`, a camera URL whose password is still masked uses the saved
    password, and fields left out use the room's own."""
    saved = rooms.get_room(body["room_id"]) if body.get("room_id") else None
    kind = body.get("kind") or (saved or {}).get("kind") or "device"
    url = None
    try:
        if kind == "stream":
            typed = str(body.get("url") or (saved or {}).get("url") or "")
            url = rooms.check_url(rooms.merge_url(typed, (saved or {}).get("url")))
            level = await probe_level(url=url)
        else:
            # A field sent empty means the environment's device, as it does
            # when the room is saved. Only a field left out means the room's.
            def pick(key, default):
                value = body[key] if key in body else (saved or {}).get(key)
                return str(value or default).strip()
            backend = pick("backend", config.AUDIO_BACKEND)
            device = pick("device", config.AUDIO_DEVICE)
            level = await probe_level(backend, device)
    except ValueError as e:
        raise HTTPException(400, str(e))
    except Exception as e:
        hint = (" The listener may be holding the device: stop listening and try again."
                if listener.listening and kind != "stream" else "")
        raise HTTPException(400, f"{rooms.redact(str(e), url)}{hint}")
    return {"level_db": level, "silent": level < SILENT_DBFS}


# --- users, for an administrator ---------------------------------------------

@app.get("/api/users")
async def users(admin: dict = Depends(require_admin)):
    return [_public(u) for u in accounts.list_users()]


@app.post("/api/users")
async def add_user(body: dict, admin: dict = Depends(require_admin)):
    try:
        user = accounts.create_user(str(body.get("username", "")),
                                    str(body.get("password", "")),
                                    is_admin=bool(body.get("is_admin")),
                                    room_mic=bool(body.get("room_mic")))
    except ValueError as e:
        raise HTTPException(400, str(e))
    except Exception:
        raise HTTPException(409, "that user name is taken")
    return _public(user)


@app.put("/api/users/{user_id}")
async def edit_user(user_id: int, body: dict, admin: dict = Depends(require_admin)):
    """An administrator manages other accounts, including a forgotten password.

    Changing your own password still needs the current one, so a stolen session
    cannot lock the owner out. Recover that with ADMIN_PASSWORD instead.
    """
    if user_id == admin["id"] and body.get("password"):
        raise HTTPException(400, "use the password form to change your own password")
    if accounts.get_user(user_id) is None:
        raise HTTPException(404, "no such account")
    if body.get("is_admin") is not None and not body["is_admin"] \
            and accounts.is_last_admin(user_id):
        raise HTTPException(400, "keep at least one administrator")
    try:
        changed = accounts.update_user(
            user_id, password=body.get("password"), is_admin=body.get("is_admin"))
    except ValueError as e:
        raise HTTPException(400, str(e))
    return _public(changed)


@app.delete("/api/users/{user_id}")
async def remove_user(user_id: int, admin: dict = Depends(require_admin)):
    if user_id == admin["id"]:
        raise HTTPException(400, "you cannot delete your own account")
    accounts.delete_user(user_id)
    return {"ok": True}


# --- pages -------------------------------------------------------------------

@app.get("/app.css")
async def stylesheet():
    return FileResponse(_static / "app.css", media_type="text/css")


@app.get("/icon.png")
async def icon():
    return FileResponse(_static / "icon.png", media_type="image/png")


@app.get("/login")
async def login_page():
    return FileResponse(_static / "login.html")


@app.get("/")
async def index():
    return FileResponse(_static / "index.html")


def main():
    # proxy_headers=False: uvicorn otherwise takes the client address from
    # X-Forwarded-For when the peer is local, so a reverse proxy on the same
    # host that passes the header through would let anyone claim the ingress
    # address, and with it the owner's account.
    uvicorn.run(app, host="0.0.0.0", port=config.PORT, proxy_headers=False)


if __name__ == "__main__":
    main()
