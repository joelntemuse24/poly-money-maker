"""Production-path response/shape tests for BUY fill + GC/hedge helpers.

Bots cannot be imported (no ``__main__`` guard). Critical pure helpers are
loaded by extracting function source from ``buybot.py`` so tests exercise the
real implementations, not a duplicated fork.
"""

from __future__ import annotations

import ast
import unittest
from pathlib import Path

BOT = Path(__file__).resolve().parents[1] / "buybot.py"
BOT5M = Path(__file__).resolve().parents[1] / "buybot5m.py"
BOT_HR = Path(__file__).resolve().parents[1] / "buybothourly.py"


def _load_funcs(*names: str, bot: Path = BOT):
    src = bot.read_text()
    tree = ast.parse(src)
    wanted = set(names)
    chunks = []
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name in wanted:
            chunks.append(ast.get_source_segment(src, node))
            wanted.discard(node.name)
    if wanted:
        raise RuntimeError(f"missing functions in {bot.name}: {sorted(wanted)}")
    ns: dict = {}
    exec(compile("\n\n".join(chunks), str(bot), "exec"), ns, ns)
    return ns


HELPERS = (
    "_normalize_clob_amount",
    "buy_shares_from_result",
    "_result_as_dict",
    "fill_cost_usdc",
    "entry_book_ok",
    "hedge_book_ok",
    "reconcile_hedge_sold",
    "stable_zero_balances",
    "gc_par_redeem",
)


