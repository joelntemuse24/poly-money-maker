"""Reclaim after a both-sides dump: guards, one buy, stop, hot poll, latency."""

from __future__ import annotations

import time
import unittest
from pathlib import Path
from types import SimpleNamespace

from buy.book import ask_fill_depth, best_ask_with_min_size, book_age_s
from buy.mint_sell import (
    cycle_sleep_s,
    reclaim_arm_block,
    reclaim_candidate_order,
    reclaim_entry_decision,
    reclaim_entry_qualify,
    sell_intent_hot,
    sell_plan_banner,
)
from test_mint_cpu import _dump_cfg, _held_after_scrap, _sell_runtime
from test_mint_only_ops import _assign, _fn


ROOT = Path(__file__).resolve().parents[1]
MINT = ROOT / "mintbot.py"
ART = Path("/opt/cursor/artifacts")


def _lvl(px: float, size: float) -> list:
    return [{"price": f"{px:.4f}", "size": f"{size:.4f}"}]


def _row(bid, ask, size=500.0, age=None):
    bids = _lvl(bid, size) if bid is not None else []
    asks = _lvl(ask, size) if ask is not None else []
    bid_sz = size if bid is not None else 0.0
    ask_sz = size if ask is not None else 0.0
    return (bid, bid_sz, bids, ask, ask_sz, asks, age)


def _reclaim_cfg(**extra) -> dict:
    cfg = _dump_cfg(
        reclaim_enabled=True,
        reclaim_usd=100.0,
        reclaim_entry=0.91,
        reclaim_entry_persist_s=0.5,
        reclaim_stop=0.75,
        reclaim_stop_enabled=True,
        reclaim_stop_persist_s=0.5,
        reclaim_max_ttm_s=0.0,
        sell_dump_also_kept=True,
        poll_s=5.0,
        sell_armed_poll_s=0.2,
        sell_cooldown_s=3.0,
        sell_floor=0.02,
        sell_clob_min_price=0.01,
    )
    cfg.update(extra)
    return cfg


def _dumped(end: float, **extra) -> dict:
    row = _held_after_scrap(
        end,
        sold_dump=True,
        sold_winner=True,
        sell_dump_leg="up",
        sell_dump_kept_done=True,
        sell_dump_kept_leg="dn",
        sell_scrap_keep=0.0,
        shares=200.0,
    )
    row.update(extra)
    return row


def _qualifying(**kwargs):
    up = kwargs.get("up", (0.93, 0.94, 500.0, None))
    dn = kwargs.get("dn", (0.06, 0.07, 500.0, None))
    return {"up": _row(*up), "dn": _row(*dn)}


class _Loop:
    def __init__(self, *, real_buy: bool = False, buy_result=None, sell_result=None):
        self.events: list = []
        self.buys: list = []
        self.sells: list = []
        self.fetches: list = []
        self.inventories: list = []
        self.sleeps: list = []
        self.client_calls: list = []
        self.clock = {"now": 1_700_000_000.0}
        self.book = _qualifying()
        self.buy_result = buy_result
        self.sell_result = sell_result
        saves: list = []
        ns = _sell_runtime(saves)
        ns["time"] = SimpleNamespace(
            time=lambda: self.clock["now"],
            sleep=lambda delay, *_a, **_k: self.sleeps.append(delay),
        )
        ns["log_event"] = lambda event, **kwargs: self.events.append(
            {"event": event, **kwargs}
        )

        def fetch_books(_up, _dn, _min):
            return self.book["up"], self.book["dn"]

        def fetch_book(*args, **_kwargs):
            self.fetches.append(args)
            return self.book["up"]

        def fak_buy(token, shares, price, dry_run, capture=None):
            self.buys.append(
                {
                    "token_id": token,
                    "size": float(shares),
                    "price": float(price),
                    "dry_run": dry_run,
                }
            )
            if dry_run:
                return 0.0, "dry"
            if self.buy_result is not None:
                bought, status = self.buy_result(shares, price)
            else:
                bought, status = float(shares), "matched"
            if capture is not None and float(bought or 0) > 0:
                capture.append(
                    {
                        "status": status,
                        "takingAmount": float(bought),
                        "makingAmount": float(bought) * float(price),
                    }
                )
            return bought, status

        def fak_sell(token, size, price, dry_run, capture=None):
            self.sells.append(
                {
                    "token_id": token,
                    "size": float(size),
                    "price": float(price),
                    "dry_run": dry_run,
                }
            )
            if dry_run:
                return 0.0, "dry"
            if self.sell_result is not None:
                return self.sell_result(size, price)
            return float(size), "matched"

        def sell_inventory(*args, **_kwargs):
            self.inventories.append(args)
            shares = float(args[4]) if len(args) > 4 else 0.0
            return shares, "unknown"

        def get_client():
            self.client_calls.append(1)
            raise AssertionError("clob client")

        ns["_fetch_books"] = fetch_books
        ns["_fetch_book"] = fetch_book
        if not real_buy:
            ns["_fak_buy"] = fak_buy
        ns["_fak_sell"] = fak_sell
        ns["_sell_inventory"] = sell_inventory
        ns["_get_clob_client"] = get_client
        self.ns = ns

    def tick(self, cfg, intent, cid="cid-reclaim"):
        state = {"intents": {cid: intent}}
        self.ns["remember_persisted_state"](state)
        self.ns["_manage_sells_locked"](cfg, state, object())
        return state

    def events_named(self, name: str) -> list:
        return [row for row in self.events if row["event"] == name]


