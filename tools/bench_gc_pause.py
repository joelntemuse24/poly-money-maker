#!/usr/bin/env python3
"""Measure the GC pause distribution of the 5m hot path, before and after each knob.

Three configurations, each driving the same scripted tick stream (entries, dumps,
journal lines) while every collector pause is recorded through ``gc.callbacks``:

``off``
    ``gc_freeze_hotpath`` false: the bot as deployed today.

``freeze``
    ``gc_freeze_hotpath`` true: ``gc.collect(); gc.freeze()`` after warmup.

``freeze+thresholds``
    freeze plus raised gen0/gen2 thresholds and one collection per 5m boundary at
    a safe point (``gc_hotpath_boundary_collect``).

The number that matters is the pause a feed thread can be stopped by, so p99/max
of the collector's own duration is the metric, not throughput.

``--heap`` allocates that many small long-lived containers before the run, standing
in for the bot's startup objects (client objects, config dicts, the 15m state
tree). ``gc.freeze()`` moves them out of the collector's tracking set, so its
benefit scales with this number and is invisible on a tiny heap. That is the
honest shape of the measure: report it at both sizes.

Usage:
    python tools/bench_gc_pause.py --ticks 400 --heap 0 --json /tmp/gc_small.json
    python tools/bench_gc_pause.py --ticks 400 --heap 400000 --json /tmp/gc_big.json
"""
from __future__ import annotations

import argparse
import gc
import json
import sys
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).resolve().parents[1]
for extra in (ROOT, ROOT / "tests"):
    if str(extra) not in sys.path:
        sys.path.insert(0, str(extra))

from buy.hotpath import GcPauseRecorder, HotPathGc  # noqa: E402
from test_tick_lock_short import Harness, _book  # noqa: E402

DEFAULT_TICKS = 400
DEFAULT_HEAP = 0
CYCLE = [
    {"up": _book(0.50, 0.52), "down": _book(0.48, 0.50)},
    {"up": _book(0.05, 0.06), "down": _book(0.93, 0.95)},
    {"up": _book(0.04, 0.05), "down": _book(0.94, 0.96)},
    {"up": _book(0.75, 0.78), "down": _book(0.18, 0.20)},
    {"up": _book(0.75, 0.78), "down": _book(0.18, 0.20)},
    {"up": _book(0.75, 0.78), "down": _book(0.18, 0.20)},
]
STEP_S = 3.5
# A real epoch so the 5m boundary safe point actually comes up during the run.
BASE_S = 1_700_000_000.0


def _cfg(mode: str) -> dict:
    cfg = {"gc_freeze_hotpath": mode != "off", "gc_hotpath_boundary_collect": False}
    if mode == "freeze+thresholds":
        cfg.update(gc_hotpath_gen0_threshold=20_000, gc_hotpath_gen2_threshold=5_000,
                   gc_hotpath_boundary_collect=True, gc_hotpath_boundary_offset_s=0.0)
    return cfg


def _live_heap(n: int) -> list:
    """Long-lived containers, like the objects a running bot already holds."""
    return [{"i": i, "v": [i, i + 1, i + 2], "s": f"obj-{i}"} for i in range(n)]


def _run(mode: str, ticks: int, heap: int = 0) -> dict:
    cfg = _cfg(mode)
    baseline_threshold = gc.get_threshold()
    recorder = GcPauseRecorder()
    recorder.install()
    gc_ctl = HotPathGc(lambda: cfg, log=lambda *a, **k: None, recorder=recorder)
    harness = Harness(short=True)
    live = _live_heap(heap)
    collected = 0
    try:
        if cfg["gc_freeze_hotpath"]:
            gc_ctl.arm(now=BASE_S)
        harness.runner.hotpath_gc = gc_ctl
        for n in range(ticks):
            base = BASE_S + n * len(CYCLE) * STEP_S
            harness.reset_market(base, ttm_s=len(CYCLE) * STEP_S)
            for idx, quotes in enumerate(CYCLE):
                harness.step(base + idx * STEP_S, quotes)
            # The safe point the Runner calls at the top of every tick.
            if gc_ctl.maybe_collect(harness.clock[0], 0):
                collected += 1
    finally:
        recorder.uninstall()
        gc_ctl.disarm()
        gc.set_threshold(*baseline_threshold)
    stats = recorder.stats(reset=True)
    stats["mode"] = mode
    stats["orders"] = len(harness.buys) + len(harness.sells)
    stats["boundary_collections"] = collected
    stats["threshold"] = list(gc.get_threshold())
    stats["heap_objects"] = len(live)
    return stats


def run(ticks: int = DEFAULT_TICKS, heap: int = DEFAULT_HEAP) -> list[dict]:
    return [_run(mode, ticks, heap) for mode in ("off", "freeze", "freeze+thresholds")]


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--ticks", type=int, default=DEFAULT_TICKS)
    parser.add_argument("--heap", type=int, default=DEFAULT_HEAP,
                        help="long-lived containers allocated before the run")
    parser.add_argument("--json", help="write the results here as JSON")
    args = parser.parse_args(argv)

    rows = run(args.ticks, args.heap)
    print(f"heap objects: {rows[0]['heap_objects']}")
    print(f"{'mode':>18} {'collects':>9} {'gen0':>7} {'gen1':>6} {'gen2':>6} "
          f"{'p50 us':>9} {'p90 us':>9} {'p99 us':>9} {'max us':>9} {'orders':>7}")
    for row in rows:
        print(f"{row['mode']:>18} {row['gc_collections']:>9} {row['gc_gen0']:>7} "
              f"{row['gc_gen1']:>6} {row['gc_gen2']:>6} "
              f"{str(row['gc_pause_p50_us']):>9} {str(row['gc_pause_p90_us']):>9} "
              f"{str(row['gc_pause_p99_us']):>9} {str(row['gc_pause_max_us']):>9} "
              f"{row['orders']:>7}")
    if args.json:
        Path(args.json).write_text(json.dumps(rows, indent=2) + "\n", encoding="utf-8")
        print(f"wrote {args.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
