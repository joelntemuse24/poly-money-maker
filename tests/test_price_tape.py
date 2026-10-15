"""Price tape: last 360s of a bag, from books the sell loop already has."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from buy import price_tape
from buy.log_archive import flush_archive_jobs
from test_mint_cpu import _open_scrap_bag, _scrap_cfg, _scrap_harness
from test_mint_only_ops import MINT, MINT_EXAMPLE, _assign


def _read(path: Path) -> list:
    price_tape.flush()
    if not path.exists():
        return []
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            out.append(json.loads(line))
    return out


class TapeDueTests(unittest.TestCase):
    def test_window_and_switch(self):
        self.assertTrue(price_tape.tape_due(360, 360, enabled_flag=True))
        self.assertTrue(price_tape.tape_due(1, 360, enabled_flag=True))
        self.assertFalse(price_tape.tape_due(360.1, 360, enabled_flag=True))
        self.assertFalse(price_tape.tape_due(0, 360, enabled_flag=True))
        self.assertFalse(price_tape.tape_due(None, 360, enabled_flag=True))
        self.assertFalse(price_tape.tape_due(10, 360, enabled_flag=False))
        self.assertFalse(price_tape.tape_due(10, 0, enabled_flag=True))
        self.assertTrue(price_tape.enabled({}))
        self.assertFalse(price_tape.enabled({"price_tape_enabled": False}))
        self.assertEqual(price_tape.window_s({}), 360.0)
        self.assertEqual(price_tape.window_s({"price_tape_window_s": 0}), 0.0)

    def test_defaults_match_the_example(self):
        defaults = _assign("DEFAULTS")
        example = json.loads(MINT_EXAMPLE.read_text())
        self.assertIs(defaults["price_tape_enabled"], True)
        self.assertIs(example["price_tape_enabled"], True)
        self.assertEqual(defaults["price_tape_window_s"], 360.0)
        self.assertEqual(example["price_tape_window_s"], 360.0)
        src = MINT.read_text()
        fn = src[src.find("def _price_tape_tick") : src.find("\ndef _shadow_early_tick")]
        self.assertNotIn("time.sleep", fn)
        self.assertNotIn("_fetch_book", fn)
        self.assertNotIn("post_order", fn)
        self.assertEqual(src.count("_price_tape_tick("), 2)


class TapeWriteTests(unittest.TestCase):
    def test_line_has_both_legs_and_the_cached_oracle(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "price_tape.jsonl"
            price_tape.set_log_path(path)
            price_tape.note(
                ts=1_700_000_000.0,
                slug="btc-updown-15m-1",
                condition_id="cid",
                ttm_s=120.5,
                up_bid=0.31,
                up_ask=0.33,
                dn_bid=0.67,
                dn_ask=0.69,
                up_bid_sz=40,
                up_ask_sz=12,
                dn_bid_sz=80,
                dn_ask_sz=9,
                oracle={"oracle_px": 101000.12, "oracle_start": 100000.0, "oracle_dist_usd": 1000.12},
            )
            rows = _read(path)
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["slug"], "btc-updown-15m-1")
        self.assertEqual(row["up_bid"], 0.31)
        self.assertEqual(row["up_ask"], 0.33)
        self.assertEqual(row["dn_bid"], 0.67)
        self.assertEqual(row["dn_ask"], 0.69)
        self.assertEqual(row["up_bid_sz"], 40.0)
        self.assertEqual(row["dn_bid_sz"], 80.0)
        self.assertEqual(row["oracle_px"], 101000.12)
        self.assertEqual(row["oracle_start"], 100000.0)
        self.assertEqual(row["oracle_dist_usd"], 1000.12)
        self.assertAlmostEqual(row["ttm_s"], 120.5)

    def test_outside_the_window_and_disabled_write_nothing(self):
        self.assertFalse(
            price_tape.tape_due(400, price_tape.window_s({}), enabled_flag=price_tape.enabled({}))
        )
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "quiet.jsonl"
            price_tape.set_log_path(path)
            price_tape.flush()
            self.assertFalse(path.exists())

    def test_writer_failure_is_swallowed(self):
        real = price_tape.append_jsonl

        def boom(*_args, **_kwargs):
            raise OSError("disk full")

        price_tape.append_jsonl = boom
        try:
            price_tape.note(
                ts=1.0,
                slug="s",
                condition_id="c",
                ttm_s=10,
                up_bid=0.4,
                up_ask=0.42,
                dn_bid=0.58,
                dn_ask=0.6,
            )
            price_tape.flush()
        finally:
            price_tape.append_jsonl = real

    def test_size_guard_rolls_the_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "price_tape.jsonl"
            price_tape.set_log_path(path)
            price_tape.set_max_bytes(200)
            fields = dict(
                ts=1.0,
                slug="btc-updown-15m-roll",
                condition_id="cid",
                ttm_s=30,
                up_bid=0.2,
                up_ask=0.21,
                dn_bid=0.8,
                dn_ask=0.81,
                up_bid_sz=10,
                dn_bid_sz=10,
            )
            price_tape.note(**fields)
            price_tape.flush()
            price_tape.note(**fields)
            price_tape.flush()
            archived = list((Path(tmp) / "archive").glob("price_tape.jsonl.*"))
            self.assertTrue(archived)
            self.assertTrue(path.exists())
            self.assertLess(path.stat().st_size, 200 * 4)
            flush_archive_jobs()
        price_tape.set_max_bytes(price_tape.PRICE_TAPE_MAX_BYTES)


class TapeLoopTests(unittest.TestCase):
    def _view(self, clock):
        def view(_cid):
            return SimpleNamespace(
                twap="101000",
                open_usd="100000",
                obs_ts=clock["now"],
                recv_ts=clock["now"],
                open_source="rtds_twap_at_start",
            )

        return view

    def test_sell_tick_writes_only_the_open_bag_inside_the_window(self):
        ns, _events, fak_on, clock, _book = _scrap_harness()
        ns["_oracle_bag_view"] = self._view(clock)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "price_tape.jsonl"
            ns["_price_tape_tick"].log_path = path
            end = clock["now"]
            near = _open_scrap_bag(
                end + 120.0,
                slug="btc-updown-15m-near",
                sell_loser_armed_at=clock["now"] - 10.0,
            )
            far = _open_scrap_bag(
                end + 500.0,
                slug="btc-updown-15m-far",
                sell_loser_armed_at=clock["now"] - 10.0,
            )
            cfg = _scrap_cfg(sell_persist_s=0.0, sell_cooldown_s=0.0, sell_scrap_max_ttm_s=0)
            state = {"intents": {"near": near, "far": far}}
            ns["remember_persisted_state"](state)
            ns["_manage_sells_locked"](cfg, state, object())
            rows = _read(path)
            self.assertEqual([row["slug"] for row in rows], ["btc-updown-15m-near"])
            self.assertEqual(rows[0]["up_bid"], 0.01)
            self.assertEqual(rows[0]["dn_bid"], 0.98)
            self.assertIn("up_ask", rows[0])
            self.assertIn("dn_ask", rows[0])
            self.assertEqual(rows[0]["up_bid_sz"], 40.0)
            self.assertEqual(rows[0]["dn_bid_sz"], 80.0)
            self.assertEqual(rows[0]["oracle_px"], 101000.0)
            self.assertEqual(rows[0]["oracle_start"], 100000.0)
            self.assertGreater(rows[0]["ttm_s"], 0)
            self.assertLessEqual(rows[0]["ttm_s"], 360)
            on_faks = len(fak_on)

            quiet = Path(tmp) / "off.jsonl"
            ns["_price_tape_tick"].log_path = quiet
            cfg["price_tape_enabled"] = False
            fresh = _open_scrap_bag(
                end + 120.0,
                slug="btc-updown-15m-near",
                sell_loser_armed_at=clock["now"] - 10.0,
            )
            state_off = {"intents": {"near": fresh}}
            ns["remember_persisted_state"](state_off)
            fak_before = len(fak_on)
            ns["_manage_sells_locked"](cfg, state_off, object())
            price_tape.flush()
            self.assertFalse(quiet.exists())
            self.assertGreater(len(fak_on), fak_before)
            self.assertEqual(len(fak_on) - fak_before, 1)
            self.assertGreaterEqual(on_faks, 1)

    def test_tape_exception_does_not_stop_the_sell_tick(self):
        ns, _events, fak_calls, clock, _book = _scrap_harness()

        def boom(**_kwargs):
            raise RuntimeError("tape failed")

        real = price_tape.note
        price_tape.note = boom
        try:
            end = clock["now"] + 120.0
            intent = _open_scrap_bag(end, sell_loser_armed_at=clock["now"] - 10.0)
            cfg = _scrap_cfg(sell_persist_s=0.0, sell_cooldown_s=0.0, sell_scrap_max_ttm_s=0)
            state = {"intents": {"cid": intent}}
            ns["remember_persisted_state"](state)
            ns["_manage_sells_locked"](cfg, state, object())
        finally:
            price_tape.note = real
        self.assertTrue(fak_calls)
        self.assertTrue(intent.get("sold_loser"))


if __name__ == "__main__":
    unittest.main()
