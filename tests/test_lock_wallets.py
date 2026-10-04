"""Wallet tape parser and the head-to-head latency join. No network."""

from __future__ import annotations

import unittest

from buy.lock_report import format_summary, summarize
from buy.lock_wallets import (
    WALLETS,
    WalletTape,
    compare_fills,
    head_to_head,
    normalize_wallet_fill,
    parse_activity_frame,
)


NIULAI4 = "0x44832d0d2ec11187c1e77d786feb15f6a50254c6"
ASDA = "0x75cc3b63a2f2423085e10706c78b494017b93ce1"
DVAS = "0x5d4aba8ad45bb5eab3499a0294b42da5d1e455d3"
SLUG = "btc-updown-5m-1791153600"


def _frame(wallet: str, *, slug: str = SLUG, price: float = 0.36, size: float = 5, ts: int = 1791155729, side: str = "BUY", outcome: str = "Up", tx: str = "0xdead") -> dict:
    return {
        "payload": {
            "asset": "123",
            "conditionId": "0xabc",
            "eventSlug": slug,
            "outcome": outcome,
            "price": price,
            "proxyWallet": wallet,
            "side": side,
            "size": size,
            "slug": slug,
            "timestamp": ts,
            "transactionHash": tx,
        },
        "timestamp": ts * 1000 + 857,
        "topic": "activity",
        "type": "trades",
    }


class ParseTests(unittest.TestCase):
    def test_watched_btc_fill(self):
        payloads = parse_activity_frame(_frame(NIULAI4))
        self.assertEqual(len(payloads), 1)
        fill = normalize_wallet_fill(payloads[0], recv_ts=1791155728.2)
        self.assertEqual(fill["name"], "NIULAI4")
        self.assertEqual(fill["wallet"], NIULAI4)
        self.assertEqual(fill["slug"], SLUG)
        self.assertEqual(fill["duration"], "5m")
        self.assertEqual(fill["outcome"], "Up")
        self.assertEqual(fill["trade_side"], "BUY")
        self.assertEqual(fill["price"], 0.36)
        self.assertEqual(fill["size"], 5)
        self.assertEqual(fill["their_ts"], 1791155729)
        self.assertAlmostEqual(fill["recv_ts"], 1791155728.2)
        self.assertEqual(fill["tx"], "0xdead")

    def test_all_three_wallets_and_both_durations(self):
        for wallet, name in WALLETS.items():
            fill = normalize_wallet_fill(parse_activity_frame(_frame(wallet))[0], recv_ts=1)
            self.assertEqual(fill["name"], name)
        fifteen = "btc-updown-15m-1791153900"
        fill = normalize_wallet_fill(parse_activity_frame(_frame(DVAS, slug=fifteen))[0], recv_ts=1)
        self.assertEqual(fill["duration"], "15m")
        sell = normalize_wallet_fill(parse_activity_frame(_frame(ASDA, side="SELL"))[0], recv_ts=1)
        self.assertEqual(sell["trade_side"], "SELL")

    def test_drops_other_markets_and_wallets(self):
        eth = _frame(NIULAI4, slug="eth-updown-5m-1791153600")
        self.assertIsNone(normalize_wallet_fill(parse_activity_frame(eth)[0], recv_ts=1))
        stranger = _frame("0x0000000000000000000000000000000000000001")
        self.assertIsNone(normalize_wallet_fill(parse_activity_frame(stranger)[0], recv_ts=1))
        self.assertIsNone(parse_activity_frame({"topic": "crypto_prices_chainlink", "type": "update", "payload": {}}))

    def test_event_slug_and_millisecond_timestamp(self):
        raw = _frame(ASDA)
        raw["payload"]["slug"] = ""
        raw["payload"]["timestamp"] = 1791155729000
        fill = normalize_wallet_fill(parse_activity_frame(raw)[0], recv_ts=1)
        self.assertEqual(fill["slug"], SLUG)
        self.assertEqual(fill["their_ts"], 1791155729)

    def test_duplicate_tx_is_delivered_once(self):
        seen = []
        tape = WalletTape(on_fill=seen.append)
        self.assertEqual(tape.handle_message(_frame(NIULAI4), recv_ts=10), 1)
        self.assertEqual(tape.handle_message(_frame(NIULAI4), recv_ts=11), 0)
        self.assertEqual(len(seen), 1)
        self.assertEqual(tape.fills, 1)


