#!/usr/bin/env python3
"""Mint-only bot: split pUSD into Up+Down complete sets for manual selling.

No CLOB buys. No hedges/sells. Discovers BTC Up/Down markets (5m/15m/hourly),
mints `shares` (default 6) only for markets that are **not yet open**
(start_ts in the future) and open within enter_max_ttm_min (default 70)
minutes, if collateral is available.

Usage:
  # dry-run (default when strategy_mint.json has dry_run true / entry_enabled false)
  python mintbot.py

Live requires strategy_mint.json with dry_run=false and entry_enabled=true.
Keep polybuybot* stopped while using this.
"""

from __future__ import annotations

import fcntl
import json
import os
import signal
import sys
import time
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import requests
from dotenv import load_dotenv
from eth_utils import to_checksum_address
from rich import box
from rich.align import Align
from rich.console import Console
from rich.panel import Panel

from buy.chain import ChainReader
from buy.contracts import ContractCall, build_atomic_mint_calls
from buy.market import MarketGateway, MintMarket

load_dotenv()

console = Console()
REPO = Path(__file__).resolve().parent

STRATEGY_FILE = REPO / "strategy_mint.json"
STATE_FILE = REPO / "positions_mint.json"
LOG_FILE = REPO / "mintbot.log"
LOCK_FILE = REPO / ".mintbot.lock"
HEARTBEAT_FILE = REPO / ".heartbeat_mint"
STOP_FILE = REPO / "STOP_MINT"

DEFAULTS = {
    "entry_enabled": False,
    "dry_run": True,
    "shares": 6.0,
    "enter_min_ttm_min": 0.0,
    "enter_max_ttm_min": 70.0,
    "series_slugs": [
        "btc-up-or-down-5m",
        "btc-up-or-down-15m",
        "btc-up-or-down-hourly",
    ],
    "one_entry_per_market": True,
    "max_open_sets": 40,
    "max_daily_notional": 500.0,
    "poll_s": 10.0,
    "position_tolerance": 0.01,
    "require_accepting_orders": True,
    # Relayer PROXY default is 500k — approve + pUSD unwrap + CTF split +
    # ERC1155 batch transfer needs more (failed txs OOG'd at ~413k on split).
    "relayer_gas_limit": "1500000",
    "rpc_url": "https://polygon.drpc.org",
    "gamma_url": "https://gamma-api.polymarket.com",
    "data_api_url": "https://data-api.polymarket.com",
    "relayer_url": "https://relayer-v2.polymarket.com",
    "chain_id": 137,
    "pUSD_address": "0xC011a7E12a19f7B1f670d46F03B03f3342E82DFB",
    "ctf_address": "0x4D97DCd97eC945f40cF65F87097ACe5EA0476045",
    "standard_adapter_address": "0xAdA100Db00Ca00073811820692005400218FcE1f",
}

ACTIVE_STATUSES = frozenset(
    {
        "submitting",
        "pending",
        "executed",
        "mined",
        "confirmed_waiting_inventory",
        "confirmed",
    }
)

# Terminal for one_entry_per_market — do not resubmit forever.
DONE_STATUSES = frozenset({"completed", "failed", "invalid"})

_shutdown = False


def _signal_handler(signum, frame):
    global _shutdown
    _shutdown = True


def log_setup() -> None:
    import logging

    logger = logging.getLogger("mintbot")
    logger.setLevel(logging.INFO)
    logger.handlers.clear()
    fmt = logging.Formatter("%(message)s")
    fh = RotatingFileHandler(LOG_FILE, maxBytes=2_000_000, backupCount=3)
    fh.setFormatter(fmt)
    logger.addHandler(fh)
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    logger.addHandler(sh)


def log_event(event: str, **kwargs: Any) -> None:
    import logging

    payload = {"ts": time.time(), "event": event, **kwargs}
    logging.getLogger("mintbot").info(json.dumps(payload, default=str))


def notify(title: str, message: str, priority: str = "default") -> None:
    topic = os.getenv("NTFY_TOPIC") or os.getenv("NTFY_TOPIC_BUY") or "polybot-joel-btc"
    try:
        requests.post(
            f"https://ntfy.sh/{topic}",
            data=message.encode("utf-8"),
            headers={"Title": title, "Priority": priority},
            timeout=5,
        )
    except Exception:
        return


