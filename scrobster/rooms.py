"""Rooms: the inputs scrobSter listens to, and who receives each one.

A room is a microphone on this machine or a stream, such as a camera's RTSP or
RTSPS feed. A camera URL usually carries a password, so the password never
leaves the server: the API shows it as MASK, and error text from ffmpeg is
redacted before anybody can read it.
"""
import re
import time
from urllib.parse import unquote, urlsplit

from . import db

KINDS = ("device", "stream")
STREAM_SCHEMES = ("rtsp", "rtsps", "http", "https")
MASK = "***"
NAME_MAX = 40


# --- camera URLs -------------------------------------------------------------

def _password(url):
    """The password as written in the URL, still percent-encoded, or None."""
    userinfo, at, _ = urlsplit(url).netloc.rpartition("@")
    if not at or ":" not in userinfo:
        return None
    return userinfo.partition(":")[2]


def mask_url(url):
    """The URL with its password replaced by MASK, safe to show."""
    if not url:
        return url
    password = _password(url)
    if not password:
        return url
    parts = urlsplit(url)
    userinfo, _, host = parts.netloc.rpartition("@")
    netloc = f"{userinfo.partition(':')[0]}:{MASK}@{host}"
    return parts._replace(netloc=netloc).geturl()


def merge_url(new, old):
    """Apply an edited URL. A password still shown as MASK keeps the saved one,
    so the form can change a path without asking for the password again."""
    if _password(new) != MASK:
        return new
    saved = _password(old or "")
    if saved is None:
        raise ValueError(f"type the camera password in place of {MASK}")
    parts = urlsplit(new)
    userinfo, _, host = parts.netloc.rpartition("@")
    netloc = f"{userinfo.partition(':')[0]}:{saved}@{host}"
    return parts._replace(netloc=netloc).geturl()


def redact(text, url):
    """Remove a URL's password from text, such as an ffmpeg error that quotes
    the input it could not open."""
    if not text or not url:
        return text
    text = text.replace(url, mask_url(url))
    password = _password(url)
    # A very short password would also hide ordinary letters. The full URL,
    # which is what ffmpeg quotes, is already gone.
    if password and len(password) >= 4:
        for form in {password, unquote(password)}:
            text = text.replace(form, MASK)
    return text


def check_url(url):
    """A stream URL ffmpeg should open. Anything else, a local file for one, is
    refused, because ffmpeg would read it."""
    url = (url or "").strip()
    parts = urlsplit(url)
    if parts.scheme.lower() not in STREAM_SCHEMES or not parts.hostname:
        raise ValueError("a camera URL starts with rtsp://, rtsps://, http:// or https://"
                         " and names a host")
    if re.search(r"\s", url):
        raise ValueError("a camera URL cannot contain spaces")
    return url


# --- storage -----------------------------------------------------------------

def list_rooms():
    with db._conn() as c:
        return [dict(r) for r in c.execute("SELECT * FROM rooms ORDER BY id").fetchall()]


def get_room(room_id):
    with db._conn() as c:
        row = c.execute("SELECT * FROM rooms WHERE id=?", (room_id,)).fetchone()
    return None if row is None else dict(row)


def _clean(fields, existing=None):
    """Check a room's fields and fill in what an edit left out."""
    room = dict(existing or {"kind": "device", "enabled": 1})
    for key in ("name", "kind", "backend", "device", "url", "enabled"):
        if key in fields and fields[key] is not None:
            room[key] = fields[key]
    name = str(room.get("name") or "").strip()
    if not name:
        raise ValueError("give the room a name")
    if len(name) > NAME_MAX:
        raise ValueError(f"keep the name to {NAME_MAX} characters")
    kind = room.get("kind")
    if kind not in KINDS:
        raise ValueError(f"a room is one of {', '.join(KINDS)}")
    out = {"name": name, "kind": kind, "enabled": int(bool(room.get("enabled"))),
           "backend": None, "device": None, "url": None}
    if kind == "stream":
        out["url"] = check_url(merge_url(str(room.get("url") or ""),
                                         (existing or {}).get("url")))
    else:
        backend = str(room.get("backend") or "").strip() or None
        if backend and not re.fullmatch(r"[a-z0-9_]+", backend):
            raise ValueError("that is not an ffmpeg input format")
        out["backend"] = backend
        # Empty follows AUDIO_DEVICE, like the first room of an upgrade.
        out["device"] = str(room.get("device") or "").strip() or None
    return out


def add_room(**fields) -> dict:
    room = _clean(fields)
    with db._conn() as c:
        if c.execute("SELECT 1 FROM rooms WHERE name=?", (room["name"],)).fetchone():
            raise ValueError("another room has that name")
        room_id = c.execute(
            "INSERT INTO rooms(name, kind, backend, device, url, enabled, created_at)"
            " VALUES(?,?,?,?,?,?,?)",
            (room["name"], room["kind"], room["backend"], room["device"], room["url"],
             room["enabled"], int(time.time()))).lastrowid
    return get_room(room_id)


def update_room(room_id, **fields) -> dict:
    existing = get_room(room_id)
    if existing is None:
        raise LookupError("no such room")
    room = _clean(fields, existing)
    with db._conn() as c:
        if c.execute("SELECT 1 FROM rooms WHERE name=? AND id!=?",
                     (room["name"], room_id)).fetchone():
            raise ValueError("another room has that name")
        c.execute("UPDATE rooms SET name=?, kind=?, backend=?, device=?, url=?, enabled=?"
                  " WHERE id=?", (room["name"], room["kind"], room["backend"],
                                  room["device"], room["url"], room["enabled"], room_id))
    return get_room(room_id)


def delete_room(room_id):
    with db._conn() as c:
        c.execute("DELETE FROM rooms WHERE id=?", (room_id,))


# --- who receives a room -------------------------------------------------------

def members(room_id) -> list[dict]:
    """Users who chose to receive what this room hears."""
    with db._conn() as c:
        return [dict(r) for r in c.execute(
            "SELECT u.* FROM room_members m JOIN users u ON u.id = m.user_id"
            " WHERE m.room_id=? ORDER BY u.id", (room_id,)).fetchall()]


def rooms_of(user_id) -> list[int]:
    with db._conn() as c:
        return [r[0] for r in c.execute(
            "SELECT room_id FROM room_members WHERE user_id=? ORDER BY room_id",
            (user_id,)).fetchall()]


def set_member(room_id, user_id, on: bool):
    with db._conn() as c:
        if on:
            c.execute("INSERT OR IGNORE INTO room_members(room_id, user_id) VALUES(?,?)",
                      (room_id, user_id))
        else:
            c.execute("DELETE FROM room_members WHERE room_id=? AND user_id=?",
                      (room_id, user_id))


def join_all(user_id):
    with db._conn() as c:
        c.execute("INSERT OR IGNORE INTO room_members(room_id, user_id)"
                  " SELECT id, ? FROM rooms", (user_id,))
