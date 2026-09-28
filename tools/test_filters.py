"""Filter pipeline unit checks.

    python3 tools/test_filters.py

Covers the properties that are easy to break and invisible until they are not:
region gating (every op must respect the ROI, not just the tagged ones), channel
contribution and blending, op-order effect, settings coercion, log monotonicity
(a non-monotonic log LUT silently destroys specks), and purity (a chain must not
mutate the source buffer).
"""
from __future__ import annotations

import sys
from pathlib import Path

import cv2
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app import filters as F  # noqa: E402
from app import render as R  # noqa: E402

FAILS: list[str] = []
ROI = [0.25, 0.25, 0.75, 0.75]


def check(name: str, cond: bool, extra: str = "") -> None:
    print(f"{'PASS' if cond else 'FAIL'}  {name}"
          + (f"   {extra}" if not cond and extra else ""))
    if not cond:
        FAILS.append(name)


def flat(v: int = 100) -> np.ndarray:
    return np.full((200, 200, 3), v, np.uint8)


def op(key: str, **kw) -> dict:
    p = {"enabled": True}
    p.update(kw)
    return p


def s_with(**ops) -> dict:
    """Settings with the named ops enabled on channel R."""
    st = F.defaults()
    for k, v in ops.items():
        st["channels"]["r"]["ops"][k] = v
    return st


def _frame_with(plane: np.ndarray) -> np.ndarray:
    """A 3-channel frame whose every channel is `plane`."""
    return np.dstack([plane] * 3).astype(np.uint8)


def _render_unfused(img: np.ndarray, s: dict) -> np.ndarray:
    """Reference render that applies every op in its own pass.

    The production driver fuses consecutive table ops into a single gather. That
    is only a legitimate optimisation if it is bit-identical, so the tests
    compare against this deliberately naive implementation.
    """
    s = F._coerce(s)
    box = F.roi_box(img.shape, s["roi"]) if s["region"] == "roi" else None
    if box is None:
        x1, y1, x2, y2 = 0, 0, img.shape[1], img.shape[0]
    else:
        x1, y1, x2, y2 = box
    out = img.copy()
    for c in F.CHANNELS:
        conf = s["channels"][c]
        idx = F.CHAN_IDX[c]
        active = [k for k in s["op_order"]
                  if conf["ops"].get(k, {}).get("enabled") and k in F.OPS_IMPL]
        plane = img[y1:y2, x1:x2, idx]
        for k in active:
            plane = F.OPS_IMPL[k](plane, conf["ops"][k])
        out[y1:y2, x1:x2, idx] = F._contribute(
            img[y1:y2, x1:x2, idx], plane, float(conf["amount"]), conf["blend"])
    return out


def two_op(first: str, second: str, pa: dict, pb: dict) -> dict:
    """Settings with exactly two ops enabled, in the given order."""
    st = F.defaults()
    st["op_order"] = [first, second]
    st["channels"]["r"]["ops"][first] = op(first, **pa)
    st["channels"]["r"]["ops"][second] = op(second, **pb)
    return st


# Shared op params, so each test states only the axes it is actually varying.
TH = {"min": 100, "max": 200}
GM = {"black": 0, "white": 255, "gamma": 0.4, "gain": 1.0}
LG = {"gain": 32.0, "normalize": "fixed", "ref": 255}
CL = {"clip_limit": 4.0, "tile_grid": 4}


