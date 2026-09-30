"""Post-dump kept-loser stop: arm/persist/fire, partial fills, gates, alerts."""

from __future__ import annotations

import ast
import json
import tempfile
import unittest
from pathlib import Path

from buy.mint_gas import validate_mint_gas
from buy.mint_sell import (
    kept_loser_open,
    post_dump_kept_stop_knobs,
    post_dump_kept_stop_plan,
)
from buy.whatsapp_notify import BagAlerts, WhatsAppNotifier, bag_start_label, kept_stop_message
from test_mint_cpu import _dump_cfg, _dump_harness, _fill_fak, _held_after_scrap, _load

ROOT = Path(__file__).resolve().parents[1]
STOP_EVENTS = ("post_dump_stop_armed", "post_dump_stop_fill", "post_dump_stop_miss", "post_dump_stop_flat")


def _dumped_bag(end_ts: float, **extra) -> dict:
    """Live config shape: 125 minted, 62 DN scrapped, 63 DN kept, UP dumped."""
    row = _held_after_scrap(
        end_ts,
        shares=125.0,
        sell_filled=62.0,
        sell_scrap_target=62.0,
        sell_scrap_keep=63.0,
        sold_dump=True,
        sold_winner=True,
        sell_dump_leg="up",
        sell_dump_filled=125.0,
        sell_dump_fill_px=0.37,
        sold_dump_at=end_ts - 200.0,
    )
    row.update(extra)
    return row


def _side(px):
    if px is None:
        return (None, 0.0, [])
    return (px, 80.0, [{"price": str(px), "size": "80"}])


class Harness:
    """Real ``_manage_sells_locked`` with a mutable book and a scripted FAK."""

    def __init__(self, *, up=0.70, dn=0.30, fill_px=0.29, alerts=None):
        self.ns, self.events, _calls, self.clock = _dump_harness(_fill_fak)
        self.book = {"up": _side(up), "dn": _side(dn)}
        self.ns["_fetch_books"] = lambda *_a: (self.book["up"], self.book["dn"])
        self.ns["_fetch_book"] = lambda token, _min: self.book["up" if token == "up-tok" else "dn"]
        self.calls: list = []
        self.budget: list = []
        self.fill_px = fill_px

        def fak(token_id, size, price, dry_run, capture=None):
            self.calls.append({"token_id": token_id, "size": float(size), "price": float(price), "dry_run": dry_run})
            if dry_run:
                return 0.0, "dry"
            cap = self.budget.pop(0) if self.budget else float(size)
            sold = min(float(size), cap)
            if sold <= 0:
                return 0.0, "error:no orders found to match with FAK order"
            if capture is not None:
                capture.append({"takingAmount": str(round(sold * self.fill_px, 6))})
            return sold, "matched"

        self.ns["_fak_sell"] = fak
        if alerts is not None:
            self.ns["_BAG_ALERTS"] = alerts

    def set(self, leg, px):
        self.book[leg] = _side(px)

    def tick(self, cfg, intent, n=1, step=1.0):
        for _ in range(n):
            state = {"intents": {"cid-stop": intent}}
            self.ns["remember_persisted_state"](state)
            self.ns["_manage_sells_locked"](cfg, state, object())
            self.clock["now"] += step

    def named(self, name):
        return [e for e in self.events if e["event"] == name]

    def stop_events(self):
        return [e for e in self.events if e["event"] in STOP_EVENTS]


def _cfg(**extra):
    return _dump_cfg(post_dump_kept_stop=True, **extra)


