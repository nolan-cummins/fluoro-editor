"""ROI move and resize, driven through real pointer events.

The panel says "shift-drag to adjust" whenever a box exists, but for a long time
there was no code behind that gesture at all: pointerdown had two branches
(draw / pan), so the only way to change a box was to draw a new one. These
checks drive actual mouse input at real screen coordinates and assert the ROI
moved, resized, or stayed put as appropriate.

    python3 tools/test_roi_drag.py [base_url]

Restores the original ROI on exit.
"""
from __future__ import annotations

import copy
import json
import sys
import urllib.request

from playwright.sync_api import sync_playwright

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8800"
FAILS: list[str] = []
ORIGINAL: dict = {}


def check(label, cond, detail=""):
    print(f"{'PASS' if cond else 'FAIL'}  {label}" + (f"   {detail}" if detail and not cond else ""))
    if not cond:
        FAILS.append(label)


def get_settings():
    return json.load(urllib.request.urlopen(f"{BASE}/api/settings"))


def put_global(patch):
    urllib.request.urlopen(urllib.request.Request(
        f"{BASE}/api/settings/global", json.dumps(patch).encode(),
        {"Content-Type": "application/json"}, method="PUT"))


def roi_of(page):
    return page.evaluate("() => window.Viewport.getROI()")


def box_of(page):
    b = page.locator("#roi-box").bounding_box()
    return b if b and b["width"] > 1 else None


def drag(page, x0, y0, x1, y1, steps=10, mods=()):
    # P2: a plain drag PANS. Moving or resizing the box is shift+drag (or ctrl
    # to draw a new one), so every box gesture here must hold shift or it would
    # silently pan the image and leave the ROI untouched — which reads as a
    # broken ROI, which is the bug this mapping was changed to fix.
    for m in mods:
        page.keyboard.down(m)
    page.mouse.move(x0, y0)
    page.mouse.down()
    page.mouse.move(x1, y1, steps=steps)
    page.mouse.up()
    for m in reversed(mods):
        page.keyboard.up(m)
    page.wait_for_timeout(150)


def close(a, b, tol=0.01):
    return a is not None and b is not None and abs(a - b) <= tol


TEST_ROI = [0.30, 0.30, 0.55, 0.65]

def main() -> int:
    global ORIGINAL
    ORIGINAL = copy.deepcopy(get_settings()["global"])

    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1500, "height": 950})
        page.goto(BASE, wait_until="domcontentloaded")
        page.wait_for_function("() => window.Viewport && state && state.current",
                               timeout=30000)
        page.wait_for_timeout(1500)

        put_global({"roi": TEST_ROI, "region": "frame"})
        page.reload(wait_until="domcontentloaded")
        page.wait_for_function("() => window.Viewport && state && state.current",
                               timeout=30000)
        page.wait_for_timeout(1500)

        r0 = roi_of(page)
        check("ROI restored from settings on load", r0 is not None
              and close(r0[0], TEST_ROI[0], 0.005), f"got {r0}")

        handles = page.locator("#roi-box .roi-handle")
        check("8 resize handles exist", handles.count() == 8, f"got {handles.count()}")

        b = box_of(page)
        check("ROI box is drawn", b is not None, f"got {b}")
        if not b:
            return finish(browser)

        # ── move ───────────────────────────────────────────────
        w0, h0 = b["width"], b["height"]
        cx, cy = b["x"] + w0 / 2, b["y"] + h0 / 2
        drag(page, cx, cy, cx + 90, cy + 40, mods=("Shift",))
        r1 = roi_of(page)
        check("dragging inside the box MOVES it",
              r1 is not None and not close(r1[0], r0[0], 0.005), f"{r0} -> {r1}")
        if r1:
            check("move preserves the box size",
                  close(r1[2] - r1[0], r0[2] - r0[0], 0.004)
                  and close(r1[3] - r1[1], r0[3] - r0[1], 0.004), f"{r1} vs {r0}")

        # ── resize ─────────────────────────────────────────────
        se = page.locator('#roi-box .roi-handle[data-h="se"]').bounding_box()
        check("south-east handle is grabbable", se is not None, f"got {se}")
        if se:
            sx, sy = se["x"] + se["width"] / 2, se["y"] + se["height"] / 2
            drag(page, sx, sy, sx + 80, sy + 60, mods=("Shift",))
            r2 = roi_of(page)
            check("dragging a handle RESIZES the box",
                  r2 is not None and (r2[2] > r1[2] + 0.01 or r2[3] > r1[3] + 0.01),
                  f"{r1} -> {r2}")
            if r2:
                check("resize moves only the dragged corner",
                      close(r2[0], r1[0], 0.004) and close(r2[1], r1[1], 0.004),
                      f"{r1} -> {r2}")
                check("resize made the box larger",
                      r2[2] > r1[2] + 0.01 and r2[3] > r1[3] + 0.01, f"{r1} -> {r2}")

        # ── pan outside the box still pans ────────────────────
        b = box_of(page)
        vp = page.locator("#viewport").bounding_box()
        tr_before = page.evaluate(
            "() => document.getElementById('zoomer').style.transform")
        ox, oy = vp["x"] + 24, vp["y"] + vp["height"] - 24
        if b["x"] > vp["x"] + 40:                       # ensure we're outside the box
            drag(page, ox, oy, ox + 70, oy + 50)
            r3 = roi_of(page)
            tr_after = page.evaluate(
                "() => document.getElementById('zoomer').style.transform")
            check("dragging outside the box pans, and does not touch the ROI",
                  (r3 is None or all(close(a, c, 1e-9) for a, c in zip(r3, r2 or r3)))
                  and tr_after != tr_before,
                  f"roi {r2} -> {r3}; transform {tr_before} -> {tr_after}")

        # ── a press on the box must not clear it ──────────────
        b = box_of(page)
        page.mouse.move(b["x"] + b["width"] / 2, b["y"] + b["height"] / 2)
        page.mouse.down()
        page.mouse.up()
        page.wait_for_timeout(200)
        check("a click on the box does not clear the ROI", roi_of(page) is not None,
              f"got {roi_of(page)}")

        return finish(browser)


def finish(browser):
    browser.close()
    put_global({"roi": ORIGINAL.get("roi"), "region": ORIGINAL.get("region")})
    print(f"\n{'ALL PASS' if not FAILS else str(len(FAILS)) + ' FAILED'}")
    for f in FAILS:
        print("  FAIL  " + f)
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())

