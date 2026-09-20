"""Mint-only + 15m pathlog operational contracts (no bot imports)."""

from __future__ import annotations

import ast
import json
import unittest
from pathlib import Path

import pathlog


ROOT = Path(__file__).resolve().parents[1]
MINT = ROOT / "mintbot.py"
MINT_EXAMPLE = ROOT / "strategy_mint.example.json"
DEPLOY = ROOT / "deploy"
BUY = ROOT / "buy"


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


class PathlogSeriesTests(unittest.TestCase):
    def test_series_is_15m_only(self):
        self.assertEqual(pathlog.SERIES, ["btc-up-or-down-15m"])
        self.assertNotIn("btc-up-or-down-5m", pathlog.SERIES)
        self.assertNotIn("btc-up-or-down-hourly", pathlog.SERIES)
        src = (ROOT / "pathlog.py").read_text()
        self.assertIn(
            "Mint-only stack (2026-09-19): pathlog records 15m only.",
            src,
        )


class MintDefaultsTests(unittest.TestCase):
    def test_example_and_defaults_are_15m_only(self):
        example = json.loads(MINT_EXAMPLE.read_text())
        defaults = _assign("DEFAULTS")
        for blob, label in ((example, "example"), (defaults, "defaults")):
            self.assertEqual(blob["series_slugs"], ["btc-up-or-down-15m"], label)
            self.assertIs(blob["entry_enabled"], False, label)
            self.assertIs(blob["dry_run"], True, label)
            self.assertIs(blob["sell_enabled"], False, label)
        self.assertEqual(defaults["shares"], example["shares"])
        self.assertEqual(defaults["enter_max_ttm_min"], 30.0)
        self.assertEqual(example["enter_max_ttm_min"], 30.0)
        self.assertEqual(defaults["enter_max_ttm_min"], example["enter_max_ttm_min"])
        self.assertEqual(defaults["enter_min_ttm_min"], 0.0)
        self.assertEqual(defaults["mint_fail_cooldown_s"], 90.0)
        self.assertEqual(example["mint_fail_cooldown_s"], 90.0)
        self.assertEqual(defaults["mint_max_attempts"], 3)
        self.assertEqual(example["mint_max_attempts"], 3)
        self.assertEqual(defaults["max_open_sets"], example["max_open_sets"])
        for blob, label in ((example, "example"), (defaults, "defaults")):
            self.assertEqual(blob["sell_threshold"], 0.03, label)
            self.assertEqual(blob["sell_floor"], 0.02, label)
            self.assertAlmostEqual(blob["sell_opposite_min"], 0.90, msg=label)
            self.assertEqual(blob["sell_persist_s"], 5.0, label)
            self.assertEqual(blob["sell_persist_last_min_s"], 2.0, label)
            self.assertEqual(blob["sell_persist_last_min_window_s"], 60.0, label)
            self.assertEqual(blob["sell_dump_persist_s"], 2.0, label)
            self.assertAlmostEqual(blob["sell_winner_min"], 0.999, msg=label)
            self.assertEqual(blob["sell_min_bid_size"], 1.0, label)
            self.assertEqual(blob["poll_s"], 5.0, label)
            self.assertEqual(blob["sell_armed_poll_s"], 2.0, label)

    def test_open_intent_count_ignores_expired_redeem_holds(self):
        statuses = frozenset(
            {
                "submitting",
                "pending",
                "executed",
                "mined",
                "confirmed_waiting_inventory",
                "confirmed",
            }
        )
        count = _fn("open_intent_count", {"ACTIVE_STATUSES": statuses})
        now = 1_000_000.0
        state = {
            "intents": {
                "live": {"status": "confirmed", "end_ts": now + 60},
                "expired": {"status": "confirmed", "end_ts": now - 121},
                "pending": {"status": "pending", "end_ts": now + 10},
                "done": {"status": "completed", "end_ts": now + 10},
                "winner_only": {
                    "status": "confirmed",
                    "end_ts": now + 60,
                    "sold_loser": True,
                },
                "sold_leg_only": {
                    "status": "confirmed",
                    "end_ts": now + 60,
                    "sold_leg": "dn",
                },
            }
        }
        self.assertEqual(count(state, now=now), 2)


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
_START_A = 10_000.0  # 1:30
_END_A = _START_A + _WIN  # 1:45
_START_B = _END_A  # 1:45
_END_B = _START_B + _WIN  # 2:00
_START_C = _END_B  # 2:00
_NOW = _END_A - 60.0  # ~1:44, still holding 1:30–1:45


