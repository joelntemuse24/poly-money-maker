"""Join live fills to pathlog ticks without requiring slug on buy_fill."""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from check_last120_tick_autopsy import (
    NAMED_LOSS_SLUGS,
    book_stats_after,
    build_report,
    collect_fills,
    extract_json_obj,
    fill_avg,
    index_tick_headers,
    pathlog_gui_ok,
    repo_from_argv,
    resolve_slug,
    slug_from_start_ts,
    tick_density,
    walk_live_exit,
)


def _write_jsonl(path: Path, rows: list) -> None:
    path.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")


def _tick(ts: float, ttm: float, ua: float, da: float, ub=None, db=None):
    return {
        "e": "tick",
        "ts": ts,
        "ttm": ttm,
        "ua": ua,
        "da": da,
        "ub": ua - 0.01 if ub is None else ub,
        "db": da - 0.01 if db is None else db,
    }


class BootstrapPathTests(unittest.TestCase):
    def test_repo_flag_not_file_parent(self):
        repo = repo_from_argv(["/tmp/check_last120_tick_autopsy.py", "--repo", "/opt/poly"])
        self.assertEqual(repo, Path("/opt/poly").resolve())

    def test_cwd_when_flag_missing(self):
        self.assertEqual(repo_from_argv(["/tmp/check_last120_tick_autopsy.py"]), Path.cwd().resolve())

    def test_copied_to_tmp_imports_with_repo_flag(self):
        src = Path(__file__).resolve().parents[1] / "check_last120_tick_autopsy.py"
        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp) / "check_last120_tick_autopsy.py"
            dest.write_text(src.read_text(encoding="utf-8"), encoding="utf-8")
            proc = subprocess.run(
                [sys.executable, str(dest), "--repo", str(src.parent), "--help"],
                cwd=tmp,
                capture_output=True,
                text=True,
            )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("--since", proc.stdout)
        self.assertNotIn("No module named 'buy'", proc.stderr)


class ExtractAndSlugTests(unittest.TestCase):
    def test_prefixed_log_line(self):
        row = extract_json_obj('INFO {"event":"buy_fill","token_id":"abc"}')
        self.assertEqual(row["event"], "buy_fill")
        self.assertEqual(row["token_id"], "abc")

    def test_slug_from_start_ts(self):
        self.assertEqual(slug_from_start_ts(1787898600), "btc-updown-5m-1787898600")
        self.assertIsNone(slug_from_start_ts(None))


class JoinWithoutSlugTests(unittest.TestCase):
    def test_buy_fill_token_id_maps_through_tick_header(self):
        with tempfile.TemporaryDirectory() as tmp:
            tick_dir = Path(tmp)
            _write_jsonl(
                tick_dir / "btc-updown-5m-1787898600.jsonl",
                [
                    {
                        "e": "open",
                        "slug": "btc-updown-5m-1787898600",
                        "cid": "cond-1",
                        "start": 1787898600,
                        "end": 1787898900,
                        "up": "tok-up",
                        "dn": "tok-dn",
                        "series": "btc-up-or-down-5m",
                        "q": "t",
                    },
                    _tick(1787898780, 120, 0.82, 0.18),
                ],
            )
            index = index_tick_headers(tick_dir)
            row = {
                "event": "buy_fill",
                "ts": "2026-08-28T10:53:01",
                "token_id": "tok-dn",
                "avg_price": 0.81,
            }
            self.assertIsNone(row.get("slug"))
            self.assertEqual(resolve_slug(row, index), "btc-updown-5m-1787898600")
            fills = collect_fills([row], index, since_unix=1787851560)
            self.assertEqual(len(fills), 1)
            self.assertEqual(fills[0]["slug"], "btc-updown-5m-1787898600")
            self.assertEqual(fills[0]["leg"], "down")
            self.assertAlmostEqual(fills[0]["avg"], 0.81)

    def test_research_logged_at_and_start_ts(self):
        index = {
            "by_token": {},
            "by_cid": {},
            "by_start": {},
            "headers": {},
        }
        row = {
            "event": "buy_fill",
            "logged_at": 1787898780.4,
            "start_ts": 1787898600,
            "leg": "up",
            "price": 0.88,
        }
        fills = collect_fills([row], index, since_unix=1787851560)
        self.assertEqual(fills[0]["slug"], "btc-updown-5m-1787898600")
        self.assertEqual(fills[0]["leg"], "up")

    def test_since_drops_older_research(self):
        index = {"by_token": {}, "by_cid": {}, "by_start": {}, "headers": {}}
        row = {
            "event": "buy_fill",
            "logged_at": 1787800000,
            "start_ts": 1787800000,
            "price": 0.80,
        }
        self.assertEqual(collect_fills([row], index, since_unix=1787851560), [])

    def test_fill_avg_prefers_avg_price(self):
        self.assertAlmostEqual(fill_avg({"avg_price": 0.77, "price": 0.90}), 0.77)