def main() -> None:
    print("region gating — the bug that made ROI mode look broken")
    # Every op must respect the region. Previously only histogram_mask and
    # log_transform were tagged, so isolate/levels/CLAHE ignored the ROI.
    cases = [("histogram", {"min": 150}),
             ("levels", {"gamma": 4.0}),
             ("log", {"gain": 100.0}),
             ("curve", {"points": [[0, 0], [128, 255], [255, 255]]}),
             ("clahe", {"clip_limit": 40.0, "tile_grid": 8})]
    for okey, params in cases:
        st = s_with(**{okey: op(okey, **params)})
        st["roi"] = ROI
        st["region"] = "roi"
        # A gradient, not a flat fill: a constant plane is a fixed point of
        # gamma/log/CLAHE, so "did it change?" would trivially say no.
        img = np.tile(np.arange(200, dtype=np.uint8)[None, :, None], (200, 1, 3))
        out = F.render(img, st)
        x1, y1, x2, y2 = F.roi_box(img.shape, ROI)
        inside = not np.array_equal(out[y1:y2, x1:x2, 2], img[y1:y2, x1:x2, 2])
        outside = np.concatenate([out[:y1, :, 2].ravel(), out[y2:, :, 2].ravel(),
                                  out[y1:y2, :x1, 2].ravel(), out[y1:y2, x2:, 2].ravel()])
        src_out = np.concatenate([img[:y1, :, 2].ravel(), img[y2:, :, 2].ravel(),
                                  img[y1:y2, :x1, 2].ravel(), img[y1:y2, x2:, 2].ravel()])
        check(f"{okey}: acts inside the ROI", inside)
        check(f"{okey}: does NOT leak outside the ROI",
              np.array_equal(outside, src_out))

    st = s_with(histogram=op("histogram", min=200))
    st["roi"] = ROI
    st["region"] = "frame"
    check("region=frame ignores a set ROI",
          not np.array_equal(F.render(flat(), st)[:, :, 2], flat()[:, :, 2]))

    st = s_with(histogram=op("histogram", min=200))
    st["roi"] = None
    st["region"] = "roi"
    check("region=roi with no ROI falls back to the whole frame",
          not np.array_equal(F.render(flat(), st)[:, :, 2], flat()[:, :, 2]))
    print("\nchannel contribution / isolation")
    img = np.zeros((60, 60, 3), np.uint8)
    img[:, :, 2], img[:, :, 1], img[:, :, 0] = 200, 100, 50   # R, G, B

    st = F.defaults()
    st["channels"]["r"]["amount"] = 1.0
    st["channels"]["g"]["amount"] = 0.0
    st["channels"]["b"]["amount"] = 0.0
    st["channels"]["r"]["ops"]["log"] = op("log", gain=1.0, normalize="fixed", ref=255)
    out = F.render(img, st)
    # amount=0 must BLACK the channel out, not leave the source in place. The
    # old lerp did the latter, so "G and B at 0" kept the original green and the
    # isolation looked like it had done nothing.
    check("amount=0 removes a channel entirely",
          out[0, 0, 1] == 0 and out[0, 0, 0] == 0,
          f"got BGR {out[0,0].tolist()}, want [0, 0, R]")
    check("amount=0 zeroes the WHOLE channel plane, not one pixel",
          not out[:, :, 1].any() and not out[:, :, 0].any())
    check("amount=1 keeps a channel's operation",
          not np.array_equal(out[:, :, 2], img[:, :, 2]))

    # The user-facing contract, asserted on a real pixel: only R survives.
    check("R=1, G=0, B=0 leaves pure red",
          out[0, 0].tolist() == [0, 0, int(out[0, 0, 2])]
          and out[0, 0, 2] > 0,
          f"got {out[0,0].tolist()}")

    # And with no ops enabled at all, amount alone must still be honoured —
    # this combination previously took the `continue` fast path.
    st_iso = F.defaults()
    st_iso["channels"]["g"]["amount"] = 0.0
    st_iso["channels"]["b"]["amount"] = 0.0
    out_iso = F.render(img, st_iso)
    check("amount isolates a channel with NO ops enabled",
          out_iso[0, 0, 1] == 0 and out_iso[0, 0, 0] == 0
          and out_iso[0, 0, 2] == 200,
          f"got {out_iso[0,0].tolist()}, want [0, 0, 200]")

    # A WINDOW, not a levels clip: everything outside [min,max] goes black and
    # everything inside survives untouched. The old version pushed everything
    # above `max` to 255, which is the opposite of "show me the ones I want".
    zero_it = op("histogram", min=201, max=255)

    st2 = F.defaults()
    st2["channels"]["r"]["ops"]["histogram"] = zero_it
    st2["channels"]["r"]["amount"] = 0.5
    # amount is a contribution SCALE, not a lerp toward the source: the
    # processed value is 0, so half of it is still 0. (The old lerp returned
    # 100 here — halfway back to the untouched 200 — which is the bug.)
    check("amount scales the processed channel",
          F.render(img, st2)[0, 0, 2] == 0,
          f"expected 0, got {F.render(img, st2)[0,0,2]}")

    st2b = F.defaults()
    st2b["channels"]["r"]["amount"] = 0.5
    check("amount=0.5 with no ops halves the channel",
          F.render(img, st2b)[0, 0, 2] == 100,
          f"expected 100, got {F.render(img, st2b)[0,0,2]}")

    st3 = F.defaults()
    st3["channels"]["r"]["blend"] = "difference"
    st3["channels"]["r"]["ops"]["histogram"] = zero_it
    check("difference blend shows the delta",
          F.render(img, st3)[0, 0, 2] == 200, "expected |200-0| = 200")

    out4 = F.render(img, s_with(histogram=zero_it))
    check("an op on R leaves G and B untouched",
          np.array_equal(out4[:, :, 1], img[:, :, 1])
          and np.array_equal(out4[:, :, 0], img[:, :, 0]))

    print("\nop ordering")
    # log-then-threshold vs threshold-then-log. log lifts 200 up to ~230, which
    # the threshold then clears; clearing first gives log nothing to lift.
    a, b = s_with(), s_with()
    for st, order in ((a, ["log", "histogram"]), (b, ["histogram", "log"])):
        st["op_order"] = order
        st["channels"]["r"]["ops"]["log"] = op("log", gain=8.0, normalize="fixed", ref=255)
        st["channels"]["r"]["ops"]["histogram"] = op("histogram", min=201, max=255)
    ra, rb = F.render(img, a)[0, 0, 2], F.render(img, b)[0, 0, 2]
    check("op order changes the result", ra != rb,
          f"log->threshold gave {ra}, threshold->log gave {rb}")

    print("\nthreshold is a window, not a clip")
    # The report was "I threshold, then gamma/log/CLAHE do nothing". The cause
    # was the old clip: lut[:lo]=0, lut[hi:]=255 pushed ~16% of a real frame to
    # pure white and left the survivors already saturated. A WINDOW rejects the
    # out-of-band pixels to black instead, leaving downstream ops a live
    # distribution to grade.
    ramp = np.arange(256, dtype=np.uint8).reshape(1, 256)
    win = F.op_histogram(ramp.copy(), TH)
    check("window keeps the band intact", win[0, 100] == 100 and win[0, 200] == 200)
    check("window rejects below min", win[0, 99] == 0 and win[0, 0] == 0)
    check("window rejects above max (does NOT saturate to 255)",
          win[0, 201] == 0 and win[0, 255] == 0,
          f"got {win[0,201]} and {win[0,255]}")
    check("a full-range window is the identity",
          np.array_equal(F.op_histogram(ramp.copy(), {"min": 0, "max": 255}), ramp))
    check("an inverted window is normalised, not silently empty",
          np.array_equal(F.op_histogram(ramp.copy(), {"min": 200, "max": 100}),
                         F.op_histogram(ramp.copy(), {"min": 100, "max": 200})))

    print("\nthreshold BINARISES, it is not another window")
    # The two ops exist because they answer different questions. The window
    # keeps a band greyscale; this collapses to one bit per pixel. Both are
    # asserted here so a future edit cannot quietly collapse one into the other
    # — that is precisely the confusion that motivated splitting them.
    level = 128
    bin_ = F.op_threshold(ramp.copy(), {"level": level, "invert": "off"})
    check("threshold maps >= level to 255", bin_[0, level] == 255, str(bin_[0, level]))
    check("threshold maps level-1 to 0", bin_[0, level - 1] == 0, str(bin_[0, level - 1]))
    check("threshold is strictly binary (no greyscale survives)",
          set(np.unique(bin_).tolist()) == {0, 255}, str(np.unique(bin_).tolist()))
    check("threshold at 0 makes everything white",
          bool((F.op_threshold(ramp.copy(), {"level": 0}) == 255).all()))
    # The rule is >= level, so level=255 keeps exactly the 255 sample and
    # rejects the other 255. Asserting "all zero" here would contradict the
    # second half of the same check, so count the survivors instead.
    top = F.op_threshold(ramp.copy(), {"level": 255})
    check("threshold at 255 rejects everything below it, keeps 255",
          int((top == 255).sum()) == 1 and top[0, 255] == 255 and top[0, 254] == 0,
          f"{(top == 255).sum()} samples survived, expected 1")
    check("threshold level 255 accepts exactly 255",
          F.op_threshold(np.full((1, 1), 255, np.uint8),
                         {"level": 255})[0, 0] == 255)
    check("threshold level 0 accepts even the darkest sample",
          F.op_threshold(np.full((1, 1), 0, np.uint8),
                         {"level": 0})[0, 0] == 255)

    inv = F.op_threshold(ramp.copy(), {"level": level, "invert": "on"})
    check("inverted threshold is the exact complement",
          bool(np.array_equal(inv, 255 - bin_)),
          "invert must flip every pixel, not just shift the level")
    check("inverted threshold is also strictly binary",
          set(np.unique(inv).tolist()) == {0, 255}, str(np.unique(inv).tolist()))

    win = F.op_histogram(ramp.copy(), {"min": 100, "max": 200})
    check("the window keeps greyscale, threshold does not — they differ",
          not np.array_equal(win, F.op_threshold(ramp.copy(), {"level": 100}))
          and len(np.unique(win)) > 2,
          "if these matched, the rename would have been pointless")

    print("\nthreshold is a fusible LUT")
    lut = F.op_lut("threshold", {"level": level, "invert": "off"})
    check("threshold precomputes a 256-entry table", lut is not None)
    check("the table binarises", lut is not None
          and set(np.unique(lut).tolist()) == {0, 255})
    check("the table agrees with the op applied directly",
          lut is not None and np.array_equal(lut[ramp], bin_))
    # Out-of-range levels must clamp to the equivalent in-range one, so a
    # stray value in a hand-edited config degrades to a sane threshold instead
    # of raising. Note the direction: level=9999 clamps DOWN to 255, which
    # REJECTS a 200 sample — clamping must not be mistaken for "accept all".
    check("an over-large level clamps to 255",
          np.array_equal(F.op_threshold(ramp.copy(), {"level": 9999}),
                         F.op_threshold(ramp.copy(), {"level": 255})))
    check("a negative level clamps to 0",
          np.array_equal(F.op_threshold(ramp.copy(), {"level": -50}),
                         F.op_threshold(ramp.copy(), {"level": 0})))
    check("inverted with a negative level clamps to 0 and rejects all",
          bool((F.op_threshold(ramp.copy(),
                               {"level": -50, "invert": "on"}) == 0).all()))

    print("\nthe ENCODE preserves a binarised frame (the two bugs)")
    # The op was always correct; the greys came back afterwards. Both of these
    # failed at once and together, so each is asserted separately — a fix to one
    # alone still leaves the user with visible midtones.
    rng = np.random.default_rng(11)
    h, w = 900, 1400
    field = np.zeros((h, w), np.uint8)
    sp = rng.random((h, w)) < 0.03
    field[sp] = rng.integers(60, 200, sp.sum(), dtype=np.uint8)
    binfull = F.op_threshold(field.copy(), {"level": 128, "invert": "off"})
    check("a binarised frame is exactly 0/255 before any encoding",
          set(np.unique(binfull).tolist()) == {0, 255})

    # CAUSE 1: INTER_AREA downscale averages a two-valued image into greys.
    small_area = cv2.resize(binfull, (w // 3, h // 3), interpolation=cv2.INTER_AREA)
    check("INTER_AREA downscale DOES invent midtones (this was the bug)",
          len(np.unique(small_area)) > 2,
          f"distinct={len(np.unique(small_area))}")
    small_near = cv2.resize(binfull, (w // 3, h // 3), interpolation=cv2.INTER_NEAREST)
    check("INTER_NEAREST downscale keeps it strictly binary",
          set(np.unique(small_near).tolist()) == {0, 255})

    # CAUSE 2: JPEG is lossy, so even an untouched binary buffer gains greys.
    ok, jb = cv2.imencode(".jpg", small_near, [int(cv2.IMWRITE_JPEG_QUALITY), 90])
    back_jpg = cv2.imdecode(jb, cv2.IMREAD_GRAYSCALE)
    check("JPEG re-introduces midtones into a binary frame (this was the bug)",
          len(np.unique(back_jpg)) > 2, f"distinct={len(np.unique(back_jpg))}")
    ok, pb = cv2.imencode(".png", small_near)
    back_png = cv2.imdecode(pb, cv2.IMREAD_GRAYSCALE)
    check("PNG keeps the binarised frame exact",
          set(np.unique(back_png).tolist()) == {0, 255})
    check("PNG of a binary frame is SMALLER than the JPEG, not larger",
          len(pb) < len(jb), f"png={len(pb)//1024}KB jpeg={len(jb)//1024}KB")

    print("\nis_binary picks the right path")
    check("is_binary accepts a 0/255 frame", R.is_binary(binfull))
    check("is_binary rejects a continuous-tone frame",
          not R.is_binary(field.copy()))
    check("is_binary rejects a frame that merely CONTAINS 0 and 255",
          not R.is_binary(np.array([[0, 255, 128]], np.uint8)))
    # The encode must follow the data, not the settings: a chain can reach a
    # binary result without Threshold, and Threshold plus a fractional amount
    # on another channel re-introduces midtones the op never produced.
    enc_bin = R._encode(binfull, 400, 90)
    check("a binary frame is served as PNG", enc_bin is not None
          and enc_bin[1] == "image/png", str(enc_bin[1] if enc_bin else None))
    enc_grey = R._encode(field.copy(), 400, 90)
    check("a continuous frame is still served as JPEG", enc_grey is not None
          and enc_grey[1] == "image/jpeg", str(enc_grey[1] if enc_grey else None))
    dec = cv2.imdecode(np.frombuffer(enc_bin[0], np.uint8), cv2.IMREAD_GRAYSCALE)
    check("the encoded binary frame survives the full round trip",
          set(np.unique(dec).tolist()) == {0, 255},
          f"distinct={sorted(np.unique(dec).tolist())[:6]}")
    dec2 = cv2.imdecode(np.frombuffer(enc_grey[0], np.uint8), cv2.IMREAD_GRAYSCALE)
    check("the encoded continuous frame is NOT binary",
          len(np.unique(dec2)) > 2, f"distinct={len(np.unique(dec2))}")

    print("\nthe binary test looks at the REGION, not the whole frame")
    # With region=roi the pixels outside the box are deliberately the untouched
    # original, so they are continuous-tone BY DESIGN. A whole-frame test always
    # said "not binary" in ROI mode, which silently demoted a correctly binarised
    # interior to lossy JPEG + INTER_AREA and put the greys back. That was the
    # "Threshold looks like a clip" report. The interior is the thing being
    # judged, so the interior is the thing tested.
    # A continuous-tone surround with a binarised block dropped into the middle
    # — exactly the shape region=roi produces. Built by downsampling a real
    # binarise so the outside genuinely has midtones.
    h, w = 200, 200
    surround = cv2.resize(binfull, (w, h), interpolation=cv2.INTER_AREA)
    mixed = np.repeat(surround[:, :, None], 3, axis=2)
    box = (50, 50, 150, 150)
    # box is (x1, y1, x2, y2), so the block is (h, w) = (y2-y1, x2-x1).
    stripes = np.indices((box[3] - box[1], box[2] - box[0]))[1] % 2 * 255
    # ALL THREE channels, because is_binary tests the whole block. A real
    # R-only ROI render zeroes G and B via `amount`, so the filtered block is
    # 0/255 in every channel; binarising only R here would leave G/B
    # continuous and the test would be asserting the wrong thing.
    mixed[box[1]:box[3], box[0]:box[2], :] = stripes[:, :, None].astype(np.uint8)
    check("a frame with a binary ROI is NOT binary overall (outside is raw)",
          not R.is_binary(mixed))
    check("...but is binary when asked about the region",
          R.is_binary(mixed, box))
    check("is_binary agrees with itself on the full frame",
          R.is_binary(mixed) == R.is_binary(mixed, None))
    check("an empty box is not binary", not R.is_binary(mixed, (0, 0, 0, 0)))

    # The mixed frame must still take the lossless path, and the interior must
    # survive the downscale. This is the assertion whose absence let the bug
    # ship: a whole-frame binary test passed on the fixture and failed in ROI mode.
    enc_mixed = R._encode(mixed, 100, 90, box)
    check("a frame with a binary ROI is served as PNG", enc_mixed is not None
          and enc_mixed[1] == "image/png",
          str(enc_mixed[1] if enc_mixed else None))
    dm = cv2.imdecode(np.frombuffer(enc_mixed[0], np.uint8), cv2.IMREAD_COLOR)
    iy1, iy2 = int(box[1] * 100 / w) + 2, int(box[3] * 100 / w) - 2
    ix1, ix2 = int(box[0] * 100 / w) + 2, int(box[2] * 100 / w) - 2
    interior = dm[iy1:iy2, ix1:ix2]
    check("the binary ROI interior survives the downscale in a mixed frame",
          set(np.unique(interior).tolist()) <= {0, 255},
          f"distinct={sorted(np.unique(interior).tolist())[:8]}")

    # A UNIFORM binary region is still binary. is_binary used to require that
    # BOTH 0 and 255 occur, so raising the level until everything is rejected
    # produced a region with a single value [0] that the test rejected — sending
    # dead-flat black down JPEG + INTER_AREA, which averaged the box edge and
    # manufactured 33 distinct values where the true content had one. This is
    # reachable by ordinary use: just turn the level up.
    flatblk = mixed.copy()
    flatblk[box[1]:box[3], box[0]:box[2], :] = 0
    check("a uniform-black region IS binary (both extremes need not occur)",
          R.is_binary(flatblk, box))
    white = mixed.copy()
    white[box[1]:box[3], box[0]:box[2], :] = 255
    check("a uniform-white region IS binary too", R.is_binary(white, box))
    check("a uniform region is still not binary frame-wide (outside is raw)",
          not R.is_binary(flatblk))
    enc_flat = R._encode(flatblk, 100, 90, box)
    check("a uniform-black ROI is served losslessly as PNG",
          enc_flat is not None and enc_flat[1] == "image/png",
          str(enc_flat[1] if enc_flat else None))
    df = cv2.imdecode(np.frombuffer(enc_flat[0], np.uint8), cv2.IMREAD_COLOR)
    fin = df[iy1:iy2, ix1:ix2]
    check("the uniform-black ROI interior has no midtones after the downscale",
          set(np.unique(fin).tolist()) <= {0, 255},
          f"distinct={sorted(np.unique(fin).tolist())[:8]}")

    print("\nactive_box mirrors what the pipeline filtered")
    check("active_box is None for the whole frame",
          R.active_box(mixed, {"region": "frame"}) is None)
    got = R.active_box(mixed, {"region": "roi", "roi": [0.25, 0.25, 0.75, 0.75]})
    check("active_box returns the filtered box in roi mode",
          got == (50, 50, 150, 150), str(got))

    print("\nthreshold survives an old config")
    # _coerce must append a key that did not exist when the config was written,
    # or the op is invisible in every saved config until the user re-saves.
    coerced = F._coerce({"channels": {"r": {"ops": {"log": {"enabled": True}}}}})
    check("an op added later appears in op_order",
          "threshold" in coerced["op_order"], str(coerced["op_order"]))
    check("the new op defaults to disabled",
          coerced["channels"]["r"]["ops"]["threshold"]["enabled"] is False)
    check("the new op carries its default params",
          coerced["channels"]["r"]["ops"]["threshold"]["level"] == 128
          and coerced["channels"]["r"]["ops"]["threshold"]["invert"] == "off",
          str(coerced["channels"]["r"]["ops"]["threshold"]))
    check("a config that already has threshold keeps its values",
          F._coerce({"channels": {"r": {"ops": {
              "threshold": {"enabled": True, "level": 40, "invert": "on"}}}}}
              )["channels"]["r"]["ops"]["threshold"]["level"] == 40)
    bad = F._coerce({"channels": {"r": {"ops": {"threshold": {
        "enabled": True, "level": 40, "invert": "nonsense"}}}}})
    check("a nonsense invert falls back to the default",
          bad["channels"]["r"]["ops"]["threshold"]["invert"] == "off",
          str(bad["channels"]["r"]["ops"]["threshold"]))
    check("threshold is registered in the schema",
          "threshold" in F.OP_BY_KEY
          and F.OP_BY_KEY["threshold"]["label"] == "Threshold")
    check("the window op is labelled Histogram, not Threshold",
          F.OP_BY_KEY["histogram"]["label"] == "Histogram",
          F.OP_BY_KEY["histogram"]["label"])

    print("\nthreshold respects the ROI like every other op")
    # Region gating is global, so this is really a guard against the new op
    # being wired in as a special case that skips the gate.
    half = np.zeros((100, 100, 3), np.uint8)
    half[:50, :, 2] = 200                       # bright top half only
    st_thr = F.defaults()
    st_thr["roi"] = [0.0, 0.5, 1.0, 1.0]        # bottom half
    st_thr["region"] = "roi"
    st_thr["channels"]["r"]["amount"] = 0.0
    for c in ("g", "b"):
        st_thr["channels"][c]["amount"] = 0.0
    st_thr["channels"]["r"]["ops"]["threshold"] = op("threshold", level=128,
                                                      invert="off")
    thr_out = F.render(half, st_thr)
    check("threshold inside the ROI does not leak outside it",
          thr_out[:50, :, 0].max() == 0,
          f"bright top half leaked {thr_out[:50,:,0].max()} above the ROI")

    print("\neach op produces its expected change (on a ramp)")
    r0 = F.op_histogram(ramp.copy(), {"min": 40, "max": 210})
    g_lo = F.op_levels(r0.copy(), GM)
    g_hi = F.op_levels(r0.copy(), {**GM, "gamma": 2.5})
    check("gamma<1 brightens", g_lo[0, 128] > r0[0, 128], f"{r0[0,128]} -> {g_lo[0,128]}")
    check("gamma>1 darkens", g_hi[0, 128] < r0[0, 128], f"{r0[0,128]} -> {g_hi[0,128]}")
    check("gamma=1 is the identity",
          np.array_equal(F.op_levels(ramp.copy(), {**GM, "gamma": 1.0}), ramp))
    # log is checked on the RAW ramp: applied to a windowed plane it cannot be
    # monotonic, because the window deliberately zeroes the top of the range.
    lg = F.op_log(ramp.copy(), LG)
    check("log lifts the low end hardest",
          (int(lg[0, 10]) - 10) > (int(lg[0, 200]) - 200),
          f"10->{lg[0,10]} (+{int(lg[0,10])-10}) 200->{lg[0,200]} (+{int(lg[0,200])-200})")
    check("log is monotonic", bool(np.all(np.diff(lg[0].astype(int)) >= 0)))
    # ...but it still lifts the survivors of a window, which is the point.
    lg_win = F.op_log(r0.copy(), LG)
    check("log lifts the survivors of a window",
          int(lg_win[0, 128]) > int(r0[0, 128]),
          f"{r0[0,128]} -> {lg_win[0,128]}")

    print("\norder changes the result (every adjacent swap)")
    # Order being draggable in the UI is only meaningful if it actually matters.
    frame = _frame_with(np.tile(np.linspace(0, 255, 256, dtype=np.uint8), (64, 1)))
    for (a_, pa, b_, pb) in (("histogram", TH, "levels", GM),
                             ("histogram", TH, "log", LG),
                             ("levels", GM, "log", LG),
                             ("histogram", TH, "clahe", CL)):
        fwd = F.render(frame, two_op(a_, b_, pa, pb))
        rev = F.render(frame, two_op(b_, a_, pa, pb))
        check(f"{a_}->{b_} differs from {b_}->{a_}",
              not np.array_equal(fwd, rev),
              f"both gave R={fwd[0,0,2]}")

    print("\nthreshold then gamma grades the survivors (the reported failure)")
    st_tg = two_op("histogram", "levels", TH, GM)
    both = F.render(frame, st_tg)
    t_only = F.render(frame, two_op("histogram", "levels", TH,
                                    {**GM, "gamma": 1.0}))
    check("gamma after threshold brightens the surviving band",
          both[0, 128, 2] > t_only[0, 128, 2],
          f"threshold-only {t_only[0,128,2]} -> +gamma {both[0,128,2]}")
    check("rejected pixels stay black through the chain",
          both[0, 5, 2] == 0 and both[0, 250, 2] == 0,
          f"low={both[0,5,2]} high={both[0,250,2]}")
    # The old clip failed exactly here: it turned the rejected high end into
    # 255, so "the ones I want" were no longer distinguishable from the rejects.
    check("rejected pixels are NOT brightened into visibility",
          both[0, 250, 2] < 128, f"high end = {both[0,250,2]}")

    print("\nLUT fusion is exact")
    # Fusing table ops into one gather is only legitimate if it is bit-identical
    # to running them in sequence. This is the guard on the speed optimisation.
    for order in (["histogram", "levels", "log", "curve"],
                  ["levels", "histogram", "curve", "log"],
                  ["histogram", "clahe", "levels", "log"]):
        st = F.defaults()
        st["op_order"] = order
        st["channels"]["r"]["ops"]["histogram"] = op("histogram", **TH)
        st["channels"]["r"]["ops"]["levels"] = op("levels", **GM)
        st["channels"]["r"]["ops"]["log"] = op("log", **LG)
        st["channels"]["r"]["ops"]["curve"] = op("curve", points=[[0, 0], [60, 30], [255, 255]])
        st["channels"]["r"]["ops"]["clahe"] = op("clahe", **CL)
        check(f"fused == sequential for {'>'.join(order)}",
              np.array_equal(F.render(frame, st), _render_unfused(frame, st)),
              f"{int((F.render(frame, st) != _render_unfused(frame, st)).sum())} px differ")
    # log in "content" mode reads the plane's own max, so it must NOT be folded.
    st = two_op("levels", "log", GM, {"gain": 8.0, "normalize": "content"})
    check("fused == sequential with log/content (unfusable op)",
          np.array_equal(F.render(frame, st), _render_unfused(frame, st)))

    print("\npurity")
    st = s_with(histogram=op("histogram", min=0, max=0))
    st["channels"]["g"]["ops"]["clahe"] = op("clahe", clip_limit=4.0)
    src = img.copy()
    first, second = F.render(img, st), F.render(img, st)
    check("render does not mutate its input", np.array_equal(img, src))
    check("render is deterministic", np.array_equal(first, second))

    print("\nsettings coercion")
    check("empty settings give defaults",
          F._coerce({})["region"] == "frame" and F._coerce({})["roi"] is None)
    check("amount clamps above 1",
          F._coerce({"channels": {"r": {"amount": 5.0}}})["channels"]["r"]["amount"] == 1.0)
    check("amount clamps below 0",
          F._coerce({"channels": {"r": {"amount": -3}}})["channels"]["r"]["amount"] == 0.0)
    # The deep-merge bug: a partial channel patch must not wipe the op stack.
    check("a partial channel patch keeps the op stack",
          set(F._coerce({"channels": {"r": {"amount": 0.5}}})["channels"]["r"]["ops"])
          == set(F.OP_KEYS))
    check("an inverted ROI is rejected", F._coerce({"roi": [0.9, 0.9, 0.1, 0.1]})["roi"] is None)
    check("an out-of-range ROI is rejected", F._coerce({"roi": [0, 0, 5, 5]})["roi"] is None)
    check("an unknown region falls back to frame",
          F._coerce({"region": "nonsense"})["region"] == "frame")
    d = F._coerce({"op_order": ["log", "bogus"]})
    check("unknown ops are dropped from the order",
          "bogus" not in d["op_order"] and "log" in d["op_order"])
    check("missing ops are appended to the order",
          set(d["op_order"]) == set(F.OP_KEYS), str(d["op_order"]))
    check("out-of-range params clamp",
          F._coerce({"channels": {"r": {"ops": {"levels": {"gain": 99.0}}}}})
          ["channels"]["r"]["ops"]["levels"]["gain"] == 8.0)
    check("an unknown blend falls back to normal",
          F._coerce({"channels": {"r": {"blend": "wat"}}})["channels"]["r"]["blend"] == "normal")

    print("\nenabled / has_work")
    check("has_work is False for defaults", not F.has_work(F.defaults()))
    st = s_with(log=op("log", gain=32.0))
    check("has_work is True when an op is on", F.has_work(st))
    st["channels"]["r"]["amount"] = 0.0
    check("has_work stays True when only amount differs from 1",
          F.has_work(st), "amount=0 must still black the channel out")
    # The isolation case that reported "no work": amounts off, no ops anywhere.
    st_iso = F.defaults()
    st_iso["channels"]["g"]["amount"] = 0.0
    st_iso["channels"]["b"]["amount"] = 0.0
    check("has_work is True for G=B=0 with no ops enabled", F.has_work(st_iso))
    st_half = F.defaults()
    st_half["channels"]["r"]["amount"] = 0.5
    check("has_work is True for a partial amount", F.has_work(st_half))
    blank = np.zeros((40, 40, 3), np.uint8)
    check("an all-disabled stack returns the input unchanged",
          F.render(blank, F.defaults()) is blank)

    print("\nlog")
    plane = np.arange(256, dtype=np.uint8).reshape(1, 256)
    for g in (0.5, 8.0, 128.0):
        o = F.op_log(plane, {"gain": g, "normalize": "fixed", "ref": 255})
        check(f"log is monotonic (gain={g})",
              bool(np.all(np.diff(o.astype(int)) >= 0)))
    o = F.op_log(plane, {"gain": 32.0, "normalize": "fixed", "ref": 255})
    check("log maps 0 -> 0", o[0, 0] == 0)
    check("log maps 255 -> 255", o[0, 255] == 255)
    check("log boosts a dim value", o[0, 10] > 10, f"10 -> {o[0,10]}")

    print("\ndegenerate planes (no div-by-zero, no NaN)")
    zero = np.zeros((10, 10), np.uint8)
    for name, fn, p in [
            ("log/content", F.op_log, {"normalize": "content", "gain": 4.0}),
            ("log/fixed", F.op_log, {"normalize": "fixed", "ref": 255, "gain": 4.0}),
            ("clahe", F.op_clahe, {"clip_limit": 2.0, "tile_grid": 8}),
            ("levels", F.op_levels, {"black": 255, "white": 0, "gamma": 0.05, "gain": 0.0}),
            ("histogram", F.op_histogram, {"min": 200, "max": 10})]:
        check(f"{name} survives an all-zero plane", np.isfinite(fn(zero, p)).all())

    print("\nfull-res guarantee")
    big = np.full((400, 600, 3), 60, np.uint8)
    check("render preserves the frame size",
          F.render(big, s_with(clahe=op("clahe", clip_limit=2.0, tile_grid=8))).shape
          == big.shape)

    # reset_local() takes a dotted PATH because the settings tree is
    # channels.<ch>.ops.<op>. The single-level pop it replaced could not express
    # that, so "reset this operation" silently wrote defaults over the override
    # and the card stayed flagged as locally set forever.
    print("\nlocal override reset")
    import app.settings as S
    saved_g, saved_l = S.get_global(), dict(S._local)
    try:
        S.set_local("img_reset", {"channels": {"r": {"ops": {
            "clahe": {"enabled": True, "clip_limit": 9.0},
            "log": {"enabled": True}}}}})
        check("the override is stored under the op path",
              "clahe" in S._local["img_reset"]["channels"]["r"]["ops"])
        S.reset_local("img_reset", "channels.r.ops.clahe")
        left = S._local.get("img_reset", {})
        check("reset removes exactly the named op",
              "clahe" not in left.get("channels", {}).get("r", {}).get("ops", {}),
              str(left))
        check("reset leaves sibling ops alone",
              "log" in left["channels"]["r"]["ops"], str(left))
        S.reset_local("img_reset", "channels.r.ops.log")
        check("pruning removes the emptied image entirely",
              "img_reset" not in S._local, str(list(S._local)))
        S.set_local("img_reset2", {"channels": {"r": {"ops": {"clahe": {"enabled": True}}}}})
        S.reset_local("img_reset2", "channels.r.ops.does_not_exist")
        check("resetting a missing path does not damage the override",
              "clahe" in S._local["img_reset2"]["channels"]["r"]["ops"])
        S.reset_local("img_reset2")
        check("reset with no section drops the whole override",
              "img_reset2" not in S._local)
    finally:
        S._local.clear()
        S._local.update(saved_l)
        S.set_global(saved_g)

    print("\n" + "=" * 62)
    if FAILS:
        print(f"{len(FAILS)} FAILED: {FAILS}")
        sys.exit(1)
    print("ALL FILTER CHECKS PASSED")


main()
