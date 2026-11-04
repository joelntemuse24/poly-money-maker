"""Sniper wrong-side (direction) guard, trigger-lag cutoff and reconnect backoff.

The direction guard exists for the Nov 3 15:53:32 case: Coinbase ticks in the
500ms window went 77492.30 -> 77602.64 -> 77571.72, which the window maths read
as up_jump 79.42 and down_jump 30.92. Up failed min_gap, so the DOWN side fired
on the retrace although the window net moved +79.
"""
from __future__ import annotations

import time
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

import mintbot
from buy.binance_trades import BinanceTrades, PriceGrid, reconnect_backoff_s
from buy.fivemin import FiveMinIO, Strategy

import test_fivemin as helpers

STRIKE = 90000.0
WINDOW_MS = 500.0
PRIMARY = 77490.0


def cfg(**extra):
    value = helpers.cfg(sniper_5m_enabled=True, sniper_use_offset=True,
                        sniper_gap_estimator="legacy", sniper_window_ms=WINDOW_MS)
    value.update(extra)
    return value


class Case:
    """One sniper scan against real books and a real ``PriceGrid``."""

    def __init__(self, ttm=100.0, *, strike=None, ask=.94):
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
        self.row["strike"] = STRIKE if strike is None else strike
        self.row["offset"] = 0.0
        self.strategy.sample_cash(time.time(), 1000)
        self.ttm = ttm
        self.ask = ask

    def quotes(self, ask=None):
        ask = self.ask if ask is None else ask
        return {"up": {"bid": ask - .04, "bids": [], "asks": [[ask, 25.0]], "age_ms": 12.0, "src": "ws"},
                "down": {"bid": .06, "bids": [], "asks": [[ask, 25.0]], "age_ms": 12.0, "src": "ws"}}

    def check(self, config, grid=None, quotes=None):
        self.row["end"] = time.time() + self.ttm
        return self.strategy._sniper_check(config, time.time(), self.row,
                                           quotes or self.quotes(), grid if grid is not None else PriceGrid(),
                                           self.ttm, defer_buy=True)

    def rejects(self):
        return [kw for event, kw in self.logs if event == "sniper_gate_reject"]

    def events(self, name):
        return [kw for event, kw in self.logs if event == name]


def jump_grid(points, *, source="coinbase", lag_ms=0.0, primary=PRIMARY):
    """A grid whose ``source`` feed holds ``points`` (recv offset ms, price).

    The offsets are placed inside the 500ms window the scan evaluates; the
    primary feed gets one flat tick so ``latest_exchange_ms`` stays fresh. The
    newest ``source`` tick carries ``lag_ms`` between its exchange stamp and its
    local receive wall stamp, which is what the lag cutoff reads.
    """
    grid = PriceGrid()
    wall_ms = time.time() * 1000.0
    mono_ms = time.monotonic() * 1000.0
    # The grid buckets exchange stamps up to the next 100ms, so keep the primary
    # tick well behind the scan clock or ``latest_exchange_ms`` reads as future.
    grid.add(wall_ms - 250.0, primary, recv_ms=mono_ms - 250.0, recv_wall_ms=wall_ms - 250.0)
    base = mono_ms - 400.0
    for index, (offset, px) in enumerate(points):
        newest = index == len(points) - 1
        exchange_ms = wall_ms - (lag_ms if newest else 0.0) - (len(points) - 1 - index)
        recv_wall_ms = wall_ms - (0.0 if newest else (len(points) - 1 - index))
        grid.add(exchange_ms, px, source=source, recv_ms=base + offset,
                 recv_wall_ms=recv_wall_ms)
    return grid


def spike_and_retrace():
    """The 15:53:32 shape: up_jump 79.42, down_jump 30.92, net +79.42."""
    return jump_grid([(0.0, 77492.30), (200.0, 77602.64), (400.0, 77571.72)])


def clean_up_move():
    """A straight move up: up_jump 30, down_jump 0, net +30."""
    return jump_grid([(0.0, 77500.0), (400.0, 77530.0)])


