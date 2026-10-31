#!/usr/bin/env python3
"""Make the screenshots in docs/images from fake data.

    .venv/bin/python scripts/screenshots.py

Runs the real app in this process with a fresh database in a temporary folder,
seeded with made-up accounts, rooms and plays. Everything that would touch the
outside world is replaced here, not in the app: the audio devices, ffmpeg,
Shazam, the scrobbling services, MusicBrainz and the cover art. A guard on the
socket module refuses any connection that is not to this machine, and the run
fails if anything tried one. Then scripts/screenshots.mjs drives Chromium with
Playwright and the pictures are shrunk with pngquant and optipng when they are
installed.

Needs Node.js with the playwright package and its Chromium (`npm install
playwright` and `npx playwright install chromium`, or NODE_PATH pointing at a
global install).

The developer's .env is never read: the process changes to the temporary
folder before scrobster is imported, and every variable config.py reads is
set here first.
"""
import array
import asyncio
import hashlib
import io
import math
import os
import pathlib
import random
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import wave

ROOT = pathlib.Path(__file__).resolve().parent.parent
OUT = ROOT / "docs" / "images"
DRIVER = ROOT / "scripts" / "screenshots.mjs"

ADMIN, ADMIN_PASSWORD = "alex", "screenshots-only"
OTHER, OTHER_PASSWORD = "sam", "screenshots-only"
# Seen by nobody: the services only need something in these fields to count
# as connected, and the page never shows a secret.
PLACEHOLDER = "placeholder-not-a-real-secret"

TURNTABLE = "hw:CARD=CODEC,DEV=0"
CAMERA = f"rtsps://viewer:{PLACEHOLDER}@camera.local:7441/kitchenStream0"
OFFICE = "hw:CARD=PCH,DEV=0"


# --- 1. a clean environment, before scrobster is imported ----------------------

TMP = pathlib.Path(tempfile.mkdtemp(prefix="scrobster-shots-"))
os.chdir(TMP)  # config.py reads .env from the working folder

_config_src = (ROOT / "scrobster" / "config.py").read_text()
for name in set(re.findall(r'os\.environ(?:\.get\(|\[)"([A-Z0-9_]+)"', _config_src)):
    # Empty is unset for config.py, and it keeps /data/options.json from
    # filling the name in, because that only sets what is missing.
    os.environ[name] = ""
os.environ.update({
    "DB_PATH": str(TMP / "scrobster.db"),
    "PORT": "8000",
    "ADMIN_USERNAME": ADMIN,
    "ADMIN_PASSWORD": ADMIN_PASSWORD,
    "AUDIO_BACKEND": "alsa",
    "AUDIO_DEVICE": "default",
    "CHUNK_SECONDS": "12",
    "MATCH_INTERVAL": "15",
    "RECOGNITION_GAP_SECONDS": "4",
    "SKIP_KNOWN_SECONDS": "60",
    "RESCROBBLE_MINUTES": "30",
    "NOW_PLAYING_STOP_SECONDS": "180",
    "LISTEN_ON_START": "1",
    "TRUST_INGRESS": "0",
    "INGRESS_IP": "192.0.2.1",
    "MQTT_ENABLED": "0",
    "MQTT_PORT": "1883",
    "MQTT_TOPIC": "scrobster",
    "MQTT_DISCOVERY_PREFIX": "homeassistant",
    # Last.fm counts as connected only when the app is registered.
    "LASTFM_API_KEY": PLACEHOLDER,
    "LASTFM_API_SECRET": PLACEHOLDER,
    "LISTENBRAINZ_URL": "https://api.listenbrainz.org",
})
# A library that honours a proxy finds nothing there.
for name in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy", "ALL_PROXY", "all_proxy"):
    os.environ[name] = "http://127.0.0.1:9"
for name in ("NO_PROXY", "no_proxy"):
    os.environ.pop(name, None)


# --- 2. no network but this machine ---------------------------------------------

BLOCKED = []
_LOOPBACK = {"127.0.0.1", "::1", "localhost", "0.0.0.0", "", None}
_getaddrinfo = socket.getaddrinfo
_connect, _connect_ex = socket.socket.connect, socket.socket.connect_ex


def _host(h):
    return h.decode() if isinstance(h, bytes) else h


def _guard_getaddrinfo(host, *args, **kwargs):
    if _host(host) not in _LOOPBACK:
        BLOCKED.append(f"resolve {_host(host)}")
        raise OSError(f"screenshots: no network, refused to resolve {_host(host)}")
    return _getaddrinfo(host, *args, **kwargs)


