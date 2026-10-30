"""``sniper_trigger_sources``: restrict the 5m sniper to jumps from named feeds."""
from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest import mock

from buy.binance_trades import PriceGrid
from buy.fivemin import FiveMinIO, Strategy

import test_fivemin as helpers


BYBIT = "wss://stream.bybit.com/v5/public/linear"
COINBASE = "wss://ws-feed.exchange.coinbase.com"


class GridSourceFilterTests(unittest.TestCase):
    def _grid(self):
        grid = PriceGrid()
        grid.add(1_000_000.0, 70000.0, recv_ms=1_000_000.0)
        grid.add(1_000_400.0, 70040.0, recv_ms=1_000_400.0)
        grid.add(1_000_000.0, 70000.0, source="bybit", recv_ms=1_000_000.0)
        grid.add(1_000_400.0, 70500.0, source="bybit", recv_ms=1_000_400.0)
        grid.add(1_000_000.0, 70000.0, source="coinbase", recv_ms=1_000_000.0)
        grid.add(1_000_400.0, 70100.0, source="coinbase", recv_ms=1_000_400.0)
        return grid

    def test_bybit_jump_is_ignored_when_only_coinbase_is_allowed(self):
        grid = self._grid()
        info = grid.best_jump_in_window(500.0, recv_now_ms=1_000_450.0,
                                        sources=frozenset({"coinbase"}))
        self.assertIsNotNone(info)
        self.assertEqual(info["source"], "coinbase")
        self.assertAlmostEqual(info["up_jump"], 100.0)

    def test_bybit_jump_is_ignored_even_if_it_is_the_largest(self):
        grid = self._grid()
        self.assertEqual(grid.best_jump_in_window(500.0, recv_now_ms=1_000_450.0)["source"], "bybit")
        info = grid.best_jump_in_window(500.0, recv_now_ms=1_000_450.0,
                                        sources=frozenset({"primary"}))
        self.assertEqual(info["source"], "primary")
        self.assertAlmostEqual(info["up_jump"], 40.0)

    def test_sources_none_keeps_current_behaviour(self):
        grid = self._grid()
        self.assertEqual(grid.best_jump_in_window(500.0, recv_now_ms=1_000_450.0),
                         grid.best_jump_in_window(500.0, recv_now_ms=1_000_450.0, sources=None))

    def test_no_allowed_source_yields_none(self):
        grid = self._grid()
        self.assertIsNone(grid.best_jump_in_window(500.0, recv_now_ms=1_000_450.0,
                                                   sources=frozenset({"kraken"})))


class StrategySourceFilterTests(unittest.TestCase):
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

    def _check(self, case, grid, **cfg_extra):
        quotes = {"up": {"bid": .90, "bids": [], "asks": [[.94, 25.0]], "age_ms": 12.0, "src": "ws"},
                  "down": {"bid": .06, "bids": [], "asks": [], "age_ms": 12.0, "src": "ws"}}
        cfg = helpers.cfg(fivemin_dry_run=False, sniper_5m_enabled=True, sniper_use_offset=False,
                          **cfg_extra)
        return case.strategy._sniper_check(
            cfg, 1200.0, case.strategy.state["markets"]["cid"], quotes, grid, 100.0,
            defer_buy=True, fire_path="poll",
        )

    @staticmethod
    def _grid(source, jump=40.0):
        grid = PriceGrid()
        # Exchange stamps sit far from the poll clock so the ``jump_in_window``
        # fallback (no recv anchor) cannot resurrect the stale jump.
        grid.add(5_000_000.0, 70000.0, recv_ms=1_200_100.0, recv_wall_ms=1_200_100.0, source=source)
        grid.add(5_000_000.0, 70000.0 + jump, recv_ms=1_200_400.0, recv_wall_ms=1_200_400.0,
                 source=source)
        return grid

    def test_bybit_jump_does_not_fire_when_only_coinbase_is_allowed(self):
        case = self._case()
        with mock.patch("buy.fivemin.time.monotonic", return_value=1_200.5):
            self.assertIsNone(self._check(case, self._grid("bybit"),
                                          sniper_trigger_sources=["coinbase"]))

    def test_coinbase_jump_fires_when_only_coinbase_is_allowed(self):
        case = self._case()
        with mock.patch("buy.fivemin.time.monotonic", return_value=1_200.5):
            prepared = self._check(case, self._grid("coinbase"),
                                   sniper_trigger_sources=["coinbase"])
        self.assertIsNotNone(prepared)
        self.assertAlmostEqual(prepared["jump_usd"], 40.0)

    def test_bybit_jump_fires_by_default(self):
        case = self._case()
        with mock.patch("buy.fivemin.time.monotonic", return_value=1_200.5):
            prepared = self._check(case, self._grid("bybit"))
        self.assertIsNotNone(prepared)
        self.assertAlmostEqual(prepared["jump_usd"], 40.0)

    def test_url_keyed_grid_is_allowed_by_short_name(self):
        case = self._case()
        allowed = case.strategy._sniper_allowed_sources(
            helpers.cfg(sniper_trigger_sources=["coinbase"]))
        self.assertIn(COINBASE, allowed)
        with mock.patch("buy.fivemin.time.monotonic", return_value=1_200.5):
            prepared = self._check(case, self._grid(COINBASE),
                                   sniper_trigger_sources=["coinbase"])
        self.assertIsNotNone(prepared)


if __name__ == "__main__":
    unittest.main()
