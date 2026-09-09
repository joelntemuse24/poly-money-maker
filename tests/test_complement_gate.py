"""Unit tests for the second-account complement buyer (no network)."""

from __future__ import annotations

import ast
import json
import unittest
from pathlib import Path

from buy.complement_gate import (
    apply_balance_evidence,
    apply_complement_outcome,
    arm_from_primary_meta,
    build_complement_clob_clients,
    complement_fill_from_post,
    complement_target_shares,
    evaluate_complement,
    funder_from_env_file,
    mark_submit_quarantine,
    other_leg_token,
    oracle_favors_other_leg,
    primary_and_complement_same_wallet,
    resolve_inflight,
    should_block_post,
)


def _meta(**kwargs):
    row = {
        "bought_token": "DOWN1",
        "bought_leg": "down",
        "bought_size": 10.87,
        "up_token": "UP1",
        "dn_token": "DOWN1",
        "end_ts": 1_800_000_000,
        "slug": "btc-updown-5m-1800000000",
        "hedge_closed": False,
    }
    row.update(kwargs)
    return row


class OtherLegTests(unittest.TestCase):
    def test_down_hold_buys_up(self):
        token, leg = other_leg_token("down", "UP1", "DOWN1")
        self.assertEqual((token, leg), ("UP1", "up"))

    def test_up_hold_buys_down(self):
        token, leg = other_leg_token("up", "UP1", "DOWN1")
        self.assertEqual((token, leg), ("DOWN1", "down"))

    def test_unknown_leg_uses_token_id(self):
        token, leg = other_leg_token("", "UP1", "DOWN1", bought_token="UP1")
        self.assertEqual((token, leg), ("DOWN1", "down"))


class ArmFromPrimaryTests(unittest.TestCase):
    def test_arms_confirmed_fill(self):
        armed = arm_from_primary_meta(
            {"c1": _meta()},
            source="5m",
            now_s=1_799_999_900,
        )
        self.assertEqual(len(armed), 1)
        self.assertEqual(armed[0].condition_id, "c1")
        self.assertEqual(armed[0].other_token, "UP1")
        self.assertEqual(armed[0].other_leg, "up")
        self.assertAlmostEqual(armed[0].held_shares, 10.87)
        self.assertEqual(armed[0].source, "5m")

    def test_skips_hedge_closed(self):
        armed = arm_from_primary_meta(
            {"c1": _meta(hedge_closed=True)},
            source="5m",
            now_s=1_799_999_900,
        )
        self.assertEqual(armed, [])

    def test_skips_flat_size(self):
        armed = arm_from_primary_meta(
            {"c1": _meta(bought_size=0.0)},
            source="5m",
            now_s=1_799_999_900,
        )
        self.assertEqual(armed, [])

    def test_skips_uncertain_without_fill(self):
        armed = arm_from_primary_meta(
            {"c1": _meta(bought_token="", bought_size=0, buy_uncertain=True)},
            source="5m",
            now_s=1_799_999_900,
        )
        self.assertEqual(armed, [])

    def test_skips_expired_past_grace(self):
        armed = arm_from_primary_meta(
            {"c1": _meta(end_ts=1000.0)},
            source="5m",
            now_s=2000.0,
            grace_s=30.0,
        )
        self.assertEqual(armed, [])

    def test_keeps_just_expired_inside_grace(self):
        armed = arm_from_primary_meta(
            {"c1": _meta(end_ts=1980.0)},
            source="5m",
            now_s=2000.0,
            grace_s=30.0,
        )
        self.assertEqual(len(armed), 1)

    def test_merges_5m_and_15m(self):
        rows = []
        rows.extend(arm_from_primary_meta({"a": _meta()}, source="5m", now_s=1))
        rows.extend(
            arm_from_primary_meta(
                {"b": _meta(bought_token="UP2", bought_leg="up", up_token="UP2", dn_token="DN2")},
                source="15m",
                now_s=1,
            )
        )
        self.assertEqual({r.source for r in rows}, {"5m", "15m"})
        self.assertEqual(rows[1].other_token, "DN2")


