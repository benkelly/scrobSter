"""Self-check for the pure logic. Run: .venv/bin/python test_scrobster.py"""
import collections
import io
import json
import math
import os
import struct
import tempfile
import time
import wave

from scrobster.config import _load_options_json

from scrobster.accounts import merge_credential, normalise_credential
from scrobster.listener import (MAX_SEGMENT_SECONDS, NOW_PLAYING_REFRESH_SECONDS,
                                PLAY_MEMORY_SECONDS, forget_old_plays,
                                merge_alsa_devices, parse_device_list,
                                parse_proc_asound_cards,
                                parse_proc_asound_pcm, parse_track,
                                peak_dbfs, segment_seconds, should_announce,
                                should_clear, should_scrobble)
from scrobster.scrobble import profile_urls


def _wav(amplitude):
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        w.writeframes(b"".join(
            struct.pack("<h", int(amplitude * math.sin(i * 0.1))) for i in range(1600)))
    return buf.getvalue()


def main():
    cooldown = 1800
    plays = {}
    # A 200s track. First heard 50s in.
    assert should_scrobble("a", 1000, 50, plays, cooldown), "new track scrobbles"
    plays["a"] = (1000, 50)
    assert should_scrobble("b", 1010, 5, plays, cooldown), "different track scrobbles"

    # Same play continuing: offset advances with the clock, so it is not a new play.
    assert not should_scrobble("a", 1015, 65, plays, cooldown), "still the same play"
    assert not should_scrobble("a", 1100, 150, plays, cooldown), "still the same play, later"
    assert not should_scrobble("a", 1140, 188, plays, cooldown), "near the end of the play"

    # Track restarted: the clock moved on but the offset dropped back to the start.
    assert should_scrobble("a", 1200, 50, plays, cooldown), "on repeat, scrobble again"
    assert should_scrobble("a", 1210, 60, plays, cooldown), "repeat detected from a low offset"

    # Guard: never count two plays of one track within MIN_REPEAT_GAP_SECONDS.
    assert not should_scrobble("a", 1020, 1, plays, cooldown), "too soon to be a second play"

    # Heard again much later is a new play.
    assert should_scrobble("a", 5000, 30, plays, cooldown), "same track hours later"

    # No offset in the response: fall back to the plain cooldown.
    noff = {"c": (1000, None)}
    assert not should_scrobble("c", 1000 + cooldown - 1, None, noff, cooldown)
    assert should_scrobble("c", 1000 + cooldown, None, noff, cooldown), "fallback cooldown"

    # Old plays are forgotten, but only once forgetting cannot change the answer.
    now = 1000 + cooldown + PLAY_MEMORY_SECONDS
    plays = {"old": (1000, 50), "none": (1000, None),
             # one unbroken play that has outlasted the cooldown and is still going
             "long": (now - cooldown - 600, 30)}
    before = {k: should_scrobble(k, now, 60, plays, cooldown) for k in ("old", "none")}
    forget_old_plays(plays, now, cooldown)
    assert list(plays) == ["long"], plays
    assert before == {"old": True, "none": True}, "the dropped plays had already expired"
    assert not should_scrobble("long", now, 30 + cooldown + 600, plays, cooldown), \
        "a play longer than the cooldown is still one play"

    assert parse_track(None) is None
    assert parse_track({}) is None
    assert parse_track({"matches": []}) is None, "no-match response"
    info = parse_track({"matches": [{"offset": 56.98}], "track": {
        "key": "k1", "title": "T", "subtitle": "A",
        "images": {"coverart": "u"}, "isrc": "GBAAA0000001",
        "sections": [{"metadata": [{"title": "Album", "text": "Al"}]}],
    }})
    assert info == {"track_key": "k1", "title": "T", "artist": "A", "album": "Al",
                    "art_url": "u", "isrc": "GBAAA0000001", "offset": 56.98}
    assert parse_track({"track": {"key": "k"}})["offset"] is None, "offset may be absent"

    # the device picker reads ffmpeg's listings
    mac = """[AVFoundation indev @ 0x1] AVFoundation video devices:
[AVFoundation indev @ 0x1] [0] FaceTime HD Camera
[AVFoundation indev @ 0x1] AVFoundation audio devices:
[AVFoundation indev @ 0x1] [0] BlackHole 2ch
[AVFoundation indev @ 0x1] [1] MacBook Pro Microphone
: Input/output error"""
    assert parse_device_list("avfoundation", mac) == [
        {"device": ":0", "label": "BlackHole 2ch"},
        {"device": ":1", "label": "MacBook Pro Microphone"}], "video devices are skipped"
    linux = """Auto-detected sources for alsa:
* default [Playback/recording through the PulseAudio sound server]
  hw:CARD=PCH,DEV=0 [HDA Intel PCH, ALC3232 Analog]
"""
    assert parse_device_list("alsa", linux) == [
        {"device": "default", "label": "Playback/recording through the PulseAudio sound server",
         "default": True},
        {"device": "hw:CARD=PCH,DEV=0", "label": "HDA Intel PCH, ALC3232 Analog", "default": False}]
    assert parse_device_list("alsa", "Cannot list sources for alsa") == []
    pcm = """00-00: ALC3232 Analog : ALC3232 Analog : playback 1 : capture 1
00-03: HDMI 0 : HDMI 0 : playback 1
01-00: USB Audio : USB Audio : capture 1
"""
    assert parse_proc_asound_pcm(pcm) == [
        {"device": "hw:0,0", "label": "ALC3232 Analog"},
        {"device": "hw:1,0", "label": "USB Audio"}], "only capture devices"
    # ffmpeg 5.0 and later end each line with the media types
    assert parse_device_list("alsa", """Auto-detected sources for alsa:
  null [Discard all samples (playback) or generate zero samples (capture)] (none)
* hw:CARD=CODEC,DEV=0 [USB Audio CODEC, USB Audio] (audio)
""") == [
        {"device": "null", "default": False,
         "label": "Discard all samples (playback) or generate zero samples (capture)"},
        {"device": "hw:CARD=CODEC,DEV=0", "label": "USB Audio CODEC, USB Audio",
         "default": True}]
    # a card's id outlives its number, which follows the order cards appear at boot
    cards = parse_proc_asound_cards(""" 0 [PCH            ]: HDA-Intel - HDA Intel PCH
                      HDA Intel PCH at 0xf7f10000 irq 32
 1 [CODEC          ]: USB-Audio - USB Audio CODEC
                      Burr-Brown from TI USB Audio CODEC at usb-0000:00:14.0-2, full speed
""")
    assert cards == {0: ("PCH", "HDA Intel PCH"), 1: ("CODEC", "USB Audio CODEC")}
    assert parse_proc_asound_pcm(pcm, cards) == [
        {"device": "hw:CARD=PCH,DEV=0", "label": "HDA Intel PCH, ALC3232 Analog",
         "numeric": "hw:0,0"},
        {"device": "hw:CARD=CODEC,DEV=0", "label": "USB Audio CODEC, USB Audio",
         "numeric": "hw:1,0"}]
    assert parse_proc_asound_pcm(pcm, {0: ("PCH", "HDA Intel PCH")})[1] == \
        {"device": "hw:1,0", "label": "USB Audio"}, "a card missing from cards keeps its number"
    assert parse_proc_asound_cards("--- no soundcards ---") == {}
    listed = [{"device": "null", "label": "Discard all samples", "default": False},
              {"device": "hw:CARD=PCH,DEV=0", "label": "HDA Intel PCH, ALC3232 Analog",
               "default": False}]
    assert merge_alsa_devices(listed, parse_proc_asound_pcm(pcm, cards)) == [
        listed[0],
        {**listed[1], "numeric": "hw:0,0"},
        {"device": "hw:CARD=CODEC,DEV=0", "label": "USB Audio CODEC, USB Audio",
         "numeric": "hw:1,0"}], "a card ffmpeg missed comes from /proc, and both keep the number"
    assert merge_alsa_devices(listed, []) == listed
    from scrobster import listener as listener_mod
    saved_proc = listener_mod.PROC_ASOUND
    with tempfile.TemporaryDirectory() as d:
        listener_mod.PROC_ASOUND = listener_mod.pathlib.Path(d)
        try:
            assert listener_mod._read_proc_asound("cards") == "", "no cards file"
            os.mkdir(os.path.join(d, "cards"))
            assert listener_mod._read_proc_asound("cards") == "", "an unreadable cards file"
            assert parse_proc_asound_pcm(pcm, parse_proc_asound_cards(
                listener_mod._read_proc_asound("cards")))[0]["device"] == "hw:0,0"
        finally:
            listener_mod.PROC_ASOUND = saved_proc

    # a password is hashed before it is stored, and never kept as typed
    saved = normalise_credential({"username": "u", "password": "hunter2"})
    assert saved == {"username": "u", "password_hash": "2ab96390c7dbe3439de74d0c9b0b1767"}
    assert normalise_credential({"password_hash": "abc", "password": "x"}) == \
        {"password_hash": "abc"}, "a given hash wins over a password"
    assert normalise_credential({"token": "t", "url": ""}) == {"token": "t"}, "blanks dropped"

    # the settings form: blanks keep the saved value, a new password replaces the hash
    saved = {"username": "u", "password_hash": "2ab96390c7dbe3439de74d0c9b0b1767"}
    assert merge_credential(saved, {"username": "v", "password": ""}) == \
        {"username": "v", "password_hash": "2ab96390c7dbe3439de74d0c9b0b1767"}
    assert merge_credential(saved, {"password": "other"}) == \
        {"username": "u", "password_hash": "795f3202b17cb6bc3d4b771d8c6c9eaf"}, "new password wins"
    assert merge_credential({"url": "http://m:1", "key": "k"}, {"url": "http://m:2", "key": ""}) == \
        {"url": "http://m:2", "key": "k"}, "a URL change keeps the key"

    # profile links, where the service can show a listen
    assert profile_urls({"librefm": {"username": "me", "password_hash": "h"},
                         "listenbrainz": {"token": "t", "username": "me"},
                         "maloja": {"url": "http://maloja:42010/", "key": "k"}}) == {
        "librefm": "https://libre.fm/user/me",
        "listenbrainz": "https://listenbrainz.org/user/me",
        "maloja": "http://maloja:42010"}
    assert profile_urls({"listenbrainz": {"token": "t"}}) == {}, "no user name, no link"

    # a silent device must be detectable, not just "no match forever"
    assert peak_dbfs(_wav(0)) == -99.0, "digital silence"
    assert peak_dbfs(_wav(32767)) > -1, "full scale"
    assert -7 < peak_dbfs(_wav(16384)) < -5, "half scale is about -6 dBFS"
    assert peak_dbfs(_wav(0)) < -80 < peak_dbfs(_wav(16384)), "silence threshold separates them"

    # a room's live meter: one peak per block, however ffmpeg splits its output
    from scrobster import listener as live
    heard = []
    blocks = live.LevelBlocks(heard.append)
    loud = struct.pack("<h", 16384) * (blocks.size // 2)
    quiet = b"\0" * blocks.size
    pcm = loud + quiet + loud[:10]
    for i in range(0, len(pcm), 777):
        blocks.feed(pcm[i:i + 777])
    assert len(heard) == 2 and -7 < heard[0] < -5 and heard[1] == -99.0, heard
    room = live.Room({"id": 1, "name": "r", "kind": "device", "enabled": 1,
                      "url": None, "backend": "alsa", "device": "default"})
    assert room.live_level_db() is None, "nothing heard yet"
    room.heard_level(-30.0)
    room.heard_level(-50.0)
    assert room.live_level_db() == -30.0, "the loudest recent block"
    room._live_levels = collections.deque(
        [(time.monotonic() - live.LEVEL_STALE_SECONDS - 1, -20.0)])
    assert room.live_level_db() is None, "between recordings there is no live level"

    # "playing now" is announced on change, then refreshed before it expires
    assert should_announce("a", 1000, None), "first match announces"
    assert should_announce("a", 1000, ("b", 999)), "track changed, announce"
    assert not should_announce("a", 1010, ("a", 1000)), "same track, mark still fresh"
    assert not should_announce("a", 1000 + NOW_PLAYING_REFRESH_SECONDS - 1, ("a", 1000))
    assert should_announce("a", 1000 + NOW_PLAYING_REFRESH_SECONDS, ("a", 1000)), "refresh"

    # the mark is cleared once the music stops, but not during a quiet passage
    stop_after = 180
    assert not should_clear(9999, 1000, None, stop_after), "no mark, nothing to clear"
    assert not should_clear(1030, 1000, ("a", 1000), stop_after), "still matching"
    assert not should_clear(1000 + stop_after - 1, 1000, ("a", 1000), stop_after)
    assert should_clear(1000 + stop_after, 1000, ("a", 1000), stop_after), "music stopped"

    # a browser clip keeps no more than the 12s that is fingerprinted
    assert MAX_SEGMENT_SECONDS == 12, "the window Shazam's apps send, and shazamio's"
    assert segment_seconds(10) == 10, "short window passes through"
    assert segment_seconds(12) == 12
    assert segment_seconds(30) == MAX_SEGMENT_SECONDS, "oversized window is capped"

    # Home Assistant options arrive as JSON and must become environment variables
    for key in ("LISTENBRAINZ_TOKEN", "MATCH_INTERVAL", "LISTEN_ON_START", "BLANK"):
        os.environ.pop(key, None)
    os.environ["AUDIO_DEVICE"] = "real-env-wins"
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "options.json")
        with open(path, "w") as fh:
            json.dump({"listenbrainz_token": "abc", "match_interval": 20,
                       "listen_on_start": False, "blank": "", "missing": None,
                       "audio_device": "from-options"}, fh)
        _load_options_json(path)
    assert os.environ["LISTENBRAINZ_TOKEN"] == "abc", "lower case key becomes upper case"
    assert os.environ["MATCH_INTERVAL"] == "20", "numbers become strings"
    assert os.environ["LISTEN_ON_START"] == "false", "bool is lower case, not Python True"
    assert "BLANK" not in os.environ, "empty value is skipped"
    assert "MISSING" not in os.environ, "null value is skipped"
    assert os.environ["AUDIO_DEVICE"] == "real-env-wins", "real environment wins"
    _load_options_json("/definitely/not/here.json")  # absent file must not raise

    check_accounts()
    check_migration()
    check_cleanup()
    check_client_cache()
    check_privacy()
    check_keep_listening()
    check_shazamio()
    check_enrich()
    check_rooms()
    check_server_mic_room()
    check_rates()
    check_events()
    check_mqtt()
    check_colours()
    check_event_stream()
    check_restart()
    check_known_song()
    check_librefm_scrobble()
    check_outage()
    check_quiet()
    check_display()
    check_browser_clip()
    check_display_scope_migration()
    check_browser_room()
    check_install()
    print("ok")


def check_accounts():
    from scrobster import accounts
    stored = accounts.hash_password("correct horse battery")
    assert accounts.verify_password("correct horse battery", stored), "right password"
    assert not accounts.verify_password("wrong", stored), "wrong password"
    assert not accounts.verify_password("correct horse battery", "rubbish"), "bad record"
    assert stored != accounts.hash_password("correct horse battery"), "salted, not fixed"


