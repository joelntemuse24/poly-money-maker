"""Optional books.jsonl logger: off is a no-op, on is throttled and bounded."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

import lockbot
from buy.lock_book_log import QUEUE_MAX, BookSnapshotLogger
from buy.lock_bookws import ClobBookFeed
from buy.lock_config import DEFAULTS, apply_defaults, validate_config
from buy.lock_markets import LockMarket
from buy.log_archive import flush_archive_jobs


def market(slug="btc-updown-5m-1000", up="up1", dn="dn1", key="btc_5m"):
    return LockMarket(
        asset="btc", duration="5m", lane="x", key=key, symbol="BTC", slug=slug,
        series_slug="btc-up-or-down-5m", condition_id="c", question="q",
        start_ts=900.0, end_ts=1200.0, up_token=up, dn_token=dn,
        resolution_source="", price_to_beat=None, resolution_ok=True, accepting_orders=True,
    )


def snap(feed_token="up1", asks=None, bids=None, recv=1.0):
    return {
        "event_type": "book",
        "asset_id": feed_token,
        "asks": [{"price": str(p), "size": str(s)} for p, s in (asks or [(0.6, 10), (0.61, 5), (0.7, 1)])],
        "bids": [{"price": str(p), "size": str(s)} for p, s in (bids or [(0.58, 7), (0.57, 3)])],
    }


class Harness:
    def __init__(self, test, **cfg):
        tmp = tempfile.TemporaryDirectory()
        test.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.now = [100.0]
        self.feed = ClobBookFeed(clock=lambda: self.now[0])
        self.log = BookSnapshotLogger(self.root, self.feed.book, clock=lambda: self.now[0], threaded=False)
        test.addCleanup(self.log.close)
        base = {"book_log_enabled": True, "book_log_min_interval_ms": 100}
        base.update(cfg)
        self.log.configure(apply_defaults(base))
        self.log.set_markets([market()])
        if self.log.enabled:
            self.feed.touch_hook = self.log.on_touch

    def feed_msg(self, payload):
        self.feed.handle_message(json.dumps(payload))

    def rows(self):
        self.log.pump()
        self.log.close()
        path = self.root / "logs" / "books.jsonl"
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text().splitlines()]


class ConfigTests(unittest.TestCase):
    def test_defaults_are_off_and_valid(self):
        cfg = apply_defaults({})
        self.assertFalse(cfg["book_log_enabled"])
        self.assertEqual(cfg["book_log_levels"], 5)
        self.assertEqual(cfg["book_log_min_interval_ms"], 100)
        self.assertEqual(cfg["book_log_path"], "logs/books.jsonl")
        self.assertEqual(cfg["book_log_max_bytes"], 104857600)
        validate_config(cfg)
        self.assertTrue(cfg["dry_run"])

    def test_example_mirrors_defaults(self):
        root = Path(__file__).resolve().parent.parent
        example = json.loads((root / "lockbot.example.json").read_text())
        for key in ("book_log_enabled", "book_log_levels", "book_log_min_interval_ms", "book_log_path", "book_log_max_bytes"):
            self.assertEqual(example[key], DEFAULTS[key], key)

    def test_validation(self):
        for bad in ({"book_log_levels": 0}, {"book_log_levels": 2.5}, {"book_log_min_interval_ms": -1},
                    {"book_log_max_bytes": -5}, {"book_log_path": ""}):
            with self.assertRaises(ValueError, msg=str(bad)):
                validate_config(apply_defaults(bad))
        validate_config(apply_defaults({"book_log_min_interval_ms": 0}))

    def test_enabled_string_is_parsed(self):
        self.assertTrue(apply_defaults({"book_log_enabled": "true"})["book_log_enabled"])


class DisabledTests(unittest.TestCase):
    def test_disabled_is_a_no_op(self):
        h = Harness(self, book_log_enabled=False)
        self.assertIsNone(h.feed.touch_hook)
        h.feed_msg(snap())
        h.log.on_touch(["up1"], 1.0)
        self.assertEqual(h.log._q.qsize(), 0)
        self.assertIsNone(h.log._thread)
        self.assertEqual(h.rows(), [])
        self.assertFalse((h.root / "logs").exists())
        self.assertEqual(h.log.stats()["drops"], 0)


class EnabledTests(unittest.TestCase):
    def test_row_has_meta_and_levels(self):
        h = Harness(self)
        h.feed_msg(snap())
        row = h.rows()[0]
        self.assertEqual(row["token_id"], "up1")
        self.assertEqual((row["slug"], row["asset"], row["duration"], row["side"]), ("btc-updown-5m-1000", "btc", "5m", "up"))
        self.assertEqual(row["ts"], 100.0)
        self.assertEqual(row["recv_ts"], 100.0)
        self.assertEqual(row["best_ask"], 0.6)
        self.assertEqual(row["best_bid"], 0.58)
        self.assertEqual(row["ask_levels"], [[0.6, 10.0], [0.61, 5.0], [0.7, 1.0]])
        self.assertEqual(row["bid_levels"], [[0.58, 7.0], [0.57, 3.0]])

    def test_down_token_and_unknown_token(self):
        h = Harness(self)
        h.feed_msg(snap("dn1"))
        h.feed_msg(snap("other"))
        rows = h.rows()
        self.assertEqual([r["side"] for r in rows], ["down"])

    def test_empty_side_is_null(self):
        h = Harness(self)
        h.feed_msg({"event_type": "book", "asset_id": "up1", "asks": [], "bids": [{"price": "0.5", "size": "2"}]})
        row = h.rows()[0]
        self.assertIsNone(row["best_ask"])
        self.assertEqual(row["ask_levels"], [])

    def test_levels_n(self):
        h = Harness(self, book_log_levels=2)
        h.feed_msg(snap(asks=[(0.6, 1), (0.61, 1), (0.62, 1)], bids=[(0.5, 1), (0.49, 1), (0.48, 1)]))
        row = h.rows()[0]
        self.assertEqual(len(row["ask_levels"]), 2)
        self.assertEqual([lv[0] for lv in row["bid_levels"]], [0.5, 0.49])

    def test_throttle_skips_unchanged_top_inside_interval(self):
        h = Harness(self)
        h.feed_msg(snap())
        h.log.pump()
        h.now[0] += 0.05
        h.feed_msg({"event_type": "price_change", "price_changes": [{"asset_id": "up1", "side": "SELL", "price": "0.7", "size": "9"}]})
        h.log.pump()
        h.now[0] += 0.01
        h.feed_msg({"event_type": "price_change", "price_changes": [{"asset_id": "up1", "side": "SELL", "price": "0.59", "size": "9"}]})
        h.log.pump()
        h.now[0] += 0.2
        h.feed_msg({"event_type": "price_change", "price_changes": [{"asset_id": "up1", "side": "SELL", "price": "0.72", "size": "9"}]})
        rows = h.rows()
        # first row, then the best-ask change inside the interval, then the
        # unchanged-top update after the interval. The unchanged one at +0.05 is skipped.
        self.assertEqual([r["best_ask"] for r in rows], [0.6, 0.59, 0.59])

    def test_zero_interval_logs_every_change(self):
        h = Harness(self, book_log_min_interval_ms=0)
        h.feed_msg(snap())
        h.log.pump()
        for px in ("0.7", "0.71"):
            h.now[0] += 0.001
            h.feed_msg({"event_type": "price_change", "price_changes": [{"asset_id": "up1", "side": "SELL", "price": px, "size": "9"}]})
            h.log.pump()
        self.assertEqual(len(h.rows()), 3)

    def test_throttle_is_per_token(self):
        h = Harness(self)
        h.feed_msg(snap("up1"))
        h.feed_msg(snap("dn1"))
        self.assertEqual(len(h.rows()), 2)

    def test_queue_coalesces_and_drops_when_full(self):
        h = Harness(self)
        h.log.set_markets([market(up=f"u{i}", dn=f"d{i}") for i in range(QUEUE_MAX)] + [market(up="extra", dn="extra2")])
        tokens = [f"u{i}" for i in range(QUEUE_MAX)]
        h.log.on_touch(tokens + tokens, 1.0)
        self.assertEqual(h.log.stats()["queued"], QUEUE_MAX)
        self.assertEqual(h.log.stats()["drops"], 0)
        h.log.on_touch(["extra"], 1.0)
        self.assertEqual(h.log.stats()["drops"], 1)
        self.assertEqual(h.log.stats()["queued"], QUEUE_MAX)

    def test_book_log_error_never_reaches_the_feed(self):
        h = Harness(self)
        h.feed.touch_hook = lambda tokens, now: 1 / 0
        h.feed_msg(snap())
        self.assertEqual(h.feed.messages, 1)

    def test_size_rotation(self):
        h = Harness(self, book_log_max_bytes=600, book_log_min_interval_ms=0)
        for i in range(12):
            h.feed_msg(snap(asks=[(0.6 + i / 1000, 10)]))
            h.log.pump()
            h.now[0] += 1
        h.log.close()
        flush_archive_jobs()
        archive = h.root / "logs" / "archive"
        self.assertTrue(archive.exists() and any(archive.iterdir()))
        self.assertLess((h.root / "logs" / "books.jsonl").stat().st_size, 1200)

    def test_threaded_writer_flushes(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        feed = ClobBookFeed()
        log = BookSnapshotLogger(Path(tmp.name), feed.book)
        self.addCleanup(log.close)
        log.configure(apply_defaults({"book_log_enabled": True}))
        log.set_markets([market()])
        feed.touch_hook = log.on_touch
        feed.handle_message(json.dumps(snap()))
        import time
        deadline = time.time() + 3
        while log.rows == 0 and time.time() < deadline:
            time.sleep(0.01)
        self.assertEqual(log.rows, 1)


class LockBotWiringTests(unittest.TestCase):
    def make_bot(self, root, cfg):
        bot = lockbot.LockBot.__new__(lockbot.LockBot)
        bot.cfg = apply_defaults(cfg)
        bot.book_feed = ClobBookFeed()
        bot.book_log = BookSnapshotLogger(root, bot.book_feed.book, threaded=False)
        bot.markets = {}
        return bot

    def test_hot_reload_toggles_hook(self):
        with tempfile.TemporaryDirectory() as tmp:
            bot = self.make_bot(Path(tmp), {})
            bot._apply_book_log(1000.0)
            self.assertIsNone(bot.book_feed.touch_hook)
            bot.cfg = apply_defaults({"book_log_enabled": True, "book_log_levels": 3})
            bot._apply_book_log(1000.0)
            self.assertIsNotNone(bot.book_feed.touch_hook)
            self.assertEqual(bot.book_log._levels_n, 3)
            bot.cfg = apply_defaults({})
            bot._apply_book_log(1000.0)
            self.assertIsNone(bot.book_feed.touch_hook)
            self.assertFalse(bot.book_log.enabled)

    def test_subscribe_registers_btc_tokens_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            bot = self.make_bot(Path(tmp), {"book_log_enabled": True})
            bot._apply_book_log(1000.0)
            bot.markets = {"btc-updown-5m-1000": market(), "eth": market("eth-x", "eu", "ed", key="eth_5m")}
            bot.subscribe_books(1000.0)
            self.assertEqual(set(bot.book_log._meta), {"up1", "dn1"})


if __name__ == "__main__":
    unittest.main()
