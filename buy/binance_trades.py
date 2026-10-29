"""Multi-source BTC trade/quote WebSocket client and 100ms last-trade grid."""
from __future__ import annotations

import collections
import json
import math
import os
import socket
import threading
import time
from datetime import datetime, timezone
from typing import Any, Optional

try:
    import orjson  # type: ignore

    _HAS_ORJSON = True
except Exception:  # pragma: no cover - optional dependency
    orjson = None  # type: ignore
    _HAS_ORJSON = False

URL = "wss://stream.binance.com:9443/ws/btcusdt@aggTrade"
BINANCE_FALLBACK_URLS = (
    URL,
    "wss://stream.binance.com:443/ws/btcusdt@aggTrade",
    "wss://data-stream.binance.vision:9443/ws/btcusdt@aggTrade",
    "wss://data-stream.binance.vision:443/ws/btcusdt@aggTrade",
)
DEFAULT_EXTRA_WS_URLS = (
    "wss://stream.binance.com:9443/ws/btcusdt@bookTicker",
    "wss://fstream.binance.com/ws/btcusdt@bookTicker",
    "wss://stream.bybit.com/v5/public/linear",
    "wss://ws-feed.exchange.coinbase.com",
    "wss://www.deribit.com/ws/api/v2",
    "wss://ws.kraken.com/v2",
    "wss://ws.bitstamp.net",
)
MAX_BBO_SPREAD_USD = 5.0
# Deribit closes a session after 10 minutes without a client request, even while
# subscribed data keeps flowing. ``public/test`` is a cheap no-op request.
DERIBIT_KEEPALIVE = '{"jsonrpc":"2.0","id":99,"method":"public/test"}'


def _fast_loads(raw: Any) -> Any:
    if isinstance(raw, dict):
        return raw
    if _HAS_ORJSON and isinstance(raw, (bytes, bytearray, str)):
        return orjson.loads(raw)
    return json.loads(raw)


_TICK_RING_CLS: Optional[type] = None
_TICK_RING_IMPORTED = False


def _tick_ring_cls() -> Optional[type]:
    """Return the ``polyhot.TickRing`` class once, or None if the module is absent."""
    global _TICK_RING_CLS, _TICK_RING_IMPORTED
    if not _TICK_RING_IMPORTED:
        _TICK_RING_IMPORTED = True
        try:
            from polyhot import TickRing  # type: ignore
        except Exception:  # missing wheel, broken build, wrong ABI
            _TICK_RING_CLS = None
        else:
            _TICK_RING_CLS = TickRing
    return _TICK_RING_CLS


def _rust_hotpath_enabled(use_rust: Optional[bool]) -> bool:
    if use_rust is None:
        return os.environ.get("POLY_RUST_HOTPATH") == "1"
    return bool(use_rust)


