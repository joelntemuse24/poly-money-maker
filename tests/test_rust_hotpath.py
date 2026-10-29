"""Parity tests for the Rust ``polyhot.TickRing`` window scan behind ``PriceGrid``."""
from __future__ import annotations

import collections
import os
import random
import unittest

from hypothesis import given, settings as hyp_settings, strategies as st

from buy.binance_trades import PriceGrid

polyhot = None
try:  # pragma: no cover - import guard
    import polyhot  # type: ignore  # noqa: F401
except Exception:  # pragma: no cover
    polyhot = None

requires_rust = unittest.skipIf(polyhot is None, "polyhot not importable")

SOURCES = ("primary", "extra_a", "extra_b")
FINITE_FLOAT = st.floats(min_value=-1e15, max_value=1e15, allow_nan=False, allow_infinity=False)
POSITIVE_FLOAT = st.floats(min_value=1e-6, max_value=1e9, allow_nan=False, allow_infinity=False)


def _ticks(n: int, rnd: random.Random) -> list[tuple[str, float, float, float]]:
    """(source, exchange_ms, recv_ms, px) with recv_ms non-monotonic and unsorted."""
    out = []
    base = 1_760_000_000_000.0
    for _ in range(n):
        source = rnd.choice(SOURCES)
        ex_ms = base + rnd.uniform(0.0, 120_000.0)
        recv_ms = ex_ms + rnd.uniform(-50.0, 50.0)  # out-of-order arrivals
        px = 80_000.0 + rnd.gauss(0.0, 40.0)
        out.append((source, ex_ms, recv_ms, max(px, 1e-6)))
    return out


def _feed(ticks) -> tuple[PriceGrid, PriceGrid]:
    py_grid, rs_grid = PriceGrid(use_rust=False), PriceGrid(use_rust=True)
    assert rs_grid.rust_enabled
    for source, ex_ms, recv_ms, px in ticks:
        py_grid.add(ex_ms, px, source=source, recv_ms=recv_ms)
        rs_grid.add(ex_ms, px, source=source, recv_ms=recv_ms)
    return py_grid, rs_grid


class TickRingParityTest(unittest.TestCase):
    @requires_rust
    def test_unit_tick_ring_matches_deque(self):
        rnd = random.Random(1234)
        for maxlen in (1, 2, 7, 64, 4096):
            ring = polyhot.TickRing(maxlen)
            ref: collections.deque = collections.deque(maxlen=maxlen)
            self.assertEqual(len(ring), 0)
            self.assertIsNone(ring.last())
            for _ in range(3 * maxlen + 5):
                ts, px = rnd.uniform(-1e6, 1e6), rnd.uniform(1.0, 1e5)
                ring.append(ts, px)
                ref.append((ts, px))
                self.assertEqual(len(ring), len(ref))
                self.assertEqual(ring.last(), ref[-1])
                lower = ts - rnd.uniform(0.0, 2e6)
                upper = ts + rnd.uniform(0.0, 2e6)
                prices = [p for t, p in ref if lower <= t <= upper]
                expected = (min(prices), max(prices)) if prices else None
                self.assertEqual(ring.window_minmax(lower, upper), expected)

    @requires_rust
    def test_unit_window_edge_cases(self):
        ring = polyhot.TickRing(4)
        with self.assertRaises(Exception):
            polyhot.TickRing(0)
        for ts, px in ((10.0, 5.0), (20.0, 3.0), (30.0, 9.0), (40.0, 1.0), (50.0, 2.0)):
            ring.append(ts, px)
        self.assertEqual(len(ring), 4)  # 10.0 evicted
        self.assertEqual(ring.last(), (50.0, 2.0))
        self.assertIsNone(ring.window_minmax(0.0, 9.0))
        self.assertIsNone(ring.window_minmax(60.0, 100.0))
        self.assertEqual(ring.window_minmax(20.0, 20.0), (3.0, 3.0))
        self.assertEqual(ring.window_minmax(15.0, 60.0), (1.0, 9.0))
        self.assertEqual(ring.window_minmax(45.0, 50.0), (2.0, 2.0))

    @requires_rust
    def test_tick_ring_vs_deque_random(self):
        rnd = random.Random(99)
        ring = polyhot.TickRing(4096)
        ref: collections.deque = collections.deque(maxlen=4096)
        for i in range(9000):  # crosses the 4096 overflow
            ts = 1_760_000_000_000.0 + rnd.uniform(-1000.0, 40_000.0)
            px = rnd.uniform(1.0, 100_000.0)
            ring.append(ts, px)
            ref.append((ts, px))
            if i % 97 == 0:
                lower = ts - rnd.uniform(0.0, 5000.0)
                upper = ts + rnd.uniform(0.0, 5000.0)
                prices = [p for t, p in ref if lower <= t <= upper]
                expected = (min(prices), max(prices)) if prices else None
                self.assertEqual(ring.window_minmax(lower, upper), expected)
        self.assertEqual(len(ring), 4096)
        self.assertEqual(ring.last(), ref[-1])


