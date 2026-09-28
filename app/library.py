"""Image library: discovery, metadata, thumbnails, natural sort, ordering."""
from __future__ import annotations

import hashlib
import json
import os
import re
import threading
from collections import OrderedDict
from pathlib import Path

import cv2
import numpy as np
from PIL import Image

from .decode import (apply_exif_transpose, decode_bytes, decode_reduced,
                     decoder_for, exif_orientation, rotate_to_orientation,
                     supported_exts)

DATA_DIR = Path(os.environ.get("DATA_DIR", "/data"))
UPLOAD_DIR = DATA_DIR / "uploads"
STATE_FILE = DATA_DIR / "library.json"

_lock = threading.RLock()
_items: dict[str, dict] = {}      # id -> item
_order: list[str] = []            # manual order of ids
_thumb_cache: OrderedDict[str, bytes] = OrderedDict()
_preview_cache: OrderedDict[tuple, bytes] = OrderedDict()
_array_cache: OrderedDict[tuple, np.ndarray] = OrderedDict()
MAX_THUMBS = 400
MAX_PREVIEWS = 64
# A decoded 1024px frame is ~2MB. At 64 this holds ~128MB, which covers the
# whole sweep window (12 ahead + 3 behind) with room to spare, so stepping
# through the library never misses and a neighbour can no longer evict the draft
# source of the frame being edited. It was 16, which is less than the prefetch
# window itself.
MAX_ARRAYS = 64
# Decoded FULL-resolution frames. A 24MP BGR frame is 72MB, so this is a hard
# 144MB ceiling at 2 entries. Sized to the access pattern, not the library: the
# histogram and a full-res render both want the SAME frame, and back to back
# that is the only pair worth holding.
MAX_FULL = 2
_full_cache: OrderedDict[str, np.ndarray] = OrderedDict()


def natural_key(name: str) -> tuple:
    """Natural sort: IMG_2 before IMG_10.

    Plain lexicographic ordering puts IMG_10 before IMG_2, which scrambles a
    time series. Splitting digit runs out of the name fixes it.
    """
    parts = re.split(r"(\d+)", name.lower())
    return tuple(int(p) if p.isdigit() else p for p in parts)


def _item_id(path: Path) -> str:
    """Stable id from the absolute path. Never use the filename as a key —
    experiment folders collide on IMG_1234.jpg across sessions."""
    return hashlib.sha1(str(path).encode()).hexdigest()[:16]


def _probe(path: Path) -> dict:
    """Dimensions + format info WITHOUT decoding pixels.

    PIL's Image.open is lazy: it parses the header and stops. That matters —
    this runs over every file on every startup, and fully decoding a folder of
    24MP Canon frames would take minutes and stall the server. Orientation is
    read from the EXIF tag so portrait shots report swapped w/h, still header-only.
    """
    try:
        with Image.open(path) as im:
            w, h = im.size
            depth = 16 if im.mode in ("I;16", "I;16B", "I;16L", "I") else 8
            try:
                orient = im.getexif().get(274) if im.getexif() else None
            except Exception:
                orient = None
        if orient in (5, 6, 7, 8):  # rotated 90/270: displayed dims are swapped
            w, h = h, w
        dec = decoder_for(path.suffix)
        return {"w": int(w), "h": int(h),
                "decoder": dec.name if dec else None,
                "bit_depth": depth, "error": None}
    except Exception as e:
        # Header unreadable: try a real decode to tell "corrupt" from
        # "PIL can't read this format".
        try:
            data = path.read_bytes()
            img, dec, depth = decode_bytes(data, path.suffix)
            h, w = img.shape[:2]
            return {"w": w, "h": h, "decoder": dec,
                    "bit_depth": depth, "error": None}
        except Exception as e2:
            return {"w": 0, "h": 0, "decoder": None, "bit_depth": 0,
                    "error": str(e2)[:200]}


def scan(images_dir: Path | None = None, recursive: bool = True) -> list[dict]:
    """(Re)discover images under images_dir, preserving any existing manual
    order and any existing ids so per-image overrides stay attached."""
    if images_dir is None:
        images_dir = Path(os.environ.get("IMAGES_DIR", "/images"))
    exts = supported_exts()
    found: dict[str, dict] = {}

    if images_dir.is_dir():
        it = images_dir.rglob("*") if recursive else images_dir.glob("*")
        for p in it:
            if not p.is_file() or p.suffix.lower() not in exts:
                continue
            iid = _item_id(p)
            found[iid] = {
                "id": iid,
                "name": p.name,
                "path": str(p),
                "source": "mount",
                **_probe(p),
            }

    if UPLOAD_DIR.is_dir():
        for p in UPLOAD_DIR.iterdir():
            if not p.is_file() or p.suffix.lower() not in exts:
                continue
            iid = _item_id(p)
            found[iid] = {
                "id": iid,
                "name": p.name,
                "path": str(p),
                "source": "upload",
                **_probe(p),
            }

    with _lock:
        old_order = [i for i in _order if i in found]
        seen = set(old_order)
        new_ids = sorted((i for i in found if i not in seen),
                         key=lambda i: natural_key(found[i]["name"]))
        _items.clear()
        _items.update(found)
        _order[:] = old_order + new_ids
        _thumb_cache.clear()
        _preview_cache.clear()
        _save()

    return as_list()

