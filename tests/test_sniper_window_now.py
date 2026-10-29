"""Recv-window anchoring for the poll path of the 5m sniper.

``best_jump_in_window`` stamps ticks with ``time.monotonic()`` ms, so a poll
evaluation must anchor ``recv_now_ms`` to the monotonic clock. Anchoring it to
wall-clock ms used to make ``ref_r`` fall back to each source's own last tick,
which kept an old jump "in window" forever.
"""
from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest import mock

from buy.binance_trades import PriceGrid
from buy.fivemin import FiveMinIO, Strategy

import test_fivemin as helpers


BYBIT = "wss://stream.bybit.com/v5/public/linear"


class BestJumpRecvNowTests(unittest.TestCase):
    def test_jump_whose_trigger_tick_is_600ms_old_is_not_in_window(self):
        grid = PriceGrid()
        grid.add(1_000_000.0, 70000.0, recv_ms=1_000_000.0)
        grid.add(1_000_400.0, 70040.0, recv_ms=1_000_400.0)

        self.assertIsNone(grid.best_jump_in_window(500.0, recv_now_ms=1_001_000.0))

        fresh = grid.best_jump_in_window(500.0, recv_now_ms=1_000_450.0)
        self.assertIsNotNone(fresh)
        self.assertEqual(fresh["source"], "primary")
        self.assertAlmostEqual(fresh["up_jump"], 40.0)

    def test_stale_source_is_skipped_while_fresh_source_still_counts(self):
        grid = PriceGrid()
        grid.add(1_000_000.0, 70000.0, recv_ms=1_000_000.0)
        grid.add(1_000_400.0, 70040.0, recv_ms=1_000_400.0)
        # A $500 jump on a source whose newest tick is 800ms old: outside the window.
        grid.add(999_250.0, 70000.0, source=BYBIT, recv_ms=999_250.0)
        grid.add(999_650.0, 70500.0, source=BYBIT, recv_ms=999_650.0)

        info = grid.best_jump_in_window(500.0, recv_now_ms=1_000_450.0)
        self.assertIsNotNone(info)
        self.assertEqual(info["source"], "primary")
        self.assertAlmostEqual(info["up_jump"], 40.0)

    def test_fresh_jump_returns_with_and_without_the_recv_anchor(self):
        grid = PriceGrid()
        grid.add(1_000_100.0, 70000.0, recv_ms=1_000_100.0)
        grid.add(1_000_500.0, 70030.0, recv_ms=1_000_500.0)

        anchored = grid.best_jump_in_window(500.0, recv_now_ms=1_000_510.0)
        self.assertIsNotNone(anchored)
        self.assertAlmostEqual(anchored["up_jump"], 30.0)

        # Tick path: no anchor, ref_r is the newest tick of each source.
        tick_path = grid.best_jump_in_window(500.0)
        self.assertEqual(tick_path, anchored)


class SniperPollRecvNowTests(unittest.TestCase):
    def _case(self):
        case = SimpleNamespace(logs=[])
        case.io = FiveMinIO(
            buy=lambda *a, **kw: {},
            sell=lambda *a: {}, reserves=lambda cfg: 0, redeem=lambda *a: None,
            save=lambda state: None,
            log=lambda event, **kw: case.logs.append((event, kw)),
            alert=lambda *a, **kw: None, clock=lambda: 1200.0,
        )
        case.strategy = Strategy(case.io, startup_ts=0)
        case.strategy.add_market(helpers.market(), now=1000)
        row = case.strategy.state["markets"]["cid"]
        row["strike"] = 70000.0
        row["offset"] = 0.0
        case.strategy.sample_cash(1200, 1000)
        return case

    def _check(self, case, grid):
        quotes = {"up": {"bid": .90, "bids": [], "asks": [[.94, 25.0]], "age_ms": 12.0, "src": "ws"},
                  "down": {"bid": .06, "bids": [], "asks": [], "age_ms": 12.0, "src": "ws"}}
        cfg = helpers.cfg(fivemin_dry_run=False, sniper_5m_enabled=True, sniper_use_offset=False)
        return case.strategy._sniper_check(
            cfg, 1200.0, case.strategy.state["markets"]["cid"], quotes, grid, 100.0,
            defer_buy=True, fire_path="poll",
        )

    @staticmethod
    def _grid(*ticks):
        grid = PriceGrid()
        # Exchange stamps sit far from the poll clock so the ``jump_in_window``
        # fallback (no recv anchor) cannot resurrect the stale jump.
        for recv_ms, px in ticks:
            grid.add(5_000_000.0, px, recv_ms=recv_ms, recv_wall_ms=recv_ms)
        return grid

    def test_poll_ignores_a_jump_that_triggered_600ms_ago(self):
        grid = self._grid((1_199_700.0, 70000.0), (1_199_900.0, 70040.0))
        with mock.patch("buy.fivemin.time.monotonic", return_value=1_200.5):
            self.assertIsNone(self._check(self._case(), grid))

    def test_poll_fires_on_a_fresh_jump(self):
        grid = self._grid((1_200_100.0, 70000.0), (1_200_400.0, 70040.0))
        with mock.patch("buy.fivemin.time.monotonic", return_value=1_200.5):
            prepared = self._check(self._case(), grid)
        self.assertIsNotNone(prepared)
        self.assertEqual(prepared["fav"], "up")
        self.assertAlmostEqual(prepared["jump_usd"], 40.0)


if __name__ == "__main__":
    unittest.main()