def check_migration():
    """Schema 1 kept one row per scrobble with the services in a JSON column.
    An upgrade must keep that history and give it to the owner."""
    import sqlite3
    from scrobster import config

    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "old.db")
        old = sqlite3.connect(path)
        old.execute("""CREATE TABLE matches(
            id INTEGER PRIMARY KEY AUTOINCREMENT, ts INTEGER NOT NULL,
            artist TEXT, title TEXT, album TEXT, track_key TEXT, art_url TEXT,
            services TEXT)""")
        old.execute("INSERT INTO matches(ts, artist, title, album, track_key,"
                    " art_url, services) VALUES(?,?,?,?,?,?,?)",
                    (111, "A", "T", "Al", "k1", "u", '{"listenbrainz": "ok"}'))
        old.commit()
        old.close()

        previous = config.DB_PATH
        config.DB_PATH = path
        try:
            from scrobster import accounts, db
            db.init()
            owner = accounts.create_user("owner", "password123", is_admin=True)
            assert owner is not None, "create_user must return the new account"

            assert db.adopt_legacy_history(owner["id"]) == 1, "the old row moves"
            history = db.recent(owner["id"])
            assert len(history) == 1, history
            assert history[0]["title"] == "T", history
            assert history[0]["services"] == {"listenbrainz": "ok"}, history

            assert db.adopt_legacy_history(owner["id"]) == 0, "moving twice is safe"
            version = sqlite3.connect(path).execute("PRAGMA user_version").fetchone()[0]
            assert version == db.SCHEMA_VERSION, version

            # A second user must not see somebody else's history.
            other = accounts.create_user("other", "password123")
            assert db.recent(other["id"]) == [], "history is per user"

            # ADMIN_PASSWORD must apply to an account that already exists,
            # otherwise setting it after the first start locks the owner out.
            previous_name, previous_pw = config.ADMIN_USERNAME, config.ADMIN_PASSWORD
            try:
                config.ADMIN_USERNAME, config.ADMIN_PASSWORD = "owner", "chosen-password"
                assert accounts.sync_admin_password() is True, "applies when different"
                fresh = accounts.get_user(owner["id"])
                assert accounts.verify_password("chosen-password", fresh["password_hash"])
                assert accounts.sync_admin_password() is False, "no work when equal"
                config.ADMIN_PASSWORD = "short"
                assert accounts.sync_admin_password() is False, "too short is refused"
                still = accounts.get_user(owner["id"])
                assert accounts.verify_password("chosen-password", still["password_hash"]), \
                    "a refused password must not clear the working one"
                config.ADMIN_PASSWORD = None
                assert accounts.sync_admin_password() is False, "unset changes nothing"
            finally:
                config.ADMIN_USERNAME, config.ADMIN_PASSWORD = previous_name, previous_pw
        finally:
            config.DB_PATH = previous


def check_cleanup():
    """Expired sessions and matches nobody scrobbled are deleted, and nothing else."""
    import sqlite3
    import time
    from scrobster import accounts, config, db

    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "clean.db")
        previous = config.DB_PATH
        config.DB_PATH = path
        try:
            db.init()
            a = accounts.create_user("alice", "password123", is_admin=True)
            b = accounts.create_user("bob", "password123")

            # A session past SESSION_DAYS is deleted by the next sign-in.
            expired = int(time.time()) - accounts.SESSION_DAYS * 86400 - 1
            with sqlite3.connect(path) as c:
                c.execute("INSERT INTO sessions VALUES('stale', ?, ?, ?)",
                          (a["id"], expired, expired))
            kept = accounts.start_session(a["id"])
            fresh = accounts.start_session(b["id"])
            with sqlite3.connect(path) as c:
                tokens = {r[0] for r in c.execute("SELECT token FROM sessions")}
            assert tokens == {kept, fresh}, tokens
            assert accounts.session_user(kept)["id"] == a["id"], "a live session survives"

            # One match shared by both users, one that only bob scrobbled.
            shared = db.add_match(100, "A", "Shared", None, "k1", None, "server")
            db.add_scrobbles(shared, a["id"], {"listenbrainz": "ok"})
            db.add_scrobbles(shared, b["id"], {"listenbrainz": "ok"})
            own = db.add_match(200, "A", "Bob's", None, "k2", None, "server")
            db.add_scrobbles(own, b["id"], {"listenbrainz": "ok"})
            assert db.delete_orphaned_matches() == 0, "nothing to remove yet"

            accounts.delete_user(b["id"])
            with sqlite3.connect(path) as c:
                plan = " ".join(r[-1] for r in c.execute(
                    "EXPLAIN QUERY PLAN DELETE FROM matches WHERE id NOT IN"
                    " (SELECT match_id FROM scrobbles)"))
            assert "idx_scrobbles_match" in plan, "the startup delete must not scan"
            assert db.delete_orphaned_matches() == 1, "bob's own match goes"
            assert [m["title"] for m in db.recent(a["id"])] == ["Shared"], \
                "a match somebody still has stays"
        finally:
            config.DB_PATH = previous


def check_client_cache():
    """A new Libre.fm password replaces the cached client instead of being ignored."""
    from scrobster import scrobble

    class Fake:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

    pylast = scrobble.pylast
    real = pylast.LibreFMNetwork, pylast.LastFMNetwork
    pylast.LibreFMNetwork = pylast.LastFMNetwork = Fake
    scrobble._networks.clear()
    try:
        first = scrobble._pylast_network("librefm", {"username": "u", "password_hash": "a"})
        again = scrobble._pylast_network("librefm", {"username": "u", "password_hash": "a"})
        assert again is first, "the same secret reuses the client"
        changed = scrobble._pylast_network("librefm", {"username": "u", "password_hash": "b"})
        assert changed is not first and changed.kwargs["password_hash"] == "b", \
            "a new password signs in again"
        assert len(scrobble._networks) == 1, "one client per account"

        # Two people on one Last.fm account, one connected in the browser and one
        # with a password, must not keep rebuilding each other's client.
        browser = {"username": "bob", "session_key": "sk"}
        password = {"username": "bob", "password_hash": "h"}
        a = scrobble._pylast_network("lastfm", browser)
        b = scrobble._pylast_network("lastfm", password)
        assert scrobble._pylast_network("lastfm", browser) is a
        assert scrobble._pylast_network("lastfm", password) is b
    finally:
        pylast.LibreFMNetwork, pylast.LastFMNetwork = real
        scrobble._networks.clear()


def check_privacy():
    """A browser clip stays with its owner, a new password ends other sessions,
    the last administrator stays one, and X-Forwarded-For is never trusted."""
    import asyncio
    from fastapi import HTTPException
    from scrobster import accounts, app, config, db, listener as listener_mod

    with tempfile.TemporaryDirectory() as d:
        previous = config.DB_PATH
        config.DB_PATH = os.path.join(d, "privacy.db")
        try:
            db.init()
            alice = accounts.create_user("alice", "password123", is_admin=True)
            bob = accounts.create_user("bob", "password123")

            here = accounts.start_session(alice["id"])
            elsewhere = accounts.start_session(alice["id"])
            accounts.update_user(alice["id"], password="new-password", keep_session=here)
            assert accounts.session_user(here)["id"] == alice["id"], "this session stays"
            assert accounts.session_user(elsewhere) is None, "every other session ends"
            other = accounts.start_session(bob["id"])
            accounts.update_user(bob["id"], password="reset-by-admin")
            assert accounts.session_user(other) is None, "an admin reset ends them all"

            async def edit(user_id, body):
                return await app.edit_user(user_id, body, admin=accounts.get_user(alice["id"]))
            try:
                asyncio.run(edit(alice["id"], {"is_admin": False}))
                raise AssertionError("the last administrator was demoted")
            except HTTPException as e:
                assert e.status_code == 400, e
            asyncio.run(edit(bob["id"], {"is_admin": True}))
            assert not asyncio.run(edit(alice["id"], {"is_admin": False}))["is_admin"], \
                "with another administrator, stepping down is fine"

            # Neither user has a service, so nothing is sent anywhere.
            room = listener_mod.Listener()
            info = {"track_key": "k", "title": "Private", "artist": "A", "album": None,
                    "art_url": None, "offset": None}
            asyncio.run(room._on_match(info, "browser", [bob]))
            assert room.last_match_for(bob["id"])["title"] == "Private", "bob sees his clip"
            assert room.last_match_for(alice["id"]) is None, "alice does not"
            asyncio.run(room.sync_rooms())
            kitchen = room.first_room()
            asyncio.run(room._on_match({**info, "title": "Radio"}, "server", [], kitchen))
            assert room.last_match_for(alice["id"])["title"] == "Radio", "the room is shared"
            assert room.last_match_for(alice["id"])["room"] == "Server mic"

            # No marks, nothing to clear, and nothing logged every cycle.
            calls = []
            real = listener_mod.scrobble.clear_now_playing_all
            async def record(credentials):
                calls.append(credentials)
            listener_mod.scrobble.clear_now_playing_all = record
            try:
                asyncio.run(room._clear_if_stopped())
                assert calls == [], "no mark, nothing to clear"
                room._now_playing = {bob["id"]: ("k", 0), alice["id"]: ("r", 0)}
                room._last_match_at = {bob["id"]: int(__import__("time").time()),
                                       alice["id"]: 0}
                asyncio.run(room._clear_if_stopped())
                assert list(room._now_playing) == [bob["id"]], "only the stale mark goes"
            finally:
                listener_mod.scrobble.clear_now_playing_all = real
        finally:
            config.DB_PATH = previous

    seen = {}
    real_run = app.uvicorn.run
    app.uvicorn.run = lambda *a, **kw: seen.update(kw)
    try:
        app.main()
    finally:
        app.uvicorn.run = real_run
    assert seen.get("proxy_headers") is False, "X-Forwarded-For must not pick the client"


def check_keep_listening():
    """A stuck ffmpeg, a rate-limiting Shazam or a dead service must not stop
    the capture loop for long."""
    import asyncio
    import time
    from aiohttp import web
    from scrobster import listener, scrobble

    assert [listener.rate_limit_wait(w) for w in (0, 60, 400, 600)] == [60, 120, 600, 600]
    assert isinstance(listener.Listener()._shazam.http_client, listener.ShazamClient), \
        "shazamio's own client retries a 429 for twelve minutes"

    async def shazam_says_429():
        hits = []
        async def handler(request):
            hits.append(1)
            return web.Response(status=429)
        app = web.Application()
        app.router.add_route("*", "/", handler)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]
        try:
            await listener.ShazamClient().request("POST", f"http://127.0.0.1:{port}/", json={})
            raise AssertionError("a 429 must raise")
        except listener.RateLimited:
            pass
        finally:
            await runner.cleanup()
        return hits
    assert asyncio.run(shazam_says_429()) == [1], "asked once, not retried"

    # An ffmpeg that never finishes, standing in for an input that sends no audio.
    with tempfile.TemporaryDirectory() as d:
        pidfile = os.path.join(d, "pid")
        fake = os.path.join(d, "ffmpeg")
        with open(fake, "w") as fh:
            fh.write(f"#!/bin/sh\necho $$ > {pidfile}\nexec sleep 60\n")
        os.chmod(fake, 0o755)
        path, grace = os.environ["PATH"], listener.FFMPEG_GRACE_SECONDS
        os.environ["PATH"] = d + os.pathsep + path
        listener.FFMPEG_GRACE_SECONDS = 1

        def gone():
            try:
                os.kill(int(open(pidfile).read()), 0)
            except ProcessLookupError:
                return True
            return False
        try:
            start = time.monotonic()
            try:
                asyncio.run(listener.capture_chunk(seconds=1))
                raise AssertionError("a stuck ffmpeg must time out")
            except RuntimeError as e:
                assert "gave nothing" in str(e), e
            assert time.monotonic() - start < 10
            assert gone(), "a timed-out ffmpeg is killed"

            async def cancel_mid_capture():
                task = asyncio.create_task(listener.capture_chunk(seconds=30))
                while not os.path.exists(pidfile) or not open(pidfile).read().strip():
                    await asyncio.sleep(0.05)
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
            os.remove(pidfile)
            asyncio.run(cancel_mid_capture())
            assert gone(), "stopping the listener frees the device"
        finally:
            os.environ["PATH"], listener.FFMPEG_GRACE_SECONDS = path, grace

    # Services run together, and a timeout still says what it was.
    async def each():
        async def handle(service, data):
            await asyncio.sleep(0.5)
            if service == "maloja":
                raise asyncio.TimeoutError()
        start = time.monotonic()
        out = await scrobble._each_service(
            {"listenbrainz": {"token": "t"}, "maloja": {"url": "u", "key": "k"}},
            handle, lambda s, e: None)
        return out, time.monotonic() - start
    results, took = asyncio.run(each())
    assert results == {"listenbrainz": "ok", "maloja": "error: TimeoutError"}, results
    assert took < 0.9, f"services ran one after another ({took:.2f}s)"


def _music(seconds, seed=7):
    """Seconds of changing tones, enough for shazamio-core to fingerprint."""
    import random
    rnd = random.Random(seed)
    samples, phase = [], [0.0, 0.0, 0.0]
    for note in range(seconds * 4):
        freqs = [rnd.uniform(200, 3000) for _ in phase]
        for _ in range(4000):
            total = 0.0
            for k, f in enumerate(freqs):
                phase[k] += 2 * math.pi * f / 16000
                total += math.sin(phase[k])
            samples.append(6000 * total)
    return _wav_of(samples)


def check_shazamio():
    """shazamio 0.9: the listener asks with its own client, a 12s window and
    no warning, and the fingerprint is made locally. Shazam itself is faked."""
    import asyncio
    import warnings
    import shazamio.api
    from scrobster import listener

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        house = listener.Listener()
    assert not [w for w in caught if "segment_duration_seconds" in str(w.message)], \
        [str(w.message) for w in caught]
    assert MAX_SEGMENT_SECONDS == shazamio.api.WINDOW_SECONDS
    client = house._shazam.http_client
    assert isinstance(client, listener.ShazamClient)

    sent, asked = [], []
    track = {"key": "42", "title": "Song", "subtitle": "Artist",
             "images": {"coverart": "https://example.invalid/c.jpg"}, "isrc": "GBAAA0000001",
             "sections": [{"metadata": [{"title": "Album", "text": "Record"}]}]}

    async def fake_request(method, url, **kwargs):
        sent.append((method, url, kwargs))
        return {"matches": [{"offset": 31.5}], "track": track}
    client.request = fake_request
    real_recognize = house._shazam.recognize

    async def spy(data, *args, **kwargs):
        asked.append(data)
        return await real_recognize(data, *args, **kwargs)
    house._shazam.recognize = spy

    for seconds in (12, 20, 8):
        sent.clear()
        asked.clear()
        house._last_request = 0.0
        wav = _music(seconds)
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            result = asyncio.run(house.recognize(wav))
        assert not [w for w in caught if "segment_duration_seconds" in str(w.message)]
        assert asked == [wav], "recognize() gets the WAV as captured"
        with wave.open(io.BytesIO(asked[0])) as w:
            assert w.getnframes() == seconds * w.getframerate()
        assert len(sent) == 1, "one request, through our client"
        method, url, kwargs = sent[0]
        assert method == "POST" and url.startswith("https://amp.shazam.com/"), url
        signature = kwargs["json"]["signature"]
        assert signature["uri"].startswith("data:audio/vnd.shazam.sig;base64,")
        assert signature["samplems"] == min(seconds, 12) * 1000, \
            (seconds, signature["samplems"], "the middle 12s, or a shorter clip whole")
        assert parse_track(result) == {
            "track_key": "42", "title": "Song", "artist": "Artist", "album": "Record",
            "art_url": "https://example.invalid/c.jpg", "isrc": "GBAAA0000001", "offset": 31.5}

    # shazamio passes our 429 through, so the shared backoff starts.
    async def says_429(method, url, **kwargs):
        raise listener.RateLimited("Shazam is limiting requests from this address")
    client.request = says_429
    house._last_request = 0.0
    try:
        asyncio.run(house.recognize(_music(12)))
        raise AssertionError("a 429 must raise RateLimited")
    except listener.RateLimited:
        pass
    assert house.rate_limited_for() > 0 and house.last_rate_limited


