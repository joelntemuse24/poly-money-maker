"""Other-side ceiling and the reclaim hedge posted with the main buy."""

from __future__ import annotations

import threading
import time
import unittest
from types import SimpleNamespace

from buy.mint_loops import reclaim_hedge_cash_reserve
from buy.mint_sell import (
    RECLAIM_HEDGE_RETRY_BUDGET_S,
    reclaim_complement_max,
    reclaim_entry_decision,
    reclaim_entry_qualify,
    reclaim_hedge_keep_decision,
    reclaim_hedge_plan,
    reclaim_hedge_retry_allowed,
    reclaim_hedge_retry_plan,
    reclaim_wallet_covers,
)
from buy.mint_sequence import seq_busy_bag, seq_exit_open
from test_mint_only_ops import _assign, _fn
from test_mint_reclaim import _Loop, _arm_entry, _dumped, _qualifying, _reclaim_cfg
from test_mint_sequential import ACTIVE, S, MintHarness, _cfg, _market, _prev_bag


class CeilingTests(unittest.TestCase):
    def test_null_ceiling_stays_one_minus_entry(self):
        self.assertAlmostEqual(reclaim_complement_max(0.90), 0.10)
        self.assertAlmostEqual(reclaim_complement_max(0.90, None), 0.10)
        self.assertAlmostEqual(reclaim_complement_max(0.91), 0.09)
        bad, why, _shares = reclaim_entry_qualify(
            leg="up", bid=0.88, ask=0.91, other_bid=0.12, other_ask=0.15,
            entry=0.90, usd=10.0,
        )
        self.assertFalse(bad)
        self.assertEqual(why, "sister_unconfirmed")

    def test_other_max_overrides_the_ceiling(self):
        self.assertAlmostEqual(reclaim_complement_max(0.90, 0.15), 0.15)
        ok, reason, _shares = reclaim_entry_qualify(
            leg="up", bid=0.88, ask=0.91, other_bid=0.12, other_ask=0.15,
            entry=0.90, usd=10.0, other_max=0.15,
        )
        self.assertTrue(ok, reason)
        over, why, _shares = reclaim_entry_qualify(
            leg="up", bid=0.88, ask=0.91, other_bid=0.12, other_ask=0.1501,
            entry=0.90, usd=10.0, other_max=0.15,
        )
        self.assertFalse(over)
        self.assertEqual(why, "sister_unconfirmed")
        decision = reclaim_entry_decision(
            now_s=100.0,
            end_ts=400.0,
            ttm_s=300.0,
            max_ttm_s=0.0,
            entry=0.90,
            usd=10.0,
            persist_s=0.0,
            armed_ts=None,
            armed_leg=None,
            locked_leg=None,
            up_bid=0.88,
            up_ask=0.91,
            dn_bid=0.12,
            dn_ask=0.15,
            other_max=0.15,
            slippage=0.03,
            max_price=0.96,
        )
        self.assertEqual(decision["action"], "buy")
        self.assertEqual(decision["leg"], "up")

    def test_defaults_leave_the_ceiling_and_the_hedge_off(self):
        defaults = _assign("DEFAULTS")
        self.assertIsNone(defaults["reclaim_other_max"])
        self.assertIs(defaults["reclaim_hedge_enabled"], False)
        self.assertEqual(defaults["reclaim_hedge_usd"], 1.0)
        self.assertEqual(defaults["reclaim_hedge_slippage"], 0.02)
        self.assertEqual(defaults["reclaim_hedge_keep_above"], 0.50)
        self.assertEqual(defaults["reclaim_hedge_keep_persist_s"], 1.0)
        self.assertEqual(defaults["reclaim_hedge_retries"], 3)
        self.assertEqual(defaults["reclaim_hedge_max_price"], 0.20)
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
        validate(dict(defaults))
        validate(dict(defaults, reclaim_other_max=None))
        validate(dict(defaults, reclaim_other_max=0.15))
        with self.assertRaises(ValueError):
            validate(dict(defaults, reclaim_other_max=0))
        with self.assertRaises(ValueError):
            validate(dict(defaults, reclaim_hedge_usd=0))
        with self.assertRaises(ValueError):
            validate(dict(defaults, reclaim_hedge_retries=-1))
        with self.assertRaises(ValueError):
            validate(dict(defaults, reclaim_hedge_max_price=0))
        with self.assertRaises(ValueError):
            validate(dict(defaults, reclaim_hedge_max_price=1))