def add_upload(name: str, data: bytes) -> dict:
    """Store a browser-uploaded file. Names are sanitized; collisions get a
    numeric suffix rather than overwriting an existing frame."""
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    safe = re.sub(r"[^\w.\-]+", "_", Path(name).name) or "upload.jpg"
    dest = UPLOAD_DIR / safe
    n = 1
    while dest.exists() and dest.read_bytes() != data:
        dest = UPLOAD_DIR / f"{Path(safe).stem}_{n}{Path(safe).suffix}"
        n += 1
    dest.write_bytes(data)
    iid = _item_id(dest)
    item = {"id": iid, "name": dest.name, "path": str(dest), "source": "upload",
            **_probe(dest)}
    with _lock:
        _items[iid] = item
        if iid not in _order:
            _order.append(iid)
        _save()
    return item


def set_order(ids: list[str]) -> list[str]:
    """Persist a manual reorder. Multi-select block moves arrive here already
    flattened by the client; unknown ids are ignored, new ones appended in
    natural order so a rescan never silently reorders the visible list."""
    with _lock:
        known = [i for i in ids if i in _items]
        rest = sorted((i for i in _items if i not in set(known)),
                      key=lambda i: natural_key(_items[i]["name"]))
        _order[:] = known + rest
        _save()
        return list(_order)


def get(item_id: str) -> dict | None:
    return _items.get(item_id)


def by_id(item_id: str) -> np.ndarray | None:
    """Decode to BGR u8, EXIF-corrected. Returns None on failure.

    Cached, 2 entries. This was the most-repeated expensive line in the app:
    /api/histogram decoded the entire 24MP frame on every single call (measured
    462ms round trip), and the full-res render then decoded the same file again
    a moment later. Two entries is enough because the access pattern is
    "histogram then render, for the same frame".

    The array is SHARED, so callers must treat it as read-only. filters.render
    copies before it writes and never mutates its input, and the exporters only
    read it.
    """
    with _lock:
        if item_id in _full_cache:
            _full_cache.move_to_end(item_id)
            return _full_cache[item_id]
    it = _items.get(item_id)
    if not it:
        return None
    try:
        data = Path(it["path"]).read_bytes()
        img, _dec, _depth = decode_bytes(data, Path(it["path"]).suffix)
        img = apply_exif_transpose(img, data)
    except Exception:
        return None
    with _lock:
        if len(_full_cache) >= MAX_FULL:
            _full_cache.popitem(last=False)
        _full_cache[item_id] = img
    return img


def thumb(item_id: str, size: int = 160) -> bytes | None:
    """JPEG thumbnail bytes, LRU-capped.

    Decoded straight to ~thumbnail size via the decoder's DCT-domain scaling, not
    by decoding 24MP and then shrinking it. That was ~500ms per thumbnail, and
    the page requests one per library row — so loading a 176-image library put
    ~90 seconds of decode in front of the first interactive frame, and a slider
    drag queued behind all of it.
    """
    with _lock:
        if item_id in _thumb_cache:
            return _thumb_cache[item_id]
    it = _items.get(item_id)
    if not it:
        return None
    try:
        data = Path(it["path"]).read_bytes()
        img = decode_reduced(data, size)
        img = rotate_to_orientation(img, exif_orientation(data))
    except Exception:
        return None
    h, w = img.shape[:2]
    scale = min(size / w, size / h, 1.0)
    if scale < 1.0:
        img = cv2.resize(img, (max(1, int(w * scale)), max(1, int(h * scale))),
                         interpolation=cv2.INTER_AREA)
    ok, buf = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 82])
    if not ok:
        return None
    data = buf.tobytes()
    with _lock:
        if len(_thumb_cache) >= MAX_THUMBS:
            _thumb_cache.popitem(last=False)
        _thumb_cache[item_id] = data
    return data


def preview(item_id: str, max_side: int = 1600, quality: int = 88) -> bytes | None:
    """Encoded, downscaled JPEG for the viewport — cached.

    This is the hot path: dragging the sweep slider requests a new preview per
    step, and without a cache each one costs a full JPEG decode + EXIF
    transpose + re-encode. On a 24MP Canon frame that is seconds per step, which
    is exactly the lag this cache removes.

    Downscaling before encoding matters as much as the cache: a 6000x4000 frame
    is ~72MB decoded and ~10MB encoded, and the viewport can never use more
    than a couple thousand pixels.
    """
    key = (item_id, max_side)
    with _lock:
        if key in _preview_cache:
            _preview_cache.move_to_end(key)
            return _preview_cache[key]

    img = by_id(item_id)
    if img is None:
        return None
    h, w = img.shape[:2]
    if max_side and max(h, w) > max_side:
        s = max_side / max(h, w)
        img = cv2.resize(img, (max(1, int(w * s)), max(1, int(h * s))),
                         interpolation=cv2.INTER_AREA)
    ok, buf = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, quality])
    if not ok:
        return None
    data = buf.tobytes()
    with _lock:
        if len(_preview_cache) >= MAX_PREVIEWS:
            _preview_cache.popitem(last=False)
        _preview_cache[key] = data
    return data