@requires_rust
class PriceGridParityTest(unittest.TestCase):
    def _compare(self, ticks, windows, nows, recv_nows):
        py_grid, rs_grid = _feed(ticks)
        for window_ms, now_ms, recv_now_ms in zip(windows, nows, recv_nows):
            for source in SOURCES + ("all",):
                self.assertEqual(
                    py_grid.window_extrema(window_ms, now_ms, source=source),
                    rs_grid.window_extrema(window_ms, now_ms, source=source),
                    (source, window_ms, now_ms),
                )
                self.assertEqual(
                    py_grid.jump_in_window(window_ms, now_ms, source=source),
                    rs_grid.jump_in_window(window_ms, now_ms, source=source),
                    (source, window_ms, now_ms),
                )
            self.assertEqual(
                py_grid.best_jump_in_window(window_ms, now_ms, recv_now_ms=recv_now_ms),
                rs_grid.best_jump_in_window(window_ms, now_ms, recv_now_ms=recv_now_ms),
                (window_ms, now_ms, recv_now_ms),
            )

    def test_random_streams_match(self):
        rnd = random.Random(7)
        for n in (0, 1, 5, 200, 4200, 9000):
            ticks = _ticks(n, rnd)
            base = 1_760_000_000_000.0
            windows = [rnd.choice((1.0, 500.0, 5000.0, 200_000.0)) for _ in range(6)]
            nows = [None] + [base + rnd.uniform(0.0, 120_000.0) for _ in range(5)]
            recv_nows = [None, base + rnd.uniform(0.0, 120_000.0), base + 1e9]
            self._compare(ticks, windows, nows, recv_nows)

    def test_env_flag_and_fallback(self):
        old = os.environ.get("POLY_RUST_HOTPATH")
        try:
            os.environ["POLY_RUST_HOTPATH"] = "1"
            self.assertTrue(PriceGrid().rust_enabled)
            os.environ["POLY_RUST_HOTPATH"] = "0"
            self.assertFalse(PriceGrid().rust_enabled)
        finally:
            if old is None:
                os.environ.pop("POLY_RUST_HOTPATH", None)
            else:
                os.environ["POLY_RUST_HOTPATH"] = old
        self.assertFalse(PriceGrid(use_rust=False).rust_enabled)
        self.assertTrue(PriceGrid(use_rust=True).rust_enabled)

    @hyp_settings(max_examples=40, deadline=None)
    @given(
        prices=st.lists(POSITIVE_FLOAT, min_size=0, max_size=60),
        stamps=st.lists(st.floats(min_value=0.0, max_value=1000.0), min_size=60, max_size=60),
        window=st.floats(min_value=0.0, max_value=1200.0),
        ref=st.floats(min_value=-500.0, max_value=1500.0),
    )
    def test_hypothesis_single_source(self, prices, stamps, window, ref):
        py_grid, rs_grid = PriceGrid(use_rust=False), PriceGrid(use_rust=True)
        base = 1_760_000_000_000.0
        for px, stamp in zip(prices, stamps):
            ex_ms = base + stamp
            py_grid.add(ex_ms, px, source="primary", recv_ms=ex_ms)
            rs_grid.add(ex_ms, px, source="primary", recv_ms=ex_ms)
        now_ms = base + ref
        self.assertEqual(py_grid.window_extrema(window, now_ms),
                         rs_grid.window_extrema(window, now_ms))
        self.assertEqual(py_grid.jump_in_window(window, now_ms),
                         rs_grid.jump_in_window(window, now_ms))
        self.assertEqual(py_grid.best_jump_in_window(window, now_ms),
                         rs_grid.best_jump_in_window(window, now_ms))
        self.assertEqual(py_grid.best_jump_in_window(window, now_ms, recv_now_ms=now_ms + 1.0),
                         rs_grid.best_jump_in_window(window, now_ms, recv_now_ms=now_ms + 1.0))

    @hyp_settings(max_examples=30, deadline=None)
    @given(
        ticks=st.lists(st.tuples(st.sampled_from(SOURCES), FINITE_FLOAT, FINITE_FLOAT, POSITIVE_FLOAT),
                       min_size=0, max_size=80),
        window=st.floats(min_value=0.0, max_value=1e14),
        ref=st.floats(min_value=-1e15, max_value=1e15),
    )
    def test_hypothesis_multi_source(self, ticks, window, ref):
        py_grid, rs_grid = PriceGrid(use_rust=False), PriceGrid(use_rust=True)
        for source, ex_ms, recv_ms, px in ticks:
            py_grid.add(ex_ms, px, source=source, recv_ms=recv_ms)
            rs_grid.add(ex_ms, px, source=source, recv_ms=recv_ms)
        for source in SOURCES + ("all",):
            self.assertEqual(py_grid.window_extrema(window, ref, source=source),
                             rs_grid.window_extrema(window, ref, source=source))
            self.assertEqual(py_grid.jump_in_window(window, ref, source=source),
                             rs_grid.jump_in_window(window, ref, source=source))
        self.assertEqual(py_grid.best_jump_in_window(window, ref),
                         rs_grid.best_jump_in_window(window, ref))
        self.assertEqual(py_grid.best_jump_in_window(window, ref, recv_now_ms=ref),
                         rs_grid.best_jump_in_window(window, ref, recv_now_ms=ref))


if __name__ == "__main__":
    unittest.main()