class PriceGrid:
    def __init__(self, slots: int = 1800, *, use_rust: Optional[bool] = None):
        self.slots = max(1800, int(slots))
        ring_cls = _tick_ring_cls() if _rust_hotpath_enabled(use_rust) else None
        self._ring_cls: Optional[type] = ring_cls
        self.rust_enabled: bool = ring_cls is not None
        # Parallel Rust rings mirror the tick deques below; they only serve the
        # window scans in ``window_extrema`` / ``best_jump_in_window``.
        self._ring_ms_ticks: Any = ring_cls() if ring_cls is not None else None
        self._ring_ms_by_source: dict[str, Any] = {}
        self._ring_recv_by_source: dict[str, Any] = {}
        self._rows: list[Optional[tuple[int, float]]] = [None] * self.slots
        self._latest: Optional[int] = None
        self._twap_cache: dict[int, Optional[float]] = {}
        self._ms_ticks: collections.deque[tuple[float, float]] = collections.deque(maxlen=4096)
        self._ms_ticks_by_source: dict[str, collections.deque[tuple[float, float]]] = {}
        self._recv_ticks_by_source: dict[str, collections.deque[tuple[float, float]]] = {}
        self._latest_by_source: dict[str, tuple[float, float, float]] = {}
        self._latest_recv_wall_ms: dict[str, float] = {}
        self._basis_to_primary: dict[str, float] = {}
        self._lock = threading.Lock()

    def add(
        self,
        exchange_ms: Any,
        price: Any,
        *,
        source: str = "primary",
        recv_ms: Optional[float] = None,
        recv_wall_ms: Optional[float] = None,
    ) -> Optional[int]:
        try:
            g = (int(exchange_ms) + 99) // 100
            px = float(price)
            ex_ms = float(exchange_ms)
            if not math.isfinite(px) or px <= 0 or not math.isfinite(ex_ms):
                return None
        except (TypeError, ValueError, OverflowError):
            return None
        src_key = str(source or "primary")
        r_ms = ex_ms if recv_ms is None else float(recv_ms)
        with self._lock:
            buf = self._ms_ticks_by_source.get(src_key)
            if buf is None:
                buf = collections.deque(maxlen=4096)
                self._ms_ticks_by_source[src_key] = buf
            buf.append((ex_ms, px))

            rbuf = self._recv_ticks_by_source.get(src_key)
            if rbuf is None:
                rbuf = collections.deque(maxlen=4096)
                self._recv_ticks_by_source[src_key] = rbuf
            rbuf.append((r_ms, px))

            if self.rust_enabled:
                ring = self._ring_ms_by_source.get(src_key)
                if ring is None:
                    ring = self._ring_cls()
                    self._ring_ms_by_source[src_key] = ring
                ring.append(ex_ms, px)
                rring = self._ring_recv_by_source.get(src_key)
                if rring is None:
                    rring = self._ring_cls()
                    self._ring_recv_by_source[src_key] = rring
                rring.append(r_ms, px)

            self._latest_by_source[src_key] = (r_ms, ex_ms, px)
            if recv_wall_ms is not None:
                self._latest_recv_wall_ms[src_key] = float(recv_wall_ms)
            if src_key == "primary":
                self._basis_to_primary["primary"] = 0.0
                for other_src, (_o_r, _o_e, o_px) in self._latest_by_source.items():
                    if other_src != "primary":
                        diff = px - o_px
                        prev = self._basis_to_primary.get(other_src)
                        self._basis_to_primary[other_src] = (
                            diff if prev is None else (0.8 * prev + 0.2 * diff)
                        )
            else:
                prim = self._latest_by_source.get("primary")
                if prim is not None and src_key not in self._basis_to_primary:
                    self._basis_to_primary[src_key] = prim[2] - px
                return g
            if self._latest is not None and g <= self._latest - self.slots:
                return None
            self._ms_ticks.append((ex_ms, px))
            if self.rust_enabled:
                self._ring_ms_ticks.append(ex_ms, px)
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
        self,
        window_ms: float = 500.0,
        now_ms: Optional[float] = None,
        *,
        source: str = "primary",
    ) -> Optional[tuple[float, float, float, float]]:
        src_key = str(source or "primary")
        with self._lock:
            ticks = self._ms_ticks if src_key == "primary" else self._ms_ticks_by_source.get(src_key)
            if not ticks:
                return None
            latest_ms = ticks[-1][0]
            ref_ms = latest_ms if now_ms is None else float(now_ms)
            lower = ref_ms - float(window_ms)
            if self.rust_enabled:
                ring = self._ring_ms_ticks if src_key == "primary" else self._ring_ms_by_source.get(src_key)
                bounds = None if ring is None else ring.window_minmax(lower, ref_ms)
                if bounds is None:
                    return None
                spot_now = ticks[-1][1]
                return (spot_now, bounds[0], bounds[1], latest_ms)
            prices = [px for ts, px in ticks if lower <= ts <= ref_ms]
            if not prices:
                return None
            spot_now = ticks[-1][1]
            return (spot_now, min(prices), max(prices), latest_ms)

    def jump_in_window(
        self,
        window_ms: float = 500.0,
        now_ms: Optional[float] = None,
        *,
        source: str = "primary",
    ) -> Optional[dict]:
        if str(source or "primary") == "all":
            return self.best_jump_in_window(window_ms=window_ms, now_ms=now_ms)
        extrema = self.window_extrema(window_ms=window_ms, now_ms=now_ms, source=source)
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
            "recv_ms": self._latest_recv_ms(str(source or "primary")),
            "recv_wall_ms": self.latest_recv_wall_ms(source),
            "source": str(source or "primary"),
        }

    def _latest_recv_ms(self, source: str) -> Optional[float]:
        with self._lock:
            latest = self._latest_by_source.get(source)
            return None if latest is None else latest[0]

    def latest_recv_wall_ms(self, source: str = "primary") -> Optional[float]:
        """Local wall-clock receive time (epoch ms) of the newest tick from ``source``."""
        with self._lock:
            return self._latest_recv_wall_ms.get(str(source or "primary"))

    def feed_recv_snapshot(self, window_ms: float,
                           ref_ms: Optional[float] = None) -> dict[str, Any]:
        """Cheap snapshot of feeds whose newest tick landed within ``window_ms`` of ``ref_ms``.

        Uses only the per-source timestamps already tracked by ``add``, so it is safe to call
        on the logging path. ``feed_recv`` values are local recv wall ms (epoch). Feeds without
        a stored wall stamp fall back to their monotonic recv stamp shifted by the current
        ``time.time() - time.monotonic()`` offset.
        """
        win = float(window_ms)
        wall_offset_ms = time.time() * 1000.0 - time.monotonic() * 1000.0
        with self._lock:
            recv_by_src = {src: info[0] for src, info in self._latest_by_source.items()}
            if not recv_by_src:
                return {"feed_recv": {}, "first_feed": None}
            # ``recv_ms`` is monotonic; default the window anchor to the newest tick.
            ref = max(recv_by_src.values()) if ref_ms is None else float(ref_ms)
            seen = {src: recv for src, recv in recv_by_src.items() if ref - recv <= win}
            if not seen:
                return {"feed_recv": {}, "first_feed": None}
            feed_recv = {}
            for src, recv in seen.items():
                wall = self._latest_recv_wall_ms.get(src)
                feed_recv[src] = wall if wall is not None else recv + wall_offset_ms
            first_feed = min(seen, key=seen.__getitem__)
        return {"feed_recv": feed_recv, "first_feed": first_feed}

    def best_jump_in_window(
        self,
        window_ms: float = 500.0,
        now_ms: Optional[float] = None,
        *,
        recv_now_ms: Optional[float] = None,
    ) -> Optional[dict]:
        """Return the largest single-source jump across primary and extra feeds.

        Each source is evaluated purely against its own sliding window so cross-exchange
        price spreads never create false jumps. When an extra feed triggers first, its
        current price is translated into the primary (Binance Spot) basis using the
        tracked EMA basis ``primary - source``.
        """
        win = float(window_ms)
        with self._lock:
            if not self._recv_ticks_by_source and not self._ms_ticks:
                return None
            best: Optional[dict] = None
            best_mag = -1.0
            prim_latest = self._latest_by_source.get("primary")
            for src_key, rticks in self._recv_ticks_by_source.items():
                if not rticks:
                    continue
                latest_r, spot_raw = rticks[-1]
                latest_info = self._latest_by_source.get(src_key)
                latest_ex = latest_info[1] if latest_info is not None else latest_r
                if recv_now_ms is not None:
                    ref_r = float(recv_now_ms)
                elif now_ms is not None and abs(float(now_ms) - latest_r) <= 60_000.0:
                    ref_r = float(now_ms)
                else:
                    ref_r = latest_r
                # Ignore sources that have not ticked recently relative to ref_r
                if ref_r - latest_r > max(win * 2.0, 1500.0):
                    continue
                lower = ref_r - win
                if self.rust_enabled:
                    rring = self._ring_recv_by_source.get(src_key)
                    bounds = None if rring is None else rring.window_minmax(lower, ref_r)
                    if bounds is None:
                        continue
                    min_px, max_px = bounds
                else:
                    prices = [px for ts, px in rticks if lower <= ts <= ref_r]
                    if not prices:
                        continue
                    min_px = min(prices)
                    max_px = max(prices)
                up_j = spot_raw - min_px
                dn_j = max_px - spot_raw
                mag = max(up_j, dn_j)
                if src_key == "primary":
                    implied_spot = spot_raw
                    ts_out = latest_ex
                else:
                    basis = self._basis_to_primary.get(src_key)
                    if basis is not None:
                        implied_spot = spot_raw + basis
                    elif prim_latest is not None:
                        implied_spot = prim_latest[2] + (up_j if up_j >= dn_j else -dn_j)
                    else:
                        implied_spot = spot_raw
                    ts_out = prim_latest[1] if prim_latest is not None and abs(latest_ex - prim_latest[1]) > 60_000.0 else latest_ex
                cand = {
                    "spot": implied_spot,
                    "raw_spot": spot_raw,
                    "min": min_px,
                    "max": max_px,
                    "up_jump": up_j,
                    "down_jump": dn_j,
                    "ts_ms": ts_out,
                    "recv_ms": latest_r,
                    "recv_wall_ms": self._latest_recv_wall_ms.get(src_key),
                    "source": src_key,
                }
                if mag > best_mag or (mag == best_mag and src_key == "primary"):
                    best_mag = mag
                    best = cand
            return best


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
        obj = _fast_loads(message)
        if not isinstance(obj, dict):
            return None
        # Combined stream wrapper: {"stream": ..., "data": {...}}
        if isinstance(obj.get("data"), dict) and "stream" in obj:
            obj = obj["data"]

        # 1. Binance trade / aggTrade
        if "T" in obj and "p" in obj:
            return float(obj["T"]), float(obj["p"])

        # 2. Binance bookTicker (spot or USD-M futures) -> mid price (tight spread filter <= $5)
        if "b" in obj and "a" in obj and not isinstance(obj["b"], list):
            bid = float(obj["b"])
            ask = float(obj["a"])
            if bid > 0 and ask >= bid and (ask - bid) <= MAX_BBO_SPREAD_USD:
                ts = obj.get("T") or obj.get("E") or (time.time() * 1000.0)
                return float(ts), (bid + ask) / 2.0

        # 3. Coinbase matches or ticker
        msg_type = obj.get("type")
        if msg_type in ("match", "last_match", "ticker") and "price" in obj:
            if msg_type == "ticker" and "best_bid" in obj and "best_ask" in obj:
                bid = float(obj["best_bid"])
                ask = float(obj["best_ask"])
                if bid > 0 and ask >= bid and (ask - bid) <= MAX_BBO_SPREAD_USD:
                    ts = _parse_iso_ms(obj.get("time")) or (time.time() * 1000.0)
                    return float(ts), (bid + ask) / 2.0
            ts = obj.get("T") or obj.get("E") or _parse_iso_ms(obj.get("time")) or (time.time() * 1000.0)
            return float(ts), float(obj["price"])

        # 4. Bybit v5 publicTrade.BTCUSDT or orderbook.1.BTCUSDT
        topic = obj.get("topic")
        if isinstance(topic, str):
            data = obj.get("data")
            if topic.startswith("publicTrade.") and isinstance(data, list) and data:
                last = data[-1]
                return float(last["T"]), float(last["p"])
            if topic.startswith("orderbook.") and isinstance(data, dict):
                bids, asks = data.get("b"), data.get("a")
                if isinstance(bids, list) and bids and isinstance(asks, list) and asks:
                    bid = float(bids[0][0])
                    ask = float(asks[0][0])
                    if bid > 0 and ask >= bid and (ask - bid) <= MAX_BBO_SPREAD_USD:
                        ts = obj.get("cts") or obj.get("ts") or (time.time() * 1000.0)
                        return float(ts), (bid + ask) / 2.0

        # 5. Deribit trades / ticker
        params = obj.get("params")
        if isinstance(params, dict):
            ch = str(params.get("channel") or "")
            pdata = params.get("data")
            if ch.startswith("trades.") and isinstance(pdata, list) and pdata:
                last = pdata[-1]
                return float(last["timestamp"]), float(last["price"])
            if ch.startswith("ticker.") and isinstance(pdata, dict):
                bid = pdata.get("best_bid_price")
                ask = pdata.get("best_ask_price")
                if bid is not None and ask is not None:
                    b_f, a_f = float(bid), float(ask)
                    if b_f > 0 and a_f >= b_f and (a_f - b_f) <= MAX_BBO_SPREAD_USD:
                        return float(pdata.get("timestamp") or (time.time() * 1000.0)), (b_f + a_f) / 2.0

        # 6. Kraken v2 trade / ticker
        if obj.get("channel") == "trade" and isinstance(obj.get("data"), list) and obj["data"]:
            last = obj["data"][-1]
            if "price" in last:
                ts = _parse_iso_ms(last.get("timestamp")) or (time.time() * 1000.0)
                return float(ts), float(last["price"])

        # 7. Bitstamp live_trades_btcusd
        if obj.get("event") == "trade" and isinstance(obj.get("data"), dict):
            bdata = obj["data"]
            if "price" in bdata:
                mts = bdata.get("microtimestamp")
                ts = (float(mts) / 1000.0) if mts is not None else float(bdata.get("timestamp", time.time())) * 1000.0
                return float(ts), float(bdata["price"])
    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
        return None
    return None


