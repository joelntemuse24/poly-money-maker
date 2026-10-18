"""Tiny Binance BTCUSDT aggTrade client and 100ms last-trade grid."""
from __future__ import annotations

import json
import math
import threading
import time
from typing import Any, Optional

URL = "wss://stream.binance.com:9443/ws/btcusdt@aggTrade"

class PriceGrid:
    def __init__(self, slots: int = 1800):
        self.slots = max(1800, int(slots))
        self._rows: list[Optional[tuple[int, float]]] = [None] * self.slots
        self._latest: Optional[int] = None
        self._lock = threading.Lock()

    def add(self, exchange_ms: Any, price: Any) -> Optional[int]:
        try:
            g = (int(exchange_ms) + 99) // 100
            px = float(price)
            if not math.isfinite(px) or px <= 0:
                return None
        except (TypeError, ValueError, OverflowError):
            return None
        with self._lock:
            if self._latest is not None and g <= self._latest - self.slots:
                return None
            self._latest = max(g, self._latest if self._latest is not None else g)
            self._rows[g % self.slots] = (g, px)
        return g

    def latest_exchange_ms(self) -> Optional[float]:
        with self._lock:
            return None if self._latest is None else self._latest * 100.0

    def price_at(self, g: int) -> Optional[float]:
        with self._lock:
            if self._latest is None:
                return None
            lower = max(self._latest - self.slots, int(g) - self.slots)
            for slot in range(min(int(g), self._latest), lower, -1):
                row = self._rows[slot % self.slots]
                if row is not None and row[0] == slot:
                    return row[1]
        return None


def parse_agg_trade(message: Any) -> Optional[tuple[float, float]]:
    try:
        obj = json.loads(message) if isinstance(message, (str, bytes, bytearray)) else message
        if not isinstance(obj, dict):
            return None
        return float(obj["T"]), float(obj["p"])
    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
        return None


class BinanceTrades:
    def __init__(self, grid: Optional[PriceGrid] = None, *, url: str = URL,
                 connect=None, sleep=None, on_error=None):
        self.grid = grid or PriceGrid()
        self.url = url
        self._connect = connect
        self._sleep = sleep or time.sleep
        self._on_error = on_error
        self._last_error_at = 0.0
        self._stop = threading.Event()
        self._thread = None
        self._ws = None

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="fivemin-binance", daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        ws = self._ws
        if ws is not None:
            try:
                ws.close()
            except Exception:
                pass

    def _run(self):
        import websocket
        attempt = 0
        while not self._stop.is_set():
            try:
                ws = self._connect(self.url) if self._connect else websocket.create_connection(self.url, timeout=10, enable_multithread=True)
                self._ws = ws
                received = False
                while not self._stop.is_set():
                    raw = ws.recv()
                    if not raw:
                        break
                    parsed = parse_agg_trade(raw)
                    if parsed:
                        self.grid.add(*parsed)
                        received = True
                ws.close()
                attempt = 0 if received else attempt + 1
                if not self._stop.is_set():
                    self._stop.wait(min(30.0, 0.5 * (2 ** min(attempt, 6))))
            except Exception as exc:
                attempt += 1
                now = time.time()
                if self._on_error is not None and now - self._last_error_at >= 60:
                    self._last_error_at = now
                    try:
                        self._on_error(str(exc))
                    except Exception:
                        pass
                if self._sleep is time.sleep:
                    self._stop.wait(min(30.0, 0.5 * (2 ** min(attempt, 6))))
                else:
                    self._sleep(min(30.0, 0.5 * (2 ** min(attempt, 6))))
            finally:
                self._ws = None
