"""Polymarket CLOB market websocket. Books stay in memory for the hot path.

The receive thread only queues frames. A worker applies them. Pings run on
their own thread and never touch a socket that is already gone. Token
subscriptions are the current and next windows; expired ids are removed.
"""

from __future__ import annotations

import json
import queue
import threading
import time
from typing import Any, Callable, Optional

from buy.lock_gates import parse_levels


CLOB_MARKET_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/market"

try:
    import orjson

    def _loads(raw: Any) -> Any:
        if isinstance(raw, str):
            raw = raw.encode("utf-8")
        return orjson.loads(raw)

    def _dumps(payload: Any) -> str:
        return orjson.dumps(payload).decode("utf-8")

    JSON_PARSER = "orjson"
except ImportError:  # pragma: no cover - depends on the environment

    def _loads(raw: Any) -> Any:
        if isinstance(raw, (bytes, bytearray)):
            raw = raw.decode("utf-8", "replace")
        return json.loads(raw)

    def _dumps(payload: Any) -> str:
        return json.dumps(payload, separators=(",", ":"))

    JSON_PARSER = "json"


def _as_text(raw: Any) -> str:
    if isinstance(raw, (bytes, bytearray)):
        return raw.decode("utf-8", "replace")
    return str(raw or "")


def _px(price: Any) -> Optional[float]:
    try:
        value = float(price)
    except (TypeError, ValueError):
        return None
    if not 0 < value < 1:
        return None
    return round(value, 6)


def _size(size: Any) -> Optional[float]:
    try:
        value = float(size)
    except (TypeError, ValueError):
        return None
    return value


def _blank() -> dict:
    return {"asks": {}, "bids": {}, "recv_ts": 0.0}


def _put_level(ladder: dict, price: Any, size: Any) -> None:
    px = _px(price)
    sz = _size(size)
    if px is None or sz is None:
        return
    if sz <= 0:
        ladder.pop(px, None)
    else:
        ladder[px] = sz


def _rows_to_map(rows: Any, side: str) -> dict[float, float]:
    out: dict[float, float] = {}
    for price, size in parse_levels(rows, side):
        if size > 0:
            out[round(float(price), 6)] = float(size)
    return out


def materialize_book(row: dict) -> dict:
    """Sorted ladders. Asks ascend, bids descend. Size 0 is already gone."""
    asks = sorted((p, s) for p, s in (row.get("asks") or {}).items() if s > 0)
    bids = sorted(((p, s) for p, s in (row.get("bids") or {}).items() if s > 0), reverse=True)
    return {"asks": asks, "bids": bids, "recv_ts": float(row.get("recv_ts") or 0.0)}


def decode_payload(raw: Any) -> Any:
    """One websocket frame as a dict or list. Pings and junk are None."""
    if isinstance(raw, (dict, list)):
        return raw
    text = _as_text(raw).strip()
    if not text or text.upper() in {"PING", "PONG"}:
        return None
    try:
        return _loads(text)
    except (json.JSONDecodeError, ValueError, TypeError):
        return None


def merge_book_payload(books: dict[str, dict], payload: Any, *, now: float) -> list[str]:
    """Apply one decoded payload onto price→size maps. Returns tokens touched."""
    events = payload if isinstance(payload, list) else [payload]
    touched: list[str] = []
    stamp = float(now)
    for event in events:
        if not isinstance(event, dict):
            continue
        kind = str(event.get("event_type") or event.get("type") or "")
        if kind == "book":
            token = str(event.get("asset_id") or "")
            if not token:
                continue
            books[token] = {
                "asks": _rows_to_map(event.get("asks"), "ask"),
                "bids": _rows_to_map(event.get("bids"), "bid"),
                "recv_ts": stamp,
            }
            touched.append(token)
            continue
        if kind != "price_change":
            continue
        changes = event.get("price_changes") or event.get("changes") or []
        if not isinstance(changes, list):
            continue
        for change in changes:
            if not isinstance(change, dict):
                continue
            token = str(change.get("asset_id") or event.get("asset_id") or "")
            if not token:
                continue
            book = books.get(token)
            if book is None or not isinstance(book.get("asks"), dict):
                book = _blank()
            side_name = str(change.get("side") or "").upper()
            ladder = "bids" if side_name in {"BUY", "BID"} else "asks"
            _put_level(book[ladder], change.get("price"), change.get("size"))
            book["recv_ts"] = stamp
            books[token] = book
            touched.append(token)
    return touched


