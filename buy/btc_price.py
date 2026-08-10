"""BTC underlying price helpers for Up/Down buy gates.

Polymarket BTC Up/Down markets resolve on Chainlink BTC/USD TWAP vs the
window's Price To Beat (PTB = BTC at event start). Crypto PTB is not in the
public REST API, so we approximate:

- Live: Polymarket RTDS `crypto_prices_chainlink` (btc/usd), with Coinbase
  spot as fallback.
- PTB: Coinbase 1-minute candle open nearest to market `start_ts` (same
  ballpark as Chainlink for a $15+ edge gate).

This is intentionally a coarse filter: if spot is not clearly above/below
PTB, do not buy a 96–99¢ ask just because the CLOB looks decided.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from datetime import datetime, timezone
from typing import Optional, Tuple

import requests

log = logging.getLogger("btc_price")

RTDS_URL = "wss://ws-live-data.polymarket.com"
COINBASE_SPOT_URL = "https://api.coinbase.com/v2/prices/BTC-USD/spot"
COINBASE_CANDLES_URL = "https://api.exchange.coinbase.com/products/BTC-USD/candles"
KRAKEN_TICKER_URL = "https://api.kraken.com/0/public/Ticker"

LIVE_STALE_S = 30.0
PTB_CACHE_MAX = 256


def _coinbase_spot() -> Optional[float]:
    try:
        resp = requests.get(COINBASE_SPOT_URL, timeout=5)
        resp.raise_for_status()
        return float(resp.json()["data"]["amount"])
    except Exception as e:
        log.debug("coinbase_spot_fail: %s", e)
        return None


def _kraken_spot() -> Optional[float]:
    try:
        resp = requests.get(KRAKEN_TICKER_URL, params={"pair": "XBTUSD"}, timeout=5)
        resp.raise_for_status()
        result = resp.json().get("result") or {}
        book = next(iter(result.values()), None) or {}
        last = (book.get("c") or [None])[0]
        return float(last) if last is not None else None
    except Exception as e:
        log.debug("kraken_spot_fail: %s", e)
        return None


def fetch_spot_price() -> Optional[float]:
    return _coinbase_spot() or _kraken_spot()


def fetch_price_at(start_ts: float) -> Optional[float]:
    """Approximate BTC USD at window open via Coinbase 1m candle open."""
    ts = int(start_ts)
    start_iso = datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    end_iso = datetime.fromtimestamp(ts + 120, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    try:
        resp = requests.get(
            COINBASE_CANDLES_URL,
            params={"start": start_iso, "end": end_iso, "granularity": 60},
            timeout=8,
        )
        resp.raise_for_status()
        rows = resp.json() or []
        if not rows:
            return None
        # Coinbase returns [time, low, high, open, close, volume], newest first.
        best = min(rows, key=lambda r: abs(int(r[0]) - ts))
        return float(best[3])
    except Exception as e:
        log.debug("coinbase_candle_fail: %s", e)
        return None


class BtcUnderlyingFeed:
    """Background live BTC price + cached price-to-beat lookups."""

    def __init__(self):
        self._live: Optional[float] = None
        self._live_ts: float = 0.0
        self._ptb_cache: dict[int, float] = {}
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._started = False

    def start(self) -> None:
        if self._started:
            return
        self._started = True
        # Seed immediately so first cycle isn't empty.
        spot = fetch_spot_price()
        if spot is not None:
            with self._lock:
                self._live = spot
                self._live_ts = time.time()
        self._thread = threading.Thread(target=self._run, name="btc-underlying-feed", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def live_price(self) -> Optional[float]:
        with self._lock:
            live, ts = self._live, self._live_ts
        if live is not None and (time.time() - ts) <= LIVE_STALE_S:
            return live
        spot = fetch_spot_price()
        if spot is not None:
            with self._lock:
                self._live = spot
                self._live_ts = time.time()
            return spot
        return live  # possibly stale; better than nothing for logging

    def price_to_beat(self, start_ts: float) -> Optional[float]:
        key = int(start_ts)
        with self._lock:
            cached = self._ptb_cache.get(key)
        if cached is not None:
            return cached
        ptb = fetch_price_at(start_ts)
        if ptb is None:
            return None
        with self._lock:
            if len(self._ptb_cache) >= PTB_CACHE_MAX:
                # Drop an arbitrary old entry
                self._ptb_cache.pop(next(iter(self._ptb_cache)))
            self._ptb_cache[key] = ptb
        return ptb

    def underlying_check(
        self, start_ts: float, min_edge_usd: float
    ) -> Tuple[bool, Optional[str], Optional[float], Optional[float], Optional[float]]:
        """Return (ok, favored_leg, ptb, live, edge).

        favored_leg is 'up' / 'down' when |live - ptb| >= min_edge_usd.
        ok is False when data missing or edge too small.
        """
        ptb = self.price_to_beat(start_ts)
        live = self.live_price()
        if ptb is None or live is None:
            return False, None, ptb, live, None
        edge = live - ptb
        if abs(edge) < min_edge_usd:
            return False, None, ptb, live, edge
        favored = "up" if edge > 0 else "down"
        return True, favored, ptb, live, edge

    def _set_live(self, value: float, ts_ms: Optional[float] = None) -> None:
        with self._lock:
            self._live = float(value)
            self._live_ts = (ts_ms / 1000.0) if ts_ms else time.time()

    def _run(self) -> None:
        while not self._stop.is_set():
            if self._run_rtds_once():
                continue
            # Fallback poll if websocket unavailable / drops
            spot = fetch_spot_price()
            if spot is not None:
                self._set_live(spot)
            self._stop.wait(2.0)

    def _run_rtds_once(self) -> bool:
        try:
            import websocket  # type: ignore
        except ImportError:
            return False

        connected = {"ok": False}

        def on_message(ws, message):
            if message == "PONG" or not message:
                return
            try:
                data = json.loads(message)
            except Exception:
                return
            payload = data.get("payload") or {}
            # Live update
            if "value" in payload and "data" not in payload:
                try:
                    self._set_live(float(payload["value"]), payload.get("timestamp"))
                    connected["ok"] = True
                except (TypeError, ValueError):
                    pass
                return
            # Initial historical dump — take newest point
            hist = payload.get("data")
            if isinstance(hist, list) and hist:
                try:
                    last = hist[-1]
                    self._set_live(float(last["value"]), last.get("timestamp"))
                    connected["ok"] = True
                except (TypeError, ValueError, KeyError):
                    pass

        def on_open(ws):
            ws.send(
                json.dumps(
                    {
                        "action": "subscribe",
                        "subscriptions": [
                            {
                                "topic": "crypto_prices_chainlink",
                                "type": "*",
                                "filters": json.dumps({"symbol": "btc/usd"}),
                            }
                        ],
                    }
                )
            )

        def on_error(ws, error):
            log.debug("rtds_error: %s", error)

        ws = websocket.WebSocketApp(
            RTDS_URL,
            on_message=on_message,
            on_open=on_open,
            on_error=on_error,
        )

        def pinger():
            while not self._stop.is_set():
                try:
                    ws.send("PING")
                except Exception:
                    break
                if self._stop.wait(5.0):
                    break

        ping_thread = threading.Thread(target=pinger, name="btc-rtds-ping", daemon=True)
        ping_thread.start()
        try:
            ws.run_forever(ping_interval=None)
        except Exception as e:
            log.debug("rtds_run_fail: %s", e)
        try:
            ws.close()
        except Exception:
            pass
        return connected["ok"]


# Process-wide feed used by buy bots
_FEED: Optional[BtcUnderlyingFeed] = None
_FEED_LOCK = threading.Lock()


def get_btc_feed() -> BtcUnderlyingFeed:
    global _FEED
    with _FEED_LOCK:
        if _FEED is None:
            _FEED = BtcUnderlyingFeed()
            _FEED.start()
        return _FEED