class LiveExitWalkerTests(unittest.TestCase):
    def _loser_fade(self):
        # Fill at t=1000 / 80¢ up. Book stays 80, then 50/52 for 6s, then 20¢.
        ticks = [_tick(1000 + i, 120 - i, 0.80, 0.20, ub=0.79, db=0.19) for i in range(5)]
        for i in range(6):
            ts = 1005 + i
            ticks.append(_tick(ts, 120 - (ts - 1000), 0.52, 0.50, ub=0.50, db=0.48))
        ticks.append(_tick(1012, 108, 0.21, 0.80, ub=0.20, db=0.79))
        ticks.append({"e": "resolved", "winner": "down"})
        return ticks

    def test_persist_0_sells_first_50_52(self):
        ticks = self._loser_fade()
        out = walk_live_exit(
            ticks, "up", fill_ts=1000, persist_s=0.0, require_gui=False,
            avg=0.80, winner="down",
        )
        self.assertEqual(out["exit"], "persist")
        self.assertAlmostEqual(out["exit_bid"], 0.50)

    def test_persist_5_waits_then_sells(self):
        ticks = self._loser_fade()
        out = walk_live_exit(
            ticks, "up", fill_ts=1000, persist_s=5.0, require_gui=False,
            avg=0.80, winner="down",
        )
        self.assertEqual(out["exit"], "persist")
        self.assertAlmostEqual(out["exit_bid"], 0.50)
        # first 50/52 is t=1005; ready at t=1010
        self.assertGreaterEqual(out["pnl"], -2.5)

    def test_gui_min_edge_blocks_50_50_then_dump(self):
        """Live min_bid_edge 5¢ treats 51¢ vs 49¢ as ambiguous; dump still hits 32."""
        ticks = self._loser_fade()
        out = walk_live_exit(
            ticks, "up", fill_ts=1000, persist_s=5.0, require_gui=True,
            avg=0.80, winner="down",
        )
        self.assertEqual(out["exit"], "dump")
        self.assertAlmostEqual(out["exit_bid"], 0.20)

    def test_dump_32_beats_persist(self):
        ticks = [
            _tick(1, 100, 0.80, 0.20, ub=0.79, db=0.19),
            _tick(2, 99, 0.31, 0.70, ub=0.30, db=0.69),
        ]
        out = walk_live_exit(
            ticks, "up", fill_ts=1, persist_s=5.0, require_gui=True,
            avg=0.80, winner="down",
        )
        self.assertEqual(out["exit"], "dump")
        self.assertAlmostEqual(out["exit_bid"], 0.30)

    def test_never_prints_50_redeems_loss(self):
        ticks = [_tick(10 + i, 80 - i, 0.88, 0.12, ub=0.87, db=0.11) for i in range(20)]
        out = walk_live_exit(
            ticks, "up", fill_ts=10, persist_s=5.0, require_gui=False,
            avg=0.88, winner="down",
        )
        self.assertEqual(out["exit"], "redeem_loss")
        self.assertAlmostEqual(out["pnl"], -2.5)

    def test_book_stats_max_run(self):
        ticks = self._loser_fade()
        stats = book_stats_after(ticks, "up", 1000)
        self.assertIsNotNone(stats["first50"])
        self.assertGreaterEqual(stats["max_book_run_s"], 5.0)
        self.assertAlmostEqual(stats["min_bid"], 0.20)


class GuiProxyTests(unittest.TestCase):
    def test_50_52_vs_48_50_fails_min_edge(self):
        ok, why = pathlog_gui_ok(0.50, 0.52, 0.48, 0.50)
        self.assertFalse(ok)
        self.assertEqual(why, "ambiguous")

    def test_50_52_vs_55_57_passes(self):
        # held mid 51¢, other mid 56¢: 5¢ edge and other still ≥ 48¢.
        ok, why = pathlog_gui_ok(0.50, 0.52, 0.55, 0.57)
        self.assertTrue(ok)
        self.assertEqual(why, "ok")


class DensityTests(unittest.TestCase):
    def test_counts_ticks_and_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            tick_dir = Path(tmp)
            _write_jsonl(
                tick_dir / "btc-updown-5m-1.jsonl",
                [
                    {"e": "open", "slug": "btc-updown-5m-1"},
                    _tick(1, 10, 0.8, 0.2),
                    _tick(2, 9, 0.8, 0.2),
                ],
            )
            _write_jsonl(
                tick_dir / "btc-updown-5m-2.jsonl",
                [{"e": "open", "slug": "btc-updown-5m-2"}],
            )
            dens = tick_density(tick_dir)
            self.assertEqual(dens["n_files"], 2)
            self.assertEqual(dens["empty"], 1)
            self.assertEqual(dens["p50_ticks"], 1.0)

    def test_named_loss_list_is_56(self):
        self.assertEqual(len(NAMED_LOSS_SLUGS), 56)
        self.assertEqual(len(set(NAMED_LOSS_SLUGS)), 56)


class ReportSmokeTests(unittest.TestCase):
    def test_empty_repo_prints_join_diagnosis(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            (repo / "pathlog" / "ticks").mkdir(parents=True)
            text = build_report(repo, Path(tmp) / "out", "2026-08-27T17:26:00")
            self.assertIn("journal_unresolved_no_token_map=0", text)
            self.assertIn("merged_unique_slugs=0", text)
            self.assertIn("named_loss present=0 missing=56", text)
            self.assertIn("sample_keys: (no fill events in this source)", text)


if __name__ == "__main__":
    unittest.main()
