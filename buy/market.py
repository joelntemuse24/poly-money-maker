from __future__ import annotations

import json
import math
import re
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, Iterable, Optional

import requests


@dataclass(frozen=True)
class MintMarket:
    condition_id: str
    slug: str
    question: str
    end_ts: float
    series_slug: str
    up_token: str
    dn_token: str
    active: bool
    closed: bool
    accepting_orders: bool
    neg_risk: bool
    start_ts: float = 0.0

    def ttm_minutes(self, now: Optional[float] = None) -> float:
        return (self.end_ts - (time.time() if now is None else now)) / 60.0

    def minutes_to_start(self, now: Optional[float] = None) -> float:
        return (self.start_ts - (time.time() if now is None else now)) / 60.0


def _list(value: Any) -> list:
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, list) else []
        except (TypeError, ValueError):
            return []
    return []


def _end_timestamp(value: str) -> float:
    try:
        timestamp = datetime.fromisoformat(
            value.replace("Z", "+00:00")
        ).timestamp()
    except (TypeError, ValueError, OverflowError, OSError) as exc:
        raise ValueError("invalid timestamp") from exc
    if not math.isfinite(timestamp):
        raise ValueError("non-finite timestamp")
    return timestamp


def _bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).lower() in ("1", "true", "yes")


_SLUG_TS_RE = re.compile(r"-(\d{10,})$")
_SERIES_DURATION_S = {
    "btc-up-or-down-5m": 5 * 60,
    "btc-up-or-down-15m": 15 * 60,
    "btc-up-or-down-hourly": 60 * 60,
}
FIVE_M_DURATION_S = _SERIES_DURATION_S["btc-up-or-down-5m"]


def slug_start_ts(slug: str) -> Optional[float]:
    """Extract the real market start timestamp from a slug like
    'btc-updown-5m-1784638800'. Returns None if no timestamp found."""
    m = _SLUG_TS_RE.search(slug or "")
    if m:
        ts = int(m.group(1))
        if ts > 1_700_000_000:
            return float(ts)
    return None


def _slug_start_ts(slug: str) -> Optional[float]:
    return slug_start_ts(slug)


def slug_window_end_ts(slug: str, duration_s: float = FIVE_M_DURATION_S) -> Optional[float]:
    """True 5m/15m/hourly close: slug unix start + series duration."""
    start = slug_start_ts(slug)
    if start is None:
        return None
    try:
        return float(start) + float(duration_s)
    except (TypeError, ValueError):
        return None


def entry_seconds_left(
    now_s,
    gamma_end_ts,
    slug=None,
    duration_s: float = FIVE_M_DURATION_S,
) -> float:
    """TTM for entry/hedge clocks.

    Gamma ``endDate`` can sit several seconds off ``slug+300``. Live 22 Aug
    late 91–99 posts hugged TTM 113–120: Gamma still said early (99¢ FAK)
    while the slug clock was already inside the last 120s. Use the
    **earliest** close so a 93¢ ask at slug-TTM 116 cannot POST.
    """
    ends = []
    try:
        if gamma_end_ts not in (None, ""):
            ends.append(float(gamma_end_ts))
    except (TypeError, ValueError):
        pass
    slug_end = slug_window_end_ts(slug, duration_s) if slug else None
    if slug_end is not None:
        ends.append(float(slug_end))
    if not ends:
        return 0.0
    try:
        now = float(now_s)
    except (TypeError, ValueError):
        return 0.0
    return min(ends) - now


def _parse_event(event: dict, series_slug: str) -> Iterable[MintMarket]:
    for market in event.get("markets") or []:
        end = market.get("endDate") or event.get("endDate")
        start = market.get("startDate") or event.get("startDate") or end
        condition_id = market.get("conditionId") or market.get("condition_id")
        tokens = _list(market.get("clobTokenIds"))
        outcomes = [str(value).lower() for value in _list(market.get("outcomes"))]
        if (
            not end
            or not condition_id
            or len(tokens) != 2
            or len(set(str(token) for token in tokens)) != 2
            or len(outcomes) != 2
        ):
            continue
        mapping = dict(zip(outcomes, (str(token) for token in tokens)))
        if "up" not in mapping or "down" not in mapping:
            continue
        try:
            end_ts = _end_timestamp(str(end))
        except (TypeError, ValueError):
            continue
        slug = str(market.get("slug") or event.get("slug") or condition_id)
        duration_s = _SERIES_DURATION_S.get(series_slug)
        if duration_s is None:
            continue
        slug_start = slug_start_ts(slug)
        if slug_start is not None:
            # Slug unix is the real open. Pin the close to slug+duration so
            # a late Gamma endDate cannot keep the early 99¢ FAK armed after
            # T-120 (live 22 Aug late_gt_90 at TTM 113–120).
            start_ts = slug_start
            end_ts = slug_start + duration_s
        else:
            try:
                api_start_ts = _end_timestamp(str(start))
            except (TypeError, ValueError):
                api_start_ts = end_ts
            # Gamma sometimes exposes event creation time as startDate. Derive
            # the actual window from the requested series whenever the supplied
            # duration is inconsistent, not only when it is hours too old.
            api_duration = end_ts - api_start_ts
            duration_tolerance = max(5.0, duration_s * 0.05)
            if abs(api_duration - duration_s) > duration_tolerance:
                start_ts = end_ts - duration_s
            else:
                start_ts = api_start_ts
        # A wrong series/slug association changes the Price-To-Beat window.
        # Reject metadata that cannot represent the requested cadence.
        observed_duration = end_ts - start_ts
        if (
            not math.isfinite(start_ts)
            or start_ts <= 0
            or end_ts <= start_ts
            or abs(observed_duration - duration_s) > max(5.0, duration_s * 0.05)
        ):
            continue
        yield MintMarket(
            condition_id=str(condition_id),
            slug=slug,
            question=str(market.get("question") or event.get("title") or ""),
            end_ts=end_ts,
            series_slug=series_slug,
            up_token=mapping["up"],
            dn_token=mapping["down"],
            active=_bool(market.get("active", event.get("active", False))),
            closed=_bool(market.get("closed", event.get("closed", False))),
            accepting_orders=_bool(market.get("acceptingOrders", False)),
            neg_risk=_bool(market.get("negRisk", event.get("negRisk", False))),
            start_ts=start_ts,
        )


