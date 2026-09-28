/* fluoro-editor — the filter panel.
 *
 * Three levels, matching how you actually reason about a fluorescence frame:
 *
 *   REGION   whole frame | ROI          gates every operation
 *   CHANNEL  R | G | B                 each has amount 0..1 + blend mode
 *   OPS      threshold/gamma/clahe/log/curve, in a drag-sortable order
 *
 * Only the selected channel's operations are shown. The previous layout showed
 * every parameter of every filter at once, each duplicated per channel — a
 * 3x-multiplied slider grid where almost nothing was relevant to the channel you
 * were actually working on.
 *
 * Controls are generated from GET /api/filters/schema, so adding an operation
 * is a change in filters.py alone.
 */
window.Filters = (() => {
  "use strict";
  const $ = (id) => document.getElementById(id);

  let ops = [];              // [{key,label,params}] from the server
  let blends = ["normal", "difference"];
  let scope = "global";      // global | local
  let channel = "r";         // which channel's ops are on screen
  let hooks = {};
  let ready = false;         // true only once init() has supplied hooks
  let collapsed = new Set();
  let saveTimer = null;

  // ── state access ──────────────────────────────────────────
  // view() is the settings the CURRENT frame renders with (global + its local
  // override); bag() is the dict the active scope WRITES into. They are
  // different objects on purpose: reading what is displayed must not imply you
  // are editing the thing you are reading.
  function view() {
    const s = hooks.getState();
    if (scope === "global") return s.global;
    const id = hooks.getCurrentId();
    if (!id) return s.global;
    // effective() is a HOOK, not a field of the state object. Calling
    // s.effective(id) threw "s.effective is not a function", and because
    // renderRegion() calls view() first, that aborted render() before
    // renderScopeTarget() — leaving the scope button lit but its caption stale.
    return (hooks.effective && hooks.effective(id)) || s.global;
  }

  function bag() {
    const s = hooks.getState();
    return scope === "global" ? s.global : (s.local[hooks.getCurrentId()] ||= {});
  }

  function chanConf() {
    return view().channels?.[channel] || {};
  }

  function isInherited() {
    if (scope === "global") return false;
    const id = hooks.getCurrentId();
    const loc = hooks.getState().local[id];
    return !(loc && loc.channels && loc.channels[channel]);
  }

  // ── render ────────────────────────────────────────────────
  function render() {
    if (!ready) return;
    renderRegion();
    renderChannels();
    renderOrder();
    renderOps();
    renderScopeTarget();
    syncCurve();
    syncHistogram();
    syncRoiInfo();
    // renderOps() rebuilt the cards, so #hist-scope is a fresh empty node.
    // Repaint it from the last fetch — otherwise any re-render blanks the
    // region caption and the user reads "whole frame" as a stale value.
    if (binsLabel) paintHistScope(binsLabel);
    // The panel now describes THIS frame, so record it. Seeding the id here is
    // what makes the onImageChanged() guard effective from the very first
    // preview tick rather than the second: the panel is built before any drag
    // starts, and without this the first tick still found a null id, rebuilt,
    // and detached the <input> out from under the user's thumb. One rebuild is
    // enough to end a drag — the browser's pointer capture does not survive the
    // element being replaced.
    shownFor = hooks.getCurrentId() ?? null;
  }

  function renderRegion() {
    const r = view().region || "frame";
    document.querySelectorAll("#region-seg button").forEach(b =>
      b.classList.toggle("on", b.dataset.region === r));
    const has = !!view().roi;
    $("roi-clear").disabled = !has;
    // Spell out the CURRENT mapping, which is the opposite of what it used to
    // say: a plain drag pans the image, shift is what moves or resizes the box,
    // and ctrl draws a new one. The old text promised a plain drag inside the
    // box would move it, which is exactly the behaviour that was removed.
    $("roi-hint").textContent = has
      ? "drag = pan · shift+drag inside = move · shift+handle = resize · ctrl+drag = new"
      : "no ROI drawn — ctrl+drag on the image, or use ROI mode";
  }

  function renderScopeTarget() {
    const t = $("scope-target");
    if (!t) return;
    const it = hooks.getCurrentItem && hooks.getCurrentItem();
    t.textContent = scope === "global"
      ? "applies to all images"
      : `applies to ${it ? it.name : "the current image"} only`;
  }

  function renderChannels() {
    const chans = view().channels || {};
    const host = $("chan-tabs");
    host.innerHTML = "";
    for (const c of ["r", "g", "b"]) {
      const conf = chans[c] || {};
      const b = document.createElement("button");
      b.dataset.ch = c;
      b.className = "chan-tab" + (c === channel ? " on" : "");
      // The swatch shows the channel's colour; the bar shows its contribution,
      // so "which channel am I looking at" and "how much is it contributing"
      // are answerable without reading a number.
      b.innerHTML = `<span class="sw sw-${c}"></span>${c.toUpperCase()}
        <span class="cbar"><i style="width:${Math.round((conf.amount ?? 1) * 100)}%"></i></span>`;
      // loadBins, not just render(): the histogram is per-channel, so switching
      // tabs must pull that channel's bins, not relabel the previous one's.
      b.addEventListener("click", () => { channel = c; render(); loadBins(); });
      host.appendChild(b);
    }
    const conf = chanConf();
    const amt = $("amount");
    amt.value = conf.amount ?? 1;
    $("amount-val").textContent = (+(conf.amount ?? 1)).toFixed(2);
    $("blend").value = conf.blend || "normal";
    // amount=0 means the channel is out entirely; say so rather than leaving
    // the user to wonder why their sliders do nothing.
    $("chan-note").textContent = (conf.amount ?? 1) <= 0
      ? "contributes nothing — drag amount up to include it"
      : (scope === "local" && isInherited() ? "inherited from global" : "");
    $("chan-body").classList.toggle("off", (conf.amount ?? 1) <= 0);
  }
  // ── the operation stack, for the selected channel only ───
  function renderOrder() {
    const host = $("chan-order");
    const order = view().op_order || [];
    const opsOf = chanConf().ops || {};
    host.innerHTML = "";
    order.forEach((key, i) => {
      const spec = ops.find(o => o.key === key);
      if (!spec) return;
      const on = !!opsOf[key]?.enabled;
      const li = document.createElement("li");
      li.className = "fitem" + (on ? " on" : "");
      li.draggable = true;
      li.dataset.op = key;
      li.dataset.idx = i;
      li.innerHTML = `<span class="fnum">${i + 1}</span>
        <span class="fnm">${spec.label}</span>
        <span class="fst">${on ? "on" : "off"}</span>`;
      li.addEventListener("click", () => {
        // No existence guard. The old `if (!ops_) return;` was the same silent
        // bail as queuePreview's: in LOCAL scope the bag is {} until the first
        // commit, so clicking an op in the order list did nothing at all until
        // some other control had written to this channel.
        commit({ channels: { [channel]: { ops: { [key]: { enabled: !on } } } } });
      });
      host.appendChild(li);
    });
    bindOrderDnD();
  }

  function bindOrderDnD() {
    const host = $("chan-order");
    let dragKey = null;
    host.querySelectorAll(".fitem").forEach(li => {
      li.addEventListener("dragstart", (e) => {
        dragKey = li.dataset.op;
        li.classList.add("dragging");
        e.dataTransfer.effectAllowed = "move";
        // Firefox/Chrome both need data set for the drag to start at all.
        e.dataTransfer.setData("text/plain", dragKey);
      });
      li.addEventListener("dragend", () => li.classList.remove("dragging"));
      li.addEventListener("dragover", (e) => {
        e.preventDefault();
        e.dataTransfer.dropEffect = "move";
      });
      li.addEventListener("drop", (e) => {
        e.preventDefault();
        e.stopPropagation();
        if (!dragKey || dragKey === li.dataset.op) return;
        const order = [...(view().op_order || [])];
        const from = order.indexOf(dragKey);
        if (from < 0) return;
        order.splice(from, 1);
        order.splice(order.indexOf(li.dataset.op), 0, dragKey);
        commit({ op_order: order });
      });
    });
  }

  // The cards for the SELECTED channel only.
  function renderOps() {
    const host = $("chan-ops");
    const opsOf = chanConf().ops || {};
    const order = view().op_order || [];
    // Whether THIS operation has a local override — per operation, not per
    // channel. A channel-scoped check is wrong: enabling log on R would then
    // mark threshold and CLAHE as "explicitly set" too, when they are still
    // inherited, and the dimming stops meaning anything.
    const loc = scope === "local"
      ? (hooks.getState().local[hooks.getCurrentId()] || {}).channels?.[channel]?.ops
      : null;
    const overridden = (k) => !!(loc && loc[k]);
    host.innerHTML = "";
    for (const key of order) {
      const spec = ops.find(o => o.key === key);
      if (!spec) continue;
      const conf = opsOf[key] || {};
      const on = !!conf.enabled;
      const inh = scope === "local" && !overridden(key);
      const sec = document.createElement("div");
      sec.className = "fcard" + (on ? " on" : "") + (inh ? " inh" : "");
      sec.dataset.op = key;
      sec.innerHTML = `
        <div class="fhead">
          <label class="fen"><input type="checkbox" class="fen-b"
            ${on ? "checked" : ""}><strong>${spec.label}</strong></label>
          ${inh ? '<span class="tag inh">inh</span>' : ""}
          <span class="grow"></span>
          ${inh ? "" : '<button class="mini-btn rst" title="Reset this operation">reset</button>'}
          <button class="mini-btn car" title="Collapse">−</button>
        </div>
        <div class="fbody" ${collapsed.has(key) ? "hidden" : ""}>
          ${spec.params.filter(p => p.type !== "points")
              .map(p => paramRow(key, p, conf[p.key])).join("")}
          ${key === "histogram" ? '<canvas class="hist-canvas" id="hist-canvas" width="600" height="150"></canvas><div class="tiny subtle" id="hist-scope"></div>' : ""}
          ${key === "curve" ? '<canvas class="curve-canvas" id="curve-canvas" width="600" height="260"></canvas>' : ""}
        </div>`;
      host.appendChild(sec);
    }
    bindCards();
  }

  function paramRow(opKey, p, v) {
    // Fall back to the schema default: a config written by an older version can
    // lack a param, and `value="undefined"` is silently dropped by the browser,
    // leaving the control reading 0 as if it were real data.
    if (v === undefined || v === null) v = p.default;
    const id = `p-${opKey}-${p.key}`;
    let ctrl;
    if (p.type === "choice") {
      ctrl = `<select id="${id}" data-op="${opKey}" data-p="${p.key}"
        data-type="${p.type}">${p.options.map(o =>
          `<option ${o === v ? "selected" : ""}>${o}</option>`).join("")}</select>`;
    } else {
      ctrl = `<input type="range" id="${id}" data-op="${opKey}" data-p="${p.key}"
        data-type="${p.type}" min="${p.min}" max="${p.max}" step="${p.step}" value="${v}">
        <input type="number" class="fnum-in" data-op="${opKey}" data-p="${p.key}"
          data-type="${p.type}" min="${p.min}" max="${p.max}" step="${p.step}" value="${v}">`;
    }
    return `<div class="frow"><label class="fl" for="${id}">${p.label}</label>${ctrl}</div>`;
  }
  function bindCards() {
    const host = $("chan-ops");

    host.querySelectorAll(".fcard").forEach(sec => {
      const key = sec.dataset.op;
      sec.querySelector(".car").addEventListener("click", (e) => {
        const b = sec.querySelector(".fbody");
        const show = b.hasAttribute("hidden");
        b.toggleAttribute("hidden", !show);
        e.currentTarget.textContent = show ? "−" : "+";
        if (show) collapsed.delete(key); else collapsed.add(key);
        if (show) { syncCurve(); syncHistogram(); }
      });
      // The reset button is omitted on inherited cards, so this can be null —
      // binding unconditionally would throw on every local-scope render.
      const rst = sec.querySelector(".rst");
      if (rst) rst.addEventListener("click", () => {
        // Drop the override, do NOT write defaults over it. Writing defaults
        // leaves the card permanently flagged as locally set, so "reset" never
        // actually returned the operation to inheriting from global.
        const path = `channels.${channel}.ops.${key}`;
        hooks.resetSection(path)
          .then(() => { render(); hooks.refresh(); })
          .catch((e) => hooks.toast(`reset failed: ${e.message}`, "err"));
      });
      const cb = sec.querySelector(".fen-b");
      cb.addEventListener("change", () =>
        commit({ channels: { [channel]: { ops: { [key]: { enabled: cb.checked } } } } }));
    });

    // Sliders preview live while dragging, then persist once on release — a
    // full-res render per input event would make a drag unusable.
    host.querySelectorAll('input[type="range"]').forEach(inp => {
      const num = host.querySelector(
        `.fnum-in[data-op="${inp.dataset.op}"][data-p="${inp.dataset.p}"]`);
      inp.addEventListener("input", () => {
        if (num) num.value = inp.value;
        queuePreview(inp.dataset.op, inp.dataset.p, coerce(inp));
        // The min/max NUMBER sliders drive the same values as the canvas
        // handles, so they have to move the canvas too. setRanges is otherwise
        // reached only from syncHistogram() <- render(), i.e. only on mouseup:
        // the number inputs would show 65/156 while the markers stayed at the
        // old ~130/250 and the out-of-range shading showed the discarded
        // region as if it were still being kept. The canvas handles drag
        // correctly on their own (pointermove -> draw); these sliders were the
        // orphaned half of the pair.
        //
        // Deliberately NOT render() — rebuilding the panel mid-drag detaches
        // the <input> under the user's thumb. This is a canvas redraw only.
        if (inp.dataset.op === "histogram") {
          const h = (chanConf().ops || {}).histogram || {};
          window.Histogram.setRanges([coerce(inp), h.max ?? 255]);
        }
      });
      inp.addEventListener("change", () => commitOp(inp.dataset.op, inp.dataset.p, coerce(inp)));
    });
    host.querySelectorAll(".fnum-in").forEach(inp => {
      inp.addEventListener("change", () => {
        const rng = host.querySelector(
          `input[type="range"][data-op="${inp.dataset.op}"][data-p="${inp.dataset.p}"]`);
        if (rng) rng.value = inp.value;
        commitOp(inp.dataset.op, inp.dataset.p, coerce(inp));
      });
    });
    host.querySelectorAll("select").forEach(sel => {
      sel.addEventListener("change", () =>
        commitOp(sel.dataset.op, sel.dataset.p, sel.value));
    });
  }

  // Int params must persist as ints; a float 200.0 takes a different path
  // through the pipeline than 200.
  function coerce(inp) {
    const t = inp.dataset.type;
    if (t === "int") return Math.round(+inp.value);
    if (t === "float") return +(+inp.value).toFixed(4);
    return inp.value;
  }

  // Live drag feedback. The first request of a gesture goes out IMMEDIATELY as a
  // downscaled draft render, and the rest coalesce. The old code waited 120ms
  // and then asked for a FULL-res render, so a drag cost one 700ms render per
  // 120ms window and always trailed the pointer.
  //
  // 16ms = one 60Hz frame. This is the floor on how quickly the picture can
  // change, and it is what the "slider tick" latency number actually measures:
  // moving the slider to seeing new pixels is (throttle + render). A draft tick
  // measures ~4.6ms end-to-end (GPU) / ~5ms (CPU), so a 40ms throttle was
  // spending 8x longer waiting than working. Anything below ~16ms just queues
  // requests the display cannot show.
  // LATEST-WINS, ONE IN FLIGHT.
  //
  // The old version stamped previewLast when the timer FIRED and had no idea
  // whether a request was still running. Each draft takes ~30ms but the
  // throttle only waits 16ms, so a fast drag could emit a request per step and
  // a slow one built a real queue; the final full-res render from mouseup then
  // landed BEHIND the stale drafts. That is the reported symptom: nothing
  // appears to change until you let go, and then it catches up all at once.
  //
  // The fix is not a longer throttle, it is to stop queueing. One request
  // outstanding at a time; a tick arriving while one is in flight sets a
  // pending flag and re-issues when it lands. Intermediate positions are
  // dropped, which is the right trade for a drag: the newest position is the
  // one worth showing, and each render reads current state rather than
  // replaying a backlog.
  const PREVIEW_THROTTLE_MS = 16;
  let previewPending = null, previewLast = 0;
  let previewBusy = false;      // a refresh is in flight
  let previewQueued = false;    // state changed while one was in flight
  let previewSeq = 0;           // counts requests issued, for tests
  let commitWaiters = [];       // commits waiting for the in-flight preview

  function requestPreview() {
    // Entering draft mode paints once straight away, so the very first movement
    // of a drag is already on screen rather than waiting for the throttle.
    //
    // That immediate paint used to be an UNTRACKED request: setDraft(true)
    // re-renders the stage itself, entirely outside the previewBusy lock, and
    // then issuePreview takes the lock and issues a second one. Two renders of
    // the same frame in flight — the exact thing this whole mechanism exists to
    // prevent, and the reason a drag could still show peak concurrency 2 after
    // the coalescer was in place.
    //
    // The draft flag is now set WITHOUT the side effect, and the lock-holding
    // issuePreview below does the painting. The first movement is still on
    // screen within the throttle window; it is simply counted properly.
    if (hooks.setDraftFlag) hooks.setDraftFlag(true);
    else if (hooks.setDraft) hooks.setDraft(true);

    // In flight: do not start a second one. Remember that we owe a refresh and
    // let the completion handler issue it. This is the whole fix.
    if (previewBusy) { previewQueued = true; return; }

    const wait = Math.max(0, PREVIEW_THROTTLE_MS - (performance.now() - previewLast));
    clearTimeout(previewPending);
    previewPending = setTimeout(issuePreview, wait);
  }

  // Issue at most one refresh, and arrange for exactly one more if state moved
  // on while it was running.
  function issuePreview() {
    previewPending = null;
    if (previewBusy) { previewQueued = true; return; }
    previewBusy = true;
    previewQueued = false;
    previewSeq++;
    let ret;
    try {
      ret = hooks.refresh();
    } catch (e) {
      previewBusy = false;
      throw e;
    }
    // refresh() is async and resolves when the new PIXELS are on screen, not
    // when the request was dispatched. Settle on either outcome, but always
    // release the lock.
    Promise.resolve(ret).then(previewSettled, previewSettled);
  }

  function previewSettled() {
    previewBusy = false;
    // Stamp on COMPLETION, not dispatch. Stamping on dispatch is what let the
    // throttle believe it had a free slot while a render was still running.
    previewLast = performance.now();
    // Commit waiters drain FIRST. A commit is the full-resolution render the
    // user is waiting on; a queued draft is an intermediate position that is
    // about to be superseded anyway. Issuing the draft first meant the commit
    // queued behind it, and if another tick arrived in between, the commit's
    // refresh and that tick's draft overlapped again.
    if (commitWaiters.length) { commitWaiters.shift()(); return; }
    if (previewQueued) { previewQueued = false; requestPreview(); }
  }

  // Run fn once no preview is in flight, and HOLD the lock while it runs.
  //
  // The mouseup commit used to call hooks.refresh() directly, which BYPASSED
  // this lock entirely — so the full-resolution render raced the last draft.
  //
  // The fix at this layer was only half a fix: `afterPreview` waited for an
  // in-flight draft, but the commit's own refresh never set previewBusy. A
  // slider tick arriving DURING the commit's full render therefore saw a free
  // slot and issued a second request — peak concurrency 2, which the suite
  // caught. Waiting for the lock is not the same as holding it: this has to
  // take the lock too, or the very next tick walks straight past it.
  function afterPreview(fn) {
    if (!previewBusy) {
      previewBusy = true;
      let ret;
      try {
        ret = fn();
      } catch (e) {
        previewBusy = false;
        throw e;
      }
      Promise.resolve(ret).then(previewSettled, previewSettled);
      return;
    }
    commitWaiters.push(fn);
  }

  // Materialise the path bag().channels[channel].ops[opKey][pKey] in the dict
  // we are writing into, creating the levels as needed.
  //
  // This is the fix for "the filter never updates until I let go of the slider".
  // queuePreview() used to bail out when the path did not exist yet:
  //
  //     const ops_ = bag().channels?.[channel]?.ops;
  //     if (!ops_ || !ops_[opKey]) return;      // silent bail
  //
  // In LOCAL scope bag() is state.local[id], which is {} until the first commit.
  // So channels was undefined, the guard returned, and the entire drag issued
  // ZERO previews. Releasing the slider ran commitOp, which builds the nested
  // shape from scratch, and that is the only moment anything appeared. Measured
  // on the same gesture and the same frame, distinct stage.src while the mouse
  // was held down:
  //
  //     global  19        local   1
  //
  // The bug hid behind the obvious probe too: grab the `amount` slider in GLOBAL
  // scope, where bag() is state.global and the path always exists, and it looks
  // perfectly healthy. The right probe is the slider the user named, in the scope
  // they are using.
  //
  // commitOp() deliberately does NOT go through here — it sends a patch the
  // server deep-merges, and sending the full local bag instead would turn a
  // one-field edit into a write of every inherited value.
  function ensureOpPath(opKey) {
    const b = bag();
    const chans = (b.channels ||= {});
    const ch = (chans[channel] ||= {});
    const ops = (ch.ops ||= {});
    return (ops[opKey] ||= {});
  }

  // Live preview WITHOUT persisting: patch the in-memory state and re-render.
  // The release handler writes it for real.
  function queuePreview(opKey, pKey, val) {
    ensureOpPath(opKey)[pKey] = val;
    requestPreview();
  }

  function commitOp(opKey, pKey, val) {
    commit({ channels: { [channel]: { ops: { [opKey]: { [pKey]: val } } } } });
  }

  // Returns the save promise so callers can chain follow-up work (e.g. force a
  // histogram refetch after a region change) without duplicating the error path.
  function commit(patch) {
    return hooks.save(patch)
      .then(() => {
        // Leaving draft mode is what brings the full-resolution frame back, so it
        // has to happen before the refresh, not after.
        //
        // Flag only, no side effect: hooks.setDraft(false) re-renders the stage
        // on the spot, which is a request outside the lock. The refresh below is
        // the one that should paint, and it goes through the gate.
        if (hooks.setDraftFlag) hooks.setDraftFlag(false);
        else if (hooks.setDraft) hooks.setDraft(false);
        render();
        // Through the gate, not straight to hooks.refresh(): the last drag draft
        // is usually still in flight at mouseup, and issuing the full render
        // alongside it is the overlap the coalescer exists to prevent.
        afterPreview(() => hooks.refresh());
      })
      .catch((e) => { hooks.toast(`save failed: ${e.message}`, "err"); });
  }

  // ── curve / histogram / ROI widgets ───────────────────────
  function syncCurve() {
    const cv = $("curve-canvas");
    if (!cv) return;
    if (!cv._bound) {
      window.Curve.init(cv, (pts) => {
        // ensureOpPath, not a guard: dragging the curve in LOCAL scope used to
        // update nothing until release, for the same reason the sliders did.
        ensureOpPath("curve").points = pts;
        requestPreview();
        clearTimeout(saveTimer);
        saveTimer = setTimeout(() => commitOp("curve", "points", pts), 200);
      });
      cv._bound = true;
    }
    window.Curve.setPoints(chanConf().ops?.curve?.points || [[0, 0], [255, 255]]);
  }

  // The histogram shows ONE channel — the selected one — because the threshold
  // it drives applies to that channel alone. Showing all three at once was part
  // of why the old panel felt like a wall of controls.
  function histPatch(min, max) {
    return { channels: { [channel]: { ops: { histogram: { enabled: true, min, max } } } } };
  }

  function syncHistogram() {
    const cv = $("hist-canvas");
    if (!cv) return;
    if (!cv._bound) {
      window.Histogram.init(
        cv,
        (r) => {                      // live while dragging
          const op = ensureOpPath("histogram");
          op.min = r[0];
          op.max = r[1];
          // Mirror into the number sliders, the other half of the pair. Dragging
          // the canvas handle left the two <input>s reading their old values
          // until mouseup, so the panel contradicted itself mid-gesture. Write
          // the values directly — no render(), which would rebuild the card and
          // detach the canvas the pointer is still captured on.
          // $("chan-ops"), not the `host` local from bindCards: that one is out
          // of scope here, and a bare `host` would throw inside the pointermove
          // handler — killing the drag silently, since the release commit is a
          // separate listener and would still persist the value.
          const host2 = $("chan-ops");
          for (const [pk, v] of [["min", r[0]], ["max", r[1]]]) {
            const rng = host2.querySelector(
              `input[type="range"][data-op="histogram"][data-p="${pk}"]`);
            const num = host2.querySelector(
              `.fnum-in[data-op="histogram"][data-p="${pk}"]`);
            if (rng) rng.value = v;
            if (num) num.value = v;
          }
          requestPreview();
        },
        (r) => commit(histPatch(r[0], r[1])));   // persist once, on release
      cv._bound = true;
    }
    const h = chanConf().ops?.histogram || {};
    window.Histogram.setChannel(channel);
    window.Histogram.setRanges([h.min ?? 0, h.max ?? 255]);
  }
  function syncRoiInfo() {
    const r = (ready && hooks.getROI) ? hooks.getROI() : null;
    // Normalised extents, so this reads the same on any frame size.
    $("roi-info").textContent = r
      ? `${Math.round((r[2] - r[0]) * 100)}% × ${Math.round((r[3] - r[1]) * 100)}%`
      : "none";
  }

  // Bins depend only on (image, region, roi) — not on the filter settings.
  // Re-fetching on every sweep step or slider tick would double the request
  // count for no change in the data, and a 24MP histogram is not free.
  let binsKey = null, binsTimer = null;
  const BINS_DEBOUNCE_MS = 250;
  // The channel label is part of the cache key: switching R->G shows a
  // different channel's histogram, so it must re-fetch (or at least re-label)
  // rather than reusing R's bins under G's name.
  let binsLabel = null;

  function loadBins(force) {
    if (!ready) return;
    clearTimeout(binsTimer);
    binsTimer = setTimeout(() => fetchBins(force), force ? 0 : BINS_DEBOUNCE_MS);
  }

  // renderOps() rebuilds the cards, which recreates #hist-scope and blanks it.
  // Re-apply the label from the last fetch so a re-render never erases it.
  function paintHistScope(d) {
    const el = $("hist-scope");
    if (!el) return;
    el.textContent = `${channel.toUpperCase()} · source · ${d.roi ? "inside ROI" : "whole frame"}`;
  }

  async function fetchBins(force) {
    const id = hooks.getCurrentId();
    if (!id) { window.Histogram.setBins(null); binsKey = null; return; }
    const v = view();
    const key = [id, v.region || "frame", JSON.stringify(v.roi), channel].join("|");
    if (!force && key === binsKey) {
      if (binsLabel) paintHistScope(binsLabel);
      return;
    }
    try {
      const d = await hooks.loadBins(id);
      // A slow response for a frame we have already left must not overwrite the
      // histogram of the frame now on screen.
      if (hooks.getCurrentId() !== id) return;
      window.Histogram.setBins(d.bins);
      binsKey = key;
      binsLabel = d;
      paintHistScope(d);
    } catch (e) {
      window.Histogram.setBins(null);
    }
  }

  // The frame id the panel was last built for. onImageChanged() is called from
  // showCurrent(), which runs on EVERY preview tick as well as every sweep step,
  // so "the image changed" has to be decided by comparing ids, not assumed.
  let shownFor = null;

  // Called when the displayed frame changes. Deliberately NOT render(): a full
  // rebuild per sweep step re-creates every control, destroying focus and any
  // in-progress drag. In global scope the values are identical across frames,
  // so only the target label needs updating.
  //
  // In LOCAL scope a rebuild IS needed when the frame genuinely changes, because
  // that frame's overrides are different values. It used to run unconditionally,
  // which meant every slider tick rebuilt the panel underneath the user's
  // thumb: the <input> was removed from the DOM mid-drag, the browser's drag
  // tracking pointed at a detached node, and the value froze at whatever the
  // first tick set. Measured over a held drag:
  //
  //     step 1: value= 28  sameNode=False  mutations=7
  //     step 2: value= 28  sameNode=False  mutations=21
  //     ...
  //     step 7: value= 28  sameNode=False  mutations=21
  //
  // Value pinned at 28 for the rest of the drag while 14 more renders were
  // issued — which is why the symptom looked like "the filter never responds
  // until I let go". The requests WERE going out; they were all rendering the
  // same frozen value. Guarding on the id is the whole fix.
  function onImageChanged(id) {
    if (!ready) return;
    renderScopeTarget();
    if (id === shownFor) return;      // same frame: a preview tick, not a switch
    shownFor = id ?? null;
    if (scope === "local") render();
    loadBins();
  }

  function setScope(s) {
    scope = s;
    document.querySelectorAll("#scope-seg button").forEach(b =>
      b.classList.toggle("on", b.dataset.scope === s));
    render();
  }

  async function init(h) {
    hooks = h;
    const d = await hooks.getSchema();
    ops = d.ops;
    blends = d.blends || blends;

    $("scope-seg").addEventListener("click", (e) => {
      const b = e.target.closest("button[data-scope]");
      if (b) setScope(b.dataset.scope);
    });
    $("region-seg").addEventListener("click", (e) => {
      const b = e.target.closest("button[data-region]");
      if (!b) return;
      if (b.dataset.region === "roi" && !view().roi) {
        // Switching to "ROI only" with nothing drawn is a no-op that LOOKS like
        // a broken ROI (the whole frame renders, unchanged). Say so rather than
        // letting the user conclude the control is dead.
        hooks.toast("draw an ROI first: ctrl+drag on the image", "warn");
        render();
        return;
      }
      // force: the bins are computed over the region, so a region switch must
      // re-derive them, not merely relabel the previous region's histogram.
      commit({ region: b.dataset.region }).then(() => loadBins(true));
    });

    const amt = $("amount");
    amt.addEventListener("input", () => {
      $("amount-val").textContent = (+amt.value).toFixed(2);
      const ch = bag().channels || (bag().channels = {});
      ch[channel] = Object.assign({}, ch[channel], { amount: +amt.value });
      requestPreview();
    });
    amt.addEventListener("change", () => {
      const ch = bag().channels || (bag().channels = {});
      commit({ channels: { [channel]: { amount: +amt.value } } });
    });
    $("blend").addEventListener("change", () =>
      commit({ channels: { [channel]: { blend: $("blend").value } } }));
    $("roi-clear").addEventListener("click", () => {
      // Silent: commit() is what persists it. Going through the normal path too
      // would fire onROIChange and write the same value twice.
      hooks.setROI(null, true);
      commit({ roi: null });
    });
    window.addEventListener("resize", () => {
      window.Curve.redraw();
      window.Histogram.redraw();
    });
    // ready BEFORE the first render: render() early-returns while hooks are
    // missing, and the hooks are exactly what we just installed.
    ready = true;
    render();
    loadBins();
  }

  return {
    init, render, setScope, loadBins, syncRoiInfo, onImageChanged,
    // Callers must check this, NOT `typeof window.Filters`: the IIFE assigns
    // that global at parse time, long before hooks exist, so the type check
    // passes while every call inside throws.
    isReady() { return ready; },
    // Coalescing state, for tests. Without this the "only one render in flight"
    // invariant is unobservable from outside, which is how the original queueing
    // bug shipped: nothing could see that several requests were outstanding.
    previewStats() {
      return {
        busy: previewBusy, queued: previewQueued,
        timerPending: previewPending !== null, issued: previewSeq,
      };
    },
    getScope() { return scope; },
    setChannel(c) { channel = c; render(); },
    getChannel() { return channel; },
  };
})();
