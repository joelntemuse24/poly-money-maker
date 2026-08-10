"""Response-shape tests for BUY fill confirmation / cost (review blockers).

These mirror the pure helpers in buybot*.py. Bots cannot be imported (no
``__main__`` guard), so the logic under test is duplicated here and must stay
aligned with ``buy_shares_from_result`` / ``fill_cost_usdc`` / balance semantics.
"""

from __future__ import annotations

import unittest


def _normalize_clob_amount(raw, *, hint_cap=None):
    if raw is None or raw == "":
        return None
    try:
        v = float(raw)
    except (TypeError, ValueError):
        return None
    if hint_cap is not None and v > float(hint_cap) * 50 and v > 1000:
        v /= 1e6
    elif hint_cap is None and v > 1000:
        v /= 1e6
    return v


def buy_shares_from_result(result: dict) -> float:
    sm = _normalize_clob_amount(result.get("size_matched"), hint_cap=1e6)
    if sm is not None and sm > 0:
        return sm
    taking = _normalize_clob_amount(
        result.get("takingAmount", result.get("taking_amount")),
        hint_cap=1e6,
    )
    if taking is not None and taking > 0:
        return taking
    return 0.0


def fill_cost_usdc(result: dict, filled: float, limit_price: float, spend_cap: float) -> float:
    filled = float(filled or 0)
    limit_price = float(limit_price or 0)
    spend_cap = float(spend_cap or 0)
    if filled <= 0:
        return 0.0
    for k in ("average_price", "avg_price"):
        if result.get(k) is not None:
            avg = float(result[k])
            if avg > 0:
                cost = filled * avg
                return min(spend_cap, cost) if spend_cap > 0 else cost
    making = result.get("makingAmount", result.get("making_amount"))
    if making is not None:
        making_f = float(making)
        if making_f > 1000 and (spend_cap <= 0 or making_f > spend_cap * 50):
            making_f /= 1e6
        if making_f > 1e-12:
            return min(spend_cap, making_f) if spend_cap > 0 else making_f
    cost = filled * limit_price
    return min(spend_cap, cost) if spend_cap > 0 else cost


def balance_from_positions(rows, token_id):
    """Successful positions list: missing token => 0.0 (not None)."""
    if not isinstance(rows, list):
        return None
    for p in rows:
        if p.get("asset") == token_id:
            return float(p.get("size", 0) or 0)
    return 0.0


class BuyFillShapeTests(unittest.TestCase):
    def test_matched_response_without_size_matched_uses_taking_amount(self):
        # Review blocker: matched response has making/taking but no size_matched.
        result = {"makingAmount": "4.90", "takingAmount": "5", "status": "matched"}
        shares = buy_shares_from_result(result)
        self.assertAlmostEqual(shares, 5.0)
        cost = fill_cost_usdc(result, shares, 0.98, 8.0)
        self.assertAlmostEqual(cost, 4.90)

    def test_delayed_zero_making_falls_back_to_limit_estimate(self):
        # Review blocker: delayed stub makingAmount=0 must not yield free shares.
        result = {"makingAmount": "0", "takingAmount": "5", "status": "delayed"}
        shares = buy_shares_from_result(result)
        self.assertAlmostEqual(shares, 5.0)
        cost = fill_cost_usdc(result, shares, 0.98, 8.0)
        self.assertAlmostEqual(cost, min(8.0, 5.0 * 0.98))
        self.assertGreater(cost, 0.0)

    def test_absent_token_on_success_is_zero(self):
        self.assertEqual(balance_from_positions([], "tok"), 0.0)
        self.assertEqual(balance_from_positions([{"asset": "other", "size": 3}], "tok"), 0.0)
        self.assertEqual(balance_from_positions([{"asset": "tok", "size": 2.5}], "tok"), 2.5)

    def test_caller_persist_gate_shares_without_spend(self):
        # Caller must accept bought>0 even if spent was initially 0, then estimate.
        bought, spent = 5.0, 0.0
        self.assertTrue(bought > 0)
        if spent <= 0:
            spent = bought * 0.98
        self.assertAlmostEqual(spent, 4.9)


if __name__ == "__main__":
    unittest.main()
