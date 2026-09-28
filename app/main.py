"""fluoro-editor — fluorescence time-series viewer.

Phase 1 (scaffold): health, library scan/upload/order/thumbs, settings store,
static shell. Render + histogram land in P2.
"""
from __future__ import annotations

import base64
import json
from pathlib import Path

import cv2
from fastapi import FastAPI, File, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles

from . import config, export, filters, gpu, library, render, settings, warm

ROOT = Path(__file__).resolve().parent.parent
STATIC_DIR = ROOT / "static"

app = FastAPI(title="fluoro-editor")


@app.on_event("startup")
def _startup():
    settings.load()
    library.load()
    # Initial discovery. A missing /images mount is not fatal — upload still
    # works, so the app starts and simply shows an empty list.
    try:
        library.scan()
    except Exception:
        pass


@app.get("/health")
def health():
    # `gpu` is the reason or the excuse. "no CUDA device" means the container was
    # started without the GPU compose override, which is otherwise invisible —
    # the app looks identical, just slower.
    return {"ok": True, "items": len(library.as_list()),
            "gpu": gpu.status(), "gpu_on": gpu.enabled()}


# ── library ─────────────────────────────────────────────────────
@app.get("/api/library")
def get_library():
    return {"items": library.as_list()}


@app.post("/api/library/scan")
def rescan():
    return {"items": library.scan()}


@app.post("/api/library/upload")
async def upload(file: UploadFile = File(...)):
    data = await file.read()
    if not data:
        raise HTTPException(400, "empty upload")
    return library.add_upload(file.filename or "upload.jpg", data)


@app.post("/api/library/order")
async def set_order(req: Request):
    body = await req.json()
    ids = body.get("ids")
    if not isinstance(ids, list):
        raise HTTPException(400, "expected {ids: [...]}")
    return {"order": library.set_order([str(i) for i in ids])}


@app.get("/api/library/thumb/{item_id}")
def thumb(item_id: str, size: int = Query(160, ge=32, le=512)):
    data = library.thumb(item_id, size)
    if data is None:
        raise HTTPException(404, "no such image or decode failed")
    return Response(content=data, media_type="image/jpeg",
                    headers={"Cache-Control": "public, max-age=86400"})


@app.get("/api/library/preview/{item_id}")
def preview(item_id: str, max_side: int = Query(1600, ge=256, le=8000)):
    """Cached, downscaled JPEG for the viewport. This is what the sweep slider
    hammers, so it is cached server-side and never returns a full-res frame."""
    data = library.preview(item_id, max_side)
    if data is None:
        raise HTTPException(404, "no such image or decode failed")
    return Response(content=data, media_type="image/jpeg",
                    headers={"Cache-Control": "public, max-age=3600"})


@app.get("/api/library/raw/{item_id}")
def raw(item_id: str, max_side: int = Query(0, ge=0, le=20000)):
    """Unfiltered FULL-RES image. P2's render endpoint supersedes this for the
    viewport; kept for P9 export, which needs every pixel."""
    img = library.by_id(item_id)
    if img is None:
        raise HTTPException(404, "no such image or decode failed")
    if max_side:
        h, w = img.shape[:2]
        if max(h, w) > max_side:
            s = max_side / max(h, w)
            img = cv2.resize(img, (int(w * s), int(h * s)),
                             interpolation=cv2.INTER_AREA)
    ok, buf = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 90])
    if not ok:
        raise HTTPException(500, "encode failed")
    return Response(content=buf.tobytes(), media_type="image/jpeg")


@app.post("/api/library/remove/{item_id}")
def remove_item(item_id: str):
    """Drop one image from the list. Never deletes the file."""
    if not library.remove(item_id):
        raise HTTPException(404, "not in the library")
    # Its cached renders go too. Nothing else can reach this id, so they would
    # sit in the LRU until evicted, holding ~157KB per settings value.
    render.invalidate(item_id)
    return {"removed": item_id, "items": library.as_list()}


@app.post("/api/library/clear")
def clear_library():
    """Empty the list. Never deletes files — the images dir is a mount of the
    user's own research data."""
    n = library.clear()
    render.invalidate()          # every cached render belongs to a removed image
    return {"cleared": n, "items": []}


# ── render pipeline ──────────────────────────────────────────
@app.get("/api/filters/schema")
def filters_schema():
    return {"ops": filters.op_schema(), "channels": list(filters.CHANNELS),
            "blends": filters.BLENDS, "defaults": filters.default_channel()}


@app.get("/api/render/stats")
def render_stats():
    # MUST stay above /api/render/{item_id}: FastAPI matches in declaration
    # order, so a literal path declared after a parameterised one is swallowed
    # by it and returns a confusing 404.
    return render.stats() | {"gpu": gpu.status(), "gpu_on": gpu.enabled()}


