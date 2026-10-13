"""Dump persist requires the other side's bid for the whole clock."""

from __future__ import annotations

import json
import unittest
from pathlib import Path

from buy.mint_gas import validate_mint_gas
from buy.mint_redeem import validate_redeem
from buy.mint_sell import DEFAULT_SELL_KNOBS, sell_dump_other_confirms
from buy.mint_sequence import validate_seq

from test_mint_cpu import _dump_cfg, _dump_harness, _fill_fak, _held_after_scrap
from test_mint_only_ops import _assign, _fn


ROOT = Path(__file__).resolve().parents[1]
MINT = ROOT / "mintbot.py"


def _level(px: float):
    text = f"{px:.2f}"
    return (px, 50.0, [{"price": text, "size": "80"}])


class SellDumpOtherConfirmsTests(unittest.TestCase):
    def test_off_and_on(self):
        self.assertTrue(sell_dump_other_confirms(0.10, None))
        self.assertTrue(sell_dump_other_confirms(0.10, ""))
        self.assertTrue(sell_dump_other_confirms(0.10, 0))
        self.assertTrue(sell_dump_other_confirms(0.10, 0.0))
        self.assertTrue(sell_dump_other_confirms(None, 0))
        self.assertFalse(sell_dump_other_confirms(None, 0.30))
        self.assertFalse(sell_dump_other_confirms(0.29, 0.30))
        self.assertTrue(sell_dump_other_confirms(0.30, 0.30))
        self.assertTrue(sell_dump_other_confirms(0.31, 0.30))
        self.assertFalse(sell_dump_other_confirms(0.30, 1))
        self.assertTrue(sell_dump_other_confirms(0.10, -0.01))
        self.assertFalse(sell_dump_other_confirms(0.30, "no"))

    def test_defaults_and_validation(self):
        defaults = _assign("DEFAULTS")
        example = json.loads((ROOT / "strategy_mint.example.json").read_text())
        self.assertEqual(defaults["sell_dump_other_min"], 0.30)
        self.assertEqual(example["sell_dump_other_min"], 0.30)
        self.assertEqual(DEFAULT_SELL_KNOBS["sell_dump_other_min"], 0.30)
        validate = _fn(
            "validate_strategy",
            {
                "validate_mint_gas": validate_mint_gas,
                "validate_seq": validate_seq,
                "validate_redeem": validate_redeem,
            },
        )
        validate(dict(defaults, sell_dump_other_min=None))
        validate(dict(defaults, sell_dump_other_min=0))
        validate(dict(defaults, sell_dump_other_min=0.30))
        for bad in (-0.01, 1, 1.5, "no"):
            with self.assertRaises(ValueError):
                validate(dict(defaults, sell_dump_other_min=bad))


