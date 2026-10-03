"""Scrap oracle veto: no loser scrap while the 60s TWAP still favours that leg.

Covers the pure ``scrap_oracle_veto`` helper, the sell loop (arm, fire,
blind, rest, hot reload, stale fallback), the dump staying untouched, the
RTDS feed's local receive stamp, and the hot-span watchdog that keeps that
in-memory value fresh through the last 6 minutes.
"""

from __future__ import annotations

import json
import tempfile
import timeit
import unittest
from pathlib import Path
from types import SimpleNamespace

from buy.mint_sell import (
    DEFAULT_SELL_KNOBS,
    scrap_oracle_settings,
    scrap_oracle_veto,
)
from buy.oracle_log import (
    FEED_BACKOFF_MAX_S,
    FEED_HOT_BACKOFF_S,
    FEED_HOT_TTM_S,
    FEED_SILENT_HOT_S,
    FEED_SILENT_RECONNECT_S,
    GAMMA_FIRST_S,
    RTDS_LIVE_TOPIC,
    SUBSCRIBE_FRAME,
    GammaStrike,
    OracleLogService,
    OracleWindow,
    RtdsTwapFeed,
    TwapSample,
    _WindowMemory,
    parse_rtds_live,
)
from test_mint_cpu import (
    MINT,
    _dump_cfg,
    _dump_harness,
    _fill_fak,
    _held_after_scrap,
    _open_scrap_bag,
    _scrap_cfg,
    _scrap_harness,
)
from test_oracle_feed_health import ReconnectFeed, Recorder, _open_only, _service
from test_oracle_log import END, START, _bag, _rows, _sample

# btc-updown-15m-1791039600 (3 Oct 2026, 16:00-16:15 IST): 100 Up scrapped at
# 2-3c with 31s left while the TWAP sat $0.42 above the strike. Up won.
STRIKE = 84828.58
TWAP_UP_WINNING = 84829.0
CID = "cid-scrap"


def _veto(leg, twap, *, strike=STRIKE, age=0.5, thr=5.0, stale=3.0, **kw):
    return scrap_oracle_veto(
        scrap_leg=leg,
        twap_usd=twap,
        strike_usd=strike,
        twap_age_s=age,
        threshold_usd=thr,
        stale_s=stale,
        **kw,
    )


class VetoHelperTests(unittest.TestCase):
    def test_blocks_scrapping_up_while_oracle_favours_up(self):
        block, why, detail = _veto("up", TWAP_UP_WINNING)
        self.assertTrue(block)
        self.assertEqual(why, "oracle_favors_leg")
        self.assertAlmostEqual(detail["margin"], 0.42)
        self.assertEqual(detail["threshold"], 5.0)
        self.assertEqual(detail["strike"], STRIKE)
        self.assertEqual(detail["twap"], TWAP_UP_WINNING)

    def test_blocks_scrapping_down_while_oracle_favours_down(self):
        block, why, _ = _veto("dn", STRIKE - 0.5)
        self.assertTrue(block)
        self.assertEqual(why, "oracle_favors_leg")

    def test_blocks_within_threshold_on_the_losing_side(self):
        block, why, detail = _veto("up", STRIKE - 3.0)
        self.assertTrue(block)
        self.assertEqual(why, "within_threshold")
        self.assertAlmostEqual(detail["margin"], -3.0)
        block, why, _ = _veto("dn", STRIKE + 4.99)
        self.assertTrue(block)
        self.assertEqual(why, "within_threshold")
        block, why, _ = _veto("up", STRIKE)
        self.assertTrue(block)
        self.assertEqual(why, "within_threshold")

    def test_allows_when_oracle_is_more_than_threshold_against(self):
        block, why, detail = _veto("up", STRIKE - 6.0)
        self.assertFalse(block)
        self.assertEqual(why, "clear_against")
        self.assertAlmostEqual(detail["margin"], -6.0)
        block, why, _ = _veto("dn", STRIKE + 6.0)
        self.assertFalse(block)
        self.assertEqual(why, "clear_against")
        # Exactly $5 against is not "within" $5.
        self.assertFalse(_veto("up", STRIKE - 5.0)[0])
        self.assertFalse(_veto("dn", STRIKE + 5.0)[0])

    def test_stale_missing_and_no_strike_fall_back_to_no_veto(self):
        self.assertEqual(_veto("up", TWAP_UP_WINNING, age=3.01)[:2], (False, "stale_twap"))
        self.assertEqual(_veto("up", TWAP_UP_WINNING, age=3.0)[:2], (True, "oracle_favors_leg"))
        self.assertEqual(_veto("up", None)[:2], (False, "missing_twap"))
        self.assertEqual(_veto("up", TWAP_UP_WINNING, strike=None)[:2], (False, "missing_strike"))
        self.assertEqual(_veto("up", TWAP_UP_WINNING, age=None)[:2], (False, "missing_twap_age"))
        self.assertEqual(
            _veto("up", TWAP_UP_WINNING, obs_age_s=10.5)[:2], (False, "stale_obs"),
        )
        self.assertTrue(_veto("up", TWAP_UP_WINNING, obs_age_s=2.4)[0])
        self.assertEqual(_veto("up", "nan")[:2], (False, "missing_twap"))

    def test_disabled_and_bad_leg_never_block(self):
        self.assertEqual(_veto("up", TWAP_UP_WINNING, enabled=False)[:2], (False, "disabled"))
        self.assertEqual(_veto(None, TWAP_UP_WINNING)[:2], (False, "bad_leg"))

    def test_settings_defaults_and_bad_values(self):
        self.assertEqual(scrap_oracle_settings({}), (True, 5.0, 3.0, True))
        self.assertEqual(scrap_oracle_settings(None), (True, 5.0, 3.0, True))
        self.assertEqual(
            scrap_oracle_settings(
                {"scrap_oracle_veto_enabled": False, "scrap_oracle_veto_usd": 7.5,
                 "scrap_oracle_veto_stale_s": 2.0, "scrap_oracle_veto_use_live": False}
            ),
            (False, 7.5, 2.0, False),
        )
        self.assertEqual(
            scrap_oracle_settings(
                {"scrap_oracle_veto_usd": "abc", "scrap_oracle_veto_stale_s": -1}
            ),
            (True, 5.0, 3.0, True),
        )
        self.assertFalse(scrap_oracle_settings({"scrap_oracle_veto_enabled": "false"})[0])
        self.assertTrue(scrap_oracle_settings({"scrap_oracle_veto_enabled": "true"})[0])
        self.assertFalse(scrap_oracle_settings({"scrap_oracle_veto_use_live": "off"})[3])
        self.assertTrue(DEFAULT_SELL_KNOBS["scrap_oracle_veto_enabled"])
        self.assertEqual(DEFAULT_SELL_KNOBS["scrap_oracle_veto_usd"], 5.0)
        self.assertEqual(DEFAULT_SELL_KNOBS["scrap_oracle_veto_stale_s"], 3.0)
        self.assertTrue(DEFAULT_SELL_KNOBS["scrap_oracle_veto_use_live"])


