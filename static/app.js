/* fluoro-editor — P1: library list (multi-select, drag reorder, thumbs toggle),
   viewport, sweep bar. Filter pipeline arrives in P2/P3. */
"use strict";

const $ = (id) => document.getElementById(id);
const G = window.ViewportGeometry;

// ── settings ────────────────────────────────────────────────
const state = {
  items: [],            // ordered, server-provided
  current: null,        // id shown in the viewport
  selected: new Set(),  // multi-select
  anchor: null,         // shift-range anchor
  global: {},           // global filter settings
  local: {},            // image_id -> partial override
  playing: false,
  playTimer: null,        // requestAnimationFrame handle, not a timer id
  // The sweep bar's "filters" checkbox. Display-only: it swaps which endpoint
  // the stage and the prefetcher read, and never touches stored settings, so
  // toggling it cannot alter what the pipeline would produce.
  filtersOn: true,
  // An export job is in flight; the export dialog cancels it on close rather
  // than orphaning a render nobody is watching.
  xRunning: false,
};

// The export dialog (static/export.js) needs state, toast and api, and loads
// after this file. Exposing them as one object rather than three globals keeps
// the coupling visible and greppable.
window.App = { state, api, toast };

// Global merged with this image's local override — mirrors
// settings.effective() on the server. Only used to build a cache-busting key;
// the server is the source of truth for the actual values.
//
// The merge is RECURSIVE, because the server's is (settings.deep_merge). It used
// to be a single-level spread, which looked fine until a local override for one
// op existed:
//
//   global R keys : ['amount', 'blend', 'ops']
//   client  R keys: ['ops']              <- amount and blend vanished
//   SERVER  R keys: ['amount', 'blend', 'ops']
//
// Two consequences, both real. settingsKey() hashed a settings object that
// differed from the one the server would actually render, so the cache-buster
// could be identical for two different renderings. And the Filters panel reads
// view(), which is built from this, so a local override for a single op made the
// amount slider and the sibling op cards disappear.
//
// Arrays are replaced wholesale, matching the server: a partial curve point list
// or a partial op_order would be nonsense.
function deepMerge(base, over) {
  const out = Array.isArray(base) ? base.slice() : { ...base };
  for (const [k, v] of Object.entries(over || {})) {
    const isObj = (x) => x && typeof x === "object" && !Array.isArray(x);
    out[k] = (isObj(v) && isObj(out[k])) ? deepMerge(out[k], v) : v;
  }
  return out;
}

function effective(id) {
  const out = JSON.parse(JSON.stringify(state.global));
  const over = state.local[id];
  return over ? deepMerge(out, over) : out;
}

// Short, stable hash of the effective settings. Goes in the render URL as a
// cache-buster so the browser refetches exactly when the pixels would differ.
function settingsKey(id) {
  const s = JSON.stringify(effective(id));
  let h = 0x811c9dc5;
  for (let i = 0; i < s.length; i++) {
    h ^= s.charCodeAt(i);
    h = Math.imul(h, 0x01000193) >>> 0;
  }
  return h.toString(36);
}

async function loadSettings() {
  const d = await api("GET", "/api/settings");
  state.global = d.global;
  state.local = d.local || {};
}

// ── the ONLY way to write settings ────────────────────────────
//
// Every mutation goes through here so the client's copy can never drift from
// the server's. The ROI path used to PUT and then ignore the response, leaving
// `state.global` stale: settingsKey() then hashed unchanged state, previewUrl()
// returned a byte-identical URL, and the browser served the old image from its
// cache. The server had the ROI; the screen never changed — and toggling any
// filter "fixed" it, because that path re-read the server. One helper, one
// adopt-the-response, no second path to forget.
function saveSettings(patch) {
  const id = currentId();
  const scope = window.Filters && window.Filters.getScope();
  const local = scope === "local" && id;
  return api("PUT", local ? `/api/settings/local/${id}` : "/api/settings/global", patch)
    .then((d) => {
      if (d) {
        if (d.global) state.global = d.global;
        if (d.local) {
          if (id) state.local[id] = d.local;
          else state.local = d.local;
        }
      }
      return d;
    });
}

// P3 hooks every control mutation through here.
// `light` is for the ~60x/second preview tick: the image list and the warm job
// cannot have changed meaning between two ticks of the same slider, and doing
// them anyway cost 7.8ms of a 16ms budget (rebuilding all 176 rows) plus a
// POST /api/warm/cancel per tick. Commits and structural changes pass nothing
// and get the full path.
function settingsChanged(light) {
  if (!light) {
    prefetched.clear();
    renderList();
    // NOTE: deliberately does NOT kick off a library-wide warm.
    //
    // It used to, and that was the single largest source of jank in the app:
    // every slider release cancelled and re-queued all 150 frames at ~500ms
    // each, so the server was doing ~133 seconds of full-resolution work
    // continuously and the frame being edited competed with it for memory
    // bandwidth. Warming is now something you ASK for with "Render all", once,
    // after you have settled the look you want — which is also the only time
    // it is worth paying for.
    cancelWarm();
  }
  // A settings change re-renders whatever frame is current, so it goes through
  // stepTo like everything else. Calling showCurrent() directly meant a settings
  // commit could land AFTER a newer sweep frame and paint the older one back,
  // since nothing ordered the two.
  const i = state.items.findIndex(x => x.id === state.current);
  return i >= 0 ? stepTo(i, { light: true }) : Promise.resolve();
}

// ── background warming ────────────────────────────────────────
// The frame you're looking at renders in the foreground (showCurrent) because
// that's the latency you feel. "Render all" renders the REST of the library
// into the same LRU on demand, so by the time you sweep to it the response is a
// ~6ms cache hit instead of a 0.5-1.0s full-res render.
//
// It is strictly on demand. This used to be automatic on every settings
// change, which meant every slider release cancelled and re-queued all 150
// frames — 133 seconds of full-resolution work, continuously, competing with
// the frame being edited. Rendering the whole library is worth doing once you
// have settled on a look; it is never worth doing between two adjustments.
const warmTimer = { id: null };
let warmPoll = null;
// The frame the viewport ROI was last synced from settings for. Guards the
// restore so a same-frame re-render cannot wipe a freshly drawn ROI.
let roiFrame = null;

