"""Env-var config. A service with all of its variables set is enabled."""
import os
import pathlib
import sys


def _load_env_file(path=".env"):
    """Read KEY=VALUE lines from .env so secrets stay out of the shell history.

    A real environment variable always wins over the file.
    """
    try:
        text = pathlib.Path(path).read_text()
    except OSError:
        return
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip("\"'"))


_load_env_file()

_darwin = sys.platform == "darwin"

AUDIO_BACKEND = os.environ.get("AUDIO_BACKEND", "avfoundation" if _darwin else "alsa")
AUDIO_DEVICE = os.environ.get("AUDIO_DEVICE", ":0" if _darwin else "default")
CHUNK_SECONDS = int(os.environ.get("CHUNK_SECONDS", "12"))
MATCH_INTERVAL = int(os.environ.get("MATCH_INTERVAL", "15"))  # min seconds per cycle
RESCROBBLE_MINUTES = int(os.environ.get("RESCROBBLE_MINUTES", "30"))
DB_PATH = os.environ.get("DB_PATH", "scrobster.db")
PORT = int(os.environ.get("PORT", "8000"))
API_TOKEN = os.environ.get("API_TOKEN")  # unset = no auth (LAN default)
LISTEN_ON_START = os.environ.get("LISTEN_ON_START", "1").lower() not in ("0", "false", "no")

LASTFM_API_KEY = os.environ.get("LASTFM_API_KEY")
LASTFM_API_SECRET = os.environ.get("LASTFM_API_SECRET")
LASTFM_USERNAME = os.environ.get("LASTFM_USERNAME")
# Preferred: a session key from `python -m scrobster.auth`. No password needed.
LASTFM_SESSION_KEY = os.environ.get("LASTFM_SESSION_KEY")
LASTFM_PASSWORD = os.environ.get("LASTFM_PASSWORD")
# Preferred: supply the md5 hash so no plaintext password is stored or exported.
LASTFM_PASSWORD_HASH = os.environ.get("LASTFM_PASSWORD_HASH")

LIBREFM_USERNAME = os.environ.get("LIBREFM_USERNAME")
LIBREFM_PASSWORD = os.environ.get("LIBREFM_PASSWORD")
LIBREFM_PASSWORD_HASH = os.environ.get("LIBREFM_PASSWORD_HASH")

LISTENBRAINZ_TOKEN = os.environ.get("LISTENBRAINZ_TOKEN")
LISTENBRAINZ_URL = os.environ.get("LISTENBRAINZ_URL", "https://api.listenbrainz.org")

MALOJA_URL = os.environ.get("MALOJA_URL")  # e.g. http://maloja:42010
MALOJA_KEY = os.environ.get("MALOJA_KEY")
