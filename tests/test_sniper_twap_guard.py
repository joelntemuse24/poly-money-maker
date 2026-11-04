"""Last-minute settlement-average guard for the 5m sniper."""
from __future__ import annotations

import json
import time
import unittest
from types import SimpleNamespace

from buy.fivemin import (DEFAULTS, FiveMinIO, Strategy, chainlink_guard_reading,
                         projected_settlement_average)
from buy.oracle_log import RtdsTwapFeed

import test_fivemin as helpers

STRIKE = 90000.0


def cfg(**extra):
    value = helpers.cfg(sniper_5m_enabled=True, sniper_use_offset=True)
    value.update(extra)
    return value


class FakeGrid:
    """Only the futures-side reads one scan performs; the CL series is separate.

    ``up_jump`` / ``down_jump`` pick the side that reaches the guard: the scan
    needs the jump threshold only on the side under test.
    """

    def __init__(self, *, up_jump=0.0, down_jump=0.0, spot=89970.0):
        self.exchange_ms = time.time() * 1000.0 - 100.0
        self.info = {"spot": spot, "min": spot, "max": spot,
                     "up_jump": up_jump, "down_jump": down_jump,
                     "ts_ms": self.exchange_ms, "source": "primary"}

    def best_jump_in_window(self, window_ms=500.0, now_ms=None, *, sources=None, recv_now_ms=None):
        return self.info

    def latest_exchange_ms(self):
        return self.exchange_ms

    def latest_source_tick(self, source="primary"):
        return None

    def source_price_at(self, source, target_ms, *, window_ms=60000.0):
        return None


def live(price, obs_ts):
    return SimpleNamespace(price=str(price) if price is not None else None,
                          obs_ts=obs_ts, recv_ts=obs_ts)


def feed_with(points, *, history_s=90.0):
    """Real RTDS feed seeded with ``(seconds_before_now, price)`` live prints."""
    now = time.time()
    feed = RtdsTwapFeed(history_s=history_s, clock=lambda: now)
    for offset, price in sorted(points, reverse=True):
        obs_ms = int(round((now - offset) * 1000.0))
        feed.handle_message(json.dumps({
            "topic": "crypto_prices_chainlink",
            "type": "update",
            "timestamp": obs_ms,
            "payload": {"symbol": "btc/usd", "timestamp": obs_ms, "value": price},
        }))
    return feed


class Case:
    def __init__(self, ttm=30.0):
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
        start = int(time.time()) - (300 - int(ttm))
        self.strategy.add_market(helpers.market(start=start), now=start - 1)
        self.row = self.strategy.state["markets"]["cid"]
        self.row["strike"] = STRIKE
        self.row["offset"] = 0.0
        self.strategy.sample_cash(time.time(), 1000)
        self.ttm = ttm

    def attach(self, feed):
        self.strategy.rtds_feed = feed

    @staticmethod
    def quotes(ask=.94):
        return {"up": {"bid": .90, "bids": [], "asks": [[ask, 25.0]], "age_ms": 12.0, "src": "ws"},
                "down": {"bid": .06, "bids": [], "asks": [[ask, 25.0]], "age_ms": 12.0, "src": "ws"}}

    def check(self, config, grid=None, quotes=None):
        self.row["end"] = time.time() + self.ttm
        return self.strategy._sniper_check(config, time.time(), self.row,
                                           quotes or self.quotes(), grid or FakeGrid(),
                                           self.ttm, defer_buy=True)

    def rejects(self):
        return [kw for event, kw in self.logs if event == "sniper_gate_reject"]

    def events(self, name):
        return [kw for event, kw in self.logs if kw.get("slug") and event == name]


def down_window_feed(spot=89994.2):
    """02:34 UTC shape: spot $5.8 below the strike, elapsed average ~$26 above.

    Elapsed 30s of the averaging window held ~90030 for the first 20s and
    drifted to 90000 for the last 10s, so the elapsed average is ~90020. With
    30s left the projection holds spot (89994.2) for the rest and settles at
    ~90007, which is above the strike: Down loses on average.
    """
    points = [(30.0 - 2.0 * i, 90030.0) for i in range(1, 10)]
    points += [(10.0 - 2.0 * i, 90000.0) for i in range(0, 5)]
    points += [(1.0, spot)]
    return feed_with(points)


