"""Global settings + per-image local overrides.

The local override stores ONLY the fields the user actually touched, so a deep
merge over global means an un-overridden section keeps tracking global edits
live. Resetting a section just drops those keys.

`roi` is stored NORMALIZED (0-1) rather than in pixels: a 6000x4000 source and
a ~1200px preview canvas are different scales, and the ROI has to mean the same
thing on every frame in the series.
"""
from __future__ import annotations

import copy
import json
import os
import threading
from pathlib import Path

# For `filters._coerce` on the read path — see get_global(). No import cycle:
# filters imports numpy/cv2 and only pulls in gpu lazily inside functions.
from . import filters

DATA_DIR = Path(os.environ.get("DATA_DIR", "/data"))
STATE_FILE = DATA_DIR / "settings.json"

_lock = threading.RLock()
_global: dict = {}
_local: dict[str, dict] = {}   # image_id -> partial override


def _ident() -> list[list[int]]:
    return [[0, 0], [255, 255]]


def defaults() -> dict:
    """A fresh session.

    Delegates to filters.defaults() rather than rebuilding the shape here. That
    duplication was the source of drift twice already (a parameter added to the
    schema and then missing from the defaults, surfacing as a KeyError deep in
    the render pipeline), and the filter model is now defined in exactly one
    place.

    Channel `amount` defaults to 1.0 — every channel fully present. Nothing is
    isolated until the user drags a contribution to 0, because the right
    channel depends on the experiment and a hardcoded default would bake a
    guess into the UI.
    """
    from .filters import defaults as fdefaults

    return fdefaults()


def deep_merge(base: dict, over: dict) -> dict:
    """Recursive dict merge. Lists (curves, order) are replaced wholesale —
    a partial curve list would be nonsense."""
    out = copy.deepcopy(base)
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = copy.deepcopy(v)
    return out


def get_global() -> dict:
    # Coerced on the way OUT, not only at render time. `filters._coerce` is what
    # appends newly-added operations to `op_order`, and it used to run only from
    # render(): so a config saved before an op existed still reported the old
    # order over the API, the frontend built its cards from that list, and the
    # new op was simply INVISIBLE until something else happened to trigger a
    # save. Adding an operation to the schema has to be enough to make it appear.
    with _lock:
        return filters._coerce(copy.deepcopy(_global))


def set_global(patch: dict) -> dict:
    """Deep-merge the patch. A shallow update would drop the filter's sibling
    params — PATCHing {"levels": {"r_black": 40}} must not erase a r_white the
    user set earlier. It is masked by effective() re-filling from defaults(), so
    it does not crash; it silently discards tuning."""
    with _lock:
        merged = deep_merge(_global, copy.deepcopy(patch or {}))
        _global.clear()
        _global.update(merged)
        _save()
        return copy.deepcopy(_global)


def get_local(image_id: str) -> dict:
    with _lock:
        return copy.deepcopy(_local.get(image_id, {}))


def set_local(image_id: str, patch: dict) -> dict:
    """Deep-merge for the same reason as set_global — a local override is
    always a partial patch of one filter's params."""
    with _lock:
        cur = deep_merge(_local.get(image_id, {}), copy.deepcopy(patch or {}))
        _local[image_id] = cur
        _save()
        return copy.deepcopy(cur)


def reset_local(image_id: str, section: str | None = None) -> dict:
    """Drop a local override, and return what remains for that image.

    `section` is a dotted PATH ("channels.r.ops.clahe"), not a top-level key.
    The old single-level pop could not express the region/channel/ops tree, so
    "reset this operation" silently did nothing — it wrote defaults over the
    override instead of removing it, leaving the card permanently marked as
    locally set. Intermediate dicts that become empty are pruned, so an image
    with no overrides left disappears from the map entirely.
    """
    with _lock:
        if image_id not in _local:
            return {}
        if not section:
            _local.pop(image_id)
        else:
            parts = section.split(".")
            *parents, leaf = parts
            # Walk down, remembering each node AND the key that reached it, so
            # pruning can walk back up. Pruning from the root with a reversed
            # parent list looks right and silently does nothing, because the
            # deepest parent is not a direct child of the root.
            chain: list[tuple[dict, str]] = []
            node = _local[image_id]
            for p in parents:
                if not isinstance(node, dict) or not isinstance(node.get(p), dict):
                    node = None
                    break
                chain.append((node, p))
                node = node[p]
            if isinstance(node, dict):
                node.pop(leaf, None)
            # Prune emptied ancestors, innermost first.
            for parent, key in reversed(chain):
                if not parent[key]:
                    parent.pop(key)
                else:
                    break
            if not _local[image_id]:
                _local.pop(image_id)
        _save()
        return copy.deepcopy(_local.get(image_id, {}))


def effective(image_id: str | None) -> dict:
    """Global merged with this image's local override. This is the single
    function that decides what any given frame renders with.

    Seeded from defaults() so the result is never a half-empty schema — a
    missing filter key would otherwise surface much later as a KeyError inside
    the render pipeline rather than here.
    """
    with _lock:
        base = deep_merge(defaults(), _global)
        over = copy.deepcopy(_local.get(image_id, {})) if image_id else {}
    return deep_merge(base, over)


def export_config() -> dict:
    """Portable snapshot of everything the user tuned: global settings plus
    every per-image override. This is what the Save button writes."""
    with _lock:
        return {"version": 1, "global": copy.deepcopy(_global),
                "local": copy.deepcopy(_local)}


def import_config(payload: dict, replace: bool = True) -> dict:
    """Load a saved config. With replace=False the file's values are merged
    over the current ones, so a shared 'base look' config can be layered under
    per-experiment tweaks. Always seeded from defaults() so a config saved by
    an older build can't leave a filter key missing."""
    if not isinstance(payload, dict):
        raise ValueError("config must be a JSON object")
    with _lock:
        if replace:
            _global.clear()
            _local.clear()
        _global.update(deep_merge(defaults(), payload.get("global", {})))
        for image_id, over in (payload.get("local") or {}).items():
            if isinstance(image_id, str) and isinstance(over, dict):
                _local.setdefault(image_id, {}).update(copy.deepcopy(over))
        _save()
        return export_config()


def all_locals() -> dict:
    with _lock:
        return copy.deepcopy(_local)


def get_roi(image_id: str | None) -> list | None:
    r = effective(image_id).get("roi")
    return list(r) if r else None


def _save():
    """Atomic tmp+rename, matching library._save — a half-written settings file
    would silently reset every filter the user tuned."""
    try:
        STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = STATE_FILE.with_suffix(".json.tmp")
        tmp.write_text(json.dumps({"global": _global, "local": _local}, indent=1))
        tmp.replace(STATE_FILE)
    except Exception:
        pass


def load():
    with _lock:
        if not _global:
            _global.update(defaults())
    try:
        payload = json.loads(STATE_FILE.read_text())
        with _lock:
            _global.update(payload.get("global", {}))
            _local.update(payload.get("local", {}))
    except Exception:
        pass
    with _lock:
        if not _global:
            _global.update(defaults())
