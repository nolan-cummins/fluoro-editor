# Performance

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

## Background pre-rendering — on demand only

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

## Playback — 30fps

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

## GPU (CUDA, optional)

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