class ProjectedAverageTests(unittest.TestCase):
    def test_elapsed_average_plus_current_price_for_the_remainder(self):
        # 30s elapsed at a flat 130 plus 30s held at spot 100 -> 115.
        feed = feed_with([(1.0, 130.0)] + [(float(i), 130.0) for i in range(2, 30)])
        avg, covered = projected_settlement_average(feed, time.time(), 30.0, 100.0)
        self.assertAlmostEqual(covered, 30.0, delta=0.6)
        self.assertAlmostEqual(avg, 115.0, delta=0.5)

    def test_missing_or_short_history_returns_none(self):
        self.assertIsNone(projected_settlement_average(None, time.time(), 30.0, 100.0))
        self.assertIsNone(projected_settlement_average(SimpleNamespace(), time.time(), 30.0, 100.0))
        # 40s of the window elapsed but the feed only holds the last 5s: fail
        # open instead of averaging the short slice.
        late = feed_with([(1.0, 100.0), (5.0, 100.0)])
        self.assertIsNone(projected_settlement_average(late, time.time(), 20.0, 100.0))

    def test_empty_feed_returns_none(self):
        self.assertIsNone(projected_settlement_average(RtdsTwapFeed(history_s=90.0),
                                                      time.time(), 30.0, 100.0))


