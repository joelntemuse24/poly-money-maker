"""Unit tests for the second-account complement buyer (no network)."""

from __future__ import annotations

import json
import unittest
from pathlib import Path

from buy.complement_gate import (
    arm_from_primary_meta,
    complement_target_shares,
    evaluate_complement,
    other_leg_token,
    primary_and_complement_same_wallet,
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
        )
        # 10.87 × 99¢ is not exact maker cents; snap down to a legal 2dp size.
        self.assertAlmostEqual(shares, 10.00, places=2)
        self.assertLessEqual(shares * 0.99, 16.0 + 1e-9)

    def test_spend_cap_clips(self):
        shares = complement_target_shares(
            20.0, ask=0.80, limit=0.99, spend_cap=10.0, share_cap=40.0,
        )
        self.assertLessEqual(shares * 0.99, 10.0 + 1e-9)
        self.assertGreaterEqual(shares, 10.0)

    def test_zero_without_held(self):
        self.assertEqual(
            complement_target_shares(0.0, ask=0.80, limit=0.99, spend_cap=16.0),
            0.0,
        )


class EvaluateComplementTests(unittest.TestCase):
    def test_fires_at_80_tight_book(self):
        fire, why, shares = evaluate_complement(
            other_ask=0.80,
            other_bid=0.78,
            held_shares=10.87,
            already_bought=False,
            primary_still_holding=True,
            oracle_favors_other=True,
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


class ExampleJsonTests(unittest.TestCase):
    def test_example_is_disarmed_80_99(self):
        data = json.loads(
            (Path(__file__).resolve().parents[1] / "strategy_complement.example.json").read_text()
        )
        self.assertIs(data["dry_run"], True)
        self.assertIs(data["entry_enabled"], False)
        self.assertEqual(data["buy_min_price"], 0.80)
        self.assertEqual(data["buy_max_price"], 0.99)
        self.assertIs(data["require_oracle"], True)
        self.assertIn("positions_buy5m.json", data["primary_state_files"])
        self.assertIn("positions_buy.json", data["primary_state_files"])


if __name__ == "__main__":
    unittest.main()
