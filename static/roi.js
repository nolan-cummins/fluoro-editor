/* fluoro-editor — viewport navigation (zoom/pan) and ROI selection.

   Gestures share one surface, so they are disambiguated explicitly:
     wheel                 -> zoom at the cursor
     plain drag            -> pan, ALWAYS (including from inside the box)
     ctrl+drag / ROI mode  -> draw a NEW region of interest
     shift+drag inside     -> move the ROI
     shift+drag a handle   -> resize that edge/corner

   Pan is the default and unconditional. It used to lose to the box: a drag that
   started inside the ROI moved the box, so the image could only be panned by
   starting outside it — and a draw could not start inside an existing box
   either. Panning is the thing you reach for when you are lost, so it must never
   be the gesture that gets stolen.

   Because pan must win even from inside the box, the box is
   pointer-events:none (see style.css) and the handle hit-test below is
   GEOMETRIC against the stored pixel bounds rather than an e.target test: with
   the overlay transparent to the pointer, e.target is always the stage and
   e.target.closest(".roi-handle") could never match.

   ctrl draws rather than shift because ctrl-drag already means "new thing" in
   every image editor, while shift is spoken for by shift-click range-select in
   the image list. The ROI mode button is sticky ctrl, kept for accessibility
   and for trackpads where holding a modifier is awkward.

   The ROI is stored in NORMALISED image coordinates ([x1,y1,x2,y2] in 0..1), not
   pixels. Deliberate: a 1280x854 test frame and a 6000x4000 real frame can sit
   in the same session, and a pixel ROI saved from one would be nonsense on the
   other. The renderer scales it per image. */
"use strict";

