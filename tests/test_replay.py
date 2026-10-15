"""Replay math: depth at a price and a market sell walking the bids."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from recorder.replay import Tape, up_bid_scenario
from recorder.tape import TapeWriter


def _book(recv: float, bids: list, asks: list | None = None, *, leg: str = "up", exch: float | None = None) -> dict:
    return {
        "event_type": "book",
        "recv_ts": recv,
        "exch_ts": recv if exch is None else exch,
        "slug": "btc-updown-15m-1700000000",
        "start_ts": 1700000000.0,
        "leg": leg,
        "bids": [[str(px), str(sz)] for px, sz in bids],
        "asks": [[str(px), str(sz)] for px, sz in (asks or [])],
    }


class ReplayMathTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tape = Tape(
            [
                _book(
                    100.0,
                    [(0.93, 40), (0.92, 70), (0.91, 200)],
                    [(0.94, 15)],
                ),
                {
                    "event_type": "last_trade",
                    "recv_ts": 100.5,
                    "exch_ts": 100.5,
                    "slug": "btc-updown-15m-1700000000",
                    "start_ts": 1700000000.0,
                    "leg": "up",
                    "price": "0.93",
                    "size": "5",
                    "side": "BUY",
                },
                {
                    "event_type": "oracle",
                    "source": "twap",
                    "recv_ts": 100.2,
                    "exch_ts": 99.0,
                    "slug": "btc-updown-15m-1700000000",
                    "start_ts": 1700000000.0,
                    "leg": "",
                    "price": "67000.5",
                },
            ]
        )

    def test_depth_and_exact_rung_and_sell_100(self):
        report = up_bid_scenario(self.tape, 100.0)
        self.assertEqual(report["shares_bid_at_or_above"], 110.0)
        self.assertEqual(report["shares_bid_at_price"], 70.0)
        self.assertEqual(report["sell_filled"], 100.0)
        self.assertEqual(report["sell_remaining"], 0.0)
        self.assertAlmostEqual(report["sell_vwap"], 0.924)
        self.assertAlmostEqual(report["sell_notional"], 92.4)

    def test_sell_larger_than_the_book_is_partial(self):
        fill = self.tape.fill_estimate(100.0, "sell", 1000)
        self.assertEqual(fill.filled, 310.0)
        self.assertEqual(fill.remaining, 690.0)
        self.assertAlmostEqual(fill.notional, 40 * 0.93 + 70 * 0.92 + 200 * 0.91)

    def test_events_after_ts_are_not_applied(self):
        self.assertEqual(self.tape.depth_at(99.0, "bid", 0.92), 0.0)
        self.assertIsNone(self.tape.oracle_at(100.0, "twap"))
        self.assertEqual(self.tape.oracle_at(100.2, "twap"), "67000.5")

    def test_trade_print_does_not_change_the_book(self):
        self.assertEqual(self.tape.size_at(100.5, "bid", 0.93), 40.0)

    def test_price_change_replaces_one_level_after_the_snapshot(self):
        tape = Tape(
            [
                _book(10.0, [(0.92, 70), (0.91, 5)]),
                {
                    "event_type": "price_change",
                    "recv_ts": 11.0,
                    "exch_ts": 11.0,
                    "slug": "btc-updown-15m-1700000000",
                    "start_ts": 1700000000.0,
                    "leg": "up",
                    "price": "0.92",
                    "size": "0",
                    "side": "BUY",
                },
            ]
        )
        self.assertEqual(tape.depth_at(11.0, "bid", 0.90), 5.0)
        self.assertEqual(tape.size_at(11.0, "bid", 0.92), 0.0)

    def test_exchange_clock_ignores_a_late_receive(self):
        tape = Tape([_book(200.0, [(0.92, 10)], exch=50.0)])
        self.assertEqual(tape.depth_at(60.0, "bid", 0.92, clock="exch"), 10.0)
        self.assertEqual(tape.depth_at(60.0, "bid", 0.92, clock="recv"), 0.0)

    def test_down_leg_is_separate(self):
        tape = Tape(
            [
                _book(5.0, [(0.20, 8)], leg="down"),
                _book(5.0, [(0.80, 8)], leg="up"),
            ]
        )
        self.assertEqual(tape.depth_at(5.0, "bid", 0.10, leg="down"), 8.0)
        self.assertEqual(tape.depth_at(5.0, "bid", 0.10, leg="up"), 8.0)

    def test_buy_walks_asks_low_to_high(self):
        fill = self.tape.fill_estimate(100.0, "buy", 100)
        self.assertEqual(fill.filled, 15.0)
        self.assertAlmostEqual(fill.vwap, 0.94)
        self.assertEqual(fill.remaining, 85.0)

    def test_file_roundtrip_matches_the_scenario(self):
        with tempfile.TemporaryDirectory() as tmp:
            writer = TapeWriter(tmp)
            for record in self.tape.records:
                writer.write(record)
            writer.close()
            path = next(Path(tmp).rglob("*.jsonl.gz"))
            loaded = Tape.load(path)
            self.assertEqual(up_bid_scenario(loaded, 100.0)["sell_vwap"], 0.924)

    def test_load_reads_the_sibling_oracle_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            writer = TapeWriter(tmp)
            writer.write(_book(100.0, [(0.92, 10)]))
            writer.write(
                {
                    "event_type": "oracle",
                    "source": "twap",
                    "recv_ts": 100.2,
                    "exch_ts": 99.0,
                    "slug": "oracle",
                    "start_ts": 1700000000.0,
                    "leg": "",
                    "price": "67000.5",
                }
            )
            writer.close()
            files = {path.name: path for path in Path(tmp).rglob("*.jsonl.gz")}
            self.assertEqual(set(files), {"btc-updown-15m-1700000000.jsonl.gz", "oracle.jsonl.gz"})
            loaded = Tape.load(files["btc-updown-15m-1700000000.jsonl.gz"])
            self.assertEqual(loaded.depth_at(100.0, "bid", 0.92), 10.0)
            self.assertEqual(loaded.oracle_at(100.2, "twap"), "67000.5")
            oracle_only = Tape.load(files["oracle.jsonl.gz"])
            self.assertEqual(len(oracle_only.records), 1)

    def test_example_cli_prints_the_scenario(self):
        import io
        from contextlib import redirect_stdout

        import tools.example_up_bid as example

        with tempfile.TemporaryDirectory() as tmp:
            writer = TapeWriter(tmp)
            for record in self.tape.records:
                writer.write(record)
            writer.close()
            path = next(Path(tmp).rglob("*.jsonl.gz"))
            buf = io.StringIO()
            with redirect_stdout(buf):
                code = example.main([str(path), "--ts", "100"])
            self.assertEqual(code, 0)
            payload = json.loads(buf.getvalue())
            self.assertEqual(payload["shares_bid_at_or_above"], 110.0)
            self.assertAlmostEqual(payload["sell_vwap"], 0.924)
