"""Generate a synthetic Rhodamine B series so development needs no real data.

Simulates what the experiment actually looks like: a dark microfluidic chip
(so the dye is genuinely dim in the raw file), an orange-red dye front
advancing, and single-pixel hot specks for the log transform to find.

The frame numbers deliberately skip counts and include IMG_2/IMG_10/IMG_100 so
natural sort is exercised, not just lexicographic.
"""
from __future__ import annotations

import argparse
from pathlib import Path

import cv2
import numpy as np

RNG_SEED = 20260925


def make_frame(i: int, n: int, w: int, h: int) -> np.ndarray:
    """One frame. Deliberately LOW brightness — a low-QE dye does not blow out,
    which is the whole reason the filters exist."""
    rng = np.random.default_rng(RNG_SEED + i)
    # Dark chip: faint sensor-noise floor (~4/255) plus a soft vignette.
    base = rng.normal(4.0, 1.6, (h, w)).clip(0, None)
    yy, xx = np.mgrid[0:h, 0:w]
    r2 = (xx - w * 0.5) ** 2 + (yy - h * 0.5) ** 2
    base += 14.0 * np.exp(-r2 / (2 * (0.55 * w) ** 2))
    # Channel walls of the microfluidic chip.
    base[:, : int(w * 0.12)] += 26
    base[:, -int(w * 0.12):] += 26
    # Dye front: a Gaussian blob travelling left->right, amplitude ramped so
    # brightness grows as the fluid loads.
    t = i / max(1, n - 1)
    cx = int(w * (0.18 + 0.64 * t))
    cy = int(h * (0.5 + 0.06 * np.sin(t * 3.1)))
    sigma = w * 0.07
    blob = np.exp(-(((xx - cx) ** 2 + (yy - cy) ** 2) / (2 * sigma**2)))
    amp = 26.0 + 46.0 * t          # dim, and never saturates
    # Rhodamine B emits ~580-600nm: strong R, some G, essentially no B.
    b = base.copy()
    g = base + amp * 0.30 * blob
    r = base + amp * 1.00 * blob
    # High-frequency specks: single-pixel, sparse, slightly hot. These are what
    # the log transform is for.
    for _ in range(220):
        py, px = int(rng.integers(0, h)), int(rng.integers(0, w))
        r[py, px] += rng.uniform(40, 150)
    # A couple of fixed-pattern CCD hot pixels, always in the same place.
    for py, px in ((int(h * 0.2), int(w * 0.3)), (int(h * 0.8), int(w * 0.7))):
        b[py, px] += 120
        g[py, px] += 120
        r[py, px] += 120
    return np.clip(np.dstack([b, g, r]), 0, 255).astype(np.uint8)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="data/images")
    ap.add_argument("--count", type=int, default=24)
    ap.add_argument("--width", type=int, default=1280)
    ap.add_argument("--height", type=int, default=854)
    a = ap.parse_args()

    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    # Non-contiguous numbers on purpose: catches lexicographic sort bugs.
    numbers = sorted({1 + i * 3 + (i * i) // 7 for i in range(a.count)})

    for idx, num in enumerate(numbers):
        f = make_frame(idx, len(numbers), a.width, a.height)
        cv2.imwrite(str(out / f"IMG_{num:04d}.jpg"), f,
                    [cv2.IMWRITE_JPEG_QUALITY, 92])
    print(f"wrote {len(numbers)} frames to {out}")
    print("first 8:", [f"IMG_{n:04d}.jpg" for n in numbers[:8]])
    # One deliberately corrupt file: the list must survive it.
    (out / "IMG_9999.jpg").write_bytes(b"not a jpeg at all")
    print("wrote 1 corrupt frame (IMG_9999.jpg) to test error handling")


if __name__ == "__main__":
    main()