def _veto_live(leg, twap, live, *, age=0.5, live_age=0.5, use_live=True, **kw):
    return _veto(
        leg, twap, age=age, live_usd=live, live_age_s=live_age, use_live=use_live, **kw,
    )


class LiveVetoHelperTests(unittest.TestCase):
    def test_live_favours_side_while_average_is_against_blocks(self):
        block, why, d = _veto_live("up", STRIKE - 9.0, STRIKE + 1.25)
        self.assertTrue(block)
        self.assertEqual(why, "live_favors_leg")
        self.assertAlmostEqual(d["margin"], -9.0)
        self.assertAlmostEqual(d["live_margin"], 1.25)
        self.assertEqual(d["twap_why"], "clear_against")
        self.assertEqual(d["live_why"], "favors")
        self.assertEqual(d["basis"], "twap+live")
        self.assertIsNone(d["fallback"])
        block, why, _ = _veto_live("dn", STRIKE + 9.0, STRIKE - 0.5)
        self.assertEqual((block, why), (True, "live_favors_leg"))

    def test_live_within_threshold_blocks(self):
        block, why, d = _veto_live("up", STRIKE - 9.0, STRIKE - 4.0)
        self.assertEqual((block, why), (True, "live_within_threshold"))
        self.assertAlmostEqual(d["live_margin"], -4.0)

    def test_average_still_blocks_when_live_is_clear(self):
        block, why, _ = _veto_live("up", TWAP_UP_WINNING, STRIKE - 20.0)
        self.assertEqual((block, why), (True, "oracle_favors_leg"))

    def test_both_against_by_more_than_threshold_allows(self):
        block, why, d = _veto_live("up", STRIKE - 6.0, STRIKE - 5.01)
        self.assertEqual((block, why), (False, "clear_against"))
        self.assertEqual(d["basis"], "twap+live")
        block, why, _ = _veto_live("dn", STRIKE + 6.0, STRIKE + 7.0)
        self.assertEqual((block, why), (False, "clear_against"))
        # Exactly $5 against on the live print goes ahead too.
        self.assertFalse(_veto_live("up", STRIKE - 6.0, STRIKE - 5.0)[0])

    def test_live_stale_means_average_alone(self):
        block, why, d = _veto_live("up", STRIKE - 6.0, STRIKE + 3.0, live_age=3.5)
        self.assertEqual((block, why), (False, "clear_against"))
        self.assertEqual(d["basis"], "twap")
        self.assertEqual(d["fallback"], "twap_only")
        self.assertEqual(d["live_why"], "stale_live")
        self.assertIsNone(d["live_margin"])
        block, why, d = _veto_live("up", TWAP_UP_WINNING, STRIKE - 20.0, live_age=None)
        self.assertEqual((block, why), (True, "oracle_favors_leg"))
        self.assertEqual(d["live_why"], "missing_live_age")
        block, _why, d = _veto_live("up", STRIKE - 6.0, None)
        self.assertFalse(block)
        self.assertEqual(d["live_why"], "missing_live")
        _b, _w, d = _veto_live("up", STRIKE - 6.0, STRIKE + 3, live_obs_age_s=10.5)
        self.assertEqual(d["live_why"], "stale_live_obs")
        self.assertFalse(_b)

    def test_average_stale_means_live_alone(self):
        block, why, d = _veto_live("up", STRIKE - 9.0, STRIKE + 1.0, age=4.0)
        self.assertEqual((block, why), (True, "live_favors_leg"))
        self.assertEqual(d["fallback"], "live_only")
        self.assertEqual(d["twap_why"], "stale_twap")
        block, _why, d = _veto_live("up", TWAP_UP_WINNING, STRIKE - 9.0, age=4.0)
        self.assertFalse(block)
        self.assertEqual(d["basis"], "live")

    def test_both_stale_falls_back_to_scrap(self):
        block, why, d = _veto_live("up", TWAP_UP_WINNING, STRIKE + 1, age=4.0, live_age=4.0)
        self.assertEqual((block, why), (False, "stale_twap"))
        self.assertEqual(d["fallback"], "none")
        self.assertEqual(d["live_why"], "stale_live")

    def test_use_live_false_ignores_the_live_print(self):
        block, why, d = _veto_live("up", STRIKE - 9.0, STRIKE + 5.0, use_live=False)
        self.assertEqual((block, why), (False, "clear_against"))
        self.assertIsNone(d["live_price"])
        self.assertEqual(d["live_why"], "live_off")
        self.assertIsNone(d["fallback"])
        block, why, d = _veto_live("up", TWAP_UP_WINNING, STRIKE + 5.0, age=4.0, use_live=False)
        self.assertEqual((block, why, d["fallback"]), (False, "stale_twap", "none"))


