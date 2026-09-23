#!/usr/bin/env python3
"""Sister scrap bidder (wallet B). Bids only. Never mints. Never sells.

Reads mintbot's ``positions_mint.json`` so a token A still holds is not bid.
Credentials come from ``.env.complement`` only (POLY_1271 / deposit wallet).
This process does not import ``mintbot`` and does not load ``.env``.

Stays idle unless ``strategy_scrapbid.json`` sets ``bid_enabled`` true.
``dry_run`` true logs the bid and does not post. Do not start this unit
unless the operator asks.
"""

from __future__ import annotations

import fcntl
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Optional

import requests
from dotenv import load_dotenv
from eth_utils import to_checksum_address

from buy.book import best_bid_with_min_size, best_from_levels
from buy.market import MarketGateway
from buy.mint_sell import rest_order_matched_shares
from buy.sister_bid import (
    SISTER_DEFAULTS,
    buy_matched_shares,
    markets_needing_books,
    plan_dump_hedges,
    plan_sister_bids,
    sister_dump_on,
    sister_scrap_on,
    resolve_sister_client_config,
    sister_book_wanted,
    sister_miss_events,
    sister_poll_s,
)
from buy.sister_topup import place_error_is_broke


REPO = Path(__file__).resolve().parent
STRATEGY_FILE = REPO / "strategy_scrapbid.json"
EXAMPLE_FILE = REPO / "strategy_scrapbid.example.json"
MINT_STATE_FILE = REPO / "positions_mint.json"
STATE_FILE = REPO / "positions_scrapbid.json"
LOCK_FILE = REPO / ".scrapbid.lock"
STOP_FILE = REPO / "STOP_SCRAPBID"
LOG_FILE = REPO / "scrapbid.log"

_shutdown = False
_client = None


def _log(event: str, **kwargs: Any) -> None:
    import logging

    payload = {"ts": time.time(), "event": event, **kwargs}
    logging.getLogger("scrapbid").info(json.dumps(payload, default=str))


def _setup_log() -> None:
    import logging
    from logging.handlers import RotatingFileHandler

    logger = logging.getLogger("scrapbid")
    logger.setLevel(logging.INFO)
    logger.handlers.clear()
    fmt = logging.Formatter("%(message)s")
    fh = RotatingFileHandler(LOG_FILE, maxBytes=1_000_000, backupCount=2)
    fh.setFormatter(fmt)
    logger.addHandler(fh)
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    logger.addHandler(sh)


def _handle_signal(signum, frame) -> None:
    global _shutdown
    _shutdown = True


