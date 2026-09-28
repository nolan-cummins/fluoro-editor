"""Filter pipeline — per-channel, region-scoped.

The model is three levels, because that is how you actually reason about a
fluorescence frame:

    region  ->  channel  ->  operations

**region** is "ROI" or "whole frame", chosen ONCE at the top, and it gates every
operation. The previous design tagged individual filters `roi_scoped: True`, so
only 2 of 6 respected the ROI while isolate/levels/CLAHE silently applied to the
whole frame — which is why "ROI mode" appeared not to work.

**channel** is R, G, or B. Each has its own operation stack AND its own
contribution `amount` (0..1) plus a blend mode. `amount` is how a channel is
isolated: R=1, G=0, B=0 leaves pure red. This replaces the old
`channel_isolate` gains, which ranged 0..2 and multiplied the source — an
operation, not a mix, so it could not blend a processed channel back over an
untouched one.

**operations** (histogram, levels, clahe, log, curve) run per channel in a
user-sortable order, each independently switchable.

Every operation runs at FULL RESOLUTION. Downscaling for the viewport happens
afterwards, in render.py. Filtering a downscaled preview would change the
result — CLAHE's tile grid and log gain both act on absolute pixel values, and
the whole point of this tool is judging faint specks.
"""
from __future__ import annotations

import math

import cv2
import numpy as np

# BGR channel order -> our named channels.
CHAN_IDX = {"b": 0, "g": 1, "r": 2}
CHANNELS = ("r", "g", "b")

VERSION = 2
BLENDS = ["normal", "difference"]

# ── the operation set ────────────────────────────────────────
# The frontend builds its controls from this, so adding a parameter needs no UI
# change. Every channel gets the same set; `op_schema()` describes one operation.
OPS = [
    # "Histogram" is a CLIP: it keeps the [min,max] window and blacks everything
    # outside it. It used to be labelled "Threshold", which is a different
    # operation — a real threshold BINARISES. Two ops that mean different things
    # must not share a name, so the binarising one is below under `threshold`.
    {"key": "histogram", "label": "Histogram", "params": [
        {"key": "min", "label": "min", "type": "int", "min": 0, "max": 255,
         "step": 1, "default": 0},
        {"key": "max", "label": "max", "type": "int", "min": 0, "max": 255,
         "step": 1, "default": 255},
    ]},
    # A real threshold: one bit per pixel. Everything at or above `level` becomes
    # 255, everything below becomes 0. This is the op you want for "show me the
    # droplets and nothing else" — the histogram clip leaves survivors greyscale,
    # which for a dim fluorescent sample still buries them in background.
    {"key": "threshold", "label": "Threshold", "params": [
        {"key": "level", "label": "level", "type": "int", "min": 0, "max": 255,
         "step": 1, "default": 128},
        # A choice rather than a new "bool" param type: off/on is exactly a
        # two-option select, and adding a third param type to the schema means
        # teaching the frontend a control it does not have yet.
        {"key": "invert", "label": "invert", "type": "choice",
         "options": ["off", "on"], "default": "off"},
    ]},
    {"key": "levels", "label": "Gamma / gain", "params": [
        {"key": "black", "label": "black", "type": "int", "min": 0, "max": 255,
         "step": 1, "default": 0},
        {"key": "white", "label": "white", "type": "int", "min": 0, "max": 255,
         "step": 1, "default": 255},
        {"key": "gamma", "label": "gamma", "type": "float", "min": 0.05,
         "max": 8.0, "step": 0.01, "default": 1.0},
        {"key": "gain", "label": "gain", "type": "float", "min": 0.0, "max": 8.0,
         "step": 0.01, "default": 1.0},
    ]},
    {"key": "clahe", "label": "CLAHE", "params": [
        {"key": "clip_limit", "label": "clip", "type": "float", "min": 0.5,
         "max": 40.0, "step": 0.5, "default": 2.0},
        {"key": "tile_grid", "label": "tiles", "type": "int", "min": 2,
         "max": 32, "step": 1, "default": 8},
    ]},
    {"key": "log", "label": "Log", "params": [
        {"key": "normalize", "label": "norm", "type": "choice",
         "options": ["fixed", "content"], "default": "fixed"},
        {"key": "ref", "label": "ref", "type": "int", "min": 1, "max": 255,
         "step": 1, "default": 255},
        {"key": "gain", "label": "gain", "type": "float", "min": 0.1,
         "max": 512.0, "step": 0.5, "default": 32.0},
    ]},
    {"key": "curve", "label": "Curve",
     # A list, not a scalar: handled by _coerce_points rather than the
     # scalar path. Declared explicitly so the frontend knows it is editable.
     "params": [{"key": "points", "label": "points", "type": "points"}]},
]
OP_KEYS = [o["key"] for o in OPS]
OP_BY_KEY = {o["key"]: o for o in OPS}


