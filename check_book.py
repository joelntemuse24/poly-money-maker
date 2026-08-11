import json
import time
from datetime import datetime

import requests


def get_json(url, **kwargs):
    response = requests.get(url, timeout=10, **kwargs)
    response.raise_for_status()
    return response.json()


now = time.time() * 1000
events = get_json(
    "https://gamma-api.polymarket.com/events",
    params={
        "series_slug": "btc-up-or-down-hourly",
        "active": "true",
        "closed": "false",
        "limit": 20,
    },
)
if not isinstance(events, list):
    raise RuntimeError("Gamma events response was not a list")

for ev in events:
    if not isinstance(ev, dict):
        continue
    for market in ev.get("markets") or []:
        if not isinstance(market, dict):
            continue
        end = market.get("endDate") or market.get("end_date")
        if not end:
            continue
        try:
            end_ts = (
                datetime.fromisoformat(str(end).replace("Z", "+00:00")).timestamp()
                * 1000
            )
        except (TypeError, ValueError, OverflowError, OSError):
            continue
        mins = (end_ts - now) / 60000
        if not 120 < mins < 180:
            continue
        raw_tokens = market.get("clobTokenIds")
        try:
            tokens = (
                json.loads(raw_tokens)
                if isinstance(raw_tokens, str)
                else raw_tokens
            )
        except (TypeError, ValueError):
            continue
        if not isinstance(tokens, list):
            continue
        print(f"Market: {str(market.get('question') or '')[:40]}... mins={mins:.0f}")
        for i, token_id in enumerate(tokens):
            book = get_json(
                "https://clob.polymarket.com/book",
                params={"token_id": token_id},
            )
            price_buy = get_json(
                "https://clob.polymarket.com/price",
                params={"token_id": token_id, "side": "BUY"},
            )
            price_sell = get_json(
                "https://clob.polymarket.com/price",
                params={"token_id": token_id, "side": "SELL"},
            )

            asks = book.get("asks", []) if isinstance(book, dict) else []
            bids = book.get("bids", []) if isinstance(book, dict) else []
            best_ask = min(asks, key=lambda x: float(x["price"])) if asks else None
            best_bid = max(bids, key=lambda x: float(x["price"])) if bids else None

            print(f"  Token {i}:")
            print(f"    get_price BUY:  {price_buy}")
            print(f"    get_price SELL: {price_sell}")
            print(f"    Best ask: {best_ask}")
            print(f"    Best bid: {best_bid}")
        raise SystemExit(0)
