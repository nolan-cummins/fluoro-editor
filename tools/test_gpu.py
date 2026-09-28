"""CPU/GPU agreement, and what the GPU is actually worth.

The GPU path in app/gpu.py is a SECOND implementation of the filter chain, and
the failure mode for a second implementation is silent divergence: the two
disagree by a few levels on some pixel, the picture looks almost right, and
nobody notices for a month. So every case here asserts the two paths are
BIT-IDENTICAL, not merely close.

Also pins the two decisions that are easy to get wrong:
  * CLAHE must be refused by the GPU, not silently skipped.
  * log in "content" mode depends on the image, so it cannot be a table.

Run inside the GPU image:
    docker exec fluoro-editor-fluoro-editor-1 python3 tools/test_gpu.py
"""
from __future__ import annotations

import copy
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np

from app import filters, gpu

FAILS: list[str] = []


def check(label, cond, detail=""):
    print(f"{'PASS' if cond else 'FAIL'}  {label}" + (f"   {detail}" if detail and not cond else ""))
    if not cond:
        FAILS.append(label)


def both(img, s):
    """Run the same settings down both paths. Returns (cpu_result, gpu_result)."""
    saved_state, saved_cp = gpu._state, gpu._cp
    try:
        gpu._state, gpu._cp = "forced off", None
        ref = filters.render(img, s)
        import cupy
        gpu._state, gpu._cp = "ok", cupy
        got = filters.render(img, s)
    finally:
        gpu._state, gpu._cp = saved_state, saved_cp
    return ref, got


def hist_both(img, roi):
    saved_state, saved_cp = gpu._state, gpu._cp
    try:
        gpu._state, gpu._cp = "forced off", None
        ref = filters.histogram(img, roi)
        import cupy
        gpu._state, gpu._cp = "ok", cupy
        got = filters.histogram(img, roi)
    finally:
        gpu._state, gpu._cp = saved_state, saved_cp
    return ref, got


def base():
    s = filters.defaults()
    s["region"] = "frame"
    s["roi"] = None
    return s


def synth(h=900, w=1400):
    """Deterministic, non-trivial content: gradients plus structure, so a LUT
    error shows up as a spatial difference and not just a value range."""
    y, x = np.mgrid[0:h, 0:w].astype(np.float32)
    r = (x / w * 255)
    g = (y / h * 255)
    b = ((x + y) / (w + h) * 255)
    img = np.stack([r, g, b], axis=-1)
    img[100:400, 200:900] *= 0.35
    img[500:800, 300:1200] = np.clip(img[500:800, 300:1200] * 1.6, 0, 255)
    return np.ascontiguousarray(img.astype(np.uint8))

def cases():
    """(label, settings) pairs. Each must render identically on both paths."""
    out = []

    # The configuration the user actually has: R carries the work, G and B are
    # zeroed to isolate it.
    s = base()
    for c in "rgb":
        s["channels"][c]["amount"] = 1.0 if c == "r" else 0.0
    s["channels"]["r"]["ops"]["histogram"] = {
        "enabled": True, "min": 102, "max": 181}
    out.append(("R isolated with a threshold band", s))

    # Every LUT op at once, on one channel.
    s = base()
    s["channels"]["r"]["ops"]["histogram"] = {"enabled": True, "min": 60, "max": 200}
    s["channels"]["r"]["ops"]["levels"] = {
        "enabled": True, "black": 20, "white": 240, "gamma": 0.45, "gain": 1.3}
    s["channels"]["r"]["ops"]["log"] = {
        "enabled": True, "normalize": "fixed", "ref": 255, "gain": 32.0}
    s["channels"]["r"]["ops"]["curve"] = {
        "enabled": True, "points": [[0, 0], [64, 40], [128, 170], [255, 255]]}
    out.append(("all four LUT ops fused", s))

    # Op ORDER must change the result, and both paths must agree on both orders.
    a = copy.deepcopy(s)
    a["op_order"] = ["histogram", "levels", "log", "curve"]
    b = copy.deepcopy(s)
    b["op_order"] = ["curve", "log", "levels", "histogram"]
    out.append(("order A", a))
    out.append(("order B (reversed)", b))

    # Fractional contribution, and difference blending.
    for c in "rgb":
        s["channels"][c]["amount"] = 0.37
    out.append(("fractional amount on all channels", s))
    d = copy.deepcopy(s)
    d["channels"]["g"]["blend"] = "difference"
    out.append(("difference blend on G", d))

    # ROI region.
    r = base()
    r["region"] = "roi"
    r["roi"] = [0.2, 0.15, 0.7, 0.8]
    for c in "rgb":
        r["channels"][c]["amount"] = 1.0 if c == "r" else 0.0
    r["channels"]["r"]["ops"]["histogram"] = {"enabled": True, "min": 40, "max": 220}
    out.append(("ROI-scoped threshold + isolation", r))

    # Nothing to do: both paths must return the input untouched.
    out.append(("no ops, amount 1 (no-op)", base()))

    return out