def op_schema() -> list[dict]:
    """Operation schema, for the frontend."""
    return [dict(o) for o in OPS]


def roi_box(shape, roi) -> tuple[int, int, int, int] | None:
    """Normalised ROI -> integer pixel box, clipped to the frame."""
    if not roi:
        return None
    h, w = shape[:2]
    x1 = max(0, min(w - 1, int(round(roi[0] * w))))
    y1 = max(0, min(h - 1, int(round(roi[1] * h))))
    x2 = max(x1 + 1, min(w, int(round(roi[2] * w))))
    y2 = max(y1 + 1, min(h, int(round(roi[3] * h))))
    return (x1, y1, x2, y2)


# ── settings ─────────────────────────────────────────────────
def default_channel() -> dict:
    ch = {"amount": 1.0, "blend": "normal", "ops": {}}
    for o in OPS:
        p = {"enabled": False}
        for prm in o["params"]:
            p[prm["key"]] = ([[0, 0], [255, 255]] if prm["type"] == "points"
                             else prm["default"])
        ch["ops"][o["key"]] = p
    return ch


def defaults() -> dict:
    return {
        "version": VERSION,
        "region": "frame",          # "frame" | "roi"
        "roi": None,                # [x1,y1,x2,y2], normalised 0..1
        "op_order": list(OP_KEYS),
        "channels": {c: default_channel() for c in CHANNELS},
    }


def _coerce(s: dict) -> dict:
    """Fill in anything missing, drop anything unrecognised.

    Deep-merges a (possibly older or partial) settings dict onto the defaults so
    a config written before a parameter existed still loads. The deep merge is
    load-bearing: a shallow `update()` would wipe a channel's whole operation
    stack when only `amount` was supplied — silent data loss that is invisible
    until a render looks subtly wrong.
    """
    d = defaults()
    if not isinstance(s, dict):
        return d
    out = dict(d)
    if s.get("region") in ("frame", "roi"):
        out["region"] = s["region"]
    roi = s.get("roi")
    if (isinstance(roi, (list, tuple)) and len(roi) == 4
            and all(isinstance(v, (int, float)) for v in roi)):
        x1, y1, x2, y2 = (float(v) for v in roi)
        if 0.0 <= x1 < x2 <= 1.0 and 0.0 <= y1 < y2 <= 1.0:
            out["roi"] = [x1, y1, x2, y2]
    order = [k for k in (s.get("op_order") or []) if k in OP_BY_KEY]
    seen = set(order)
    order += [k for k in OP_KEYS if k not in seen]      # append ops added later
    out["op_order"] = order
    chans = s.get("channels") or {}
    for c in CHANNELS:
        dc = default_channel()
        sc = chans.get(c)
        if isinstance(sc, dict):
            amt = sc.get("amount")
            if isinstance(amt, (int, float)) and not isinstance(amt, bool):
                dc["amount"] = max(0.0, min(1.0, float(amt)))
            if sc.get("blend") in BLENDS:
                dc["blend"] = sc["blend"]
            for ok, sp in (sc.get("ops") or {}).items():
                if ok not in dc["ops"] or not isinstance(sp, dict):
                    continue
                dc["ops"][ok]["enabled"] = bool(sp.get("enabled"))
                for prm in OP_BY_KEY[ok]["params"]:
                    _coerce_param(dc["ops"][ok], prm, sp.get(prm["key"]))
        out["channels"][c] = dc
    return out


