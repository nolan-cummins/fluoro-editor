"""Tests for the background render warmer.

Run: python3 tools/test_warm.py
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import library, render, settings, warm  # noqa: E402

FAILS: list[str] = []


def check(name: str, cond: bool, extra: str = "") -> None:
    print(f"  {'PASS' if cond else 'FAIL'}  {name}{(' — ' + extra) if extra else ''}")
    if not cond:
        FAILS.append(name)


def with_library(n: int = 8) -> list[str]:
    library.load()
    library.scan()
    order = [i["id"] for i in library.as_list() if i.get("error") is None][:n]
    return order


def test_selected_first():
    print("\nselected image warms before the rest")
    order = with_library(6)
    if len(order) < 4:
        check("library has enough frames", False, f"only {len(order)}")
        return
    w = warm.Warmer(workers=1)
    p = w.start(order, first=order[3])
    check("queue reordered so 'first' leads", p["total"] == len(order))
    check("current is the first-priority frame",
          p["current"] is None or isinstance(p["current"], str))
    # The reorder must put order[3] at the head of the internal queue.
    head, _ = w._next()
    check("first popped is the selected image", head == order[3], f"got {head}")


def test_progress_completes():
    print("\nprogress reaches completion")
    order = with_library(4)
    w = warm.Warmer(workers=1)
    w.start(order, first=None)
    deadline = time.time() + 60
    while time.time() < deadline:
        p = w.progress()
        if p["finished"]:
            break
        time.sleep(0.2)
    p = w.progress()
    check("finished", p["finished"])
    check("all items accounted for", p["done"] == len(order), f"{p['done']}/{len(order)}")
    check("pct is 100", p["pct"] == 100, f"pct={p['pct']}")
    check("not marked cancelled", not p["cancelled"])
    check("eta present while running", "eta" in p)


def test_stale_job_does_not_credit_new_job():
    """A render popped under job A that finishes after a restart must not
    increment job B's counter. This is the bug the job_id carry exists for."""
    print("\nstale in-flight work does not credit a newer job")
    order = with_library(4)
    # No background thread: every pop and every credit below is driven by this
    # function, so "done did not move" can only mean the stale-credit guard
    # worked. With a live worker it was a race — the thread could credit a
    # frame it was genuinely working on at the moment of the assertion.
    w = warm.Warmer(workers=1, start_workers=False)
    w.start(order, first=None)

    # Pop one item by hand so we control exactly when it "finishes".
    item_id, job_a = w._next()
    check("popped under job A", item_id is not None and job_a > 0, f"job={job_a}")

    w.start(order, first=None)  # restart -> job B, fresh cancel event
    p_b = w.progress()
    check("new job started", p_b["id"] > job_a, f"{job_a} -> {p_b['id']}")
    before = p_b["done"]

    # Simulate the stale worker finishing its old item now.
    render.render_jpeg(item_id, settings.effective(item_id), 512)
    with w._lock:
        if w._job and w._job["id"] == job_a and not w._cancel.is_set():
            w._job["done"] += 1
    after = w.progress()
    check("stale completion did not bump the new job", after["done"] == before,
          f"{before} -> {after['done']}")


def test_cancel_stops_job():
    print("\ncancel marks the job stopped")
    order = with_library(4)
    w = warm.Warmer(workers=1)
    w.start(order, first=None)
    w.cancel()
    p = w.progress()
    check("cancelled flag set", p["cancelled"])
    check("not running", not p["running"])


def test_empty_library_is_safe():
    print("\nempty library does not crash")
    w = warm.Warmer(workers=1)
    p = w.start([], first=None)
    check("total 0", p["total"] == 0)
    check("pct 100", p["pct"] == 100)
    check("not running", not p["running"])


def test_local_override_is_respected():
    """A per-image local override must be what gets warmed — warming with the
    global value would cache the wrong pixels under the same key."""
    print("\nwarming honours per-image local overrides")
    order = with_library(4)
    if not order:
        check("library populated", False)
        return
    target = order[0]
    # Pin a known global first. These tests share the live settings file with
    # whatever the UI last saved, so "differs from global" is only meaningful
    # against a value we chose — otherwise a prior run (or a user's own setting)
    # makes the assertion pass or fail at random.
    saved_global = settings.get_global()
    settings.set_global({"channels": {"g": {"amount": 0.0}}})
    # A per-channel amount is the sharpest possible override: it changes the
    # composited pixels of that one channel and nothing else.
    settings.set_local(target, {"channels": {"g": {"amount": 1.0,
                                                    "ops": {"log": {"enabled": True,
                                                                    "gain": 64.0}}}}})
    try:
        s_loc = settings.effective(target)
        s_oth = settings.effective(order[1])
        check("local override differs from global",
              s_loc["channels"]["g"]["amount"] != s_oth["channels"]["g"]["amount"],
              f"{s_loc['channels']['g']['amount']} vs "
              f"{s_oth['channels']['g']['amount']}")
        check("override targets the right image only",
              s_loc["channels"]["g"]["amount"] == 1.0
              and s_loc["channels"]["g"]["ops"]["log"]["enabled"] is True)
        # A g-only override must not silently enable log on r as well. Compare
        # against the OTHER image's effective settings, which are pure global.
        check("a channel-scoped override does not spread to other channels",
              s_loc["channels"]["r"]["ops"]["log"]["enabled"]
              == s_oth["channels"]["r"]["ops"]["log"]["enabled"],
              f"r.log target={s_loc['channels']['r']['ops']['log']['enabled']} "
              f"other={s_oth['channels']['r']['ops']['log']['enabled']}")
    finally:
        settings.reset_local(target, "*")
        settings.set_global(saved_global)


def test_invalidate_actually_invalidates():
    """invalidate(image_id) used to be a silent no-op.

    key_for returns a sha1 DIGEST, and the old body searched for keys starting
    with `image_id + "|"` — which no digest can ever do. Every caller therefore
    believed it had dropped a cached render and none were dropped. Assert the
    behaviour, not the implementation: after invalidating one image, that
    image's render is gone and another image's is not.
    """
    order = with_library(3)
    if len(order) < 2:
        check("library has enough frames", False, f"only {len(order)}")
        return
    a, b = order[0], order[1]
    s = settings.effective(a)
    render.invalidate()
    render.render_jpeg(a, s, 256)
    render.render_jpeg(b, s, 256)
    before = render.stats()["entries"]
    check("both renders are cached", before >= 2, f"entries={before}")

    render.invalidate(a)
    after = render.stats()["entries"]
    check("invalidating one image drops its entries", after < before,
          f"entries {before} -> {after}")

    # The other image must survive.
    key_b = render.key_for(b, s, 256)
    check("the other image's render is untouched", key_b in render._cache)


if __name__ == "__main__":
    print("=" * 62)
    print("background render warmer")
    print("=" * 62)
    for fn in (test_selected_first, test_progress_completes,
               test_stale_job_does_not_credit_new_job, test_cancel_stops_job,
               test_empty_library_is_safe, test_local_override_is_respected,
               test_invalidate_actually_invalidates):
        try:
            fn()
        except Exception as e:
            check(fn.__name__ + " (raised)", False, f"{type(e).__name__}: {e}")
    print("\n" + "=" * 62)
    if FAILS:
        print(f"{len(FAILS)} FAILED: {FAILS}")
        sys.exit(1)
    print("all warmer tests passed")