class HedgePlanTests(unittest.TestCase):
    def test_rounds_up_to_the_dollar_and_share_minimums(self):
        cheap = reclaim_hedge_plan(0.15, 1.0, 0.02)
        self.assertTrue(cheap["ok"])
        self.assertAlmostEqual(cheap["limit"], 0.17)
        self.assertEqual(cheap["raw_shares"], 5.0)
        self.assertGreaterEqual(cheap["shares"], 5.0)
        self.assertGreaterEqual(cheap["usdc"], 1.0)
        self.assertTrue(cheap["rounded_up"])
        self.assertAlmostEqual(cheap["shares"], 6.0)
        self.assertAlmostEqual(cheap["usdc"], 1.02)
        rich = reclaim_hedge_plan(0.40, 1.0, 0.02)
        self.assertTrue(rich["ok"])
        self.assertEqual(rich["raw_shares"], 2.0)
        self.assertGreaterEqual(rich["shares"], 5.0)
        self.assertTrue(rich["rounded_up"])
        self.assertGreaterEqual(rich["usdc"], 1.0)
        exact = reclaim_hedge_plan(0.08, 1.0, 0.02)
        self.assertTrue(exact["ok"])
        self.assertEqual(exact["shares"], 10.0)
        self.assertAlmostEqual(exact["usdc"], 1.0)
        self.assertFalse(exact["rounded_up"])
        self.assertTrue(reclaim_wallet_covers(None, 99.84, 1.02))
        self.assertFalse(reclaim_wallet_covers(100.0, 99.84, 1.02))
        self.assertTrue(reclaim_wallet_covers(100.86, 99.84, 1.02))

    def test_retry_plan_keeps_the_minimums_and_stops_above_the_cap(self):
        bumped = reclaim_hedge_retry_plan(0.10, 0.02, 6.0, 0.20)
        self.assertTrue(bumped["ok"])
        self.assertAlmostEqual(bumped["limit"], 0.12)
        self.assertAlmostEqual(bumped["shares"], 9.0)
        self.assertAlmostEqual(bumped["usdc"], 1.08)
        self.assertTrue(bumped["rounded_up"])
        capped = reclaim_hedge_retry_plan(0.19, 0.02, 6.0, 0.20)
        self.assertFalse(capped["ok"])
        self.assertEqual(capped["reason"], "above_cap")
        self.assertAlmostEqual(capped["limit"], 0.21)
        self.assertEqual(reclaim_hedge_retry_plan(None, 0.02, 6.0, 0.20)["reason"], "no_ask")
        self.assertFalse(reclaim_hedge_retry_allowed(10.0, 10.2, 3, 3))
        self.assertFalse(reclaim_hedge_retry_allowed(10.0, 11.1, 0, 3, RECLAIM_HEDGE_RETRY_BUDGET_S))
        self.assertTrue(reclaim_hedge_retry_allowed(10.0, 10.2, 2, 3))


