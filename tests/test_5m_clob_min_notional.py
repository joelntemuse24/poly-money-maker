"""5m CLOB min notional: $1 @ 99¢ signs $0.99 and 400s. $1.01 is not cents."""

from __future__ import annotations

import ast
import json
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

from buy.probe_5m import (
    clob_min_slice_usd,
    min_marketable_buy_shares,
    min_marketable_buy_spend,
    raise_spend_for_clob_min_notional,
)
from buy.probe_5m import probe_spend_usd
from buy.strategy_coherence import validate_5m_strategy_coherence
from buy.entry_skip import validate_late_90_start_s

ROOT = Path(__file__).resolve().parents[1]
BOT = ROOT / "buybot5m.py"
PROBE = ROOT / "strategy_buy5m_probe.example.json"

try:
    from tests.test_buy_fill_shapes import BOT5M, _load_funcs
except ImportError:
    from test_buy_fill_shapes import BOT5M, _load_funcs


def _extract_load_strategy():
    src = BOT.read_text()
    tree = ast.parse(src)
    defaults = None
    load_fn = None
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == "_STRATEGY_DEFAULTS":
                    defaults = ast.literal_eval(node.value)
        if isinstance(node, ast.FunctionDef) and node.name == "load_strategy":
            load_fn = ast.get_source_segment(src, node)
    if defaults is None or load_fn is None:
        raise AssertionError("could not extract load_strategy")
    return defaults, load_fn


def _load_ns(defaults, load_fn, path: Path) -> dict:
    ns = {
        "os": __import__("os"),
        "json": json,
        "math": __import__("math"),
        "STRATEGY_FILE": str(path),
        "_STRATEGY_DEFAULTS": dict(defaults),
        "_STRATEGY_DOC_KEYS": {
            "_comment", "_canonical", "_source_tape", "_notes", "_live_flip",
            "_vs_15m",
        },
        "_strat_cache": None,
        "_strat_mtime": 0.0,
        "EXPECTED_TICK_SIZE": "0.001",
        "validate_5m_strategy_coherence": validate_5m_strategy_coherence,
        "validate_late_90_start_s": validate_late_90_start_s,
        "probe_spend_usd": probe_spend_usd,
        "raise_spend_for_clob_min_notional": raise_spend_for_clob_min_notional,
        "console": type("C", (), {"print": staticmethod(lambda *_a, **_k: None)})(),
    }
    exec(compile(load_fn, "buybot5m.py", "exec"), ns, ns)
    return ns


class MinMarketableAt99Tests(unittest.TestCase):
    def test_two_shares_at_99_is_198_cents(self):
        self.assertEqual(min_marketable_buy_shares(0.99), 2.0)
        self.assertEqual(min_marketable_buy_spend(0.99), 1.98)
        maker = Decimal("2.00") * Decimal("0.99")
        self.assertEqual(maker, Decimal("1.98"))
        self.assertEqual(maker.quantize(Decimal("0.01")), maker)

    def test_one_share_at_99_is_below_clob_min(self):
        maker = Decimal("1.00") * Decimal("0.99")
        self.assertEqual(maker, Decimal("0.99"))
        self.assertLess(maker, Decimal("1.00"))

    def test_dollar_oh_one_is_not_exact_cents(self):
        """1.01 × 0.99 = 0.9999 — CLOB still 400s. Do not use $1.01."""
        maker = Decimal("1.01") * Decimal("0.99")
        self.assertNotEqual(maker.quantize(Decimal("0.01")), maker)
        self.assertGreater(clob_min_slice_usd(0.99), 1.01)

    def test_json_floor_is_two_dollars_not_198(self):
        self.assertEqual(clob_min_slice_usd(0.99), 2.0)


