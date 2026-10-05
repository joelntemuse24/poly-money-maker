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
* Strike and close: the 60s TWAP sample stamped exactly at the window's
  start (``priceToBeat``) and end (``finalPrice``). Gamma's
  ``eventMetadata.priceToBeat`` for window N equals its ``finalPrice`` for
  N-1, and both equal that boundary sample. The subscribe burst replays
  about the last minute, so a reconnect shortly after the boundary still
  recovers it. A later sample for the same second with a different value
  supersedes the first (``oracle_strike_revised``).
* Strike fallback only: ``GET /api/crypto/crypto-price`` (``variant=fifteen``)
  ``openPrice``, when no boundary sample arrived by ``STRIKE_WAIT_S``. That
  endpoint publishes a different boundary price series (its open/close
  differ from priceToBeat/finalPrice by up to ~$40), so its rows are
  labelled ``strike_source=crypto_price_open`` and its close is not used.
* Reconciliation: ``GET gamma-api /events?slug=`` once ``eventMetadata`` is
  published (``priceToBeat`` ~10 min after the end, ``finalPrice`` later).
  Logs ``oracle_strike_check`` / ``oracle_close_check`` and corrects the
  tape if ours differs. It runs after the window, so it cannot reach a
  live decision.

Feed health: protocol ping/pong plus a silence watchdog reconnect the
socket when no sample arrives for ``FEED_SILENT_RECONNECT_S`` (``FEED_SILENT_HOT_S``
while a bag is in its last ``FEED_HOT_TTM_S``, where mintbot's scrap oracle
veto reads ``bag_view``; samples carry a local ``recv_ts``). A stall is
logged once when it starts (``oracle_feed_stall``), at most once a minute
while it lasts, and once when it ends (``oracle_feed_recovered``).
HTTP errors are logged once per window per kind and source; a 429 backs off.
"""

from __future__ import annotations

import json
import math
import random
import threading
import time
from collections import deque
from dataclasses import dataclass, field, replace
from decimal import Decimal, InvalidOperation
from typing import Any, Callable, Optional

from buy.chain import thread_session
from buy.log_archive import roll_if_over
from buy.market import slug_start_ts


RTDS_URL = "wss://ws-live-data.polymarket.com"
RTDS_TOPIC = "crypto_prices_twap_sixty"
# Live Chainlink BTC/USD price on the same socket (~1 update/s). Held in
# memory for the scrap oracle veto only; never written as a tape sample.
RTDS_LIVE_TOPIC = "crypto_prices_chainlink"
RTDS_SYMBOL = "btc/usd"
RTDS_SOURCE = "polymarket_rtds"


def _allowed_symbols(symbols: Any) -> frozenset[str]:
    """Default is btc/usd only, so mintbot's parser stays single-asset.

    Callers pass the symbols they subscribed to. An empty list does not
    mean "every symbol".
    """
    if symbols is None:
        return frozenset({RTDS_SYMBOL})
    allowed = []
    for item in symbols:
        text = str(item or "").strip().lower()
        if text and text not in allowed:
            allowed.append(text)
    return frozenset(allowed)
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
# A window with no end-boundary sample by this long after the end logs
# ``oracle_window_end_missed``. Sampling still stops at GRACE_AFTER_S.
CLOSE_GRACE_S = 300.0
HTTP_BACKOFF_CAP_S = 120.0
HTTP_JITTER_S = 3.0
# RTDS feed health. The socket can stay connected but silent, so a
# watchdog closes it after this long without a sample while a bag is
# tracked. Forced reconnects back off 45s -> 90s -> 120s cap.
FEED_SILENT_RECONNECT_S = 45.0
FEED_WATCHDOG_CAP_S = 120.0
# While any tracked bag is within FEED_HOT_TTM_S of its end (the loser-scrap
# span, where the scrap oracle veto reads this feed) the watchdog fires after
# FEED_SILENT_HOT_S of silence (5s -> 10s cap), a dead socket redials within
# FEED_HOT_BACKOFF_S, and the post-window Gamma audit (blocking HTTP on this
# thread) waits until no bag is hot.
FEED_HOT_TTM_S = 360.0
FEED_SILENT_HOT_S = 5.0
FEED_HOT_WATCHDOG_CAP_S = 10.0
FEED_HOT_BACKOFF_S = 2.0
FEED_PING_INTERVAL_S = 20.0
FEED_PING_TIMEOUT_S = 10.0
FEED_BACKOFF_MAX_S = 30.0
STALL_REMIND_S = 60.0
FEED_EVENT_REPEAT_S = 60.0
# Strike capture. The subscribe replay covers ~57s, so past this no
# reconnect can still deliver the boundary sample; fall back to crypto-price.
STRIKE_WAIT_S = 75.0
STRIKE_RTDS = "rtds_twap_at_start"
STRIKE_CRYPTO = "crypto_price_open"
STRIKE_GAMMA = "gamma_price_to_beat"
CLOSE_RTDS = "rtds_twap_at_end"
CLOSE_GAMMA = "gamma_final_price"
GAMMA_SOURCE = "gamma_event_metadata"
GAMMA_EVENTS_URL = "https://gamma-api.polymarket.com/events"
GAMMA_SLUG_PREFIX = "btc-updown-15m-"
# Gamma publishes priceToBeat ~10 min after the window ends and finalPrice
# later still. Checks start then, retry every 2 min, and stop after an hour.
GAMMA_FIRST_S = 600.0
GAMMA_RETRY_S = 120.0
GAMMA_BACKOFF_CAP_S = 600.0
GAMMA_GIVE_UP_S = 3600.0
STRIKE_MATCH_USD = Decimal("0.01")
BOUNDARY_KEEP = 32

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
        },
        {
            "topic": RTDS_LIVE_TOPIC,
            "type": "*",
            "filters": "{\"symbol\":\"btc/usd\"}",
        },
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
    # Local wall time the feed first held this observation. ``obs_ts`` is
    # the Chainlink stamp and trails arrival by ~1.5-2.5s.
    recv_ts: Optional[float] = field(default=None, compare=False)


@dataclass(frozen=True)
class LivePrice:
    """Latest live Chainlink BTC/USD print (``crypto_prices_chainlink``)."""

    price: str
    obs_ts: float
    recv_ts: Optional[float] = None


@dataclass(frozen=True)
class WindowPrice:
    open_ref: Optional[str]
    close_twap: Optional[str]
    completed: bool


@dataclass(frozen=True)
class GammaStrike:
    price_to_beat: Optional[str]
    final_price: Optional[str]


@dataclass(frozen=True)
class OracleBagView:
    """Read-only TWAP + window-open snapshot for the loser-scrap oracle gates."""

    twap: Optional[str]
    open_usd: Optional[str]
    obs_ts: Optional[float]
    source: str = RTDS_SOURCE
    open_source: Optional[str] = None
    recv_ts: Optional[float] = None
    live_price: Optional[str] = None
    live_obs_ts: Optional[float] = None
    live_recv_ts: Optional[float] = None


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


def parse_rtds_message(raw: Any, *, symbols: Any = None) -> list[TwapSample]:
    """Parse one RTDS text frame into 60s TWAP samples.

    Blank frames, PING/PONG, symbols outside ``symbols``, and the 30s
    topic are ignored. ``symbols`` defaults to btc/usd. A subscribe
    payload's ``data`` array is the recent path; an update payload is
    one observation.
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
    symbol = str(body.get("symbol") or "").strip().lower()
    if symbol not in _allowed_symbols(symbols):
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


