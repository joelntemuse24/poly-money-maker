"""CLOB market-channel WebSocket: live top-of-book for buy-bot speed path.

Endpoint: wss://ws-subscriptions-clob.polymarket.com/ws/market

Initial subscribe:
  {"assets_ids": [...], "type": "market", "custom_feature_enabled": true}

Dynamic updates (required `operation`):
  {"assets_ids": [...], "operation": "subscribe"|"unsubscribe",
   "custom_feature_enabled": true}

REST remains the fallback when a quote is missing or stale.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from typing import Dict, Iterable, Optional, Set, Tuple

from buy.book import best_from_levels as _best_from_levels
from buy.book import finite_float as _f

log = logging.getLogger("clob_book_ws")

CLOB_MARKET_WS = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
PING_INTERVAL_S = 8.0
STALE_S = 2.0
RECONNECT_BASE_S = 0.5
RECONNECT_MAX_S = 8.0

# (bid, bid_size, ask, ask_size, mid)
Quote = Tuple[Optional[float], float, Optional[float], float, Optional[float]]


def _event_ts_ms(value) -> Optional[int]:
    """Parse an exchange event timestamp without trusting future/invalid values."""
    parsed = _f(value)
    if parsed is None or parsed <= 0:
        return None
    # The market stream normally uses milliseconds, but tolerate seconds.
    if parsed < 10_000_000_000:
        parsed *= 1000
    now_ms = time.time() * 1000
    if parsed > now_ms + 2_000 or now_ms - parsed > 10_000:
        return None
    return int(parsed)


class ClobMarketBookFeed:
    """Background WS cache of best bid/ask per token_id."""

    def __init__(self):
        self._lock = threading.Lock()
        self._quotes: Dict[str, Quote] = {}
        self._updated_at: Dict[str, float] = {}
        self._server_ts_ms: Dict[str, int] = {}
        self._tick_sizes: Dict[str, str] = {}
        self._wanted: Set[str] = set()
        self._subscribed: Set[str] = set()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._started = False
        self._ws = None
        self._want_resub = threading.Event()
        self._generation = 0

    def start(self) -> None:
        if self._started:
            return
        self._started = True
        self._thread = threading.Thread(target=self._run, name="clob-book-ws", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        ws = self._ws
        if ws is not None:
            try:
                ws.close()
            except Exception:
                pass

    def set_tokens(self, token_ids: Iterable[str]) -> None:
        wanted = {str(t) for t in token_ids if t}
        with self._lock:
            if wanted == self._wanted:
                return
            self._wanted = wanted
            for tid in list(self._quotes.keys()):
                if tid not in wanted:
                    self._quotes.pop(tid, None)
                    self._updated_at.pop(tid, None)
                    self._server_ts_ms.pop(tid, None)
                    self._tick_sizes.pop(tid, None)
        self._want_resub.set()

    def quote(self, token_id: str, max_age_s: float = STALE_S) -> Optional[Quote]:
        """Return cached quote if fresh enough; else None."""
        tid = str(token_id)
        with self._lock:
            q = self._quotes.get(tid)
            ts = self._updated_at.get(tid)
        if q is None or ts is None:
            return None
        age = time.monotonic() - ts
        if age < 0 or age > max_age_s:
            return None
        return q

    def quote_age(self, token_id: str) -> Optional[float]:
        tid = str(token_id)
        with self._lock:
            ts = self._updated_at.get(tid)
        if ts is None:
            return None
        age = time.monotonic() - ts
        return age if age >= 0 else None

    def tick_size(self, token_id: str) -> Optional[str]:
        with self._lock:
            return self._tick_sizes.get(str(token_id))

    def _store(
        self,
        asset_id: str,
        bid,
        bid_sz,
        ask,
        ask_sz,
        *,
        preserve_sizes: bool = False,
        generation: Optional[int] = None,
        server_ts_ms: Optional[int] = None,
    ) -> None:
        """Store top-of-book.

        - None bid/ask clears that side (no stale-side retention).
        - Full book snapshots replace sizes.
        - Price-only events (best_bid_ask / price_change) keep prior sizes only
          when the top price is unchanged; a price move zeroes size so callers
          must REST-refresh before sizing an order.
        """
        if not asset_id:
            return
        with self._lock:
            if generation is not None and generation != self._generation:
                return
            if asset_id not in self._wanted:
                return
            previous_server_ts = self._server_ts_ms.get(asset_id)
            if (
                server_ts_ms is not None
                and previous_server_ts is not None
                and server_ts_ms < previous_server_ts
            ):
                return
            prev = self._quotes.get(asset_id)
            if preserve_sizes and prev is not None:
                # Price-only events omit size. Keep prior size ONLY when the
                # top-of-book price is unchanged and the prior update is recent.
                # Otherwise the old level's size is a lie.
                previous_received = self._updated_at.get(asset_id)
                sizes_fresh = (
                    previous_received is not None
                    and 0 <= time.monotonic() - previous_received <= 1.0
                )
                if bid is not None:
                    if bid_sz is None or float(bid_sz or 0) <= 0:
                        if (
                            sizes_fresh
                            and prev[0] is not None
                            and abs(float(prev[0]) - float(bid)) < 1e-12
                        ):
                            bid_sz = prev[1]
                        else:
                            bid_sz = 0.0
                if ask is not None:
                    if ask_sz is None or float(ask_sz or 0) <= 0:
                        if (
                            sizes_fresh
                            and prev[2] is not None
                            and abs(float(prev[2]) - float(ask)) < 1e-12
                        ):
                            ask_sz = prev[3]
                        else:
                            ask_sz = 0.0
            bid = _f(bid)
            ask = _f(ask)
            bid_sz = _f(bid_sz)
            ask_sz = _f(ask_sz)
            if bid is not None and not 0 < bid < 1:
                bid = None
            if ask is not None and not 0 < ask < 1:
                ask = None
            if bid_sz is None or bid_sz <= 0:
                bid_sz = 0.0
            if ask_sz is None or ask_sz <= 0:
                ask_sz = 0.0
            mid = None
            if bid is not None and ask is not None:
                mid = (bid + ask) / 2.0
            self._quotes[asset_id] = (
                bid,
                float(bid_sz or 0.0) if bid is not None else 0.0,
                ask,
                float(ask_sz or 0.0) if ask is not None else 0.0,
                mid,
            )
            self._updated_at[asset_id] = time.monotonic()
            if server_ts_ms is not None:
                self._server_ts_ms[asset_id] = server_ts_ms

    def _handle_message(self, raw: str, generation: Optional[int] = None) -> None:
        if not raw or raw == "PONG":
            return
        try:
            msg = json.loads(raw)
        except Exception:
            return
        if isinstance(msg, list):
            for item in msg:
                if isinstance(item, dict):
                    self._handle_event(item, generation)
            return
        if isinstance(msg, dict):
            self._handle_event(msg, generation)

    def _handle_event(self, msg: dict, generation: Optional[int] = None) -> None:
        et = msg.get("event_type") or msg.get("type") or ""
        server_ts_ms = _event_ts_ms(msg.get("timestamp"))
        if et in {"book", "best_bid_ask", "price_change"} and server_ts_ms is None:
            return
        if et == "book":
            asset_id = str(msg.get("asset_id") or "")
            bid, bid_sz = _best_from_levels(msg.get("bids") or [], "bid")
            ask, ask_sz = _best_from_levels(msg.get("asks") or [], "ask")
            self._store(
                asset_id,
                bid,
                bid_sz,
                ask,
                ask_sz,
                preserve_sizes=False,
                generation=generation,
                server_ts_ms=server_ts_ms,
            )
            return
        if et == "best_bid_ask":
            asset_id = str(msg.get("asset_id") or "")
            # Preserve the omitted side. Price-only events often update one
            # side; clearing the other would fabricate a one-sided book.
            with self._lock:
                prev = self._quotes.get(asset_id)
            bid = msg.get("best_bid") if "best_bid" in msg else (
                prev[0] if prev is not None else None
            )
            ask = msg.get("best_ask") if "best_ask" in msg else (
                prev[2] if prev is not None else None
            )
            self._store(
                asset_id,
                _f(bid) if bid is not None else None,
                None,
                _f(ask) if ask is not None else None,
                None,
                preserve_sizes=True,
                generation=generation,
                server_ts_ms=server_ts_ms,
            )
            return
        if et == "price_change":
            for pc in msg.get("price_changes") or []:
                if not isinstance(pc, dict):
                    continue
                asset_id = str(pc.get("asset_id") or "")
                if "best_bid" not in pc and "best_ask" not in pc:
                    continue
                with self._lock:
                    prev = self._quotes.get(asset_id)
                bid = pc.get("best_bid") if "best_bid" in pc else (
                    prev[0] if prev is not None else None
                )
                ask = pc.get("best_ask") if "best_ask" in pc else (
                    prev[2] if prev is not None else None
                )
                self._store(
                    asset_id,
                    _f(bid) if bid is not None else None,
                    None,
                    _f(ask) if ask is not None else None,
                    None,
                    preserve_sizes=True,
                    generation=generation,
                    server_ts_ms=_event_ts_ms(pc.get("timestamp")) or server_ts_ms,
                )
            return
        if et == "tick_size_change":
            asset_id = str(msg.get("asset_id") or "")
            tick_size = str(msg.get("new_tick_size") or msg.get("tick_size") or "")
            if tick_size not in {"0.1", "0.01", "0.005", "0.0025", "0.001", "0.0001"}:
                return
            with self._lock:
                if (
                    (generation is None or generation == self._generation)
                    and asset_id in self._wanted
                ):
                    self._tick_sizes[asset_id] = tick_size
            return

    def _sock_ready(self, ws) -> bool:
        """True only when WebSocketApp has an open underlying socket."""
        return ws is not None and getattr(ws, "sock", None) is not None

    def _safe_send(self, ws, payload) -> bool:
        if not self._sock_ready(ws):
            return False
        try:
            ws.send(payload)
            return True
        except Exception as e:
            # Common during teardown/reconnect: sock already None.
            log.debug("clob_book_ws send_fail: %s", e)
            return False

    def _initial_subscribe(self, ws, tokens: Set[str]) -> None:
        if not tokens:
            self._subscribed = set()
            return
        payload = {
            "assets_ids": list(tokens),
            "type": "market",
            "custom_feature_enabled": True,
        }
        if not self._safe_send(ws, json.dumps(payload)):
            raise RuntimeError("clob_book_ws initial_subscribe: socket not ready")
        self._subscribed = set(tokens)
        log.info("clob_book_ws initial_subscribe n=%s", len(tokens))

    def _diff_subscribe(self, ws, tokens: Set[str]) -> None:
        """Add/remove assets on a live socket using documented operation field."""
        to_add = tokens - self._subscribed
        to_drop = self._subscribed - tokens
        if to_drop:
            if not self._safe_send(ws, json.dumps({
                "assets_ids": list(to_drop),
                "operation": "unsubscribe",
            })):
                raise RuntimeError("clob_book_ws unsubscribe: socket not ready")
            log.info("clob_book_ws unsubscribe n=%s", len(to_drop))
        if to_add:
            if not self._safe_send(ws, json.dumps({
                "assets_ids": list(to_add),
                "operation": "subscribe",
                "custom_feature_enabled": True,
            })):
                raise RuntimeError("clob_book_ws subscribe: socket not ready")
            log.info("clob_book_ws subscribe n=%s", len(to_add))
        self._subscribed = set(tokens)

    def _run(self) -> None:
        try:
            import websocket  # type: ignore
        except ImportError:
            log.error("websocket-client required for ClobMarketBookFeed")
            return

        backoff = RECONNECT_BASE_S
        while not self._stop.is_set():
            try:
                with self._lock:
                    self._generation += 1
                    generation = self._generation
                    # Quotes from a dead socket are not valid on its replacement.
                    self._quotes.clear()
                    self._updated_at.clear()
                    self._server_ts_ms.clear()
                    self._tick_sizes.clear()
                last_ping = [0.0]

                def on_open(ws):
                    nonlocal backoff
                    backoff = RECONNECT_BASE_S
                    self._ws = ws
                    with self._lock:
                        tokens = set(self._wanted)
                    self._subscribed = set()
                    self._initial_subscribe(ws, tokens)
                    last_ping[0] = time.monotonic()

                def on_message(ws, message):
                    try:
                        self._handle_message(message, generation)
                    except Exception as e:
                        log.debug("clob_book_ws handle_fail: %s", e)

                def on_error(ws, error):
                    # websocket-client often reports AttributeError on sock during
                    # reconnect races; demote that noise and force a clean loop.
                    msg = str(error)
                    if "sock" in msg and "NoneType" in msg:
                        log.debug("clob_book_ws reconnect_race: %s", error)
                    else:
                        log.warning("clob_book_ws error: %s", error)

                def on_close(ws, status_code, msg):
                    log.info("clob_book_ws closed code=%s msg=%s", status_code, msg)
                    if self._ws is ws:
                        self._ws = None

                def on_pong(ws, message):
                    pass

                ws = websocket.WebSocketApp(
                    CLOB_MARKET_WS,
                    on_open=on_open,
                    on_message=on_message,
                    on_error=on_error,
                    on_close=on_close,
                    on_pong=on_pong,
                )
                # Do not publish self._ws until on_open — App exists before sock.

                def runner():
                    ws.run_forever(ping_interval=None, ping_timeout=None)

                t = threading.Thread(target=runner, name="clob-book-ws-io", daemon=True)
                t.start()

                while t.is_alive() and not self._stop.is_set():
                    now = time.monotonic()
                    live = self._ws if self._sock_ready(self._ws) else None
                    if live is not None and now - last_ping[0] >= PING_INTERVAL_S:
                        if not self._safe_send(live, "PING"):
                            break
                        last_ping[0] = now
                    if self._want_resub.is_set():
                        self._want_resub.clear()
                        with self._lock:
                            tokens = set(self._wanted)
                        live = self._ws if self._sock_ready(self._ws) else None
                        if live is not None and tokens != self._subscribed:
                            try:
                                self._diff_subscribe(live, tokens)
                            except Exception as e:
                                log.warning("clob_book_ws resub_fail: %s", e)
                                break
                    time.sleep(0.05)

                try:
                    if self._sock_ready(ws):
                        ws.close()
                except Exception:
                    pass
                if self._ws is ws:
                    self._ws = None
                t.join(timeout=2)
            except Exception as e:
                log.warning("clob_book_ws loop_fail: %s", e)

            if self._stop.is_set():
                break
            time.sleep(backoff)
            backoff = min(RECONNECT_MAX_S, backoff * 2)


_FEED: Optional[ClobMarketBookFeed] = None
_FEED_LOCK = threading.Lock()


def get_book_feed() -> ClobMarketBookFeed:
    global _FEED
    with _FEED_LOCK:
        if _FEED is None:
            _FEED = ClobMarketBookFeed()
            _FEED.start()
        return _FEED
