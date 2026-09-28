# Architecture

```
app/decode.py     decoder registry — the seam for adding Canon RAW later
app/library.py    discovery, metadata, thumbnails, natural sort, ordering
app/filters.py    the operation registry (one dict entry per filter)
app/render.py     full-res pipeline -> downscale -> sha1 LRU; is_binary() encoder choice
app/gpu.py        optional CUDA path for the LUT-family ops
app/warm.py       on-demand background pre-render of the whole library
app/export.py     frame / ZIP / ffmpeg video jobs
app/config.py     save + load fluoro-editor.config.json
app/settings.py   global settings + partial per-image overrides (deep-merged)
app/main.py       FastAPI routes
static/           vanilla JS, no build step
tools/            test-data generator + test/verification suites
```

The whole stack is FastAPI + `opencv-python-headless` + numpy + Pillow, chosen
to match the existing MALDI-WEB project so its filter pipeline could be ported
rather than rewritten.

## Two decisions worth knowing

**Local overrides store only what you touched.** A per-image override is
deep-merged over the global settings, so a section you have not overridden keeps
tracking global edits live. Resetting a section is dropping its keys.

**The ROI is stored normalized (0–1), not in pixels.** A 6000×4000 source and a
~1200px preview canvas are different scales, so a pixel ROI would not survive a
zoom or transfer between frames — and the ROI has to mean the same thing on
every image in the series.

# Adding RAW (CR2/CR3) later

JPEGs only for now, by design. RAW is one registry entry, not a refactor:

```python
# app/decode.py
Decoders.append(Decoder(exts={".cr2", ".cr3"}, fn=decode_rawpy, slow=True))
```

plus `rawpy` in `requirements.txt`. `slow=True` routes those files through a
background prewarm queue instead of decoding inline during a request — that
queue already exists for full-res renders. Every decoder returns BGR `uint8`,
so library, render and export need no changes.
