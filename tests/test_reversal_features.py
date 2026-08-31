"""Unit tests for reversal-feature helpers (no network)."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from check_reversal_features import (
    BtcSeries,
    COMBO_SPECS,
    Features,
    Sample,
    breakeven_flip_rate,
    bucket,
    csv_join_report,
    first_live_shaped_touch,
    first_touch_on_path,
    features_at,
    five_m_windows,
    gate_table,
    implied_fill_px,
    kline_sample,
    late_band_touch,
    merge_klines,
    paper_ev,
    paper_redeem_pnl,
    seconds_to_cross,
    session_fill_split,
    session_markets,
    session_replay_table,
    side_of,
    skip_cost_table,
    window_path,
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

    def test_breakeven_flip_is_one_minus_fill_without_hedge(self):
        self.assertAlmostEqual(breakeven_flip_rate(0.85), 0.15, places=6)
        self.assertAlmostEqual(breakeven_flip_rate(0.75), 0.25, places=6)
        # $1 salvage on $2.50 / 85¢: ~22.7% flips still EV=0.
        self.assertAlmostEqual(breakeven_flip_rate(0.85, salvage=1.0), 0.2272727, places=4)
        self.assertAlmostEqual(paper_ev(0.85, 0.15, salvage=0.0), 0.0, places=6)

    def test_gate_table_keep_counts(self):
        rows = [
            _sample(12, False, True, -2.5),
            _sample(28, False, True, -2.5),
            _sample(45, False, False, 0.44),
            _sample(90, False, False, 0.44),
        ]
        lines = gate_table(rows, mins=(0, 20, 40), fill_px=0.85, salvage=0.0)
        body = "\n".join(lines)
        self.assertIn("0\t4\t0", body)
        self.assertIn("20\t3\t1", body)
        self.assertIn("40\t2\t2", body)

    def test_first_touch_respects_ttm_and_edge(self):
        # ttm 150 → 50 over 100s, dist from +10 to +40.
        path = []
        for ttm in range(150, 49, -1):
            ts = 1000 - ttm
            dist = 10.0 + (150 - ttm) * 0.3
            path.append((ts, float(ttm), dist, 100_000 + dist))
        miss = first_touch_on_path(path, ttm_min=0, ttm_max=120, min_abs_dist=25)
        self.assertIsNotNone(miss)
        self.assertLessEqual(miss[1], 120)
        self.assertGreaterEqual(abs(miss[2]), 25)
        late_only = first_touch_on_path(path, ttm_min=0, ttm_max=60, min_abs_dist=25)
        self.assertIsNotNone(late_only)
        self.assertLessEqual(late_only[1], 60)
        none = first_touch_on_path(path, ttm_min=0, ttm_max=120, min_abs_dist=500)
        self.assertIsNone(none)

    def test_first_touch_max_abs_dist_skips_wide_names(self):
        path = [
            (1, 90.0, 50.0, 100_050.0),
            (2, 40.0, 30.0, 100_030.0),
        ]
        hit = first_touch_on_path(
            path, ttm_min=0, ttm_max=120, min_abs_dist=15, max_abs_dist=40
        )
        self.assertIsNotNone(hit)
        self.assertEqual(hit[0], 2)
        self.assertIsNone(
            first_touch_on_path(
                path, ttm_min=0, ttm_max=120, min_abs_dist=15, max_abs_dist=25
            )
        )

    def test_live_shaped_waits_out_of_band_mid_late(self):
        path = [
            (1, 90.0, 50.0, 100_050.0),
            (2, 40.0, 50.0, 100_050.0),
        ]
        hit = first_live_shaped_touch(path)
        self.assertIsNotNone(hit)
        self.assertEqual(hit[1], 40.0)

    def test_combo_specs_include_last45_e25(self):
        names = {s.name for s in COMBO_SPECS}
        self.assertIn("last45_e25", names)
        self.assertIn("last45_e20", names)
        self.assertIn("last45_e40", names)
        last45 = next(s for s in COMBO_SPECS if s.name == "last45_e25")
        self.assertEqual(last45.ttm_max, 45.0)
        self.assertEqual(last45.min_abs_dist, 25.0)

    def test_session_replay_keeps_last45_edge(self):
        keep = _sample(80, False, False, 0.50)
        keep.feat.ttm = 40
        skip = _sample(10, False, True, -1.70)
        skip.feat.ttm = 110
        body = "\n".join(session_replay_table([keep, skip], 1.0))
        self.assertIn("all_fills\t2\t0", body)
        self.assertIn("last45_e25\t1\t1", body)

    def test_implied_fill_and_window_path(self):
        self.assertEqual(implied_fill_px(10), 0.80)
        self.assertEqual(implied_fill_px(22), 0.85)
        self.assertEqual(implied_fill_px(90), 0.94)
        ts = list(range(0, 20))
        px = [100_000.0 + i for i in ts]
        btc = BtcSeries(ts=ts, px=px)
        pack = window_path(btc, 0, 10, step=1)
        self.assertIsNotNone(pack)
        ptb, close, path = pack
        self.assertEqual(ptb, 100_000.0)
        self.assertTrue(path)
        self.assertEqual(path[0][1], 9)  # ttm at ts=1

    def test_five_m_windows_aligned(self):
        # ends 1000..1600 → first aligned end is 1200 if WINDOW_S=300
        wins = five_m_windows(1000, 1600)
        self.assertEqual(wins[0][1], 1200)
        self.assertEqual(wins[0][2], "btc-updown-5m-1200")
        self.assertEqual(wins[-1][1], 1500)

    def test_1m_kline_stamps_close_time_not_open(self):
        # Market end unix 300. TTM 45 is t=255. The last 1m bar opens at 240
        # and closes at settlement (299.999) at 99000. Open-time stamp of that
        # close would leak into TTM 45; closeTime must not.
        bars = [
            [180_000, "0", "0", "0", "100025", "0", 239_999],
            [240_000, "0", "0", "0", "99000", "0", 299_999],
        ]
        series = merge_klines([[kline_sample(bar) for bar in bars]])
        self.assertEqual(series.ts, [239, 299])
        self.assertEqual(series.at_or_before(255), 100025.0)
        self.assertEqual(series.at_or_before(299), 99000.0)
        open_stamped = merge_klines(
            [[(int(bar[0]) // 1000, float(bar[4])) for bar in bars]]
        )
        self.assertEqual(open_stamped.at_or_before(255), 99000.0)


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

    def test_generic_title_joins_via_slug(self):
        raw = (
            "marketName,action,usdcAmount,tokenAmount,tokenName,timestamp,slug\n"
            "BTC Up or Down 5m,Buy,2.50,3.00,Up,1787852000,btc-updown-5m-1787851800\n"
            "BTC Up or Down 5m,Redeem,3.00,3.00,Up,1787852200,btc-updown-5m-1787851800\n"
            "BTC Up or Down 5m,Buy,2.50,2.94,Down,1787852300,btc-updown-5m-1787852100\n"
            "BTC Up or Down 5m,Sell,0.40,2.94,Down,1787852500,btc-updown-5m-1787852100\n"
        )
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "h.csv"
            path.write_text(raw, encoding="utf-8")
            rows = load_csv(path)
        self.assertEqual(series_of(rows[0]["market"]), "unknown")
        self.assertEqual(series_of(rows[0]["slug"]), "5m")
        mk = session_markets(rows, restart_ts=1787851560, year=2026)
        self.assertEqual(len(mk), 2)
        by_start = {m["start_ts"]: m for m in mk}
        self.assertEqual(len(by_start[1787851800]["buys"]), 1)
        self.assertEqual(len(by_start[1787851800]["redeems"]), 1)
        self.assertEqual(len(by_start[1787852100]["sells"]), 1)
        report = csv_join_report(rows, mk, restart_ts=1787851560, year=2026)
        self.assertIn("session_markets=2", report)
        self.assertIn("post_restart_5m_joinable=4", report)
        self.assertIn("post_restart_series=", report)
        split = session_fill_split(mk)
        self.assertIn("SESSION fill×TTM split  n=2", split)
        self.assertIn("by fill ¢", split)

    def test_ms_timestamp_and_title_only_still_works(self):
        raw = (
            "marketName,action,usdcAmount,tokenAmount,tokenName,timestamp\n"
            "\"Bitcoin Up or Down - August 27, 5:00AM-5:05AM ET\",Buy,2.50,3.00,Up,1787821393000\n"
        )
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "h.csv"
            path.write_text(raw, encoding="utf-8")
            rows = load_csv(path)
        self.assertAlmostEqual(rows[0]["ts"], 1787821393.0)
        mk = session_markets(rows, restart_ts=1787821036, year=2026)
        self.assertEqual(len(mk), 1)


if __name__ == "__main__":
    unittest.main()