def load_strategy(path: Path = STRATEGY_FILE) -> dict:
    if not path.exists():
        raise FileNotFoundError(
            f"missing {path.name} — copy {EXAMPLE_FILE.name}"
        )
    raw = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(raw, dict):
        raise ValueError("strategy_scrapbid.json must be an object")
    cfg = dict(SISTER_DEFAULTS)
    for key, value in raw.items():
        if key in cfg:
            cfg[key] = value
    if isinstance(cfg["series_slugs"], str):
        cfg["series_slugs"] = [
            part.strip() for part in cfg["series_slugs"].split(",") if part.strip()
        ]
    shares = float(cfg["shares"])
    px = float(cfg["bid_max_px"])
    if not 0 < shares <= 20:
        raise ValueError("shares must be in (0, 20]")
    if not 0 < px <= 0.10:
        raise ValueError("bid_max_px must be in (0, 0.10]")
    rest_px = float(cfg["bid_rest_px"])
    if not 0 < rest_px <= px + 1e-12:
        raise ValueError("bid_rest_px must be in (0, bid_max_px]")
    fak_min = float(cfg["bid_fak_min_notional"])
    if fak_min <= 0:
        raise ValueError("bid_fak_min_notional must be > 0")
    fak_max = float(cfg["bid_fak_max_notional"])
    if fak_max + 1e-12 < fak_min:
        raise ValueError("bid_fak_max_notional must be >= bid_fak_min_notional")
    # A copied example that still says 60s would post a GTD Polymarket rejects.
    if float(cfg["min_gtd_ahead_s"]) < 180:
        cfg["min_gtd_ahead_s"] = 180.0
    dump_shares = float(cfg["dump_hedge_shares"])
    if not 0 < dump_shares <= 50:
        raise ValueError("dump_hedge_shares must be in (0, 50]")
    dump_rest = float(cfg["dump_hedge_rest_px"])
    if not 0 < dump_rest < 1:
        raise ValueError("dump_hedge_rest_px must be in (0, 1)")
    dump_min = float(cfg["dump_hedge_fak_min_notional"])
    if dump_min <= 0:
        raise ValueError("dump_hedge_fak_min_notional must be > 0")
    dump_max = float(cfg["dump_hedge_fak_max_notional"])
    if dump_max + 1e-12 < dump_min:
        raise ValueError("dump_hedge_fak_max_notional must be >= dump_hedge_fak_min_notional")
    topup_usd = float(cfg["topup_usd"])
    if not 0 < topup_usd <= 50:
        raise ValueError("topup_usd must be in (0, 50]")
    topup_need = float(cfg["topup_need_usd"])
    if not 0 < topup_need <= 50:
        raise ValueError("topup_need_usd must be in (0, 50]")
    if float(cfg["topup_retry_s"]) < 0:
        raise ValueError("topup_retry_s must be >= 0")
    if float(cfg["topup_check_s"]) < 1:
        raise ValueError("topup_check_s must be >= 1")
    if float(cfg["active_ttm_s"]) <= float(cfg["cancel_ttm_s"]):
        raise ValueError("active_ttm_s must be > cancel_ttm_s")
    if float(cfg["cancel_ttm_s"]) < 0:
        raise ValueError("cancel_ttm_s must be >= 0")
    if not cfg["series_slugs"]:
        raise ValueError("series_slugs must not be empty")
    return cfg


