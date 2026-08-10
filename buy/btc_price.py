"""BTC underlying prices aligned to Polymarket Up/Down markets.

Resolution (Polymarket rules): Chainlink BTC/USD TWAP vs Price To Beat (BTC at
window open). Crypto PTB is not exposed on public REST — the accurate approach
is to record Polymarket's own Chainlink RTDS stream at `start_ts`.

- Live + history: `wss://ws-live-data.polymarket.com` topic
  `crypto_prices_chainlink` filter `{"symbol":"btc/usd"}` (same pipe the UI uses).
- PTB: nearest Chainlink tick to market `start_ts` from an in-memory ring buffer
  (persisted to disk when captured). If we missed the open (bot down), PTB is
  marked missing and the trade gate refuses — we will not invent a Coinbase
  substitute for trading decisions.
- Hot path is memory-only (no HTTP). Spot HTTP is emergency live fallback only
  and is tagged so research logs stay honest about source quality.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from collections import deque
from typing import Any, Deque, Dict, Optional, Tuple

import requests

log = logging.getLogger("btc_price")

RTDS_URL = "wss://ws-live-data.polymarket.com"
COINBASE_SPOT_URL = "https://api.coinbase.com/v2/prices/BTC-USD/spot"
KRAKEN_TICKER_URL = "https://api.kraken.com/0/public/Ticker"

# Keep ~3h of ~1Hz ticks so 15m/hourly windows still resolve PTB after brief blips.
RING_MAX_SAMPLES = 12_000
# PTB tick must land within this many ms of market start_ts.
PTB_MAX_SKEW_MS = 2000
# Live Chainlink older than this → treat as stale for trading.
LIVE_STALE_S = 5.0
PTB_CACHE_MAX = 512
DEFAULT_PTB_STORE = "ptb_chainlink_cache.json"


def _coinbase_spot() -> Optional[float]:
    try:
        resp = requests.get(COINBASE_SPOT_URL, timeout=2)
        resp.raise_for_status()
        return float(resp.json()["data"]["amount"])
    except Exception:
        return None


def _kraken_spot() -> Optional[float]:
    try:
        resp = requests.get(KRAKEN_TICKER_URL, params={"pair": "XBTUSD"}, timeout=2)
        resp.raise_for_status()
        result = resp.json().get("result") or {}
        book = next(iter(result.values()), None) or {}
        last = (book.get("c") or [None])[0]
        return float(last) if last is not None else None
    except Exception:
        return None


def fetch_spot_fallback() -> Optional[float]:
    return _coinbase_spot() or _kraken_spot()


def append_research(path: str, record: Dict[str, Any]) -> None:
    """Append one JSON line for offline correlation / regression."""
    try:
        row = dict(record)
        row.setdefault("logged_at", time.time())
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(row, separators=(",", ":")) + "\n")
    except Exception as e:
        log.debug("research_log_fail: %s", e)


class BtcUnderlyingFeed:
    """Chainlink RTDS ring buffer + PTB capture for Up/Down windows."""

    def __init__(self, ptb_store_path: str = DEFAULT_PTB_STORE):
        self._live: Optional[float] = None
        self._live_ts: float = 0.0  # unix seconds (wall)
        self._live_src: str = "none"
        self._ticks: Deque[Tuple[int, float]] = deque(maxlen=RING_MAX_SAMPLES)  # (ts_ms, px)
        self._ptb: Dict[int, Dict[str, Any]] = {}  # start_ts_int -> record
        self._ptb_store_path = ptb_store_path
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._started = False
        self._load_ptb_store()

    def start(self) -> None:
        if self._started:
            return
        self._started = True
        self._thread = threading.Thread(target=self._run, name="btc-chainlink-rtds", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def live_quote(self) -> Tuple[Optional[float], str, Optional[float]]:
        """Return (price, source, age_s). Memory only — never blocks on HTTP."""
        with self._lock:
            live, ts, src = self._live, self._live_ts, self._live_src
        if live is None:
            return None, "none", None
        age = time.time() - ts
        return live, src, age

    def live_price(self, *, allow_stale: bool = False) -> Optional[float]:
        px, src, age = self.live_quote()
        if px is None:
            return None
        if age is not None and age > LIVE_STALE_S and not allow_stale:
            return None
        return px

    def price_to_beat(self, start_ts: float) -> Optional[float]:
        rec = self.ptb_record(start_ts)
        if not rec or not rec.get("ok"):
            return None
        return float(rec["ptb"])

    def ptb_record(self, start_ts: float) -> Optional[Dict[str, Any]]:
        """Return cached PTB record; try capture from ring buffer if missing."""
        key = int(start_ts)
        with self._lock:
            cached = self._ptb.get(key)
        if cached is not None:
            return dict(cached)
        return self.capture_ptb(start_ts)

    def capture_ptb(self, start_ts: float) -> Optional[Dict[str, Any]]:
        """Lock PTB from Chainlink ticks nearest to start_ts.

        Call this as soon as a market's window opens (or when discovered if
        start is already past but still inside the ring buffer).
        """
        key = int(start_ts)
        target_ms = key * 1000
        now = time.time()
        # Too early — window not open yet
        if now + 0.05 < start_ts:
            return None

        with self._lock:
            if key in self._ptb:
                return dict(self._ptb[key])
            if not self._ticks:
                rec = {
                    "ok": False,
                    "reason": "no_ticks",
                    "start_ts": key,
                    "source": "missing",
                    "captured_at": now,
                }
                self._ptb[key] = rec
                self._trim_ptb_unlocked()
                self._save_ptb_store_unlocked()
                return dict(rec)

            # Nearest tick by |ts_ms - start_ms|
            best_ts, best_px = min(self._ticks, key=lambda t: abs(t[0] - target_ms))
            skew_ms = abs(best_ts - target_ms)
            ok = skew_ms <= PTB_MAX_SKEW_MS
            rec = {
                "ok": ok,
                "ptb": float(best_px),
                "ptb_tick_ts_ms": int(best_ts),
                "ptb_skew_ms": int(skew_ms),
                "start_ts": key,
                "source": "chainlink_rtds" if ok else "chainlink_rtds_skewed",
                "reason": None if ok else "skew_too_large",
                "captured_at": now,
            }
            # Only persist usable PTBs as authoritative; skewed kept for research
            self._ptb[key] = rec
            self._trim_ptb_unlocked()
            self._save_ptb_store_unlocked()
            return dict(rec)

    def underlying_check(
        self, start_ts: float, min_edge_usd: float
    ) -> Dict[str, Any]:
        """Fast memory-only gate result for the buy loop."""
        ptb_rec = self.ptb_record(start_ts) or {}
        live, live_src, live_age = self.live_quote()
        ptb = ptb_rec.get("ptb") if ptb_rec.get("ok") else None
        out: Dict[str, Any] = {
            "ok": False,
            "favored": None,
            "ptb": ptb,
            "live_btc": live,
            "edge_usd": None,
            "ptb_source": ptb_rec.get("source", "missing"),
            "ptb_skew_ms": ptb_rec.get("ptb_skew_ms"),
            "ptb_ok": bool(ptb_rec.get("ok")),
            "live_source": live_src,
            "live_age_s": live_age,
        }
        if ptb is None or live is None:
            out["reason"] = "missing_ptb" if ptb is None else "missing_live"
            return out
        if live_age is not None and live_age > LIVE_STALE_S:
            out["reason"] = "live_stale"
            return out
        edge = float(live) - float(ptb)
        out["edge_usd"] = edge
        if abs(edge) < min_edge_usd:
            out["reason"] = "edge_too_small"
            return out
        out["ok"] = True
        out["favored"] = "up" if edge > 0 else "down"
        out["reason"] = None
        return out

    def _trim_ptb_unlocked(self) -> None:
        while len(self._ptb) > PTB_CACHE_MAX:
            self._ptb.pop(next(iter(self._ptb)))

    def _load_ptb_store(self) -> None:
        path = self._ptb_store_path
        if not path or not os.path.exists(path):
            return
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                for k, v in data.items():
                    try:
                        self._ptb[int(k)] = v
                    except (TypeError, ValueError):
                        continue
        except Exception as e:
            log.debug("ptb_store_load_fail: %s", e)

    def _save_ptb_store_unlocked(self) -> None:
        path = self._ptb_store_path
        if not path:
            return
        try:
            tmp = path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self._ptb, f, separators=(",", ":"))
            os.replace(tmp, path)
        except Exception as e:
            log.debug("ptb_store_save_fail: %s", e)

    def _push_tick(self, ts_ms: int, value: float) -> None:
        with self._lock:
            self._ticks.append((int(ts_ms), float(value)))
            self._live = float(value)
            self._live_ts = ts_ms / 1000.0
            self._live_src = "chainlink_rtds"

    def _set_live_fallback(self, value: float, source: str) -> None:
        with self._lock:
            self._live = float(value)
            self._live_ts = time.time()
            self._live_src = source

    def _run(self) -> None:
        while not self._stop.is_set():
            if self._run_rtds_once():
                continue
            # Emergency only — tagged so we never confuse with Chainlink PTB.
            spot = fetch_spot_fallback()
            if spot is not None:
                self._set_live_fallback(spot, "spot_fallback")
            self._stop.wait(1.0)

    def _run_rtds_once(self) -> bool:
        try:
            import websocket  # type: ignore
        except ImportError:
            return False

        got_chainlink = {"ok": False}

        def on_message(ws, message):
            if message == "PONG" or not message:
                return
            try:
                data = json.loads(message)
            except Exception:
                return
            payload = data.get("payload") or {}
            topic = data.get("topic") or ""
            # Prefer chainlink topic; also accept dumps that carry symbol btc/usd
            if "value" in payload and "data" not in payload:
                try:
                    ts_ms = int(payload.get("timestamp") or time.time() * 1000)
                    self._push_tick(ts_ms, float(payload["value"]))
                    got_chainlink["ok"] = True
                except (TypeError, ValueError):
                    pass
                return
            hist = payload.get("data")
            if isinstance(hist, list) and hist:
                for point in hist:
                    try:
                        ts_ms = int(point["timestamp"])
                        self._push_tick(ts_ms, float(point["value"]))
                        got_chainlink["ok"] = True
                    except (TypeError, ValueError, KeyError):
                        continue

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

        threading.Thread(target=pinger, name="btc-rtds-ping", daemon=True).start()
        try:
            ws.run_forever(ping_interval=None)
        except Exception as e:
            log.debug("rtds_run_fail: %s", e)
        try:
            ws.close()
        except Exception:
            pass
        return got_chainlink["ok"]


_FEED: Optional[BtcUnderlyingFeed] = None
_FEED_LOCK = threading.Lock()


def get_btc_feed(ptb_store_path: str = DEFAULT_PTB_STORE) -> BtcUnderlyingFeed:
    global _FEED
    with _FEED_LOCK:
        if _FEED is None:
            _FEED = BtcUnderlyingFeed(ptb_store_path=ptb_store_path)
            _FEED.start()
        return _FEED
