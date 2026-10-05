"""Live-switch, split ledgers, per-market caps, and the cheap book path."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from buy.lock_bookws import ClobBookFeed, apply_book_message, plan_subscriptions
from buy.lock_config import apply_defaults, validate_config
from buy.lock_gates import evaluate_strategy1, evaluate_strategy2
from buy.lock_orders import open_live_client
from buy.lock_paper import latency_summary
from buy.lock_state import decision_wait_s, ledger_path, load_ledger, reset_live_ledger, save_ledger


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


class BookFeedTests(unittest.TestCase):
    def test_plan_drops_expired_tokens_and_keeps_order(self):
        drop, add = plan_subscriptions({"a", "b", "c"}, ["b", "d"])
        self.assertEqual(drop, ["a", "c"])
        self.assertEqual(add, ["d"])

    def test_send_does_not_touch_a_missing_socket(self):
        feed = ClobBookFeed()
        self.assertIsNone(feed._ws)
        self.assertFalse(feed._send({"type": "market", "assets_ids": ["tok"]}))
        feed._safe_close()
        self.assertIsNone(feed._ws)

    def test_deltas_stay_sorted_without_a_full_replace(self):
        feed = ClobBookFeed(clock=lambda: 10.0)
        feed.handle_message(
            {"event_type": "book", "asset_id": "UP", "asks": [{"price": "0.40", "size": "10"}], "bids": [{"price": "0.39", "size": "4"}]}
        )
        feed.handle_message(
            {"event_type": "price_change", "price_changes": [{"asset_id": "UP", "side": "SELL", "price": "0.41", "size": "5"}]}
        )
        book = feed.book("UP")
        self.assertEqual(book["asks"], [(0.4, 10.0), (0.41, 5.0)])
        feed.handle_message(
            {"event_type": "price_change", "price_changes": [{"asset_id": "UP", "side": "SELL", "price": "0.40", "size": "0"}]}
        )
        self.assertEqual(feed.book("UP")["asks"], [(0.41, 5.0)])

    def test_apply_book_message_still_returns_lists(self):
        books = {}
        apply_book_message(
            books,
            {"event_type": "book", "asset_id": "UP", "asks": [{"price": "0.40", "size": "10"}], "bids": []},
            now=1.0,
        )
        self.assertEqual(books["UP"]["asks"], [(0.4, 10.0)])


class CapTests(unittest.TestCase):
    def test_market_rule_combined_usd_overrides_the_global_cap(self):
        cfg = apply_defaults(
            {
                "combined_per_market_usd": 40.0,
                "market_rules": {"btc_5m": {"combined_usd": 10.0}, "btc_15m": {"combined_usd": 5.0}},
            }
        )
        spent = {"spent_s1": 5.0, "spent_s2": 5.0, "spent_total": 10.0, "open_cost": 0.0, "cash": 500.0}
        blocked = evaluate_strategy2(_s2_view(), spent, cfg)
        self.assertEqual(blocked["reason"], "combined_cap")
        fifteen = evaluate_strategy1(_view(), {"spent_s1": 0.0, "spent_s2": 0.0, "spent_total": 0.0, "open_cost": 0.0, "cash": 500.0}, cfg)
        self.assertEqual(fifteen["action"], "buy")
        self.assertAlmostEqual(fifteen["notional"], 5.0)
        full = evaluate_strategy1(
            _view(),
            {"spent_s1": 5.0, "spent_s2": 0.0, "spent_total": 5.0, "open_cost": 0.0, "cash": 500.0},
            cfg,
        )
        self.assertEqual(full["reason"], "combined_cap")

    def test_missing_market_combined_keeps_the_global_cap(self):
        cfg = apply_defaults({"combined_per_market_usd": 40.0, "strategy1_market_usd": 20.0, "strategy2_market_usd": 20.0})
        account = {"spent": 20.0, "spent_s2": 20.0, "spent_total": 20.0, "open_cost": 0.0, "cash": 500.0, "loss_stopped": False}
        decision = evaluate_strategy1(_view(), account, cfg)
        self.assertEqual(decision["action"], "buy")
        self.assertAlmostEqual(decision["notional"], 5.0)

    def test_combined_usd_must_be_positive(self):
        cfg = apply_defaults({"market_rules": {"btc_5m": {"combined_usd": 0}}})
        with self.assertRaises(ValueError):
            validate_config(cfg)


class LedgerTests(unittest.TestCase):
    def test_live_and_paper_are_different_files(self):
        root = Path("/tmp/lockbot-ledger-names")
        self.assertEqual(ledger_path(root, True).name, "positions_lockbot.json")
        self.assertEqual(ledger_path(root, False).name, "positions_lockbot_live.json")

    def test_reset_clears_live_and_leaves_paper(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paper = ledger_path(root, True)
            save_ledger(paper, {"positions": {"keep": {"pnl": -15.0}}, "intents": {}, "redeems": {}, "ledger": "paper"})
            live = ledger_path(root, False)
            save_ledger(live, {"positions": {"gone": {"pnl": -1.0}}, "intents": {"a": 1}, "redeems": {}, "ledger": "live"})
            reset_live_ledger(root, now=10.0)
            self.assertEqual(load_ledger(paper, "paper")["positions"]["keep"]["pnl"], -15.0)
            cleared = load_ledger(live, "live")
            self.assertEqual(cleared["positions"], {})
            self.assertEqual(cleared["intents"], {})
            self.assertEqual(cleared["reset_ts"], 10.0)
            self.assertEqual(cleared["ledger"], "live")

    def test_live_loss_stop_does_not_read_the_paper_book(self):
        """The stop is whatever positions the caller passes. Live mode loads only the live file."""
        from buy.lock_gates import day_pnl, loss_stop_active

        paper_loss = day_pnl([{"settled_ts": 1.0, "pnl": -15.62, "cost": 20.0}], 2.0, {})
        live_loss = day_pnl([], 2.0, {})
        self.assertLess(paper_loss, -15.0)
        self.assertEqual(live_loss, 0.0)
        self.assertTrue(loss_stop_active(paper_loss, 15.0, None, "2026-10-05"))
        self.assertFalse(loss_stop_active(live_loss, 15.0, None, "2026-10-05"))


class LiveSwitchTests(unittest.TestCase):
    def test_missing_client_is_a_restart(self):
        client, err = open_live_client(lambda: None)
        self.assertIsNone(client)
        self.assertIn("restart", err)

    def test_builder_errors_are_returned(self):
        def boom():
            raise RuntimeError("relayer down")

        client, err = open_live_client(boom)
        self.assertIsNone(client)
        self.assertIn("relayer down", err)

    def test_a_client_is_returned(self):
        sentinel = object()
        client, err = open_live_client(lambda: sentinel)
        self.assertIs(client, sentinel)
        self.assertEqual(err, "")


class WaitTests(unittest.TestCase):
    def test_wait_is_the_next_timer_not_a_spin(self):
        now = 1_000.0
        self.assertAlmostEqual(decision_wait_s(now, poll_s=1.0), 1.0)
        self.assertAlmostEqual(decision_wait_s(now, poll_s=1.0, s1_next=1_000.4), 0.4)
        self.assertAlmostEqual(decision_wait_s(now, poll_s=1.0, paper_next=1_000.2, s1_next=1_000.4), 0.2)
        self.assertAlmostEqual(decision_wait_s(now, poll_s=1.0, window_in=30.0), 1.0)

    def test_schedule_and_post_latencies_are_not_double_counted(self):
        summary = latency_summary(
            [
                {"event": "signal", "recv_to_decision_ms": 4.0, "recv_to_handoff_ms": 5.0, "recv_to_post_ms": 5.0},
                {"event": "paper_fill", "decision_to_post_ms": 200.0, "recv_to_post_ms": 210.0},
                {"event": "entry", "decision_to_post_ms": 12.0, "recv_to_post_ms": 18.0},
            ]
        )
        self.assertEqual(summary["recv_to_handoff_ms"]["n"], 1)
        self.assertEqual(summary["recv_to_handoff_ms"]["p50"], 5.0)
        self.assertEqual(summary["recv_to_post_ms"]["n"], 2)
        self.assertEqual(summary["decision_to_post_ms"]["n"], 2)


if __name__ == "__main__":
    unittest.main()