class DirectionGuardTests(unittest.TestCase):
    def test_spike_and_retrace_does_not_fire_the_down_side(self):
        case = Case(strike=77600.0)
        grid = spike_and_retrace()
        assert case.check(cfg(), grid=grid) is None
        self.assertFalse(case.buys)

        by_side = {kw["side"]: kw for kw in case.rejects() if kw["gate"] == "direction"}
        # Both magnitudes clear the threshold, so neither side is tradeable.
        self.assertEqual(by_side["down"]["detail"]["decision"], "reject_both")
        self.assertEqual(by_side["up"]["detail"]["decision"], "reject_both")
        self.assertEqual(by_side["down"]["jump_up_usd"], 79.42)
        self.assertEqual(by_side["down"]["jump_down_usd"], 30.92)
        # The window net move (+79.42) is what makes down the wrong side.
        self.assertEqual(by_side["down"]["jump_net_usd"], 79.42)
        self.assertEqual(by_side["down"]["detail"]["net_usd"], 79.42)
        self.assertTrue(by_side["down"]["jump_dir_check"])
        # The blocked trade's ask rides with the reject.
        self.assertEqual(by_side["down"]["best_ask"], .94)

    def test_a_jump_a_hair_under_the_threshold_still_counts_as_tripped(self):
        case = Case(strike=77600.0)
        # down_jump lands 1e-10 under sniper_jump_usd; the side loop's
        # ``jump_usd + EPS >= threshold`` gate lets the down side through, so the
        # both-trip test must call it tripped too. Net +30 from the window open.
        grid = jump_grid([(0.0, 77445.0000000001), (200.0, 77500.0), (400.0, 77475.0000000001)])
        assert case.check(cfg(), grid=grid) is None
        by_side = {kw["side"]: kw for kw in case.rejects() if kw["gate"] == "direction"}
        self.assertEqual(by_side["down"]["detail"]["decision"], "reject_both")
        self.assertEqual(by_side["down"]["jump_down_usd"], 25.0)
        self.assertFalse(case.buys)

    def test_spike_and_retrace_fires_the_down_side_with_the_check_off(self):
        case = Case(strike=77600.0)
        grid = spike_and_retrace()
        prepared = case.check(cfg(sniper_jump_direction_check=False), grid=grid)
        self.assertIsNotNone(prepared)
        self.assertEqual(prepared["fav"], "down")
        self.assertEqual(prepared["gap_meta"]["jump_dir_decision"], "off")
        self.assertFalse([kw for kw in case.rejects() if kw["gate"] == "direction"])

    def test_v_shape_rejects_the_up_side_on_the_net_move(self):
        case = Case(strike=77450.0)
        # up_jump 26 trips the threshold, down_jump 4 does not; net is -4.
        grid = jump_grid([(0.0, 77500.0), (200.0, 77470.0), (400.0, 77496.0)])
        assert case.check(cfg(), grid=grid) is None
        reject = case.rejects()[0]
        self.assertEqual((reject["side"], reject["gate"]), ("up", "direction"))
        self.assertEqual(reject["detail"]["decision"], "reject_net")
        self.assertEqual(reject["jump_net_usd"], -4.0)
        self.assertEqual(reject["detail"]["net_usd"], -4.0)
        self.assertFalse(case.buys)

    def test_a_clean_move_passes_and_the_order_lines_carry_the_verdict(self):
        case = Case(strike=77450.0)
        prepared = case.check(cfg(), grid=clean_up_move())
        self.assertIsNotNone(prepared)
        self.assertEqual(prepared["fav"], "up")
        self.assertEqual(prepared["gap_meta"]["jump_dir_decision"], "pass")
        self.assertEqual(prepared["gap_meta"]["jump_net_usd"], 30.0)

        case.strategy._sniper_execute(prepared)
        fill = case.events("fivemin_sniper_fill")[0]
        self.assertEqual(fill["jump_dir_decision"], "pass")
        self.assertEqual(fill["jump_net_usd"], 30.0)
        latency = case.events("sniper_order_latency")[0]
        self.assertEqual(latency["jump_dir_decision"], "pass")
        self.assertEqual(latency["jump_net_usd"], 30.0)
        self.assertEqual(latency["jump_up_usd"], 30.0)
        self.assertEqual(latency["jump_down_usd"], 0.0)

    def test_a_grid_without_window_open_price_fails_open(self):
        class NoOpenPrice:
            """The primary ``jump_in_window`` fallback shape: no bounds either."""

            def __init__(self):
                now_ms = time.time() * 1000.0
                self.info = {"spot": 77530.0, "min": 77500.0, "max": 77530.0,
                             "up_jump": 30.0, "down_jump": 0.0,
                             "ts_ms": now_ms, "source": "primary"}
                self.exchange_ms = now_ms

            def best_jump_in_window(self, window_ms=500.0, now_ms=None, *, sources=None,
                                    recv_now_ms=None):
                return self.info

            def latest_exchange_ms(self):
                return self.exchange_ms

        case = Case(strike=77450.0)
        prepared = case.check(cfg(), grid=NoOpenPrice())
        self.assertIsNotNone(prepared)
        self.assertEqual(prepared["gap_meta"]["jump_dir_decision"], "unavailable")
        self.assertIsNone(prepared["gap_meta"]["jump_net_usd"])

    def test_shared_grid_trigger_fails_open(self):
        case = Case(strike=77450.0)
        # feed_process SharedGrid: ``best_jump_in_window`` returns the trigger
        # record, which has no window bounds and no window_open_price method.
        info = {"spot": 77530.0, "raw_spot": 77530.0, "min": 77500.0, "max": 77530.0,
                "up_jump": 30.0, "down_jump": 0.0, "ts_ms": time.time() * 1000.0,
                "recv_wall_ms": time.time() * 1000.0, "source": "coinbase"}
        grid = SimpleNamespace(best_jump_in_window=lambda *a, **kw: info,
                               latest_exchange_ms=lambda: info["ts_ms"])
        prepared = case.check(cfg(), grid=grid)
        self.assertIsNotNone(prepared)
        self.assertEqual(prepared["gap_meta"]["jump_dir_decision"], "unavailable")


