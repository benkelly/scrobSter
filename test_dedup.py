"""Self-check for the pure logic. Run: .venv/bin/python test_dedup.py"""
import io
import math
import struct
import wave

from scrobster.listener import (MAX_SEGMENT_SECONDS, parse_track, peak_dbfs,
                                segment_seconds, should_scrobble)


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

    # a silent device must be detectable, not just "no match forever"
    assert peak_dbfs(_wav(0)) == -99.0, "digital silence"
    assert peak_dbfs(_wav(32767)) > -1, "full scale"
    assert -7 < peak_dbfs(_wav(16384)) < -5, "half scale is about -6 dBFS"
    assert peak_dbfs(_wav(0)) < -80 < peak_dbfs(_wav(16384)), "silence threshold separates them"

    # oversized fingerprint windows stop matching, so they must be capped
    assert segment_seconds(10) == 10, "short window passes through"
    assert segment_seconds(12) == 12
    assert segment_seconds(30) == MAX_SEGMENT_SECONDS, "oversized window is capped"
    print("ok")


if __name__ == "__main__":
    main()