def _coerce_param(dest: dict, prm: dict, v) -> None:
    """Clamp one parameter into range, by declared type. Silently ignoring an
    out-of-range value would make a saved config load as something the user
    never chose."""
    if v is None:
        return
    if prm["type"] == "points":
        pts = _coerce_points(v)
        if pts:
            dest[prm["key"]] = pts
    elif prm["type"] == "choice":
        if v in prm["options"]:
            dest[prm["key"]] = v
    elif prm["type"] == "int":
        try:
            dest[prm["key"]] = int(max(prm["min"], min(prm["max"], int(v))))
        except (TypeError, ValueError):
            pass
    else:
        try:
            f = float(v)
        except (TypeError, ValueError):
            return
        dest[prm["key"]] = max(prm["min"], min(prm["max"], f))


def _coerce_points(v) -> list | None:
    """Validate curve control points. Returns None if the input is unusable, so
    the caller keeps its default rather than storing something the LUT builder
    would choke on."""
    if not isinstance(v, (list, tuple)) or len(v) < 2:
        return None
    pts: list[list[int]] = []
    for pt in v:
        if not isinstance(pt, (list, tuple)) or len(pt) != 2:
            return None
        try:
            x = float(pt[0])
            y = float(pt[1])
        except (TypeError, ValueError):
            return None
        pts.append([int(max(0, min(255, round(x)))), int(max(0, min(255, round(y))))])
    pts.sort(key=lambda p: p[0])
    return pts




# ── LUT helpers ──────────────────────────────────────────────
def _monotone_lut(points) -> np.ndarray:
    """Fritsch-Carlson monotone-cubic Hermite through control points -> 256 LUT.

    Deliberately NOT Catmull-Rom / natural cubic. Those ring: with control
    points that dip, the spline overshoots past them, and here that means
    clipping to 0/255 and destroying the faint specks the log transform just
    worked to reveal. The Fritsch-Carlson limiter constrains tangents so the
    curve never leaves the band set by its two neighbouring control points.

    Note the guarantee is per-segment shape preservation, NOT global
    monotonicity — a curve whose control points rise then fall still falls.
    That is the user asking for it, not an artefact.
    """
    pts = sorted(([float(x), float(y)] for x, y in points), key=lambda p: p[0])
    if len(pts) < 2:
        y = pts[0][1] if pts else 0.0
        return np.full(256, y, dtype=np.float32)

    xs = np.array([p[0] for p in pts], dtype=np.float64)
    ys = np.array([p[1] for p in pts], dtype=np.float64)
    n = len(xs)

    d = np.diff(ys) / np.maximum(np.diff(xs), 1e-9)      # secants
    m = np.empty(n, dtype=np.float64)                   # initial tangents
    m[0], m[-1] = d[0], d[-1]
    if n > 2:
        m[1:-1] = (d[:-1] + d[1:]) / 2.0
    for i in range(n - 1):                              # monotonicity limiter
        if d[i] == 0:
            m[i] = m[i + 1] = 0.0
        else:
            a, b = m[i] / d[i], m[i + 1] / d[i]
            s = a * a + b * b
            if s > 9.0:
                t = 3.0 / math.sqrt(s)
                m[i] = t * a * d[i]
                m[i + 1] = t * b * d[i]

    xq = np.arange(256, dtype=np.float64)
    out = np.interp(xq, xs, ys)  # flat tails where x falls outside the control points
    idx = np.clip(np.searchsorted(xs, xq, side="right") - 1, 0, n - 2)
    h = np.maximum(xs[idx + 1] - xs[idx], 1e-9)
    t = np.clip((xq - xs[idx]) / h, 0.0, 1.0)
    t2, t3 = t * t, t * t * t
    herm = ((2 * t3 - 3 * t2 + 1) * ys[idx]
            + (t3 - 2 * t2 + t) * h * m[idx]
            + (-2 * t3 + 3 * t2) * ys[idx + 1]
            + (t3 - t2) * h * m[idx + 1])
    inner = (xq >= xs[0]) & (xq <= xs[-1])
    out[inner] = herm[inner]
    return np.clip(out, 0, 255).astype(np.float32)


