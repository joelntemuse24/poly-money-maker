"""gc_freeze_hotpath and thread_switch_interval_s: opt-in interpreter tuning."""
from __future__ import annotations

import ast
import gc
import json
import sys
import threading
import time
import unittest
from pathlib import Path

from buy import hotpath
from buy.fivemin import DEFAULTS as FM_DEFAULTS
from buy.hotpath import (
    DEFAULT_THRESHOLDS, GcPauseRecorder, HotPathGc, apply_switch_interval,
)

ROOT = Path(__file__).resolve().parents[1]
MINT = ROOT / "mintbot.py"


def _defaults() -> dict:
    tree = ast.parse(MINT.read_text(), filename=str(MINT))
    for node in tree.body:
        if isinstance(node, ast.Assign) and getattr(node.targets[0], "id", None) == "DEFAULTS":
            return ast.literal_eval(node.value)
    raise AssertionError("DEFAULTS not found")


class FlagDefaultsTest(unittest.TestCase):
    def test_both_flags_default_off_in_both_default_tables(self):
        defaults = _defaults()
        for key in ("gc_freeze_hotpath", "thread_switch_interval_s", "tick_lock_short",
                    "feed_process"):
            self.assertIn(key, defaults)
        self.assertIs(defaults["gc_freeze_hotpath"], False)
        self.assertEqual(defaults["thread_switch_interval_s"], 0.0)
        self.assertIs(defaults["tick_lock_short"], False)
        self.assertIs(defaults["feed_process"], False)
        self.assertIs(FM_DEFAULTS["gc_freeze_hotpath"], False)
        self.assertEqual(FM_DEFAULTS["thread_switch_interval_s"], 0.0)

    def test_validate_strategy_accepts_defaults_and_rejects_bad_values(self):
        tree = ast.parse(MINT.read_text(), filename=str(MINT))
        node = next(n for n in tree.body
                    if isinstance(n, ast.FunctionDef) and n.name == "validate_strategy")
        from buy.mint_gas import validate_mint_gas
        from buy.mint_sequence import validate_seq
        from buy.mint_redeem import validate_redeem

        ns = {"validate_mint_gas": validate_mint_gas, "validate_seq": validate_seq,
              "validate_redeem": validate_redeem}
        exec(compile(ast.Module(body=[node], type_ignores=[]), "<validate>", "exec"), ns)
        validate = ns["validate_strategy"]
        good = _defaults()
        validate(good)
        for key, value in (("gc_freeze_hotpath", "yes"), ("tick_lock_short", 1),
                           ("feed_process", None), ("thread_switch_interval_s", -1),
                           ("thread_switch_interval_s", "fast"),
                           ("feed_process_trigger_ttl_ms", 0),
                           ("gc_hotpath_boundary_offset_s", 10_000)):
            cfg = dict(good)
            cfg[key] = value
            with self.assertRaises(ValueError, msg=key):
                validate(cfg)

    def test_mintbot_calls_switch_interval_before_startup_log(self):
        src = MINT.read_text()
        main = src[src.find("def main() -> int:"):]
        self.assertIn("apply_switch_interval(cfg, log=log_event)", main)
        self.assertLess(main.find("apply_switch_interval("), main.find('log_event(\n        "startup"'))
        self.assertIn("HotPathGc", src)


class GcRecorderTest(unittest.TestCase):
    def test_recorder_tracks_pauses_per_generation(self):
        recorder = GcPauseRecorder()
        self.assertTrue(recorder.install())
        try:
            gc.collect(0)
            gc.collect(1)
            gc.collect(2)
        finally:
            self.assertTrue(recorder.uninstall())
        stats = recorder.stats()
        self.assertEqual(stats["gc_collections"], 3)
        self.assertEqual(stats["gc_gen0"], 1)
        self.assertEqual(stats["gc_gen1"], 1)
        self.assertEqual(stats["gc_gen2"], 1)
        for key in ("gc_pause_p50_us", "gc_pause_p90_us", "gc_pause_p99_us", "gc_pause_max_us"):
            self.assertIsInstance(stats[key], float)
            self.assertGreaterEqual(stats[key], 0.0)

    def test_uninstall_is_idempotent_and_stops_recording(self):
        recorder = GcPauseRecorder()
        recorder.install()
        recorder.uninstall()
        self.assertFalse(recorder.uninstall())
        before = recorder.stats()["gc_collections"]
        gc.collect()
        self.assertEqual(recorder.stats()["gc_collections"], before)

    def test_stats_reset_clears_samples(self):
        recorder = GcPauseRecorder()
        recorder.install()
        try:
            gc.collect()
        finally:
            recorder.uninstall()
        first = recorder.stats(reset=True)
        second = recorder.stats()
        self.assertEqual(first["gc_collections"], 1)
        self.assertEqual(second["gc_collections"], 0)
        self.assertIsNone(second["gc_pause_p50_us"])