class WindowOpenPriceTests(unittest.TestCase):
    def _grid(self):
        grid = PriceGrid()
        for recv, px in ((1000.0, 1.0), (1100.0, 2.0), (1200.0, 3.0)):
            grid.add(9_000_000.0, px, source="coinbase", recv_ms=recv)
        grid.add(9_000_000.0, 99.0, recv_ms=1150.0)
        return grid

    def test_returns_the_oldest_tick_inside_the_window(self):
        grid = self._grid()
        self.assertEqual(grid.window_open_price("coinbase", 1050.0, 1150.0), 2.0)
        self.assertEqual(grid.window_open_price("coinbase", 1000.0, 1200.0), 1.0)
        self.assertEqual(grid.window_open_price("coinbase", 1150.0, 1200.0), 3.0)

    def test_returns_none_outside_the_ticks_or_for_another_source(self):
        grid = self._grid()
        self.assertIsNone(grid.window_open_price("coinbase", 900.0, 950.0))
        self.assertIsNone(grid.window_open_price("coinbase", 1250.0, 1300.0))
        self.assertIsNone(grid.window_open_price("bybit", 900.0, 1300.0))
        self.assertIsNone(grid.window_open_price("coinbase", 1200.0, 1100.0))

    def test_primary_ticks_stay_in_their_own_source(self):
        grid = self._grid()
        # The primary tick at recv 1150 is not Coinbase's window.
        self.assertEqual(grid.window_open_price("primary", 1100.0, 1200.0), 99.0)

    def test_best_jump_carries_the_window_bounds(self):
        grid = PriceGrid()
        grid.add(1_000_100.0, 70000.0, recv_ms=1_000_100.0)
        grid.add(1_000_500.0, 70030.0, recv_ms=1_000_500.0)
        info = grid.best_jump_in_window(500.0, recv_now_ms=1_000_520.0)
        self.assertEqual(info["win_lower_ms"], 1_000_020.0)
        self.assertEqual(info["win_ref_ms"], 1_000_520.0)
        self.assertEqual(grid.window_open_price("primary", info["win_lower_ms"],
                                                info["win_ref_ms"]), 70000.0)