def check_enrich():
    """MusicBrainz IDs for ListenBrainz, and Shazam's cover for Maloja."""
    import asyncio
    import base64
    from aiohttp import web
    from scrobster import enrich, scrobble

    body = {"recordings": [{"id": "r-live", "title": "Song (Live)"},
                           {"id": "r1", "title": "Song"}]}
    assert [r["id"] for r in enrich.recordings_to_try(body, "song")] == ["r1", "r-live"], \
        "the recording with Shazam's title is tried first"
    assert enrich.recordings_to_try({}, "Song") == []
    releases = [{"id": "single", "title": "Song", "status": "Official"},
                {"id": "bootleg", "title": "Album", "status": "Bootleg"},
                {"id": "album", "title": "album", "status": "Official"}]
    assert enrich.album_release(releases, "Album") == "album", \
        "the official release of Shazam's album"
    assert enrich.album_release(releases, "Greatest Hits") is None, \
        "no release is guessed when none has Shazam's album title"
    assert enrich.album_release(releases, None) is None
    assert enrich._ids({"id": "r1", "artist-credit": [{"artist": {"id": "a1"}}]},
                       "album") == {"recording_mbid": "r1", "artist_mbids": ["a1"],
                                    "release_mbid": "album"}
    assert asyncio.run(enrich.musicbrainz_ids(None, "Song", "Album")) == {}, \
        "no ISRC, no lookup"

    assert enrich.is_placeholder(202, None) is None, "Maloja is still looking"
    assert enrich.is_placeholder(307, "/static/svg/placeholder_album.svg")
    assert enrich.is_placeholder(307, "https://generative-placeholders.glitch.me/x")
    assert not enrich.is_placeholder(307, "https://lastfm.freetls.fastly.net/i/u/a.jpg")

    # A fake Maloja that has no picture for one album and has one for another.
    uploads, picture_asks = [], []
    cover = b"\xff\xd8 a jpeg"

    async def album_info(request):
        ids = {"Bare": 1, "Pictured": 2}
        if request.query.get("albumtitle") not in ids:
            return web.json_response({"error": "no such album"}, status=404)
        assert request.query["albumartist"] == "Artist"
        return web.json_response({"id": ids[request.query["albumtitle"]]})

    async def image(request):
        picture_asks.append(request.query["album_id"])
        where = ("/static/svg/placeholder_album.svg" if request.query["album_id"] == "1"
                 else "https://example.com/real.jpg")
        return web.Response(status=307, headers={"Location": where})

    async def add_picture(request):
        uploads.append(await request.json())
        return web.json_response({"status": "success"})

    async def shazam_cover(request):
        return web.Response(body=cover, content_type="image/jpeg")

    async def run():
        app = web.Application()
        app.add_routes([web.get("/apis/mlj_1/albuminfo", album_info),
                        web.get("/image", image),
                        web.post("/apis/mlj_1/addpicture", add_picture),
                        web.get("/cover.jpg", shazam_cover)])
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        base = f"http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}"
        maloja = {"url": base + "/", "key": "k"}
        try:
            for album in ("Bare", "Bare", "Pictured", "Unknown"):
                enrich.maloja_cover(maloja, "Artist", "Song", album, base + "/cover.jpg")
            enrich.maloja_cover(maloja, "Artist", "Song", "Other", None)
            await asyncio.gather(*enrich._tasks)
        finally:
            await runner.cleanup()

    asyncio.run(run())
    assert picture_asks == ["1", "2"] or picture_asks == ["2", "1"], \
        f"each album checked once, a repeat and a missing cover skipped: {picture_asks}"
    assert len(uploads) == 1, f"only the album with no picture gets one: {uploads}"
    sent = uploads[0]
    assert sent["key"] == "k" and sent["albumtitle"] == "Bare"
    assert sent["albumartist"] == ["Artist"]
    assert sent["b64"] == "data:image/jpeg;base64," + base64.b64encode(cover).decode()

    # A cover that arrives in pieces is read to its end, not only its top.
    big = bytes(range(256)) * 600

    async def slow_cover(request):
        response = web.StreamResponse(headers={"Content-Type": "image/jpeg"})
        await response.prepare(request)
        try:
            for start in range(0, len(big), 20000):
                await response.write(big[start:start + 20000])
                await asyncio.sleep(0.01)
            await response.write_eof()
        except ConnectionResetError:
            pass  # the client stopped reading, as it should past MAX_ART_BYTES
        return response

    async def fetch_slow(limit):
        app = web.Application()
        app.add_routes([web.get("/slow.jpg", slow_cover)])
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        saved = enrich.MAX_ART_BYTES
        enrich.MAX_ART_BYTES = limit
        try:
            async with aiohttp.ClientSession() as session:
                return await enrich.fetch_image(
                    session, f"http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}"
                             "/slow.jpg")
        finally:
            enrich.MAX_ART_BYTES = saved
            await runner.cleanup()

    import aiohttp
    assert asyncio.run(fetch_slow(len(big))) == ("image/jpeg", big)
    try:
        asyncio.run(fetch_slow(len(big) - 1))
        raise AssertionError("a cover larger than MAX_ART_BYTES is refused")
    except ValueError:
        pass

    # The IDs ride along in additional_info, which ListenBrainz and Maloja read.
    sent = []

    async def submit(request):
        sent.append(await request.json())
        return web.json_response({"status": "ok"})

    async def submit_one():
        app = web.Application()
        app.add_routes([web.post("/1/submit-listens", submit)])
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        base = f"http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}"
        try:
            await scrobble.scrobble_all({"listenbrainz": {"token": "t", "url": base}},
                                        "A", "T", "Al", 1000,
                                        {"recording_mbid": "r1", "isrc": "X"})
        finally:
            await runner.cleanup()

    asyncio.run(submit_one())
    info = sent[0]["payload"][0]["track_metadata"]["additional_info"]
    assert info["recording_mbid"] == "r1" and info["isrc"] == "X"
    assert info["submission_client"] == "scrobSter"


def check_rooms():
    """Cameras and other rooms: the upgrade keeps the one microphone, a camera
    password never leaves the server, each person receives only the rooms they
    chose, and every room shares one Shazam budget."""
    import asyncio
    import sqlite3
    import time
    from scrobster import accounts, app, config, db, listener as listener_mod, rooms

    # Camera URLs. The password is shown masked, kept through an edit that
    # leaves the mask in place, and removed from error text.
    url = "rtsps://viewer:s3cr%40t@cam.local:7441/kitchen?enableSrtp"
    assert rooms.mask_url(url) == "rtsps://viewer:***@cam.local:7441/kitchen?enableSrtp"
    assert rooms.mask_url("rtsp://cam.local/live") == "rtsp://cam.local/live", "no password"
    assert rooms.merge_url(rooms.mask_url(url), url) == url, "the mask keeps the password"
    assert rooms.merge_url("rtsp://viewer:new@cam/x", url) == "rtsp://viewer:new@cam/x"
    try:
        rooms.merge_url("rtsp://viewer:***@cam/x", None)
        raise AssertionError("a mask with nothing saved behind it is refused")
    except ValueError:
        pass
    said = rooms.redact(f"{url}: Connection refused, s3cr@t and s3cr%40t", url)
    assert "s3cr" not in said and "cam.local" in said, said
    for bad in ("file:///etc/passwd", "/dev/snd", "rtsp://", "rtsp://cam/a b"):
        try:
            rooms.check_url(bad)
            raise AssertionError(f"{bad} must be refused")
        except ValueError:
            pass

    # ffmpeg reads a camera's audio only, over TCP.
    args = listener_mod.input_args(url=url)
    assert args[args.index("-allowed_media_types") + 1] == "audio", args
    assert args[args.index("-rtsp_transport") + 1] == "tcp", args
    assert args[args.index("-i") + 1] == url and "-vn" in args, args
    assert "-rtsp_transport" not in listener_mod.input_args(url="http://go2rtc/api/stream.aac")
    assert listener_mod.input_args("pulse", "default") == ["-f", "pulse", "-i", "default"]

    # One readable line from ffmpeg's log, not the generic lines after it.
    assert listener_mod.short_error(
        "ALSA lib pcm.c:2721:(snd_pcm_open_noupdate) Unknown PCM default\n"
        "[alsa @ 0x55] cannot open audio device default (No such file or directory)\n"
        "[in#0 @ 0x55] Error opening input: Input/output error\n"
        "Error opening input file default.\n") == \
        "cannot open audio device default (No such file or directory)"

    # An ffmpeg that quotes the URL in its error must not hand out the password.
    with tempfile.TemporaryDirectory() as d:
        fake = os.path.join(d, "ffmpeg")
        with open(fake, "w") as fh:
            fh.write('#!/bin/sh\necho "$*: 401 Unauthorized" >&2\nexit 1\n')
        os.chmod(fake, 0o755)
        path = os.environ["PATH"]
        os.environ["PATH"] = d + os.pathsep + path
        try:
            asyncio.run(listener_mod.capture_chunk(seconds=1, url="rtsp://u:hunter22@cam/s"))
            raise AssertionError("a failed capture raises")
        except RuntimeError as e:
            assert "hunter22" not in str(e) and "Unauthorized" in str(e), e
        finally:
            os.environ["PATH"] = path

    with tempfile.TemporaryDirectory() as d:
        previous = config.DB_PATH
        config.DB_PATH = os.path.join(d, "rooms.db")
        try:
            # A schema 2 database: one microphone, a device chosen in Settings,
            # and one of two users receiving it.
            with sqlite3.connect(config.DB_PATH) as c:
                c.executescript("""
                    CREATE TABLE users(id INTEGER PRIMARY KEY AUTOINCREMENT,
                        username TEXT NOT NULL UNIQUE COLLATE NOCASE,
                        password_hash TEXT NOT NULL, is_admin INTEGER NOT NULL DEFAULT 0,
                        room_mic INTEGER NOT NULL DEFAULT 0, api_token TEXT UNIQUE,
                        created_at INTEGER NOT NULL);
                    CREATE TABLE matches(id INTEGER PRIMARY KEY AUTOINCREMENT,
                        ts INTEGER NOT NULL, artist TEXT, title TEXT, album TEXT,
                        track_key TEXT, art_url TEXT,
                        source TEXT NOT NULL DEFAULT 'server');
                    CREATE TABLE settings(key TEXT PRIMARY KEY, value TEXT NOT NULL);
                    INSERT INTO users VALUES(1, 'alice', 'x', 1, 1, 'ta', 0);
                    INSERT INTO users VALUES(2, 'bob', 'x', 0, 0, 'tb', 0);
                    INSERT INTO matches(ts, title) VALUES(5, 'Old');
                    INSERT INTO settings VALUES('audio_device', 'hw:1,0');
                    PRAGMA user_version = 2;""")
            db.init()
            db.init()  # a second start changes nothing
            first, = rooms.list_rooms()
            assert (first["name"], first["kind"], first["device"]) == \
                ("Server mic", "device", "hw:1,0"), first
            assert rooms.rooms_of(1) == [first["id"]] and rooms.rooms_of(2) == [], \
                "whoever received the microphone receives the room"
            assert db.get_setting("audio_device") is None, "the choice moved into the room"
            with sqlite3.connect(config.DB_PATH) as c:
                assert c.execute("SELECT room FROM matches").fetchone() == (None,)

            alice, bob = accounts.get_user(1), accounts.get_user(2)
            try:
                rooms.add_room(name="server MIC", kind="device")
                raise AssertionError("names are unique, whatever the case")
            except ValueError:
                pass
            try:
                rooms.add_room(name="Hall", kind="stream", url="")
                raise AssertionError("a camera needs a URL")
            except ValueError:
                pass

            async def as_admin(fn, *a):
                return await fn(*a, admin=alice)
            hall = asyncio.run(as_admin(app.add_room, {"name": "Hall", "kind": "stream",
                                                       "url": url}))
            assert hall["url"] == rooms.mask_url(url), "the page never gets the password"
            assert rooms.get_room(hall["id"])["url"] == url, "the server keeps it"
            assert hall["member"], "whoever adds a room receives it"
            asyncio.run(as_admin(app.edit_room, hall["id"],
                                 {"url": rooms.mask_url(url).replace("kitchen", "hall")}))
            assert rooms.get_room(hall["id"])["url"] == url.replace("kitchen", "hall"), \
                "a path edit keeps the saved password"
            seen_by_bob = {r["name"]: r for r in app._rooms_for(bob)}
            assert "url" not in seen_by_bob["Hall"] and not seen_by_bob["Hall"]["member"], \
                "a user sees the room, not where it records from"
            asyncio.run(app.room_member(hall["id"], {"on": True}, user=bob))
            assert rooms.rooms_of(bob["id"]) == [hall["id"]]

            # Each room scrobbles for its own members. A song two rooms hear at
            # once is one play for somebody who receives both.
            for user in (alice, bob):
                accounts.set_credential(user["id"], "listenbrainz", {"token": "t"})
            sent, real = [], (listener_mod.scrobble.scrobble_all,
                              listener_mod.scrobble.now_playing_all,
                              listener_mod.enrich.musicbrainz_ids)

            async def scrobble_all(credentials, artist, title, *rest):
                sent.append((credentials["listenbrainz"]["who"], title))
                return {"listenbrainz": "ok"}

            async def nothing(*a, **kw):
                return {}
            listener_mod.scrobble.scrobble_all = scrobble_all
            listener_mod.scrobble.now_playing_all = nothing
            listener_mod.enrich.musicbrainz_ids = nothing
            accounts.set_credential(alice["id"], "listenbrainz", {"token": "t", "who": "alice"})
            accounts.set_credential(bob["id"], "listenbrainz", {"token": "t", "who": "bob"})
            gap, config.RECOGNITION_GAP_SECONDS = config.RECOGNITION_GAP_SECONDS, 0
            try:
                house = listener_mod.Listener()
                asyncio.run(house.sync_rooms())
                assert not house.listening, "nothing runs until listening starts"
                track = {"track": {"key": "k1", "title": "Song", "subtitle": "A"},
                         "matches": [{"offset": 30}]}

                async def found(wav):
                    return track
                house._shazam.recognize = found
                mic, cam = house.rooms[first["id"]], house.rooms[hall["id"]]
                asyncio.run(house.match_bytes(b"", room=mic))
                assert sent == [("alice", "Song")], f"only the microphone's members: {sent}"
                asyncio.run(house.match_bytes(b"", room=cam))
                assert sent == [("alice", "Song"), ("bob", "Song")], \
                    f"the same play in the hall is new only for bob: {sent}"
                assert cam.last_match["room"] == "Hall" and mic.last_match["room"] != "Hall"
                with sqlite3.connect(config.DB_PATH) as c:
                    assert {r[0] for r in c.execute(
                        "SELECT room FROM matches WHERE title='Song'")} == \
                        {"Server mic", "Hall"}, "the history says where"
                assert db.recent(bob["id"])[0]["room"] == "Hall"

                # Turning a room off or removing it stops only that room.
                asyncio.run(as_admin(app.edit_room, hall["id"], {"enabled": False}))
                assert rooms.get_room(hall["id"])["enabled"] == 0
                asyncio.run(as_admin(app.remove_room, hall["id"]))
                assert rooms.rooms_of(bob["id"]) == [], "membership goes with the room"
                assert db.recent(bob["id"])[0]["room"] == "Hall", "the history stays"
            finally:
                config.RECOGNITION_GAP_SECONDS = gap
                (listener_mod.scrobble.scrobble_all, listener_mod.scrobble.now_playing_all,
                 listener_mod.enrich.musicbrainz_ids) = real
        finally:
            config.DB_PATH = previous

    # One Shazam queue: requests keep their distance, and a 429 in one room
    # stops the others asking too.
    gap = config.RECOGNITION_GAP_SECONDS
    config.RECOGNITION_GAP_SECONDS = 0.3
    try:
        house = listener_mod.Listener()
        asked = []

        async def answer(wav):
            asked.append(time.monotonic())
            if wav == b"429":
                raise listener_mod.RateLimited("slow down")
            return {}
        house._shazam.recognize = answer

        async def three_rooms_at_once():
            await asyncio.gather(*(house.recognize(b"") for _ in range(3)))
        asyncio.run(three_rooms_at_once())
        spacing = [b - a for a, b in zip(asked, asked[1:])]
        assert len(asked) == 3 and min(spacing) >= 0.29, spacing

        asked.clear()
        try:
            asyncio.run(house.recognize(b"429"))
            raise AssertionError("a 429 raises")
        except listener_mod.RateLimited as e:
            assert "60s" in str(e), e
        assert 55 < house.rate_limited_for() <= 60
        try:
            asyncio.run(house.recognize(b""))
            raise AssertionError("every room waits out the 429")
        except listener_mod.RateLimited:
            pass
        assert len(asked) == 1, "the room that waits does not ask"
    finally:
        config.RECOGNITION_GAP_SECONDS = gap