_SAME = object()


class _View:
    """Mutable stand-in for ``_oracle_bag_view`` (an in-memory read).

    ``live`` / ``live_age`` default to following the TWAP and its age."""

    def __init__(
        self, clock, *, twap=TWAP_UP_WINNING, strike=STRIKE, age=0.8, lag=2.0,
        live=_SAME, live_age=_SAME,
    ):
        self.clock = clock
        self.twap = twap
        self.strike = strike
        self.age = age
        self.lag = lag
        self.live = live
        self.live_age = live_age
        self.calls = 0
        self.script: list = []

    def __call__(self, _cid):
        self.calls += 1
        if self.script:
            self.twap = self.script.pop(0)
        now = self.clock["now"]
        recv = None if self.age is None else now - self.age
        live = self.twap if self.live is _SAME else self.live
        live_age = self.age if self.live_age is _SAME else self.live_age
        live_recv = None if live_age is None else now - live_age
        return SimpleNamespace(
            twap=None if self.twap is None else str(self.twap),
            open_usd=None if self.strike is None else str(self.strike),
            obs_ts=None if recv is None else recv - self.lag,
            recv_ts=recv,
            open_source="rtds_twap_at_start",
            source="polymarket_rtds",
            live_price=None if live is None else str(live),
            live_obs_ts=None if live_recv is None else live_recv - 1.2,
            live_recv_ts=live_recv,
        )


def _named(events, name):
    return [row for row in events if row["event"] == name]


