"""Scrobble history. stdlib sqlite3, one table, connection per call."""
import json
import sqlite3

from . import config


def _conn():
    conn = sqlite3.connect(config.DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init():
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
