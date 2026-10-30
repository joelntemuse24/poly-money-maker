"""E3 sniper gap estimator: per-feed Chainlink offsets and the min-gap gate."""
from __future__ import annotations

import unittest
from types import SimpleNamespace

from buy.binance_trades import PriceGrid
from buy.fivemin import DEFAULTS, FiveMinIO, SniperGapEstimator, Strategy

import test_fivemin as helpers


def cfg(**extra):
    value = helpers.cfg(sniper_5m_enabled=True, sniper_use_offset=True)
    value.update(extra)
    return value


class FakeGrid:
    """The handful of grid reads E3 and the sniper scan use."""

    def __init__(self, *, spot=70000.0, low=69900.0, high=70000.0, exchange_ms=1_200_000.0):
        self.spot, self.exchange_ms = spot, exchange_ms
        self.info = {"spot": spot, "min": low, "max": high, "up_jump": spot - low,
                     "down_jump": high - spot, "ts_ms": exchange_ms, "source": "primary"}
        self.feeds: dict[str, tuple[float, float, float]] = {}
        self.lookups: dict[str, float] = {}
        self.lookup_calls: list[tuple[str, float]] = []

    def best_jump_in_window(self, window_ms=500.0, now_ms=None, *, sources=None, recv_now_ms=None):
        return self.info

    def latest_exchange_ms(self):
        return self.exchange_ms

    def latest_source_tick(self, source="primary"):
        return self.feeds.get(source)

    def source_price_at(self, source, target_ms, *, window_ms=60000.0):
        self.lookup_calls.append((source, target_ms))
        return self.lookups.get(source)


def live(price, obs_ts, recv_ts=None):
    return SimpleNamespace(price=str(price), obs_ts=obs_ts, recv_ts=recv_ts)


class EstimatorMathTests(unittest.TestCase):
    def test_first_sample_initialises_and_later_samples_use_time_based_ewma(self):
        grid = FakeGrid()
        grid.lookups = {"coinbase": 100.0}
        est = SniperGapEstimator()
        config = cfg(sniper_e3_feeds=["coinbase"])

        assert est.update(config, live(105.0, 1_000.0, recv_ts=1_001.0), grid, 1_001.0)
        self.assertAlmostEqual(est.offsets()["coinbase"]["offset"], 5.0)
        # The lag shifts the lookup back by sniper_e3_lag_s (0.5s default).
        self.assertEqual(grid.lookup_calls[-1], ("coinbase", 999_500.0))

        # 15s of obs_ts time with a 15s half-life halves the old weight.
        assert est.update(config, live(115.0, 1_015.0, recv_ts=1_016.0), grid, 1_016.0)
        self.assertAlmostEqual(est.offsets()["coinbase"]["offset"], 10.0)
        self.assertEqual(est.offsets()["coinbase"]["obs_ts"], 1_015.0)

    def test_same_obs_ts_does_not_advance_the_ewma(self):
        grid = FakeGrid()
        grid.lookups = {"coinbase": 100.0}
        est = SniperGapEstimator()
        config = cfg(sniper_e3_feeds=["coinbase"])
        self.assertTrue(est.update(config, live(105.0, 1_000.0), grid, 1_000.0))
        self.assertFalse(est.update(config, live(200.0, 1_000.0), grid, 1_000.5))
        self.assertAlmostEqual(est.offsets()["coinbase"]["offset"], 5.0)

    def test_feed_without_a_price_at_the_lag_is_left_alone(self):
        grid = FakeGrid()
        grid.lookups = {"coinbase": 100.0}
        est = SniperGapEstimator()
        config = cfg(sniper_e3_feeds=["coinbase", "bybit"])
        est.update(config, live(105.0, 1_000.0), grid, 1_000.0)
        self.assertNotIn("bybit", est.offsets())