parse_price_message = parse_agg_trade


def feed_name_for_url(url: str, *, primary: bool = False) -> str:
    """Short feed name for a WS URL; ``fstream`` (USD-M futures) is ``binance_fut``."""
    if primary:
        return "primary"
    u = str(url or "").lower()
    if "fstream.binance.com" in u:
        return "binance_fut"
    if "binance.com" in u or "binance.vision" in u:
        return "binance_spot"
    if "bybit.com" in u:
        return "bybit"
    if "coinbase.com" in u:
        return "coinbase"
    if "deribit.com" in u:
        return "deribit"
    if "kraken.com" in u:
        return "kraken"
    if "bitstamp.net" in u:
        return "bitstamp"
    return str(url)


def subscription_for_url(url: str) -> Optional[str]:
    """Return initial JSON subscription message when ``url`` requires one."""
    u = str(url or "").lower()
    if "bybit.com" in u:
        return '{"op":"subscribe","args":["publicTrade.BTCUSDT","orderbook.1.BTCUSDT"]}'
    if "coinbase.com" in u:
        return '{"type":"subscribe","product_ids":["BTC-USD"],"channels":["matches","ticker"]}'
    if "deribit.com" in u:
        return '{"jsonrpc":"2.0","id":1,"method":"public/subscribe","params":{"channels":["trades.BTC-PERPETUAL.100ms","ticker.BTC-PERPETUAL.100ms"]}}'
    if "kraken.com" in u:
        return '{"method":"subscribe","params":{"channel":"trade","symbol":["BTC/USD"],"snapshot":false}}'
    if "bitstamp.net" in u:
        return '{"event":"bts:subscribe","data":{"channel":"live_trades_btcusd"}}'
    return None


