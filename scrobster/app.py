"""FastAPI app: web UI and JSON API.

A request is identified in this order: a bearer token, then a session cookie,
then Home Assistant ingress. Ingress is trusted because Home Assistant has
already signed the user in, and its documentation says an add-on must not ask
again.
"""
import asyncio
import contextlib
import logging
import pathlib
import re
import secrets

import pylast
import uvicorn
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse

from . import accounts, config, db, scrobble
from .listener import (SILENT_DBFS, Listener, decode_to_wav, list_devices, peak_dbfs,
                       probe_level)

MAX_UPLOAD_BYTES = 10 * 1024 * 1024
COOKIE = "scrobster_session"

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("scrobster")
listener = Listener()


@contextlib.asynccontextmanager
async def _lifespan(app):
    db.init()
    accounts.ensure_first_user()
    accounts.sync_admin_password()
    # A device chosen in Settings outranks the environment, see /api/audio.
    config.set_audio(db.get_setting("audio_backend"), db.get_setting("audio_device"))
    if config.LISTEN_ON_START:
        listener.start()
    yield
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
            "is_admin": bool(user["is_admin"]), "room_mic": bool(user["room_mic"])}


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
    changed = accounts.update_user(user["id"], room_mic=body.get("room_mic"))
    return _public(changed)


@app.post("/api/me/password")
async def change_password(body: dict, user: dict = Depends(require_user)):
    if not accounts.verify_password(str(body.get("current", "")), user["password_hash"]):
        raise HTTPException(403, "the current password is wrong")
    try:
        accounts.update_user(user["id"], password=str(body.get("password", "")))
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

@app.get("/api/status")
async def status(user: dict = Depends(require_user)):
    credentials = accounts.get_credentials(user["id"])
    return {
        "listening": listener.listening,
        "started_at": listener.started_at,
        "last_match": listener.last_match,
        "last_error": listener.last_error,
        "level_db": listener.last_level_db,
        "attempts": listener.attempts,
        "last_attempt": listener.last_attempt,
        "services": scrobble.enabled_services(credentials),
        "profiles": scrobble.profile_urls(credentials),
        "interval": config.MATCH_INTERVAL,
        "stop_seconds": config.NOW_PLAYING_STOP_SECONDS,
        "room_mic": bool(user["room_mic"]),
        "audio": {"backend": config.AUDIO_BACKEND, "device": config.AUDIO_DEVICE},
    }


@app.post("/api/listen")
async def listen(body: dict, user: dict = Depends(require_admin)):
    """Only an administrator starts or stops the shared microphone."""
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
    return {"match": await listener.match_bytes(wav, source="browser", users=[user]),
            "level_db": peak_dbfs(wav)}


# --- the audio input, for an administrator -----------------------------------

def _audio_state() -> dict:
    saved = db.get_setting("audio_device") or db.get_setting("audio_backend")
    return {"backend": config.AUDIO_BACKEND, "device": config.AUDIO_DEVICE,
            "env_backend": config.ENV_AUDIO_BACKEND, "env_device": config.ENV_AUDIO_DEVICE,
            "saved": bool(saved), "listening": listener.listening}


@app.get("/api/audio")
async def audio(admin: dict = Depends(require_admin)):
    """The current capture device, and the ones ffmpeg can see."""
    return {**_audio_state(), "devices": await list_devices(config.AUDIO_BACKEND)}


@app.put("/api/audio")
async def set_audio(body: dict, admin: dict = Depends(require_admin)):
    """Choose the capture device. It is kept in the database and outranks the
    environment, and the next capture already uses it."""
    device = str(body.get("device") or "").strip()
    backend = str(body.get("backend") or "").strip() or None
    if not device:
        raise HTTPException(400, "choose a device")
    if backend and not re.fullmatch(r"[a-z0-9_]+", backend):
        raise HTTPException(400, "that is not an ffmpeg input format")
    db.set_setting("audio_device", device)
    if backend:
        db.set_setting("audio_backend", backend)
    config.set_audio(backend, device)
    listener.reset_input()
    log.info("audio input changed by %s: %s %s", admin["username"],
             config.AUDIO_BACKEND, config.AUDIO_DEVICE)
    return _audio_state()


@app.delete("/api/audio")
async def reset_audio(admin: dict = Depends(require_admin)):
    """Forget the choice made here and go back to the environment."""
    db.set_setting("audio_device", None)
    db.set_setting("audio_backend", None)
    config.set_audio(config.ENV_AUDIO_BACKEND, config.ENV_AUDIO_DEVICE)
    listener.reset_input()
    return _audio_state()


@app.post("/api/audio/test")
async def test_audio(body: dict, admin: dict = Depends(require_admin)):
    """Record three seconds from a device and report the peak, so a silent
    device is found before it is chosen."""
    device = str(body.get("device") or config.AUDIO_DEVICE).strip()
    backend = str(body.get("backend") or config.AUDIO_BACKEND).strip()
    try:
        level = await probe_level(backend, device)
    except Exception as e:
        hint = (" The listener may be holding the device: stop listening and try again."
                if listener.listening else "")
        raise HTTPException(400, f"{e}{hint}")
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
    try:
        changed = accounts.update_user(
            user_id, password=body.get("password"),
            is_admin=body.get("is_admin"), room_mic=body.get("room_mic"))
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
    uvicorn.run(app, host="0.0.0.0", port=config.PORT)


if __name__ == "__main__":
    main()