class KnobAndPlanTests(unittest.TestCase):
    def test_defaults_off_and_bad_values_fall_back(self):
        self.assertEqual(post_dump_kept_stop_knobs({}), (False, 0.40, 3.0, 0.0))
        self.assertEqual(post_dump_kept_stop_knobs(None), (False, 0.40, 3.0, 0.0))
        self.assertEqual(
            post_dump_kept_stop_knobs({"post_dump_kept_stop": True, "post_dump_kept_stop_px": 0.35,
                                       "post_dump_kept_stop_hold_s": 0, "post_dump_kept_stop_max_ttm_s": 120}),
            (True, 0.35, 0.0, 120.0),
        )
        self.assertEqual(
            post_dump_kept_stop_knobs({"post_dump_kept_stop": "false", "post_dump_kept_stop_px": 1.5,
                                       "post_dump_kept_stop_hold_s": -2, "post_dump_kept_stop_max_ttm_s": "x"}),
            (False, 0.40, 3.0, 0.0),
        )
        self.assertEqual(post_dump_kept_stop_knobs({"post_dump_kept_stop": "on"})[0], True)
        self.assertEqual(post_dump_kept_stop_knobs({"post_dump_kept_stop_px": True})[1], 0.40)

    def test_plan_requires_a_filled_dump_and_kept_shares(self):
        end = 1_000.0
        plan = lambda intent, bid=0.3, ttm=100.0, cutoff=0.0: post_dump_kept_stop_plan(  # noqa: E731
            intent, bid=bid, stop_px=0.40, ttm_s=ttm, max_ttm_s=cutoff, tol=0.01,
        )
        self.assertEqual(plan(_dumped_bag(end)), ("dn", 63.0, True, "below_stop"))
        self.assertEqual(plan(_dumped_bag(end, sold_dump=False))[2:], (False, "no_dump_fill"))
        self.assertEqual(plan(_dumped_bag(end, sell_dump_leg=None))[2:], (False, "no_dump_fill"))
        self.assertEqual(plan(_dumped_bag(end, sell_dump_leg="dn"))[2:], (False, "no_dump_fill"))
        self.assertEqual(plan(_dumped_bag(end, sell_scrap_keep=None))[2:], (False, "no_kept_shares"))
        self.assertEqual(plan(_dumped_bag(end, post_dump_stop_filled=63.0))[2:], (False, "no_kept_shares"))
        self.assertEqual(plan(_dumped_bag(end, post_dump_stop_filled=40.0))[1:3], (23.0, True))
        self.assertEqual(plan(_dumped_bag(end, post_dump_stop_done=True))[2:], (False, "done"))
        self.assertEqual(plan(_dumped_bag(end), bid=0.40)[2:], (False, "at_or_above_stop"))
        self.assertEqual(plan(_dumped_bag(end), bid=None)[2:], (False, "no_bid"))
        self.assertEqual(plan(_dumped_bag(end), ttm=200.0, cutoff=60.0)[2:], (False, "time_gated"))
        self.assertEqual(plan(_dumped_bag(end), ttm=None, cutoff=60.0)[2:], (False, "time_gated"))
        self.assertTrue(plan(_dumped_bag(end), ttm=None, cutoff=0.0)[2])
        self.assertEqual(plan(None)[2:], (False, "no_intent"))
        self.assertEqual(plan({"sold_leg": None})[2:], (False, "no_kept_leg"))

    def test_kept_loser_closed_after_stop(self):
        self.assertTrue(kept_loser_open(_dumped_bag(1_000.0)))
        self.assertFalse(kept_loser_open(_dumped_bag(1_000.0, post_dump_stop_done=True)))

    def test_defaults_example_and_reload_with_missing_or_unknown_keys(self):
        tree = ast.parse((ROOT / "mintbot.py").read_text(encoding="utf-8"))
        defaults = next(
            ast.literal_eval(node.value)
            for node in tree.body
            if isinstance(node, ast.Assign)
            and any(isinstance(t, ast.Name) and t.id == "DEFAULTS" for t in node.targets)
        )
        expect = {
            "post_dump_kept_stop": False,
            "post_dump_kept_stop_px": 0.40,
            "post_dump_kept_stop_hold_s": 3.0,
            "post_dump_kept_stop_max_ttm_s": 0.0,
        }
        example = json.loads((ROOT / "strategy_mint.example.json").read_text(encoding="utf-8"))
        for key, value in expect.items():
            self.assertEqual(defaults[key], value, key)
            self.assertEqual(example[key], value, key)
        folder = Path(tempfile.mkdtemp(prefix="strategy-"))
        for extra in ({}, {"post_dump_kept_stop_typo": 1}, {"post_dump_kept_stop": True}):
            raw = {k: v for k, v in example.items() if not k.startswith("post_dump_")}
            raw.update(extra)
            path = folder / "strategy_mint.json"
            path.write_text(json.dumps(raw), encoding="utf-8")
            ns = _load("load_strategy", "validate_strategy", extras={
                "STRATEGY_FILE": path, "DEFAULTS": defaults, "validate_mint_gas": validate_mint_gas,
            })
            cfg = ns["load_strategy"]()
            self.assertIs(cfg["post_dump_kept_stop"], bool(extra.get("post_dump_kept_stop")))
            self.assertEqual(cfg["post_dump_kept_stop_px"], 0.40)
            self.assertNotIn("post_dump_kept_stop_typo", cfg)


