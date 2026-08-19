"""Tests for in-window skip reasons and log summarizer."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from buy.entry_skip import window_no_buy_reason
from check_buy_skips import cycle_error_label, load_events, skip_reason, summarize


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

    def test_cycle_error_prefers_error_field(self):
        self.assertEqual(
            cycle_error_label({
                "event": "cycle_error",
                "error": "NameError: name 'known_cost' is not defined",
                "traceback": "Traceback...\nNameError: other",
            }),
            "NameError: name 'known_cost' is not defined",
        )

    def test_cycle_error_falls_back_to_traceback(self):
        self.assertEqual(
            cycle_error_label({
                "event": "cycle_error",
                "traceback": (
                    "Traceback (most recent call last):\n"
                    "  File x\n"
                    "NameError: name 'known_cost' is not defined\n"
                ),
            }),
            "NameError: name 'known_cost' is not defined",
        )

    def test_summarize_separates_cycle_errors_from_skips(self):
        stats = summarize(
            [
                {
                    "ts": "2026-08-13T04:33:10",
                    "event": "cycle_error",
                    "error": "NameError: name 'known_cost' is not defined",
                },
                {
                    "ts": "2026-08-13T04:33:11",
                    "event": "cycle_error",
                    "traceback": "tb\nNameError: name 'known_cost' is not defined",
                },
                {"ts": "2026-08-19T10:00:00", "event": "buy_attempt",
                 "condition_id": "c1"},
                {"ts": "2026-08-19T10:00:01", "event": "buy_fill",
                 "condition_id": "c1"},
            ]
        )
        self.assertEqual(stats["attempts"], 1)
        self.assertEqual(stats["fills"], 1)
        self.assertEqual(sum(stats["cycle_errors"].values()), 2)
        self.assertEqual(
            stats["cycle_errors"]["NameError: name 'known_cost' is not defined"],
            2,
        )
        self.assertNotIn(
            "NameError: name 'known_cost' is not defined",
            stats["reasons"],
        )

    def test_load_events_includes_cycle_error_since_cutoff(self):
        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp) / "buybot5m.log"
            lines = [
                json.dumps({
                    "ts": "2026-08-13T04:33:10",
                    "event": "cycle_error",
                    "error": "NameError: name 'known_cost' is not defined",
                }),
                json.dumps({
                    "ts": "2026-08-19T09:50:00",
                    "event": "buy_fill",
                    "condition_id": "c1",
                }),
            ]
            log.write_text("\n".join(lines) + "\n")
            before = load_events([log], since="2026-08-13T00:00:00")
            self.assertEqual(len(before), 2)
            after = load_events([log], since="2026-08-19T09:42:23")
            self.assertEqual(len(after), 1)
            self.assertEqual(after[0]["event"], "buy_fill")


if __name__ == "__main__":
    unittest.main()