def _read_json(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _atomic_save(path: Path, payload: dict) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    os.replace(temporary, path)


def merge_markets(discovered: list, intents: dict, now_s: float) -> list:
    """Gamma rows plus any mint intent still inside (or just after) its window."""
    rows: dict[str, dict] = {}
    for market in discovered:
        cid = str(market.get("condition_id") or "")
        if cid:
            rows[cid] = dict(market)
    for cid, intent in (intents or {}).items():
        if not isinstance(intent, dict):
            continue
        end_ts = float(intent.get("end_ts") or 0)
        if cid in rows:
            row = rows[cid]
            if intent.get("up_token"):
                row["up_token"] = intent.get("up_token")
            if intent.get("dn_token"):
                row["dn_token"] = intent.get("dn_token")
            if not row.get("end_ts") and end_ts:
                row["end_ts"] = end_ts
            continue
        if not end_ts or end_ts + 30 < float(now_s):
            continue
        if not intent.get("up_token") or not intent.get("dn_token"):
            continue
        rows[str(cid)] = {
            "condition_id": str(cid),
            "slug": intent.get("slug"),
            "up_token": intent.get("up_token"),
            "dn_token": intent.get("dn_token"),
            "end_ts": end_ts,
        }
    return list(rows.values())


def _fetch_top(token_id: str) -> tuple[Optional[float], Optional[float]]:
    """``(best_bid, best_ask)`` from the public book. Dust under 1 share is ignored."""
    if not token_id:
        return None, None
    try:
        response = requests.get(
            "https://clob.polymarket.com/book",
            params={"token_id": str(token_id)},
            timeout=5,
        )
        if response.status_code != 200:
            return None, None
        payload = response.json()
        book = payload if isinstance(payload, dict) else {}
        bid, _bid_sz = best_bid_with_min_size(book.get("bids") or [], min_size=1.0)
        ask, ask_sz = best_from_levels(book.get("asks") or [], "ask")
        if ask_sz + 1e-12 < 1.0:
            ask = None
        return bid, ask
    except Exception as exc:
        _log("scrapbid_book_fail", token_id=str(token_id)[:18], error=str(exc)[:160])
        return None, None


def _get_client():
    global _client
    if _client is not None:
        return _client
    private_key = os.getenv("PRIVATE_KEY") or ""
    if not private_key:
        raise RuntimeError("missing PRIVATE_KEY in .env.complement")
    conf = resolve_sister_client_config(dict(os.environ))
    from py_clob_client_v2 import ApiCreds, ClobClient

    host = conf["host"]
    chain_id = int(conf["chain_id"])
    api_key = os.getenv("API_KEY") or ""
    api_secret = os.getenv("API_SECRET") or ""
    api_passphrase = os.getenv("API_PASSPHRASE") or ""
    if api_key and api_secret and api_passphrase:
        creds = ApiCreds(
            api_key=api_key,
            api_secret=api_secret,
            api_passphrase=api_passphrase,
        )
    else:
        tmp = ClobClient(host=host, key=private_key, chain_id=chain_id)
        creds = tmp.create_or_derive_api_key()
    _client = ClobClient(
        host=host,
        key=private_key,
        chain_id=chain_id,
        creds=creds,
        signature_type=int(conf["signature_type"]),
        funder=to_checksum_address(conf["funder"]),
    )
    _log(
        "scrapbid_client_ready",
        funder=conf["funder"],
        signature_type=int(conf["signature_type"]),
    )
    return _client


def _place_bid(token_id: str, price: float, shares: float, tif: str, expiration: int):
    """BUY only. ``FAK`` lifts an ask at or under the cap. GTC/GTD rests."""
    from py_clob_client_v2 import OrderArgs, OrderType
    from py_clob_client_v2.order_builder.constants import BUY

    client = _get_client()
    signed = client.create_order(
        OrderArgs(
            token_id=str(token_id),
            price=float(price),
            size=float(shares),
            side=BUY,
            expiration=int(expiration or 0),
        )
    )
    if tif == "FAK":
        order_type = OrderType.FAK
    elif tif == "GTD":
        order_type = OrderType.GTD
    else:
        order_type = OrderType.GTC
    result = client.post_order(signed, order_type=order_type)
    order_id = ""
    status = "posted"
    if isinstance(result, dict):
        order_id = str(result.get("orderID") or result.get("orderId") or result.get("id") or "")
        status = str(result.get("status") or status)
    matched = buy_matched_shares(result, shares)
    return order_id, status, matched


def _cancel_order(order_id: str) -> bool:
    from py_clob_client_v2.clob_types import OrderPayload

    if not order_id or str(order_id).startswith("dry"):
        return True
    try:
        _get_client().cancel_order(OrderPayload(orderID=str(order_id)))
        return True
    except Exception as exc:
        _log("scrapbid_cancel_fail", order_id=str(order_id)[:18], error=str(exc)[:160])
        return False


def _poll_order(order_id: str, shares: float):
    """``(bought_shares, status)``. Status comes from the order; the share
    count ignores BUY ``makingAmount`` (USDC paid).
    """
    if not order_id or str(order_id).startswith("dry"):
        return 0.0, "live"
    try:
        order = _get_client().get_order(str(order_id))
    except Exception as exc:
        _log("scrapbid_poll_fail", order_id=str(order_id)[:18], error=str(exc)[:160])
        return 0.0, "unknown"
    _sell_matched, status = rest_order_matched_shares(order, shares)
    bought = buy_matched_shares(order, shares)
    if bought > 0:
        return bought, status
    if status == "filled" and float(shares or 0) > 0:
        return float(shares), status
    return 0.0, status


def _merged_open_orders(state: dict) -> dict:
    """Scrap and dump rests, so a dump-only order still gets a book quote."""
    merged: dict = {}
    for key in ("orders", "dump_orders"):
        block = state.get(key) or {}
        if not isinstance(block, dict):
            continue
        for cid, legs in block.items():
            slot = merged.setdefault(str(cid), {})
            if isinstance(legs, dict):
                slot.update(legs)
    return merged


def _bucket(state: dict, book: str, kind: str) -> dict:
    """``orders`` / ``filled`` for scrap, ``dump_orders`` / ``dump_filled`` for dump."""
    key = f"dump_{kind}" if book == "dump" else kind
    slot = state.setdefault(key, {})
    if not isinstance(slot, dict):
        slot = {}
        state[key] = slot
    return slot


def _add_filled(state: dict, cid: str, leg: str, shares: float, book: str = "scrap") -> None:
    if shares <= 0 or not cid or not leg:
        return
    filled = _bucket(state, book, "filled")
    slot = filled.setdefault(cid, {})
    if not isinstance(slot, dict):
        slot = {}
        filled[cid] = slot
    try:
        prev = float(slot.get(leg) or 0)
    except (TypeError, ValueError):
        prev = 0.0
    slot[leg] = round(prev + float(shares), 4)


def _sync_collateral() -> None:
    """Refresh B's CLOB pUSD cache so a top-up is spendable on the next place."""
    try:
        from py_clob_client_v2 import AssetType, BalanceAllowanceParams

        _get_client().update_balance_allowance(
            BalanceAllowanceParams(asset_type=AssetType.COLLATERAL)
        )
    except Exception as exc:
        _log("scrapbid_allowance_warn", error=str(exc)[:160])


_last_topup_spawn = 0.0
_last_topup_force = 0.0


def _maybe_topup(
    cfg: dict,
    *,
    force_broke: bool,
    cleared: bool,
    reason: str,
    want: bool,
) -> None:
    """Spawn the A→B top-up script. Dry-run only logs. Throttled per kind."""
    global _last_topup_spawn, _last_topup_force
    if not bool(cfg.get("topup_enabled", False)):
        return
    if not want and not force_broke and not cleared:
        return
    now = time.time()
    wait = float(cfg.get("topup_check_s") or 15)
    if force_broke:
        if now < _last_topup_force + wait:
            return
        _last_topup_force = now
    else:
        if now < _last_topup_spawn + wait:
            return
        _last_topup_spawn = now
    if bool(cfg.get("dry_run", True)):
        _log(
            "sister_topup_dry",
            reason=reason,
            force_broke=bool(force_broke),
            cleared=bool(cleared),
        )
        return
    cmd = [
        sys.executable,
        str(REPO / "sister_topup.py"),
        "--once",
        "--live",
        "--reason",
        str(reason or "hedge"),
    ]
    if force_broke:
        cmd.append("--force-broke")
    if cleared:
        cmd.append("--cleared")
    try:
        subprocess.run(cmd, check=False, timeout=40, cwd=str(REPO))
    except Exception as exc:
        _log("sister_topup_spawn_fail", error=str(exc)[:160])


def apply_actions(actions: list, state: dict, *, dry_run: bool) -> bool:
    """Apply the plan. Returns true when a place failed for balance or allowance."""
    skips = state.setdefault("skip_why", {})
    broke = False
    for action in actions:
        cid = str(action.get("condition_id") or "")
        leg = str(action.get("leg") or "")
        op = action.get("op")
        book = "dump" if action.get("book") == "dump" else "scrap"
        orders = _bucket(state, book, "orders")
        slot = orders.setdefault(cid, {})
        if op == "skip":
            key = f"{book}:{cid}:{leg}"
            reason = action.get("reason")
            if skips.get(key) != reason:
                skips[key] = reason
                _log("scrapbid_skip", book=book, **{k: action.get(k) for k in (
                    "condition_id", "leg", "reason", "slug", "ttm_s",
                )})
            continue
        if op == "cancel":
            oid = str(action.get("order_id") or "")
            if dry_run or _cancel_order(oid):
                slot.pop(leg, None)
                _log(
                    "scrapbid_cancel",
                    condition_id=cid,
                    leg=leg,
                    order_id=oid,
                    reason=action.get("reason"),
                    slug=action.get("slug"),
                    dry_run=dry_run,
                )
            continue
        if op == "keep":
            oid = str(action.get("order_id") or "")
            if dry_run or oid.startswith("dry"):
                continue
            matched, status = _poll_order(oid, float((slot.get(leg) or {}).get("size") or 0))
            if status in {"filled", "cancelled"}:
                if matched > 0:
                    _add_filled(state, cid, leg, matched, book)
                slot.pop(leg, None)
                _log(
                    "scrapbid_done",
                    condition_id=cid,
                    leg=leg,
                    book=book,
                    order_id=oid,
                    status=status,
                    matched=matched,
                    slug=action.get("slug"),
                )
            continue
        if op != "place":
            continue
        price = float(action.get("price") or 0)
        shares = float(action.get("shares") or 0)
        token_id = str(action.get("token_id") or "")
        tif = str(action.get("tif") or "GTC")
        if dry_run:
            oid, status, matched = f"dry-{cid}-{leg}", "dry", 0.0
        else:
            try:
                oid, status, matched = _place_bid(
                    token_id,
                    price,
                    shares,
                    tif,
                    int(action.get("expiration") or 0),
                )
            except Exception as exc:
                message = str(exc)[:200]
                if place_error_is_broke(message):
                    broke = True
                _log(
                    "scrapbid_place_fail",
                    condition_id=cid,
                    leg=leg,
                    book=book,
                    error=message,
                    slug=action.get("slug"),
                    broke=place_error_is_broke(message),
                )
                continue
            if tif == "FAK" and matched <= 0 and oid:
                polled, polled_status = _poll_order(oid, shares)
                if polled > matched:
                    matched = polled
                    status = polled_status or status
        if tif == "FAK" and not dry_run:
            if matched > 0:
                _add_filled(state, cid, leg, matched, book)
            _log(
                "scrapbid_take",
                condition_id=cid,
                leg=leg,
                book=book,
                order_id=oid,
                price=price,
                shares=shares,
                matched=matched,
                tif=tif,
                reason=action.get("reason"),
                price_why=action.get("price_why"),
                tif_why=action.get("tif_why"),
                slug=action.get("slug"),
                status=status,
            )
            continue
        if not oid:
            _log("scrapbid_place_fail", condition_id=cid, leg=leg, book=book, status=status)
            continue
        slot[leg] = {
            "order_id": oid,
            "price": price,
            "size": shares,
            "token_id": token_id,
            "tif": action.get("tif"),
            "placed_at": time.time(),
        }
        _log(
            "scrapbid_place",
            condition_id=cid,
            leg=leg,
            book=book,
            order_id=oid,
            price=price,
            shares=shares,
            tif=action.get("tif"),
            reason=action.get("reason"),
            price_why=action.get("price_why"),
            tif_why=action.get("tif_why"),
            slug=action.get("slug"),
            dry_run=dry_run,
            status=status,
        )
    return broke


def _mint_intents() -> dict:
    mint = _read_json(MINT_STATE_FILE)
    intents = mint.get("intents") if isinstance(mint.get("intents"), dict) else {}
    return intents


def _fill_books(
    markets: list,
    intents: dict,
    open_orders: dict,
    now_s: float,
    cfg: dict,
) -> int:
    """Quote markets that can act. Already-quoted rows are left alone."""
    fetched = 0
    active = float(cfg["active_ttm_s"])
    cancel = float(cfg["cancel_ttm_s"])
    for market in markets:
        cid = str(market.get("condition_id") or "")
        intent = intents.get(cid) if cid else None
        if not sister_book_wanted(
            market,
            intent,
            open_orders,
            now_s=now_s,
            active_ttm_s=active,
            cancel_ttm_s=cancel,
        ):
            continue
        if "up_bid" in market and "dn_bid" in market:
            continue
        up_bid, up_ask = _fetch_top(str(market.get("up_token") or ""))
        dn_bid, dn_ask = _fetch_top(str(market.get("dn_token") or ""))
        market["up_bid"], market["up_ask"] = up_bid, up_ask
        market["dn_bid"], market["dn_ask"] = dn_bid, dn_ask
        fetched += 1
    return fetched


def run_once(cfg: dict, now: Optional[float] = None) -> float:
    wall = now is None
    now_s = time.time() if wall else float(now)
    # First snapshot only chooses which books to read. Planning uses a
    # second read so a scrap that lands during discovery is not missed.
    intents = _mint_intents()
    state = _read_json(STATE_FILE)
    if not isinstance(state.get("orders"), dict):
        state["orders"] = {}
    if not isinstance(state.get("dump_orders"), dict):
        state["dump_orders"] = {}
    discovered = []
    try:
        gateway = MarketGateway(
            gamma_url="https://gamma-api.polymarket.com",
            data_api_url="https://data-api.polymarket.com",
        )
        for market in gateway.discover(list(cfg["series_slugs"])):
            discovered.append(
                {
                    "condition_id": market.condition_id,
                    "slug": market.slug,
                    "up_token": market.up_token,
                    "dn_token": market.dn_token,
                    "end_ts": market.end_ts,
                }
            )
    except Exception as exc:
        _log("scrapbid_discover_fail", error=str(exc)[:200])
    markets = merge_markets(discovered, intents, now_s)
    orders = _merged_open_orders(state)
    fetched = _fill_books(markets, intents, orders, now_s, cfg)
    intents = _mint_intents()
    if wall:
        now_s = time.time()
    fetched += _fill_books(markets, intents, orders, now_s, cfg)
    wanted = markets_needing_books(
        markets,
        intents,
        orders,
        now_s=now_s,
        active_ttm_s=float(cfg["active_ttm_s"]),
        cancel_ttm_s=float(cfg["cancel_ttm_s"]),
    )
    _log(
        "scrapbid_books",
        fetched=fetched,
        planned=len(wanted),
        discovered=len(markets),
    )
    if not isinstance(state.get("filled"), dict):
        state["filled"] = {}
    if not isinstance(state.get("dump_filled"), dict):
        state["dump_filled"] = {}
    filled = state["filled"]
    scrap_on = sister_scrap_on(cfg)
    dump_on = sister_dump_on(cfg)
    actions = plan_sister_bids(
        markets=wanted,
        intents=intents,
        open_orders=state.get("orders") or {},
        now_s=now_s,
        shares=float(cfg["shares"]),
        bid_max_px=float(cfg["bid_max_px"]),
        bid_rest_px=float(cfg["bid_rest_px"]),
        active_ttm_s=float(cfg["active_ttm_s"]),
        cancel_ttm_s=float(cfg["cancel_ttm_s"]),
        min_gtd_ahead_s=max(180.0, float(cfg["min_gtd_ahead_s"])),
        fak_min_notional=float(cfg.get("bid_fak_min_notional") or 1.0),
        fak_max_notional=float(cfg.get("bid_fak_max_notional") or 1.5),
        enabled=scrap_on,
        take_enabled=bool(cfg.get("bid_take_enabled", True)),
        absent_enabled=bool(cfg.get("bid_absent_enabled", False)),
        filled_shares=filled,
    )
    actions.extend(
        plan_dump_hedges(
            markets=wanted,
            intents=intents,
            open_orders=state.get("dump_orders") or {},
            now_s=now_s,
            shares=float(cfg.get("dump_hedge_shares") or 10),
            bid_rest_px=float(cfg.get("dump_hedge_rest_px") or 0.10),
            cancel_ttm_s=float(cfg["cancel_ttm_s"]),
            min_gtd_ahead_s=max(180.0, float(cfg["min_gtd_ahead_s"])),
            fak_min_notional=float(cfg.get("dump_hedge_fak_min_notional") or 1.0),
            fak_max_notional=float(cfg.get("dump_hedge_fak_max_notional") or 1.5),
            enabled=dump_on,
            take_enabled=bool(cfg.get("bid_take_enabled", True)),
            filled_shares=state.get("dump_filled") or {},
        )
    )
    want_place = any(row.get("op") == "place" for row in actions)
    dry_run = bool(cfg.get("dry_run"))
    if want_place and not dry_run:
        _sync_collateral()
    _maybe_topup(
        cfg, force_broke=False, cleared=False, reason="hedge", want=want_place,
    )
    broke = apply_actions(actions, state, dry_run=dry_run)
    if broke:
        _maybe_topup(
            cfg, force_broke=True, cleared=False, reason="place_fail", want=True,
        )
    elif want_place:
        _maybe_topup(
            cfg, force_broke=False, cleared=True, reason="place_ok", want=True,
        )
    if scrap_on:
        events, flat_at, emit_at = sister_miss_events(
            intents=intents,
            open_orders=state.get("orders") or {},
            now_s=now_s,
            first_flat_at=state.get("miss_flat_at") if isinstance(state.get("miss_flat_at"), dict) else {},
            last_emit_at=state.get("miss_emit_at") if isinstance(state.get("miss_emit_at"), dict) else {},
            cancel_ttm_s=float(cfg["cancel_ttm_s"]),
            miss_after_s=float(cfg.get("miss_after_s") or 10.0),
            throttle_s=float(cfg.get("miss_throttle_s") or 30.0),
            filled_shares=filled,
        )
        state["miss_flat_at"] = flat_at
        state["miss_emit_at"] = emit_at
        for row in events:
            payload = dict(row)
            payload.pop("event", None)
            _log("scrapbid_miss", **payload)
    _atomic_save(STATE_FILE, state)
    return sister_poll_s(
        intents=intents,
        open_orders=state.get("orders") or {},
        now_s=now_s,
        poll_s=float(cfg.get("poll_s") or 2.0),
        hot_poll_s=float(cfg.get("poll_hot_s") or 1.0),
        cancel_ttm_s=float(cfg["cancel_ttm_s"]),
        enabled=scrap_on or dump_on,
        filled_shares=filled,
        shares=float(cfg.get("shares") or 20.0) if scrap_on else 0.0,
        dump_shares=float(cfg.get("dump_hedge_shares") or 0) if dump_on else 0.0,
        dump_filled=state.get("dump_filled") or {},
        dump_orders=state.get("dump_orders") or {},
    )


def main() -> None:
    load_dotenv(REPO / ".env.complement")
    _setup_log()
    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)
    cfg = load_strategy()
    _log(
        "scrapbid_start",
        bid_enabled=bool(cfg.get("bid_enabled")),
        dry_run=bool(cfg.get("dry_run")),
        shares=cfg.get("shares"),
        bid_max_px=cfg.get("bid_max_px"),
        bid_rest_px=cfg.get("bid_rest_px"),
        bid_fak_min_notional=cfg.get("bid_fak_min_notional"),
        bid_fak_max_notional=cfg.get("bid_fak_max_notional"),
        min_gtd_ahead_s=cfg.get("min_gtd_ahead_s"),
        bid_take_enabled=bool(cfg.get("bid_take_enabled", True)),
        bid_absent_enabled=bool(cfg.get("bid_absent_enabled", False)),
        scrap_hedge_enabled=bool(cfg.get("scrap_hedge_enabled", False)),
        dump_hedge_enabled=bool(cfg.get("dump_hedge_enabled", True)),
        dump_hedge_shares=cfg.get("dump_hedge_shares"),
        dump_hedge_fak_max_notional=cfg.get("dump_hedge_fak_max_notional"),
        topup_enabled=bool(cfg.get("topup_enabled", False)),
        topup_usd=cfg.get("topup_usd"),
        topup_need_usd=cfg.get("topup_need_usd"),
    )
    LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)
    lock_fh = open(LOCK_FILE, "a+", encoding="utf-8")
    try:
        fcntl.flock(lock_fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        _log("scrapbid_lock_busy")
        sys.exit(1)
    while not _shutdown and not STOP_FILE.exists():
        step = float(cfg.get("poll_s") or 2.0)
        try:
            step = float(run_once(cfg))
        except Exception as exc:
            _log("scrapbid_cycle_error", error=str(exc)[:240])
        try:
            cfg = load_strategy()
        except Exception as exc:
            _log("scrapbid_strategy_fail", error=str(exc)[:200])
        slept = 0.0
        while slept < step and not _shutdown and not STOP_FILE.exists():
            time.sleep(min(0.5, step - slept))
            slept += 0.5
    _log("scrapbid_stop")


if __name__ == "__main__":
    main()