class TriggerLagTests(unittest.TestCase):
    def test_lag_over_the_threshold_rejects_with_trigger_lag(self):
        case = Case(strike=77450.0)
        grid = jump_grid([(0.0, 77500.0), (400.0, 77530.0)], lag_ms=600.0)
        assert case.check(cfg(sniper_max_trigger_lag_ms=500.0), grid=grid) is None
        reject = case.rejects()[0]
        self.assertEqual((reject["side"], reject["gate"]), ("up", "trigger_lag"))
        self.assertEqual(reject["trigger_lag_ms"], 600.0)
        self.assertEqual(reject["detail"]["lag_ms"], 600.0)
        self.assertEqual(reject["detail"]["max_lag_ms"], 500.0)
        self.assertEqual(reject["best_ask"], .94)
        self.assertFalse(case.buys)

    def test_threshold_zero_disables_the_cutoff_and_still_logs_the_lag(self):
        case = Case(strike=77450.0)
        grid = jump_grid([(0.0, 77500.0), (400.0, 77530.0)], lag_ms=600.0)
        prepared = case.check(cfg(sniper_max_trigger_lag_ms=0), grid=grid)
        self.assertIsNotNone(prepared)
        self.assertEqual(prepared["gap_meta"]["trigger_lag_ms"], 600.0)
        self.assertEqual(prepared["gap_meta"]["max_trigger_lag_ms"], 0.0)
        self.assertFalse([kw for kw in case.rejects() if kw["gate"] == "trigger_lag"])

    def test_missing_stamps_skip_the_check(self):
        case = Case(strike=77450.0)
        grid = jump_grid([(0.0, 77500.0), (400.0, 77530.0)])
        grid._latest_recv_wall_ms.pop("coinbase", None)
        prepared = case.check(cfg(sniper_max_trigger_lag_ms=1.0), grid=grid)
        self.assertIsNotNone(prepared)
        self.assertIsNone(prepared["gap_meta"]["trigger_lag_ms"])


class ReconnectBackoffTests(unittest.TestCase):
    def test_schedule(self):
        self.assertEqual(RECONNECT_SCHEDULE, (1.0, 2.0, 5.0, 10.0))
        self.assertEqual([reconnect_backoff_s(a) for a in (0, 1, 2, 3, 4, 5, 9, 100)],
                         [1.0, 1.0, 2.0, 5.0, 10.0, 10.0, 10.0, 10.0])

    def _run_with_connections(self, make_ws, *, limit=8):
        """Drive ``_run`` over extra-feed connections, recording the sleeps."""
        sleeps = []
        connects = []

        def connect(url):
            connects.append(url)
            return make_ws(len(connects))

        def sleep(seconds):
            sleeps.append(seconds)
            if len(sleeps) >= limit:
                bt.stop()

        bt = BinanceTrades(connect=connect, sleep=sleep)
        bt._run("wss://ws-feed.exchange.coinbase.com")
        return sleeps, connects

    def test_a_connection_that_received_messages_resets_to_one_second(self):
        class DroppingWS:
            """Delivers one tick, then the exchange drops the socket."""

            def __init__(self):
                self._sent = False

            def recv(self):
                if not self._sent:
                    self._sent = True
                    return '{"type":"match","price":"70000.0"}'
                raise ConnectionError("Connection to remote host was lost")

            def close(self):
                pass

        sleeps, connects = self._run_with_connections(lambda _n: DroppingWS(), limit=5)
        # Every drop happened on a live connection, so every wait is 1s.
        self.assertEqual(sleeps, [1.0, 1.0, 1.0, 1.0, 1.0])
        self.assertEqual(len(connects), 5)

    def test_a_connection_that_never_delivered_climbs_the_ladder(self):
        def connect(_url):
            raise TimeoutError("connect timed out")

        sleeps = []

        def sleep(seconds):
            sleeps.append(seconds)
            if len(sleeps) >= 4:
                bt.stop()

        bt = BinanceTrades(connect=connect, sleep=sleep)
        bt._run("wss://ws-feed.exchange.coinbase.com")
        self.assertEqual(sleeps, [1.0, 2.0, 5.0, 10.0])


RECONNECT_SCHEDULE = (1.0, 2.0, 5.0, 10.0)