class CompareTests(unittest.TestCase):
    def test_we_were_first_uses_vwap_and_their_timestamp(self):
        attempts = [
            {
                "slug": SLUG,
                "strategy": "s2",
                "side": "up",
                "decision_ts": 100.0,
                "ask": 0.55,
                "limit": 0.55,
                "post_ts": 100.2,
                "ack_ts": 100.21,
                "vwap": 0.54,
                "shares": 5,
            }
        ]
        fills = [
            {
                "wallet": ASDA,
                "name": "asdaefef",
                "slug": SLUG,
                "outcome": "Up",
                "trade_side": "BUY",
                "price": 0.50,
                "size": 20,
                "their_ts": 103.0,
                "recv_ts": 101.5,
                "tx": "0x1",
            }
        ]
        case = compare_fills(attempts, fills)[0]
        self.assertAlmostEqual(case["us_minus_them_s"], -3.0)
        self.assertTrue(case["we_first"])
        self.assertAlmostEqual(case["us_minus_them_recv_s"], -1.5)
        self.assertEqual(case["our_decision_ts"], 100.0)
        self.assertEqual(case["our_post_ts"], 100.2)
        self.assertEqual(case["our_ack_ts"], 100.21)
        self.assertEqual(case["their_price"], 0.50)
        self.assertEqual(case["their_size"], 20)
        self.assertAlmostEqual(case["price_diff"], 0.04)
        self.assertEqual(case["our_price_source"], "vwap")
        self.assertTrue(case["same_side"])

    def test_we_were_after_and_same_side_prefers_that_signal(self):
        attempts = [
            {"slug": SLUG, "strategy": "s1", "side": "up", "decision_ts": 10.0, "ask": 0.60, "limit": 0.60, "post_ts": 10.2, "ack_ts": 10.2, "shares": 0},
            {"slug": SLUG, "strategy": "s2", "side": "down", "decision_ts": 20.0, "ask": 0.40, "limit": 0.40, "post_ts": 20.2, "ack_ts": 20.2, "shares": 0},
        ]
        fills = [
            {"wallet": DVAS, "name": "dvasdkasodk", "slug": SLUG, "outcome": "Down", "trade_side": "BUY", "price": 0.42, "size": 8, "their_ts": 15.0, "recv_ts": 14.0, "tx": "0x2"}
        ]
        case = compare_fills(attempts, fills)[0]
        self.assertEqual(case["our_side"], "down")
        self.assertAlmostEqual(case["us_minus_them_s"], 5.0)
        self.assertFalse(case["we_first"])
        self.assertAlmostEqual(case["price_diff"], -0.02)
        self.assertEqual(case["our_price_source"], "ask")

    def test_other_market_is_not_a_case_and_other_side_skips_price(self):
        attempts = [
            {"slug": SLUG, "strategy": "s1", "side": "up", "decision_ts": 50.0, "ask": 0.70, "limit": 0.70, "shares": 0}
        ]
        fills = [
            {"wallet": NIULAI4, "name": "NIULAI4", "slug": "btc-updown-5m-1791153900", "outcome": "Up", "trade_side": "BUY", "price": 0.2, "size": 1, "their_ts": 40.0, "tx": "0xa"},
            {"wallet": NIULAI4, "name": "NIULAI4", "slug": SLUG, "outcome": "Down", "trade_side": "SELL", "price": 0.3, "size": 2, "their_ts": 60.0, "tx": "0xb"},
        ]
        cases = compare_fills(attempts, fills)
        self.assertEqual(len(cases), 1)
        self.assertFalse(cases[0]["same_side"])
        self.assertIsNone(cases[0]["price_diff"])
        self.assertTrue(cases[0]["we_first"])

    def test_summary_median_p90_and_we_first_share(self):
        rows = []
        deltas = [-2, -1, 0, 1, 2, 3, 4, 5, 6, 7]
        for i, delta in enumerate(deltas):
            their = 1_000.0
            decision = their + delta
            slug = f"btc-updown-5m-{1791153600 + i}"
            wallet = (NIULAI4, ASDA, DVAS)[i % 3]
            rows.append(
                {
                    "event": "signal",
                    "slug": slug,
                    "strategy": "s2",
                    "side": "up",
                    "decision_ts": decision,
                    "ask": 0.50 + i * 0.01,
                    "limit": 0.50,
                }
            )
            rows.append(
                {
                    "event": "wallet_fill",
                    "wallet": wallet,
                    "name": WALLETS[wallet],
                    "slug": slug,
                    "outcome": "Up",
                    "trade_side": "BUY",
                    "price": 0.40,
                    "size": 1 + i,
                    "their_ts": their,
                    "recv_ts": their - 1.0,
                    "tx": f"0x{i}",
                }
            )
        rows.append(
            {
                "event": "wallet_fill",
                "wallet": NIULAI4,
                "name": "NIULAI4",
                "slug": "btc-updown-15m-9",
                "outcome": "Up",
                "trade_side": "BUY",
                "price": 0.2,
                "size": 3,
                "their_ts": 1.0,
                "tx": "0xsolo",
            }
        )
        cases, summary = head_to_head(rows)
        self.assertEqual(len(cases), 10)
        self.assertEqual(summary["fills"], 11)
        self.assertEqual(summary["all"]["n"], 10)
        self.assertAlmostEqual(summary["all"]["us_minus_them_s"]["median"], 2.5)
        self.assertAlmostEqual(summary["all"]["us_minus_them_s"]["p90"], 6.1)
        self.assertAlmostEqual(summary["all"]["we_first"], 0.2)
        self.assertEqual(summary["all"]["price_diff"]["n"], 10)
        self.assertIn("NIULAI4", summary["by_wallet"])
        text = format_summary(summarize(rows))
        self.assertIn("vs wallets", text)
        self.assertIn("us_minus_them_s median 2.5", text)
        self.assertIn("p90 6.1", text)
        self.assertIn("we_first 20.0%", text)
        self.assertIn("price_diff median", text)


if __name__ == "__main__":
    unittest.main()
