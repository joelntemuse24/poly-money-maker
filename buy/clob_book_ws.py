"""One CLOB market websocket for the sell loop's current bag and the next.

Same channel the recorder uses
(``wss://ws-subscriptions-clob.polymarket.com/ws/market``). Each token
keeps a bid map and an ask map (at most 200 levels a side). A ``book``
event replaces both maps. A ``price_change`` sets or deletes one level.
The best bid and ask are computed from those maps. The event's
``best_bid`` is ignored. Old bags are dropped. A stale, crossed, or
unhealthy quote is not a book: the sell loop falls back to REST.
"""

from __future__ import annotations

import json
import socket
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from buy.book import (
    best_ask_with_min_size,
    best_bid_with_min_size,
    finite_float,
)


CLOB_MARKET_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
MAX_TOKENS = 4
MAX_LEVELS = 200
PING_S = 3.0
# Silence must sit above the ping interval so one missed PONG is not a drop.
SILENCE_S = 10.0
REST_DIFF_MAX_AGE_S = 30.0
_OPEN_STATUSES = frozenset(
    {"confirmed", "confirmed_waiting_inventory", "mined", "executed"}
)


def backoff_s(attempt: int, *, base: float = 0.5, cap: float = 8.0) -> float:
    """Reconnect delay. Doubles from ``base`` and never exceeds ``cap``."""
    try:
        n = int(attempt)
    except (TypeError, ValueError):
        n = 1
    if n < 1:
        n = 1
    return min(float(cap), float(base) * (2 ** min(n, 8)))


def subscribe_payload(asset_ids: list[str]) -> dict:
    return {
        "assets_ids": list(asset_ids),
        "type": "market",
        "custom_feature_enabled": True,
    }


def watch_token_ids(intents: Any, now: float) -> list[str]:
    """Current open bag, then the next one. At most four token ids."""
    markets: list[tuple[float, float, str, str]] = []
    try:
        now_s = float(now)
    except (TypeError, ValueError):
        now_s = 0.0
    if not isinstance(intents, dict):
        return []
    for intent in intents.values():
        if not isinstance(intent, dict):
            continue
        if intent.get("status") not in _OPEN_STATUSES:
            continue
        try:
            end = float(intent.get("end_ts") or 0)
            start = float(intent.get("start_ts") or 0)
        except (TypeError, ValueError):
            continue
        if end <= now_s:
            continue
        up = str(intent.get("up_token") or "")
        dn = str(intent.get("dn_token") or "")
        if not up or not dn:
            continue
        markets.append((start, end, up, dn))
    markets.sort()
    current = [row for row in markets if row[0] <= now_s]
    upcoming = [row for row in markets if row[0] > now_s]
    chosen: list[tuple[float, float, str, str]] = []
    if current:
        chosen.append(current[-1])
    elif markets:
        chosen.append(markets[0])
    if upcoming:
        chosen.append(upcoming[0])
    out: list[str] = []
    for _start, _end, up, dn in chosen[:2]:
        for token in (up, dn):
            if token not in out:
                out.append(token)
            if len(out) >= MAX_TOKENS:
                return out
    return out


def book_diff_fields(ws_bid: Any, rest_bid: Any) -> dict:
    """``ws_bid``, ``rest_bid``, and ``bid_diff`` (ws − rest) when both exist."""
    ws = finite_float(ws_bid)
    rest = finite_float(rest_bid)
    diff = None
    if ws is not None and rest is not None:
        diff = round(ws - rest, 4)
    return {"ws_bid": ws, "rest_bid": rest, "bid_diff": diff}


def _price_key(value: Any) -> Optional[float]:
    """Six-decimal price so ``0.1`` and ``0.10`` are the same level."""
    parsed = finite_float(value)
    if parsed is None or not 0.0 < parsed < 1.0:
        return None
    return round(parsed, 6)


def _size_value(value: Any) -> Optional[float]:
    parsed = finite_float(value)
    if parsed is None or parsed < 0:
        return None
    return parsed


def _ladder_name(side: Any) -> Optional[str]:
    text = str(side or "").strip().upper()
    if text in {"BUY", "BID"}:
        return "bid"
    if text in {"SELL", "ASK"}:
        return "ask"
    return None


def _cap_levels(levels: dict[float, float], side: str) -> None:
    """Drop the worst prices once a side exceeds ``MAX_LEVELS``."""
    extra = len(levels) - MAX_LEVELS
    if extra <= 0:
        return
    ordered = sorted(levels)
    # Lowest bids and highest asks are the ones a sell does not need.
    drop = ordered[:extra] if side == "bid" else ordered[-extra:]
    for key in drop:
        levels.pop(key, None)


