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

    def source(self, token) -> str:
        return "rest"

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


class WsFallbackBookFeed:
    """Prefer fresh websocket tops; poll REST only for stale, missing or
    disconnected ws data.

    A fresh empty ws book is a valid empty reading: the market simply
    has no bid, so it stays an ``empty`` miss reason downstream instead
    of triggering a REST refetch. Feed health is logged as
    ``fivemin_ws_feed_stale`` (whole feed stale/disconnected) or
    ``fivemin_book_quiet_token`` (feed alive, one token without recent
    updates), and ``fivemin_ws_connect`` / ``fivemin_ws_disconnect``
    carry the down duration.
    """

    def __init__(self, ws, rest, *, ws_stale_s=lambda: 1.0, clock=time.time, log=None):
        self.ws, self.rest = ws, rest
        self.ws_stale_s = ws_stale_s
        self.clock, self.log = clock, log
        self._healthy = None
        self._fallback = None
        self._down_since = None
        self._last_log = {}

    def start(self):
        self.ws.start()
        self.rest.start()

    def stop(self):
        self.ws.stop()
        self.rest.stop()

    def set_tokens(self, tokens):
        tokens = list(tokens)
        self.ws.set_stale_s(self.ws_stale_s())
        self.ws.set_tokens(tokens)
        self.rest.set_tokens(tokens)

    def set_stale_s(self, seconds):
        self.rest.set_stale_s(seconds)

    def set_min_size(self, size):
        self.ws.set_min_size(size)
        self.rest.set_min_size(size)

    def _event(self, event, key=None, **fields):
        now = self.clock()
        name = event if key is None else (event, key)
        if self.log and now - self._last_log.get(name, float("-inf")) >= 30:
            self.log(event, **fields)
            self._last_log[name] = now

    def _fresh_empty_ws(self, token):
        probe = getattr(self.ws, "fresh_empty", None)
        if not callable(probe):
            return False
        try:
            return probe(token) is True
        except Exception:
            return False

    def _ws_silence_ms(self):
        probe = getattr(self.ws, "silence_s", None)
        value = None
        if callable(probe):
            try:
                value = probe()
            except Exception:
                return None
        if isinstance(value, (int, float)) and value >= 0:
            return round(float(value) * 1000.0, 1)
        return None

    def _ws_health(self, token):
        state = getattr(self.ws, "token_health", None)
        if callable(state):
            try:
                state = state(token)
                if isinstance(state, dict):
                    return state
            except Exception:
                pass
        return {}

    def source(self, token) -> str:
        return "ws" if self.ws.usable(token) is not None else "rest"

    def poll(self, now, up_tok, dn_tok):
        self.ws.set_stale_s(self.ws_stale_s())
        healthy = bool(self.ws.healthy())
        if healthy != self._healthy:
            down_ms = None
            if not healthy:
                self._down_since = now
            elif self._down_since is not None:
                down_ms = round(max(0.0, now - self._down_since) * 1000.0, 1)
            self._healthy = healthy
            if healthy:
                self._event("fivemin_ws_connect", ws_down_ms=down_ms)
            else:
                self._event("fivemin_ws_disconnect", ws_down_ms=0.0)
        missing = [tok for tok in (up_tok, dn_tok)
                   if self.ws.usable(tok) is None and not self._fresh_empty_ws(tok)]
        fallback = bool(missing)
        if fallback:
            if not self._fallback:
                if not healthy:
                    # Whole feed stale or disconnected: one event, not per token.
                    self._event("fivemin_ws_feed_stale",
                                last_ws_msg_age_ms=self._ws_silence_ms())
                else:
                    for token in missing:
                        health = self._ws_health(token)
                        self._event("fivemin_book_quiet_token", key=token, token=token,
                                    last_token_msg_age_ms=health.get("last_ws_msg_age_ms"),
                                    ws_book_empty=bool(health.get("ws_book_empty")))
            self.rest.poll(now, up_tok, dn_tok)
        self._fallback = fallback

    def usable(self, token):
        top = self.ws.usable(token)
        if top is not None:
            return top
        if self._fresh_empty_ws(token):
            # The ws book is the live truth: a fresh empty book means no bid,
            # so a stale REST top must not resurrect one.
            return None
        return self.rest.usable(token)

    def reason(self, token):
        if self.ws.usable(token) is not None:
            return None
        if self._fresh_empty_ws(token):
            return "empty"
        return self.rest.reason(token)
