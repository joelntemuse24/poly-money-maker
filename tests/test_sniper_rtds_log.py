"""Sniper orders log Polymarket's live RTDS/Chainlink print next to the offset estimate.

Diagnostics only: the helper runs after the POST, never raises, and the
snapshot it reads is taken at prepare time.
"""
import json
import unittest
from types import SimpleNamespace

from buy.binance_trades import PriceGrid
from buy.fivemin import FiveMinIO, Runner, Strategy, rtds_compare_fields
from buy.oracle_log import LivePrice

import test_fivemin as helpers

FIELDS = {"rtds_price", "rtds_obs_ts", "rtds_recv_ts", "rtds_age_ms", "offset",
          "est_price", "rtds_gap_usd", "est_minus_rtds_usd"}


def _grid(recv_wall_ms=1_200_020.0):
    grid = PriceGrid()
    grid.add(1_199_600.0, 70000.0, recv_ms=1_199_600.0)
    grid.add(1_200_000.0, 70030.0, recv_ms=1_200_000.0, recv_wall_ms=recv_wall_ms)
    return grid


def _quotes():
    return {"up": {"bid": .90, "bids": [], "asks": [[.94, 25.0]], "age_ms": 12.0, "src": "ws"},
            "down": {"bid": .06, "bids": [], "asks": [], "age_ms": 12.0, "src": "ws"}}


def _prepared(live, *, fav="up", price="70030.456", obs_ts=999.0, recv_ts=999.5,
              decision_ts=1000.0, strike=70000.0, spot_now=70010.0, offset=5.0):
    return {"row": {"slug": "s", "strike": strike}, "fav": fav, "spot_now": spot_now,
            "offset": offset, "rtds_live": live,
            "post_meta": {"decision_ts": decision_ts}}


def _live(price="70030.456", obs_ts=999.0, recv_ts=999.5):
    return LivePrice(price=price, obs_ts=obs_ts, recv_ts=recv_ts)


class CompareFieldsTests(unittest.TestCase):
    def test_up_and_down_gap_signs(self):
        up = rtds_compare_fields(_prepared(_live(), fav="up"))
        assert set(up) == FIELDS
        assert up["rtds_price"] == 70030.46
        assert up["est_price"] == 70015.0
        assert up["offset"] == 5.0
        assert up["rtds_gap_usd"] == 30.46
        assert up["est_minus_rtds_usd"] == -15.46
        down = rtds_compare_fields(_prepared(_live(), fav="down"))
        assert down["rtds_gap_usd"] == -30.46
        assert down["est_minus_rtds_usd"] == -15.46

    def test_age_prefers_recv_ts_then_obs_ts_and_rounds_to_a_tenth(self):
        fields = rtds_compare_fields(_prepared(_live(recv_ts=997.5), decision_ts=1000.0))
        assert fields["rtds_recv_ts"] == 997.5
        assert fields["rtds_obs_ts"] == 999.0
        assert fields["rtds_age_ms"] == 2500.0
        stale = rtds_compare_fields(_prepared(_live(obs_ts=990.25, recv_ts=None), decision_ts=1000.0))
        assert stale["rtds_recv_ts"] is None
        assert stale["rtds_age_ms"] == 9750.0
        fresh = rtds_compare_fields(_prepared(_live(recv_ts=999.9876), decision_ts=1000.0))
        assert fresh["rtds_age_ms"] == 12.4

    def test_missing_feed_or_values_degrade_to_none(self):
        for live in (None, object(), SimpleNamespace(price="abc", obs_ts=None, recv_ts=None)):
            fields = rtds_compare_fields(_prepared(live))
            assert fields["rtds_price"] is None
            assert fields["rtds_gap_usd"] is None
            assert fields["est_minus_rtds_usd"] is None
            assert fields["est_price"] == 70015.0
        no_offset = rtds_compare_fields(_prepared(_live(), offset=None))
        assert no_offset["est_price"] is None
        assert no_offset["est_minus_rtds_usd"] is None
        assert no_offset["rtds_gap_usd"] == 30.46
        assert no_offset["offset"] is None

    def test_never_raises_and_unknown_side_skips_gap(self):
        empty = rtds_compare_fields({})
        assert empty == {key: None for key in FIELDS}
        assert rtds_compare_fields(None) == {key: None for key in FIELDS}
        assert rtds_compare_fields({"rtds_live": _live(), "fav": "sideways"})["rtds_gap_usd"] is None
        broken = {"rtds_live": _live(), "fav": "up", "row": "not-a-dict", "post_meta": 7}
        assert set(rtds_compare_fields(broken)) == FIELDS


class _Case:
    def __init__(self, result=None):
        self.logs = []
        self.clock = [1200.0]
        self.result = result or {"shares": 10, "cost": 9.4, "price": 0.94,
                                 "status": "matched", "timing": {"post": 12.5}}
        self.io = FiveMinIO(
            buy=lambda *a, **kw: dict(self.result),
            sell=lambda *a: {}, reserves=lambda cfg: 0, redeem=lambda *a: None,
            save=lambda state: None,
            log=lambda event, **kw: self.logs.append((event, kw)),
            alert=lambda *a, **kw: None, clock=lambda: self.clock[0],
        )
        self.strategy = Strategy(self.io, startup_ts=0)
        self.strategy.add_market(helpers.market(), now=1000)
        row = self.strategy.state["markets"]["cid"]
        row["strike"] = 70000.0
        row["offset"] = 5.0
        self.strategy.sample_cash(1200, 1000)

    def prepare(self, feed):
        self.strategy.rtds_feed = feed
        prepared = self.strategy._sniper_check(
            helpers.cfg(fivemin_dry_run=False, sniper_5m_enabled=True), 1200.0,
            self.strategy.state["markets"]["cid"], _quotes(), _grid(), 100.0, defer_buy=True,
        )
        assert prepared is not None
        prepared["post_meta"]["decision_ts"] = 1_700_000_000.0
        return prepared

    def events(self, name):
        return [kw for event, kw in self.logs if event == name]


