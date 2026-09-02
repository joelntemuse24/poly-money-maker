"""Underlying BTC feeds for trading gates and optional resolution logging.

Trading gates (entry, hedge, complement) compare **last live BTC** to the
window-open Price To Beat. They must never treat a 30s/60s/any TWAP as live.

| Bot / window | Trading live vs PTB | RTDS topic |
|---|---|---|
| 5m (`buybot5m`) | Chainlink BTC/USD last | `crypto_prices_chainlink` `btc/usd` |
| 15m (`buybot`) | Chainlink BTC/USD last | `crypto_prices_chainlink` `btc/usd` |
| Hourly (`buybothourly`) | Binance BTC/USDT last | `crypto_prices` `btcusdt` |

TWAP 30s/60s sources remain for optional resolution-feed logging only.
`underlying_check` refuses them (`twap_not_live`).

Refs:
- https://docs.polymarket.com/market-data/realtime
- https://data.chain.link/streams/btc-usd-twap-30s-streams
- https://data.chain.link/streams/btc-usd-twap-60s-streams
- https://www.binance.com/en/trade/BTC_USDT?type=spot
- https://docs.polymarket.com/market-data/chainlink-twap

Price To Beat = nearest last-print tick from that same trading feed to
market `start_ts` (≤2s skew). If the bot missed the open, PTB is missing
and buys are refused.
"""

from __future__ import annotations

import json
import logging
import math
import os
import threading
import time
from collections import deque
from typing import Any, Deque, Dict, Optional, Tuple

log = logging.getLogger("btc_price")

RTDS_URL = "wss://ws-live-data.polymarket.com"

# Keep ~3h of ~1Hz ticks so windows still resolve PTB after brief blips.
RING_MAX_SAMPLES = 12_000
PTB_MAX_SKEW_MS = 2000
LIVE_STALE_S = 5.0
PTB_CACHE_MAX = 512
RESEARCH_MAX_BYTES = 50 * 1024 * 1024  # 50 MiB per research file
RESEARCH_BACKUP_COUNT = 2

# Last-print sources used by trading gates (entry / hedge / complement).
SOURCE_CHAINLINK = "chainlink"  # 5m + 15m last BTC/USD vs window-open PTB
SOURCE_BINANCE = "binance"  # hourly last BTCUSDT vs window-open PTB

# Resolution-feed logging only. Not a trading live quote.
SOURCE_TWAP_30 = "twap_30"
SOURCE_TWAP_60 = "twap_60"

LIVE_KIND_LAST_PRINT = "last_print"
LIVE_KIND_TWAP = "twap"

SOURCE_META = {
    SOURCE_CHAINLINK: {
        "label": "chainlink_btc_usd",
        "resolution_url": "https://data.chain.link/streams",
        "rtds_topic": "crypto_prices_chainlink",
        "rtds_type": "*",
        # Filter string form is unreliable on RTDS; subscribe broadly and filter by symbol.
        "filters": None,
        "symbol": "btc/usd",
        "live_kind": LIVE_KIND_LAST_PRINT,
    },
    SOURCE_BINANCE: {
        "label": "binance_btcusdt",
        "resolution_url": "https://www.binance.com/en/trade/BTC_USDT?type=spot",
        # Filter string form is unreliable on RTDS; subscribe broadly and filter by symbol.
        "rtds_topic": "crypto_prices",
        "rtds_type": "*",
        "filters": None,
        "symbol": "btcusdt",
        "live_kind": LIVE_KIND_LAST_PRINT,
    },
    SOURCE_TWAP_30: {
        "label": "chainlink_twap_30s",
        "resolution_url": "https://data.chain.link/streams/btc-usd-twap-30s-streams",
        "rtds_topic": "crypto_prices_twap_thirty",
        "rtds_type": "update",
        "filters": '{"symbol":"btc/usd"}',
        "symbol": "btc/usd",
        "live_kind": LIVE_KIND_TWAP,
    },
    SOURCE_TWAP_60: {
        "label": "chainlink_twap_60s",
        "resolution_url": "https://data.chain.link/streams/btc-usd-twap-60s-streams",
        "rtds_topic": "crypto_prices_twap_sixty",
        "rtds_type": "update",
        "filters": '{"symbol":"btc/usd"}',
        "symbol": "btc/usd",
        "live_kind": LIVE_KIND_TWAP,
    },
}