# ── operations ───────────────────────────────────────────────
# Each takes and returns a SINGLE-channel uint8 plane. Working per-plane is what
# makes "select a channel, then adjust it" mean something: the old whole-image
# filters looped over channels internally and interleaved their parameters,
# which is how the UI ended up with a 3x-duplicated slider grid.
#
# Every op except CLAHE is a pure per-pixel mapping, so each exposes its 256-entry
# table and the driver FUSES a run of them into one pass. Four fused LUT ops cost
# one 1.7M-pixel gather instead of four — the single biggest interactive win,
# and it cannot change the result because table composition is exact.
def _lut_histogram(p: dict) -> np.ndarray:
    """Window: keep [min,max], reject everything outside it to black.

    This is a band-pass, not a levels clip. It used to set everything above
    `max` to 255, which is the opposite of "show me the ones I want": on a real
    frame that slammed ~16% of the pixels to pure white and left the surviving
    band already saturated, so every operation placed after it had nothing left
    to grade and appeared to do nothing. The window is what makes
    threshold -> gamma/log/CLAHE a usable chain.
    """
    lo = int(np.clip(p.get("min", 0), 0, 255))
    hi = int(np.clip(p.get("max", 255), 0, 255))
    if hi < lo:
        lo, hi = hi, lo
    lut = np.zeros(256, dtype=np.uint8)
    lut[lo:hi + 1] = np.arange(lo, hi + 1, dtype=np.uint8)
    return lut


def _lut_threshold(p: dict) -> np.ndarray:
    """Binarise: >= level -> 255, below -> 0. `invert` swaps which side wins.

    Distinct from `_lut_histogram`, which is a CLIP: that keeps the survivors
    greyscale inside the window, this collapses them to a single bit. The two
    answer different questions — "show me this range" vs "show me only what is
    above this" — and for a low-QE fluorescent sample the second is usually the
    one you actually want, because a greyscale survivor still sits on top of
    background noise.

    Expressed as a 256-entry table like every other pure op, so it fuses into
    the same single gather and costs nothing extra in the chain.
    """
    level = int(np.clip(p.get("level", 128), 0, 255))
    lut = np.zeros(256, dtype=np.uint8)
    if p.get("invert", "off") == "on":
        lut[:level] = 255
    else:
        lut[level:] = 255
    return lut


def _lut_levels(p: dict) -> np.ndarray:
    """black/white/gamma/gain, collapsed into one LUT.

    `gamma` follows the universal `dst = src^gamma` convention, so gamma < 1
    BRIGHTENS and gamma > 1 darkens — matching GIMP, Photoshop, OpenCV and every
    other tool a user has muscle memory for. This used to be `src^(1/gamma)`,
    which inverted the control: dragging gamma down made the image darker, and
    it read as "gamma is broken" rather than as a wrong exponent.
    """
    black = float(np.clip(p.get("black", 0), 0, 254))
    white = float(np.clip(p.get("white", 255), 1, 255))
    if white <= black:
        white = black + 1.0
    gamma = max(1e-3, float(p.get("gamma", 1.0)))
    gain = float(np.clip(p.get("gain", 1.0), 0.0, 64.0))
    x = np.arange(256, dtype=np.float32)
    x = np.clip((x - black) / (white - black), 0.0, 1.0)
    x = np.power(x, gamma)
    return np.clip(x * 255.0 * gain, 0, 255).astype(np.uint8)


def _lut_log(p: dict) -> np.ndarray:
    """log(1 + gain*x/ref), normalised back to 0..255.

    `normalize: "fixed"` divides by a FIXED reference (default 255) rather than
    the plane's own max. Dividing by the current max makes every frame
    self-normalise, so the series flickers as brightness drifts and frames stop
    being comparable to each other — the opposite of what a sweep is for. That
    fixed reference is also what makes this fusable: "content" depends on the
    plane and so cannot be folded into a static table.
    """
    gain = max(1e-3, float(p.get("gain", 32.0)))
    top = float(np.clip(p.get("ref", 255), 1, 255))
    denom = math.log1p(gain) or 1.0
    x = np.arange(256, dtype=np.float32) / np.float32(top)
    return (255.0 * np.log1p(gain * x) / denom).astype(np.uint8)


def _lut_curve(p: dict) -> np.ndarray:
    pts = p.get("points") or [[0, 0], [255, 255]]
    if not isinstance(pts, list) or len(pts) < 2:
        return np.arange(256, dtype=np.uint8)
    # _monotone_lut returns float32; the cast to uint8 is NOT optional. Indexing
    # a float LUT with a uint8 index yields a float PLANE, which then poisons
    # every later op and the blend arithmetic.
    return _monotone_lut(pts).astype(np.uint8)


