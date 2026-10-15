"""Kept-leg dump sells only when its bid is strictly under sell_dump_kept_max_px."""

from __future__ import annotations

import json
import unittest
from unittest.mock import Mock

from buy.mint_redeem import RedeemDesk, RedeemIO, RedeemSettings
from buy.mint_sell import kept_dump_blocked, kept_dump_max_px, kept_loser_open, reclaim_arm_block
from test_mint_cpu import _dump_cfg, _dump_harness, _fill_fak, _held_after_scrap
from test_mint_dump_kept import _priced, _ready_bag, _tick
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
            # A later blocked tick keeps the original hold result and log.
            before = len(fak_calls)
            clock["now"] += 5.0
            _tick(ns, _dump_cfg(sell_dump_also_kept=True, sell_dump_kept_max_px=0.40), intent)
            self.assertEqual(len(fak_calls), before)
            self.assertEqual(
                len([e for e in events if e["event"] == "sell_dump_kept"]),
                1,
            )

    def test_later_ticks_recheck_and_sell_under_cap_despite_cooldown(self):
        for parallel in (False, True):
            with self.subTest(parallel=parallel):
                ns, events, calls, clock, intent = self._fire(
                    0.85, sell_dump_parallel_kept=parallel,
                )
                cfg = _dump_cfg(sell_dump_also_kept=True, sell_dump_kept_max_px=0.40,
                                sell_cooldown_s=60.0, sell_dump_parallel_kept=parallel)
                for bid in (0.70, 0.40):
                    _set_book(ns, 0.51, bid)
                    ns["_fetch_book"] = Mock(side_effect=AssertionError("extra fetch"))
                    clock["now"] += 0.1
                    _tick(ns, cfg, intent)
                    ns["_fetch_book"].assert_not_called()
                    self.assertEqual(len(calls), 1)
                    self.assertEqual(intent["sell_dump_kept_outcome"], "held_above_max")
                self.assertEqual(len([e for e in events if e["event"] == "sell_dump_kept"]), 1)
                _set_book(ns, 0.51, 0.39)
                ns["_fetch_book"] = Mock(return_value=(
                    0.38, 50.0, [{"price": "0.38", "size": "50"}],
                ))
                ns["_fak_sell"] = _priced(
                    calls, lambda _token, size, px: min(size, 5.0) if px > 0.01 else size,
                )
                clock["now"] += 0.1
                _tick(ns, cfg, intent)
                self.assertEqual(
                    [(c["size"], c["price"]) for c in calls if c["token_id"] == "dn-tok"],
                    [(25.0, 0.39), (20.0, 0.38), (15.0, 0.38), (10.0, 0.01)],
                )
                self.assertEqual(ns["_fetch_book"].call_count, 2)
                self.assertTrue(intent["sell_dump_kept_sold"])
                self.assertEqual(intent["sell_dump_kept_filled"], 25.0)
                self.assertAlmostEqual(intent["sell_dump_kept_fill_px"], 0.234)
                kept = [e for e in events if e["event"] == "sell_dump_kept"]
                self.assertEqual([e["outcome"] for e in kept], ["held_above_max", "filled"])
                self.assertEqual(kept[-1]["swept"], 10.0)
                clock["now"] += 61.0
                ns["_fetch_book"] = Mock(side_effect=AssertionError("sold refetch"))
                _tick(ns, cfg, intent)
                self.assertEqual(len(calls), 5)
                ns["_fetch_book"].assert_not_called()
                self.assertEqual(len([e for e in events if e["event"] == "sell_dump_kept"]), 2)

    def _retry_with_leftovers(self, first_fill, **cfg_overrides):
        ns, events, calls, clock, intent = self._fire(0.85)
        cfg = _dump_cfg(sell_dump_also_kept=True, sell_dump_kept_max_px=0.40,
                        sell_cooldown_s=60.0, **cfg_overrides)
        _set_book(ns, 0.51, 0.39)
        pending = [first_fill]
        ns["_fak_sell"] = _priced(
            calls, lambda _t, size, _p: min(size, pending.pop()) if pending else 0.0,
        )
        clock["now"] += 0.1
        _tick(ns, cfg, intent)
        self.assertEqual(intent["sell_dump_kept_outcome"], "partial" if first_fill else "no_fill")
        self.assertEqual(intent["sell_dump_kept_filled"], first_fill)
        self.assertTrue(intent["sell_dump_kept_cap_hold"])
        self.assertTrue(kept_loser_open(intent))
        self.assertEqual(reclaim_arm_block(intent, also_kept=True), "kept_open")
        return ns, events, calls, clock, intent, cfg

    def test_no_fill_and_partial_retry_remaining_on_later_ticks(self):
        for first_fill in (0.0, 5.0):
            with self.subTest(first_fill=first_fill):
                ns, events, calls, clock, intent, cfg = self._retry_with_leftovers(first_fill)
                # Another partial tick, then a no-fill tick, must preserve all fills.
                for filled in (3.0, 0.0):
                    before = len(calls)
                    pending = [filled]
                    ns["_fak_sell"] = _priced(
                        calls, lambda _t, size, _p: min(size, pending.pop()) if pending else 0.0,
                    )
                    clock["now"] += 0.1
                    _tick(ns, cfg, intent)
                    self.assertEqual(calls[before]["size"], 25.0 - first_fill)
                    first_fill += filled
                    self.assertEqual(intent["sell_dump_kept_filled"], first_fill)
                    self.assertEqual(reclaim_arm_block(intent, also_kept=True), "kept_open")
                before = len(calls)
                ns["_fak_sell"] = _priced(calls, lambda _t, size, _p: size)
                clock["now"] += 0.1
                _tick(ns, cfg, intent)
                self.assertEqual([c["size"] for c in calls[before:]], [25.0 - first_fill])
                self.assertEqual(intent["sell_dump_kept_filled"], 25.0)
                self.assertFalse(intent["sell_dump_kept_cap_hold"])
                self.assertFalse(kept_loser_open(intent))
                self.assertIsNone(reclaim_arm_block(intent, also_kept=True))
                before = len(calls)
                clock["now"] += 0.1
                _tick(ns, cfg, intent)
                self.assertEqual(len(calls), before)

    def test_leftovers_hold_at_cap_and_respect_inflight_and_window_end(self):
        for first_fill in (0.0, 5.0):
            with self.subTest(first_fill=first_fill):
                ns, events, calls, clock, intent, cfg = self._retry_with_leftovers(first_fill)
                before = len(calls)
                for bid in (0.40, 0.85):
                    _set_book(ns, 0.51, bid)
                    clock["now"] += 0.1
                    _tick(ns, cfg, intent)
                    self.assertEqual(len(calls), before)
                    self.assertEqual(intent["sell_dump_kept_filled"], first_fill)
                _set_book(ns, 0.51, 0.39)
                intent["sell_exit_inflight"] = True
                clock["now"] += 0.1
                _tick(ns, cfg, intent)
                self.assertEqual(len(calls), before)
                intent["sell_exit_inflight"] = False
                clock["now"] = intent["end_ts"]
                ns["_fetch_books"] = Mock(side_effect=AssertionError("closed window fetch"))
                _tick(ns, cfg, intent)
                self.assertEqual(len(calls), before)
                ns["_fetch_books"].assert_not_called()

    def test_flat_inventory_after_leftovers_clears_hold(self):
        for first_fill in (0.0, 5.0):
            with self.subTest(first_fill=first_fill):
                ns, events, calls, clock, intent, cfg = self._retry_with_leftovers(first_fill)
                ns["_sell_inventory"] = Mock(return_value=(0.0, "already_flat"))
                before = len(calls)
                clock["now"] += 0.1
                _tick(ns, cfg, intent)
                self.assertFalse(intent["sell_dump_kept_cap_hold"])
                self.assertFalse(kept_loser_open(intent))
                self.assertIsNone(reclaim_arm_block(intent, also_kept=True))
                ns["_sell_inventory"].reset_mock()
                clock["now"] += 0.1
                _tick(ns, cfg, intent)
                ns["_sell_inventory"].assert_not_called()
                self.assertEqual(len(calls), before)

    def test_partial_without_initial_cap_hold_keeps_existing_behavior(self):
        for cap in (None, 0.40):
            with self.subTest(cap=cap):
                ns, events, calls, clock = _dump_harness(_fill_fak)
                _set_book(ns, 0.51, 0.39)
                intent = _ready_bag(clock, keep=25.0)
                cfg = _dump_cfg(sell_dump_also_kept=True, sell_dump_kept_max_px=cap)
                ns["_fak_sell"] = _priced(
                    calls, lambda token, size, _p: size if token == "up-tok" else 0.0,
                )
                _tick(ns, cfg, intent)
                self.assertEqual(intent["sell_dump_kept_outcome"], "no_fill")
                self.assertFalse(intent.get("sell_dump_kept_cap_hold"))
                before = len(calls)
                clock["now"] += 5.0
                _tick(ns, cfg, intent)
                self.assertEqual(len(calls), before)
                self.assertIsNone(reclaim_arm_block(intent, also_kept=True))

    def test_legacy_cap_hold_remains_eligible_after_no_fill(self):
        ns, events, calls, clock, intent = self._fire(0.85)
        intent.pop("sell_dump_kept_cap_hold")
        _set_book(ns, 0.51, 0.39)
        cfg = _dump_cfg(sell_dump_also_kept=True, sell_dump_kept_max_px=0.40)
        ns["_fak_sell"] = _priced(calls, lambda *_a: 0.0)
        clock["now"] += 0.1
        _tick(ns, cfg, intent)
        self.assertTrue(intent["sell_dump_kept_cap_hold"])
        before = len(calls)
        ns["_fak_sell"] = _priced(calls, lambda _t, size, _p: size)
        clock["now"] += 0.1
        _tick(ns, cfg, intent)
        self.assertEqual([c["size"] for c in calls[before:]], [25.0])
        self.assertTrue(intent["sell_dump_kept_sold"])

    def test_never_retried_after_window_end(self):
        ns, events, calls, clock, intent = self._fire(0.85)
        _set_book(ns, 0.51, 0.39)
        clock["now"] = intent["end_ts"]
        ns["_fetch_books"] = Mock(side_effect=AssertionError("closed window fetch"))
        _tick(ns, _dump_cfg(sell_dump_also_kept=True, sell_dump_kept_max_px=0.40), intent)
        self.assertEqual(len(calls), 1)
        self.assertEqual(intent["sell_dump_kept_outcome"], "held_above_max")
        self.assertTrue(kept_loser_open(intent))
        ns["_fetch_books"].assert_not_called()

    def test_retry_requires_dump_open_shares_and_no_inflight_exit(self):
        for extra in (
            {"sold_dump": False}, {"sell_dump_kept_sold": True},
            {"sell_scrap_keep": 0.0}, {"sell_exit_inflight": True},
            {"sell_winner_leg": "dn", "sold_winner": True},
        ):
            with self.subTest(extra=extra):
                ns, events, calls, clock, intent = self._fire(0.85)
                intent.update(extra)
                _set_book(ns, 0.70, 0.39)
                ns["_fetch_book"] = Mock(side_effect=AssertionError("ineligible fetch"))
                clock["now"] += 0.1
                _tick(ns, _dump_cfg(sell_dump_also_kept=True, sell_dump_kept_max_px=0.40), intent)
                self.assertEqual(len(calls), 1)
                ns["_fetch_book"].assert_not_called()

    def test_retry_checks_inventory_before_posting(self):
        ns, events, calls, clock, intent = self._fire(0.85)
        _set_book(ns, 0.51, 0.39)
        ns["_sell_inventory"] = lambda *_a, **_k: (0.0, "already_flat")
        clock["now"] += 0.1
        _tick(ns, _dump_cfg(sell_dump_also_kept=True, sell_dump_kept_max_px=0.40), intent)
        self.assertEqual(len(calls), 1)
        self.assertEqual(intent["sell_dump_kept_outcome"], "nothing_kept")

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

    def test_redeem_enqueues_cap_retry_leftovers_and_observes_flat_balances(self):
        for first_fill in (0.0, 5.0):
            for flat in (False, True):
                with self.subTest(first_fill=first_fill, flat=flat):
                    ns, events, calls, clock, intent, cfg = self._retry_with_leftovers(first_fill)
                    if flat:
                        ns["_fak_sell"] = _priced(calls, lambda _t, size, _p: size)
                        clock["now"] += 0.1
                        _tick(ns, cfg, intent)
                    self.assertEqual(kept_loser_open(intent), not flat)
                    self.assertEqual(
                        reclaim_arm_block(intent, also_kept=True), None if flat else "kept_open",
                    )
                    balance = 0.0 if flat else 25.0 - first_fill
                    submits = Mock(return_value=("0x", "ok"))
                    desk = RedeemDesk(RedeemIO(
                        payout_denominator=lambda _c: 1,
                        payout_numerator=lambda _c, i: 1 if i == 1 else 0,
                        balance=lambda token: balance if token == "dn-tok" else 0.0,
                        is_approved=lambda: True,
                        submit=submits,
                        relayer_status=lambda _i: None,
                        log=lambda *_a, **_k: None,
                    ))
                    state = {"intents": {"cid": intent}}
                    now = intent["end_ts"] + 120.0
                    redeem_cfg = {"redeem_enabled": True, "dry_run": False, "redeem_startup_sweep": False}
                    settings = RedeemSettings(enabled=True, dry_run=False, startup_sweep=False)
                    self.assertEqual(desk.enqueue_ended_bags(state, settings, now=now), 1)
                    desk.tick(state, redeem_cfg, now=now)
                    desk.tick(state, redeem_cfg, now=now + settings.poll_s)
                    if flat:
                        submits.assert_not_called()
                        self.assertEqual(state["redeems"]["cid"]["reason"], "nothing_held")
                    else:
                        submits.assert_called_once()

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