class TargetSharesTests(unittest.TestCase):
    def test_share_match_at_80(self):
        shares = complement_target_shares(
            10.87, ask=0.80, limit=0.99, spend_cap=16.0, share_cap=20.0,
            share_multiple=1.0,
        )
        # 10.87 × 99¢ is not exact maker cents; snap down to a legal 2dp size.
        self.assertAlmostEqual(shares, 10.00, places=2)
        self.assertLessEqual(shares * 0.99, 16.0 + 1e-9)

    def test_spend_cap_clips(self):
        shares = complement_target_shares(
            20.0, ask=0.80, limit=0.99, spend_cap=10.0, share_cap=40.0,
            share_multiple=1.0,
        )
        self.assertLessEqual(shares * 0.99, 10.0 + 1e-9)
        self.assertGreaterEqual(shares, 10.0)

    def test_zero_without_held(self):
        self.assertEqual(
            complement_target_shares(0.0, ask=0.80, limit=0.99, spend_cap=16.0),
            0.0,
        )

    def test_2x_primary_shares_for_2_50_clip_capped_near_5(self):
        """5m $2.50 @ 75¢ ≈ 3.33 sh; complement wants 2×, spend ~$5."""
        held = 2.50 / 0.75
        shares = complement_target_shares(
            held, ask=0.80, limit=0.99, spend_cap=5.0, share_cap=8.0,
        )
        self.assertGreater(shares, held + 0.01)
        self.assertLessEqual(shares * 0.99, 5.0 + 1e-9)
        self.assertGreaterEqual(shares * 0.99, 4.50)

    def test_default_multiple_is_two(self):
        one = complement_target_shares(
            3.0, ask=0.80, limit=0.99, spend_cap=16.0, share_cap=20.0,
            share_multiple=1.0,
        )
        two = complement_target_shares(
            3.0, ask=0.80, limit=0.99, spend_cap=16.0, share_cap=20.0,
        )
        self.assertAlmostEqual(two, min(6.0, one * 2.0), places=2)