def check_server_mic_room():
    """CREATE_SERVER_MIC_ROOM=0 starts a new database with no rooms, for a host
    that only reads cameras, and never takes the room from a database that has
    one."""
    import sqlite3
    from scrobster import config, db, rooms

    with tempfile.TemporaryDirectory() as d:
        saved = config.DB_PATH, config.CREATE_SERVER_MIC_ROOM
        try:
            config.DB_PATH = os.path.join(d, "default.db")
            db.init()
            assert [r["name"] for r in rooms.list_rooms()] == ["Server mic"], \
                "by default a new database starts with the microphone room"

            config.CREATE_SERVER_MIC_ROOM = False
            db.init()
            assert [r["name"] for r in rooms.list_rooms()] == ["Server mic"], \
                "an existing database keeps its room"

            config.DB_PATH = os.path.join(d, "cameras.db")
            db.init()
            db.init()
            assert rooms.list_rooms() == [], "a cameras-only install starts empty"

            # A schema 2 database had a microphone, so it still becomes a room.
            config.DB_PATH = os.path.join(d, "schema2.db")
            with sqlite3.connect(config.DB_PATH) as c:
                c.executescript("""
                    CREATE TABLE matches(id INTEGER PRIMARY KEY AUTOINCREMENT,
                        ts INTEGER NOT NULL, artist TEXT, title TEXT, album TEXT,
                        track_key TEXT, art_url TEXT,
                        source TEXT NOT NULL DEFAULT 'server');
                    CREATE TABLE users(id INTEGER PRIMARY KEY AUTOINCREMENT,
                        username TEXT NOT NULL UNIQUE COLLATE NOCASE,
                        password_hash TEXT NOT NULL, is_admin INTEGER NOT NULL DEFAULT 0,
                        room_mic INTEGER NOT NULL DEFAULT 0, api_token TEXT UNIQUE,
                        created_at INTEGER NOT NULL);
                    CREATE TABLE settings(key TEXT PRIMARY KEY, value TEXT NOT NULL);
                    PRAGMA user_version = 2;""")
            db.init()
            assert [r["name"] for r in rooms.list_rooms()] == ["Server mic"], \
                "an upgrade keeps the microphone it had"
        finally:
            config.DB_PATH, config.CREATE_SERVER_MIC_ROOM = saved


def check_rates():
    """An administrator tunes how often Shazam is asked, within safe limits, and
    a change applies without a restart."""
    import asyncio
    import time
    from fastapi import HTTPException
    from scrobster import accounts, app, config, db, listener as listener_mod

    with tempfile.TemporaryDirectory() as d:
        previous = config.DB_PATH, config.RECOGNITION_GAP_SECONDS, config.MATCH_INTERVAL
        config.DB_PATH = os.path.join(d, "rates.db")
        try:
            db.init()
            admin = accounts.create_user("admin", "password123", is_admin=True)

            def put(body):
                return asyncio.run(app.set_recognition(body, admin=admin))
            for bad in ({"gap": 0.5}, {"gap": 61}, {"interval": 11}, {"gap": "fast"}):
                try:
                    put(bad)
                    raise AssertionError(f"{bad} must be refused")
                except HTTPException as e:
                    assert e.status_code == 400, e
            assert config.RECOGNITION_GAP_SECONDS == previous[1], "a refusal changes nothing"

            state = put({"gap": 2.5, "interval": 20})
            assert (config.RECOGNITION_GAP_SECONDS, config.MATCH_INTERVAL) == (2.5, 20)
            assert state["saved"] and state["gap"] == 2.5 and state["interval"] == 20
            assert db.get_setting("recognition_gap_seconds") == "2.5"
            state = put({"interval": 30})
            assert config.RECOGNITION_GAP_SECONDS == 2.5, "a field left out keeps its value"

            # What a restart reads back.
            config.set_rates(config.ENV_RECOGNITION_GAP_SECONDS, config.ENV_MATCH_INTERVAL)
            config.set_rates(db.get_setting("recognition_gap_seconds"),
                             db.get_setting("match_interval"))
            assert (config.RECOGNITION_GAP_SECONDS, config.MATCH_INTERVAL) == (2.5, 30)

            state = asyncio.run(app.reset_recognition(admin=admin))
            assert not state["saved"]
            assert (config.RECOGNITION_GAP_SECONDS, config.MATCH_INTERVAL) == \
                (config.ENV_RECOGNITION_GAP_SECONDS, config.ENV_MATCH_INTERVAL)
        finally:
            config.DB_PATH = previous[0]
            config.set_rates(previous[1], previous[2])

    # A room waiting out a long interval wakes when the interval shrinks.
    house = listener_mod.Listener()

    async def shorten_mid_wait():
        config.set_rates(interval=600)
        start = time.monotonic()
        wait = asyncio.create_task(house._until_next_cycle(start - 10))
        await asyncio.sleep(0.1)
        assert not wait.done(), "still inside the long interval"
        config.set_rates(interval=12)
        house.rates_changed()
        await asyncio.wait_for(wait, 5)
        return time.monotonic() - start
    try:
        took = asyncio.run(shorten_mid_wait())
        assert took < 3, f"the new interval had already passed, so the wait ends ({took:.1f}s)"
    finally:
        config.set_rates(interval=previous[2])

    # Requests are counted for the last ten minutes, and a 429 is remembered.
    gap = config.RECOGNITION_GAP_SECONDS
    config.set_rates(gap=0)
    try:
        async def answer(wav):
            if wav == b"429":
                raise listener_mod.RateLimited("slow down")
            return {}
        house._shazam.recognize = answer
        house._requests.append(time.monotonic() - listener_mod.REQUEST_WINDOW_SECONDS - 1)
        for _ in range(3):
            asyncio.run(house.recognize(b""))
        assert house.requests_recently() == 3, "an old request falls out of the count"
        assert house.last_rate_limited is None
        try:
            asyncio.run(house.recognize(b"429"))
        except listener_mod.RateLimited:
            pass
        assert abs(house.last_rate_limited - time.time()) < 5
    finally:
        config.set_rates(gap=gap)



def check_events():
    """A subscriber reads what happened at its own pace, and a slow one is told
    what it lost instead of holding everybody up."""
    import asyncio
    from scrobster import events

    with events.subscribe(size=3) as sub:
        events.emit("rooms")
        events.emit("member", room_id=1, user_id=2, on=True)
        got = sub.drain()
        assert [e["kind"] for e in got] == ["rooms", "member"], got
        assert got[1]["on"] is True and sub.drain() == []
        for _ in range(5):
            events.emit("rates")
        assert len(sub.drain()) == 3 and sub.dropped == 2, "the oldest are dropped"

        async def later():
            asyncio.get_running_loop().call_later(0.05, events.emit, "users")
            return await sub.wait(5)
        assert [e["kind"] for e in asyncio.run(later())] == ["users"], "wait wakes on emit"
        assert asyncio.run(sub.wait(0.01)) == [], "a timeout returns nothing"
    events.emit("rooms")
    assert sub.drain() == [], "a closed subscription receives nothing"


