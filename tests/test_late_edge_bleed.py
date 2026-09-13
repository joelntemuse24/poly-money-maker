"""Late-window |live−PTB| bleed math — no network, no bot import."""

from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from check_reversal_features import BtcSeries
from check_late_edge_bleed import score_windows
from buy.late_edge_bleed import (
    EVENT_HOUR,
    EVENT_SUMMARY,
    SCHEMA_VERSION,
    EdgeSample,
    edge_samples,
    format_hourly_slug,
    hour_report,
    iter_completed_hourly_windows,
    load_research_ptb,
    parse_hourly_slug,
    render_text,
    summarize_hours,
    upsert_hour_jsonl,
    window_stats,
)

ET = ZoneInfo("America/New_York")


def _path(start: float, end: float, live_at) -> list[tuple[float, float]]:
    """Build (ts, px) path. live_at(ts) -> price."""
    out = []
    ts = start + 1.0
    while ts <= end:
        out.append((ts, float(live_at(ts))))
        ts += 1.0
    return out


def _samples(start: float, end: float, ptb: float, live_at) -> list[EdgeSample]:
    return edge_samples(_path(start, end, live_at), start_ts=start, end_ts=end, ptb=ptb)


class SlugAndWindowTests(unittest.TestCase):
    def test_parse_format_roundtrip_8am(self):
        slug = "bitcoin-up-or-down-september-13-2026-8am-et"
        start, end = parse_hourly_slug(slug)
        self.assertEqual(end - start, 3600.0)
        dt = datetime.fromtimestamp(start, tz=ET)
        self.assertEqual((dt.year, dt.month, dt.day, dt.hour), (2026, 9, 13, 8))
        self.assertEqual(format_hourly_slug(start), slug)

    def test_noon_and_midnight(self):
        noon = parse_hourly_slug("bitcoin-up-or-down-september-5-2026-12pm-et")
        midnight = parse_hourly_slug("bitcoin-up-or-down-september-5-2026-12am-et")
        self.assertEqual(
            datetime.fromtimestamp(noon[0], tz=ET).hour, 12
        )
        self.assertEqual(
            datetime.fromtimestamp(midnight[0], tz=ET).hour, 0
        )
        self.assertEqual(
            format_hourly_slug(noon[0]),
            "bitcoin-up-or-down-september-5-2026-12pm-et",
        )
        self.assertEqual(
            format_hourly_slug(midnight[0]),
            "bitcoin-up-or-down-september-5-2026-12am-et",
        )

    def test_parse_rejects_garbage(self):
        self.assertIsNone(parse_hourly_slug("btc-updown-5m-1784638800"))
        self.assertIsNone(parse_hourly_slug(""))

    def test_last_n_completed_et_hours(self):
        now = datetime(2026, 9, 13, 8, 15, tzinfo=ET)
        wins = iter_completed_hourly_windows(now=now, last_n=3)
        slugs = [w[0] for w in wins]
        self.assertEqual(
            slugs,
            [
                "bitcoin-up-or-down-september-13-2026-5am-et",
                "bitcoin-up-or-down-september-13-2026-6am-et",
                "bitcoin-up-or-down-september-13-2026-7am-et",
            ],
        )

    def test_today_excludes_open_hour(self):
        now = datetime(2026, 9, 13, 2, 10, tzinfo=ET)
        wins = iter_completed_hourly_windows(now=now, today=True)
        slugs = [w[0] for w in wins]
        self.assertEqual(
            slugs,
            [
                "bitcoin-up-or-down-september-13-2026-12am-et",
                "bitcoin-up-or-down-september-13-2026-1am-et",
            ],
        )


class SampleSplitTests(unittest.TestCase):
    def test_edge_samples_skip_open_print_and_sign(self):
        # start live == ptb would be a zero-edge open print; first kept ts is start+1
        samples = _samples(0.0, 10.0, 100.0, lambda ts: 100.0 + ts)
        self.assertEqual(samples[0].ts, 1.0)
        self.assertEqual(samples[-1].ts, 10.0)
        self.assertAlmostEqual(samples[0].edge_usd, 1.0)
        self.assertAlmostEqual(samples[-1].ttm_s, 0.0)

    def test_late_cutoff_is_ttm_le_120(self):
        start, end, ptb = 0.0, 3600.0, 50_000.0
        samples = _samples(start, end, ptb, lambda ts: ptb + 10.0)
        early = [s for s in samples if s.ttm_s > 120.0]
        late = [s for s in samples if s.ttm_s <= 120.0]
        self.assertEqual(len(early), 3479)  # ts 1..3479, ttm 3599..121
        self.assertEqual(len(late), 121)  # ts 3480..3600, ttm 120..0
        self.assertTrue(all(s.ttm_s > 120.0 for s in early))
        self.assertTrue(all(s.ttm_s <= 120.0 for s in late))


