# fluoro-editor

Web UI (Docker) for a microfluidic fluorescence time series: load a folder of
Canon `IMG_###.jpg` frames, reorder them, and push a per-image or global filter
pipeline over them to make a low-quantum-efficiency dye visible.

**Status: Phase 1 (scaffold) complete.** Library, ordering, viewport and sweep
are done. The filter panel lands in P3; see `~/src/plans/fluoro-editor-plan.md`
for the full phase list and the design decisions behind it.

## Run

```bash
# point at your experiment folder, or comment out the mount to use only upload
IMAGES_DIR=~/google/research/ESI\ \&\ Droplet/series docker compose up -d
```

Open <http://127.0.0.1:8800>. Bound to loopback only.

Data lives in `./data` (settings, uploads, exports) — safe to delete to reset.

## Using it

| | |
|---|---|
| Load | Mounts `/images` automatically. **Load folder** picks a folder and pulls in every image in it. **Add images** takes individual files. Or drag either anywhere in the window. |
| Reorder | Drag rows. **Ctrl/⌘-click** multi-select, **Shift-click** range-select; a drag moves the whole selected block. **Alt+↑/↓** nudges without dragging. **sort** returns to name order. |
| Sweep | Bottom bar. `←`/`→` step, `space` plays, and it stops at the end rather than wrapping. |
| Thumbnails | `thumbs` toggle in the list header. |
| **Clear** | Empties the list. **Never deletes files** — the images dir is a mount of your own data, so this only resets the app. Rescan brings everything back. |
| **× on a row** | Removes just that image from the list. Also non-destructive: Rescan brings it back. |
| **Save config** | Writes `fluoro-editor.config.json` into the images folder (falls back to `data/` if that mount is read-only). Captures global settings *and* every per-image override. |
| **Load config** | Re-applies a saved config. The button shows ✓ when one is found. |

Images sort naturally, so `IMG_2` precedes `IMG_10`. Order, settings and the
saved config all persist across restarts. A file that will not decode is listed
in red and struck through, and never becomes the default view — the rest of the
series still loads.

## Performance

There are **two** render paths, and the split is the whole point.

**Full resolution** — the frame is decoded full-res, the whole pipeline is
applied, and only then is the result downscaled. CLAHE's tile grid and the log
gain act on absolute pixel values, so filtering a 1600px preview and filtering a
6000×4000 frame are genuinely different operations. This is what the sweep, the
settled frame and the export use.

**Draft** (`/api/render/{id}?draft=1`) — the same pipeline over a cached 1024px
copy of the source, for *live slider feedback only*. A full-res render of a 24MP
frame is ~700ms, so every tick of every drag used to be a visible stall. The
draft is ~13ms server-side.

| Operation on a 6000×4000 frame | |
|---|---|
| Header-only scan (176 files) | 0.11s |
| Log + ROI, full res | 0.11s |
| CLAHE, full res, 3 channels | 0.33s |
| Full render round-trip, cold | 0.5–1.0s |
| Full render round-trip, cached | ~6ms |
| **Draft render (live drag), round trip** | **~13ms** |
| Thumbnail, cold (was ~500ms) | ~70ms |

One filter slider tick, measured end-to-end in a real browser: median **44ms**,
worst **66ms** in steady state. That includes the browser decoding and painting
the JPEG, which no server-side change can remove — the server's share is ~5ms.

The draft is an *approximation* of the same pipeline, not a different one:
filtering a downscaled copy is genuinely a slightly different computation for
CLAHE and log. That is the deliberate trade — tune on the draft, and the
full-resolution render replaces it the moment the drag settles. The client
requests both.

Two things keep the draft cheap:

- **LUT fusion.** Histogram, Threshold, Gamma/gain, Log and Curve are all pure
  per-pixel tables, so a run of consecutive ones is composed into a single
  256-entry table
  and applied in one gather. Composition is exact, so this is bit-identical to
  running them in sequence (`test_filters.py` asserts that, against a
  deliberately naive reference implementation). CLAHE is spatial and log in
  `content` mode reads the plane, so those break the run and run as their own
  pass.
