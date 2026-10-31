"""Storage. stdlib sqlite3, one connection per call, no ORM.

Schema 2 splits a recognition from its outcome. `matches` is what the audio was,
which is shared. `scrobbles` is what each user sent where, because one match can
go to several users with different services.

Schema 3 adds rooms. Each room is one input, a microphone on this machine or a
camera stream, and each user chooses which rooms scrobble for them.

Schema 4 adds display tokens, which may read what the rooms play and nothing
else. Only a hash of each is kept.
"""
import json
import os
import pathlib
import sqlite3

from . import config

SCHEMA_VERSION = 4


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
            -- Schema 2 had one room. Its choice moved to room_members in schema 3
            -- and this column is no longer read.
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
            source TEXT NOT NULL DEFAULT 'server',
            -- The room's name when it was heard, so the history still reads
            -- right after the room is renamed or removed.
            room TEXT);

        CREATE TABLE IF NOT EXISTS scrobbles(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            match_id INTEGER NOT NULL REFERENCES matches(id) ON DELETE CASCADE,
            user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            service TEXT NOT NULL,
            status TEXT NOT NULL);

        CREATE INDEX IF NOT EXISTS idx_scrobbles_user ON scrobbles(user_id, match_id);
        -- Deleting a match looks up its scrobbles, see delete_orphaned_matches().
        CREATE INDEX IF NOT EXISTS idx_scrobbles_match ON scrobbles(match_id);
        CREATE INDEX IF NOT EXISTS idx_matches_ts ON matches(ts DESC);

        -- Instance-wide choices made in the web page, such as the audio device.
        CREATE TABLE IF NOT EXISTS settings(
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL);

        -- One input each. A device room records with ffmpeg's `backend` from
        -- `device`, and empty values follow AUDIO_BACKEND and AUDIO_DEVICE. A
        -- stream room reads `url`, an RTSP or RTSPS camera for example.
        CREATE TABLE IF NOT EXISTS rooms(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL UNIQUE COLLATE NOCASE,
            kind TEXT NOT NULL DEFAULT 'device',
            backend TEXT, device TEXT, url TEXT,
            enabled INTEGER NOT NULL DEFAULT 1,
            created_at INTEGER NOT NULL);

        -- A room cannot know who is listening, so receiving what it hears is a
        -- choice each user makes, room by room.
        CREATE TABLE IF NOT EXISTS room_members(
            room_id INTEGER NOT NULL REFERENCES rooms(id) ON DELETE CASCADE,
            user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            PRIMARY KEY (room_id, user_id));

        -- The last scrobbled play of each track, per user, so a restart in the
        -- middle of a song does not scrobble it again. See should_scrobble().
        CREATE TABLE IF NOT EXISTS plays(
            user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            track_key TEXT NOT NULL,
            ts INTEGER NOT NULL,
            offset REAL,
            PRIMARY KEY (user_id, track_key));

        -- A screen by the hi-fi. Its token reads what the rooms play, nothing
        -- more, and is kept as a SHA-256 hash, so the database does not hold it.
        CREATE TABLE IF NOT EXISTS display_tokens(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            token_hash TEXT NOT NULL UNIQUE,
            created_at INTEGER NOT NULL,
            last_seen INTEGER);
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