def _remote(sock, address):
    return (sock.family in (socket.AF_INET, socket.AF_INET6)
            and isinstance(address, tuple) and _host(address[0]) not in _LOOPBACK)


def _guard_connect(self, address):
    if _remote(self, address):
        BLOCKED.append(f"connect {address[0]}:{address[1]}")
        raise OSError(f"screenshots: no network, refused {address[0]}")
    return _connect(self, address)


def _guard_connect_ex(self, address):
    if _remote(self, address):
        BLOCKED.append(f"connect {address[0]}:{address[1]}")
        raise OSError(f"screenshots: no network, refused {address[0]}")
    return _connect_ex(self, address)


socket.getaddrinfo = _guard_getaddrinfo
socket.socket.connect = _guard_connect
socket.socket.connect_ex = _guard_connect_ex

for probe in (lambda: socket.create_connection(("example.org", 443), timeout=2),
              lambda: socket.socket().connect(("192.0.2.1", 443))):
    try:
        probe()
    except OSError:
        pass
    else:
        sys.exit("the network guard let a connection through")
assert len(BLOCKED) == 2, BLOCKED
BLOCKED.clear()


# --- 3. scrobster, with the outside world replaced -----------------------------

sys.path.insert(0, str(ROOT))
from scrobster import accounts, app, config, db, enrich, listener, rooms, scrobble  # noqa: E402
import uvicorn  # noqa: E402
from fastapi.responses import Response  # noqa: E402

rng = random.Random(7)

# Album -> two colours for a made-up cover, so no real artwork is fetched.
ALBUMS = {
    "Rumours": ("#c9a27e", "#3b2a20"),
    "The Dark Side of the Moon": ("#1b1b2f", "#6c4ab6"),
    "Kind of Blue": ("#1d3557", "#a8dadc"),
    "Random Access Memories": ("#2b2d42", "#d4af37"),
    "In Rainbows": ("#e63946", "#f4a261"),
    "Hounds of Love": ("#5e3c58", "#c08497"),
    "Talking Book": ("#6b4226", "#e9c46a"),
    "Menagerie": ("#264653", "#e76f51"),
    "Speaking in Tongues": ("#f72585", "#4361ee"),
    "Sound of Silver": ("#adb5bd", "#212529"),
    "Abbey Road": ("#3a86ff", "#8ecae6"),
    "I Put a Spell on You": ("#370617", "#dc2f02"),
    "AM": ("#0b090a", "#f5f3f4"),
    "Currents": ("#7209b7", "#f72585"),
    "Blue Lines": ("#0d1b2a", "#4ea8de"),
    "Discovery": ("#003049", "#fcbf49"),
}


def slug(s):
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")


def art_url(album):
    return f"demo-art/{slug(album)}.svg"


def cover_svg(name):
    album = next((a for a in ALBUMS if slug(a) == name), None)
    a, b = ALBUMS.get(album, ("#30363d", "#8b949e"))
    seed = int(hashlib.md5(name.encode()).hexdigest()[:6], 16)
    shape = seed % 3
    if shape == 0:
        art = f'<circle cx="150" cy="150" r="{70 + seed % 40}" fill="{b}" opacity=".85"/>'
    elif shape == 1:
        art = "".join(f'<rect x="0" y="{y}" width="300" height="14" fill="{b}" opacity=".55"/>'
                      for y in range(40 + seed % 20, 300, 44))
    else:
        art = "".join(f'<circle cx="150" cy="150" r="{r}" fill="none" stroke="{b}" '
                      f'stroke-width="6" opacity=".7"/>' for r in range(30, 150, 28))
    return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 300 300">'
            f'<defs><linearGradient id="g" x1="0" y1="0" x2="1" y2="1">'
            f'<stop offset="0" stop-color="{a}"/><stop offset="1" stop-color="{b}" '
            f'stop-opacity=".6"/></linearGradient></defs>'
            f'<rect width="300" height="300" fill="{a}"/><rect width="300" height="300" '
            f'fill="url(#g)"/>{art}</svg>')


@app.app.get("/demo-art/{name}.svg")
async def _demo_art(name: str):
    return Response(cover_svg(name), media_type="image/svg+xml")


def track_key(artist, title):
    return str(int(hashlib.md5(f"{artist}/{title}".encode()).hexdigest()[:7], 16))


# Audio: a tone at a level, in the WAV the app expects. Each clip is
# remembered by its bytes, so the fake Shazam knows which input made it.
HEARD = {}  # sha1 of the WAV -> what it is


