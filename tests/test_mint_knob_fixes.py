"""Startup banner, kept-loser mint slots, explicit-zero seconds, poll_s floor."""

from __future__ import annotations

import json
import unittest
from pathlib import Path

from buy.mint_redeem import validate_redeem
from buy.mint_sequence import validate_seq
from buy.mint_gas import validate_mint_gas
from buy.mint_sell import cfg_seconds, kept_loser_open, sell_plan_banner
from test_mint_cpu import _dump_cfg, _dump_harness, _fill_fak, _held_after_scrap
from test_mint_only_ops import _ACTIVE, _END_A, _NOW, _START_A, _START_B, _START_C, _assign, _fn


ROOT = Path(__file__).resolve().parents[1]
MINT = ROOT / "mintbot.py"
MINT_EXAMPLE = ROOT / "strategy_mint.example.json"

LIVE_30_SEP = {
    "sell_enabled": True,
    "sell_threshold": 0.03,
    "sell_fak_px": 0.03,
    "sell_floor": 0.01,
    "sell_scrap_fraction": 0.5,
    "sell_scrap_max_ttm_s": 360,
    "sell_dump_enabled": True,
    "sell_dump_below": 0.4,
    "sell_dump_max_ttm_s": 240,
    "sell_winner_min": 0.9995,
    "sell_dump_persist_s": 2,
    "sell_cooldown_s": 3.0,
    "sell_scrap_rest_min_ahead_s": 180.0,
}


class StartupBannerTests(unittest.TestCase):
    def test_live_config_describes_the_floor_sweep(self):
        text = sell_plan_banner(LIVE_30_SEP)
        self.assertEqual(
            text,
            "loser <=3c -> one FAK @ floor 1c · scrap 50% keep rest · "
            "scrap ttm<=360s · dump held <40c ttm<=240s · keep winner (cash >=0.9995)",
        )
        self.assertNotIn("ladder", text)

    def test_ladder_mode_lists_the_rungs(self):
        cfg = dict(LIVE_30_SEP, sell_scrap_sweep_enabled=False)
        self.assertTrue(sell_plan_banner(cfg).startswith("loser <=3c -> ladder 3c->2c->1c"))

    def test_sell_off_and_example_defaults(self):
        self.assertEqual(
            sell_plan_banner({"sell_enabled": False}),
            "sell off (sell_enabled=false) · keep both legs",
        )
        example = json.loads(MINT_EXAMPLE.read_text())
        example["sell_enabled"] = True
        self.assertEqual(
            sell_plan_banner(example),
            "loser <=2c -> one FAK @ floor 2c · scrap ttm<=600s · "
            "dump held <80c ttm<=240s · keep winner (cash >=0.999)",
        )

    def test_main_prints_and_logs_the_loaded_plan(self):
        src = MINT.read_text(encoding="utf-8")
        main = src[src.find("def main") :]
        self.assertNotIn("3c->2c", src)
        self.assertIn("sell_plan_banner(cfg)", main)
        self.assertIn("sell_plan=sell_plan_banner(cfg)", main)


def _kept(**extra) -> dict:
    row = {
        "status": "confirmed",
        "start_ts": _START_A,
        "end_ts": _END_A,
        "sold_loser": True,
        "sold_leg": "up",
        "sell_scrap_target": 50.0,
        "sell_scrap_keep": 50.0,
    }
    row.update(extra)
    return row


class KeptLoserSlotTests(unittest.TestCase):
    def _fns(self):
        extras = {"ACTIVE_STATUSES": _ACTIVE, "kept_loser_open": kept_loser_open}
        return (
            _fn("open_intent_count", extras),
            _fn("mint_slots_full", extras),
            _fn("mint_discovery_capped", extras),
        )

    def test_default_off_frees_the_slot_like_before(self):
        count, slots_full, capped = self._fns()
        state = {"intents": {"a": _kept()}}
        for cfg in ({"max_open_sets": 1}, {"max_open_sets": 1, "count_kept_loser_as_open": False}):
            self.assertEqual(count(state, now=_NOW, cfg=cfg), 0)
            self.assertFalse(slots_full(state, cfg, _NOW, _START_C))
            self.assertFalse(capped(state, cfg, _NOW))
        self.assertEqual(count(state, now=_NOW), 0)

    def test_opt_in_counts_kept_shares_toward_max_open_sets(self):
        count, slots_full, capped = self._fns()
        cfg = {"max_open_sets": 1, "count_kept_loser_as_open": True}
        state = {"intents": {"a": _kept()}}
        self.assertEqual(count(state, now=_NOW, cfg=cfg), 1)
        self.assertTrue(slots_full(state, cfg, _NOW, _START_C))
        # The adjacent next window is still allowed at the cap, as for any full bag.
        self.assertFalse(slots_full(state, cfg, _NOW, _START_B))
        self.assertFalse(capped(state, cfg, _NOW))
        state["intents"]["b"] = _kept(start_ts=_START_B, end_ts=_START_B + 900.0)
        self.assertTrue(capped(state, cfg, _NOW))

    def test_opt_in_releases_after_grace_or_when_nothing_was_kept(self):
        count, slots_full, _capped = self._fns()
        cfg = {"max_open_sets": 1, "count_kept_loser_as_open": True}
        full_scrap = {"intents": {"a": _kept(sell_scrap_keep=0.0)}}
        self.assertEqual(count(full_scrap, now=_NOW, cfg=cfg), 0)
        no_plan = {"intents": {"a": _kept(sell_scrap_keep=None, sell_scrap_target=None)}}
        self.assertEqual(count(no_plan, now=_NOW, cfg=cfg), 0)
        expired = {"intents": {"a": _kept()}}
        self.assertEqual(count(expired, now=_END_A + 121.0, cfg=cfg), 0)
        self.assertFalse(slots_full(expired, cfg, _END_A + 121.0, _START_C))

    def test_kept_leg_cashed_as_winner_releases_but_a_dump_does_not(self):
        self.assertTrue(kept_loser_open(_kept()))
        self.assertFalse(
            kept_loser_open(_kept(sold_winner=True, sell_winner_leg="up"))
        )
        self.assertTrue(
            kept_loser_open(_kept(sold_winner=True, sell_winner_leg="dn"))
        )
        self.assertTrue(
            kept_loser_open(_kept(sold_winner=True, sold_dump=True, sell_dump_leg="dn"))
        )
        self.assertFalse(kept_loser_open(None))

    def test_winner_fill_records_its_leg(self):
        src = MINT.read_text(encoding="utf-8")
        self.assertEqual(src.count('intent["sell_winner_leg"] = winner'), 2)

    def test_defaults_and_example_keep_it_off(self):
        self.assertIs(_assign("DEFAULTS")["count_kept_loser_as_open"], False)
        self.assertIs(json.loads(MINT_EXAMPLE.read_text())["count_kept_loser_as_open"], False)


