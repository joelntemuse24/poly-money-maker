"""Tests for check_buy_rejects counterfactual counts."""

from __future__ import annotations

import unittest

from check_buy_rejects import is_amount_reject, summarize


class BuyRejectSummarizeTests(unittest.TestCase):
    def test_amount_reject_needle(self):
        self.assertTrue(
            is_amount_reject(
                {
                    "event": "buy_attempt_rejected",
                    "error": "invalid amounts, the market buy orders maker amount supports a max accuracy of 2 decimals, taker amount a max of 4 decimals",
                }
            )
        )
        self.assertFalse(
            is_amount_reject({"event": "buy_attempt_rejected", "error": "not enough balance"})
        )

    def test_unique_market_blocked_when_never_filled(self):
        events = [
            {
                "ts": "2026-08-19T06:38:11",
                "event": "buy_attempt",
                "condition_id": "0xaaa",
                "ask": 0.82,
            },
            {
                "ts": "2026-08-19T06:38:15",
                "event": "buy_attempt_rejected",
                "token_id": "1",
                "error": "PolyApiException[status_code=400, error_message={'error': 'invalid amounts, the market buy orders maker amount supports a max accuracy of 2 decimals, taker amount a max of 4 decimals'}]",
            },
            {
                "ts": "2026-08-19T06:38:30",
                "event": "buy_attempt",
                "condition_id": "0xaaa",
                "ask": 0.90,
            },
            {
                "ts": "2026-08-19T06:38:35",
                "event": "buy_attempt_rejected",
                "token_id": "1",
                "error": "invalid amounts, the market buy orders maker amount supports a max accuracy of 2 decimals",
            },
            {
                "ts": "2026-08-19T06:58:10",
                "event": "buy_attempt",
                "condition_id": "0xbbb",
                "ask": 0.79,
            },
            {
                "ts": "2026-08-19T06:58:13",
                "event": "buy_attempt_rejected",
                "token_id": "2",
                "error": "invalid amounts, the market buy orders maker amount supports a max accuracy of 2 decimals",
            },
            {
                "ts": "2026-08-19T07:00:00",
                "event": "buy_fill",
                "condition_id": "0xccc",
            },
        ]
        stats = summarize(events)
        self.assertEqual(len(stats["blocked"]), 2)
        self.assertEqual(stats["reject_posts"], 3)
        ids = {row["condition_id"] for row in stats["blocked"]}
        self.assertEqual(ids, {"0xaaa", "0xbbb"})


if __name__ == "__main__":
    unittest.main()
