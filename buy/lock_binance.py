"""Binance BTCUSDT trade stream. In-memory only. No decision-path HTTP."""

from __future__ import annotations

import json
import math
import threading
import time
from collections import deque
from typing import Any, Callable, Optional

from buy.lock_fair import resample_1s, sigma_1s


BINANCE_TRADE_URLS = (
    "wss://stream.binance.com:9443/ws/btcusdt@trade",
    "wss://data-stream.binance.vision/ws/btcusdt@trade",
)


def parse_trade(raw: Any) -> Optional[tuple[float, float]]:
    """``(obs_ts seconds, price)`` from one trade frame, or None."""
    if isinstance(raw, (bytes, bytearray)):
        raw = raw.decode("utf-8", "replace")
    if isinstance(raw, str):
        text = raw.strip()
        if not text or text.upper() in {"PING", "PONG"}:
            return None
        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            return None
    elif isinstance(raw, dict):
        payload = raw
    else:
        return None
    if not isinstance(payload, dict):
        return None
    if payload.get("e") not in (None, "trade"):
        return None
    price = payload.get("p")
    stamp = payload.get("T", payload.get("E"))
    try:
        px = float(price)
        obs = float(stamp)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(px) or px <= 0 or not math.isfinite(obs):
        return None
    if obs > 10_000_000_000:
        obs = obs / 1000.0
    return obs, px


class BinanceTradeFeed:
    """Last trade plus a short history for the 3-second move and 1-second sigma."""

    def __init__(
        self,
        urls: tuple[str, ...] = BINANCE_TRADE_URLS,
        *,
        history_s: float = 1200.0,
        clock: Callable[[], float] = time.time,
        on_trade: Optional[Callable[[float, float, float], None]] = None,
    ) -> None:
        self.urls = urls
        self.history_s = float(history_s)
        self._clock = clock
        self._on_trade = on_trade
        self._lock = threading.Lock()
        self._hist: deque[tuple[float, float, float]] = deque()
        self._last: Optional[tuple[float, float, float]] = None
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._error = ""
        self._url = ""

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="lockbot-binance", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def last_error(self) -> str:
        with self._lock:
            return self._error

    def url(self) -> str:
        return self._url

    def latest(self) -> Optional[tuple[float, float, float]]:
        """``(obs_ts, recv_ts, price)``."""
        with self._lock:
            return self._last

    def history(self) -> list[tuple[float, float, float]]:
        with self._lock:
            return list(self._hist)

    def price_at(self, ts: float) -> Optional[float]:
        """Last trade at or before ``ts`` (observation time)."""
        target = float(ts)
        with self._lock:
            for obs, _recv, price in reversed(self._hist):
                if obs <= target:
                    return price
            return None

    def handle_message(self, raw: Any, *, recv_ts: Optional[float] = None) -> None:
        parsed = parse_trade(raw)
        if parsed is None:
            return
        obs, price = parsed
        recv = float(self._clock() if recv_ts is None else recv_ts)
        with self._lock:
            self._last = (obs, recv, price)
            self._hist.append((obs, recv, price))
            cutoff = obs - self.history_s
            while self._hist and self._hist[0][0] < cutoff:
                self._hist.popleft()
            if len(self._hist) > 20000:
                self._hist.popleft()
            self._error = ""
        callback = self._on_trade
        if callback is not None:
            try:
                callback(obs, recv, price)
            except Exception:
                return

    def sigma_before(
        self,
        start_ts: float,
        window_s: float = 300.0,
        min_n: int = 30,
    ) -> tuple[Optional[float], int, str]:
        """Sigma of 1-second returns over ``[start - window, start]``.

        Falls back to the latest ``window`` seconds when the pre-open span
        is too thin. Returns ``(sigma, n_returns, source)``.
        """
        start = float(start_ts)
        window = float(window_s)
        with self._lock:
            rows = list(self._hist)
        pre = resample_1s([(obs, px) for obs, _recv, px in rows if start - window - 1.0 <= obs <= start + 1e-6])
        # Drop the second after the open if the resampler ran past it.
        if len(pre) > int(window) + 2:
            pre = pre[: int(window) + 1]
        sigma, n = sigma_1s(pre)
        if n >= int(min_n):
            return sigma, n, "pre_window"
        tail = resample_1s([(obs, px) for obs, _recv, px in rows])
        if len(tail) > int(window) + 1:
            tail = tail[-int(window) - 1 :]
        sigma, n = sigma_1s(tail)
        if n >= int(min_n):
            return sigma, n, "rolling"
        return None, n, "short"

    def _run(self) -> None:
        import websocket

        while not self._stop.is_set():
            for url in self.urls:
                if self._stop.is_set():
                    return
                self._url = url
                try:
                    ws = websocket.WebSocketApp(
                        url,
                        on_message=lambda _ws, message: self.handle_message(message),
                        on_error=lambda _ws, err: self._set_error(str(err)[:200]),
                    )
                    ws.run_forever(ping_interval=20, ping_timeout=10)
                except Exception as exc:
                    self._set_error(str(exc)[:200])
                if self._stop.is_set():
                    return
                self._stop.wait(1.0)
            self._stop.wait(2.0)

    def _set_error(self, message: str) -> None:
        with self._lock:
            self._error = message
