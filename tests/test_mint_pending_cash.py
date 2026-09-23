"""Pending pUSD reserve so overlapping mints cannot over-commit cash."""

from __future__ import annotations

import ast
import unittest
from pathlib import Path

from buy.mint_loops import mint_cash_block, pending_mint_reserve


ROOT = Path(__file__).resolve().parents[1]
MINT = ROOT / "mintbot.py"

_ACTIVE = frozenset(
    {
        "submitting",
        "pending",
        "executed",
        "mined",
        "confirmed_waiting_inventory",
        "confirmed",
    }
)
_WIN = 900.0
_START_A = 10_000.0
_END_A = _START_A + _WIN
_START_B = _END_A
_END_B = _START_B + _WIN
_START_C = _END_B
_NOW = _END_A - 60.0


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


def _pending(condition_id: str, **extra) -> dict:
    row = {
        "status": "pending",
        "shares": 50.0,
        "condition_id": condition_id,
        "start_ts": _START_A,
        "end_ts": _END_A,
    }
    row.update(extra)
    return row


class PendingCashReserveTests(unittest.TestCase):
    def test_overlapping_mint_blocks_when_cash_covers_only_one(self):
        state = {"intents": {"cid-a": _pending("cid-a")}}
        reserved = pending_mint_reserve(state)
        self.assertEqual(reserved, 50.0)
        block = mint_cash_block(balance=50.0, need=50.0, reserved=reserved)
        self.assertIsNotNone(block)
        assert block is not None
        self.assertEqual(block["reason"], "pending_reserve")
        self.assertEqual(block["balance"], 50.0)
        self.assertEqual(block["reserved"], 50.0)
        self.assertEqual(block["free"], 0.0)
        self.assertEqual(block["need"], 50.0)

    def test_two_inflight_reserves_sum(self):
        state = {
            "intents": {
                "cid-a": _pending("cid-a", status="submitting"),
                "cid-b": _pending("cid-b", status="executed", shares=50),
            }
        }
        self.assertEqual(pending_mint_reserve(state), 100.0)
        block = mint_cash_block(100.0, 50.0, pending_mint_reserve(state))
        assert block is not None
        self.assertEqual(block["reason"], "pending_reserve")
        self.assertEqual(block["free"], 0.0)

    def test_confirm_releases_reserve_and_second_mint_is_allowed(self):
        state = {
            "intents": {
                "cid-a": _pending("cid-a", status="confirmed"),
            }
        }
        self.assertEqual(pending_mint_reserve(state), 0.0)
        self.assertIsNone(mint_cash_block(50.0, 50.0, pending_mint_reserve(state)))

    def test_hard_fail_releases_reserve(self):
        mark_failed = _fn("mark_intent_failed")
        intent = _pending("cid-a", status="pending")
        state = {"intents": {"cid-a": intent}}
        self.assertEqual(pending_mint_reserve(state), 50.0)
        mark_failed(intent, _NOW, error_msg="relayer_fail")
        self.assertEqual(intent["status"], "failed")
        self.assertEqual(pending_mint_reserve(state), 0.0)
        self.assertIsNone(mint_cash_block(50.0, 50.0, 0.0))

    def test_stale_submit_timeout_releases_reserve(self):
        mark_failed = _fn("mark_intent_failed")
        intent = _pending(
            "cid-a",
            status="submitting",
            transaction_id=None,
            updated_at=_NOW - 200.0,
        )
        state = {"intents": {"cid-a": intent}}
        self.assertEqual(pending_mint_reserve(state), 50.0)
        mark_failed(intent, _NOW, error_msg="stale_submitting_no_tx")
        self.assertEqual(pending_mint_reserve(state), 0.0)
        self.assertIsNone(mint_cash_block(50.0, 50.0, pending_mint_reserve(state)))

    def test_mined_and_waiting_inventory_still_reserve(self):
        for status in ("mined", "confirmed_waiting_inventory", "pending"):
            state = {"intents": {"cid-a": _pending("cid-a", status=status)}}
            self.assertEqual(pending_mint_reserve(state), 50.0, status)

    def test_plain_short_balance_is_not_the_reserve_reason(self):
        block = mint_cash_block(40.0, 50.0, 0.0)
        assert block is not None
        self.assertEqual(block["reason"], "no_balance")
        self.assertEqual(block["reserved"], 0.0)
        self.assertEqual(block["free"], 40.0)
        self.assertEqual(block["need"], 50.0)

    def test_max_open_sets_still_blocks_when_cash_would_allow(self):
        slots = _fn("mint_slots_full", {"ACTIVE_STATUSES": _ACTIVE})
        state = {
            "intents": {
                "cid-a": _pending("cid-a", start_ts=_START_A, end_ts=_END_A),
                "cid-b": _pending(
                    "cid-b",
                    status="pending",
                    start_ts=_START_B,
                    end_ts=_END_B,
                ),
            }
        }
        cfg = {"max_open_sets": 2}
        self.assertTrue(slots(state, cfg, _NOW, _START_C))
        # Wallet could fund a third set; the slot cap still refuses it.
        self.assertIsNone(mint_cash_block(500.0, 50.0, 0.0))
        # One in-flight bag does not fill max_open_sets=2, but its cash does.
        one = {"intents": {"cid-a": state["intents"]["cid-a"]}}
        self.assertFalse(slots(one, cfg, _NOW, _START_B))
        reserved = pending_mint_reserve(one)
        block = mint_cash_block(50.0, 50.0, reserved)
        assert block is not None
        self.assertEqual(block["reason"], "pending_reserve")

    def test_cycle_logs_pending_reserve_before_submit(self):
        src = MINT.read_text()
        cycle = src[src.find("def run_mint_cycle") : src.find("\ndef _reload_cfg")]
        self.assertIn("pending_mint_reserve", cycle)
        self.assertIn("mint_cash_block", cycle)
        self.assertIn("mint_skip_pending_reserve", cycle)
        self.assertIn("reserved", cycle)
        self.assertIn("free", cycle)
        self.assertLess(cycle.find("pending_mint_reserve"), cycle.find("submit_mint_batch"))
        self.assertLess(
            cycle.find("mint_skip_pending_reserve"),
            cycle.find("submit_mint_batch"),
        )
        self.assertLess(cycle.find("mint_slots_full"), cycle.find("pending_mint_reserve"))
        self.assertIn('log_event("mint_skip_balance"', cycle)


if __name__ == "__main__":
    unittest.main()
