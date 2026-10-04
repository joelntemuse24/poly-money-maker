"""NIULAI4 ladder details, the Binance move, and latency-aware paper fills."""

from __future__ import annotations

import unittest

from buy.lock_binance import BinanceTradeFeed, parse_trade
from buy.lock_bookws import apply_book_message
from buy.lock_config import apply_defaults
from buy.lock_fair import signed_move
from buy.lock_gates import evaluate_strategy2
from buy.lock_paper import enqueue_paper, latency_summary, take_due, walk_late_book


def _s2(**over):
    base = {
        "now": 1_000.0,
        "end_ts": 1_200.0,
        "key": "btc_5m",
        "market_enabled": True,
        "resolution_ok": True,
        "resolution_source": "https://data.chain.link/streams/btc-usd-twap-60s-streams",
        "slug": "btc-updown-5m-100",
        "asset": "btc",
        "duration": "5m",
        "up_token": "UP",
        "dn_token": "DN",
        "binance_recv_ts": 999.5,
        "binance_move": 2.4,
        "sigma1s": 1.0,
        "sigma_source": "pre_window",
        "up": {"asks": [(0.40, 100.0)], "bids": [(0.39, 10.0)], "recv_ts": 999.5},
        "down": {"asks": [(0.62, 100.0)], "bids": [(0.60, 10.0)], "recv_ts": 999.5},
    }
    base.update(over)
    return base