def apply_book_message(books: dict[str, dict], raw: Any, *, now: float) -> list[str]:
    """Merge one payload and leave sorted ``(price, size)`` ladders.

    A ``book`` event replaces the ladder. A ``price_change`` updates one
    level (size 0 deletes it). ``BUY`` is the bid, ``SELL`` is the ask.
    """
    payload = decode_payload(raw)
    if payload is None:
        return []
    maps: dict[str, dict] = {}
    for token, row in books.items():
        asks = row.get("asks") if isinstance(row, dict) else None
        bids = row.get("bids") if isinstance(row, dict) else None
        if isinstance(asks, dict) and isinstance(bids, dict):
            maps[token] = {"asks": dict(asks), "bids": dict(bids), "recv_ts": row.get("recv_ts") or 0.0}
        else:
            maps[token] = {
                "asks": {round(float(p), 6): float(s) for p, s in (asks or [])},
                "bids": {round(float(p), 6): float(s) for p, s in (bids or [])},
                "recv_ts": float(row.get("recv_ts") or 0.0) if isinstance(row, dict) else 0.0,
            }
    touched = merge_book_payload(maps, payload, now=now)
    for token in touched:
        books[token] = materialize_book(maps[token])
    return touched


def plan_subscriptions(subscribed: set[str], wanted: list[str]) -> tuple[list[str], list[str]]:
    """Ids to drop and ids to add. Order of ``wanted`` is preserved for adds."""
    want = set(wanted)
    drop = sorted(token for token in subscribed if token not in want)
    add = [token for token in wanted if token not in subscribed]
    return drop, add


def _sock_open(ws: Any) -> bool:
    if ws is None:
        return False
    return getattr(ws, "sock", None) is not None


