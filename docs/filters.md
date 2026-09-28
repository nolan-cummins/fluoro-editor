# The filter panel

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

The six operations themselves — parameters, what each one does, and why a
binarised frame is served as PNG — are in [filter-reference.md](filter-reference.md).