class BinanceTrades:
    def __init__(self, grid: Optional[PriceGrid] = None, *, url: str = URL,
                 connect=None, sleep=None, on_error=None, on_tick=None,
                 extra_urls: Optional[list[str]] = None, on_event=None):
        self.grid = grid or PriceGrid()
        self.url = str(url or URL)
        self._on_event = on_event
        self._feed_state: dict[str, dict[str, Any]] = {}
        self._last_health_log = 0.0
        self._last_disconnect_log: dict[str, float] = {}
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

    def _event(self, event: str, **fields: Any) -> None:
        if self._on_event is not None:
            try:
                self._on_event(event, **fields)
            except Exception:
                pass

    def health(self) -> dict[str, dict[str, Any]]:
        now = time.monotonic()
        with self._ws_lock:
            return {feed: {"connected": state["connected"],
                           "last_message_age_s": None if state["last_message"] is None else
                           round(max(0.0, now - state["last_message"]), 2)}
                    for feed, state in self._feed_state.items()}

    def log_health(self) -> None:
        now = time.monotonic()
        if now - self._last_health_log >= 60:
            self._last_health_log = now
            self._event("fivemin_binance_health", feeds=self.health())

    def _feed_update(self, feed: str, connected: bool, message: bool = False) -> None:
        with self._ws_lock:
            state = self._feed_state.setdefault(feed, {"connected": False, "last_message": None})
            state["connected"] = connected
            if message:
                state["last_message"] = time.monotonic()

    def _disconnect_event(self, feed: str, url: str, reason: str) -> None:
        now = time.monotonic()
        previous = self._last_disconnect_log.get(feed)
        if previous is None or now - previous >= 60.0:
            self._last_disconnect_log[feed] = now
            self._event("fivemin_binance_disconnect", feed=feed, url=url, reason=reason)

    def _run(self, run_url: Optional[str] = None):
        import websocket
        primary = run_url is None
        candidates = (
            (self.url,) + tuple(u for u in BINANCE_FALLBACK_URLS if u != self.url)
            if primary and self.url.startswith("wss://") else (URL,) if primary else (run_url,)
        )
        feed = feed_name_for_url(run_url, primary=primary)
        attempt = 0
        while not self._stop.is_set():
            target_url = candidates[attempt % len(candidates)]
            sub_msg = subscription_for_url(target_url)
            is_bybit = "bybit.com" in target_url.lower()
            is_deribit = "deribit.com" in target_url.lower()
            ws = None
            tracked = False
            try:
                if self._connect:
                    ws = self._connect(target_url)
                else:
                    ws = websocket.create_connection(
                        target_url,
                        timeout=10,
                        enable_multithread=True,
                        sockopt=((socket.IPPROTO_TCP, socket.TCP_NODELAY, 1),),
                    )
                self._track_ws(ws, True)
                tracked = True
                self._feed_update(feed, True)
                self._event("fivemin_binance_connect", feed=feed, url=target_url)
                if sub_msg and hasattr(ws, "send"):
                    ws.send(sub_msg)
                received = False
                last_price = None
                last_ping = time.monotonic()
                last_message = last_ping
                while not self._stop.is_set():
                    now_m = time.monotonic()
                    if is_bybit and now_m - last_ping >= 20.0 and hasattr(ws, "send"):
                        try:
                            ws.send('{"op":"ping"}')
                        except Exception:
                            pass
                        last_ping = now_m
                    elif is_deribit and now_m - last_ping >= 60.0 and hasattr(ws, "send"):
                        # Deribit closes sessions that send no request for 10 min;
                        # market data alone does not count as client activity.
                        try:
                            ws.send(DERIBIT_KEEPALIVE)
                        except Exception:
                            pass
                        last_ping = now_m
                    try:
                        raw = ws.recv()
                    except websocket.WebSocketTimeoutException:
                        now_m = time.monotonic()
                        if primary or now_m - last_message >= 60.0:
                            raise
                        if hasattr(ws, "send") and now_m - last_ping >= 10.0:
                            if "bitstamp.net" in target_url.lower():
                                ws.send('{"event":"bts:heartbeat"}')
                            else:
                                ws.ping()
                            last_ping = now_m
                        continue
                    if not raw:
                        break
                    last_message = time.monotonic()
                    self._feed_update(feed, True, message=True)
                    recv_ms = time.monotonic() * 1000.0
                    recv_wall_ms = time.time() * 1000.0
                    parsed = parse_agg_trade(raw)
                    if parsed:
                        exchange_ms, price = parsed
                        if price != last_price:
                            src = feed
                            try:
                                self.grid.add(exchange_ms, price, source=src, recv_ms=recv_ms,
                                              recv_wall_ms=recv_wall_ms)
                            except TypeError:
                                try:
                                    self.grid.add(exchange_ms, price, source=src)
                                except TypeError:
                                    if src == "primary":
                                        self.grid.add(exchange_ms, price)
                            last_price = price
                            if self._on_tick is not None:
                                try:
                                    self._on_tick(exchange_ms, price)
                                except Exception:
                                    pass
                        received = True
                ws.close()
                self._feed_update(feed, False)
                self._disconnect_event(feed, target_url, "closed")
                attempt = 0 if received else attempt + 1
                if not self._stop.is_set():
                    self._stop.wait(min(30.0, 0.5 * (2 ** min(attempt, 6))))
            except Exception as exc:
                attempt += 1
                self._feed_update(feed, False)
                self._disconnect_event(feed, target_url, str(exc)[:240])
                now = time.time()
                if self._on_error is not None and now - self._last_error_at >= 60:
                    self._last_error_at = now
                    try:
                        self._on_error(f"{feed} {target_url}: {exc}")
                    except Exception:
                        pass
                if primary:
                    self._stop.wait(min(30.0, 0.5 * (2 ** min(attempt, 6))))
                    continue
                if self._sleep is time.sleep:
                    self._stop.wait(min(30.0, 0.5 * (2 ** min(attempt, 6))))
                else:
                    self._sleep(min(30.0, 0.5 * (2 ** min(attempt, 6))))
            finally:
                if tracked and ws is not None:
                    self._track_ws(ws, False)
                if run_url is None:
                    self._ws = None