def _arm_entry(intent: dict, now: float, leg: str = "up") -> None:
    intent["reclaim_armed"] = True
    intent["reclaim_entry_armed_at"] = now - 0.5
    intent["reclaim_entry_leg"] = leg


class BookHelpersTests(unittest.TestCase):
    def test_sized_ask_age_and_buy_depth(self):
        asks = [
            {"price": "0.90", "size": "0.1"},
            {"price": "0.94", "size": "80"},
            {"price": "0.97", "size": "40"},
        ]
        px, size = best_ask_with_min_size(asks, min_size=1.0)
        self.assertEqual(px, 0.94)
        self.assertEqual(size, 80.0)
        self.assertEqual(book_age_s(None, 100.0), None)
        self.assertEqual(book_age_s("", 100.0), None)
        self.assertAlmostEqual(book_age_s(1_700_000_000_000, 1_700_000_006.0), 6.0)
        self.assertEqual(book_age_s(1_700_000_010.0, 1_700_000_000.0), 0.0)
        depth = ask_fill_depth(asks, 0.94)["depth_at_limit"]
        self.assertAlmostEqual(depth, 80.1)


class GuardDecisionTests(unittest.TestCase):
    def _decision(self, **kwargs):
        base = dict(
            now_s=100.0,
            end_ts=400.0,
            ttm_s=300.0,
            max_ttm_s=0.0,
            entry=0.91,
            usd=100.0,
            persist_s=0.5,
            armed_ts=99.5,
            armed_leg="up",
            locked_leg=None,
            up_bid=0.93,
            up_ask=0.94,
            dn_bid=0.06,
            dn_ask=0.07,
            up_depth=500.0,
            dn_depth=500.0,
            up_age=None,
            dn_age=None,
        )
        base.update(kwargs)
        return reclaim_entry_decision(**base)

    def test_other_side_does_not_confirm(self):
        out = self._decision(up_bid=0.90, dn_bid=0.07, dn_ask=0.08)
        self.assertEqual(out["action"], "skip")
        self.assertEqual(out["reason"], "wick_unconfirmed")
        self.assertIsNone(out["armed_ts"])

    def test_single_tick_does_not_fire(self):
        out = self._decision(armed_ts=None, armed_leg=None)
        self.assertEqual(out["action"], "wait")
        self.assertEqual(out["reason"], "armed")
        self.assertEqual(out["armed_ts"], 100.0)
        waiting = self._decision(armed_ts=100.0, now_s=100.4)
        self.assertEqual(waiting["action"], "wait")
        self.assertEqual(waiting["reason"], "waiting")

    def test_fires_on_the_first_tick_after_persist(self):
        early = self._decision(armed_ts=99.5, now_s=99.99)
        self.assertEqual(early["action"], "wait")
        out = self._decision(armed_ts=99.5, now_s=100.0)
        self.assertEqual(out["action"], "buy")
        self.assertEqual(out["leg"], "up")
        self.assertAlmostEqual(out["shares"], 106.0)
        self.assertAlmostEqual(out["limit"], 0.94)

    def test_stale_empty_crossed_locked_wide_and_thin(self):
        stale = self._decision(up_age=5.01)
        self.assertEqual((stale["action"], stale["reason"]), ("skip", "stale_book"))
        fresh = self._decision(up_age=5.0)
        self.assertEqual(fresh["action"], "buy")
        missing = self._decision(up_age=None)
        self.assertEqual(missing["action"], "buy")
        empty = self._decision(up_ask=None)
        self.assertEqual(empty["reason"], "empty_book")
        crossed = self._decision(up_bid=0.95, up_ask=0.93)
        self.assertEqual(crossed["reason"], "crossed")
        locked = self._decision(up_bid=0.94, up_ask=0.94)
        self.assertEqual(locked["reason"], "locked")
        wide = self._decision(up_bid=0.82, up_ask=0.94)
        self.assertEqual(wide["reason"], "wide_spread")
        thin = self._decision(up_depth=10.0)
        self.assertEqual(thin["reason"], "thin_depth")

    def test_quiet_book_is_no_entry_and_both_rich_skips(self):
        quiet = self._decision(
            armed_ts=None, armed_leg=None,
            up_bid=0.50, up_ask=0.51, dn_bid=0.49, dn_ask=0.50,
        )
        self.assertEqual(quiet["reason"], "no_entry")
        both = self._decision(dn_bid=0.93, dn_ask=0.94)
        self.assertEqual(both["reason"], "both_rich")
        self.assertNotEqual(both["action"], "buy")

    def test_favourite_side_and_candidate_tie_break(self):
        dn = self._decision(
            armed_ts=95.0, armed_leg="dn",
            up_bid=0.06, up_ask=0.07, dn_bid=0.93, dn_ask=0.94,
        )
        self.assertEqual(dn["action"], "buy")
        self.assertEqual(dn["leg"], "dn")
        self.assertEqual(
            reclaim_candidate_order(0.94, 0.94, 0.91),
            ["up", "dn"],
        )
        self.assertEqual(reclaim_candidate_order(0.95, 0.97, 0.91), ["dn", "up"])

    def test_dumped_leg_is_named_when_the_guard_fails(self):
        out = self._decision(up_depth=1.0, dumped_legs=("up", "dn"))
        self.assertEqual(out["reason"], "thin_depth")
        self.assertTrue(out["dumped_leg"])
        ok = self._decision(dumped_legs=("up",))
        self.assertEqual(ok["action"], "buy")
        self.assertTrue(ok["dumped_leg"])

    def test_miss_retries_the_locked_leg_without_a_new_persist(self):
        out = self._decision(
            locked_leg="up",
            filled=0.0,
            target=106.0,
            armed_ts=95.0,
            now_s=100.0,
        )
        self.assertEqual(out["action"], "buy")
        self.assertEqual(out["reason"], "retry")
        self.assertEqual(out["leg"], "up")
        blocked = self._decision(
            locked_leg="up",
            filled=0.0,
            target=106.0,
            up_bid=0.50,
            up_ask=0.51,
            dn_bid=0.93,
            dn_ask=0.94,
        )
        self.assertEqual(blocked["action"], "skip")
        self.assertEqual(blocked["leg"], "up")
        self.assertNotEqual(blocked["leg"], "dn")

    def test_zero_persist_fires_on_the_arming_tick(self):
        out = self._decision(persist_s=0, armed_ts=None, armed_leg=None)
        self.assertEqual(out["action"], "buy")
        self.assertEqual(out["reason"], "immediate")

    def test_inflight_does_not_post_and_time_gate_resets(self):
        inflight = self._decision(inflight=True)
        self.assertEqual(inflight["reason"], "inflight")
        gated = self._decision(ttm_s=400.0, max_ttm_s=100.0, armed_ts=90.0)
        self.assertEqual(gated["reason"], "time_gated")
        self.assertIsNone(gated["armed_ts"])

    def test_qualify_matches_classify_loser_shape(self):
        ok, reason, shares = reclaim_entry_qualify(
            leg="up", bid=0.93, ask=0.94, other_bid=0.06, other_ask=0.07,
            entry=0.91, usd=100.0, ask_depth=500.0,
        )
        self.assertTrue(ok)
        self.assertEqual(reason, "ok")
        self.assertEqual(shares, 106.0)


