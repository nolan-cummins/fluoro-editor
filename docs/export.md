# Export

The **Export** button in the topbar. Three destinations:

| | what you get |
|---|---|
| **Frame** | the playhead image, full resolution, with the current filters |
| **Series** (images) | a ZIP of PNG / JPEG / TIFF frames |
| **Series** (video) | an MP4 at a chosen fps, quality and longest side |

A series export uses the current multi-selection if there is one, otherwise the
whole library in its current order. The **Apply the current filters** checkbox is
on by default, so what you export is what you were looking at; unticking it
exports the raw decode.

Formats and their parameters come from `GET /api/export/options` — including
whether ffmpeg exists in this build. A CPU-only or ffmpeg-less image does not
offer a video button that could only fail, and TIFF compression is *probed*
rather than assumed (this libtiff may lack zlib, in which case `deflate` fails
silently on every frame).

Jobs are written to `$EXPORT_DIR` (default `/data/exports`) and offered as a
download. The dialog shows a percentage and an ETA, and **Cancel** works
mid-job.

## The one invariant

**Every export runs the pipeline at full resolution.** The viewport renders a
1600px preview on purpose, so an export that reused it would hand back a 1600px
JPEG of a 24MP frame and call it a result. Export calls
`render.render_array`, which never downscales and never enters the LRU — a 24MP
array is 72MB and 150 of them would evict everything else.

Video is the one exception to the longest side, and it is explicit in the UI:
a 24MP frame is not a sane video resolution and encoding one costs minutes of
CPU per frame, so video downsamples to the chosen `max_side`.

## Implementation notes

- **Video is piped, not staged.** ffmpeg reads rawvideo from stdin, so a
  176-frame export never writes 176 temporary PNGs (4.6GB at this library's
  size) and never reads them back for a second pass.
- **Crop to even in numpy, not in ffmpeg.** `yuv420p` — the only chroma
  subsampling every player and browser supports — rejects odd dimensions.
  Rounding down in ffmpeg's scale filter is one moving part fewer but it also
  resamples, which under the lossless preset would quietly destroy the thing
  the user asked for.
- **One job at a time**, mirroring the warmer's shape so progress polling and
  cancel semantics are the same to reason about. Starting another supersedes the
  first: the cancel event belongs to the *job*, not to a shared field, or a
  thread reading a reassigned event runs to completion after being cancelled.
- **Cancel kills the ffmpeg child**, not just the loop, or the pipe fills and the
  process wedges.
- **The exporter stands down while a foreground render is in flight**, the same
  as the warmer, so exporting never makes the slider feel broken.
- **Names come from the browser, so they are sanitised** to `[A-Za-z0-9._-]`
  and joined under `EXPORT_DIR`; `/api/export/download` re-sanitises at the point
  of use, because the check belongs where the value becomes a path. Everything
  numeric is clamped against a declared range rather than trusted — this is
  untrusted input that ends up as subprocess arguments and encoder settings.

## Presets

| preset | encoder |
|---|---|
| `lossless` | x264 `-qp 0 -preset veryslow -pix_fmt yuv444p` |
| `high` (default) | x264 `-crf 14 -preset slow -pix_fmt yuv420p` |
| `compact` | x264 `-crf 23 -preset medium -pix_fmt yuv420p` |

For JPEG, chroma 4:2:0 averages over 2×2 blocks, which is what destroys
isolated specks — a single speck drops from 255 to 86. Pick **4:4:4** for
fluorescence data.

## Tests

`tools/test_export.py` (unit) and `tools/test_export_api.py` (end-to-end
through the HTTP API) cover the encoders, the guardrails, and the ffmpeg pipe.