class EvaluateComplementTests(unittest.TestCase):
    def test_fires_at_80_tight_book(self):
        fire, why, shares = evaluate_complement(
            other_ask=0.80,
            other_bid=0.78,
            held_shares=10.87,
            already_bought=False,
            primary_still_holding=True,
            oracle_favors_other=True,
            spend_cap=16.0,
            share_cap=20.0,
        )
        self.assertTrue(fire)
        self.assertEqual(why, "fire")
        self.assertGreaterEqual(shares, 10.0)

    def test_fires_at_95_no_ceiling(self):
        fire, why, shares = evaluate_complement(
            other_ask=0.95,
            other_bid=0.93,
            held_shares=10.87,
            already_bought=False,
            primary_still_holding=True,
            oracle_favors_other=True,
        )
        self.assertTrue(fire)
        self.assertEqual(why, "fire")

    def test_skips_below_80(self):
        fire, why, _ = evaluate_complement(
            other_ask=0.79,
            other_bid=0.77,
            held_shares=10.87,
            already_bought=False,
            primary_still_holding=True,
            oracle_favors_other=True,
        )
        self.assertFalse(fire)
        self.assertEqual(why, "ask_below_min")

    def test_skips_wide_book(self):
        fire, why, _ = evaluate_complement(
            other_ask=0.80,
            other_bid=0.10,
            held_shares=10.87,
            already_bought=False,
            primary_still_holding=True,
            oracle_favors_other=True,
        )
        self.assertFalse(fire)
        self.assertEqual(why, "wide_book")

    def test_skips_when_primary_already_sold(self):
        fire, why, _ = evaluate_complement(
            other_ask=0.80,
            other_bid=0.78,
            held_shares=10.87,
            already_bought=False,
            primary_still_holding=False,
            oracle_favors_other=True,
        )
        self.assertFalse(fire)
        self.assertEqual(why, "primary_flat")

    def test_skips_second_fill(self):
        fire, why, _ = evaluate_complement(
            other_ask=0.80,
            other_bid=0.78,
            held_shares=10.87,
            already_bought=True,
            primary_still_holding=True,
            oracle_favors_other=True,
        )
        self.assertFalse(fire)
        self.assertEqual(why, "already_bought")

    def test_skips_oracle_still_on_held_side(self):
        fire, why, _ = evaluate_complement(
            other_ask=0.80,
            other_bid=0.78,
            held_shares=10.87,
            already_bought=False,
            primary_still_holding=True,
            oracle_favors_other=False,
        )
        self.assertFalse(fire)
        self.assertEqual(why, "oracle_still_held")

    def test_oracle_favors_other_follows_last_print_check(self):
        last_print = {"ok": True, "favored": "down", "live_kind": "last_print"}
        self.assertTrue(oracle_favors_other_leg(last_print, "down"))
        self.assertFalse(oracle_favors_other_leg(last_print, "up"))
        twap_refused = {"ok": False, "favored": None, "reason": "twap_not_live"}
        self.assertFalse(oracle_favors_other_leg(twap_refused, "down"))
        fire, why, _ = evaluate_complement(
            other_ask=0.86,
            other_bid=0.84,
            held_shares=10.87,
            already_bought=False,
            primary_still_holding=True,
            oracle_favors_other=oracle_favors_other_leg(last_print, "down"),
        )
        self.assertTrue(fire)
        self.assertEqual(why, "fire")

    def test_skips_missing_ask(self):
        fire, why, _ = evaluate_complement(
            other_ask=None,
            other_bid=0.78,
            held_shares=10.87,
            already_bought=False,
            primary_still_holding=True,
            oracle_favors_other=True,
        )
        self.assertFalse(fire)
        self.assertEqual(why, "no_ask")


class _RecordingClobClient:
    """Stand-in for py_clob_client_v2.ClobClient — records constructor kwargs."""

    def __init__(self, **kwargs):
        self.kwargs = dict(kwargs)
        type(self)._calls.append(self.kwargs)

    def create_or_derive_api_key(self):
        return {"api_key": "derived", "api_secret": "s", "api_passphrase": "p"}


