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
                                parse_device_list, parse_proc_asound_pcm, parse_track,
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

    # oversized fingerprint windows stop matching, so they must be capped
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
    check_enrich()
    check_rooms()
    check_rates()
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


if __name__ == "__main__":
    main()