def tone_wav(db_peak, freq, seconds=1.0):
    n = int(16000 * seconds)
    amp = 32767 * 10 ** (db_peak / 20)
    samples = array.array("h", (int(amp * math.sin(2 * math.pi * freq * i / 16000)
                                    * (0.6 + 0.4 * math.sin(i / 900)))
                                for i in range(n)))
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        w.writeframes(samples.tobytes())
    return buf.getvalue()


# What each input "hears": (name, typical peak dBFS, swing, tone).
INPUTS = {TURNTABLE: ("turntable", -12.0, 4.0, 330),
          CAMERA: ("kitchen", -29.0, 5.0, 520),
          OFFICE: ("office", -45.0, 3.0, 660)}


async def fake_capture_chunk(seconds=None, backend=None, device=None, url=None,
                             on_level=None):
    """Record nothing: tick the live meter in real time and return a tone."""
    seconds = seconds or config.CHUNK_SECONDS
    name, level, swing, freq = INPUTS.get(url or device, ("other", -40.0, 3.0, 440))
    started, t = time.monotonic(), 0.0
    while time.monotonic() - started < seconds:
        await asyncio.sleep(0.25)
        t += 0.25
        if on_level:
            db_now = level + swing * math.sin(t * 1.3) + rng.uniform(-1.5, 1.5)
            on_level(round(min(-1.0, db_now), 1))
    wav = tone_wav(level + swing / 2, freq)
    HEARD[hashlib.sha1(wav).hexdigest()] = name
    return wav


async def fake_probe_level(backend=None, device=None, url=None, seconds=3):
    await asyncio.sleep(0.8)
    return {TURNTABLE: -14.2, CAMERA: -27.6}.get(url or device, -41.0)


async def fake_list_devices(backend):
    return [
        {"device": "default", "label": "Default ALSA device", "default": True},
        {"device": TURNTABLE, "label": "USB Audio CODEC, USB Audio", "numeric": "hw:1,0"},
        {"device": OFFICE, "label": "HDA Intel PCH, ALC3232 Analog", "numeric": "hw:0,0"},
        {"device": "hw:CARD=PCH,DEV=2", "label": "HDA Intel PCH, ALC3232 Alt Analog",
         "numeric": "hw:0,2"},
    ]


async def fake_decode_to_wav(data):
    wav = tone_wav(-21.0, 440)
    HEARD[hashlib.sha1(wav).hexdigest()] = "browser"
    return wav


SCRIPT_START = time.time()
# What Shazam "answers" for each input. The kitchen hears nothing it knows.
LIVE = {
    "turntable": ("Fleetwood Mac", "Dreams", "Rumours", 95),
    "browser": ("The Beatles", "Here Comes the Sun", "Abbey Road", 40),
}


def shazam_result(artist, title, album, offset):
    return {"matches": [{"offset": offset}],
            "track": {"key": track_key(artist, title), "title": title, "subtitle": artist,
                      "images": {"coverart": art_url(album)},
                      "sections": [{"metadata": [{"title": "Album", "text": album}]}]}}


class FakeShazam:
    async def recognize(self, wav):
        await asyncio.sleep(0.4)
        song = LIVE.get(HEARD.get(hashlib.sha1(wav).hexdigest()))
        if not song:
            return {"matches": []}
        artist, title, album, start = song
        return shazam_result(artist, title, album, start + time.time() - SCRIPT_START)


async def fake_send_all(credentials, *args, **kwargs):
    return {s: "ok" for s in scrobble.enabled_services(credentials)}


async def fake_clear_all(credentials):
    return {}


async def fake_listenbrainz_username(url, token):
    return ADMIN


async def fake_musicbrainz_ids(*args):
    return {}


app.listener._shazam = FakeShazam()
listener.capture_chunk = fake_capture_chunk
app.probe_level = fake_probe_level
app.list_devices = fake_list_devices
app.decode_to_wav = fake_decode_to_wav
scrobble.scrobble_all = scrobble.now_playing_all = fake_send_all
scrobble.clear_now_playing_all = fake_clear_all
scrobble.listenbrainz_username = fake_listenbrainz_username
enrich.musicbrainz_ids = fake_musicbrainz_ids
enrich.look_up_length = lambda *args: None
enrich.maloja_cover = lambda *args: None


# --- 4. fake but plausible data ----------------------------------------------------