class ExplicitZeroSecondsTests(unittest.TestCase):
    KEYS = (
        ("sell_dump_persist_s", 2.0),
        ("sell_cooldown_s", 3.0),
        ("sell_scrap_rest_min_ahead_s", 180.0),
    )

    def test_zero_is_respected_and_bad_values_fall_back(self):
        for key, default in self.KEYS:
            self.assertEqual(cfg_seconds({key: 0}, key, default), 0.0, key)
            self.assertEqual(cfg_seconds({key: 0.0}, key, default), 0.0, key)
            for bad in (None, "", "x", -1, float("nan"), True):
                self.assertEqual(cfg_seconds({key: bad}, key, default), default, (key, bad))
            self.assertEqual(cfg_seconds({}, key, default), default, key)

    def test_live_values_are_unchanged(self):
        for key, default in self.KEYS:
            old = float(LIVE_30_SEP.get(key) or default)
            self.assertEqual(cfg_seconds(LIVE_30_SEP, key, default), old, key)

    def test_sell_loop_reads_the_keys_through_cfg_seconds(self):
        src = MINT.read_text(encoding="utf-8")
        for key, _default in self.KEYS:
            self.assertIn(f'cfg_seconds(cfg, "{key}"', src)
            self.assertNotIn(f'cfg.get("{key}") or', src)
            self.assertNotIn(f'cfg.get("{key}", 180.0) or', src)

    def test_dump_persist_zero_fires_on_the_first_tick(self):
        ns, _events, fak_calls, clock = _dump_harness(_fill_fak)
        intent = _held_after_scrap(clock["now"] + 200.0)
        state = {"intents": {"cid-zero": intent}}
        ns["remember_persisted_state"](state)
        ns["_manage_sells_locked"](_dump_cfg(sell_dump_persist_s=0), state, object())
        self.assertEqual(len(fak_calls), 1)
        self.assertTrue(intent.get("sold_dump"))

        ns2, _e2, fak2, clock2 = _dump_harness(_fill_fak)
        intent2 = _held_after_scrap(clock2["now"] + 200.0)
        state2 = {"intents": {"cid-two": intent2}}
        ns2["remember_persisted_state"](state2)
        ns2["_manage_sells_locked"](_dump_cfg(sell_dump_persist_s=2), state2, object())
        self.assertEqual(fak2, [])

    def test_validation_rejects_negatives_and_accepts_zero(self):
        validate = _fn("validate_strategy", {"validate_mint_gas": validate_mint_gas, "validate_seq": validate_seq, "validate_redeem": validate_redeem})
        base = _assign("DEFAULTS")
        for key, _default in self.KEYS:
            ok = dict(base, **{key: 0})
            validate(ok)
            bad = dict(base, **{key: -1})
            with self.assertRaises(ValueError) as caught:
                validate(bad)
            self.assertIn(key, str(caught.exception))


class PollFloorTests(unittest.TestCase):
    def test_poll_s_floor_is_one_second(self):
        validate = _fn("validate_strategy", {"validate_mint_gas": validate_mint_gas, "validate_seq": validate_seq, "validate_redeem": validate_redeem})
        base = _assign("DEFAULTS")
        validate(dict(base, poll_s=1.0, sell_armed_poll_s=1.0))
        with self.assertRaises(ValueError) as caught:
            validate(dict(base, poll_s=0.5))
        self.assertIn("poll_s must be >= 1", str(caught.exception))
        self.assertEqual(base["poll_s"], 5.0)
        self.assertEqual(json.loads(MINT_EXAMPLE.read_text())["poll_s"], 5.0)


if __name__ == "__main__":
    unittest.main()
