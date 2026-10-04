"""Polymarket CLOB market websocket. Books stay in memory for the hot path."""

from __future__ import annotations

import json
import threading
import time
from typing import Any, Callable, Optional

from buy.lock_gates import parse_levels


CLOB_MARKET_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/market"


def _as_text(raw: Any) -> str:
    if isinstance(raw, (bytes, bytearray)):
        return raw.decode("utf-8", "replace")
    return str(raw or "")


def _levels_from_rows(rows: Any, side: str) -> list[tuple[float, float]]:
    return parse_levels(rows, side)


def apply_book_message(books: dict[str, dict], raw: Any, *, now: float) -> list[str]:
    """Merge one websocket payload into ``books``. Returns the token ids touched.

    A ``book`` event replaces the ladder. A ``price_change`` updates one
    level (size 0 deletes it). ``BUY`` is the bid, ``SELL`` is the ask.
    """
    if isinstance(raw, dict):
        payload = raw
    elif isinstance(raw, list):
        payload = raw
    else:
        text = _as_text(raw).strip()
        if not text or text.upper() in {"PING", "PONG"}:
            return []
        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            return []
    events = payload if isinstance(payload, list) else [payload]
    touched: list[str] = []
    for event in events:
        if not isinstance(event, dict):
            continue
        kind = str(event.get("event_type") or event.get("type") or "")
        if kind == "book":
            token = str(event.get("asset_id") or "")
            if not token:
                continue
            books[token] = {
                "asks": _levels_from_rows(event.get("asks"), "ask"),
                "bids": _levels_from_rows(event.get("bids"), "bid"),
                "recv_ts": float(now),
            }
            touched.append(token)
            continue
        if kind == "price_change":
            changes = event.get("price_changes") or event.get("changes") or []
            if not isinstance(changes, list):
                continue
            for change in changes:
                if not isinstance(change, dict):
                    continue
                token = str(change.get("asset_id") or event.get("asset_id") or "")
                if not token:
                    continue
                book = books.get(token) or {"asks": [], "bids": [], "recv_ts": float(now)}
                side_name = str(change.get("side") or "").upper()
                ladder = "bids" if side_name in {"BUY", "BID"} else "asks"
                _upsert(book, ladder, change.get("price"), change.get("size"))
                book["recv_ts"] = float(now)
                books[token] = book
                touched.append(token)
    return touched


def _upsert(book: dict, ladder: str, price: Any, size: Any) -> None:
    try:
        px = float(price)
        sz = float(size)
    except (TypeError, ValueError):
        return
    if not 0 < px < 1:
        return
    rows = [(p, s) for p, s in book.get(ladder) or [] if abs(p - px) > 1e-9]
    if sz > 0:
        rows.append((px, sz))
    rows.sort(key=lambda item: item[0], reverse=(ladder == "bids"))
    book[ladder] = rows


class ClobBookFeed:
    """One market-channel socket. ``set_tokens`` subscribes current and next windows."""

    def __init__(
        self,
        url: str = CLOB_MARKET_URL,
        *,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.url = url
        self._clock = clock
        self._lock = threading.Lock()
        self._books: dict[str, dict] = {}
        self._wanted: list[str] = []
        self._sent: set[str] = set()
        self._pending: list[str] = []
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._ws: Any = None
        self._error = ""
        self._last_msg = 0.0

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="lockbot-book", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        ws = self._ws
        if ws is not None:
            try:
                ws.close()
            except Exception:
                return

    def set_tokens(self, tokens: list[str]) -> None:
        cleaned: list[str] = []
        for token in tokens:
            text = str(token or "")
            if text and text not in cleaned:
                cleaned.append(text)
        with self._lock:
            self._wanted = cleaned
            for token in cleaned:
                if token not in self._sent and token not in self._pending:
                    self._pending.append(token)
        self._flush_subscribe()

    def book(self, token: str) -> Optional[dict]:
        with self._lock:
            row = self._books.get(str(token))
            if row is None:
                return None
            return {"asks": list(row["asks"]), "bids": list(row["bids"]), "recv_ts": row["recv_ts"]}

    def last_error(self) -> str:
        with self._lock:
            return self._error

    def age_s(self, now: Optional[float] = None) -> Optional[float]:
        if self._last_msg <= 0:
            return None
        return float(self._clock() if now is None else now) - self._last_msg

    def handle_message(self, raw: Any) -> None:
        now = float(self._clock())
        with self._lock:
            apply_book_message(self._books, raw, now=now)
            self._last_msg = now
            self._error = ""

    def _flush_subscribe(self) -> None:
        ws = self._ws
        if ws is None:
            return
        with self._lock:
            pending = list(self._pending)
            self._pending.clear()
            wanted = list(self._wanted)
        if not pending:
            return
        payload: dict[str, Any]
        if not self._sent:
            payload = {"type": "market", "assets_ids": wanted}
        else:
            payload = {"operation": "subscribe", "assets_ids": pending}
        try:
            ws.send(json.dumps(payload))
        except Exception as exc:
            with self._lock:
                self._error = str(exc)[:200]
                self._pending = pending + self._pending
            return
        with self._lock:
            self._sent.update(pending)

    def _run(self) -> None:
        import websocket

        while not self._stop.is_set():
            try:
                with self._lock:
                    self._sent = set()
                app = websocket.WebSocketApp(
                    self.url,
                    on_open=self._on_open,
                    on_message=lambda _ws, message: self.handle_message(message),
                    on_error=lambda _ws, err: self._set_error(str(err)[:200]),
                )
                self._ws = app
                app.run_forever(ping_interval=10, ping_timeout=5)
            except Exception as exc:
                self._set_error(str(exc)[:200])
            self._ws = None
            if self._stop.is_set():
                return
            self._stop.wait(1.0)

    def _on_open(self, ws: Any) -> None:
        self._ws = ws
        with self._lock:
            self._sent = set()
            self._pending = list(self._wanted)
        self._flush_subscribe()

    def _set_error(self, message: str) -> None:
        with self._lock:
            self._error = message
