"""Optional CUDA acceleration for the filter pipeline.

Why this exists, measured on a 24MP frame (6000x4000):

    op                          CPU        GPU (RTX 3080)
    LUT gather (thr/gam/log)    131.3ms     0.5ms
    histogram, one channel       86.3ms     0.9ms
    3x channel split + scale        -       1.6ms

`histogram` (threshold), `levels` (gamma/gain/black/white), `log` and `curve`
are ALL pure 256-entry table gathers, which is exactly the shape a GPU is good
at and exactly the set of controls the user reported as unresponsive. The
transfer is the whole story: 14ms up, 22ms down for a full 24MP frame, so the
win only exists because the arithmetic is 100-250x cheaper than the copy.

CLAHE is deliberately NOT here. Its per-tile histograms on the GPU measured
403ms for 64 tiles — worse than the CPU — because each tile is a separate kernel
launch. Doing it properly needs a batched 2D histogram into a (tiles, 256)
buffer, which is its own piece of work; `eligible()` sends any settings that
touch it back to the CPU rather than pretending.

Everything here degrades to the NumPy path: if cupy is not installed, no device
is visible, or the settings contain a non-LUT op, the caller gets `None` and
runs the original code. There is no GPU-only behaviour and no partial result.
"""
from __future__ import annotations

import os

# Set FLUORO_GPU=0 to force the CPU path even where a device is visible — for
# comparing the two, or if a driver update makes CUDA unusable.
_WANTED = os.environ.get("FLUORO_GPU", "1").strip().lower() not in (
    "0", "false", "no", "off")

_cp = None
_state: str | None = None      # None = unprobed, "", or a reason string


def _probe():
    """Import cupy once and cache the verdict. Never raises."""
    global _cp, _state
    if _state is not None:
        return
    if not _WANTED:
        _state = "disabled by FLUORO_GPU"
        return
    try:
        import cupy  # noqa: PLC0415
    except Exception as e:                      # ImportError, or a broken CUDA
        _state = f"cupy unavailable ({type(e).__name__})"
        return
    try:
        n = cupy.cuda.runtime.getDeviceCount()
    except Exception as e:
        _state = f"no CUDA device ({type(e).__name__})"
        return
    if n < 1:
        _state = "no CUDA device"
        return
    _cp = cupy
    _state = "ok"


def status() -> str:
    """Human-readable reason the GPU is or is not in use. Shown in /health."""
    _probe()
    return _state or "unprobed"


def enabled() -> bool:
    _probe()
    return _cp is not None


def xp():
    """The array module to use: cupy when available, else numpy.

    The two are API-compatible for everything this file does (`astype`, `clip`,
    `abs`, fancy-index gather), so the pipeline below is written once.
    """
    _probe()
    return _cp if _cp is not None else _numpy_fallback()


def _numpy_fallback():
    import numpy
    return numpy


def sync():
    """Block until the device queue is empty. A no-op on the CPU path."""
    if _cp is not None:
        _cp.cuda.Stream.null.synchronize()


def eligible(s: dict) -> bool:
    """True when EVERY enabled op, on EVERY channel, is a precomputable table.

    This is the gate that keeps the two implementations honest: if any setting
    needs CLAHE (or log in "content" mode, which depends on the image), the whole
    render goes back to NumPy rather than splitting the chain across a PCIe
    boundary twice.
    """
    from . import filters
    f = filters._coerce(s)
    for c in filters.CHANNELS:
        for k, p in f["channels"][c]["ops"].items():
            if not p.get("enabled"):
                continue
            try:
                if filters.op_lut(k, p) is None:
                    return False
            except Exception:
                return False
    return True


def _scale_lut(amount: float):
    """256-entry table for `value * amount`.

    Built on the CPU in float32 and truncated by the same .astype(uint8) the
    NumPy path uses, so the two agree BIT FOR BIT rather than merely closely.
    Returns None for amount >= 1, matching _contribute's early-out.
    """
    import numpy as np
    a = float(amount)
    if a >= 1.0:
        return None
    return np.clip(np.arange(256, dtype=np.float32) * a, 0, 255).astype(np.uint8)


def render_lut_chain(img, s: dict):
    """The whole pipeline on the GPU. Returns the image, or None to defer.

    Returns the SAME object it was given when no channel would change, so the
    caller can use the result unconditionally — None means "I cannot help",
    not "nothing happened".
    """
    from . import filters
    if not enabled():
        return None
    f = filters._coerce(s)
    if not eligible(f):
        return None
    box = filters.roi_box(img.shape, f["roi"]) if f["region"] == "roi" else None
    x1, y1, x2, y2 = box if box else (0, 0, img.shape[1], img.shape[0])
    sub = img[y1:y2, x1:x2]
    if sub.size == 0:
        return None

    # Upload the region ONCE and slice the channels off it. Per-channel uploads
    # would be three transfers of the same bytes.
    dev = _cp.asarray(sub)
    out = None
    for c in filters.CHANNELS:
        conf = f["channels"][c]
        idx = filters.CHAN_IDX[c]
        src = dev[:, :, idx]
        active = [k for k in f["op_order"] if conf["ops"].get(k, {}).get("enabled")]

        plane = src
        if active:
            # eligible() guarantees every one of these composes into a table, so
            # the whole chain fuses into ONE gather instead of one per op. Same
            # composition order as the CPU path, so the table is identical.
            lut = _cp.asarray(filters.op_lut(active[0], conf["ops"][active[0]]))
            for k in active[1:]:
                lut = _cp.asarray(filters.op_lut(k, conf["ops"][k]))[lut]
            plane = lut[plane]

        amount, mode = float(conf["amount"]), conf["blend"]
        if mode == "difference":
            d = _cp.abs(src.astype(_cp.int16) - plane.astype(_cp.int16)).astype(_cp.uint8)
        else:
            d = plane
        sl = _scale_lut(amount)
        if sl is not None:
            d = _cp.asarray(sl)[d]

        # "Did this channel change?" decided from the SETTINGS, not by comparing
        # arrays. A (d == src).all() would force a device sync on every channel
        # to answer a question the parameters already answer exactly.
        if active or abs(amount - 1.0) > 1e-9 or mode == "difference":
            if out is None:
                out = _cp.empty_like(dev)
                out[...] = dev
            out[:, :, idx] = d

    if out is None:
        return img
    host = _cp.asnumpy(out)                       # implicit sync
    if img.size == 0:
        return host
    full = img.copy()
    full[y1:y2, x1:x2] = host
    return full


def histogram(img: np.ndarray, roi=None):
    """256 bins per channel over the ROI, on the GPU. None = defer to NumPy."""
    from . import filters
    if not enabled():
        return None
    box = filters.roi_box(img.shape, roi)
    region = img if box is None else img[box[1]:box[3], box[0]:box[2]]
    if region.size == 0:
        return {c: [0] * 256 for c in filters.CHANNELS}
    dev = _cp.asarray(region)
    # asnumpy -> .tolist() yields plain Python ints, matching the cv2.calcHist
    # path so the JSON the client receives is byte-identical between the two.
    return {c: _cp.asnumpy(_cp.bincount(dev[:, :, filters.CHAN_IDX[c]].ravel(),
                                       minlength=256)).tolist()
            for c in filters.CHANNELS}

