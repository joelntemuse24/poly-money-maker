"""5m last-120s hybrid GTD rest (no buybot5m import)."""

from __future__ import annotations

import ast
import json
import tempfile
import unittest
from pathlib import Path

from buy.entry_rest_gtd import (
    REST_TICKS,
    ask_allows_fak_take,
    book_level_kept_for_display,
    clear_rest_meta,
    gtd_expiration,
    hybrid_late_intent,
    live_rest_from_meta,
    persist_rest_meta,
    rest_fill_state,
    rest_maker_shares,
    rest_persist_eligible,
    rest_winner_leg,
    snap_gui_to_rest_tick,
)
from buy.entry_skip import validate_late_90_start_s
from buy.probe_5m import probe_spend_usd
from buy.strategy_coherence import validate_5m_strategy_coherence

ROOT = Path(__file__).resolve().parents[1]
BOT = ROOT / "buybot5m.py"
HOURLY = ROOT / "buybothourly.py"
FIFTEEN = ROOT / "buybot.py"
PROBE = ROOT / "strategy_buy5m_probe.example.json"
HISTORICAL = ROOT / "strategy_buy5m.example.json"
HOURLY_JSON = ROOT / "strategy_buyhourly.json"
HOURLY_EXAMPLE = ROOT / "strategy_buyhourly.example.json"
FIFTEEN_PROBE = ROOT / "strategy_buy15m_probe.example.json"


def _defaults() -> dict:
    tree = ast.parse(BOT.read_text())
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == "_STRATEGY_DEFAULTS":
                    return ast.literal_eval(node.value)
    raise AssertionError("_STRATEGY_DEFAULTS not found")


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
    return src, defaults, load_fn


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
        "console": type("C", (), {"print": staticmethod(lambda *_a, **_k: None)})(),
    }
    exec(compile(load_fn, "buybot5m.py", "exec"), ns, ns)
    return ns


class SnapGuiTickTests(unittest.TestCase):
    def test_gui_099_snaps_to_099(self):
        self.assertEqual(snap_gui_to_rest_tick(0.99), 0.99)

    def test_gui_098_snaps_to_098(self):
        self.assertEqual(snap_gui_to_rest_tick(0.98), 0.98)

    def test_gui_097_snaps_to_097(self):
        self.assertEqual(snap_gui_to_rest_tick(0.97), 0.97)

    def test_gui_0994_snaps_to_099(self):
        self.assertEqual(snap_gui_to_rest_tick(0.994), 0.99)

    def test_gui_0975_tie_skips(self):
        self.assertIsNone(snap_gui_to_rest_tick(0.975))

    def test_gui_0985_tie_skips(self):
        self.assertIsNone(snap_gui_to_rest_tick(0.985))

    def test_gui_096_skips(self):
        self.assertIsNone(snap_gui_to_rest_tick(0.96))

    def test_gui_100_skips(self):
        self.assertIsNone(snap_gui_to_rest_tick(1.00))

    def test_gui_none_skips(self):
        self.assertIsNone(snap_gui_to_rest_tick(None))


class WinnerLegTests(unittest.TestCase):
    def test_locked_up_bid_099_no_ask_down_penny(self):
        leg, tick, why = rest_winner_leg(
            up_gui=0.99,
            dn_gui=0.01,
            up_bid=0.99,
            up_ask=None,
            up_last=0.99,
            dn_bid=None,
            dn_ask=0.01,
            dn_last=0.01,
        )
        self.assertEqual((leg, tick, why), ("up", 0.99, "ok"))

    def test_missing_other_gui_winner_bid_at_tick(self):
        leg, tick, why = rest_winner_leg(
            up_gui=0.99,
            dn_gui=None,
            up_bid=0.99,
            up_ask=None,
            up_last=0.99,
            dn_bid=None,
            dn_ask=0.01,
            dn_last=None,
        )
        self.assertEqual((leg, tick, why), ("up", 0.99, "ok"))

    def test_both_legs_in_band_skip(self):
        leg, tick, why = rest_winner_leg(
            up_gui=0.98,
            dn_gui=0.97,
            up_bid=0.98,
            up_ask=0.99,
            dn_bid=0.97,
            dn_ask=0.98,
        )
        self.assertIsNone(leg)
        self.assertIsNone(tick)
        self.assertEqual(why, "ambiguous")

    def test_gui_096_skip(self):
        leg, tick, why = rest_winner_leg(
            up_gui=0.96,
            dn_gui=0.04,
            up_bid=0.96,
            up_ask=0.97,
            dn_ask=0.04,
        )
        self.assertIsNone(leg)
        self.assertEqual(why, "skip_gui")


