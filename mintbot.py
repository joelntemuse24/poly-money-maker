#!/usr/bin/env python3
"""15m atomic mint: split pUSD into Up+Down complete sets.

No CLOB buys. No hedges. Discovers **btc-up-or-down-15m** only, mints
`shares` for markets that are **not yet open** (start_ts in the future)
and open within enter_max_ttm_min, if collateral is available.

Optional sell (``sell_enabled``, default off): persist a loser dump at ~3¢
for ~5s while the opposite bid is ≥ ~90¢, then FAK 3¢ → 2¢. Keep the
winner for redeem unless its bid reaches ~99.9¢. Off unless live
``strategy_mint.json`` turns it on.

Usage:
  # dry-run (default when strategy_mint.json has dry_run true / entry_enabled false)
  python mintbot.py

Live requires strategy_mint.json with dry_run=false and entry_enabled=true.
Keep polybuybot* / polycomplement / DangerZone stopped. Do not start
pathlog_hourly_dense.
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
from typing import Any, Dict, List, Optional, Sequence, Tuple

import requests
from dotenv import load_dotenv
from eth_utils import to_checksum_address
from rich import box
from rich.align import Align
from rich.console import Console
from rich.panel import Panel

from buy.book import best_bid_with_min_size
from buy.chain import ChainReader
from buy.contracts import ContractCall, build_atomic_mint_calls
from buy.market import MarketGateway, MintMarket
from buy.mint_sell import (
    classify_loser,
    inventory_latch,
    loser_ladder_limits,
    parse_sell_fill_shares,
    persist_ready,
    winner_cashout_leg,
)

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
    "shares": 50.0,
    "enter_min_ttm_min": 0.0,
    "enter_max_ttm_min": 16.0,
    "series_slugs": [
        "btc-up-or-down-15m",
    ],
    "one_entry_per_market": True,
    "max_open_sets": 1,
    "max_daily_notional": 200.0,
    "poll_s": 5.0,
    "position_tolerance": 0.01,
    "require_accepting_orders": True,
    "sell_enabled": False,
    "sell_threshold": 0.03,
    "sell_floor": 0.02,
    "sell_opposite_min": 0.90,
    "sell_persist_s": 5.0,
    "sell_cooldown_s": 3.0,
    "sell_winner_min": 0.999,
    "sell_min_bid_size": 1.0,
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
    floor = float(cfg.get("sell_floor") or 0)
    threshold = float(cfg.get("sell_threshold") or 0)
    opposite = float(cfg.get("sell_opposite_min") or 0)
    winner = float(cfg.get("sell_winner_min") or 0)
    if not (0 < floor <= threshold < opposite < winner < 1):
        raise ValueError(
            "sell_floor <= sell_threshold < sell_opposite_min < "
            "sell_winner_min must hold in (0, 1)"
        )
    if float(cfg.get("sell_persist_s") or 0) < 0:
        raise ValueError("sell_persist_s must be >= 0")
    if float(cfg.get("sell_min_bid_size") or 0) < 0:
        raise ValueError("sell_min_bid_size must be >= 0")


def today_key(now: float) -> str:
    return datetime.fromtimestamp(now, tz=timezone.utc).strftime("%Y-%m-%d")


def daily_spent(state: dict, now: float) -> float:
    return float(state.get("daily", {}).get(today_key(now), 0) or 0)


def add_daily(state: dict, now: float, amount: float) -> None:
    key = today_key(now)
    daily = state.setdefault("daily", {})
    daily[key] = float(daily.get(key, 0) or 0) + amount


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


def open_intent_count(state: dict, now: float | None = None) -> int:
    """Count bags that still block a new mint (unsold loser).

    Post-expiry redeem holds do not block. After the loser is sold we only
    hold the winner for redeem — that must not skip the next 15m window.
    """
    now = time.time() if now is None else float(now)
    n = 0
    for intent in state.get("intents", {}).values():
        if intent.get("status") not in ACTIVE_STATUSES:
            continue
        end_ts = float(intent.get("end_ts") or 0)
        if end_ts and now > end_ts + 120:
            continue
        if intent.get("sold_loser") or intent.get("sold_leg"):
            continue
        n += 1
    return n


def mint_slots_full(state: dict, cfg: dict, now: float, candidate_start_ts: float) -> bool:
    """True if minting candidate would exceed capacity.

    At max_open_sets full bags we still allow the *adjacent* next window
    (candidate.start_ts >= soonest full-bag end_ts) so we do not skip e.g.
    1:45–2:00 while holding 1:30–1:45. Only one such lookahead is allowed.
    A later (non-adjacent) window stays blocked at the cap.
    """
    max_open = int(cfg["max_open_sets"])
    full = []
    for intent in state.get("intents", {}).values():
        if intent.get("status") not in ACTIVE_STATUSES:
            continue
        end_ts = float(intent.get("end_ts") or 0)
        if end_ts and now > end_ts + 120:
            continue
        if intent.get("sold_loser") or intent.get("sold_leg"):
            continue
        full.append(intent)
    if len(full) < max_open:
        return False
    soonest_end = min((float(i.get("end_ts") or 0) for i in full), default=0.0)
    if not soonest_end or not candidate_start_ts:
        return True
    # Already holding a full bag that *is* that next window → block further.
    for intent in full:
        st = float(intent.get("start_ts") or 0)
        if st + 1e-6 >= soonest_end:
            return True
    # Adjacent next 15m window starts at soonest_end. A later start is a skip.
    candidate = float(candidate_start_ts)
    if soonest_end <= candidate + 1e-6 < soonest_end + 900:
        return False
    return True


def already_minted(state: dict, condition_id: str, cfg: dict) -> bool:
    if not cfg.get("one_entry_per_market", True):
        return False
    intent = state.get("intents", {}).get(condition_id)
    if not intent:
        return False
    # "failed" counts as attempted — do not hot-loop remint the same window.
    return intent.get("status") in ACTIVE_STATUSES | {"completed", "failed"}


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


def submit_mint_batch(calls: List[ContractCall], metadata: str) -> Tuple[Optional[str], Optional[str]]:
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
                    intent["status"] = "failed"
                    log_event("mint_failed", condition_id=cid, state=relayer_state, slug=intent.get("slug"))
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



_clob_client = None
_clob_init_error = None


def _fetch_sized_bid(token_id: str, min_size: float):
    try:
        response = requests.get(
            "https://clob.polymarket.com/book",
            params={"token_id": str(token_id)},
            timeout=5,
        )
        if response.status_code != 200:
            return None, 0.0
        payload = response.json()
        book = payload if isinstance(payload, dict) else {}
        return best_bid_with_min_size(book.get("bids") or [], min_size=min_size)
    except Exception as exc:
        log_event("book_fetch_fail", token_id=str(token_id)[:18], error=str(exc)[:160])
        return None, 0.0


def _get_clob_client():
    global _clob_client, _clob_init_error
    if _clob_client is not None:
        return _clob_client
    if _clob_init_error is not None:
        return None
    private_key = os.getenv("PRIVATE_KEY") or ""
    funder = os.getenv("FUNDER_ADDRESS") or ""
    if not private_key or not funder:
        _clob_init_error = "missing PRIVATE_KEY or FUNDER_ADDRESS"
        return None
    try:
        from py_clob_client_v2 import (
            ClobClient,
            MarketOrderArgs,
            OrderType,
            ApiCreds,
            BalanceAllowanceParams,
            AssetType,
        )
        from py_clob_client_v2.order_builder.constants import SELL

        host = "https://clob.polymarket.com"
        chain_id = int(os.getenv("CHAIN_ID") or 137)
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
        client = ClobClient(
            host=host,
            key=private_key,
            chain_id=chain_id,
            creds=creds,
            signature_type=1,
            funder=funder,
        )
        try:
            client.update_balance_allowance(
                BalanceAllowanceParams(asset_type=AssetType.COLLATERAL)
            )
        except Exception:
            pass
        _clob_client = client
        log_event("clob_client_ready")
        return _clob_client
    except Exception as exc:
        _clob_init_error = str(exc)[:200]
        log_event("clob_client_init_fail", error=_clob_init_error)
        return None


def _fak_sell(token_id: str, size: float, price: float, dry_run: bool):
    size = float(size)
    price = float(price)
    if size < 0.01 or price <= 0:
        return 0.0, "bad_args"
    if dry_run:
        console.print(
            f"  [bold black on yellow][DRY SELL][/] {size:.2f} @ >={price:.3f} "
            f"token={str(token_id)[:12]}..."
        )
        log_event("dry_sell", token_id=str(token_id), size=size, price=price)
        return 0.0, "dry"
    client = _get_clob_client()
    if client is None:
        return 0.0, f"no_clob:{_clob_init_error or 'unknown'}"
    try:
        from py_clob_client_v2 import MarketOrderArgs, OrderType, BalanceAllowanceParams, AssetType
        from py_clob_client_v2.order_builder.constants import SELL

        try:
            client.update_balance_allowance(
                BalanceAllowanceParams(
                    asset_type=AssetType.CONDITIONAL, token_id=str(token_id)
                )
            )
        except Exception as exc:
            log_event("sell_allowance_warn", error=str(exc)[:160])

        signed = client.create_market_order(
            MarketOrderArgs(
                token_id=str(token_id),
                amount=size,
                side=SELL,
                price=price,
            )
        )
        result = client.post_order(signed, order_type=OrderType.FAK)
        sold = 0.0
        status = "posted"
        if isinstance(result, dict):
            status = str(result.get("status") or "posted")
            sold = parse_sell_fill_shares(result, size)
        log_event(
            "sell_fak_result",
            token_id=str(token_id),
            size=size,
            price=price,
            sold=sold,
            status=status,
            raw=str(result)[:240] if result is not None else None,
        )
        return sold, status
    except Exception as exc:
        log_event("sell_fak_fail", token_id=str(token_id)[:18], error=str(exc)[:200])
        return 0.0, f"error:{str(exc)[:80]}"


def _sell_inventory(
    chain: ChainReader,
    ctf: str,
    funder_cs: Optional[str],
    token_id: str,
    shares: float,
    tol: float,
    seen_key: str,
    intent: dict,
) -> Tuple[float, str]:
    size = float(shares)
    if not (funder_cs and ctf):
        return size, "unknown"
    try:
        bal = chain.position_balance(ctf, funder_cs, token_id)
    except Exception as exc:
        log_event("sell_balance_fail", error=str(exc)[:160])
        return size, "unknown"
    latch = inventory_latch(
        bal, tol=tol, seen_inventory=bool(intent.get(seen_key))
    )
    if latch == "has_inventory":
        intent[seen_key] = True
        return min(size, float(bal)), latch
    return size, latch


def _run_fak_ladder(
    token_id: str,
    size: float,
    limits: Sequence[float],
    *,
    dry_run: bool,
    bid: float,
    label: str,
    slug: Any,
    tol: float,
) -> Tuple[float, str, Optional[float]]:
    sold_total = 0.0
    last_status = "none"
    last_px: Optional[float] = None
    for use_px in limits:
        remaining = size - sold_total
        if remaining < 0.01:
            break
        console.print(
            f"  [bold bright_yellow][SELL {label.upper()}][/] {slug}  "
            f"bid={bid:.3f}  limit>={use_px:.3f}  size={remaining:.2f}"
        )
        sold, last_status = _fak_sell(token_id, remaining, use_px, dry_run=dry_run)
        sold_total += float(sold or 0)
        last_px = float(use_px)
        if dry_run or sold_total >= size - tol:
            break
        time.sleep(0.35)
    return sold_total, last_status, last_px


def manage_sells(cfg: dict, state: dict, chain: ChainReader) -> None:
    """Loser persist dump at 3¢→2¢; optional winner cash-out at ~99.9¢."""
    if not cfg.get("sell_enabled"):
        return
    now = time.time()
    thr = float(cfg.get("sell_threshold") or 0.03)
    floor = float(cfg.get("sell_floor") or 0.02)
    opp_min = float(cfg.get("sell_opposite_min") or 0.90)
    persist_s = float(cfg.get("sell_persist_s") or 0.0)
    cooldown = float(cfg.get("sell_cooldown_s") or 3.0)
    winner_min = float(cfg.get("sell_winner_min") or 0.999)
    min_bid_size = float(cfg.get("sell_min_bid_size") or 1.0)
    tol = float(cfg.get("position_tolerance") or 0.01)
    dry_run = bool(cfg.get("dry_run"))
    funder = os.getenv("FUNDER_ADDRESS") or ""
    funder_cs = to_checksum_address(funder) if funder else None
    dirty = False
    ctf = str(cfg.get("ctf_address") or "")

    for cid, intent in list(state.get("intents", {}).items()):
        if intent.get("status") not in (
            "confirmed",
            "confirmed_waiting_inventory",
            "mined",
            "executed",
        ):
            continue
        end_ts = float(intent.get("end_ts") or 0)
        if end_ts and now > end_ts:
            continue

        up_tok = str(intent.get("up_token") or "")
        dn_tok = str(intent.get("dn_token") or "")
        if not up_tok or not dn_tok:
            continue

        up_bid, up_sz = _fetch_sized_bid(up_tok, min_bid_size)
        dn_bid, dn_sz = _fetch_sized_bid(dn_tok, min_bid_size)
        intent["last_up_bid"] = up_bid
        intent["last_dn_bid"] = dn_bid
        intent["last_up_bid_size"] = up_sz
        intent["last_dn_bid_size"] = dn_sz
        intent["updated_at"] = now
        dirty = True

        last = float(intent.get("last_sell_attempt_at") or 0)
        cooling = bool(last and now - last < cooldown)
        tokens = {"up": up_tok, "dn": dn_tok}
        bids = {"up": up_bid, "dn": dn_bid}
        shares = float(intent.get("shares") or cfg["shares"])
        sold_loser = bool(intent.get("sold_loser") or intent.get("sold_leg"))
        sold_winner = bool(intent.get("sold_winner"))

        winner = winner_cashout_leg(up_bid, dn_bid, winner_min)
        fire_w, armed_w, why_w = persist_ready(
            winner is not None and not sold_winner,
            now_s=now,
            armed_ts=intent.get("sell_winner_armed_at"),
            persist_s=persist_s,
        )
        intent["sell_winner_armed_at"] = armed_w
        if winner and why_w in {"armed", "waiting"}:
            log_event(
                "sell_winner_persist",
                condition_id=cid,
                slug=intent.get("slug"),
                leg=winner,
                why=why_w,
                bid=bids.get(winner),
            )

        if fire_w and winner and not cooling:
            w_tok = tokens[winner]
            size, latch = _sell_inventory(
                chain, ctf, funder_cs, w_tok, shares, tol,
                "seen_winner_inventory", intent,
            )
            if latch == "await_inventory":
                log_event(
                    "sell_skip_await_inventory",
                    condition_id=cid,
                    leg=winner,
                    path="winner",
                )
            elif latch == "already_flat":
                intent["sold_winner"] = True
                intent["sell_winner_note"] = "already_flat"
            else:
                intent["last_sell_attempt_at"] = now
                sold_total, last_status, last_px = _run_fak_ladder(
                    w_tok, size, [round(winner_min, 4)],
                    dry_run=dry_run,
                    bid=float(bids[winner] or winner_min),
                    label=f"win {winner}",
                    slug=intent.get("slug"),
                    tol=tol,
                )
                intent["sell_winner_attempts"] = int(
                    intent.get("sell_winner_attempts") or 0
                ) + 1
                intent["sell_winner_last_status"] = last_status
                if dry_run or sold_total >= size - tol:
                    intent["sold_winner"] = True
                    intent["sell_winner_filled"] = float(
                        intent.get("sell_winner_filled") or 0
                    ) + sold_total
                    intent["sell_winner_limit"] = last_px
                    if dry_run:
                        intent["sell_winner_dry"] = True
                    log_event(
                        "sell_winner_done",
                        condition_id=cid,
                        slug=intent.get("slug"),
                        leg=winner,
                        sold=sold_total,
                        bid=bids[winner],
                        status=last_status,
                    )
                    notify(
                        "Mint winner sold",
                        f"{intent.get('slug')}\n{winner} x{sold_total:.1f} @>={winner_min:.3f}",
                        priority="default",
                    )
                elif sold_total >= tol:
                    intent["sell_winner_filled"] = float(
                        intent.get("sell_winner_filled") or 0
                    ) + sold_total

        loser, loser_reason = classify_loser(
            up_bid, dn_bid, threshold=thr, opposite_min=opp_min,
        )
        if loser_reason == "both_cheap":
            log_event(
                "sell_skip_both_cheap",
                condition_id=cid,
                up_bid=up_bid,
                dn_bid=dn_bid,
                slug=intent.get("slug"),
            )
        elif loser_reason == "wick_unconfirmed":
            log_event(
                "sell_skip_wick_unconfirmed",
                condition_id=cid,
                up_bid=up_bid,
                dn_bid=dn_bid,
                slug=intent.get("slug"),
            )

        fire_l, armed_l, why_l = persist_ready(
            loser is not None and not sold_loser,
            now_s=now,
            armed_ts=intent.get("sell_loser_armed_at"),
            persist_s=persist_s,
        )
        intent["sell_loser_armed_at"] = armed_l
        if loser and why_l in {"armed", "waiting"}:
            log_event(
                "sell_loser_persist",
                condition_id=cid,
                slug=intent.get("slug"),
                leg=loser,
                why=why_l,
                bid=bids.get(loser),
            )

        if fire_l and loser and not cooling:
            l_tok = tokens[loser]
            loser_bid = float(bids[loser] or thr)
            size, latch = _sell_inventory(
                chain, ctf, funder_cs, l_tok, shares, tol,
                "seen_loser_inventory", intent,
            )
            if latch == "await_inventory":
                log_event(
                    "sell_skip_await_inventory",
                    condition_id=cid,
                    leg=loser,
                    path="loser",
                )
            elif latch == "already_flat":
                intent["sold_leg"] = loser
                intent["sold_loser"] = True
                intent["sell_note"] = "already_flat"
            else:
                limits = loser_ladder_limits(thr, floor, loser_bid)
                intent["last_sell_attempt_at"] = now
                sold_total, last_status, last_px = _run_fak_ladder(
                    l_tok, size, limits,
                    dry_run=dry_run,
                    bid=loser_bid,
                    label=loser,
                    slug=intent.get("slug"),
                    tol=tol,
                )
                intent["sell_attempts"] = int(intent.get("sell_attempts") or 0) + 1
                intent["sell_last_status"] = last_status
                done = dry_run or sold_total >= size - tol
                if sold_total >= tol or dry_run:
                    intent["sell_filled"] = float(
                        intent.get("sell_filled") or 0
                    ) + sold_total
                    intent["sell_limit"] = last_px
                if done:
                    intent["sold_leg"] = loser
                    intent["sold_loser"] = True
                    if dry_run:
                        intent["sell_dry"] = True
                    log_event(
                        "sell_loser_done",
                        condition_id=cid,
                        slug=intent.get("slug"),
                        leg=loser,
                        sold=sold_total,
                        bid=loser_bid,
                        status=last_status,
                    )
                    notify(
                        "Mint loser sold",
                        f"{intent.get('slug')}\n{loser} x{sold_total:.1f} "
                        f"@<={thr:.2f}/{floor:.2f}",
                        priority="default",
                    )
                    console.print(
                        f"  [bold bright_green][SELL OK][/] {loser} {sold_total:.2f}  "
                        f"kept opposite for redeem"
                    )

    if dirty:
        atomic_save(STATE_FILE, state)



def run_cycle(
    cfg: dict,
    state: dict,
    gateway: MarketGateway,
    chain: ChainReader,
) -> str:
    now = time.time()
    try:
        manage_sells(cfg, state, chain)
    except Exception as sell_exc:
        log_event("sell_cycle_error", error=str(sell_exc)[:240])
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

    if mint_slots_full(state, cfg, now, float(pick.start_ts)):
        write_heartbeat(
            "capped_open",
            open=open_intent_count(state),
            next_start=float(pick.start_ts),
        )
        return "capped_open"

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
            "start_ts": pick.start_ts,
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
        "start_ts": pick.start_ts,
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

    tx_id, err = submit_mint_batch(calls, metadata=f"mintbot:split:{pick.condition_id}:{int(now)}")
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
                "[dim]atomic mint · loser sell ladder 3c->2c · keep winner[/]",
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