def market_is_known_for_buy(market) -> bool:
    """True when we already have CLOB tokens and a close time for this market."""
    if market is None:
        return False
    try:
        end_ts = float(getattr(market, "end_ts", 0) or 0)
    except (TypeError, ValueError):
        return False
    if not math.isfinite(end_ts) or end_ts <= 0:
        return False
    up = str(getattr(market, "up_token", "") or "").strip()
    dn = str(getattr(market, "dn_token", "") or "").strip()
    return bool(up) and bool(dn) and up != dn


def discovery_allows_buy_look(
    discovery_fresh: bool,
    *,
    in_live_window: bool,
    market,
) -> bool:
    """Gamma age must not veto a known market already in its live buy window.

    Fresh discovery still allows the CLOB look (other gates apply). A stale
    Gamma snapshot only blocks markets we never stored tokens/end_ts for.
    """
    if discovery_fresh:
        return True
    return bool(in_live_window) and market_is_known_for_buy(market)


class MarketGateway:
    def __init__(
        self,
        *,
        gamma_url: str,
        data_api_url: str,
        timeout: float = 15.0,
        session: requests.Session | None = None,
        discover_cache_s: float = 25.0,
        stale_cache_s: float = 120.0,
    ):
        self.gamma_url = gamma_url.rstrip("/")
        self.data_api_url = data_api_url.rstrip("/")
        self.timeout = timeout
        self.session = session or requests.Session()
        self.session.headers.update({"User-Agent": "poly-money-maker-mint-buyer/0.1"})
        self._discover_cache: dict = {"ts": 0.0, "key": "", "markets": []}
        self.discover_cache_s = discover_cache_s
        self.stale_cache_s = stale_cache_s
        self.discovery_fresh = False

    def discover(self, series_slugs: list[str]) -> list[MintMarket]:
        cache_key = ",".join(series_slugs)
        now = time.time()
        if (
            self._discover_cache["markets"]
            and self._discover_cache["key"] == cache_key
            and (now - self._discover_cache["ts"]) < self.discover_cache_s
        ):
            self.discovery_fresh = True
            return list(self._discover_cache["markets"])

        markets: Dict[str, MintMarket] = {}
        for series_slug in series_slugs:
            try:
                response = self.session.get(
                    f"{self.gamma_url}/events",
                    params={
                        "series_slug": series_slug,
                        "active": "true",
                        "closed": "false",
                        "limit": "200",
                    },
                    timeout=self.timeout,
                )
                response.raise_for_status()
                payload = response.json()
                events = payload if isinstance(payload, list) else [payload]
                for event in events:
                    if not isinstance(event, dict):
                        continue
                    for market in _parse_event(event, series_slug):
                        markets[market.condition_id] = market
            except Exception:
                pass

        if not markets and (
            self._discover_cache["markets"]
            and self._discover_cache["key"] == cache_key
            and (now - self._discover_cache["ts"]) <= self.stale_cache_s
        ):
            self.discovery_fresh = False
            return list(self._discover_cache["markets"])

        result = sorted(markets.values(), key=lambda market: market.end_ts)
        if result:
            self._discover_cache = {"ts": now, "key": cache_key, "markets": result}
            self.discovery_fresh = True
        else:
            self.discovery_fresh = False
        return result

    def positions(self, funder_address: str) -> Dict[str, float]:
        response = self.session.get(
            f"{self.data_api_url}/positions",
            params={"user": funder_address, "limit": 500},
            timeout=self.timeout,
        )
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, list):
            raise ValueError("positions response was not a list")
        balances: Dict[str, float] = {}
        for position in payload:
            token_id = position.get("asset")
            if token_id is None:
                continue
            balances[str(token_id)] = float(position.get("size") or 0)
        return balances

