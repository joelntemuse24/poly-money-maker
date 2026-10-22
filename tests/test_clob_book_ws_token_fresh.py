"""``token_msg_fresh`` mode: the last token message drives book freshness."""

from __future__ import annotations

import threading
import unittest

from buy.clob_book_ws import PING_S, ClobBookFeed


def _book(asset: str, bid: str = "0.39", ask: str = "0.60", size: str = "20") -> dict:
    bids = [] if bid is None else [{"price": bid, "size": size}]
    asks = [] if ask is None else [{"price": ask, "size": size}]
    return {"event_type": "book", "asset_id": asset, "bids": bids, "asks": asks}


def _ask_change(asset: str) -> dict:
    return {
        "event_type": "price_change", "asset_id": asset,
        "side": "SELL", "price": "0.55", "size": "6",
    }


class TokenMsgFreshTests(unittest.TestCase):
    def _feed(self, *, token_msg_fresh: bool, stale_s: float = 1.0):
        clock = {"t": 1000.0}
        feed = ClobBookFeed(clock=lambda: clock["t"], token_msg_fresh=token_msg_fresh)
        feed.set_min_size(1.0)
        feed.set_stale_s(stale_s)
        feed.set_tokens(["up-tok"])
        feed._healthy = True
        return feed, clock

    def test_ask_side_changes_keep_the_book_usable(self):
        feed, clock = self._feed(token_msg_fresh=True)
        feed.apply_event(_book("up-tok"))
        for _i in range(6):
            clock["t"] += 0.5
            feed.apply_event(_ask_change("up-tok"))
        clock["t"] += 0.1
        top = feed.usable("up-tok")
        self.assertIsNotNone(top)
        self.assertAlmostEqual(top.bid, 0.39)
        self.assertAlmostEqual(top.ask, 0.55)
        health = feed.token_health("up-tok")
        self.assertAlmostEqual(health["last_ws_msg_age_ms"], 100.0)
        self.assertFalse(health["ws_book_empty"])

    def test_no_message_past_stale_is_none(self):
        feed, clock = self._feed(token_msg_fresh=True)
        feed.apply_event(_book("up-tok"))
        clock["t"] += 1.0
        self.assertIsNotNone(feed.usable("up-tok"))
        clock["t"] += 1.2
        self.assertIsNone(feed.usable("up-tok"))
        self.assertAlmostEqual(feed.cached_bid("up-tok"), 0.39)
        self.assertFalse(feed.fresh_empty("up-tok"))

    def test_ping_only_refresh_is_stale(self):
        feed, clock = self._feed(token_msg_fresh=True)
        feed.apply_event(_book("up-tok"))
        msg_at = feed._books["up-tok"].msg_at
        clock["t"] += 1.2
        stop = threading.Event()

        class Pong:
            def __init__(self) -> None:
                self.sent: list = []
                self.n = 0

            def send(self, payload: str) -> None:
                self.sent.append(payload)

            def recv(self):
                self.n += 1
                if self.n == 1:
                    clock["t"] = 1000.0 + PING_S
                    return "PONG"
                stop.set()
                raise TimeoutError()

        feed._consume(Pong(), ["up-tok"], stop)
        self.assertTrue(feed.healthy())
        self.assertGreater(feed._last_rx, msg_at)
        self.assertEqual(feed._books["up-tok"].msg_at, msg_at)
        self.assertIsNone(feed.usable("up-tok"))

    def test_bid_side_removal_stamps_the_message_time(self):
        feed, clock = self._feed(token_msg_fresh=True)
        feed.apply_event(_book("up-tok"))
        clock["t"] += 0.3
        feed.apply_event({
            "event_type": "price_change", "asset_id": "up-tok",
            "side": "BUY", "price": "0.35", "size": "0",
        })
        self.assertAlmostEqual(feed._books["up-tok"].msg_at, 1000.3)

    def test_disconnected_is_none(self):
        feed, _clock = self._feed(token_msg_fresh=True)
        feed.apply_event(_book("up-tok"))
        feed._healthy = False
        self.assertIsNone(feed.usable("up-tok"))
        self.assertFalse(feed.fresh_empty("up-tok"))

    def test_crossed_book_is_still_none(self):
        feed, _clock = self._feed(token_msg_fresh=True)
        feed.apply_event(_book("up-tok", bid="0.61", ask="0.60"))
        self.assertIsNone(feed.usable("up-tok"))
        self.assertAlmostEqual(feed.cached_bid("up-tok"), 0.61)

    def test_fresh_empty_bid_side_is_fresh_empty(self):
        feed, clock = self._feed(token_msg_fresh=True)
        feed.apply_event(_book("up-tok", bid=None))
        self.assertTrue(feed.fresh_empty("up-tok"))
        self.assertIsNone(feed.usable("up-tok"))
        clock["t"] += 2.0
        feed.apply_event(_ask_change("up-tok"))
        self.assertTrue(feed.fresh_empty("up-tok"))

    def test_default_mode_keeps_bid_side_freshness(self):
        feed, clock = self._feed(token_msg_fresh=False)
        self.assertFalse(feed._token_msg_fresh)
        feed.apply_event(_book("up-tok"))
        clock["t"] += 0.5
        feed.apply_event(_ask_change("up-tok"))
        clock["t"] += 0.1
        self.assertIsNotNone(feed.usable("up-tok"))
        clock["t"] += 1.0
        self.assertIsNone(feed.usable("up-tok"))


if __name__ == "__main__":
    unittest.main()
