"""Entry gates, the $20 cap, exposure, the Dublin loss stop, staleness."""

from __future__ import annotations

import unittest
from datetime import datetime, timezone

from buy.lock_config import apply_defaults
from buy.lock_gates import (
    day_pnl,
    dublin_day,
    evaluate_entry,
    evaluate_strategy1,
    evaluate_strategy2,
    execute_buy,
    is_stale,
    loss_stop_active,
    note_fill,
    settle_pnl,
    taker_edge,
)


def _view(**over):
    base = {
        "now": 1_000.0,
        "end_ts": 1_030.0,
        "market_enabled": True,
        "resolution_ok": True,
        "resolution_source": "https://data.chain.link/streams/btc-usd-twap-60s-streams",
        "strike": 100.0,
        "strike_reason": "match",
        "coverage_ok": True,
        "vol_samples": 120,
        "key": "btc_15m",
        "expected": 110.0,
        "p_up": 0.90,
        "live_recv_ts": 999.5,
        "twap_recv_ts": 999.5,
        "live": 110.0,
        "twap": 105.0,
        "sigma": 1.2,
        "slug": "btc-updown-15m-100",
        "asset": "btc",
        "duration": "15m",
        "lane": "btc_15m",
        "up_token": "UP",
        "dn_token": "DN",
        "up": {"asks": [(0.80, 200.0)], "bids": [(0.79, 40.0)], "recv_ts": 999.5},
        "down": {"asks": [(0.22, 200.0)], "bids": [(0.20, 40.0)], "recv_ts": 999.5},
    }
    base.update(over)
    return base


def _account(**over):
    base = {"spent": 0.0, "entries": 0, "open_cost": 0.0, "cash": 500.0, "loss_stopped": False}
    base.update(over)
    return base


def _s2_view(**over):
    base = _view(
        key="btc_5m",
        end_ts=1_200.0,
        binance_recv_ts=999.5,
        binance_move=2.5,
        sigma1s=1.0,
        sigma_source="pre_window",
    )
    base.update(over)
    return base


