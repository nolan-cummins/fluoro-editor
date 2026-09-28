/* fluoro-editor — single-channel histogram with draggable threshold handles.

   Shows the 0-255 distribution of ONE channel of the SOURCE image (so the
   handles stay anchored while the operations are being tuned) over the active
   region. The min/max lines drive that channel's `histogram` operation;
   dragging updates the render live and persists on release.

   Single channel, not all three: the threshold applies to the selected channel
   alone, so showing R/G/B together meant picking a handle out of three
   overlapping pairs — and a third of the drawn control had nothing to do with
   the channel being edited.

   Two details worth knowing:
   - Bins are drawn on a sqrt scale. Fluorescence histograms are extremely
     peaked, so a linear axis renders every bar except one as invisible and
     makes the threshold impossible to place. The axis stays linear in VALUE,
     only the bar HEIGHT is compressed.
   - One POST per pointer RELEASE, not per move. A drag would otherwise issue
     hundreds of full-res renders. */
"use strict";

window.Histogram = (() => {
  const PAD = 3;
  const HIT = 6;
  const COL = { r: "#ff6b6b", g: "#7ee787", b: "#6ba8ff" };
  let canvas = null, ctx = null;
  let bins = null;               // {r,g,b} each 256 counts, from the server
  let channel = "r";             // which one is displayed and edited
  let range = [0, 255];
  let onChange = null;           // ([lo, hi]) => void
  let onRelease = null;          // ([lo, hi]) => void
  let drag = null;               // 'min' | 'max'
  let dpr = 1;

  function init(cv, changeCb, releaseCb) {
    canvas = cv;
    ctx = cv.getContext("2d");
    onChange = changeCb;
    onRelease = releaseCb;
    resize();
    window.addEventListener("resize", resize);

    const vAt = (clientX) => {
      const r = canvas.getBoundingClientRect();
      const f = (clientX - r.left - PAD) / (r.width - 2 * PAD);
      return Math.round(Math.max(0, Math.min(1, f)) * 255);
    };

    const hit = (clientX) => {
      const v = vAt(clientX);
      if (Math.abs(v - range[0]) <= HIT) return "min";
      if (Math.abs(v - range[1]) <= HIT) return "max";
      return null;
    };

    canvas.addEventListener("pointerdown", (e) => {
      const v = vAt(e.clientX);
      // Click on empty space grabs the nearer handle — faster than demanding a
      // pixel-accurate grab.
      let which = hit(e.clientX);
      if (!which) {
        which = Math.abs(v - range[0]) <= Math.abs(v - range[1]) ? "min" : "max";
      }
      drag = which;
      canvas.setPointerCapture(e.pointerId);
      e.preventDefault();
    });

    canvas.addEventListener("pointermove", (e) => {
      if (!drag) {
        canvas.style.cursor = hit(e.clientX) ? "ew-resize" : "default";
        return;
      }
      const v = vAt(e.clientX);
      // Keep min < max: an inverted range would mask everything or nothing, and
      // the renderer's clamp would hide which of the two the user actually set.
      if (drag === "min") range = [Math.min(v, range[1] - 1), range[1]];
      else range = [range[0], Math.max(v, range[0] + 1)];
      draw();
      if (onChange) onChange([...range]);
    });

    const end = (e) => {
      if (!drag) return;
      drag = null;
      try { canvas.releasePointerCapture(e.pointerId); } catch (_) {}
      if (onRelease) onRelease([...range]);
    };
    canvas.addEventListener("pointerup", end);
    canvas.addEventListener("pointercancel", end);
  }

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

  function draw() {
    if (!ctx) return;
    const w = canvas.width / dpr, h = canvas.height / dpr;
    const X = (v) => PAD + (v / 255) * (w - 2 * PAD);
    const col = COL[channel] || "#8ab4f8";
    ctx.clearRect(0, 0, w, h);
    ctx.fillStyle = "#12151c";
    ctx.fillRect(0, 0, w, h);

    const b = bins && bins[channel];
    if (b) {
      // sqrt height compression: keeps a single hot bin from flattening the
      // rest of the distribution into nothing.
      let peak = 0;
      for (let i = 0; i < 256; i++) if (b[i] > peak) peak = b[i];
      if (peak) {
        ctx.fillStyle = col;
        ctx.globalAlpha = 0.5;
        const bw = (w - 2 * PAD) / 256;
        for (let i = 0; i < 256; i++) {
          if (!b[i]) continue;
          const bh = Math.sqrt(b[i] / peak) * (h - 2 * PAD);
          ctx.fillRect(X(i), h - PAD - bh, Math.max(1, bw), bh);
        }
        ctx.globalAlpha = 1;
      }
    }

    // out-of-range shading: shows at a glance what the threshold discards
    const [lo, hi] = range;
    ctx.fillStyle = "rgba(6,8,12,0.72)";
    if (lo > 0) ctx.fillRect(PAD, 0, X(lo) - PAD, h);
    if (hi < 255) ctx.fillRect(X(hi), 0, w - PAD - X(hi), h);

    ctx.strokeStyle = col;
    ctx.lineWidth = 1.5;
    for (const v of [lo, hi]) {
      ctx.beginPath();
      ctx.moveTo(X(v), 0);
      ctx.lineTo(X(v), h);
      ctx.stroke();
    }
  }

  return {
    init,
    setBins(bd) { bins = bd; draw(); },
    setChannel(ch) { channel = ch; draw(); },
    setRanges(r) {
      if (!Array.isArray(r)) return;
      const [lo, hi] = r;
      range = [Math.max(0, Math.min(254, Math.round(lo))),
               Math.max(Math.min(255, Math.round(hi)), Math.min(254, Math.round(lo)) + 1)];
      draw();
    },
    getRanges() { return [...range]; },
    redraw() { resize(); },
  };
})();

