"""One CLOB market websocket for the sell loop's current bag and the next.

Same channel the recorder uses
(``wss://ws-subscriptions-clob.polymarket.com/ws/market``). The cache keeps
only the best bid and ask for at most four tokens. Old bags are dropped.
A stale or unhealthy quote is not a book: the sell loop falls back to REST.
"""

from __future__ import annotations

import json
import socket
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, Optional

from buy.book import (
    best_ask_with_min_size,
    best_bid_with_min_size,
    best_from_levels,
    finite_float,
)


CLOB_MARKET_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
MAX_TOKENS = 4
PING_S = 10.0
SILENCE_S = 5.0
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


@dataclass
class Top:
    bid: Optional[float] = None
    bid_sz: float = 0.0
    ask: Optional[float] = None
    ask_sz: float = 0.0
    updated: float = 0.0
    size_known: bool = False
    age: Optional[float] = None


def ws_book_row(top: Top) -> tuple:
    """One sell-loop book row shaped like a REST ``_parse_book_payload`` result."""
    bid = top.bid
    bid_sz = float(top.bid_sz or 0.0)
    ask = top.ask
    ask_sz = float(top.ask_sz or 0.0)
    bids = [{"price": bid, "size": bid_sz}] if bid is not None and bid_sz > 0 else []
    asks = [{"price": ask, "size": ask_sz}] if ask is not None and ask_sz > 0 else []
    return (bid, bid_sz, bids, ask, ask_sz, asks, top.age)


def _price(value: Any) -> Optional[float]:
    parsed = finite_float(value)
    if parsed is None or not 0.0 < parsed < 1.0:
        return None
    return parsed


def _is_timeout(exc: BaseException) -> bool:
    name = type(exc).__name__
    return isinstance(exc, (TimeoutError, socket.timeout)) or "Timeout" in name


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
        self._tops: dict[str, Top] = {}
        self._rest: dict[str, tuple[Optional[float], float]] = {}
        self._wanted: list[str] = []
        self._min_size = 1.0
        self._stale_s = 1.5
        self._healthy = False
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
            for key in list(self._tops):
                if key not in keep:
                    self._tops.pop(key, None)
            for key in list(self._rest):
                if key not in keep:
                    self._rest.pop(key, None)
        self._wake.set()

    def start(self) -> None:
        if self._started:
            return
        self._started = True
        self._stop.clear()
        self._thread = threading.Thread(
            target=self.run, name="clob-book-ws", daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        self._healthy = False
        ws = self._ws
        self._ws = None
        if ws is not None:
            try:
                ws.close()
            except Exception:
                return

    def healthy(self) -> bool:
        return bool(self._healthy) and not self._stop.is_set()

    def usable(self, token_id: str) -> Optional[Top]:
        """Sized top when the socket is up and the update is inside the stale window."""
        if not self.healthy():
            return None
        now_m = self._clock()
        with self._lock:
            top = self._tops.get(str(token_id))
            stale = self._stale_s
            need = self._min_size
        if top is None or not top.size_known or top.bid is None:
            return None
        age = now_m - float(top.updated)
        if age < 0 or age > stale:
            return None
        if float(top.bid_sz) + 1e-12 < float(need):
            return None
        return Top(
            bid=top.bid,
            bid_sz=top.bid_sz,
            ask=top.ask,
            ask_sz=top.ask_sz,
            updated=top.updated,
            size_known=True,
            age=age,
        )

    def cached_bid(self, token_id: str) -> Optional[float]:
        """Last websocket bid, including a stale one, for the trigger diff."""
        with self._lock:
            top = self._tops.get(str(token_id))
        if top is None:
            return None
        return top.bid

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
            self._rest[token] = (_price(bid) if bid is not None else None, self._clock())
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
        elif kind == "best_bid_ask":
            self._apply_bba(event, now_m)
        elif kind == "price_change":
            self._apply_change(event, now_m)

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
                self._ws = None
                if ws is not None:
                    try:
                        ws.close()
                    except Exception:
                        pass
            if gate.is_set():
                return
            if attempt == 0:
                sleeper(0.05)

    def _consume(self, ws: Any, ids: list[str], gate: threading.Event) -> str:
        last_rx = self._clock()
        last_ping = last_rx
        while not gate.is_set():
            with self._lock:
                if list(self._wanted) != ids:
                    return "resubscribe"
            now_m = self._clock()
            if now_m - last_ping >= PING_S:
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
            text = raw.decode("utf-8", "replace") if isinstance(raw, (bytes, bytearray)) else str(raw)
            if text.strip().upper() == "PING":
                ws.send("PONG")
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

    def _wanted_has(self, asset: str) -> bool:
        with self._lock:
            return bool(asset) and asset in self._wanted

    def _put(self, asset: str, top: Top) -> None:
        with self._lock:
            if asset not in self._wanted:
                return
            self._tops[asset] = top
            if len(self._tops) > MAX_TOKENS:
                for key in list(self._tops):
                    if key not in self._wanted:
                        self._tops.pop(key, None)

    def _prev(self, asset: str) -> Optional[Top]:
        with self._lock:
            return self._tops.get(asset)

    def _apply_book(self, event: dict, now_m: float) -> None:
        asset = str(event.get("asset_id") or "")
        if not self._wanted_has(asset):
            return
        need = self._min_size
        bid, bid_sz = best_bid_with_min_size(event.get("bids") or [], need)
        ask, ask_sz = best_ask_with_min_size(event.get("asks") or [], need)
        known = bid is not None
        if bid is None:
            bid, bid_sz = best_from_levels(event.get("bids") or [], "bid")
            known = False
        if ask is None:
            ask, ask_sz = best_from_levels(event.get("asks") or [], "ask")
        self._put(asset, Top(
            bid=bid, bid_sz=float(bid_sz or 0.0),
            ask=ask, ask_sz=float(ask_sz or 0.0),
            updated=now_m, size_known=known,
        ))

    def _apply_bba(self, event: dict, now_m: float) -> None:
        asset = str(event.get("asset_id") or "")
        if not self._wanted_has(asset):
            return
        prev = self._prev(asset)
        bid = _price(event.get("best_bid")) if "best_bid" in event else (prev.bid if prev else None)
        ask = _price(event.get("best_ask")) if "best_ask" in event else (prev.ask if prev else None)
        bid_sz = finite_float(event.get("best_bid_size"))
        ask_sz = finite_float(event.get("best_ask_size"))
        known = True
        if bid_sz is None:
            if prev is not None and prev.bid is not None and bid is not None and abs(prev.bid - bid) < 1e-12 and prev.size_known:
                bid_sz = prev.bid_sz
            else:
                bid_sz = 0.0
                known = False
        if ask_sz is None and prev is not None and prev.ask is not None and ask is not None and abs(prev.ask - ask) < 1e-12:
            ask_sz = prev.ask_sz
        self._put(asset, Top(
            bid=bid, bid_sz=float(bid_sz or 0.0),
            ask=ask, ask_sz=float(ask_sz or 0.0),
            updated=now_m, size_known=known and bid is not None,
        ))

    def _apply_change(self, event: dict, now_m: float) -> None:
        changes = event.get("price_changes") or event.get("changes") or []
        if not isinstance(changes, list):
            return
        for change in changes:
            if isinstance(change, dict):
                self._apply_bba({**event, **change, "event_type": "best_bid_ask"}, now_m)


def _default_connect(url: str) -> Any:
    import websocket

    return websocket.create_connection(url, timeout=20)