def op_histogram(ch: np.ndarray, p: dict) -> np.ndarray:
    return _lut_histogram(p)[ch]


def op_threshold(ch: np.ndarray, p: dict) -> np.ndarray:
    return _lut_threshold(p)[ch]


def op_levels(ch: np.ndarray, p: dict) -> np.ndarray:
    return _lut_levels(p)[ch]


def op_clahe(ch: np.ndarray, p: dict) -> np.ndarray:
    """Equalise this channel alone. Per-channel is the right choice for
    single-channel signal, and note it also amplifies noise in channels you have
    dropped `amount` on — the trade you make by using it.

    The one op that is NOT a pure LUT: CLAHE is spatial (it equalises within
    tiles), so it cannot be fused and must run as its own pass.
    """
    clip = float(p.get("clip_limit", 2.0))
    grid = int(max(2, p.get("tile_grid", 8)))
    return cv2.createCLAHE(clipLimit=clip, tileGridSize=(grid, grid)).apply(ch)


def op_log(ch: np.ndarray, p: dict) -> np.ndarray:
    if p.get("normalize", "fixed") == "content":
        top = float(ch.max())
        if top <= 0:
            return ch
        gain = max(1e-3, float(p.get("gain", 32.0)))
        denom = math.log1p(gain) or 1.0
        x = ch.astype(np.float32) / np.float32(top)
        return np.clip(255.0 * np.log1p(gain * x) / denom, 0, 255).astype(np.uint8)
    return _lut_log(p)[ch]


def op_curve(ch: np.ndarray, p: dict) -> np.ndarray:
    return _lut_curve(p)[ch]


OPS_IMPL = {
    "histogram": op_histogram,
    "threshold": op_threshold,
    "levels": op_levels,
    "clahe": op_clahe,
    "log": op_log,
    "curve": op_curve,
}

# Ops whose table can be precomputed without looking at the pixels. `log` is
# listed but qualified: in "content" mode it reads the plane's own max, so
# `op_lut` declines to fuse it and it runs as a normal pass.
_FUSABLE = {"histogram": _lut_histogram, "threshold": _lut_threshold,
            "levels": _lut_levels, "log": _lut_log, "curve": _lut_curve}


def op_lut(key: str, p: dict) -> np.ndarray | None:
    """The 256-entry table for `key`, or None if it cannot be precomputed."""
    fn = _FUSABLE.get(key)
    if fn is None:
        return None
    if key == "log" and p.get("normalize", "fixed") == "content":
        return None
    return fn(p)


def _contribute(src_ch: np.ndarray, out_ch: np.ndarray, amount: float,
                mode: str) -> np.ndarray:
    """Scale one channel's contribution to the composite by `amount` (0..1).

    `out_ch` is the source channel with the op chain already applied (the
    identity when no op is enabled), so this is a pure *contribution* scale:

        amount=1 -> the processed channel
        amount=0 -> black, i.e. the channel is gone
        amount=k -> that channel at k intensity over black

    which is what makes R=1, G=0, B=0 leave pure red.

    "difference" substitutes |source - processed| for the processed value before
    scaling, so you see what the operations actually changed rather than the
    result.

    The earlier version lerped source -> processed by `amount`, which made
    amount=0 mean "leave the source untouched" — the exact inverse of what
    isolating a channel needs, and the reason G/B at 0 still showed green.
    """
    a = float(amount)
    if mode == "difference":
        val = np.abs(src_ch.astype(np.int16) - out_ch.astype(np.int16)).astype(np.float32)
    else:
        val = out_ch.astype(np.float32)
    if a >= 1.0:
        return val.astype(np.uint8)
    return np.clip(val * a, 0, 255).astype(np.uint8)