def live_kind_for(source: str) -> str:
    meta = SOURCE_META.get(source) or {}
    return str(meta.get("live_kind") or "")


def is_last_print_source(source: str) -> bool:
    return live_kind_for(source) == LIVE_KIND_LAST_PRINT


def require_last_print_source(source: str) -> str:
    """Refuse TWAP (or unknown) sources as a trading oracle."""
    kind = live_kind_for(source)
    if kind != LIVE_KIND_LAST_PRINT:
        raise ValueError(
            f"trading oracle {source!r} must be last-print vs PTB, not {kind or 'unknown'}"
        )
    return source


def side_from_live_vs_ptb(
    live: float,
    ptb: float,
    min_edge_usd: float,
) -> Dict[str, Any]:
    """Favored Up/Down from last live BTC vs window-open PTB. No averaging."""
    out: Dict[str, Any] = {
        "ok": False,
        "favored": None,
        "ptb": float(ptb),
        "live_btc": float(live),
        "edge_usd": None,
        "reason": None,
    }
    try:
        ptb_f = float(ptb)
        live_f = float(live)
    except (TypeError, ValueError):
        out["reason"] = "non_finite_price"
        return out
    if not math.isfinite(ptb_f) or not math.isfinite(live_f):
        out["reason"] = "non_finite_price"
        return out
    edge = live_f - ptb_f
    out["edge_usd"] = edge
    # Fail-closed: flat underlying never picks a side (even if min_edge is 0).
    if edge == 0:
        out["reason"] = "edge_zero"
        return out
    if abs(edge) < float(min_edge_usd or 0):
        out["reason"] = "edge_too_small"
        return out
    out["ok"] = True
    out["favored"] = "up" if edge > 0 else "down"
    out["reason"] = None
    return out


def append_research(path: str, record: Dict[str, Any]) -> None:
    """Append one JSON line for offline correlation / regression.

    Rotates when the file exceeds RESEARCH_MAX_BYTES (keeps RESEARCH_BACKUP_COUNT
    backups) so sustained skip loops cannot fill the disk.
    """
    try:
        row = dict(record)
        row.setdefault("logged_at", time.time())
        if path:
            try:
                if os.path.exists(path) and os.path.getsize(path) >= RESEARCH_MAX_BYTES:
                    for i in range(RESEARCH_BACKUP_COUNT, 0, -1):
                        src = path if i == 1 else f"{path}.{i - 1}"
                        dst = f"{path}.{i}"
                        if os.path.exists(src):
                            os.replace(src, dst)
            except OSError:
                pass
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(row, separators=(",", ":")) + "\n")
    except Exception as e:
        log.debug("research_log_fail: %s", e)


