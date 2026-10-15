"""Recorder frame parsing, window selection, and gzip tape writes."""

from __future__ import annotations

import json
import tempfile
import threading
import unittest
from pathlib import Path

from buy.market import MintMarket
from recorder.markets import bags_to_record, select_bags
from recorder.oracle_rows import oracle_records
from recorder.parse import (
    TokenRef,
    apply_record,
    backoff_s,
    exchange_ts,
    records_from_payload,
)
from recorder.run import consume_market
from recorder.tape import TapeWriter


def _market(slug: str, start: float, *, series: str = "btc-up-or-down-15m", up: str = "U", dn: str = "D") -> MintMarket:
    return MintMarket(
        condition_id="c-" + slug,
        slug=slug,
        question="",
        end_ts=start + 900.0,
        series_slug=series,
        up_token=up,
        dn_token=dn,
        active=True,
        closed=False,
        accepting_orders=True,
        neg_risk=False,
        start_ts=start,
    )


TOKENS = {
    "up-token": TokenRef("btc-updown-15m-1700000000", "up", 1700000000.0, 1700000900.0),
    "dn-token": TokenRef("btc-updown-15m-1700000000", "down", 1700000000.0, 1700000900.0),
}


class ParseTests(unittest.TestCase):
    def test_exchange_ts_scales_milliseconds(self):
        self.assertEqual(exchange_ts("1700000000500"), 1700000000.5)
        self.assertEqual(exchange_ts(1700000000.25), 1700000000.25)
        self.assertIsNone(exchange_ts("PING"))
        self.assertIsNone(exchange_ts(0))

    def test_backoff_doubles_and_caps(self):
        self.assertEqual(backoff_s(0), 1.0)
        self.assertEqual(backoff_s(1), 2.0)
        self.assertEqual(backoff_s(3), 8.0)
        self.assertEqual(backoff_s(99), 30.0)
        self.assertEqual(backoff_s(-4), 1.0)

    def test_book_snapshot_and_price_change_and_trade(self):
        raw = json.dumps(
            [
                {
                    "event_type": "book",
                    "asset_id": "up-token",
                    "market": "0xabc",
                    "timestamp": "1700000000100",
                    "hash": "h1",
                    "bids": [{"price": "0.92", "size": "70"}, {"price": "0.93", "size": "40"}],
                    "asks": [{"price": "0.94", "size": "10"}],
                },
                {
                    "event_type": "price_change",
                    "market": "0xabc",
                    "timestamp": "1700000000200",
                    "price_changes": [
                        {"asset_id": "up-token", "price": "0.92", "size": "0", "side": "BUY"},
                        {"asset_id": "other", "price": "0.50", "size": "5", "side": "BUY"},
                    ],
                },
                {
                    "event_type": "last_trade_price",
                    "asset_id": "dn-token",
                    "timestamp": "1700000000300",
                    "price": "0.07",
                    "size": "12",
                    "side": "SELL",
                },
            ]
        )
        rows = records_from_payload(raw, 1700000000.4, TOKENS)
        self.assertEqual([row["event_type"] for row in rows], ["book", "price_change", "last_trade"])
        self.assertEqual(rows[0]["exch_ts"], 1700000000.1)
        self.assertEqual(rows[0]["recv_ts"], 1700000000.4)
        self.assertEqual(rows[0]["leg"], "up")
        self.assertEqual(rows[0]["bids"][0], ["0.92", "70"])
        self.assertEqual(rows[1]["price"], "0.92")
        self.assertEqual(rows[1]["size"], "0")
        self.assertEqual(rows[1]["exch_ts"], 1700000000.2)
        self.assertEqual(rows[2]["leg"], "down")
        self.assertEqual(rows[2]["event_type"], "last_trade")
        books: dict = {}
        for row in rows:
            apply_record(books, row)
        self.assertNotIn(0.92, books["up"]["bid"])
        self.assertEqual(books["up"]["bid"][0.93], 40.0)
        self.assertEqual(books["up"]["ask"][0.94], 10.0)
        self.assertNotIn("down", books)

    def test_ping_and_junk_are_ignored(self):
        self.assertEqual(records_from_payload("PING", 1.0, TOKENS), [])
        self.assertEqual(records_from_payload("not-json", 1.0, TOKENS), [])

    def test_price_change_creates_a_level_and_sell_is_the_ask(self):
        row = records_from_payload(
            {
                "event_type": "price_change",
                "timestamp": 10,
                "price_changes": [
                    {"asset_id": "up-token", "price": "0.40", "size": "15", "side": "SELL"},
                ],
            },
            11.0,
            TOKENS,
        )[0]
        books: dict = {}
        apply_record(books, row)
        self.assertEqual(books["up"]["ask"][0.4], 15.0)
        self.assertEqual(books["up"]["bid"], {})

    def test_oracle_frame_uses_the_shared_parser(self):
        raw = json.dumps(
            {
                "topic": "crypto_prices_chainlink",
                "type": "update",
                "timestamp": 1_791_042_659_000,
                "payload": {
                    "full_accuracy_value": "84841164260323815000000",
                    "symbol": "btc/usd",
                    "timestamp": 1_791_042_658_000,
                    "value": 84841.16,
                },
            }
        )
        rows = oracle_records(raw, 1_791_042_659.2)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["source"], "live")
        self.assertEqual(rows[0]["exch_ts"], 1_791_042_658.0)
        self.assertEqual(rows[0]["recv_ts"], 1_791_042_659.2)
        self.assertTrue(rows[0]["price"].startswith("84841."))


