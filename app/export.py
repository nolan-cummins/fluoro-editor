"""Export: a single frame, or the whole series as video or a ZIP of images.

The one thing that must be true of every format here is that the PIPELINE ran at
full resolution. The viewport renders downscaled on purpose (it is a 1600px
preview); an export that reused that would hand back a 1600px JPEG of a 24MP
frame and call it a result. So export calls render.render_array, which never
downscales and never enters the LRU — a 24MP array is 72MB and 150 of them
would evict everything else.

Video is piped, not staged. ffmpeg reads rawvideo from stdin, so a 176-frame
export never writes 176 temporary PNGs (4.6GB at this library's size) and never
reads them back for a second pass.

Guardrails, because a job name comes from the browser:
  - names are sanitised to [A-Za-z0-9._-] and joined under EXPORT_DIR, so a job
    cannot write outside it no matter what is posted
  - exactly one job at a time; starting another cancels the first
  - cancel kills the ffmpeg child, not just the loop, or the pipe fills and the
    process wedges
  - the exporter stands down while a foreground render is in flight, the same as
    the warmer, so exporting never makes the slider feel broken
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import threading
import time
import zipfile
from pathlib import Path

import cv2
import numpy as np

from . import library, render, settings

EXPORT_DIR = Path(os.environ.get("EXPORT_DIR", "/data/exports"))
FFMPEG = os.environ.get("FFMPEG_BIN", "ffmpeg")


# yuv420p — the only chroma subsampling every player and every browser supports —
# rejects odd width and height outright. Rounding down in ffmpeg's scale filter
# would be one moving part fewer, but it also resamples, and under the lossless
# preset that would quietly destroy the very thing the user asked for. Crop to
# even in numpy instead: exact, free, and the last column is not data anyone
# looks at.
def _even(a: np.ndarray) -> np.ndarray:
    h, w = a.shape[:2]
    return a[: h - (h % 2), : w - (w % 2)]


# ── name / path safety ─────────────────────────────────────────
_SAFE = re.compile(r"[^A-Za-z0-9._-]+")


def safe_name(name: str, fallback: str = "export") -> str:
    """A single path SEGMENT. Never returns '', '.', '..', or anything with a
    separator — the result is joined to EXPORT_DIR and that is the only thing
    standing between a posted name and the filesystem.

    Stripping separators is what actually prevents an escape. Collapsing dot
    RUNS afterwards is cosmetic but worth it: a name like "sub/../../out"
    becomes "sub-..-..-out", which is already harmless, and leaving the ".."
    visible in a filename invites the next reader to assume it is not.
    """
    name = _SAFE.sub("-", (name or "").strip())
    name = re.sub(r"\.{2,}", ".", name).strip(".-")
    return name[:120] or fallback


def safe_stem(name: str, fallback: str = "export") -> str:
    """A name WITHOUT an extension, for callers that append their own."""
    return safe_name(name, fallback).rsplit(".", 1)[0] or fallback


def _target(stem: str, ext: str) -> Path:
    p = (EXPORT_DIR / f"{stem}{ext}").resolve()
    # Belt and braces: safe_name already removed every separator, but the
    # resolve-and-check is the assertion that it actually worked.
    if p.parent != EXPORT_DIR.resolve():
        raise ValueError("export path escaped the export directory")
    EXPORT_DIR.mkdir(parents=True, exist_ok=True)
    return p


# ── encoders ───────────────────────────────────────────────────
def encode_png(arr: np.ndarray, compression: int = 3) -> bytes:
    # IMWRITE_PNG_COMPRESSION 0 means STORE: the fastest PNG encoder path and a
    # genuinely uncompressed file, which is the point of offering it.
    ok, buf = cv2.imencode(".png", arr,
                           [cv2.IMWRITE_PNG_COMPRESSION, int(compression)])
    if not ok:
        raise RuntimeError("png encode failed")
    return buf.tobytes()


def encode_jpeg(arr: np.ndarray, quality: int = 92, subsampling: int = 0) -> bytes:
    # subsampling 0 = 4:4:4. The default 4:2:0 halves the chroma resolution,
    # which on a binarised fluorescence ROI destroys exactly the speckle
    # structure the whole log/threshold chain exists to reveal.
    ok, buf = cv2.imencode(".jpg", arr, [
        cv2.IMWRITE_JPEG_QUALITY, int(quality),
        cv2.IMWRITE_JPEG_SAMPLING_FACTOR,
        {0: cv2.IMWRITE_JPEG_SAMPLING_FACTOR_444,
         1: cv2.IMWRITE_JPEG_SAMPLING_FACTOR_422,
         2: cv2.IMWRITE_JPEG_SAMPLING_FACTOR_420}.get(int(subsampling), 0),
    ])
    if not ok:
        raise RuntimeError("jpeg encode failed")
    return buf.tobytes()


# Probed, not assumed. OpenCV's TIFF support is whatever libtiff the wheel was
# built against, and this one is not compiled with zlib — IMWRITE_TIFF_COMPRESSION
# 6 (deflate) returns ok=False and prints "Old-style JPEG compression support is
# not configured" rather than raising. Offering a compression the build cannot do
# is a menu item that always fails, so the client menu is built from the probe.
def _tiff_ok(value: int) -> bool:
    ok, _ = cv2.imencode(".tif", np.zeros((4, 4, 3), np.uint8),
                         [cv2.IMWRITE_TIFF_COMPRESSION, value])
    return bool(ok)


TIFF_COMPRESSION = {k: v for k, v in
                    (("none", 1), ("lzw", 5), ("deflate", 6), ("packbits", 32773))
                    if _tiff_ok(v)}


def encode_tiff(arr: np.ndarray, compression: str = "none") -> bytes:
    # Flat [tag, value], like the JPEG params above. OpenCV 5 rejects the nested
    # [(tag, value)] form with a bare "Bad argument" on every codec — it parses
    # the first sequence element as the tag, and a 1-tuple is not a valid int.
    val = TIFF_COMPRESSION.get(compression, TIFF_COMPRESSION["none"])
    ok, buf = cv2.imencode(".tif", arr, [cv2.IMWRITE_TIFF_COMPRESSION, val])
    if not ok:
        raise RuntimeError(f"tiff encode failed ({compression})")
    return buf.tobytes()


def encode(arr: np.ndarray, fmt: str, params: dict) -> tuple[bytes, str]:
    """-> (bytes, extension)"""
    fmt = (fmt or "png").lower()
    if fmt == "png":
        return encode_png(arr, int(params.get("compression", 3))), ".png"
    if fmt in ("jpg", "jpeg"):
        return encode_jpeg(arr, int(params.get("quality", 92)),
                           int(params.get("subsampling", 0))), ".jpg"
    if fmt in ("tif", "tiff"):
        return encode_tiff(arr, params.get("compression", "none")), ".tif"
    raise ValueError(f"unknown format {fmt!r}")


# x264 presets. "lossless" is -qp 0, which is real x264 lossless and still plays
# in a browser; the alternative (ffv1, or PNG-in-MKV) is lossless too but no
# browser will play it, and the point of exporting is to look at the result.
VIDEO_PRESETS = {
    "lossless": ["-c:v", "libx264", "-qp", "0", "-preset", "veryslow",
                 "-pix_fmt", "yuv444p"],
    "high":     ["-c:v", "libx264", "-crf", "14", "-preset", "slow",
                 "-pix_fmt", "yuv420p"],
    "compact":  ["-c:v", "libx264", "-crf", "23", "-preset", "medium",
                 "-pix_fmt", "yuv420p"],
}

class Exporter:
    """One job at a time, mirroring warm.Warmer's shape so the progress polling
    and the cancel semantics are the same to reason about."""

    def __init__(self):
        self._lock = threading.Lock()
        self._job: dict | None = None
        self._seq = 0
        self._proc: subprocess.Popen | None = None
        self._t: threading.Thread | None = None

    # ── public ──────────────────────────────────────────────
    def start(self, spec: dict) -> dict:
        with self._lock:
            # A new job supersedes the old one: set the OLD job's own event, not
            # a shared field. A thread reading a reassigned self._cancel would be
            # reading the NEW job's event — unset — and would run to completion
            # after being cancelled. The same ownership mistake the warmer made
            # with its job counter.
            if self._job and not self._job["finished"]:
                self._job["_cancel"].set()
            self._kill_proc_locked()
            self._seq += 1
            self._job = {
                "id": self._seq, "kind": spec.get("kind"),
                "total": len(spec.get("ids") or []), "done": 0, "failed": 0,
                "current": None, "running": True, "finished": False,
                "cancelled": False, "error": None, "started": time.time(),
                "file": None, "size": None, "eta": None, "spec": spec,
                "_cancel": threading.Event(),
            }
        # A fresh thread per job, so a superseded one cannot race the new job.
        # `args=(job,)` — the trailing comma is not decoration. `args=(job)`
        # unpacks the dict into its KEYS as positional arguments.
        self._t = threading.Thread(target=self._run, args=(self._job,), daemon=True)
        self._t.start()
        return self.progress()

    def cancel(self) -> dict:
        with self._lock:
            if self._job and not self._job["finished"]:
                self._job["_cancel"].set()
                self._job["cancelled"] = True
                self._job["running"] = False
            self._kill_proc_locked()
        return self.progress()

    def progress(self) -> dict:
        with self._lock:
            if not self._job:
                return {"id": 0, "kind": None, "total": 0, "done": 0, "failed": 0,
                        "current": None, "running": False, "finished": True,
                        "cancelled": False, "error": None, "file": None,
                        "size": None, "eta": None, "pct": 100}
            # _cancel is a live Event, not JSON: progress() is polled over HTTP
            # and serialised, and an Event is not serialisable. Drop it along
            # with the spec.
            j = {k: v for k, v in self._job.items()
                 if k not in ("spec", "_cancel")}
            j["pct"] = round(100 * j["done"] / j["total"]) if j["total"] else 100
            if j["eta"] is None and j["done"] >= 2 and j["total"] > j["done"]:
                rate = (time.time() - j["started"]) / j["done"]
                j["eta"] = round(rate * (j["total"] - j["done"]))
            j["elapsed"] = round(time.time() - j["started"])
            return j

    # ── internals ───────────────────────────────────────────
    def _kill_proc_locked(self):
        if self._proc and self._proc.poll() is None:
            try:
                self._proc.kill()
            except Exception:
                pass
        self._proc = None

    def _cancelled(self, job) -> bool:
        """THIS job's event, not self._cancel — see the note in start()."""
        return job["_cancel"].is_set()

    def _frame(self, iid: str, filters_on: bool):
        if not filters_on:
            return library.by_id(iid)
        return render.render_array(iid, settings.effective(iid))

    def _run(self, job: dict):
        spec = job.get("spec") or {}
        try:
            if spec.get("kind") == "series" and spec.get("format") == "video":
                self._video(job, spec)
            elif spec.get("kind") == "series":
                self._zip(job, spec)
            else:
                self._one(job, spec)
        except Exception as e:
            with self._lock:
                if self._job and self._job["id"] == job["id"]:
                    self._job["error"] = f"{type(e).__name__}: {e}"
        finally:
            with self._lock:
                if self._job and self._job["id"] == job["id"]:
                    self._job["running"] = False
                    self._job["finished"] = True
                    self._job["current"] = None
            self._kill_proc_locked()

    def _one(self, job, spec):
        iid = (spec.get("ids") or [None])[0]
        arr = self._frame(iid, spec.get("filters", True))
        if arr is None:
            raise RuntimeError("frame could not be decoded")
        data, ext = encode(arr, spec.get("format", "png"), spec.get("params") or {})
        stem = spec.get("name") or (library.get(iid) or {}).get("name") or "frame"
        path = _target(safe_stem(stem), ext)
        path.write_bytes(data)
        with self._lock:
            if self._job["id"] == job["id"]:
                self._job.update(done=1, total=1, file=path.name, size=len(data))

    def _zip(self, job, spec):
        fmt = spec.get("format", "png")
        ext = ".tif" if fmt == "tiff" else ".png"
        path = _target(safe_stem(spec.get("name") or "series"), ".zip")
        ids = [i for i in spec["ids"] if library.get(i)]
        params = spec.get("params") or {}
        with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
            for iid in ids:
                if self._cancelled(job):
                    break
                render.wait_while_busy()
                name = (library.get(iid) or {}).get("name") or iid
                ok = False
                try:
                    arr = self._frame(iid, spec.get("filters", True))
                    if arr is None:
                        raise RuntimeError("decode failed")
                    data, _ = encode(arr, fmt, params)
                    z.writestr(f"{name}{ext}", data)
                    ok = True
                except Exception:
                    pass
                self._credit(job, ok, name)
        with self._lock:
            if self._job["id"] == job["id"] and path.exists():
                self._job["file"] = path.name
                self._job["size"] = path.stat().st_size

    def _video(self, job, spec):
        ids = [i for i in spec["ids"] if library.get(i)]
        if not ids:
            raise RuntimeError("no readable frames")
        fps = max(1, min(120, int(spec.get("fps", 15))))
        max_side = int(spec.get("max_side", 1920) or 0)
        preset = spec.get("preset", "high")
        if preset not in VIDEO_PRESETS:
            preset = "high"
        path = _target(safe_stem(spec.get("name") or "series"), ".mp4")

        # Probe the first frame to fix the geometry for the whole stream.
        first = self._frame(ids[0], spec.get("filters", True))
        if first is None:
            raise RuntimeError("first frame could not be decoded")
        w0, h0 = first.shape[1], first.shape[0]
        if max_side and max(w0, h0) > max_side:
            s = max_side / max(w0, h0)
            w0, h0 = int(w0 * s), int(h0 * s)
        w0, h0 = w0 - (w0 % 2), h0 - (h0 % 2)

        cmd = [FFMPEG, "-y", "-hide_banner", "-loglevel", "error",
               "-f", "rawvideo", "-pix_fmt", "bgr24",
               "-s", f"{w0}x{h0}", "-r", str(fps), "-i", "pipe:0",
               *VIDEO_PRESETS[preset],
               # +faststart so the result is seekable in a browser without a
               # second pass. A cancelled job has no moov atom to move, but the
               # file is discarded then anyway.
               "-movflags", "+faststart", str(path)]
        proc = subprocess.Popen(cmd, stdin=subprocess.PIPE,
                                stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        with self._lock:
            self._proc = proc
        try:
            for iid in ids:
                if self._cancelled(job):
                    break
                render.wait_while_busy()
                name = (library.get(iid) or {}).get("name")
                arr = self._frame(iid, spec.get("filters", True))
                ok = arr is not None
                if ok:
                    if arr.shape[1] != w0 or arr.shape[0] != h0:
                        # A frame of a different size than the first would
                        # desync the rawvideo stream and corrupt every frame
                        # after it, so resize rather than trust it.
                        arr = cv2.resize(arr, (w0, h0),
                                         interpolation=cv2.INTER_AREA)
                    arr = _even(arr)
                    if not arr.flags["C_CONTIGUOUS"]:
                        arr = np.ascontiguousarray(arr)
                    try:
                        proc.stdin.write(arr.tobytes())
                    except (BrokenPipeError, OSError):
                        ok = False
                self._credit(job, ok, name)
            try:
                proc.stdin.close()
            except Exception:
                pass
            err = proc.stderr.read().decode("utf-8", "replace")[-800:]
            rc = proc.wait()
            if rc != 0 and not self._cancelled(job):
                raise RuntimeError(f"ffmpeg exited {rc}: {err.strip()[:400]}")
        finally:
            with self._lock:
                self._proc = None
        with self._lock:
            if self._job["id"] != job["id"]:
                return
            if self._cancelled(job):
                # A half-written mp4 is not a video; do not offer it for download.
                path.unlink(missing_ok=True)
            elif path.exists():
                self._job["file"] = path.name
                self._job["size"] = path.stat().st_size

    def _credit(self, job, ok, name):
        with self._lock:
            if self._job and self._job["id"] == job["id"] and not self._cancelled(job):
                self._job["done"] += 1
                self._job["current"] = name
                if not ok:
                    self._job["failed"] += 1


exporter = Exporter()


def has_ffmpeg() -> bool:
    return shutil.which(FFMPEG) is not None