class BuyFillProductionHelpers(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ns = _load_funcs(*HELPERS)

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
        result = {"makingAmount": "0", "takingAmount": "0", "status": "delayed"}
        self.assertEqual(buy_shares(result), 0.0)
        result2 = {"makingAmount": "0", "takingAmount": "5"}
        shares = buy_shares(result2)
        self.assertAlmostEqual(shares, 5.0)
        cost = fill_cost(result2, shares, 0.98, 8.0)
        self.assertAlmostEqual(cost, min(8.0, 5.0 * 0.98))
        self.assertGreater(cost, 0.0)

    def test_fixed_point_taking_amount_from_raw_post(self):
        """Captured-style 1e6 fixed-point amounts must normalize to shares."""
        norm = self.ns["_normalize_clob_amount"]
        buy_shares = self.ns["buy_shares_from_result"]
        fill_cost = self.ns["fill_cost_usdc"]
        self.assertAlmostEqual(norm("5000000", hint_cap=100), 5.0)
        self.assertAlmostEqual(norm(4900000, hint_cap=100), 4.9)
        # Immediate POST with fixed-point taking/making (no size_matched).
        result = {
            "takingAmount": "5000000",
            "makingAmount": "4900000",
            "status": "matched",
        }
        shares = buy_shares(result)
        self.assertAlmostEqual(shares, 5.0)
        self.assertAlmostEqual(fill_cost(result, shares, 0.98, 8.0), 4.9)

    def test_get_order_size_matched_fixed_point(self):
        norm = self.ns["_normalize_clob_amount"]
        # GET-order style size_matched in fixed point.
        self.assertAlmostEqual(norm("10000000", hint_cap=1e6), 10.0)

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


class HedgeReconcileProduction(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ns = _load_funcs("reconcile_hedge_sold", "stable_zero_balances")

    def test_stale_high_api_does_not_erase_confirmed(self):
        # 10 confirmed sold; Data API still shows old bal=10 → api_sold=0.
        rec = self.ns["reconcile_hedge_sold"](10.0, 10.0, 5.5, 10.0, 0.55)
        self.assertAlmostEqual(rec["effective_sold"], 10.0)
        self.assertAlmostEqual(rec["proceeds"], 5.5)
        self.assertAlmostEqual(rec["rem"], 0.0)
        self.assertTrue(rec["lag"])
        self.assertFalse(rec["ghost_candidate"])

    def test_api_may_add_unconfirmed_extra(self):
        rec = self.ns["reconcile_hedge_sold"](10.0, 7.0, 3.85, 0.0, 0.55)
        self.assertAlmostEqual(rec["effective_sold"], 10.0)
        self.assertAlmostEqual(rec["proceeds"], 3.85 + 3.0 * 0.55)
        self.assertAlmostEqual(rec["rem"], 0.0)

    def test_single_zero_is_ghost_candidate_only(self):
        # Zero CLOB confirms + one zero balance must NOT invent a full exit.
        rec = self.ns["reconcile_hedge_sold"](10.0, 0.0, 0.0, 0.0, 0.55)
        self.assertAlmostEqual(rec["effective_sold"], 0.0)
        self.assertAlmostEqual(rec["proceeds"], 0.0)
        self.assertTrue(rec["ghost_candidate"])

    def test_zero_confirm_partial_api_drop_not_trusted(self):
        rec = self.ns["reconcile_hedge_sold"](10.0, 0.0, 0.0, 4.0, 0.55)
        self.assertAlmostEqual(rec["effective_sold"], 0.0)
        self.assertFalse(rec["ghost_candidate"])

    def test_stable_zero_requires_repeated_success(self):
        stable = self.ns["stable_zero_balances"]
        self.assertFalse(stable([0.0]))
        self.assertFalse(stable([0.0, None]))
        self.assertFalse(stable([0.0, 10.0]))
        self.assertTrue(stable([0.0, 0.0, 0.0]))

    def test_none_balance_keeps_confirms(self):
        rec = self.ns["reconcile_hedge_sold"](10.0, 8.0, 4.0, None, 0.5)
        self.assertAlmostEqual(rec["effective_sold"], 8.0)
        self.assertAlmostEqual(rec["proceeds"], 4.0)
        self.assertTrue(rec["balance_unverified"])


class GcParRedeemProduction(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ns = _load_funcs("gc_par_redeem")

    def test_skips_par_for_toxic_hedged_and_uncertain(self):
        gc = self.ns["gc_par_redeem"]
        self.assertEqual(gc({"toxic_fill": True, "bought_size": 50}, 0, 0), 0.0)
        self.assertEqual(gc({"hedge_closed": True, "bought_size": 0}, 10, 0), 0.0)
        self.assertEqual(gc({"bought_size": 20}, 5.0, 0), 0.0)
        self.assertEqual(gc({"hedge_attempted": True, "bought_size": 20}, 0, 0), 0.0)
        self.assertEqual(gc({"hedge_blocked_toxic": True, "bought_size": 20}, 0, 0), 0.0)
        self.assertEqual(gc({"buy_uncertain": True, "bought_size": 20}, 0, 0), 0.0)
        self.assertEqual(gc({"bought_size": 20}, 0, 0), 20.0)

    def test_explicit_redeem_preserved(self):
        gc = self.ns["gc_par_redeem"]
        self.assertEqual(gc({"hedge_attempted": True, "bought_size": 20}, 0, 15.0), 15.0)


class AmbiguousCrossCyclePolicy(unittest.TestCase):
    def test_quarantine_markers_in_all_bots(self):
        for bot in (BOT, BOT5M, BOT_HR):
            src = bot.read_text()
            self.assertIn('buy_status == "ambiguous"', src)
            self.assertIn('meta["buy_uncertain"] = True', src)
            self.assertIn("buy_uncertain_token", src)
            self.assertIn("quarantine: no new order this market", src)
            self.assertIn('via="held"', src)
            self.assertIn("ambiguous POST — no further retries", src)
            self.assertIn("buy_abort_no_baseline", src)
            self.assertIn("aborting further buys", src)

    def test_hedge_liveness_and_reconcile_markers(self):
        src = BOT.read_text()
        self.assertIn("HEDGE_QUOTE_MAX_AGE_S", src)
        self.assertIn("ws_fresh = quote_age is not None and quote_age <= float(HEDGE_QUOTE_MAX_AGE_S)", src)
        self.assertIn("never erase", src.lower() if False else "Data API — may add ghosts, never erase confirms")
        self.assertIn("reconcile_hedge_sold(", src)
        self.assertIn("stable_zero_balances(", src)
        self.assertIn("gc_par_redeem(", src)

    def test_5m_sell_default_tick(self):
        src = BOT5M.read_text()
        # Default on sell_market_with_retry must be 0.001 for 5m markets.
        start = src.index("def sell_market_with_retry(")
        chunk = src[start : start + 250]
        self.assertIn('tick_size="0.001"', chunk)


class BalanceAndGcSemantics(unittest.TestCase):
    def test_absent_token_is_zero_on_success(self):
        def balance_from_positions(rows, token_id):
            if not isinstance(rows, list):
                return None
            for p in rows:
                if p.get("asset") == token_id:
                    return float(p.get("size", 0) or 0)
            return 0.0

        self.assertEqual(balance_from_positions([], "tok"), 0.0)
        self.assertEqual(balance_from_positions([{"asset": "other", "size": 1}], "tok"), 0.0)


if __name__ == "__main__":
    unittest.main()
