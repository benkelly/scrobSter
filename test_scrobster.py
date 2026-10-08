"""Self-check for the pure logic. Run: .venv/bin/python test_scrobster.py"""
import io
import json
import math
import os
import struct
import tempfile
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
        "images": {"coverart": "u"},
        "sections": [{"metadata": [{"title": "Album", "text": "Al"}]}],
    }})
    assert info == {"track_key": "k1", "title": "T", "artist": "A", "album": "Al",
                    "art_url": "u", "offset": 56.98}
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
            asyncio.run(room._on_match({**info, "title": "Radio"}, "server", []))
            assert room.last_match_for(alice["id"])["title"] == "Radio", "the room is shared"

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


if __name__ == "__main__":
    main()
