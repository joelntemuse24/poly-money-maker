"""Unit tests for pathlog tick retention (no network, no disk writes)."""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import pathlog
from pathlog import files_to_prune
from buy.market import MintMarket


NOW = 1_000_000.0


class FilesToPruneTests(unittest.TestCase):
    def test_empty(self):
        self.assertEqual(files_to_prune([], NOW, 14 * 86400, 400, 120), [])

    def test_recent_files_never_deleted_even_over_cap(self):
        files = [
            ("a.jsonl", NOW - 1, 500),
            ("b.jsonl", NOW - 5, 500),
        ]
        self.assertEqual(
            files_to_prune(files, NOW, retain_s=10, max_bytes=100, protect_recent_s=120),
            [],
        )

    def test_age_prune_deletes_past_retain(self):
        files = [
            ("old.jsonl", NOW - 20, 10),
            ("keep.jsonl", NOW - 5, 10),
        ]
        self.assertEqual(
            files_to_prune(files, NOW, retain_s=10, max_bytes=10_000, protect_recent_s=1),
            ["old.jsonl"],
        )

    def test_under_cap_within_retain_kept(self):
        files = [
            ("a.jsonl", NOW - 50, 100),
            ("b.jsonl", NOW - 40, 100),
        ]
        self.assertEqual(
            files_to_prune(files, NOW, retain_s=100, max_bytes=400, protect_recent_s=1),
            [],
        )

    def test_size_cap_deletes_oldest_first(self):
        files = [
            ("old.jsonl", NOW - 100, 300),
            ("mid.jsonl", NOW - 50, 300),
            ("new.jsonl", NOW - 20, 50),
        ]
        # 650 > 400; drop oldest (300) → 350 <= 400.
        self.assertEqual(
            files_to_prune(files, NOW, retain_s=1000, max_bytes=400, protect_recent_s=1),
            ["old.jsonl"],
        )

    def test_size_cap_keeps_deleting_until_under(self):
        files = [
            ("a.jsonl", NOW - 30, 200),
            ("b.jsonl", NOW - 20, 200),
            ("c.jsonl", NOW - 10, 200),
        ]
        self.assertEqual(
            files_to_prune(files, NOW, retain_s=1000, max_bytes=250, protect_recent_s=1),
            ["a.jsonl", "b.jsonl"],
        )

    def test_age_then_size(self):
        files = [
            ("ancient.jsonl", NOW - 500, 10),
            ("old.jsonl", NOW - 80, 300),
            ("mid.jsonl", NOW - 40, 300),
        ]
        # Age drops ancient; remaining 600 > 400 so also drop oldest kept.
        self.assertEqual(
            files_to_prune(files, NOW, retain_s=100, max_bytes=400, protect_recent_s=1),
            ["ancient.jsonl", "old.jsonl"],
        )

    def test_protected_bytes_count_toward_cap_but_are_not_deleted(self):
        files = [
            ("old.jsonl", NOW - 1000, 100),
            ("live.jsonl", NOW - 1, 500),
        ]
        # Protected 500 already over cap; still only delete eligible old file.
        self.assertEqual(
            files_to_prune(files, NOW, retain_s=10_000, max_bytes=400, protect_recent_s=120),
            ["old.jsonl"],
        )


class PruneTickDirTests(unittest.TestCase):
    def test_unlinks_age_expired_jsonl(self):
        with tempfile.TemporaryDirectory() as tmp:
            tick_dir = Path(tmp)
            old = tick_dir / "old.jsonl"
            keep = tick_dir / "keep.jsonl"
            old.write_text("{}\n")
            keep.write_text("{}\n")
            os.utime(old, (NOW - 20, NOW - 20))
            os.utime(keep, (NOW - 5, NOW - 5))
            with patch.object(pathlog, "TICK_DIR", tick_dir), patch.object(
                pathlog, "RETAIN_S", 10
            ), patch.object(pathlog, "MAX_TICK_BYTES", 10_000), patch.object(
                pathlog, "PROTECT_RECENT_S", 1
            ):
                removed = pathlog.prune_tick_dir(NOW)
            self.assertEqual(removed, 1)
            self.assertFalse(old.exists())
            self.assertTrue(keep.exists())


class SampleMarketSizeTests(unittest.TestCase):
    def test_no_local_best_parser(self):
        self.assertFalse(hasattr(pathlog, "_best"))
        from buy.book import best_from_levels

        self.assertIs(pathlog.best_from_levels, best_from_levels)

    def test_tick_includes_top_of_book_size(self):
        market = MintMarket(
            condition_id="0x1",
            slug="btc-updown-5m-1",
            question="q",
            end_ts=200.0,
            series_slug="btc-up-or-down-5m",
            up_token="up",
            dn_token="dn",
            active=True,
            closed=False,
            accepting_orders=True,
            neg_risk=False,
            start_ts=0.0,
        )
        with patch.object(
            pathlog,
            "fetch_book",
            side_effect=[(0.79, 10.0, 0.80, 3.5), (0.19, 8.0, 0.20, 40.0)],
        ):
            tick = pathlog.sample_market(market, 100.0)
        self.assertIsNotNone(tick)
        self.assertEqual(tick["ua"], 0.80)
        self.assertEqual(tick["da"], 0.20)
        self.assertEqual(tick["uas"], 3.5)
        self.assertEqual(tick["das"], 40.0)
        self.assertEqual(tick["ubs"], 10.0)
        self.assertEqual(tick["dbs"], 8.0)


if __name__ == "__main__":
    unittest.main()
