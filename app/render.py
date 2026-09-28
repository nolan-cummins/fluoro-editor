"""Render cache.

Key invariant, and the reason this module exists: **filters run at full
resolution**. The frame is decoded full-res, the whole pipeline is applied, and
only the RESULT is downscaled for the viewport. Downscaling first would change
the output — CLAHE's tile grid and the log gain both act on absolute pixel
values, and on a 6000x4000 frame those are very different numbers than on a
1600px preview.

Cost: one full-res pass per distinct settings value. That is why the key is a
hash of (image, settings, size) — dragging a slider back to a previous value is
free, and repeated sweeps over the same settings cost nothing.
"""
from __future__ import annotations

import hashlib
import json
import os
import threading
import time
from collections import OrderedDict

import cv2
import numpy as np

from . import filters, library

_lock = threading.RLock()
_cache: OrderedDict[str, tuple] = OrderedDict()

# ── foreground priority ───────────────────────────────────────
# The background warmer renders 176 frames back-to-back at ~700ms each. That is
# a continuous CPU load, and a slider tick queued behind it measured 1331ms
# instead of 26ms. Warming is a nicety; the frame the user is dragging is the
# thing they feel, so the warmer stands down whenever a foreground render is in
# flight.
# A COUNTER, not an Event. The previous Event version was broken in a way that
# made it decorative: `render_jpeg` — which the warmer itself calls — raised the
# flag, so the warmer marked its own background work as foreground, and a user
# draft render finishing mid-warm cleared the flag out from under it. There was
# never any actual exclusion; the two renders just overlapped and fought for
# memory bandwidth. Nested and concurrent foreground renders are the normal
# case (three channels, a histogram request, a sweep), so set/clear cannot
# represent the state.
_fg_lock = threading.Lock()
_fg_cond = threading.Condition(_fg_lock)
_fg_count = 0


class foreground:
    """Context manager marking a render as interactive.

    Held for the whole duration of a request, so the warmer can check
    `busy()` between items instead of being interrupted mid-render. Background
    callers pass `interactive=False` so they do not count themselves.
    """

    def __init__(self, interactive: bool = True):
        self._on = interactive

    def __enter__(self):
        global _fg_count
        if self._on:
            with _fg_lock:
                _fg_count += 1
        return self

    def __exit__(self, *exc):
        global _fg_count
        if self._on:
            with _fg_lock:
                _fg_count = max(0, _fg_count - 1)
                if _fg_count == 0:
                    _fg_cond.notify_all()
        return False


def busy() -> bool:
    with _fg_lock:
        return _fg_count > 0


def wait_while_busy(timeout: float = 3.0) -> None:
    """Block (bounded) until no foreground render is in flight.

    Wakes on release rather than polling, so a 20ms render does not cost the
    warmer up to 20ms of blind sleep.
    """
    deadline = time.time() + timeout
    with _fg_lock:
        while _fg_count > 0:
            left = deadline - time.time()
            if left <= 0:
                return
            _fg_cond.wait(left)
MAX_ENTRIES = int(os.environ.get("RENDER_CACHE_ENTRIES", "320"))
# 320 ~= two full 176-image libraries at ~157KB each (~50MB). The default of 48
# was sized before the library grew, and it silently defeats background warming:
# warming 176 frames into a 48-slot LRU evicts the first 128 before you can
# sweep to them, so the warm buys nothing.
DEFAULT_MAX_SIDE = 1600
# Source size for the live-drag render. See render_draft.
DRAFT_MAX_SIDE = 1024


def key_for(image_id: str, settings: dict, max_side: int) -> str:
    payload = json.dumps(settings, sort_keys=True, separators=(",", ":"),
                         default=str)
    return hashlib.sha1(f"{image_id}|{max_side}|{payload}".encode()).hexdigest()


