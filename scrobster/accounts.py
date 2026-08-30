"""Users, passwords, sessions and per-user service credentials.

Passwords use scrypt from hashlib, so there is no new dependency. Sessions are
random tokens in the database, which makes them revocable, unlike a signed
cookie.
"""
import hashlib
import hmac
import json
import logging
import secrets
import time

from . import config, db

log = logging.getLogger("scrobster")

SERVICES = ("lastfm", "librefm", "listenbrainz", "maloja")
SESSION_DAYS = 30
_SCRYPT = {"n": 2 ** 14, "r": 8, "p": 1, "dklen": 32, "maxmem": 64 * 1024 * 1024}


# --- passwords -------------------------------------------------------------

def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    dk = hashlib.scrypt(password.encode(), salt=salt, **_SCRYPT)
    return f"scrypt${salt.hex()}${dk.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        algorithm, salt_hex, hash_hex = stored.split("$")
        if algorithm != "scrypt":
            return False
        dk = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt_hex), **_SCRYPT)
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(dk.hex(), hash_hex)


# --- users -----------------------------------------------------------------

def _row_to_user(r):
    return None if r is None else dict(r)


def create_user(username, password, is_admin=False, room_mic=False) -> dict:
    username = username.strip()
    if not username:
        raise ValueError("the user name cannot be empty")
    if len(password) < 8:
        raise ValueError("the password must be at least 8 characters")
    with db._conn() as c:
        cur = c.execute(
            "INSERT INTO users(username, password_hash, is_admin, room_mic, api_token,"
            " created_at) VALUES(?,?,?,?,?,?)",
            (username, hash_password(password), int(is_admin), int(room_mic),
             secrets.token_urlsafe(24), int(time.time())),
        )
        user_id = cur.lastrowid
    # Read after the block, because the insert commits when the block ends and a
    # second connection cannot see it before that.
    return get_user(user_id)


def get_user(user_id):
    with db._conn() as c:
        return _row_to_user(
            c.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone())


def get_user_by_name(username):
    with db._conn() as c:
        return _row_to_user(
            c.execute("SELECT * FROM users WHERE username=?", (username,)).fetchone())


def get_user_by_token(token):
    if not token:
        return None
    with db._conn() as c:
        return _row_to_user(
            c.execute("SELECT * FROM users WHERE api_token=?", (token,)).fetchone())


def first_admin():
    """The owner. Requests through Home Assistant ingress act as this account."""
    with db._conn() as c:
        return _row_to_user(c.execute(
            "SELECT * FROM users WHERE is_admin=1 ORDER BY id LIMIT 1").fetchone())


def list_users():
    with db._conn() as c:
        return [dict(r) for r in
                c.execute("SELECT * FROM users ORDER BY id").fetchall()]


def count_users() -> int:
    with db._conn() as c:
        return c.execute("SELECT COUNT(*) FROM users").fetchone()[0]


def room_mic_users():
    """Users who chose to receive what the server microphone hears."""
    with db._conn() as c:
        return [dict(r) for r in
                c.execute("SELECT * FROM users WHERE room_mic=1").fetchall()]


def update_user(user_id, *, room_mic=None, is_admin=None, password=None):
    sets, values = [], []
    if room_mic is not None:
        sets.append("room_mic=?")
        values.append(int(room_mic))
    if is_admin is not None:
        sets.append("is_admin=?")
        values.append(int(is_admin))
    if password is not None:
        if len(password) < 8:
            raise ValueError("the password must be at least 8 characters")
        sets.append("password_hash=?")
        values.append(hash_password(password))
    if not sets:
        return get_user(user_id)
    values.append(user_id)
    with db._conn() as c:
        c.execute(f"UPDATE users SET {', '.join(sets)} WHERE id=?", values)
    return get_user(user_id)


def delete_user(user_id):
    with db._conn() as c:
        c.execute("DELETE FROM users WHERE id=?", (user_id,))


def new_api_token(user_id) -> str:
    token = secrets.token_urlsafe(24)
    with db._conn() as c:
        c.execute("UPDATE users SET api_token=? WHERE id=?", (token, user_id))
    return token