def _slots():
    extras = {"ACTIVE_STATUSES": _ACTIVE}
    return (
        _fn("open_intent_count", extras),
        _fn("mint_slots_full", extras),
        _fn("mint_discovery_capped", extras),
    )


class MintSlotChainTests(unittest.TestCase):
    def test_redeem_hold_does_not_block(self):
        count, slots, _capped = _slots()
        cfg = {"max_open_sets": 1}
        for flag in (
            {"sold_loser": True},
            {"sold_leg": "dn"},
        ):
            state = {
                "intents": {
                    "a": {
                        "status": "confirmed",
                        "start_ts": _START_A,
                        "end_ts": _END_A,
                        **flag,
                    }
                }
            }
            self.assertEqual(count(state, now=_NOW), 0, flag)
            self.assertFalse(slots(state, cfg, _NOW, _START_B), flag)

    def test_adjacent_next_window_allowed_at_cap(self):
        _, slots, capped = _slots()
        state = {
            "intents": {
                "a": {
                    "status": "confirmed",
                    "start_ts": _START_A,
                    "end_ts": _END_A,
                }
            }
        }
        self.assertFalse(slots(state, {"max_open_sets": 1}, _NOW, _START_B))
        self.assertFalse(capped(state, {"max_open_sets": 1}, _NOW))

    def test_second_lookahead_blocked_if_adjacent_already_held(self):
        _, slots, capped = _slots()
        state = {
            "intents": {
                "a": {
                    "status": "confirmed",
                    "start_ts": _START_A,
                    "end_ts": _END_A,
                },
                "b": {
                    "status": "confirmed",
                    "start_ts": _START_B,
                    "end_ts": _END_B,
                },
            }
        }
        self.assertTrue(slots(state, {"max_open_sets": 1}, _NOW, _START_C))
        self.assertTrue(capped(state, {"max_open_sets": 1}, _NOW))

    def test_non_adjacent_future_window_blocked_at_cap(self):
        _, slots, capped = _slots()
        state = {
            "intents": {
                "a": {
                    "status": "confirmed",
                    "start_ts": _START_A,
                    "end_ts": _END_A,
                }
            }
        }
        self.assertTrue(slots(state, {"max_open_sets": 1}, _NOW, _START_C))
        self.assertFalse(capped(state, {"max_open_sets": 1}, _NOW))

    def test_run_cycle_picks_then_gates_and_records_start_ts(self):
        src = MINT.read_text()
        cycle = src[src.find("def run_mint_cycle") : src.find("\ndef _reload_cfg")]
        self.assertLess(cycle.find("pick = market"), cycle.find("if mint_slots_full"))
        self.assertIn(
            "if mint_slots_full(state, cfg, now, float(pick.start_ts)):",
            cycle,
        )
        self.assertNotIn(
            'if open_intent_count(state) >= int(cfg["max_open_sets"]):',
            cycle,
        )
        self.assertGreaterEqual(cycle.count('"start_ts": pick.start_ts'), 2)
        self.assertIn("already_minted(state, market.condition_id, cfg, now)", cycle)
        self.assertIn("mint_attempts", cycle)
        self.assertIn("last_fail_ts", cycle)
        self.assertIn("_claim_mint_intent", cycle)


def _mint_market(*, start_ts: float, condition_id: str = "cid-1"):
    from buy.market import MintMarket

    return MintMarket(
        condition_id=condition_id,
        slug=f"btc-updown-15m-{int(start_ts)}",
        question="Bitcoin Up or Down",
        end_ts=start_ts + 900.0,
        series_slug="btc-up-or-down-15m",
        up_token="1",
        dn_token="2",
        active=True,
        closed=False,
        accepting_orders=True,
        neg_risk=False,
        start_ts=start_ts,
    )


