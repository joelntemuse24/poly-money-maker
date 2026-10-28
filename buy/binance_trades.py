"""Tiny Binance BTCUSDT aggTrade client and 100ms last-trade grid."""
from __future__ import annotations

import collections
import json
import math
import threading
import time
from datetime import datetime, timezone
from typing import Any, Optional

URL = "wss://stream.binance.com:9443/ws/btcusdt@aggTrade"


class PriceGrid:
    def __init__(self, slots: int = 1800):
        self.slots = max(1800, int(slots))
        self._rows: list[Optional[tuple[int, float]]] = [None] * self.slots
        self._latest: Optional[int] = None
        self._twap_cache: dict[int, Optional[float]] = {}
        self._ms_ticks: collections.deque[tuple[float, float]] = collections.deque(maxlen=4096)
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
            self._ms_ticks.append((float(exchange_ms), px))
            self._latest = max(g, self._latest if self._latest is not None else g)
            self._rows[g % self.slots] = (g, px)
            self._twap_cache = {cached: value for cached, value in self._twap_cache.items() if cached < g}
        return g

    def twap60(self, g_end: int) -> Optional[float]:
        target = int(g_end)
        with self._lock:
            if target in self._twap_cache:
                return self._twap_cache[target]
            values = []
            for j in range(60):
                wanted = target - 10 * j
                value = None
                upper = min(wanted, self._latest if self._latest is not None else wanted)
                lower = max(upper - self.slots, self._latest - self.slots if self._latest is not None else upper - self.slots)
                for slot in range(upper, lower, -1):
                    row = self._rows[slot % self.slots]
                    if row is not None and row[0] == slot:
                        value = row[1]
                        break
                if value is None:
                    self._twap_cache[target] = None
                    return None
                values.append(value)
            result = sum(values) / 60.0
            self._twap_cache[target] = result
            while len(self._twap_cache) > 8:
                self._twap_cache.pop(min(self._twap_cache))
            return result

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

    def latest_spot(self) -> Optional[tuple[float, float]]:
        with self._lock:
            if self._ms_ticks:
                return self._ms_ticks[-1]
            if self._latest is None:
                return None
            px = self.price_at(self._latest)
            if px is None:
                return None
            return (self._latest * 100.0, px)

    def window_extrema(
        self, window_ms: float = 500.0, now_ms: Optional[float] = None
    ) -> Optional[tuple[float, float, float, float]]:
        with self._lock:
            if not self._ms_ticks:
                return None
            latest_ms = self._ms_ticks[-1][0]
            ref_ms = latest_ms if now_ms is None else float(now_ms)
            lower = ref_ms - float(window_ms)
            prices = [px for ts, px in self._ms_ticks if lower <= ts <= ref_ms]
            if not prices:
                return None
            spot_now = self._ms_ticks[-1][1]
            return (spot_now, min(prices), max(prices), latest_ms)

    def jump_in_window(
        self, window_ms: float = 500.0, now_ms: Optional[float] = None
    ) -> Optional[dict]:
        extrema = self.window_extrema(window_ms=window_ms, now_ms=now_ms)
        if extrema is None:
            return None
        spot_now, min_px, max_px, latest_ms = extrema
        return {
            "spot": spot_now,
            "min": min_px,
            "max": max_px,
            "up_jump": spot_now - min_px,
            "down_jump": max_px - spot_now,
            "ts_ms": latest_ms,
        }


def _parse_iso_ms(value: Any) -> Optional[float]:
    if not isinstance(value, str):
        return None
    try:
        text = value.replace("Z", "+00:00")
        parsed = datetime.fromisoformat(text)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.timestamp() * 1000.0
    except (ValueError, TypeError):
        return None


def parse_agg_trade(message: Any) -> Optional[tuple[float, float]]:
    try:
        obj = message if isinstance(message, dict) else json.loads(message)
        if not isinstance(obj, dict):
            return None
        # Combined stream wrapper: {"stream": ..., "data": {...}}
        if isinstance(obj.get("data"), dict):
            obj = obj["data"]

        # Binance aggTrade.
        if "T" in obj and "p" in obj:
            return float(obj["T"]), float(obj["p"])

        # Binance bookTicker -> mid price.
        if "b" in obj and "a" in obj:
            bid = float(obj["b"])
            ask = float(obj["a"])
            if bid > 0 and ask > 0:
                ts = obj.get("T") or obj.get("E") or (time.time() * 1000.0)
                return float(ts), (bid + ask) / 2.0

        # Coinbase ticker.
        if obj.get("type") == "ticker" and "price" in obj:
            ts = obj.get("T") or obj.get("E") or _parse_iso_ms(obj.get("time")) or (time.time() * 1000.0)
            return float(ts), float(obj["price"])
    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
        return None
    return None


parse_price_message = parse_agg_trade


class BinanceTrades:
    def __init__(self, grid: Optional[PriceGrid] = None, *, url: str = URL,
                 connect=None, sleep=None, on_error=None, on_tick=None,
                 extra_urls: Optional[list[str]] = None):
        self.grid = grid or PriceGrid()
        self.url = url
        self._connect = connect
        self._sleep = sleep or time.sleep
        self._on_error = on_error
        self._on_tick = on_tick
        self.extra_urls = list(extra_urls or [])
        self._last_error_at = 0.0
        self._stop = threading.Event()
        self._thread = None
        self._extra_threads: list[threading.Thread] = []
        self._ws = None
        self._wss: list[Any] = []
        self._ws_lock = threading.Lock()

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="fivemin-binance", daemon=True)
        self._thread.start()
        self._extra_threads = []
        for idx, extra_url in enumerate(self.extra_urls):
            thread = threading.Thread(
                target=self._run,
                args=(extra_url,),
                name=f"fivemin-binance-extra-{idx}",
                daemon=True,
            )
            self._extra_threads.append(thread)
            thread.start()

    def stop(self):
        self._stop.set()
        with self._ws_lock:
            sockets = list(self._wss)
        for ws in sockets:
            try:
                ws.close()
            except Exception:
                pass

    def _track_ws(self, ws: Any, tracked: bool) -> None:
        with self._ws_lock:
            if tracked:
                self._wss.append(ws)
                self._ws = ws
            else:
                try:
                    self._wss.remove(ws)
                except ValueError:
                    pass
                if self._ws is ws:
                    self._ws = self._wss[-1] if self._wss else None

    def _run(self, run_url: Optional[str] = None):
        import websocket
        target_url = run_url or self.url
        attempt = 0
        while not self._stop.is_set():
            ws = None
            tracked = False
            try:
                ws = self._connect(target_url) if self._connect else websocket.create_connection(target_url, timeout=10, enable_multithread=True)
                self._track_ws(ws, True)
                tracked = True
                received = False
                last_slot = None
                last_price = None
                while not self._stop.is_set():
                    raw = ws.recv()
                    if not raw:
                        break
                    parsed = parse_agg_trade(raw)
                    if parsed:
                        exchange_ms, price = parsed
                        slot = (int(exchange_ms) + 99) // 100
                        if slot != last_slot or price != last_price:
                            self.grid.add(exchange_ms, price)
                            last_slot, last_price = slot, price
                            if self._on_tick is not None:
                                try:
                                    self._on_tick(exchange_ms, price)
                                except Exception:
                                    pass
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
                if tracked and ws is not None:
                    self._track_ws(ws, False)
                if run_url is None:
                    self._ws = None