class SellLoopVetoTests(unittest.TestCase):
    def _setup(self, ttm=31.0, **view_kw):
        ns, events, fak_calls, clock, book = _scrap_harness()
        view = _View(clock, **view_kw)
        ns["_oracle_bag_view"] = view
        end = clock["now"] + ttm
        intent = _open_scrap_bag(end, slug="btc-updown-15m-1791039600")
        return ns, events, fak_calls, clock, intent, view, end

    def _tick(self, ns, cfg, intent):
        state = {"intents": {CID: intent}}
        ns["remember_persisted_state"](state)
        ns["_manage_sells_locked"](cfg, state, object())

    def test_1791039600_replay_never_scraps_the_up_leg(self):
        ns, events, fak_calls, clock, intent, _view, end = self._setup()
        cfg = _scrap_cfg()
        while clock["now"] < end - 1.0:
            self._tick(ns, cfg, intent)
            clock["now"] += 0.5
        self.assertEqual(fak_calls, [])
        self.assertFalse(intent.get("sold_loser"))
        self.assertIsNone(intent.get("sell_loser_armed_at"))
        vetoes = _named(events, "scrap_oracle_veto")
        self.assertGreaterEqual(len(vetoes), 6)
        # Throttled to once per 5s per bag across ~30s of 0.5s ticks.
        self.assertLessEqual(len(vetoes), 7)
        row = vetoes[0]
        self.assertEqual(row["slug"], "btc-updown-15m-1791039600")
        self.assertEqual(row["side"], "up")
        self.assertAlmostEqual(row["bid"], 0.01)
        self.assertAlmostEqual(row["ttm"], 31.0)
        self.assertEqual(row["twap"], TWAP_UP_WINNING)
        self.assertEqual(row["strike"], STRIKE)
        self.assertAlmostEqual(row["margin"], 0.42)
        self.assertEqual(row["threshold"], 5.0)
        self.assertEqual(row["why"], "oracle_favors_leg")
        self.assertFalse(_named(events, "scrap_oracle_stale"))
        self.assertFalse(_named(events, "sell_loser_persist"))

    def test_within_threshold_blocks(self):
        ns, events, fak_calls, clock, intent, _view, _end = self._setup(twap=STRIKE - 4.0)
        cfg = _scrap_cfg()
        for _ in range(20):
            self._tick(ns, cfg, intent)
            clock["now"] += 0.5
        self.assertEqual(fak_calls, [])
        self.assertEqual(_named(events, "scrap_oracle_veto")[0]["why"], "within_threshold")

    def test_clear_against_scraps_with_margin_in_the_events(self):
        ns, events, fak_calls, clock, intent, _view, _end = self._setup(twap=STRIKE - 6.0)
        cfg = _scrap_cfg()
        self._tick(ns, cfg, intent)
        armed = intent.get("sell_loser_armed_at")
        self.assertEqual(armed, clock["now"])
        clock["now"] = armed + 1.9
        self._tick(ns, cfg, intent)
        self.assertEqual(fak_calls, [])
        clock["now"] = armed + 2.0
        self._tick(ns, cfg, intent)
        self.assertEqual(len(fak_calls), 1)
        self.assertEqual(fak_calls[0]["token_id"], "up-tok")
        self.assertTrue(intent.get("sold_loser"))
        self.assertFalse(_named(events, "scrap_oracle_veto"))
        done = _named(events, "sell_loser_done")
        self.assertEqual(len(done), 1)
        self.assertAlmostEqual(done[0]["oracle_margin"], -6.0)
        self.assertEqual(done[0]["oracle_strike"], STRIKE)
        self.assertEqual(done[0]["oracle_why"], "clear_against")
        sweep = _named(events, "sell_scrap_sweep")
        self.assertEqual(len(sweep), 1)
        self.assertAlmostEqual(sweep[0]["oracle_margin"], -6.0)
        self.assertAlmostEqual(intent["sell_scrap_oracle_margin"], -6.0)

    def test_oracle_turning_against_then_scraps_after_full_persist(self):
        ns, events, fak_calls, clock, intent, view, end = self._setup(ttm=200.0)
        cfg = _scrap_cfg()
        for _ in range(10):
            self._tick(ns, cfg, intent)
            clock["now"] += 1.0
        self.assertEqual(fak_calls, [])
        self.assertIsNone(intent.get("sell_loser_armed_at"))
        view.twap = STRIKE - 5.5
        self._tick(ns, cfg, intent)
        armed = intent["sell_loser_armed_at"]
        self.assertEqual(armed, clock["now"])
        clock["now"] = armed + 4.9
        self._tick(ns, cfg, intent)
        self.assertEqual(fak_calls, [])
        # Swinging back inside $5 mid-persist resets the arm.
        view.twap = STRIKE - 1.0
        clock["now"] = armed + 5.0
        self._tick(ns, cfg, intent)
        self.assertEqual(fak_calls, [])
        self.assertIsNone(intent.get("sell_loser_armed_at"))
        view.twap = STRIKE - 8.0
        self._tick(ns, cfg, intent)
        rearmed = intent["sell_loser_armed_at"]
        clock["now"] = rearmed + 5.0
        self._tick(ns, cfg, intent)
        self.assertEqual(len(fak_calls), 1)
        self.assertTrue(intent.get("sold_loser"))
        self.assertLess(end - clock["now"], 200.0)

    def test_fire_time_recheck_blocks_the_post(self):
        ns, events, fak_calls, clock, intent, view, _end = self._setup(twap=STRIKE - 6.0)
        cfg = _scrap_cfg()
        self._tick(ns, cfg, intent)
        armed = intent["sell_loser_armed_at"]
        clock["now"] = armed + 2.0
        # Arm-phase read is clear; the read right before the FAK favours Up.
        view.script = [STRIKE - 6.0, TWAP_UP_WINNING]
        self._tick(ns, cfg, intent)
        self.assertEqual(fak_calls, [])
        self.assertFalse(intent.get("sold_loser"))
        self.assertIsNone(intent.get("sell_loser_armed_at"))
        vetoes = _named(events, "scrap_oracle_veto")
        self.assertEqual(len(vetoes), 1)
        self.assertEqual(vetoes[0]["phase"], "fire")

    def test_stale_oracle_falls_back_to_scrap_and_logs_loudly(self):
        ns, events, fak_calls, clock, intent, _view, _end = self._setup(age=4.0)
        cfg = _scrap_cfg()
        self._tick(ns, cfg, intent)
        armed = intent["sell_loser_armed_at"]
        self.assertEqual(armed, clock["now"])
        clock["now"] = armed + 2.0
        self._tick(ns, cfg, intent)
        self.assertEqual(len(fak_calls), 1)
        self.assertTrue(intent.get("sold_loser"))
        self.assertFalse(_named(events, "scrap_oracle_veto"))
        stale = _named(events, "scrap_oracle_stale")
        self.assertGreaterEqual(len(stale), 1)
        self.assertEqual(stale[0]["reason"], "stale_twap")
        self.assertAlmostEqual(stale[0]["age_s"], 4.0)
        self.assertEqual(stale[0]["level"], "WARNING")
        self.assertEqual(stale[0]["side"], "up")
        done = _named(events, "sell_loser_done")
        self.assertEqual(done[0]["oracle_why"], "stale_twap")
        self.assertIsNone(done[0]["oracle_margin"])

    def test_missing_strike_or_feed_falls_back(self):
        for kw, reason in (
            ({"strike": None}, "missing_strike"),
            ({"twap": None}, "missing_twap"),
            ({"age": None}, "missing_twap_age"),
        ):
            with self.subTest(reason=reason):
                ns, events, fak_calls, clock, intent, _view, _end = self._setup(**kw)
                cfg = _scrap_cfg()
                self._tick(ns, cfg, intent)
                clock["now"] += 2.0
                self._tick(ns, cfg, intent)
                self.assertEqual(len(fak_calls), 1)
                self.assertEqual(_named(events, "scrap_oracle_stale")[0]["reason"], reason)

    def test_hot_reload_disable_and_threshold(self):
        ns, events, fak_calls, clock, intent, view, _end = self._setup(twap=STRIKE - 3.0)
        cfg = _scrap_cfg()
        self._tick(ns, cfg, intent)
        self.assertIsNone(intent.get("sell_loser_armed_at"))
        cfg["scrap_oracle_veto_usd"] = 2.0
        clock["now"] += 0.5
        self._tick(ns, cfg, intent)
        armed = intent["sell_loser_armed_at"]
        self.assertEqual(armed, clock["now"])
        view.twap = TWAP_UP_WINNING
        cfg["scrap_oracle_veto_enabled"] = False
        clock["now"] = armed + 2.0
        self._tick(ns, cfg, intent)
        self.assertEqual(len(fak_calls), 1)

    def test_blocked_scrap_pulls_a_resting_scrap_sell(self):
        ns, events, fak_calls, clock, intent, _view, _end = self._setup()
        intent.update(
            sell_scrap_rest_id="dry-rest-1",
            sell_scrap_rest_size=50.0,
            sell_scrap_rest_px=0.02,
            sell_loser_leg="up",
            sell_loser_armed_at=clock["now"] - 10.0,
        )
        self._tick(ns, _scrap_cfg(), intent)
        self.assertFalse(intent.get("sell_scrap_rest_id"))
        cancels = _named(events, "sell_scrap_rest_cancel")
        self.assertEqual(len(cancels), 1)
        self.assertEqual(fak_calls, [])

    def test_blind_fak_is_vetoed(self):
        ns, events, fak_calls, clock, intent, view, _end = self._setup(twap=STRIKE - 6.0)
        cfg = _scrap_cfg()
        intent.update(
            sell_loser_leg="up",
            sell_loser_armed_at=clock["now"] - 10.0,
            sell_last_status="empty",
        )
        # Up book vanished: the blind 1c FAK path would fire.
        book_empty = {"up": (None, 0.0, []), "dn": (0.98, 80.0, [{"price": "0.98", "size": "80"}])}
        ns["_fetch_books"] = lambda *_a: (book_empty["up"], book_empty["dn"])
        ns["_sell_inventory"] = lambda *a: (float(a[4]), "has_inventory")
        view.script = [STRIKE - 6.0, TWAP_UP_WINNING]
        self._tick(ns, cfg, intent)
        self.assertEqual(fak_calls, [])
        vetoes = _named(events, "scrap_oracle_veto")
        self.assertEqual([row["phase"] for row in vetoes], ["blind"])
        self.assertFalse(_named(events, "sell_scrap_blind"))
        # Clear on both reads: the blind FAK goes out with the margin.
        clock["now"] += 5.0
        intent["sell_loser_armed_at"] = clock["now"] - 10.0
        view.twap = STRIKE - 6.0
        self._tick(ns, cfg, intent)
        blind = _named(events, "sell_scrap_blind")
        self.assertEqual(len(blind), 1)
        self.assertAlmostEqual(blind[0]["oracle_margin"], -6.0)
        self.assertEqual(len(fak_calls), 1)
        self.assertAlmostEqual(fak_calls[0]["price"], 0.01)

    def test_no_check_and_no_log_without_a_scrap_candidate(self):
        ns, events, fak_calls, clock, intent, view, _end = self._setup()
        ns["_fetch_books"] = lambda *_a: (
            (0.50, 80.0, [{"price": "0.50", "size": "80"}]),
            (0.49, 80.0, [{"price": "0.49", "size": "80"}]),
        )
        self._tick(ns, _scrap_cfg(), intent)
        self.assertEqual(view.calls, 0)
        self.assertFalse(_named(events, "scrap_oracle_veto"))
        self.assertFalse(_named(events, "scrap_oracle_stale"))

    def test_live_price_favouring_the_side_blocks_while_average_is_against(self):
        ns, events, fak_calls, clock, intent, _view, _end = self._setup(
            twap=STRIKE - 9.0, live=STRIKE + 0.8,
        )
        cfg = _scrap_cfg()
        for _ in range(12):
            self._tick(ns, cfg, intent)
            clock["now"] += 0.5
        self.assertEqual(fak_calls, [])
        self.assertIsNone(intent.get("sell_loser_armed_at"))
        row = _named(events, "scrap_oracle_veto")[0]
        self.assertEqual(row["why"], "live_favors_leg")
        self.assertAlmostEqual(row["margin"], -9.0)
        self.assertAlmostEqual(row["live_price"], STRIKE + 0.8)
        self.assertAlmostEqual(row["live_margin"], 0.8)
        self.assertEqual(row["basis"], "twap+live")
        self.assertFalse(_named(events, "scrap_oracle_stale"))

    def test_both_against_by_more_than_5_scraps_with_live_fields(self):
        ns, events, fak_calls, clock, intent, _view, _end = self._setup(
            twap=STRIKE - 6.0, live=STRIKE - 7.5,
        )
        cfg = _scrap_cfg()
        self._tick(ns, cfg, intent)
        armed = intent["sell_loser_armed_at"]
        clock["now"] = armed + 2.0
        self._tick(ns, cfg, intent)
        self.assertEqual(len(fak_calls), 1)
        for name in ("sell_scrap_sweep", "sell_loser_done"):
            row = _named(events, name)[0]
            self.assertAlmostEqual(row["oracle_margin"], -6.0)
            self.assertAlmostEqual(row["oracle_live_price"], STRIKE - 7.5)
            self.assertAlmostEqual(row["oracle_live_margin"], -7.5)
            self.assertEqual(row["oracle_basis"], "twap+live")
        self.assertAlmostEqual(intent["sell_scrap_oracle_live_margin"], -7.5)

    def test_live_turning_at_fire_time_blocks_the_post(self):
        ns, events, fak_calls, clock, intent, view, _end = self._setup(
            twap=STRIKE - 9.0, live=STRIKE - 9.0,
        )
        cfg = _scrap_cfg()
        self._tick(ns, cfg, intent)
        armed = intent["sell_loser_armed_at"]
        clock["now"] = armed + 2.0
        calls = {"n": 0}
        base = view.__call__

        def flip(cid):
            calls["n"] += 1
            if calls["n"] == 2:
                view.live = STRIKE + 0.3
            return base(cid)

        ns["_oracle_bag_view"] = flip
        self._tick(ns, cfg, intent)
        self.assertEqual(fak_calls, [])
        vetoes = _named(events, "scrap_oracle_veto")
        self.assertEqual([(r["phase"], r["why"]) for r in vetoes], [("fire", "live_favors_leg")])

    def test_live_stale_uses_the_average_alone_and_logs(self):
        ns, events, fak_calls, clock, intent, _view, _end = self._setup(
            twap=STRIKE - 6.0, live=STRIKE + 2.0, live_age=4.0,
        )
        cfg = _scrap_cfg()
        self._tick(ns, cfg, intent)
        armed = intent["sell_loser_armed_at"]
        clock["now"] = armed + 2.0
        self._tick(ns, cfg, intent)
        self.assertEqual(len(fak_calls), 1)
        stale = _named(events, "scrap_oracle_stale")
        self.assertEqual(stale[0]["reason"], "stale_live")
        self.assertEqual(stale[0]["fallback"], "twap_only")
        self.assertAlmostEqual(stale[0]["live_age_s"], 4.0)
        self.assertEqual(stale[0]["level"], "WARNING")
        done = _named(events, "sell_loser_done")[0]
        self.assertEqual(done["oracle_basis"], "twap")
        self.assertIsNone(done["oracle_live_margin"])

    def test_live_stale_average_favouring_still_blocks(self):
        ns, events, fak_calls, clock, intent, _view, _end = self._setup(
            live=STRIKE - 20.0, live_age=None,
        )
        for _ in range(6):
            self._tick(ns, _scrap_cfg(), intent)
            clock["now"] += 0.5
        self.assertEqual(fak_calls, [])
        self.assertEqual(_named(events, "scrap_oracle_veto")[0]["why"], "oracle_favors_leg")
        self.assertEqual(_named(events, "scrap_oracle_stale")[0]["reason"], "missing_live_age")

    def test_both_stale_scraps_as_today(self):
        ns, events, fak_calls, clock, intent, _view, _end = self._setup(
            live=STRIKE + 2.0, age=4.0, live_age=5.0,
        )
        cfg = _scrap_cfg()
        self._tick(ns, cfg, intent)
        clock["now"] += 2.0
        self._tick(ns, cfg, intent)
        self.assertEqual(len(fak_calls), 1)
        stale = _named(events, "scrap_oracle_stale")[0]
        self.assertEqual((stale["reason"], stale["fallback"]), ("stale_twap", "none"))
        self.assertAlmostEqual(stale["live_age_s"], 5.0)

    def test_use_live_hot_reload_off_ignores_live(self):
        ns, events, fak_calls, clock, intent, _view, _end = self._setup(
            twap=STRIKE - 9.0, live=STRIKE + 1.0,
        )
        cfg = _scrap_cfg()
        self._tick(ns, cfg, intent)
        self.assertIsNone(intent.get("sell_loser_armed_at"))
        cfg["scrap_oracle_veto_use_live"] = False
        clock["now"] += 0.5
        self._tick(ns, cfg, intent)
        armed = intent["sell_loser_armed_at"]
        clock["now"] = armed + 2.0
        self._tick(ns, cfg, intent)
        self.assertEqual(len(fak_calls), 1)

    def test_scrap_time_gate_still_applies_first(self):
        ns, events, fak_calls, clock, intent, view, _end = self._setup(ttm=700.0, twap=STRIKE - 9.0)
        self._tick(ns, _scrap_cfg(), intent)
        self.assertEqual(view.calls, 0)
        self.assertEqual(len(_named(events, "sell_scrap_time_gated")), 1)
        self.assertIsNone(intent.get("sell_loser_armed_at"))