_REMINT_CFG = {
    "one_entry_per_market": True,
    "mint_fail_cooldown_s": 90.0,
    "mint_max_attempts": 3,
}
_INCIDENT_CID = "btc-updown-15m-1789805700"


class MintEligibilityLeadTests(unittest.TestCase):
    def test_eligible_when_opens_in_25m_and_max_is_30(self):
        defaults = _assign("DEFAULTS")
        fn = _fn("eligible_markets")
        now = 1_000_000.0
        market = _mint_market(start_ts=now + 25.0 * 60.0, condition_id=_INCIDENT_CID)
        out = fn([market], defaults, now)
        self.assertEqual([item.condition_id for item in out], [_INCIDENT_CID])

    def test_not_eligible_when_opens_beyond_enter_max(self):
        defaults = _assign("DEFAULTS")
        fn = _fn("eligible_markets")
        now = 1_000_000.0
        market = _mint_market(start_ts=now + 30.1 * 60.0, condition_id=_INCIDENT_CID)
        self.assertEqual(fn([market], defaults, now), [])


class MintRemintTests(unittest.TestCase):
    def _already(self):
        return _fn("already_minted", {"ACTIVE_STATUSES": _ACTIVE})

    def test_failed_intent_blocked_during_cooldown(self):
        fn = self._already()
        t0 = 2_000_000.0
        state = {
            "intents": {
                _INCIDENT_CID: {
                    "status": "failed",
                    "condition_id": _INCIDENT_CID,
                    "mint_attempts": 1,
                    "last_fail_ts": t0,
                }
            }
        }
        self.assertTrue(fn(state, _INCIDENT_CID, _REMINT_CFG, now=t0 + 89.0))

    def test_failed_intent_eligible_after_90s_if_attempts_under_3(self):
        fn = self._already()
        t0 = 2_000_000.0
        state = {
            "intents": {
                _INCIDENT_CID: {
                    "status": "failed",
                    "condition_id": _INCIDENT_CID,
                    "mint_attempts": 2,
                    "last_fail_ts": t0,
                }
            }
        }
        self.assertFalse(fn(state, _INCIDENT_CID, _REMINT_CFG, now=t0 + 90.0))
        self.assertFalse(fn(state, _INCIDENT_CID, _REMINT_CFG, now=t0 + 91.0))

    def test_failed_intent_not_eligible_when_attempts_at_max(self):
        fn = self._already()
        t0 = 2_000_000.0
        state = {
            "intents": {
                _INCIDENT_CID: {
                    "status": "failed",
                    "condition_id": _INCIDENT_CID,
                    "mint_attempts": 3,
                    "last_fail_ts": t0,
                }
            }
        }
        self.assertTrue(fn(state, _INCIDENT_CID, _REMINT_CFG, now=t0 + 10_000.0))

    def test_confirmed_intent_still_blocks_remint(self):
        fn = self._already()
        cid = "btc-updown-15m-1789798500"
        cfg = dict(_REMINT_CFG)
        self.assertTrue(
            fn(
                {"intents": {cid: {"status": "confirmed", "mint_attempts": 1}}},
                cid,
                cfg,
                now=9_999_999.0,
            )
        )
        self.assertTrue(
            fn(
                {"intents": {cid: {"status": "completed"}}},
                cid,
                cfg,
                now=9_999_999.0,
            )
        )
        self.assertFalse(fn({"intents": {}}, cid, cfg, now=9_999_999.0))
        self.assertFalse(
            fn(
                {"intents": {cid: {"status": "failed", "mint_attempts": 1}}},
                cid,
                {"one_entry_per_market": False},
                now=9_999_999.0,
            )
        )


