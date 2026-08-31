"""Unit tests for public last-trade hedge helpers (no network)."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from check_hedge_threshold import (
    fade_touch,
    first_touch,
    five_m_start_ts,
    persist_touch,
    series_of,
)
from check_path_backtest import main as path_main


class ParseTests(unittest.TestCase):
    def test_five_m_slug_unix(self):
        ts = five_m_start_ts(
            "Bitcoin Up or Down - August 21, 7:05PM-7:10PM ET", 2026
        )
        self.assertEqual(ts, 1787353500)
        self.assertEqual(
            series_of("Bitcoin Up or Down - August 21, 7:05PM-7:10PM ET"), "5m"
        )
        self.assertEqual(series_of("Bitcoin Up or Down - August 21, 6PM ET"), "hourly")
        self.assertEqual(series_of("btc-updown-5m-1787353500"), "5m")
        self.assertEqual(five_m_start_ts("btc-updown-5m-1787353500", 2026), 1787353500)
        self.assertEqual(series_of("BTC Up or Down 5m"), "unknown")
        self.assertEqual(
            series_of("Bitcoin Up or Down - August 27, 5:00-5:05PM ET"), "5m"
        )
        self.assertEqual(
            five_m_start_ts("Bitcoin Up or Down - August 27, 5:00-5:05PM ET", 2026),
            five_m_start_ts("Bitcoin Up or Down - August 27, 5:00PM-5:05PM ET", 2026),
        )


class TouchTests(unittest.TestCase):
    def test_first_touch_and_gap(self):
        path = [(1.0, 0.92), (2.0, 0.40)]
        hit = first_touch(path, 0.70)
        self.assertEqual(hit, (2.0, 0.40))

    def test_persist_cancels_on_recovery(self):
        path = [(1.0, 0.68), (2.0, 0.90), (8.0, 0.66)]
        self.assertIsNone(persist_touch(path, 0.70, 2.0))
        self.assertEqual(persist_touch([(1.0, 0.68), (1.6, 0.67)], 0.70, 0.5), (1.6, 0.67))

    def test_persist_holds(self):
        path = [(1.0, 0.68), (2.0, 0.66), (4.0, 0.64)]
        self.assertEqual(persist_touch(path, 0.70, 2.0), (4.0, 0.64))

    def test_fade_from_fill(self):
        path = [(1.0, 0.90), (2.0, 0.83)]
        self.assertEqual(fade_touch(path, 0.92, 0.08), (2.0, 0.83))


class HedgeSweepCliTests(unittest.TestCase):
    def test_missing_ticks_exits_2(self):
        with tempfile.TemporaryDirectory() as tmp:
            rc = path_main(["--hedge-sweep", "--series", "5m", "--dir", tmp])
        self.assertEqual(rc, 2)


if __name__ == "__main__":
    unittest.main()