def _wait_hedge(intent, loop, event="reclaim_hedge_buy", timeout=2.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        flagged = intent.get("reclaim_hedge_buy_done") or intent.get("reclaim_hedge_buy_uncertain")
        if flagged and loop.events_named(event):
            return
        time.sleep(0.005)
    raise AssertionError(f"hedge did not finish: {intent.get('reclaim_hedge_buy_done')}")


class HedgeLoopTests(unittest.TestCase):
    def _buy(self, loop, intent, cfg):
        now = loop.clock["now"]
        _arm_entry(intent, now)
        loop.book = _qualifying()
        loop.tick(cfg, intent)
        _wait_hedge(intent, loop)
        return now

    def test_hedge_posts_concurrently_with_the_main(self):
        loop = _Loop()
        now = loop.clock["now"]
        intent = _dumped(now + 200.0)
        state = {"active": 0, "max": 0}
        lock = threading.Lock()
        real = loop.ns["_fak_buy"]

        def fak(token, shares, price, dry_run, capture=None):
            with lock:
                state["active"] += 1
                state["max"] = max(state["max"], state["active"])
            time.sleep(0.05)
            try:
                return real(token, shares, price, dry_run, capture)
            finally:
                with lock:
                    state["active"] -= 1

        loop.ns["_fak_buy"] = fak
        cfg = _reclaim_cfg(reclaim_hedge_enabled=True, reclaim_hedge_usd=1.0)
        self._buy(loop, intent, cfg)
        self.assertEqual(state["max"], 2)
        tokens = {row["token_id"] for row in loop.buys}
        self.assertEqual(tokens, {"up-tok", "dn-tok"})
        main = [row for row in loop.buys if row["token_id"] == "up-tok"]
        hedge = [row for row in loop.buys if row["token_id"] == "dn-tok"]
        self.assertEqual(len(main), 1)
        self.assertEqual(len(hedge), 1)
        self.assertAlmostEqual(main[0]["price"], 0.96)
        self.assertAlmostEqual(hedge[0]["price"], 0.09)
        self.assertGreaterEqual(hedge[0]["size"], 5.0)
        self.assertTrue(intent.get("reclaim_bought"))
        posted = loop.events_named("reclaim_hedge_buy")
        self.assertTrue(posted)
        self.assertTrue(posted[-1]["sent"])
        self.assertGreater(posted[-1]["filled"], 0)
        self.assertTrue(posted[-1]["rounded_up"])

    def test_hedge_failure_does_not_change_the_main_buy(self):
        loop = _Loop()
        now = loop.clock["now"]
        intent = _dumped(now + 200.0)
        real = loop.ns["_fak_buy"]

        def fak(token, shares, price, dry_run, capture=None):
            if token == "dn-tok":
                loop.buys.append(
                    {
                        "token_id": token,
                        "size": float(shares),
                        "price": float(price),
                        "dry_run": dry_run,
                    }
                )
                return 0.0, "error:timeout"
            return real(token, shares, price, dry_run, capture)

        loop.ns["_fak_buy"] = fak
        cfg = _reclaim_cfg(reclaim_hedge_enabled=True)
        self._buy(loop, intent, cfg)
        self.assertTrue(intent.get("reclaim_bought"))
        self.assertGreater(float(intent.get("reclaim_filled") or 0), 0)
        self.assertTrue(intent.get("reclaim_hedge_buy_uncertain"))
        self.assertFalse(intent.get("reclaim_hedge_buy_done"))
        self.assertEqual(loop.events_named("reclaim_hedge_buy")[-1]["status"], "error:timeout")
        self.assertEqual(loop.events_named("reclaim_hedge_buy")[-1]["filled"], 0.0)

    def test_cash_short_skips_the_hedge_and_still_buys_the_main(self):
        loop = _Loop()
        now = loop.clock["now"]
        intent = _dumped(now + 200.0)
        _arm_entry(intent, now)
        loop.book = _qualifying()
        cfg = _reclaim_cfg(reclaim_hedge_enabled=True)
        chain = SimpleNamespace(_pusd_sample={"balance": 50.0})
        state = {"intents": {"cid-reclaim": intent}}
        loop.ns["remember_persisted_state"](state)
        loop.ns["_manage_sells_locked"](cfg, state, chain)
        self.assertEqual([row["token_id"] for row in loop.buys], ["up-tok"])
        self.assertTrue(intent.get("reclaim_bought"))
        self.assertTrue(intent.get("reclaim_hedge_buy_done"))
        self.assertEqual(loop.events_named("reclaim_hedge_buy")[-1]["status"], "cash_short")
        self.assertFalse(loop.events_named("reclaim_hedge_buy")[-1]["sent"])

    def test_keep_above_50_for_one_second_and_sell_when_it_does_not(self):
        kept = _Loop()
        intent = _dumped(kept.clock["now"] + 200.0)
        cfg = _reclaim_cfg(
            reclaim_hedge_enabled=True,
            reclaim_hedge_keep_above=0.50,
            reclaim_hedge_keep_persist_s=1.0,
            reclaim_stop=0.75,
            reclaim_stop_persist_s=0.5,
        )
        now = self._buy(kept, intent, cfg)
        self.assertTrue(intent.get("reclaim_bought"))
        kept.book = _qualifying(
            up=(0.70, 0.72, 500.0, None),
            dn=(0.60, 0.62, 500.0, None),
        )
        kept.clock["now"] = now + 0.2
        kept.tick(cfg, intent)
        self.assertEqual(kept.sells, [])
        kept.clock["now"] = now + 0.7
        kept.tick(cfg, intent)
        self.assertEqual(
            [row["token_id"] for row in kept.sells],
            ["up-tok"],
        )
        decision = kept.events_named("reclaim_hedge_keep_decision")[-1]
        self.assertEqual(decision["action"], "wait")
        self.assertFalse(intent.get("reclaim_hedge_kept"))
        kept.clock["now"] = now + 0.7 + 1.0
        kept.tick(cfg, intent)
        self.assertTrue(intent.get("reclaim_hedge_kept"))
        self.assertFalse(intent.get("reclaim_hedge_sold"))
        self.assertEqual([row["token_id"] for row in kept.sells], ["up-tok"])
        self.assertEqual(kept.events_named("reclaim_hedge_keep_decision")[-1]["action"], "keep")
        self.assertEqual(kept.events_named("reclaim_hedge_sell"), [])

        sold = _Loop()
        intent2 = _dumped(sold.clock["now"] + 200.0)
        now2 = self._buy(sold, intent2, cfg)
        sold.book = _qualifying(
            up=(0.70, 0.72, 500.0, None),
            dn=(0.40, 0.42, 500.0, None),
        )
        sold.clock["now"] = now2 + 0.2
        sold.tick(cfg, intent2)
        sold.clock["now"] = now2 + 0.7
        sold.tick(cfg, intent2)
        self.assertEqual(
            [row["token_id"] for row in sold.sells],
            ["up-tok", "dn-tok"],
        )
        self.assertEqual(sold.events_named("reclaim_hedge_keep_decision")[-1]["reason"], "below")
        self.assertTrue(intent2.get("reclaim_hedge_sold"))
        self.assertTrue(sold.events_named("reclaim_hedge_sell"))

        fade = _Loop()
        intent3 = _dumped(fade.clock["now"] + 200.0)
        now3 = self._buy(fade, intent3, cfg)
        fade.book = _qualifying(
            up=(0.70, 0.72, 500.0, None),
            dn=(0.60, 0.62, 500.0, None),
        )
        fade.clock["now"] = now3 + 0.2
        fade.tick(cfg, intent3)
        fade.clock["now"] = now3 + 0.7
        fade.tick(cfg, intent3)
        self.assertEqual(fade.events_named("reclaim_hedge_keep_decision")[-1]["action"], "wait")
        fade.book = _qualifying(
            up=(0.70, 0.72, 500.0, None),
            dn=(0.40, 0.42, 500.0, None),
        )
        fade.clock["now"] = now3 + 1.1
        fade.tick(cfg, intent3)
        self.assertEqual(
            fade.events_named("reclaim_hedge_keep_decision")[-1]["reason"],
            "not_persisting",
        )
        self.assertTrue(intent3.get("reclaim_hedge_sold"))
        self.assertIn("dn-tok", [row["token_id"] for row in fade.sells])

    def test_keep_decision_helper(self):
        waiting = reclaim_hedge_keep_decision(
            now_s=10.0, bid=0.60, armed_ts=None, keep_above=0.50, persist_s=1.0,
        )
        self.assertEqual(waiting["action"], "wait")
        held = reclaim_hedge_keep_decision(
            now_s=11.0, bid=0.60, armed_ts=10.0, keep_above=0.50, persist_s=1.0,
        )
        self.assertEqual(held["action"], "keep")
        below = reclaim_hedge_keep_decision(
            now_s=10.4, bid=0.50, armed_ts=10.0, keep_above=0.50, persist_s=1.0,
        )
        self.assertEqual(below["action"], "sell")
        self.assertEqual(below["reason"], "not_persisting")


class HedgeDeferTests(unittest.TestCase):
    def test_open_hedge_buy_defers_even_after_the_stop(self):
        cashed = dict(sold_loser=True, sold_dump=True, sold_winner=True)
        for extra in (
            {"reclaim_hedge_buy_inflight": True},
            {"reclaim_hedge_buy_uncertain": True},
            {"reclaim_hedge_buy_posted": True, "reclaim_hedge_buy_done": False},
            {"reclaim_hedge_retrying": True, "reclaim_hedge_buy_inflight": True},
        ):
            bag = _prev_bag(**cashed, reclaim_stopped=True, **extra)
            self.assertTrue(seq_exit_open(bag), extra)
            self.assertIsNotNone(
                seq_busy_bag({"intents": {"0x1": bag}}, S - 30.0, ACTIVE),
                extra,
            )
        released = _prev_bag(
            **cashed,
            reclaim_stopped=True,
            reclaim_hedge_buy_posted=True,
            reclaim_hedge_buy_done=True,
            reclaim_hedge_kept=True,
        )
        self.assertFalse(seq_exit_open(released))
        state = {
            "intents": {
                "0x1": _prev_bag(
                    **cashed, reclaim_stopped=True, reclaim_hedge_buy_inflight=True,
                )
            }
        }
        h = MintHarness(_cfg(), state, [_market(S, 2)], balance=200.0)
        self.assertEqual(h.tick(S - 30.0), "seq_wait_prev")
        self.assertEqual(h.submits, [])

    def test_mint_cash_reserves_the_open_hedge(self):
        open_hedge = _prev_bag(
            reclaim_hedge_buy_inflight=True,
            reclaim_hedge_usdc=5.0,
            end_ts=S,
        )
        self.assertAlmostEqual(
            reclaim_hedge_cash_reserve({"intents": {"0x1": open_hedge}}),
            5.0,
        )
        done = dict(open_hedge)
        done["reclaim_hedge_buy_done"] = True
        self.assertEqual(reclaim_hedge_cash_reserve({"intents": {"0x1": done}}), 0.0)
        state = {"intents": {"0x1": open_hedge}}
        h = MintHarness(_cfg(), state, [_market(S, 2)], balance=200.0)
        self.assertEqual(h.tick(S + 1.0), "seq_wait_cash")
        self.assertEqual(h.submits, [])
        self.assertEqual(h.named("mint_seq_wait_cash")[0]["cash_reason"], "pending_reserve")
        clear = {"intents": {"0x1": done}}
        h2 = MintHarness(_cfg(), clear, [_market(S, 2)], balance=200.0)
        self.assertEqual(h2.tick(S + 1.0), "submitted")


class HedgeRetryTests(unittest.TestCase):
    def _arm(self, loop, intent, cfg):
        _arm_entry(intent, loop.clock["now"])
        loop.book = _qualifying()
        loop.ns["remember_persisted_state"]({"intents": {"cid-reclaim": intent}})
        loop.ns["_manage_sells_locked"](cfg, {"intents": {"cid-reclaim": intent}}, object())

    def test_miss_then_fill_on_retry(self):
        loop = _Loop()
        intent = _dumped(loop.clock["now"] + 200.0)
        posts = {"n": 0}
        real = loop.ns["_fak_buy"]

        def fak(token, shares, price, dry_run, capture=None):
            if token != "dn-tok":
                return real(token, shares, price, dry_run, capture)
            posts["n"] += 1
            loop.buys.append(
                {"token_id": token, "size": float(shares), "price": float(price), "dry_run": dry_run}
            )
            if posts["n"] == 1:
                return 0.0, "reject:400:no orders found to match with FAK order"
            return float(shares), "matched"

        def fetch_book(*args, **_kwargs):
            loop.fetches.append(args)
            return (0.09, 100.0, [], 0.10, 100.0, [], None)

        loop.ns["_fak_buy"] = fak
        loop.ns["_fetch_book"] = fetch_book
        cfg = _reclaim_cfg(reclaim_hedge_enabled=True, reclaim_hedge_usd=1.0)
        self._arm(loop, intent, cfg)
        _wait_hedge(intent, loop, "reclaim_hedge_outcome")
        hedge = [row for row in loop.buys if row["token_id"] == "dn-tok"]
        self.assertEqual(len(hedge), 2)
        self.assertAlmostEqual(hedge[0]["price"], 0.09)
        self.assertAlmostEqual(hedge[1]["price"], 0.12)
        self.assertAlmostEqual(hedge[1]["size"], 12.0)
        retry = loop.events_named("reclaim_hedge_retry")
        self.assertEqual(len(retry), 1)
        self.assertEqual(retry[0]["attempt"], 1)
        self.assertAlmostEqual(retry[0]["px"], 0.12)
        self.assertAlmostEqual(retry[0]["ask"], 0.10)
        self.assertEqual(retry[0]["filled"], 0.0)
        self.assertAlmostEqual(retry[0]["remaining"], 12.0)
        self.assertEqual(loop.events_named("reclaim_hedge_outcome")[-1]["outcome"], "filled")
        self.assertTrue(intent.get("reclaim_bought"))
        self.assertTrue(intent.get("reclaim_hedge_buy_done"))

    def test_partial_then_fill_rounds_the_remainder(self):
        loop = _Loop()
        intent = _dumped(loop.clock["now"] + 200.0)
        posts = {"n": 0}
        real = loop.ns["_fak_buy"]

        def fak(token, shares, price, dry_run, capture=None):
            if token != "dn-tok":
                return real(token, shares, price, dry_run, capture)
            posts["n"] += 1
            loop.buys.append(
                {"token_id": token, "size": float(shares), "price": float(price), "dry_run": dry_run}
            )
            if posts["n"] == 1:
                return float(shares) / 2.0, "matched"
            return float(shares), "matched"

        def fetch_book(*args, **_kwargs):
            return (0.09, 100.0, [], 0.10, 100.0, [], None)

        loop.ns["_fak_buy"] = fak
        loop.ns["_fetch_book"] = fetch_book
        cfg = _reclaim_cfg(reclaim_hedge_enabled=True, reclaim_hedge_usd=1.0)
        self._arm(loop, intent, cfg)
        _wait_hedge(intent, loop, "reclaim_hedge_outcome")
        hedge = [row for row in loop.buys if row["token_id"] == "dn-tok"]
        self.assertEqual(len(hedge), 2)
        self.assertAlmostEqual(hedge[0]["size"], 12.0)
        self.assertAlmostEqual(hedge[1]["price"], 0.12)
        self.assertAlmostEqual(hedge[1]["size"], 9.0)
        retry = loop.events_named("reclaim_hedge_retry")[-1]
        self.assertAlmostEqual(retry["filled"], 6.0)
        self.assertAlmostEqual(retry["remaining"], 6.0)
        self.assertAlmostEqual(retry["px"], 0.12)
        self.assertEqual(loop.events_named("reclaim_hedge_outcome")[-1]["outcome"], "filled")

    def test_cap_stops_retries(self):
        loop = _Loop()
        intent = _dumped(loop.clock["now"] + 200.0)
        real = loop.ns["_fak_buy"]

        def fak(token, shares, price, dry_run, capture=None):
            if token != "dn-tok":
                return real(token, shares, price, dry_run, capture)
            loop.buys.append(
                {"token_id": token, "size": float(shares), "price": float(price), "dry_run": dry_run}
            )
            return 0.0, "no orders found to match with FAK order"

        def fetch_book(*args, **_kwargs):
            return (0.18, 100.0, [], 0.19, 100.0, [], None)

        loop.ns["_fak_buy"] = fak
        loop.ns["_fetch_book"] = fetch_book
        cfg = _reclaim_cfg(reclaim_hedge_enabled=True)
        self._arm(loop, intent, cfg)
        _wait_hedge(intent, loop, "reclaim_hedge_outcome")
        hedge = [row for row in loop.buys if row["token_id"] == "dn-tok"]
        self.assertEqual(len(hedge), 1)
        retry = loop.events_named("reclaim_hedge_retry")
        self.assertEqual(len(retry), 1)
        self.assertAlmostEqual(retry[0]["ask"], 0.19)
        self.assertAlmostEqual(retry[0]["px"], 0.21)
        self.assertEqual(retry[0]["status"], "above_cap")
        self.assertEqual(loop.events_named("reclaim_hedge_outcome")[-1]["outcome"], "above_cap")
        self.assertTrue(intent.get("reclaim_bought"))

    def test_retries_do_not_delay_the_main_buy(self):
        loop = _Loop()
        intent = _dumped(loop.clock["now"] + 200.0)
        real = loop.ns["_fak_buy"]
        entered = threading.Event()
        release = threading.Event()

        def fak(token, shares, price, dry_run, capture=None):
            if token == "up-tok":
                time.sleep(0.05)
                return real(token, shares, price, dry_run, capture)
            loop.buys.append(
                {"token_id": token, "size": float(shares), "price": float(price), "dry_run": dry_run}
            )
            if not entered.is_set() and len([row for row in loop.buys if row["token_id"] == "dn-tok"]) == 1:
                return 0.0, "no orders found to match with FAK order"
            entered.set()
            release.wait(2.0)
            return float(shares), "matched"

        def fetch_book(*args, **_kwargs):
            return (0.09, 100.0, [], 0.10, 100.0, [], None)

        loop.ns["_fak_buy"] = fak
        loop.ns["_fetch_book"] = fetch_book
        cfg = _reclaim_cfg(reclaim_hedge_enabled=True)
        _arm_entry(intent, loop.clock["now"])
        loop.book = _qualifying()
        started = time.time()
        loop.tick(cfg, intent)
        elapsed = time.time() - started
        self.assertLess(elapsed, 0.25)
        self.assertTrue(entered.is_set())
        self.assertTrue(intent.get("reclaim_bought"))
        self.assertFalse(intent.get("reclaim_hedge_buy_done"))
        self.assertTrue(
            intent.get("reclaim_hedge_retrying") or intent.get("reclaim_hedge_buy_inflight")
        )
        self.assertTrue(seq_exit_open(intent))
        self.assertGreater(reclaim_hedge_cash_reserve({"intents": {"cid-reclaim": intent}}), 0.0)
        open_bag = _prev_bag(
            reclaim_stopped=True,
            sold_loser=True,
            sold_dump=True,
            sold_winner=True,
            reclaim_hedge_retrying=True,
            reclaim_hedge_buy_inflight=True,
            reclaim_hedge_usdc=intent.get("reclaim_hedge_usdc"),
        )
        self.assertTrue(seq_exit_open(open_bag))
        h = MintHarness(_cfg(), {"intents": {"0x1": open_bag}}, [_market(S, 2)], balance=200.0)
        self.assertEqual(h.tick(S - 30.0), "seq_wait_prev")
        self.assertEqual(h.submits, [])
        closed = dict(open_bag)
        closed["end_ts"] = S - 10.0
        self.assertIsNone(seq_busy_bag({"intents": {"0x1": closed}}, S, ACTIVE))
        release.set()
        _wait_hedge(intent, loop, "reclaim_hedge_outcome")
        self.assertEqual(loop.events_named("reclaim_hedge_outcome")[-1]["outcome"], "filled")


if __name__ == "__main__":
    unittest.main()
