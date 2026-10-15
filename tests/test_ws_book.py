"""Websocket book cache for the sell loop, and the zero armed-poll backoff."""

from __future__ import annotations

import json
import threading
import unittest

from buy.clob_book_ws import (
    ClobBookFeed,
    backoff_s,
    book_diff_fields,
    watch_token_ids,
)
from buy.mint_sell import ws_book_enabled, ws_book_stale_s
from test_mint_cpu import _dump_cfg, _dump_harness, _fill_fak, _held_after_scrap
from test_mint_only_ops import MINT_EXAMPLE, _assign


def _book(asset: str, bid: str, ask: str = "0.99", size: str = "20") -> dict:
    return {
        "event_type": "book",
        "asset_id": asset,
        "bids": [{"price": bid, "size": size}],
        "asks": [{"price": ask, "size": size}],
    }


class _FakeWS:
    def __init__(self, frames: list):
        self.frames = list(frames)
        self.sent: list = []
        self.closed = False

    def settimeout(self, _timeout: float) -> None:
        return None

    def send(self, payload: str) -> None:
        self.sent.append(payload)

    def recv(self):
        if self.frames:
            return self.frames.pop(0)
        raise ConnectionError("eof")

    def close(self) -> None:
        self.closed = True


class FeedTests(unittest.TestCase):
    def _feed(self, tokens, *, now=1000.0):
        clock = {"t": now}
        feed = ClobBookFeed(clock=lambda: clock["t"])
        feed.set_min_size(1.0)
        feed.set_stale_s(1.5)
        feed.set_tokens(tokens)
        feed._healthy = True
        return feed, clock

    def test_stale_top_is_not_usable_and_old_tokens_drop(self):
        feed, clock = self._feed(["up-tok", "dn-tok", "old-tok", "extra", "fifth"])
        self.assertEqual(len(feed._wanted), 4)
        self.assertNotIn("fifth", feed._wanted)
        feed.apply_event(_book("up-tok", "0.39"))
        feed.apply_event(_book("dn-tok", "0.49"))
        self.assertIsNotNone(feed.usable("up-tok"))
        self.assertAlmostEqual(feed.usable("up-tok").bid, 0.39)
        clock["t"] = 1001.6
        self.assertIsNone(feed.usable("up-tok"))
        self.assertAlmostEqual(feed.cached_bid("up-tok"), 0.39)
        feed.set_tokens(["next-up", "next-dn"])
        self.assertIsNone(feed.cached_bid("up-tok"))
        self.assertEqual(feed._wanted, ["next-up", "next-dn"])

    def test_unhealthy_socket_falls_back_even_when_the_top_is_new(self):
        feed, _clock = self._feed(["up-tok"])
        feed.apply_event(_book("up-tok", "0.39"))
        feed._healthy = False
        self.assertIsNone(feed.usable("up-tok"))

    def test_price_move_without_size_is_not_a_sized_bid(self):
        feed, _clock = self._feed(["up-tok"])
        feed.apply_event(_book("up-tok", "0.39"))
        feed.apply_event({
            "event_type": "best_bid_ask",
            "asset_id": "up-tok",
            "best_bid": "0.20",
            "best_ask": "0.80",
        })
        self.assertIsNone(feed.usable("up-tok"))
        self.assertAlmostEqual(feed.cached_bid("up-tok"), 0.20)

    def test_reconnect_backs_off_and_resubscribes(self):
        feed, _clock = self._feed(["tok-a", "tok-b"])
        feed._healthy = False
        made: list = []
        sleeps: list = []
        stop = threading.Event()

        def connect(_url: str):
            if not made:
                made.append(None)
                raise OSError("down")
            ws = _FakeWS([json.dumps(_book("tok-a", "0.42", "0.44"))])
            made.append(ws)
            return ws

        def sleeper(delay: float) -> None:
            sleeps.append(delay)
            if len(sleeps) >= 2:
                stop.set()

        feed._connect = connect
        feed._sleep = sleeper
        feed.run(stop)
        self.assertEqual(sleeps[0], backoff_s(1))
        self.assertGreaterEqual(feed.reconnects, 1)
        subscribed = made[-1].sent[0]
        payload = json.loads(subscribed)
        self.assertEqual(payload["type"], "market")
        self.assertCountEqual(payload["assets_ids"], ["tok-a", "tok-b"])
        self.assertAlmostEqual(feed.cached_bid("tok-a"), 0.42)
        self.assertFalse(feed.healthy())
        self.assertTrue(made[-1].closed)

    def test_watch_ids_are_current_plus_next(self):
        intents = {
            "old": {
                "status": "confirmed", "start_ts": 0, "end_ts": 50,
                "up_token": "old-u", "dn_token": "old-d",
            },
            "now": {
                "status": "confirmed", "start_ts": 100, "end_ts": 1000,
                "up_token": "now-u", "dn_token": "now-d",
            },
            "next": {
                "status": "confirmed", "start_ts": 1000, "end_ts": 1900,
                "up_token": "next-u", "dn_token": "next-d",
            },
            "later": {
                "status": "confirmed", "start_ts": 1900, "end_ts": 2800,
                "up_token": "later-u", "dn_token": "later-d",
            },
        }
        self.assertEqual(
            watch_token_ids(intents, 500),
            ["now-u", "now-d", "next-u", "next-d"],
        )
        self.assertEqual(book_diff_fields(0.39, 0.51)["bid_diff"], -0.12)
        self.assertIsNone(book_diff_fields(0.39, None)["bid_diff"])

    def test_defaults_leave_the_socket_off(self):
        defaults = _assign("DEFAULTS")
        example = json.loads(MINT_EXAMPLE.read_text())
        self.assertIs(defaults["ws_book_enabled"], False)
        self.assertIs(example["ws_book_enabled"], False)
        self.assertEqual(defaults["ws_book_stale_s"], 1.5)
        self.assertEqual(example["ws_book_stale_s"], 1.5)
        self.assertFalse(ws_book_enabled({}))
        self.assertFalse(ws_book_enabled({"ws_book_enabled": False}))
        self.assertTrue(ws_book_enabled({"ws_book_enabled": True}))
        self.assertEqual(ws_book_stale_s({}), 1.5)
        self.assertEqual(ws_book_stale_s({"ws_book_stale_s": 0}), 1.5)