def check_mqtt():
    """Home Assistant devices: one per room and per user, removed when the room
    or user goes, even while scrobSter was away, and commands act like the API."""
    import asyncio
    import types
    from scrobster import accounts, colours, config, db, display, events
    from scrobster import listener as listener_mod, mqtt, rooms

    topics = mqtt.Topics("scrobster", "homeassistant", "abc123")
    assert topics.owns("scrobster_abc123") and topics.owns("scrobster_abc123_room_4")
    assert not topics.owns("scrobster_abc1234_room_4"), "another install"
    assert topics.states_of("scrobster_abc123_room_4") == [
        "scrobster/abc123/room/4/state", "scrobster/abc123/room/4/cover",
        "scrobster/abc123/room/4/now_playing", "scrobster/abc123/room/4/playing"]
    assert topics.states_of("scrobster_abc123_user_2") == ["scrobster/abc123/user/2/state"]
    assert topics.states_of("scrobster_abc123") == ["scrobster/abc123/state"]
    payload = {"components": {"a": {"platform": "sensor", "name": "A"}}}
    removed = mqtt.with_removals(payload, {"a": "sensor", "b": "switch"})
    assert removed["components"]["b"] == {"platform": "switch"}, "gone, platform only"
    assert mqtt.with_removals(payload, {"a": "sensor"}) is payload

    # A command off the broker is checked like a request to the API.
    assert not asyncio.run(mqtt.handle_command(None, topics, "scrobster/abc123/gap/set", "0.1"))
    assert not asyncio.run(mqtt.handle_command(None, topics, "scrobster/abc123/gap/set", "x"))
    assert not asyncio.run(mqtt.handle_command(None, topics, "scrobster/abc123/what/set", "1"))

    class Client:
        """Enough of aiomqtt.Client, with a broker that keeps retained messages."""
        def __init__(self, retained):
            self.inbox = asyncio.Queue()
            for topic, body in retained.items():
                self.inbox.put_nowait(self._message(topic, body))
            self.published = []

        @staticmethod
        def _message(topic, body):
            return types.SimpleNamespace(topic=types.SimpleNamespace(value=topic),
                                         payload=body.encode())

        async def subscribe(self, *args, **kwargs):
            pass

        async def unsubscribe(self, *args, **kwargs):
            pass

        async def publish(self, topic, payload, qos=0, retain=False):
            self.published.append((topic, payload, retain))

        def last(self, topic):
            found = [p for t, p, _ in self.published if t == topic]
            return found[-1] if found else None

        @property
        def messages(self):
            async def read():
                while True:
                    yield await self.inbox.get()
            return read()

    async def until(test, what):
        for _ in range(200):
            if test():
                return
            await asyncio.sleep(0.01)
        raise AssertionError(f"timed out: {what}")

    # The cover and its colours, without the network or ffmpeg: check_colours
    # tests the extraction itself.
    red = {"primary": {"rgb": [200, 30, 30], "hex": "#c81e1e"},
           "secondary": {"rgb": [20, 60, 200], "hex": "#143cc8"},
           "brightness": 200, "saturated": True}
    covers_asked = []

    async def fake_fetch(url):
        covers_asked.append(url)
        return "image/jpeg", b"cover", '"1"'

    worked_out = []

    async def fake_colours(image):
        assert image == b"cover"
        worked_out.append(image)
        return red

    with tempfile.TemporaryDirectory() as d:
        saved = (config.DB_PATH, mqtt.TICK_SECONDS, mqtt.RETAINED_WAIT_SECONDS,
                 config.RECOGNITION_GAP_SECONDS, display._fetch, colours.of_image)
        config.DB_PATH = os.path.join(d, "mqtt.db")
        mqtt.TICK_SECONDS = mqtt.RETAINED_WAIT_SECONDS = 0.05
        display._fetch, colours.of_image = fake_fetch, fake_colours
        colours._found.clear()
        colours._failed.clear()
        try:
            db.init()
            admin = accounts.create_user("admin", "password123", is_admin=True)
            sam = accounts.create_user("sam", "password123")
            mic = rooms.list_rooms()[0]
            iid = mqtt.instance_id()
            assert mqtt.instance_id() == iid, "kept, so entities keep their history"
            topics = mqtt.Topics("scrobster", "homeassistant", iid)
            gone = topics.device_id("room", 99)
            elsewhere = "scrobster_ffffff_room_1"
            client = Client({
                topics.config(gone): json.dumps({"components": {
                    "level": {"platform": "sensor"}}}),
                topics.config(topics.device_id("user", admin["id"])): json.dumps({
                    "components": {"room_42": {"platform": "switch", "name": "x"}}}),
                topics.config(elsewhere): json.dumps({"components": {}}),
            })

            async def scenario():
                house = listener_mod.Listener()
                await house.sync_rooms()
                with events.subscribe() as sub:
                    session = mqtt._Session(house, client, topics, sub)
                    task = asyncio.create_task(session.run())
                    await until(lambda: client.last(topics.status) == "online", "online")

                    # Left behind while away: removed with its state.
                    assert client.last(topics.config(gone)) == ""
                    assert client.last(topics.room(99)) == "" == client.last(topics.room_cover(99))
                    assert client.last(topics.config(elsewhere)) is None, "not ours"
                    user_config = json.loads(client.last(
                        topics.config(topics.device_id("user", admin["id"]))))
                    assert user_config["components"]["room_42"] == {"platform": "switch"}
                    switch = user_config["components"][f"room_{mic['id']}"]
                    assert switch["command_topic"] == \
                        f"{topics.root}/user/{admin['id']}/room/{mic['id']}/set"
                    hub = json.loads(client.last(topics.config(topics.device_id())))
                    assert hub["components"]["listening"]["state_topic"] == topics.hub
                    room_config = json.loads(client.last(
                        topics.config(topics.device_id("room", mic["id"]))))
                    assert room_config["device"]["suggested_area"] == mic["name"]
                    assert "url" not in json.dumps(room_config["components"]["enabled"])
                    state = json.loads(client.last(topics.room(mic["id"])))
                    assert state["now_playing"] == "idle" and not state["playing"]
                    assert client.last(topics.room_cover(mic["id"])) is None, "no cover yet"
                    assert json.loads(client.last(topics.room_now_playing(mic["id"]))) == {
                        "room": mic["name"], "playing": False, "artist": None,
                        "title": None, "album": None, "art_url": None, "colors": None}
                    assert client.last(topics.room_playing(mic["id"])) == "OFF"
                    # The cover is unavailable while the room is idle, and
                    # while scrobSter is away, like everything else.
                    assert room_config["availability"] == [{"topic": topics.status}]
                    cover = room_config["components"]["cover"]
                    assert cover["availability_mode"] == "all"
                    assert [a["topic"] for a in cover["availability"]] == [
                        topics.status, topics.room(mic["id"])]
                    assert "playing" in cover["availability"][1]["value_template"]
                    assert cover["unique_id"] == f"{topics.device_id('room', mic['id'])}_cover", \
                        "kept, for the installs that have it"
                    # Every entity keeps the unique_id it had, and the cover's
                    # colour is a new one, unavailable like the cover.
                    device = topics.device_id("room", mic["id"])
                    assert {k: c["unique_id"] for k, c in room_config["components"].items()} \
                        == {k: f"{device}_{k}" for k in (
                            "now_playing", "cover", "cover_colour", "playing", "level",
                            "running", "problem", "error", "enabled", "match")}
                    assert hub["components"]["listening"]["unique_id"] == \
                        f"{topics.device_id()}_listening"
                    assert user_config["components"]["last_scrobble"]["unique_id"] == \
                        f"{topics.device_id('user', admin['id'])}_last_scrobble"
                    colour = room_config["components"]["cover_colour"]
                    assert colour["platform"] == "sensor" and colour["name"] == "Cover colour"
                    assert colour["icon"] == "mdi:palette"
                    assert colour["state_topic"] == topics.room(mic["id"])
                    assert colour["availability_mode"] == "all"
                    assert [a["topic"] for a in colour["availability"]] == [
                        topics.status, topics.room(mic["id"])]
                    assert "colors" in colour["availability"][1]["value_template"]
                    assert state["colors"] is None

                    # Commands act at once, and the new state follows.
                    client.inbox.put_nowait(Client._message(
                        f"{topics.root}/user/{sam['id']}/room/{mic['id']}/set", "ON"))
                    await until(lambda: rooms.rooms_of(sam["id"]) == [mic["id"]], "member")
                    await until(lambda: json.loads(client.last(topics.user(sam["id"])))
                                ["rooms"] == [mic["id"]], "user state")
                    client.inbox.put_nowait(Client._message(f"{topics.root}/gap/set", "7"))
                    await until(lambda: config.RECOGNITION_GAP_SECONDS == 7, "rate")
                    await until(lambda: json.loads(client.last(topics.hub))["gap"] == 7, "hub")

                    # A match fires the room's event and shows as playing.
                    info = {"artist": "A", "title": "Song", "album": "Al", "art_url": "u",
                            "offset": 10, "isrc": None, "track_key": "k"}
                    await house._on_match(info, "server", [sam], house.rooms[mic["id"]])
                    await until(lambda: client.last(topics.room_event(mic["id"])), "event")
                    fired = json.loads(client.last(topics.room_event(mic["id"])))
                    assert fired["event_type"] == "match" and fired["users"] == ["sam"]
                    state = json.loads(client.last(topics.room(mic["id"])))
                    assert state["now_playing"] == "A - Song" and state["playing"]
                    assert client.last(topics.room_cover(mic["id"])) == "u"
                    assert client.last(topics.room_playing(mic["id"])) == "ON"
                    # The cover's colours follow as soon as they are worked out.
                    await until(lambda: json.loads(client.last(
                        topics.room_now_playing(mic["id"])))["colors"], "colours")
                    playing = json.loads(client.last(topics.room_now_playing(mic["id"])))
                    assert playing == {"room": mic["name"], "playing": True, "artist": "A",
                                       "title": "Song", "album": "Al", "art_url": "u",
                                       "colors": red}
                    assert json.loads(client.last(topics.room(mic["id"])))["colors"] == red
                    assert covers_asked == ["u"], "once, at the size the host allows"

                    # The same song on the next cycle is not a new event.
                    fired_before = sum(t == topics.room_event(mic["id"])
                                       for t, _, _ in client.published)
                    await house._on_match(info, "server", [sam], house.rooms[mic["id"]])
                    await asyncio.sleep(0.15)
                    assert sum(t == topics.room_event(mic["id"]) for t, _, _
                               in client.published) == fired_before, "one event per song"
                    assert session.starts_song(mic["id"], {"track_key": "k2", "ts": 1}), \
                        "another song is"
                    assert session.starts_song(mic["id"], {
                        "track_key": "k2", "ts": 1 + config.NOW_PLAYING_STOP_SECONDS}), \
                        "and so is the same song after a silence"

                    # A deleted room leaves Home Assistant, and its switches go.
                    other = rooms.add_room(name="Hall", kind="device")
                    await house.sync_rooms()
                    hall = topics.device_id("room", other["id"])
                    await until(lambda: client.last(topics.config(hall)), "new room")
                    rooms.delete_room(other["id"])
                    await house.sync_rooms()
                    await until(lambda: client.last(topics.config(hall)) == "", "removed")
                    user_config = json.loads(client.last(
                        topics.config(topics.device_id("user", sam["id"]))))
                    assert user_config["components"][f"room_{other['id']}"] == \
                        {"platform": "switch"}

                    # The same song matching again sends no new record to a display.
                    sent_before = sum(t == topics.room_now_playing(mic["id"])
                                      for t, _, _ in client.published)
                    await house._on_match(info, "server", [sam], house.rooms[mic["id"]])
                    await asyncio.sleep(0.15)
                    assert sum(t == topics.room_now_playing(mic["id"]) for t, _, _
                               in client.published) == sent_before, "the record did not change"

                    # The music stops: a display goes blank, and Home Assistant's
                    # image is unavailable, so neither keeps the last record.
                    house.rooms[mic["id"]].last_match["ts"] -= config.NOW_PLAYING_STOP_SECONDS
                    await until(lambda: client.last(topics.room_playing(mic["id"])) == "OFF",
                                "stopped")
                    stopped = json.loads(client.last(topics.room_now_playing(mic["id"])))
                    assert not stopped["playing"] and stopped["art_url"] is None
                    assert stopped["artist"] is None and stopped["title"] is None
                    assert stopped["colors"] is None
                    state = json.loads(client.last(topics.room(mic["id"])))
                    assert state["now_playing"] == "idle" and not state["playing"]
                    assert state["colors"] is None, "the colour sensor goes unavailable"
                    assert len(worked_out) == 1, "the same cover is not worked out again"
                    assert client.last(topics.room_cover(mic["id"])) == "u", \
                        "never emptied, Home Assistant would log an error"

                    published = len(client.published)
                    await asyncio.sleep(0.2)
                    assert len(client.published) == published, "nothing changed, nothing sent"
                    task.cancel()
                    try:
                        await task
                    except asyncio.CancelledError:
                        pass
            asyncio.run(scenario())
        finally:
            (config.DB_PATH, mqtt.TICK_SECONDS, mqtt.RETAINED_WAIT_SECONDS) = saved[:3]
            config.set_rates(gap=saved[3])
            display._fetch, colours.of_image = saved[4:]
            display._covers.clear()
            colours._found.clear()
            colours._failed.clear()



def _cover(source):
    """A JPEG from one of ffmpeg's test sources, as a cover would be."""
    import subprocess
    return subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", source,
                           "-frames:v", "1", "-f", "image2pipe", "-c:v", "mjpeg", "-"],
                          capture_output=True, check=True).stdout


def check_colours():
    """A cover's colours for a lamp: the main one a light can show, another
    that looks different, and a warm white for a cover with no colour."""
    import asyncio
    import colorsys
    import shutil
    import time
    from scrobster import colours

    def raw(*areas):
        """24x24 RGB pixels: (rgb, share) areas, in order."""
        out = []
        for rgb, share in areas:
            out += [rgb] * round(share * 576)
        out += [areas[-1][0]] * (576 - len(out))
        return bytes(c for p in out for c in p)

    def hue(c):
        return colorsys.rgb_to_hsv(*(v / 255 for v in c["rgb"]))[0] * 360

    # The picking alone, on raw pixels, so it runs without ffmpeg too.
    found = colours.pick(raw(((200, 20, 20), 0.7), ((20, 20, 20), 0.3)))
    assert found["primary"]["hex"] == "#c81414" and found["secondary"] is None, found
    assert found["saturated"] and found["brightness"] == 200
    assert found["primary"]["rgb"] == [200, 20, 20]
    # The most common colour a light can show, not the most common colour.
    found = colours.pick(raw(((5, 5, 5), 0.6), ((30, 90, 220), 0.25), ((240, 200, 30), 0.15)))
    assert found["primary"]["hex"] == "#1e5adc" and found["secondary"]["hex"] == "#f0c81e", found
    # One hue, light and dark: one colour for a lamp, so no secondary.
    found = colours.pick(raw(((220, 30, 30), 0.6), ((110, 15, 15), 0.4)))
    assert found["primary"]["hex"] == "#dc1e1e" and found["secondary"] is None, found
    found = colours.pick(bytes([255, 0, 0]) * 346 + bytes([64, 0, 0]) * 230)
    assert found["primary"]["hex"] == "#ff0000" and found["secondary"] is None, \
        "a dark red is the same colour to a lamp, however dark"
    # Red and pink differ in colour, not only in light.
    assert colours.pick(raw(((220, 30, 30), 0.6), ((250, 140, 170), 0.4)))["secondary"]
    # A speck of colour is not the cover's colour.
    found = colours.pick(raw(((200, 20, 20), 0.02), ((128, 128, 128), 0.98)))
    assert not found["saturated"] and found["secondary"] is None
    # No colour at all: a warm white, as light as the cover.
    for grey, low in (((0, 0, 0), True), ((128, 128, 128), None), ((255, 255, 255), False)):
        found = colours.pick(raw((grey, 1)))
        assert found["primary"]["rgb"] == list(colours.WARM_WHITE) and not found["saturated"]
        assert found["secondary"] is None
        assert 0 < found["brightness"] <= 255
        if low is not None:
            assert (found["brightness"] < 100) == low, (grey, found)
    # The same pixels, the same answer, whatever their order.
    mixed = raw(((30, 90, 220), 0.5), ((240, 200, 30), 0.3), ((10, 10, 10), 0.2))
    turned = bytes(c for i in range(575, -1, -1) for c in mixed[i * 3:i * 3 + 3])
    assert colours.pick(mixed) == colours.pick(turned) == colours.pick(mixed)

    if not shutil.which("ffmpeg"):
        print("check_colours: no ffmpeg here, so covers are not decoded")
        return

    async def of(source):
        return await colours.of_image(_cover(source))

    # A mostly red cover with a little writing on it.
    started = time.monotonic()
    found = asyncio.run(of("color=c=0xd02020:s=200x200,"
                           "drawbox=x=20:y=150:w=120:h=20:c=white:t=fill"))
    assert time.monotonic() - started < 3, "quick"
    assert found["saturated"] and (hue(found["primary"]) < 15 or hue(found["primary"]) > 345)
    r, g, b = found["primary"]["rgb"]
    assert r > 170 and g < 70 and b < 70, found
    assert found["secondary"] is None, "white is not a colour for a lamp"
    # Two tones: blue on most of it, yellow on the rest.
    found = asyncio.run(of("color=c=0x1040c0:s=200x200,"
                           "drawbox=x=0:y=0:w=80:h=200:c=0xf0c020:t=fill"))
    assert 210 < hue(found["primary"]) < 235 and 35 < hue(found["secondary"]) < 55, found
    assert found["primary"]["hex"].startswith("#") and len(found["primary"]["hex"]) == 7
    # ffmpeg's busy test card has many colours; two of them are picked.
    found = asyncio.run(of("testsrc=s=200x200"))
    assert found["saturated"] and found["secondary"], found
    # Black, white and grey: a usable warm white, said to be no colour.
    for colour in ("black", "white", "0x808080"):
        found = asyncio.run(of(f"color=c={colour}:s=200x200"))
        assert not found["saturated"] and found["primary"]["rgb"] == list(colours.WARM_WHITE)
        assert found["secondary"] is None, colour
    # Not a picture: None, and nothing raised.
    assert asyncio.run(colours.of_image(b"\xff\xd8 not really a jpeg")) is None
    assert asyncio.run(colours.of_image(b"")) is None

    # Per cover: worked out once, then looked up; an unreadable cover is
    # kept as None, a cover that cannot be fetched is asked for again later.
    from scrobster import display, events
    blue = _cover("color=c=0x1040c0:s=100x100")
    asked = []

    async def fake_fetch(url):
        asked.append(url)
        if "missing" in url:
            raise ValueError("cover is text/html, not an image")
        return "image/jpeg", b"junk" if "junk" in url else blue, '"1"'

    apple = ("https://is1-ssl.mzstatic.com/image/thumb/Music1/v4/aa/0c72/x.rgb.jpg/"
             "400x400cc.jpg")
    saved = display._fetch
    display._fetch = fake_fetch
    for cache in (display._covers, display._failed, colours._found, colours._failed):
        cache.clear()
    try:
        async def scenario():
            with events.subscribe() as sub:
                colours.prepare_soon(apple, 1)
                colours.prepare_soon(apple, 2)  # another room, the same record
                for _ in range(300):
                    if colours.cached(apple):
                        break
                    await asyncio.sleep(0.01)
                found = colours.cached(apple)
                assert found and 210 < hue(found["primary"]) < 235, found
                assert asked == [display.sized_url(apple, colours.FETCH_SIZE)], asked
                assert asked[0].endswith("/100x100cc.jpg"), "a small one is enough"
                await asyncio.sleep(0.05)
                told = [(e["room_id"], e["art_url"]) for e in sub.drain()
                        if e["kind"] == "colors"]
                assert told == [(1, apple), (2, apple)], told
                colours.prepare_soon(apple, 1)
                assert await colours.colours_of(apple) is found and len(asked) == 1

                junk = "https://example.com/junk.jpg"
                assert await colours.colours_of(junk) is None
                assert junk in colours._found and colours.cached(junk) is None
                colours.prepare_soon(junk, 1)
                assert not colours._rooms, "not tried again"

                missing = "https://example.com/missing.jpg"
                assert await colours.colours_of(missing) is None
                assert missing not in colours._found and missing in colours._failed
                tries = len(asked)
                assert await colours.colours_of(missing) is None and len(asked) == tries, \
                    "not asked again within RETRY_SECONDS"
                await asyncio.sleep(0.05)
                assert not [e for e in sub.drain() if e["kind"] == "colors"]
                assert colours.cached(None) is None
        asyncio.run(scenario())
    finally:
        display._fetch = saved
        for cache in (display._covers, display._failed, colours._found, colours._failed):
            cache.clear()