class BtcUnderlyingFeed:
    """Ring buffer + PTB capture for one resolution source."""

    def __init__(self, source: str, ptb_store_path: str):
        if source not in SOURCE_META:
            raise ValueError(f"unknown underlying source: {source}")
        self.source = source
        self.meta = SOURCE_META[source]
        self._live: Optional[float] = None
        self._live_ts: float = 0.0
        self._live_received_mono: float = 0.0
        self._live_src: str = "none"
        self._latest_observation_ms: int = 0
        self._ticks: Deque[Tuple[int, float]] = deque(maxlen=RING_MAX_SAMPLES)
        self._ptb: Dict[int, Dict[str, Any]] = {}
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
        self._thread = threading.Thread(
            target=self._run,
            name=f"btc-feed-{self.source}",
            daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def live_quote(self) -> Tuple[Optional[float], str, Optional[float]]:
        """Return (price, source_label, age_s). Memory only.

        On a last-print source this is the last tick, not a TWAP. TWAP
        sources still return their rolling average here for logging, but
        ``underlying_check`` refuses them as a trading live quote.
        """
        with self._lock:
            live = self._live
            received_mono = self._live_received_mono
            src = self._live_src
        if live is None:
            return None, "none", None
        age = time.monotonic() - received_mono
        return live, src, age if age >= 0 else None

    def live_price(self, *, allow_stale: bool = False) -> Optional[float]:
        px, _src, age = self.live_quote()
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
        key = int(start_ts)
        with self._lock:
            cached = self._ptb.get(key)
        if cached is not None:
            return dict(cached)
        return self.capture_ptb(start_ts)

    def capture_ptb(self, start_ts: float) -> Optional[Dict[str, Any]]:
        """Lock PTB from this feed's ticks nearest to start_ts."""
        key = int(start_ts)
        target_ms = key * 1000
        now = time.time()
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
                    "feed": self.source,
                    "feed_label": self.meta["label"],
                    "resolution_url": self.meta["resolution_url"],
                    "captured_at": now,
                }
                # RTDS has no snapshot/replay for TWAP streams. A feed that
                # connects just after market open can briefly have no tick;
                # do not permanently poison this market's PTB cache.
                return dict(rec)

            best_ts, best_px = min(self._ticks, key=lambda t: abs(t[0] - target_ms))
            skew_ms = abs(best_ts - target_ms)
            ok = skew_ms <= PTB_MAX_SKEW_MS
            label = self.meta["label"]
            rec = {
                "ok": ok,
                "ptb": float(best_px),
                "ptb_tick_ts_ms": int(best_ts),
                "ptb_skew_ms": int(skew_ms),
                "start_ts": key,
                "source": label if ok else f"{label}_skewed",
                "feed": self.source,
                "feed_label": label,
                "resolution_url": self.meta["resolution_url"],
                "reason": None if ok else "skew_too_large",
                "captured_at": now,
            }
            if ok:
                self._ptb[key] = rec
                self._trim_ptb_unlocked()
                self._save_ptb_store_unlocked()
            return dict(rec)

    def underlying_check(self, start_ts: float, min_edge_usd: float) -> Dict[str, Any]:
        """Fast memory-only trading gate: last live BTC vs window-open PTB."""
        ptb_rec = self.ptb_record(start_ts) or {}
        live, live_src, live_age = self.live_quote()
        ptb = ptb_rec.get("ptb") if ptb_rec.get("ok") else None
        live_kind = str(self.meta.get("live_kind") or "")
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
            "live_kind": live_kind,
            "feed": self.source,
            "feed_label": self.meta["label"],
            "resolution_url": self.meta["resolution_url"],
        }
        if live_kind != LIVE_KIND_LAST_PRINT:
            out["reason"] = "twap_not_live"
            return out
        if ptb is None or live is None:
            out["reason"] = "missing_ptb" if ptb is None else "missing_live"
            return out
        if live_age is None or not math.isfinite(live_age) or live_age < 0:
            out["reason"] = "invalid_live_age"
            return out
        if live_age > LIVE_STALE_S:
            out["reason"] = "live_stale"
            return out
        side = side_from_live_vs_ptb(live, ptb, min_edge_usd)
        out["ok"] = bool(side.get("ok"))
        out["favored"] = side.get("favored")
        out["edge_usd"] = side.get("edge_usd")
        out["reason"] = side.get("reason")
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
                        key = int(k)
                        if not isinstance(v, dict) or v.get("ok") is not True:
                            continue
                        ptb = float(v.get("ptb"))
                        tick_ts_ms = int(v.get("ptb_tick_ts_ms"))
                        start_ts = int(v.get("start_ts"))
                        if (
                            not math.isfinite(ptb)
                            or not 100 <= ptb <= 10_000_000
                            or key != start_ts
                            or abs(tick_ts_ms - key * 1000) > PTB_MAX_SKEW_MS
                            or v.get("source") != self.meta["label"]
                            or (
                                v.get("feed") is not None
                                and v.get("feed") != self.source
                            )
                        ):
                            continue
                        self._ptb[key] = dict(v)
                    except (TypeError, ValueError, OverflowError):
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
                json.dump(
                    self._ptb, f, separators=(",", ":"), allow_nan=False,
                )
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, path)
            parent = os.path.dirname(os.path.abspath(path)) or "."
            dir_fd = os.open(parent, os.O_RDONLY)
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
        except Exception as e:
            log.debug("ptb_store_save_fail: %s", e)

    def _push_tick(self, ts_ms: int, value: float, *, live: bool) -> bool:
        try:
            ts_ms = int(ts_ms)
            value = float(value)
        except (TypeError, ValueError, OverflowError):
            return False
        now_ms = int(time.time() * 1000)
        if (
            not math.isfinite(value)
            or not 100 <= value <= 10_000_000
            or ts_ms <= 0
            or ts_ms > now_ms + 2_000
            or ts_ms < now_ms - (4 * 60 * 60 * 1000)
            or (live and ts_ms < now_ms - 10_000)
        ):
            return False
        with self._lock:
            self._ticks.append((ts_ms, value))
            if live and ts_ms >= self._latest_observation_ms:
                self._latest_observation_ms = ts_ms
                self._live = value
                self._live_ts = ts_ms / 1000.0
                self._live_received_mono = time.monotonic()
                self._live_src = self.meta["label"]
        return True

    def _run(self) -> None:
        while not self._stop.is_set():
            self._run_rtds_once()
            # Brief pause before reconnect; no alternate HTTP oracle (sources must match resolution).
            self._stop.wait(1.0)

    def _run_rtds_once(self) -> bool:
        try:
            import websocket  # type: ignore
        except ImportError:
            log.error("websocket-client required for underlying feed %s", self.source)
            return False

        meta = self.meta
        got = {"ok": False}
        want_symbol = meta["symbol"]
        opened_mono = {"value": 0.0}
        last_valid_mono = {"value": 0.0}
        ping_stop = threading.Event()

        def on_message(ws, message):
            if message == "PONG" or not message:
                return
            try:
                data = json.loads(message)
            except Exception:
                return
            topic = data.get("topic") or ""
            payload = data.get("payload") or {}

            # Live update
            if "value" in payload and "data" not in payload:
                if topic != meta["rtds_topic"]:
                    return
                sym = str(payload.get("symbol") or "").lower()
                if sym != want_symbol:
                    return
                try:
                    ts_ms = int(payload["timestamp"])
                    if self._push_tick(ts_ms, float(payload["value"]), live=True):
                        got["ok"] = True
                        last_valid_mono["value"] = time.monotonic()
                except (TypeError, ValueError, OverflowError):
                    pass
                return

            # Historical dump on subscribe
            hist = payload.get("data")
            if isinstance(hist, list) and hist:
                if topic != meta["rtds_topic"]:
                    return
                dump_sym = str(payload.get("symbol") or "").lower()
                if dump_sym != want_symbol:
                    return
                sortable = []
                for point in hist:
                    if not isinstance(point, dict):
                        continue
                    try:
                        sortable.append((int(point["timestamp"]), point))
                    except (TypeError, ValueError, OverflowError, KeyError):
                        continue
                for ts_ms, point in sorted(sortable, key=lambda item: item[0]):
                    try:
                        if self._push_tick(ts_ms, float(point["value"]), live=False):
                            got["ok"] = True
                    except (TypeError, ValueError, OverflowError, KeyError):
                        continue
                return

        def on_open(ws):
            opened_mono["value"] = time.monotonic()
            last_valid_mono["value"] = opened_mono["value"]
            sub = {
                "topic": meta["rtds_topic"],
                "type": meta["rtds_type"],
            }
            if meta.get("filters") is not None:
                sub["filters"] = meta["filters"]
            ws.send(json.dumps({"action": "subscribe", "subscriptions": [sub]}))

            def pinger():
                while not self._stop.is_set() and not ping_stop.wait(5.0):
                    try:
                        now_mono = time.monotonic()
                        if now_mono - last_valid_mono["value"] > 15.0:
                            ws.close()
                            break
                        ws.send("PING")
                    except Exception:
                        break

            threading.Thread(
                target=pinger,
                name=f"btc-ping-{self.source}",
                daemon=True,
            ).start()

        def on_error(ws, error):
            log.debug("rtds_error[%s]: %s", self.source, error)

        ws = websocket.WebSocketApp(
            RTDS_URL,
            on_message=on_message,
            on_open=on_open,
            on_error=on_error,
        )

        try:
            ws.run_forever(ping_interval=None)
        except Exception as e:
            log.debug("rtds_run_fail[%s]: %s", self.source, e)
        ping_stop.set()
        try:
            ws.close()
        except Exception:
            pass
        return got["ok"]


_FEEDS: Dict[str, BtcUnderlyingFeed] = {}
_FEEDS_LOCK = threading.Lock()


def get_btc_feed(source: str, ptb_store_path: str) -> BtcUnderlyingFeed:
    """Return a process-wide feed for this resolution source."""
    key = f"{source}:{ptb_store_path}"
    with _FEEDS_LOCK:
        feed = _FEEDS.get(key)
        if feed is None:
            feed = BtcUnderlyingFeed(source=source, ptb_store_path=ptb_store_path)
            feed.start()
            _FEEDS[key] = feed
        return feed