class SellLoopBookTests(unittest.TestCase):
    def _tick(self, ns, cfg, intent):
        state = {"intents": {"cid-ws": intent}}
        ns["remember_persisted_state"](state)
        ns["_manage_sells_locked"](cfg, state, object())

    def test_disabled_matches_rest_and_does_not_log_a_source(self):
        ns, events, fak_calls, clock = _dump_harness(_fill_fak)
        fetches = {"n": 0}
        real = ns["_fetch_books"]

        def counting(*args, **kwargs):
            fetches["n"] += 1
            return real(*args, **kwargs)

        ns["_fetch_books"] = counting
        end = clock["now"] + 200.0
        intent = _held_after_scrap(end, sell_dump_armed_at=clock["now"] - 2.0)
        self._tick(ns, _dump_cfg(), intent)
        self.assertEqual(fetches["n"], 1)
        self.assertAlmostEqual(fak_calls[0]["price"], 0.51)
        self.assertFalse(any(row["event"] == "book_source" for row in events))
        self.assertTrue(intent["sold_dump"])

    def test_fresh_ws_sets_the_dump_price_and_skips_rest(self):
        ns, events, fak_calls, clock = _dump_harness(_fill_fak)
        fetches = {"n": 0}
        real = ns["_fetch_books"]

        def counting(*args, **kwargs):
            fetches["n"] += 1
            return real(*args, **kwargs)

        ns["_fetch_books"] = counting
        feed, _mono = FeedTests()._feed(["up-tok", "dn-tok"])
        feed._started = True
        feed.apply_event(_book("up-tok", "0.39", "0.40"))
        feed.apply_event(_book("dn-tok", "0.49", "0.51"))
        feed.remember_rest("up-tok", 0.51)
        feed.remember_rest("dn-tok", 0.49)
        ns["_BOOK_FEED_BOX"] = {"feed": feed}
        end = clock["now"] + 200.0
        intent = _held_after_scrap(end, sell_dump_armed_at=clock["now"] - 2.0)
        self._tick(ns, _dump_cfg(ws_book_enabled=True), intent)
        self.assertEqual(fetches["n"], 0)
        self.assertAlmostEqual(fak_calls[0]["price"], 0.39)
        sources = [row for row in events if row["event"] == "book_source"]
        self.assertTrue(sources)
        self.assertEqual(sources[0]["path"], "dump")
        self.assertEqual(sources[0]["book_source"], "ws")
        self.assertAlmostEqual(sources[0]["bid_diff"], -0.12)

    def test_stale_ws_falls_back_to_the_rest_book(self):
        ns, events, fak_calls, clock = _dump_harness(_fill_fak)
        fetches = {"n": 0}
        real = ns["_fetch_books"]

        def counting(*args, **kwargs):
            fetches["n"] += 1
            return real(*args, **kwargs)

        ns["_fetch_books"] = counting
        feed, mono = FeedTests()._feed(["up-tok", "dn-tok"])
        feed._started = True
        feed.apply_event(_book("up-tok", "0.39", "0.40"))
        feed.apply_event(_book("dn-tok", "0.49", "0.51"))
        mono["t"] += 3.0
        ns["_BOOK_FEED_BOX"] = {"feed": feed}
        end = clock["now"] + 200.0
        intent = _held_after_scrap(end, sell_dump_armed_at=clock["now"] - 2.0)
        self._tick(ns, _dump_cfg(ws_book_enabled=True, ws_book_stale_s=1.5), intent)
        self.assertEqual(fetches["n"], 1)
        self.assertAlmostEqual(fak_calls[0]["price"], 0.51)
        sources = [row for row in events if row["event"] == "book_source"]
        self.assertEqual(sources[0]["book_source"], "rest")
        self.assertAlmostEqual(sources[0]["ws_bid"], 0.39)
        self.assertAlmostEqual(sources[0]["rest_bid"], 0.51)
        self.assertAlmostEqual(sources[0]["bid_diff"], -0.12)


if __name__ == "__main__":
    unittest.main()