def parse_rtds_live(raw: Any, *, symbols: Any = None) -> Optional[LivePrice]:
    """One ``crypto_prices_chainlink`` update, or None.

    The subscribe snapshot (topic ``crypto_prices``) is ignored here, so a
    reconnect only trusts prints that arrive live. Symbols outside
    ``symbols`` are ignored. ``symbols`` defaults to btc/usd."""
    if isinstance(raw, (bytes, bytearray)):
        raw = raw.decode("utf-8", "replace")
    if isinstance(raw, str):
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            return None
    elif isinstance(raw, dict):
        payload = raw
    else:
        return None
    if not isinstance(payload, dict) or payload.get("topic") != RTDS_LIVE_TOPIC:
        return None
    body = payload.get("payload")
    if not isinstance(body, dict):
        return None
    symbol = str(body.get("symbol") or "").strip().lower()
    if symbol not in _allowed_symbols(symbols):
        return None
    price = None
    if body.get("full_accuracy_value") is not None:
        price = e18_to_decimal_str(body.get("full_accuracy_value"))
    if price is None and body.get("value") is not None:
        price = _loose_decimal_str(body.get("value"))
    obs = _obs_seconds(body.get("timestamp"))
    if price is None or obs is None:
        return None
    return LivePrice(price=price, obs_ts=obs)


def _json_payload(raw: Any) -> Optional[dict]:
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
    return payload if isinstance(payload, dict) else None


def parse_rtds_price_snapshot(raw: Any, *, symbols: Any = None) -> list[LivePrice]:
    """Recent live path from a ``crypto_prices`` subscribe snapshot.

    Chainlink *updates* stay on ``parse_rtds_live``. This is the ~60s
    backlog the socket sends on connect (``timestamp`` + ``value``).
    Symbols outside ``symbols`` are dropped. Default is btc/usd.
    """
    payload = _json_payload(raw)
    if payload is None or payload.get("topic") != "crypto_prices":
        return []
    body = payload.get("payload")
    if not isinstance(body, dict):
        return []
    symbol = str(body.get("symbol") or "").strip().lower()
    if symbol not in _allowed_symbols(symbols):
        return []
    points = body.get("data")
    if not isinstance(points, list):
        points = [body]
    out: list[LivePrice] = []
    for point in points:
        if not isinstance(point, dict):
            continue
        price = _loose_decimal_str(point.get("value"))
        obs = _obs_seconds(point.get("timestamp"))
        if price is None or obs is None:
            continue
        out.append(LivePrice(price=price, obs_ts=obs))
    return out


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


class OracleHTTPError(RuntimeError):
    """Non-2xx crypto-price or Gamma reply. ``retry_after`` is seconds, if sent."""

    def __init__(self, status: int, retry_after: Optional[float] = None, body: str = "") -> None:
        self.status = int(status)
        self.retry_after = retry_after
        super().__init__(f"HTTP {self.status}" + (f" {body[:120]}" if body else ""))


CryptoPriceHTTPError = OracleHTTPError


def boundary_second(obs_ts: Any) -> Optional[int]:
    """The 15m boundary (unix seconds) when ``obs_ts`` is exactly one."""
    obs = _as_float(obs_ts)
    if obs is None:
        return None
    sec = round(obs)
    if abs(obs - sec) > 0.0005 or sec % int(FIFTEEN_S):
        return None
    return int(sec)


