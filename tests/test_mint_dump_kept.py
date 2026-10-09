"""``sell_dump_also_kept``: the held dump also sells the kept scrap half."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from buy import mint_sell
from buy.mint_gas import validate_mint_gas
from buy.mint_redeem import validate_redeem
from buy.mint_sell import kept_loser_open
from buy.mint_sequence import validate_seq
from test_mint_cpu import _dump_cfg, _dump_harness, _fill_fak, _held_after_scrap
from test_mint_only_ops import MINT, MINT_EXAMPLE, _assign, _fn


def _tick(ns, cfg, intent, cid="cid-kept"):
    state = {"intents": {cid: intent}}
    ns["remember_persisted_state"](state)
    ns["_manage_sells_locked"](cfg, state, object())
    return intent


def _ready_bag(clock, keep=25.0, **extra):
    """Loser ``dn`` scrapped with ``keep`` shares kept; held ``up`` armed to dump."""
    end = clock["now"] + 200.0
    return _held_after_scrap(
        end,
        sell_dump_armed_at=clock["now"] - 2.0,
        sell_scrap_keep=keep,
        sell_scrap_target=50.0 - keep,
        sell_filled=50.0 - keep,
        **extra,
    )


def _priced(fak_calls, fill_for):
    """FAK stub: ``fill_for(token, size, price)`` -> shares; reply priced at the limit."""

    def fak(token_id, size, price, dry_run, capture=None):
        fak_calls.append(
            {"token_id": token_id, "size": float(size), "price": float(price), "dry_run": dry_run}
        )
        sold = float(fill_for(token_id, float(size), float(price)))
        if capture is not None and sold > 0:
            capture.append(
                {
                    "status": "matched",
                    "makingAmount": str(sold),
                    "takingAmount": str(round(sold * float(price), 6)),
                }
            )
        return sold, "matched" if sold > 0 else "no orders found to match"

    return fak


def _events(events, name):
    return [row for row in events if row["event"] == name]


class DumpAlsoKeptFiresTests(unittest.TestCase):
    def test_fires_right_after_the_dump_and_sells_all_kept_shares(self):
        ns, events, fak_calls, clock = _dump_harness(_fill_fak)
        ns["_fak_sell"] = _priced(fak_calls, lambda _t, size, _p: size)
        intent = _ready_bag(clock, keep=25.0)
        _tick(ns, _dump_cfg(sell_dump_also_kept=True), intent)

        self.assertEqual(
            [(c["token_id"], c["size"], c["price"]) for c in fak_calls],
            [("up-tok", 50.0, 0.51), ("dn-tok", 25.0, 0.49)],
        )
        self.assertTrue(intent["sold_dump"])
        self.assertEqual(intent["sell_dump_leg"], "up")
        self.assertTrue(intent["sell_dump_kept_done"])
        self.assertTrue(intent["sell_dump_kept_sold"])
        self.assertEqual(intent["sell_dump_kept_leg"], "dn")
        self.assertEqual(intent["sell_dump_kept_outcome"], "filled")
        self.assertAlmostEqual(intent["sell_dump_kept_filled"], 25.0)
        self.assertEqual(intent["sell_dump_kept_fill_px"], 0.49)
        self.assertFalse(kept_loser_open(intent))

        order = [row["event"] for row in events if row["event"] in {"sell_dump_done", "sell_dump_kept"}]
        self.assertEqual(order, ["sell_dump_done", "sell_dump_kept"])
        kept = _events(events, "sell_dump_kept")[0]
        self.assertEqual(kept["condition_id"], "cid-kept")
        self.assertEqual(kept["slug"], "btc-updown-15m-test")
        self.assertEqual(kept["leg"], "dn")
        self.assertEqual(kept["planned"], 25.0)
        self.assertEqual(kept["sold"], 25.0)
        self.assertEqual(kept["avg_px"], 0.49)
        self.assertEqual(kept["outcome"], "filled")
        self.assertEqual(kept["remaining"], 0.0)

    def test_runs_once_per_bag(self):
        ns, events, fak_calls, clock = _dump_harness(_fill_fak)
        intent = _ready_bag(clock)
        cfg = _dump_cfg(sell_dump_also_kept=True)
        _tick(ns, cfg, intent)
        calls = len(fak_calls)
        for _ in range(3):
            clock["now"] += 5.0
            _tick(ns, cfg, intent)
        self.assertEqual(len(fak_calls), calls)
        self.assertEqual(len(_events(events, "sell_dump_kept")), 1)

    def test_partial_dump_waits_and_kept_sells_only_when_the_dump_completes(self):
        ns, events, fak_calls, clock = _dump_harness(_fill_fak)
        # First shot plus two refires still leave the held leg short.
        held_fills = iter([10.0, 10.0, 10.0, float("inf")])

        def fill_for(token, size, _price):
            if token == "up-tok":
                return min(size, next(held_fills))
            return size

        ns["_fak_sell"] = _priced(fak_calls, fill_for)
        intent = _ready_bag(clock)
        cfg = _dump_cfg(sell_dump_also_kept=True)
        _tick(ns, cfg, intent)
        held = [(c["size"], c["price"]) for c in fak_calls if c["token_id"] == "up-tok"]
        self.assertEqual(held, [(50.0, 0.51), (40.0, 0.48), (30.0, 0.48)])
        self.assertFalse(intent.get("sold_dump"))
        self.assertNotIn("dn-tok", [c["token_id"] for c in fak_calls])
        self.assertEqual(_events(events, "sell_dump_kept"), [])

        clock["now"] += 5.0
        _tick(ns, cfg, intent)
        self.assertTrue(intent.get("sold_dump"))
        self.assertEqual([c["token_id"] for c in fak_calls][-1], "dn-tok")
        self.assertEqual(intent["sell_dump_kept_outcome"], "filled")

    def test_dry_run_marks_the_kept_leg_without_live_posts(self):
        ns, events, fak_calls, clock = _dump_harness(_fill_fak)
        intent = _ready_bag(clock)
        _tick(ns, _dump_cfg(sell_dump_also_kept=True, dry_run=True), intent)
        self.assertEqual([c["token_id"] for c in fak_calls], ["up-tok", "dn-tok"])
        self.assertTrue(all(c["dry_run"] for c in fak_calls))
        self.assertEqual(intent["sell_dump_kept_outcome"], "dry_run")
        self.assertEqual(_events(events, "sell_dump_kept")[0]["outcome"], "dry_run")


class DumpAlsoKeptOffTests(unittest.TestCase):
    def _assert_no_kept_sale(self, cfg):
        ns, events, fak_calls, clock = _dump_harness(_fill_fak)
        intent = _ready_bag(clock)
        _tick(ns, cfg, intent)
        self.assertTrue(intent["sold_dump"])
        self.assertEqual([c["token_id"] for c in fak_calls], ["up-tok"])
        self.assertEqual(_events(events, "sell_dump_kept"), [])
        self.assertNotIn("sell_dump_kept_done", intent)
        self.assertTrue(kept_loser_open(intent))

    def test_absent_key_keeps_the_old_dump(self):
        cfg = _dump_cfg()
        self.assertNotIn("sell_dump_also_kept", cfg)
        self._assert_no_kept_sale(cfg)

    def test_false_key_keeps_the_old_dump(self):
        self._assert_no_kept_sale(_dump_cfg(sell_dump_also_kept=False))


class DumpAlsoKeptPartialTests(unittest.TestCase):
    def test_partial_fill_remainder_is_swept_at_one_cent(self):
        ns, events, fak_calls, clock = _dump_harness(_fill_fak)

        def fill_for(token, size, price):
            if token == "dn-tok" and price > 0.01:
                return min(size, 5.0)
            return size

        ns["_fak_sell"] = _priced(fak_calls, fill_for)
        intent = _ready_bag(clock, keep=25.0)
        _tick(ns, _dump_cfg(sell_dump_also_kept=True), intent)
        kept_calls = [(c["size"], c["price"]) for c in fak_calls if c["token_id"] == "dn-tok"]
        # Fresh bids for the retry budget, then the 1¢ sweep for what is left.
        self.assertEqual(
            kept_calls,
            [(25.0, 0.49), (20.0, 0.48), (15.0, 0.48), (10.0, 0.01)],
        )
        self.assertEqual(intent["sell_dump_kept_outcome"], "filled")
        self.assertAlmostEqual(intent["sell_dump_kept_filled"], 25.0)
        self.assertAlmostEqual(intent["sell_dump_kept_fill_px"], 0.294)
        self.assertTrue(intent["sell_dump_kept_sold"])
        kept = _events(events, "sell_dump_kept")[0]
        self.assertEqual(kept["swept"], 10.0)
        self.assertEqual(kept["fills"], 4)
        self.assertAlmostEqual(kept["avg_px"], 0.294)

    def test_zero_fill_miss_refires_the_ladder_down_to_the_floor(self):
        ns, events, fak_calls, clock = _dump_harness(_fill_fak)

        def fill_for(token, size, price):
            if token == "dn-tok" and price > 0.01:
                return 0.0
            return size

        ns["_fak_sell"] = _priced(fak_calls, fill_for)
        intent = _ready_bag(clock, keep=25.0)
        _tick(ns, _dump_cfg(sell_dump_also_kept=True), intent)
        kept_px = [c["price"] for c in fak_calls if c["token_id"] == "dn-tok"]
        self.assertEqual(kept_px[0], 0.49)
        self.assertEqual(kept_px[-1], 0.01)
        self.assertGreater(len(kept_px), 2)
        self.assertTrue(all(px >= 0.01 for px in kept_px))
        self.assertEqual(intent["sell_dump_kept_outcome"], "filled")
        self.assertEqual(intent["sell_dump_kept_fill_px"], 0.01)

    def test_unsold_remainder_is_logged_partial_and_not_retried(self):
        ns, events, fak_calls, clock = _dump_harness(_fill_fak)
        budget = {"left": 20.0}

        def fill_for(token, size, _price):
            if token != "dn-tok":
                return size
            got = min(size, budget["left"])
            budget["left"] -= got
            return got

        ns["_fak_sell"] = _priced(fak_calls, fill_for)
        intent = _ready_bag(clock, keep=25.0)
        cfg = _dump_cfg(sell_dump_also_kept=True)
        _tick(ns, cfg, intent)
        self.assertEqual(intent["sell_dump_kept_outcome"], "partial")
        self.assertAlmostEqual(intent["sell_dump_kept_filled"], 20.0)
        self.assertNotIn("sell_dump_kept_sold", intent)
        self.assertTrue(kept_loser_open(intent))
        kept = _events(events, "sell_dump_kept")[0]
        self.assertEqual(kept["remaining"], 5.0)
        calls = len(fak_calls)
        clock["now"] += 5.0
        _tick(ns, cfg, intent)
        self.assertEqual(len(fak_calls), calls)


class DumpAlsoKeptNothingKeptTests(unittest.TestCase):
    def test_zero_keep_is_a_logged_no_op(self):
        ns, events, fak_calls, clock = _dump_harness(_fill_fak)
        intent = _ready_bag(clock, keep=0.0)
        _tick(ns, _dump_cfg(sell_dump_also_kept=True), intent)
        self.assertTrue(intent["sold_dump"])
        self.assertEqual([c["token_id"] for c in fak_calls], ["up-tok"])
        self.assertTrue(intent["sell_dump_kept_done"])
        self.assertEqual(intent["sell_dump_kept_outcome"], "nothing_kept")
        kept = _events(events, "sell_dump_kept")
        self.assertEqual(len(kept), 1)
        self.assertEqual(kept[0]["outcome"], "nothing_kept")
        self.assertEqual(kept[0]["planned"], 0.0)

    def test_missing_keep_is_a_no_op(self):
        ns, events, fak_calls, clock = _dump_harness(_fill_fak)
        intent = _held_after_scrap(clock["now"] + 200.0, sell_dump_armed_at=clock["now"] - 2.0)
        _tick(ns, _dump_cfg(sell_dump_also_kept=True), intent)
        self.assertEqual([c["token_id"] for c in fak_calls], ["up-tok"])
        self.assertEqual(intent["sell_dump_kept_outcome"], "nothing_kept")


class _Chain:
    def __init__(self, balances):
        self.balances = balances

    def position_balance(self, _ctf, _owner, token_id):
        return self.balances.get(token_id)


class DumpAlsoKeptBalanceTests(unittest.TestCase):
    def _run(self, balances):
        ns, events, fak_calls, clock = _dump_harness(_fill_fak)
        ns["os"] = SimpleNamespace(getenv=lambda key, default=None: "0xfunder" if key == "FUNDER_ADDRESS" else default)
        ns["to_checksum_address"] = lambda addr: addr
        intent = _ready_bag(clock, keep=25.0)
        state = {"intents": {"cid-kept": intent}}
        ns["remember_persisted_state"](state)
        cfg = _dump_cfg(sell_dump_also_kept=True, ctf_address="0xctf")
        ns["_manage_sells_locked"](cfg, state, _Chain(balances))
        return intent, events, fak_calls

    def test_on_chain_balance_below_keep_caps_the_size(self):
        intent, _events_, fak_calls = self._run({"up-tok": 50.0, "dn-tok": 12.0})
        self.assertEqual([(c["token_id"], c["size"]) for c in fak_calls], [("up-tok", 50.0), ("dn-tok", 12.0)])
        self.assertEqual(intent["sell_dump_kept_planned"], 12.0)
        self.assertEqual(intent["sell_dump_kept_outcome"], "filled")

    def test_zero_position_is_a_no_op(self):
        intent, events, fak_calls = self._run({"up-tok": 50.0, "dn-tok": 0.0})
        self.assertEqual([c["token_id"] for c in fak_calls], ["up-tok"])
        self.assertEqual(intent["sell_dump_kept_outcome"], "nothing_kept")
        self.assertEqual(_events(events, "sell_dump_kept")[0]["planned"], 0.0)

    def test_keep_caps_a_larger_balance(self):
        intent, _events_, fak_calls = self._run({"up-tok": 50.0, "dn-tok": 40.0})
        self.assertEqual(fak_calls[-1]["size"], 25.0)
        self.assertEqual(intent["sell_dump_kept_planned"], 25.0)


class DumpAlsoKeptHotReloadTests(unittest.TestCase):
    def test_flag_is_read_from_cfg_on_the_dump_tick(self):
        ns, events, fak_calls, clock = _dump_harness(_fill_fak)
        end = clock["now"] + 200.0
        intent = _held_after_scrap(end, sell_scrap_keep=25.0)
        _tick(ns, _dump_cfg(sell_dump_also_kept=False), intent)
        self.assertEqual(fak_calls, [])
        clock["now"] += 2.0
        _tick(ns, _dump_cfg(sell_dump_also_kept=True), intent)
        self.assertEqual([c["token_id"] for c in fak_calls], ["up-tok", "dn-tok"])
        self.assertEqual(intent["sell_dump_kept_outcome"], "filled")

    def test_enabling_after_the_dump_does_not_sell_kept_later(self):
        ns, events, fak_calls, clock = _dump_harness(_fill_fak)
        intent = _ready_bag(clock)
        _tick(ns, _dump_cfg(), intent)
        self.assertEqual([c["token_id"] for c in fak_calls], ["up-tok"])
        clock["now"] += 5.0
        _tick(ns, _dump_cfg(sell_dump_also_kept=True), intent)
        self.assertEqual([c["token_id"] for c in fak_calls], ["up-tok"])
        self.assertEqual(_events(events, "sell_dump_kept"), [])

    def test_reload_cfg_picks_up_the_key_from_the_strategy_file(self):
        defaults = _assign("DEFAULTS")
        validate = _fn(
            "validate_strategy",
            {"validate_mint_gas": validate_mint_gas, "validate_seq": validate_seq, "validate_redeem": validate_redeem},
        )
        raw = json.loads(MINT_EXAMPLE.read_text())
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "strategy_mint.json"
            load = _fn(
                "load_strategy",
                {"json": json, "DEFAULTS": defaults, "validate_strategy": validate, "STRATEGY_FILE": path},
            )
            reload_cfg = _fn("_reload_cfg", {"load_strategy": load, "log_event": lambda *_a, **_k: None})
            box: dict = {}
            path.write_text(json.dumps(raw))
            self.assertIs(reload_cfg(box)["sell_dump_also_kept"], False)
            raw["sell_dump_also_kept"] = True
            path.write_text(json.dumps(raw))
            self.assertIs(reload_cfg(box)["sell_dump_also_kept"], True)
            self.assertIs(box["cfg"]["sell_dump_also_kept"], True)
            del raw["sell_dump_also_kept"]
            path.write_text(json.dumps(raw))
            self.assertIs(reload_cfg(box)["sell_dump_also_kept"], False)


class DumpAlsoKeptDefaultsTests(unittest.TestCase):
    def test_defaults_and_example_are_off(self):
        self.assertIs(_assign("DEFAULTS")["sell_dump_also_kept"], False)
        self.assertIs(mint_sell.DEFAULT_SELL_KNOBS["sell_dump_also_kept"], False)
        self.assertIs(json.loads(MINT_EXAMPLE.read_text())["sell_dump_also_kept"], False)

    def test_mintbot_reads_the_flag_from_cfg(self):
        self.assertIn('cfg.get("sell_dump_also_kept", False)', MINT.read_text())


if __name__ == "__main__":
    unittest.main()