class ComplementClobClientBuilderTests(unittest.TestCase):
    def setUp(self):
        _RecordingClobClient._calls = []

    def test_derive_and_trading_clients_both_get_funder_and_signature_type(self):
        creds, client = build_complement_clob_clients(
            _RecordingClobClient,
            host="https://clob.polymarket.com",
            key="0xpriv",
            chain_id=137,
            creds=None,
            signature_type=3,
            funder="0xFUNDER",
        )
        self.assertEqual(creds["api_key"], "derived")
        self.assertEqual(client.kwargs["funder"], "0xFUNDER")
        self.assertEqual(client.kwargs["signature_type"], 3)
        self.assertEqual(len(_RecordingClobClient._calls), 2)
        derive_kwargs, trading_kwargs = _RecordingClobClient._calls
        for kwargs in (derive_kwargs, trading_kwargs):
            self.assertEqual(kwargs["funder"], "0xFUNDER")
            self.assertEqual(kwargs["signature_type"], 3)
            self.assertEqual(kwargs["host"], "https://clob.polymarket.com")
            self.assertEqual(kwargs["key"], "0xpriv")
            self.assertEqual(kwargs["chain_id"], 137)
        self.assertNotIn("creds", derive_kwargs)
        self.assertEqual(trading_kwargs["creds"], creds)
        self.assertIs(trading_kwargs["retry_on_error"], False)

    def test_pregenerated_creds_still_pass_proxy_kwargs_on_trading_client(self):
        preset = {"api_key": "preset"}
        creds, client = build_complement_clob_clients(
            _RecordingClobClient,
            host="https://clob.polymarket.com",
            key="0xpriv",
            chain_id=137,
            creds=preset,
            signature_type=3,
            funder="0xFUNDER",
        )
        self.assertIs(creds, preset)
        self.assertEqual(len(_RecordingClobClient._calls), 1)
        self.assertEqual(client.kwargs["funder"], "0xFUNDER")
        self.assertEqual(client.kwargs["signature_type"], 3)
        self.assertEqual(client.kwargs["creds"], preset)

    def test_complementbot_wires_deposit_wallet_type3_builder(self):
        src = (Path(__file__).resolve().parents[1] / "complementbot.py").read_text()
        tree = ast.parse(src)
        calls = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "build_complement_clob_clients"
        ]
        self.assertEqual(len(calls), 1, "complementbot must use the shared CLOB builder")
        self.assertTrue(
            isinstance(calls[0].args[0], ast.Name)
            and calls[0].args[0].id == "ComplementDepositClobClient",
            "complementbot must pass ComplementDepositClobClient, not ClobClient",
        )
        by_name = {kw.arg: kw.value for kw in calls[0].keywords}
        self.assertIn("signature_type", by_name)
        self.assertIn("funder", by_name)
        sig = by_name["signature_type"]
        funder = by_name["funder"]
        creds = by_name.get("creds")
        self.assertTrue(
            isinstance(sig, ast.Name) and sig.id == "COMPLEMENT_SIGNATURE_TYPE"
        )
        self.assertTrue(isinstance(funder, ast.Name) and funder.id == "COMPLEMENT_WALLET")
        self.assertTrue(isinstance(creds, ast.Constant) and creds.value is None)
        leftover = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "ClobClient"
        ]
        self.assertEqual(
            leftover,
            [],
            "complementbot must not construct ClobClient (py-clob L1 binds the EOA)",
        )
        called = {
            node.func.id
            for node in ast.walk(tree)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
        }
        self.assertIn("relayer_api_key_from_env", called)
        self.assertIn("require_complement_deposit_wallet", called)
        self.assertIn("RELAYER_API_KEY and RELAYER_ADDRESS in", src)
        self.assertIn(".env.complement", src)


class WalletIsolationTests(unittest.TestCase):
    def test_same_funder_is_rejected(self):
        self.assertTrue(
            primary_and_complement_same_wallet(
                "0xAbc", "0xabc",
            )
        )
        self.assertFalse(
            primary_and_complement_same_wallet(
                "0xaaa", "0xbbb",
            )
        )
        self.assertTrue(primary_and_complement_same_wallet("", ""))

    def test_funder_from_file_ignores_process_env(self):
        import os
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            primary = Path(tmp) / ".env"
            complement = Path(tmp) / ".env.complement"
            primary.write_text("FUNDER_ADDRESS=0x8222PRIMARY\n", encoding="utf-8")
            complement.write_text("FUNDER_ADDRESS=0xCFF5COMPLEMENT\n", encoding="utf-8")
            os.environ["FUNDER_ADDRESS"] = "0xCFF5COMPLEMENT"
            try:
                a = funder_from_env_file(primary)
                b = funder_from_env_file(complement)
            finally:
                os.environ.pop("FUNDER_ADDRESS", None)
        self.assertEqual(a.lower(), "0x8222primary")
        self.assertEqual(b.lower(), "0xcff5complement")
        self.assertFalse(primary_and_complement_same_wallet(a, b))