class HotPathGcTest(unittest.TestCase):
    def setUp(self):
        self.logs = []
        self.threshold = gc.get_threshold()
        self.frozen = gc.get_freeze_count()

    def tearDown(self):
        gc.set_threshold(*self.threshold)
        gc.unfreeze()

    def _gc(self, **cfg):
        base = {"gc_freeze_hotpath": True, "gc_hotpath_boundary_collect": False,
                "gc_hotpath_boundary_offset_s": 0.0}
        base.update(cfg)
        return HotPathGc(lambda: base, log=lambda e, **k: self.logs.append((e, k)),
                         wall=lambda: 1_000_000.0)

    def test_arm_freezes_collects_and_logs_thresholds(self):
        obj = self._gc()
        applied = obj.arm(now=1_000_000.0)
        try:
            self.assertGreaterEqual(gc.get_freeze_count(), self.frozen)
            self.assertEqual(applied["gc_threshold_before"], list(self.threshold))
            self.assertEqual(applied["gc_threshold_after"], list(self.threshold))
            self.assertIn("gc_frozen", applied)
            self.assertEqual([e for e, _ in self.logs], ["gc_hotpath_armed"])
        finally:
            obj.disarm()

    def test_threshold_knobs_apply_and_restore(self):
        obj = self._gc(gc_hotpath_gen0_threshold=20_000, gc_hotpath_gen2_threshold=5_000)
        obj.arm(now=1_000_000.0)
        try:
            self.assertEqual(gc.get_threshold(), (20_000, self.threshold[1], 5_000))
        finally:
            obj.disarm()
            gc.set_threshold(*self.threshold)
        self.assertEqual(gc.get_threshold(), self.threshold)

    def test_arm_is_idempotent_and_disarm_unfreezes(self):
        obj = self._gc()
        obj.arm(now=1_000_000.0)
        obj.arm(now=1_000_000.0)
        self.assertEqual(sum(e == "gc_hotpath_armed" for e, _ in self.logs), 1)
        obj.disarm()
        self.assertEqual(gc.get_freeze_count(), 0)
        obj.disarm()

    def test_boundary_safe_point_needs_flag_boundary_and_no_order(self):
        obj = self._gc(gc_hotpath_boundary_collect=True)
        obj.arm(now=1_000_000.0)
        boundary = obj.next_boundary()
        try:
            self.assertEqual(boundary % 300, 0)
            self.assertFalse(obj.maybe_collect(1_000_000.5, 0))
            self.assertFalse(obj.maybe_collect(boundary - 0.1, 0))
            # An order in flight blocks the collection even past the boundary.
            self.assertFalse(obj.maybe_collect(boundary + 0.1, 1))
            self.assertFalse(obj.maybe_collect(boundary + 0.2, 2))
            self.assertEqual(obj.stats(reset=False)["gc_boundary_collections"], 0)
            self.assertTrue(obj.maybe_collect(boundary + 0.3, 0))
            self.assertEqual(obj.stats(reset=False)["gc_boundary_collections"], 1)
            self.assertFalse(obj.maybe_collect(boundary + 0.4, 0))
        finally:
            obj.disarm()
        events = [e for e, _ in self.logs]
        self.assertIn("gc_hotpath_boundary_collect", events)
        fields = next(k for e, k in self.logs if e == "gc_hotpath_boundary_collect")
        self.assertIn("duration_ms", fields)
        self.assertEqual(fields["boundary"], boundary + 300)

    def test_boundary_collect_off_by_default_is_a_noop(self):
        obj = self._gc()
        obj.arm(now=1_000_000.0)
        try:
            boundary = obj.next_boundary()
            self.assertFalse(obj.maybe_collect(boundary + 5.0, 0))
            self.assertEqual(obj.stats(reset=False)["gc_boundary_collections"], 0)
        finally:
            obj.disarm()
        self.assertFalse([e for e, _ in self.logs if e == "gc_hotpath_boundary_collect"])

    def test_offset_delays_the_safe_point(self):
        obj = self._gc(gc_hotpath_boundary_collect=True, gc_hotpath_boundary_offset_s=2.0)
        obj.arm(now=1_000_000.0)
        boundary = obj.next_boundary()
        try:
            self.assertFalse(obj.maybe_collect(boundary + 0.5, 0))
            self.assertFalse(obj.due(boundary + 0.5))
            self.assertTrue(obj.due(boundary + 2.5))
            self.assertTrue(obj.maybe_collect(boundary + 2.6, 0))
        finally:
            obj.disarm()

    def test_disabled_flag_does_nothing(self):
        obj = HotPathGc(lambda: {"gc_freeze_hotpath": False}, log=lambda *a, **k: None)
        self.assertFalse(obj.enabled())
        self.assertFalse(obj.maybe_collect(1_000_600.0, 0))
        self.assertFalse(obj.due(1_000_600.0))
        obj.disarm()

    def test_stats_are_logged_at_most_every_interval(self):
        obj = self._gc(gc_hotpath_boundary_collect=True)
        obj.arm(now=1_000_000.0)
        try:
            boundary = obj.next_boundary()
            start = max(boundary + 0.5, 1_000_300.5)
            for now in (start, start + 0.5, start + 1.0):
                obj.maybe_collect(now, 0)
            stats = [k for e, k in self.logs if e == "gc_hotpath_stats"]
            self.assertEqual(len(stats), 1)
            for key in ("gc_pause_p50_us", "gc_pause_p99_us", "gc_collections"):
                self.assertIn(key, stats[0])
        finally:
            obj.disarm()