// A warm in flight was rendered under the settings currently on screen, so the
// instant a setting changes it is stale. Stop it rather than let it compete
// with the frame being edited — that is the same jank, just triggered by hand.
function cancelWarm() {
  if (warmTimer.id) { clearTimeout(warmTimer.id); warmTimer.id = null; }
  clearTimeout(warmPoll);
  warmPoll = null;
  if (state.items.length) api("POST", "/api/warm/cancel").catch(() => {});
  hideWarm();
}

async function startWarm() {
  if (!state.items.length) return;
  if (warmTimer.id) { clearTimeout(warmTimer.id); warmTimer.id = null; }
  try {
    const p = await api("POST", "/api/warm/start", { first: state.current });
    showWarm(p);
    pollWarm();
  } catch (e) {
    // Warming is an optimisation; never surface its failures to the user.
    hideWarm();
  }
}

function pollWarm() {
  clearTimeout(warmPoll);
  warmPoll = setTimeout(async () => {
    let p;
    try {
      p = await api("GET", "/api/warm/progress");
    } catch (e) {
      return hideWarm();
    }
    showWarm(p);
    // Keep polling while work remains. A cancelled or finished job stops.
    if (p.running && !p.finished) pollWarm();
    else setTimeout(hideWarm, 1200);
  }, 400);
}

function showWarm(p) {
  if (!p || !p.total) return hideWarm();
  const el = $("warm");
  el.hidden = false;
  $("warm-fill").style.width = `${p.pct}%`;
  const eta = p.eta != null ? ` · ${p.eta}s left` : "";
  const failed = p.failed ? ` · ${p.failed} failed` : "";
  $("warm-label").textContent = `rendering ${p.done}/${p.total}${eta}${failed}`;
}

function hideWarm() {
  clearTimeout(warmPoll);
  const el = $("warm");
  if (el) el.hidden = true;
}

async function api(method, url, body) {
  const opt = { method, headers: {} };
  if (body !== undefined) {
    opt.headers["Content-Type"] = "application/json";
    opt.body = JSON.stringify(body);
  }
  const r = await fetch(url, opt);
  if (!r.ok) throw new Error(`${r.status} ${await r.text()}`);
  return r.json();
}

let toastTimer = null;
function toast(msg, kind = "") {
  const t = $("toast");
  t.textContent = msg;
  t.className = "toast " + kind;
  t.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { t.hidden = true; }, 2600);
}

// ── list rendering ─────────────────────────────────────────
function renderList() {
  const ul = $("img-list");
  $("img-count").textContent = state.items.length;
  $("vp-empty").style.display = state.items.length ? "none" : "block";
  if (!state.items.length) {
    ul.innerHTML = `<li class="list-empty">no images<br><span class="subtle">
      mount a folder at /images, or drop files anywhere</span></li>`;
    updateSelInfo();
    return;
  }
  ul.innerHTML = state.items.map((it, i) => {
    const sel = state.selected.has(it.id);
    const cur = it.id === state.current;
    const bad = !!it.error;
    const dim = bad ? "unreadable" : `${it.w}×${it.h}`;
    return `<li class="img-row${sel ? " sel" : ""}${cur ? " current" : ""}${bad ? " bad" : ""}"
              data-id="${it.id}" data-i="${i}" draggable="true" title="${it.path}">
      <span class="grip">⠿</span>
      <img class="thumb" loading="lazy" src="/api/library/thumb/${it.id}"
           alt="" onerror="this.style.visibility='hidden'">
      <span class="meta">
        <span class="nm">${it.name}</span>
        <span class="dim2">${dim}</span>
      </span>
      ${it.has_override ? '<span class="ovr" title="has local filter override">●</span>' : ""}
      <button class="rm" data-rm="${it.id}" draggable="false" title="Remove from list (file is kept)"
              aria-label="Remove ${it.name}">×</button>
    </li>`;
  }).join("");
  updateSelInfo();
}

function updateSelInfo() {
  const n = state.selected.size;
  $("sel-info").textContent = n === 0 ? "no selection"
    : n === 1 ? "1 selected" : `${n} selected`;
}

// ── selection ──────────────────────────────────────────────
function selectRow(id, e) {
  if (e.shiftKey && state.anchor) {
    const a = state.items.findIndex(x => x.id === state.anchor);
    const b = state.items.findIndex(x => x.id === id);
    if (a >= 0 && b >= 0) {
      const [lo, hi] = a < b ? [a, b] : [b, a];
      for (let i = lo; i <= hi; i++) state.selected.add(state.items[i].id);
    }
  } else if (e.metaKey || e.ctrlKey) {
    state.selected.has(id) ? state.selected.delete(id) : state.selected.add(id);
    state.anchor = id;
  } else {
    state.selected.clear();
    state.selected.add(id);
    state.anchor = id;
  }
  setCurrent(id);
  renderList();
}

function setCurrent(id) {
  if (state.current === id) return;
  const i = state.items.findIndex(x => x.id === id);
  if (i < 0) return;
  // No `follow`: selectRow runs just before this and has already built whatever
  // shift/ctrl selection was asked for, and stepping must not disturb it.
  stepTo(i);
}

// ── viewport ────────────────────────────────────────────────
const PREFETCH_AHEAD = 12, PREFETCH_BEHIND = 3;
const prefetched = new Set();

// While a filter control is being dragged we render the DRAFT: the same
// pipeline on a downscaled source, ~15ms instead of ~700ms. The full-res render
// replaces it as soon as the drag settles. Set by Filters during an input
// gesture, cleared when the value is committed.
let draftMode = false;

function setDraftMode(v) {
  if (draftMode === !!v) return;
  draftMode = !!v;
  // Re-point the current frame without touching selection or the list: the drag
  // is already in progress and owns both.
  const i = state.items.findIndex(x => x.id === state.current);
  if (i >= 0) stepTo(i, { light: true });
}