class HybridIntentTests(unittest.TestCase):
    def _quotes(self, **kwargs):
        q = dict(
            up_gui=0.99,
            dn_gui=0.01,
            up_bid=0.99,
            up_ask=None,
            up_last=0.99,
            dn_bid=None,
            dn_ask=0.01,
            dn_last=0.01,
            up_token="UP",
            dn_token="DN",
            seconds_left=90.0,
            late_start_s=120.0,
            end_ts=1_700_000_300,
            enabled=True,
            already_filled=False,
        )
        q.update(kwargs)
        return q

    def test_missing_ask_bid_099_rests(self):
        intent = hybrid_late_intent(live=None, **self._quotes())
        self.assertEqual(intent.action, "rest")
        self.assertEqual(intent.leg, "up")
        self.assertEqual(intent.tick, 0.99)
        self.assertEqual(intent.token, "UP")
        self.assertEqual(intent.expiration, 1_700_000_300)
        self.assertIsNone(intent.cancel_order_id)

    def test_ask_at_tick_faks(self):
        intent = hybrid_late_intent(
            live=None, **self._quotes(up_ask=0.99, up_gui=0.99)
        )
        self.assertEqual(intent.action, "fak")
        self.assertEqual(intent.tick, 0.99)
        self.assertEqual(intent.fak_limit, 0.99)
        self.assertGreaterEqual(intent.fak_min, 0.97)
        self.assertLessEqual(intent.fak_min, 0.99)

    def test_ask_098_at_gui_099_still_faks_tick(self):
        intent = hybrid_late_intent(
            live=None,
            **self._quotes(up_ask=0.98, up_bid=0.97, up_gui=0.99, up_last=0.99),
        )
        self.assertEqual(intent.action, "fak")
        self.assertEqual(intent.fak_limit, 0.99)
        self.assertFalse(intent.fak_limit < 0.99)

    def test_live_gtd_blocks_same_slice_fak(self):
        live = {
            "order_id": "rest-1",
            "price": 0.99,
            "leg": "up",
            "token": "UP",
            "expiration": 1_700_000_300,
        }
        intent = hybrid_late_intent(
            live=live, **self._quotes(up_ask=0.99, up_gui=0.99)
        )
        self.assertEqual(intent.action, "keep")
        self.assertNotEqual(intent.action, "fak")

    def test_gui_97_to_98_replace(self):
        live = {
            "order_id": "rest-97",
            "price": 0.97,
            "leg": "up",
            "token": "UP",
            "expiration": 1_700_000_300,
        }
        intent = hybrid_late_intent(
            live=live,
            **self._quotes(
                up_gui=0.98, up_bid=0.98, up_ask=None, up_last=0.98,
            ),
        )
        self.assertEqual(intent.action, "replace")
        self.assertEqual(intent.tick, 0.98)
        self.assertEqual(intent.cancel_order_id, "rest-97")
        self.assertEqual(intent.expiration, 1_700_000_300)

    def test_gui_leaves_set_cancels(self):
        live = {
            "order_id": "rest-99",
            "price": 0.99,
            "leg": "up",
            "token": "UP",
            "expiration": 1_700_000_300,
        }
        intent = hybrid_late_intent(
            live=live,
            **self._quotes(up_gui=0.96, dn_gui=0.04, up_bid=0.96, up_last=0.96),
        )
        self.assertEqual(intent.action, "cancel")
        self.assertEqual(intent.cancel_order_id, "rest-99")

    def test_ttm_over_120_cancels(self):
        live = {
            "order_id": "rest-99",
            "price": 0.99,
            "leg": "up",
            "token": "UP",
            "expiration": 1_700_000_300,
        }
        intent = hybrid_late_intent(
            live=live, **self._quotes(seconds_left=121.0)
        )
        self.assertEqual(intent.action, "cancel")
        self.assertEqual(intent.cancel_order_id, "rest-99")

    def test_become_loser_cancels(self):
        live = {
            "order_id": "rest-up",
            "price": 0.99,
            "leg": "up",
            "token": "UP",
            "expiration": 1_700_000_300,
        }
        intent = hybrid_late_intent(
            live=live,
            **self._quotes(
                up_gui=0.01, dn_gui=0.99, up_bid=0.01, up_ask=0.02,
                dn_bid=0.99, dn_ask=None, dn_last=0.99,
            ),
        )
        self.assertEqual(intent.action, "cancel")
        self.assertEqual(intent.cancel_order_id, "rest-up")

    def test_one_entry_live_gtd_no_second_rest(self):
        live = {
            "order_id": "rest-99",
            "price": 0.99,
            "leg": "up",
            "token": "UP",
            "expiration": 1_700_000_300,
        }
        intent = hybrid_late_intent(live=live, **self._quotes())
        self.assertEqual(intent.action, "keep")

    def test_already_filled_does_not_rest(self):
        intent = hybrid_late_intent(
            live=None, **self._quotes(already_filled=True)
        )
        self.assertEqual(intent.action, "skip")
        self.assertEqual(intent.why, "already_filled")

    def test_knob_off_skips_and_cancels_live(self):
        live = {
            "order_id": "rest-99",
            "price": 0.99,
            "leg": "up",
            "token": "UP",
            "expiration": 1_700_000_300,
        }
        intent = hybrid_late_intent(
            live=live, **self._quotes(enabled=False)
        )
        self.assertEqual(intent.action, "cancel")
        self.assertEqual(intent.cancel_order_id, "rest-99")