def _decode_settings(s: str) -> dict | None:
    """base64url -> dict, or None if it is not one.

    None is the "no override, use persisted settings" signal, which is why a
    malformed param degrades to a normal render instead of a 500: a corrupted
    query string must never be able to take the preview endpoint down. The
    payload is untrusted input from the query, so it is length-capped by the
    Query() declaration and re-validated by filters._coerce before it reaches
    the pipeline — _coerce clamps every parameter to its declared range and
    drops anything unrecognised.
    """
    if not s:
        return None
    try:
        pad = "=" * (-len(s) % 4)
        obj = json.loads(base64.urlsafe_b64decode(s + pad))
    except Exception:
        return None
    return obj if isinstance(obj, dict) else None


@app.get("/api/render/{item_id}")
def render_item(item_id: str, key: str = Query("", max_length=64),
                s: str = Query("", max_length=8192),
                max_side: int = Query(1600, ge=256, le=8000),
                draft: bool = Query(False),
                raw: bool = Query(False)):
    """Processed frame. Filters are applied at FULL resolution; only the result
    is downscaled to max_side for the viewport. `key` is the client's settings
    hash — it busts the browser cache, the server keys its own cache properly.

    `draft=1` returns the downscaled fast render instead (~15ms vs ~700ms) for
    live slider feedback. It is an approximation of the same pipeline; the client
    requests the full render once the drag settles.

    `s` is the LIVE PREVIEW TRANSPORT, and it is the whole fix for "filters only
    update when I let go of the slider".

    While a slider is held, the client mutates only its own in-memory settings and
    sends nothing — the render handler resolved everything from PERSISTED state, so
    every tick of the drag re-rendered the last committed value. Identical pixels,
    every filter, both scopes; the picture moved only on mouseup because that is
    when commit() finally PUTs. `key` could not carry the value: it is a browser
    cache-buster and the server has no idea what it hashes.

    So the draft request now carries the settings themselves, base64url-encoded.
    Absent `s` (committed render, warmer, prefetch) the behaviour is unchanged:
    `settings.effective` decides, and the server LRU stays keyed on real settings.
    """
    if library.get(item_id) is None:
        raise HTTPException(404, "not in the library")
    override = _decode_settings(s)
    st = filters._coerce(override) if override is not None else settings.effective(item_id)
    if raw:
        out = render.render_array(item_id, st)
        if out is None:
            raise HTTPException(404, "decode failed")
        ok, buf = cv2.imencode(".png", out)
        if not ok:
            raise HTTPException(500, "encode failed")
        return Response(content=buf.tobytes(), media_type="image/png")

    if draft:
        enc = render.render_draft(item_id, st)
        if enc is None:
            raise HTTPException(404, "decode failed")
        data, media = enc
        # no-store: a draft is only ever shown for the instant it is requested.
        # Caching it would let a stale draft outlive the settings that made it.
        return Response(content=data, media_type=media,
                        headers={"Cache-Control": "no-store",
                                 "X-Settings-Key": key, "X-Draft": "1"})

    enc = render.render_jpeg(item_id, st, max_side)
    if enc is None:
        raise HTTPException(404, "decode failed")
    data, media = enc
    # media is image/png for a binarised (Threshold) result and image/jpeg
    # otherwise — the header has to match the bytes, not assume JPEG.
    return Response(content=data, media_type=media,
                    headers={"Cache-Control": "public, max-age=3600",
                             "X-Settings-Key": key})


# ── background warming ───────────────────────────────────────────
# Foreground renders stay synchronous: the frame the user is looking at is the
# latency they actually feel. These endpoints warm the REST of the library into
# the same LRU so sweeping hits cache instead of paying 0.5-1.0s per frame.
@app.post("/api/warm/start")
async def warm_start(req: Request):
    body = await _body(req)
    first = body.get("first") or None
    order = library.as_list()
    return warm.warmer.start([i["id"] for i in order], first)


@app.get("/api/warm/progress")
def warm_progress():
    return warm.warmer.progress()


@app.post("/api/warm/cancel")
def warm_cancel():
    warm.warmer.cancel()
    return warm.warmer.progress()


# ── export (P9) ─────────────────────────────────────────────────────
# The spec is a POST body, not a query, because it carries a list of ids and a
# params dict. Everything numeric is clamped in app/export.py against a
# declared range rather than trusted: this is untrusted input that ends up as
# subprocess arguments and encoder settings.
@app.get("/api/export/options")
def export_options():
    """What this server can actually do. The client renders the menu from this
    rather than hardcoding it, so a build without ffmpeg offers ZIP and frame
    exports and does not offer a video button that can only fail."""
    return {
        "dir": str(export.EXPORT_DIR),
        "ffmpeg": export.has_ffmpeg(),
        "video_presets": list(export.VIDEO_PRESETS),
        "image_formats": ["png", "jpg", "tiff"],
        # Probed, not assumed: this libtiff may not have zlib, in which case
        # "deflate" silently fails on every frame.
        "tiff_compression": list(export.TIFF_COMPRESSION),
    }


