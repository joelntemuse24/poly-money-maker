"""Pre-order Chainlink side guard for the 5m sniper."""
from __future__ import annotations

import time
import unittest
from types import SimpleNamespace

from buy.fivemin import DEFAULTS, FiveMinIO, Strategy

import test_fivemin as helpers



def cfg(**extra):
    value = helpers.cfg(sniper_5m_enabled=True, sniper_use_offset=True)
    value.update(extra)
    return value


class FakeGrid:
    """The grid reads the sniper scan and the CL guard perform."""

    def __init__(self, *, spot=70060.0, low=70030.0, high=70060.0, exchange_ms=None):
        self.spot = spot
        self.exchange_ms = time.time() * 1000.0 if exchange_ms is None else exchange_ms
        self.info = {"spot": spot, "min": low, "max": high, "up_jump": spot - low,
                     "down_jump": high - spot, "ts_ms": self.exchange_ms, "source": "primary"}
        self.feeds: dict[str, tuple[float, float, float]] = {}
        self.lookups: dict[str, float] = {}

    def best_jump_in_window(self, window_ms=500.0, now_ms=None, *, sources=None, recv_now_ms=None):
        return self.info

    def latest_exchange_ms(self):
        return self.exchange_ms

    def latest_source_tick(self, source="primary"):
        return self.feeds.get(source)

    def source_price_at(self, source, target_ms, *, window_ms=60000.0):
        return self.lookups.get(source)


def live(price, obs_ts, recv_ts=None):
    return SimpleNamespace(price=str(price) if price is not None else None,
                           obs_ts=obs_ts, recv_ts=recv_ts)


class Case:
    def __init__(self):
        self.logs, self.buys = [], []
        self.io = FiveMinIO(
            buy=lambda token, shares, limit, dry: self.buys.append((token, shares, limit)) or {
                "shares": shares, "cost": shares * limit, "price": limit, "status": "matched"},
            sell=lambda *a: {}, reserves=lambda cfg: 0, redeem=lambda *a: None,
            save=lambda state: None,
            log=lambda event, **kw: self.logs.append((event, kw)),
            alert=lambda *a, **kw: None, clock=lambda: time.time(),
        )
        self.strategy = Strategy(self.io, startup_ts=0)
        # End the window ~100s out so ``_sniper_prepare`` does not see "expired".
        start = int(time.time()) - 200
        self.strategy.add_market(helpers.market(start=start), now=start - 1)
        self.row = self.strategy.state["markets"]["cid"]
        self.row["strike"] = 70000.0
        self.row["offset"] = 0.0
        self.strategy.sample_cash(time.time(), 1000)

    def set_live(self, price, obs_ts):
        self.strategy.rtds_feed = SimpleNamespace(_live=live(price, obs_ts))

    def quotes(self):
        return {"up": {"bid": .90, "bids": [], "asks": [[.94, 25.0]], "age_ms": 12.0, "src": "ws"},
                "down": {"bid": .06, "bids": [], "asks": [[.94, 25.0]], "age_ms": 12.0, "src": "ws"}}

    def check(self, config, grid=None):
        grid = grid or FakeGrid()
        return self.strategy._sniper_check(config, time.time(), self.row, self.quotes(),
                                           grid, 100.0, defer_buy=True)

    def rejects(self):
        return [kw for event, kw in self.logs if event == "sniper_gate_reject"]

    def events(self, name):
        return [kw for event, kw in self.logs if event == name]


class OffTests(unittest.TestCase):
    def test_guard_off_leaves_behaviour_and_gap_meta_unchanged(self):
        on_side = Case()
        on_side.set_live(69900.0, time.time())  # wrong side for both, but guard off
        prepared = on_side.check(cfg(sniper_cl_guard=False))
        self.assertIsNotNone(prepared)
        self.assertFalse(on_side.rejects())
        self.assertEqual(prepared["gap_meta"]["cl_guard_decision"], "off")
        self.assertIsNone(prepared["gap_meta"]["cl_gap_usd"])
        self.assertIsNone(prepared["gap_meta"]["cl_age_s"])
        self.assertIsNone(prepared["gap_meta"]["cl_guard_mode"])

        # Identical run without any RTDS feed at all also prepares an order.
        plain = Case()
        plain.strategy.rtds_feed = None
        self.assertIsNotNone(plain.check(cfg(sniper_cl_guard=False)))

    def test_defaults_registered(self):
        self.assertFalse(DEFAULTS["sniper_cl_guard"])
        self.assertEqual(DEFAULTS["sniper_cl_guard_mode"], "raw")
        self.assertEqual(DEFAULTS["sniper_cl_max_age_s"], 5.0)
        self.assertEqual(DEFAULTS["sniper_cl_min_gap_usd"], 0.0)