# (minutes ago the session started, room, [(artist, title, album, minutes long)])
SESSIONS = [
    (9, "Living room turntable", [
        ("Fleetwood Mac", "Second Hand News", "Rumours", 2.9)]),
    (68, "Kitchen camera", [
        ("Nina Simone", "Feeling Good", "I Put a Spell on You", 2.9),
        ("Stevie Wonder", "Superstition", "Talking Book", 4.5),
        ("Bill Withers", "Lovely Day", "Menagerie", 4.3)]),
    (60 * 26 + 10, "Living room turntable", [
        ("Pink Floyd", "Time", "The Dark Side of the Moon", 7.1),
        ("Pink Floyd", "The Great Gig in the Sky", "The Dark Side of the Moon", 4.7),
        ("Pink Floyd", "Money", "The Dark Side of the Moon", 6.4),
        ("Pink Floyd", "Us and Them", "The Dark Side of the Moon", 7.8)]),
    (60 * 28 + 30, "Living room turntable", [
        ("Miles Davis", "So What", "Kind of Blue", 9.4),
        ("Miles Davis", "Freddie Freeloader", "Kind of Blue", 9.8),
        ("Miles Davis", "Blue in Green", "Kind of Blue", 5.6)]),
    (60 * 33, "Kitchen camera", [
        ("Daft Punk", "Get Lucky", "Random Access Memories", 6.1),
        ("Massive Attack", "Unfinished Sympathy", "Blue Lines", 5.2),
        ("Talking Heads", "This Must Be the Place (Naive Melody)", "Speaking in Tongues", 4.9)]),
    (60 * 49, None, [
        ("Tame Impala", "The Less I Know the Better", "Currents", 3.6)]),
    (60 * 51, "Living room turntable", [
        ("Kate Bush", "Running Up That Hill (A Deal with God)", "Hounds of Love", 5.0),
        ("Kate Bush", "Hounds of Love", "Hounds of Love", 3.0),
        ("Kate Bush", "The Big Sky", "Hounds of Love", 4.6),
        ("Kate Bush", "Cloudbusting", "Hounds of Love", 5.2)]),
    (60 * 56, "Kitchen camera", [
        ("Arctic Monkeys", "Do I Wanna Know?", "AM", 4.5),
        ("Daft Punk", "Digital Love", "Discovery", 5.0),
        ("LCD Soundsystem", "All My Friends", "Sound of Silver", 7.6)]),
    (60 * 74, "Living room turntable", [
        ("Radiohead", "15 Step", "In Rainbows", 4.0),
        ("Radiohead", "Bodysnatchers", "In Rainbows", 4.0),
        ("Radiohead", "Nude", "In Rainbows", 4.3),
        ("Radiohead", "Weird Fishes/Arpeggi", "In Rainbows", 5.3),
        ("Radiohead", "Reckoner", "In Rainbows", 4.8)]),
    (60 * 79, "Kitchen camera", [
        ("Stevie Wonder", "You Are the Sunshine of My Life", "Talking Book", 2.9),
        ("Bill Withers", "Lovely Day", "Menagerie", 4.3)]),
]
MALOJA_SINCE_HOURS = 60  # older plays went to Last.fm and ListenBrainz only


def seed():
    db.init()
    accounts.ensure_first_user()
    alex = accounts.get_user_by_name(ADMIN)
    sam = accounts.create_user(OTHER, OTHER_PASSWORD)

    first = rooms.list_rooms()[0]  # db.init() makes "Server mic"
    turntable = rooms.update_room(first["id"], name="Living room turntable",
                                  kind="device", device=TURNTABLE, backend="")
    kitchen = rooms.add_room(name="Kitchen camera", kind="stream", url=CAMERA)
    office = rooms.add_room(name="Office mic", kind="device", device=OFFICE, enabled=0)
    for room in (turntable, kitchen, office):
        rooms.set_member(room["id"], alex["id"], True)
    rooms.set_member(turntable["id"], sam["id"], True)

    accounts.set_credential(alex["id"], "lastfm",
                            {"session_key": PLACEHOLDER, "username": "alex_spins"})
    accounts.set_credential(alex["id"], "listenbrainz",
                            {"token": PLACEHOLDER, "username": "alex_spins"})
    accounts.set_credential(alex["id"], "maloja",
                            {"url": "http://maloja:42010", "key": PLACEHOLDER})
    accounts.set_credential(sam["id"], "lastfm",
                            {"session_key": PLACEHOLDER, "username": "sam_listens"})

    now = time.time()
    for started, room, tracks in SESSIONS:
        ts = now - started * 60
        for artist, title, album, minutes in tracks:
            ts += rng.uniform(25, 70)  # Shazam needs a moment into the song
            match = db.add_match(int(ts), artist, title, album, track_key(artist, title),
                                 art_url(album), "browser" if room is None else "server",
                                 room)
            services = {"lastfm": "ok", "listenbrainz": "ok"}
            if started < MALOJA_SINCE_HOURS * 60:
                services["maloja"] = "ok"
            db.add_scrobbles(match, alex["id"], services)
            if room == "Living room turntable":
                db.add_scrobbles(match, sam["id"], {"lastfm": "ok"})
            ts += minutes * 60 - 40

    # The song playing now was scrobbled when it was first heard, a minute
    # and a half ago, so the live match continues that play.
    artist, title, album, offset = LIVE["turntable"]
    heard = int(now - 70)
    match = db.add_match(heard, artist, title, album, track_key(artist, title),
                         art_url(album), "server", "Living room turntable")
    db.add_scrobbles(match, alex["id"], {"lastfm": "ok", "listenbrainz": "ok", "maloja": "ok"})
    db.add_scrobbles(match, sam["id"], {"lastfm": "ok"})
    for user in (alex, sam):
        db.save_play(user["id"], track_key(artist, title), heard, offset - 70, 0)
    kitchen_heard = max(r["ts"] for r in db.recent(alex["id"], 500)
                        if r["room"] == "Kitchen camera")
    return turntable["id"], kitchen["id"], kitchen_heard


