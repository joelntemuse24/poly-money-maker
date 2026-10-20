"""REST book snapshots for the isolated 5m loop; no background thread."""
from __future__ import annotations

import time
from buy.clob_book_ws import Top


class RestBookFeed:
    def __init__(self, fetch_pair, *, clock=time.time):
        self.fetch_pair = fetch_pair
        self.clock = clock
        self._tops = {}
        self._tokens = set()
        self._stale_s = 30.0

    def start(self):
        pass

    def stop(self):
        self._tops.clear()

    def set_tokens(self, tokens):
        self._tokens = set(tokens)
        self._tops = {token: top for token, top in self._tops.items() if token in self._tokens}

    def set_stale_s(self, seconds):
        if float(seconds) > 0:
            self._stale_s = float(seconds)

    def set_min_size(self, size):
        # fetch_pair uses raw top-of-book (min_size=0), matching the strategy.
        pass

    def poll(self, now, up_tok, dn_tok):
        rows = self.fetch_pair(up_tok, dn_tok)
        for token, row in zip((up_tok, dn_tok), rows):
            bid, size, bids, ask, ask_size, asks, age = row
            self._tops[token] = Top(
                bid=bid, bid_sz=float(size or 0), bids=bids,
                ask=ask, ask_sz=float(ask_size or 0), asks=asks,
                updated=now, size_known=bid is not None, age=age,
            )

    def usable(self, token):
        top = self._tops.get(token)
        if top is None or top.bid is None:
            return None
        age = self.clock() - top.updated
        if age < 0 or age > self._stale_s:
            return None
        if top.ask is not None and top.bid >= top.ask - 1e-12:
            return None
        return top
