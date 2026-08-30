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
import secrets

import pylast
import uvicorn
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse

from . import accounts, config, db, scrobble
from .listener import Listener, decode_to_wav, peak_dbfs

MAX_UPLOAD_BYTES = 10 * 1024 * 1024
COOKIE = "scrobster_session"

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("scrobster")
listener = Listener()


@contextlib.asynccontextmanager
async def _lifespan(app):
    db.init()
    accounts.ensure_first_user()
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
    out = {}
    for name in accounts.SERVICES:
        data = credentials.get(name) or {}
        out[name] = {
            "connected": name in enabled,
            "username": data.get("username"),
            "url": data.get("url"),
            "has_secret": bool(data.get("token") or data.get("key")
                               or data.get("session_key") or data.get("password_hash")),
        }
    out["lastfm"]["app_registered"] = bool(config.LASTFM_API_KEY
                                           and config.LASTFM_API_SECRET)
    return out


@app.put("/api/services/{service}")
async def set_service(service: str, body: dict, user: dict = Depends(require_user)):
    try:
        accounts.set_credential(user["id"], service, body)
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
    return {
        "listening": listener.listening,
        "started_at": listener.started_at,
        "last_match": listener.last_match,
        "last_error": listener.last_error,
        "level_db": listener.last_level_db,
        "attempts": listener.attempts,
        "last_attempt": listener.last_attempt,
        "services": scrobble.enabled_services(accounts.get_credentials(user["id"])),
        "interval": config.MATCH_INTERVAL,
        "room_mic": bool(user["room_mic"]),
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