class LimitAndPresignTests(unittest.TestCase):
    def test_the_limit_is_sniper_max_ask_whatever_the_ask(self):
        for ask in (.74, .90, .96):
            with self.subTest(ask=ask):
                case = Case(strike=77450.0, ask=ask)
                prepared = case.check(cfg(), grid=clean_up_move())
                self.assertIsNotNone(prepared)
                self.assertAlmostEqual(prepared["limit"], .97)
                case.strategy._sniper_execute(prepared)
                self.assertAlmostEqual(case.buys[0][2], .97)
                fill = case.events("fivemin_sniper_fill")[0]
                self.assertAlmostEqual(fill["limit"], .97)
                self.assertNotIn("limit_cap", fill)
                self.assertAlmostEqual(fill["decision_best_ask"], ask)
                latency = case.events("sniper_order_latency")[0]
                self.assertAlmostEqual(latency["limit"], .97)
                self.assertNotIn("limit_cap", latency)

    def test_warmup_signs_one_price_at_sniper_max_ask(self):
        class ClobClient:
            """The warmup only pre-signs for the real client class name."""

            def __init__(self):
                self.builder_config = None

            def get_tick_size(self, _token):
                return "0.01"

            def get_neg_risk(self, _token):
                return False

            def _ClobClient__resolve_version(self):
                return 2

        cache = Mock()
        events = []
        with patch.object(mintbot, "_fak_market_cache", {}), \
                patch.object(mintbot, "_fak_warmup_attempted", {}), \
                patch.object(mintbot, "_fak_warmup_client_attempted", False), \
                patch.object(mintbot, "_fak_warmup_warn_at", None), \
                patch.dict(mintbot.__dict__, {"_keepalive_cfg": {
                    "presign_enabled": True, "sniper_5m_enabled": True,
                    "sniper_shares": 10, "sniper_max_ask": 0.97}}), \
                patch.object(mintbot, "_get_clob_client", return_value=ClobClient()), \
                patch.object(mintbot, "log_event",
                             side_effect=lambda event, **kw: events.append((event, kw))), \
                patch("buy.presign.get_presign_cache", return_value=cache):
            mintbot._fak_warmup_sync(["up", "dn"])
        self.assertEqual(cache.presign_market_order.call_count, 2)  # one per token
        for call in cache.presign_market_order.call_args_list:
            self.assertAlmostEqual(call.args[4], .97)
        warmed = [kw for event, kw in events if event == "presign_warmed"]
        self.assertEqual([w["price"] for w in warmed], [.97, .97])


class BestAskOnRejectTests(unittest.TestCase):
    def _fake_grid(self, *, up_jump=30.0, spot=77530.0, strike=77450.0):
        now_ms = time.time() * 1000.0
        info = {"spot": spot, "min": spot - up_jump, "max": spot,
                "up_jump": up_jump, "down_jump": 0.0,
                "ts_ms": now_ms, "recv_wall_ms": now_ms, "source": "primary"}

        class FakeGrid:
            def best_jump_in_window(self, window_ms=500.0, now_ms=None, *, sources=None,
                                    recv_now_ms=None):
                return info

            def latest_exchange_ms(self):
                return now_ms

        return FakeGrid()

    def test_min_gap_reject_logs_the_side_best_ask(self):
        case = Case(strike=77600.0)
        assert case.check(cfg(), grid=self._fake_grid()) is None
        reject = case.rejects()[0]
        self.assertEqual(reject["gate"], "min_gap")
        self.assertEqual(reject["best_ask"], .94)

    def test_book_age_reject_logs_the_side_best_ask(self):
        case = Case(strike=77450.0)
        quotes = case.quotes()
        quotes["up"]["age_ms"] = 5000.0
        assert case.check(cfg(), grid=self._fake_grid(), quotes=quotes) is None
        reject = case.rejects()[0]
        self.assertEqual(reject["gate"], "book_age")
        self.assertEqual(reject["best_ask"], .94)

    def test_ask_range_reject_logs_the_side_best_ask(self):
        case = Case(strike=77450.0)
        quotes = case.quotes(ask=.40)
        assert case.check(cfg(), grid=self._fake_grid(), quotes=quotes) is None
        reject = case.rejects()[0]
        self.assertEqual(reject["gate"], "ask_range")
        self.assertEqual(reject["best_ask"], .40)

    def test_min_fill_reject_logs_the_side_best_ask(self):
        case = Case(strike=77450.0)
        # The ask-range gate already catches a cheap ask, so drive the min-fill
        # branch directly to check it also carries the blocked side's ask.
        with patch.object(type(case.strategy), "_min_fill_blocked", return_value=True):
            assert case.check(cfg(), grid=self._fake_grid()) is None
        reject = case.rejects()[0]
        self.assertEqual(reject["gate"], "min_fill")
        self.assertEqual(reject["best_ask"], .94)


if __name__ == "__main__":
    unittest.main()