class EstimatorFreshnessTests(unittest.TestCase):
    def _est(self, **kw):
        grid = FakeGrid(**kw)
        grid.lookups = {"coinbase": 100.0}
        est = SniperGapEstimator()
        config = cfg(sniper_e3_feeds=["coinbase"])
        est.update(config, live(105.0, 1_000.0, recv_ts=1_000.0), grid, 1_000.0)
        return est, grid, config

    def test_estimate_uses_latest_price_plus_offset(self):
        est, grid, config = self._est()
        grid.feeds["coinbase"] = (1_010_000.0, 1_010_000.0, 101.0)
        value, feeds = est.estimate(config, grid, 1_010.0)
        self.assertAlmostEqual(value, 106.0)
        self.assertEqual(feeds, ("coinbase",))

    def test_stale_tick_drops_the_feed_and_falls_back(self):
        est, grid, config = self._est()
        grid.feeds["coinbase"] = (1_000_000.0, 1_005_000.0, 101.0)
        value, feeds = est.estimate(config, grid, 1_010.0)  # 5s old > 2s
        self.assertIsNone(value)
        self.assertEqual(feeds, ())
        # A fresh tick brings it back.
        grid.feeds["coinbase"] = (1_009_500.0, 1_009_500.0, 101.0)
        self.assertAlmostEqual(est.estimate(config, grid, 1_010.0)[0], 106.0)

    def test_stale_offset_drops_the_feed(self):
        est, grid, config = self._est()
        grid.feeds["coinbase"] = (1_040_000.0, 1_040_000.0, 101.0)
        value, _ = est.estimate(config, grid, 1_040.0)  # offset is 40s old > 30s
        self.assertIsNone(value)


class RealGridLookupTests(unittest.TestCase):
    def test_price_at_or_before_target_on_the_exchange_axis(self):
        grid = PriceGrid()
        grid.add(1_000_000.0, 70000.0, source="coinbase", recv_ms=1_000_000.0)
        grid.add(1_000_800.0, 70050.0, source="coinbase", recv_ms=1_000_800.0)
        self.assertEqual(grid.source_price_at("coinbase", 1_000_400.0), 70000.0)
        self.assertEqual(grid.source_price_at("coinbase", 1_000_900.0), 70050.0)
        self.assertIsNone(grid.source_price_at("coinbase", 1_000_400.0, window_ms=100.0))
        self.assertIsNone(grid.source_price_at("kraken", 1_000_900.0))

    def test_latest_source_tick_reports_recv_wall_stamp(self):
        grid = PriceGrid()
        grid.add(1_000_000.0, 70000.0, source="coinbase", recv_ms=1_000_000.0,
                 recv_wall_ms=1_000_050.0)
        self.assertEqual(grid.latest_source_tick("coinbase"), (1_000_000.0, 1_000_050.0, 70000.0))
        self.assertIsNone(grid.latest_source_tick("kraken"))


