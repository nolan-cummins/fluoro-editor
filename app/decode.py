"""Decoder registry — the seam that lets Canon RAW drop in later.

Every decoder normalizes to BGR uint8 HxWx3, so nothing downstream knows or
cares where pixels came from. Adding CR2/CR3 support later is one registry
entry plus ``rawpy`` in requirements.txt — no refactor of library/render/export.

    Decoders.append(Decoder(exts={".cr2", ".cr3"}, fn=decode_rawpy, slow=True))

``slow=True`` routes a file through a background prewarm queue instead of
decoding inline during a request. That queue already exists for full-res
renders, so slow decoders are nearly free to add.
"""
from __future__ import annotations

import io
import os
import threading
from dataclasses import dataclass, field
from typing import Callable

import cv2
import numpy as np
from PIL import Image, ImageOps

# OpenCV's pthreads backend defaults to one worker per core. Decoding a single
# JPEG barely parallelises, so that buys nothing and costs a lot: at 20 threads
# on a busy box, imdecode thrashes hard enough to stall the whole event loop
# (observed: startup scan wedged inside cv2.imdecode for minutes). A small
# fixed pool keeps latency predictable. Override with OPENCV_THREADS if a
# filter-heavy stage (P2+) ever wants more.
cv2.setNumThreads(int(os.environ.get("OPENCV_THREADS", "4")))


@dataclass
class Decoder:
    exts: set[str]
    fn: Callable[[bytes], np.ndarray]
    slow: bool = False
    name: str = "opencv"
    # Records the source bit depth so a 16-bit TIFF and an 8-bit JPEG stay
    # distinguishable in a mixed session.
    bit_depth: int = 8


Decoders: list[Decoder] = [
    Decoder(
        exts={".jpg", ".jpeg", ".png", ".webp", ".tif", ".tiff", ".bmp"},
        fn=lambda b: None,  # replaced below
        slow=False,
        name="opencv",
        bit_depth=8,
    ),
]


def _to_bgr_u8(arr: np.ndarray) -> np.ndarray:
    """Coerce any decoded array to contiguous 3-channel BGR uint8."""
    if arr is None:
        raise ValueError("decode produced no array")
    if arr.dtype == np.uint16:  # keep the extra range in the high bits
        arr = (arr >> 8).astype(np.uint8)
    elif arr.dtype != np.uint8:
        arr = np.clip(arr, 0, 255).astype(np.uint8)
    if arr.ndim == 2:  # grayscale -> BGR
        arr = cv2.cvtColor(arr, cv2.COLOR_GRAY2BGR)
    elif arr.shape[2] == 4:  # RGBA -> drop alpha (composite on black)
        arr = cv2.cvtColor(arr, cv2.COLOR_RGBA2BGR)
    elif arr.shape[2] == 1:
        arr = cv2.cvtColor(arr[:, :, 0], cv2.COLOR_GRAY2BGR)
    elif arr.shape[2] != 3:  # CMYK or exotic
        arr = cv2.cvtColor(arr, cv2.COLOR_BGR2BGR)
    return np.ascontiguousarray(arr)


def decode_opencv(data: bytes) -> np.ndarray:
    """Primary path. IMREAD_UNCHANGED preserves 16-bit sources so we can record
    the real bit depth instead of silently pretending everything is 8-bit."""
    buf = np.frombuffer(data, dtype=np.uint8)
    raw = cv2.imdecode(buf, cv2.IMREAD_UNCHANGED)
    if raw is None:
        raise ValueError("cv2.imdecode returned None (corrupt or unsupported)")
    return _to_bgr_u8(raw)


def decode_reduced(data: bytes, target_long: int) -> np.ndarray:
    """Decode at ~1/2, 1/4 or 1/8 scale instead of full size.

    A JPEG decoder can downscale in the DCT domain while it is already
    entropy-decoding, so 1/8 scale costs a fraction of a full decode rather than
    a full decode plus a resize. That is the difference between a 500ms thumbnail
    and a ~30ms one.

    The scale is snapped to a power of two because that is all libjpeg's
    DCT-domain path supports; the caller resizes the remainder.

    Orientation is deliberately NOT transposed here: `target_long` is about
    pixels, and a portrait shot rotated is still the same image. Callers that
    need EXIF-corrected output (the viewport) use the full path.
    """
    buf = np.frombuffer(data, dtype=np.uint8)
    raw = None
    for flag in (cv2.IMREAD_REDUCED_COLOR_8, cv2.IMREAD_REDUCED_COLOR_4,
                 cv2.IMREAD_REDUCED_COLOR_2, cv2.IMREAD_COLOR):
        raw = cv2.imdecode(buf, flag)
        if raw is None:
            continue
        h, w = raw.shape[:2]
        # Accept this scale only if it is still big enough for the target.
        if max(h, w) >= target_long or flag == cv2.IMREAD_COLOR:
            return _to_bgr_u8(raw)
    raise ValueError("cv2.imdecode returned None (corrupt or unsupported)")


