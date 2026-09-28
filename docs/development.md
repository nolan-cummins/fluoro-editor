# Development

The original phase plan and design history live outside the repo, in
`~/src/plans/fluoro-editor-plan.md`. The docs here are the current state.

## Tests

```bash
python3 tools/gen_test_images.py --out data/images   # synthetic series
python3 tools/test_filters.py                        # 81 filter unit checks
python3 tools/test_chain.py                          # 17 chain checks, real frames
python3 tools/test_warm.py                           # background warmer + render cache
python3 tools/test_export.py                         # encoders, name safety, job lifecycle
python3 tools/test_roi_drag.py                       # ROI move / resize, real mouse input
python3 tools/verify_ui.py                           # Playwright, full UI pass

# GPU-only: asserts the CUDA path is BIT-IDENTICAL to NumPy, and that it wins.
docker exec fluoro-editor-fluoro-editor-1 python3 tools/test_gpu.py

# needs a running server (test_export_api.py [base_url])
python3 tools/test_export_api.py
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
against the whole class of regression [performance.md](performance.md)
describes — a background
warmer that outranks the foreground, a prefetch that fires N full-resolution
renders on load, a thumbnail path that decodes 24MP to produce 160 pixels.

## Notes

- `decode.py` pins OpenCV to 4 threads (`OPENCV_THREADS`). Its default of one
  thread per core made `imdecode` thrash hard enough to wedge the event loop on
  a loaded box. Raise it if a filter stage wants more.
- `docker-compose.override.yml` is **dev-only** (auto-merged): it attaches the
  `llm-helpers` docker network so the web-u-eye browser stack can reach the app
  by name for visual checks, and bind-mounts `static/` so frontend edits need no
  rebuild. Delete it for a standalone deployment.
