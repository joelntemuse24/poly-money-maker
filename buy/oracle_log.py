"""Recording-only Chainlink BTC/USD 60s TWAP tape for 15m mint windows.

Polymarket resolves btc-up-or-down-15m on Chainlink's 60-second TWAP.
This module records that relay. It must not be imported by sell or mint
decision helpers (``buy/mint_sell.py`` and the sell/mint cycles). It never
places orders and never mutates intent state. Feed failures surface as
``oracle_log_fail`` and trading continues.

Source, chosen because it needs no Chainlink Data Streams credentials:

* Live path: Polymarket RTDS ``wss://ws-live-data.polymarket.com`` topic
  ``crypto_prices_twap_sixty`` (symbol ``btc/usd``). The subscribe burst
  carries the recent 1Hz path; updates then arrive about once a second.
  ``full_accuracy_value`` is the signed 1e18 Chainlink price.
* Window open and completed close: ``GET /api/crypto/crypto-price`` with
  ``variant=fifteen``. That variant is the 15m Chainlink series. Other
  variant strings fall back to hourly Binance and are not used.
"""

from __future__ import annotations

import json
import threading
import time
from collections import deque
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any, Callable, Optional

import requests

from buy.market import slug_start_ts


RTDS_URL = "wss://ws-live-data.polymarket.com"
RTDS_TOPIC = "crypto_prices_twap_sixty"
RTDS_SYMBOL = "btc/usd"
RTDS_SOURCE = "polymarket_rtds"
HTTP_SOURCE = "polymarket_crypto_price"
CRYPTO_PRICE_URL = "https://polymarket.com/api/crypto/crypto-price"
CRYPTO_PRICE_VARIANT = "fifteen"
FIFTEEN_S = 900.0
SERIES_15M = "btc-up-or-down-15m"
E18 = 10**18

# Cadence is recording-only. These are not trading knobs.
OPEN_BAND_S = 30.0
HOT_WINDOW_S = 180.0
LAST_MIN_S = 60.0
GRACE_AFTER_S = 120.0
INTERVAL_LAST_MIN_S = 1.0
INTERVAL_HOT_S = 2.0
INTERVAL_COLD_S = 15.0
STALE_AFTER_S = 20.0
FAIL_REPEAT_S = 30.0
HTTP_RETRY_S = 20.0
IDLE_STOP_S = 30.0

BAG_STATUSES = frozenset(
    {
        "submitting",
        "pending",
        "executed",
        "mined",
        "confirmed_waiting_inventory",
        "confirmed",
        "completed",
    }
)

SUBSCRIBE_FRAME = {
    "action": "subscribe",
    "subscriptions": [
        {
            "topic": RTDS_TOPIC,
            "type": "update",
            "filters": "{\"symbol\":\"btc/usd\"}",
        }
    ],
}

FailFn = Callable[[str], None]


@dataclass(frozen=True)
class TwapSample:
    symbol: str
    window_s: int
    twap: str
    obs_ts: float
    source: str = RTDS_SOURCE


@dataclass(frozen=True)
class WindowPrice:
    open_ref: Optional[str]
    close_twap: Optional[str]
    completed: bool


@dataclass(frozen=True)
class OracleWindow:
    condition_id: str
    slug: str
    start_ts: float
    end_ts: float


def e18_to_decimal_str(raw: Any) -> Optional[str]:
    """Divide a Chainlink signed 1e18 integer string into a decimal string."""
    text = str(raw if raw is not None else "").strip()
    if not text or text[0] == "+" or not _is_signed_int(text):
        return None
    try:
        scaled = int(text)
    except ValueError:
        return None
    sign = "-" if scaled < 0 else ""
    whole, frac = divmod(abs(scaled), E18)
    frac_txt = f"{frac:018d}".rstrip("0")
    if frac_txt:
        return f"{sign}{whole}.{frac_txt}"
    return f"{sign}{whole}"


def _is_signed_int(text: str) -> bool:
    body = text[1:] if text.startswith("-") else text
    return bool(body) and body.isdigit()