def is_binary(img: np.ndarray, box=None) -> bool:
    """True when the FILTERED REGION is exactly 0 or 255 everywhere.

    Used to pick a lossless path for the Threshold op. The check is on the DATA
    rather than on the settings because a chain can reach a binary result by
    accident, and because `amount` on a second channel re-introduces midtones
    that the op itself never produced.

    `box` is the active ROI (x1, y1, x2, y2) and it is NOT optional in spirit:
    with region="roi" the pixels OUTSIDE the box are deliberately the untouched
    original, so they are continuous-tone by design. Testing the whole frame
    therefore always said "not binary" in ROI mode, which silently demoted the
    binarised interior to the lossy JPEG + INTER_AREA path — and put the greys
    straight back. That is the "Threshold looks like a clip" report: the op was
    correct in memory and the encoder undid it.

    Measured on a real 24MP frame, region=roi, threshold=128:
        is_binary(whole frame) = False   -> image/jpeg,  256 distinct inside the ROI
        is_binary(ROI interior) = True   -> image/png,      2 distinct inside the ROI

    So the question is "is the part the filters touched binary", never "is the
    whole buffer binary".

    Membership, not presence. An earlier version required `min == 0 and
    max == 255` — i.e. that BOTH extremes occur. That is wrong for a perfectly
    binary region that happens to be uniform: raise the level until everything
    is rejected and the ROI is a single value, `[0]`, which is exactly as binary
    as a 50/50 split. The test then said "not binary" and sent a dead-flat black
    region down the lossy JPEG + INTER_AREA path, which averaged the box edge and
    manufactured midtone ringing at the seam (measured: 33 distinct values
    inside a region whose true content had exactly one).

    So: every pixel is 0 or 255. How many of each is not the encoder's business.
    """
    if box is not None:
        x1, y1, x2, y2 = box
        if x1 or y1 or x2 < img.shape[1] or y2 < img.shape[0]:
            img = img[y1:y2, x1:x2]
            if img.size == 0:
                return False
    # One pass, no allocation: anything that is neither 0 nor 255 fails.
    return not bool(((img != 0) & (img != 255)).any())


def active_box(img: np.ndarray, settings: dict):
    """The (x1, y1, x2, y2) the filters actually wrote, or None for the frame.

    Same decision filters.render and gpu.render_lut_chain make, in one place, so
    the encoder cannot disagree with the pipeline about which pixels are
    filtered.
    """
    if settings.get("region") != "roi":
        return None
    from . import filters
    box = filters.roi_box(img.shape, settings.get("roi"))
    return box or None


def _encode(img: np.ndarray, max_side: int, quality: int,
            box=None) -> tuple[bytes, str] | None:
    """Encode a preview, returning (bytes, media_type).

    A binary FILTERED REGION gets NEAREST-neighbour downscaling and PNG. All
    three of those matter, and all three were wrong before:

      * the binary test has to look at the region, not the whole buffer — in
        ROI mode the outside is the untouched original, so a whole-frame test
        always failed and demoted a correctly binarised interior to JPEG.
      * INTER_AREA averages, so downscaling a 0/255 frame to 1600px invents
        86 distinct greys out of 2 — measured on a 4000px-wide binarised frame,
        260946 midtone pixels from a clean binarise.
      * JPEG is lossy, so even an untouched 0/255 buffer comes back with 28
        distinct values and ringing around every edge.

    Together they meant the Threshold op looked like a clip: the op was correct
    in memory and the two steps after it put the greys back. PNG of the same
    binary frame is also ~9x SMALLER than the JPEG (35KB vs 306KB), because
    flat runs of one value compress to almost nothing.

    Non-binary output keeps JPEG + INTER_AREA: for a continuous-tone
    fluorescence frame that is the right trade, and binarised output is the only
    case where exactness matters more than bytes.
    """
    binary = is_binary(img, box)
    h, w = img.shape[:2]
    if max_side and max(h, w) > max_side:
        s = max_side / max(h, w)
        nw, nh = max(1, int(w * s)), max(1, int(h * s))
        if binary and box is not None:
            # Mixed content: binarised inside the box, continuous outside.
            # A single INTER_AREA over the whole frame averages across the box
            # edge and drags the interior to midtones; a single INTER_NEAREST
            # aliases the outside, which is the readable half. So resize the two
            # parts with the interpolation each one needs and paste them back.
            # The box is scaled by the same factor as the frame, so the seam
            # lands where the box is.
            x1, y1, x2, y2 = box
            out = cv2.resize(img, (nw, nh), interpolation=cv2.INTER_AREA)
            inside = cv2.resize(img[y1:y2, x1:x2],
                                (max(1, int((x2 - x1) * s)),
                                 max(1, int((y2 - y1) * s))),
                                interpolation=cv2.INTER_NEAREST)
            nx1, ny1 = int(x1 * s), int(y1 * s)
            nx2, ny2 = min(nw, nx1 + inside.shape[1]), min(nh, ny1 + inside.shape[0])
            out[ny1:ny2, nx1:nx2] = inside[:ny2 - ny1, :nx2 - nx1]
            img = out
        else:
            # NEAREST for binary: area-averaging a two-valued image is averaging
            # with itself and produces the greys we are trying to remove.
            interp = cv2.INTER_NEAREST if binary else cv2.INTER_AREA
            img = cv2.resize(img, (nw, nh), interpolation=interp)
    if binary:
        # PNG is lossless, so the 0/255 the filter produced is the 0/255 the
        # browser gets. NEAREST above guarantees the resize introduced nothing
        # inside the region.
        ok, buf = cv2.imencode(".png", img)
        return (buf.tobytes(), "image/png") if ok else None
    ok, buf = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, quality])
    return (buf.tobytes(), "image/jpeg") if ok else None


