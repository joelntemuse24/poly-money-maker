"""Kept-leg dump sells only when its bid is strictly under sell_dump_kept_max_px."""

from __future__ import annotations

import json
import unittest

from buy.mint_redeem import RedeemDesk, RedeemIO, RedeemSettings
from buy.mint_sell import kept_dump_blocked, kept_dump_max_px, kept_loser_open, reclaim_arm_block
from test_mint_cpu import _dump_cfg, _dump_harness, _fill_fak, _held_after_scrap
from test_mint_dump_kept import _ready_bag, _tick
from test_mint_only_ops import MINT_EXAMPLE, _assign


def _set_book(ns, up, dn):
    ns["_fetch_books"] = lambda *_a, **_k: (
        (up, 50.0, [{"price": str(up), "size": "50"}]),
        (dn, 50.0, [{"price": str(dn), "size": "50"}]),
    )
    ns["_fetch_book"] = lambda *_a, **_k: (
        dn, 50.0, [{"price": str(dn), "size": "50"}],
    )


class KeptMaxHelperTests(unittest.TestCase):
    def test_null_cap_never_blocks(self):
        self.assertIsNone(kept_dump_max_px(None))
        self.assertIsNone(kept_dump_max_px(""))
        self.assertFalse(kept_dump_blocked(0.85, None))
        self.assertFalse(kept_dump_blocked(None, None))

    def test_threshold_is_strict(self):
        self.assertFalse(kept_dump_blocked(0.39, 0.40))
        self.assertTrue(kept_dump_blocked(0.40, 0.40))
        self.assertTrue(kept_dump_blocked(0.85, 0.40))
        self.assertTrue(kept_dump_blocked(None, 0.40))

    def test_defaults_are_null(self):
        defaults = _assign("DEFAULTS")
        example = json.loads(MINT_EXAMPLE.read_text())
        self.assertIsNone(defaults["sell_dump_kept_max_px"])
        self.assertIsNone(example["sell_dump_kept_max_px"])


class KeptMaxLoopTests(unittest.TestCase):
    def _fire(self, kept_bid, **cfg):
        ns, events, fak_calls, clock = _dump_harness(_fill_fak)
        _set_book(ns, 0.51, kept_bid)
        intent = _ready_bag(clock, keep=25.0)
        _tick(
            ns,
            _dump_cfg(sell_dump_also_kept=True, sell_dump_kept_max_px=0.40, **cfg),
            intent,
        )
        return ns, events, fak_calls, clock, intent

    def test_bid_under_the_cap_sells_the_kept_leg(self):
        _ns, events, fak_calls, _clock, intent = self._fire(0.39)
        kept = [c for c in fak_calls if c["token_id"] == "dn-tok"]
        held = [c for c in fak_calls if c["token_id"] == "up-tok"]
        self.assertTrue(held)
        self.assertEqual(len(kept), 1)
        self.assertAlmostEqual(kept[0]["price"], 0.39)
        self.assertTrue(intent.get("sold_dump"))
        self.assertTrue(intent.get("sell_dump_kept_sold"))
        self.assertFalse(kept_loser_open(intent))
        row = [e for e in events if e["event"] == "sell_dump_kept"][0]
        self.assertEqual(row["outcome"], "filled")

    def test_bid_at_or_above_the_cap_holds_to_redeem(self):
        for bid in (0.40, 0.85):
            ns, events, fak_calls, clock, intent = self._fire(bid)
            self.assertTrue(intent.get("sold_dump"), bid)
            self.assertEqual(
                [c["token_id"] for c in fak_calls],
                ["up-tok"],
                bid,
            )
            self.assertTrue(intent.get("sell_dump_kept_done"), bid)
            self.assertFalse(intent.get("sell_dump_kept_sold"), bid)
            self.assertEqual(intent.get("sell_dump_kept_outcome"), "held_above_max")
            self.assertTrue(kept_loser_open(intent), bid)
            row = [e for e in events if e["event"] == "sell_dump_kept"][0]
            self.assertEqual(row["outcome"], "held_above_max")
            self.assertAlmostEqual(row["bid"], bid)
            self.assertEqual(
                reclaim_arm_block(intent, also_kept=True),
                "kept_open",
            )
            # One check. A later tick does not post the kept leg.
            before = len(fak_calls)
            clock["now"] += 5.0
            _tick(ns, _dump_cfg(sell_dump_also_kept=True, sell_dump_kept_max_px=0.40), intent)
            self.assertEqual(len(fak_calls), before)
            self.assertEqual(
                len([e for e in events if e["event"] == "sell_dump_kept"]),
                1,
            )

    def test_null_cap_sells_as_before(self):
        ns, events, fak_calls, clock = _dump_harness(_fill_fak)
        intent = _ready_bag(clock, keep=25.0)
        cfg = _dump_cfg(sell_dump_also_kept=True)
        self.assertIsNone(cfg.get("sell_dump_kept_max_px"))
        _tick(ns, cfg, intent)
        self.assertIn("dn-tok", [c["token_id"] for c in fak_calls])
        self.assertTrue(intent.get("sell_dump_kept_sold"))
        self.assertNotEqual(intent.get("sell_dump_kept_outcome"), "held_above_max")
        row = [e for e in events if e["event"] == "sell_dump_kept"][0]
        self.assertEqual(row["outcome"], "filled")

    def test_held_above_max_still_enqueues_redeem(self):
        intent = _held_after_scrap(
            1_000.0,
            status="confirmed",
            sell_scrap_keep=100.0,
            sold_dump=True,
            sell_dump_kept_done=True,
            sell_dump_kept_sold=False,
            sell_dump_kept_outcome="held_above_max",
            up_token="up",
            dn_token="dn",
        )
        self.assertTrue(kept_loser_open(intent))
        desk = RedeemDesk(
            RedeemIO(
                payout_denominator=lambda _c: 0,
                payout_numerator=lambda _c, _i: 0,
                balance=lambda _t: 100.0,
                is_approved=lambda: True,
                submit=lambda _c, _a: ("0x", "ok"),
                relayer_status=lambda _i: None,
                log=lambda *_a, **_k: None,
            )
        )
        state = {"intents": {"cid": intent}}
        added = desk.enqueue_ended_bags(state, RedeemSettings(), now=1_000.0 + 120.0)
        self.assertEqual(added, 1)
        self.assertIn("cid", state["redeems"])


if __name__ == "__main__":
    unittest.main()