class DumpUntouchedTests(unittest.TestCase):
    def test_held_dump_ignores_the_scrap_veto(self):
        ns, events, fak_calls, clock = _dump_harness(_fill_fak)
        view = _View(clock, twap=STRIKE + 50.0)
        ns["_oracle_bag_view"] = view
        end = clock["now"] + 200.0
        intent = _held_after_scrap(end, sell_dump_armed_at=clock["now"] - 5.0)
        state = {"intents": {"cid-dump": intent}}
        ns["remember_persisted_state"](state)
        ns["_manage_sells_locked"](_dump_cfg(sell_dump_below=0.60), state, object())
        self.assertTrue(fak_calls)
        self.assertEqual(fak_calls[0]["token_id"], "up-tok")
        self.assertFalse(_named(events, "scrap_oracle_veto"))
        self.assertFalse(_named(events, "scrap_oracle_stale"))

    def test_veto_is_not_in_dump_or_mint_code(self):
        src = MINT.read_text()

        def body(name):
            start = src.find(f"\ndef {name}(")
            self.assertGreater(start, 0, name)
            end = src.find("\ndef ", start + 5)
            return src[start:end]

        for name in (
            "_run_dump_fak_with_refire",
            "_sell_kept_after_dump",
            "run_mint_cycle",
            "submit_mint_batch",
        ):
            self.assertNotIn("_scrap_oracle_gate", body(name), name)
            self.assertNotIn("scrap_oracle_veto", body(name), name)
        gate = body("_scrap_oracle_gate")
        for banned in ("requests", "session", "fetch", "await", "sleep", "_io_unlocked"):
            self.assertNotIn(banned, gate, banned)
        manage = body("_manage_sells_locked")
        dump_at = manage.find("_run_dump_fak_with_refire")
        veto_at = manage.find("_scrap_oracle_gate")
        self.assertGreater(dump_at, 0)
        self.assertGreater(veto_at, dump_at)