class MintFailErrorMsgTests(unittest.TestCase):
    def test_errorMsg_threaded_into_fail_state_and_log(self):
        detail = _fn("relayer_error_detail")
        record = {
            "state": "STATE_FAILED",
            "errorMsg": "relay hub: internal transaction failure",
            "transactionHash": "0xabc123",
        }
        msg, txh = detail(record)
        self.assertEqual(msg, "relay hub: internal transaction failure")
        self.assertEqual(txh, "0xabc123")

        mark = _fn("mark_intent_failed")
        intent = {"status": "pending", "mint_attempts": 1, "slug": _INCIDENT_CID}
        now = 2_000_000.0
        mark(intent, now, error_msg=msg, transaction_hash=txh)
        self.assertEqual(intent["status"], "failed")
        self.assertEqual(intent["errorMsg"], msg)
        self.assertEqual(intent["error"], msg)
        self.assertEqual(intent["last_fail_ts"], now)
        self.assertEqual(intent["transaction_hash"], txh)

        src = MINT.read_text()
        recon = src[src.find("def reconcile_intents") : src.find("\ndef acquire_lock")]
        self.assertIn("relayer_error_detail", recon)
        self.assertIn("mark_intent_failed", recon)
        self.assertIn('"mint_failed"', recon)
        self.assertIn("errorMsg=", recon)
        cycle = src[src.find("def run_mint_cycle") : src.find("\ndef _reload_cfg")]
        self.assertIn("mark_intent_failed", cycle)
        self.assertIn("last_fail_ts", cycle)
        self.assertIn("mint_attempts", cycle)