class BleedComparisonTests(unittest.TestCase):
    def test_late_dump_from_stable_peak_is_late_adverse(self):
        """Joel shape: ~$100 favor most of the hour, last 2m bleeds to ~$50."""
        start, end, ptb = 0.0, 3600.0, 100_000.0

        def live(ts: float) -> float:
            if ts <= 3480.0:
                return ptb - 100.0
            # linear -100 → -50 over last 120s
            frac = (ts - 3480.0) / 120.0
            return ptb - (100.0 - 50.0 * frac)

        report = hour_report(
            _samples(start, end, ptb, live),
            slug="bitcoin-up-or-down-september-13-2026-8am-et",
            start_ts=start,
            end_ts=end,
            ptb=ptb,
            ptb_source="test",
            tape="synthetic_1s",
        )
        self.assertTrue(report["ok"])
        self.assertEqual(report["event"], EVENT_HOUR)
        self.assertEqual(report["schema"], SCHEMA_VERSION)
        self.assertEqual(report["peak_side"], "down")
        self.assertAlmostEqual(report["early"]["peak_abs"], 100.0, places=4)
        self.assertAlmostEqual(report["late"]["last_abs"], 50.0, places=4)
        self.assertGreater(report["late_bleed"], report["early_bleed"])
        self.assertTrue(report["max_adverse_in_late"])
        self.assertLess(report["late"]["abs_change"], 0.0)
        self.assertAlmostEqual(report["early"]["abs_change"], 0.0, places=4)

    def test_early_fade_then_flat_late_is_not_late_adverse(self):
        start, end, ptb = 0.0, 3600.0, 100_000.0

        def live(ts: float) -> float:
            if ts <= 3480.0:
                # -100 → -40 over the first 58m
                frac = (ts - 1.0) / 3479.0
                return ptb - (100.0 - 60.0 * frac)
            return ptb - 40.0

        report = hour_report(
            _samples(start, end, ptb, live),
            slug="x",
            start_ts=start,
            end_ts=end,
            ptb=ptb,
            ptb_source="test",
            tape="synthetic_1s",
        )
        self.assertTrue(report["ok"])
        self.assertGreater(report["early_bleed"], report["late_bleed"])
        self.assertFalse(report["max_adverse_in_late"])

    def test_late_flip_through_flat_counts_as_late_adverse(self):
        start, end, ptb = 0.0, 3600.0, 100_000.0

        def live(ts: float) -> float:
            if ts <= 3480.0:
                return ptb - 80.0
            frac = (ts - 3480.0) / 120.0
            return ptb - 80.0 + 100.0 * frac  # -80 → +20

        report = hour_report(
            _samples(start, end, ptb, live),
            slug="x",
            start_ts=start,
            end_ts=end,
            ptb=ptb,
            ptb_source="test",
            tape="synthetic_1s",
        )
        self.assertTrue(report["max_adverse_in_late"])
        self.assertGreater(report["late_bleed"], 80.0)
        self.assertEqual(report["close_side"], "up")
        self.assertEqual(report["peak_side"], "down")

    def test_sparse_tape_is_not_ok(self):
        samples = [
            EdgeSample(ts=10.0, ttm_s=3590.0, live_btc=100.0, edge_usd=1.0),
            EdgeSample(ts=3500.0, ttm_s=100.0, live_btc=102.0, edge_usd=3.0),
        ]
        report = hour_report(
            samples,
            slug="x",
            start_ts=0.0,
            end_ts=3600.0,
            ptb=99.0,
            ptb_source="test",
            tape="sparse",
        )
        self.assertFalse(report["ok"])
        self.assertEqual(report["reason"], "sparse_tape")

    def test_window_stats_peak_and_median(self):
        samples = [
            EdgeSample(ts=1, ttm_s=9, live_btc=12, edge_usd=2.0),
            EdgeSample(ts=2, ttm_s=8, live_btc=15, edge_usd=5.0),
            EdgeSample(ts=3, ttm_s=7, live_btc=11, edge_usd=1.0),
        ]
        stats = window_stats(samples)
        self.assertEqual(stats["n"], 3)
        self.assertAlmostEqual(stats["peak_abs"], 5.0)
        self.assertAlmostEqual(stats["min_abs"], 1.0)
        self.assertAlmostEqual(stats["median_abs"], 2.0)
        self.assertAlmostEqual(stats["abs_change"], -1.0)


