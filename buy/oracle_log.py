"""Chainlink BTC/USD 60s TWAP tape for 15m mint windows.

Polymarket resolves btc-up-or-down-15m on Chainlink's 60-second TWAP.
This module records that relay and exposes a read-only bag snapshot for
mintbot's late-window loser-scrap veto. It must not place orders and must
not mutate intent state. Feed failures surface as ``oracle_log_fail``;
outside the late scrap gate, trading continues without the tape.

Source, chosen because it needs no Chainlink Data Streams credentials:

* Live path: Polymarket RTDS ``wss://ws-live-data.polymarket.com`` topic
  ``crypto_prices_twap_sixty`` (symbol ``btc/usd``). The subscribe burst
  carries the recent 1Hz path; updates then arrive about once a second.
  ``full_accuracy_value`` is the signed 1e18 Chainlink price.
* Window open and completed close: ``GET /api/crypto/crypto-price`` with
  ``variant=fifteen``. That variant is the 15m Chainlink series. Other
  variant strings fall back to hourly Binance and are not used.

Feed health: protocol ping/pong plus a silence watchdog reconnect the
socket when no sample arrives for ``FEED_SILENT_RECONNECT_S``. A stall is
logged once when it starts (``oracle_feed_stall``), at most once a minute
while it lasts, and once when it ends (``oracle_feed_recovered``).
crypto-price errors are logged once per window per kind; a 429 backs off.
"""

from __future__ import annotations

import json
import random
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any, Callable, Optional

from buy.chain import thread_session
from buy.log_archive import roll_if_over
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
# The tape rolls into logs/archive at this size, gzipped, never pruned.
TAPE_MAX_BYTES = 20_000_000
STALE_AFTER_S = 20.0
FAIL_REPEAT_S = 30.0
HTTP_RETRY_S = 20.0
IDLE_STOP_S = 30.0
# crypto-price close fetch. The close usually shows ``completed`` 60-100s
# after the end; until then the reply is ``incomplete`` (a normal wait).
# Only the close fetch uses the longer grace; sampling still stops at
# GRACE_AFTER_S.
CLOSE_GRACE_S = 300.0
HTTP_BACKOFF_CAP_S = 120.0
HTTP_JITTER_S = 3.0
# RTDS feed health. The socket can stay connected but silent, so a
# watchdog closes it after this long without a sample while a bag is
# tracked. Forced reconnects back off 45s -> 90s -> 120s cap.
FEED_SILENT_RECONNECT_S = 45.0
FEED_WATCHDOG_CAP_S = 120.0
FEED_PING_INTERVAL_S = 20.0
FEED_PING_TIMEOUT_S = 10.0
FEED_BACKOFF_MAX_S = 30.0
STALL_REMIND_S = 60.0
FEED_EVENT_REPEAT_S = 60.0

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
EventFn = Callable[[str, dict], None]


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
class OracleBagView:
    """Read-only TWAP + window-open snapshot for the late loser-scrap gate."""

    twap: Optional[str]
    open_usd: Optional[str]
    obs_ts: Optional[float]
    source: str = RTDS_SOURCE


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


class CryptoPriceHTTPError(RuntimeError):
    """Non-2xx crypto-price reply. ``retry_after`` is seconds, if sent."""

    def __init__(self, status: int, retry_after: Optional[float] = None, body: str = "") -> None:
        self.status = int(status)
        self.retry_after = retry_after
        super().__init__(f"HTTP {self.status}" + (f" {body[:120]}" if body else ""))


def parse_retry_after(value: Any) -> Optional[float]:
    """Seconds from a Retry-After header (delta-seconds or HTTP date)."""
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    seconds = _as_float(text)
    if seconds is None:
        from email.utils import parsedate_to_datetime

        try:
            when = parsedate_to_datetime(text)
        except (TypeError, ValueError, IndexError):
            return None
        if when is None:
            return None
        seconds = when.timestamp() - time.time()
    return max(0.0, seconds)


