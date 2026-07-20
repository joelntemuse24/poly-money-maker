from __future__ import annotations

import json
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

    def ttm_minutes(self, now: Optional[float] = None) -> float:
        return (self.end_ts - (time.time() if now is None else now)) / 60.0


def _list(value: Any) -> list:
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        parsed = json.loads(value)
        return parsed if isinstance(parsed, list) else []
    return []


def _end_timestamp(value: str) -> float:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()


def _bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).lower() in ("1", "true", "yes")


def _parse_event(event: dict, series_slug: str) -> Iterable[MintMarket]:
    for market in event.get("markets") or []:
        end = market.get("endDate") or event.get("endDate")
        condition_id = market.get("conditionId") or market.get("condition_id")
        tokens = _list(market.get("clobTokenIds"))
        outcomes = [str(value).lower() for value in _list(market.get("outcomes"))]
        if not end or not condition_id or len(tokens) != 2 or len(outcomes) != 2:
            continue
        mapping = dict(zip(outcomes, (str(token) for token in tokens)))
        if "up" not in mapping or "down" not in mapping:
            continue
        try:
            end_ts = _end_timestamp(str(end))
        except (TypeError, ValueError):
            continue
        yield MintMarket(
            condition_id=str(condition_id),
            slug=str(market.get("slug") or event.get("slug") or condition_id),
            question=str(market.get("question") or event.get("title") or ""),
            end_ts=end_ts,
            series_slug=series_slug,
            up_token=mapping["up"],
            dn_token=mapping["down"],
            active=_bool(market.get("active", event.get("active", False))),
            closed=_bool(market.get("closed", event.get("closed", False))),
            accepting_orders=_bool(market.get("acceptingOrders", False)),
            neg_risk=_bool(market.get("negRisk", event.get("negRisk", False))),
        )


class MarketGateway:
    def __init__(
        self,
        *,
        gamma_url: str,
        data_api_url: str,
        geoblock_url: str,
        timeout: float = 15.0,
        session: requests.Session | None = None,
    ):
        self.gamma_url = gamma_url.rstrip("/")
        self.data_api_url = data_api_url.rstrip("/")
        self.geoblock_url = geoblock_url
        self.timeout = timeout
        self.session = session or requests.Session()
        self.session.headers.update({"User-Agent": "poly-money-maker-mint-buyer/0.1"})

    def discover(self, series_slugs: list[str]) -> list[MintMarket]:
        markets: Dict[str, MintMarket] = {}
        for series_slug in series_slugs:
            response = self.session.get(
                f"{self.gamma_url}/events",
                params={
                    "series_slug": series_slug,
                    "active": "true",
                    "closed": "false",
                    "limit": "80",
                },
                timeout=self.timeout,
            )
            response.raise_for_status()
            payload = response.json()
            events = payload if isinstance(payload, list) else [payload]
            for event in events:
                if not isinstance(event, dict):
                    raise ValueError("Gamma events response contained a non-object event")
                for market in _parse_event(event, series_slug):
                    markets[market.condition_id] = market
        return sorted(markets.values(), key=lambda market: market.end_ts)

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

    def geoblock(self) -> dict:
        response = self.session.get(self.geoblock_url, timeout=self.timeout)
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict) or "blocked" not in payload:
            raise ValueError("invalid geoblock response")
        return payload