class SummaryAndArtifactTests(unittest.TestCase):
    def _hour(self, slug: str, late_adverse: bool, late_chg: float, early_chg: float) -> dict:
        return {
            "event": EVENT_HOUR,
            "schema": SCHEMA_VERSION,
            "ok": True,
            "slug": slug,
            "max_adverse_in_late": late_adverse,
            "late": {"abs_change": late_chg, "peak_abs": 80.0, "median_abs": 60.0},
            "early": {"abs_change": early_chg, "peak_abs": 100.0, "median_abs": 90.0},
            "late_bleed": 40.0 if late_adverse else 5.0,
            "early_bleed": 5.0 if late_adverse else 40.0,
        }

    def test_summarize_rates_and_medians(self):
        hours = [
            self._hour("a", True, -30.0, 0.0),
            self._hour("b", True, -20.0, -5.0),
            self._hour("c", False, -2.0, -40.0),
            {"ok": False, "reason": "sparse_tape", "slug": "d"},
        ]
        summary = summarize_hours(hours)
        self.assertEqual(summary["event"], EVENT_SUMMARY)
        self.assertEqual(summary["n_hours"], 3)
        self.assertEqual(summary["n_skipped"], 1)
        self.assertAlmostEqual(summary["pct_max_adverse_in_late"], 2 / 3)
        self.assertAlmostEqual(summary["median_late_abs_change"], -20.0)
        self.assertAlmostEqual(summary["median_early_abs_change"], -5.0)

    def test_render_mentions_rates(self):
        hours = [self._hour("a", True, -30.0, 0.0)]
        text = render_text(hours, summarize_hours(hours))
        self.assertIn("pct_max_adverse_in_late", text)
        self.assertIn("a", text)
        self.assertIn("LATE", text)

    def test_load_research_ptb_prefers_capture(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "research.jsonl"
            rows = [
                {"event": "buy_fill", "slug": "s1", "ptb": 1.0},
                {"event": "ptb_capture", "slug": "s1", "ptb": 76792.32, "source": "binance_btcusdt"},
                {"event": "ptb_capture", "slug": "s2", "ptb": 10.0, "source": "x"},
            ]
            path.write_text("".join(json.dumps(r) + "\n" for r in rows))
            out = load_research_ptb(path)
            self.assertAlmostEqual(out["s1"][0], 76792.32)
            self.assertEqual(out["s1"][1], "binance_btcusdt")
            self.assertEqual(out["s2"][0], 10.0)

    def test_upsert_jsonl_keeps_latest_slug(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "late_edge_bleed.jsonl"
            first = {"event": EVENT_HOUR, "slug": "a", "late_bleed": 1.0}
            second = {"event": EVENT_HOUR, "slug": "a", "late_bleed": 9.0}
            other = {"event": EVENT_HOUR, "slug": "b", "late_bleed": 3.0}
            upsert_hour_jsonl(path, [first, other])
            upsert_hour_jsonl(path, [second])
            rows = [json.loads(line) for line in path.read_text().splitlines() if line]
            by = {r["slug"]: r for r in rows}
            self.assertEqual(len(rows), 2)
            self.assertEqual(by["a"]["late_bleed"], 9.0)
            self.assertEqual(by["b"]["late_bleed"], 3.0)

    def test_score_windows_uses_research_ptb(self):
        start, end = parse_hourly_slug("bitcoin-up-or-down-september-13-2026-8am-et")
        assert start is not None
        ts = []
        px = []
        t = int(start) + 1
        while t <= int(end):
            live = 76_692.32 if t <= int(end) - 120 else 76_742.32
            ts.append(t)
            px.append(live)
            t += 1
        btc = BtcSeries(ts=ts, px=px)
        slug = "bitcoin-up-or-down-september-13-2026-8am-et"
        rows = score_windows(
            [(slug, start, end)],
            btc=btc,
            research_ptb={slug: (76_792.32, "binance_btcusdt")},
            ptb_override=None,
            interval="1s",
            late_ttm_s=120.0,
        )
        self.assertEqual(len(rows), 1)
        self.assertTrue(rows[0]["ok"])
        self.assertEqual(rows[0]["ptb_source"], "binance_btcusdt")
        self.assertAlmostEqual(rows[0]["early"]["peak_abs"], 100.0, places=2)
        self.assertTrue(rows[0]["max_adverse_in_late"])


if __name__ == "__main__":
    unittest.main()