class ArmTests(unittest.TestCase):
    def test_arm_block_matrix(self):
        self.assertEqual(
            reclaim_arm_block({"sold_loser": True}, also_kept=True),
            "no_dump",
        )
        self.assertEqual(
            reclaim_arm_block(
                {"sold_dump": True, "sell_scrap_keep": 100.0},
                also_kept=True,
            ),
            "kept_pending",
        )
        self.assertEqual(
            reclaim_arm_block(
                {"sold_dump": True, "sell_scrap_keep": 100.0},
                also_kept=False,
            ),
            "kept_open",
        )
        self.assertIsNone(
            reclaim_arm_block(
                {"sold_dump": True, "sell_scrap_keep": 0.0},
                also_kept=False,
            )
        )
        self.assertIsNone(
            reclaim_arm_block(
                {
                    "sold_dump": True,
                    "sell_scrap_keep": 100.0,
                    "sell_dump_kept_done": True,
                },
                also_kept=True,
            )
        )

    def test_defaults_are_off_with_the_stop_on(self):
        defaults = _assign("DEFAULTS")
        self.assertIs(defaults["reclaim_enabled"], False)
        self.assertIs(defaults["reclaim_stop_enabled"], True)
        self.assertEqual(defaults["reclaim_entry_persist_s"], 0.5)
        self.assertEqual(defaults["reclaim_stop_persist_s"], 0.5)
        self.assertIn("reclaim off", sell_plan_banner({"sell_enabled": True}))
        text = sell_plan_banner(
            {"sell_enabled": True, "reclaim_enabled": True}
        )
        self.assertIn("reclaim $100 ask>=91c persist 0.5s stop<=75c/0.5s", text)

    def test_entry_persist_allows_zero_and_rejects_negative(self):
        from buy.mint_gas import validate_mint_gas
        from buy.mint_redeem import validate_redeem
        from buy.mint_sequence import validate_seq

        validate = _fn(
            "validate_strategy",
            {
                "validate_mint_gas": validate_mint_gas,
                "validate_seq": validate_seq,
                "validate_redeem": validate_redeem,
            },
        )
        defaults = _assign("DEFAULTS")
        validate(dict(defaults, reclaim_entry_persist_s=0))
        validate(dict(defaults, reclaim_stop_persist_s=0))
        with self.assertRaises(ValueError) as caught:
            validate(dict(defaults, reclaim_entry_persist_s=-0.01))
        self.assertIn("reclaim_entry_persist_s", str(caught.exception))

    def test_done_bag_stays_cold_until_reclaim_hot(self):
        done = {
            "status": "confirmed",
            "end_ts": 100.0,
            "sold_loser": True,
            "sold_dump": True,
            "sold_winner": True,
        }
        self.assertFalse(sell_intent_hot(done, now_s=20.0))
        done["reclaim_hot"] = True
        self.assertTrue(sell_intent_hot(done, now_s=20.0))
        self.assertEqual(
            cycle_sleep_s(
                {"poll_s": 5.0, "sell_armed_poll_s": 0.2},
                {"intents": {"a": done}},
                now_s=20.0,
            ),
            0.2,
        )