def main() -> int:
    if not gpu.enabled():
        print(f"GPU not available: {gpu.status()}")
        print("Run this inside the GPU image:")
        print("  docker exec fluoro-editor-fluoro-editor-1 python3 tools/test_gpu.py")
        return 0

    img = synth()
    print(f"test frame {img.shape[1]}x{img.shape[0]}, GPU: {gpu.status()}\n")

    print("CPU/GPU agreement (bit-identical, not close)")
    for label, s in cases():
        ref, got = both(img, s)
        check(label, np.array_equal(ref, got),
              f"max |diff| = {int(np.abs(ref.astype(int) - got.astype(int)).max())}, "
              f"{(ref != got).sum()} px differ")

    print("\nthe GPU must REFUSE what it cannot do faithfully")
    c = base()
    c["channels"]["r"]["ops"]["clahe"] = {"enabled": True, "clip_limit": 2.0,
                                           "tile_grid": 8}
    check("CLAHE makes the settings ineligible", not gpu.eligible(c))
    ref, got = both(img, c)
    check("CLAHE still renders correctly (falls back)", np.array_equal(ref, got))
    check("CLAHE actually changed the frame", not np.array_equal(ref, img))

    l = base()
    l["channels"]["r"]["ops"]["log"] = {"enabled": True, "normalize": "content",
                                         "ref": 255, "gain": 8.0}
    check("log in content mode is ineligible (needs the image)", not gpu.eligible(l))
    ref, got = both(img, l)
    check("content-mode log renders correctly (falls back)", np.array_equal(ref, got))

    print("\nhistogram agreement")
    for roi in (None, [0.2, 0.15, 0.7, 0.8]):
        ref, got = hist_both(img, roi)
        check(f"histogram roi={roi is not None}", ref == got,
              "bin counts differ")
    ref, _ = hist_both(img, None)
    check("histogram totals the pixel count", sum(ref["r"]) == img.shape[0] * img.shape[1])

    print("\nwhat the GPU is worth (24MP frame)")
    big = synth(4000, 6000)
    s = base()
    s["channels"]["r"]["amount"] = 1.0
    for c in "gb":
        s["channels"][c]["amount"] = 0.0
    s["channels"]["r"]["ops"]["histogram"] = {"enabled": True, "min": 102, "max": 181}
    s["channels"]["r"]["ops"]["levels"] = {"enabled": True, "black": 0, "white": 255,
                                           "gamma": 0.45, "gain": 1.0}

    def timed(force_cpu):
        saved = gpu._state, gpu._cp
        try:
            if force_cpu:
                gpu._state, gpu._cp = "forced off", None
            else:
                import cupy
                gpu._state, gpu._cp = "ok", cupy
            filters.render(big, s)
            t0 = time.perf_counter()
            for _ in range(3):
                filters.render(big, s)
            return (time.perf_counter() - t0) / 3 * 1000
        finally:
            gpu._state, gpu._cp = saved

    cpu_ms, gpu_ms = timed(True), timed(False)
    print(f"  CPU : {cpu_ms:8.1f} ms")
    print(f"  GPU : {gpu_ms:8.1f} ms   ({cpu_ms / gpu_ms:.1f}x)")
    check("the GPU is actually faster on a 24MP frame", gpu_ms < cpu_ms,
          f"gpu {gpu_ms:.0f}ms vs cpu {cpu_ms:.0f}ms")

    print(f"\n{'ALL PASS' if not FAILS else str(len(FAILS)) + ' FAILED'}")
    for f in FAILS:
        print("  FAIL  " + f)
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())

