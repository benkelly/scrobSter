"""Storage. stdlib sqlite3, one connection per call, no ORM.

Schema 2 splits a recognition from its outcome. `matches` is what the audio was,
which is shared. `scrobbles` is what each user sent where, because one match can
go to several users with different services.
"""
import json
import os
import pathlib
import sqlite3

from . import config

SCHEMA_VERSION = 2


def _conn():
    conn = sqlite3.connect(config.DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def _table_exists(c, name):
    return c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                     (name,)).fetchone() is not None


def _create_schema(c):
    c.executescript(
        """
        CREATE TABLE IF NOT EXISTS users(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT NOT NULL UNIQUE COLLATE NOCASE,
            password_hash TEXT NOT NULL,
            is_admin INTEGER NOT NULL DEFAULT 0,
            -- A room microphone cannot know who is listening, so sharing its
            -- matches is a choice each user makes.
            room_mic INTEGER NOT NULL DEFAULT 0,
            api_token TEXT UNIQUE,
            created_at INTEGER NOT NULL);

        CREATE TABLE IF NOT EXISTS credentials(
            user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            service TEXT NOT NULL,
            data TEXT NOT NULL,
            PRIMARY KEY (user_id, service));

        CREATE TABLE IF NOT EXISTS sessions(
            token TEXT PRIMARY KEY,
            user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            created_at INTEGER NOT NULL,
            last_seen INTEGER NOT NULL);

        CREATE TABLE IF NOT EXISTS matches(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts INTEGER NOT NULL,
            artist TEXT, title TEXT, album TEXT,
            track_key TEXT, art_url TEXT,
            source TEXT NOT NULL DEFAULT 'server');

        CREATE TABLE IF NOT EXISTS scrobbles(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            match_id INTEGER NOT NULL REFERENCES matches(id) ON DELETE CASCADE,
            user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            service TEXT NOT NULL,
            status TEXT NOT NULL);

        CREATE INDEX IF NOT EXISTS idx_scrobbles_user ON scrobbles(user_id, match_id);
        CREATE INDEX IF NOT EXISTS idx_matches_ts ON matches(ts DESC);
        """
    )


def adopt_legacy_history(user_id) -> int:
    """Give the schema 1 history to the owner, once that account exists.

    Schema 1 held one row per scrobble with the services in a JSON column, and
    no idea of a user. The rows wait in matches_v1 until there is somebody to
    own them, otherwise the history would survive with no owner and show
    nowhere.
    """
    moved = 0
    with _conn() as c:
        if not _table_exists(c, "matches_v1"):
            return 0
        for r in c.execute("SELECT * FROM matches_v1").fetchall():
            cur = c.execute(
                "INSERT INTO matches(ts, artist, title, album, track_key, art_url,"
                " source) VALUES(?,?,?,?,?,?,'server')",
                (r["ts"], r["artist"], r["title"], r["album"], r["track_key"],
                 r["art_url"]),
            )
            try:
                services = json.loads(r["services"] or "{}")
            except ValueError:
                services = {}
            for service, status in services.items():
                c.execute("INSERT INTO scrobbles(match_id, user_id, service, status)"
                          " VALUES(?,?,?,?)", (cur.lastrowid, user_id, service, status))
            moved += 1
        c.execute("DROP TABLE matches_v1")
    return moved


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
        conn = _conn()
    except sqlite3.OperationalError as e:
        uid = os.geteuid() if hasattr(os, "geteuid") else "unknown"
        raise RuntimeError(
            f"cannot open the database at {config.DB_PATH}: {e}. The folder"
            f" {folder} must be writable by the user running scrobSter,"
            f" which is uid {uid}."
        ) from e

    with conn as c:
        version = c.execute("PRAGMA user_version").fetchone()[0]
        old = version == 0 and _table_exists(c, "matches") and not _table_exists(c, "users")
        if old:
            # Park the old rows. adopt_legacy_history() moves them once the
            # owner account exists.
            c.execute("ALTER TABLE matches RENAME TO matches_v1")
        _create_schema(c)
        c.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
    conn.close()


# --- matches and scrobbles -------------------------------------------------

def add_match(ts, artist, title, album, track_key, art_url, source) -> int:
    with _conn() as c:
        cur = c.execute(
            "INSERT INTO matches(ts, artist, title, album, track_key, art_url, source)"
            " VALUES(?,?,?,?,?,?,?)",
            (ts, artist, title, album, track_key, art_url, source),
        )
        return cur.lastrowid


def add_scrobbles(match_id, user_id, results: dict):
    with _conn() as c:
        c.executemany(
            "INSERT INTO scrobbles(match_id, user_id, service, status) VALUES(?,?,?,?)",
            [(match_id, user_id, s, status) for s, status in results.items()],
        )


def recent(user_id, limit=50):
    """History for one user: the matches they scrobbled, with the per-service result."""
    with _conn() as c:
        rows = c.execute(
            """SELECT m.*, s.service, s.status
               FROM scrobbles s JOIN matches m ON m.id = s.match_id
               WHERE s.user_id = ?
               ORDER BY m.ts DESC, m.id DESC
               LIMIT ?""",
            (user_id, limit * 4),  # several services share one match
        ).fetchall()
    out, index = [], {}
    for r in rows:
        item = index.get(r["id"])
        if item is None:
            item = {k: r[k] for k in
                    ("id", "ts", "artist", "title", "album", "track_key", "art_url", "source")}
            item["services"] = {}
            index[r["id"]] = item
            out.append(item)
        item["services"][r["service"]] = r["status"]
        if len(out) >= limit:
            break
    return out
