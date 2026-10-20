"""REST book snapshots for the isolated 5m loop; no background thread."""
from __future__ import annotations

import time
import inspect
from buy.clob_book_ws import Top


class RestBookFeed:
    def __init__(self, fetch_pair, *, clock=time.time, log=None):
        self.fetch_pair = fetch_pair
        try:
            inspect.signature(fetch_pair).bind(None, None, fail={})
        except (TypeError, ValueError):
            self._fetch_accepts_fail = False
        else:
            self._fetch_accepts_fail = True
        self.clock = clock
        self.log = log
        self._status = {}
        self._fail_count = 0
        self._last_fail_log = None
        self._tops = {}
        self._tokens = set()
        self._stale_s = 30.0

    def start(self):
        pass

    def stop(self):
        self._tops.clear()
        self._status.clear()

    def set_tokens(self, tokens):
        self._tokens = set(tokens)
        self._tops = {token: top for token, top in self._tops.items() if token in self._tokens}
        self._status = {token: status for token, status in self._status.items() if token in self._tokens}

    def set_stale_s(self, seconds):
        if float(seconds) > 0:
            self._stale_s = float(seconds)

    def set_min_size(self, size):
        # fetch_pair uses raw top-of-book (min_size=0), matching the strategy.
        pass

    def _fetch_failed(self, now, tokens, error):
        for token in tokens:
            self._status[token] = "fetch_fail"
        self._fail_count += 1
        if self.log and (self._last_fail_log is None or now - self._last_fail_log >= 30):
            self.log("fivemin_book_fetch_fail", count=self._fail_count, error=str(error)[:160])
            self._last_fail_log = now
            self._fail_count = 0

    def poll(self, now, up_tok, dn_tok):
        fail = {}
        try:
            if self._fetch_accepts_fail:
                rows = self.fetch_pair(up_tok, dn_tok, fail=fail)
            else:
                rows = self.fetch_pair(up_tok, dn_tok)
        except Exception as exc:
            self._fetch_failed(self.clock(), (up_tok, dn_tok), str(exc))
            return
        failed = []
        error = "fetch failed"
        for side, token, row in zip(("up", "down"), (up_tok, dn_tok), rows):
            if side in fail or (isinstance(row, dict) and row.get("failed")):
                failed.append(token)
                error = fail.get(side) or (row.get("error") if isinstance(row, dict) else None) or error
                continue
            bid, size, bids, ask, ask_size, asks, age = row
            self._status[token] = "ok" if bid is not None else "empty"
            self._tops[token] = Top(
                bid=bid, bid_sz=float(size or 0), bids=bids,
                ask=ask, ask_sz=float(ask_size or 0), asks=asks,
                updated=now, size_known=bid is not None, age=age,
            )
        if failed:
            self._fetch_failed(self.clock(), failed, error)

    def reason(self, token):
        return self.bid_status(token)

    def bid_status(self, token, now=None) -> str | None:
        now = self.clock() if now is None else now
        top = self._tops.get(token)
        age = None if top is None else now - top.updated
        if (top is not None and 0 <= age <= self._stale_s and top.bid is not None
                and (top.ask is None or top.bid < top.ask - 1e-12)):
            return None
        if self._status.get(token) == "fetch_fail":
            return "fetch_fail"
        if top is None:
            return "no_book"
        if age < 0 or age > self._stale_s:
            return "stale"
        if top.bid is None:
            return "empty"
        if top.ask is not None and top.bid >= top.ask - 1e-12:
            return "crossed"
        return None

    def usable(self, token):
        return self._tops.get(token) if self.reason(token) is None else None