def gamma_event_slug(start_ts: Any) -> str:
    return f"{GAMMA_SLUG_PREFIX}{int(float(start_ts))}"


def parse_gamma_event_body(text: str) -> GammaStrike:
    """``eventMetadata.priceToBeat`` / ``finalPrice`` from ``/events?slug=``.

    Missing metadata (not yet published) gives ``None`` fields."""
    data = json.loads(text, parse_float=Decimal, parse_int=Decimal)
    if isinstance(data, list):
        data = data[0] if data else {}
    if not isinstance(data, dict):
        raise ValueError("gamma event payload was not an object")
    meta = data.get("eventMetadata")
    if isinstance(meta, str):
        try:
            meta = json.loads(meta, parse_float=Decimal, parse_int=Decimal)
        except json.JSONDecodeError:
            meta = None
    if not isinstance(meta, dict):
        return GammaStrike(price_to_beat=None, final_price=None)

    def pick(key: str) -> Optional[str]:
        value = meta.get(key)
        if value is None or isinstance(value, bool):
            return None
        if isinstance(value, Decimal):
            return _format_decimal(value) if value.is_finite() and value > 0 else None
        text_value = _loose_decimal_str(value)
        if text_value is None or Decimal(text_value) <= 0:
            return None
        return text_value

    return GammaStrike(price_to_beat=pick("priceToBeat"), final_price=pick("finalPrice"))


def fetch_gamma_strike(slug: str, *, timeout: float = 5.0) -> GammaStrike:
    response = thread_session("gamma_strike").get(
        GAMMA_EVENTS_URL,
        params={"slug": slug},
        timeout=timeout,
        headers={"User-Agent": "poly-money-maker-oracle-log/1.0"},
    )
    status = int(getattr(response, "status_code", 200) or 200)
    if status >= 400:
        headers = getattr(response, "headers", None) or {}
        raise OracleHTTPError(
            status,
            parse_retry_after(headers.get("Retry-After")),
            str(getattr(response, "text", "") or "").strip(),
        )
    return parse_gamma_event_body(response.text)


def usd_diff(ours: Any, official: Any) -> Optional[Decimal]:
    """``ours - official`` exactly, or None when either is unusable."""
    try:
        a = Decimal(str(ours))
        b = Decimal(str(official))
    except (InvalidOperation, ValueError, TypeError):
        return None
    if ours is None or official is None or not (a.is_finite() and b.is_finite()):
        return None
    return a - b


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


def http_retry_delay_s(
    kind: str,
    n429: int,
    retry_after: Optional[float],
    *,
    base_s: float = HTTP_RETRY_S,
    cap_s: float = HTTP_BACKOFF_CAP_S,
) -> float:
    """``base_s`` normally (20s crypto-price, 120s Gamma). The n-th consecutive
    429 waits base, 2x, 4x, ... up to ``cap_s``, or longer if the server
    sent Retry-After."""
    if kind != "http_429":
        return base_s
    delay = min(cap_s, base_s * (2 ** max(0, n429 - 1)))
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


_JSONL_DIRS: set[str] = set()
_JSONL_SIZE: dict[str, int] = {}


def append_jsonl(path: Any, row: dict, *, max_bytes: int = 0) -> None:
    """Append one row. With ``max_bytes`` > 0 the tape rolls into
    ``<dir>/archive/<name>.<UTC stamp>.gz`` first, like ``mintbot.log``.

    The parent directory is created once per process, and the size check
    uses the bytes this process has written, so a hot log line does not
    stat the file.
    """
    from pathlib import Path

    target = Path(path)
    parent = str(target.parent)
    if parent not in _JSONL_DIRS:
        target.parent.mkdir(parents=True, exist_ok=True)
        _JSONL_DIRS.add(parent)
    data = (json.dumps(row, separators=(",", ":"), sort_keys=True) + "\n").encode("utf-8")
    key = str(target)
    if max_bytes > 0:
        known = _JSONL_SIZE.get(key)
        if known is None:
            try:
                known = target.stat().st_size
            except OSError:
                known = 0
        if known > 0 and known + len(data) >= int(max_bytes):
            rolled = roll_if_over(target, len(data), max_bytes)
            if rolled is not None or not target.exists():
                known = 0
            else:
                try:
                    known = target.stat().st_size
                except OSError:
                    known = 0
        _JSONL_SIZE[key] = int(known) + len(data)
    with open(target, "ab") as handle:
        handle.write(data)
        handle.flush()


