"""Interpreter-level tuning for the BTC 5m sniper hot path (all opt-in).

Every helper here is inert unless its config flag is set:

* ``HotPathGc``     - ``gc_freeze_hotpath`` (+ optional gen0/gen2 thresholds and
                      safe-point collections). Records every collector pause with
                      ``gc.callbacks`` so the effect is measurable.

Nothing here is read by a trading decision. ``TickEffects`` never buffers
``save``: a state snapshot must be taken at call time, or a crash before an
order could lose the attempt marker.
"""
from __future__ import annotations

import gc
import threading
import time
from typing import Any, Callable, Optional

# CPython 3.11/3.12 defaults; used to report what a knob changed.
DEFAULT_THRESHOLDS = (700, 10, 10)
STATS_EVERY_S = 300.0
BOUNDARY_S = 300


def _percentile(values: list[float], q: float) -> Optional[float]:
    if not values:
        return None
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    pos = (len(ordered) - 1) * q
    low = int(pos)
    high = min(low + 1, len(ordered) - 1)
    frac = pos - low
    return ordered[low] + (ordered[high] - ordered[low]) * frac


class GcPauseRecorder:
    """Collector pause distribution from ``gc.callbacks`` (nanoseconds in, us out)."""

    def __init__(self, *, clock: Callable[[], int] = time.perf_counter_ns,
                 wall: Callable[[], float] = time.time, max_samples: int = 8192):
        self._clock, self._wall = clock, wall
        self._max_samples = max(64, int(max_samples))
        self._lock = threading.Lock()
        self._samples: list[float] = []
        self._generation: list[int] = [0, 0, 0]
        self._collections = 0
        self._started_ns: list[int] = []
        self._installed = False
        self._t0 = wall()

    def install(self) -> bool:
        if self._installed:
            return False
        gc.callbacks.append(self._callback)
        self._installed = True
        return True

    def uninstall(self) -> bool:
        if not self._installed:
            return False
        try:
            gc.callbacks.remove(self._callback)
        except ValueError:
            pass
        self._installed = False
        return True

    def _callback(self, phase: str, info: dict) -> None:
        # A collector run can nest when a weakref callback allocates; keep a stack.
        if phase == "start":
            self._started_ns.append(self._clock())
            return
        if not self._started_ns:
            return
        started = self._started_ns.pop()
        duration_ns = self._clock() - started
        generation = int(info.get("generation", 0) or 0)
        with self._lock:
            self._collections += 1
            if 0 <= generation < 3:
                self._generation[generation] += 1
            self._samples.append(duration_ns / 1000.0)
            if len(self._samples) > self._max_samples:
                del self._samples[: len(self._samples) - self._max_samples]

    def stats(self, *, reset: bool = False) -> dict:
        with self._lock:
            samples = list(self._samples)
            generation = list(self._generation)
            collections = self._collections
            if reset:
                self._samples = []
                self._generation = [0, 0, 0]
                self._collections = 0
        return {
            "gc_collections": collections,
            "gc_gen0": generation[0],
            "gc_gen1": generation[1],
            "gc_gen2": generation[2],
            "gc_pause_p50_us": None if not samples else round(_percentile(samples, 0.5), 1),
            "gc_pause_p90_us": None if not samples else round(_percentile(samples, 0.9), 1),
            "gc_pause_p99_us": None if not samples else round(_percentile(samples, 0.99), 1),
            "gc_pause_max_us": None if not samples else round(max(samples), 1),
        }