# --- sessions --------------------------------------------------------------

def start_session(user_id) -> str:
    token = secrets.token_urlsafe(32)
    now = int(time.time())
    with db._conn() as c:
        c.execute("INSERT INTO sessions(token, user_id, created_at, last_seen)"
                  " VALUES(?,?,?,?)", (token, user_id, now, now))
    return token


def session_user(token):
    if not token:
        return None
    cutoff = int(time.time()) - SESSION_DAYS * 86400
    with db._conn() as c:
        row = c.execute(
            "SELECT u.* FROM sessions s JOIN users u ON u.id = s.user_id"
            " WHERE s.token=? AND s.created_at >= ?", (token, cutoff)).fetchone()
        if row is not None:
            c.execute("UPDATE sessions SET last_seen=? WHERE token=?",
                      (int(time.time()), token))
    return _row_to_user(row)


def end_session(token):
    with db._conn() as c:
        c.execute("DELETE FROM sessions WHERE token=?", (token,))


# --- per-user service credentials ------------------------------------------

def get_credentials(user_id) -> dict:
    """Returns {service: {field: value}} for one user."""
    with db._conn() as c:
        rows = c.execute("SELECT service, data FROM credentials WHERE user_id=?",
                         (user_id,)).fetchall()
    out = {}
    for r in rows:
        try:
            out[r["service"]] = json.loads(r["data"])
        except ValueError:
            continue
    return out


def set_credential(user_id, service, data: dict):
    if service not in SERVICES:
        raise ValueError(f"unknown service: {service}")
    with db._conn() as c:
        c.execute("INSERT INTO credentials(user_id, service, data) VALUES(?,?,?)"
                  " ON CONFLICT(user_id, service) DO UPDATE SET data=excluded.data",
                  (user_id, service, json.dumps(data)))


def delete_credential(user_id, service):
    with db._conn() as c:
        c.execute("DELETE FROM credentials WHERE user_id=? AND service=?",
                  (user_id, service))


# --- first run -------------------------------------------------------------

def _credentials_from_env() -> dict:
    """Carry a single-user environment setup into the first account."""
    found = {}
    if config.LISTENBRAINZ_TOKEN:
        found["listenbrainz"] = {"token": config.LISTENBRAINZ_TOKEN,
                                 "url": config.LISTENBRAINZ_URL}
    if config.LASTFM_SESSION_KEY or config.LASTFM_PASSWORD_HASH or config.LASTFM_PASSWORD:
        found["lastfm"] = {"session_key": config.LASTFM_SESSION_KEY,
                           "username": config.LASTFM_USERNAME,
                           "password_hash": config.LASTFM_PASSWORD_HASH,
                           "password": config.LASTFM_PASSWORD}
    if config.LIBREFM_USERNAME:
        found["librefm"] = {"username": config.LIBREFM_USERNAME,
                            "password_hash": config.LIBREFM_PASSWORD_HASH,
                            "password": config.LIBREFM_PASSWORD}
    if config.MALOJA_URL and config.MALOJA_KEY:
        found["maloja"] = {"url": config.MALOJA_URL, "key": config.MALOJA_KEY}
    return found


def ensure_first_user():
    """Create the owner account on first run, and keep an existing setup working.

    The password comes from ADMIN_PASSWORD. Without it a random one is generated
    and written to the log once, because an account with no password would leave
    the instance open.
    """
    if count_users() > 0:
        return None
    password = config.ADMIN_PASSWORD or secrets.token_urlsafe(12)
    user = create_user(config.ADMIN_USERNAME, password, is_admin=True, room_mic=True)
    for service, data in _credentials_from_env().items():
        set_credential(user["id"], service, data)
    moved = db.adopt_legacy_history(user["id"])
    if moved:
        log.info("Gave %d earlier scrobbles to %r.", moved, user["username"])
    if not config.ADMIN_PASSWORD:
        log.warning("Created the first account %r with the password %s -"
                    " change it in Settings, or set ADMIN_PASSWORD.",
                    user["username"], password)
    else:
        log.info("Created the first account %r.", user["username"])
    return user