@app.post("/api/export/start")
async def export_start(req: Request):
    spec = await _body(req)
    kind = spec.get("kind")
    if kind not in ("frame", "series"):
        raise HTTPException(400, "kind must be 'frame' or 'series'")
    fmt = spec.get("format", "png")
    if fmt not in ("png", "jpg", "jpeg", "tif", "tiff", "video"):
        raise HTTPException(400, f"unsupported format {fmt!r}")
    if fmt == "video" and not export.has_ffmpeg():
        raise HTTPException(400, "ffmpeg is not available in this build")
    # Drop anything not in the library rather than trusting the posted list: an
    # id that is not in the library is either a typo or a probe.
    ids = [i for i in (spec.get("ids") or []) if library.get(i)]
    if kind == "frame" and not ids:
        raise HTTPException(400, "no valid frame in the request")
    if not ids:
        raise HTTPException(400, "no valid frames in the request")
    spec["ids"] = ids
    return export.exporter.start(spec)


@app.get("/api/export/progress")
def export_progress():
    return export.exporter.progress()


@app.post("/api/export/cancel")
def export_cancel():
    return export.exporter.cancel()


@app.get("/api/export/download")
def export_download(name: str = Query(..., max_length=200)):
    """Stream a finished export. The name is re-sanitised here even though
    export.py already sanitised it when writing: this value becomes a path, and
    the check belongs at the point of use rather than only at the point of
    creation."""
    safe = export.safe_name(name, "")
    if not safe:
        raise HTTPException(400, "bad name")
    p = (export.EXPORT_DIR / safe).resolve()
    if p.parent != export.EXPORT_DIR.resolve() or not p.is_file():
        raise HTTPException(404, "no such export")
    return FileResponse(p, filename=p.name)


@app.get("/api/histogram/{item_id}")
def histogram_item(item_id: str):
    """Per-channel 256-bin histogram of the SOURCE image over the active region.

    Source, not the filtered result: the threshold handles must stay anchored
    while the operations above them are being tuned, otherwise the histogram
    moves under the drag. Respects `region` — with region="frame" the ROI is
    ignored, exactly as the render does.
    """
    img = library.by_id(item_id)
    if img is None:
        raise HTTPException(404, "not in the library or decode failed")
    eff = settings.effective(item_id)
    roi = eff.get("roi") if eff.get("region") == "roi" else None
    return {"bins": filters.histogram(img, roi), "roi": roi,
            "region": eff.get("region", "frame")}


# ── config ───────────────────────────────────────────────────
@app.get("/api/config")
def config_info():
    return config.describe()


async def _body(req: Request) -> dict:
    raw = await req.body()
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except Exception:
        return {}


@app.post("/api/config/save")
async def config_save(req: Request):
    try:
        return config.save((await _body(req)).get("path"))
    except Exception as e:
        raise HTTPException(500, f"save failed: {e}")


@app.post("/api/config/load")
async def config_load(req: Request):
    """Accepts either a path to a config on the server, or an inline config
    object — the latter is how a folder drop delivers its config, parsed in the
    browser and posted straight through."""
    body = await _body(req)
    try:
        if isinstance(body.get("config"), dict):
            applied = settings.import_config(body["config"],
                                            replace=bool(body.get("replace", True)))
            return {"loaded": True, "from": "inline",
                    "globals": len(applied["global"]), "images": len(applied["local"])}
        return config.load(body.get("path"), replace=bool(body.get("replace", True)))
    except FileNotFoundError as e:
        raise HTTPException(404, str(e))
    except Exception as e:
        raise HTTPException(400, f"load failed: {e}")


# ── settings ────────────────────────────────────────────────────
@app.get("/api/settings")
def get_settings():
    return {"global": settings.get_global(), "local": settings.all_locals()}


@app.put("/api/settings/global")
async def put_global(req: Request):
    return {"global": settings.set_global(await req.json())}


@app.put("/api/settings/local/{item_id}")
async def put_local(item_id: str, req: Request, reset: str | None = Query(None)):
    if reset is not None:
        return {"local": settings.reset_local(item_id, None if reset == "*" else reset)}
    return {"local": settings.set_local(item_id, await req.json())}


# ── static ──────────────────────────────────────────────────────
@app.get("/")
def index():
    return FileResponse(STATIC_DIR / "index.html")


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
