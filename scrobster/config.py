"""Env-var config. A service with all of its variables set is enabled."""
import json
import os
import pathlib
import sys


def _load_options_json(path="/data/options.json"):
    """Read Home Assistant add-on options.

    The Supervisor validates the options and writes them here. Keys match the
    environment variable names, so no wrapper script is needed. A real
    environment variable still wins.
    """
    try:
        data = json.loads(pathlib.Path(path).read_text())
    except (OSError, ValueError):
        return
    if not isinstance(data, dict):
        return
    for key, value in data.items():
        if value is None or value == "":
            continue
        if isinstance(value, bool):
            value = "true" if value else "false"
        os.environ.setdefault(key.upper(), str(value))


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
_load_options_json()


def _drop_stale_cert_file():
    """Ignore SSL_CERT_FILE when it points at a file that is not there.

    macOS python.org builds need this variable, so it ends up in .env. Carrying
    that .env into a container would otherwise break every HTTPS call, because
    the path does not exist there and the system certificates are fine.
    """
    cert = os.environ.get("SSL_CERT_FILE")
    if cert and not pathlib.Path(cert).is_file():
        del os.environ["SSL_CERT_FILE"]


_drop_stale_cert_file()

_darwin = sys.platform == "darwin"

# The device a room records from when its own device is left empty. The first
# room of an install leaves it empty, so these keep working as before.
AUDIO_BACKEND = os.environ.get("AUDIO_BACKEND", "avfoundation" if _darwin else "alsa")
AUDIO_DEVICE = os.environ.get("AUDIO_DEVICE", ":0" if _darwin else "default")
CHUNK_SECONDS = int(os.environ.get("CHUNK_SECONDS", "12"))
MATCH_INTERVAL = int(os.environ.get("MATCH_INTERVAL", "15"))  # min seconds per cycle
# Every room and browser clip asks Shazam from one address, so requests wait
# their turn: at most one per this many seconds, whatever the number of rooms.
RECOGNITION_GAP_SECONDS = float(os.environ.get("RECOGNITION_GAP_SECONDS", "4"))
# The two rates above are where an install starts. An administrator can change
# them on the Rooms tab, which keeps them in the database, outranks these, and
# applies them with set_rates() without a restart.
ENV_MATCH_INTERVAL = MATCH_INTERVAL
ENV_RECOGNITION_GAP_SECONDS = RECOGNITION_GAP_SECONDS
# The same limits as the Home Assistant add-on options. Below 12 seconds a room
# asks Shazam more often than is safe from one address.
MATCH_INTERVAL_RANGE = (12, 600)
RECOGNITION_GAP_RANGE = (1, 60)


def set_rates(gap=None, interval=None):
    """Change the rates for the running process. None keeps a value."""
    global RECOGNITION_GAP_SECONDS, MATCH_INTERVAL
    if gap is not None:
        RECOGNITION_GAP_SECONDS = float(gap)
    if interval is not None:
        MATCH_INTERVAL = int(interval)
RESCROBBLE_MINUTES = int(os.environ.get("RESCROBBLE_MINUTES", "30"))
# Clear the playing-now mark after this long with no match. Keep it well above one
# song, because matches drop out during quiet passages and the mark would flicker.
NOW_PLAYING_STOP_SECONDS = int(os.environ.get("NOW_PLAYING_STOP_SECONDS", "180"))
DB_PATH = os.environ.get("DB_PATH", "scrobster.db")
PORT = int(os.environ.get("PORT", "8000"))
# Legacy shared token. It still works, as an admin key, so an existing Home
# Assistant REST sensor keeps reading the API after accounts were introduced.
API_TOKEN = os.environ.get("API_TOKEN")
ADMIN_USERNAME = os.environ.get("ADMIN_USERNAME", "admin")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD")
# Home Assistant authenticates the user before ingress, and its documentation
# says an add-on must not ask again. Requests from the ingress address are
# therefore treated as the owner.
TRUST_INGRESS = os.environ.get("TRUST_INGRESS", "1").lower() not in ("0", "false", "no")
INGRESS_IP = os.environ.get("INGRESS_IP", "172.30.32.2")
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
