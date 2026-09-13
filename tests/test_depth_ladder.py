"""Depth-ladder telemetry helpers (no bot import)."""

from __future__ import annotations

import ast
import json
import tempfile
import unittest
from pathlib import Path

from buy.depth_ladder import (
    DEFAULT_BUDGETS,
    DEFAULT_TOPUP_TARGETS,
    DepthPathBuffer,
    DepthPathTracker,
    available_at_limit,
    build_depth_ladder_event,
    emit_depth_ladder_event,
    make_depth_sample,
    simulate_topup_path,
)

ROOT = Path(__file__).resolve().parents[1]
BOT = ROOT / "buybot.py"
HOURLY = ROOT / "buybothourly.py"


def _fn_source(path: Path, name: str) -> str:
    src = path.read_text()
    tree = ast.parse(src)
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return ast.get_source_segment(src, node) or ""
    raise AssertionError(f"{name} not found in {path.name}")


class DepthLadderEventTests(unittest.TestCase):
    def test_event_payload_matches_hourly_fields(self):
        asks = [(0.95, 20.0), (0.96, 10.0)]
        payload = build_depth_ladder_event(
            asks,
            0.99,
            budgets=DEFAULT_BUDGETS,
            ask=0.95,
            condition_id="cond",
            token_id="tok",
            slug="btc-updown-15m-1",
            leg="up",
            ttm=2.5,
            attempt=1,
            outcome="filled",
        )
        self.assertEqual(payload["condition_id"], "cond")
        self.assertEqual(payload["token_id"], "tok")
        self.assertEqual(payload["slug"], "btc-updown-15m-1")
        self.assertEqual(payload["leg"], "up")
        self.assertEqual(payload["ask"], 0.95)
        self.assertEqual(payload["limit"], 0.99)
        self.assertEqual(payload["ttm"], 2.5)
        self.assertEqual(payload["attempt"], 1)
        self.assertEqual(payload["outcome"], "filled")
        self.assertGreater(payload["available_notional"], 0)
        self.assertGreater(payload["max_fill_usd"], 0)
        self.assertIn("5", payload["budgets"])
        self.assertIn("20", payload["budgets"])
        self.assertIn("40", payload["budgets"])
        self.assertIn("50", payload["budgets"])
        self.assertIn("100", payload["budgets"])
        self.assertEqual(payload["budgets"]["5"]["fill_status"], "full")
        self.assertTrue(payload["top_asks"])
        self.assertIn("p", payload["top_asks"][0])
        self.assertIn("s", payload["top_asks"][0])
        self.assertIn("scale_summary", payload)
        self.assertIn("clips_above_usd", payload)

    def test_emit_logs_and_writes_jsonl(self):
        events = []

        def log_event(name, **kwargs):
            events.append((name, kwargs))

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = str(Path(tmp.name) / "depth_ladder.jsonl")
        payload = emit_depth_ladder_event(
            log_event=log_event,
            jsonl_path=path,
            asks=[(0.34, 2.0)],
            limit_price=0.40,
            budgets=(5.0, 20.0, 50.0, 100.0),
            token_id="t1",
            ask=0.34,
            outcome="order_path",
        )
        self.assertIsNotNone(payload)
        self.assertEqual(events[0][0], "buy_depth_ladder")
        logged = events[0][1]
        self.assertIn("available_notional", logged)
        self.assertIn("max_fill_usd", logged)
        self.assertIn("budgets", logged)
        self.assertIn("top_asks", logged)
        self.assertEqual(logged["budgets"]["5"]["fill_status"], "partial")
        rows = Path(path).read_text().strip().splitlines()
        self.assertEqual(len(rows), 1)
        row = json.loads(rows[0])
        self.assertEqual(row["event"], "buy_depth_ladder")
        self.assertEqual(row["token_id"], "t1")

    def test_emit_swallows_log_failures(self):
        def boom(*_a, **_k):
            raise RuntimeError("log down")

        out = emit_depth_ladder_event(
            log_event=boom,
            jsonl_path="",
            asks=[],
            limit_price=0.99,
            token_id="t1",
        )
        self.assertIsNone(out)