def atomic_save(path: Path, payload: dict) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    data = json.dumps(payload, indent=2, sort_keys=True)
    with open(temporary, "w", encoding="utf-8") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)
    try:
        dir_fd = os.open(str(path.parent), os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    except OSError:
        pass


def load_state() -> dict:
    if not STATE_FILE.exists():
        return {"intents": {}, "daily": {}}
    with open(STATE_FILE, encoding="utf-8") as handle:
        payload = json.load(handle)
    if not isinstance(payload, dict):
        raise ValueError("positions_mint.json must be an object")
    payload.setdefault("intents", {})
    payload.setdefault("daily", {})
    return payload


def load_strategy() -> dict:
    if not STRATEGY_FILE.exists():
        raise FileNotFoundError(
            f"missing {STRATEGY_FILE.name} — copy strategy_mint.example.json"
        )
    with open(STRATEGY_FILE, encoding="utf-8-sig") as handle:
        raw = json.load(handle)
    if not isinstance(raw, dict):
        raise ValueError("strategy_mint.json must be an object")
    cfg = dict(DEFAULTS)
    for key, value in raw.items():
        if key in cfg:
            cfg[key] = value
    if isinstance(cfg["series_slugs"], str):
        cfg["series_slugs"] = [
            part.strip() for part in cfg["series_slugs"].split(",") if part.strip()
        ]
    validate_strategy(cfg)
    return cfg


def validate_strategy(cfg: dict) -> None:
    if float(cfg["shares"]) <= 0:
        raise ValueError("shares must be positive")
    amount = int(round(float(cfg["shares"]) * 1_000_000))
    if abs(amount / 1_000_000 - float(cfg["shares"])) > 1e-9:
        raise ValueError("shares must have at most 6 decimal places")
    if float(cfg["enter_min_ttm_min"]) < 0:
        raise ValueError("enter_min_ttm_min must be >= 0")
    if float(cfg["enter_max_ttm_min"]) <= float(cfg["enter_min_ttm_min"]):
        raise ValueError("enter_max_ttm_min must be > enter_min_ttm_min")
    if not cfg["series_slugs"]:
        raise ValueError("series_slugs must not be empty")
    if int(cfg["max_open_sets"]) < 1:
        raise ValueError("max_open_sets must be >= 1")
    if float(cfg["max_daily_notional"]) < float(cfg["shares"]):
        raise ValueError("max_daily_notional must cover one mint")
    if float(cfg["poll_s"]) < 2:
        raise ValueError("poll_s must be >= 2")
    gas_limit = str(cfg.get("relayer_gas_limit") or "").strip()
    if not gas_limit.isdigit() or int(gas_limit) < 500_000:
        raise ValueError("relayer_gas_limit must be an integer >= 500000")


def today_key(now: float) -> str:
    return datetime.fromtimestamp(now, tz=timezone.utc).strftime("%Y-%m-%d")


def daily_spent(state: dict, now: float) -> float:
    return float(state.get("daily", {}).get(today_key(now), 0) or 0)


def add_daily(state: dict, now: float, amount: float) -> None:
    key = today_key(now)
    daily = state.setdefault("daily", {})
    daily[key] = float(daily.get(key, 0) or 0) + amount


def refund_daily(state: dict, now: float, amount: float) -> None:
    """Undo a notional reservation after a failed relayer mint."""
    if amount <= 0:
        return
    key = today_key(now)
    daily = state.setdefault("daily", {})
    daily[key] = max(0.0, float(daily.get(key, 0) or 0) - amount)


def eligible_markets(markets: List[MintMarket], cfg: dict, now: float) -> List[MintMarket]:
    """Only markets that have not opened yet, starting within the configured window.

    enter_min/max_ttm_min are minutes-until-start (legacy key names), not time-to-end.
    """
    lo = float(cfg["enter_min_ttm_min"])
    hi = float(cfg["enter_max_ttm_min"])
    out: List[MintMarket] = []
    for market in markets:
        # Crucially: never mint a market that is already open.
        if market.start_ts <= now or market.minutes_to_start(now) <= 0:
            continue
        mts = market.minutes_to_start(now)
        if not (lo < mts <= hi):
            continue
        if not market.active or market.closed or market.neg_risk:
            continue
        if cfg.get("require_accepting_orders") and not market.accepting_orders:
            continue
        out.append(market)
    return sorted(out, key=lambda m: m.start_ts)


def open_intent_count(state: dict) -> int:
    return sum(
        1
        for intent in state.get("intents", {}).values()
        if intent.get("status") in ACTIVE_STATUSES
    )


def already_minted(state: dict, condition_id: str, cfg: dict) -> bool:
    if not cfg.get("one_entry_per_market", True):
        return False
    intent = state.get("intents", {}).get(condition_id)
    if not intent:
        return False
    return intent.get("status") in ACTIVE_STATUSES | DONE_STATUSES


def get_relayer_headers(body: dict) -> Optional[dict]:
    relayer_key = os.getenv("RELAYER_API_KEY")
    relayer_addr = os.getenv("RELAYER_API_KEY_ADDRESS")
    if relayer_key and relayer_addr:
        return {
            "Content-Type": "application/json",
            "RELAYER_API_KEY": relayer_key,
            "RELAYER_API_KEY_ADDRESS": relayer_addr,
        }
    builder_key = os.getenv("POLY_BUILDER_API_KEY") or os.getenv("BUILDER_API_KEY")
    builder_secret = os.getenv("POLY_BUILDER_SECRET") or os.getenv("BUILDER_SECRET")
    builder_pass = os.getenv("POLY_BUILDER_PASSPHRASE") or os.getenv("BUILDER_PASS_PHRASE")
    if not (builder_key and builder_secret and builder_pass):
        return None
    from py_builder_signing_sdk.config import BuilderConfig
    from py_builder_signing_sdk.sdk_types import BuilderApiKeyCreds

    config = BuilderConfig(
        local_builder_creds=BuilderApiKeyCreds(
            key=builder_key,
            secret=builder_secret,
            passphrase=builder_pass,
        )
    )
    payload = config.generate_builder_headers(
        method="POST",
        path="/submit",
        body=json.dumps(body),
    )
    if payload is None:
        return None
    headers = dict(payload)
    headers["Content-Type"] = "application/json"
    return headers


def submit_mint_batch(
    calls: List[ContractCall],
    metadata: str,
    *,
    gas_limit: str = "1500000",
) -> Tuple[Optional[str], Optional[str]]:
    """Submit approve+split as one PROXY batch via Polymarket relayer."""
    private_key = os.getenv("PRIVATE_KEY") or ""
    funder = os.getenv("FUNDER_ADDRESS") or ""
    if not private_key or not funder:
        return None, "missing PRIVATE_KEY or FUNDER_ADDRESS"

    from py_builder_relayer_client.builder.proxy import build_proxy_transaction_request
    from py_builder_relayer_client.config import get_contract_config as get_relayer_contract_config
    from py_builder_relayer_client.encode.proxy import encode_proxy_transaction_data
    from py_builder_relayer_client.models import (
        CallType,
        ProxyTransaction,
        ProxyTransactionArgs,
    )
    from py_builder_relayer_client.signer import Signer as RelayerSigner

    try:
        chain_id = int(os.getenv("CHAIN_ID") or 137)
        relayer_url = (os.getenv("RELAYER_URL") or "https://relayer-v2.polymarket.com").rstrip("/")
        signer = RelayerSigner(private_key, chain_id)
        eoa = signer.address()
        relayer_addr = os.getenv("RELAYER_API_KEY_ADDRESS")
        if relayer_addr and str(relayer_addr).lower() != str(eoa).lower():
            return None, "RELAYER_API_KEY_ADDRESS does not match PRIVATE_KEY signer"

        nonce_r = requests.get(
            f"{relayer_url}/relay-payload",
            params={"address": eoa, "type": "PROXY"},
            timeout=15,
        )
        if nonce_r.status_code != 200:
            return None, f"relay payload fetch fail HTTP {nonce_r.status_code}"
        relay_payload = nonce_r.json()
        if not isinstance(relay_payload, dict):
            return None, "invalid relay payload"
        nonce = relay_payload.get("nonce")
        relay = relay_payload.get("address")
        if nonce is None or not relay:
            return None, "relay payload missing nonce/address"

        encoded_data = encode_proxy_transaction_data(
            [
                ProxyTransaction(
                    to=str(call.to),
                    type_code=CallType.Call,
                    data=str(call.data),
                    value="0",
                )
                for call in calls
            ]
        )
        config = get_relayer_contract_config(chain_id)
        request = build_proxy_transaction_request(
            signer=signer,
            args=ProxyTransactionArgs(
                from_address=eoa,
                nonce=str(nonce),
                gas_price="0",
                gas_limit=str(gas_limit),
                data=encoded_data,
                relay=str(relay),
            ),
            config=config,
            metadata=metadata,
        )
        body = request.to_dict()
        if str(body.get("proxyWallet") or "").lower() != str(funder).lower():
            return None, "derived proxyWallet does not match FUNDER_ADDRESS"
        headers = get_relayer_headers(body)
        if headers is None:
            return None, "could not generate relayer authentication headers"
        submit_r = requests.post(
            f"{relayer_url}/submit",
            json=body,
            headers=headers,
            timeout=20,
        )
        if submit_r.status_code == 200:
            payload = submit_r.json()
            tx_id = payload.get("transactionID") if isinstance(payload, dict) else None
            if tx_id:
                return str(tx_id), None
            return None, "relayer response missing transactionID"
        return None, f"HTTP {submit_r.status_code} · {submit_r.text[:120]}"
    except Exception as exc:
        return None, f"relayer request failed: {str(exc)[:200]}"


def get_relayer_transaction(relayer_url: str, transaction_id: str) -> Optional[dict]:
    try:
        response = requests.get(
            f"{relayer_url.rstrip('/')}/transaction",
            params={"id": transaction_id},
            timeout=15,
        )
        if response.status_code == 404:
            return None
        response.raise_for_status()
        payload = response.json()
        if isinstance(payload, list):
            return payload[0] if payload and isinstance(payload[0], dict) else None
        return payload if isinstance(payload, dict) else None
    except Exception as exc:
        log_event("relayer_status_fail", transaction_id=str(transaction_id)[:36], error=str(exc)[:160])
        return None


def reconcile_intents(
    state: dict,
    cfg: dict,
    chain: ChainReader,
    funder: str,
    now: float,
) -> None:
    relayer_url = str(cfg["relayer_url"])
    ctf = str(cfg["ctf_address"])
    tol = float(cfg["position_tolerance"])
    for cid, intent in list(state.get("intents", {}).items()):
        status = intent.get("status")
        tx_id = intent.get("transaction_id")
        if status in ("submitting", "pending", "executed", "mined") and tx_id:
            record = get_relayer_transaction(relayer_url, str(tx_id))
            if record:
                relayer_state = str(record.get("state") or "")
                intent["relayer_state"] = relayer_state
                intent["updated_at"] = now
                if relayer_state in ("STATE_FAILED", "STATE_INVALID"):
                    err = str(
                        record.get("errorMsg")
                        or record.get("error")
                        or record.get("message")
                        or ""
                    )[:240]
                    tx_hash = str(record.get("transactionHash") or "")[:66]
                    was_pending = intent.get("status") in (
                        "pending",
                        "executed",
                        "mined",
                        "submitting",
                    )
                    intent["status"] = "failed" if relayer_state == "STATE_FAILED" else "invalid"
                    intent["error"] = err or relayer_state
                    intent["transaction_hash"] = tx_hash or intent.get("transaction_hash")
                    if was_pending and not intent.get("daily_refunded"):
                        refund_daily(state, now, float(intent.get("shares") or 0))
                        intent["daily_refunded"] = True
                    log_event(
                        "mint_failed",
                        condition_id=cid,
                        state=relayer_state,
                        slug=intent.get("slug"),
                        error=err or None,
                        transaction_hash=tx_hash or None,
                        transaction_id=str(tx_id)[:36],
                    )
                    console.print(
                        f"  [bold red][MINT FAIL][/] {intent.get('slug')}  "
                        f"{relayer_state}  {err or 'no errorMsg'}"
                    )
                    notify(
                        "Mint failed",
                        f"{intent.get('slug')}\n{relayer_state}\n{err or 'see logs'}",
                        priority="high",
                    )
                elif relayer_state == "STATE_CONFIRMED":
                    intent["status"] = "confirmed_waiting_inventory"
                elif relayer_state == "STATE_MINED":
                    intent["status"] = "mined"
                elif relayer_state == "STATE_EXECUTED":
                    intent["status"] = "executed"
        if intent.get("status") in ("confirmed_waiting_inventory", "confirmed", "mined"):
            try:
                up = chain.position_balance(ctf, funder, intent["up_token"])
                dn = chain.position_balance(ctf, funder, intent["dn_token"])
            except Exception as exc:
                log_event("inventory_check_fail", condition_id=cid, error=str(exc)[:160])
                continue
            intent["observed_up"] = up
            intent["observed_dn"] = dn
            intent["updated_at"] = now
            expected = float(intent.get("before_up") or 0) + float(intent["shares"])
            expected_dn = float(intent.get("before_dn") or 0) + float(intent["shares"])
            if up + tol >= expected and dn + tol >= expected_dn:
                if intent.get("status") != "confirmed":
                    log_event(
                        "mint_confirmed",
                        condition_id=cid,
                        slug=intent.get("slug"),
                        shares=intent.get("shares"),
                        up=up,
                        dn=dn,
                    )
                    notify(
                        "Mint confirmed",
                        f"{intent.get('slug')}\n{intent.get('shares')} Up + Down",
                        priority="high",
                    )
                    console.print(
                        f"  [bold bright_green][MINT OK][/] {intent.get('slug')}  "
                        f"up={up:.2f} dn={dn:.2f}"
                    )
                intent["status"] = "confirmed"
            elif now > float(intent.get("end_ts") or 0) + 120:
                # Market ended; inventory may have been sold manually — stop waiting.
                if max(up, dn) <= tol:
                    intent["status"] = "completed"


def acquire_lock():
    handle = open(LOCK_FILE, "a+")
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        handle.close()
        raise SystemExit("another mintbot instance holds the lock")
    return handle


def write_heartbeat(status: str, **fields: Any) -> None:
    payload = {"ts": time.time(), "status": status, **fields}
    temporary = str(HEARTBEAT_FILE) + ".tmp"
    with open(temporary, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, sort_keys=True)
    os.replace(temporary, HEARTBEAT_FILE)


def run_cycle(
    cfg: dict,
    state: dict,
    gateway: MarketGateway,
    chain: ChainReader,
) -> str:
    now = time.time()
    if STOP_FILE.exists():
        write_heartbeat("stopped")
        return "stopped"

    funder = os.getenv("FUNDER_ADDRESS") or ""
    if funder:
        reconcile_intents(state, cfg, chain, to_checksum_address(funder), now)
        atomic_save(STATE_FILE, state)

    if any(
        intent.get("status") == "submitting"
        for intent in state.get("intents", {}).values()
    ):
        write_heartbeat("wait_submit")
        return "wait_submit"

    if not cfg.get("entry_enabled"):
        write_heartbeat("disabled")
        return "disabled"

    markets = gateway.discover(list(cfg["series_slugs"]))
    candidates = eligible_markets(markets, cfg, now)
    if not candidates:
        write_heartbeat("idle", markets=len(markets), eligible=0)
        return "idle"

    if open_intent_count(state) >= int(cfg["max_open_sets"]):
        write_heartbeat("capped_open", open=open_intent_count(state))
        return "capped_open"

    spent = daily_spent(state, now)
    if spent + float(cfg["shares"]) > float(cfg["max_daily_notional"]) + 1e-9:
        write_heartbeat("capped_daily", spent=spent)
        return "capped_daily"

    data_positions: Dict[str, float] = {}
    if funder:
        try:
            data_positions = gateway.positions(funder)
        except Exception as exc:
            log_event("positions_fetch_fail", error=str(exc)[:160])

    tol = float(cfg["position_tolerance"])
    pick: Optional[MintMarket] = None
    for market in candidates:
        if already_minted(state, market.condition_id, cfg):
            continue
        if float(data_positions.get(market.up_token, 0)) > tol:
            continue
        if float(data_positions.get(market.dn_token, 0)) > tol:
            continue
        pick = market
        break

    if pick is None:
        write_heartbeat("idle", markets=len(markets), eligible=len(candidates), reason="owned")
        return "idle_owned"

    shares = float(cfg["shares"])
    mts = pick.minutes_to_start(now)

    if cfg.get("dry_run"):
        console.print(
            f"  [bold black on yellow][DRY MINT][/] {pick.slug}  "
            f"shares={shares:.2f}  opens_in={mts:.1f}m  cost=${shares:.2f}"
        )
        log_event(
            "dry_mint",
            condition_id=pick.condition_id,
            slug=pick.slug,
            shares=shares,
            opens_in_min=round(mts, 2),
            question=pick.question,
        )
        # Record dry intent so we don't spam the same market every poll.
        state.setdefault("intents", {})[pick.condition_id] = {
            "created_at": now,
            "updated_at": now,
            "status": "completed",
            "dry_run": True,
            "condition_id": pick.condition_id,
            "slug": pick.slug,
            "question": pick.question,
            "series_slug": pick.series_slug,
            "end_ts": pick.end_ts,
            "shares": shares,
            "up_token": pick.up_token,
            "dn_token": pick.dn_token,
        }
        add_daily(state, now, 0.0)  # dry does not spend
        atomic_save(STATE_FILE, state)
        write_heartbeat("dry_mint", slug=pick.slug)
        return "dry_mint"

    if not funder:
        return "missing_funder"
    funder_cs = to_checksum_address(funder)

    try:
        if not chain.has_contract(str(cfg["pUSD_address"])):
            return "no_pusd_contract"
        if not chain.has_contract(str(cfg["standard_adapter_address"])):
            return "no_adapter"
        if chain.outcome_slot_count(str(cfg["ctf_address"]), pick.condition_id) != 2:
            log_event("mint_skip_not_binary", condition_id=pick.condition_id, slug=pick.slug)
            return "not_binary"
        balance = chain.pUSD_balance(str(cfg["pUSD_address"]), funder_cs)
        if balance + 1e-9 < shares:
            console.print(
                f"  [dim red][SKIP][/] insufficient pUSD  bal={balance:.2f} need={shares:.2f}"
            )
            log_event("mint_skip_balance", balance=balance, need=shares)
            write_heartbeat("no_balance", balance=balance)
            return "no_balance"
        before_up = chain.position_balance(str(cfg["ctf_address"]), funder_cs, pick.up_token)
        before_dn = chain.position_balance(str(cfg["ctf_address"]), funder_cs, pick.dn_token)
    except Exception as exc:
        log_event("precheck_fail", error=str(exc)[:200], slug=pick.slug)
        return "precheck_fail"

    if max(before_up, before_dn) > tol:
        log_event(
            "mint_skip_existing",
            condition_id=pick.condition_id,
            up=before_up,
            dn=before_dn,
        )
        return "existing_position"

    calls = build_atomic_mint_calls(
        pUSD_address=str(cfg["pUSD_address"]),
        adapter_address=str(cfg["standard_adapter_address"]),
        condition_id=pick.condition_id,
        shares=shares,
    )
    intent = {
        "created_at": now,
        "updated_at": now,
        "submitted_at": 0.0,
        "status": "submitting",
        "condition_id": pick.condition_id,
        "slug": pick.slug,
        "question": pick.question,
        "series_slug": pick.series_slug,
        "end_ts": pick.end_ts,
        "up_token": pick.up_token,
        "dn_token": pick.dn_token,
        "shares": shares,
        "before_up": before_up,
        "before_dn": before_dn,
        "transaction_id": None,
        "dry_run": False,
    }
    state.setdefault("intents", {})[pick.condition_id] = intent
    atomic_save(STATE_FILE, state)

    console.print(
        Panel(
            f"  [bright_white]{pick.question}[/]\n"
            f"  shares [bold]{shares:.2f}[/] Up+Down  ·  cost [bold]${shares:.2f}[/]  ·  "
            f"opens in [bold]{mts:.1f}m[/]",
            title="[bold bright_cyan]◆ MINT COMPLETE SET[/]",
            border_style="bright_cyan",
            box=box.HEAVY,
        )
    )
    log_event(
        "mint_attempt",
        condition_id=pick.condition_id,
        slug=pick.slug,
        shares=shares,
        opens_in_min=round(mts, 2),
        start_ts=pick.start_ts,
        balance=balance,
    )

    tx_id, err = submit_mint_batch(
        calls,
        metadata=f"mintbot:split:{pick.condition_id}:{int(now)}",
        gas_limit=str(cfg["relayer_gas_limit"]),
    )
    intent = state["intents"][pick.condition_id]
    intent["updated_at"] = time.time()
    if not tx_id:
        intent["status"] = "failed"
        intent["error"] = err
        atomic_save(STATE_FILE, state)
        console.print(f"  [dim red][MINT FAIL][/] {err}")
        log_event("mint_submit_fail", condition_id=pick.condition_id, error=err)
        notify("Mint submit failed", f"{pick.slug}\n{err}", priority="high")
        write_heartbeat("submit_fail")
        return "submit_fail"

    intent["transaction_id"] = tx_id
    intent["submitted_at"] = time.time()
    intent["status"] = "pending"
    add_daily(state, now, shares)
    atomic_save(STATE_FILE, state)
    console.print(f"  [bold bright_green][MINT ▶][/] tx={tx_id[:18]}…")
    log_event("mint_submitted", condition_id=pick.condition_id, transaction_id=tx_id)
    notify("Mint submitted", f"{pick.slug}\n{shares:.0f} sets · {tx_id[:18]}…", priority="default")
    write_heartbeat("submitted", slug=pick.slug)
    return "submitted"


def main() -> int:
    signal.signal(signal.SIGINT, _signal_handler)
    signal.signal(signal.SIGTERM, _signal_handler)
    log_setup()
    lock = acquire_lock()

    try:
        cfg = load_strategy()
    except Exception as exc:
        console.print(f"[bold red]strategy load failed:[/] {exc}")
        return 1

    state = load_state()
    gateway = MarketGateway(
        gamma_url=str(cfg["gamma_url"]),
        data_api_url=str(cfg["data_api_url"]),
        discover_cache_s=8.0,
    )
    chain = ChainReader(str(cfg["rpc_url"]))

    console.print(
        Panel(
            Align.center(
                "[bold bright_cyan]MINT DESK[/]\n"
                f"[dim]shares={cfg['shares']} · not-yet-open · opens within "
                f"{cfg['enter_max_ttm_min']}m · "
                f"dry_run={cfg['dry_run']} · entry_enabled={cfg['entry_enabled']}[/]\n"
                "[dim]no CLOB buys · no auto-sells · you sell manually[/]",
                vertical="middle",
            ),
            title="[bold]polymintbot[/]",
            border_style="bright_cyan",
            box=box.HEAVY_EDGE,
        )
    )
    if cfg["dry_run"]:
        console.print("[bold black on yellow]▶ DRY RUN[/] [dim]no relayer submits[/]")
    if not cfg["entry_enabled"]:
        console.print("[bold yellow]▶ ENTRY OFF[/] [dim]set entry_enabled=true to mint[/]")

    log_event(
        "startup",
        dry_run=cfg["dry_run"],
        entry_enabled=cfg["entry_enabled"],
        shares=cfg["shares"],
        max_ttm=cfg["enter_max_ttm_min"],
        series=cfg["series_slugs"],
    )

    while not _shutdown:
        try:
            cfg = load_strategy()
        except Exception as exc:
            log_event("strategy_reload_fail", error=str(exc)[:200])
            # Fail closed on entries; keep reconciling with last-good cfg if any.
            cfg = {**cfg, "entry_enabled": False}

        try:
            status = run_cycle(cfg, state, gateway, chain)
            if status not in ("idle", "idle_owned", "disabled", "wait_submit"):
                log_event("cycle", status=status)
        except Exception as exc:
            log_event("cycle_error", error=str(exc)[:300])
            console.print(f"[red]cycle_error[/] {exc}")
            write_heartbeat("error", error=str(exc)[:120])

        time.sleep(float(cfg.get("poll_s") or 10))

    console.print("[dim]mintbot stopped[/]")
    try:
        lock.close()
    except Exception:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