class ClobBookFeed:
    """One market-channel socket for the tokens ``set_tokens`` currently wants."""

    def __init__(
        self,
        url: str = CLOB_MARKET_URL,
        *,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.url = url
        self._clock = clock
        self._lock = threading.Lock()
        self._io_lock = threading.Lock()
        self._books: dict[str, dict] = {}
        self._views: dict[str, dict] = {}
        self._wanted: list[str] = []
        self._subscribed: set[str] = set()
        self._stop = threading.Event()
        self._reconnect = threading.Event()
        self._threads: list[threading.Thread] = []
        self._ws: Any = None
        self._queue: queue.Queue = queue.Queue(maxsize=512)
        self._error = ""
        self._last_msg = 0.0
        self.connects = 0
        self.reconnects = 0
        self.messages = 0
        self.drops = 0

    def start(self) -> None:
        if any(thread.is_alive() for thread in self._threads):
            return
        self._stop.clear()
        self._threads = [
            threading.Thread(target=self._read_loop, name="lockbot-book", daemon=True),
            threading.Thread(target=self._apply_loop, name="lockbot-book-apply", daemon=True),
            threading.Thread(target=self._ping_loop, name="lockbot-book-ping", daemon=True),
        ]
        for thread in self._threads:
            thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._reconnect.set()
        self._safe_close()

    def set_tokens(self, tokens: list[str]) -> None:
        cleaned: list[str] = []
        for token in tokens:
            text = str(token or "")
            if text and text not in cleaned:
                cleaned.append(text)
        with self._lock:
            self._wanted = cleaned
        self._sync_subscriptions()

    def wanted(self) -> list[str]:
        with self._lock:
            return list(self._wanted)

    def subscribed(self) -> set[str]:
        with self._lock:
            return set(self._subscribed)

    def book(self, token: str) -> Optional[dict]:
        with self._lock:
            cached = self._views.get(str(token))
            if cached is None:
                return None
            return {"asks": list(cached["asks"]), "bids": list(cached["bids"]), "recv_ts": cached["recv_ts"]}

    def last_error(self) -> str:
        with self._lock:
            return self._error

    def age_s(self, now: Optional[float] = None) -> Optional[float]:
        if self._last_msg <= 0:
            return None
        return float(self._clock() if now is None else now) - self._last_msg

    def request_reconnect(self) -> None:
        """Ask the reader to drop the socket. Callers do not close it."""
        self._reconnect.set()

    def stats(self) -> dict:
        with self._lock:
            return {
                "parser": JSON_PARSER,
                "connects": self.connects,
                "reconnects": self.reconnects,
                "messages": self.messages,
                "drops": self.drops,
                "tokens": len(self._wanted),
            }

    def handle_message(self, raw: Any) -> None:
        """Test hook. Production frames go through the apply thread."""
        self._apply_raw(raw, now=float(self._clock()))

    def _apply_raw(self, raw: Any, *, now: float) -> None:
        payload = decode_payload(raw)
        if payload is None:
            return
        with self._lock:
            touched = merge_book_payload(self._books, payload, now=now)
            self._last_msg = now
            self.messages += 1
            self._error = ""
            seen: set[str] = set()
            for token in touched:
                if token in seen:
                    continue
                seen.add(token)
                row = self._books.get(token)
                if row is not None:
                    self._views[token] = materialize_book(row)

    def _sync_subscriptions(self) -> None:
        with self._lock:
            wanted = list(self._wanted)
            subscribed = set(self._subscribed)
        drop, add = plan_subscriptions(subscribed, wanted)
        if not drop and not add:
            return
        if not _sock_open(self._ws):
            return
        if drop:
            if not self._send({"operation": "unsubscribe", "assets_ids": drop}):
                self.request_reconnect()
                return
            with self._lock:
                self._subscribed.difference_update(drop)
        if not add:
            return
        with self._lock:
            still = set(self._subscribed)
        if not still:
            payload = {"type": "market", "assets_ids": wanted}
        else:
            payload = {"operation": "subscribe", "assets_ids": add}
        if not self._send(payload):
            self.request_reconnect()
            return
        with self._lock:
            self._subscribed.update(add)

    def _send(self, payload: dict) -> bool:
        text = _dumps(payload)
        with self._io_lock:
            ws = self._ws
            if not _sock_open(ws):
                return False
            try:
                ws.send(text)
                return True
            except Exception as exc:
                self._note_error(exc)
                return False

    def _safe_close(self) -> None:
        with self._io_lock:
            ws = self._ws
            self._ws = None
            if not _sock_open(ws):
                return
            try:
                ws.close()
            except Exception:
                return

    def _note_error(self, exc: BaseException) -> None:
        with self._lock:
            self._error = str(exc)[:200]

    def _read_loop(self) -> None:
        import websocket

        backoff = 0.5
        retries = 0
        while not self._stop.is_set():
            self._reconnect.clear()
            if retries:
                with self._lock:
                    self.reconnects += 1
            retries += 1
            try:
                ws = websocket.create_connection(self.url, timeout=10)
                with self._io_lock:
                    if self._stop.is_set():
                        try:
                            if _sock_open(ws):
                                ws.close()
                        except Exception:
                            pass
                        return
                    self._ws = ws
                try:
                    ws.settimeout(1.0)
                except Exception:
                    pass
                with self._lock:
                    self._subscribed = set()
                    self.connects += 1
                self._sync_subscriptions()
                backoff = 0.5
                while not self._stop.is_set() and not self._reconnect.is_set():
                    try:
                        message = ws.recv()
                    except websocket.WebSocketTimeoutException:
                        if self._last_msg > 0 and self._clock() - self._last_msg > 15.0:
                            break
                        continue
                    except Exception as exc:
                        self._note_error(exc)
                        break
                    try:
                        self._queue.put_nowait(message)
                    except queue.Full:
                        with self._lock:
                            self.drops += 1
            except Exception as exc:
                self._note_error(exc)
            self._safe_close()
            with self._lock:
                self._subscribed = set()
            if self._stop.is_set():
                return
            self._stop.wait(backoff)
            backoff = min(backoff * 2.0, 15.0)

    def _apply_loop(self) -> None:
        while not self._stop.is_set():
            try:
                raw = self._queue.get(timeout=0.5)
            except queue.Empty:
                continue
            try:
                self._apply_raw(raw, now=float(self._clock()))
            except Exception as exc:
                self._note_error(exc)

    def _ping_loop(self) -> None:
        while not self._stop.wait(10.0):
            with self._io_lock:
                ws = self._ws
                if not _sock_open(ws):
                    continue
                try:
                    ws.ping()
                except Exception as exc:
                    self._note_error(exc)
                    self._reconnect.set()

    def _run(self) -> None:
        """Kept so an older caller that starts ``_run`` still has a target."""
        self._read_loop()
