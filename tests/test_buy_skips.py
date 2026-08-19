"""Tests for in-window skip reasons and log summarizer."""

from __future__ import annotations

import unittest

from buy.entry_skip import window_no_buy_reason
from check_buy_skips import skip_reason, summarize


class WindowNoBuyReasonTests(unittest.TestCase):
    def test_armed_buy_is_none(self):
        self.assertIsNone(
            window_no_buy_reason(
                up_ask=0.80,
                dn_ask=0.20,
                up_winning=True,
                dn_winning=False,
                up_ask_ok=True,
                dn_ask_ok=False,
                up_consensus=True,
                dn_consensus=False,
                up_buy=True,
                dn_buy=False,
                threshold=0.75,
                max_price=0.90,
            )
        )

    def test_winning_ask_below_band(self):
        self.assertEqual(
            window_no_buy_reason(
                up_ask=0.72,
                dn_ask=0.28,
                up_winning=True,
                dn_winning=False,
                up_ask_ok=False,
                dn_ask_ok=False,
                up_consensus=False,
                dn_consensus=False,
                up_buy=False,
                dn_buy=False,
                threshold=0.75,
                max_price=0.90,
            ),
            "ask_below_band",
        )

    def test_winning_ask_above_band(self):
        self.assertEqual(
            window_no_buy_reason(
                up_ask=0.20,
                dn_ask=0.94,
                up_winning=False,
                dn_winning=True,
                up_ask_ok=False,
                dn_ask_ok=False,
                up_consensus=False,
                dn_consensus=False,
                up_buy=False,
                dn_buy=False,
                threshold=0.75,
                max_price=0.90,
            ),
            "ask_above_band",
        )

    def test_no_consensus_when_ask_in_band(self):
        self.assertEqual(
            window_no_buy_reason(
                up_ask=0.82,
                dn_ask=0.18,
                up_winning=True,
                dn_winning=False,
                up_ask_ok=True,
                dn_ask_ok=False,
                up_consensus=False,
                dn_consensus=False,
                up_buy=False,
                dn_buy=False,
                threshold=0.75,
                max_price=0.90,
            ),
            "no_consensus",
        )


class SkipSummarizeTests(unittest.TestCase):
    def test_reason_from_legacy_event_name(self):
        self.assertEqual(
            skip_reason({"event": "buy_skip_underlying_side"}),
            "underlying_side",
        )
        self.assertEqual(
            skip_reason({"event": "buy_skip", "reason": "ask_below_band"}),
            "ask_below_band",
        )

    def test_counts_windows_attempts_fills(self):
        stats = summarize(
            [
                {"ts": "1", "event": "buy_window", "condition_id": "0xa"},
                {
                    "ts": "2",
                    "event": "buy_skip",
                    "reason": "ask_below_band",
                    "condition_id": "0xa",
                },
                {
                    "ts": "3",
                    "event": "buy_skip_underlying_edge",
                    "condition_id": "0xb",
                },
                {"ts": "4", "event": "buy_attempt", "condition_id": "0xb"},
                {"ts": "5", "event": "buy_fail", "condition_id": "0xb", "status": "empty"},
                {"ts": "6", "event": "buy_fill", "condition_id": "0xc"},
            ]
        )
        self.assertEqual(stats["windows"], 1)
        self.assertEqual(stats["attempts"], 1)
        self.assertEqual(stats["fills"], 1)
        self.assertEqual(stats["reasons"]["ask_below_band"], 1)
        self.assertEqual(stats["reasons"]["underlying_edge"], 1)
        self.assertEqual(stats["reasons"]["fail_empty"], 1)


if __name__ == "__main__":
    unittest.main()