def _loose_decimal_str(value: Any) -> Optional[str]:
    if isinstance(value, bool) or value is None:
        return None
    try:
        decimal = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    if not decimal.is_finite():
        return None
    text = format(decimal, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"


def _format_decimal(value: Decimal) -> str:
    text = format(value, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"


def _as_float(value: Any) -> Optional[float]:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    if parsed != parsed or parsed in (float("inf"), float("-inf")):
        return None
    return parsed


def _obs_seconds(value: Any) -> Optional[float]:
    parsed = _as_float(value)
    if parsed is None:
        return None
    if parsed > 10_000_000_000:
        return parsed / 1000.0
    return parsed


def _point_to_sample(point: Any, symbol: str) -> Optional[TwapSample]:
    if not isinstance(point, dict):
        return None
    twap = None
    if point.get("full_accuracy_value") is not None:
        twap = e18_to_decimal_str(point.get("full_accuracy_value"))
    if twap is None and point.get("value") is not None:
        twap = _loose_decimal_str(point.get("value"))
    obs = _obs_seconds(point.get("timestamp"))
    if twap is None or obs is None:
        return None
    return TwapSample(symbol=symbol, window_s=60, twap=twap, obs_ts=obs)


def parse_rtds_message(raw: Any) -> list[TwapSample]:
    """Parse one RTDS text frame into 60s btc/usd TWAP samples.

    Blank frames, PING/PONG, other symbols, and the 30s topic are ignored.
    A subscribe payload's ``data`` array is the recent path; an update
    payload is one observation.
    """
    if isinstance(raw, (bytes, bytearray)):
        raw = raw.decode("utf-8", "replace")
    if isinstance(raw, str):
        text = raw.strip()
        if not text or text.upper() in {"PING", "PONG"}:
            return []
        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            return []
    elif isinstance(raw, dict):
        payload = raw
    else:
        return []
    if not isinstance(payload, dict):
        return []
    topic = str(payload.get("topic") or "")
    if topic not in {RTDS_TOPIC, "prices.crypto.chainlink.twap"}:
        return []
    body = payload.get("payload")
    if not isinstance(body, dict):
        return []
    symbol = str(body.get("symbol") or RTDS_SYMBOL).strip().lower()
    if symbol != RTDS_SYMBOL:
        return []
    window_s = body.get("window_s", body.get("window_seconds"))
    if window_s is not None:
        try:
            window_i = int(window_s)
        except (TypeError, ValueError):
            return []
        if window_i != 60:
            return []
    elif topic != RTDS_TOPIC:
        return []
    points = body.get("data")
    if isinstance(points, list):
        samples: list[TwapSample] = []
        for point in points:
            sample = _point_to_sample(point, symbol)
            if sample is not None:
                samples.append(sample)
        return samples
    sample = _point_to_sample(body, symbol)
    return [sample] if sample is not None else []


def crypto_price_params(start_ts: int) -> dict[str, Any]:
    """15m Chainlink series. ``variant`` must stay ``fifteen``."""
    return {
        "symbol": "btc",
        "eventStartTime": int(start_ts),
        "variant": CRYPTO_PRICE_VARIANT,
    }


def parse_crypto_price_body(text: str) -> WindowPrice:
    data = json.loads(text, parse_float=Decimal, parse_int=Decimal)
    if not isinstance(data, dict):
        raise ValueError("crypto-price payload was not an object")

    def pick(key: str) -> Optional[str]:
        value = data.get(key)
        if value is None:
            return None
        if isinstance(value, Decimal):
            return _format_decimal(value)
        return _loose_decimal_str(value)

    completed = bool(data.get("completed")) and not bool(data.get("incomplete"))
    close_twap = pick("closePrice") if completed else None
    return WindowPrice(
        open_ref=pick("openPrice"),
        close_twap=close_twap,
        completed=completed,
    )


def fetch_crypto_price(start_ts: int, *, timeout: float = 3.0) -> WindowPrice:
    response = requests.get(
        CRYPTO_PRICE_URL,
        params=crypto_price_params(start_ts),
        timeout=timeout,
        headers={"User-Agent": "poly-money-maker-oracle-log/1.0"},
    )
    response.raise_for_status()
    return parse_crypto_price_body(response.text)


def sample_interval_s(now: float, start_ts: float, end_ts: float) -> float:
    """1s in the last minute and after the end, 2s near the open and last 3m, else 15s."""
    if end_ts - LAST_MIN_S <= now <= end_ts + GRACE_AFTER_S:
        return INTERVAL_LAST_MIN_S
    if start_ts - OPEN_BAND_S <= now <= start_ts + OPEN_BAND_S:
        return INTERVAL_HOT_S
    if end_ts - HOT_WINDOW_S <= now < end_ts:
        return INTERVAL_HOT_S
    return INTERVAL_COLD_S


def notes_for(obs_ts: float, start_ts: float, end_ts: float) -> str:
    if obs_ts >= end_ts:
        return "end"
    if abs(obs_ts - start_ts) <= OPEN_BAND_S:
        return "open"
    remaining = end_ts - obs_ts
    if remaining <= LAST_MIN_S:
        return "last_min"
    if remaining <= HOT_WINDOW_S:
        return "hot"
    return "cold"


def snapshot_intents(state: Any) -> dict:
    """Shallow copy of intents. Caller holds the state lock."""
    intents = state.get("intents") if isinstance(state, dict) else None
    copied: dict[str, dict] = {}
    if isinstance(intents, dict):
        for key, intent in intents.items():
            if isinstance(intent, dict):
                copied[str(key)] = dict(intent)
    return {"intents": copied}


def windows_from_intents(state: Any, now: float) -> list[OracleWindow]:
    """15m bags we hold or are about to hold, through a short post-end grace."""
    intents = state.get("intents") if isinstance(state, dict) else None
    if not isinstance(intents, dict):
        return []
    windows: list[OracleWindow] = []
    for key, intent in intents.items():
        window = _window_from_intent(key, intent, now)
        if window is not None:
            windows.append(window)
    windows.sort(key=lambda item: (item.start_ts, item.condition_id))
    return windows


def _window_from_intent(key: Any, intent: Any, now: float) -> Optional[OracleWindow]:
    if not isinstance(intent, dict):
        return None
    if str(intent.get("status") or "") not in BAG_STATUSES:
        return None
    series = str(intent.get("series_slug") or "").strip()
    if series and series != SERIES_15M:
        return None
    slug = str(intent.get("slug") or "").strip()
    slug_start = slug_start_ts(slug) if slug else None
    if slug_start is not None:
        start = float(slug_start)
        end = start + FIFTEEN_S
    else:
        if series != SERIES_15M and "15m" not in slug:
            return None
        start = _as_float(intent.get("start_ts"))
        end = _as_float(intent.get("end_ts"))
        if start is None:
            return None
        if end is None or end <= start:
            end = start + FIFTEEN_S
        if abs((end - start) - FIFTEEN_S) > 5:
            return None
    if now > end + GRACE_AFTER_S:
        return None
    condition_id = str(intent.get("condition_id") or key or "").strip()
    if not condition_id:
        return None
    return OracleWindow(
        condition_id=condition_id,
        slug=slug or condition_id,
        start_ts=start,
        end_ts=end,
    )


def _in_span(obs_ts: float, window: OracleWindow) -> bool:
    return (window.start_ts - OPEN_BAND_S) <= obs_ts <= (window.end_ts + GRACE_AFTER_S)


def build_oracle_row(
    *,
    now: float,
    window: Optional[OracleWindow],
    source: str,
    event: str,
    twap: Optional[str],
    twap_ts: Optional[float],
    open_ref: Optional[str],
    notes: str,
    error: Optional[str] = None,
) -> dict[str, Any]:
    row: dict[str, Any] = {
        "ts": now,
        "event": event,
        "slug": window.slug if window is not None else None,
        "condition_id": window.condition_id if window is not None else None,
        "window_start": window.start_ts if window is not None else None,
        "window_end": window.end_ts if window is not None else None,
        "source": source,
        "symbol": RTDS_SYMBOL,
        "window_s": 60,
        "twap": twap,
        "twap_ts": twap_ts,
        "open_ref": open_ref,
        "notes": notes,
    }
    if error:
        row["error"] = error[:240]
    return row


def append_jsonl(path: Any, row: dict) -> None:
    from pathlib import Path

    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    with open(target, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(row, separators=(",", ":"), sort_keys=True) + "\n")
        handle.flush()


class RtdsTwapFeed:
    """Background RTDS subscription. ``handle_message`` is the test seam."""

    def __init__(self, url: str = RTDS_URL) -> None:
        self.url = url
        self._lock = threading.Lock()
        self._latest: Optional[TwapSample] = None
        self._backlog: list[TwapSample] = []
        self._last_error = ""
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._ws: Any = None

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run,
            name="oracle-rtds",
            daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        ws = self._ws
        if ws is not None:
            try:
                ws.close()
            except Exception:
                return

    def latest(self) -> Optional[TwapSample]:
        with self._lock:
            return self._latest

    def drain(self) -> list[TwapSample]:
        with self._lock:
            items = self._backlog
            self._backlog = []
            return items

    def last_error(self) -> str:
        with self._lock:
            return self._last_error

    def handle_message(self, raw: Any) -> None:
        text = raw.decode("utf-8", "replace") if isinstance(raw, (bytes, bytearray)) else str(raw)
        try:
            samples = parse_rtds_message(text)
        except Exception as exc:
            self._set_error(str(exc)[:240])
            return
        if samples:
            with self._lock:
                self._backlog.extend(samples)
                if len(self._backlog) > 500:
                    self._backlog = self._backlog[-500:]
                newest = max(samples, key=lambda sample: sample.obs_ts)
                if self._latest is None or newest.obs_ts >= self._latest.obs_ts:
                    self._latest = newest
                self._last_error = ""
            return
        stripped = text.strip()
        if not stripped or stripped.upper() in {"PING", "PONG"}:
            return
        lowered = stripped.lower()
        if "error" in lowered or "not found" in lowered:
            self._set_error(stripped[:240])

    def _set_error(self, message: str) -> None:
        with self._lock:
            self._last_error = message

    def _run(self) -> None:
        import websocket

        backoff = 1.0
        while not self._stop.is_set():
            try:
                self._run_once(websocket)
                backoff = 1.0
            except Exception as exc:
                self._set_error(str(exc)[:240])
            if self._stop.is_set():
                return
            self._stop.wait(backoff)
            backoff = min(30.0, backoff * 2.0)

    def _run_once(self, websocket: Any) -> None:
        ping_stop = threading.Event()

        def on_open(ws: Any) -> None:
            self._ws = ws
            self._set_error("")
            ws.send(json.dumps(SUBSCRIBE_FRAME))

        def on_message(ws: Any, message: Any) -> None:
            del ws
            self.handle_message(message)

        def on_error(ws: Any, error: Any) -> None:
            del ws
            self._set_error(str(error)[:240])

        def on_close(ws: Any, code: Any, reason: Any) -> None:
            del ws, code, reason
            self._ws = None

        app = websocket.WebSocketApp(
            self.url,
            on_open=on_open,
            on_message=on_message,
            on_error=on_error,
            on_close=on_close,
        )
        self._ws = app

        def ping() -> None:
            while not ping_stop.is_set() and not self._stop.is_set():
                if ping_stop.wait(5.0):
                    return
                try:
                    app.send("PING")
                except Exception:
                    return

        threading.Thread(target=ping, name="oracle-rtds-ping", daemon=True).start()
        try:
            app.run_forever(ping_interval=0)
        finally:
            ping_stop.set()
            self._ws = None


@dataclass
class _WindowMemory:
    last_sample_wall: float = 0.0
    open_ref: Optional[str] = None
    open_logged: bool = False
    end_logged: bool = False
    last_http: float = 0.0


class OracleLogService:
    """Sample the TWAP tape for open 15m bags. ``tick`` does not raise."""

    def __init__(
        self,
        path: Any,
        *,
        feed: Any = None,
        fetch_price: Optional[Callable[[int], WindowPrice]] = None,
    ) -> None:
        self.path = path
        self._feed = feed if feed is not None else RtdsTwapFeed()
        self._fetch_price = fetch_price if fetch_price is not None else fetch_crypto_price
        self.sleep_s = 5.0
        self._memory: dict[str, _WindowMemory] = {}
        self._seen: set[tuple[Any, ...]] = set()
        self._seen_order: deque[tuple[Any, ...]] = deque()
        self._last_fail_wall = 0.0
        self._last_fail_text = ""
        self._idle_since: Optional[float] = None
        self._awaiting_since: Optional[float] = None
        self._on_fail: Optional[FailFn] = None

    def tick(
        self,
        state: Any,
        now: float,
        *,
        enabled: bool = True,
        on_fail: Optional[FailFn] = None,
    ) -> None:
        self._on_fail = on_fail
        try:
            self._tick(state, now, enabled=enabled)
        except Exception as exc:
            self._fail(f"tick: {exc}", now=now, window=None)
            self.sleep_s = 5.0

    def _tick(self, state: Any, now: float, *, enabled: bool) -> None:
        if not enabled:
            self._stop_feed()
            self.sleep_s = 5.0
            self._idle_since = None
            self._awaiting_since = None
            return
        windows = windows_from_intents(state, now)
        if not windows:
            self.sleep_s = 5.0
            self._awaiting_since = None
            self._note_idle(now)
            return
        self._idle_since = None
        self._feed.start()
        # Wake every second while a bag is open so a cold 15s gap cannot
        # skip the open or the last minute. Stored rows stay on the slower
        # cadence until the window is hot.
        self.sleep_s = INTERVAL_LAST_MIN_S
        try:
            samples = list(self._feed.drain())
            latest = self._feed.latest()
        except Exception as exc:
            self._fail(f"feed: {exc}", now=now, window=windows[0])
            samples = []
            latest = None
        for window in windows:
            self._record_window(window, samples, latest, now)
        self._note_silence(windows[0], latest, samples, now)

    def _stop_feed(self) -> None:
        stop = getattr(self._feed, "stop", None)
        if stop is not None:
            stop()

    def _note_idle(self, now: float) -> None:
        if self._idle_since is None:
            self._idle_since = now
            return
        if now - self._idle_since >= IDLE_STOP_S:
            self._stop_feed()

    def _note_silence(
        self,
        window: OracleWindow,
        latest: Optional[TwapSample],
        samples: list[TwapSample],
        now: float,
    ) -> None:
        if latest is not None or samples:
            self._awaiting_since = None
            if latest is not None and now - latest.obs_ts > STALE_AFTER_S:
                self._fail(
                    f"stale twap age={now - latest.obs_ts:.0f}s",
                    now=now,
                    window=window,
                )
            return
        if self._awaiting_since is None:
            self._awaiting_since = now
            return
        if now - self._awaiting_since < STALE_AFTER_S:
            return
        error = ""
        last_error = getattr(self._feed, "last_error", None)
        if last_error is not None:
            try:
                error = str(last_error() or "")
            except Exception as exc:
                error = str(exc)
        self._fail(error or "no twap sample", now=now, window=window)

    def _record_window(
        self,
        window: OracleWindow,
        samples: list[TwapSample],
        latest: Optional[TwapSample],
        now: float,
    ) -> None:
        memory = self._memory.setdefault(window.condition_id, _WindowMemory())
        self._maybe_http(window, memory, now)
        interval = sample_interval_s(now, window.start_ts, window.end_ts)
        dense = interval <= INTERVAL_HOT_S
        if dense:
            for sample in samples:
                if _in_span(sample.obs_ts, window):
                    self._write_sample(window, memory, sample, now, stale=False)
            if latest is not None and _in_span(latest.obs_ts, window):
                stale = now - latest.obs_ts > STALE_AFTER_S
                self._write_sample(window, memory, latest, now, stale=stale)
        elif latest is not None and _in_span(latest.obs_ts, window):
            due = now - memory.last_sample_wall >= interval
            if due:
                stale = now - latest.obs_ts > STALE_AFTER_S
                wrote = self._write_sample(window, memory, latest, now, stale=stale)
                if wrote:
                    memory.last_sample_wall = now

    def _write_sample(
        self,
        window: OracleWindow,
        memory: _WindowMemory,
        sample: TwapSample,
        now: float,
        *,
        stale: bool,
    ) -> bool:
        key = (window.condition_id, "rtds", int(round(sample.obs_ts * 1000.0)))
        if not self._remember(key):
            return False
        notes = notes_for(sample.obs_ts, window.start_ts, window.end_ts)
        if stale:
            notes = f"{notes}; stale"
        self._append(
            build_oracle_row(
                now=now,
                window=window,
                source=sample.source,
                event="oracle_twap",
                twap=sample.twap,
                twap_ts=sample.obs_ts,
                open_ref=memory.open_ref,
                notes=notes,
            )
        )
        return True

    def _maybe_http(self, window: OracleWindow, memory: _WindowMemory, now: float) -> None:
        need_open = memory.open_ref is None and now >= window.start_ts
        need_end = (not memory.end_logged) and now >= window.end_ts
        if not need_open and not need_end:
            return
        if memory.last_http and now - memory.last_http < HTTP_RETRY_S:
            return
        memory.last_http = now
        try:
            price = self._fetch_price(int(window.start_ts))
        except Exception as exc:
            self._fail(f"crypto-price: {exc}", now=now, window=window)
            return
        if not isinstance(price, WindowPrice):
            self._fail("crypto-price: bad payload", now=now, window=window)
            return
        if price.open_ref and not memory.open_logged:
            memory.open_ref = price.open_ref
            memory.open_logged = True
            self._append(
                build_oracle_row(
                    now=now,
                    window=window,
                    source=HTTP_SOURCE,
                    event="oracle_open_ref",
                    twap=price.open_ref,
                    twap_ts=window.start_ts,
                    open_ref=price.open_ref,
                    notes="open_ref",
                )
            )
        elif price.open_ref and memory.open_ref is None:
            memory.open_ref = price.open_ref
        if need_end and price.completed and price.close_twap and not memory.end_logged:
            memory.end_logged = True
            if memory.open_ref is None and price.open_ref:
                memory.open_ref = price.open_ref
            self._append(
                build_oracle_row(
                    now=now,
                    window=window,
                    source=HTTP_SOURCE,
                    event="oracle_window_end",
                    twap=price.close_twap,
                    twap_ts=window.end_ts,
                    open_ref=memory.open_ref,
                    notes="window_end",
                )
            )

    def _remember(self, key: tuple[Any, ...]) -> bool:
        if key in self._seen:
            return False
        self._seen.add(key)
        self._seen_order.append(key)
        while len(self._seen_order) > 8000:
            old = self._seen_order.popleft()
            self._seen.discard(old)
        return True

    def _append(self, row: dict) -> None:
        try:
            append_jsonl(self.path, row)
        except Exception as exc:
            if self._on_fail is not None:
                try:
                    self._on_fail(f"write: {exc}"[:240])
                except Exception:
                    return

    def _fail(self, message: str, *, now: float, window: Optional[OracleWindow]) -> None:
        text = " ".join(str(message).split())[:240] or "oracle_log_fail"
        if text == self._last_fail_text and now - self._last_fail_wall < FAIL_REPEAT_S:
            return
        self._last_fail_wall = now
        self._last_fail_text = text
        try:
            append_jsonl(
                self.path,
                build_oracle_row(
                    now=now,
                    window=window,
                    source=RTDS_SOURCE,
                    event="oracle_log_fail",
                    twap=None,
                    twap_ts=None,
                    open_ref=None,
                    notes="oracle_log_fail",
                    error=text,
                ),
            )
        except Exception:
            pass
        if self._on_fail is None:
            return
        try:
            self._on_fail(text)
        except Exception:
            return