class DumpOtherMinPersistTests(unittest.TestCase):
    def _books(self, ns, book):
        def fetch(_up, _dn, _min_size):
            book["fetches"] = book.get("fetches", 0) + 1
            return book["up"], book["dn"]

        ns["_fetch_books"] = fetch

    def _tick(self, ns, cfg, intent):
        state = {"intents": {"cid-dump": intent}}
        ns["remember_persisted_state"](state)
        ns["_manage_sells_locked"](cfg, state, object())
        return intent

    def test_fires_after_half_second_of_both(self):
        ns, events, fak_calls, clock = _dump_harness(_fill_fak)
        book = {"up": _level(0.40), "dn": _level(0.30)}
        self._books(ns, book)
        end = clock["now"] + 200.0
        intent = _held_after_scrap(end)
        cfg = _dump_cfg(sell_dump_persist_s=0.5, sell_dump_below=0.60)
        self._tick(ns, cfg, intent)
        armed = intent.get("sell_dump_armed_at")
        self.assertEqual(armed, clock["now"])
        self.assertEqual(fak_calls, [])
        persist = [row for row in events if row["event"] == "sell_dump_persist"]
        self.assertEqual(persist[-1]["why"], "armed")
        self.assertEqual(persist[-1]["other_leg"], "dn")
        self.assertAlmostEqual(persist[-1]["other_bid"], 0.30)
        self.assertAlmostEqual(persist[-1]["bid"], 0.40)

        clock["now"] = armed + 0.49
        self._tick(ns, cfg, intent)
        self.assertEqual(fak_calls, [])
        self.assertEqual(intent.get("sell_dump_armed_at"), armed)
        waiting = [row for row in events if row["event"] == "sell_dump_persist"]
        self.assertEqual(waiting[-1]["why"], "waiting")
        self.assertAlmostEqual(waiting[-1]["other_bid"], 0.30)

        clock["now"] = armed + 0.5
        self._tick(ns, cfg, intent)
        self.assertEqual(len(fak_calls), 1)
        self.assertEqual(fak_calls[0]["token_id"], "up-tok")
        self.assertAlmostEqual(fak_calls[0]["price"], 0.40)
        self.assertTrue(intent.get("sold_dump"))

    def test_other_dip_resets_and_a_new_persist_is_required(self):
        ns, events, fak_calls, clock = _dump_harness(_fill_fak)
        book = {"up": _level(0.40), "dn": _level(0.30)}
        self._books(ns, book)
        intent = _held_after_scrap(clock["now"] + 200.0)
        cfg = _dump_cfg(sell_dump_persist_s=0.5, sell_dump_below=0.60)
        self._tick(ns, cfg, intent)
        first = intent["sell_dump_armed_at"]

        clock["now"] = first + 0.4
        book["dn"] = _level(0.29)
        self._tick(ns, cfg, intent)
        self.assertIsNone(intent.get("sell_dump_armed_at"))
        self.assertEqual(fak_calls, [])
        low = [row for row in events if row.get("why") == "other_low"]
        self.assertEqual(len(low), 1)
        self.assertAlmostEqual(low[0]["other_bid"], 0.29)
        self.assertAlmostEqual(low[0]["bid"], 0.40)
        self.assertEqual(low[0]["leg"], "up")

        book["dn"] = _level(0.30)
        self._tick(ns, cfg, intent)
        restarted = intent.get("sell_dump_armed_at")
        self.assertEqual(restarted, clock["now"])
        self.assertNotEqual(restarted, first)
        self.assertEqual(fak_calls, [])

        clock["now"] = first + 0.5
        self._tick(ns, cfg, intent)
        self.assertEqual(fak_calls, [])
        self.assertEqual(intent.get("sell_dump_armed_at"), restarted)

        clock["now"] = restarted + 0.5
        self._tick(ns, cfg, intent)
        self.assertEqual(len(fak_calls), 1)
        self.assertTrue(intent.get("sold_dump"))

    def test_held_recovery_still_resets_without_other_low(self):
        ns, events, fak_calls, clock = _dump_harness(_fill_fak)
        book = {"up": _level(0.40), "dn": _level(0.40)}
        self._books(ns, book)
        intent = _held_after_scrap(clock["now"] + 200.0)
        cfg = _dump_cfg(sell_dump_persist_s=0.5, sell_dump_below=0.60)
        self._tick(ns, cfg, intent)
        armed = intent["sell_dump_armed_at"]
        clock["now"] = armed + 0.4
        book["up"] = _level(0.80)
        self._tick(ns, cfg, intent)
        self.assertIsNone(intent.get("sell_dump_armed_at"))
        self.assertEqual(fak_calls, [])
        self.assertFalse(any(row.get("why") == "other_low" for row in events))

        book["up"] = _level(0.40)
        self._tick(ns, cfg, intent)
        self.assertEqual(intent.get("sell_dump_armed_at"), clock["now"])
        self.assertEqual(fak_calls, [])

    def test_missing_other_bid_resets_and_zero_disables(self):
        ns, events, fak_calls, clock = _dump_harness(_fill_fak)
        book = {"up": _level(0.40), "dn": (None, 0.0, [])}
        self._books(ns, book)
        intent = _held_after_scrap(
            clock["now"] + 200.0,
            sell_dump_armed_at=clock["now"] - 5.0,
        )
        cfg = _dump_cfg(sell_dump_persist_s=0.5, sell_dump_below=0.60)
        self._tick(ns, cfg, intent)
        self.assertIsNone(intent.get("sell_dump_armed_at"))
        self.assertEqual(fak_calls, [])
        self.assertEqual(
            [row["why"] for row in events if row["event"] == "sell_dump_persist"],
            ["other_low"],
        )
        self.assertIsNone(
            [row for row in events if row["event"] == "sell_dump_persist"][-1]["other_bid"]
        )

        book["dn"] = _level(0.10)
        cfg_off = _dump_cfg(
            sell_dump_persist_s=0,
            sell_dump_below=0.60,
            sell_dump_other_min=0,
        )
        fresh = _held_after_scrap(clock["now"] + 200.0)
        self._tick(ns, cfg_off, fresh)
        self.assertTrue(fresh.get("sold_dump"))
        self.assertEqual(len(fak_calls), 1)

    def test_retries_and_kept_sell_are_not_gated(self):
        ns, _events, fak_calls, clock = _dump_harness(_fill_fak)
        book = {"up": _level(0.40), "dn": _level(0.30), "fetches": 0}

        def fetch(_up, _dn, _min_size):
            book["fetches"] += 1
            # A later both-books read would see the other side under the min.
            row = book["up"], book["dn"]
            book["dn"] = _level(0.05)
            return row

        def fak(token, size, price, dry, capture=None):
            fak_calls.append(
                {
                    "token_id": token,
                    "size": float(size),
                    "price": float(price),
                    "dry_run": dry,
                }
            )
            held_posts = [row for row in fak_calls if row["token_id"] == "up-tok"]
            if token == "up-tok" and len(held_posts) == 1:
                return 0.0, "no orders found to match with FAK order"
            return float(size), "matched"

        ns["_fetch_books"] = fetch
        ns["_fak_sell"] = fak
        ns["_fetch_book"] = lambda *_a, **_k: _level(0.22)
        intent = _held_after_scrap(
            clock["now"] + 200.0,
            sell_scrap_keep=25.0,
        )
        cfg = _dump_cfg(
            sell_dump_persist_s=0,
            sell_dump_below=0.60,
            sell_dump_also_kept=True,
            sell_dump_fak_retries=2,
        )
        self._tick(ns, cfg, intent)
        self.assertEqual(book["fetches"], 1)
        self.assertTrue(intent.get("sold_dump"))
        self.assertEqual(intent.get("sell_dump_kept_outcome"), "filled")
        held = [row for row in fak_calls if row["token_id"] == "up-tok"]
        kept = [row for row in fak_calls if row["token_id"] == "dn-tok"]
        self.assertEqual([round(row["price"], 2) for row in held], [0.40, 0.22])
        self.assertTrue(kept)
        self.assertAlmostEqual(kept[0]["price"], 0.30)
        src = MINT.read_text()
        refire = src[
            src.find("def _run_dump_fak_with_refire") : src.find("\ndef _sell_kept_after_dump")
        ]
        kept_fn = src[
            src.find("def _sell_kept_after_dump") : src.find("\ndef _apply_sell_fire_cancel")
        ]
        self.assertNotIn("sell_dump_other", refire)
        self.assertNotIn("sell_dump_other", kept_fn)