class ReplicaTests(unittest.TestCase):
    def setUp(self):
        self.cfg = apply_defaults({})

    def test_move_of_two_sigma_buys_that_side(self):
        up = evaluate_strategy2(_s2(), {"cash": 500.0, "open_cost": 0.0}, self.cfg)
        self.assertEqual(up["action"], "buy")
        self.assertEqual(up["side"], "up")
        self.assertEqual(up["strategy"], "s2")
        self.assertAlmostEqual(up["notional"], 5.0)
        down = evaluate_strategy2(_s2(binance_move=-2.4), {"cash": 500.0, "open_cost": 0.0}, self.cfg)
        self.assertEqual(down["action"], "buy")
        self.assertEqual(down["side"], "down")
        quiet = evaluate_strategy2(_s2(binance_move=1.9), {"cash": 500.0, "open_cost": 0.0}, self.cfg)
        self.assertEqual(quiet["reason"], "move_below")

    def test_strategy2_has_no_q_filter_unless_configured(self):
        bought = evaluate_strategy2(_s2(q_up=0.1), {"cash": 500.0, "open_cost": 0.0}, self.cfg)
        self.assertEqual(bought["action"], "buy")
        filtered = evaluate_strategy2(
            _s2(q_up=0.1),
            {"cash": 500.0, "open_cost": 0.0},
            apply_defaults({"s2_q_edge_min": 0.0}),
        )
        self.assertEqual(filtered["reason"], "q_edge_below")

    def test_strategy2_stays_off_outside_btc_5m_and_when_binance_is_stale(self):
        other = evaluate_strategy2(_s2(key="btc_15m"), {"cash": 500.0, "open_cost": 0.0}, self.cfg)
        self.assertEqual(other["reason"], "strategy_market_off")
        stale = evaluate_strategy2(_s2(binance_recv_ts=990.0), {"cash": 500.0, "open_cost": 0.0}, self.cfg)
        self.assertEqual(stale["reason"], "stale_binance")
        off = evaluate_strategy2(_s2(), {"cash": 500.0, "open_cost": 0.0}, apply_defaults({"strategy2_enabled": False}))
        self.assertEqual(off["reason"], "strategy_off")

    def test_sigma_prefers_the_pre_window_and_falls_back_to_rolling(self):
        feed = BinanceTradeFeed(clock=lambda: 1_000.0)
        start = 500.0
        for sec in range(200, 501):
            feed.handle_message({"e": "trade", "p": str(100.0 + (sec % 5) * 0.5), "T": sec}, recv_ts=float(sec))
        sigma, n, source = feed.sigma_before(start, 300, min_n=30)
        self.assertEqual(source, "pre_window")
        self.assertGreaterEqual(n, 30)
        self.assertGreater(sigma, 0.0)
        late = BinanceTradeFeed(clock=lambda: 1_000.0)
        for sec in range(520, 600):
            late.handle_message({"e": "trade", "p": str(100.0 + (sec % 3)), "T": sec}, recv_ts=float(sec))
        sigma, n, source = late.sigma_before(start, 300, min_n=30)
        self.assertEqual(source, "rolling")
        self.assertGreaterEqual(n, 30)
        parsed = parse_trade('{"e":"trade","p":"100.5","T":1700000000000}')
        self.assertEqual(parsed[0], 1_700_000_000.0)
        self.assertAlmostEqual(signed_move(103.0, 100.0, sigma=1.5), 2.0, places=9)

    def test_book_message_replaces_and_updates_one_level(self):
        books = {}
        apply_book_message(
            books,
            {"event_type": "book", "asset_id": "UP", "asks": [{"price": "0.40", "size": "10"}], "bids": [{"price": "0.39", "size": "4"}]},
            now=10.0,
        )
        self.assertEqual(books["UP"]["asks"], [(0.4, 10.0)])
        apply_book_message(
            books,
            {"event_type": "price_change", "price_changes": [{"asset_id": "UP", "side": "SELL", "price": "0.41", "size": "5"}]},
            now=11.0,
        )
        self.assertEqual(books["UP"]["asks"], [(0.4, 10.0), (0.41, 5.0)])
        apply_book_message(
            books,
            {"event_type": "price_change", "price_changes": [{"asset_id": "UP", "side": "SELL", "price": "0.40", "size": "0"}]},
            now=12.0,
        )
        self.assertEqual(books["UP"]["asks"], [(0.41, 5.0)])
        self.assertEqual(books["UP"]["recv_ts"], 12.0)

    def test_paper_fill_waits_out_the_latency_and_logs_times(self):
        queue = []
        decision = {
            "strategy": "s2",
            "slug": "btc-updown-5m-1",
            "side": "up",
            "token_id": "UP",
            "ask": 0.40,
            "limit": 0.40,
            "notional": 5.0,
            "asks": [(0.40, 20.0)],
            "binance_recv_ts": 1_000.0,
            "book_recv_ts": 1_000.05,
        }
        enqueue_paper(queue, decision, now=1_000.10)
        due, keep = take_due(queue, 1_000.20, 0.20)
        self.assertEqual(due, [])
        self.assertEqual(len(keep), 1)
        due, keep = take_due(queue, 1_000.35, 0.20)
        self.assertEqual(len(due), 1)
        self.assertEqual(keep, [])
        missed = walk_late_book(due[0], [(0.55, 10.0)], now=1_000.35)
        self.assertEqual(missed["reason"], "book_moved")
        self.assertEqual(missed["shares"], 0.0)
        self.assertIn("decision_ts", missed)
        self.assertIn("post_ts", missed)
        self.assertIn("ack_ts", missed)
        self.assertGreater(missed["decision_to_post_ms"], 0.0)
        self.assertIn("recv_to_post_ms", missed)
        filled = walk_late_book(due[0], [(0.40, 20.0)], now=1_000.35)
        self.assertEqual(filled["reason"], "paper_fill")
        self.assertAlmostEqual(filled["cost"], 5.0, places=6)
        summary = latency_summary([
            {"event": "paper_fill", "decision_to_post_ms": filled["decision_to_post_ms"], "recv_to_post_ms": filled["recv_to_post_ms"]},
        ])
        self.assertEqual(summary["decision_to_post_ms"]["n"], 1)
        self.assertIsNotNone(summary["recv_to_post_ms"])


if __name__ == "__main__":
    unittest.main()