def _add_rooms(c, fresh=False):
    """Turn the single microphone of schema 2 into the first room.

    A fresh database gets the same room, so a new install listens as before,
    unless CREATE_SERVER_MIC_ROOM is off. An older database always gets it,
    because it already had a microphone. The device chosen in Settings moves
    into the room, and so does each user's choice to receive the microphone.
    """
    columns = {r["name"] for r in c.execute("PRAGMA table_info(matches)")}
    if "room" not in columns:
        c.execute("ALTER TABLE matches ADD COLUMN room TEXT")
    if c.execute("SELECT COUNT(*) FROM rooms").fetchone()[0]:
        return
    if fresh and not config.CREATE_SERVER_MIC_ROOM:
        return
    saved = {r["key"]: r["value"] for r in c.execute(
        "SELECT key, value FROM settings WHERE key IN ('audio_backend', 'audio_device')")}
    room = c.execute(
        "INSERT INTO rooms(name, kind, backend, device, created_at)"
        " VALUES('Server mic', 'device', ?, ?, strftime('%s', 'now'))",
        (saved.get("audio_backend"), saved.get("audio_device"))).lastrowid
    c.execute("INSERT INTO room_members(room_id, user_id)"
              " SELECT ?, id FROM users WHERE room_mic=1", (room,))
    c.execute("DELETE FROM settings WHERE key IN ('audio_backend', 'audio_device')")


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
        fresh = version == 0 and not _table_exists(c, "matches")
        if old:
            # Park the old rows. adopt_legacy_history() moves them once the
            # owner account exists.
            c.execute("ALTER TABLE matches RENAME TO matches_v1")
        _create_schema(c)
        if version < 3:
            _add_rooms(c, fresh)
        c.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
    conn.close()


# --- matches and scrobbles -------------------------------------------------

def add_match(ts, artist, title, album, track_key, art_url, source, room=None) -> int:
    with _conn() as c:
        cur = c.execute(
            "INSERT INTO matches(ts, artist, title, album, track_key, art_url, source,"
            " room) VALUES(?,?,?,?,?,?,?,?)",
            (ts, artist, title, album, track_key, art_url, source, room),
        )
        return cur.lastrowid


def add_scrobbles(match_id, user_id, results: dict):
    with _conn() as c:
        c.executemany(
            "INSERT INTO scrobbles(match_id, user_id, service, status) VALUES(?,?,?,?)",
            [(match_id, user_id, s, status) for s, status in results.items()],
        )


def delete_orphaned_matches() -> int:
    """Delete matches no scrobble points to, which a deleted user leaves behind.
    History is read through scrobbles, so nobody can see these.

    Call it before the listener starts. A match shared by several users is
    written before the second user's scrobbles, so deleting the first user
    mid-cycle would briefly make it look orphaned.
    """
    with _conn() as c:
        return c.execute("DELETE FROM matches WHERE id NOT IN"
                         " (SELECT match_id FROM scrobbles)").rowcount


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
                    ("id", "ts", "artist", "title", "album", "track_key", "art_url", "source",
                     "room")}
            item["services"] = {}
            index[r["id"]] = item
            out.append(item)
        item["services"][r["service"]] = r["status"]
        if len(out) >= limit:
            break
    return out


def save_play(user_id, track_key, ts, offset, forget_before):
    """Keep a scrobbled play, and drop those older than forget_before."""
    with _conn() as c:
        c.execute("INSERT INTO plays(user_id, track_key, ts, offset) VALUES(?,?,?,?)"
                  " ON CONFLICT(user_id, track_key) DO UPDATE"
                  " SET ts=excluded.ts, offset=excluded.offset",
                  (user_id, track_key, ts, offset))
        c.execute("DELETE FROM plays WHERE ts < ?", (forget_before,))


def load_plays(forget_before) -> dict:
    """{(user_id, track_key): (ts, offset)} of the plays still worth knowing."""
    with _conn() as c:
        c.execute("DELETE FROM plays WHERE ts < ?", (forget_before,))
        return {(r["user_id"], r["track_key"]): (r["ts"], r["offset"])
                for r in c.execute("SELECT * FROM plays").fetchall()}


def scrobbled_since(user_id, ts) -> int:
    """How many plays a user scrobbled from ts on, to any service."""
    with _conn() as c:
        return c.execute("SELECT COUNT(DISTINCT m.id) FROM scrobbles s"
                         " JOIN matches m ON m.id = s.match_id"
                         " WHERE s.user_id = ? AND m.ts >= ?", (user_id, ts)).fetchone()[0]


# --- settings ----------------------------------------------------------------

def get_setting(key, default=None):
    with _conn() as c:
        row = c.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default


def set_setting(key, value):
    with _conn() as c:
        if value is None:
            c.execute("DELETE FROM settings WHERE key=?", (key,))
        else:
            c.execute("INSERT INTO settings(key, value) VALUES(?,?)"
                      " ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                      (key, str(value)))