def cache_preview_array(item_id: str, img: np.ndarray,
                        max_side: int = 1024) -> np.ndarray | None:
    """Populate the preview_array cache from an ALREADY-decoded full-res frame.

    The full-res render has the pixels in hand anyway, so downscaling them costs
    ~25ms and saves the next draft render a redundant ~540ms JPEG decode. The
    background warmer sweeps the whole library through render_jpeg, so after one
    pass every frame the user might drag to is already draft-ready.
    """
    if img is None:
        return None
    h, w = img.shape[:2]
    key = (item_id, max_side)
    with _lock:
        if key in _array_cache:
            _array_cache.move_to_end(key)
            return _array_cache[key]
    if max_side and max(h, w) > max_side:
        s = max_side / max(h, w)
        small = cv2.resize(img, (max(1, int(w * s)), max(1, int(h * s))),
                           interpolation=cv2.INTER_AREA)
    else:
        small = img
    small = np.ascontiguousarray(small)
    with _lock:
        if len(_array_cache) >= MAX_ARRAYS:
            _array_cache.popitem(last=False)
        _array_cache[key] = small
    return small


def preview_array(item_id: str, max_side: int = 1024) -> np.ndarray | None:
    """Downscaled DECODED frame, cached. The interactive render's source.

    `preview()` returns encoded JPEG bytes, which is useless as a filter input:
    the pipeline needs pixels, and re-decoding the JPEG on every slider tick
    costs more than the filtering does. This holds the array itself, so after
    the first call a settings change costs only the filter pass.

    1024px rather than the viewport's 1600px: this exists to make dragging feel
    instant, and 1024 is already wider than the image pane at typical window
    sizes. The full-resolution render is still what the sweep, zoom and export
    use — see render.render_jpeg vs render.render_draft.
    """
    key = (item_id, max_side)
    with _lock:
        if key in _array_cache:
            _array_cache.move_to_end(key)
            return _array_cache[key]
    img = by_id(item_id)
    if img is None:
        return None
    h, w = img.shape[:2]
    if max_side and max(h, w) > max_side:
        s = max_side / max(h, w)
        img = cv2.resize(img, (max(1, int(w * s)), max(1, int(h * s))),
                         interpolation=cv2.INTER_AREA)
    # Contiguous: every op is a fancy-index gather, which is markedly slower on
    # a strided view of the full-res buffer.
    img = np.ascontiguousarray(img)
    with _lock:
        if len(_array_cache) >= MAX_ARRAYS:
            _array_cache.popitem(last=False)
        _array_cache[key] = img
    return img


def remove(item_id: str) -> bool:
    """Drop one image from the list. Non-destructive, same contract as clear():
    the file stays on disk, so a rescan brings it back. Removing an accidental
    upload and then hitting Rescan should not silently lose the file."""
    with _lock:
        if item_id not in _items:
            return False
        _items.pop(item_id, None)
        _order[:] = [i for i in _order if i != item_id]
        _thumb_cache.pop(item_id, None)
        _full_cache.pop(item_id, None)
        for k in [k for k in _preview_cache if k[0] == item_id]:
            _preview_cache.pop(k, None)
        for k in [k for k in _array_cache if k[0] == item_id]:
            _array_cache.pop(k, None)
        _save()
        return True


def clear() -> int:
    """Empty the library. Deliberately NON-DESTRUCTIVE: files on disk are never
    touched, because the images dir is a mount of the user's own research data.
    Only the in-memory list, the manual order and the thumbnails go away."""
    with _lock:
        n = len(_items)
        _items.clear()
        _order.clear()
        _thumb_cache.clear()
        _preview_cache.clear()
        _array_cache.clear()
        _full_cache.clear()
        _save()
        return n


def as_list() -> list[dict]:
    with _lock:
        return [_items[i] for i in _order if i in _items]


# ── persistence ─────────────────────────────────────────────────
def _save():
    """Atomic write: tmp + rename. A crash mid-write must not leave a truncated
    state file that loses the whole session's work."""
    try:
        STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = STATE_FILE.with_suffix(".json.tmp")
        tmp.write_text(json.dumps({"order": _order, "items": _items}, indent=1))
        tmp.replace(STATE_FILE)
    except Exception:
        pass


def load() -> None:
    """Restore a previous session's order + metadata. Items are re-probed on
    rescan anyway; this just avoids a flash of empty list on startup."""
    try:
        payload = json.loads(STATE_FILE.read_text())
        with _lock:
            _order[:] = [i for i in payload.get("order", []) if isinstance(i, str)]
            _items.update({k: v for k, v in payload.get("items", {}).items()})
    except Exception:
        pass

