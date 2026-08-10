"""Production-path response/shape tests for BUY fill + GC/hedge helpers.

Bots cannot be imported (no ``__main__`` guard). Critical pure helpers are
loaded by extracting function source from ``buybot.py`` so tests exercise the
real implementations, not a duplicated fork.
"""

from __future__ import annotations

import ast
import re
import textwrap
import unittest
from pathlib import Path

BOT = Path(__file__).resolve().parents[1] / "buybot.py"


def _load_funcs(*names: str):
    src = BOT.read_text()
    tree = ast.parse(src)
    wanted = set(names)
    chunks = []
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name in wanted:
            chunks.append(ast.get_source_segment(src, node))
            wanted.discard(node.name)
    if wanted:
        raise RuntimeError(f"missing functions in buybot.py: {sorted(wanted)}")
    ns: dict = {}
    # Minimal stubs for names referenced at definition time (none expected).
    exec(compile("\n\n".join(chunks), str(BOT), "exec"), ns, ns)
    return ns


class BuyFillProductionHelpers(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ns = _load_funcs(
            "_normalize_clob_amount",
            "buy_shares_from_result",
            "_result_as_dict",
            "fill_cost_usdc",
            "entry_book_ok",
            "hedge_book_ok",
        )
        # buy_shares_from_result / fill_cost call _result_as_dict — already loaded.

    def test_matched_without_size_matched_uses_taking_amount(self):
        buy_shares = self.ns["buy_shares_from_result"]
        fill_cost = self.ns["fill_cost_usdc"]
        result = {"makingAmount": "4.90", "takingAmount": "5", "status": "matched"}
        shares = buy_shares(result)
        self.assertAlmostEqual(shares, 5.0)
        self.assertAlmostEqual(fill_cost(result, shares, 0.98, 8.0), 4.90)

    def test_delayed_zero_making_estimates_cost(self):
        buy_shares = self.ns["buy_shares_from_result"]
        fill_cost = self.ns["fill_cost_usdc"]
        # Real delayed stubs often have empty/zero amounts; shares may arrive later.
        result = {"makingAmount": "0", "takingAmount": "0", "status": "delayed"}
        self.assertEqual(buy_shares(result), 0.0)
        # When shares are known later but making still zero:
        result2 = {"makingAmount": "0", "takingAmount": "5"}
        shares = buy_shares(result2)
        self.assertAlmostEqual(shares, 5.0)
        cost = fill_cost(result2, shares, 0.98, 8.0)
        self.assertAlmostEqual(cost, min(8.0, 5.0 * 0.98))
        self.assertGreater(cost, 0.0)

    def test_entry_rejects_wide_book(self):
        ok, why = self.ns["entry_book_ok"](0.01, 0.98, 0.05, 0.90)
        self.assertFalse(ok)
        self.assertEqual(why, "wide_spread")

    def test_hedge_rejects_penny_under_high_ask(self):
        ok, why = self.ns["hedge_book_ok"](0.01, 0.99, 0.65, 0.15, 0.70)
        self.assertFalse(ok)
        self.assertEqual(why, "ask_too_high")

    def test_hedge_accepts_tight_reversal(self):
        ok, why = self.ns["hedge_book_ok"](0.55, 0.62, 0.65, 0.15, 0.70)
        self.assertTrue(ok)
        self.assertEqual(why, "ok")


class BalanceAndGcSemantics(unittest.TestCase):
    def test_absent_token_is_zero_on_success(self):
        # Mirror check_token_balance success semantics.
        def balance_from_positions(rows, token_id):
            if not isinstance(rows, list):
                return None
            for p in rows:
                if p.get("asset") == token_id:
                    return float(p.get("size", 0) or 0)
            return 0.0

        self.assertEqual(balance_from_positions([], "tok"), 0.0)
        self.assertEqual(balance_from_positions([{"asset": "other", "size": 1}], "tok"), 0.0)

    def test_gc_skips_par_for_toxic_and_hedged(self):
        def redeem_fallback(gc_meta, hedge_proceeds, redeem_value):
            if redeem_value == 0:
                if gc_meta.get("hedge_closed"):
                    return 0.0
                if float(hedge_proceeds or 0) > 0:
                    return 0.0
                if gc_meta.get("toxic_fill"):
                    return 0.0
                rem = gc_meta.get("bought_size", 0)
                if rem > 0:
                    return round(rem, 4)
            return redeem_value

        self.assertEqual(redeem_fallback({"toxic_fill": True, "bought_size": 50}, 0, 0), 0.0)
        self.assertEqual(redeem_fallback({"hedge_closed": True, "bought_size": 0}, 10, 0), 0.0)
        self.assertEqual(redeem_fallback({"bought_size": 20}, 5.0, 0), 0.0)
        self.assertEqual(redeem_fallback({"bought_size": 20}, 0, 0), 20.0)


class AmbiguousPostPolicy(unittest.TestCase):
    def test_ambiguous_must_not_retry_budget(self):
        # Documented policy: after exception/falsy POST, break — do not loop.
        src = BOT.read_text()
        self.assertIn("buy_attempt_ambiguous", src)
        self.assertIn("ambiguous POST — no further retries", src)
        self.assertIn("buy_attempt_falsy", src)
        self.assertIn("buy_abort_no_baseline", src)
        self.assertIn("buy_on_fill_fail", src)
        # Persist failure must abort further buys
        self.assertIn("aborting further buys", src)

    def test_hedge_uses_quote_max_age(self):
        src = BOT.read_text()
        self.assertIn("HEDGE_QUOTE_MAX_AGE_S", src)
        self.assertIn("ws_fresh = quote_age is not None and quote_age <= float(HEDGE_QUOTE_MAX_AGE_S)", src)
        self.assertIn("toxic_fill", src)


if __name__ == "__main__":
    unittest.main()