def fetch_crypto_price(start_ts: int, *, timeout: float = 3.0) -> WindowPrice:
    response = thread_session("crypto_price").get(
        CRYPTO_PRICE_URL,
        params=crypto_price_params(start_ts),
        timeout=timeout,
        headers={"User-Agent": "poly-money-maker-oracle-log/1.0"},
    )
    status = int(getattr(response, "status_code", 200) or 200)
    if status >= 400:
        headers = getattr(response, "headers", None) or {}
        raise CryptoPriceHTTPError(
            status,
            parse_retry_after(headers.get("Retry-After")),
            str(getattr(response, "text", "") or "").strip(),
        )
    return parse_crypto_price_body(response.text)


def classify_http_error(exc: BaseException) -> tuple[str, Optional[float]]:
    """``(kind, retry_after)``: ``http_429``, ``http_400``, ..., or ``error``."""
    status = getattr(exc, "status", None)
    retry_after = getattr(exc, "retry_after", None)
    response = getattr(exc, "response", None)
    if status is None and response is not None:
        status = getattr(response, "status_code", None)
        headers = getattr(response, "headers", None) or {}
        try:
            retry_after = parse_retry_after(headers.get("Retry-After"))
        except Exception:
            retry_after = None
    try:
        code = int(status) if status is not None else None
    except (TypeError, ValueError):
        code = None
    if code is None:
        return "error", None
    return f"http_{code}", retry_after if code == 429 else None


def http_retry_delay_s(kind: str, n429: int, retry_after: Optional[float]) -> float:
    """20s normally. The n-th consecutive 429 waits 20, 40, 80, then 120s,
    or longer if the server sent Retry-After."""
    if kind != "http_429":
        return HTTP_RETRY_S
    delay = min(HTTP_BACKOFF_CAP_S, HTTP_RETRY_S * (2 ** max(0, n429 - 1)))
    if retry_after is not None:
        delay = max(delay, float(retry_after))
    return delay


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


def windows_from_intents(
    state: Any, now: float, *, grace_s: float = GRACE_AFTER_S
) -> list[OracleWindow]:
    """15m bags we hold or are about to hold, through a short post-end grace."""
    intents = state.get("intents") if isinstance(state, dict) else None
    if not isinstance(intents, dict):
        return []
    windows: list[OracleWindow] = []
    for key, intent in intents.items():
        window = _window_from_intent(key, intent, now, grace_s)
        if window is not None:
            windows.append(window)
    windows.sort(key=lambda item: (item.start_ts, item.condition_id))
    return windows


def _window_from_intent(
    key: Any, intent: Any, now: float, grace_s: float = GRACE_AFTER_S
) -> Optional[OracleWindow]:
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
    if now > end + grace_s:
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
    extra: Optional[dict] = None,
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
    if extra:
        for key, value in extra.items():
            row.setdefault(key, value)
    return row


def append_jsonl(path: Any, row: dict, *, max_bytes: int = 0) -> None:
    """Append one row. With ``max_bytes`` > 0 the tape rolls into
    ``<dir>/archive/<name>.<UTC stamp>.gz`` first, like ``mintbot.log``."""
    from pathlib import Path

    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    data = (json.dumps(row, separators=(",", ":"), sort_keys=True) + "\n").encode("utf-8")
    if max_bytes > 0:
        roll_if_over(target, len(data), max_bytes)
    with open(target, "ab") as handle:
        handle.write(data)
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
        self._events: deque[dict] = deque(maxlen=50)
        self._force_reason = ""
        self._conn_samples = 0
        self._reconnects = 0

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

    def reconnect(self, reason: str) -> bool:
        """Close the live socket so ``_run`` reconnects. False if none is open."""
        ws = self._ws
        if ws is None:
            return False
        with self._lock:
            self._force_reason = str(reason)[:120]
        try:
            ws.close()
        except Exception as exc:
            self._set_error(str(exc)[:240])
            return False
        return True

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

    def pop_events(self) -> list[dict]:
        with self._lock:
            items = list(self._events)
            self._events.clear()
            return items

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
                self._conn_samples += len(samples)
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
            with self._lock:
                self._conn_samples = 0
                self._force_reason = ""
            try:
                self._run_once(websocket)
            except Exception as exc:
                self._set_error(str(exc)[:240])
            if self._stop.is_set():
                return
            wait, backoff = self._after_disconnect(backoff)
            self._stop.wait(wait)

    def _after_disconnect(self, backoff: float) -> tuple[float, float]:
        """Queue one reconnect event. Returns ``(wait_s, next_backoff)``.

        A connection that delivered samples reconnects after 1s and resets
        the backoff. One that delivered nothing (silent, refused or closed
        early) waits the current backoff, which doubles up to
        ``FEED_BACKOFF_MAX_S``."""
        with self._lock:
            delivered = self._conn_samples > 0
            reason = self._force_reason or ("closed" if delivered else "no_samples")
            if delivered:
                self._reconnects = 0
                wait, next_backoff = 1.0, 2.0
            else:
                self._reconnects += 1
                wait = min(FEED_BACKOFF_MAX_S, max(1.0, backoff))
                next_backoff = min(FEED_BACKOFF_MAX_S, wait * 2.0)
            self._events.append(
                {
                    "kind": "reconnect",
                    "reason": reason,
                    "last_error": self._last_error,
                    "attempt": self._reconnects,
                    "backoff_s": wait,
                    "samples": self._conn_samples,
                }
            )
        return wait, next_backoff

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

        # RTDS also wants an application-level text PING every few seconds.
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
            app.run_forever(
                ping_interval=FEED_PING_INTERVAL_S,
                ping_timeout=FEED_PING_TIMEOUT_S,
            )
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
    window: Optional[OracleWindow] = None
    next_http: float = 0.0
    open_armed: bool = False
    end_armed: bool = False
    inflight: bool = False
    n429: int = 0
    http_attempts: int = 0
    http_counts: dict = field(default_factory=dict)
    http_logged: set = field(default_factory=set)
    end_final: bool = False