class RawModeTests(unittest.TestCase):
    def test_up_side_rejected_when_chainlink_is_below_strike(self):
        case = Case()
        case.set_live(69980.0, time.time())
        self.assertIsNone(case.check(cfg(sniper_cl_guard=True)))
        reject = case.rejects()[0]
        self.assertEqual(reject["gate"], "cl_wrong_side")
        self.assertEqual(reject["side"], "up")
        self.assertEqual(reject["cl_gap_usd"], -20.0)
        self.assertEqual(reject["cl_guard_mode"], "raw")
        self.assertEqual(reject["cl_guard_decision"], "reject")
        self.assertIn("cl_age_s", reject)
        self.assertEqual(reject["detail"]["cl_gap_usd"], -20.0)
        self.assertEqual(reject["detail"]["cl_guard_decision"], "reject")

    def test_down_side_rejected_when_chainlink_is_above_strike(self):
        case = Case()
        case.row["strike"] = 70090.0
        case.set_live(70100.0, time.time())
        # UP's jump is under the threshold, so DOWN is the side under test.
        grid = FakeGrid(spot=70060.0, low=70040.0, high=70090.0)
        self.assertIsNone(case.check(cfg(sniper_cl_guard=True), grid=grid))
        reject = case.rejects()[0]
        self.assertEqual(reject["gate"], "cl_wrong_side")
        self.assertEqual(reject["side"], "down")
        self.assertEqual(reject["cl_gap_usd"], -10.0)
        self.assertEqual(reject["cl_guard_decision"], "reject")

    def test_pass_when_chainlink_is_on_our_side(self):
        case = Case()
        case.set_live(70020.0, time.time())
        prepared = case.check(cfg(sniper_cl_guard=True))
        self.assertIsNotNone(prepared)
        self.assertEqual(prepared["fav"], "up")
        self.assertEqual(prepared["gap_meta"]["cl_guard_decision"], "pass")
        self.assertEqual(prepared["gap_meta"]["cl_gap_usd"], 20.0)
        self.assertEqual(prepared["gap_meta"]["cl_guard_mode"], "raw")
        case.strategy._sniper_execute(prepared)
        fill = case.events("fivemin_sniper_fill")[0]
        self.assertEqual(fill["cl_guard_decision"], "pass")
        self.assertEqual(fill["cl_gap_usd"], 20.0)

    def test_min_gap_floor_accepts_a_small_positive_gap(self):
        case = Case()
        case.set_live(70003.0, time.time())
        self.assertIsNotNone(case.check(cfg(sniper_cl_guard=True, sniper_cl_min_gap_usd=2.0)))
        strict = Case()
        strict.set_live(70001.0, time.time())
        self.assertIsNone(strict.check(cfg(sniper_cl_guard=True, sniper_cl_min_gap_usd=2.0)))
        self.assertEqual(strict.rejects()[0]["gate"], "cl_wrong_side")


class FailOpenTests(unittest.TestCase):
    def test_stale_print_fails_open(self):
        case = Case()
        case.set_live(69900.0, time.time() - 30.0)
        prepared = case.check(cfg(sniper_cl_guard=True))
        self.assertIsNotNone(prepared)
        self.assertEqual(prepared["gap_meta"]["cl_guard_decision"], "fail_open_stale")
        self.assertIsNone(prepared["gap_meta"]["cl_gap_usd"])

    def test_age_limit_is_configurable(self):
        case = Case()
        case.set_live(70020.0, time.time() - 3.0)
        self.assertIsNotNone(case.check(cfg(sniper_cl_guard=True, sniper_cl_max_age_s=5.0)))
        strict = Case()
        strict.set_live(70020.0, time.time() - 3.0)
        prepared = strict.check(cfg(sniper_cl_guard=True, sniper_cl_max_age_s=1.0))
        self.assertIsNotNone(prepared)
        self.assertEqual(prepared["gap_meta"]["cl_guard_decision"], "fail_open_stale")

    def test_missing_feed_fails_open(self):
        for rtds in (None, SimpleNamespace(_live=None), SimpleNamespace(_live=live(None, None))):
            case = Case()
            case.strategy.rtds_feed = rtds
            prepared = case.check(cfg(sniper_cl_guard=True))
            self.assertIsNotNone(prepared)
            self.assertEqual(prepared["gap_meta"]["cl_guard_decision"], "fail_open_missing")
            self.assertFalse([kw for kw in case.rejects() if kw["gate"] == "cl_wrong_side"])


