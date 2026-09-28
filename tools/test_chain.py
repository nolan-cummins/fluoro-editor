"""The filter CHAIN, end to end, on real frames.

    IMAGES_DIR=./data/images python3 tools/test_chain.py

The unit tests in test_filters.py prove each op in isolation; this proves the
compositions users actually build. The report that prompted it was "I threshold,
then gamma/log/CLAHE do nothing" — and the cause was invisible to any per-op
test: threshold was a levels CLIP, so it pushed ~16% of a real frame to pure
white and left the surviving band already saturated, so nothing placed after it
had anything left to grade.

These run through the real filter module against real frames (not synthetic
ramps), and assert direction, not just "the pixels changed". Skips cleanly if
no images are mounted.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("DATA_DIR", str(Path(__file__).resolve().parent.parent / "data"))
os.environ.setdefault("IMAGES_DIR", str(Path(__file__).resolve().parent.parent / "data" / "images"))

from app import filters as F  # noqa: E402
from app import library  # noqa: E402

FAILS: list[str] = []
ORDER = ["histogram", "levels", "clahe", "log", "curve"]


def check(name: str, cond: bool, extra: str = "") -> None:
    print(f"{'PASS' if cond else 'FAIL'}  {name}" + (f"   {extra}" if not cond and extra else ""))
    if not cond:
        FAILS.append(name)


def base(**over) -> dict:
    """Explicit settings: region, order, amounts and every op. Never inherits."""
    ops = {
        "histogram": {"enabled": False, "min": 0, "max": 255},
        "levels": {"enabled": False, "black": 0, "white": 255, "gamma": 1.0, "gain": 1.0},
        "clahe": {"enabled": False, "clip_limit": 2.0, "tile_grid": 8},
        "log": {"enabled": False, "normalize": "fixed", "ref": 255, "gain": 32.0},
        "curve": {"enabled": False, "points": [[0, 0], [255, 255]]},
    }
    for k, v in over.items():
        ops[k] = dict(ops[k], **v)
    s = F.defaults()
    s["region"] = "frame"
    s["op_order"] = list(ORDER)
    s["channels"] = {c: F.default_channel() for c in F.CHANNELS}
    s["channels"]["r"] = {"amount": 1.0, "blend": "normal", "ops": ops}
    s["channels"]["g"]["amount"] = 0.0
    s["channels"]["b"]["amount"] = 0.0
    return s


def render(img, s):
    return F.render(img, s)[:, :, F.CHAN_IDX["r"]].astype(float)


def main() -> None:
    library.load()
    items = library.as_list()
    if not items:
        print("no images mounted (set IMAGES_DIR) — nothing to check")
        return
    it = items[0]
    img = library.by_id(it["id"])
    if img is None:
        print(f"{it['name']} failed to decode — nothing to check")
        return
    print(f"frame {it['name']}  {img.shape[1]}x{img.shape[0]}\n")

    # A band that actually splits this frame, derived from its own histogram,
    # so the assertions are about real data rather than a guessed range.
    hist = F.histogram(img)
    tot = sum(hist["r"]) or 1
    acc, lo = 0, 0
    for v, c in enumerate(hist["r"]):
        acc += c
        if acc >= 0.15 * tot:
            lo = v
            break
    acc, hi = 0, 255
    for v in range(255, -1, -1):
        acc += hist["r"][v]
        if acc >= 0.15 * tot:
            hi = v
            break
    if hi - lo < 20:
        lo, hi = 60, 200
    print(f"threshold band chosen from the frame's own histogram: [{lo}, {hi}]\n")

    TH = {"min": lo, "max": hi}
    raw = render(img, base())
    th = render(img, base(histogram=dict(TH, enabled=True)))
    low, high = raw < lo, raw > hi
    band = ~low & ~high
    if band.sum() == 0 or low.sum() == 0 or high.sum() == 0:
        print("degenerate frame (one side of the band is empty) — skipping chain checks")
        return

    print("threshold is a window, not a clip")
    check("blacks the out-of-band low end", th[low].max() <= 1, f"max {th[low].max()}")
    check("blacks the out-of-band high end", th[high].max() <= 1, f"max {th[high].max()}")
    check("leaves the in-band values untouched", np.array_equal(th[band], raw[band]))
    check("does not saturate the rejects to white (the original bug)",
          not np.array_equal(th[high], np.full(int(high.sum()), 255.0)))

    print("\neach op grades the survivors of the window")
    for label, over, up in (("gamma<1", {"levels": {"enabled": True, "gamma": 0.4}}, True),
                            ("gamma>1", {"levels": {"enabled": True, "gamma": 2.5}}, False)):
        a = render(img, base(histogram=dict(TH, enabled=True), **over))
        check(f"{label} {'brightens' if up else 'darkens'} the band",
              (a[band].mean() > th[band].mean()) == up,
              f"{th[band].mean():.2f} -> {a[band].mean():.2f}")
        check(f"{label} does not resurrect the rejects",
              a[low].max() <= 1 and a[high].max() <= 1)
    lg = render(img, base(histogram=dict(TH, enabled=True), log={"enabled": True, "gain": 32.0}))
    check("log lifts the band", lg[band].mean() > th[band].mean(),
          f"{th[band].mean():.2f} -> {lg[band].mean():.2f}")
    check("log keeps the rejects black", lg[low].max() <= 1 and lg[high].max() <= 1)
    cl = render(img, base(histogram=dict(TH, enabled=True), clahe={"enabled": True, "clip_limit": 2.0}))
    check("CLAHE changes the band", not np.array_equal(cl[band], th[band]))
    check("CLAHE raises local contrast in the band", cl[band].std() > th[band].std(),
          f"std {th[band].std():.2f} -> {cl[band].std():.2f}")

    print("\norder changes the result")
    for a, b, over in (
            ("histogram", "levels", {"histogram": dict(TH, enabled=True),
                                     "levels": {"enabled": True, "gamma": 0.4}}),
            ("histogram", "log", {"histogram": dict(TH, enabled=True),
                                  "log": {"enabled": True, "gain": 32.0}}),
            ("histogram", "clahe", {"histogram": dict(TH, enabled=True),
                                    "clahe": {"enabled": True, "clip_limit": 2.0}})):
        s1, s2 = base(**over), base(**over)
        s1["op_order"], s2["op_order"] = [a, b], [b, a]
        fwd, rev = render(img, s1), render(img, s2)
        check(f"{a} > {b} differs from {b} > {a}", not np.array_equal(fwd, rev),
              f"both mean {fwd.mean():.3f}")

    print("\nfusion is exact on a real frame")
    st = base(histogram=dict(TH, enabled=True), levels={"enabled": True, "gamma": 0.4},
              log={"enabled": True, "gain": 32.0},
              curve={"enabled": True, "points": [[0, 0], [lo, 40], [hi, 220], [255, 255]]})
    fused = F.render(img, st)
    seq = _unfused(img, st)
    check("fused == sequential, all four table ops", np.array_equal(fused, seq),
          f"{int((fused != seq).sum())} px differ")

    print()
    print("=" * 62)
    if FAILS:
        print(f"{len(FAILS)} FAILED: {FAILS}")
        sys.exit(1)
    print("ALL CHAIN CHECKS PASSED")


def _unfused(img, s):
    """Naive reference: one pass per op, no fusion. See test_filters._render_unfused."""
    s = F._coerce(s)
    out = img.copy()
    for c in F.CHANNELS:
        conf = s["channels"][c]
        idx = F.CHAN_IDX[c]
        active = [k for k in s["op_order"]
                  if conf["ops"].get(k, {}).get("enabled") and k in F.OPS_IMPL]
        plane = img[:, :, idx]
        for k in active:
            plane = F.OPS_IMPL[k](plane, conf["ops"][k])
        out[:, :, idx] = F._contribute(img[:, :, idx], plane,
                                       float(conf["amount"]), conf["blend"])
    return out


if __name__ == "__main__":
    main()