class LoggedFieldsTests(unittest.TestCase):
    def test_fill_and_latency_lines_carry_the_fields(self):
        case = _Case()
        feed = SimpleNamespace(_live=_live(price="70029.5", obs_ts=1_699_999_999.0,
                                           recv_ts=1_699_999_998.0))
        prepared = case.prepare(feed)
        case.strategy._sniper_execute(prepared)
        fill = case.events("fivemin_sniper_fill")[0]
        latency = case.events("sniper_order_latency")[0]
        for fields in (fill, latency):
            assert set(FIELDS) <= set(fields)
            json.dumps(fields)
            assert fields["rtds_price"] == 70029.5
            assert fields["rtds_obs_ts"] == 1_699_999_999.0
            assert fields["rtds_recv_ts"] == 1_699_999_998.0
            assert fields["rtds_age_ms"] == 2000.0
            assert fields["offset"] == 5.0
            assert fields["rtds_gap_usd"] == 29.5
            assert fields["est_price"] == 70035.0
            assert fields["est_minus_rtds_usd"] == 5.5

    def test_nofill_line_carries_the_fields(self):
        nofill = _Case(result={"shares": 0, "cost": 0, "status": "unfilled"})
        nofill.strategy.rtds_feed = SimpleNamespace(_live=_live(price="70001.0"))
        prepared = nofill.prepare(SimpleNamespace(_live=_live(price="70001.0")))
        nofill.strategy._sniper_execute(prepared)
        assert nofill.events("fivemin_sniper_nofill")[0]["rtds_price"] == 70001.0
        assert nofill.events("sniper_order_latency")[0]["rtds_price"] == 70001.0

    def test_missing_feed_still_logs_null_fields(self):
        case = _Case()
        prepared = case.prepare(None)
        assert prepared["rtds_live"] is None
        case.strategy._sniper_execute(prepared)
        for name in ("fivemin_sniper_fill", "sniper_order_latency"):
            fields = case.events(name)[0]
            assert set(FIELDS) <= set(fields)
            assert fields["rtds_price"] is None
            assert fields["rtds_gap_usd"] is None
            assert fields["rtds_age_ms"] is None
            assert fields["offset"] == 5.0
            assert fields["est_price"] == 70035.0

    def test_snapshot_is_taken_at_prepare_time(self):
        case = _Case()
        first = _live(price="70000.0", obs_ts=1_699_999_999.0, recv_ts=1_699_999_999.5)
        feed = SimpleNamespace(_live=first)
        prepared = case.prepare(feed)
        assert prepared["rtds_live"] is first
        feed._live = _live(price="70500.0", obs_ts=1_800_000_000.0, recv_ts=1_800_000_000.0)
        case.strategy._sniper_execute(prepared)
        for name in ("fivemin_sniper_fill", "sniper_order_latency"):
            fields = case.events(name)[0]
            assert fields["rtds_price"] == 70000.0
            assert fields["rtds_gap_usd"] == 0.0
            assert fields["est_minus_rtds_usd"] == 35.0

    def test_order_error_path_logs_the_fields(self):
        case = _Case()

        def boom(*_a, **_k):
            raise RuntimeError("socket closed")

        case.io = FiveMinIO(**{**case.io.__dict__, "buy": boom})
        case.strategy.io = case.io
        prepared = case.prepare(SimpleNamespace(_live=_live(price="70002.25")))
        case.strategy._sniper_execute(prepared)
        fields = case.events("sniper_order_latency")[0]
        assert fields["status"] == "error:socket closed"
        assert fields["rtds_price"] == 70002.25


class RunnerWiringTests(unittest.TestCase):
    def _runner(self, strategy, rtds):
        stopped = SimpleNamespace(start=lambda: None, stop=lambda: None)
        return Runner(strategy, lambda: helpers.cfg(), None, None, "funder",
                      PriceGrid(), stopped, rtds, stopped, clock=lambda: 1200.0)

    def test_runner_hands_the_feed_to_the_strategy(self):
        io = FiveMinIO(buy=lambda *a: {}, sell=lambda *a: {}, reserves=lambda cfg: 0,
                       redeem=lambda *a: None, save=lambda *a: None)
        strategy = Strategy(io, startup_ts=0)
        assert strategy.rtds_feed is None
        rtds = SimpleNamespace(start=lambda: None, stop=lambda: None)
        self._runner(strategy, rtds)
        assert strategy.rtds_feed is rtds

    def test_wiring_guard_never_raises(self):
        class Locked:
            @property
            def rtds_feed(self):
                return None

            @rtds_feed.setter
            def rtds_feed(self, value):
                raise RuntimeError("locked")

        io = SimpleNamespace(rest_book=None, live_book=None, log=lambda *a, **k: None)
        strategy = Locked()
        strategy.io = io
        rtds = SimpleNamespace(start=lambda: None, stop=lambda: None)
        runner = self._runner(strategy, rtds)
        assert runner.rtds is rtds
