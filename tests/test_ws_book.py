"""Websocket book cache for the sell loop, and the zero armed-poll backoff."""

from __future__ import annotations

import json
import threading
import unittest

import time

from buy.clob_book_ws import (
    PING_S,
    SILENCE_S,
    ClobBookFeed,
    backoff_s,
    book_diff_fields,
    watch_token_ids,
    ws_book_row,
)
from buy.mint_gas import validate_mint_gas
from buy.mint_redeem import validate_redeem
from buy.mint_sell import ws_book_enabled, ws_book_mode, ws_book_stale_s
from buy.mint_sequence import validate_seq
from test_mint_cpu import _dump_cfg, _dump_harness, _fill_fak, _held_after_scrap, _load
from test_mint_only_ops import MINT_EXAMPLE, _assign, _fn


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

    def test_levels_replace_and_a_zero_size_deletes(self):
        feed, clock = self._feed(["up-tok"])
        feed.apply_event({
            "event_type": "book",
            "asset_id": "up-tok",
            "bids": [
                {"price": "0.39", "size": "10"},
                {"price": "0.30", "size": "8"},
            ],
            "asks": [{"price": "0.60", "size": "12"}],
        })
        top = feed.usable("up-tok")
        self.assertAlmostEqual(top.bid, 0.39)
        self.assertAlmostEqual(top.bid_sz, 10.0)
        row = ws_book_row(top)
        self.assertEqual(
            [(level["price"], level["size"]) for level in row[2]],
            [(0.39, 10.0), (0.30, 8.0)],
        )
        self.assertEqual(row[5][0]["price"], 0.60)
        feed.apply_event({
            "event_type": "best_bid_ask",
            "asset_id": "up-tok",
            "best_bid": "0.20",
            "best_ask": "0.80",
        })
        self.assertAlmostEqual(feed.cached_bid("up-tok"), 0.39)
        self.assertIsNotNone(feed.usable("up-tok"))
        feed.apply_event({
            "event_type": "price_change",
            "price_changes": [{
                "asset_id": "up-tok",
                "price": "0.10",
                "size": "4",
                "side": "BUY",
            }],
        })
        feed.apply_event({
            "event_type": "price_change",
            "asset_id": "up-tok",
            "price": "0.1",
            "size": "0",
            "side": "BID",
        })
        self.assertAlmostEqual(feed.usable("up-tok").bid, 0.39)
        bid_at = feed._books["up-tok"].bid_at
        clock["t"] = 1001.0
        feed.apply_event({
            "event_type": "price_change",
            "asset_id": "up-tok",
            "price": "0.55",
            "size": "6",
            "side": "SELL",
        })
        self.assertEqual(feed._books["up-tok"].bid_at, bid_at)
        self.assertAlmostEqual(feed._books["up-tok"].ask_at, 1001.0)
        clock["t"] = bid_at + 1.6
        self.assertIsNone(feed.usable("up-tok"))
        self.assertGreater(feed._books["up-tok"].ask_at, bid_at)

    def test_ping_does_not_refresh_the_bid(self):
        feed, clock = self._feed(["up-tok"])
        feed.apply_event(_book("up-tok", "0.39", "0.60"))
        bid_at = feed._books["up-tok"].bid_at
        stop = threading.Event()

        class Pong:
            def __init__(self):
                self.sent = []
                self.n = 0

            def send(self, payload):
                self.sent.append(payload)

            def recv(self):
                self.n += 1
                if self.n == 1:
                    clock["t"] = 1000.0 + PING_S
                    return "PONG"
                stop.set()
                raise TimeoutError()

        feed._consume(Pong(), ["up-tok"], stop)
        self.assertEqual(feed._books["up-tok"].bid_at, bid_at)
        self.assertGreater(feed._last_rx, bid_at)

    def test_level_cap_drops_the_worst_prices(self):
        feed, _clock = self._feed(["up-tok"])
        bids = [{"price": f"{(i + 1) / 1000:.3f}", "size": "2"} for i in range(201)]
        feed.apply_event({
            "event_type": "book",
            "asset_id": "up-tok",
            "bids": bids,
            "asks": [{"price": "0.90", "size": "2"}],
        })
        book = feed._books["up-tok"]
        self.assertEqual(len(book.bids), 200)
        self.assertNotIn(0.001, book.bids)
        self.assertIn(0.201, book.bids)
        self.assertAlmostEqual(feed.usable("up-tok").bid, 0.201)

    def test_crossed_book_is_not_usable(self):
        feed, _clock = self._feed(["up-tok"])
        feed.apply_event(_book("up-tok", "0.61", "0.60"))
        self.assertIsNone(feed.usable("up-tok"))
        self.assertAlmostEqual(feed.cached_bid("up-tok"), 0.61)

    def test_ping_interval_is_under_the_silence_timeout(self):
        self.assertEqual(PING_S, 3.0)
        self.assertGreater(SILENCE_S, PING_S)

    def test_stop_resets_started_and_closes_off_the_caller(self):
        feed, _clock = self._feed(["tok"])
        ident = {"close": None}

        class Slow:
            def close(self):
                ident["close"] = threading.get_ident()
                time.sleep(0.3)

        feed._ws = Slow()
        feed._started = True
        started = time.perf_counter()
        feed.stop()
        self.assertLess(time.perf_counter() - started, 0.15)
        self.assertFalse(feed._started)
        deadline = time.perf_counter() + 1.0
        while ident["close"] is None and time.perf_counter() < deadline:
            time.sleep(0.01)
        self.assertNotEqual(ident["close"], threading.get_ident())

        release = threading.Event()

        def block():
            release.wait(2)

        old = threading.Thread(target=block)
        feed._thread = old
        old.start()
        feed.start()
        self.assertTrue(feed._stop.is_set())
        self.assertFalse(feed._started)
        release.set()
        old.join(1)
        opened = threading.Event()

        def connect(_url):
            opened.set()
            raise OSError("down")

        feed._connect = connect
        feed._sleep = lambda _delay: feed._stop.set()
        feed.start()
        self.assertTrue(opened.wait(1))
        self.assertTrue(feed._started)
        feed._thread.join(1)
        feed.stop()

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
        self.assertEqual(defaults["ws_book_mode"], "shadow")
        self.assertEqual(example["ws_book_mode"], "shadow")
        self.assertEqual(ws_book_mode({}), "off")
        self.assertEqual(ws_book_mode({"ws_book_enabled": True}), "shadow")
        self.assertEqual(
            ws_book_mode({"ws_book_enabled": True, "ws_book_mode": "live"}),
            "live",
        )
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

    def test_shadow_keeps_the_rest_price_and_logs_the_lead(self):
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
        ns["_BOOK_FEED_BOX"] = {"feed": feed}
        end = clock["now"] + 200.0
        intent = _held_after_scrap(end, sell_dump_armed_at=clock["now"] - 2.0)
        self._tick(ns, _dump_cfg(ws_book_enabled=True), intent)
        self.assertEqual(fetches["n"], 1)
        self.assertAlmostEqual(fak_calls[0]["price"], 0.51)
        sources = [row for row in events if row["event"] == "book_source"]
        self.assertEqual(sources[0]["path"], "dump")
        self.assertEqual(sources[0]["book_source"], "rest")
        self.assertAlmostEqual(sources[0]["bid_diff"], -0.12)
        self.assertIsNotNone(sources[0]["lead_s"])
        shadow = [row for row in events if row["event"] == "book_shadow"]
        self.assertEqual(len(shadow), 1)
        self.assertIsNotNone(shadow[0]["lead_s"])
        self.assertAlmostEqual(shadow[0]["up_diff"], -0.12)

    def test_live_uses_the_ws_bid_and_logs_rest_on_the_next_tick(self):
        ns, events, fak_calls, clock = _dump_harness(_fill_fak)
        fetches = {"n": 0}
        real = ns["_fetch_books"]

        def counting(*args, **kwargs):
            fetches["n"] += 1
            return real(*args, **kwargs)

        ns["_fetch_books"] = counting
        feed, _mono = FeedTests()._feed(["up-tok", "dn-tok"])
        feed._started = True
        feed.apply_event({
            "event_type": "book",
            "asset_id": "up-tok",
            "bids": [
                {"price": "0.40", "size": "5"},
                {"price": "0.38", "size": "100"},
            ],
            "asks": [{"price": "0.60", "size": "20"}],
        })
        feed.apply_event(_book("dn-tok", "0.49", "0.51"))
        ns["_BOOK_FEED_BOX"] = {"feed": feed}
        end = clock["now"] + 200.0
        intent = _held_after_scrap(end, sell_dump_armed_at=clock["now"] - 2.0)
        cfg = _dump_cfg(ws_book_enabled=True, ws_book_mode="live", sell_armed_poll_s=0.05)
        self._tick(ns, cfg, intent)
        self.assertEqual(fetches["n"], 0)
        self.assertAlmostEqual(fak_calls[0]["price"], 0.40)
        sources = [row for row in events if row["event"] == "book_source"]
        self.assertEqual(sources[0]["book_source"], "ws")
        self.assertIsNone(sources[0]["rest_bid"])
        depth = [row for row in events if row["event"] == "sell_book_depth"]
        ladder = depth[0]["ladder"]
        at_two = [step for step in ladder if abs(step["price"] - 0.38) < 1e-9]
        self.assertEqual(at_two[0]["depth"], 105.0)
        self._tick(ns, cfg, intent)
        self.assertEqual(fetches["n"], 1)
        self.assertEqual(len(fak_calls), 1)
        follow = [row for row in events if row.get("path") == "ws_followup" and row.get("leg") == "up"]
        self.assertTrue(follow)
        self.assertAlmostEqual(follow[0]["ws_bid"], 0.40)
        self.assertAlmostEqual(follow[0]["rest_bid"], 0.51)
        self.assertAlmostEqual(follow[0]["bid_diff"], -0.11)

    def test_live_crossed_or_stale_falls_back_to_rest(self):
        ns, events, fak_calls, clock = _dump_harness(_fill_fak)
        fetches = {"n": 0}
        real = ns["_fetch_books"]

        def counting(*args, **kwargs):
            fetches["n"] += 1
            return real(*args, **kwargs)

        ns["_fetch_books"] = counting
        feed, mono = FeedTests()._feed(["up-tok", "dn-tok"])
        feed._started = True
        feed.apply_event(_book("up-tok", "0.61", "0.40"))
        feed.apply_event(_book("dn-tok", "0.49", "0.51"))
        ns["_BOOK_FEED_BOX"] = {"feed": feed}
        end = clock["now"] + 200.0
        intent = _held_after_scrap(end, sell_dump_armed_at=clock["now"] - 2.0)
        cfg = _dump_cfg(ws_book_enabled=True, ws_book_mode="live")
        self._tick(ns, cfg, intent)
        self.assertEqual(fetches["n"], 1)
        self.assertAlmostEqual(fak_calls[0]["price"], 0.51)
        feed.apply_event(_book("up-tok", "0.39", "0.60"))
        feed.apply_event(_book("dn-tok", "0.49", "0.51"))
        self.assertIsNotNone(feed.usable("up-tok"))
        mono["t"] += 3.0
        self.assertIsNone(feed.usable("up-tok"))

    def test_hot_missing_token_sets_the_spin_floor(self):
        ns, _events, fak_calls, clock = _dump_harness(_fill_fak)
        end = clock["now"] + 200.0
        hot = _held_after_scrap(end, sell_dump_armed_at=clock["now"] - 2.0)
        hot["up_token"] = ""
        self._tick(ns, _dump_cfg(sell_armed_poll_s=0), hot)
        _backoff, _ws, missing = ns["take_book_pace"]()
        self.assertTrue(missing)
        self.assertEqual(fak_calls, [])
        cold = _held_after_scrap(end)
        cold["sold_loser"] = False
        cold["sold_leg"] = None
        cold["up_token"] = ""
        self._tick(ns, _dump_cfg(sell_armed_poll_s=0), cold)
        _backoff, _ws, missing = ns["take_book_pace"]()
        self.assertFalse(missing)

    def test_fail_backoff_grows_on_this_thread_only(self):
        ns = _load(
            "_book_pace",
            "_note_book_fetch_failed",
            "_note_book_fetch_ok",
            "take_book_pace",
        )
        ns["_note_book_fetch_failed"]()
        ns["_note_book_fetch_failed"]()
        seen = {}

        def worker():
            ns["_note_book_fetch_failed"]()
            seen["worker"] = ns["take_book_pace"]()

        thread = threading.Thread(target=worker)
        thread.start()
        thread.join(1)
        self.assertAlmostEqual(seen["worker"][0], 0.25)
        backoff, _ws, _missing = ns["take_book_pace"]()
        self.assertAlmostEqual(backoff, 0.5)
        ns["_note_book_fetch_ok"]()
        ns["_note_book_fetch_failed"]()
        again, _ws, _missing = ns["take_book_pace"]()
        self.assertAlmostEqual(again, 0.25)

    def test_enabled_socket_rejects_a_spin_poll(self):
        validate = _fn(
            "validate_strategy",
            {
                "validate_mint_gas": validate_mint_gas,
                "validate_seq": validate_seq,
                "validate_redeem": validate_redeem,
            },
        )
        base = _assign("DEFAULTS")
        validate(dict(base, sell_armed_poll_s=0))
        with self.assertRaises(ValueError) as caught:
            validate(dict(base, ws_book_enabled=True, sell_armed_poll_s=0))
        self.assertIn(
            "ws_book_enabled requires sell_armed_poll_s >= 0.05",
            str(caught.exception),
        )
        validate(dict(base, ws_book_enabled=True, sell_armed_poll_s=0.05))
        with self.assertRaises(ValueError) as caught:
            validate(dict(base, ws_book_mode="fast"))
        self.assertIn("ws_book_mode must be shadow or live", str(caught.exception))

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