def check_event_stream():
    """/api/events shows each user what the rest of the API would show them."""
    from scrobster.app import _sse, visible_event

    admin, sam = {"id": 1, "is_admin": 1}, {"id": 2, "is_admin": 0}
    match = {"artist": "A", "title": "T"}
    room = {"kind": "match", "ts": 1, "room_id": 3, "users": [1], "match": match}
    assert visible_event(room, sam) == {"kind": "match", "ts": 1, "room_id": 3,
                                        "match": match, "mine": False}, "a room is everybody's"
    assert visible_event(room, admin)["users"] == [1] and visible_event(room, admin)["mine"]
    clip = {**room, "room_id": None}
    assert visible_event(clip, sam) is None, "a browser clip is its owner's"
    assert visible_event(clip, admin)["mine"]
    scrobbled = {"kind": "scrobble", "ts": 1, "user_id": 1, "match_id": 9,
                 "results": {"listenbrainz": "ok"}}
    assert visible_event(scrobbled, sam) is None and visible_event(scrobbled, admin)["match_id"] == 9
    assert visible_event({"kind": "services", "ts": 1, "user_id": 1}, sam) is None
    assert visible_event({"kind": "cleared", "ts": 1, "users": [1]}, sam) is None
    assert "users" not in visible_event({"kind": "cleared", "ts": 1, "users": [1, 2]}, sam)
    member = {"kind": "member", "ts": 1, "room_id": 3, "user_id": 1, "on": True}
    assert visible_event(member, sam) is None and visible_event(member, admin)["on"]
    assert visible_event({**member, "user_id": 2}, sam)["room_id"] == 3
    assert visible_event({"kind": "users", "ts": 1}, sam) is None
    assert visible_event({"kind": "users", "ts": 1}, admin) is not None
    assert visible_event({"kind": "listening", "ts": 1, "on": False}, sam)["on"] is False
    assert visible_event({"kind": "rooms", "ts": 1}, sam) is not None
    colors = {"kind": "colors", "ts": 1, "room_id": 3, "art_url": "u"}
    assert visible_event(colors, sam) == {"kind": "colors", "ts": 1, "room_id": 3}
    assert visible_event({"kind": "something new", "ts": 1}, admin) is None, "unknown stays out"
    assert _sse("rooms", {"a": 1}) == 'event: rooms\ndata: {"a":1}\n\n'



def check_restart():
    """A restart keeps what was scrobbled, so a song still playing is not
    scrobbled twice, and the add-on finds a broker installed after it started."""
    import asyncio
    from scrobster import accounts, config, db, listener as listener_mod, mqtt

    with tempfile.TemporaryDirectory() as d:
        saved = config.DB_PATH
        config.DB_PATH = os.path.join(d, "restart.db")
        try:
            db.init()
            sam = accounts.create_user("sam", "password123")
            now = int(time.time())
            keep_s = listener_mod.play_memory_seconds(config.RESCROBBLE_MINUTES * 60)
            db.save_play(sam["id"], "old", now - keep_s - 10, 5, now - keep_s - 60)
            db.save_play(sam["id"], "song", now - 60, 30.0, now - keep_s)
            db.save_play(sam["id"], "song", now - 50, 40.0, now - keep_s)

            house = listener_mod.Listener()
            house.restore_plays()
            assert house._last_plays == {(sam["id"], "song"): (now - 50, 40.0)}, \
                house._last_plays
            cooldown = config.RESCROBBLE_MINUTES * 60
            assert not listener_mod.should_scrobble((sam["id"], "song"), now, 90,
                                                    house._last_plays, cooldown), \
                "the same play, after a restart, is not scrobbled again"
            assert listener_mod.should_scrobble((sam["id"], "song"), now, 5,
                                                house._last_plays, cooldown), "a repeat is"
            accounts.delete_user(sam["id"])
            assert db.load_plays(0) == {}, "a deleted user's plays go with them"
        finally:
            config.DB_PATH = saved

    async def late_broker():
        answers = [None, None, {"host": "core-mosquitto", "port": 1883}]

        async def ask(warn=True):
            return answers.pop(0)
        previous = (mqtt.broker_settings, mqtt.BROKER_RETRY_SECONDS, config.SUPERVISOR_TOKEN)
        mqtt.broker_settings, mqtt.BROKER_RETRY_SECONDS = ask, 0.01
        config.SUPERVISOR_TOKEN = "token"
        try:
            return await mqtt.Publisher(None)._find_broker()
        finally:
            mqtt.broker_settings, mqtt.BROKER_RETRY_SECONDS, config.SUPERVISOR_TOKEN = previous
    assert asyncio.run(late_broker())["host"] == "core-mosquitto"

    async def no_supervisor():
        async def ask(warn=True):
            return None
        previous = mqtt.broker_settings, config.SUPERVISOR_TOKEN
        mqtt.broker_settings, config.SUPERVISOR_TOKEN = ask, None
        try:
            return await asyncio.wait_for(mqtt.Publisher(None)._find_broker(), 1)
        finally:
            mqtt.broker_settings, config.SUPERVISOR_TOKEN = previous
    assert asyncio.run(no_supervisor()) is None, "outside an add-on, give up at once"



def check_known_song():
    """A room that hears the song it matched twice stops asking Shazam until
    the song should end, at most SKIP_KNOWN_SECONDS, and silence or another
    song makes it ask again."""
    import asyncio
    from scrobster import accounts, config, db, enrich, listener as listener_mod

    known_for = listener_mod.known_for
    song = {"track_key": "k", "offset": 100.0}
    assert known_for(None, song, 300, 15, 60) == 0, "once is not enough"
    assert known_for("other", song, 300, 15, 60) == 0, "a new song is not known"
    assert known_for("k", None, 300, 15, 60) == 0, "no match, nothing known"
    assert known_for("k", song, 300, 15, 0) == 0, "0 turns it off"
    assert known_for("k", song, None, 15, 60) == 60, "no length: the cap"
    assert known_for("k", song, 300, 15, 60) == 60, "far from the end: the cap"
    assert known_for("k", song, 140, 15, 60) == 140 - 100 - 15 - 5, "ask before it ends"
    assert known_for("k", song, 110, 15, 60) == 0, "already ending"
    assert known_for("k", {"track_key": "k", "offset": None}, 300, 15, 60) == 60

    loud, quiet = _wav(8000), _wav(0)
    heard = []  # what each cycle recorded

    async def scenario():
        house = listener_mod.Listener()
        await house.sync_rooms()
        room = house.rooms[min(house.rooms)]
        asked = []

        async def capture(**kwargs):
            await asyncio.sleep(0)
            return heard.pop(0) if heard else quiet

        async def recognize(wav):
            asked.append(wav)
            return {"track": {"key": "k", "title": "T", "subtitle": "A", "isrc": "X"},
                    "matches": [{"offset": 50.0}]}
        saved = listener_mod.capture_chunk, config.MATCH_INTERVAL, config.SKIP_KNOWN_SECONDS
        listener_mod.capture_chunk = capture
        house.recognize = recognize
        config.MATCH_INTERVAL, config.SKIP_KNOWN_SECONDS = 0, 60
        try:
            heard.extend([loud] * 5 + [quiet] + [loud] * 2)
            task = asyncio.create_task(house._loop(room))
            while heard:
                await asyncio.sleep(0.01)
            await asyncio.sleep(0.05)
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        finally:
            listener_mod.capture_chunk, config.MATCH_INTERVAL, config.SKIP_KNOWN_SECONDS = saved
        return len(asked), room.skipped

    with tempfile.TemporaryDirectory() as d:
        saved = config.DB_PATH
        config.DB_PATH = os.path.join(d, "known.db")
        try:
            db.init()
            accounts.create_user("admin", "password123", is_admin=True)
            enrich._mbids["X"] = {}  # no network: MusicBrainz is already "asked"
            asked, skipped = asyncio.run(scenario())
        finally:
            config.DB_PATH = saved
            enrich._mbids.pop("X", None)
    # Two asks make the song known, three cycles skip, silence forgets it, and
    # the next two cycles must ask again before skipping resumes.
    assert (asked, skipped) == (4, 3), (asked, skipped)



def check_librefm_scrobble():
    """Libre.fm gets one scrobble in the plain form, which it accepts, rather
    than pylast's indexed batch form, which it answers with "Invalid
    parameters". Last.fm keeps pylast's own call."""
    import pylast
    from scrobster import scrobble

    assert scrobble.scrobble_params("A", "T", None, 1700000000) == {
        "artist": "A", "track": "T", "timestamp": "1700000000"}, "no album, no key"
    assert scrobble.scrobble_params("A", "T", "Al", 1)["album"] == "Al"

    sent, scrobbled = [], []

    class Request:
        def __init__(self, network, method, params):
            sent.append((network, method, params))

        def execute(self):
            pass

    class Network:
        def scrobble(self, **kwargs):
            scrobbled.append(kwargs)
    saved = pylast._Request, scrobble._pylast_network
    pylast._Request, scrobble._pylast_network = Request, lambda service, data: Network()
    try:
        scrobble._pylast_scrobble("librefm", {}, "A", "T", "Al", 5)
        scrobble._pylast_scrobble("lastfm", {}, "A", "T", "Al", 5)
    finally:
        pylast._Request, scrobble._pylast_network = saved
    assert [(m, p) for _, m, p in sent] == [("track.scrobble", {
        "artist": "A", "track": "T", "timestamp": "5", "album": "Al"})], sent
    assert not any("[" in key for _, _, p in sent for key in p), "no indexed keys"
    assert scrobbled == [{"artist": "A", "title": "T", "album": "Al", "timestamp": 5}]



def check_outage():
    """A room that keeps failing logs once, then a reminder every 15 minutes
    and each new reason, then once when it works again, and retries more
    gently in between."""
    import asyncio
    import logging
    from scrobster import accounts, config, db, listener as listener_mod

    out = listener_mod.Outage()
    assert out.recovered(0) is None, "nothing to recover from"
    assert out.retry_after(15) == 15
    level, said = out.failed("No route to host", 1000)
    assert level == "warning" and "No route to host" in said
    assert out.failed("No route to host", 1015) is None, "the same failure is quiet"
    assert out.retry_after(15) == 30 and out.failed("No route to host", 1045) is None
    assert out.retry_after(15) == 60 and out.failed("No route to host", 1105) is None
    assert out.retry_after(15) == 60, "never longer than a minute"
    assert out.retry_after(120) == 120, "never shorter than the room's own interval"
    assert out.failed("Connection refused", 1165)[0] == "info", "a new reason is told"
    assert out.failed("No route to host", 1225) is None, "an old reason is not again"
    level, said = out.failed("No route to host", 1000 + 15 * 60)
    assert level == "warning" and "15 min" in said and "(7 tries)" in said, said
    assert out.failed("No route to host", 1000 + 16 * 60) is None
    assert out.recovered(1000 + 47 * 60) == "working again after 47 min"
    assert out.since is None and out.tries == 0 and out.retry_after(15) == 15
    assert out.failed("No route to host", 5000)[0] == "warning", "a new outage warns again"

    lines = []

    class Keep(logging.Handler):
        def emit(self, record):
            if record.levelno >= logging.INFO and "Master" in record.getMessage():
                lines.append(record.getMessage())

    async def scenario():
        house = listener_mod.Listener()
        await house.sync_rooms()
        room = house.rooms[min(house.rooms)]
        room.name = "Master"
        script = ["fail"] * 6 + ["ok"] * 2
        seen = []

        async def capture(**kwargs):
            await asyncio.sleep(0)
            step = script.pop(0) if script else "ok"
            if step == "fail":
                raise RuntimeError("the stream failed: No route to host")
            seen.append(room.outage.since)
            return _wav(0)  # silent, so Shazam is never asked
        saved = listener_mod.capture_chunk, config.MATCH_INTERVAL
        listener_mod.capture_chunk, config.MATCH_INTERVAL = capture, 0
        try:
            task = asyncio.create_task(house._loop(room))
            while script:
                await asyncio.sleep(0.01)
            await asyncio.sleep(0.05)
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        finally:
            listener_mod.capture_chunk, config.MATCH_INTERVAL = saved
        return room, seen

    handler = Keep()
    logging.getLogger("scrobster").addHandler(handler)
    with tempfile.TemporaryDirectory() as d:
        saved = config.DB_PATH
        config.DB_PATH = os.path.join(d, "outage.db")
        try:
            db.init()
            accounts.create_user("admin", "password123", is_admin=True)
            room, seen = asyncio.run(scenario())
        finally:
            config.DB_PATH = saved
            logging.getLogger("scrobster").removeHandler(handler)
    failing = [line for line in lines if "fail" in line]
    assert len(failing) == 1 and "No route to host" in failing[0], lines
    assert any("working again" in line for line in lines), lines
    assert seen[0] is not None, "the first success still sees the outage"
    assert room.outage.since is None and room.last_error is None


def _wav_of(samples, rate=16000):
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(b"".join(struct.pack("<h", max(-32768, min(32767, int(s))))
                               for s in samples))
    return buf.getvalue()


def _vinyl(seconds=3, seed=1):
    """A record between sides: surface noise near -58 dBFS RMS, with a loud
    click about every 0.3 s."""
    import random
    rnd = random.Random(seed)
    out = [rnd.gauss(0, 41) for _ in range(16000 * seconds)]
    for start in range(1000, len(out), 4800):
        for i in range(16):
            out[start + i] = 16000 * (-1) ** i
    return _wav_of(out)


def _tone(amplitude, seconds=3):
    return _wav_of(amplitude * math.sin(i * 0.1) for i in range(16000 * seconds))


def check_quiet():
    """The end of a record side counts as silence once QUIET_DBFS is set: its
    clicks lift the peak, but not the floor. Music, quiet music too, does not.
    Left unset, nothing changes."""
    import asyncio
    from scrobster import accounts, config, db, enrich, listener as listener_mod
    floor_dbfs, is_quiet = listener_mod.floor_dbfs, listener_mod.is_quiet

    vinyl, music, soft = _vinyl(), _tone(8000), _tone(300)
    assert peak_dbfs(vinyl) > -10, "the clicks are loud, so the peak test hears sound"
    assert -61 < floor_dbfs(vinyl) < -55, floor_dbfs(vinyl)
    assert -17 < floor_dbfs(music) < -14, "a sine's RMS is 3 dB under its peak"
    assert -45 < floor_dbfs(soft) < -42, floor_dbfs(soft)
    assert floor_dbfs(_wav(0)) == -99.0, "digital silence"
    assert floor_dbfs(_wav_of([])) == -99.0, "no samples at all"
    assert floor_dbfs(_wav_of([8000] * 10)) > -13, "shorter than one window"
    # Music with a gap under half the clip, as between two songs, keeps its floor.
    gap = _wav_of([0] * 16000 + [8000 * math.sin(i * 0.1) for i in range(32000)])
    assert floor_dbfs(gap) > -20, floor_dbfs(gap)

    assert not is_quiet(floor_dbfs(vinyl), None), "off by default"
    assert is_quiet(floor_dbfs(vinyl), -50), "the run-out groove is quiet"
    assert not is_quiet(floor_dbfs(music), -50), "music is not"
    assert not is_quiet(floor_dbfs(soft), -50), "a quiet intro is not"
    assert config.QUIET_DBFS is None, "the default keeps the peak test alone"

    heard = []

    async def scenario(quiet_dbfs):
        house = listener_mod.Listener()
        await house.sync_rooms()
        room = house.rooms[min(house.rooms)]
        asked, quiet_seen, levels = [], [], []

        async def capture(**kwargs):
            await asyncio.sleep(0)
            return heard.pop(0) if heard else _wav(0)

        async def recognize(wav):
            asked.append(wav)
            if wav is vinyl:
                return {}
            return {"track": {"key": "k", "title": "T", "subtitle": "A", "isrc": "X"},
                    "matches": [{"offset": 50.0}]}

        async def clear():
            quiet_seen.append(room.quiet)
            levels.append(room.last_level_db)
        saved = (listener_mod.capture_chunk, config.MATCH_INTERVAL,
                 config.SKIP_KNOWN_SECONDS, config.QUIET_DBFS)
        listener_mod.capture_chunk = capture
        house.recognize, house._clear_if_stopped = recognize, clear
        config.MATCH_INTERVAL, config.SKIP_KNOWN_SECONDS = 0, 60
        config.QUIET_DBFS = quiet_dbfs
        try:
            heard.extend([music] * 3 + [vinyl] * 3 + [music])
            task = asyncio.create_task(house._loop(room))
            while heard:
                await asyncio.sleep(0.01)
            await asyncio.sleep(0.05)
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        finally:
            (listener_mod.capture_chunk, config.MATCH_INTERVAL,
             config.SKIP_KNOWN_SECONDS, config.QUIET_DBFS) = saved
        return asked, quiet_seen[:7], levels[:7]

    with tempfile.TemporaryDirectory() as d:
        saved = config.DB_PATH
        config.DB_PATH = os.path.join(d, "quiet.db")
        try:
            db.init()
            accounts.create_user("admin", "password123", is_admin=True)
            enrich._mbids["X"] = {}  # no network: MusicBrainz is already "asked"
            asked, quiet_seen, levels = asyncio.run(scenario(-50))
            asked_off, quiet_off, _ = asyncio.run(scenario(None))
        finally:
            config.DB_PATH = saved
            enrich._mbids.pop("X", None)
    # Two asks make the song known and the third cycle skips. The side ends:
    # three quiet cycles ask nothing and forget the song, so the next side asks.
    assert [w is vinyl for w in asked] == [False, False, False], len(asked)
    assert quiet_seen == [False] * 3 + [True] * 3 + [False], quiet_seen
    assert min(levels[3:6]) > -10, "the level still shows the peak"
    # Unset, as before, the clicks pass for the song still playing, which the
    # room keeps skipping as known.
    assert [w is vinyl for w in asked_off] == [False, False], len(asked_off)
    assert not any(quiet_off)