// The settings themselves, base64url-encoded, for the live-preview transport.
//
// A drag mutates ONLY the in-memory bag (Filters.ensureOpPath) and PUTs nothing
// until mouseup. The render endpoint resolves settings from PERSISTED state, so
// a preview request that carried only `key` re-rendered the last committed
// value: identical pixels for the whole drag, every filter, both scopes — the
// picture moved only on release, because that is when commit() finally PUTs.
// `key` cannot fix this; it is a browser cache-buster and the server has no idea
// what it hashes. The value needs a wire format.
//
// base64url, not encodeURIComponent: JSON here is mostly punctuation, which
// percent-encoding inflates ~2.5x, and '+'/'/' are legal in a query value but
// need the url-safe alphabet to survive a round trip unescaped. UTF-8 is
// encoded by hand first because btoa() throws on any code point above U+00FF.
function encodeSettings(obj) {
  const json = JSON.stringify(obj);
  const bytes = new TextEncoder().encode(json);
  let bin = "";
  for (let i = 0; i < bytes.length; i++) bin += String.fromCharCode(bytes[i]);
  return btoa(bin).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}

// The ONE place that decides which endpoint a frame comes from. The stage and
// the prefetcher must agree, or the sweep arrives at frames it will never
// display — which is precisely what the dead "filters" checkbox was doing: the
// stage read the toggle and the prefetcher did not.
//
//   unfiltered -> /api/library/preview, the cached downscale, no pipeline.
//                  A different URL from the filtered one, so toggling the box
//                  cannot serve a stale frame from the HTTP cache.
//   filtered   -> /api/render, the whole pipeline at FULL resolution,
//                  downscaled only for the viewport. `key` is the settings
//                  hash: it busts the browser cache on a filter change while an
//                  unchanged sweep step stays a cache hit.
//
// DRAFT mode additionally sends `s`, the client's live settings. Only the draft
// carries them: a committed render must be reproducible from the server's own
// state, or the LRU and the HTTP cache would fill with entries keyed on values
// the server was never told about.
//
// The toggle is DISPLAY ONLY. It never touches stored settings, so switching it
// back on restores the filters exactly, and it deliberately does not invalidate
// the server-side warm cache — those renders stay valid and reusable, which is
// what makes flipping back on instant.
function frameUrl(id, draft) {
  if (!state.filtersOn) return `/api/library/preview/${id}?max_side=1600`;
  return draft
    ? `/api/render/${id}?draft=1&key=${settingsKey(id)}&s=${encodeSettings(effective(id))}`
    : `/api/render/${id}?key=${settingsKey(id)}&max_side=1600`;
}

function previewUrl(id) {
  return frameUrl(id, draftMode);
}

// Warm the browser HTTP cache around the current index. Without this, every
// sweep step is a cold request: the server encodes a fresh JPEG and the browser
// waits on it, which is the 1-2s lag this replaces. With the server-side
// preview cache the request itself is cheap, so prefetching makes the swap
// effectively instant.
//
// These used to be DRAFT requests, and that was a mistake in both directions.
// They were the wrong SHAPE — the stage only asks for a draft mid-drag, so the
// window was ~15 renders nobody would look at — and they were the wrong COST
// trade too, since a full-res prefetch fires 13 concurrent ~700ms renders on
// page load and that flood is what a drag then queued behind (a measured 2290ms
// tick). Now that a draft URL carries uncommitted settings it is unrepeatable by
// construction, so warming drafts can never warm anything: the window is the
// committed render the sweep will genuinely request, bounded by
// PREFETCH_MAX_INFLIGHT below.
//
// The cap is the point. This used to fire 15 uncapped `new Image()` loads every
// time the current frame settled, and those compete with the frame you are
// looking at for the same connections and the same server-side render cache.
// The measured cost of one full-frame decode with a prefetch burst in flight
// went from 43-212ms to 2.6-12.6 SECONDS. Prefetching exists to make a sweep
// step instant; uncoupled it did the opposite and was the sweep's stall.
//
// AHEAD-biased because sweeping forward is the common direction, and the window
// is walked nearest-first so the next step is always in the first batch. The
// current index is excluded — see prefetchAround.
const PREFETCH_MAX_INFLIGHT = 4;
let prefetchActive = 0;
const prefetchQueue = [];

function prefetchOne(url) {
  prefetchActive++;
  const im = new Image();
  im.onload = im.onerror = () => {
    prefetchActive--;
    drainPrefetch();
  };
  im.src = url;
}

function drainPrefetch() {
  while (prefetchActive < PREFETCH_MAX_INFLIGHT && prefetchQueue.length) {
    prefetchOne(prefetchQueue.shift());
  }
}

function prefetchAround(index) {
  // NEVER the current index. The stage is already requesting that exact frame;
  // warming it duplicates the work and puts a second render of the frame the
  // user is looking at in flight, competing with it for the server's foreground
  // lock and the browser's connections. That was survivable when the prefetch
  // was a ~25ms draft; with the committed render it is a ~700ms duplicate and
  // it showed up immediately as P3 peak concurrency 2 and 83 requests for 50
  // drag steps. The stage owns the current frame. Warming is for the
  // neighbours.
  const lo = Math.max(0, index - PREFETCH_BEHIND);
  const hi = Math.min(state.items.length - 1, index + PREFETCH_AHEAD);
  const order = [];
  for (let i = index + 1; i <= hi; i++) order.push(i);
  for (let i = index - 1; i >= lo; i--) order.push(i);
  // A settings change invalidates every queued URL, so anything still waiting
  // is work nobody will look at. Drop the backlog, not just the in-flight ones.
  prefetchQueue.length = 0;
  for (const i of order) {
    const it = state.items[i];
    if (!it || it.error) continue;
    // Same function the stage uses — see frameUrl(). This used to be a second,
    // hardcoded copy of the draft URL, which is why the checkbox looked dead:
    // the stage honoured it and the prefetcher filled the cache with frames
    // from the mode you had just left.
    // draft=0, i.e. the COMMITTED render — the one a sweep step actually asks
    // for. This used to prefetch draft URLs, which is doubly wrong now: the
    // stage only requests a draft mid-drag, so each prefetch was ~15 renders
    // nobody would look at, and a draft URL now carries the client's
    // UNCOMMITTED settings, making it unrepeatable by construction. Warming
    // drafts could never have warmed anything.
    const url = frameUrl(it.id, false);
    if (prefetched.has(url)) continue;
    prefetched.add(url);
    prefetchQueue.push(url);
  }
  drainPrefetch();
}