- **A cached decoded array, not cached JPEG.** The draft's source is held as
  pixels, because re-decoding a JPEG per tick would cost more than the filtering.
  The full-res render populates this cache as a side effect, so the frame you are
  looking at is always draft-ready.
- **Foreground priority.** The background warmer stands down while a
  interactive render is in flight. Warming 176 frames back-to-back is a nicety;
  the frame being dragged is the thing you feel.
- **Thumbnails that do not decode 24MP.** They were ~500ms each and the page
  requests one per row, so a 176-image library put ~90 seconds of decode in
  front of the first interactive frame. They now use the decoder's DCT-domain
  scaling (`IMREAD_REDUCED_COLOR_*`), which downsamples while entropy-decoding
  rather than after — and still applies EXIF orientation, so portrait frames
  are not sideways.
- **Draft prefetch.** The sweep prefetch used to fire 13 concurrent
  full-resolution renders on page load, which is exactly what a drag then queued
  behind. It now prefetches drafts, so the whole sweep window costs less than a
  single full render.
- **Prefetch concurrency is capped at 4**, and the window is walked
  nearest-first (12 ahead, 3 behind). Uncapped, a 16-image prefetch burst took
  one full-frame decode from 43-212ms to **2.6-12.6 seconds** — prefetching is
  meant to make a sweep step instant, and uncoupled it starved the one frame you
  were actually looking at. A settings change also drops the queued backlog,
  since every URL in it is already stale.
- **One render in flight, latest wins.** A drag does not queue: at most one
  stage request is outstanding, and a tick arriving while one is in flight sets
  a pending flag and re-issues when it lands. `previewLast` is stamped when the
  response *lands*, not when it is dispatched — stamping on dispatch is what let
  the throttle believe it had a free slot while a render was still running, so a
  sustained drag piled up ~9 requests and the mouseup render landed behind
  them. That was the "nothing changes until I let go" report.
  Intermediate positions are dropped on purpose: each render reads current
  state, so replaying a backlog would only ever show stale frames.
- **`showCurrent()` returns a promise** that resolves on the stage image's
  `load` event, not on `img.src` assignment. Without this the in-flight window
  was fiction — the lock was released before the fetch even started. An
  unchanged URL resolves immediately (assigning the same `src` fires no `load`,
  so waiting would hang the queue).
- **The preview tick is "light".** `settingsChanged(true)` skips `renderList()`
  (7.8ms rebuilding 176 rows, half a frame budget) and the
  `POST /api/warm/cancel` that was firing 60x/second. Commits still take the
  full path.

Measured, old vs new, identical 60-step drag: **peak concurrent stage renders 2
→ 1**, requests 61 → 60. `Filters.previewStats()` exposes `busy`/`queued`/
`timerPending`/`issued` so the invariant is assertable — it was unobservable
before, which is how the queueing bug shipped.

The viewport also uses a cached, downscaled preview, and the client prefetches a
window of frames around the current one, so a sweep step is effectively
instant. A fast drag across 12 steps issues one request, not twelve.

**The `filters` checkbox** in the sweep bar swaps the stage and the prefetcher
onto `/api/library/preview` — the cached downscale with no pipeline — so you can
compare a filtered frame against the raw one while sweeping. It is **display
only**: it never touches stored settings, so switching it back on restores the
filters exactly, and it deliberately does *not* invalidate the server-side warm
cache, so flipping back on is instant.

Both the stage and the prefetcher call one `frameUrl()`. That is not tidiness —
the checkbox shipped dead, and the reason is instructive: the stage read the
toggle while the prefetcher had its own hardcoded copy of the render URL, so
every prefetch filled the cache with frames from the mode you had just left.
`tools/verify_ui.py` asserts the two independently, on real network activity.

Renders are cached by `sha1(image + settings + size)`, so dragging a slider
back to a previous value is free.