class StrategyGapTests(unittest.TestCase):
    def _case(self):
        case = SimpleNamespace(logs=[], buys=[])
        case.io = FiveMinIO(
            buy=lambda token, shares, limit, dry: case.buys.append((token, shares, limit)) or {
                "shares": shares, "cost": shares * limit, "price": limit, "status": "matched"},
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

    def _quotes(self):
        return {"up": {"bid": .90, "bids": [], "asks": [[.94, 25.0]], "age_ms": 12.0, "src": "ws"},
                "down": {"bid": .06, "bids": [], "asks": [], "age_ms": 12.0, "src": "ws"}}

    def _grid(self):
        # +$30 jump, spot $60 above the strike: legacy gap is $60, E3 gap $10.
        grid = FakeGrid(spot=70060.0, low=70030.0, high=70060.0)
        grid.lookups = {"coinbase": 70010.0}
        grid.feeds["coinbase"] = (1_200_000.0, 1_200_000.0, 70010.0)
        return grid

    def _seed(self, case, cfg_obj, grid):
        case.strategy.sniper_e3.update(cfg_obj, live(70005.0, 1_199.0, recv_ts=1_199.0), grid, 1_199.0)

    def _check(self, case, grid, config):
        return case.strategy._sniper_check(
            config, 1200.0, case.strategy.state["markets"]["cid"], self._quotes(),
            grid, 100.0, defer_buy=True,
        )

    def _rejects(self, case):
        return [kw for event, kw in case.logs if event == "sniper_gate_reject"]

    def test_legacy_flag_keeps_the_old_gap_and_ignores_e3(self):
        case, grid = self._case(), self._grid()
        config = cfg(sniper_gap_estimator="legacy", sniper_e3_feeds=["coinbase"],
                     sniper_min_gap_usd=20.0)
        self._seed(case, config, grid)
        prepared = self._check(case, grid, config)
        # Legacy gap is $60 >= $20, so the trade goes ahead even though E3 is $5.
        self.assertIsNotNone(prepared)
        self.assertEqual(prepared["gap_usd"], 60.0)
        self.assertEqual(prepared["gap_meta"]["gap_estimator"], "legacy")
        case.strategy._sniper_execute(prepared)
        fill = [kw for event, kw in case.logs if event == "fivemin_sniper_fill"][0]
        self.assertEqual(fill["gap_usd"], 60.0)
        self.assertEqual(fill["gap_legacy_usd"], 60.0)
        self.assertEqual(fill["gap_estimator"], "legacy")

    def test_e3_gap_drives_the_min_gap_gate_and_the_fill_log(self):
        case, grid = self._case(), self._grid()
        config = cfg(sniper_gap_estimator="e3", sniper_e3_feeds=["coinbase"],
                     sniper_min_gap_usd=20.0)
        self._seed(case, config, grid)
        # offset = 70005 - 70010 = -5, estimate 70010 - 5 = 70005; gap = +5 < 20.
        self.assertIsNone(self._check(case, grid, config))
        reject = self._rejects(case)[0]
        self.assertEqual(reject["gate"], "min_gap")
        self.assertEqual(reject["gap_usd"], 5.0)
        self.assertEqual(reject["detail"]["gap_usd"], 5.0)
        self.assertEqual(reject["detail"]["gap_legacy_usd"], 60.0)
        self.assertEqual(reject["detail"]["gap_e3_usd"], 5.0)
        self.assertEqual(reject["gap_legacy_usd"], 60.0)
        self.assertEqual(reject["gap_e3_usd"], 5.0)
        self.assertEqual(reject["gap_estimator"], "e3")
        self.assertEqual(reject["e3_feeds"], ["coinbase"])
        self.assertEqual(reject["est_e3"], 70005.0)

        # A lower gate lets the same tick through, and the fill line carries E3.
        config["sniper_min_gap_usd"] = 1.0
        prepared = self._check(case, grid, config)
        self.assertIsNotNone(prepared)
        self.assertEqual(prepared["gap_usd"], 5.0)
        self.assertEqual(prepared["gap_meta"]["gap_legacy_usd"], 60.0)
        case.strategy._sniper_execute(prepared)
        fill = [kw for event, kw in case.logs if event == "fivemin_sniper_fill"][0]
        self.assertEqual(fill["gap_usd"], 5.0)
        self.assertEqual(fill["gap_estimator"], "e3")
        self.assertEqual(fill["est_e3"], 70005.0)
        self.assertEqual(fill["e3_feed_count"], 1)
        latency = [kw for event, kw in case.logs if event == "sniper_order_latency"][0]
        self.assertEqual(latency["gap_estimator"], "e3")
        self.assertEqual(latency["gap_legacy_usd"], 60.0)
        self.assertEqual(latency["gap_e3_usd"], 5.0)

    def test_fallback_to_legacy_gap_when_no_feed_is_fresh(self):
        case, grid = self._case(), self._grid()
        config = cfg(sniper_gap_estimator="e3", sniper_e3_feeds=["coinbase"],
                     sniper_min_gap_usd=20.0)
        self._seed(case, config, grid)
        grid.feeds["coinbase"] = (1_000_000.0, 1_000_000.0, 70010.0)  # 200s old
        prepared = self._check(case, grid, config)
        self.assertIsNotNone(prepared)
        self.assertEqual(prepared["gap_usd"], 60.0)
        self.assertEqual(prepared["gap_meta"]["gap_estimator"], "fallback")
        self.assertIsNone(prepared["gap_meta"]["est_e3"])
        self.assertEqual(prepared["gap_meta"]["e3_feed_count"], 0)

    def test_e3_available_lets_a_missing_row_offset_through(self):
        case, grid = self._case(), self._grid()
        case.strategy.state["markets"]["cid"]["offset"] = None
        config = cfg(sniper_gap_estimator="e3", sniper_e3_feeds=["coinbase"],
                     sniper_min_gap_usd=1.0)
        self._seed(case, config, grid)
        prepared = self._check(case, grid, config)
        self.assertIsNotNone(prepared)
        self.assertEqual(prepared["gap_usd"], 5.0)
        self.assertEqual(prepared["gap_meta"]["gap_estimator"], "e3")
        self.assertIsNone(prepared["gap_meta"]["gap_legacy_usd"])

    def test_missing_offset_still_blocks_the_legacy_estimator(self):
        case, grid = self._case(), self._grid()
        case.strategy.state["markets"]["cid"]["offset"] = None
        config = cfg(sniper_gap_estimator="legacy", sniper_min_gap_usd=1.0)
        self.assertIsNone(self._check(case, grid, config))
        self.assertEqual(self._rejects(case)[0]["gate"], "offset")

    def test_offsets_do_not_update_inside_sniper_scan(self):
        case, grid = self._case(), self._grid()
        config = cfg(sniper_gap_estimator="e3", sniper_e3_feeds=["coinbase"],
                     sniper_min_gap_usd=20.0)
        self._seed(case, config, grid)
        before = case.strategy.sniper_e3.offsets()
        calls = len(grid.lookup_calls)
        self._check(case, grid, config)
        self.assertEqual(case.strategy.sniper_e3.offsets(), before)
        self.assertEqual(len(grid.lookup_calls), calls)  # no lag lookup either


class RunnerUpdateTests(unittest.TestCase):
    def test_runner_tick_folds_new_chainlink_samples_off_the_scan_path(self):
        from buy.fivemin import Runner

        updates = []

        class Recorder(SniperGapEstimator):
            def update(self, cfg_obj, live, grid_obj, now):
                updates.append((live, grid_obj, now))
                return super().update(cfg_obj, live, grid_obj, now)

        grid = FakeGrid()
        grid.lookups = {"coinbase": 70010.0}
        case = StrategyGapTests()
        case_ = SimpleNamespace(logs=[])
        io = FiveMinIO(buy=lambda *a, **k: {}, sell=lambda *a: {}, reserves=lambda cfg: 0,
                       redeem=lambda *a: None, save=lambda state: None,
                       log=lambda event, **kw: case_.logs.append((event, kw)),
                       alert=lambda *a, **kw: None, clock=lambda: 1200.0)
        strategy = Strategy(io, startup_ts=0)
        strategy.sniper_e3 = Recorder()
        books = SimpleNamespace(set_tokens=lambda tokens: None, set_stale_s=lambda _: None,
                                set_min_size=lambda _: None, usable=lambda token: None)
        rtds = SimpleNamespace(start=lambda: None, stop=lambda: None, drain=lambda: [],
                               reconnect=lambda reason: None,
                               _live=live(70005.0, 1_199.0, recv_ts=1_199.0))
        runner = Runner(strategy, lambda: cfg(sniper_e3_feeds=["coinbase"]),
                        SimpleNamespace(discover=lambda slugs: []),
                        SimpleNamespace(pUSD_balance=lambda *a: 100), "funder",
                        grid, SimpleNamespace(start=lambda: None, stop=lambda: None),
                        rtds, books, clock=lambda: 1200.0)
        runner.tick()
        self.assertEqual(len(updates), 1)
        live_arg, grid_arg, now_arg = updates[-1]
        self.assertEqual(grid_arg, grid)
        self.assertEqual(now_arg, 1200.0)
        self.assertAlmostEqual(strategy.sniper_e3.offsets()["coinbase"]["offset"], -5.0)
        # A second tick with the same obs_ts is a no-op update.
        before = strategy.sniper_e3.offsets()
        runner.tick()
        self.assertEqual(strategy.sniper_e3.offsets(), before)


class DefaultsTests(unittest.TestCase):
    def test_new_keys_are_registered(self):
        for source in (DEFAULTS,):
            self.assertEqual(source["sniper_gap_estimator"], "e3")
            self.assertEqual(source["sniper_e3_feeds"], ["binance_spot", "coinbase", "bybit"])
            self.assertEqual(source["sniper_e3_halflife_s"], 15.0)
            self.assertEqual(source["sniper_e3_lag_s"], 0.5)
            self.assertEqual(source["sniper_e3_max_age_s"], 2.0)
            self.assertEqual(source["sniper_e3_offset_max_age_s"], 30.0)


if __name__ == "__main__":
    unittest.main()