class RaiseSpendTests(unittest.TestCase):
    def test_live_one_dollar_trial_bumps_to_two(self):
        cfg = {
            "buy_max_price": 0.99,
            "buy_threshold": 0.97,
            "buy_budget": 1.0,
            "late_buy_budget": 1.0,
            "buy_max_spend": 1.0,
            "market_spend_cap": 2.5,
            "buy_max_shares": 3.0,
        }
        changed = raise_spend_for_clob_min_notional(cfg)
        self.assertEqual(cfg["buy_budget"], 2.0)
        self.assertEqual(cfg["late_buy_budget"], 2.0)
        self.assertEqual(cfg["buy_max_spend"], 2.0)
        self.assertEqual(cfg["market_spend_cap"], 2.5)
        self.assertIn("buy_budget", changed)
        self.assertNotIn("market_spend_cap", changed)

    def test_five_dollar_probe_is_unchanged(self):
        cfg = {
            "buy_max_price": 0.99,
            "buy_threshold": 0.975,
            "buy_budget": 5.0,
            "late_buy_budget": 5.0,
            "buy_max_spend": 5.0,
            "market_spend_cap": 5.0,
            "buy_max_shares": 8.0,
        }
        changed = raise_spend_for_clob_min_notional(cfg)
        self.assertEqual(changed, {})
        self.assertEqual(cfg["buy_budget"], 5.0)
        self.assertEqual(cfg["buy_max_spend"], 5.0)


class QuotedSizeTests(unittest.TestCase):
    def _fn(self):
        ns = _load_funcs(
            "finite_float",
            "quoted_buy_shares",
            "quoted_buy_shares_up_to_limit",
            bot=BOT5M,
        )
        return ns["quoted_buy_shares_up_to_limit"]

    def test_one_dollar_cap_does_not_sign_99_cents(self):
        shares = self._fn()(1.0, 0.99, 0.99, 3.0, spend_cap=1.0)
        self.assertEqual(shares, 0.0)

    def test_two_dollar_cap_posts_two_shares_exact_cents(self):
        shares = self._fn()(2.0, 0.99, 0.99, 3.0, spend_cap=2.0)
        self.assertEqual(shares, 2.0)
        maker = Decimal(str(shares)) * Decimal("0.99")
        self.assertEqual(maker, Decimal("1.98"))
        self.assertGreaterEqual(float(maker), 1.0)
        self.assertEqual(maker.quantize(Decimal("0.01")), maker)


class LoadStrategyBumpTests(unittest.TestCase):
    def test_load_bumps_live_one_dollar_json_in_memory(self):
        defaults, load_fn = _extract_load_strategy()
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = Path(tmp.name) / "strategy_buy5m.json"
        payload = json.loads(PROBE.read_text())
        payload.update({
            "dry_run": True,
            "entry_enabled": False,
            "buy_threshold": 0.97,
            "buy_budget": 1.0,
            "late_buy_budget": 1.0,
            "buy_max_spend": 1.0,
            "buy_max_shares": 3.0,
            "market_spend_cap": 2.5,
            "entry_book_persist_s": 1.0,
        })
        path.write_text(json.dumps(payload))
        ns = _load_ns(defaults, load_fn, path)
        cfg = ns["load_strategy"]()
        self.assertEqual(cfg["buy_budget"], 2.0)
        self.assertEqual(cfg["late_buy_budget"], 2.0)
        self.assertEqual(cfg["buy_max_spend"], 2.0)
        self.assertEqual(cfg["market_spend_cap"], 2.5)
        self.assertEqual(json.loads(path.read_text())["buy_budget"], 1.0)


class DecimalRailsStayOnTests(unittest.TestCase):
    def test_tick_amount_two_and_no_fake_wallet(self):
        src = BOT.read_text()
        self.assertIn("_tick_001.amount = 2", src)
        self.assertIn("Omit user_usdc_balance", src)
        self.assertNotIn("user_usdc_balance=remaining_budget", src)

    def test_create_order_kwargs_omit_balance(self):
        tree = ast.parse(BOT.read_text())
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            name = ""
            if isinstance(func, ast.Name):
                name = func.id
            elif isinstance(func, ast.Attribute):
                name = func.attr
            if name != "create_order":
                continue
            keys = [
                kw.arg for kw in node.keywords if kw.arg
            ]
            self.assertNotIn("user_usdc_balance", keys)


if __name__ == "__main__":
    unittest.main()