window.Viewport = (() => {
  const G = window.ViewportGeometry;
  const MIN_ZOOM = 1, MAX_ZOOM = 24;
  const CLICK_SLOP = 3;        // px of movement still counted as a click

  let viewport = null, zoomer = null, overlay = null;
  let zoom = 1, panX = 0, panY = 0;
  let imgW = 0, imgH = 0;
  let roi = null;              // normalised [x1,y1,x2,y2] or null
  let roiMode = false;
  let onROI = null, onView = null;
  let drag = null;

  function init(vp, zm, ov) {
    viewport = vp; zoomer = zm; overlay = ov;

    // Handles are CHILDREN of the overlay, so they ride the box through every
    // zoom/pan transform for free rather than needing a second coordinate
    // system kept in sync by hand. `apply()` counter-scales them so they stay a
    // constant size on screen.
    for (const h of ["nw", "n", "ne", "e", "se", "s", "sw", "w"]) {
      const el = document.createElement("div");
      el.className = "roi-handle";
      el.dataset.h = h;
      overlay.appendChild(el);
    }

    viewport.addEventListener("wheel", (e) => {
      if (!imgW) return;
      e.preventDefault();
      const r = viewport.getBoundingClientRect();
      // Trackpads emit many small deltas; scale down or the zoom is unusable.
      const factor = Math.exp(-e.deltaY * (e.deltaMode === 1 ? 0.05 : 0.0015));
      const next = Math.max(MIN_ZOOM, Math.min(MAX_ZOOM, zoom * factor));
      if (next === zoom) return;
      // zoomAtPoint works in raw scale, so convert in and back out.
      const z = G.zoomAtPoint(effZoom(), next * fitScale(), panX, panY,
                              e.clientX - r.left, e.clientY - r.top);
      zoom = z.zoom / fitScale();
      panX = z.panX; panY = z.panY;
      clamp(); apply();
    }, { passive: false });

    // Advertise the modifier: with shift held, a drag means move/resize rather
    // than pan, and that is invisible until you try it. Keyup is bound to the
    // window so the class clears even when focus leaves the viewport mid-hold.
    const shiftHint = (e) => viewport.classList.toggle("shift", !!e.shiftKey);
    // On the window, not the viewport: the viewport is a div and is not
    // focusable, so a keydown there only fires if something inside it happens to
    // hold focus. The hint must appear whenever shift is down.
    window.addEventListener("keydown", shiftHint);
    window.addEventListener("keyup", shiftHint);
    window.addEventListener("blur", () => viewport.classList.remove("shift"));

    viewport.addEventListener("pointerdown", (e) => {
      if (!imgW || e.button !== 0) return;
      const r = viewport.getBoundingClientRect();
      const vx = e.clientX - r.left, vy = e.clientY - r.top;
      const p = toImage(vx, vy);
      if (e.ctrlKey || e.metaKey || roiMode) {
        drag = { mode: "roi", x0: p.x, y0: p.y, vx, vy, moved: false };
        position(p.x, p.y, p.x, p.y);
      } else if (e.shiftKey && roi) {
        // Modifier mapping. Pan is the DEFAULT and always wins, including from
        // inside the box — the old mapping made a plain drag inside the ROI move
        // the box, so the image could only be panned by starting outside it.
        //
        //   plain drag            pan, always
        //   ctrl + drag           draw a NEW ROI (replaces the old one)
        //   shift + drag inside   move the ROI
        //   shift + drag a handle resize that edge/corner
        //   ROI mode button       sticky ctrl, for accessibility
        //
        // ctrl draws rather than shift because ctrl-drag already means "new
        // thing" in every image editor, while shift is spoken for by
        // shift-click range-select in the image list.
        //
        // The hit-test is geometric rather than e.target-based: the overlay is
        // pointer-events:none so that a plain drag starting inside the box still
        // reaches this handler, which means e.target is always the stage and the
        // old e.target.closest(".roi-handle") test could never fire. These are
        // the same numbers position() wrote to the dataset, so there is no
        // second copy of the bounds maths to drift.
        const box = currentBox();
        const handle = hitHandle(p, box);
        if (handle) {
          drag = { mode: "resize", h: handle, vx, vy, moved: false, ...box };
        } else if (p.x >= box.x1 && p.x <= box.x2
                   && p.y >= box.y1 && p.y <= box.y2) {
          drag = { mode: "move", p0: p, vx, vy, moved: false, ...box };
        } else {
          drag = { mode: "pan", vx, vy, panX, panY };
          viewport.classList.add("grabbing");
        }
      } else {
        drag = { mode: "pan", vx, vy, panX, panY };
        viewport.classList.add("grabbing");
      }
      viewport.setPointerCapture(e.pointerId);
      e.preventDefault();
    });

    viewport.addEventListener("pointermove", (e) => {
      if (!drag) return;
      const r = viewport.getBoundingClientRect();
      const vx = e.clientX - r.left, vy = e.clientY - r.top;
      if (Math.abs(vx - drag.vx) > CLICK_SLOP ||
          Math.abs(vy - drag.vy) > CLICK_SLOP) drag.moved = true;
      if (drag.mode === "pan") {
        // Pan deltas are viewport px; convert with the effective scale.
        const z = effZoom();
        panX = drag.panX + (vx - drag.vx) / z;
        panY = drag.panY + (vy - drag.vy) / z;
        clamp(); apply();
        return;
      }
      const p = toImage(vx, vy);
      if (drag.mode === "roi") {
        position(drag.x0, drag.y0, p.x, p.y);
      } else if (drag.mode === "move") {
        // Deltas in IMAGE px, so the box tracks the cursor at any zoom, and
        // position() clamps it back inside the frame.
        const dx = p.x - drag.p0.x, dy = p.y - drag.p0.y;
        position(drag.x1 + dx, drag.y1 + dy, drag.x2 + dx, drag.y2 + dy);
      } else {
        // Resize: only the edges this handle names move, off the box captured
        // at pointerdown. position() normalises the order, so dragging an edge
        // past its opposite one flips the box instead of inverting it.
        let { x1, y1, x2, y2 } = drag;
        if (drag.h.includes("w")) x1 = p.x;
        if (drag.h.includes("e")) x2 = p.x;
        if (drag.h.includes("n")) y1 = p.y;
        if (drag.h.includes("s")) y2 = p.y;
        position(x1, y1, x2, y2);
      }
    });

    const end = (e) => {
      if (!drag) return;
      const d = drag;
      drag = null;
      viewport.classList.remove("grabbing");
      try { viewport.releasePointerCapture(e.pointerId); } catch (_) {}
      if (d.mode === "roi") {
        // A click (not a drag) clears the ROI — the fastest way out of a bad
        // selection without hunting for the clear button.
        if (!d.moved) setROI(null); else commitROI();
        return;
      }
      if ((d.mode === "move" || d.mode === "resize") && d.moved) commitROI();
      // A move/resize that never moved is a no-op. It must NOT fall through to
      // the clear-above: pressing on the box you just drew would erase it.
    };
    viewport.addEventListener("pointerup", end);
    viewport.addEventListener("pointercancel", end);
  }
  // Viewport px -> image (display) px.
  function toImage(vx, vy) { return G.viewportToImage(vx, vy, effZoom(), panX, panY); }

  // The box in image px, straight from what position() last wrote. Reading the
  // dataset rather than recomputing keeps the hit-test and the rendered box
  // identical by construction.
  function currentBox() {
    return {
      x1: parseFloat(overlay.dataset.x1), y1: parseFloat(overlay.dataset.y1),
      x2: parseFloat(overlay.dataset.x2), y2: parseFloat(overlay.dataset.y2),
    };
  }

  // Which handle (if any) is under this image-space point.
  //
  // The handles are 10 CSS px on screen at any zoom (roi.js apply() counter-scales
  // them), so the grab tolerance has to be expressed in image px and divided by
  // the effective scale — a fixed image-space radius would be unreachable at 12x
  // and would swallow the whole box at fit.
  function hitHandle(p, box) {
    const tol = 7 / (effZoom() || 1);   // ~7 screen px of slop around the dot
    const mx = (box.x1 + box.x2) / 2, my = (box.y1 + box.y2) / 2;
    const spots = {
      nw: [box.x1, box.y1], n: [mx, box.y1], ne: [box.x2, box.y1],
      e: [box.x2, my], se: [box.x2, box.y2], s: [mx, box.y2],
      sw: [box.x1, box.y2], w: [box.x1, my],
    };
    // Corners first: at a small box the edge handles sit within tolerance of the
    // corners too, and a corner drag is the more useful of the two.
    for (const h of ["nw", "ne", "se", "sw", "n", "e", "s", "w"]) {
      const [hx, hy] = spots[h];
      if (Math.abs(p.x - hx) <= tol && Math.abs(p.y - hy) <= tol) return h;
    }
    return null;
  }

  // zoom is expressed as a multiple of FIT, not of raw pixels: zoom 1 shows the
  // whole frame, which is what you want when a 6000x4000 image would otherwise
  // overflow the pane and show only its top-left corner. The geometry helpers
  // want a raw scale, so every call passes effZoom() and the transform uses it
  // too — keeping the two sides of the conversion consistent by construction.
  function fitScale() {
    if (!imgW || !viewport) return 1;
    const r = viewport.getBoundingClientRect();
    if (!r.width || !r.height) return 1;
    return Math.min(r.width / imgW, r.height / imgH);
  }
  function effZoom() { return zoom * fitScale(); }

  function commitROI() {
    // Read back from the rendered box: that is the authoritative selection, and
    // it keeps the clamp logic in exactly one place.
    const el = overlay;
    const x1 = parseFloat(el.dataset.x1), y1 = parseFloat(el.dataset.y1);
    const x2 = parseFloat(el.dataset.x2), y2 = parseFloat(el.dataset.y2);
    // A near-zero box is a mis-drag, not a 1-pixel ROI. Normalised 0.004 ~= 24px
    // on a 6000px frame, below which the filter would have nothing to work on.
    if (!(x2 - x1 > 0.004) || !(y2 - y1 > 0.004)) { setROI(null); return; }
    setROI([x1 / imgW, y1 / imgH, x2 / imgW, y2 / imgH]);
  }

  function setROI(r, silent) {
    roi = r;
    if (!r) {
      overlay.hidden = true;
      for (const a of ["x1", "y1", "x2", "y2"]) delete overlay.dataset[a];
      if (onROI && !silent) onROI(null);
      return;
    }
    position(r[0] * imgW, r[1] * imgH, r[2] * imgW, r[3] * imgH);
    if (onROI && !silent) onROI(r);
  }

  function position(x1, y1, x2, y2) {
    const ax = Math.min(x1, x2), ay = Math.min(y1, y2);
    const bx = Math.max(x1, x2), by = Math.max(y1, y2);
    const cx = Math.max(0, Math.min(imgW, ax)), cy = Math.max(0, Math.min(imgH, ay));
    const dx = Math.max(0, Math.min(imgW, bx)), dy = Math.max(0, Math.min(imgH, by));
    overlay.hidden = false;
    overlay.style.left = cx + "px";
    overlay.style.top = cy + "px";
    overlay.style.width = (dx - cx) + "px";
    overlay.style.height = (dy - cy) + "px";
    overlay.dataset.x1 = cx; overlay.dataset.y1 = cy;
    overlay.dataset.x2 = dx; overlay.dataset.y2 = dy;
  }

  function clamp() {
    const r = viewport.getBoundingClientRect();
    const c = G.clampPan(panX, panY, effZoom(), r.width, r.height, imgW, imgH);
    panX = c.x; panY = c.y;
  }

  function apply() {
    const z = effZoom();
    zoomer.style.transform = `translate(${panX * z}px, ${panY * z}px) scale(${z})`;
    // The handles live inside the zoomer, so without this they would scale with
    // the image: a 10px grab target becomes 120px at 12x zoom and 0.8px at fit.
    // Counter-scaling keeps them a constant size on screen at any zoom.
    zoomer.style.setProperty("--roi-inv", String(1 / (z || 1)));
    if (onView) onView(zoom);
  }

  return {
    init,
    onROIChange(fn) { onROI = fn; },
    onViewChange(fn) { onView = fn; },
    setImageSize(w, h) {
      const changed = (w !== imgW || h !== imgH);
      imgW = w; imgH = h;
      if (!w) { imgW = imgH = 0; zoom = 1; panX = panY = 0; apply(); return; }
      if (changed) {
        // A different image invalidates the pixel-space box; the normalised ROI
        // still applies, so just redraw it.
        zoom = 1; panX = panY = 0;
        if (roi) setROI(roi); else overlay.hidden = true;
      }
      apply();
    },
    setROI(r, silent) { setROI(r ? r.slice() : null, silent); },
    getROI() { return roi ? roi.slice() : null; },
    getZoom() { return zoom; },
    setROIMode(v) {
      roiMode = v;
      viewport.classList.toggle("roi-mode", v);
    },
    isROIMode() { return roiMode; },
    fit() { zoom = 1; panX = panY = 0; clamp(); apply(); },
    redraw() { clamp(); apply(); },
  };
})();