def _pairs_to_map(raw: Any, side: str) -> dict[float, float]:
    levels: dict[float, float] = {}
    if not isinstance(raw, list):
        return levels
    for level in raw:
        if isinstance(level, dict):
            px = _price_key(level.get("price"))
            sz = _size_value(level.get("size"))
        elif isinstance(level, (list, tuple)) and len(level) >= 2:
            px = _price_key(level[0])
            sz = _size_value(level[1])
        else:
            continue
        if px is None or sz is None or sz <= 0:
            continue
        levels[px] = sz
    _cap_levels(levels, side)
    return levels


def _level_rows(levels: dict[float, float], side: str) -> list[dict]:
    ordered = sorted(levels.items(), reverse=(side == "bid"))
    return [{"price": px, "size": sz} for px, sz in ordered]


@dataclass
class Top:
    bid: Optional[float] = None
    bid_sz: float = 0.0
    ask: Optional[float] = None
    ask_sz: float = 0.0
    updated: float = 0.0
    size_known: bool = False
    age: Optional[float] = None
    bids: list = field(default_factory=list)
    asks: list = field(default_factory=list)


@dataclass
class _Book:
    bids: dict[float, float] = field(default_factory=dict)
    asks: dict[float, float] = field(default_factory=dict)
    bid_at: float = 0.0
    ask_at: float = 0.0
    bid_ok: bool = False
    ask_ok: bool = False


def ws_book_row(top: Top) -> tuple:
    """Sell-loop book row shaped like a REST ``_parse_book_payload`` result.

    ``bids`` / ``asks`` are the full maps (bids high to low, asks low to
    high) so ladder depth matches a REST book. The prices are the sized tops.
    """
    bid = top.bid
    bid_sz = float(top.bid_sz or 0.0)
    ask = top.ask
    ask_sz = float(top.ask_sz or 0.0)
    return (bid, bid_sz, list(top.bids), ask, ask_sz, list(top.asks), top.age)


def _is_timeout(exc: BaseException) -> bool:
    name = type(exc).__name__
    return isinstance(exc, (TimeoutError, socket.timeout)) or "Timeout" in name


def _close_socket(ws: Any) -> None:
    try:
        ws.close()
    except Exception:
        return