class SellLoopTests(unittest.TestCase):
    def test_off_by_default(self):
        h = Harness()
        intent = _dumped_bag(h.clock["now"] + 200.0)
        h.tick(_dump_cfg(), intent, 10)
        self.assertEqual(h.calls, [])
        self.assertEqual(h.stop_events(), [])
        self.assertNotIn("post_dump_stop_armed_at", intent)

    def test_fires_after_three_seconds_continuously_below(self):
        h = Harness()
        end = h.clock["now"] + 200.0
        intent = _dumped_bag(end)
        h.tick(_cfg(), intent, 3)
        self.assertEqual(h.calls, [])
        armed = h.named("post_dump_stop_armed")
        self.assertEqual(len(armed), 1)
        self.assertEqual(
            {k: armed[0][k] for k in ("leg", "bid", "below", "hold_s", "kept", "dump_leg", "dump_px")},
            {"leg": "dn", "bid": 0.30, "below": 0.40, "hold_s": 3.0, "kept": 63.0, "dump_leg": "up", "dump_px": 0.37},
        )
        h.tick(_cfg(), intent, 1)
        self.assertEqual(h.calls, [{"token_id": "dn-tok", "size": 63.0, "price": 0.30, "dry_run": False}])
        self.assertTrue(intent["post_dump_stop_done"])
        self.assertEqual(intent["post_dump_stop_filled"], 63.0)
        self.assertEqual(intent["post_dump_stop_fill_px"], 0.29)
        self.assertEqual(intent["post_dump_stop_limit"], 0.30)
        self.assertIsNone(intent["post_dump_stop_armed_at"])
        fills = h.named("post_dump_stop_fill")
        self.assertEqual(len(fills), 1)
        self.assertEqual(
            {k: fills[0][k] for k in ("leg", "sold", "avg_px", "limit", "bid", "status", "done", "remaining", "ttm", "dry_run")},
            {"leg": "dn", "sold": 63.0, "avg_px": 0.29, "limit": 0.30, "bid": 0.30, "status": "matched",
             "done": True, "remaining": 0.0, "ttm": 197.0, "dry_run": False},
        )
        self.assertEqual(intent["sell_dump_filled"], 125.0)

    def test_recovery_at_two_seconds_resets_the_timer(self):
        h = Harness()
        intent = _dumped_bag(h.clock["now"] + 200.0)
        h.tick(_cfg(), intent, 2)
        h.set("dn", 0.45)
        h.tick(_cfg(), intent, 1)
        self.assertIsNone(intent["post_dump_stop_armed_at"])
        h.set("dn", 0.30)
        h.tick(_cfg(), intent, 3)
        self.assertEqual(h.calls, [])
        h.set("dn", None)
        h.tick(_cfg(), intent, 1)
        h.set("dn", 0.30)
        h.tick(_cfg(), intent, 3)
        self.assertEqual(h.calls, [])
        h.tick(_cfg(), intent, 1)
        self.assertEqual(len(h.calls), 1)
        self.assertEqual(len(h.named("post_dump_stop_armed")), 3)

    def test_exactly_at_the_stop_price_does_not_arm(self):
        h = Harness(dn=0.40)
        intent = _dumped_bag(h.clock["now"] + 200.0)
        h.tick(_cfg(), intent, 10)
        self.assertEqual(h.calls, [])
        self.assertEqual(h.stop_events(), [])

    def test_never_before_a_dump_fill(self):
        for extra in (
            {"sold_dump": False, "sold_winner": False, "sell_dump_leg": None, "sell_dump_filled": 0.0},
            {"sell_dump_leg": None, "sell_dump_note": "already_flat"},
        ):
            h = Harness(up=0.70, dn=0.30)
            intent = _dumped_bag(h.clock["now"] + 200.0, **extra)
            h.tick(_cfg(), intent, 10)
            self.assertEqual(h.calls, [], extra)
            self.assertEqual(h.stop_events(), [], extra)

    def test_real_dump_then_stop_on_the_kept_shares(self):
        h = Harness(up=0.39, dn=0.58)
        end = h.clock["now"] + 200.0
        intent = _held_after_scrap(end, shares=125.0, sell_filled=62.0, sell_scrap_target=62.0, sell_scrap_keep=63.0)
        cfg = _cfg()
        h.tick(cfg, intent, 3)
        self.assertTrue(intent.get("sold_dump"))
        self.assertEqual(intent["sell_dump_leg"], "up")
        self.assertEqual([c["token_id"] for c in h.calls], ["up-tok"])
        self.assertEqual(h.stop_events(), [])
        h.set("up", 0.70)
        h.set("dn", 0.28)
        h.tick(cfg, intent, 5)
        self.assertEqual([c["token_id"] for c in h.calls], ["up-tok", "dn-tok"])
        self.assertEqual(h.calls[-1]["size"], 63.0)
        self.assertTrue(intent["post_dump_stop_done"])

    def test_once_per_bag(self):
        h = Harness()
        intent = _dumped_bag(h.clock["now"] + 200.0)
        h.tick(_cfg(), intent, 4)
        self.assertEqual(len(h.calls), 1)
        h.set("dn", 0.10)
        h.tick(_cfg(), intent, 20)
        h.set("dn", 0.60)
        h.tick(_cfg(), intent, 2)
        h.set("dn", 0.10)
        h.tick(_cfg(), intent, 20)
        self.assertEqual(len(h.calls), 1)
        self.assertEqual(len(h.named("post_dump_stop_fill")), 1)
        self.assertEqual(len(h.named("post_dump_stop_armed")), 1)

    def test_partial_fill_retries_the_remainder_after_cooldown(self):
        h = Harness()
        intent = _dumped_bag(h.clock["now"] + 200.0)
        h.budget = [40.0, 0.0, 0.0, 0.0]
        h.tick(_cfg(), intent, 4)
        self.assertEqual(len(h.calls), 1)
        self.assertEqual(intent["post_dump_stop_filled"], 40.0)
        self.assertFalse(intent.get("post_dump_stop_done"))
        self.assertIsNotNone(intent["post_dump_stop_armed_at"])
        first = h.named("post_dump_stop_fill")
        self.assertEqual(len(first), 1)
        self.assertEqual((first[0]["sold"], first[0]["done"], first[0]["remaining"]), (40.0, False, 23.0))
        self.assertTrue(kept_loser_open(intent))
        h.tick(_cfg(), intent, 2)
        self.assertEqual(len(h.calls), 1)
        h.fill_px = 0.25
        h.budget = []
        h.tick(_cfg(), intent, 1)
        self.assertEqual(len(h.calls), 2)
        self.assertEqual(h.calls[1]["size"], 23.0)
        self.assertTrue(intent["post_dump_stop_done"])
        self.assertEqual(intent["post_dump_stop_filled"], 63.0)
        self.assertEqual(intent["post_dump_stop_fill_px"], round((40 * 0.29 + 23 * 0.25) / 63, 4))
        self.assertFalse(kept_loser_open(intent))
        h.tick(_cfg(), intent, 10)
        self.assertEqual(len(h.calls), 2)

    def test_zero_fill_logs_a_miss_and_keeps_the_arm(self):
        h = Harness()
        intent = _dumped_bag(h.clock["now"] + 200.0)
        h.budget = [0.0] * 20
        h.tick(_cfg(sell_dump_fak_retries=0), intent, 4)
        self.assertEqual(len(h.calls), 1)
        miss = h.named("post_dump_stop_miss")
        self.assertEqual(len(miss), 1)
        self.assertEqual(miss[0]["remaining"], 63.0)
        self.assertEqual(intent["post_dump_stop_filled"], 0.0)
        self.assertIsNotNone(intent["post_dump_stop_armed_at"])
        self.assertEqual(h.named("post_dump_stop_fill"), [])

    def test_stops_at_window_end(self):
        h = Harness()
        end = h.clock["now"] + 2.0
        intent = _dumped_bag(end)
        h.tick(_cfg(), intent, 10)
        self.assertEqual(h.calls, [])
        self.assertEqual(len(h.named("post_dump_stop_armed")), 1)
        self.assertEqual(h.named("post_dump_stop_fill"), [])

    def test_optional_ttm_gate(self):
        h = Harness()
        end = h.clock["now"] + 70.0
        intent = _dumped_bag(end)
        cfg = _cfg(post_dump_kept_stop_max_ttm_s=60.0)
        h.tick(cfg, intent, 10)
        self.assertEqual(h.calls, [])
        self.assertEqual(h.stop_events(), [])
        h.tick(cfg, intent, 4)
        self.assertEqual(len(h.calls), 1)
        self.assertLessEqual(h.named("post_dump_stop_armed")[0]["ttm"], 60.0)

    def test_hot_reload_turns_it_on_and_off(self):
        h = Harness()
        intent = _dumped_bag(h.clock["now"] + 200.0)
        h.tick(_cfg(), intent, 2)
        h.tick(_dump_cfg(post_dump_kept_stop=False), intent, 1)
        self.assertIsNone(intent["post_dump_stop_armed_at"])
        h.tick(_cfg(post_dump_kept_stop_hold_s=1.0, post_dump_kept_stop_px=0.25), intent, 3)
        self.assertEqual(h.calls, [])
        h.tick(_cfg(post_dump_kept_stop_hold_s=1.0), intent, 2)
        self.assertEqual(len(h.calls), 1)

    def test_dry_run_only_logs(self):
        h = Harness()
        intent = _dumped_bag(h.clock["now"] + 200.0)
        h.tick(_cfg(dry_run=True), intent, 6)
        self.assertEqual([c["dry_run"] for c in h.calls], [True])
        fill = h.named("post_dump_stop_fill")
        self.assertEqual(len(fill), 1)
        self.assertEqual((fill[0]["sold"], fill[0]["dry_run"], fill[0]["done"]), (0.0, True, True))
        self.assertTrue(intent["post_dump_stop_dry"])


