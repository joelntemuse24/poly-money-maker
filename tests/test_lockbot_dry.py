"""Dry-run never posts, and the in-memory view feeds the gate."""

from __future__ import annotations

import sys
import unittest

import lockbot
from buy.lock_config import apply_defaults
from buy.lock_engine import build_view, path_covered
from buy.lock_markets import parse_lock_event
from buy.lock_orders import dispatch_buy
from buy.lock_report import format_summary, summarize


class BoomClient:
    def __getattr__(self, name):
        raise AssertionError(f"dry_run touched the clob client via {name}")


class DryRunTests(unittest.TestCase):
    def test_import_does_not_pull_in_mintbot(self):
        self.assertNotIn("mintbot", sys.modules)
        self.assertTrue(callable(lockbot.main))

    def test_dry_run_dispatch_never_posts(self):
        plan = {
            "token_id": "111",
            "asks": [(0.80, 40.0)],
            "bids": [(0.79, 10.0)],
            "limit": 0.80,
            "notional": 20.0,
            "shares": 25.0,
        }
        fill = dispatch_buy(plan, dry_run=True, client=BoomClient())
        self.assertTrue(fill["dry_run"])
        self.assertFalse(fill["posted"])
        self.assertAlmostEqual(fill["shares"], 25.0, places=6)
        self.assertAlmostEqual(fill["cost"], 20.0, places=6)
        self.assertIn("decision_to_order_ms", fill)

    def test_live_dispatch_posts_once(self):
        calls = []

        class Client:
            def create_market_order(self, args):
                calls.append(("create", args.price, args.amount, args.side))
                return {"signed": True}

            def post_order(self, signed, order_type):
                calls.append(("post", order_type, signed))
                return {"status": "matched", "takingAmount": "25", "makingAmount": "20"}

        plan = {
            "token_id": "111",
            "asks": [(0.80, 40.0)],
            "limit": 0.80,
            "notional": 20.0,
            "shares": 25.0,
        }
        fill = dispatch_buy(plan, dry_run=False, client=Client())
        self.assertEqual([row[0] for row in calls], ["create", "post"])
        self.assertTrue(fill["posted"])
        self.assertIn("decision_to_order_ms", fill)

    def test_view_locks_up_when_the_elapsed_path_is_above_the_strike(self):
        end = 1_791_144_900.0
        start = end - 900.0
        now = end - 30.0
        live = []
        price = 100.0
        for sec in range(int(now) - 200, int(now) + 1):
            price += 0.02
            live.append((float(sec), float(sec), price))
        twap = [(start, start, 100.0), (now, now, price)]
        market = parse_lock_event(
            {
                "slug": f"btc-updown-15m-{int(start)}",
                "eventMetadata": {"priceToBeat": 100.0},
                "markets": [
                    {
                        "slug": f"btc-updown-15m-{int(start)}",
                        "conditionId": "0x" + "cd" * 32,
                        "outcomes": ["Up", "Down"],
                        "clobTokenIds": ["11", "22"],
                        "resolutionSource": "https://data.chain.link/streams/btc-usd-twap-60s-streams",
                    }
                ],
            }
        )
        self.assertIsNotNone(market)
        assert market is not None
        self.assertTrue(path_covered(live, end - 60.0, now, 3.0))
        view = build_view(
            market,
            now=now,
            live_hist=live,
            twap_hist=twap,
            gamma_strike=100.0,
            cfg=apply_defaults({}),
        )
        self.assertEqual(view["strike_reason"], "match")
        self.assertGreater(view["p_up"], 0.8)
        self.assertGreater(view["vol_samples"], 60)
        self.assertTrue(view["coverage_ok"])

    def test_summary_groups_strategy_and_asset(self):
        rows = [
            {"event": "eval", "slug": "ignore"},
            {
                "event": "settlement",
                "slug": "btc-updown-15m-1",
                "lane": "btc_15m",
                "strategy": "s1",
                "asset": "btc",
                "duration": "15m",
                "pnl": 2.5,
                "won": True,
            },
            {
                "event": "settlement",
                "slug": "eth-updown-15m-1",
                "lane": "ext",
                "asset": "eth",
                "duration": "15m",
                "pnl": -1.0,
                "won": False,
            },
            {
                "event": "settlement",
                "slug": "btc-updown-5m-1",
                "lane": "btc_5m",
                "strategy": "s2",
                "asset": "btc",
                "duration": "5m",
                "pnl": 0.4,
                "won": True,
            },
        ]
        summary = summarize(rows)
        self.assertEqual(summary["markets"], 3)
        self.assertAlmostEqual(summary["pnl"], 1.9, places=4)
        self.assertAlmostEqual(summary["win_rate"], 2 / 3, places=4)
        self.assertEqual(summary["worst_slug"], "eth-updown-15m-1")
        self.assertEqual(summary["lanes"]["btc_15m"]["markets"], 1)
        self.assertEqual(summary["strategies"]["s1"]["markets"], 1)
        self.assertEqual(summary["strategies"]["s2"]["pnl"], 0.4)
        self.assertEqual(summary["assets"]["btc_5m"]["pnl"], 0.4)
        text = format_summary(summary)
        self.assertIn("strategy 1", text)
        self.assertIn("strategy 2", text)
        self.assertIn("eth_15m", text)
        self.assertIn("latency ms", text)


if __name__ == "__main__":
    unittest.main()
