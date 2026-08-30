"""Scrobble history. stdlib sqlite3, one table, connection per call."""
import json
import os
import pathlib
import sqlite3

from . import config


def _conn():
    conn = sqlite3.connect(config.DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init():
    # sqlite reports only "unable to open database file" for a missing directory
    # and for one it may not write. Say which, because the usual cause is a
    # container volume owned by another user.
    folder = pathlib.Path(config.DB_PATH).parent
    try:
        folder.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        raise RuntimeError(f"cannot create the folder {folder} for DB_PATH="
                           f"{config.DB_PATH}: {e}") from e
    try:
        _conn().close()
    except sqlite3.OperationalError as e:
        uid = os.geteuid() if hasattr(os, "geteuid") else "unknown"
        raise RuntimeError(
            f"cannot open the database at {config.DB_PATH}: {e}. The folder"
            f" {folder} must be writable by the user running scrobSter,"
            f" which is uid {uid}."
        ) from e
    with _conn() as c:
        c.execute(
            """CREATE TABLE IF NOT EXISTS matches(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts INTEGER NOT NULL,
                artist TEXT, title TEXT, album TEXT,
                track_key TEXT, art_url TEXT,
                services TEXT)"""
        )


def add_match(ts, artist, title, album, track_key, art_url, services: dict):
    with _conn() as c:
        c.execute(
            "INSERT INTO matches(ts, artist, title, album, track_key, art_url, services)"
            " VALUES(?,?,?,?,?,?,?)",
            (ts, artist, title, album, track_key, art_url, json.dumps(services)),
        )


def recent(limit=50):
    with _conn() as c:
        rows = c.execute("SELECT * FROM matches ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["services"] = json.loads(d["services"] or "{}")
        out.append(d)
    return out
