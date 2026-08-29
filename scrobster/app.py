"""FastAPI app: web UI + JSON API. The API doubles as the mobile / Home Assistant API."""
import contextlib
import logging
import pathlib

import uvicorn
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse

from . import config, db, scrobble
from .listener import Listener, decode_to_wav, peak_dbfs

MAX_UPLOAD_BYTES = 10 * 1024 * 1024

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
listener = Listener()


@contextlib.asynccontextmanager
async def _lifespan(app):
    db.init()
    if config.LISTEN_ON_START:
        listener.start()
    yield
    await listener.stop()


app = FastAPI(title="scrobSter", lifespan=_lifespan)


@app.middleware("http")
async def _auth(request: Request, call_next):
    if config.API_TOKEN and request.url.path.startswith("/api"):
        if request.headers.get("authorization") != f"Bearer {config.API_TOKEN}":
            return JSONResponse({"detail": "unauthorized"}, status_code=401)
    return await call_next(request)


@app.get("/api/status")
async def status():
    return {
        "listening": listener.listening,
        "started_at": listener.started_at,
        "last_match": listener.last_match,
        "last_error": listener.last_error,
        "level_db": listener.last_level_db,
        "attempts": listener.attempts,
        "last_attempt": listener.last_attempt,
        "services": scrobble.enabled_services(),
        "interval": config.MATCH_INTERVAL,
    }


@app.post("/api/listen")
async def listen(body: dict):
    if body.get("on"):
        listener.start()
    else:
        await listener.stop()
    return {"listening": listener.listening}


@app.post("/api/match")
async def match(request: Request):
    """Identify one clip recorded by a browser mic. Uses the same dedup and scrobble path."""
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
    return {"match": await listener.match_bytes(wav, source="browser"),
            "level_db": peak_dbfs(wav)}


@app.get("/api/recent")
async def recent(limit: int = 50):
    return db.recent(min(limit, 500))


_static = pathlib.Path(__file__).parent / "static"


@app.get("/")
async def index():
    return FileResponse(_static / "index.html")


def main():
    uvicorn.run(app, host="0.0.0.0", port=config.PORT)


if __name__ == "__main__":
    main()