class TwapGuardTests(unittest.TestCase):
    def test_0234_case_blocks_down(self):
        """Down at 30s left: spot is $5.8 on our side, the average is not."""
        case = Case(ttm=30.0)
        case.attach(down_window_feed())
        grid = FakeGrid(down_jump=30.0, spot=89970.0)
        self.assertIsNone(case.check(
            cfg(sniper_cl_guard=True, sniper_cl_guard_mode="raw"), grid=grid))
        reject = case.rejects()[0]
        self.assertEqual(reject["gate"], "cl_wrong_side")
        self.assertEqual(reject["side"], "down")
        self.assertEqual(reject["cl_twap_mode"], "settlement")
        # The projected average sits above the strike, so Down's gap is negative.
        self.assertLess(reject["cl_gap_usd"], 0)
        self.assertGreater(reject["cl_guard_value"], STRIKE)
        self.assertGreater(reject["cl_twap_covered_s"], 20.0)
        # The blocked trade's ask is logged for audit.
        self.assertEqual(reject["detail"]["best_ask"], .94)
        self.assertEqual(reject["best_ask"], .94)
        self.assertEqual(reject["detail"]["cl_twap_mode"], "settlement")

    def test_raw_guard_would_have_passed_the_0234_case(self):
        """Same window with the minute guard off: spot alone says Down is fine."""
        case = Case(ttm=30.0)
        case.attach(down_window_feed())
        grid = FakeGrid(down_jump=30.0, spot=89970.0)
        prepared = case.check(
            cfg(sniper_cl_guard=True, sniper_cl_guard_mode="raw",
                sniper_cl_twap_guard=False), grid=grid)
        self.assertIsNotNone(prepared)
        self.assertEqual(prepared["fav"], "down")
        self.assertEqual(prepared["gap_meta"]["cl_guard_decision"], "pass")
        self.assertEqual(prepared["gap_meta"]["cl_twap_mode"], "off")

    def test_normal_case_passes(self):
        """Spot and its averaging window agree above the strike: Up passes."""
        case = Case(ttm=30.0)
        case.attach(feed_with([(1.0, 90030.0)] + [(float(i), 90028.0) for i in range(2, 30)]))
        grid = FakeGrid(up_jump=30.0, spot=90030.0)
        prepared = case.check(cfg(sniper_cl_guard=True, sniper_cl_guard_mode="raw"), grid=grid)
        self.assertIsNotNone(prepared)
        self.assertEqual(prepared["fav"], "up")
        self.assertEqual(prepared["gap_meta"]["cl_guard_decision"], "pass")
        self.assertEqual(prepared["gap_meta"]["cl_twap_mode"], "settlement")
        self.assertAlmostEqual(prepared["gap_meta"]["cl_guard_value"], 90030.0, delta=2.0)
        self.assertFalse([kw for kw in case.rejects() if kw["gate"] == "cl_wrong_side"])

    def test_outside_the_last_minute_keeps_the_raw_behaviour(self):
        case = Case(ttm=100.0)
        case.attach(down_window_feed())
        grid = FakeGrid(down_jump=30.0, spot=89970.0)
        prepared = case.check(cfg(sniper_cl_guard=True, sniper_cl_guard_mode="raw"), grid=grid)
        # Spot is $5.8 past the strike for Down, exactly as the raw guard reads it.
        self.assertIsNotNone(prepared)
        self.assertEqual(prepared["fav"], "down")
        self.assertEqual(prepared["gap_meta"]["cl_guard_value"], 89994.2)
        self.assertIsNone(prepared["gap_meta"]["cl_twap_mode"])
        self.assertEqual(prepared["gap_meta"]["cl_guard_mode"], "raw")

    def test_the_guard_switches_on_at_sixty_seconds(self):
        high = [(1.0, 90030.0)] + [(float(i), 90028.0) for i in range(2, 30)]
        just_out = Case(ttm=61.0)
        just_out.attach(feed_with(high))
        prepared = just_out.check(cfg(sniper_cl_guard=True),
                                  grid=FakeGrid(up_jump=30.0, spot=90030.0))
        self.assertIsNotNone(prepared)
        self.assertEqual(prepared["gap_meta"]["cl_guard_value"], 90030.0)
        self.assertIsNone(prepared["gap_meta"]["cl_twap_mode"])

        inside = Case(ttm=50.0)
        inside.attach(feed_with(high))
        prepared = inside.check(cfg(sniper_cl_guard=True),
                                grid=FakeGrid(up_jump=30.0, spot=90030.0))
        self.assertEqual(prepared["gap_meta"]["cl_twap_mode"], "settlement")
        self.assertGreater(prepared["gap_meta"]["cl_guard_value"], STRIKE)

    def test_an_elapsed_minute_at_the_high_side_blocks_the_spot_side(self):
        """Eight seconds elapsed far above the strike outvote the spot print."""
        case = Case(ttm=52.0)
        case.attach(feed_with([(1.0, 89994.2)]
                              + [(float(i), 90200.0) for i in range(2, 9)]))
        grid = FakeGrid(down_jump=30.0, spot=89970.0)
        self.assertIsNone(case.check(cfg(sniper_cl_guard=True), grid=grid))
        reject = case.rejects()[0]
        self.assertEqual(reject["side"], "down")
        self.assertEqual(reject["gate"], "cl_wrong_side")
        self.assertEqual(reject["cl_twap_mode"], "settlement")
        self.assertGreater(reject["cl_guard_value"], STRIKE)

    def test_missing_history_fails_open_to_the_old_reference(self):
        case = Case(ttm=30.0)
        case.attach(SimpleNamespace(_live=live(90030.0, time.time() - 1.0)))
        prepared = case.check(cfg(sniper_cl_guard=True),
                              grid=FakeGrid(up_jump=30.0, spot=90030.0))
        self.assertIsNotNone(prepared)
        self.assertEqual(prepared["gap_meta"]["cl_twap_mode"], "fallback_spot")
        self.assertEqual(prepared["gap_meta"]["cl_guard_value"], 90030.0)

    def test_stale_print_still_fails_open_inside_the_minute(self):
        case = Case(ttm=30.0)
        case.attach(down_window_feed())
        case.strategy.rtds_feed._live = live(89990.0, time.time() - 30.0)
        prepared = case.check(cfg(sniper_cl_guard=True),
                              grid=FakeGrid(up_jump=30.0, spot=90030.0))
        self.assertIsNotNone(prepared)
        self.assertEqual(prepared["gap_meta"]["cl_guard_decision"], "fail_open_stale")
        self.assertIsNone(prepared["gap_meta"]["cl_twap_mode"])

    def test_projected_mode_keeps_its_nowcast_with_the_minute_guard_off(self):
        now = time.time()
        reading = chainlink_guard_reading(
            cfg(sniper_cl_guard=True, sniper_cl_guard_mode="projected",
                sniper_cl_twap_guard=False),
            FakeGrid(), live(90005.0, now - 1.0), now, ttm=30.0, feed=None)
        self.assertEqual(reading["cl_guard_mode"], "projected_fallback_raw")
        self.assertEqual(reading["cl_ref"], 90005.0)
        self.assertEqual(reading["cl_twap_mode"], "off")

    def test_guard_is_disabled_when_the_guard_flag_is_off(self):
        now = time.time()
        reading = chainlink_guard_reading(cfg(sniper_cl_guard=False), None,
                                          live(90005.0, now - 1.0), now, ttm=30.0)
        self.assertEqual(reading, {"cl_spot": None, "cl_ref": None, "cl_age_s": None,
                                   "cl_guard_mode": None, "cl_guard_decision": "off",
                                   "cl_twap_mode": None, "cl_twap_covered_s": None})

    def test_defaults_are_on_and_revertible(self):
        self.assertTrue(DEFAULTS["sniper_cl_twap_guard"])


if __name__ == "__main__":
    unittest.main()