class DepthTopupReuseTests(unittest.TestCase):
    def test_default_budgets_include_five_and_scale_rungs(self):
        self.assertEqual(DEFAULT_BUDGETS, (5.0, 10.0, 20.0, 32.0, 40.0, 50.0, 100.0))
        self.assertEqual(DEFAULT_TOPUP_TARGETS, (20.0, 32.0, 40.0, 50.0))

    def test_two_samples_can_complete_twenty(self):
        t0 = 1000.0
        samples = [
            make_depth_sample(
                available_notional=12.0, gates_ok=True, limit=0.99,
                ts_mono=t0, ts_wall=t0,
            ),
            make_depth_sample(
                available_notional=10.0, gates_ok=True, limit=0.99,
                ts_mono=t0 + 1.0, ts_wall=t0 + 1.0,
            ),
        ]
        buf = DepthPathBuffer(maxlen=8)
        buf.extend(samples)
        sim = simulate_topup_path(buf.samples(), targets=(20.0, 50.0))
        self.assertTrue(sim["targets"]["20"]["completed"])
        self.assertFalse(sim["targets"]["50"]["completed"])

    def test_tracker_emits_topup_after_two_forced_samples(self):
        events = []
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        tracker = DepthPathTracker(
            jsonl_path=str(Path(tmp.name) / "topup.jsonl"),
            targets=(20.0, 50.0),
            sample_min_interval_s=0.0,
            emit_min_interval_s=0.0,
        )
        tracker.record_sample(
            available_notional=12.0, gates_ok=True, limit=0.99,
            token_id="tok", condition_id="cond", leg="up", force=True,
        )
        tracker.record_sample(
            available_notional=10.0, gates_ok=True, limit=0.99,
            token_id="tok", condition_id="cond", leg="up", force=True,
        )
        payload = tracker.emit_topup(
            log_event=lambda name, **kw: events.append((name, kw)),
            condition_id="cond",
            token_id="tok",
            leg="up",
            limit_price=0.99,
            outcome="filled",
            force=True,
        )
        self.assertIsNotNone(payload)
        self.assertEqual(events[0][0], "buy_depth_topup_sim")
        self.assertTrue(payload["targets"]["20"]["completed"])
        self.assertFalse(payload["targets"]["50"]["completed"])