class ClobBookFeed:
    """Background cache. ``start`` opens one socket; ``stop`` closes it."""

    def __init__(
        self,
        *,
        connect: Optional[Callable[[str], Any]] = None,
        sleep: Optional[Callable[[float], None]] = None,
        clock: Optional[Callable[[], float]] = None,
        on_error: Optional[Callable[[str, float], None]] = None,
        url: str = CLOB_MARKET_URL,
    ) -> None:
        self._connect = connect
        self._sleep = sleep
        self._clock = clock or time.monotonic
        self._on_error = on_error
        self._url = url
        self._lock = threading.Lock()
        self._books: dict[str, _Book] = {}
        self._rest: dict[str, tuple[Optional[float], float]] = {}
        self._wanted: list[str] = []
        self._min_size = 1.0
        self._stale_s = 1.5
        self._healthy = False
        # 0 means no socket timestamp yet. Tests that only set ``_healthy``
        # stay usable; a live socket must have heard something inside SILENCE_S.
        self._last_rx = 0.0
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._started = False
        self._ws: Any = None
        self.reconnects = 0

    def set_stale_s(self, seconds: float) -> None:
        try:
            value = float(seconds)
        except (TypeError, ValueError):
            return
        if value > 0:
            self._stale_s = value

    def set_min_size(self, min_size: float) -> None:
        try:
            value = float(min_size)
        except (TypeError, ValueError):
            return
        if value >= 0:
            self._min_size = value

    def set_tokens(self, token_ids: Any) -> None:
        wanted: list[str] = []
        for token in token_ids or []:
            text = str(token or "")
            if text and text not in wanted:
                wanted.append(text)
            if len(wanted) >= MAX_TOKENS:
                break
        with self._lock:
            if wanted == self._wanted:
                return
            self._wanted = wanted
            keep = set(wanted)
            for key in list(self._books):
                if key not in keep:
                    self._books.pop(key, None)
            for key in list(self._rest):
                if key not in keep:
                    self._rest.pop(key, None)
        self._wake.set()

    def start(self) -> None:
        """Open the socket. A previous ``run`` that is still exiting is left alone.

        Clearing ``_stop`` while that thread is alive would leave two loops
        on one socket. The next call starts again once it has joined.
        """
        thread = self._thread
        if thread is not None and thread.is_alive():
            return
        self._started = True
        self._stop.clear()
        self._thread = threading.Thread(
            target=self.run, name="clob-book-ws", daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        """Mark the feed stopped and close the socket off the caller thread.

        ``_started`` goes false so a later ``start`` can run when the flag
        is turned back on. ``close`` can block, so it runs on a daemon
        thread and cannot stall the sell loop.
        """
        self._stop.set()
        self._wake.set()
        self._healthy = False
        self._started = False
        ws = self._ws
        self._ws = None
        if ws is None:
            return
        threading.Thread(
            target=_close_socket, args=(ws,), name="clob-book-ws-close", daemon=True,
        ).start()

    def healthy(self) -> bool:
        if self._stop.is_set() or not self._healthy:
            return False
        last = float(self._last_rx or 0.0)
        if last <= 0:
            return True
        age = self._clock() - last
        return age >= 0 and age <= SILENCE_S

    def _snapshot(self, token_id: str) -> tuple[Optional[_Book], float, float]:
        with self._lock:
            book = self._books.get(str(token_id))
            return book, self._stale_s, self._min_size

    def _view(self, book: _Book, now_m: float, need: float) -> Top:
        bids = _level_rows(book.bids, "bid")
        asks = _level_rows(book.asks, "ask")
        bid, bid_sz = best_bid_with_min_size(bids, need)
        ask, ask_sz = best_ask_with_min_size(asks, need)
        age = None
        if book.bid_ok:
            age = now_m - float(book.bid_at)
        return Top(
            bid=bid,
            bid_sz=float(bid_sz or 0.0),
            ask=ask,
            ask_sz=float(ask_sz or 0.0),
            updated=float(book.bid_at or 0.0),
            size_known=bid is not None,
            age=age,
            bids=bids,
            asks=asks,
        )

    def usable(self, token_id: str) -> Optional[Top]:
        """Sized top when the bid side is fresh and the book is not crossed.

        A bid is fresh only after a ``book`` event or a bid-side
        ``price_change``. An ask-side change does not refresh it. A ping
        does not either: a ping proves the socket is up, not that the
        level is still there. When ask levels exist, that side must be
        confirmed and inside the same window. ``bid >= ask`` is unusable.
        """
        if not self.healthy():
            return None
        book, stale, need = self._snapshot(token_id)
        if book is None or not book.bid_ok:
            return None
        now_m = self._clock()
        bid_age = now_m - float(book.bid_at)
        if bid_age < 0 or bid_age > stale:
            return None
        if book.asks:
            if not book.ask_ok:
                return None
            ask_age = now_m - float(book.ask_at)
            if ask_age < 0 or ask_age > stale:
                return None
        top = self._view(book, now_m, need)
        if top.bid is None:
            return None
        any_bid, _any_bid_sz = best_bid_with_min_size(top.bids, 0.0)
        any_ask, _any_ask_sz = best_ask_with_min_size(top.asks, 0.0)
        if (
            any_bid is not None
            and any_ask is not None
            and float(any_bid) >= float(any_ask) - 1e-12
        ):
            return None
        return top

    def cached_bid(self, token_id: str) -> Optional[float]:
        """Last websocket bid, including a stale or crossed one, for the diff."""
        book, _stale, _need = self._snapshot(token_id)
        if book is None:
            return None
        rows = _level_rows(book.bids, "bid")
        bid, _sz = best_bid_with_min_size(rows, 0.0)
        return bid

    def rest_bid(self, token_id: str) -> Optional[float]:
        now_m = self._clock()
        with self._lock:
            row = self._rest.get(str(token_id))
        if row is None:
            return None
        bid, seen = row
        if now_m - seen > REST_DIFF_MAX_AGE_S:
            return None
        return bid

    def remember_rest(self, token_id: str, bid: Any) -> None:
        token = str(token_id or "")
        if not token:
            return
        with self._lock:
            if token not in self._wanted and self._wanted:
                return
            px = _price_key(bid) if bid is not None else None
            self._rest[token] = (px, self._clock())
            if len(self._rest) > MAX_TOKENS:
                for key in list(self._rest):
                    if key not in set(self._wanted):
                        self._rest.pop(key, None)

    def apply_event(self, event: dict) -> None:
        if not isinstance(event, dict):
            return
        kind = str(event.get("event_type") or event.get("type") or "")
        now_m = self._clock()
        if kind == "book":
            self._apply_book(event, now_m)
        elif kind == "price_change":
            self._apply_change(event, now_m)
        # best_bid_ask is not applied. The maps are the book.

    def apply_raw(self, raw: Any) -> None:
        if isinstance(raw, (bytes, bytearray)):
            raw = raw.decode("utf-8", "replace")
        text = str(raw or "").strip()
        if not text or text.upper() in {"PING", "PONG"}:
            return
        try:
            payload = json.loads(text)
        except (json.JSONDecodeError, ValueError, TypeError):
            return
        if isinstance(payload, list):
            for item in payload:
                self.apply_event(item)
            return
        self.apply_event(payload)

    def run(self, stop: Optional[threading.Event] = None) -> None:
        """Block until ``stop``. One socket, reconnect with backoff."""
        gate = stop or self._stop
        opener = self._connect or _default_connect
        sleeper = self._sleep or (lambda delay: gate.wait(delay))
        attempt = 0
        while not gate.is_set():
            with self._lock:
                ids = list(self._wanted)
            if not ids:
                self._healthy = False
                self._wake.wait(1.0)
                self._wake.clear()
                continue
            ws = None
            try:
                ws = opener(self._url)
                self._ws = ws
                try:
                    ws.settimeout(1.0)
                except Exception:
                    pass
                ws.send(json.dumps(subscribe_payload(ids), separators=(",", ":")))
                attempt = 0
                self._healthy = True
                why = self._consume(ws, ids, gate)
                if why == "silence":
                    self._note_error("silence", backoff_s(1))
            except Exception as exc:
                attempt += 1
                self.reconnects += 1
                delay = backoff_s(attempt)
                self._note_error(str(exc)[:200], delay)
                sleeper(delay)
            finally:
                self._healthy = False
                held = self._ws
                self._ws = None
                if held is not None and held is not ws:
                    _close_socket(held)
                if ws is not None:
                    _close_socket(ws)
            if gate.is_set():
                return
            if attempt == 0:
                sleeper(0.05)

    def _consume(self, ws: Any, ids: list[str], gate: threading.Event) -> str:
        last_rx = self._clock()
        self._last_rx = last_rx
        last_ping = last_rx
        while not gate.is_set():
            with self._lock:
                if list(self._wanted) != ids:
                    return "resubscribe"
            now_m = self._clock()
            if now_m - last_ping >= PING_S:
                # A ping proves the socket is up. It does not confirm a
                # bid or an ask, so side timestamps stay where the last
                # book or price_change left them.
                ws.send("PING")
                last_ping = now_m
            if now_m - last_rx >= SILENCE_S:
                return "silence"
            try:
                raw = ws.recv()
            except Exception as exc:
                if _is_timeout(exc):
                    continue
                raise
            if raw is None:
                return "closed"
            last_rx = self._clock()
            self._last_rx = last_rx
            text = raw.decode("utf-8", "replace") if isinstance(raw, (bytes, bytearray)) else str(raw)
            if text.strip().upper() == "PING":
                ws.send("PONG")
                continue
            if text.strip().upper() == "PONG":
                continue
            self.apply_raw(raw)
        return "stop"

    def _note_error(self, message: str, delay: float) -> None:
        if self._on_error is None:
            return
        try:
            self._on_error(message, delay)
        except Exception:
            return

    def _book_for(self, asset: str) -> Optional[_Book]:
        with self._lock:
            if asset not in self._wanted:
                return None
            book = self._books.get(asset)
            if book is None:
                book = _Book()
                self._books[asset] = book
            return book

    def _apply_book(self, event: dict, now_m: float) -> None:
        asset = str(event.get("asset_id") or "")
        book = self._book_for(asset)
        if book is None:
            return
        book.bids = _pairs_to_map(event.get("bids") or [], "bid")
        book.asks = _pairs_to_map(event.get("asks") or [], "ask")
        book.bid_at = now_m
        book.ask_at = now_m
        book.bid_ok = True
        book.ask_ok = True

    def _apply_one_change(self, change: dict, now_m: float) -> None:
        asset = str(change.get("asset_id") or "")
        side = _ladder_name(change.get("side"))
        px = _price_key(change.get("price"))
        sz = _size_value(change.get("size"))
        if side is None or px is None or sz is None:
            return
        book = self._book_for(asset)
        if book is None:
            return
        levels = book.bids if side == "bid" else book.asks
        if sz <= 0:
            levels.pop(px, None)
        else:
            levels[px] = sz
            _cap_levels(levels, side)
        if side == "bid":
            book.bid_at = now_m
            book.bid_ok = True
        else:
            book.ask_at = now_m
            book.ask_ok = True

    def _apply_change(self, event: dict, now_m: float) -> None:
        changes = event.get("price_changes") or event.get("changes")
        if isinstance(changes, list):
            for change in changes:
                if not isinstance(change, dict):
                    continue
                merged = dict(event)
                merged.update(change)
                merged.pop("price_changes", None)
                merged.pop("changes", None)
                self._apply_one_change(merged, now_m)
            return
        self._apply_one_change(event, now_m)


def _default_connect(url: str) -> Any:
    import websocket

    return websocket.create_connection(url, timeout=20)
