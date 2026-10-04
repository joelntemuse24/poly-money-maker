"""FAK buy dispatch. Dry-run never touches the CLOB client."""

from __future__ import annotations

import time
from typing import Any, Optional

from buy.lock_fair import taker_fee
from buy.lock_gates import buy_spent_usd, execute_buy
from buy.sister_bid import buy_matched_shares


def post_fak_buy(client: Any, plan: dict) -> dict:
    """Sign and post one FAK buy. ``amount`` is USDC notional, limit is the ask."""
    from py_clob_client_v2 import MarketOrderArgs, OrderType
    from py_clob_client_v2.order_builder.constants import BUY

    started = time.perf_counter()
    signed = client.create_market_order(
        MarketOrderArgs(
            token_id=str(plan["token_id"]),
            amount=float(plan["notional"]),
            side=BUY,
            price=float(plan["limit"]),
            order_type=OrderType.FAK,
        )
    )
    result = client.post_order(signed, order_type=OrderType.FAK)
    if not isinstance(result, dict):
        result = {"raw": str(result)[:500]}
    result["decision_to_order_ms"] = (time.perf_counter() - started) * 1000.0
    result["posted"] = True
    return result


def dispatch_buy(plan: dict, *, dry_run: bool, client: Any = None) -> dict:
    """Paper-fill against the in-memory book, or post a live FAK.

    ``client`` is not read when ``dry_run`` is true.
    """
    started = time.perf_counter()
    if dry_run:
        fill = execute_buy(plan, dry_run=True, poster=None)
    else:
        if client is None:
            raise RuntimeError("live buy requires a clob client")
        fill = execute_buy(plan, dry_run=False, poster=lambda item: post_fak_buy(client, item))
    fill.setdefault("decision_to_order_ms", (time.perf_counter() - started) * 1000.0)
    return fill


def normalize_fill(fill: dict, plan: dict, cfg: dict) -> dict:
    """Shares, dollars, and the modelled taker fee for one buy result."""
    if fill.get("dry_run"):
        shares = float(fill.get("shares") or 0.0)
        cost = float(fill.get("cost") or 0.0)
        vwap = fill.get("vwap")
    else:
        shares = buy_matched_shares(fill, float(plan.get("shares") or 0.0))
        cost = buy_spent_usd(fill, float(plan.get("notional") or 0.0))
        vwap = (cost / shares) if shares > 0 and cost > 0 else plan.get("limit")
    price = float(vwap if vwap else plan.get("limit") or 0.0)
    fee = taker_fee(
        price,
        float(cfg.get("taker_fee_rate") or 0.07),
        float(cfg.get("taker_fee_exponent") or 1.0),
    ) * shares
    return {
        "shares": shares,
        "cost": cost,
        "vwap": vwap,
        "fee": fee,
        "posted": bool(fill.get("posted")),
        "dry_run": bool(fill.get("dry_run")),
        "decision_to_order_ms": fill.get("decision_to_order_ms"),
        "raw_status": fill.get("status") or fill.get("errorMsg") or fill.get("error"),
    }


def build_clob_client() -> Optional[Any]:
    """Mintbot's signing setup, constructed only for a live run."""
    import os

    private_key = os.getenv("PRIVATE_KEY") or ""
    funder = os.getenv("FUNDER_ADDRESS") or ""
    if not private_key or not funder:
        return None
    from py_clob_client_v2 import ApiCreds, ClobClient

    host = "https://clob.polymarket.com"
    chain_id = int(os.getenv("CHAIN_ID") or 137)
    api_key = os.getenv("API_KEY") or ""
    api_secret = os.getenv("API_SECRET") or ""
    api_passphrase = os.getenv("API_PASSPHRASE") or ""
    if api_key and api_secret and api_passphrase:
        creds = ApiCreds(api_key=api_key, api_secret=api_secret, api_passphrase=api_passphrase)
    else:
        creds = ClobClient(host=host, key=private_key, chain_id=chain_id).create_or_derive_api_key()
    return ClobClient(
        host=host,
        key=private_key,
        chain_id=chain_id,
        creds=creds,
        signature_type=1,
        funder=funder,
    )