class OracleLogService:
    """Sample the TWAP tape for open 15m bags. ``tick`` does not raise."""

    def __init__(
        self,
        path: Any,
        *,
        feed: Any = None,
        fetch_price: Optional[Callable[[int], WindowPrice]] = None,
        max_bytes: int = TAPE_MAX_BYTES,
        jitter: Optional[Callable[[], float]] = None,
    ) -> None:
        self.path = path
        self.max_bytes = int(max_bytes)
        self._feed = feed if feed is not None else RtdsTwapFeed()
        self._fetch_price = fetch_price if fetch_price is not None else fetch_crypto_price
        self._jitter = jitter if jitter is not None else (lambda: random.uniform(0.0, HTTP_JITTER_S))
        self.sleep_s = 5.0
        self._memory: dict[str, _WindowMemory] = {}
        self._seen: set[tuple[Any, ...]] = set()
        self._seen_order: deque[tuple[Any, ...]] = deque()
        self._fail_wall: dict[str, float] = {}
        self._idle_since: Optional[float] = None
        self._awaiting_since: Optional[float] = None
        self._on_fail: Optional[FailFn] = None
        self._on_event: Optional[EventFn] = None
        self._last_arrival: Optional[float] = None
        self._watchdog_at: Optional[float] = None
        self._watchdog_gap = FEED_SILENT_RECONNECT_S
        self._stall_origin: Optional[float] = None
        self._stall_noted = 0.0
        self._stall_reminders = 0
        self._event_wall: dict[str, float] = {}
        self._event_suppressed: dict[str, int] = {}

    def bag_view(self, condition_id: str) -> OracleBagView:
        """Latest TWAP + open_ref for one bag. Reuses the live feed (no second WS)."""
        cid = str(condition_id or "")
        memory = self._memory.get(cid)
        open_usd = memory.open_ref if memory is not None else None
        latest: Optional[TwapSample] = None
        try:
            latest = self._feed.latest()
        except Exception:
            latest = None
        if latest is None:
            return OracleBagView(
                twap=None, open_usd=open_usd, obs_ts=None, source=RTDS_SOURCE
            )
        return OracleBagView(
            twap=latest.twap,
            open_usd=open_usd,
            obs_ts=float(latest.obs_ts),
            source=str(latest.source or RTDS_SOURCE),
        )

    def tick(
        self,
        state: Any,
        now: float,
        *,
        enabled: bool = True,
        on_fail: Optional[FailFn] = None,
        on_event: Optional[EventFn] = None,
    ) -> None:
        self._on_fail = on_fail
        self._on_event = on_event
        try:
            self._tick(state, now, enabled=enabled)
        except Exception as exc:
            self._fail(f"tick: {exc}", now=now, window=None, kind="tick")
            self.sleep_s = 5.0

    def _tick(self, state: Any, now: float, *, enabled: bool) -> None:
        if not enabled:
            self._stop_feed()
            self.sleep_s = 5.0
            self._idle_since = None
            self._awaiting_since = None
            self._end_stall(now, None, reason="disabled")
            self._reset_feed_watch()
            return
        windows = windows_from_intents(state, now)
        tracked = {window.condition_id for window in windows}
        closing = [
            window
            for window in windows_from_intents(state, now, grace_s=CLOSE_GRACE_S)
            if window.condition_id not in tracked
        ]
        if not windows:
            self._end_stall(now, None, reason="no_windows")
            self._reset_feed_watch()
            self._awaiting_since = None
            for window in closing:
                self._maybe_http(window, self._window_memory(window), now)
            self._finish_http(now)
            pending = any(
                not self._window_memory(window).end_logged for window in closing
            )
            self.sleep_s = INTERVAL_LAST_MIN_S if pending else 5.0
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
            self._fail(f"feed: {exc}", now=now, window=windows[0], kind="feed")
            samples = []
            latest = None
        if samples or self._last_arrival is None:
            self._last_arrival = now
        if samples:
            self._watchdog_at = None
            self._watchdog_gap = FEED_SILENT_RECONNECT_S
        for window in windows:
            self._record_window(window, samples, latest, now)
        for window in closing:
            self._maybe_http(window, self._window_memory(window), now)
        self._finish_http(now)
        self._log_feed_events(now, windows[0])
        self._watch_feed(now, windows[0])
        self._note_silence(windows[0], latest, samples, now)

    def _window_memory(self, window: OracleWindow) -> _WindowMemory:
        memory = self._memory.setdefault(window.condition_id, _WindowMemory())
        if memory.window is None:
            memory.window = window
        return memory

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

    def _feed_error(self) -> str:
        last_error = getattr(self._feed, "last_error", None)
        if last_error is None:
            return ""
        try:
            return str(last_error() or "")[:240]
        except Exception as exc:
            return str(exc)[:240]

    def _reset_feed_watch(self) -> None:
        self._last_arrival = None
        self._watchdog_at = None
        self._watchdog_gap = FEED_SILENT_RECONNECT_S

    def _watch_feed(self, now: float, window: OracleWindow) -> None:
        """Force a reconnect when a tracked bag gets no sample for too long."""
        if self._last_arrival is None:
            return
        silent = now - self._last_arrival
        since = self._watchdog_at if self._watchdog_at is not None else self._last_arrival
        if silent < FEED_SILENT_RECONNECT_S or now - since < self._watchdog_gap:
            return
        reconnect = getattr(self._feed, "reconnect", None)
        if reconnect is None:
            return
        try:
            closed = bool(reconnect(f"watchdog: silent {silent:.0f}s"))
        except Exception as exc:
            closed = False
            self._fail(f"feed: reconnect {exc}", now=now, window=window, kind="feed")
        self._watchdog_gap = min(FEED_WATCHDOG_CAP_S, self._watchdog_gap * 2.0)
        self._watchdog_at = now
        self._event(
            "oracle_feed_watchdog",
            now=now,
            window=window,
            fields={
                "silent_s": round(silent, 1),
                "closed": closed,
                "next_check_s": self._watchdog_gap,
                "last_error": self._feed_error(),
            },
            throttle="watchdog",
        )

    def _log_feed_events(self, now: float, window: OracleWindow) -> None:
        pop = getattr(self._feed, "pop_events", None)
        if pop is None:
            return
        try:
            events = list(pop())
        except Exception:
            return
        for item in events:
            if not isinstance(item, dict) or item.get("kind") != "reconnect":
                continue
            self._event(
                "oracle_feed_reconnect",
                now=now,
                window=window,
                fields={
                    "reason": str(item.get("reason") or "")[:120],
                    "last_error": str(item.get("last_error") or "")[:240],
                    "attempt": item.get("attempt"),
                    "backoff_s": item.get("backoff_s"),
                    "samples": item.get("samples"),
                },
                throttle="reconnect",
            )

    def _note_silence(
        self,
        window: OracleWindow,
        latest: Optional[TwapSample],
        samples: list[TwapSample],
        now: float,
    ) -> None:
        """One ``oracle_feed_stall`` when the TWAP goes stale, a reminder at
        most every ``STALL_REMIND_S``, and one ``oracle_feed_recovered``."""
        if latest is not None:
            self._awaiting_since = None
            origin = float(latest.obs_ts)
        else:
            if self._awaiting_since is None:
                self._awaiting_since = now
            origin = self._awaiting_since
        del samples
        age = now - origin
        if age <= STALE_AFTER_S:
            self._end_stall(now, window, reason="fresh")
            return
        if self._stall_origin is None:
            self._stall_origin = origin
            self._stall_noted = now
            self._stall_reminders = 0
            self._event(
                "oracle_feed_stall",
                now=now,
                window=window,
                fields={
                    "age_s": round(age, 1),
                    "reminder": 0,
                    "no_sample_yet": latest is None,
                    "last_error": self._feed_error(),
                },
            )
            return
        if now - self._stall_noted >= STALL_REMIND_S:
            self._stall_noted = now
            self._stall_reminders += 1
            self._event(
                "oracle_feed_stall",
                now=now,
                window=window,
                fields={
                    "age_s": round(age, 1),
                    "reminder": self._stall_reminders,
                    "no_sample_yet": latest is None,
                    "last_error": self._feed_error(),
                },
            )

    def _end_stall(self, now: float, window: Optional[OracleWindow], *, reason: str) -> None:
        if self._stall_origin is None:
            return
        duration = now - self._stall_origin
        self._stall_origin = None
        self._stall_reminders = 0
        self._event(
            "oracle_feed_recovered",
            now=now,
            window=window,
            fields={"duration_s": round(duration, 1), "reason": reason},
        )

    def _record_window(
        self,
        window: OracleWindow,
        samples: list[TwapSample],
        latest: Optional[TwapSample],
        now: float,
    ) -> None:
        memory = self._window_memory(window)
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
        """At most one crypto-price request in flight per window.

        The first request waits for the open (or the end) plus 0-3s jitter.
        ``incomplete`` is a normal 20s wait. A 429 backs off 20/40/80/120s
        (or Retry-After). The close is fetched until ``CLOSE_GRACE_S``."""
        if memory.inflight:
            return
        need_open = memory.open_ref is None and now >= window.start_ts
        need_end = (
            not memory.end_logged
            and window.end_ts <= now <= window.end_ts + CLOSE_GRACE_S
        )
        if not need_open and not need_end:
            return
        if need_open and not memory.open_armed:
            memory.open_armed = True
            memory.next_http = max(memory.next_http, window.start_ts + self._jitter_s())
        if need_end and not memory.end_armed:
            memory.end_armed = True
            memory.next_http = max(memory.next_http, window.end_ts + self._jitter_s())
        if now < memory.next_http:
            return
        memory.inflight = True
        memory.last_http = now
        memory.http_attempts += 1
        try:
            try:
                price = self._fetch_price(int(window.start_ts))
            except Exception as exc:
                self._http_failed(window, memory, now, exc)
                return
            if not isinstance(price, WindowPrice):
                self._http_failed(window, memory, now, ValueError("bad payload"))
                return
            memory.n429 = 0
            self._http_ok(window, memory, now, price, need_end)
        finally:
            memory.inflight = False

    def _jitter_s(self) -> float:
        try:
            value = float(self._jitter())
        except Exception:
            return 0.0
        return min(HTTP_JITTER_S, max(0.0, value))

    def _http_failed(
        self, window: OracleWindow, memory: _WindowMemory, now: float, exc: BaseException
    ) -> None:
        kind, retry_after = classify_http_error(exc)
        memory.http_counts[kind] = int(memory.http_counts.get(kind, 0)) + 1
        memory.n429 = memory.n429 + 1 if kind == "http_429" else 0
        delay = http_retry_delay_s(kind, memory.n429, retry_after)
        memory.next_http = now + delay + self._jitter_s()
        if kind in memory.http_logged:
            return
        memory.http_logged.add(kind)
        hint = f"; retry_after={retry_after:.0f}s" if retry_after is not None else ""
        self._fail(
            f"crypto-price {kind}: {exc} (retry in {delay:.0f}s{hint}; once per window)",
            now=now,
            window=window,
            kind=f"http:{window.condition_id}:{kind}",
        )

    def _http_ok(
        self,
        window: OracleWindow,
        memory: _WindowMemory,
        now: float,
        price: WindowPrice,
        need_end: bool,
    ) -> None:
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
            self._close_summary(window, memory, now, outcome="ok")
            return
        if need_end:
            # Not published yet: a normal wait, not an error.
            memory.http_counts["incomplete"] = int(memory.http_counts.get("incomplete", 0)) + 1
        if need_end or memory.open_ref is None:
            memory.next_http = now + HTTP_RETRY_S + self._jitter_s()

    def _finish_http(self, now: float) -> None:
        for cid in list(self._memory):
            memory = self._memory[cid]
            window = memory.window
            if window is None:
                continue
            past = now - (window.end_ts + CLOSE_GRACE_S)
            if past <= 0:
                continue
            if memory.end_armed and not memory.end_logged and not memory.end_final:
                self._close_summary(window, memory, now, outcome="gave_up")
            if past > 60.0:
                del self._memory[cid]

    def _close_summary(
        self, window: OracleWindow, memory: _WindowMemory, now: float, *, outcome: str
    ) -> None:
        """One line per window: always on give-up, on success only after errors."""
        memory.end_final = True
        errors = {k: v for k, v in memory.http_counts.items() if k != "incomplete"}
        if outcome == "ok" and not errors:
            return
        self._event(
            "oracle_close_fetch" if outcome == "ok" else "oracle_window_end_missed",
            now=now,
            window=window,
            fields={
                "outcome": outcome,
                "attempts": memory.http_attempts,
                "counts": dict(sorted(memory.http_counts.items())),
                "after_end_s": round(now - window.end_ts, 1),
            },
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
            append_jsonl(self.path, row, max_bytes=self.max_bytes)
        except Exception as exc:
            if self._on_fail is None:
                return
            if not self._throttle_ok(self._fail_wall, "write", float(row.get("ts") or 0.0), FAIL_REPEAT_S):
                return
            try:
                self._on_fail(f"write: {exc}"[:240])
            except Exception:
                return

    @staticmethod
    def _throttle_ok(book: dict, key: str, now: float, every: float) -> bool:
        last = book.get(key)
        if last is not None and 0.0 <= now - last < every:
            return False
        book[key] = now
        return True

    def _event(
        self,
        name: str,
        *,
        now: float,
        window: Optional[OracleWindow],
        fields: dict,
        throttle: Optional[str] = None,
    ) -> None:
        """Tape row + ``on_event`` (``on_fail`` text if no event hook).
        ``throttle`` keys allow one line per ``FEED_EVENT_REPEAT_S``."""
        payload = dict(fields)
        if throttle is not None:
            if not self._throttle_ok(self._event_wall, throttle, now, FEED_EVENT_REPEAT_S):
                self._event_suppressed[throttle] = self._event_suppressed.get(throttle, 0) + 1
                return
            payload["suppressed"] = self._event_suppressed.pop(throttle, 0)
        if window is not None:
            payload.setdefault("slug", window.slug)
            payload.setdefault("condition_id", window.condition_id)
        try:
            append_jsonl(
                self.path,
                max_bytes=self.max_bytes,
                row=build_oracle_row(
                    now=now,
                    window=window,
                    source=RTDS_SOURCE,
                    event=name,
                    twap=None,
                    twap_ts=None,
                    open_ref=None,
                    notes=name,
                    extra={k: v for k, v in payload.items() if k not in ("slug", "condition_id")},
                ),
            )
        except Exception:
            pass
        if self._on_event is not None:
            try:
                self._on_event(name, payload)
            except Exception:
                return
            return
        if self._on_fail is not None:
            detail = " ".join(f"{k}={v}" for k, v in payload.items() if v not in ("", None))
            try:
                self._on_fail(f"{name} {detail}"[:240])
            except Exception:
                return

    def _fail(
        self,
        message: str,
        *,
        now: float,
        window: Optional[OracleWindow],
        kind: str,
    ) -> None:
        """Throttled per ``kind`` (not exact text) to one per ``FAIL_REPEAT_S``."""
        text = " ".join(str(message).split())[:240] or "oracle_log_fail"
        if not self._throttle_ok(self._fail_wall, kind, now, FAIL_REPEAT_S):
            return
        try:
            append_jsonl(
                self.path,
                max_bytes=self.max_bytes,
                row=build_oracle_row(
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