// Point the stage at a URL and resolve when those PIXELS are on screen.
//
// Setting img.src returns synchronously; the fetch and decode happen after. That
// gap is why the preview throttle could not see its own work: it released its
// lock the instant it assigned a src, so an in-progress render looked finished
// and the next tick piled on top. Waiting for `load` is what makes the in-flight
// window real.
//
// Resolves immediately when the URL is unchanged — assigning the same src fires
// no load event, so waiting for one would hang forever. That is the common case
// for a no-op settings change and must not deadlock the queue.
function setStageSrc(url) {
  const img = $("stage");
  if (img.getAttribute("src") === url) return Promise.resolve();
  return new Promise((resolve) => {
    let done = false;
    const finish = () => {
      if (done) return;
      done = true;
      img.removeEventListener("load", finish);
      img.removeEventListener("error", finish);
      resolve();
    };
    img.addEventListener("load", finish);
    img.addEventListener("error", finish);
    img.src = url;
    // A cached image can finish decoding before the next task runs, and
    // complete/naturalWidth catches that rather than waiting forever.
    if (img.complete && img.naturalWidth > 0) finish();
  });
}

// `idx` is the frame to render. It defaults to state.current, but every caller
// that already knows which frame it means passes it explicitly — the coalescer
// below starts a render for one index while the user's thumb may have moved on,
// and a render that re-reads state.current renders whatever the state became
// rather than what was asked for.
function showCurrent(idx) {
  const i = idx === undefined
    ? state.items.findIndex(x => x.id === state.current)
    : idx;
  const it = state.items[i];
  const img = $("stage");
  if (!it || it.error) {
    img.classList.add("empty");
    $("vp-name").textContent = it ? `${it.name} — unreadable` : "—";
    $("vp-dim").textContent = "";
    return Promise.resolve();
  }
  img.classList.remove("empty");
  // Pin the element to the frame's real pixel size. The zoom maths assumes one
  // image px is one coordinate unit, so letting CSS scale this would desync
  // the ROI box and every pan/zoom delta from the pixels actually shown.
  img.style.width = it.w + "px";
  img.style.height = it.h + "px";
  // Returned, not fired-and-forgotten: callers that throttle need to know when
  // the new pixels are actually on screen.
  const shown = setStageSrc(previewUrl(it.id));
  $("vp-name").textContent = it.name;
  $("vp-dim").textContent = `${it.w}×${it.h}  ${it.bit_depth}-bit  ${it.decoder || ""}`;
  prefetchAround(state.items.findIndex(x => x.id === state.current));
  // The viewport navigates in image px, so it needs the frame's real size, and
  // the panel's histogram is a property of THIS frame.
  if (window.Viewport) {
    window.Viewport.setImageSize(it.w, it.h);
    // Restore the ROI from settings when the FRAME changes. The ROI is persisted
    // and travels with the config, so a reload or a frame switch must bring the
    // box back.
    //
    // Only on a frame change, never on every showCurrent: persisting a new ROI
    // triggers settingsChanged -> showCurrent, and if the restore ran there it
    // would read the *pre-PUT* value and immediately wipe the box the user just
    // drew. Same-frame re-renders must leave the viewport alone.
    if (roiFrame !== it.id) {
      roiFrame = it.id;
      window.Viewport.setROI(effective(it.id).roi || null, true);
    }
  }
  // isReady(), not `typeof window.Filters`: the global exists from parse time,
  // but its hooks only exist after Filters.init(), which runs after the first
  // loadLibrary(). Calling render() before then throws inside loadLibrary.
  if (window.Filters && window.Filters.isReady()) window.Filters.onImageChanged(it.id);
  return shown;
}

// ── frame navigation ───────────────────────────────────────────
// ONE way to change the current frame: slider drag, arrow keys, the transport
// buttons, the play loop, and clicking a row all come through here.
//
// The coalescer is the point. A full-res render is up to ~700ms, and the old
// slider handler fired a fresh request on a 50ms debounce, so 50 drag steps
// produced 83 requests at peak concurrency 2 — requests the user had already
// dragged past, queued behind each other. The play loop had already been fixed
// for this (it waits for the pixels before advancing) and the slider had not.
//
// So: at most ONE render in flight and ONE pending. A pending target that has
// not started is dropped, and its promise resolves false. Skip, never stack —
// the playhead falls behind a slow frame and catches up, rather than queueing
// frames the user will never look at.
let navBusy = false;
let navPending = null;

function stepTo(i, opts) {
  const n = state.items.length;
  if (!n) return Promise.resolve(false);
  i = Math.max(0, Math.min(n - 1, i | 0));
  if (navPending) {
    const dropped = navPending;
    navPending = null;
    dropped.resolve(false);
  }
  const rec = { i, resolve: null };
  const done = new Promise((res) => { rec.resolve = res; });
  navPending = rec;
  // Paint synchronously: the thumb, the label and the row highlight must track
  // the pointer at 60fps, which no await can do.
  paintStep(i, opts);
  pumpNav();
  return done;
}

function pumpNav() {
  if (navBusy || !navPending) return;
  const rec = navPending;
  navPending = null;
  navBusy = true;
  const settle = () => {
    navBusy = false;
    rec.resolve(true);
    pumpNav();
  };
  showCurrent(rec.i).then(settle, settle);
}

// The single paint. `light` skips the full list rebuild (7.8ms measured for
// ~176 rows, a quarter of the frame budget on its own) when the only thing that
// changed is which row is current.
//
// `follow` moves the SELECTION with the playhead, and defaults to FALSE. The
// two are different things: the playhead is what you are looking at, the
// selection is what you have picked out to reorder. Coupling them meant that
// selecting forty frames to drag them as a block and then pressing ▶ to look
// through the series silently threw the selection away — and the row highlight
// does not need it, because renderList marks the current row from
// state.current, not from state.selected.
function paintStep(i, opts) {
  const o = opts || {};
  const it = state.items[i];
  if (!it) return;
  state.current = it.id;
  if (o.follow) {
    state.selected.clear();
    state.selected.add(it.id);
  }
  $("sweep").value = i;
  $("vp-name").textContent = it.error ? `${it.name} — unreadable` : it.name;
  $("vp-dim").textContent = it.error ? "" : `${it.w}×${it.h}  ${it.bit_depth}-bit  ${it.decoder || ""}`;
  $("sweep-label").textContent = `${i + 1} / ${state.items.length}  ${it.name}`;
  if (o.light) markCurrentRow(it.id); else renderList();
}

