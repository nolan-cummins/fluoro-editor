/* fluoro-editor — per-channel curve editor.

   Control points are [input, output] pairs in 0-255, owned by ONE channel —
   the one selected in the filter panel. The server fits a Fritsch-Carlson
   monotone spline through them, so this widget only edits the points and the
   shape between them is the server's job. Deliberate: a naive Catmull-Rom
   overshoots the control-point band by thousands of levels on a dipping curve,
   and that overshoot is exactly the clipping that erases the faint specks this
   tool exists to reveal.

   The preview uses the SAME monotone() fit the server applies, so what you see
   here is what you get — judging a curve's real shape is the editor's whole
   job, and a straight-line preview would misrepresent every dipping curve.

   Points stay sorted by x and clamped to 0-255, so the server never sees an
   out-of-range or out-of-order list. */
"use strict";

window.Curve = (() => {
  const PAD = 10;            // px padding inside the canvas
  const HIT = 9;             // px grab radius for a control point
  let canvas = null, ctx = null;
  let channel = "r";         // for display colour only; the panel owns the value
  let pts = [[0, 0], [255, 255]];
  let onChange = null;       // (points) => void
  let dragIdx = -1;
  let dpr = 1;

  const COL = { r: "#ff6b6b", g: "#7ee787", b: "#6ba8ff" };

  function init(cv, changeCb) {
    canvas = cv;
    ctx = cv.getContext("2d");
    onChange = changeCb;
    resize();
    window.addEventListener("resize", resize);

    const pos = (e) => {
      const r = canvas.getBoundingClientRect();
      // CSS px -> value space, inverted on y (0 at the bottom).
      const fx = (e.clientX - r.left - PAD) / (r.width - 2 * PAD);
      const fy = 1 - (e.clientY - r.top - PAD) / (r.height - 2 * PAD);
      return [Math.round(Math.max(0, Math.min(1, fx)) * 255),
              Math.round(Math.max(0, Math.min(1, fy)) * 255)];
    };

    canvas.addEventListener("pointerdown", (e) => {
      const [x, y] = pos(e);
      dragIdx = nearest(x, y);
      if (dragIdx < 0) {
        // Clicking empty canvas inserts a point there — the fastest way to add
        // a bend without hunting for a free x slot.
        dragIdx = pts.length;
        pts.push([x, y]);
        sortPts();
        commit();
      }
      canvas.setPointerCapture(e.pointerId);
      e.preventDefault();
    });

    canvas.addEventListener("pointermove", (e) => {
      const [x, y] = pos(e);
      if (dragIdx < 0) {
        canvas.style.cursor = nearest(x, y) >= 0 ? "grab" : "crosshair";
        return;
      }
      // The two endpoints stay pinned in x: they anchor 0 and 255, which is what
      // makes this a true input->output mapping rather than a free spline.
      const lo = dragIdx === 0 ? 0 : pts[dragIdx - 1][0] + 1;
      const hi = dragIdx === pts.length - 1 ? 255 : pts[dragIdx + 1][0] - 1;
      pts[dragIdx] = [Math.max(lo, Math.min(hi, x)), y];
      commit();
    });

    const end = (e) => {
      if (dragIdx < 0) return;
      dragIdx = -1;
      try { canvas.releasePointerCapture(e.pointerId); } catch (_) {}
    };
    canvas.addEventListener("pointerup", end);
    canvas.addEventListener("pointercancel", end);

    // Right-click removes a point, but never the two anchors — a curve without
    // them is not a mapping.
    canvas.addEventListener("contextmenu", (e) => {
      e.preventDefault();
      const [x, y] = pos(e);
      const i = nearest(x, y);
      if (i > 0 && i < pts.length - 1) {
        pts.splice(i, 1);
        commit();
      }
    });
  }

  function nearest(x, y) {
    let best = -1, bd = HIT * HIT;
    pts.forEach((p, i) => {
      const dx = p[0] - x, dy = p[1] - y;
      const d = dx * dx + dy * dy;
      if (d <= bd) { bd = d; best = i; }
    });
    return best;
  }

  function sortPts() { pts.sort((a, b) => a[0] - b[0]); }

  // onChange carries points only. The channel is the panel's concern: the widget
  // used to also pass a channel, which meant two sources of truth for which
  // curve was being edited.
  function commit() { draw(); if (onChange) onChange(pts.map(p => [...p])); }

  function resize() {
    if (!canvas) return;
    dpr = window.devicePixelRatio || 1;
    const r = canvas.getBoundingClientRect();
    if (!r.width) return;
    canvas.width = Math.round(r.width * dpr);
    canvas.height = Math.round(r.height * dpr);
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    draw();
  }
  // Draw. The curve is drawn with the same monotone interpolation the server
  // applies, so the preview matches the real result — drawing straight lines
  // between points would misrepresent a dipping curve as smooth, and judging the
  // real shape is this editor's entire job.
  function draw() {
    if (!ctx) return;
    const w = canvas.width / dpr, h = canvas.height / dpr;
    const X = (v) => PAD + (v / 255) * (w - 2 * PAD);
    const Y = (v) => h - PAD - (v / 255) * (h - 2 * PAD);
    const col = COL[channel] || COL.master;

    ctx.clearRect(0, 0, w, h);
    ctx.fillStyle = "#12151c";
    ctx.fillRect(0, 0, w, h);

    ctx.strokeStyle = "#232a35";
    ctx.lineWidth = 1;
    for (let i = 0; i <= 4; i++) {
      const t = (i / 4) * 255;
      ctx.beginPath(); ctx.moveTo(X(t), PAD); ctx.lineTo(X(t), h - PAD); ctx.stroke();
      ctx.beginPath(); ctx.moveTo(PAD, Y(t)); ctx.lineTo(w - PAD, Y(t)); ctx.stroke();
    }
    // identity reference
    ctx.strokeStyle = "#39424f";
    ctx.setLineDash([3, 3]);
    ctx.beginPath(); ctx.moveTo(X(0), Y(0)); ctx.lineTo(X(255), Y(255)); ctx.stroke();
    ctx.setLineDash([]);

    const lut = monotone(pts);
    ctx.strokeStyle = col;
    ctx.lineWidth = 2;
    ctx.beginPath();
    for (let v = 0; v <= 255; v++) {
      const px = X(v), py = Y(lut[v]);
      v ? ctx.lineTo(px, py) : ctx.moveTo(px, py);
    }
    ctx.stroke();

    pts.forEach((p, i) => {
      ctx.beginPath();
      ctx.arc(X(p[0]), Y(p[1]), i === dragIdx ? 6 : 4.5, 0, Math.PI * 2);
      ctx.fillStyle = i === dragIdx ? "#fff" : col;
      ctx.fill();
      ctx.strokeStyle = "#0b0e13";
      ctx.lineWidth = 1.5;
      ctx.stroke();
    });
  }

  /* Fritsch-Carlson monotone cubic, matching filters.curve_lut() so the editor
     cannot preview a shape the server will not produce. 256-entry LUT out. */
  function monotone(p) {
    const n = p.length;
    const out = new Array(256);
    if (n === 0) return out.fill(0);
    if (n === 1) { out.fill(p[0][1]); return out; }
    const xs = p.map(q => q[0]), ys = p.map(q => q[1]);
    const d = [], m = [];
    for (let i = 0; i < n - 1; i++) {
      const dx = xs[i + 1] - xs[i];
      d.push(dx === 0 ? 0 : (ys[i + 1] - ys[i]) / dx);
    }
    m.push(d[0]);
    for (let i = 1; i < n - 1; i++) {
      m.push(d[i - 1] * d[i] <= 0 ? 0 : (d[i - 1] + d[i]) / 2);
    }
    m.push(d[n - 2]);
    for (let i = 0; i < n - 1; i++) {
      if (d[i] === 0) { m[i] = 0; m[i + 1] = 0; continue; }
      const a = m[i] / d[i], b = m[i + 1] / d[i];
      const s = a * a + b * b;
      if (s > 9) {
        const t = 3 / Math.sqrt(s);
        m[i] = t * a * d[i];
        m[i + 1] = t * b * d[i];
      }
    }
    for (let v = 0; v <= 255; v++) {
      if (v <= xs[0]) { out[v] = ys[0]; continue; }
      if (v >= xs[n - 1]) { out[v] = ys[n - 1]; continue; }
      let k = 0;
      while (k < n - 2 && v > xs[k + 1]) k++;
      const h = xs[k + 1] - xs[k];
      const t = h === 0 ? 0 : (v - xs[k]) / h;
      const t2 = t * t, t3 = t2 * t;
      out[v] = (2 * t3 - 3 * t2 + 1) * ys[k]
             + (t3 - 2 * t2 + t) * h * m[k]
             + (-2 * t3 + 3 * t2) * ys[k + 1]
             + (t3 - t2) * h * m[k + 1];
    }
    return out;
  }

  return {
    init,
    setChannel(ch) { channel = ch; draw(); },
    getChannel() { return channel; },
    setPoints(p) {
      if (!Array.isArray(p) || !p.length) return;
      pts = p.map(q => [Math.max(0, Math.min(255, q[0] | 0)),
                        Math.max(0, Math.min(255, q[1] | 0))]);
      sortPts();
      draw();
    },
    getPoints() { return pts.map(p => [...p]); },
    redraw() { resize(); },
  };
})();