# ── driver ───────────────────────────────────────────────────
def render(img: np.ndarray, s: dict) -> np.ndarray:
    """Apply the pipeline at FULL resolution.

    Order comes from `op_order`, so the drag-reordered list is honoured, and each
    channel's result is composited back only inside the active region — so
    "region = ROI" confines EVERY operation, not just the ones that happened to
    be tagged for it. That per-filter tag was the bug that made ROI mode look
    broken: isolate/levels/CLAHE were untagged and so ignored the ROI entirely.

    Every channel is composited unconditionally, whether or not it has an op
    enabled: `amount` is the channel's CONTRIBUTION, so a channel with no
    operations but amount=0 must still be blacked out. Skipping it (as this used
    to) left the original green and blue in the frame, which is why dragging the
    G and B amounts to 0 changed nothing at all.
    """
    s = _coerce(s)
    # GPU fast path, taken only when every enabled op is a precomputable table
    # (see app.gpu). It fuses the same tables the NumPy loop below composes, so
    # the two agree bit for bit; anything involving CLAHE or a content-dependent
    # log returns None and falls through to the original code unchanged.
    from . import gpu
    if gpu.enabled():
        done = gpu.render_lut_chain(img, s)
        if done is not None:
            return done
    box = roi_box(img.shape, s["roi"]) if s["region"] == "roi" else None
    if box is None:
        x1, y1, x2, y2 = 0, 0, img.shape[1], img.shape[0]
    else:
        x1, y1, x2, y2 = box

    out = None
    for c in CHANNELS:
        conf = s["channels"][c]
        idx = CHAN_IDX[c]
        active = [k for k in s["op_order"]
                  if conf["ops"].get(k, {}).get("enabled") and k in OPS_IMPL]
        amount = float(conf["amount"])

        src_plane = img[y1:y2, x1:x2, idx]
        plane = src_plane
        # Fuse each maximal run of LUT ops into ONE pass. Composing tables is
        # exact, so the result is bit-identical to running them one at a time —
        # this is purely a speed change. CLAHE (and log in "content" mode) break
        # the run because they are not pure tables.
        i = 0
        while i < len(active):
            k = active[i]
            lut = None
            try:
                lut = op_lut(k, conf["ops"][k])
            except Exception as e:
                print(f"[filter] {c}.{k} table failed: {e}")
            if lut is None:
                try:
                    plane = OPS_IMPL[k](plane, conf["ops"][k])
                except Exception as e:
                    # One misbehaving op must not blank the frame; the user still
                    # needs the rest of the stack to find their way back.
                    print(f"[filter] {c}.{k} failed: {e}")
                i += 1
                continue
            j = i + 1
            while j < len(active):
                nxt = None
                try:
                    nxt = op_lut(active[j], conf["ops"][active[j]])
                except Exception:
                    nxt = None
                if nxt is None:
                    break
                lut = nxt[lut]          # apply in list order
                j += 1
            plane = lut[plane]
            i = j
        plane = _contribute(src_plane, plane, amount, conf["blend"])
        if not np.array_equal(plane, src_plane):
            if out is None:
                out = img.copy()
            out[y1:y2, x1:x2, idx] = plane
    return img if out is None else out


def histogram(img: np.ndarray, roi=None) -> dict:
    """256 bins per channel over the ROI, from the SOURCE image.

    Source, not the filtered result: the threshold handles must stay put while
    the user tunes the operations above them, otherwise the histogram moves
    under the drag.
    """
    from . import gpu
    if gpu.enabled():
        got = gpu.histogram(img, roi)
        if got is not None:
            return got
    box = roi_box(img.shape, roi)
    region = img if box is None else img[box[1]:box[3], box[0]:box[2]]
    if region.size == 0:
        return {c: [0] * 256 for c in CHANNELS}
    return {c: [int(v) for v in cv2.calcHist(
        [region], [CHAN_IDX[c]], None, [256], [0, 256]).flatten()]
        for c in CHANNELS}


def has_work(s: dict) -> bool:
    """True when the settings would change a single pixel — lets the client skip
    the render and show the cheap raw preview.

    A channel contributes whenever its amount is not 1, OR it has an enabled op.
    An earlier version required amount > 0 *and* an enabled op, which meant
    "G and B at 0" reported no work: the exact isolation the user asked for was
    the one configuration that took the fast path and returned the untouched
    frame.
    """
    s = _coerce(s)
    for c in CHANNELS:
        conf = s["channels"][c]
        if abs(float(conf["amount"]) - 1.0) > 1e-9:
            return True
        if any(o.get("enabled") for o in conf["ops"].values()):
            return True
    return False