`library._probe()` reads image **headers only** (PIL's lazy `Image.open`). The
obvious implementation — decode every file to get its dimensions — took minutes
and wedged startup on a 175-image library. Don't "optimize" it back.

### Background pre-rendering — on demand only

The frame you're looking at renders in the **foreground** — that's the only
latency you feel. **Render all** in the topbar pre-renders the rest of the
library (`app/warm.py`) into the same cache, so when you sweep to it the
response is a cache hit. A progress bar in the sweep bar shows `done/total` and
an ETA; it hides itself when finished.

| 6000×4000 frame, one sweep step | |
|---|---|
| cold (filter change, nothing cached) | 631ms mean, 792ms worst |
| warm (pre-rendered) | **5.5ms mean, 7ms worst** |

A full render-all of 176 frames takes ~70s and 27MB.

### Playback — 30fps

Pressing play used to be `setInterval(220ms)`, which is a hard **4.5fps** ceiling
no matter how fast the code underneath is. It was not a performance problem: a
warm step measures **18.5ms median** end to end (0.3ms of that is the URL fetch,
6.7ms synchronous). The app could already do 54fps — the timer was the only thing
holding it back.

Playback is now a `requestAnimationFrame` loop with a 33ms budget that re-arms
only once the previous frame's pixels have actually landed. That second part is
the point: a timer cannot see whether a frame arrived, so firing every 33ms
unconditionally means one 200ms frame leaves five steps queued behind it — and
that queue is what a sweep freeze *is*. A slow frame is now skipped, never
stacked.

Playback also stops rebuilding the image list. `renderList()` regenerates all
176 rows from an `innerHTML` string — 7.8ms, a quarter of the frame budget — for
a change that is one CSS class on one row, so `markCurrentRow()` (~0.1ms) does it
during playback. Discrete steps (arrow keys, the step buttons) keep the full
rebuild, where 7.8ms is irrelevant.

| warm playback, 176 frames | |
|---|---|
| median | **30.1fps** (33.2ms/frame) |
| p90 | 22.4fps |
| worst single frame | 100ms |
| frames over 100ms | 7 of 174 |

> **Measuring this:** drive playback *and* timestamp it inside the page
> (`performance.now()` on the stage `load` event). Pacing the loop from Python
> with `wait_for_timeout` measures your own sleeps — that mistake reported
> 184ms "frame gaps" that were entirely harness latency. And warm the **whole**
> library first: warming 50 frames then playing 176 makes the cold tail look
> like stalls.

This is a **warm** number, and it is worth being explicit about that. A cold
library stutters on first pass, because each frame is a real render; `Render all`
is the fix and it is one click away. The alternative — showing a synthetic
frame rate for frames that aren't actually decoded — would be a lie.

**It does not run on its own, and that is deliberate.** It used to fire on every
settings change *and* on page load. A slider drag fires a change per tick, so
each one cancelled and re-queued all 176 frames: the server spent ~133 seconds
of continuous full-resolution work (observed at warm job #142) and every frame
you touched competed with it for memory bandwidth. Rendering the whole library
is worth doing once, after you have settled on a look — never between two
adjustments. So:

- no warm on load, and none on a settings change
- **Render all** starts one (with a confirm — it is expensive by design)
- any settings change **cancels** a warm in flight, because it was rendered
  under settings that are no longer on screen

Warming does **not** starve the API: `/api/settings` latency is 6ms idle vs 4ms
while a 176-frame warm is running (OpenCV and numpy release the GIL). The
warmer also stands down while an interactive render is in flight, and does not
count *itself* as one — the priority primitive is a counter, and a background
call passes `interactive=False` so it defers to real user requests rather than
to itself. Knobs: `RENDER_CACHE_ENTRIES` (default 320) and `WARM_WORKERS`
(default 1 — more workers halve warm time but compete with foreground renders).

### GPU (CUDA, optional)

```bash
docker compose -f docker-compose.yml -f docker-compose.gpu.yml up -d --build
```

Needs the NVIDIA container toolkit on the host. The plain `docker compose up`
path is unchanged and CPU-only. `/health` reports which is in use:

```json
{"ok": true, "items": 176, "gpu": "ok", "gpu_on": true}
```

`app/gpu.py` accelerates the ops that are pure 256-entry table gathers —
**Histogram, Threshold, Gamma/gain, Log and Curve**. Those are exactly
the controls that felt dead, and they are 100–250× cheaper on the GPU than in
NumPy. Measured on a 24MP frame:

| | CPU | GPU |
|---|---|---|
| Filter chain, all LUT ops | 200.4ms | **26.8ms** (7.5×) |
| Full render, decode→filter→encode | 246.6ms | **52.2ms** (4.7×) |
| Histogram, whole frame | 86.3ms | **0.9ms** |

**CLAHE deliberately stays on the CPU.** Its per-tile histograms measured 403ms
on the GPU — *worse* than the CPU — because each tile is a separate kernel
launch. Doing it properly needs a batched 2D histogram into a (tiles, 256)
buffer, which is its own piece of work. `gpu.eligible()` refuses any settings
that touch CLAHE (or log in `content` mode, which reads the plane), and the whole
render falls back to NumPy rather than splitting the chain across PCIe twice.

This is a second implementation of the filter maths, and the failure mode for
that is silent divergence, so `tools/test_gpu.py` asserts the two paths are
**bit-identical** — not merely close — across isolation, all four ops fused,
both op orders, fractional amounts, difference blending, ROI scoping, the
no-op case, and both histogram paths. Set `FLUORO_GPU=0` to force the CPU path
and compare.

The **draft** (the live-drag path) also runs on the GPU, because it turns out to
be a win rather than a loss. The draft source is only 2.1MB, so the PCIe round
trip is ~0.5ms — not the 14ms/22ms a full 24MP frame costs. Measured on one
slider tick: **5.22ms → 2.23ms (2.3×)**. It is faster with the GPU because the
arithmetic still dominates, and the transfer does not.

(I had assumed the opposite — that a 1024px copy would be swamped by transfer
overhead — because the 14ms/22ms figures are for a 72MB frame. Measuring it
instead of reasoning from the wrong number is what caught it. No code change
was needed: the draft path calls `filters.render`, which picks the GPU up on
its own.)

## The filter panel

Three levels, because that is how you actually reason about a fluorescence frame:

```
REGION  ->  CHANNEL  ->  OPERATIONS
```

**1. Region** — `whole frame` or `ROI only`, chosen once and gating *every*
operation for *every* channel. On the image:

| gesture | does |
|---|---|
| **plain drag** | **pan, always** — including from inside the box |
| **ctrl+drag** (or toggle `ROI mode`) | draw a *new* box |
| **shift+drag** inside the box | move it |
| **shift+drag** a handle | resize that edge or corner |

The wheel zooms. The ROI is stored normalised to 0-1, so it stays valid across
frames of different sizes, and it persists with the config. Zoom `1.0×` means
*fit to the pane*, not 100% pixels.

Pan is unconditional on purpose. It used to lose to the box: a drag starting
inside the ROI moved the box, so the image could only be panned by starting
outside it, and a new box could not be drawn over an existing one. Pan is what
you reach for when you are lost, so it is never the gesture that gets stolen.

Because pan must win even from inside the box, the box is `pointer-events: none`
and handle hit-testing is **geometric** against the stored pixel bounds rather
than an `e.target` test — with the overlay transparent to the pointer,
`e.target` is always the stage and `closest(".roi-handle")` could never match.
The box is also explicitly unfilled: a tinted overlay makes a dim sample
indistinguishable from a bright one, which defeats the point of the tool.

The box carries eight handles, counter-scaled so they stay a constant size on
screen however far you zoom. `tools/test_roi_drag.py` drives real mouse input at
real screen coordinates and asserts the box moves, that a handle resizes, that
only the dragged corner moves, that an outside drag pans without touching the
ROI, and that pressing on the box does not erase it.

> **Note when touching the pan tests:** pan is clamped to the image bounds, and
> at fit the image fills the pane — one axis has zero slack and the other is
> already at its limit. A pan assertion that does not zoom in first measures the
> clamp, not the gesture, and fails with identical before/after transforms.

> The move and resize gestures were advertised by the panel long before any code
> implemented them: `pointerdown` had exactly two branches, so every drag either
> panned or drew a brand-new box. The hint text is now true rather than
> aspirational.

> An earlier version tagged individual filters `roi_scoped: True`, so only
> histogram and log respected the ROI while levels and CLAHE silently applied to
> the whole frame — which made ROI mode look broken. The region gate is now
> global, and `tools/verify_ui.py` asserts the region control has a usable
> height *and* that switching it changes the render.

**2. Channel** — R / G / B tabs. Each has its own complete operation stack, plus:

- **amount** (0–1) — how much this channel contributes to the composite. This
  is how you isolate: `R=1, G=0, B=0` leaves pure red. `0` means the channel is
  **black**, not "leave the source alone" — an earlier version lerped source→processed
  by `amount`, which made 0 mean the exact opposite and left the original green
  and blue in the frame. It also applies when the channel has no operations
  enabled at all, so isolation works without switching anything on.
- **blend** — `normal` scales the processed channel by `amount`; `difference`
  shows `|source − processed|` (scaled the same way), i.e. what the operations
  actually changed.

**3. Operations** — for the selected channel, in a drag-reorderable order:
`Histogram`, `Threshold`, `Gamma / gain`, `CLAHE`, `Log`, `Curve`. Each has an
enable checkbox; the order list and the cards are two views of one state. The
order is part of the settings, so it round-trips through Save/Load config.

**Histogram** shows the 0-255 distribution of the *source* over the active region
for the selected channel, with draggable min/max lines driving that channel's
`histogram`. Bar heights are `sqrt`-compressed (fluorescence histograms are
extremely peaked, so a linear axis renders every bar but one invisible) — the
value axis stays linear. Dragging previews live and persists once on release.

**Curve** edits control points. Click empty space to add a point, drag to move,
right-click to remove; the two endpoints stay pinned so the curve remains a true
input→output mapping. The preview uses the *same* Fritsch-Carlson monotone fit
the server applies, so what you see is what you get.

**Global / Local** (the switch at the top of the pane) decides *where* an edit
lands: `Global` applies to every frame, `Local` writes a per-image override and
shows what *this* frame actually renders with. Sections with no override are
dimmed, because otherwise "inherited" is indistinguishable from "explicitly set
to the same value". Editing a dimmed section creates the override; `reset` drops
it and falls back to global.

## Filters

| operation | params | notes |
|---|---|---|
| Histogram | min / max | a **window**: values outside [min,max] go to black, values inside are untouched |
| Threshold | level, invert | **binarises**: `>= level` → 255, below → 0. One bit per pixel. Served as lossless PNG — see below |
| Gamma / gain | black, white, gamma, gain | folded into one LUT; `dst = src^gamma`, so gamma < 1 brightens |
| CLAHE | clip limit, tile grid | spatial, so it is never fused with the table ops |
| Log | normalize, ref, gain | `fixed` normalize by default — see below |
| Curve | control points | shape-preserving (Fritsch–Carlson) |

**Histogram and Threshold are different operations and both exist**, because
they answer different questions. The window keeps a *band* and leaves it
greyscale; the threshold collapses to a single bit. Neither subsumes the other:
for a low-QE fluorescent sample the window often keeps a band so wide that
survivors still sit on background noise, which is the case only the binarising
threshold handles.

### A binarised frame is served as PNG, not JPEG

Enabling Threshold used to *look* like a clip — shades of red at every level,
not black and pure `#ff0000`. The op was never wrong. Two steps *after* it put
the greys back, and both had to be fixed together:

| step | what it did to a 0/255 frame |
|---|---|
| `INTER_AREA` downscale to 1600px | averaged 2 values into **86 distinct greys** (260946 midtone pixels) |
| JPEG encode at q90 | turned an untouched 0/255 buffer into **28 distinct values**, ringing at every edge |

So `render.is_binary()` now inspects the rendered data, and a binary result is
downscaled with `INTER_NEAREST` and encoded as PNG. Continuous-tone frames keep
JPEG and `INTER_AREA` — for those, exactness does not matter and bytes do.

Three things worth knowing about that choice:

- The test is on the **data**, not the settings. A chain can reach a binary
  result without Threshold, and Threshold plus a fractional `amount` on another
  channel re-introduces midtones the op never produced.
- The test is on the **filtered region**, not the whole buffer. With
  `region = ROI` the pixels *outside* the box are deliberately the untouched
  original, so they are continuous-tone by design — a whole-frame test therefore
  always failed in ROI mode and quietly demoted a correctly binarised interior
  back to the lossy path, which is the same "looks like a clip" bug in a second
  disguise. `is_binary(img, box)` and `active_box(img, settings)` exist so the
  encoder cannot disagree with the pipeline about which pixels were filtered.
- The test is on **membership, not presence**: every pixel is 0 or 255. It does
  not require that *both* values occur, because a uniformly-black region is
  exactly as binary as a 50/50 split — and turning the level up until the ROI
  rejects everything is ordinary use. Requiring both sent a dead-flat black
  region down the lossy path, which averaged the box edge and invented 33
  distinct values where the true content had one.
- PNG of a binarised frame is about **9× smaller** than the JPEG of the same
  frame (35KB vs 306KB), because flat runs of one value compress to almost
  nothing. Lossless is cheaper here, not dearer.

Measured on a real 24MP frame, `region = ROI`, Threshold at 128, R-only, the
values inside the box as the browser received them:

| | before | after |
|---|---|---|
| distinct values in the ROI | 256 | **2** (`0`, `255`) |
| content type | `image/jpeg` | `image/png` |

`tools/test_filters.py` asserts each cause separately, so fixing one alone still
fails the suite. `tools/verify_ui.py` additionally reads the painted `<img>` back
through a canvas and asserts the ROI interior is strictly `0`/`255` — the bug
lived between the filter output and the screen, so only the screen settles it.

> **The 3px inset in those assertions is not fudging.** At the ROI edge the
> encoder's area-average for the *unfiltered outside* meets the box, and that
> one seam column is a blend of both. It is not part of the filtered interior
> being judged, and treating it as if it were would mask the real bug.
>
> **A later op can still put the greys back, legitimately.** CLAHE is spatial,
> so anything downstream of it re-introduces midtones. Binarisation is only
> preserved while Threshold is the *last* thing in the chain.

The window is deliberately a band-pass, not a levels clip. Clipping — pushing
everything above `max` to white — is the opposite of "show me the ones I want",
and it leaves the surviving band already saturated, so every operation placed
after it has nothing left to grade. The window rejects the out-of-band pixels to
black instead, which is what makes `window → gamma/log/CLAHE` a usable chain.
It is also what MALDI-WEB's `histogram_mask` did (`cv2.inRange`, a true window).

> The window used to be *called* "Threshold", which is why the two got confused
> in the first place. `tools/test_filters.py` now asserts they differ, that
> threshold's output is strictly `{0, 255}`, and that a nonsense `invert` falls
> back to the default rather than silently emptying the frame.

Gamma follows the universal `dst = src^gamma` convention — gamma < 1 brightens,
gamma > 1 darkens — matching GIMP, Photoshop and OpenCV.

Adding an operation is one dict entry in `app/filters.py`; the frontend builds
its controls from `GET /api/filters/schema`.

Two deliberate departures from the MALDI original:

**The log transform defaults to `fixed` normalization.** MALDI divides by each
frame's own maximum, so every frame normalizes to its own peak and the series
*flickers* as brightness drifts — which defeats the purpose of sweeping it.
`fixed` uses a user-set reference so frame-to-frame brightness is comparable.
`content` is still available for parity.

**The curve is shape-preserving, not globally monotone.** A natural/naive cubic
rings: on a dipping curve it overshoots the control-point band by ~2700 levels,
which clips and destroys the faint specks the log transform just revealed. The
Fritsch–Carlson limiter keeps it inside the band. `tools/test_filters.py`
asserts both directions.


## Architecture

```
app/decode.py     decoder registry — the seam for adding Canon RAW later
app/library.py    discovery, metadata, thumbnails, natural sort, ordering
app/settings.py   global settings + partial per-image overrides (deep-merged)
app/main.py       FastAPI routes
static/           vanilla JS, no build step
tools/            test-data generator + browser verification suite
```

The whole stack is FastAPI + `opencv-python-headless` + numpy + Pillow, chosen
to match the existing MALDI-WEB project so its filter pipeline could be ported
rather than rewritten.

### Two decisions worth knowing

**Local overrides store only what you touched.** A per-image override is
deep-merged over the global settings, so a section you have not overridden keeps
tracking global edits live. Resetting a section is dropping its keys.

**The ROI is stored normalized (0–1), not in pixels.** A 6000×4000 source and a
~1200px preview canvas are different scales, so a pixel ROI would not survive a
zoom or transfer between frames — and the ROI has to mean the same thing on
every image in the series.

## Adding RAW (CR2/CR3) later

JPEGs only for now, by design. RAW is one registry entry, not a refactor:

```python
# app/decode.py
Decoders.append(Decoder(exts={".cr2", ".cr3"}, fn=decode_rawpy, slow=True))
```

plus `rawpy` in `requirements.txt`. `slow=True` routes those files through a
background prewarm queue instead of decoding inline during a request — that
queue already exists for full-res renders. Every decoder returns BGR `uint8`,
so library, render and export need no changes.

## Tests

```bash
python3 tools/gen_test_images.py --out data/images   # synthetic series
python3 tools/test_filters.py                        # 81 filter unit checks
python3 tools/test_chain.py                          # 17 chain checks, real frames
python3 tools/test_warm.py                           # background warmer + render cache
python3 tools/test_roi_drag.py                       # ROI move / resize, real mouse input
python3 tools/verify_ui.py                           # Playwright, 107 checks

# GPU-only: asserts the CUDA path is BIT-IDENTICAL to NumPy, and that it wins.
docker exec fluoro-editor-fluoro-editor-1 python3 tools/test_gpu.py
```

`gen_test_images.py` synthesizes a Rhodamine B series — dark frames, an
orange-red dye front advancing, injected single-pixel specks and two fixed
hot pixels — plus one deliberately corrupt file, so development and the test
suite never depend on the real experiment data.

`verify_ui.py` drives a real browser: natural sort, click/ctrl/shift select,
block drag, Alt-arrow nudge, thumbnail toggle, sort round-trip, order
persistence across reload, sweep stepping, play/pause, render-endpoint and
latency assertions, request-count-on-drag, single remove, clear-then-rescan,
config save/load, and console cleanliness. It normalizes the image order first
so repeated runs are idempotent, and restores the original order on exit.

`test_filters.py` is pure and fast — no server needed. It covers the properties
that break silently: log monotonicity, curve shape preservation, per-channel
correctness, ROI compositing, filter-order effect, chain purity, and histogram
totals. It also pins the properties that are easy to "optimise" wrongly —
that LUT fusion is bit-identical to sequential application, that the histogram
window rejects rather than saturates, and that the binarising threshold really
is binary and is not the window wearing a different name.

`test_chain.py` runs the compositions users actually build, on real frames, and
asserts *direction* rather than "the pixels changed": that a threshold blacks
its rejects, that gamma brightens/darkens the survivors in the expected
direction without resurrecting them, that log lifts them, that CLAHE raises
local contrast, and that swapping any adjacent pair of ops changes the result.
It picks its threshold band from the frame's own histogram rather than a guessed
range, and skips cleanly if no images are mounted.

The latency assertions in `verify_ui.py` are deliberately tight (median
<100ms, worst <250ms for a filter slider tick). Those numbers are the guard
against the whole class of regression this section describes — a background
warmer that outranks the foreground, a prefetch that fires N full-resolution
renders on load, a thumbnail path that decodes 24MP to produce 160 pixels.

## Notes

- The images mount is **read-write** so Save config can write into the folder.
  Clear never deletes anything, but Save does write one file. Point `IMAGES_DIR`
  at a copy if the source must stay pristine.
- `decode.py` pins OpenCV to 4 threads (`OPENCV_THREADS`). Its default of one
  thread per core made `imdecode` thrash hard enough to wedge the event loop on
  a loaded box. Raise it if a filter stage in P2+ wants more.
- `docker-compose.override.yml` is **dev-only** (auto-merged): it attaches the
  `llm-helpers` docker network so the web-u-eye browser stack can reach the app
  by name for visual checks, and bind-mounts `static/` so frontend edits need no
  rebuild. Delete it for a standalone deployment.
- Both files bind to `127.0.0.1` only.

