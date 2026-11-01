"""The colours of a cover, so a lamp or an LED strip can match the record.

ffmpeg, which scrobSter needs anyway, shrinks the cover to a few hundred
pixels of raw RGB. Those are grouped into a handful of colours (median cut,
then a few rounds of k-means, so the same cover always gives the same answer),
and two are picked:

- `primary`, the most common colour a light can show: not near black, not near
  white, not grey. A cover with none, such as a black and white photo, gets a
  warm white, with `saturated: false`.
- `secondary`, the next such colour that looks different from the first, or
  null.

`brightness` (0-255) follows how light the primary colour is on the cover.

They are worked out once per cover, when a room starts a song, and kept, so
the API and MQTT only look them up. A cover that cannot be read gives null
and never gets in the way of the match.
"""
import asyncio
import collections
import colorsys
import logging
import math
import time

from . import events

log = logging.getLogger("scrobster")

# The cover is fetched this big where its host allows it, and shrunk to SIDE.
FETCH_SIZE = 100
SIDE = 24
FFMPEG_TIMEOUT_SECONDS = 5
CLUSTERS = 8
KMEANS_ROUNDS = 8
# Two clusters closer than this (CIE76 delta E) are one colour, JPEG noise apart.
MERGE_DELTA_E = 12
# What a light can show: HSV value and saturation, 0 to 1.
MIN_VALUE = 0.22  # darker reads as black
MIN_SATURATION = 0.25  # less reads as grey or white
# A colour must cover this share of the cover to count at all.
MIN_SHARE = 0.04
# The secondary colour differs from the primary by this much hue, in degrees,
# or by this distance in a*b* (delta E without the lightness), such as red and
# pink, with both brought to full value first. Lightness alone does not count:
# a lamp sets its brightness apart.
MIN_HUE_DEGREES = 30
MIN_CHROMA_APART = 45
# For a cover with no colour to show: about 3000 K.
WARM_WHITE = (255, 197, 143)
WARM_WHITE_BRIGHTNESS = (40, 200)  # from a dark cover to a light one
CACHE_SIZE = 64
RETRY_SECONDS = 60

_found = collections.OrderedDict()  # art url -> colours, or None if unreadable
_failed = {}  # art url -> monotonic time it could not be read
_working = {}  # art url -> the task working it out
_tasks = set()  # started by prepare_soon(), kept so they are not collected
_rooms = {}  # art url -> the rooms waiting for its colours


# --- the picture --------------------------------------------------------------------

async def decode(image: bytes, side=SIDE, timeout=FFMPEG_TIMEOUT_SECONDS) -> bytes | None:
    """The image as side x side RGB triplets, or None when ffmpeg cannot read it."""
    proc = await asyncio.create_subprocess_exec(
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-i", "pipe:0",
        "-vf", f"scale={side}:{side}:flags=area", "-frames:v", "1",
        "-f", "rawvideo", "-pix_fmt", "rgb24", "pipe:1",
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE)
    try:
        out, err = await asyncio.wait_for(proc.communicate(image), timeout)
    finally:
        if proc.returncode is None:
            proc.kill()
            await proc.wait()
    if proc.returncode != 0 or len(out) != side * side * 3:
        log.debug("cover colours: ffmpeg could not read the cover, so it has none: %s",
                  err.decode(errors="replace").strip()[:200])
        return None
    return out


# --- colour arithmetic --------------------------------------------------------------

def _lab(rgb):
    """sRGB 0-255 to CIE L*a*b* (D65)."""
    def linear(c):
        c /= 255
        return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4
    r, g, b = (linear(c) for c in rgb)
    x = (0.4124 * r + 0.3576 * g + 0.1805 * b) / 0.95047
    y = 0.2126 * r + 0.7152 * g + 0.0722 * b
    z = (0.0193 * r + 0.1192 * g + 0.9505 * b) / 1.08883

    def f(t):
        return t ** (1 / 3) if t > 0.008856 else 7.787 * t + 16 / 116
    fx, fy, fz = f(x), f(y), f(z)
    return 116 * fy - 16, 500 * (fx - fy), 200 * (fy - fz)


def delta_e(a, b) -> float:
    return math.dist(_lab(a), _lab(b))


def _hsv(rgb):
    return colorsys.rgb_to_hsv(*(c / 255 for c in rgb))


def usable(rgb) -> bool:
    """A colour a light can show: neither near black, near white nor grey."""
    _, s, v = _hsv(rgb)
    return v >= MIN_VALUE and s >= MIN_SATURATION


def _hue_apart(a, b) -> float:
    d = abs(_hsv(a)[0] - _hsv(b)[0]) * 360
    return min(d, 360 - d)


def _full(rgb):
    """The colour at full HSV value, as a lamp shows it at any brightness."""
    top = max(rgb)
    return tuple(c * 255 / top for c in rgb) if top else rgb


def distinct(a, b) -> bool:
    """Two colours a lamp would show differently. Both are compared at full
    value, because a*b* shrinks as a colour darkens: a red and a dark red are
    one colour to a lamp."""
    chroma = math.dist(_lab(_full(a))[1:], _lab(_full(b))[1:])
    return _hue_apart(a, b) >= MIN_HUE_DEGREES or chroma >= MIN_CHROMA_APART


# --- grouping -----------------------------------------------------------------------

def _mean(pixels):
    n = len(pixels)
    return tuple(sum(p[i] for p in pixels) / n for i in range(3))