class ProjectedModeTests(unittest.TestCase):
    def test_projected_uses_coinbase_move_since_the_print(self):
        # CL printed 70005, $5 above the strike, so raw mode passes UP. Coinbase
        # has fallen $10 since that print, so the projected print is 69995 and
        # UP is rejected before any order is sent.
        obs = time.time() - 1.0
        grid = FakeGrid()
        grid.feeds["coinbase"] = (grid.exchange_ms, grid.exchange_ms, 70000.0)
        grid.lookups = {"coinbase": 70010.0}

        raw = Case()
        raw.set_live(70005.0, obs)
        prepared = raw.check(cfg(sniper_cl_guard=True, sniper_cl_guard_mode="raw",
                                 sniper_cl_min_gap_usd=0.0), grid=grid)
        self.assertIsNotNone(prepared)
        self.assertEqual(prepared["fav"], "up")
        self.assertEqual(prepared["gap_meta"]["cl_gap_usd"], 5.0)

        case = Case()
        case.set_live(70005.0, obs)
        self.assertIsNone(case.check(cfg(sniper_cl_guard=True, sniper_cl_guard_mode="projected",
                                         sniper_cl_min_gap_usd=0.0), grid=grid))
        reject = case.rejects()[0]
        self.assertEqual(reject["side"], "up")
        self.assertEqual(reject["gate"], "cl_wrong_side")
        self.assertEqual(reject["cl_gap_usd"], -5.0)
        self.assertEqual(reject["cl_guard_mode"], "projected")
        self.assertEqual(reject["cl_guard_decision"], "reject")

    def test_projected_falls_back_to_raw_without_coinbase(self):
        case = Case()
        case.set_live(70020.0, time.time() - 1.0)
        grid = FakeGrid()  # no coinbase tick or lookup
        prepared = case.check(cfg(sniper_cl_guard=True, sniper_cl_guard_mode="projected"), grid=grid)
        self.assertIsNotNone(prepared)
        self.assertEqual(prepared["gap_meta"]["cl_guard_mode"], "projected_fallback_raw")
        self.assertEqual(prepared["gap_meta"]["cl_gap_usd"], 20.0)

        # A missing lookup alone also falls back to raw.
        case2 = Case()
        case2.set_live(70020.0, time.time() - 1.0)
        grid2 = FakeGrid()
        grid2.feeds["coinbase"] = (grid2.exchange_ms, grid2.exchange_ms, 70010.0)
        prepared2 = case2.check(cfg(sniper_cl_guard=True, sniper_cl_guard_mode="projected"), grid=grid2)
        self.assertEqual(prepared2["gap_meta"]["cl_guard_mode"], "projected_fallback_raw")


class HotConfigTests(unittest.TestCase):
    def test_guard_reads_config_each_call(self):
        case = Case()
        case.set_live(69900.0, time.time())
        config = cfg(sniper_cl_guard=False)
        self.assertIsNotNone(case.check(config))
        case.row.pop("sniper_5m", None)
        config["sniper_cl_guard"] = True
        self.assertIsNone(case.check(config))
        self.assertEqual(case.rejects()[0]["gate"], "cl_wrong_side")

        # Switching mode is picked up on the next call without a restart.
        case2 = Case()
        case2.set_live(70005.0, time.time() - 1.0)
        grid = FakeGrid()
        grid.feeds["coinbase"] = (grid.exchange_ms, grid.exchange_ms, 70000.0)
        grid.lookups = {"coinbase": 70010.0}
        config2 = cfg(sniper_cl_guard=True, sniper_cl_guard_mode="raw")
        prepared = case2.check(config2, grid=grid)
        self.assertEqual(prepared["gap_meta"]["cl_guard_mode"], "raw")
        case2.row.pop("sniper_5m", None)
        case2.logs.clear()
        config2["sniper_cl_guard_mode"] = "projected"
        # Coinbase fell $10 since the print, so projected CL is now on the far side.
        self.assertIsNone(case2.check(config2, grid=grid))
        reject = case2.rejects()[0]
        self.assertEqual(reject["gate"], "cl_wrong_side")
        self.assertEqual(reject["cl_guard_mode"], "projected")
        self.assertEqual(reject["cl_gap_usd"], -5.0)


if __name__ == "__main__":
    unittest.main()