function markCurrentRow(id) {
  document.querySelectorAll(".img-row").forEach(r => {
    r.classList.toggle("current", r.dataset.id === id);
  });
}

// ── drag reorder (block-aware) ─────────────────────────────
// A drag moves the WHOLE selected block, so a multi-select reorder is one
// gesture. The server persists the flattened result.
let dragIds = null;
function onDragStart(e) {
  const row = e.target.closest(".img-row");
  if (!row) return;
  const id = row.dataset.id;
  if (!state.selected.has(id)) {  // dragging an unselected row selects just it
    state.selected.clear();
    state.selected.add(id);
    renderList();
  }
  dragIds = [...state.selected];
  row.classList.add("dragging");
  e.dataTransfer.effectAllowed = "move";
  e.dataTransfer.setData("text/plain", dragIds.join(","));
}

function onDragOver(e) {
  const row = e.target.closest(".img-row");
  if (!row || !dragIds) return;
  e.preventDefault();
  const r = row.getBoundingClientRect();
  const after = (e.clientY - r.top) > r.height / 2;
  document.querySelectorAll(".drop-before,.drop-after")
    .forEach(x => x.classList.remove("drop-before", "drop-after"));
  if (dragIds.includes(row.dataset.id)) return;
  row.classList.add(after ? "drop-after" : "drop-before");
}

function onDrop(e) {
  const row = e.target.closest(".img-row");
  document.querySelectorAll(".drop-before,.drop-after")
    .forEach(x => x.classList.remove("drop-before", "drop-after"));
  if (!row || !dragIds) return;
  e.preventDefault();
  const r = row.getBoundingClientRect();
  const after = (e.clientY - r.top) > r.height / 2;
  commitMove(row.dataset.id, after, dragIds);
  dragIds = null;
}

// Move the block to just before/after the target, preserving internal order.
function commitMove(targetId, after, moving) {
  const ids = state.items.map(x => x.id);
  const block = moving.filter(id => id !== targetId);
  if (!block.length) return;
  const rest = ids.filter(id => !block.includes(id));
  const at = rest.indexOf(targetId);
  if (at < 0) return;
  rest.splice(after ? at + 1 : at, 0, ...block);
  applyOrder(rest);
}

function applyOrder(ids) {
  const pos = new Map(ids.map((id, i) => [id, i]));
  state.items.sort((a, b) => pos.get(a.id) - pos.get(b.id));
  // Re-step to the SAME image at its new index. The frame on screen is
  // unchanged, so this is a cache hit, but without it the slider thumb and the
  // playhead disagree after every reorder: index N now holds a different image,
  // and the next ▶ steps from the wrong place.
  const i = state.items.findIndex(x => x.id === state.current);
  if (i >= 0) stepTo(i);
  else renderList();
  api("POST", "/api/library/order", { ids }).catch(e => toast(e.message, "err"));
}

// Natural-sort comparator, matching app/library.py::natural_key so client and
// server agree on order. Must be a real comparator: handing .sort() an array of
// mixed string/number arrays falls back to string comparison, which puts
// IMG_10 before IMG_2 — the exact bug this avoids.
function naturalCompare(a, b) {
  const A = a.toLowerCase().split(/(\d+)/);
  const B = b.toLowerCase().split(/(\d+)/);
  for (let i = 0; i < Math.max(A.length, B.length); i++) {
    const x = A[i], y = B[i];
    if (x === undefined) return -1;
    if (y === undefined) return 1;
    const nx = /^\d+$/.test(x), ny = /^\d+$/.test(y);
    if (nx && ny) { if (+x !== +y) return +x - +y; }
    else if (x !== y) return x < y ? -1 : 1;
  }
  return 0;
}

function nudge(dir) {
  const sel = state.items.filter(x => state.selected.has(x.id)).map(x => x.id);
  if (!sel.length) return;
  const ids = state.items.map(x => x.id);
  if (dir < 0) {                       // block up
    const first = ids.indexOf(sel[0]);
    if (first <= 0) return;
    commitMove(ids[first - 1], false, sel);
  } else {                             // block down
    const last = ids.indexOf(sel[sel.length - 1]);
    if (last >= ids.length - 1) return;
    commitMove(ids[last + 1], true, sel);
  }
}

// ── sweep ───────────────────────────────────────────────────
function updateSweepLabel() {
  const it = state.items.find(x => x.id === state.current);
  const i = state.items.findIndex(x => x.id === state.current);
  $("sweep-label").textContent = it ? `${i + 1} / ${state.items.length}  ${it.name}` : "—";
}

// The playback target. 33ms is one 30Hz frame; the loop below advances on
// rAF and only when the previous frame has actually painted, so a slow frame
// costs one skipped step rather than a growing queue.
const PLAY_BUDGET_MS = 33;

// Read-only view of the coalescer, for the UI test. A stranded navPending here
// means a superseded request never resolved, which would make the next step
// silently disappear.
window.__navState = () => ({ busy: navBusy, pending: navPending ? navPending.i : null });

// Relative step, in frames. The ±1e9 used by ⏮/⏭ clamps inside stepTo.
function step(d, opts) {
  return stepTo(state.items.findIndex(x => x.id === state.current) + d, opts);
}