def _median_cut(pixels, k):
    """k starting colours: split the box with the widest channel until there are k."""
    boxes = [pixels]
    while len(boxes) < k:
        def spread(box):
            return max(max(p[i] for p in box) - min(p[i] for p in box) for i in range(3))
        box = max(boxes, key=lambda b: (spread(b) * len(b), len(b)))
        if len(box) < 2 or spread(box) == 0:
            break
        channel = max(range(3), key=lambda i: max(p[i] for p in box) - min(p[i] for p in box))
        box.sort(key=lambda p: p[channel])
        half = len(box) // 2
        boxes.remove(box)
        boxes += [box[:half], box[half:]]
    return [_mean(box) for box in boxes]


def clusters(pixels, k=CLUSTERS):
    """[(rgb, share)], most common first. Deterministic for the same pixels."""
    centres = _median_cut(list(pixels), k)
    groups = []
    for _ in range(KMEANS_ROUNDS):
        groups = [[] for _ in centres]
        for p in pixels:
            nearest = min(range(len(centres)), key=lambda i: math.dist(p, centres[i]))
            groups[nearest].append(p)
        moved = [_mean(g) if g else c for g, c in zip(groups, centres)]
        if moved == centres:
            break
        centres = moved
    found = [[c, len(g)] for c, g in zip(centres, groups) if g]
    # Merge what is one colour, so its share is not split between clusters.
    merged = []
    for centre, count in sorted(found, key=lambda f: -f[1]):
        for other in merged:
            if delta_e(centre, other[0]) < MERGE_DELTA_E:
                total = other[1] + count
                other[0] = tuple((a * other[1] + b * count) / total
                                 for a, b in zip(other[0], centre))
                other[1] = total
                break
        else:
            merged.append([centre, count])
    total = len(pixels)
    return [(tuple(round(c) for c in centre), count / total)
            for centre, count in sorted(merged, key=lambda f: -f[1])]


def _colour(rgb):
    return {"rgb": list(rgb), "hex": "#%02x%02x%02x" % tuple(rgb)}


def pick(raw: bytes) -> dict:
    """{primary, secondary, brightness, saturated} from raw RGB triplets."""
    pixels = [tuple(raw[i:i + 3]) for i in range(0, len(raw) - 2, 3)]
    found = clusters(pixels)
    shown = [rgb for rgb, share in found if share >= MIN_SHARE and usable(rgb)]
    if not shown:
        # Nothing a light could match: a warm white, as light as the cover.
        lightest = max(_hsv(rgb)[2] for rgb, _ in found)
        low, high = WARM_WHITE_BRIGHTNESS
        return {"primary": _colour(WARM_WHITE), "secondary": None,
                "brightness": round(low + (high - low) * lightest), "saturated": False}
    primary = shown[0]
    secondary = next((rgb for rgb in shown[1:] if distinct(rgb, primary)), None)
    return {"primary": _colour(primary),
            "secondary": _colour(secondary) if secondary else None,
            "brightness": round(_hsv(primary)[2] * 255), "saturated": True}


async def of_image(image: bytes) -> dict | None:
    """The colours of an image, or None when it cannot be read."""
    raw = await decode(image)
    if raw is None:
        return None
    # A few thousand distance sums: quick, but not on the event loop.
    return await asyncio.to_thread(pick, raw)


# --- per cover ----------------------------------------------------------------------

def cached(art_url) -> dict | None:
    """The colours worked out for this cover, or None while there are none."""
    return _found.get(art_url) if art_url else None


async def _work_out(art_url):
    from . import display  # display imports the listener, which imports this module
    found = await display.room_cover(art_url, FETCH_SIZE)
    if found is None:
        raise LookupError("the cover could not be fetched")  # asked again later
    return await of_image(found[1])


async def colours_of(art_url) -> dict | None:
    """The colours of the cover at art_url, worked out once and kept. A cover
    that is not a picture ffmpeg reads is kept as None, and one that could not
    be fetched is tried again after RETRY_SECONDS."""
    if art_url in _found:
        _found.move_to_end(art_url)
        return _found[art_url]
    if time.monotonic() - _failed.get(art_url, -RETRY_SECONDS) < RETRY_SECONDS:
        return None
    task = _working.get(art_url)
    if task is None:
        task = _working[art_url] = asyncio.ensure_future(_work_out(art_url))
        task.add_done_callback(lambda _: _working.pop(art_url, None))
    try:
        result = await asyncio.shield(task)
    except Exception as e:
        log.debug("cover colours for %s failed: %s", art_url, e or type(e).__name__)
        if len(_failed) > CACHE_SIZE:
            _failed.clear()
        _failed[art_url] = time.monotonic()
        return None
    _failed.pop(art_url, None)
    _found[art_url] = result
    while len(_found) > CACHE_SIZE:
        _found.popitem(last=False)
    return result


async def prepare(art_url, room_id=None):
    """Work out a cover's colours, then say so, so MQTT and the displays show
    them. Never raises: a match goes on without colours."""
    try:
        found = await colours_of(art_url)
    except Exception:
        log.debug("cover colours for %s failed", art_url, exc_info=True)
        found = None
    waiting = _rooms.pop(art_url, set()) | {room_id}
    if found is not None:
        for room in sorted(waiting, key=str):
            events.emit("colors", room_id=room, art_url=art_url)


def prepare_soon(art_url, room_id=None):
    """prepare() in the background, for a room that starts a song. Nothing
    happens when the colours are known or being worked out."""
    if not art_url or art_url in _found:
        return  # known, or known to be unreadable
    if art_url in _rooms:
        _rooms[art_url].add(room_id)  # another room has it on the way
        return
    if time.monotonic() - _failed.get(art_url, -RETRY_SECONDS) < RETRY_SECONDS:
        return
    _rooms[art_url] = set()
    task = asyncio.ensure_future(prepare(art_url, room_id))
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)
    # Also when it was cancelled before it began, so the cover is not
    # taken to be on the way for good.
    task.add_done_callback(lambda _: _rooms.pop(art_url, None))
