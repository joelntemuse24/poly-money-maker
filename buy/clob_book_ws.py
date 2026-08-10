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

log = logging.getLogger("clob_book_ws")

CLOB_MARKET_WS = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
PING_INTERVAL_S = 8.0
STALE_S = 2.0
RECONNECT_BASE_S = 0.5
RECONNECT_MAX_S = 8.0

# (bid, bid_size, ask, ask_size, mid)
Quote = Tuple[Optional[float], float, Optional[float], float, Optional[float]]


def _f(v) -> Optional[float]:
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _best_from_levels(levels, side: str) -> Tuple[Optional[float], float]:
    if not levels:
        return None, 0.0
    try:
        if side == "bid":
            best = max(levels, key=lambda x: float(x.get("price", 0) or 0))
        else:
            best = min(levels, key=lambda x: float(x.get("price", 0) or 0))
        return _f(best.get("price")), float(best.get("size", 0) or 0)
    except Exception:
        return None, 0.0


class ClobMarketBookFeed:
    """Background WS cache of best bid/ask per token_id."""

    def __init__(self):
        self._lock = threading.Lock()
        self._quotes: Dict[str, Quote] = {}
        self._updated_at: Dict[str, float] = {}
        self._wanted: Set[str] = set()
        self._subscribed: Set[str] = set()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._started = False
        self._ws = None
        self._want_resub = threading.Event()

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
        self._want_resub.set()

    def quote(self, token_id: str, max_age_s: float = STALE_S) -> Optional[Quote]:
        """Return cached quote if fresh enough; else None."""
        tid = str(token_id)
        with self._lock:
            q = self._quotes.get(tid)
            ts = self._updated_at.get(tid)
        if q is None or ts is None:
            return None
        if (time.time() - ts) > max_age_s:
            return None
        return q

    def quote_age(self, token_id: str) -> Optional[float]:
        tid = str(token_id)
        with self._lock:
            ts = self._updated_at.get(tid)
        if ts is None:
            return None
        return time.time() - ts

    def _store(self, asset_id: str, bid, bid_sz, ask, ask_sz) -> None:
        """Authoritative top-of-book replace. None clears that side — never keep
        a previous bid/ask just because the update omitted it, and never refresh
        the timestamp without applying the new sides (stale-fresh bug).
        """
        if not asset_id:
            return
        mid = None
        if bid is not None and ask is not None:
            mid = (bid + ask) / 2.0
        with self._lock:
            self._quotes[asset_id] = (
                bid,
                float(bid_sz or 0.0) if bid is not None else 0.0,
                ask,
                float(ask_sz or 0.0) if ask is not None else 0.0,
                mid,
            )
            self._updated_at[asset_id] = time.time()

    def _handle_message(self, raw: str) -> None:
        if not raw or raw == "PONG":
            return
        try:
            msg = json.loads(raw)
        except Exception:
            return
        if isinstance(msg, list):
            for item in msg:
                if isinstance(item, dict):
                    self._handle_event(item)
            return
        if isinstance(msg, dict):
            self._handle_event(msg)

    def _handle_event(self, msg: dict) -> None:
        et = msg.get("event_type") or msg.get("type") or ""
        if et == "book":
            asset_id = str(msg.get("asset_id") or "")
            bid, bid_sz = _best_from_levels(msg.get("bids") or [], "bid")
            ask, ask_sz = _best_from_levels(msg.get("asks") or [], "ask")
            self._store(asset_id, bid, bid_sz, ask, ask_sz)
            return
        if et == "best_bid_ask":
            asset_id = str(msg.get("asset_id") or "")
            # Both fields are authoritative; null/missing clears that side.
            self._store(
                asset_id,
                _f(msg.get("best_bid")),
                0.0,
                _f(msg.get("best_ask")),
                0.0,
            )
            return
        if et == "price_change":
            for pc in msg.get("price_changes") or []:
                if not isinstance(pc, dict):
                    continue
                asset_id = str(pc.get("asset_id") or "")
                # price_change includes best_bid/best_ask for the asset — treat
                # as authoritative top-of-book (do not merge with previous).
                if "best_bid" not in pc and "best_ask" not in pc:
                    continue
                self._store(
                    asset_id,
                    _f(pc.get("best_bid")),
                    0.0,
                    _f(pc.get("best_ask")),
                    0.0,
                )
            return

    def _initial_subscribe(self, ws, tokens: Set[str]) -> None:
        if not tokens:
            self._subscribed = set()
            return
        payload = {
            "assets_ids": list(tokens),
            "type": "market",
            "custom_feature_enabled": True,
        }
        ws.send(json.dumps(payload))
        self._subscribed = set(tokens)
        log.info("clob_book_ws initial_subscribe n=%s", len(tokens))

    def _diff_subscribe(self, ws, tokens: Set[str]) -> None:
        """Add/remove assets on a live socket using documented operation field."""
        to_add = tokens - self._subscribed
        to_drop = self._subscribed - tokens
        if to_drop:
            ws.send(json.dumps({
                "assets_ids": list(to_drop),
                "operation": "unsubscribe",
            }))
            log.info("clob_book_ws unsubscribe n=%s", len(to_drop))
        if to_add:
            ws.send(json.dumps({
                "assets_ids": list(to_add),
                "operation": "subscribe",
                "custom_feature_enabled": True,
            }))
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
                last_ping = [0.0]

                def on_open(ws):
                    nonlocal backoff
                    backoff = RECONNECT_BASE_S
                    self._ws = ws
                    with self._lock:
                        tokens = set(self._wanted)
                    self._subscribed = set()
                    self._initial_subscribe(ws, tokens)
                    last_ping[0] = time.time()

                def on_message(ws, message):
                    try:
                        self._handle_message(message)
                    except Exception as e:
                        log.debug("clob_book_ws handle_fail: %s", e)

                def on_error(ws, error):
                    log.warning("clob_book_ws error: %s", error)

                def on_close(ws, status_code, msg):
                    log.info("clob_book_ws closed code=%s msg=%s", status_code, msg)

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
                self._ws = ws

                def runner():
                    ws.run_forever(ping_interval=None, ping_timeout=None)

                t = threading.Thread(target=runner, name="clob-book-ws-io", daemon=True)
                t.start()

                while t.is_alive() and not self._stop.is_set():
                    now = time.time()
                    if self._ws is not None and now - last_ping[0] >= PING_INTERVAL_S:
                        try:
                            self._ws.send("PING")
                        except Exception:
                            break
                        last_ping[0] = now
                    if self._want_resub.is_set():
                        self._want_resub.clear()
                        with self._lock:
                            tokens = set(self._wanted)
                        if tokens != self._subscribed and self._ws is not None:
                            try:
                                self._diff_subscribe(self._ws, tokens)
                            except Exception as e:
                                log.warning("clob_book_ws resub_fail: %s", e)
                                break
                    time.sleep(0.05)

                try:
                    ws.close()
                except Exception:
                    pass
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
