"""Config file on disk — where a saved look lives and how it is found.

The default home for a config is the EXPERIMENT FOLDER (the images dir), so a
config sits next to the frames it describes and travels with the data. If that
mount is read-only, saving falls back to the data dir rather than failing.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

from . import settings

DATA_DIR = Path(os.environ.get("DATA_DIR", "/data"))
CONFIG_NAME = "fluoro-editor.config.json"
MAX_CONFIG_BYTES = 8 * 1024 * 1024


def images_dir() -> Path:
    return Path(os.environ.get("IMAGES_DIR", "/images"))


def candidate_dirs() -> list[Path]:
    """Searched in order when loading: the live images dir first (that's where
    a folder the user just loaded from will have written its config), then the
    data dir for configs saved while the mount was read-only."""
    seen, out = set(), []
    for d in (images_dir(), DATA_DIR):
        r = d.resolve() if d.exists() else d
        if str(r) not in seen:
            seen.add(str(r))
            out.append(r)
    return out


def find() -> Path | None:
    for d in candidate_dirs():
        p = d / CONFIG_NAME
        if p.is_file():
            return p
    return None


def describe() -> dict:
    p = find()
    return {"exists": p is not None,
            "name": CONFIG_NAME,
            "path": str(p) if p else None,
            "searched": [str(d) for d in candidate_dirs()],
            "writable": _writable(images_dir())}


def _writable(d: Path) -> bool:
    try:
        return d.is_dir() and os.access(d, os.W_OK)
    except Exception:
        return False


def target_path(explicit: str | None = None) -> Path:
    """Where Save will write. An explicit path wins; otherwise the images dir
    when writable, else the data dir."""
    if explicit:
        return Path(explicit)
    return (images_dir() if _writable(images_dir()) else DATA_DIR) / CONFIG_NAME


def save(path: str | None = None) -> dict:
    dest = target_path(path)
    dest.parent.mkdir(parents=True, exist_ok=True)
    payload = settings.export_config()
    # Atomic: a config half-written by a crash would lose the whole look.
    tmp = dest.with_suffix(dest.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=1))
    tmp.replace(dest)
    return {"saved": True, "path": str(dest),
            "globals": len(payload["global"]),
            "images": len(payload["local"])}


def load(path: str | None = None, replace: bool = True) -> dict:
    src = Path(path) if path else find()
    if src is None or not src.is_file():
        raise FileNotFoundError(
            f"no {CONFIG_NAME} found in {[str(d) for d in candidate_dirs()]}")
    if src.stat().st_size > MAX_CONFIG_BYTES:
        raise ValueError("config file is implausibly large; refusing to load")
    payload = json.loads(src.read_text())
    applied = settings.import_config(payload, replace=replace)
    return {"loaded": True, "path": str(src),
            "globals": len(applied["global"]), "images": len(applied["local"])}


def load_from_bytes(data: bytes, replace: bool = True) -> dict:
    """Config arriving via the browser as part of a folder drop."""
    if len(data) > MAX_CONFIG_BYTES:
        raise ValueError("config file is too large")
    applied = settings.import_config(json.loads(data.decode("utf-8")), replace)
    return {"loaded": True, "from": "upload",
            "globals": len(applied["global"]), "images": len(applied["local"])}