class WhatsAppTests(unittest.TestCase):
    def _alerts(self):
        sent: list = []

        class Resp:
            status_code = 200
            text = "Message queued"

        notifier = WhatsAppNotifier(
            "+353830867820", "k", http_get=lambda url, *, params, timeout: sent.append(params["text"]) or Resp(),
            sleep=lambda _s: None,
        )
        return BagAlerts(notifier), sent

    def test_message_format(self):
        intent = _dumped_bag(2_000_000_000.0, post_dump_stop_filled=63.0, post_dump_stop_fill_px=0.35)
        text = kept_stop_message(intent, now=2_000_000_000.0 - 100.0, bids={"up": 0.64, "dn": 0.35})
        label = bag_start_label(2_000_000_000.0 - 900.0)
        self.assertEqual(text, f"Mintbot KEPT STOP: {label} bag | sold 63 DN @ 0.35 ($22.05) | 1m40s left | UP bid 0.64")
        self.assertIsNone(kept_stop_message(_dumped_bag(1.0), now=0.0))

    def test_rides_on_the_dump_knob(self):
        for on in (False, True):
            alerts, sent = self._alerts()
            h = Harness(alerts=alerts)
            intent = _dumped_bag(h.clock["now"] + 200.0)
            h.tick(_cfg(notify_dump_whatsapp=on), intent, 4)
            self.assertTrue(intent["post_dump_stop_done"])
            alerts.notifier.flush()
            if on:
                self.assertEqual(len(sent), 1)
                self.assertTrue(sent[0].startswith("Mintbot KEPT STOP: "))
                self.assertIn("sold 63 DN @ 0.29 ($18.27)", sent[0])
                self.assertIn("UP bid 0.7", sent[0])
            else:
                self.assertEqual(sent, [])

    def test_dry_run_sends_nothing(self):
        alerts, sent = self._alerts()
        h = Harness(alerts=alerts)
        intent = _dumped_bag(h.clock["now"] + 200.0)
        h.tick(_cfg(dry_run=True, notify_dump_whatsapp=True), intent, 6)
        alerts.notifier.flush()
        self.assertEqual(sent, [])


if __name__ == "__main__":
    unittest.main()
