# Filter reference

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

## A binarised frame is served as PNG, not JPEG

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