def _request(asgi_app, path, headers=None, method="GET", body=b""):
    """One request straight into the ASGI app, with no server and no HTTP
    client. Returns (status, {header: value}, body)."""
    sending = body
    import asyncio
    from urllib.parse import urlsplit
    url = urlsplit(path)
    scope = {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1",
             "method": method, "scheme": "http", "path": url.path,
             "raw_path": url.path.encode(), "query_string": url.query.encode(),
             "headers": [(k.lower().encode(), v.encode()) for k, v in (headers or {}).items()],
             "client": ("192.0.2.7", 50000), "server": ("scrobster", 80), "root_path": ""}
    sent, asked = [], []

    async def receive():
        # The body once, then the client goes, which ends an event stream.
        asked.append(1)
        if len(asked) == 1:
            return {"type": "http.request", "body": sending, "more_body": False}
        await asyncio.sleep(0.05)
        return {"type": "http.disconnect"}

    async def send(message):
        sent.append(message)

    asyncio.run(asgi_app(scope, receive, send))
    start = next(m for m in sent if m["type"] == "http.response.start")
    head = {k.decode(): v.decode() for k, v in start["headers"]}
    body = b"".join(m.get("body", b"") for m in sent if m["type"] == "http.response.body")
    return start["status"], head, body


def check_display():
    """A display token reads what the rooms play and nothing else. The cover
    has a fixed URL, a size, an ETag, and a defined answer while nothing plays."""
    import asyncio
    import shutil
    import sqlite3
    import struct
    import subprocess
    import time
    from scrobster import accounts, app, colours, config, db, display

    class Running:
        """Stands in for a room's capture task."""
        def done(self):
            return False

    # Apple's covers carry their size in the last part of the URL.
    apple = ("https://is1-ssl.mzstatic.com/image/thumb/Music123/v4/0c/72/a1/"
             "0c72a174-9208-6c86-e479-79865376e904/19UMGIM66006.rgb.jpg/400x400cc.jpg")
    assert display.sized_url(apple, 1000).endswith("/19UMGIM66006.rgb.jpg/1000x1000cc.jpg")
    assert display.sized_url(apple, 64).count("400") == 0, "only the size changes"
    assert display.sized_url(apple, None) == apple, "no size, no change"
    assert display.sized_url(apple.replace("400x400cc", "600x600bb"), 300).endswith("/300x300bb.jpg")
    other = "https://images.shazam.com/coverart/t52389869-b1104656584_s400.jpg"
    assert display.sized_url(other, 300) == other, "another host is served as it is"
    assert display.sized_url("http://is1.mzstatic.com.evil/image/thumb/a/400x400cc.jpg",
                             300).endswith("400x400cc.jpg"), "only Apple's own host"

    # The idle picture is a baseline JPEG of the asked size, like the covers, so
    # a client that decodes one format shows both. Black throughout.
    jpeg = display.idle_jpeg(64)
    assert jpeg.startswith(b"\xff\xd8\xff\xe0") and jpeg.endswith(b"\xff\xd9")
    sof = jpeg.index(b"\xff\xc0")
    assert struct.unpack(">BHHB", jpeg[sof + 4:sof + 10]) == (8, 64, 64, 3), "baseline, 64 square"
    assert jpeg.count(b"\xff\xc4") == 4, "the standard Huffman tables"
    for size in (32, 33, 1200):
        sof = display.idle_jpeg(size).index(b"\xff\xc0")
        assert struct.unpack(">HH", display.idle_jpeg(size)[sof + 5:sof + 9]) == (size, size)
    if shutil.which("ffmpeg"):
        for size in (64, 33):
            raw = subprocess.run(["ffmpeg", "-v", "error", "-f", "jpeg_pipe", "-i", "-",
                                  "-f", "rawvideo", "-pix_fmt", "gray", "-"],
                                 input=display.idle_jpeg(size), capture_output=True, check=True)
            assert len(raw.stdout) == size * size and set(raw.stdout) == {0}, (size, raw.stderr)

    # A display sees a match in a room, and that rooms or listening changed.
    assert app.display_event({"kind": "match", "ts": 1, "room_id": 2, "users": [1],
                              "match": {"title": "T"}}) == {"kind": "match", "ts": 1, "room_id": 2}
    assert app.display_event({"kind": "match", "ts": 1, "room_id": None, "users": [1],
                              "match": {}}) is None, "a browser clip is its owner's"
    for kind in ("scrobble", "services", "users", "member", "cleared", "rates"):
        assert app.display_event({"kind": kind, "ts": 1, "user_id": 1, "users": [1]}) is None, kind
    assert app.display_event({"kind": "rooms", "ts": 1}) == {"kind": "rooms", "ts": 1}
    # and that its cover's colours are ready, for the room it may see
    ready = {"kind": "colors", "ts": 1, "room_id": 2, "art_url": "u"}
    assert app.display_event(ready) == {"kind": "colors", "ts": 1, "room_id": 2}
    assert app.display_event(ready, 3) is None

    fetched = []

    async def fake_fetch(url):
        fetched.append(url)
        if "broken" in url or "999x999" in url:
            raise ValueError("cover is text/html, not an image")
        return "image/jpeg", b"JPEG:" + url.encode(), f'"{len(fetched)}"'

    with tempfile.TemporaryDirectory() as d:
        saved = (config.DB_PATH, display._fetch, app.listener.rooms)
        config.DB_PATH = os.path.join(d, "display.db")
        display._fetch = fake_fetch
        display._covers.clear()
        display._failed.clear()
        app.listener.rooms = {}
        try:
            db.init()
            owner = accounts.create_user("owner", "password123", is_admin=True)
            asyncio.run(app.listener.sync_rooms())
            room = app.listener.first_room()

            made = accounts.create_display("Living room tablet")
            token = made["token"]
            assert token.startswith(accounts.DISPLAY_PREFIX)
            with sqlite3.connect(config.DB_PATH) as c:
                row = c.execute("SELECT * FROM display_tokens").fetchone()
            assert token not in row and accounts._token_hash(token) in row, "only the hash is kept"
            assert "token" not in accounts.list_displays()[0], "the list never shows it"

            q = f"token={token}"
            bearer = {"authorization": f"Bearer {token}"}
            status, head, body = _request(app.app, f"/api/rooms/{room.id}/now?{q}")
            assert status == 200, (status, body)
            now = json.loads(body)
            assert now["playing"] is False and now["title"] is None and now["room"] == "Server mic"
            assert head["cache-control"] == "no-store"
            assert _request(app.app, "/api/now", bearer)[0] == 200, "as a header too"
            assert _request(app.app, "/api/rooms/99/now", bearer)[0] == 404
            last_seen = accounts.list_displays()[0]["last_seen"]
            assert last_seen, "the list says when a display last asked"

            # Nothing else, as a header or in the URL.
            for path in ("/api/status", "/api/recent", "/api/rooms", "/api/users", "/api/me",
                         "/api/levels", "/api/services", "/api/recognition", "/api/audio",
                         "/api/displays"):
                assert _request(app.app, path, bearer)[0] == 401, path
                assert _request(app.app, f"{path}?{q}")[0] == 401, path
            assert _request(app.app, f"/api/listen?{q}", method="POST")[0] == 401
            assert _request(app.app, f"/api/rooms/{room.id}/member?{q}", method="PUT")[0] == 401

            # A personal token is not read from a URL, only from the header.
            mine = owner["api_token"]
            assert _request(app.app, f"/api/now?token={mine}")[0] == 401
            assert _request(app.app, "/api/now", {"authorization": f"Bearer {mine}"})[0] == 200
            assert _request(app.app, "/api/now?token=display-guess")[0] == 401
            assert _request(app.app, "/api/now")[0] == 401
            assert _request(app.app, "/display")[0] == 200, "the page itself needs nothing"

            # The event stream takes a display token, and a wrong one is refused.
            status, head, body = _request(app.app, f"/api/events?{q}")
            assert status == 200 and head["content-type"].startswith("text/event-stream")
            assert body.startswith(b"event: hello"), body
            assert _request(app.app, "/api/events?token=display-nope")[0] == 401

            # Nothing plays: the cover answers as `idle` asks.
            cover = f"/api/rooms/{room.id}/cover?{q}"
            assert _request(app.app, cover)[0] == 204, "the default"
            assert _request(app.app, cover + "&idle=404")[0] == 404
            status, head, body = _request(app.app, cover + "&idle=image&size=64")
            assert status == 200 and head["content-type"] == "image/jpeg" and body == jpeg
            assert _request(app.app, cover + "&idle=image&size=64",
                            {"if-none-match": head["etag"]})[0] == 304
            assert _request(app.app, cover + "&idle=maybe")[0] == 400
            assert _request(app.app, cover + "&size=5000")[0] == 400
            assert _request(app.app, f"/api/cover?{q}")[0] == 204, "no room plays"
            assert fetched == [], "nothing to fetch"

            # A song plays. The same song again keeps its start, a new one starts again.
            room.task = Running()
            room.last_level_db = -12
            info = {"track_key": "k1", "title": "Song", "artist": "A", "album": "LP",
                    "art_url": apple, "offset": None}
            asyncio.run(app.listener._on_match(info, "server", [], room))
            started = room.song_since
            room.last_match["ts"] -= 30
            asyncio.run(app.listener._on_match(info, "server", [], room))
            assert room.song_since == started, "the same song goes on"

            now = json.loads(_request(app.app, f"/api/rooms/{room.id}/now?{q}")[2])
            assert now["playing"] and now["title"] == "Song" and now["album"] == "LP"
            assert now["since"] == started and now["art"] == f"/api/rooms/{room.id}/cover"
            assert now["art_key"] == display.art_key(apple)
            assert json.loads(_request(app.app, f"/api/now?{q}")[2])["room_id"] == room.id
            assert now["colors"] is None, "until they are worked out"

            status, head, body = _request(app.app, cover + "&size=600")
            assert status == 200 and head["content-type"] == "image/jpeg", status
            assert fetched == [display.sized_url(apple, 600)], fetched
            assert body == b"JPEG:" + fetched[0].encode()
            assert "no-cache" in head["cache-control"], "kept, but asked again each time"
            again = _request(app.app, cover + "&size=600", {"if-none-match": head["etag"]})
            assert again[0] == 304 and again[2] == b"", "the same song costs a 304"
            assert len(fetched) == 1, "and no second download"
            assert _request(app.app, f"/api/cover?size=600&{q}")[2] == body, "any room"

            # A size Apple refuses falls back to the cover as Shazam gave it.
            status, _, body = _request(app.app, cover + "&size=999")
            assert status == 200 and body == b"JPEG:" + apple.encode(), body
            # A cover that cannot be fetched is the idle answer, and is not
            # asked for again at once.
            room.last_match["art_url"] = "https://example.com/broken.jpg"
            assert _request(app.app, cover + "&idle=image")[1]["content-type"] == "image/jpeg"
            tries = len(fetched)
            assert _request(app.app, cover)[0] == 204
            assert len(fetched) == tries, "a failure waits RETRY_SECONDS"
            room.last_match["art_url"] = apple

            # The cover's colours, once worked out, for a display token too.
            red = {"primary": {"rgb": [200, 30, 30], "hex": "#c81e1e"}, "secondary": None,
                   "brightness": 200, "saturated": True}
            colours._found[apple] = red
            assert json.loads(_request(app.app, f"/api/rooms/{room.id}/now?{q}")[2])[
                "colors"] == red
            assert json.loads(_request(app.app, f"/api/now?{q}")[2])["colors"] == red

            # It stops: silence, a quiet record side, the room stopping, or no
            # match for NOW_PLAYING_STOP_SECONDS.
            room.last_level_db = -99
            stopped = json.loads(_request(app.app, f"/api/rooms/{room.id}/now?{q}")[2])
            assert not stopped["playing"] and stopped["colors"] is None
            assert _request(app.app, cover)[0] == 204
            room.last_level_db = -12
            room.quiet = True
            assert not display.playing(room, time.time())
            room.quiet = False
            room.last_match["ts"] = int(time.time()) - config.NOW_PLAYING_STOP_SECONDS
            assert not display.playing(room, time.time())
            room.known_until = time.monotonic() + 30
            assert display.playing(room, time.time()), "a known song is not asked about"
            room.known_until = 0.0
            room.last_match["ts"] = int(time.time())
            assert display.playing(room, time.time())
            room.task = None
            assert not display.playing(room, time.time()), "stopped listening"
            assert json.loads(_request(app.app, f"/api/now?{q}")[2]) == app._nothing_playing()

            # Revoked, it reads nothing.
            accounts.delete_display(made["id"])
            assert _request(app.app, f"/api/rooms/{room.id}/now?{q}")[0] == 401
            assert _request(app.app, cover)[0] == 401
        finally:
            config.DB_PATH, display._fetch, app.listener.rooms = saved
            display._covers.clear()
            display._failed.clear()
            colours._found.clear()

    # A display needs a name, so the list in Settings says which screen is which.
    try:
        accounts.create_display("  ")
        raise AssertionError("a display needs a name")
    except ValueError:
        pass