function togglePlay(force) {
  const want = force === undefined ? !state.playing : force;
  state.playing = want;
  $("btn-play").textContent = want ? "⏸" : "▶";
  if (state.playTimer) { cancelAnimationFrame(state.playTimer); state.playTimer = null; }
  if (!want) return;

  // rAF + "wait for the pixels" instead of setInterval.
  //
  // The old loop was setInterval(220ms), which caps playback at 4.5fps no
  // matter how fast the app is — a warm step costs 18.5ms median and 6.7ms of
  // that is synchronous work, so the 220ms was pure dead time, and the 4.5fps
  // ceiling was the single biggest reason "it doesn't look smooth".
  //
  // A timer also cannot see whether the previous frame landed. Issuing a step
  // every 33ms regardless means a frame that takes 200ms leaves 5 steps queued
  // behind it, and the queue is what turns a sweep into a freeze. Here the loop
  // advances only once stepTo() has resolved — those pixels are on screen — so a
  // slow frame is skipped, never stacked.
  //
  // The coalescer inside stepTo enforces the same rule for everything else now,
  // so playback is the one caller that is never superseded and its promise is
  // always the one it asked for. Grabbing the slider stops playback before it
  // can step.
  let last = 0;
  const frame = async (ts) => {
    if (!state.playing) return;
    if (ts - last < PLAY_BUDGET_MS) {
      state.playTimer = requestAnimationFrame(frame);
      return;
    }
    const i = state.items.findIndex(x => x.id === state.current);
    // Stop at the end rather than wrapping — a wrap looks like the fluid
    // teleported backwards, which is actively misleading in a time series.
    if (i >= state.items.length - 1) { togglePlay(false); return; }
    last = ts;
    step(1, { light: true }).then(() => {
      // Chain rather than re-arm here: the next step waits for the next rAF,
      // so timing stays on the compositor clock instead of drifting on
      // whichever promise resolved first.
      if (state.playing) state.playTimer = requestAnimationFrame(frame);
    });
  };
  state.playTimer = requestAnimationFrame(frame);
}

// ── loading ─────────────────────────────────────────────────
async function loadLibrary(selectId) {
  const d = await api("GET", "/api/library");
  const prevIds = state.items.map(x => x.id).join(",");
  state.items = d.items;
  // ids are content-addressed by path, so new uploads/scans invalidate the
  // warm cache; stale entries would pin memory for previews nobody can reach.
  if (state.items.map(x => x.id).join(",") !== prevIds) prefetched.clear();
  const s = $("sweep");
  s.max = Math.max(0, state.items.length - 1);
  s.disabled = state.items.length < 2;
  if (selectId && state.items.some(x => x.id === selectId)) {
    state.current = selectId;
    state.selected.clear();
    state.selected.add(selectId);
  } else if (!state.items.some(x => x.id === state.current)) {
    // Select the first readable frame; a corrupt file must not become the
    // default view.
    const first = state.items.find(x => !x.error) || state.items[0];
    state.current = first ? first.id : null;
    state.selected.clear();
    if (first) state.selected.add(first.id);
  }
  // Through stepTo, not showCurrent(): a reload can land while a sweep render is
  // still in flight, and two renders of different frames racing for one <img>
  // is how you end up displaying a frame the slider is not on.
  const i = state.items.findIndex(x => x.id === state.current);
  if (i >= 0) stepTo(i); else { renderList(); showCurrent(); updateSweepLabel(); }
  // Deliberately no warm on load either. A fresh session has an empty cache, so
  // the first sweep is slower — but kicking off ~150 full-resolution renders in
  // the background the moment the page opens means every frame you touch while
  // they run competes with them. "Render all" is one click away, and doing it
  // deliberately (once the look is settled) is strictly better than doing it
  // eagerly on every page load and every settings change.
}

const CONFIG_NAME = "fluoro-editor.config.json";
const UPLOAD_CONCURRENCY = 4;

// Upload with bounded concurrency. A folder can be hundreds of 24MP frames;
// firing them all at once starves the API and stalls the browser, while serial
// uploads waste the connection.
async function uploadFiles(files, { onProgress } = {}) {
  const list = [...files].filter(f => f.type.startsWith("image/"));
  if (!list.length) return { ok: 0, failed: 0 };

  let ok = 0, failed = 0, next = 0;
  const worker = async () => {
    while (next < list.length) {
      const f = list[next++];
      const fd = new FormData();
      fd.append("file", f);
      try {
        const r = await fetch("/api/library/upload", { method: "POST", body: fd });
        if (!r.ok) throw new Error((await r.json().catch(() => ({}))).detail || r.status);
        ok++;
      } catch (e) {
        failed++;
        toast(`${f.name}: ${e.message}`, "err");
      }
      onProgress?.(ok + failed, list.length);
    }
  };
  await Promise.all(Array.from({ length: UPLOAD_CONCURRENCY }, worker));
  return { ok, failed };
}

// A folder drop or pick: upload every image, and if the folder happens to hold
// a saved config, apply it too. The config is parsed in the browser and posted
// inline — no need to round-trip the file through the uploads dir.
async function loadFolder(fileList) {
  const all = [...fileList];
  const cfg = all.find(f => f.name === CONFIG_NAME);
  const imgs = all.filter(f => f.type.startsWith("image/"));
  if (!imgs.length) { toast("no images in that folder", "err"); return; }

  toast(`loading ${imgs.length} file(s)…`);
  const r = await uploadFiles(imgs, {
    onProgress: (n, total) => { $("sweep-label").textContent = `loading ${n}/${total}`; },
  });
  toast(`loaded ${r.ok} image(s)${r.failed ? `, ${r.failed} failed` : ""}`, r.failed ? "" : "ok");
  await loadLibrary();

  if (cfg) {
    try {
      const payload = JSON.parse(await cfg.text());
      const res = await api("POST", "/api/config/load", { config: payload, replace: true });
      await loadSettings();
      settingsChanged();
      toast(`config applied from ${cfg.name} (${res.images} per-image)`, "ok");
    } catch (e) {
      toast(`config in folder is unreadable: ${e.message}`, "err");
    }
  }
  await refreshConfigHint();
}

async function refreshConfigHint() {
  try {
    const d = await api("GET", "/api/config");
    const b = $("btn-load-cfg");
    b.textContent = d.exists ? "Load config ✓" : "Load config";
    b.title = d.exists
      ? `Load ${d.path}`
      : `no ${d.name} found in ${d.searched.join(" or ")}`;
  } catch { /* hint is cosmetic; never block on it */ }
}