def warm_up(turntable_id, kitchen_id, kitchen_heard):
    """Make the rooms look like they have been listening for a while."""
    live = app.listener
    now = time.time()
    live.started_at = int(now - 3 * 3600)
    tt, kitchen = live.rooms[turntable_id], live.rooms[kitchen_id]
    for room, attempts in ((tt, 431), (kitchen, 428)):
        room.attempts, room.last_attempt = attempts, int(now - 4)
        room.started_at = int(now - 3 * 3600)
    tt.last_level_db, kitchen.last_level_db = -10.6, -26.9
    artist, title, album, offset = LIVE["turntable"]
    tt.last_match = {**listener.parse_track(shazam_result(artist, title, album, offset)),
                     "ts": int(now - 20), "source": "server", "room": tt.name,
                     "room_id": tt.id}
    kitchen.last_match = {**listener.parse_track(shazam_result(
        "Bill Withers", "Lovely Day", "Menagerie", 60)),
        "ts": kitchen_heard, "source": "server", "room": kitchen.name,
        "room_id": kitchen.id}
    # Two rooms at one request per 15 s, less the cycles a known song skipped.
    mono = time.monotonic()
    live._requests.extend(sorted(mono - rng.uniform(5, 590) for _ in range(31)))


# --- 5. run it, and point a browser at it ------------------------------------------

def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def shrink(raw: pathlib.Path):
    OUT.mkdir(parents=True, exist_ok=True)
    pngquant, optipng = shutil.which("pngquant"), shutil.which("optipng")
    if not pngquant:
        print("pngquant is not installed: the pictures are left at full size")
    for png in sorted(raw.glob("*.png")):
        target = OUT / png.name
        if pngquant:
            subprocess.run([pngquant, "--quality", "70-92", "--speed", "1", "--strip",
                            "--force", "--output", str(target), str(png)], check=True)
        else:
            shutil.copyfile(png, target)
        if optipng:
            subprocess.run([optipng, "-quiet", "-o2", "-strip", "all", str(target)], check=True)
        print(f"  {target.relative_to(ROOT)}  {target.stat().st_size // 1024} KB")


def main():
    seeded = seed()
    port = free_port()
    server = uvicorn.Server(uvicorn.Config(app.app, host="127.0.0.1", port=port,
                                           log_level="warning", proxy_headers=False))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    for _ in range(100):
        if server.started:
            break
        time.sleep(0.1)
    else:
        sys.exit("the app did not start")
    warm_up(*seeded)

    raw = TMP / "raw"
    raw.mkdir()
    env = {k: v for k, v in os.environ.items()
           if not k.lower().endswith("_proxy") and not k.startswith(("LASTFM_", "ADMIN_"))}
    env.update(SHOTS_BASE_URL=f"http://127.0.0.1:{port}", SHOTS_OUT=str(raw),
               SHOTS_USER=ADMIN, SHOTS_PASSWORD=ADMIN_PASSWORD,
               SHOTS_ROOM_ID=str(seeded[0]))
    try:
        done = subprocess.run(["node", str(DRIVER)], env=env, cwd=TMP)
    finally:
        server.should_exit = True
        thread.join(15)
    if done.returncode:
        sys.exit(f"the browser run failed ({done.returncode})")
    if BLOCKED:
        sys.exit("the app tried to reach the network: " + ", ".join(sorted(set(BLOCKED))))
    print("No connection left this machine. Pictures:")
    shrink(raw)
    total = sum(p.stat().st_size for p in OUT.glob("*.png"))
    print(f"  total {total // 1024} KB")
    shutil.rmtree(TMP, ignore_errors=True)


if __name__ == "__main__":
    main()