class SwitchIntervalTest(unittest.TestCase):
    def test_zero_leaves_the_interpreter_alone(self):
        current = sys.getswitchinterval()
        self.assertIsNone(apply_switch_interval({"thread_switch_interval_s": 0.0}))
        self.assertEqual(sys.getswitchinterval(), current)
        self.assertIsNone(apply_switch_interval({}))
        self.assertIsNone(apply_switch_interval({"thread_switch_interval_s": None}))
        self.assertEqual(sys.getswitchinterval(), current)

    def test_value_is_applied_and_logged(self):
        current = sys.getswitchinterval()
        logs = []
        try:
            applied = apply_switch_interval({"thread_switch_interval_s": 0.0005},
                                            log=lambda e, **k: logs.append((e, k)))
            self.assertAlmostEqual(applied, 0.0005)
            self.assertAlmostEqual(sys.getswitchinterval(), 0.0005)
            self.assertEqual(logs[0][0], "thread_switch_interval")
            self.assertAlmostEqual(logs[0][1]["after_s"], 0.0005)
        finally:
            sys.setswitchinterval(current)
        self.assertEqual(sys.getswitchinterval(), current)

    def test_setter_injection_avoids_global_state(self):
        seen = {}
        value = apply_switch_interval(
            {"thread_switch_interval_s": 0.001},
            setter=lambda v: seen.setdefault("set", v), getter=lambda: seen.get("set", 0.005),
        )
        self.assertAlmostEqual(value, 0.001)
        self.assertAlmostEqual(seen["set"], 0.001)
        self.assertEqual(sys.getswitchinterval(), seen.get("orig", sys.getswitchinterval()))


class GilHandoffBenchmarkTest(unittest.TestCase):
    """The benchmark helper exists and measures a real handoff; not a latency gate."""

    def test_synthetic_handoff_benchmark_returns_percentiles(self):
        from tools.bench_thread_switch import measure_handoff, measure_throughput

        stats = measure_handoff(None, spins=60)
        self.assertIsNone(stats["interval_s"])
        self.assertEqual(stats["samples"], 60)
        for key in ("p50_us", "p99_us", "max_us"):
            self.assertGreater(stats["p50_us"], 0.0)
            self.assertGreaterEqual(stats[key], stats["p50_us"])
        # Shortening the interval must not change what is measured, only how fast
        # the blocked thread is handed the GIL; the helper records the request.
        short = measure_handoff(0.0005, spins=60)
        self.assertAlmostEqual(short["interval_s"], 0.0005)
        self.assertGreater(short["p99_us"], 0.0)
        self.assertGreater(measure_throughput(None, duration_s=0.05)["iters_per_s"], 0)


if __name__ == "__main__":
    unittest.main()