def _probe_depth(data: bytes) -> int:
    """Cheap header peek so a 16-bit TIFF is not mislabelled as 8-bit."""
    try:
        with Image.open(io.BytesIO(data)) as im:
            return 16 if im.mode in ("I;16", "I;16B", "I", "I;16L") else 8
    except Exception:
        return 8


Decoders[0].fn = decode_opencv


def decoder_for(ext: str) -> Decoder | None:
    """First decoder claiming this extension, or None."""
    e = ext.lower()
    for d in Decoders:
        if e in d.exts:
            return d
    return None


def supported_exts() -> set[str]:
    return set().union(*(d.exts for d in Decoders)) if Decoders else set()


def decode_bytes(data: bytes, ext: str) -> tuple[np.ndarray, str, int]:
    """-> (bgr_u8, decoder_name, bit_depth). Raises ValueError on failure so a
    corrupt file can be marked and the loader can continue."""
    d = decoder_for(ext)
    if d is None:
        raise ValueError(f"no decoder for {ext}")
    img = d.fn(data)
    depth = _probe_depth(data) if d.bit_depth == 8 else d.bit_depth
    return img, d.name, depth


def apply_exif_transpose(img: np.ndarray, data: bytes) -> np.ndarray:
    """Rotate per EXIF orientation. Canon portrait shots are stored landscape
    with an orientation tag; without this they render sideways in the canvas."""
    try:
        with Image.open(io.BytesIO(data)) as im:
            fixed = ImageOps.exif_transpose(im)
            if fixed is None:
                return img
            return _to_bgr_u8(cv2.cvtColor(np.array(fixed.convert("RGB")), cv2.COLOR_RGB2BGR))
    except Exception:
        return img


def exif_orientation(data: bytes) -> int:
    """EXIF orientation tag, or 1 if absent/unreadable. Header-only."""
    try:
        with Image.open(io.BytesIO(data)) as im:
            return int(im.getexif().get(274, 1) or 1)
    except Exception:
        return 1


def rotate_to_orientation(img: np.ndarray, orientation: int) -> np.ndarray:
    """Apply an EXIF orientation to an already-decoded (possibly reduced) array.

    `apply_exif_transpose` re-decodes the file through PIL, which is fine for
    the full-res path but would throw away the whole point of a reduced decode.
    This does the same rotation on the pixels we already have, so the reduced
    thumbnail path stays cheap AND stays correctly oriented.
    """
    if orientation == 1:
        return img
    if orientation in (2, 4):
        img = cv2.flip(img, 1) if orientation == 2 else cv2.flip(img, 0)
        if orientation == 4:
            img = cv2.flip(img, 1)
    elif orientation in (5, 7):
        img = cv2.rotate(img, cv2.ROTATE_90_CLOCKWISE if orientation == 5
                         else cv2.ROTATE_90_COUNTERCLOCKWISE)
        if orientation == 7:
            img = cv2.flip(img, 1)
    elif orientation in (3, 6, 8):
        code = {3: cv2.ROTATE_180, 6: cv2.ROTATE_90_CLOCKWISE,
                8: cv2.ROTATE_90_COUNTERCLOCKWISE}[orientation]
        img = cv2.rotate(img, code)
    return img


# ── background prewarm queue ─────────────────────────────────────
# Exists for full-res render caching; slow=True decoders route through it too.
# Bounded: a 600-image burst must not spawn 600 threads.
class Prewarmer:
    def __init__(self, workers: int = 2, maxsize: int = 64):
        self._q: list = []
        self._lock = threading.Lock()
        self._seen: set = set()
        self.maxsize = maxsize
        for _ in range(workers):
            threading.Thread(target=self._loop, daemon=True).start()

    def submit(self, key: str, fn):
        with self._lock:
            if key in self._seen or len(self._q) >= self.maxsize:
                return False
            self._seen.add(key)
            self._q.append((key, fn))
            return True

    def _loop(self):
        while True:
            with self._lock:
                if not self._q:
                    return
                _key, fn = self._q.pop(0)
            try:
                fn()
            except Exception:
                pass  # prewarm is best-effort; a failure just means no cache


prewarmer = Prewarmer()
