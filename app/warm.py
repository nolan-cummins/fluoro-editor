"""Background render warming.

The interactive render is the latency the user actually feels: they move a
slider, they want pixels. Everything else in a 176-image library is better spent
being ready *before* they sweep to it — a cache hit costs ~6ms where a cold
full-res render costs 0.5-1.0s.

So: the requested image renders in the foreground, and a background worker
renders the rest into the same LRU, current image first so the frame the user is
looking at is the first thing guaranteed warm.

One worker by default. Two would roughly halve warm time, but they also compete
with the foreground render for CPU — and foreground latency is the thing the
user notices. Warming is a background nicety; keep the foreground fast.
Override with WARM_WORKERS.
"""
from __future__ import annotations

import os
import threading
import time
from collections import deque

from . import library, render, settings

WORKERS = max(1, int(os.environ.get("WARM_WORKERS", "1")))
MAX_SIDE = 1600


class Warmer:
    def __init__(self, workers: int = WORKERS, start_workers: bool = True):
        self._lock = threading.Lock()
        self._queue: deque[str] = deque()
        self._job: dict | None = None
        self._seq = 0
        self._cancel = threading.Event()
        # start_workers=False gives a Warmer with no background loop, for tests
        # that drive _next() by hand. Sharing a live worker with a hand-driven
        # simulation made the stale-job test a coin flip: the real thread could
        # legitimately credit a frame mid-assertion, which looks exactly like the
        # bug the test exists to catch.
        if start_workers:
            for _ in range(workers):
                threading.Thread(target=self._loop, daemon=True).start()

    # ── public ──────────────────────────────────────────────
    def start(self, order: list[str], first: str | None = None) -> dict:
        """(Re)start warming. Any in-flight job is cancelled: its output was
        rendered under the previous settings and is now worthless."""
        items = [i for i in order if library.get(i)]
        if first and first in items:
            items.remove(first)
            items.insert(0, first)          # the visible frame warms first
        with self._lock:
            self._cancel.set()
            self._cancel = threading.Event()
            self._seq += 1
            self._queue = deque(items)
            self._job = {
                "id": self._seq, "total": len(items), "done": 0,
                "failed": 0, "current": None, "running": bool(items),
                "finished": False, "cancelled": False,
                "started": time.time(), "eta": None,
            }
        return self.progress()

    def cancel(self) -> None:
        with self._lock:
            self._cancel.set()
            if self._job and not self._job["finished"]:
                self._job["cancelled"] = True
                self._job["running"] = False

    def progress(self) -> dict:
        with self._lock:
            if not self._job:
                return {"id": 0, "total": 0, "done": 0, "failed": 0, "current": None,
                        "running": False, "finished": True, "cancelled": False,
                        "eta": None, "pct": 100}
            j = dict(self._job)
            j["pct"] = round(100 * j["done"] / j["total"]) if j["total"] else 100
            if j["eta"] is None and j["done"] >= 2 and j["total"] > j["done"]:
                rate = (time.time() - j["started"]) / j["done"]
                j["eta"] = round(rate * (j["total"] - j["done"]))
            j["elapsed"] = round(time.time() - j["started"])
            return j

    # ── worker ──────────────────────────────────────────────
    def _next(self) -> tuple[str | None, int]:
        """Pop the next item. Returns (item_id, job_id) — the job_id is carried
        alongside the work so a render that finishes after the queue was
        replaced cannot credit the *new* job's counter."""
        with self._lock:
            if self._cancel.is_set():
                return None, 0
            if not self._queue:
                if self._job and not self._job["finished"]:
                    self._job["finished"] = True
                    self._job["running"] = False
                    self._job["current"] = None
                return None, 0
            item_id = self._queue.popleft()
            job_id = self._job["id"] if self._job else 0
            if self._job:
                self._job["current"] = (library.get(item_id) or {}).get("name")
            return item_id, job_id

    def _loop(self) -> None:
        while True:
            item_id, job_id = self._next()
            if item_id is None:
                time.sleep(0.15)
                continue
            ok = False
            try:
                # Stand down while the user is dragging. Warming the other 175
                # frames is a nicety; a slider tick queued behind a 700ms
                # full-res render measured 1331ms, which is the difference
                # between "instant" and "broken".
                render.wait_while_busy()
                if self._cancel.is_set():
                    continue
                # Each image renders with ITS OWN effective settings, so a
                # per-image local override is warmed correctly rather than
                # silently using the global value.
                #
                # cache_draft=True: the full-res pixels are already in hand, so
                # the 1024px draft source costs ~25ms here and saves a ~135ms
                # decode later. The array cache is sized to hold the whole sweep
                # window, so this no longer evicts what you are editing.
                #
                # interactive=False: do not count yourself as foreground. The
                # counter used to be a set/clear Event that this very call
                # raised, so the warmer stood down for its own work while a
                # user's render could not stand it down at all.
                render.render_jpeg(item_id, settings.effective(item_id), MAX_SIDE,
                                   cache_draft=True, interactive=False)
                ok = True
            except Exception:
                pass
            with self._lock:
                # Only credit the job this work belonged to.
                if self._job and self._job["id"] == job_id and not self._cancel.is_set():
                    self._job["done"] += 1
                    if not ok:
                        self._job["failed"] += 1


warmer = Warmer()