def check_browser_clip():
    """A clip from a phone is checked for silence as a room's is, so it does
    not spend the shared Shazam limit. What it hears still matches, and a
    rate limit says how long to wait."""
    from scrobster import accounts, app, config, db, listener as listener_mod

    asked, decoded = [], {}

    async def fake_decode(data):
        return decoded[data]

    async def fake_recognize(wav):
        asked.append(wav)
        if wav == decoded[b"busy"]:
            raise listener_mod.RateLimited("Shazam is limiting requests")
        return {"matches": [{"offset": 30}],
                "track": {"key": "k9", "title": "Song", "subtitle": "Band"}}

    async def no_side_effects(*args, **kwargs):
        return None

    with tempfile.TemporaryDirectory() as d:
        saved = (config.DB_PATH, config.QUIET_DBFS, app.decode_to_wav,
                 app.listener._shazam.recognize, app.listener._on_match,
                 app.listener._blocked_until, app.listener._backoff)
        config.DB_PATH = os.path.join(d, "clip.db")
        config.RECOGNITION_GAP_SECONDS, gap = 0, config.RECOGNITION_GAP_SECONDS
        app.decode_to_wav = fake_decode
        app.listener._shazam.recognize = fake_recognize
        app.listener._on_match = no_side_effects
        try:
            db.init()
            owner = accounts.create_user("phone", "password123", is_admin=True)
            auth = {"authorization": f"Bearer {owner['api_token']}"}
            decoded.update({b"silent": _wav(0), b"faint": _wav(5), b"music": _tone(8000),
                            b"vinyl": _vinyl(), b"busy": _tone(8001)})

            def post(clip):
                status, head, body = _request(app.app, "/api/match", auth, "POST", clip)
                return status, head, json.loads(body)

            assert _request(app.app, "/api/match", method="POST", body=b"music")[0] == 401

            # Silence: answered at once, and Shazam is not asked.
            for clip in (b"silent", b"faint"):
                status, _, j = post(clip)
                assert status == 200, (status, j)
                assert j["match"] is None and j["skipped"] == "silent", j
                assert j["level_db"] < listener_mod.SILENT_DBFS, j
            assert asked == [], "silence costs no request"

            # A record's run-out groove is quiet only once QUIET_DBFS is set.
            config.QUIET_DBFS = None
            status, _, j = post(b"vinyl")
            assert status == 200 and j["skipped"] is None and len(asked) == 1, j
            config.QUIET_DBFS = -50
            status, _, j = post(b"vinyl")
            assert j["skipped"] == "quiet" and j["match"] is None and len(asked) == 1, j
            assert j["floor_db"] < -50, j

            # Music is asked about, and the answer has the shape the page reads.
            status, _, j = post(b"music")
            assert status == 200 and len(asked) == 2, (status, j)
            assert j["match"]["title"] == "Song" and j["skipped"] is None, j
            assert set(j) == {"match", "level_db", "floor_db", "skipped"}, j

            # A rate limit is a 429 that says when to try again.
            status, head, j = post(b"busy")
            assert status == 429, (status, j)
            assert int(head["retry-after"]) >= 1, head
            assert "trying again in" in j["detail"], j
            # ...and while it lasts, a silent clip still answers without asking.
            assert post(b"silent")[2]["skipped"] == "silent"
            assert post(b"music")[0] == 429 and len(asked) == 3, "blocked, not asked"
        finally:
            (config.DB_PATH, config.QUIET_DBFS, app.decode_to_wav,
             app.listener._shazam.recognize, app.listener._on_match,
             app.listener._blocked_until, app.listener._backoff) = saved
            config.RECOGNITION_GAP_SECONDS = gap


def check_display_scope_migration():
    """A schema 4 database keeps its display tokens. They read every room and
    listen nowhere, as before."""
    import sqlite3
    from scrobster import accounts, config, db

    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "v4.db")
        old = sqlite3.connect(path)
        old.execute("""CREATE TABLE display_tokens(
            id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL,
            token_hash TEXT NOT NULL UNIQUE, created_at INTEGER NOT NULL,
            last_seen INTEGER)""")
        token = accounts.DISPLAY_PREFIX + "old-one"
        old.execute("INSERT INTO display_tokens(name, token_hash, created_at) VALUES(?,?,?)",
                    ("Hall", accounts._token_hash(token), 1))
        old.execute("PRAGMA user_version = 4")
        old.commit()
        old.close()
        saved = config.DB_PATH
        config.DB_PATH = path
        try:
            db.init()
            columns = {r[1] for r in sqlite3.connect(path).execute(
                "PRAGMA table_info(display_tokens)")}
            assert {"room_id", "listen"} <= columns, columns
            screen = accounts.display_by_token(token)
            assert screen["name"] == "Hall" and screen["room_id"] is None, screen
            assert screen["listen"] is False
            version = sqlite3.connect(path).execute("PRAGMA user_version").fetchone()[0]
            assert version == db.SCHEMA_VERSION == 5, version
            db.init()  # twice is safe
        finally:
            config.DB_PATH = saved


def check_browser_room():
    """A browser room: a tablet sends its clips with a room token. The token
    posts to its own room only and reads that room only. A clip goes the way
    a server room's capture goes, and the room fails when clips stop."""
    import asyncio
    import time
    from scrobster import accounts, app, config, db, rooms, scrobble
    from scrobster import listener as listener_mod

    class Running:
        def done(self):
            return False

    asked, decoded, scrobbled = [], {}, []

    async def fake_decode(data):
        if data not in decoded:
            raise ValueError("could not decode audio: not audio")
        return decoded[data]

    async def fake_recognize(wav):
        asked.append(wav)
        if wav == decoded[b"busy"]:
            raise listener_mod.RateLimited("Shazam is limiting requests")
        return {"matches": [{"offset": 30 + len(asked) * 15}],
                "track": {"key": "k7", "title": "Song", "subtitle": "Band"}}

    async def fake_scrobble_all(credentials, artist, title, album, ts, extra=None):
        scrobbled.append(credentials["listenbrainz"]["token"])
        return {"listenbrainz": "ok"}

    async def nothing(*args, **kwargs):
        return {}

    with tempfile.TemporaryDirectory() as d:
        saved = (config.DB_PATH, config.QUIET_DBFS, config.SKIP_KNOWN_SECONDS,
                 config.RECOGNITION_GAP_SECONDS, app.decode_to_wav,
                 app.listener._shazam.recognize, app.listener.rooms,
                 app.listener._blocked_until, app.listener._backoff, app.listener.running,
                 scrobble.scrobble_all, scrobble.now_playing_all,
                 listener_mod.enrich.musicbrainz_ids, app.listener._last_plays)
        config.DB_PATH = os.path.join(d, "browser.db")
        config.QUIET_DBFS, config.SKIP_KNOWN_SECONDS, config.RECOGNITION_GAP_SECONDS = None, 60, 0
        app.decode_to_wav = fake_decode
        app.listener._shazam.recognize = fake_recognize
        app.listener.rooms, app.listener._last_plays = {}, {}
        scrobble.scrobble_all, scrobble.now_playing_all = fake_scrobble_all, nothing
        listener_mod.enrich.musicbrainz_ids = nothing
        try:
            db.init()
            owner = accounts.create_user("owner", "password123", is_admin=True)
            guest = accounts.create_user("guest", "password123")
            for user in (owner, guest):
                accounts.set_credential(user["id"], "listenbrainz",
                                        {"token": user["username"], "url": "http://lb"})
            lounge = rooms.add_room(name="Lounge", kind="browser", url="rtsp://x/y",
                                    device="hw:9", backend="alsa")
            assert lounge["url"] is None and lounge["device"] is None, "nothing to open"
            for user in (owner, guest):
                rooms.set_member(lounge["id"], user["id"], True)
            asyncio.run(app.listener.sync_rooms())
            room = app.listener.rooms[lounge["id"]]
            server = app.listener.first_room()
            assert room.kind == "browser" and server.kind == "device"
            assert room.describe() == "clips from a browser in the room"
            decoded.update({b"silent": _wav(0), b"music": _tone(8000), b"busy": _tone(8001)})

            mic = accounts.create_display("Lounge tablet", lounge["id"], listen=True)
            plain = accounts.create_display("Hall screen")
            for bad in ((server.id, True), (None, True), (999, False)):
                try:
                    accounts.create_display("x", *bad)
                    raise AssertionError(f"refused: {bad}")
                except ValueError:
                    pass
            assert [d["listen"] for d in accounts.list_displays()] == [True, False]

            bearer = lambda t: {"authorization": f"Bearer {t}"}
            m, p = bearer(mic["token"]), bearer(plain["token"])
            clip = f"/api/rooms/{room.id}/clip"
            advice = f"/api/rooms/{room.id}/listen"

            def post(body, headers=m, path=clip):
                status, head, out = _request(app.app, path, headers, "POST", body)
                return status, head, json.loads(out)

            # Listening is off: the page is told to wait, and a clip is refused.
            status, _, body = _request(app.app, advice, m)
            assert status == 200 and json.loads(body)["reason"] == "off", body
            assert post(b"music")[0] == 409 and asked == []

            app.listener.running = True
            room.task = server.task = Running()
            room.expect_by = time.monotonic() + 60

            # Scope: the room token posts to its own room only, reads that room
            # only, and nothing else. A plain display token never posts.
            assert post(b"music", p)[0] == 403, "a display token reads, never sends"
            assert _request(app.app, advice, p)[0] == 403
            assert post(b"music", m, f"/api/rooms/{server.id}/clip")[0] == 403
            assert post(b"music", {})[0] == 401
            assert post(b"music", bearer(guest["api_token"]))[0] == 403, "admins only"
            assert _request(app.app, f"/api/rooms/{room.id}/now", m)[0] == 200
            assert _request(app.app, f"/api/rooms/{server.id}/now", m)[0] == 403
            assert _request(app.app, f"/api/rooms/{server.id}/cover", m)[0] == 403
            assert _request(app.app, f"/api/rooms/{server.id}/now", p)[0] == 200
            for path in ("/api/status", "/api/rooms", "/api/displays", "/api/users",
                         "/api/recognition", "/api/levels", "/api/me"):
                assert _request(app.app, path, m)[0] == 401, path
            assert _request(app.app, f"/api/listen?token={mic['token']}", method="POST")[0] == 401
            assert _request(app.app, "/api/match", m, "POST", b"music")[0] == 401
            assert asked == [], "no request was let through"
            # As ?token= too, for a page that cannot set a header.
            assert _request(app.app, f"{advice}?token={mic['token']}")[0] == 200
            # Its events are this room's only.
            ev = {"kind": "match", "ts": 1, "room_id": server.id, "users": [], "match": {}}
            assert app.display_event(ev, room.id) is None
            assert app.display_event({**ev, "room_id": room.id}, room.id)["room_id"] == room.id

            # Silence: no Shazam, and the song ends.
            status, _, j = post(b"silent")
            assert status == 200 and j["skipped"] == "silent" and asked == [], j
            assert j["next"]["record"] is True and room.attempts == 1
            assert room.last_level_db < listener_mod.SILENT_DBFS

            # Music: matched and scrobbled once for each member.
            status, _, j = post(b"music")
            assert status == 200 and j["match"]["title"] == "Song" and len(asked) == 1, j
            assert sorted(scrobbled) == ["guest", "owner"], scrobbled
            assert room.last_match["room"] == "Lounge"
            assert db.recent(guest["id"])[0]["room"] == "Lounge", "history says via Lounge"
            # The same play goes on: matched again, not scrobbled again, and now known.
            status, _, j = post(b"music")
            assert status == 200 and len(asked) == 2 and len(scrobbled) == 2, (j, scrobbled)
            assert j["next"]["reason"] == "known" and j["next"]["wait"] > 0, j
            status, _, body = _request(app.app, advice, m)
            assert json.loads(body)["record"] is False, body
            status, _, j = post(b"music")
            assert j["skipped"] == "known" and len(asked) == 2, "a known song is not asked"
            assert room.skipped == 1
            now = json.loads(_request(app.app, f"/api/rooms/{room.id}/now", m)[2])
            assert now["playing"] and now["title"] == "Song", now

            # A rate limit: 429 with Retry-After, and the page is told to wait.
            room.known_key, room.known_until = None, 0.0
            status, head, j = post(b"busy")
            assert status == 429 and int(head["retry-after"]) >= 1 and len(asked) == 3
            assert "limiting" in room.last_error
            assert json.loads(_request(app.app, advice, m)[2])["reason"] == "busy"
            assert post(b"music")[0] == 429 and len(asked) == 3, "blocked, not asked"
            app.listener._blocked_until, app.listener._backoff = 0.0, 0

            # Undecodable audio is a 415 and counts as a failure.
            assert post(b"garbage")[0] == 415 and room.outage.since is not None
            assert post(b"silent")[0] == 200 and room.outage.since is None

            # Liveness: no clip in time, the room fails, then recovers.
            start = time.monotonic()
            room.expect_by = start + 60
            app.listener.check_browser(room, start + 30)
            assert room.outage.since is None, "not yet"
            app.listener.check_browser(room, start + 61)
            assert room.outage.since is not None and room.last_error == listener_mod.BROWSER_GONE
            tries = room.outage.tries
            app.listener.check_browser(room, start + 70)
            assert room.outage.tries == tries, "one try per missed window"
            from scrobster import mqtt
            snap = {r["id"]: r for r in mqtt.snapshot(app.listener)["rooms"]}
            assert snap[room.id]["problem"] and "stopped sending" in snap[room.id]["error"]
            listed = {r["id"]: r for r in app._rooms_for(owner)}
            assert listed[room.id]["failing_since"] and listed[room.id]["running"]
            assert post(b"silent")[0] == 200
            assert room.outage.since is None and room.last_error is None, "back"
            # A page that waits as told is not counted as gone.
            room.known_until = time.monotonic() + 300
            app.listener.browser_advice(room)
            assert room.expect_by > time.monotonic() + 300
            room.known_until = 0.0

            # A room's MQTT device says what it is.
            assert mqtt.room_config(mqtt.Topics("s", "ha", "i"),
                                    {"id": room.id, "name": "Lounge", "kind": "browser"}
                                    )["device"]["model"] == "Browser in the room"

            # A server room takes no clips. Removing the room revokes its token.
            assert post(b"music", bearer(owner["api_token"]),
                        f"/api/rooms/{server.id}/clip")[0] == 409
            rooms.delete_room(lounge["id"])
            assert accounts.display_by_token(mic["token"]) is None
            assert accounts.display_by_token(plain["token"]) is not None
        finally:
            (config.DB_PATH, config.QUIET_DBFS, config.SKIP_KNOWN_SECONDS,
             config.RECOGNITION_GAP_SECONDS, app.decode_to_wav,
             app.listener._shazam.recognize, app.listener.rooms,
             app.listener._blocked_until, app.listener._backoff, app.listener.running,
             scrobble.scrobble_all, scrobble.now_playing_all,
             listener_mod.enrich.musicbrainz_ids, app.listener._last_plays) = saved


def check_install():
    """The page can be added to a home screen: a manifest with relative URLs,
    an icon that exists, and the microphone script, served without signing in."""
    from scrobster import app

    status, head, body = _request(app.app, "/manifest.webmanifest")
    assert status == 200 and head["content-type"].startswith("application/manifest+json")
    manifest = json.loads(body)
    assert manifest["display"] == "standalone" and manifest["short_name"] == "scrobSter"
    assert manifest["start_url"] == "." and manifest["scope"] == ".", \
        "relative, so it works behind a proxy's path"
    assert manifest["theme_color"] == manifest["background_color"] == "#0f1216", \
        "the page's own background"
    for icon in manifest["icons"]:
        assert not icon["src"].startswith(("/", "http")), icon
        status, head, png = _request(app.app, "/" + icon["src"])
        assert status == 200 and png.startswith(b"\x89PNG"), icon
        assert icon["sizes"] == "x".join(map(str, struct.unpack(">II", png[16:24]))), icon

    status, head, body = _request(app.app, "/mic.js")
    assert status == 200 and head["content-type"].startswith("text/javascript")
    assert b"ScrobsterMic" in body and b"wakeLock" in body
    page = _request(app.app, "/")[2].decode()
    assert 'rel="manifest" href="manifest.webmanifest"' in page
    assert '<script src="mic.js">' in page
    assert 'href="manifest.webmanifest"' in _request(app.app, "/login")[2].decode()


if __name__ == "__main__":
    main()