class RtdsTwapFeed:
    """Background RTDS subscription. ``handle_message`` is the test seam."""

    def __init__(
        self,
        url: str = RTDS_URL,
        *,
        clock: Callable[[], float] = time.time,
        symbols: Any = None,
        history_s: float = 0.0,
    ) -> None:
        self.url = url
        self._clock = clock
        self._hot = False
        self._lock = threading.Lock()
        self._latest: Optional[TwapSample] = None
        self._live: Optional[LivePrice] = None
        self._backlog: list[TwapSample] = []
        self._last_error = ""
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._ws: Any = None
        self._events: deque[dict] = deque(maxlen=50)
        self._force_reason = ""
        self._conn_samples = 0
        self._reconnects = 0
        # Default stays the single btc/usd subscription mintbot already uses.
        # One symbol per socket: a shared socket only streamed
        # live updates for one symbol (checked 2026-10-04).
        if symbols is None:
            self.symbols: tuple[str, ...] = (RTDS_SYMBOL,)
        else:
            cleaned: list[str] = []
            for item in symbols:
                text = str(item or "").strip().lower()
                if text and text not in cleaned:
                    cleaned.append(text)
            self.symbols = tuple(cleaned) or (RTDS_SYMBOL,)
        self._symbol_set = frozenset(self.symbols)
        self.history_s = max(0.0, float(history_s or 0.0))
        # (obs_ts, recv_ts, price). Empty unless history_s > 0.
        self._live_hist: deque = deque()
        self._twap_hist: deque = deque()
        self._live_by: dict[str, LivePrice] = {}
        self._twap_by: dict[str, TwapSample] = {}

    def subscribe_frame(self) -> dict:
        """RTDS subscribe payload. The default btc/usd frame is the constant."""
        if self.symbols == (RTDS_SYMBOL,):
            return SUBSCRIBE_FRAME
        subs = []
        for symbol in self.symbols:
            filters = json.dumps({"symbol": symbol}, separators=(",", ":"))
            subs.append({"topic": RTDS_TOPIC, "type": "update", "filters": filters})
            subs.append({"topic": RTDS_LIVE_TOPIC, "type": "*", "filters": filters})
        return {"action": "subscribe", "subscriptions": subs}

    def _is_default_symbol(self, symbol: str) -> bool:
        """``latest()`` / ``latest_live()`` stay on btc when btc is subscribed."""
        if RTDS_SYMBOL in self._symbol_set:
            return symbol == RTDS_SYMBOL
        return True

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

    def latest(self, symbol: Optional[str] = None) -> Optional[TwapSample]:
        with self._lock:
            if symbol is None:
                return self._latest
            return self._twap_by.get(str(symbol).strip().lower())

    def latest_live(self, symbol: Optional[str] = None) -> Optional[LivePrice]:
        with self._lock:
            if symbol is None:
                return self._live
            return self._live_by.get(str(symbol).strip().lower())

    def live_history(self) -> list[tuple[float, float, float]]:
        """``(obs_ts, recv_ts, price)`` for the retained live path. Copy."""
        with self._lock:
            return list(self._live_hist)

    def twap_history(self) -> list[tuple[float, float, float]]:
        """``(obs_ts, recv_ts, twap)`` for the retained 60s TWAP path. Copy."""
        with self._lock:
            return list(self._twap_hist)

    def _merge_hist(self, bucket: deque, rows: list[tuple[float, float, float]]) -> None:
        if self.history_s <= 0 or not rows:
            return
        merged: dict[float, tuple[float, float]] = {obs: (recv, price) for obs, recv, price in bucket}
        for obs, recv, price in rows:
            prev = merged.get(obs)
            if prev is None:
                merged[obs] = (recv, price)
            else:
                merged[obs] = (prev[0], price)
        items = sorted((obs, recv, price) for obs, (recv, price) in merged.items())
        if items:
            cutoff = items[-1][0] - self.history_s
            items = [row for row in items if row[0] >= cutoff]
        if len(items) > 4000:
            items = items[-4000:]
        bucket.clear()
        bucket.extend(items)

    def set_hot(self, hot: bool) -> None:
        """Hot = a bag is in its scrap span; caps the redial backoff."""
        with self._lock:
            self._hot = bool(hot)

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

    def _remember_live(self, live: LivePrice, recv: float, symbol: str) -> None:
        """Store one live print. Same-second repeats keep the first recv_ts."""
        stamped = live
        prev = self._live_by.get(symbol)
        if prev is None or live.obs_ts > prev.obs_ts:
            stamped = replace(live, recv_ts=recv)
        elif live.obs_ts == prev.obs_ts:
            stamped = replace(live, recv_ts=prev.recv_ts)
        else:
            return
        self._live_by[symbol] = stamped
        if self._is_default_symbol(symbol):
            self._live = stamped
        if self.history_s > 0:
            try:
                price = float(stamped.price)
            except (TypeError, ValueError):
                return
            if math.isfinite(price):
                self._merge_hist(self._live_hist, [(float(stamped.obs_ts), float(stamped.recv_ts or recv), price)])

    def handle_message(self, raw: Any) -> None:
        text = raw.decode("utf-8", "replace") if isinstance(raw, (bytes, bytearray)) else str(raw)
        if self.history_s > 0 and "crypto_prices" in text and RTDS_LIVE_TOPIC not in text and RTDS_TOPIC not in text:
            try:
                snapped = parse_rtds_price_snapshot(text, symbols=self._symbol_set)
            except Exception as exc:
                self._set_error(str(exc)[:240])
                return
            if snapped:
                recv = float(self._clock())
                symbol = self.symbols[0] if len(self.symbols) == 1 else ""
                # The snapshot payload carries its own symbol; recover it
                # from the frame so a multi-symbol feed files the path.
                payload = _json_payload(text)
                body = payload.get("payload") if isinstance(payload, dict) else None
                if isinstance(body, dict):
                    symbol = str(body.get("symbol") or symbol).strip().lower()
                if symbol not in self._symbol_set:
                    return
                with self._lock:
                    rows = []
                    newest = None
                    for point in snapped:
                        try:
                            price = float(point.price)
                        except (TypeError, ValueError):
                            continue
                        if not math.isfinite(price):
                            continue
                        rows.append((float(point.obs_ts), recv, price))
                        if newest is None or point.obs_ts >= newest.obs_ts:
                            newest = point
                    self._merge_hist(self._live_hist, rows)
                    if newest is not None:
                        self._remember_live(newest, recv, symbol)
                return
        if RTDS_LIVE_TOPIC in text:
            try:
                live = parse_rtds_live(text, symbols=self._symbol_set)
            except Exception as exc:
                self._set_error(str(exc)[:240])
                return
            if live is not None:
                recv = float(self._clock())
                symbol = self.symbols[0] if len(self.symbols) == 1 else RTDS_SYMBOL
                payload = _json_payload(text)
                body = payload.get("payload") if isinstance(payload, dict) else None
                if isinstance(body, dict):
                    symbol = str(body.get("symbol") or symbol).strip().lower()
                with self._lock:
                    self._remember_live(live, recv, symbol)
                return
        try:
            samples = parse_rtds_message(text, symbols=self._symbol_set)
        except Exception as exc:
            self._set_error(str(exc)[:240])
            return
        if samples:
            recv = float(self._clock())
            samples = [replace(sample, recv_ts=recv) for sample in samples]
            with self._lock:
                self._backlog.extend(samples)
                if len(self._backlog) > 500:
                    self._backlog = self._backlog[-500:]
                rows: list[tuple[float, float, float]] = []
                for sample in sorted(samples, key=lambda item: item.obs_ts):
                    prev = self._twap_by.get(sample.symbol)
                    if prev is None or sample.obs_ts > prev.obs_ts:
                        stored = sample
                    elif sample.obs_ts == prev.obs_ts:
                        # Same second re-sent (or revised): not a newer
                        # reading, so it keeps the first arrival time.
                        stored = replace(sample, recv_ts=prev.recv_ts)
                    else:
                        stored = None
                    if stored is not None:
                        self._twap_by[sample.symbol] = stored
                        if self._is_default_symbol(sample.symbol):
                            self._latest = stored
                    try:
                        price = float(sample.twap)
                    except (TypeError, ValueError):
                        price = None
                    if price is not None and math.isfinite(price):
                        rows.append((float(sample.obs_ts), recv, price))
                if self.history_s > 0:
                    self._merge_hist(self._twap_hist, rows)
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
                cap = FEED_HOT_BACKOFF_S if self._hot else FEED_BACKOFF_MAX_S
                wait = min(cap, max(1.0, backoff))
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
            ws.send(json.dumps(self.subscribe_frame()))

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
                skip_utf8_validation=True,
            )
        finally:
            ping_stop.set()
            self._ws = None