class GateTests(unittest.TestCase):
    def setUp(self):
        self.cfg = apply_defaults({})

    def test_edge_formula_includes_the_taker_fee(self):
        edge = taker_edge(0.90, 0.80, self.cfg)
        self.assertAlmostEqual(edge, 0.90 - 0.80 - 0.07 * 0.80 * 0.20, places=9)

    def test_qualifying_ask_buys_one_clip(self):
        decision = evaluate_entry(_view(), _account(), self.cfg)
        self.assertEqual(decision["action"], "buy")
        self.assertEqual(decision["side"], "up")
        self.assertEqual(decision["strategy"], "s1")
        self.assertAlmostEqual(decision["notional"], 5.0)
        self.assertAlmostEqual(decision["limit"], 0.80)
        self.assertGreater(decision["edge"], 0.0)
        self.assertGreaterEqual(decision["z_side"], 0.0)

    def test_btc_15m_buys_the_mid_price_bucket(self):
        decision = evaluate_strategy1(
            _view(up={"asks": [(0.55, 100.0)], "bids": [], "recv_ts": 999.5}),
            _account(),
            self.cfg,
        )
        self.assertEqual(decision["action"], "buy")
        self.assertAlmostEqual(decision["ask"], 0.55)

    def test_btc_5m_requires_z_and_a_tighter_pmax(self):
        view = _view(key="btc_5m", expected=100.05)
        small = evaluate_strategy1(view, _account(), self.cfg)
        self.assertEqual(small["reason"], "z_below")
        rich = evaluate_strategy1(
            _view(
                key="btc_5m",
                up={"asks": [(0.92, 100.0)], "bids": [], "recv_ts": 999.5},
            ),
            _account(),
            self.cfg,
        )
        self.assertEqual(rich["reason"], "ask_above")
        ok = evaluate_strategy1(
            _view(
                key="btc_5m",
                up={"asks": [(0.80, 100.0)], "bids": [], "recv_ts": 999.5},
            ),
            _account(),
            self.cfg,
        )
        self.assertEqual(ok["action"], "buy")
        self.assertGreaterEqual(ok["z_side"], 0.25)

    def test_stale_live_and_book_skip(self):
        stale_live = evaluate_entry(_view(live_recv_ts=990.0), _account(), self.cfg)
        self.assertEqual(stale_live["reason"], "stale_live")
        self.assertTrue(is_stale(990.0, 1_000.0, 2.0))
        self.assertFalse(is_stale(998.0, 1_000.0, 2.0))
        book = _view()
        book["up"] = {"asks": [(0.80, 50.0)], "bids": [], "recv_ts": 990.0}
        book["down"] = {"asks": [(0.22, 50.0)], "bids": [], "recv_ts": 990.0}
        stale_book = evaluate_entry(book, _account(), self.cfg)
        self.assertEqual(stale_book["reason"], "stale_book")

    def test_unknown_or_mismatched_strike_skips(self):
        unknown = evaluate_entry(_view(strike=None, strike_reason="strike_unknown"), _account(), self.cfg)
        self.assertEqual(unknown["reason"], "strike_unknown")
        mismatch = evaluate_entry(_view(strike=None, strike_reason="strike_mismatch"), _account(), self.cfg)
        self.assertEqual(mismatch["reason"], "strike_mismatch")

    def test_price_caps(self):
        rich = evaluate_entry(
            _view(key="btc_5m", up={"asks": [(0.92, 100.0)], "bids": [], "recv_ts": 999.5}),
            _account(),
            self.cfg,
        )
        self.assertEqual(rich["reason"], "ask_above")
        insane = evaluate_entry(
            _view(up={"asks": [(0.98, 100.0)], "bids": [], "recv_ts": 999.5}),
            _account(),
            self.cfg,
        )
        self.assertEqual(insane["reason"], "max_pay")
        thin = evaluate_entry(
            _view(up={"asks": [(0.01, 100.0)], "bids": [], "recv_ts": 999.5}),
            _account(),
            self.cfg,
        )
        self.assertEqual(thin["reason"], "ask_below")

    def test_edge_below_min_skips(self):
        decision = evaluate_entry(
            _view(expected=100.0),
            _account(),
            self.cfg,
        )
        self.assertEqual(decision["reason"], "edge_below")

    def test_ladder_clips_five_until_twenty(self):
        account = _account()
        for _ in range(4):
            decision = evaluate_entry(_view(), account, self.cfg)
            self.assertEqual(decision["action"], "buy")
            self.assertAlmostEqual(decision["notional"], 5.0)
            account = note_fill(account, cost=5.0, shares=6.0, strategy="s1", side="up", now=1.0)
        fresh = {"asks": [(0.80, 200.0)], "bids": [(0.79, 40.0)], "recv_ts": 1_001.5}
        blocked = evaluate_entry(
            _view(
                now=1_002.0,
                end_ts=1_032.0,
                live_recv_ts=1_001.5,
                twap_recv_ts=1_001.5,
                up=fresh,
                down=fresh,
            ),
            account,
            self.cfg,
        )
        self.assertEqual(blocked["reason"], "strategy_cap")
        self.assertAlmostEqual(account["spent_s1"], 20.0)

    def test_partial_fill_leaves_room_for_another_clip(self):
        account = note_fill(_account(), cost=17.5, shares=20.0, strategy="s1", side="up", now=900.0)
        decision = evaluate_entry(_view(), account, self.cfg)
        self.assertEqual(decision["action"], "buy")
        self.assertAlmostEqual(decision["notional"], 2.5)
        self.assertLessEqual(17.5 + decision["notional"], 20.0 + 1e-9)

    def test_first_fill_locks_the_side(self):
        account = note_fill(_account(), cost=5.0, shares=6.0, strategy="s1", side="up", now=900.0)
        flipped = evaluate_entry(_view(expected=90.0), account, self.cfg)
        self.assertEqual(flipped["side"], "down")
        self.assertEqual(flipped["reason"], "side_locked")
        same = evaluate_entry(_view(), account, self.cfg)
        self.assertEqual(same["action"], "buy")
        self.assertEqual(same["side"], "up")
        unlocked = note_fill(_account(), cost=5.0, shares=0.0, strategy="s1", side="up")
        self.assertIsNone(unlocked.get("locked_side"))

    def test_combined_cap_blocks_the_other_strategy(self):
        account = note_fill(_account(), cost=20.0, shares=25.0, strategy="s1", side="up", now=900.0)
        decision = evaluate_strategy2(_s2_view(), account, self.cfg)
        self.assertEqual(decision["reason"], "combined_cap")
        roomy = note_fill(_account(), cost=10.0, shares=12.0, strategy="s1", side="up", now=900.0)
        second = evaluate_strategy2(_s2_view(), roomy, self.cfg)
        self.assertEqual(second["action"], "buy")
        self.assertAlmostEqual(second["notional"], 5.0)

    def test_exposure_cap_clips_the_order(self):
        decision = evaluate_entry(_view(), _account(open_cost=56.0), self.cfg)
        self.assertEqual(decision["action"], "buy")
        self.assertAlmostEqual(decision["notional"], 4.0)

    def test_exposure_block_when_the_room_cannot_pay_the_minimum(self):
        decision = evaluate_entry(_view(), _account(open_cost=59.5), self.cfg)
        self.assertEqual(decision["reason"], "exposure")

    def test_cash_buffer_blocks(self):
        decision = evaluate_entry(_view(), _account(cash=5.4), self.cfg)
        self.assertEqual(decision["reason"], "cash")

    def test_kill_switch_stops_a_qualifying_entry(self):
        decision = evaluate_entry(_view(), _account(), apply_defaults({"enabled": False}))
        self.assertEqual(decision["action"], "skip")
        self.assertEqual(decision["reason"], "disabled")
        self.assertGreater(decision["edge"], 0.03)

    def test_daily_stop_latches_for_the_dublin_day(self):
        morning = datetime(2026, 10, 4, 12, 0, tzinfo=timezone.utc).timestamp()
        after_midnight = datetime(2026, 10, 4, 23, 30, tzinfo=timezone.utc).timestamp()
        self.assertEqual(dublin_day(morning), "2026-10-04")
        self.assertEqual(dublin_day(after_midnight), "2026-10-05")
        self.assertTrue(loss_stop_active(-60.0, 60.0, None, "2026-10-04"))
        self.assertFalse(loss_stop_active(-59.99, 60.0, None, "2026-10-04"))
        self.assertTrue(loss_stop_active(0.0, 60.0, "2026-10-04", "2026-10-04"))
        self.assertFalse(loss_stop_active(0.0, 60.0, "2026-10-04", "2026-10-05"))
        yesterday = morning - 86400
        pnl = day_pnl(
            [
                {"settled_ts": yesterday, "pnl": -100.0},
                {"settled_ts": morning, "pnl": -40.0},
                {"token_id": "T", "shares": 100.0, "cost": 20.0, "last_mark": 0.10},
            ],
            morning,
            {},
        )
        # Yesterday's -100 is a different Dublin day. Today is -40 realized
        # plus 100 * 0.10 - 20 = -10 marked.
        self.assertAlmostEqual(pnl, -50.0, places=6)
        blocked = evaluate_entry(_view(), _account(loss_stopped=True), self.cfg)
        self.assertEqual(blocked["reason"], "daily_loss")

    def test_settlement_tie_goes_to_up(self):
        up = settle_pnl(side="up", shares=10, cost=8, fee=0.1, final_twap=100, strike=100)
        down = settle_pnl(side="down", shares=10, cost=8, fee=0.1, final_twap=100, strike=100)
        self.assertTrue(up["won"])
        self.assertFalse(down["won"])
        self.assertAlmostEqual(up["pnl"], 10 - 8 - 0.1, places=6)

    def test_dry_run_walks_the_book_and_does_not_call_a_poster(self):
        plan = {"asks": [(0.80, 10.0), (0.81, 100.0)], "limit": 0.80, "notional": 20.0}

        def poster(_plan):
            raise AssertionError("dry_run posted an order")

        fill = execute_buy(plan, dry_run=True, poster=poster)
        self.assertFalse(fill["posted"])
        self.assertTrue(fill["dry_run"])
        self.assertAlmostEqual(fill["cost"], 8.0, places=6)
        self.assertAlmostEqual(fill["shares"], 10.0, places=6)


if __name__ == "__main__":
    unittest.main()