class MarketWindowTests(unittest.TestCase):
    def test_current_and_next_only(self):
        start = 1_700_000_000.0
        current = _market("btc-updown-15m-1700000000", start, up="u1", dn="d1")
        nxt = _market("btc-updown-15m-1700000900", start + 900, up="u2", dn="d2")
        later = _market("btc-updown-15m-1700001800", start + 1800, up="u3", dn="d3")
        five = _market("btc-updown-5m-1700000000", start, series="btc-up-or-down-5m")
        chosen = select_bags([later, five, nxt, current], start + 100)
        self.assertEqual([m.slug for m in chosen], [current.slug, nxt.slug])

    def test_grace_keeps_the_window_gamma_dropped(self):
        start = 1_700_000_000.0
        closed = _market("btc-updown-15m-1700000000", start, up="u1", dn="d1")
        current = _market("btc-updown-15m-1700000900", start + 900, up="u2", dn="d2")
        nxt = _market("btc-updown-15m-1700001800", start + 1800, up="u3", dn="d3")
        now = start + 910
        chosen = bags_to_record([current, nxt], [closed], now, grace_s=30)
        self.assertEqual([m.slug for m in chosen], [closed.slug, current.slug, nxt.slug])
        later = bags_to_record([current, nxt], [closed], start + 960, grace_s=30)
        self.assertEqual([m.slug for m in later], [current.slug, nxt.slug])


class TapeWriteTests(unittest.TestCase):
    def test_flush_writes_a_readable_gzip_member(self):
        clock = {"t": 0.0}

        def now() -> float:
            return clock["t"]

        with tempfile.TemporaryDirectory() as tmp:
            writer = TapeWriter(tmp, clock=now, flush_s=1.0)
            writer.write(
                {
                    "event_type": "book",
                    "recv_ts": 1.0,
                    "slug": "btc-updown-15m-1700000000",
                    "start_ts": 1700000000.0,
                    "leg": "up",
                    "bids": [["0.92", "10"]],
                    "asks": [],
                }
            )
            writer.flush()
            self.assertEqual(list(Path(tmp).rglob("*.jsonl.gz")), [])
            clock["t"] = 1.0
            writer.flush()
            files = list(Path(tmp).rglob("*.jsonl.gz"))
            self.assertEqual(len(files), 1)
            self.assertIn("2023-11-14", str(files[0]))
            from recorder.replay import Tape

            tape = Tape.load(files[0])
            self.assertEqual(tape.records[0]["bids"], [["0.92", "10"]])
            writer.write(
                {
                    "event_type": "price_change",
                    "recv_ts": 2.0,
                    "slug": "btc-updown-15m-1700000000",
                    "start_ts": 1700000000.0,
                    "leg": "up",
                    "price": "0.91",
                    "size": "4",
                    "side": "BUY",
                }
            )
            writer.close()
            tape = Tape.load(files[0])
            self.assertEqual(len(tape.records), 2)


class ConsumeTests(unittest.TestCase):
    def test_one_frame_is_written_and_ping_is_answered(self):
        frame = json.dumps(
            {
                "event_type": "book",
                "asset_id": "up-token",
                "timestamp": "1700000000000",
                "bids": [{"price": "0.50", "size": "3"}],
                "asks": [],
            }
        )

        class FakeSocket:
            def __init__(self) -> None:
                self.sent: list[str] = []
                self._frames = [frame, "PING"]

            def send(self, data: str) -> None:
                self.sent.append(data)

            def recv(self):
                if self._frames:
                    return self._frames.pop(0)
                stop.set()
                raise TimeoutError("idle")

        stop = threading.Event()
        with tempfile.TemporaryDirectory() as tmp:
            writer = TapeWriter(tmp, flush_s=10.0)
            sock = FakeSocket()
            why = consume_market(
                sock,
                tokens=TOKENS,
                writer=writer,
                stop=stop,
                generation=1,
                current_generation=lambda: 1,
                silence_s=100.0,
                ping_s=10_000.0,
                wall=lambda: 1.5,
            )
            writer.flush(force=True)
            writer.close()
            self.assertEqual(why, "stop")
            self.assertIn("PONG", sock.sent)
            from recorder.replay import Tape

            tape = Tape.load(next(Path(tmp).rglob("*.jsonl.gz")))
            self.assertEqual(tape.size_at(2.0, "bid", 0.50), 3.0)


class IsolationTests(unittest.TestCase):
    def test_recorder_does_not_import_mintbot(self):
        root = Path(__file__).resolve().parents[1]
        texts = [(root / "recorder.py").read_text(encoding="utf-8")]
        texts.extend(path.read_text(encoding="utf-8") for path in (root / "recorder").glob("*.py"))
        blob = "\n".join(texts)
        self.assertNotIn("import mintbot", blob)
        self.assertNotIn("strategy_mint", blob)