class ExampleJsonTests(unittest.TestCase):
    def test_example_is_disarmed_80_99(self):
        data = json.loads(
            (Path(__file__).resolve().parents[1] / "strategy_complement.example.json").read_text()
        )
        self.assertIs(data["dry_run"], True)
        self.assertIs(data["entry_enabled"], False)
        self.assertEqual(data["buy_min_price"], 0.80)
        self.assertEqual(data["buy_max_price"], 0.99)
        self.assertEqual(data["buy_max_spend"], 5.0)
        self.assertLessEqual(float(data["buy_max_spend"]), 5.5)
        self.assertIs(data["require_oracle"], True)
        self.assertIn("positions_buy5m.json", data["primary_state_files"])
        self.assertIn("positions_buy.json", data["primary_state_files"])
        self.assertGreaterEqual(float(data["reject_cooldown_s"]), 1.0)
        self.assertGreaterEqual(float(data["empty_cycle_cooldown_s"]), 0.5)
        self.assertGreaterEqual(float(data["uncertain_timeout_s"]), 2.0)


class PostOutcomeTests(unittest.TestCase):
    def test_matched_size_is_fill_not_requested(self):
        status, shares = complement_fill_from_post(
            {"status": "matched", "size_matched": "4.20"},
            requested=10.0,
        )
        self.assertEqual(status, "filled")
        self.assertAlmostEqual(shares, 4.20)

    def test_zero_matched_is_empty(self):
        status, shares = complement_fill_from_post(
            {"status": "matched", "size_matched": "0", "takingAmount": "0"},
            requested=10.0,
        )
        self.assertEqual((status, shares), ("empty", 0.0))

    def test_unmatched_error_is_empty(self):
        status, shares = complement_fill_from_post(
            {"success": False, "errorMsg": "no orders found to match with FAK order"},
            requested=10.0,
        )
        self.assertEqual((status, shares), ("empty", 0.0))

    def test_delayed_zero_is_ambiguous(self):
        status, shares = complement_fill_from_post(
            {"status": "delayed", "orderID": "abc", "takingAmount": "0"},
            requested=10.0,
        )
        self.assertEqual((status, shares), ("ambiguous", 0.0))

    def test_none_result_is_ambiguous(self):
        status, shares = complement_fill_from_post(None, requested=10.0)
        self.assertEqual((status, shares), ("ambiguous", 0.0))

    def test_fixed_point_taking_amount(self):
        status, shares = complement_fill_from_post(
            {"status": "matched", "takingAmount": "4200000"},
            requested=4.2,
        )
        self.assertEqual(status, "filled")
        self.assertAlmostEqual(shares, 4.2)

    def test_taking_amount_without_matched_status_is_ambiguous(self):
        status, shares = complement_fill_from_post(
            {"takingAmount": "10.00"},
            requested=10.0,
        )
        self.assertEqual((status, shares), ("ambiguous", 0.0))

    def test_unmatched_plus_balance_bump_is_ghost_fill(self):
        status, shares = apply_balance_evidence(
            "empty", 0.0, baseline=1.0, after=5.2,
        )
        self.assertEqual(status, "filled")
        self.assertAlmostEqual(shares, 4.2)

    def test_unmatched_unread_balance_stays_ambiguous(self):
        status, shares = apply_balance_evidence(
            "empty", 0.0, baseline=1.0, after=None,
        )
        self.assertEqual((status, shares), ("ambiguous", 0.0))

    def test_unmatched_flat_balance_stays_empty(self):
        status, shares = apply_balance_evidence(
            "empty", 0.0, baseline=1.0, after=1.0,
        )
        self.assertEqual((status, shares), ("empty", 0.0))


