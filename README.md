# fluoro-editor

Web UI (Docker) for a microfluidic fluorescence time series: load a folder of
Canon `IMG_###.jpg` frames, reorder them, and push a per-image or global filter
pipeline over them to make a low-quantum-efficiency dye visible.

FastAPI + OpenCV + numpy + Pillow, vanilla JS frontend, no build step.

## Run

```bash
# point at your experiment folder, or comment out the mount to use only upload
IMAGES_DIR=~/google/research/ESI\ \&\ Droplet/series docker compose up -d
```

Open <http://127.0.0.1:8800>. Bound to loopback only.

`./data` holds settings, uploads and exports — safe to delete to reset.
Optional CUDA: see [docs/performance.md](docs/performance.md#gpu-cuda-optional).

## Using it

| | |
|---|---|
| Load | Mounts `/images` automatically. **Load folder** picks a folder and pulls in every image in it. **Add images** takes individual files. Or drag either anywhere in the window. |
| Reorder | Drag rows. **Ctrl/⌘-click** multi-select, **Shift-click** range-select; a drag moves the whole selected block. **Alt+↑/↓** nudges without dragging. **sort** returns to name order. |
| Sweep | Bottom bar. `←`/`→` step, `space` plays, and it stops at the end rather than wrapping. |
| Thumbnails | `thumbs` toggle in the list header. |
| **Clear** | Empties the list. **Never deletes files** — the images dir is a mount of your own data, so this only resets the app. Rescan brings everything back. |
| **× on a row** | Removes just that image from the list. Also non-destructive. |
| **Save config** | Writes `fluoro-editor.config.json` into the images folder (falls back to `data/` if that mount is read-only). Global settings *and* per-image overrides. |
| **Load config** | Re-applies a saved config. The button shows ✓ when one is found. |
| **Export** | The playhead, the selection, or the whole series: PNG / JPEG / TIFF / MP4, always at full resolution. |

Images sort naturally, so `IMG_2` precedes `IMG_10`. Order, settings and the
saved config all persist across restarts. A file that will not decode is listed
in red and struck through, and never becomes the default view — the rest of the
series still loads.

## Filtering

Three levels, because that is how you actually reason about a fluorescence frame:

```
REGION  ->  CHANNEL  ->  OPERATIONS
```

- **Region** — whole frame, or an ROI you draw on the image (plain drag pans,
  **ctrl+drag** draws, **shift+drag** moves or resizes).
- **Channel** — R / G / B tabs, each with its own operation stack plus an
  `amount` (0 means black, so `R=1, G=0, B=0` isolates) and a `blend` mode
  (`difference` shows what the operations actually changed).
- **Operations** — `Histogram` (a window), `Threshold` (a binarise),
  `Gamma / gain`, `CLAHE`, `Log`, `Curve`, in a drag-reorderable order.

`Global` / `Local` at the top of the pane decides whether an edit applies to
every frame or only the one on screen. Panel details are in
[docs/filters.md](docs/filters.md); what each operation does, and why a
binarised frame is served as PNG rather than JPEG, is in
[docs/filter-reference.md](docs/filter-reference.md).

## Documentation

The README is orientation only. Depth lives in `docs/`:

| | |
|---|---|
| [docs/filters.md](docs/filters.md) | the filter panel: region / channel / operations, ROI gestures, global vs local |
| [docs/filter-reference.md](docs/filter-reference.md) | the six operations, their parameters, and the encoder's PNG decision |
| [docs/export.md](docs/export.md) | frame / ZIP / video jobs, encoder settings, guardrails |
| [docs/performance.md](docs/performance.md) | the two render paths, caching, background warming, 30fps playback, GPU |
| [docs/architecture.md](docs/architecture.md) | module map, the two design decisions, adding Canon RAW |
| [docs/development.md](docs/development.md) | test suites and operational notes |

## Tests

```bash
python3 tools/gen_test_images.py --out data/images   # synthetic series
python3 tools/test_filters.py                        # filter unit checks
python3 tools/test_chain.py                          # compositions on real frames
python3 tools/test_warm.py                           # background warmer + render cache
python3 tools/test_export.py                         # encoders, name safety, job lifecycle
python3 tools/test_roi_drag.py                       # ROI move / resize, real mouse input
python3 tools/verify_ui.py                           # Playwright, full UI pass
python3 tools/test_export_api.py                     # export end-to-end, needs a running server
```

What each suite pins down, plus the GPU-only bit-identity check, is in
[docs/development.md](docs/development.md).

## Notes

- The images mount is **read-write** so Save config can write into the folder.
  Clear never deletes anything, but Save does write one file. Point `IMAGES_DIR`
  at a copy if the source must stay pristine.
- `decode.py` pins OpenCV to 4 threads. Its default of one thread per core made
  `imdecode` thrash hard enough to wedge the event loop on a loaded box.
- `docker-compose.override.yml` is **dev-only** (auto-merged): it joins the
  `llm-helpers` docker network and bind-mounts `static/`. Delete it for a
  standalone deployment.
- Both compose files bind to `127.0.0.1` only.
