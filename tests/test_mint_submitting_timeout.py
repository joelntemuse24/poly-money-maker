"""Regression tests for stale tx-less submitting intents."""

from __future__ import annotations

import ast
import json
import threading
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MINT = ROOT / "mintbot.py"
MINT_EXAMPLE = ROOT / "strategy_mint.example.json"


def _assign(name: str):
    tree = ast.parse(MINT.read_text(), filename=str(MINT))
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == name:
                    return ast.literal_eval(node.value)
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            if node.target.id == name and node.value is not None:
                return ast.literal_eval(node.value)
    raise AssertionError(f"{name} not found in mintbot.py")


def _fn(name: str, extras: dict | None = None):
    tree = ast.parse(MINT.read_text(), filename=str(MINT))
    want = None
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == name:
            want = node
            break
    if want is None:
        raise AssertionError(f"{name} not found")
    ns: dict = {"time": __import__("time")}
    if extras:
        ns.update(extras)
    exec(compile(ast.Module(body=[want], type_ignores=[]), str(MINT), "exec"), ns)
    return ns[name]


class MintSubmittingTimeoutConfigTests(unittest.TestCase):
    def test_defaults_and_example_include_timeout(self):
        defaults = _assign("DEFAULTS")
        example = json.loads(MINT_EXAMPLE.read_text())
        self.assertEqual(defaults["mint_submitting_timeout_s"], 90.0)
        self.assertEqual(example["mint_submitting_timeout_s"], 90.0)


class StaleSubmittingIntentTests(unittest.TestCase):
    def _subject(self, events: list, saves: list):
        mark_failed = _fn("mark_intent_failed")
        return _fn(
            "fail_stale_submitting_intents",
            {
                "STATE_LOCK": threading.RLock(),
                "STATE_FILE": Path("/tmp/positions_mint.timeout-tests.json"),
                "mark_intent_failed": mark_failed,
                "atomic_save": lambda *_args, **_kwargs: saves.append("saved"),
                "log_event": lambda event, **kwargs: events.append((event, kwargs)),
            },
        )

    def test_stale_submitting_without_tx_is_failed(self):
        events: list = []
        saves: list = []
        fn = self._subject(events, saves)
        now = 2_000_000.0
        state = {
            "intents": {
                "cid-1": {
                    "status": "submitting",
                    "transaction_id": None,
                    "created_at": now - 300.0,
                    "updated_at": now - 200.0,
                    "mint_attempts": 2,
                    "slug": "btc-updown-15m-1",
                }
            }
        }
        changed = fn(state, {"mint_submitting_timeout_s": 120.0}, now)
        self.assertEqual(changed, 1)
        intent = state["intents"]["cid-1"]
        self.assertEqual(intent["status"], "failed")
        self.assertEqual(intent["errorMsg"], "stale_submitting_no_tx")
        self.assertEqual(intent["error"], "stale_submitting_no_tx")
        self.assertEqual(intent["last_fail_ts"], now)
        self.assertEqual(intent["mint_attempts"], 2)
        self.assertEqual(len(saves), 1)
        self.assertTrue(any(name == "mint_submitting_timeout_failed" for name, _ in events))

    def test_fresh_submitting_without_tx_is_untouched(self):
        events: list = []
        saves: list = []
        fn = self._subject(events, saves)
        now = 2_000_000.0
        state = {
            "intents": {
                "cid-1": {
                    "status": "submitting",
                    "transaction_id": "",
                    "created_at": now - 40.0,
                    "updated_at": now - 20.0,
                    "mint_attempts": 1,
                }
            }
        }
        changed = fn(state, {"mint_submitting_timeout_s": 120.0}, now)
        self.assertEqual(changed, 0)
        self.assertEqual(state["intents"]["cid-1"]["status"], "submitting")
        self.assertEqual(len(saves), 0)
        self.assertEqual(events, [])

    def test_submitting_with_transaction_id_is_untouched(self):
        events: list = []
        saves: list = []
        fn = self._subject(events, saves)
        now = 2_000_000.0
        state = {
            "intents": {
                "cid-1": {
                    "status": "submitting",
                    "transaction_id": "tx-123",
                    "created_at": now - 900.0,
                    "updated_at": now - 800.0,
                    "mint_attempts": 1,
                }
            }
        }
        changed = fn(state, {"mint_submitting_timeout_s": 120.0}, now)
        self.assertEqual(changed, 0)
        self.assertEqual(state["intents"]["cid-1"]["status"], "submitting")
        self.assertEqual(len(saves), 0)
        self.assertEqual(events, [])

    def test_timeout_zero_disables_stale_auto_fail(self):
        events: list = []
        saves: list = []
        fn = self._subject(events, saves)
        now = 2_000_000.0
        state = {
            "intents": {
                "cid-1": {
                    "status": "submitting",
                    "transaction_id": None,
                    "created_at": now - 900.0,
                    "updated_at": now - 900.0,
                    "mint_attempts": 1,
                }
            }
        }
        changed = fn(state, {"mint_submitting_timeout_s": 0.0}, now)
        self.assertEqual(changed, 0)
        self.assertEqual(state["intents"]["cid-1"]["status"], "submitting")
        self.assertEqual(len(saves), 0)
        self.assertEqual(events, [])


class MintFlowRegressionTests(unittest.TestCase):
    def test_stale_submit_cleanup_runs_before_wait_submit_gate(self):
        src = MINT.read_text()
        cycle = src[src.find("def run_mint_cycle") : src.find("\ndef _reload_cfg")]
        self.assertIn("fail_stale_submitting_intents", cycle)
        self.assertLess(
            cycle.find("fail_stale_submitting_intents"),
            cycle.find("submitting = any("),
        )
        self.assertIn('if status in ("submitting", "pending", "executed", "mined") and tx_id:', src)

    def test_submit_and_pending_paths_still_present(self):
        src = MINT.read_text()
        cycle = src[src.find("def run_mint_cycle") : src.find("\ndef _reload_cfg")]
        self.assertIn("submit_mint_batch", cycle)
        self.assertIn('intent["transaction_id"] = tx_id', cycle)
        self.assertIn('intent["status"] = "pending"', cycle)
        self.assertIn('log_event("mint_submitted"', cycle)


if __name__ == "__main__":
    unittest.main()