class QuarantineAndCooldownTests(unittest.TestCase):
    def test_write_ahead_blocks_second_post(self):
        meta = mark_submit_quarantine(
            {}, token="UP1", shares=10.0, limit=0.99, baseline=0.0, now_s=100.0,
        )
        blocked, why = should_block_post(meta, 100.1)
        self.assertTrue(blocked)
        self.assertEqual(why, "in_flight")

    def test_fill_clears_uncertain(self):
        meta = mark_submit_quarantine(
            {}, token="UP1", shares=10.0, limit=0.99, baseline=0.0, now_s=100.0,
        )
        apply_complement_outcome(
            meta, status="filled", shares=4.2, token="UP1", leg="up",
            source="5m", slug="s", held_token="DOWN1", now_s=101.0,
            empty_cooldown_s=1.0, reject_cooldown_s=2.0,
        )
        self.assertFalse(meta.get("buy_uncertain"))
        self.assertAlmostEqual(meta["bought_size"], 4.2)
        blocked, why = should_block_post(meta, 101.0)
        self.assertTrue(blocked)
        self.assertEqual(why, "already_bought")

    def test_empty_sets_cooldown(self):
        meta = mark_submit_quarantine(
            {}, token="UP1", shares=10.0, limit=0.99, baseline=0.0, now_s=100.0,
        )
        apply_complement_outcome(
            meta, status="empty", shares=0.0, token="UP1", leg="up",
            source="5m", slug="s", held_token="DOWN1", now_s=101.0,
            empty_cooldown_s=1.0, reject_cooldown_s=2.0,
        )
        self.assertFalse(meta.get("buy_uncertain"))
        blocked, why = should_block_post(meta, 101.5)
        self.assertTrue(blocked)
        self.assertEqual(why, "cooldown")
        blocked, why = should_block_post(meta, 102.1)
        self.assertFalse(blocked)

    def test_rejected_uses_longer_cooldown(self):
        meta = mark_submit_quarantine(
            {}, token="UP1", shares=10.0, limit=0.99, baseline=0.0, now_s=100.0,
        )
        apply_complement_outcome(
            meta, status="rejected", shares=0.0, token="UP1", leg="up",
            source="5m", slug="s", held_token="DOWN1", now_s=101.0,
            empty_cooldown_s=1.0, reject_cooldown_s=2.0,
        )
        blocked, why = should_block_post(meta, 102.5)
        self.assertTrue(blocked)
        self.assertEqual(why, "cooldown")
        blocked, _ = should_block_post(meta, 103.1)
        self.assertFalse(blocked)

    def test_ambiguous_stays_in_flight(self):
        meta = mark_submit_quarantine(
            {}, token="UP1", shares=10.0, limit=0.99, baseline=0.0, now_s=100.0,
        )
        apply_complement_outcome(
            meta, status="ambiguous", shares=0.0, token="UP1", leg="up",
            source="5m", slug="s", held_token="DOWN1", now_s=101.0,
            empty_cooldown_s=1.0, reject_cooldown_s=2.0,
        )
        self.assertTrue(meta.get("buy_uncertain"))
        blocked, why = should_block_post(meta, 200.0)
        self.assertTrue(blocked)
        self.assertEqual(why, "in_flight")

    def test_inflight_balance_bump_is_fill(self):
        meta = mark_submit_quarantine(
            {}, token="UP1", shares=10.0, limit=0.99, baseline=1.0, now_s=100.0,
        )
        status, shares = resolve_inflight(
            meta, now_s=100.5, after=5.2, timeout_s=5.0,
        )
        self.assertEqual(status, "filled")
        self.assertAlmostEqual(shares, 4.2)

    def test_inflight_flat_balance_waits_then_empties(self):
        meta = mark_submit_quarantine(
            {}, token="UP1", shares=10.0, limit=0.99, baseline=1.0, now_s=100.0,
        )
        status, shares = resolve_inflight(
            meta, now_s=102.0, after=1.0, timeout_s=5.0,
        )
        self.assertEqual((status, shares), ("wait", 0.0))
        status, shares = resolve_inflight(
            meta, now_s=105.0, after=1.0, timeout_s=5.0,
        )
        self.assertEqual((status, shares), ("empty", 0.0))

    def test_inflight_unread_balance_stays_wait(self):
        meta = mark_submit_quarantine(
            {}, token="UP1", shares=10.0, limit=0.99, baseline=1.0, now_s=100.0,
        )
        status, shares = resolve_inflight(
            meta, now_s=200.0, after=None, timeout_s=5.0,
        )
        self.assertEqual((status, shares), ("wait", 0.0))


if __name__ == "__main__":
    unittest.main()