class HotPathGc:
    """``gc_freeze_hotpath``: startup freeze plus optional threshold/safe-point tuning.

    ``arm()`` runs after the bot's warmup so the long-lived startup objects leave
    the collector's tracking set. ``maybe_collect(now, in_flight)`` is the only
    place a full collection runs while the flag is on: it fires once per 5-minute
    boundary, and only when no order is in flight.
    """

    def __init__(self, cfg: Callable[[], dict], *, log: Callable[..., None] = lambda *a, **k: None,
                 recorder: Optional[GcPauseRecorder] = None, wall: Callable[[], float] = time.time):
        self.cfg, self.log, self.wall = cfg, log, wall
        self.recorder = recorder or GcPauseRecorder(wall=wall)
        self._armed = False
        self._next_boundary: Optional[float] = None
        self._next_stats = 0.0
        self._collections = 0
        self._last_collect_ms: Optional[float] = None
        self.applied: dict[str, Any] = {}

    def enabled(self) -> bool:
        try:
            return bool(self.cfg().get("gc_freeze_hotpath", False))
        except Exception:
            return False

    def arm(self, now: Optional[float] = None) -> dict:
        """Enable the flag: collect, freeze, apply thresholds, install the recorder."""
        if self._armed:
            return dict(self.applied)
        cfg = dict(self.cfg() or {})
        before = gc.get_threshold()
        collected_before = gc.collect()
        gc.freeze()
        applied: dict[str, Any] = {
            "gc_threshold_before": list(before),
            "gc_collect_before_freeze": collected_before,
            "gc_frozen": gc.get_freeze_count(),
        }
        gen0 = int(cfg.get("gc_hotpath_gen0_threshold") or 0)
        gen2 = int(cfg.get("gc_hotpath_gen2_threshold") or 0)
        if gen0 > 0 or gen2 > 0:
            threshold = (gen0 if gen0 > 0 else before[0], before[1], gen2 if gen2 > 0 else before[2])
            gc.set_threshold(*threshold)
        applied["gc_threshold_after"] = list(gc.get_threshold())
        applied["gc_boundary_collect"] = bool(cfg.get("gc_hotpath_boundary_collect", False))
        self.recorder.install()
        self._armed = True
        now = self.wall() if now is None else float(now)
        self._next_boundary = (int(now // BOUNDARY_S) + 1) * BOUNDARY_S
        self._next_stats = now + STATS_EVERY_S
        self.applied = dict(applied)
        self.log("gc_hotpath_armed", **applied)
        return dict(applied)

    def disarm(self) -> None:
        if not self._armed:
            return
        self.recorder.uninstall()
        try:
            gc.unfreeze()
        except Exception:
            pass
        self._armed = False
        self._next_boundary = None

    def stats(self, *, reset: bool = True) -> dict:
        out = self.recorder.stats(reset=reset)
        out["gc_boundary_collections"] = self._collections
        out["gc_last_collect_ms"] = self._last_collect_ms
        return out

    def next_boundary(self) -> Optional[float]:
        return self._next_boundary

    def due(self, now: float) -> bool:
        """True when a safe point came up since the last collection."""
        if not self._armed or self._next_boundary is None:
            return False
        if float(now) < self._next_boundary:
            return False
        offset = 0.0
        try:
            offset = float((self.cfg() or {}).get("gc_hotpath_boundary_offset_s") or 0.0)
        except Exception:
            offset = 0.0
        return float(now) >= self._next_boundary + max(0.0, offset)

    def maybe_collect(self, now: float, in_flight: int = 0) -> bool:
        """Collect at a safe point (after a 5m boundary, nothing in flight)."""
        if not self._armed:
            return False
        cfg = self.cfg() or {}
        if not bool(cfg.get("gc_hotpath_boundary_collect", False)):
            self._maybe_log_stats(now, cfg)
            return False
        if in_flight:
            return False
        if not self.due(now):
            self._maybe_log_stats(now, cfg)
            return False
        started = time.perf_counter()
        collected = gc.collect()
        self._last_collect_ms = round((time.perf_counter() - started) * 1000.0, 3)
        self._collections += 1
        self._next_boundary = (int(float(now) // BOUNDARY_S) + 1) * BOUNDARY_S
        self.log("gc_hotpath_boundary_collect", collected=collected,
                 duration_ms=self._last_collect_ms, boundary=self._next_boundary)
        self._maybe_log_stats(now, cfg)
        return True

    def _maybe_log_stats(self, now: float, cfg: dict) -> None:
        if float(now) < self._next_stats:
            return
        self._next_stats = float(now) + STATS_EVERY_S
        try:
            self.log("gc_hotpath_stats", **self.stats())
        except Exception:
            pass


class TickEffects:
    """Buffer a strategy tick's pure side effects and replay them after unlock.

    Only ``log`` and ``alert`` are buffered: both return ``None`` and no decision
    reads them. ``log_fields`` also defers building the payload, for the one
    journal line whose fields are a deep copy of the tick's books.

    The original bound callables are captured here, so swapping ``io.log`` for
    ``effects.log`` while a tick runs cannot recurse. Calls from any thread other
    than the owner pass straight through; ``flush()`` replays in call order so the
    journal keeps its sequence.
    """

    def __init__(self, io: Any, *, owner: Optional[int] = None):
        self._io = io
        self._log = io.log
        self._alert = io.alert
        self._owner = threading.get_ident() if owner is None else int(owner)
        self._items: list[tuple[str, tuple, Any]] = []
        self.flushed = 0

    def __len__(self) -> int:
        return len(self._items)

    def _mine(self) -> bool:
        return threading.get_ident() == self._owner

    def log(self, event: str, **kwargs: Any) -> None:
        if not self._mine():
            self._log(event, **kwargs)
            return
        self._items.append(("log", (event,), kwargs))

    def log_fields(self, event: str, builder: Callable[[], dict]) -> None:
        """Log ``event`` with fields built at flush time (still inside the tick's thread)."""
        if not self._mine():
            self._log(event, **builder())
            return
        self._items.append(("fields", (event, builder), None))

    def alert(self, text: str, *, kind: str) -> None:
        if not self._mine():
            self._alert(text, kind=kind)
            return
        self._items.append(("alert", (text,), {"kind": kind}))

    def flush(self) -> int:
        """Replay buffered calls on the calling thread; returns how many ran."""
        items, self._items = self._items, []
        for kind, args, kwargs in items:
            try:
                if kind == "log":
                    self._log(*args, **kwargs)
                elif kind == "fields":
                    event, builder = args
                    self._log(event, **builder())
                else:
                    self._alert(*args, **kwargs)
            except Exception:
                # Logging must never break a tick; matches Strategy.alert.
                continue
        self.flushed += len(items)
        return len(items)