class FeedFreshnessTests(unittest.TestCase):
    def _frame(self, obs_ms: int, value: str = "84829.0") -> str:
        return json.dumps(
            {
                "topic": "crypto_prices_twap_sixty",
                "type": "update",
                "timestamp": obs_ms + 2000,
                "payload": {
                    "symbol": "btc/usd",
                    "window_s": 60,
                    "timestamp": obs_ms,
                    "value": value,
                },
            }
        )

    def test_samples_carry_local_receive_time(self):
        clock = {"now": 1_791_040_469.0}
        feed = RtdsTwapFeed(clock=lambda: clock["now"])
        feed.handle_message(self._frame(1_791_040_467_000))
        latest = feed.latest()
        self.assertEqual(latest.recv_ts, clock["now"])
        self.assertEqual(latest.obs_ts, 1_791_040_467.0)
        self.assertEqual(feed.drain()[0].recv_ts, clock["now"])
        # Same Chainlink second re-sent later keeps the first arrival time.
        clock["now"] += 1.5
        feed.handle_message(self._frame(1_791_040_467_000, "84829.5"))
        self.assertEqual(feed.latest().recv_ts, 1_791_040_469.0)
        self.assertEqual(feed.latest().twap, "84829.5")
        clock["now"] += 0.4
        feed.handle_message(self._frame(1_791_040_468_000))
        self.assertEqual(feed.latest().recv_ts, clock["now"])
        # An older replayed second does not move latest back.
        feed.handle_message(self._frame(1_791_040_400_000))
        self.assertEqual(feed.latest().obs_ts, 1_791_040_468.0)
        # recv_ts does not change sample equality (tape dedupe keys).
        self.assertEqual(
            TwapSample("btc/usd", 60, "1", 5.0, recv_ts=1.0),
            TwapSample("btc/usd", 60, "1", 5.0, recv_ts=2.0),
        )

    def test_bag_view_exposes_recv_ts_and_strike(self):
        clock = {"now": START + 869.0}
        feed = RtdsTwapFeed(clock=lambda: clock["now"])
        service = OracleLogService("/dev/null", feed=feed)
        window = OracleWindow(condition_id="cid-15m", slug="s", start_ts=START, end_ts=END)
        service._memory["cid-15m"] = _WindowMemory(
            window=window, open_ref=str(STRIKE), open_source="rtds_twap_at_start",
        )
        feed.handle_message(self._frame(int((START + 867.0) * 1000)))
        view = service.bag_view("cid-15m")
        self.assertEqual(view.recv_ts, START + 869.0)
        self.assertEqual(view.obs_ts, START + 867.0)
        self.assertEqual(view.open_usd, str(STRIKE))
        self.assertEqual(float(view.twap), 84829.0)

    @staticmethod
    def _live_frame(obs_ms: int, value: str = "84841164260323815000000", topic=RTDS_LIVE_TOPIC):
        # Shape captured from wss://ws-live-data.polymarket.com on 3 Oct 2026.
        return json.dumps(
            {
                "topic": topic,
                "type": "update",
                "timestamp": obs_ms + 1049,
                "payload": {
                    "full_accuracy_value": value,
                    "symbol": "btc/usd",
                    "timestamp": obs_ms,
                    "value": 84841.16426032381,
                },
            }
        )

    def test_live_chainlink_print_is_held_in_memory(self):
        clock = {"now": 1_791_042_659.05}
        feed = RtdsTwapFeed(clock=lambda: clock["now"])
        feed.handle_message(self._live_frame(1_791_042_658_000))
        live = feed.latest_live()
        self.assertEqual(live.price, "84841.164260323815")
        self.assertEqual(live.obs_ts, 1_791_042_658.0)
        self.assertEqual(live.recv_ts, clock["now"])
        # Live prints are not TWAP samples: no tape backlog, no TWAP change.
        self.assertEqual(feed.drain(), [])
        self.assertIsNone(feed.latest())
        self.assertEqual(feed.last_error(), "")
        clock["now"] += 0.9
        feed.handle_message(self._live_frame(1_791_042_658_000, "84841000000000000000000"))
        self.assertEqual(feed.latest_live().recv_ts, 1_791_042_659.05)
        feed.handle_message(self._live_frame(1_791_042_659_000))
        self.assertEqual(feed.latest_live().recv_ts, clock["now"])
        feed.handle_message(self._live_frame(1_791_042_600_000))
        self.assertEqual(feed.latest_live().obs_ts, 1_791_042_659.0)

    def test_live_parser_ignores_snapshot_and_other_symbols(self):
        self.assertIsNone(parse_rtds_live(self._live_frame(1, topic="crypto_prices")))
        eth = json.loads(self._live_frame(1_791_042_658_000))
        eth["payload"]["symbol"] = "eth/usd"
        self.assertIsNone(parse_rtds_live(json.dumps(eth)))
        self.assertIsNone(parse_rtds_live("PONG"))
        self.assertIsNone(parse_rtds_live(self._frame(1_791_040_467_000)))
        topics = [sub["topic"] for sub in SUBSCRIBE_FRAME["subscriptions"]]
        self.assertEqual(topics, ["crypto_prices_twap_sixty", RTDS_LIVE_TOPIC])

    def test_bag_view_and_tape_carry_the_live_print(self):
        clock = {"now": START + 869.0}
        feed = RtdsTwapFeed(clock=lambda: clock["now"])
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "oracle_twap.jsonl"
            service = OracleLogService(path, feed=feed, fetch_price=_open_only, jitter=lambda: 0.0)
            window = OracleWindow(condition_id="cid-15m", slug="s", start_ts=START, end_ts=END)
            service._memory["cid-15m"] = _WindowMemory(window=window, open_ref=str(STRIKE))
            feed.handle_message(self._frame(int((START + 867.0) * 1000)))
            clock["now"] += 0.2
            feed.handle_message(self._live_frame(int((START + 868.0) * 1000)))
            view = service.bag_view("cid-15m")
            self.assertEqual(view.live_price, "84841.164260323815")
            self.assertEqual(view.live_obs_ts, START + 868.0)
            self.assertEqual(view.live_recv_ts, START + 869.2)
            self.assertEqual(view.recv_ts, START + 869.0)
            service.tick(_bag(), START + 869.5, enabled=True)
            rows = [r for r in _rows(path) if r["event"] == "oracle_twap"]
            self.assertTrue(rows)
            self.assertEqual(rows[-1]["live_price"], "84841.164260323815")
            self.assertEqual(rows[-1]["live_ts"], START + 868.0)

    def test_hot_span_watchdog_reconnects_after_5s_silence(self):
        feed = ReconnectFeed()
        feed.set_hot = lambda hot: setattr(feed, "hot", hot)
        t0 = END - FEED_HOT_TTM_S + 10.0
        feed.latest_sample = _sample(t0)
        feed.samples = [feed.latest_sample]
        service, _path = _service(feed, _open_only)
        rec = Recorder()
        service.tick(_bag(), t0, enabled=True, **rec.kwargs())
        self.assertTrue(feed.hot)
        fired = []
        for dt in range(1, 41):
            before = len(feed.reconnects)
            service.tick(_bag(), t0 + dt, enabled=True, **rec.kwargs())
            if len(feed.reconnects) > before:
                fired.append(dt)
        self.assertEqual(fired[:3], [int(FEED_SILENT_HOT_S), 15, 25])
        self.assertTrue(feed.reconnects[0].startswith("watchdog: silent 5s"))
        self.assertTrue(rec.named("oracle_feed_watchdog")[0]["hot"])

    def test_cold_watchdog_keeps_45s(self):
        feed = ReconnectFeed()
        feed.set_hot = lambda hot: setattr(feed, "hot", hot)
        t0 = START + 100.0
        feed.latest_sample = _sample(t0)
        feed.samples = [feed.latest_sample]
        service, _path = _service(feed, _open_only)
        service.tick(_bag(), t0, enabled=True)
        self.assertFalse(feed.hot)
        fired = []
        for dt in range(1, 60):
            before = len(feed.reconnects)
            service.tick(_bag(), t0 + dt, enabled=True)
            if len(feed.reconnects) > before:
                fired.append(dt)
        self.assertEqual(fired, [int(FEED_SILENT_RECONNECT_S)])

    def test_steady_hot_feed_is_never_reconnected(self):
        feed = ReconnectFeed()
        t0 = END - FEED_HOT_TTM_S
        service, _path = _service(feed, _open_only)
        for dt in range(0, int(FEED_HOT_TTM_S)):
            feed.latest_sample = _sample(t0 + dt - 2)
            feed.samples = [feed.latest_sample]
            service.tick(_bag(), t0 + dt, enabled=True)
        self.assertEqual(feed.reconnects, [])

    def test_hot_redial_backoff_is_capped(self):
        feed = RtdsTwapFeed()
        waits = []
        backoff = 1.0
        for _ in range(6):
            wait, backoff = feed._after_disconnect(backoff)
            waits.append(wait)
        self.assertEqual(waits, [1.0, 2.0, 4.0, 8.0, 16.0, FEED_BACKOFF_MAX_S])
        feed.set_hot(True)
        waits = []
        backoff = 1.0
        for _ in range(6):
            wait, backoff = feed._after_disconnect(backoff)
            waits.append(wait)
        self.assertTrue(all(w <= FEED_HOT_BACKOFF_S for w in waits), waits)

    def test_gamma_audit_waits_while_a_bag_is_hot(self):
        calls = []

        def gamma(slug):
            calls.append(slug)
            return GammaStrike(price_to_beat=None, final_price=None)

        feed = ReconnectFeed()
        service, _path = _service(feed, _open_only, fetch_gamma=gamma)
        prev = OracleWindow(
            condition_id="cid-prev", slug="prev", start_ts=START - 1800.0, end_ts=START - 900.0,
        )
        service._memory["cid-prev"] = _WindowMemory(window=prev, open_ref="1")
        hot_now = END - 100.0
        self.assertGreaterEqual(hot_now, prev.end_ts + GAMMA_FIRST_S)
        for dt in range(0, 20):
            feed.latest_sample = _sample(hot_now + dt)
            feed.samples = [feed.latest_sample]
            service.tick(_bag(), hot_now + dt, enabled=True)
        self.assertEqual(calls, [])
        service.tick({"intents": {}}, END + 400.0, enabled=True)
        self.assertEqual(len(calls), 1)