def render_jpeg(image_id: str, settings: dict, max_side: int = DEFAULT_MAX_SIDE,
                quality: int = 90, cache_draft: bool = True,
                interactive: bool = True) -> tuple[bytes, str] | None:
    """Processed preview, cached by (image, settings, size).

    Returns (bytes, media_type). NOT always JPEG: a binarised result (Threshold)
    is lossless PNG, because JPEG cannot represent a 0/255 frame without
    inventing greys. See _encode.

    `interactive=False` keeps a background call (the warmer) out of the
    foreground count, so it defers to real user requests instead of deferring to
    itself.
    """
    k = key_for(image_id, settings, max_side)
    with _lock:
        if k in _cache:
            _cache.move_to_end(k)
            return _cache[k][1], _cache[k][2]

    src = library.by_id(image_id)          # full-res, EXIF-corrected
    if src is None:
        return None
    if cache_draft:
        library.cache_preview_array(image_id, src, DRAFT_MAX_SIDE)
    with foreground(interactive):
        out = filters.render(src, settings) if filters.has_work(settings) else src
        enc = _encode(out, max_side, quality, active_box(out, settings))
    if enc is None:
        return None
    data, media = enc
    with _lock:
        if len(_cache) >= MAX_ENTRIES:
            _cache.popitem(last=False)
        # (image_id, bytes) so invalidate() can find this entry's owner. The
        # media type is cached alongside: a binarised frame is served as PNG and
        # a continuous one as JPEG, and the response header has to agree with
        # the bytes or the browser sniffs and mislabels.
        _cache[k] = (image_id, data, media)
    return data, media


def render_draft(image_id: str, settings: dict, max_side: int = DRAFT_MAX_SIDE,
                 quality: int = 82) -> tuple[bytes, str] | None:
    """Fast, approximate preview for LIVE slider feedback.

    Same pipeline, downscaled source: one decode, cached, then just the filter
    pass. A full-res render of a 24MP frame is ~700ms, which is why every slider
    tick used to feel like a stall. This is ~10-20ms.

    NOT pixel-identical to render_jpeg — CLAHE's tile grid and the log gain act
    on absolute values, so filtering a 1024px copy is a slightly different
    computation. That is the deliberate trade: the draft is what you see while
    dragging, and the full-resolution render replaces it as soon as the drag
    settles (the client requests both). Tune on the draft, judge on the full one.
    """
    src = library.preview_array(image_id, max_side)
    if src is None:
        return None
    with foreground():
        out = filters.render(src, settings) if filters.has_work(settings) else src
        # Same binary branch as the full render, so a binarised frame looks
        # identical while dragging and once the drag settles. Encoding the draft
        # as JPEG regardless would put the greys back for the entire drag and
        # then snap to black/white on mouseup — the worst version of the bug.
        # The box matters for the same reason it does in the full render: the
        # draft is already at DRAFT_MAX_SIDE, so there is no resize here, but
        # the binary TEST still has to look at the filtered region.
        if is_binary(out, active_box(out, settings)):
            ok, buf = cv2.imencode(".png", out)
            return (buf.tobytes(), "image/png") if ok else None
        ok, buf = cv2.imencode(".jpg", out, [cv2.IMWRITE_JPEG_QUALITY, quality])
    return (buf.tobytes(), "image/jpeg") if ok else None


def render_array(image_id: str, settings: dict) -> np.ndarray | None:
    """Full-res processed frame. Used by P9 export — never downscaled, and not
    cached here because a 24MP result would blow the LRU budget."""
    src = library.by_id(image_id)
    if src is None:
        return None
    return filters.render(src, settings) if filters.has_work(settings) else src


def invalidate(image_id: str | None = None) -> None:
    """Drop cached renders, for one image or all of them.

    The owner is stored WITH the entry rather than parsed out of the key:
    `key_for` returns a sha1 digest, so a key does not start with — or contain
    — the image id at all. The previous `k.startswith(image_id + "|")` therefore
    never matched anything, making this a silent no-op for every caller.
    """
    with _lock:
        if image_id is None:
            _cache.clear()
        else:
            for k in [k for k, v in _cache.items() if v[0] == image_id]:
                _cache.pop(k, None)


def stats() -> dict:
    with _lock:
        return {"entries": len(_cache), "max": MAX_ENTRIES,
                "bytes": sum(len(v[1]) for v in _cache.values())}