class BuybotEmitExtractTests(unittest.TestCase):
    def test_extracted_15m_emit_logs_hourly_fields(self):
        src = BOT.read_text()
        tree = ast.parse(src)
        emit_src = None
        for node in tree.body:
            if isinstance(node, ast.FunctionDef) and node.name == "emit_buy_depth_ladder":
                emit_src = ast.get_source_segment(src, node)
        if emit_src is None:
            raise AssertionError("emit_buy_depth_ladder not found")
        events = []
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        jsonl = str(Path(tmp.name) / "depth_ladder.jsonl")
        ns = {
            "DEPTH_LADDER_BUDGETS": (5.0, 20.0, 40.0, 50.0, 100.0),
            "DEPTH_LADDER_JSONL": jsonl,
            "log_event": lambda name, **kw: events.append((name, kw)),
            "get_cached_ask_levels": lambda *_a, **_k: [(0.95, 8.0), (0.96, 4.0)],
            "get_book_quote": lambda *_a, **_k: None,
            "emit_depth_ladder_event": emit_depth_ladder_event,
            "available_at_limit": available_at_limit,
            "compute_depth_ladder": __import__(
                "buy.depth_ladder", fromlist=["compute_depth_ladder"]
            ).compute_depth_ladder,
            "_depth_path_tracker": DepthPathTracker(
                jsonl_path=str(Path(tmp.name) / "topup.jsonl"),
                targets=(20.0, 50.0),
            ),
        }
        exec(compile(emit_src, "buybot.py", "exec"), ns, ns)
        ns["emit_buy_depth_ladder"](
            token_id="tok",
            ask=0.95,
            limit_price=0.95,
            condition_id="cond",
            slug="btc-updown-15m-1",
            leg="up",
            ttm=2.4,
            attempt=1,
            outcome="filled",
        )
        names = [name for name, _kw in events]
        self.assertIn("buy_depth_ladder", names)
        payload = next(kw for name, kw in events if name == "buy_depth_ladder")
        self.assertIn("available_notional", payload)
        self.assertIn("max_fill_usd", payload)
        self.assertIn("budgets", payload)
        self.assertIn("top_asks", payload)
        self.assertIn("5", payload["budgets"])
        self.assertEqual(payload["budgets"]["5"]["fill_status"], "full")
        self.assertGreater(payload["max_fill_usd"], 5.0)
        rows = Path(jsonl).read_text().strip().splitlines()
        self.assertEqual(json.loads(rows[0])["event"], "buy_depth_ladder")

    def test_extracted_dry_buy_uses_live_ask_not_band_max(self):
        src = BOT.read_text()
        tree = ast.parse(src)
        buy_src = None
        for node in tree.body:
            if isinstance(node, ast.FunctionDef) and node.name == "buy_market_with_retry":
                buy_src = ast.get_source_segment(src, node)
        if buy_src is None:
            raise AssertionError("buy_market_with_retry not found")
        calls = []
        ns = {
            "DRY_RUN": True,
            "console": type("C", (), {"print": staticmethod(lambda *_a, **_k: None)})(),
            "log_event": lambda *_a, **_k: None,
            "get_quote_fast": lambda *_a, **_k: (0.94, 10.0, 0.96, 8.0, 0.95),
            "emit_buy_depth_ladder": lambda **kw: calls.append(kw),
        }
        exec(compile(buy_src, "buybot.py", "exec"), ns, ns)
        result = ns["buy_market_with_retry"]("tok", 5.0, 0.99, condition_id="cond")
        self.assertEqual(result, (0.0, 0.0, "dry"))
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["ask"], 0.96)
        self.assertEqual(calls[0]["limit_price"], 0.96)
        self.assertEqual(calls[0]["outcome"], "dry")


class BuybotDepthWiringTests(unittest.TestCase):
    def test_15m_buy_path_emits_shared_ladder(self):
        src = BOT.read_text()
        buy_fn = _fn_source(BOT, "buy_market_with_retry")
        self.assertIn("from buy.depth_ladder import", src)
        self.assertIn("emit_depth_ladder_event", src)
        self.assertIn("DEPTH_LADDER_BUDGETS", src)
        self.assertIn("5.0", src)
        self.assertIn("def emit_buy_depth_ladder", src)
        self.assertIn("def get_cached_ask_levels", src)
        self.assertIn("buy_data_15m/depth_ladder.jsonl", src)
        self.assertIn("emit_buy_depth_ladder(", buy_fn)
        self.assertIn('outcome="order_path"', buy_fn)
        self.assertIn('outcome="filled"', buy_fn)
        self.assertIn('outcome="no_ask"', buy_fn)
        self.assertIn("limit_price=price", buy_fn)
        self.assertIn("dry_limit = float(dry_ask)", buy_fn)
        self.assertIn("limit_price=dry_limit", buy_fn)
        self.assertIn("emit_topup", src)
        helper = (ROOT / "buy" / "depth_ladder.py").read_text()
        self.assertIn("available_notional", helper)
        self.assertIn("max_fill_usd", helper)
        self.assertIn("top_asks", helper)
        self.assertIn("buy_depth_topup_sim", helper)

    def test_hourly_still_owns_its_emit(self):
        src = HOURLY.read_text()
        self.assertIn("def emit_buy_depth_ladder", src)
        self.assertIn("compute_depth_ladder", src)
        self.assertNotIn("emit_depth_ladder_event", src)
        self.assertIn("buy_data_hourly/depth_ladder.jsonl", src)


if __name__ == "__main__":
    unittest.main()