// ── init ────────────────────────────────────────────────────
function init() {
  const ul = $("img-list");

  ul.addEventListener("click", async (e) => {
    // The remove button lives inside the row, so it must be handled before
    // the row's own select handler or removing an image also selects it.
    const rm = e.target.closest("[data-rm]");
    if (rm) {
      e.stopPropagation();
      const id = rm.dataset.rm;
      const it = state.items.find(x => x.id === id);
      try {
        await api("POST", `/api/library/remove/${id}`);
        prefetched.delete(previewUrl(id));
        state.items = state.items.filter(x => x.id !== id);
        if (state.current === id) {
          state.current = state.items[0]?.id ?? null;
          state.selected.clear();
          if (state.current) state.selected.add(state.current);
        }
        state.selected.delete(id);
        $("sweep").max = Math.max(0, state.items.length - 1);
        $("sweep").disabled = state.items.length < 2;
        // The removed row was one of possibly many selected; stepping must not
        // disturb what is left of the user's selection.
        const i = state.items.findIndex(x => x.id === state.current);
        if (i >= 0) stepTo(i);
        else { renderList(); showCurrent(); updateSweepLabel(); }
        toast(`removed ${it?.name || "image"} from the list (file kept)`, "ok");
      } catch (err) { toast(`remove failed: ${err.message}`, "err"); }
      return;
    }
    const row = e.target.closest(".img-row");
    if (row) selectRow(row.dataset.id, e);
  });
  ul.addEventListener("dragstart", onDragStart);
  ul.addEventListener("dragover", onDragOver);
  ul.addEventListener("drop", onDrop);
  ul.addEventListener("dragend", () => {
    dragIds = null;
    document.querySelectorAll(".dragging")
      .forEach(x => x.classList.remove("dragging"));
  });

  $("thumbs-toggle").addEventListener("change", (e) => {
    $("img-list").classList.toggle("no-thumbs", !e.target.checked);
  });

  $("btn-sort").addEventListener("click", () => {
    const ids = state.items.map(x => x.id);
    ids.sort((a, b) => naturalCompare(
      state.items.find(x => x.id === a).name,
      state.items.find(x => x.id === b).name));
    applyOrder(ids);
    toast("re-sorted by name", "ok");
  });

  $("btn-scan").addEventListener("click", async () => {
    await api("POST", "/api/library/scan");
    await loadLibrary();
    toast(`rescanned — ${state.items.length} images`, "ok");
  });

  $("btn-render-all").addEventListener("click", async () => {
    if (!state.items.length) return;
    // Confirm because it is expensive by design: ~150 full-resolution renders.
    // Someone who reaches for this by accident should find out what it costs
    // before it starts, not halfway through.
    if (!window.confirm(
      `Render all ${state.items.length} images with the current filters?\n\n` +
      `This makes sweeping through the series instant. It takes a while, and ` +
      `you can change any filter while it runs — doing so cancels it.`)) return;
    startWarm();
  });

  $("file-input").addEventListener("change", (e) => {
    uploadFiles(e.target.files);
    e.target.value = "";
  });

  $("dir-input").addEventListener("change", (e) => {
    loadFolder(e.target.files);
    e.target.value = "";
  });

  $("btn-clear").addEventListener("click", async () => {
    const n = state.items.length;
    if (!n) { toast("list is already empty"); return; }
    // Not `confirm()` on a promise chain — just be explicit in the toast that
    // the files survive, since the images dir is a mount of real data.
    if (!window.confirm(
      `Remove all ${n} image(s) from the list?\n\n` +
      `Files on disk are NOT deleted — this only empties the list.`)) return;
    try {
      await api("POST", "/api/library/clear");
      state.items = [];
      state.current = null;
      state.selected.clear();
      prefetched.clear();
      renderList();
      $("stage").classList.add("empty");
      $("sweep").max = 0;
      $("sweep").value = 0;
      $("sweep").disabled = true;
      $("vp-name").textContent = "—";
      $("vp-dim").textContent = "";
      $("sweep-label").textContent = "—";
      toast(`cleared ${n} image(s) from the list (files untouched)`, "ok");
    } catch (e) { toast(`clear failed: ${e.message}`, "err"); }
  });

  $("btn-save-cfg").addEventListener("click", async () => {
    try {
      const r = await api("POST", "/api/config/save", {});
      toast(`config saved → ${r.path} (${r.images} per-image)`, "ok");
      await refreshConfigHint();
    } catch (e) { toast(`save failed: ${e.message}`, "err"); }
  });

  $("btn-load-cfg").addEventListener("click", async () => {
    try {
      const r = await api("POST", "/api/config/load", {});
      await loadSettings();
      settingsChanged();
      toast(`config loaded from ${r.path} (${r.images} per-image)`, "ok");
    } catch (e) {
      toast(e.message.includes("404") ? `no saved config found — use Save config first` : e.message, "err");
    }
  });

  // Drop anywhere in the window. dragover must preventDefault or the browser
  // navigates away and the whole session's order is lost.
  let dragDepth = 0;
  window.addEventListener("dragenter", (e) => {
    if (![...e.dataTransfer.types].includes("Files")) return;
    dragDepth++;
    document.body.classList.add("dragging");
  });
  window.addEventListener("dragover", (e) => {
    if ([...e.dataTransfer.types].includes("Files")) e.preventDefault();
  });
  window.addEventListener("dragleave", () => {
    if (--dragDepth <= 0) { dragDepth = 0; document.body.classList.remove("dragging"); }
  });
  window.addEventListener("drop", (e) => {
    if (![...e.dataTransfer.types].includes("Files")) return;
    e.preventDefault();
    dragDepth = 0;
    document.body.classList.remove("dragging");
    // Route every window drop through loadFolder: it handles plain image drops
    // too, and additionally picks up a config sitting in a dropped folder.
    loadFolder(e.dataTransfer.files);
  });

  // The slider is the ONE navigation control that fires continuously, so it is
  // the one that needs the coalescer. paintStep tracks the thumb, the label and
  // the row highlight synchronously at pointer speed; the coalescer then keeps
  // at most one render in flight and drops the frames the thumb has already
  // left. No trailing debounce — that is what used to hold the picture still
  // until the drag stopped.
  $("sweep").addEventListener("input", (e) => {
    togglePlay(false);
    stepTo(+e.target.value, { light: true });
  });
  // `change` (pointerup, or a keyboard step) repaints fully. Same index, so
  // same URL, so setStageSrc resolves without a request — this is a row
  // repaint, not a re-render.
  $("sweep").addEventListener("change", (e) => {
    stepTo(+e.target.value);
  });
  $("btn-play").addEventListener("click", () => togglePlay());
  // The "filters" checkbox. Re-point the stage and rebuild the prefetch window:
  // every cached URL is now the wrong one, so the old backlog has to go or the
  // sweep would keep arriving at frames from the mode you just left.
  $("filters-on").addEventListener("change", (e) => {
    state.filtersOn = !!e.target.checked;
    prefetched.clear();
    prefetchQueue.length = 0;
    const i = state.items.findIndex(x => x.id === state.current);
    if (i >= 0) stepTo(i, { light: true });
  });
  $("btn-prev").addEventListener("click", () => { togglePlay(false); step(-1); });
  $("btn-next").addEventListener("click", () => { togglePlay(false); step(1); });
  $("btn-first").addEventListener("click", () => { togglePlay(false); step(-1e9); });
  $("btn-last").addEventListener("click", () => { togglePlay(false); step(1e9); });

  // ←/→ step, space play, Alt+↑/↓ nudge the selected block. Ignored while
  // typing in an input so slider nudges in the filter panel still work.
  document.addEventListener("keydown", (e) => {
    const t = e.target;
    if (t && (t.tagName === "INPUT" || t.tagName === "TEXTAREA" || t.tagName === "SELECT")) {
      if (e.key !== " ") return;
    }
    if (e.altKey && (e.key === "ArrowUp" || e.key === "ArrowDown")) {
      e.preventDefault();
      nudge(e.key === "ArrowUp" ? -1 : 1);
      return;
    }
    if (e.key === "ArrowRight") { e.preventDefault(); togglePlay(false); step(1); }
    else if (e.key === "ArrowLeft") { e.preventDefault(); togglePlay(false); step(-1); }
    else if (e.key === " ") { e.preventDefault(); togglePlay(); }
  });

  $("stage").addEventListener("load", () => { /* keep the overlay glued to the image */ });

  // ── viewport navigation + ROI ─────────────────────────────
  window.Viewport.init($("viewport"), $("zoomer"), $("roi-box"));
  window.Viewport.onViewChange((z) => { $("vp-zoom").textContent = z.toFixed(1) + "×"; });
  window.Viewport.onROIChange((r) => {
    // Persist the ROI as part of the active scope's settings so it round-trips
    // through save/load config like everything else.
    //
    // Drawing an ROI also switches region to "roi", and clearing switches back
    // to "frame". Without this, drawing a box appeared to do nothing whenever
    // region was still "frame" — the box showed, the histogram said "inside
    // ROI", and the render was untouched. That mismatch is exactly the "ROI
    // mode doesn't work" report, and it is a trap rather than a feature: the
    // intent behind drawing a box is always to confine the work to it.
    const patch = { roi: r, region: r ? "roi" : "frame" };
    const live = window.Filters && window.Filters.isReady();
    if (live) window.Filters.syncRoiInfo();
    // saveSettings adopts the server's response into state.global/state.local
    // BEFORE we re-render. Discarding it (as this path used to) leaves the
    // settings key unchanged, so previewUrl() returns a byte-identical URL and
    // the browser hands back the previous image from cache — the ROI is saved
    // on the server and invisible on screen until some other control re-reads.
    saveSettings(patch)
      .then(() => {
        settingsChanged();
        // The region and ROI are both part of the bins' key, so force a refetch.
        if (live) { window.Filters.render(); window.Filters.loadBins(true); }
      })
      .catch(() => {});
  });
  $("roi-mode").addEventListener("click", () => {
    const on = !window.Viewport.isROIMode();
    window.Viewport.setROIMode(on);
    $("roi-mode").classList.toggle("accent", on);
  });

  // ── filter panel ─────────────────────────────────────────
  // Initialised AFTER loadSettings: rendering before the settings arrive would
  // emit one control per schema param with value="undefined", which the browser
  // rejects and the user sees as a flash of dead sliders.
  const filterHooks = {
    getSchema: () => api("GET", "/api/filters/schema"),
    getState: () => state,
    getCurrentId: currentId,
    getCurrentItem: () => state.items.find(x => x.id === state.current),
    // The panel needs the MERGED settings for the current frame, not the raw
    // global or local dict: in local scope it shows what this image renders
    // with, which is global+override. A missing key would read as "default"
    // and quietly show the wrong numbers.
    effective: (id) => (id ? effective(id) : null),
    toast,
    // Drag feedback renders the downscaled draft; the commit that follows
    // requests the full-resolution frame.
    setDraft: setDraftMode,
    // Flag only, no render. Filters calls this instead of setDraft so the draft
    // switch happens INSIDE its own coalescing lock: setDraft re-renders the
    // stage immediately, and doing that outside the lock issued a second,
    // untracked request for the same frame (peak concurrency 2 on a drag).
    setDraftFlag: (v) => { draftMode = !!v; },
    loadBins: (id) => api("GET", `/api/histogram/${id}`),
    // light=true: this is the coalesced preview tick, not a committed change.
    refresh: () => settingsChanged(true),
    getROI: () => window.Viewport.getROI(),
    setROI: (r, silent) => window.Viewport.setROI(r, silent),
    // Drop a local override at a dotted path ("channels.r.ops.clahe"). The
    // response carries the whole local map, so replace our copy wholesale
    // rather than trying to patch the one key we removed.
    resetSection: (path) => {
      const id = currentId();
      return api("PUT",
        `/api/settings/local/${id}?reset=${encodeURIComponent(path)}`)
        .then((d) => { state.local[id] = d.local || {}; });
    },
    save: (patch) => saveSettings(patch),
  };

  loadSettings()
    .then(() => loadLibrary())
    .then(() => window.Filters.init(filterHooks))
    .catch(e => toast(`load failed: ${e.message}`, "err"));
  refreshConfigHint();
}

function currentId() { return state.current; }

document.addEventListener("DOMContentLoaded", init);


