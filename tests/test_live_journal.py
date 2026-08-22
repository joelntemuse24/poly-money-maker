"""Tests for the 5m live-tape replay (no network)."""

from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from buy.live_journal import (
    default_journal_path,
    format_tape_line,
    is_journal_event,
    iter_rotated_paths,
    load_journal_events,
    since_iso_hours_ago,
    summarize_tape,
)


class JournalEventFilterTests(unittest.TestCase):
    def test_keeps_money_path_events(self):
        for name in (
            "buy_fill",
            "buy_skip",
            "hedge_attempt",
            "hedge_tick_retry",
            "sell_build_rejected",
            "hedge_fill",
            "redeem_submit",
            "cycle_error",
            "pnl_recorded",
            "dry_sell",
        ):
            self.assertTrue(is_journal_event(name), name)

    def test_drops_book_noise(self):
        for name in (
            "book_fetch_fail",
            "book_quote_fail",
            "last_trade_fail",
            "positions_fetch_fail",
            "",
        ):
            self.assertFalse(is_journal_event(name), name)


class JournalLoadTests(unittest.TestCase):
    def test_since_cutoff_and_rotation_order(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            primary = root / "buybot5m.journal.jsonl"
            older = Path(f"{primary}.1")
            older.write_text(
                json.dumps(
                    {
                        "ts": "2026-08-22T01:00:00",
                        "event": "buy_fill",
                        "condition_id": "old",
                    }
                )
                + "\n"
                + json.dumps(
                    {
                        "ts": "2026-08-22T01:00:01",
                        "event": "book_fetch_fail",
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            primary.write_text(
                json.dumps(
                    {
                        "ts": "2026-08-22T04:00:00",
                        "event": "hedge_attempt",
                        "leg": "down",
                        "bid": 0.61,
                        "ask": 0.62,
                    }
                )
                + "\n"
                + json.dumps(
                    {
                        "ts": "2026-08-22T04:00:01",
                        "event": "sell_build_rejected",
                        "error": "invalid tick size (0.001), minimum is 0.01",
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            paths = iter_rotated_paths(primary)
            self.assertEqual(paths, [older, primary])
            kept = load_journal_events(paths, since="2026-08-22T03:00:00")
            self.assertEqual(
                [row["event"] for row in kept],
                ["hedge_attempt", "sell_build_rejected"],
            )
            all_rows = load_journal_events(paths)
            self.assertEqual(
                [row["event"] for row in all_rows],
                ["buy_fill", "hedge_attempt", "sell_build_rejected"],
            )
            line = format_tape_line(kept[0])
            self.assertIn("hedge_attempt", line)
            self.assertIn("bid=0.610", line)
            self.assertIn("invalid tick size", format_tape_line(kept[1]))
            stats = summarize_tape(kept)
            self.assertEqual(stats["events"], 2)
            self.assertEqual(stats["counts"]["sell_build_rejected"], 1)

    def test_hours_ago_cutoff(self):
        stamp = since_iso_hours_ago(5, now=datetime(2026, 8, 22, 9, 0, 0))
        self.assertEqual(stamp, "2026-08-22T04:00:00")

    def test_default_prefers_tape_when_present(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "buybot5m.log").write_text("{}\n", encoding="utf-8")
            self.assertEqual(default_journal_path(root), root / "buybot5m.log")
            (root / "buybot5m.journal.jsonl").write_text("{}\n", encoding="utf-8")
            self.assertEqual(
                default_journal_path(root), root / "buybot5m.journal.jsonl"
            )


if __name__ == "__main__":
    unittest.main()