class LoopTests(unittest.TestCase):
    def test_scrap_only_and_incomplete_dump_do_not_buy(self):
        loop = _Loop()
        end = loop.clock["now"] + 200.0
        scrap = _held_after_scrap(end)
        loop.book = _qualifying()
        loop.tick(_reclaim_cfg(), scrap)
        self.assertEqual(loop.buys, [])
        self.assertFalse(scrap.get("reclaim_hot"))
        self.assertFalse(scrap.get("reclaim_armed"))
        self.assertEqual(loop.events_named("reclaim_skip")[0]["reason"], "no_dump")

        pending = _dumped(end, sell_dump_kept_done=False, sell_scrap_keep=100.0)
        loop.tick(_reclaim_cfg(sell_dump_also_kept=True), pending)
        self.assertTrue(pending.get("reclaim_hot"))
        self.assertFalse(pending.get("reclaim_armed"))
        self.assertEqual(loop.events_named("reclaim_skip")[-1]["reason"], "kept_pending")
        self.assertEqual(loop.buys, [])

        opened = _dumped(
            end, sell_dump_kept_done=False, sell_scrap_keep=100.0,
        )
        loop.tick(_reclaim_cfg(sell_dump_also_kept=False), opened)
        self.assertFalse(opened.get("reclaim_hot"))
        self.assertEqual(loop.events_named("reclaim_skip")[-1]["reason"], "kept_open")

    def test_complete_dump_stays_on_the_armed_poll(self):
        loop = _Loop()
        end = loop.clock["now"] + 200.0
        intent = _dumped(end)
        loop.book = {
            "up": _row(0.50, 0.51),
            "dn": _row(0.49, 0.50),
        }
        state = loop.tick(_reclaim_cfg(), intent)
        self.assertTrue(intent.get("reclaim_armed"))
        self.assertTrue(intent.get("reclaim_hot"))
        self.assertEqual(loop.buys, [])
        self.assertEqual(loop.events_named("reclaim_skip")[0]["reason"], "no_entry")
        self.assertEqual(
            cycle_sleep_s(_reclaim_cfg(), state, loop.clock["now"]),
            0.2,
        )
        loop.tick(_reclaim_cfg(), intent)
        self.assertEqual(len(loop.events_named("reclaim_skip")), 1)

    def test_disabled_clears_hot_and_does_not_buy(self):
        loop = _Loop()
        end = loop.clock["now"] + 200.0
        intent = _dumped(end, reclaim_hot=True)
        _arm_entry(intent, loop.clock["now"])
        loop.book = _qualifying()
        state = loop.tick(_reclaim_cfg(reclaim_enabled=False), intent)
        self.assertEqual(loop.buys, [])
        self.assertFalse(intent.get("reclaim_hot"))
        self.assertEqual(
            cycle_sleep_s(
                _reclaim_cfg(reclaim_enabled=False), state, loop.clock["now"],
            ),
            5.0,
        )

    def test_guards_block_the_buy_until_every_check_holds(self):
        loop = _Loop()
        end = loop.clock["now"] + 200.0
        intent = _dumped(end)
        cfg = _reclaim_cfg()
        cases = [
            ("wick", _qualifying(up=(0.90, 0.94, 500.0, None), dn=(0.07, 0.08, 500.0, None)), "wick_unconfirmed"),
            ("stale", _qualifying(up=(0.93, 0.94, 500.0, 6.0)), "stale_book"),
            ("empty", {"up": _row(None, None), "dn": _row(0.06, 0.07)}, "empty_book"),
            ("crossed", _qualifying(up=(0.95, 0.93, 500.0, None)), "crossed"),
            ("thin", _qualifying(up=(0.93, 0.94, 10.0, None)), "thin_depth"),
        ]
        for _name, book, reason in cases:
            loop.book = book
            loop.clock["now"] += 1.0
            intent.pop("reclaim_entry_armed_at", None)
            intent.pop("reclaim_entry_leg", None)
            intent.pop("reclaim_skip_reason", None)
            loop.tick(cfg, intent)
            self.assertEqual(loop.buys, [], reason)
            self.assertEqual(loop.events_named("reclaim_skip")[-1]["reason"], reason)
        thin = [row for row in loop.events_named("reclaim_skip") if row["reason"] == "thin_depth"]
        self.assertTrue(thin[-1]["dumped_leg"])

    def test_one_buy_after_persist_ignores_cooldown_and_does_not_rebuy(self):
        loop = _Loop()
        now = loop.clock["now"]
        end = now + 200.0
        intent = _dumped(end, last_sell_attempt_at=now)
        loop.book = _qualifying()
        cfg = _reclaim_cfg()
        loop.tick(cfg, intent)
        self.assertEqual(loop.buys, [])
        self.assertEqual(intent.get("reclaim_entry_armed_at"), now)
        loop.clock["now"] = now + 0.49
        loop.tick(cfg, intent)
        self.assertEqual(loop.buys, [])
        loop.clock["now"] = now + 0.5
        before_fetch = len(loop.fetches)
        before_inv = len(loop.inventories)
        before_sleep = len(loop.sleeps)
        loop.tick(cfg, intent)
        self.assertEqual(len(loop.buys), 1)
        self.assertEqual(loop.buys[0]["token_id"], "up-tok")
        self.assertAlmostEqual(loop.buys[0]["price"], 0.94)
        self.assertAlmostEqual(loop.buys[0]["size"], 106.0)
        self.assertTrue(intent.get("reclaim_bought"))
        self.assertFalse(intent.get("reclaim_buy_inflight"))
        self.assertEqual(len(loop.fetches), before_fetch)
        self.assertEqual(len(loop.inventories), before_inv)
        self.assertEqual(len(loop.sleeps), before_sleep)
        self.assertFalse(any(row["event"] == "reclaim_paper" for row in loop.events))
        loop.clock["now"] = now + 0.7
        loop.tick(cfg, intent)
        self.assertEqual(len(loop.buys), 1)
        self.assertTrue(intent.get("reclaim_hot"))

    def test_spike_resets_and_a_miss_stays_on_the_same_leg(self):
        loop = _Loop()
        now = loop.clock["now"]
        end = now + 200.0
        intent = _dumped(end)
        cfg = _reclaim_cfg()
        loop.book = _qualifying()
        loop.tick(cfg, intent)
        armed = intent["reclaim_entry_armed_at"]
        loop.book = {"up": _row(0.50, 0.51), "dn": _row(0.49, 0.50)}
        loop.clock["now"] = now + 1.0
        loop.tick(cfg, intent)
        self.assertIsNone(intent.get("reclaim_entry_armed_at"))
        self.assertNotEqual(intent.get("reclaim_entry_armed_at"), armed)
        loop.book = _qualifying()
        loop.clock["now"] = now + 1.1
        loop.tick(cfg, intent)
        self.assertEqual(loop.buys, [])
        self.assertEqual(intent.get("reclaim_entry_armed_at"), now + 1.1)

        loop2 = _Loop(buy_result=lambda _shares, _price: (0.0, "unmatched"))
        loop2.clock["now"] = now
        intent2 = _dumped(end)
        _arm_entry(intent2, now)
        loop2.book = _qualifying()
        loop2.tick(cfg, intent2)
        self.assertEqual(len(loop2.buys), 1)
        self.assertEqual(intent2.get("reclaim_leg"), "up")
        self.assertFalse(intent2.get("reclaim_bought"))
        loop2.book = _qualifying(
            up=(0.06, 0.07, 500.0, None),
            dn=(0.93, 0.94, 500.0, None),
        )
        loop2.clock["now"] = now + 0.2
        loop2.tick(cfg, intent2)
        self.assertEqual(len(loop2.buys), 1)
        self.assertEqual(intent2.get("reclaim_leg"), "up")
        self.assertNotEqual(loop2.events_named("reclaim_skip")[-1]["leg"], "dn")

    def test_dry_run_marks_state_and_does_not_touch_the_client(self):
        loop = _Loop(real_buy=True)
        now = loop.clock["now"]
        intent = _dumped(now + 200.0)
        _arm_entry(intent, now)
        loop.book = _qualifying()
        cfg = _reclaim_cfg(dry_run=True)
        loop.tick(cfg, intent)
        self.assertEqual(loop.client_calls, [])
        self.assertTrue(intent.get("reclaim_bought"))
        self.assertTrue(intent.get("reclaim_buy_dry"))
        buy = loop.events_named("reclaim_buy")
        self.assertEqual(len(buy), 1)
        self.assertTrue(buy[0]["dry_run"])
        self.assertAlmostEqual(buy[0]["price"], 0.94)
        self.assertFalse(any("reclaim_paper" in row["event"] for row in loop.events))
        self.assertNotIn("reclaim_paper", MINT.read_text())
        loop.clock["now"] = now + 0.2
        loop.tick(cfg, intent)
        self.assertEqual(len(loop.events_named("reclaim_buy")), 1)

    def test_uncertain_post_does_not_double_buy(self):
        loop = _Loop(buy_result=lambda _s, _p: (0.0, "error:timeout"))
        now = loop.clock["now"]
        intent = _dumped(now + 200.0)
        _arm_entry(intent, now)
        loop.book = _qualifying()
        cfg = _reclaim_cfg()
        loop.tick(cfg, intent)
        self.assertEqual(len(loop.buys), 1)
        self.assertTrue(intent.get("reclaim_buy_uncertain"))
        self.assertEqual(loop.inventories, [])
        loop.clock["now"] = now + 0.2
        loop.tick(cfg, intent)
        self.assertEqual(len(loop.buys), 1)
        self.assertEqual(len(loop.inventories), 1)

    def test_stop_persists_then_chases_the_live_bid(self):
        loop = _Loop()
        now = loop.clock["now"]
        intent = _dumped(now + 200.0)
        _arm_entry(intent, now)
        loop.book = _qualifying()
        cfg = _reclaim_cfg()
        loop.tick(cfg, intent)
        self.assertTrue(intent.get("reclaim_bought"))
        self.assertEqual(loop.sells, [])
        loop.book = _qualifying(up=(0.74, 0.75, 500.0, None), dn=(0.25, 0.26, 500.0, None))
        loop.clock["now"] = now + 0.2
        loop.tick(cfg, intent)
        self.assertEqual(loop.sells, [])
        self.assertEqual(intent.get("reclaim_stop_armed_at"), now + 0.2)
        loop.clock["now"] = now + 0.2 + 0.5
        before_sleep = len(loop.sleeps)
        loop.tick(cfg, intent)
        self.assertGreaterEqual(len(loop.sells), 1)
        self.assertAlmostEqual(loop.sells[0]["price"], 0.74)
        self.assertEqual(loop.sells[0]["token_id"], "up-tok")
        self.assertNotAlmostEqual(loop.sells[0]["price"], 0.02)
        self.assertEqual(loop.sleeps[before_sleep:], [])
        self.assertTrue(intent.get("reclaim_stopped"))
        self.assertFalse(intent.get("reclaim_hot"))
        state = {"intents": {"cid-reclaim": intent}}
        self.assertEqual(cycle_sleep_s(cfg, state, loop.clock["now"]), 5.0)
        stop = loop.events_named("reclaim_stop")
        self.assertTrue(stop)
        self.assertFalse(stop[-1]["dry_run"])

    def test_stop_ladder_floors_at_one_cent_after_a_miss(self):
        order: list = []

        def sell_result(_size, price):
            order.append(("sell", round(float(price), 4)))
            return 0.0, "no orders found"

        loop = _Loop(sell_result=sell_result)
        loop.ns["time"] = SimpleNamespace(
            time=lambda: loop.clock["now"],
            sleep=lambda delay, *_a, **_k: order.append(("sleep", delay)),
        )
        now = loop.clock["now"]
        intent = _dumped(now + 200.0)
        intent.update(
            reclaim_armed=True,
            reclaim_bought=True,
            reclaim_leg="up",
            reclaim_filled=106.0,
            reclaim_target=106.0,
            reclaim_stop_latched=True,
            reclaim_hot=True,
        )
        loop.book = _qualifying(up=(0.03, 0.04, 500.0, None))
        loop.tick(
            _reclaim_cfg(sell_dump_fak_retries=1, sell_dump_ladder_step=0.04, sell_dump_ladder_rungs=4),
            intent,
        )
        self.assertEqual(order[0], ("sell", 0.03))
        prices = [price for kind, price in order if kind == "sell"]
        self.assertIn(0.01, prices)
        self.assertNotIn(0.02, prices)

    def test_stop_off_holds_to_resolution(self):
        loop = _Loop()
        now = loop.clock["now"]
        intent = _dumped(now + 200.0)
        _arm_entry(intent, now)
        loop.book = _qualifying(up=(0.70, 0.71, 500.0, None))
        # Entry will not buy at 0.70. Seed a filled buy and let the stop path run.
        intent.update(
            reclaim_armed=True,
            reclaim_bought=True,
            reclaim_leg="up",
            reclaim_filled=100.0,
            reclaim_target=100.0,
            reclaim_hot=True,
        )
        state = loop.tick(_reclaim_cfg(reclaim_stop_enabled=False), intent)
        self.assertEqual(loop.sells, [])
        self.assertEqual(loop.events_named("reclaim_hold")[0]["leg"], "up")
        self.assertFalse(intent.get("reclaim_hot"))
        self.assertFalse(intent.get("reclaim_stopped"))
        self.assertEqual(cycle_sleep_s(_reclaim_cfg(reclaim_stop_enabled=False), state, now), 5.0)

    def test_buy_and_stop_latency_on_the_firing_tick(self):
        loop = _Loop()
        marks: dict = {}
        real_entry = loop.ns["reclaim_entry_decision"]
        real_stop = loop.ns["reclaim_stop_decision"]
        real_buy = loop.ns["_fak_buy"]
        real_sell = loop.ns["_fak_sell"]

        def entry(**kwargs):
            out = real_entry(**kwargs)
            if out.get("action") == "buy":
                marks["buy_decision"] = time.perf_counter()
            return out

        def stop(**kwargs):
            out = real_stop(**kwargs)
            if out.get("action") == "sell":
                marks["stop_decision"] = time.perf_counter()
            return out

        def fak_buy(*args, **kwargs):
            marks["buy_posted"] = time.perf_counter()
            return real_buy(*args, **kwargs)

        def fak_sell(*args, **kwargs):
            marks.setdefault("stop_posted", time.perf_counter())
            return real_sell(*args, **kwargs)

        loop.ns["reclaim_entry_decision"] = entry
        loop.ns["reclaim_stop_decision"] = stop
        loop.ns["_fak_buy"] = fak_buy
        loop.ns["_fak_sell"] = fak_sell
        now = loop.clock["now"]
        intent = _dumped(now + 200.0)
        _arm_entry(intent, now)
        loop.book = _qualifying()
        fetches_before = len(loop.fetches)
        inv_before = len(loop.inventories)
        loop.tick(_reclaim_cfg(), intent)
        buy_s = marks["buy_posted"] - marks["buy_decision"]
        self.assertLess(buy_s, 0.05)
        self.assertEqual(len(loop.fetches), fetches_before)
        self.assertEqual(len(loop.inventories), inv_before)
        self.assertEqual(loop.sleeps, [])
        self.assertEqual(len(loop.buys), 1)

        loop.book = _qualifying(up=(0.40, 0.41, 500.0, None))
        intent["reclaim_stop_armed_at"] = loop.clock["now"] - 0.5
        loop.tick(_reclaim_cfg(), intent)
        stop_s = marks["stop_posted"] - marks["stop_decision"]
        self.assertLess(stop_s, 0.05)
        self.assertAlmostEqual(loop.sells[0]["price"], 0.40)
        self.assertEqual(loop.sleeps, [])
        ART.mkdir(parents=True, exist_ok=True)
        ART.joinpath("reclaim_latency.txt").write_text(
            f"buy_decision_to_fak_s {buy_s:.6f}\n"
            f"stop_decision_to_fak_s {stop_s:.6f}\n"
            "scheduling_wait_s <= one sell_armed_poll_s (live 0.2)\n"
            "buy_tick extra_/book 0\n"
            "buy_tick inventory_reads 0\n"
            "buy_tick sleeps 0\n"
            "stop_first_post live bid, floor 0.01, no sleep before that post\n",
            encoding="utf-8",
        )

    def test_tick_source_does_not_sleep_or_refetch_before_the_buy(self):
        src = MINT.read_text(encoding="utf-8")
        tick = src[src.find("def _reclaim_tick"):src.find("\ndef manage_sells")]
        stop = src[src.find("def _reclaim_stop_tick"):src.find("\ndef _reclaim_tick")]
        self.assertNotIn("time.sleep", tick)
        self.assertNotIn("_fetch_book", tick)
        self.assertNotIn("_sell_inventory", tick)
        self.assertNotIn("commit_state", tick)
        self.assertNotIn("time.sleep", stop)
        self.assertIn("_sell_inventory", stop)
        self.assertIn("_run_dump_fak_with_refire", stop)


if __name__ == "__main__":
    unittest.main()