@dataclass
class _HttpSlot:
    """One HTTP purpose per window (strike fallback or Gamma check)."""

    armed: bool = False
    next_at: float = 0.0
    inflight: bool = False
    n429: int = 0
    attempts: int = 0
    counts: dict = field(default_factory=dict)
    logged: set = field(default_factory=set)
    done: bool = False


@dataclass
class _WindowMemory:
    last_sample_wall: float = 0.0
    window: Optional[OracleWindow] = None
    open_ref: Optional[str] = None
    open_source: Optional[str] = None
    open_delay_s: Optional[float] = None
    strike_late_logged: bool = False
    close_twap: Optional[str] = None
    close_source: Optional[str] = None
    end_logged: bool = False
    end_final: bool = False
    strike_checked: bool = False
    close_checked: bool = False
    check_done: bool = False
    open_http: _HttpSlot = field(default_factory=_HttpSlot)
    check_http: _HttpSlot = field(default_factory=_HttpSlot)


class OracleLogService:
    """Sample the TWAP tape for open 15m bags. ``tick`` does not raise."""

    def __init__(
        self,
        path: Any,
        *,
        feed: Any = None,
        fetch_price: Optional[Callable[[int], WindowPrice]] = None,
        fetch_gamma: Optional[Callable[[str], GammaStrike]] = None,
        max_bytes: int = TAPE_MAX_BYTES,
        jitter: Optional[Callable[[], float]] = None,
    ) -> None:
        self.path = path
        self.max_bytes = int(max_bytes)
        self._feed = feed if feed is not None else RtdsTwapFeed()
        self._fetch_price = fetch_price if fetch_price is not None else fetch_crypto_price
        self._fetch_gamma = fetch_gamma if fetch_gamma is not None else fetch_gamma_strike
        self._jitter = jitter if jitter is not None else (lambda: random.uniform(0.0, HTTP_JITTER_S))
        self.sleep_s = 5.0
        self._memory: dict[str, _WindowMemory] = {}
        self._boundary: dict[int, str] = {}
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
        self._hot_gap = FEED_SILENT_HOT_S
        self._hot = False
        self._stall_origin: Optional[float] = None
        self._stall_noted = 0.0
        self._stall_reminders = 0
        self._event_wall: dict[str, float] = {}
        self._event_suppressed: dict[str, int] = {}

    def bag_view(self, condition_id: str) -> OracleBagView:
        """Latest TWAP + strike for one bag. Reuses the live feed (no second WS)."""
        cid = str(condition_id or "")
        memory = self._memory.get(cid)
        open_usd = memory.open_ref if memory is not None else None
        open_source = memory.open_source if memory is not None else None
        latest: Optional[TwapSample] = None
        try:
            latest = self._feed.latest()
        except Exception:
            latest = None
        live: Optional[LivePrice] = None
        latest_live = getattr(self._feed, "latest_live", None)
        if latest_live is not None:
            try:
                live = latest_live()
            except Exception:
                live = None
        live_kw = {}
        if live is not None:
            live_kw = {
                "live_price": live.price,
                "live_obs_ts": float(live.obs_ts),
                "live_recv_ts": None if live.recv_ts is None else float(live.recv_ts),
            }
        if latest is None:
            return OracleBagView(
                twap=None, open_usd=open_usd, obs_ts=None, source=RTDS_SOURCE,
                open_source=open_source, **live_kw,
            )
        recv = getattr(latest, "recv_ts", None)
        return OracleBagView(
            twap=latest.twap,
            open_usd=open_usd,
            obs_ts=float(latest.obs_ts),
            source=str(latest.source or RTDS_SOURCE),
            open_source=open_source,
            recv_ts=None if recv is None else float(recv),
            **live_kw,
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
                self._resolve_boundaries(window, self._window_memory(window), now)
            self._finish_windows(now)
            self._maybe_check(now)
            self.sleep_s = 5.0
            self._note_idle(now)
            return
        self._idle_since = None
        self._feed.start()
        self._set_hot(any(window.end_ts - now <= FEED_HOT_TTM_S for window in windows))
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
            self._hot_gap = FEED_SILENT_HOT_S
        self._note_boundaries(samples, latest)
        for window in windows:
            self._record_window(window, samples, latest, now)
        for window in closing:
            self._resolve_boundaries(window, self._window_memory(window), now)
        self._finish_windows(now)
        if not self._hot:
            self._maybe_check(now)
        self._log_feed_events(now, windows[0])
        self._watch_feed(now, windows[0])
        self._note_silence(windows[0], latest, samples, now)

    def _window_memory(self, window: OracleWindow) -> _WindowMemory:
        memory = self._memory.setdefault(window.condition_id, _WindowMemory())
        if memory.window is None:
            memory.window = window
        return memory

    def _set_hot(self, hot: bool) -> None:
        self._hot = bool(hot)
        set_hot = getattr(self._feed, "set_hot", None)
        if set_hot is not None:
            try:
                set_hot(self._hot)
            except Exception:
                pass

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
        self._hot_gap = FEED_SILENT_HOT_S
        self._set_hot(False)

    def _watch_feed(self, now: float, window: OracleWindow) -> None:
        """Force a reconnect when a tracked bag gets no sample for too long."""
        if self._last_arrival is None:
            return
        silent = now - self._last_arrival
        since = self._watchdog_at if self._watchdog_at is not None else self._last_arrival
        if self._hot:
            limit, gap = FEED_SILENT_HOT_S, self._hot_gap
        else:
            limit, gap = FEED_SILENT_RECONNECT_S, self._watchdog_gap
        if silent < limit or now - since < gap:
            return
        reconnect = getattr(self._feed, "reconnect", None)
        if reconnect is None:
            return
        try:
            closed = bool(reconnect(f"watchdog: silent {silent:.0f}s"))
        except Exception as exc:
            closed = False
            self._fail(f"feed: reconnect {exc}", now=now, window=window, kind="feed")
        if self._hot:
            self._hot_gap = min(FEED_HOT_WATCHDOG_CAP_S, self._hot_gap * 2.0)
            next_check = self._hot_gap
        else:
            self._watchdog_gap = min(FEED_WATCHDOG_CAP_S, self._watchdog_gap * 2.0)
            next_check = self._watchdog_gap
        self._watchdog_at = now
        self._event(
            "oracle_feed_watchdog",
            now=now,
            window=window,
            fields={
                "silent_s": round(silent, 1),
                "closed": closed,
                "hot": self._hot,
                "next_check_s": next_check,
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

    def _note_boundaries(self, samples: list[TwapSample], latest: Optional[TwapSample]) -> None:
        """Remember the first value observed at each 15m boundary second.

        The first RTDS TWAP boundary sample is the window open reference.
        Later samples for that second are retained in the tape but cannot
        mutate the open reference."""
        ordered: list[TwapSample] = list(samples)
        if latest is not None:
            ordered.append(latest)
        for sample in ordered:
            key = boundary_second(getattr(sample, "obs_ts", None))
            if key is None or not getattr(sample, "twap", None):
                continue
            if key not in self._boundary:
                self._boundary[key] = str(sample.twap)
        while len(self._boundary) > BOUNDARY_KEEP:
            self._boundary.pop(min(self._boundary))

    def _resolve_boundaries(self, window: OracleWindow, memory: _WindowMemory, now: float) -> None:
        start_value = self._boundary.get(int(round(window.start_ts)))
        # First RTDS TWAP open_ref is sticky. Later boundary values must not
        # mutate it (live/crypto spot overwrote strike in prod). Gamma may
        # still correct via the dedicated check path.
        if (
            start_value is not None
            and memory.open_source not in (STRIKE_GAMMA, STRIKE_RTDS)
        ):
            self._set_strike(window, memory, now, start_value, STRIKE_RTDS, source=RTDS_SOURCE)
        end_value = self._boundary.get(int(round(window.end_ts)))
        if end_value is not None and memory.close_source != CLOSE_GAMMA:
            if memory.close_source != CLOSE_RTDS or memory.close_twap != end_value:
                self._set_close(window, memory, now, end_value, CLOSE_RTDS, source=RTDS_SOURCE)
        if (
            memory.open_source in (None, STRIKE_CRYPTO)
            and not memory.strike_late_logged
            and window.start_ts + STRIKE_WAIT_S <= now < window.end_ts
        ):
            memory.strike_late_logged = True
            self._event(
                "oracle_strike_late",
                now=now,
                window=window,
                fields={
                    "reason": "no_boundary_sample",
                    "wait_s": STRIKE_WAIT_S,
                    "fallback": STRIKE_CRYPTO,
                },
            )

    def _set_strike(
        self,
        window: OracleWindow,
        memory: _WindowMemory,
        now: float,
        value: str,
        strike_source: str,
        *,
        source: str,
        announce: bool = True,
    ) -> None:
        previous, previous_source = memory.open_ref, memory.open_source
        memory.open_ref = value
        memory.open_source = strike_source
        memory.open_delay_s = round(now - window.start_ts, 3)
        extra: dict[str, Any] = {
            "strike_source": strike_source,
            "capture_delay_s": memory.open_delay_s,
        }
        if previous is not None:
            extra["previous"] = previous
            extra["previous_source"] = previous_source
        self._append(
            build_oracle_row(
                now=now,
                window=window,
                source=source,
                event="oracle_open_ref",
                twap=value,
                twap_ts=window.start_ts,
                open_ref=value,
                notes="open_ref",
                extra=extra,
            )
        )
        if previous is None or not announce:
            return
        diff = usd_diff(value, previous)
        self._event(
            "oracle_strike_revised",
            now=now,
            window=window,
            fields={
                "strike": value,
                "strike_source": strike_source,
                "previous": previous,
                "previous_source": previous_source,
                "delta": None if diff is None else float(diff),
            },
        )

    def _set_close(
        self,
        window: OracleWindow,
        memory: _WindowMemory,
        now: float,
        value: str,
        close_source: str,
        *,
        source: str,
    ) -> None:
        previous, previous_source = memory.close_twap, memory.close_source
        memory.close_twap = value
        memory.close_source = close_source
        memory.end_logged = True
        extra: dict[str, Any] = {
            "close_source": close_source,
            "capture_delay_s": round(now - window.end_ts, 3),
        }
        if previous is not None:
            extra["previous"] = previous
            extra["previous_source"] = previous_source
        self._append(
            build_oracle_row(
                now=now,
                window=window,
                source=source,
                event="oracle_window_end",
                twap=value,
                twap_ts=window.end_ts,
                open_ref=memory.open_ref,
                notes="window_end",
                extra=extra,
            )
        )

    def _record_window(
        self,
        window: OracleWindow,
        samples: list[TwapSample],
        latest: Optional[TwapSample],
        now: float,
    ) -> None:
        memory = self._window_memory(window)
        self._resolve_boundaries(window, memory, now)
        self._maybe_open_fallback(window, memory, now)
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
        key = (window.condition_id, "rtds", int(round(sample.obs_ts * 1000.0)), str(sample.twap))
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
                extra=self._row_extra(sample),
            )
        )
        return True

    def _row_extra(self, sample: TwapSample) -> Optional[dict]:
        extra: dict = {}
        if sample.recv_ts is not None:
            extra["recv_ts"] = round(float(sample.recv_ts), 3)
        latest_live = getattr(self._feed, "latest_live", None)
        if latest_live is not None:
            try:
                live = latest_live()
            except Exception:
                live = None
            if live is not None:
                extra["live_price"] = live.price
                extra["live_ts"] = live.obs_ts
        return extra or None

    def _maybe_open_fallback(self, window: OracleWindow, memory: _WindowMemory, now: float) -> None:
        """crypto-price ``openPrice`` only when no boundary sample came by
        ``STRIKE_WAIT_S`` and the window is still live. One request in flight."""
        slot = memory.open_http
        if memory.open_ref is not None or slot.inflight or slot.done:
            return
        first = window.start_ts + STRIKE_WAIT_S
        if now < first or now >= window.end_ts:
            return
        if not slot.armed:
            slot.armed = True
            slot.next_at = max(slot.next_at, first + self._jitter_s())
        if now < slot.next_at:
            return
        price = self._http_call(
            slot, window, now,
            lambda: self._fetch_price(int(window.start_ts)),
            label="crypto-price", expect=WindowPrice, base_s=HTTP_RETRY_S,
            cap_s=HTTP_BACKOFF_CAP_S,
        )
        if price is None:
            return
        if price.open_ref and memory.open_ref is None:
            slot.done = True
            self._set_strike(window, memory, now, price.open_ref, STRIKE_CRYPTO, source=HTTP_SOURCE)
            return
        slot.next_at = now + HTTP_RETRY_S + self._jitter_s()

    def _maybe_check(self, now: float) -> None:
        """Gamma reconciliation after the window. At most one request per tick."""
        for memory in list(self._memory.values()):
            window = memory.window
            if window is None or memory.check_done:
                continue
            slot = memory.check_http
            first = window.end_ts + GAMMA_FIRST_S
            if slot.inflight or now < first:
                continue
            if now > window.end_ts + GAMMA_GIVE_UP_S:
                self._check_gave_up(window, memory, now)
                continue
            if not slot.armed:
                slot.armed = True
                slot.next_at = max(slot.next_at, first + self._jitter_s())
            if now < slot.next_at:
                continue
            result = self._http_call(
                slot, window, now,
                lambda: self._fetch_gamma(gamma_event_slug(window.start_ts)),
                label="gamma", expect=GammaStrike, base_s=GAMMA_RETRY_S,
                cap_s=GAMMA_BACKOFF_CAP_S,
            )
            if result is not None:
                self._check_ok(window, memory, now, result)
            return

    def _check_ok(
        self, window: OracleWindow, memory: _WindowMemory, now: float, result: GammaStrike
    ) -> None:
        slot = memory.check_http
        ptb, final = result.price_to_beat, result.final_price
        if ptb is not None and not memory.strike_checked:
            memory.strike_checked = True
            ours, ours_source = memory.open_ref, memory.open_source
            diff = usd_diff(ours, ptb)
            match = diff is not None and abs(diff) < STRIKE_MATCH_USD
            self._event(
                "oracle_strike_check",
                now=now,
                window=window,
                fields={
                    "strike": ours,
                    "strike_source": ours_source,
                    "price_to_beat": ptb,
                    "diff": None if diff is None else float(diff),
                    "match": match,
                    "capture_delay_s": memory.open_delay_s,
                },
            )
            if not match:
                self._set_strike(
                    window, memory, now, ptb, STRIKE_GAMMA, source=GAMMA_SOURCE, announce=False,
                )
        if final is not None and not memory.close_checked:
            memory.close_checked = True
            ours, ours_source = memory.close_twap, memory.close_source
            diff = usd_diff(ours, final)
            match = diff is not None and abs(diff) < STRIKE_MATCH_USD
            self._event(
                "oracle_close_check",
                now=now,
                window=window,
                fields={
                    "close": ours,
                    "close_source": ours_source,
                    "final_price": final,
                    "diff": None if diff is None else float(diff),
                    "match": match,
                },
            )
            if not match:
                self._set_close(window, memory, now, final, CLOSE_GAMMA, source=GAMMA_SOURCE)
        if memory.strike_checked and memory.close_checked:
            memory.check_done = True
            slot.done = True
            return
        if ptb is None and final is None:
            slot.counts["unpublished"] = int(slot.counts.get("unpublished", 0)) + 1
        slot.next_at = now + GAMMA_RETRY_S + self._jitter_s()

    def _check_gave_up(self, window: OracleWindow, memory: _WindowMemory, now: float) -> None:
        memory.check_done = True
        slot = memory.check_http
        missing = [
            name
            for name, done in (("price_to_beat", memory.strike_checked), ("final_price", memory.close_checked))
            if not done
        ]
        self._event(
            "oracle_check_missed",
            now=now,
            window=window,
            fields={
                "missing": missing,
                "strike": memory.open_ref,
                "strike_source": memory.open_source,
                "attempts": slot.attempts,
                "counts": dict(sorted(slot.counts.items())),
            },
        )

    def _http_call(
        self,
        slot: _HttpSlot,
        window: OracleWindow,
        now: float,
        call: Callable[[], Any],
        *,
        label: str,
        expect: type,
        base_s: float,
        cap_s: float,
    ) -> Any:
        """Run one request for ``slot``; failures back off and log once per kind."""
        slot.inflight = True
        slot.attempts += 1
        try:
            try:
                result = call()
                if not isinstance(result, expect):
                    raise ValueError("bad payload")
            except Exception as exc:
                self._slot_failed(slot, window, now, exc, label=label, base_s=base_s, cap_s=cap_s)
                return None
            slot.n429 = 0
            return result
        finally:
            slot.inflight = False

    def _jitter_s(self) -> float:
        try:
            value = float(self._jitter())
        except Exception:
            return 0.0
        return min(HTTP_JITTER_S, max(0.0, value))

    def _slot_failed(
        self,
        slot: _HttpSlot,
        window: OracleWindow,
        now: float,
        exc: BaseException,
        *,
        label: str,
        base_s: float,
        cap_s: float,
    ) -> None:
        kind, retry_after = classify_http_error(exc)
        slot.counts[kind] = int(slot.counts.get(kind, 0)) + 1
        slot.n429 = slot.n429 + 1 if kind == "http_429" else 0
        delay = http_retry_delay_s(kind, slot.n429, retry_after, base_s=base_s, cap_s=cap_s)
        slot.next_at = now + delay + self._jitter_s()
        if kind in slot.logged:
            return
        slot.logged.add(kind)
        hint = f"; retry_after={retry_after:.0f}s" if retry_after is not None else ""
        self._fail(
            f"{label} {kind}: {exc} (retry in {delay:.0f}s{hint}; once per window)",
            now=now,
            window=window,
            kind=f"http:{label}:{window.condition_id}:{kind}",
        )

    def _finish_windows(self, now: float) -> None:
        for cid in list(self._memory):
            memory = self._memory[cid]
            window = memory.window
            if window is None:
                continue
            past_close = now - (window.end_ts + CLOSE_GRACE_S)
            if past_close > 0 and not memory.end_logged and not memory.end_final:
                memory.end_final = True
                self._event(
                    "oracle_window_end_missed",
                    now=now,
                    window=window,
                    fields={
                        "outcome": "no_boundary_sample",
                        "after_end_s": round(now - window.end_ts, 1),
                    },
                )
            past_check = now - (window.end_ts + GAMMA_GIVE_UP_S)
            if past_check > 60.0 or (memory.check_done and past_close > 60.0):
                del self._memory[cid]

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
