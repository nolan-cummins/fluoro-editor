"""Export unit checks — name safety, encoder behaviour, job lifecycle.

    python3 tools/test_export.py

Split from the browser suite on purpose: none of this needs a page, and the
trust-boundary checks (a posted name must not become a path) are the kind of
thing that should be cheap enough to run on every change.
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import time
import zipfile
from pathlib import Path

import cv2
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
# BEFORE the app import: export.py reads EXPORT_DIR at module scope, so pointing
# it at a tmpdir afterwards would leave _target writing to the real one.
_TMP = tempfile.mkdtemp(prefix="fluoro-export-test-")
os.environ["EXPORT_DIR"] = _TMP
from app import export as E  # noqa: E402

FAILS: list[str] = []


def check(label, cond, detail=""):
    print(f"{'PASS' if cond else 'FAIL'}  {label}" + (f"   {detail}" if detail and not cond else ""))
    if not cond:
        FAILS.append(label)


# A realistic frame. Uniform random noise is the wrong fixture for every
# compression assertion here: it is incompressible by construction, so
# "PNG level 0 is larger than level 9" comes out within a few bytes of level 9
# and the check measures noise rather than the encoder.
def _frame(h=400, w=600):
    yy, xx = np.mgrid[0:h, 0:w].astype(np.float32)
    g = (40 + 60 * np.sin(xx / 37.0) * np.cos(yy / 29.0)).clip(0, 255).astype(np.uint8)
    r = np.random.default_rng(3)
    g[r.random((h, w)) < 0.01] = 255          # sparse specks, like a real ROI
    return cv2.cvtColor(g, cv2.COLOR_GRAY2BGR)


IMG = _frame()


def _name_checks():
    # A posted name becomes a path. This is the trust boundary.
    for bad in ["../../etc/passwd", "..", ".", "", "/etc/passwd", "a/b",
                "sub/../../out", "..\\..\\win", "x\x00y", "//", "....//"]:
        s = E.safe_name(bad, "")
        check(f"safe_name({bad!r}) cannot escape",
              not s or ("/" not in s and "\\" not in s and ".." not in s and "\x00" not in s),
              repr(s))
    check("safe_name falls back on empty", E.safe_name("") == "export")
    check("safe_name keeps a normal name", E.safe_name("IMG_0042") == "IMG_0042")
    check("safe_name survives unicode", E.safe_name("IMG_✓_01") == "IMG_-_01",
          E.safe_name("IMG_✓_01"))
    check("safe_name is length-capped", len(E.safe_name("x" * 500)) <= 120)
    check("safe_stem drops the extension",
          E.safe_stem("run.png") == "run", E.safe_stem("run.png"))
    p = E._target(E.safe_stem("../../evil"), ".png")
    check("_target stays under EXPORT_DIR",
          str(p).startswith(str(E.EXPORT_DIR.resolve())), str(p))


def _encoder_checks():
    for fmt, params in [("png", {"compression": 0}), ("png", {"compression": 9}),
                        ("jpg", {"quality": 95}), ("tiff", {})] + \
                       [("tiff", {"compression": c}) for c in E.TIFF_COMPRESSION]:
        data, ext = E.encode(IMG, fmt, params)
        dec = cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_UNCHANGED)
        check(f"encode {fmt} {params} round-trips",
              dec is not None and dec.shape == IMG.shape, f"{ext} {len(data)}B")

    d0, _ = E.encode(IMG, "png", {"compression": 0})
    d9, _ = E.encode(IMG, "png", {"compression": 9})
    check("PNG level 0 really stores (much bigger than 9)", len(d0) > len(d9) * 1.5,
          f"{len(d0)} vs {len(d9)}")

    tn, _ = E.encode(IMG, "tiff", {"compression": "none"})
    tl, _ = E.encode(IMG, "tiff", {"compression": "lzw"})
    check("TIFF lzw beats uncompressed", len(tl) < len(tn), f"{len(tl)} vs {len(tn)}")

    # Chroma: 4:2:0 averages chroma over 2x2 blocks, which is precisely what
    # destroys an ISOLATED speck — and isolated specks are the entire point of
    # this app's log/threshold chain. A smooth ramp is the wrong fixture: 4:2:0
    # preserves gradients almost perfectly, so a ramp test passes for both modes
    # and proves nothing. Measured on a checkerboard of single specks at q98:
    #   4:4:4 -> 253.6   4:2:2 -> 142.1   4:2:0 -> 86.1   (truth 255)
    # A speck at 86 is 34% of its true value, i.e. below a mid-threshold.
    speck = np.zeros((64, 64, 3), np.uint8)
    speck[::2, ::2, 0] = 255
    peaks = {}
    for sub, lab in [(0, "4:4:4"), (2, "4:2:0")]:
        data, _ = E.encode(speck, "jpg", {"quality": 98, "subsampling": sub})
        d = cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_COLOR)
        peaks[lab] = float(d[::2, ::2, 0].mean())
    check("4:4:4 keeps an isolated speck at full value",
          peaks["4:4:4"] > 240, f"{peaks['4:4:4']:.1f}")
    check("4:2:0 measurably destroys it (so the default matters)",
          peaks["4:2:0"] < peaks["4:4:4"] * 0.6,
          f"{peaks['4:2:0']:.1f} vs {peaks['4:4:4']:.1f}")

    ev = E._even(np.zeros((63, 95, 3), np.uint8))
    check("_even crops to even dimensions", ev.shape[:2] == (62, 94), str(ev.shape))


def _lifecycle_checks():
    """The job runner, with library/render stubbed so it needs no real images."""
    class FakeLib:
        @staticmethod
        def by_id(i):
            return IMG

        @staticmethod
        def get(i):
            # Ids are strings in the real library (content-addressed by path), so
            # this must not assume an int. The stub's first version used
            # f"IMG_{i:04d}" and every job failed on a format-code error, which
            # looked exactly like an exporter bug.
            n = "".join(ch for ch in str(i) if ch.isdigit()) or "0"
            return {"id": i, "name": f"IMG_{int(n):04d}"}

    class FakeRender:
        @staticmethod
        def render_array(i, s):
            return IMG

        @staticmethod
        def wait_while_busy(timeout=3.0):
            return None

    orig_lib, orig_render = E.library, E.render
    E.library, E.render = FakeLib, FakeRender
    try:
        x = E.Exporter()
        x.start({"kind": "series", "format": "png", "ids": ["a", "b", "c"],
                 "name": "run", "params": {"compression": 1}})
        for _ in range(100):
            time.sleep(0.05)
            if x.progress()["finished"]:
                break
        p = x.progress()
        check("zip job finishes", p["finished"] and not p["error"], str(p.get("error")))
        check("zip job credits every frame", p["done"] == 3, f"done={p['done']}")
        with zipfile.ZipFile(Path(_TMP) / p["file"]) as zf:
            check("zip holds one entry per frame", len(zf.namelist()) == 3,
                  str(zf.namelist()))
            check("zip entries are named after the frames",
                  all(n.startswith("IMG_") for n in zf.namelist()),
                  str(zf.namelist()))

        # Cancel must land promptly and must not leave a half-written mp4 behind.
        # 120 frames at 640px, not 3 at 320: a three-frame job finishes inside
        # the settle below, and cancelling an ALREADY-FINISHED job correctly
        # reports cancelled=false and keeps the file — which is what the first
        # version of this check tripped over and misread as a cancel bug.
        x.start({"kind": "series", "format": "video", "ids": ["a"] * 120,
                 "name": "cancelled", "fps": 10, "max_side": 640,
                 "preset": "lossless"})
        for _ in range(40):
            if x.progress()["done"] >= 2:
                break
            time.sleep(0.05)
        check("the video job is genuinely still running",
              x.progress()["running"] and not x.progress()["finished"],
              str(x.progress())[:140])
        x.cancel()
        t0 = time.time()
        while time.time() - t0 < 8 and not x.progress()["finished"]:
            time.sleep(0.05)
        check("cancel stops the job", x.progress()["finished"])
        check("cancel reports itself", x.progress()["cancelled"])
        check("a cancelled video is not left on disk",
              not (Path(_TMP) / "cancelled.mp4").exists())

        # A completed video really is a PLAYABLE file, not just a non-empty one.
        # Existence and size would both pass on a truncated mp4.
        x.start({"kind": "series", "format": "video", "ids": ["a", "b", "c", "d"],
                 "name": "good", "fps": 8, "max_side": 320, "preset": "high"})
        for _ in range(120):
            time.sleep(0.05)
            if x.progress()["finished"]:
                break
        pg = x.progress()
        check("a video job completes without error",
              pg["finished"] and not pg["error"], str(pg.get("error")))
        vp = Path(_TMP) / (pg["file"] or "nope.mp4")
        check("the video exists and is non-trivial",
              vp.exists() and vp.stat().st_size > 2000,
              f"{vp} {vp.stat().st_size if vp.exists() else 0}B")
        # csv order is width,height,nb_frames — NOT frames first. Asserting on
        # the string starting with "4," passed a real 4-frame video as a failure
        # and would have passed a 4-pixel-wide one as a success.
        probe = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=width,height,nb_frames",
             "-of", "csv=p=0", str(vp)],
            capture_output=True, text=True, timeout=30)
        parts = probe.stdout.strip().split(",")
        check("ffprobe reads it back as a real 4-frame video",
              probe.returncode == 0 and len(parts) == 3 and parts[2] == "4"
              and int(parts[0]) > 0 and int(parts[1]) > 0,
              (probe.stdout + probe.stderr).strip()[:200])

        # A second start cancels the first: two jobs must never share a counter.
        x.start({"kind": "series", "format": "png", "ids": ["z"] * 40, "name": "second"})
        first_id = x.progress()["id"]
        x.start({"kind": "series", "format": "png", "ids": ["y"] * 5, "name": "third"})
        check("a new job gets a new id", x.progress()["id"] != first_id)
        for _ in range(100):
            time.sleep(0.05)
            if x.progress()["finished"]:
                break
        check("only the newest job's frames are counted",
              x.progress()["total"] == 5, f"total={x.progress()['total']}")
    finally:
        E.library, E.render = orig_lib, orig_render


def main():
    print("export: name safety")
    _name_checks()
    print("export: encoders")
    _encoder_checks()
    print("export: job lifecycle")
    _lifecycle_checks()
    print()
    if FAILS:
        print(f"{len(FAILS)} FAILED: {FAILS}")
        sys.exit(1)
    print("ALL EXPORT CHECKS PASSED")


if __name__ == "__main__":
    main()