class AskTakeAndPersistTests(unittest.TestCase):
    def test_ask_at_tick_allows_fak(self):
        self.assertTrue(ask_allows_fak_take(0.99, 0.99))
        self.assertTrue(ask_allows_fak_take(0.98, 0.99))
        self.assertTrue(ask_allows_fak_take(0.97, 0.99))
        self.assertFalse(ask_allows_fak_take(None, 0.99))
        self.assertFalse(ask_allows_fak_take(1.00, 0.99))
        self.assertFalse(ask_allows_fak_take(0.96, 0.99))

    def test_persist_arms_from_bid_or_last_without_ask(self):
        self.assertTrue(rest_persist_eligible(tick=0.99, bid=0.99, ask=None, last=None, gui=0.99))
        self.assertTrue(rest_persist_eligible(tick=0.99, bid=None, ask=None, last=0.99, gui=0.99))
        self.assertTrue(rest_persist_eligible(tick=0.99, bid=0.99, ask=None, last=0.99, gui=None))
        self.assertFalse(rest_persist_eligible(tick=0.99, bid=0.90, ask=None, last=0.90, gui=0.90))

    def test_missing_rest_ask_does_not_reset_persist(self):
        self.assertTrue(
            rest_persist_eligible(tick=0.99, bid=0.99, ask=None, last=0.99, gui=0.99)
        )


class MakerCentsTests(unittest.TestCase):
    def test_exact_cents_at_97_98_99(self):
        for tick, notional in ((0.99, 1.98), (0.98, 1.96), (0.97, 1.94)):
            shares = rest_maker_shares(2.0, tick, share_cap=3.0)
            self.assertEqual(shares, 2.0)
            maker = round(shares * tick, 4)
            self.assertEqual(maker, notional)
            self.assertGreaterEqual(maker, 1.0)
            self.assertNotEqual(maker, 1.01)
            self.assertEqual(round(maker, 2), maker)

    def test_gtd_expiration_is_end_ts(self):
        self.assertEqual(gtd_expiration(1_700_000_300), 1_700_000_300)
        self.assertIsNone(gtd_expiration(None))
        self.assertIsNone(gtd_expiration(0))


