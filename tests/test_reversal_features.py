"""Unit tests for reversal-feature helpers (no network)."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from check_reversal_features import (
    BtcSeries,
    Features,
    Sample,
    bucket,
    features_at,
    five_m_windows,
    late_band_touch,
    paper_redeem_pnl,
    seconds_to_cross,
    session_markets,
    side_of,
    skip_cost_table,
)
from check_hedge_threshold import load_csv, series_of


def _sample(abs_dist: float, against: bool, flipped: bool, pnl: float = 0.44) -> Sample:
    feat = Features(
        ts=1,
        ttm=90,
        px=100_010,
        ptb=100_000,
        dist=abs_dist,
        abs_dist=abs_dist,
        side="up",
        against_30s=against,
        ripping_to_ptb=against,
        cross_before_end=against,
    )
    return Sample(
        slug="m",
        start_ts=0,
        end_ts=300,
        feat=feat,
        close_px=100_000 if flipped else 100_020,
        winner="down" if flipped else "up",
        reversed=flipped,
        soft_close=False,
        source="t",
        pnl_redeem=-2.5 if flipped else pnl,
        outcome="paper",
    )


class FeatureMathTests(unittest.TestCase):
    def test_side_and_cross(self):
        self.assertEqual(side_of(100_010, 100_000), "up")
        self.assertEqual(side_of(99_990, 100_000), "down")
        self.assertIsNone(side_of(100_000, 100_000))
        self.assertEqual(seconds_to_cross(30.0, -1.0), 30.0)
        self.assertIsNone(seconds_to_cross(30.0, 1.0))
        self.assertIsNone(seconds_to_cross(30.0, 0.0))
        self.assertEqual(seconds_to_cross(0.0, -1.0), 0.0)

    def test_bucket_labels(self):
        self.assertEqual(bucket(3.0, (0.0, 5.0, 10.0, float("inf"))), "0–5")
        self.assertEqual(bucket(12.0, (0.0, 5.0, 10.0, float("inf"))), ">=10")
        self.assertEqual(bucket(None, (0.0, 5.0)), "na")

    def test_features_against_momentum(self):
        # 1s series: still Up vs PTB but ripping down through the last 30s.
        ts = list(range(0, 120))
        px = [100_110.0 - i * 1.0 for i in ts]  # t=90 still +$20, −$1/s
        btc = BtcSeries(ts=ts, px=px)
        feat = features_at(btc, ts=90, end_ts=120, ptb=100_000.0)
        self.assertIsNotNone(feat)
        self.assertEqual(feat.side, "up")
        self.assertTrue(feat.against_30s)
        self.assertTrue(feat.cross_before_end)
        self.assertGreater(feat.abs_dist, 0)
        self.assertIsNotNone(feat.flip_z)
        self.assertGreater(feat.flip_z, 0)

    def test_features_with_momentum(self):
        ts = list(range(0, 120))
        px = [100_010.0 + i * 0.5 for i in ts]
        btc = BtcSeries(ts=ts, px=px)
        feat = features_at(btc, ts=90, end_ts=120, ptb=100_000.0)
        self.assertEqual(feat.side, "up")
        self.assertFalse(feat.against_30s)
        self.assertFalse(feat.cross_before_end)

    def test_sparse_1m_uses_60s_mom(self):
        ts = [0, 60, 120, 180, 240]
        px = [100_000.0, 100_040.0, 100_020.0, 100_005.0, 99_990.0]
        btc = BtcSeries(ts=ts, px=px)
        feat = features_at(btc, ts=240, end_ts=300, ptb=100_000.0)
        self.assertEqual(feat.side, "down")
        # Last two 1m bars: 100005 → 99990, still down and moving down = with.
        self.assertFalse(feat.against_30s)

    def test_sparse_1m_detects_against(self):
        ts = [0, 60, 120, 180, 240]
        px = [100_000.0, 99_950.0, 99_940.0, 99_970.0, 99_990.0]
        btc = BtcSeries(ts=ts, px=px)
        feat = features_at(btc, ts=240, end_ts=300, ptb=100_000.0)
        self.assertEqual(feat.side, "down")
        self.assertTrue(feat.against_30s)


class BandAndPnlTests(unittest.TestCase):
    def test_late_band_touch_first_in_window(self):
        trades = [
            {"ts": 100, "px": 0.80, "outcome": "up"},  # too early (end=300, late from 180)
            {"ts": 200, "px": 0.92, "outcome": "up"},  # above band
            {"ts": 210, "px": 0.82, "outcome": "down"},
            {"ts": 220, "px": 0.81, "outcome": "down"},
        ]
        hit = late_band_touch(trades, 0, 300)
        self.assertEqual(hit["ts"], 210)
        self.assertEqual(hit["outcome"], "down")

    def test_paper_redeem_pnl(self):
        # $2.50 at 85¢ → 2.941 sh; win +0.441, lose -2.50
        self.assertAlmostEqual(paper_redeem_pnl(0.85, True), 2.50 / 0.85 - 2.50, places=4)
        self.assertEqual(paper_redeem_pnl(0.85, False), -2.50)

    def test_skip_cost_against_flips(self):
        rows = [
            _sample(30, False, False, 0.44),
            _sample(8, True, True, -2.5),
            _sample(12, True, True, -2.5),
            _sample(40, False, False, 0.44),
        ]
        line = skip_cost_table(rows, lambda s: bool(s.feat.against_30s), "skip against")
        self.assertIn("skip against", line)
        self.assertIn("keep 2/4", line)
        # Skipping the two losers raises paper P&L.
        self.assertIn("delta +5.00", line)

    def test_five_m_windows_aligned(self):
        # ends 1000..1600 → first aligned end is 1200 if WINDOW_S=300
        wins = five_m_windows(1000, 1600)
        self.assertEqual(wins[0][1], 1200)
        self.assertEqual(wins[0][2], "btc-updown-5m-1200")
        self.assertEqual(wins[-1][1], 1500)


class CsvSessionTests(unittest.TestCase):
    def test_load_and_group_post_restart(self):
        raw = (
            "\ufeff\"marketName\",\"action\",\"usdcAmount\",\"tokenAmount\","
            "\"tokenName\",\"timestamp\",\"hash\"\n"
            "\"Bitcoin Up or Down - August 27, 5:00AM-5:05AM ET\",\"Buy\","
            "\"2.73\",\"3.14\",\"Up\",\"1787821393\",\"0xabc\"\n"
            "\"Bitcoin Up or Down - August 27, 5:00AM-5:05AM ET\",\"Redeem\","
            "\"3.14\",\"3.14\",\"Up\",\"1787821599\",\"0xdef\"\n"
            "\"Bitcoin Up or Down - August 27, 3AM ET\",\"Buy\","
            "\"10.15\",\"12.97\",\"Up\",\"1787816587\",\"0xhhh\"\n"
        )
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "h.csv"
            path.write_text(raw, encoding="utf-8")
            rows = load_csv(path)
        names = {r["market"]: series_of(r["market"]) for r in rows}
        self.assertEqual(
            names["Bitcoin Up or Down - August 27, 5:00AM-5:05AM ET"], "5m"
        )
        self.assertEqual(
            names["Bitcoin Up or Down - August 27, 3AM ET"], "hourly"
        )
        mk = session_markets(rows, restart_ts=1787821036, year=2026)
        self.assertEqual(len(mk), 1)
        self.assertEqual(len(mk[0]["buys"]), 1)
        self.assertEqual(len(mk[0]["redeems"]), 1)


if __name__ == "__main__":
    unittest.main()