class DeployUnitsTests(unittest.TestCase):
    def test_live_units_are_mint_and_pathlog(self):
        live = {p.name for p in DEPLOY.glob("*.service")}
        self.assertEqual(live, {"polymintbot.service", "polypathlog.service"})

    def test_buybot_sources_and_units_are_gone(self):
        for name in (
            "buybot.py",
            "buybot5m.py",
            "buybothourly.py",
            "complementbot.py",
        ):
            self.assertFalse((ROOT / name).exists(), name)
        self.assertFalse((ROOT / "archive").exists())
        self.assertFalse((DEPLOY / "polybuybot.service").exists())
        self.assertFalse((DEPLOY / "polybuybot5m.service").exists())
        self.assertFalse((DEPLOY / "polybuybothourly.service").exists())
        self.assertFalse((DEPLOY / "polycomplement.service").exists())
        self.assertFalse((DEPLOY / "polydangerzone.service").exists())

    def test_buy_helpers_are_mint_and_pathlog_only(self):
        self.assertEqual(
            {p.name for p in BUY.glob("*.py")},
            {
                "__init__.py",
                "book.py",
                "chain.py",
                "contracts.py",
                "market.py",
                "mint_sell.py",
                "mint_loops.py",
            },
        )
        market_src = (BUY / "market.py").read_text()
        self.assertNotIn("def entry_seconds_left", market_src)
        self.assertNotIn("def market_is_known_for_buy", market_src)
        self.assertNotIn("def discovery_allows_buy_look", market_src)

    def test_manage_sells_is_noop_when_disabled(self):
        manage = _fn("manage_sells")
        state = {"intents": {"x": {"status": "confirmed", "end_ts": 9_999_999}}}
        manage({"sell_enabled": False}, state, object())
        self.assertNotIn("sold_leg", state["intents"]["x"])

    def test_mintbot_sell_uses_share_leg_sized_bids_and_latch(self):
        src = MINT.read_text()
        self.assertIn("parse_sell_fill_shares", src)
        self.assertIn("inventory_latch", src)
        self.assertIn("best_bid_with_min_size", src)
        self.assertIn("persist_ready", src)
        self.assertIn("loser_persist_ready", src)
        self.assertIn("winner_cashout_leg", src)
        self.assertIn("winner_cheap_decision", src)
        self.assertIn("winner_sell_limit", src)
        self.assertIn("sell_winner_cheap_denied", src)
        self.assertIn("sell_winner_cheap_allowed", src)
        self.assertIn("sell_winner_limit_clamped", src)
        self.assertIn("sell_fire_decision", src)
        self.assertIn("sell_cancel_out_of_range", src)
        self.assertIn("last_status=intent.get(\"sell_last_status\")", src)
        self.assertIn("effective_loser_persist_s", src)
        self.assertIn("sell_window_open", src)
        self.assertIn("sell_persist_effective", src)
        self.assertIn("sell_persist_last_min_s", src)
        self.assertIn("sell_persist_last_min_window_s", src)
        self.assertIn("bid_fill_depth", src)
        self.assertIn("sell_book_depth", src)
        self.assertIn("_fetch_book", src)
        self.assertNotIn("def _fetch_sized_bid", src)
        mint_sell_src = (BUY / "mint_sell.py").read_text()
        self.assertIn("def empty_fak_status", mint_sell_src)
        self.assertIn("def loser_persist_ready", mint_sell_src)
        self.assertIn("def loser_empty_keep_qualify", mint_sell_src)
        self.assertIn("def effective_loser_persist_s", mint_sell_src)
        self.assertIn("def sell_fire_decision", mint_sell_src)
        self.assertIn("def winner_sell_limit", mint_sell_src)
        self.assertIn("empty_keep_arm", mint_sell_src)
        manage = src[src.find("def manage_sells") : src.find("\ndef _claim_mint_intent")]
        self.assertIn("persist_s=loser_persist_s", manage)
        self.assertIn("persist_s=dump_persist_s", manage)
        self.assertIn("sell_fire_decision", manage)
        self.assertIn("_apply_sell_fire_cancel", manage)
        self.assertIn("loser_empty_keep_qualify", manage)
        self.assertIn("empty_keep_arm", manage)
        self.assertIn("sell_loser_leg", manage)
        self.assertNotIn("book_empty=up_bid is None or dn_bid is None", manage)
        self.assertIn('depth_path="loser"', manage)
        self.assertIn('depth_path="winner_cheap" if cheap_on else None', manage)
        self.assertIn('depth_path="dump"', manage)
        self.assertIn('phase="ready"', manage)
        self.assertNotIn("depth_at_limit >", src)
        self.assertNotIn("depth_at_limit <", src)
        self.assertNotIn("persist_s=persist_s", manage.split("loser_persist_ready")[1][:400])
        self.assertNotIn(
            'for key in ("takingAmount", "makingAmount"',
            src,
        )
        self.assertNotIn("if bal + 1e-9 < tol:", src)

    def test_sell_fire_cancel_resets_or_keeps_arm(self):
        events: list = []
        fn = _fn(
            "_apply_sell_fire_cancel",
            {
                "Optional": __import__("typing").Optional,
                "log_event": lambda event, **kwargs: events.append((event, kwargs)),
            },
        )
        loser = {"slug": "x", "sell_loser_armed_at": 10.0, "sell_loser_leg": "dn"}
        fn(
            loser,
            path="loser",
            action="cancel_reset",
            reason="loser_above_threshold",
            bid=0.04,
            cid="c",
        )
        self.assertIsNone(loser["sell_loser_armed_at"])
        self.assertIsNone(loser["sell_loser_leg"])
        self.assertEqual(events[-1][0], "sell_cancel_out_of_range")
        self.assertEqual(events[-1][1]["reason"], "loser_above_threshold")
        self.assertEqual(events[-1][1]["path"], "loser")

        keep = {"slug": "x", "sell_loser_armed_at": 10.0, "sell_loser_leg": "dn"}
        fn(
            keep,
            path="loser",
            action="cancel_keep_arm",
            reason="empty_book",
            bid=None,
            cid="c",
        )
        self.assertEqual(keep["sell_loser_armed_at"], 10.0)
        self.assertEqual(keep["sell_loser_leg"], "dn")

        dump = {"sell_dump_armed_at": 11.0}
        fn(
            dump,
            path="dump",
            action="cancel_reset",
            reason="dump_at_or_above_below",
            bid=0.81,
            cid="c",
        )
        self.assertIsNone(dump["sell_dump_armed_at"])

        winner = {"sell_winner_armed_at": 12.0}
        fn(
            winner,
            path="winner",
            action="cancel_reset",
            reason="winner_below_min",
            bid=0.97,
            cid="c",
        )
        self.assertIsNone(winner["sell_winner_armed_at"])

    def test_concurrent_loops_do_not_skip_mint_or_change_persist(self):
        src = MINT.read_text()
        defaults = _assign("DEFAULTS")
        example = json.loads(MINT_EXAMPLE.read_text())
        self.assertEqual(defaults["sell_persist_s"], 5.0)
        self.assertEqual(defaults["sell_persist_last_min_s"], 2.0)
        self.assertEqual(defaults["sell_persist_last_min_window_s"], 60.0)
        self.assertEqual(example["sell_persist_s"], 5.0)
        self.assertEqual(example["sell_persist_last_min_s"], 2.0)
        self.assertEqual(example["sell_persist_last_min_window_s"], 60.0)
        self.assertEqual(defaults["sell_dump_persist_s"], 2.0)
        self.assertEqual(example["sell_dump_persist_s"], 2.0)
        self.assertEqual(defaults["poll_s"], 5.0)
        self.assertEqual(defaults["sell_armed_poll_s"], 2.0)
        self.assertGreaterEqual(float(defaults["poll_s"]), 2.0)
        self.assertLess(float(defaults["sell_armed_poll_s"]), 2.0 + 1e-12)

        sell = src[src.find("def run_sell_cycle") : src.find("\ndef run_mint_cycle")]
        mint = src[src.find("def run_mint_cycle") : src.find("\ndef _reload_cfg")]
        self.assertIn("manage_sells", sell)
        self.assertNotIn("gateway.discover", sell)
        self.assertNotIn("submit_mint_batch", sell)
        self.assertNotIn("manage_sells", mint)
        self.assertNotIn("skip_mint_discovery_when_armed_and_capped", mint)
        self.assertNotIn('return "sell_armed"', mint)
        self.assertIn("gateway.discover", mint)
        self.assertIn("_claim_mint_intent", mint)
        self.assertIn("skip_confirmed_inventory", src)
        self.assertIn("_fetch_books", src)
        self.assertIn("ThreadPoolExecutor", src)
        self.assertIn("_io_unlocked", src)
        main = src[src.find("def main") :]
        self.assertIn("start_mint_sell_loops", main)
        self.assertIn("cycle_sleep_s", main)
        self.assertIn("mint_cycle_sleep_s", main)
        self.assertIn("sell_armed_poll_s", src)
        self.assertIn("skip_mint_discovery_for_sell", src)
        self.assertNotIn("sell_hot_poll_s", src)
        self.assertNotIn("def run_cycle", src)
        validate = src[src.find("def validate_strategy") : src.find("def eligible_markets")]
        self.assertIn("sell_armed_poll_s", validate)
        self.assertIn("poll_s must be >= 2", validate)

    def test_sell_book_depth_log_shape_is_observability_only(self):
        from buy.book import bid_fill_depth

        events: list = []
        fn = _fn(
            "_log_sell_book_depth",
            {
                "bid_fill_depth": bid_fill_depth,
                "log_event": lambda event, **kwargs: events.append((event, kwargs)),
            },
        )
        fn(
            slug="btc-updown-15m-1",
            leg="up",
            limit=0.02,
            our_size=5.0,
            bids=[
                {"price": "0.02", "size": "5"},
                {"price": "0.01", "size": "20"},
            ],
            ttm_s=30.0,
            path="loser",
            phase="ready",
            condition_id="cid",
        )
        self.assertEqual(len(events), 1)
        event, payload = events[0]
        self.assertEqual(event, "sell_book_depth")
        self.assertEqual(payload["slug"], "btc-updown-15m-1")
        self.assertEqual(payload["leg"], "up")
        self.assertEqual(payload["limit"], 0.02)
        self.assertEqual(payload["our_size"], 5.0)
        self.assertEqual(payload["best_bid"], 0.02)
        self.assertEqual(payload["depth_at_limit"], 5.0)
        self.assertEqual(payload["ladder"][1]["depth"], 25.0)
        self.assertEqual(payload["ttm_s"], 30.0)
        self.assertEqual(payload["path"], "loser")
        self.assertEqual(payload["phase"], "ready")

    def test_docs_do_not_start_hourly_dense_or_dangerzone(self):
        for path in (
            ROOT / "AGENTS.md",
            ROOT / "CURRENT.md",
            ROOT / "deploy" / "DISK_OPS.md",
        ):
            text = path.read_text()
            self.assertNotIn("systemctl start pathlog_hourly_dense", text, path.name)
            self.assertNotIn("systemctl enable polydangerzone", text, path.name)
            self.assertNotIn("systemctl start polybuybot ", text, path.name)


if __name__ == "__main__":
    unittest.main()