class RestMetaAndFillTests(unittest.TestCase):
    def test_persist_and_clear_rest_fields(self):
        meta = {}
        persist_rest_meta(
            meta,
            order_id="abc",
            price=0.99,
            leg="up",
            token="UP",
            expiration=1_700_000_300,
            size=2.0,
        )
        live = live_rest_from_meta(meta)
        self.assertEqual(live["order_id"], "abc")
        self.assertEqual(live["price"], 0.99)
        self.assertEqual(live["leg"], "up")
        self.assertEqual(live["token"], "UP")
        self.assertEqual(live["expiration"], 1_700_000_300)
        clear_rest_meta(meta)
        self.assertIsNone(live_rest_from_meta(meta))

    def test_fill_from_matched_or_balance(self):
        self.assertEqual(rest_fill_state("matched", size_matched=2.0, token_delta=0.0), "filled")
        self.assertEqual(rest_fill_state("live", size_matched=0.0, token_delta=2.0), "filled")
        self.assertEqual(rest_fill_state("live", size_matched=0.0, token_delta=0.0), "live")
        self.assertEqual(rest_fill_state("canceled", size_matched=0.0, token_delta=0.0), "dead_empty")

    def test_dollar_offer_is_display_only(self):
        self.assertTrue(book_level_kept_for_display(1.00))
        self.assertTrue(book_level_kept_for_display(0.99))
        self.assertFalse(book_level_kept_for_display(0.0))
        self.assertIsNone(snap_gui_to_rest_tick(1.00))


class Buybot5mWiringTests(unittest.TestCase):
    def test_defaults_and_examples_keep_knob_off(self):
        defaults = _defaults()
        self.assertIs(defaults["entry_rest_gtd"], False)
        probe = json.loads(PROBE.read_text())
        hist = json.loads(HISTORICAL.read_text())
        self.assertIs(probe["entry_rest_gtd"], False)
        self.assertIs(hist["entry_rest_gtd"], False)

    def test_load_strategy_accepts_knob(self):
        _src, defaults, load_fn = _extract_load_strategy()
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = Path(tmp.name) / "strategy_buy5m.json"
        payload = json.loads(PROBE.read_text())
        payload["entry_rest_gtd"] = True
        path.write_text(json.dumps(payload))
        ns = _load_ns(defaults, load_fn, path)
        cfg = ns["load_strategy"]()
        self.assertIs(cfg["entry_rest_gtd"], True)

    def test_bot_wires_hybrid_and_never_cancel_all(self):
        src = BOT.read_text()
        self.assertIn("entry_rest_gtd", src)
        self.assertIn("hybrid_late_intent", src)
        self.assertIn("OrderType.GTD", src)
        self.assertIn("cancel_order", src)
        self.assertNotIn("cancel_all(", src)
        self.assertNotIn("client.cancel_all", src)
        self.assertNotIn("cancel_market_orders(", src)
        self.assertIn("expiration=", src)
        self.assertIn("persist_rest_meta", src)
        self.assertIn("live_rest_from_meta", src)
        self.assertIn("clear_rest_meta", src)
        self.assertIn("ENTRY_REST_GTD", src)
        self.assertIn("_tick_001.amount = 2", src)
        self.assertNotIn("user_usdc_balance=", src)
        self.assertIn("0 < price <= 1", src)

    def test_gtd_create_order_uses_end_ts(self):
        src = BOT.read_text()
        self.assertIn("order_type=OrderType.GTD", src)
        self.assertIn("gtd_expiration", src)
        self.assertIn("expiration=int(expiration)", src)
        self.assertIn("OrderPayload(orderID=", src)

    def test_hourly_and_15m_untouched(self):
        hourly_src = HOURLY.read_text()
        fifteen_src = FIFTEEN.read_text()
        self.assertNotIn("entry_rest_gtd", hourly_src)
        self.assertNotIn("entry_rest_gtd", fifteen_src)
        self.assertNotIn("hybrid_late_intent", hourly_src)
        self.assertNotIn("hybrid_late_intent", fifteen_src)
        hourly = json.loads(HOURLY_EXAMPLE.read_text())
        live = json.loads(HOURLY_JSON.read_text())
        fifteen = json.loads(FIFTEEN_PROBE.read_text())
        self.assertNotIn("entry_rest_gtd", hourly)
        self.assertNotIn("entry_rest_gtd", live)
        self.assertNotIn("entry_rest_gtd", fifteen)


if __name__ == "__main__":
    unittest.main()