class LatencyTests(unittest.TestCase):
    def test_gate_read_is_well_under_a_millisecond(self):
        clock = {"now": START + 869.0}
        feed = RtdsTwapFeed(clock=lambda: clock["now"])
        service = OracleLogService("/dev/null", feed=feed)
        window = OracleWindow(condition_id="cid-15m", slug="s", start_ts=START, end_ts=END)
        service._memory["cid-15m"] = _WindowMemory(window=window, open_ref=str(STRIKE))
        feed.handle_message(
            json.dumps(
                {
                    "topic": "crypto_prices_twap_sixty",
                    "payload": {
                        "symbol": "btc/usd",
                        "window_s": 60,
                        "timestamp": int((START + 867.0) * 1000),
                        "value": "84829.0",
                    },
                }
            )
        )

        feed.handle_message(FeedFreshnessTests._live_frame(int((START + 868.0) * 1000)))

        def check():
            view = service.bag_view("cid-15m")
            now = clock["now"]
            return scrap_oracle_veto(
                scrap_leg="up",
                twap_usd=view.twap,
                strike_usd=view.open_usd,
                twap_age_s=max(0.0, now - view.recv_ts),
                threshold_usd=5.0,
                stale_s=3.0,
                obs_age_s=max(0.0, now - view.obs_ts),
                live_usd=view.live_price,
                live_age_s=max(0.0, now - view.live_recv_ts),
                live_obs_age_s=max(0.0, now - view.live_obs_ts),
                use_live=True,
            )

        self.assertTrue(check()[0])
        per_call = min(timeit.repeat(check, number=2000, repeat=3)) / 2000
        self.assertLess(per_call, 1e-3)


if __name__ == "__main__":
    unittest.main()
