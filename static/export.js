/* Export dialog (P9).
 *
 * Export -> Frame | Series -> format + params -> background job -> download.
 *
 * The menu is built from /api/export/options, not hardcoded, so a build without
 * ffmpeg never shows a video item that could only fail. Everything numeric here
 * is a hint for the user; app/export.py re-clamps all of it, because this is
 * untrusted input that ends up as encoder settings and subprocess arguments.
 *
 * Filters follow the sweep bar's `filters` checkbox rather than having their own
 * toggle: "export what I am looking at" and "export the processed frame" are the
 * same question, and two toggles for one setting is how they get out of sync.
 */
(function () {
  "use strict";
  const $ = (id) => document.getElementById(id);
  const A = window.App;           // app.js, for toast()/api()/state
  if (!A) { console.warn("export.js: App not ready"); return; }

  let opts = null;                // /api/export/options
  let view = "pick";              // pick | frame | series
  let fmt = "png";                // png | jpg | tiff | video
  let poll = null;
  let lastFocus = null;

  // ── the shape of each destination ───────────────────────
  // `fields` drives both the DOM and which params get posted, so a control
  // cannot exist without its value reaching the server.
  const DEST = {
    png: {
      label: "PNG", ext: ".png",
      fields: [
        { k: "compression", t: "range", label: "Compression", min: 0, max: 9,
          step: 1, val: 3, fmt: (v) => (v === 0 ? "0 · store" : String(v)),
          hint: "0 stores the pixels uncompressed — biggest file, fastest write." },
      ],
    },
    jpg: {
      label: "JPEG", ext: ".jpg",
      fields: [
        { k: "quality", t: "range", label: "Quality", min: 1, max: 100,
          step: 1, val: 92, fmt: (v) => `${v}` },
        { k: "subsampling", t: "select", label: "Chroma", val: 0, options: [
          { v: 0, l: "4:4:4 · full chroma" },
          { v: 1, l: "4:2:2" },
          { v: 2, l: "4:2:0 · smallest" },
        ],
          hint: "4:2:0 averages chroma over 2×2 blocks, which is what destroys "
              + "isolated specks. Measured here: a single speck drops from 255 "
              + "to 86." },
      ],
    },
    tiff: {
      label: "TIFF", ext: ".tif",
      fields: [],  // filled from opts.tiff_compression at render time
    },
    video: {
      label: "MP4 video", ext: ".mp4",
      fields: [
        { k: "preset", t: "select", label: "Quality", val: "high", options: [
          { v: "lossless", l: "Lossless · x264 qp 0" },
          { v: "high", l: "High · crf 14" },
          { v: "compact", l: "Compact · crf 23" },
        ],
          hint: "Lossless keeps every pixel and plays in a browser, at a very "
              + "large file size." },
        { k: "fps", t: "range", label: "Frame rate", min: 1, max: 120,
          step: 1, val: 15, fmt: (v) => `${v} fps` },
        { k: "max_side", t: "range", label: "Longest side", min: 240, max: 4096,
          step: 40, val: 1920, fmt: (v) => (v >= 4096 ? "full size" : `${v} px`),
          hint: "Video is downscaled to this. A 24MP frame is not a sane video "
              + "resolution, and encoding one is minutes of CPU per frame." },
      ],
    },
  };

  // ── helpers ─────────────────────────────────────────────
  const esc = (s) => String(s ?? "").replace(/[&<>"']/g,
    (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

  function targets() {
    // The selection if there is one, else the whole series in order. A frame
    // export is always just the playhead.
    const items = A.state.items;
    if (view === "frame") {
      const it = items.find((x) => x.id === A.state.current);
      return it ? [it] : [];
    }
    if (A.state.selected.size) {
      const set = A.state.selected;
      return items.filter((x) => set.has(x.id));
    }
    return items.slice();
  }

  // A rough size for the "this will be about…" line. Deliberately coarse and
  // labelled as an estimate: per-frame render cost, the frame count and the
  // chosen format are all real inputs, but JPEG/PNG ratios depend on the actual
  // pixels, so this is a sanity check for the user, not a promise. Video is not
  // estimated at all — the spread across presets is too wide to be useful.
  function estBytes() {
    const t = targets();
    if (!t.length) return 0;
    const it = t[0];
    const px = it.w * it.h;
    const per = fmt === "png" ? px * 0.5
      : fmt === "jpg" ? px * 0.25
      : fmt === "tiff" ? px * 3
      : 0;
    return Math.round(per * t.length);
  }

  function human(n) {
    if (!n) return "";
    const u = ["B", "KB", "MB", "GB", "TB"];
    let i = 0;
    while (n >= 1024 && i < u.length - 1) { n /= 1024; i++; }
    return `${n < 10 && i > 0 ? n.toFixed(1) : Math.round(n)} ${u[i]}`;
  }

  function fields() {
    if (fmt === "tiff") {
      // Only the compressions this libtiff was built with.
      return [{
        k: "compression", t: "select", label: "Compression", val: "none",
        options: (opts.tiff_compression || ["none"]).map((c) => ({
          v: c, l: c === "none" ? "none · uncompressed" : c,
        })),
      }];
    }
    return DEST[fmt].fields;
  }

  function collect() {
    const p = {};
    for (const f of fields()) {
      const el = document.querySelector(`#xf-${f.k}`);
      if (el) p[f.k] = f.t === "range" ? +el.value : el.value;
    }
    return p;
  }

  // ── rendering ───────────────────────────────────────────
  function open() {
    lastFocus = document.activeElement;
    view = "pick"; fmt = "png";
    // Reset the chrome a previous run mutated. Without this, reopening after a
    // finished export leaves the button reading "Close" and hides Export.
    $("x-go").hidden = false;
    $("x-cancel").textContent = "Cancel";
    $("x-note").textContent = "";
    $("x-back").hidden = false;
    render();
    $("x-modal").querySelector("button, input, select")?.focus();
  }

  function close() {
    $("x-back").hidden = true;
    if (poll) { clearTimeout(poll); poll = null; }
    // A job left running is cancelled rather than orphaned: the dialog is the
    // only thing polling it, so closing would leave it rendering to a file
    // nobody is told about.
    const wasRunning = A.state.xRunning;
    if (wasRunning) {
      A.api("POST", "/api/export/cancel").catch(() => {});
      A.state.xRunning = false;
      A.toast("export cancelled", "");
    }
    if (lastFocus && lastFocus.focus) lastFocus.focus();
  }

  function render() {
    const body = $("x-body");
    const t = targets();
    $("x-crumb").textContent = view === "pick" ? "" : `Export › ${view}`;
    $("x-title").textContent =
      view === "pick" ? "Export"
      : view === "frame" ? "Export this frame"
      : "Export the series";
    $("x-back-btn").hidden = view === "pick";
    // Hidden on the pick screen: there is nothing to export until you pick, and
    // a live-looking primary button that does nothing is worse than no button.
    $("x-go").hidden = view === "pick";
    $("x-go").textContent = "Export";
    $("x-go").disabled = view !== "pick" && t.length === 0;

    if (view === "pick") { body.innerHTML = pickHTML(t); wirePick(); return; }
    body.innerHTML = formHTML(t);
    wireForm(t);
    updateNote(t);
  }

  function pickHTML(t) {
    const sel = A.state.selected.size;
    const where = sel ? `${sel} selected` : `all ${t.length} frames`;
    const canVid = opts.ffmpeg && t.length > 1;
    return `<div class="x-pick">
      <button data-go="frame">
        <span class="t">Frame</span>
        <span class="d">One image, full resolution, current filters${sel ? "" : " — the frame on screen"}</span>
      </button>
      <button data-go="series" ${canVid ? "" : "disabled title='needs at least 2 frames'"}>
        <span class="t">Series</span>
        <span class="d">${esc(where)}${canVid ? " as MP4, or a ZIP of images" : " as a ZIP of images"}</span>
      </button>
    </div>
    <div class="x-hint">${esc(opts.dir)}</div>`;
  }

  function formHTML(t) {
    const fs = fields().map((f) => {
      if (f.t === "range") {
        return `<div class="x-field">
          <label for="xf-${f.k}">${esc(f.label)}</label>
          <div class="x-row">
            <input type="range" id="xf-${f.k}" data-k="${f.k}" data-t="range"
                   min="${f.min}" max="${f.max}" step="${f.step}" value="${f.val}">
            <span class="v" id="xv-${f.k}">${esc(f.fmt(f.val))}</span>
          </div>
          ${f.hint ? `<div class="x-hint">${esc(f.hint)}</div>` : ""}
        </div>`;
      }
      const o = f.options.map((x) =>
        `<option value="${esc(x.v)}"${x.v === f.val ? " selected" : ""}>${esc(x.l)}</option>`).join("");
      return `<div class="x-field">
        <label for="xf-${f.k}">${esc(f.label)}</label>
        <select id="xf-${f.k}" data-k="${f.k}" data-t="select">${o}</select>
        ${f.hint ? `<div class="x-hint">${esc(f.hint)}</div>` : ""}
      </div>`;
    }).join("");

    const dests = Object.keys(DEST)
      .filter((k) => k !== "video" || opts.ffmpeg)
      .map((k) => `<button data-fmt="${k}">${esc(DEST[k].label)}</button>`).join("");

    return `<div class="x-field">
        <label>Format</label>
        <div class="x-pick" id="x-fmt">${dests}</div>
      </div>
      ${fs}
      <div class="x-field">
        <label for="xf-name">File name</label>
        <input type="text" id="xf-name" value="${esc(defaultName())}"
               spellcheck="false" autocomplete="off">
        <div class="x-hint" id="x-est"></div>
      </div>
      <label class="x-check">
        <input type="checkbox" id="xf-filters" ${A.state.filtersOn ? "checked" : ""}>
        Apply the current filters${view === "frame" ? "" : " to every frame"}
      </label>`;
  }

  function defaultName() {
    if (view === "frame") {
      const it = A.state.items.find((x) => x.id === A.state.current);
      return (it ? it.name : "frame").replace(/\.[^.]+$/, "");
    }
    return `series-${new Date().toISOString().slice(0, 16).replace(/[:T]/g, "")}`;
  }

  function wirePick() {
    $("x-body").querySelectorAll("[data-go]").forEach((b) => {
      b.onclick = () => { view = b.dataset.go; render(); };
    });
  }

  function wireForm(t) {
    $("x-fmt").querySelectorAll("[data-fmt]").forEach((b) => {
      b.onclick = () => {
        fmt = b.dataset.fmt;
        render();
        $("x-fmt").querySelector(`[data-fmt="${fmt}"]`)?.focus();
      };
    });
    // Re-render on format change only; slider input just updates its readout, so
    // the field under the pointer is never replaced mid-drag.
    $("x-body").querySelectorAll("[data-t=range]").forEach((el) => {
      el.oninput = () => {
        const f = fields().find((x) => x.k === el.dataset.k);
        const v = document.getElementById(`xv-${el.dataset.k}`);
        if (f && v) v.textContent = f.fmt(+el.value);
      };
    });
    $("xf-name")?.addEventListener("input", () => updateNote(t));
    $("xf-filters")?.addEventListener("change", () => updateNote(t));
  }

  function updateNote(t) {
    const est = document.getElementById("x-est");
    if (est) {
      const n = t.length;
      const e = estBytes();
      est.textContent = e
        ? `${n} frame${n === 1 ? "" : "s"} · roughly ${human(e)}`
        : `${n} frame${n === 1 ? "" : "s"}`;
    }
    $("x-note").textContent = `${t.length} frame${t.length === 1 ? "" : "s"} → ${opts.dir}`;
  }

  // ── running a job ───────────────────────────────────────
  async function go() {
    if (view === "pick") return;
    const t = targets();
    if (!t.length) { A.toast("nothing to export", "err"); return; }
    const spec = {
      kind: view === "frame" ? "frame" : "series",
      format: fmt,
      ids: t.map((x) => x.id),
      name: ($("xf-name")?.value || "").trim(),
      filters: !!$("xf-filters")?.checked,
      params: collect(),
    };
    // fps/max_side/preset are top-level on the server, not in params.
    for (const k of ["fps", "max_side", "preset"]) {
      if (k in spec.params) { spec[k] = spec.params[k]; delete spec.params[k]; }
    }
    try {
      await A.api("POST", "/api/export/start", spec);
      A.state.xRunning = true;
      showProgress();
      tick();
    } catch (e) {
      A.toast(`export failed to start: ${e.message}`, "err");
    }
  }

  function showProgress() {
    $("x-body").innerHTML = `<div class="x-prog">
        <div class="bar"><div class="fill" id="x-fill"></div></div>
        <div class="lbl"><span id="x-cur">starting…</span><span id="x-pct">0%</span></div>
      </div>`;
    $("x-go").hidden = true;
    $("x-cancel").textContent = "Cancel export";
    $("x-back-btn").hidden = true;
  }

  function tick() {
    poll = setTimeout(async () => {
      let p;
      try { p = await A.api("GET", "/api/export/progress"); }
      catch (e) { A.state.xRunning = false; A.toast("lost the export job", "err"); return close(); }
      const fill = $("x-fill");
      if (fill) {
        fill.style.width = `${p.pct}%`;
        $("x-pct").textContent = `${p.pct}%`;
        $("x-cur").textContent = p.cancelled ? "cancelled…"
          : p.current ? `${p.done}/${p.total} · ${p.current}`
          : `${p.done}/${p.total}`;
      }
      $("x-note").textContent = p.eta ? `~${p.eta}s left` : "";
      if (!p.finished && !p.cancelled) return tick();
      A.state.xRunning = false;
      finish(p);
    }, 400);
  }

  function finish(p) {
    if (p.error) {
      $("x-body").innerHTML = `<div class="x-hint" style="color:var(--err)">${esc(p.error)}</div>`;
      $("x-cancel").textContent = "Close";
      $("x-note").textContent = "";
      return;
    }
    if (p.cancelled) {
      $("x-body").innerHTML = `<div class="x-hint">Cancelled. ${p.done} of ${p.total} frames done; nothing was written.</div>`;
      $("x-cancel").textContent = "Close";
      $("x-note").textContent = "";
      return;
    }
    const q = encodeURIComponent(p.file);
    $("x-body").innerHTML = `<div class="x-done">
        <a class="btn primary" href="/api/export/download?name=${q}" download>Download ${esc(p.file)}</a>
        <span class="subtle">${esc(human(p.size))} · also saved to ${esc(opts.dir)}</span>
      </div>
      ${p.failed ? `<div class="x-est">${p.failed} frame(s) failed and were skipped.</div>` : ""}`;
    $("x-cancel").textContent = "Close";
    $("x-note").textContent = "";
    A.toast(`exported ${p.file}`, "ok");
  }

  // ── wiring ──────────────────────────────────────────────
  async function init() {
    try { opts = await A.api("GET", "/api/export/options"); }
    catch (e) {
      A.toast("export is unavailable on this server", "err");
      $("btn-export").disabled = true;
      return;
    }
    $("btn-export").addEventListener("click", open);
    $("x-cancel").addEventListener("click", close);
    $("x-back-btn").addEventListener("click", () => { view = "pick"; render(); });
    $("x-go").addEventListener("click", go);
    // Click the backdrop, not the dialog, to dismiss. mousedown rather than
    // click so a drag that starts inside the dialog and ends on the backdrop
    // does not close it.
    $("x-back").addEventListener("mousedown", (e) => { if (e.target === $("x-back")) close(); });
    document.addEventListener("keydown", (e) => {
      if ($("x-back").hidden) return;
      if (e.key === "Escape") { e.preventDefault(); close(); }
      if (e.key === "Enter" && !A.state.xRunning && !$("x-go").hidden) {
        e.preventDefault(); go();
      }
    });
    window.Export = { open, close, _spec: () => ({ view, fmt }) };
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
