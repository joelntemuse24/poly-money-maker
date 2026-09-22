#!/usr/bin/env python3
"""15m atomic mint: split pUSD into Up+Down complete sets.

No CLOB buys. No hedges. Discovers **btc-up-or-down-15m** only, mints
`shares` for markets that are **not yet open** (start_ts in the future)
and open within enter_max_ttm_min, if collateral is available.

Optional sell (``sell_enabled``, default off): arm a loser scrap when the
sized loser bid is ≤ ``sell_threshold`` (~4¢) and the opposite bid is ≥ ~90¢.
4¢ is the arm ceiling, not the print. Persist ``sell_persist_s`` (~2.5s), or
``sell_persist_last_min_s`` (~1s) in the last
``sell_persist_last_min_window_s`` (~60s). Skip that wait when TTM ≤
``sell_persist_skip_ttm_s`` (~90s) or when depth at the FAK rung covers our
size. At fire, FAK ``sell_fak_px`` (~3¢) → ``sell_floor`` (~2¢), or the live
bid when the book is thinner. Never post the 4¢ arm. Empty keep fires a
blind 1¢ FAK (backoff ~3s). A FAK miss rests a GTD/GTC sell at
``sell_scrap_rest_px`` (~3¢). Wallet A never posts a bid. Keep the winner
for redeem unless its bid reaches ~99.9¢. Off unless live
``strategy_mint.json`` turns it on. Sell and mint run as independent loops
so Gamma/relayer work cannot steal a dump tick (bag
``btc-updown-15m-1789905600``). The sell loop sleeps ``sell_armed_poll_s``
(~2s, allowed below the ``poll_s >= 2`` floor) while a bag is sell-hot
(loser armed, or loser sold and dump/winner not done). Mint keeps
``poll_s``. Persist defaults are 2.5/1/60. Live JSON keys that already
exist (threshold, persist) override these defaults until the operator
edits them.

A third loop records Chainlink BTC/USD 60s TWAP (Polymarket RTDS) to
``logs/oracle_twap.jsonl`` while a 15m bag is open. ``oracle_log_enabled``
defaults on. In the last ``sell_late_window_s`` (~120s) before ``end_ts``,
loser scrap also requires a side-aware TWAP edge ≥
``max(sell_oracle_edge_floor_usd, sell_oracle_edge_per_ttm × TTM)`` for
``sell_oracle_edge_persist_s`` (~3s), fail-closed on missing/stale tape
(combat for true reverse ``btc-updown-15m-1790078400``). Outside that
window the tape is audit-only. If the feed fails outside the late gate,
the loop logs ``oracle_log_fail`` and trading continues.

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
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
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

from buy.book import best_bid_with_min_size, bid_fill_depth
from buy.chain import ChainReader
from buy.contracts import ContractCall, build_atomic_mint_calls
from buy.market import MarketGateway, MintMarket
from buy.mint_loops import IntentStore, run_job_loop, start_mint_sell_loops
from buy.oracle_log import OracleBagView, OracleLogService, snapshot_intents
from buy.mint_sell import (
    classify_loser,
    cycle_sleep_s,
    dump_fast_retry_eligible,
    dump_retry_ladder_limits,
    effective_loser_persist_s,
    empty_fak_status,
    inventory_latch,
    late_oracle_edge_persist,
    late_oracle_scrap_ok,
    loser_blind_fak_due,
    loser_empty_keep_qualify,
    loser_ladder_limits,
    loser_partial_fak_shares,
    loser_persist_ready,
    loser_scrap_persist_s,
    mint_cycle_sleep_s,
    parse_sell_fill_shares,
    persist_ready,
    posted_order_id,
    rest_order_matched_shares,
    resting_tif,
    scrap_rest_action,
    sell_fire_decision,
    sell_window_open,
    skip_mint_discovery_for_sell,
    winner_cashout_leg,
    winner_cheap_decision,
    winner_sell_limit,
)

load_dotenv()

console = Console()
REPO = Path(__file__).resolve().parent

STRATEGY_FILE = REPO / "strategy_mint.json"
STATE_FILE = REPO / "positions_mint.json"
LOG_FILE = REPO / "mintbot.log"
ORACLE_LOG_FILE = REPO / "logs" / "oracle_twap.jsonl"
LOCK_FILE = REPO / ".mintbot.lock"
HEARTBEAT_FILE = REPO / ".heartbeat_mint"
STOP_FILE = REPO / "STOP_MINT"

DEFAULTS = {
    "entry_enabled": False,
    "dry_run": True,
    "shares": 50.0,
    "enter_min_ttm_min": 0.0,
    "enter_max_ttm_min": 30.0,
    "mint_fail_cooldown_s": 90.0,
    "mint_submitting_timeout_s": 90.0,
    "mint_max_attempts": 3,
    "series_slugs": [
        "btc-up-or-down-15m",
    ],
    "one_entry_per_market": True,
    "max_open_sets": 1,
    "poll_s": 5.0,
    "sell_armed_poll_s": 2.0,
    # Chainlink 60s TWAP tape (+ late-window loser-scrap veto only).
    "oracle_log_enabled": True,
    "position_tolerance": 0.01,
    "require_accepting_orders": True,
    "sell_enabled": False,
    "sell_threshold": 0.04,
    "sell_fak_px": 0.03,
    "sell_floor": 0.02,
    "sell_opposite_min": 0.90,
    "sell_persist_s": 2.5,
    "sell_persist_last_min_s": 1.0,
    "sell_persist_last_min_window_s": 60.0,
    "sell_persist_skip_ttm_s": 90.0,
    "sell_persist_skip_when_sized": True,
    "sell_scrap_blind_enabled": True,
    "sell_scrap_blind_px": 0.01,
    "sell_scrap_blind_backoff_s": 3.0,
    "sell_scrap_rest_enabled": True,
    "sell_scrap_rest_px": 0.03,
    "sell_scrap_rest_min_ahead_s": 60.0,
    "sell_cooldown_s": 3.0,
    "sell_winner_min": 0.999,
    # Cheap 0.99 winner only if loser ≤ this AND loser+cheap_min > 1.0 (else redeem).
    "sell_winner_cheap_if_loser_le": 0.03,
    "sell_winner_min_cheap": 0.99,
    # Winner FAK live bid is clamped into this CLOB range (rich 0.995–0.999 books).
    "sell_clob_max_price": 0.99,
    "sell_clob_min_price": 0.01,
    # After loser sold: if held leg stays under this for sell_dump_persist_s, live-bid FAK dump.
    "sell_dump_enabled": True,
    "sell_dump_below": 0.80,
    "sell_dump_persist_s": 2.0,
    "sell_dump_fak_retries": 2,
    "sell_dump_ladder_step": 0.04,
    "sell_dump_ladder_rungs": 4,
    "sell_min_bid_size": 1.0,
    # Late-window oracle veto on full loser scrap (TTM ≤ window only).
    "sell_late_window_s": 120.0,
    "sell_oracle_edge_per_ttm": 1.5,
    "sell_oracle_edge_persist_s": 3.0,
    "sell_oracle_stale_s": 5.0,
    "sell_oracle_edge_floor_usd": 25.0,
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
STATE_LOCK = threading.RLock()
_intent_store: Optional[IntentStore] = None
_heartbeat_lock = threading.Lock()
_heartbeat_parts: Dict[str, dict] = {}
_ORACLE_SERVICE: Optional[OracleLogService] = None


def _set_oracle_service(service: Optional[OracleLogService]) -> None:
    global _ORACLE_SERVICE
    _ORACLE_SERVICE = service


def _oracle_bag_view(condition_id: str) -> OracleBagView:
    svc = _ORACLE_SERVICE
    if svc is None:
        return OracleBagView(twap=None, open_usd=None, obs_ts=None)
    try:
        return svc.bag_view(condition_id)
    except Exception:
        return OracleBagView(twap=None, open_usd=None, obs_ts=None)


_notify_pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="ntfy")

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

    def _post() -> None:
        try:
            requests.post(
                f"https://ntfy.sh/{topic}",
                data=message.encode("utf-8"),
                headers={"Title": title, "Priority": priority},
                timeout=5,
            )
        except Exception:
            return

    try:
        _notify_pool.submit(_post)
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
        return {"intents": {}}
    with open(STATE_FILE, encoding="utf-8") as handle:
        payload = json.load(handle)
    if not isinstance(payload, dict):
        raise ValueError("positions_mint.json must be an object")
    payload.setdefault("intents", {})
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
    if float(cfg.get("mint_fail_cooldown_s") or 0) < 0:
        raise ValueError("mint_fail_cooldown_s must be >= 0")
    if float(cfg.get("mint_submitting_timeout_s") or 0) < 0:
        raise ValueError("mint_submitting_timeout_s must be >= 0")
    if int(cfg.get("mint_max_attempts") or 0) < 1:
        raise ValueError("mint_max_attempts must be >= 1")
    if not cfg["series_slugs"]:
        raise ValueError("series_slugs must not be empty")
    if int(cfg["max_open_sets"]) < 1:
        raise ValueError("max_open_sets must be >= 1")
    if float(cfg["poll_s"]) < 2:
        raise ValueError("poll_s must be >= 2")
    if float(cfg.get("sell_armed_poll_s") or 0) < 0.2:
        raise ValueError("sell_armed_poll_s must be >= 0.2")
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
    if float(cfg.get("sell_persist_last_min_s") or 0) < 0:
        raise ValueError("sell_persist_last_min_s must be >= 0")
    if float(cfg.get("sell_persist_last_min_window_s") or 0) < 0:
        raise ValueError("sell_persist_last_min_window_s must be >= 0")
    fak_px = float(cfg.get("sell_fak_px", 0.03) or 0)
    if not (floor <= fak_px <= threshold):
        raise ValueError("sell_floor <= sell_fak_px <= sell_threshold must hold")
    if float(cfg.get("sell_persist_skip_ttm_s") or 0) < 0:
        raise ValueError("sell_persist_skip_ttm_s must be >= 0")
    if float(cfg.get("sell_scrap_blind_px") or 0) <= 0:
        raise ValueError("sell_scrap_blind_px must be > 0")
    if float(cfg.get("sell_scrap_rest_px") or 0) <= 0:
        raise ValueError("sell_scrap_rest_px must be > 0")
    if float(cfg.get("sell_scrap_blind_backoff_s") or 0) < 0:
        raise ValueError("sell_scrap_blind_backoff_s must be >= 0")
    if int(cfg.get("sell_dump_fak_retries") or 0) < 0:
        raise ValueError("sell_dump_fak_retries must be >= 0")
    if float(cfg.get("sell_dump_ladder_step") or 0) <= 0:
        raise ValueError("sell_dump_ladder_step must be > 0")
    if int(cfg.get("sell_dump_ladder_rungs") or 0) < 1:
        raise ValueError("sell_dump_ladder_rungs must be >= 1")
    if float(cfg.get("sell_min_bid_size") or 0) < 0:
        raise ValueError("sell_min_bid_size must be >= 0")

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

def mint_discovery_capped(state: dict, cfg: dict, now: float) -> bool:
    """True when Gamma/mint would only return capped_open.

    At max_open_sets full bags the adjacent next 15m is still allowed
    unless we already hold that window. Skip discover only when that
    slot is gone, so an armed loser does not starve N+1 mint.
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
    if not soonest_end:
        return True
    for intent in full:
        st = float(intent.get("start_ts") or 0)
        if st + 1e-6 >= soonest_end:
            return True
    return False

def already_minted(
    state: dict,
    condition_id: str,
    cfg: dict,
    now: float | None = None,
) -> bool:
    """True if this market must not be minted again this cycle.

    Confirmed / completed / in-flight intents stay blocked. A ``failed``
    intent is also blocked during ``mint_fail_cooldown_s`` and after
    ``mint_max_attempts`` tries, then becomes eligible for remint.
    """
    if not cfg.get("one_entry_per_market", True):
        return False
    intent = state.get("intents", {}).get(condition_id)
    if not intent:
        return False
    status = intent.get("status")
    if status in ACTIVE_STATUSES | {"completed"}:
        return True
    if status != "failed":
        return False
    try:
        max_attempts = int(cfg.get("mint_max_attempts") or 3)
    except (TypeError, ValueError):
        max_attempts = 3
    try:
        attempts = int(intent.get("mint_attempts") or 1)
    except (TypeError, ValueError):
        attempts = 1
    if attempts < 1:
        attempts = 1
    if attempts >= max_attempts:
        return True
    try:
        cooldown = float(cfg.get("mint_fail_cooldown_s") or 90.0)
    except (TypeError, ValueError):
        cooldown = 90.0
    now_ts = time.time() if now is None else float(now)
    try:
        last_fail = float(intent.get("last_fail_ts") or intent.get("updated_at") or 0)
    except (TypeError, ValueError):
        last_fail = 0.0
    if last_fail and now_ts < last_fail + cooldown:
        return True
    return False


def relayer_error_detail(record: dict) -> tuple[str, str]:
    """Pull errorMsg and a tx hash from a relayer status payload."""
    if not isinstance(record, dict):
        return "", ""
    msg = record.get("errorMsg")
    if msg is None or str(msg).strip() == "":
        msg = record.get("error") or record.get("message") or ""
    tx_hash = (
        record.get("transactionHash")
        or record.get("txHash")
        or record.get("hash")
        or record.get("transaction_hash")
        or ""
    )
    return str(msg)[:400], (str(tx_hash) if tx_hash else "")


def mark_intent_failed(
    intent: dict,
    now: float,
    error_msg: str = "",
    transaction_hash: str = "",
) -> None:
    """Persist a failed mint: status, last_fail_ts, errorMsg, optional hash."""
    intent["status"] = "failed"
    intent["updated_at"] = now
    intent["last_fail_ts"] = now
    if error_msg:
        intent["errorMsg"] = str(error_msg)[:400]
        intent["error"] = str(error_msg)[:400]
    if transaction_hash:
        intent["transaction_hash"] = str(transaction_hash)
    try:
        attempts = int(intent.get("mint_attempts") or 0)
    except (TypeError, ValueError):
        attempts = 0
    if attempts < 1:
        intent["mint_attempts"] = 1


def fail_stale_submitting_intents(state: dict, cfg: dict, now: float) -> int:
    """Auto-fail stale ``submitting`` intents that never received a tx id."""
    try:
        timeout_s = float(cfg.get("mint_submitting_timeout_s") or 0)
    except (TypeError, ValueError):
        timeout_s = 0.0
    if timeout_s <= 0:
        return 0

    stale = 0
    dirty = False
    with STATE_LOCK:
        for condition_id, intent in state.get("intents", {}).items():
            if not isinstance(intent, dict):
                continue
            if str(intent.get("status") or "") != "submitting":
                continue
            if str(intent.get("transaction_id") or "").strip():
                continue
            try:
                entered_submitting_at = float(intent.get("updated_at") or intent.get("created_at") or 0)
            except (TypeError, ValueError):
                entered_submitting_at = 0.0
            if entered_submitting_at <= 0:
                continue
            age_s = float(now) - entered_submitting_at
            if age_s <= timeout_s:
                continue
            mark_intent_failed(intent, now, error_msg="stale_submitting_no_tx")
            log_event(
                "mint_submitting_timeout_failed",
                condition_id=condition_id,
                slug=intent.get("slug"),
                age_s=round(age_s, 3),
                timeout_s=timeout_s,
                mint_attempts=intent.get("mint_attempts"),
            )
            stale += 1
            dirty = True
        if dirty:
            atomic_save(STATE_FILE, state)
    return stale

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
    skip_confirmed_inventory: bool = False,
) -> None:
    relayer_url = str(cfg["relayer_url"])
    ctf = str(cfg["ctf_address"])
    tol = float(cfg["position_tolerance"])
    with STATE_LOCK:
        items = list(state.get("intents", {}).items())
        snapshots = [
            (
                cid,
                str(intent.get("status") or ""),
                intent.get("transaction_id"),
                str(intent.get("up_token") or ""),
                str(intent.get("dn_token") or ""),
            )
            for cid, intent in items
            if isinstance(intent, dict)
        ]
    for cid, status, tx_id, up_tok, dn_tok in snapshots:
        record = None
        if status in ("submitting", "pending", "executed", "mined") and tx_id:
            record = get_relayer_transaction(relayer_url, str(tx_id))
        if record:
            relayer_state = str(record.get("state") or "")
            with STATE_LOCK:
                intent = state.get("intents", {}).get(cid)
                if not isinstance(intent, dict):
                    continue
                intent["relayer_state"] = relayer_state
                intent["updated_at"] = now
                if relayer_state in ("STATE_FAILED", "STATE_INVALID"):
                    error_msg, tx_hash = relayer_error_detail(record)
                    mark_intent_failed(
                        intent,
                        now,
                        error_msg=error_msg,
                        transaction_hash=tx_hash,
                    )
                    log_event(
                        "mint_failed",
                        condition_id=cid,
                        state=relayer_state,
                        slug=intent.get("slug"),
                        errorMsg=intent.get("errorMsg"),
                        transaction_hash=intent.get("transaction_hash"),
                    )
                elif relayer_state == "STATE_CONFIRMED":
                    intent["status"] = "confirmed_waiting_inventory"
                elif relayer_state == "STATE_MINED":
                    intent["status"] = "mined"
                elif relayer_state == "STATE_EXECUTED":
                    intent["status"] = "executed"
                status = str(intent.get("status") or status)
        with STATE_LOCK:
            intent = state.get("intents", {}).get(cid)
            if not isinstance(intent, dict):
                continue
            status = str(intent.get("status") or "")
            if status not in ("confirmed_waiting_inventory", "confirmed", "mined"):
                continue
            if skip_confirmed_inventory and status == "confirmed":
                continue
            up_tok = str(intent.get("up_token") or up_tok)
            dn_tok = str(intent.get("dn_token") or dn_tok)
        try:
            up = chain.position_balance(ctf, funder, up_tok)
            dn = chain.position_balance(ctf, funder, dn_tok)
        except Exception as exc:
            log_event("inventory_check_fail", condition_id=cid, error=str(exc)[:160])
            continue
        with STATE_LOCK:
            intent = state.get("intents", {}).get(cid)
            if not isinstance(intent, dict):
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
    write_loop_heartbeat("mint", status, **fields)


def write_loop_heartbeat(loop: str, status: str, **fields: Any) -> None:
    part = {"ts": time.time(), "status": status, **fields}
    with _heartbeat_lock:
        _heartbeat_parts[str(loop)] = part
        sell = _heartbeat_parts.get("sell") or {}
        mint = _heartbeat_parts.get("mint") or {}
        payload = {
            "ts": time.time(),
            "status": f"sell:{sell.get('status', '?')}|mint:{mint.get('status', '?')}",
            "sell": sell,
            "mint": mint,
        }
        if loop == "mint" and not sell:
            payload = {"ts": part["ts"], "status": status, "mint": part, **fields}
        temporary = str(HEARTBEAT_FILE) + ".tmp"
        with open(temporary, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, sort_keys=True)
        os.replace(temporary, HEARTBEAT_FILE)


@contextmanager
def _io_unlocked():
    """Release STATE_LOCK around I/O; reacquire even if the call fails.

    CPython RLock._is_owned lets AST-extracted helpers fetch books without
    a held lock.
    """
    owned = STATE_LOCK._is_owned()
    if owned:
        STATE_LOCK.release()
    try:
        yield
    finally:
        if owned:
            STATE_LOCK.acquire()

_clob_client = None
_clob_init_error = None
_book_pool = ThreadPoolExecutor(max_workers=2)

def _fetch_book(token_id: str, min_size: float):
    """REST `/book` → sized best bid plus raw bid levels for depth logs."""
    try:
        response = requests.get(
            "https://clob.polymarket.com/book",
            params={"token_id": str(token_id)},
            timeout=5,
        )
        if response.status_code != 200:
            return None, 0.0, []
        payload = response.json()
        book = payload if isinstance(payload, dict) else {}
        bids = book.get("bids") or []
        price, size = best_bid_with_min_size(bids, min_size=min_size)
        return price, size, bids
    except Exception as exc:
        log_event("book_fetch_fail", token_id=str(token_id)[:18], error=str(exc)[:160])
        return None, 0.0, []


def _fetch_books(up_tok: str, dn_tok: str, min_size: float):
    """Parallel UP/DN `/book` GETs (same-tick persist + FAK, no extra refetch)."""
    fut_up = _book_pool.submit(_fetch_book, up_tok, min_size)
    fut_dn = _book_pool.submit(_fetch_book, dn_tok, min_size)
    return fut_up.result(), fut_dn.result()


def _log_sell_book_depth(
    *,
    slug: Any,
    leg: Any,
    limit: Optional[float],
    our_size: float,
    bids: Any,
    ttm_s: Optional[float],
    path: str,
    phase: str,
    condition_id: Any = None,
) -> None:
    snap = bid_fill_depth(bids, limit)
    log_event(
        "sell_book_depth",
        condition_id=condition_id,
        slug=slug,
        leg=leg,
        limit=None if limit is None else round(float(limit), 4),
        our_size=round(float(our_size or 0), 4),
        best_bid=snap["best_bid"],
        best_bid_size=snap["best_bid_size"],
        depth_at_limit=snap["depth_at_limit"],
        ladder=snap["ladder"],
        ttm_s=None if ttm_s is None else round(float(ttm_s), 3),
        path=path,
        phase=phase,
    )

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
        with _io_unlocked():
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
    depth_bids: Any = None,
    depth_path: Optional[str] = None,
    depth_leg: Any = None,
    ttm_s: Optional[float] = None,
    condition_id: Any = None,
) -> Tuple[float, str, Optional[float]]:
    sold_total = 0.0
    last_status = "none"
    last_px: Optional[float] = None
    for use_px in limits:
        remaining = size - sold_total
        if remaining < 0.01:
            break
        if depth_path:
            _log_sell_book_depth(
                slug=slug,
                leg=depth_leg,
                limit=use_px,
                our_size=remaining,
                bids=depth_bids or [],
                ttm_s=ttm_s,
                path=depth_path,
                phase="fak",
                condition_id=condition_id,
            )
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


def _run_dump_fak_with_refire(
    *,
    token_id: str,
    size: float,
    initial_bid: float,
    initial_bids: Any,
    held: str,
    slug: Any,
    condition_id: str,
    ttm_s: Optional[float],
    floor: float,
    min_bid_size: float,
    retries: int,
    ladder_step: float,
    ladder_rungs: int,
    dry_run: bool,
    tol: float,
) -> Tuple[float, str, Optional[float], int, float]:
    """Held-dump path: first live-bid FAK, then fast refire ladders on miss."""
    live_bid = float(initial_bid or 0.0)
    initial_limits = [round(live_bid, 4)] if live_bid > 0 else []
    sold_total = 0.0
    last_status = "none"
    last_px: Optional[float] = None
    attempts = 0
    if not initial_limits:
        return sold_total, "bad_bid", last_px, attempts, live_bid

    with _io_unlocked():
        sold, last_status, last_px = _run_fak_ladder(
            token_id,
            size,
            initial_limits,
            dry_run=dry_run,
            bid=live_bid,
            label=f"dump {held}",
            slug=slug,
            tol=tol,
            depth_bids=initial_bids,
            depth_path="dump",
            depth_leg=held,
            ttm_s=ttm_s,
            condition_id=condition_id,
        )
    attempts += 1
    sold_total += float(sold or 0.0)
    if dry_run or sold_total >= size - tol:
        return sold_total, last_status, last_px, attempts, live_bid
    if not dump_fast_retry_eligible(sold=sold, status=last_status, tol=tol):
        return sold_total, last_status, last_px, attempts, live_bid

    for retry_idx in range(max(0, int(retries or 0))):
        with _io_unlocked():
            retry_bid, _retry_sz, retry_bids = _fetch_book(token_id, min_bid_size)
        if retry_bid is None:
            log_event(
                "sell_dump_fast_refire_stop",
                condition_id=condition_id,
                slug=slug,
                leg=held,
                retry=retry_idx + 1,
                attempts=attempts,
                reason="empty_book",
            )
            break
        live_bid = float(retry_bid or 0.0)
        limits = dump_retry_ladder_limits(
            live_bid,
            floor=floor,
            step=ladder_step,
            max_rungs=ladder_rungs,
        )
        if not limits:
            log_event(
                "sell_dump_fast_refire_stop",
                condition_id=condition_id,
                slug=slug,
                leg=held,
                retry=retry_idx + 1,
                attempts=attempts,
                reason="no_ladder_limits",
                bid=live_bid,
            )
            break
        log_event(
            "sell_dump_fast_refire_attempt",
            condition_id=condition_id,
            slug=slug,
            leg=held,
            retry=retry_idx + 1,
            attempts=attempts + 1,
            bid=live_bid,
            limits=limits,
        )
        with _io_unlocked():
            sold, last_status, last_px = _run_fak_ladder(
                token_id,
                size - sold_total,
                limits,
                dry_run=dry_run,
                bid=live_bid,
                label=f"dump {held}",
                slug=slug,
                tol=tol,
                depth_bids=retry_bids,
                depth_path="dump_refire",
                depth_leg=held,
                ttm_s=ttm_s,
                condition_id=condition_id,
            )
        attempts += 1
        sold_total += float(sold or 0.0)
        if dry_run or sold_total >= size - tol:
            break
        if not dump_fast_retry_eligible(sold=sold, status=last_status, tol=tol):
            log_event(
                "sell_dump_fast_refire_stop",
                condition_id=condition_id,
                slug=slug,
                leg=held,
                retry=retry_idx + 1,
                attempts=attempts,
                reason="non_retryable_status",
                status=last_status,
                sold=float(sold or 0.0),
            )
            break
    return sold_total, last_status, last_px, attempts, live_bid


def _apply_sell_fire_cancel(
    intent: dict,
    *,
    path: str,
    action: str,
    reason: str,
    bid: Optional[float],
    cid: str,
    extra: Optional[dict] = None,
) -> None:
    """Log sell_cancel_out_of_range and drop the arm on cancel_reset."""
    payload = dict(extra or {})
    log_event(
        "sell_cancel_out_of_range",
        condition_id=cid,
        slug=intent.get("slug"),
        path=path,
        action=action,
        reason=reason,
        bid=bid,
        **payload,
    )
    if action != "cancel_reset":
        return
    if path == "loser":
        intent["sell_loser_armed_at"] = None
        intent["sell_loser_leg"] = None
    elif path == "dump":
        intent["sell_dump_armed_at"] = None
    elif path == "winner":
        intent["sell_winner_armed_at"] = None


def _limit_sell(
    token_id: str,
    size: float,
    price: float,
    *,
    tif: str,
    expiration: int,
    dry_run: bool,
) -> Tuple[str, str]:
    """Resting GTD/GTC sell. Returns ``(order_id, status)``."""
    size = float(size)
    price = float(price)
    if size < 0.01 or price <= 0:
        return "", "bad_args"
    if dry_run:
        log_event(
            "dry_scrap_rest",
            token_id=str(token_id),
            size=size,
            price=price,
            tif=tif,
            expiration=int(expiration or 0),
        )
        return "dry-rest", "dry"
    client = _get_clob_client()
    if client is None:
        return "", f"no_clob:{_clob_init_error or 'unknown'}"
    try:
        from py_clob_client_v2 import (
            BalanceAllowanceParams,
            AssetType,
            OrderArgs,
            OrderType,
        )
        from py_clob_client_v2.order_builder.constants import SELL

        try:
            client.update_balance_allowance(
                BalanceAllowanceParams(
                    asset_type=AssetType.CONDITIONAL, token_id=str(token_id)
                )
            )
        except Exception as exc:
            log_event("sell_allowance_warn", error=str(exc)[:160])
        signed = client.create_order(
            OrderArgs(
                token_id=str(token_id),
                price=price,
                size=size,
                side=SELL,
                expiration=int(expiration or 0),
            )
        )
        order_type = OrderType.GTD if str(tif) == "GTD" else OrderType.GTC
        result = client.post_order(signed, order_type=order_type)
        status = "posted"
        if isinstance(result, dict):
            status = str(result.get("status") or "posted")
        oid = posted_order_id(result) or ""
        log_event(
            "sell_scrap_rest_result",
            token_id=str(token_id),
            size=size,
            price=price,
            tif=tif,
            order_id=oid,
            status=status,
        )
        return oid, status
    except Exception as exc:
        log_event("sell_scrap_rest_fail", error=str(exc)[:200])
        return "", f"error:{str(exc)[:80]}"


def _cancel_clob_order(order_id: str) -> bool:
    if not order_id or str(order_id).startswith("dry"):
        return True
    client = _get_clob_client()
    if client is None:
        return False
    try:
        from py_clob_client_v2.clob_types import OrderPayload

        client.cancel_order(OrderPayload(orderID=str(order_id)))
        return True
    except Exception as exc:
        log_event(
            "sell_scrap_rest_cancel_fail",
            order_id=str(order_id)[:18],
            error=str(exc)[:160],
        )
        return False


def _poll_rest_order(order_id: str, offered: float) -> Tuple[float, str]:
    if not order_id or str(order_id).startswith("dry"):
        return 0.0, "live"
    client = _get_clob_client()
    if client is None:
        return 0.0, "unknown"
    try:
        order = client.get_order(str(order_id))
    except Exception as exc:
        log_event("sell_scrap_rest_poll_fail", error=str(exc)[:160])
        return 0.0, "unknown"
    return rest_order_matched_shares(order, offered)


def _clear_scrap_rest(intent: dict) -> None:
    intent["sell_scrap_rest_id"] = None
    intent["sell_scrap_rest_size"] = None
    intent["sell_scrap_rest_matched"] = None


def _drop_scrap_rest(intent: dict, cid: str, reason: str) -> None:
    oid = intent.get("sell_scrap_rest_id")
    if not oid:
        return
    ok = True
    if reason != "filled" and not str(oid).startswith("dry"):
        with _io_unlocked():
            ok = _cancel_clob_order(str(oid))
    if not ok:
        return
    log_event(
        "sell_scrap_rest_cancel",
        condition_id=cid,
        slug=intent.get("slug"),
        order_id=oid,
        reason=reason,
    )
    _clear_scrap_rest(intent)


def _place_scrap_rest(
    intent: dict,
    cid: str,
    token_id: str,
    size: float,
    *,
    now: float,
    end_ts: float,
    rest_px: float,
    rest_ahead: float,
    rest_enabled: bool,
    dry_run: bool,
    oracle_blocks: bool,
    loser_qualifies: bool,
    armed: bool,
    fak_miss: bool,
) -> None:
    action, why = scrap_rest_action(
        enabled=rest_enabled,
        rest_order_id=intent.get("sell_scrap_rest_id"),
        sold_loser=bool(intent.get("sold_loser")),
        window_open=sell_window_open(now, end_ts),
        loser_qualifies=loser_qualifies,
        oracle_blocks=oracle_blocks,
        fak_miss=fak_miss,
        armed=armed,
    )
    if action != "place" or float(size) < 0.01 or not token_id:
        return
    tif, exp = resting_tif(
        now_s=now, expire_ts=float(end_ts or 0), min_ahead_s=rest_ahead
    )
    with _io_unlocked():
        oid, status = _limit_sell(
            token_id,
            float(size),
            float(rest_px),
            tif=tif,
            expiration=exp,
            dry_run=dry_run,
        )
    if not oid:
        log_event(
            "sell_scrap_rest_fail",
            condition_id=cid,
            slug=intent.get("slug"),
            status=status,
            why=why,
        )
        return
    intent["sell_scrap_rest_id"] = oid
    intent["sell_scrap_rest_px"] = float(rest_px)
    intent["sell_scrap_rest_size"] = float(size)
    intent["sell_scrap_rest_matched"] = 0.0
    intent["sell_scrap_rest_tif"] = tif
    log_event(
        "sell_scrap_rest_place",
        condition_id=cid,
        slug=intent.get("slug"),
        order_id=oid,
        price=float(rest_px),
        size=float(size),
        tif=tif,
        expiration=exp,
        why=why,
        status=status,
    )


def _sync_scrap_rest(
    intent: dict,
    cid: str,
    *,
    shares: float,
    tol: float,
    oracle_blocks: bool,
    loser_qualifies: bool,
    window_open: bool,
) -> None:
    oid = intent.get("sell_scrap_rest_id")
    if not oid:
        return
    offered = float(intent.get("sell_scrap_rest_size") or shares or 0)
    status = "live"
    matched = float(intent.get("sell_scrap_rest_matched") or 0)
    if not str(oid).startswith("dry"):
        with _io_unlocked():
            matched, status = _poll_rest_order(str(oid), offered)
        prev = float(intent.get("sell_scrap_rest_matched") or 0)
        delta = max(0.0, float(matched) - prev)
        if delta >= float(tol):
            intent["sell_filled"] = float(intent.get("sell_filled") or 0) + delta
            intent["sell_scrap_rest_matched"] = float(matched)
            if intent.get("sell_scrap_rest_px") is not None:
                intent["sell_limit"] = intent.get("sell_scrap_rest_px")
    filled = shares > 0 and float(intent.get("sell_filled") or 0) >= shares - tol
    if status == "filled" or filled:
        leg = intent.get("sell_loser_leg")
        intent["sold_loser"] = True
        if leg in ("up", "dn"):
            intent["sold_leg"] = leg
        intent["sell_oracle_edge_armed_at"] = None
        log_event(
            "sell_scrap_rest_fill",
            condition_id=cid,
            slug=intent.get("slug"),
            order_id=oid,
            matched=matched,
            leg=leg,
        )
        _drop_scrap_rest(intent, cid, reason="filled")
        return
    if status == "cancelled":
        log_event(
            "sell_scrap_rest_cancel",
            condition_id=cid,
            slug=intent.get("slug"),
            order_id=oid,
            reason="exchange",
        )
        _clear_scrap_rest(intent)
        return
    action, why = scrap_rest_action(
        enabled=True,
        rest_order_id=oid,
        sold_loser=bool(intent.get("sold_loser")),
        window_open=window_open,
        loser_qualifies=loser_qualifies,
        oracle_blocks=oracle_blocks,
        fak_miss=False,
        armed=intent.get("sell_loser_armed_at") is not None,
    )
    if action == "cancel":
        _drop_scrap_rest(intent, cid, reason=why)


def _mark_loser_sold(intent: dict, leg: str, *, note: str = "") -> None:
    intent["sold_leg"] = leg
    intent["sold_loser"] = True
    intent["sell_oracle_edge_armed_at"] = None
    if note:
        intent["sell_note"] = note


def manage_sells(cfg: dict, state: dict, chain: ChainReader) -> None:
    """Loser scrap: arm ≤4¢, FAK ~3¢→2¢ or live bid; winner; held dump."""
    if not cfg.get("sell_enabled"):
        return
    STATE_LOCK.acquire()
    try:
        _manage_sells_locked(cfg, state, chain)
    finally:
        STATE_LOCK.release()


def _manage_sells_locked(cfg: dict, state: dict, chain: ChainReader) -> None:
    now = time.time()
    thr = float(cfg.get("sell_threshold") or 0.04)
    floor = float(cfg.get("sell_floor") or 0.02)
    opp_min = float(cfg.get("sell_opposite_min") or 0.90)
    persist_s = float(cfg.get("sell_persist_s") or 0.0)
    last_min_s = float(cfg.get("sell_persist_last_min_s", 1.0))
    last_min_window_s = float(cfg.get("sell_persist_last_min_window_s", 60.0))
    skip_ttm_s = float(cfg.get("sell_persist_skip_ttm_s", 90.0) or 0.0)
    skip_when_sized = bool(cfg.get("sell_persist_skip_when_sized", True))
    fak_px = float(cfg.get("sell_fak_px", 0.03) or 0.03)
    blind_enabled = bool(cfg.get("sell_scrap_blind_enabled", True))
    blind_px = float(cfg.get("sell_scrap_blind_px", 0.01) or 0.01)
    blind_backoff = float(cfg.get("sell_scrap_blind_backoff_s", 3.0) or 0.0)
    rest_enabled = bool(cfg.get("sell_scrap_rest_enabled", True))
    rest_px = float(cfg.get("sell_scrap_rest_px", 0.03) or 0.03)
    rest_ahead = float(cfg.get("sell_scrap_rest_min_ahead_s", 60.0) or 60.0)
    cooldown = float(cfg.get("sell_cooldown_s") or 3.0)
    winner_min = float(cfg.get("sell_winner_min") or 0.999)
    clob_max = float(cfg.get("sell_clob_max_price") or 0.99)
    clob_min = float(cfg.get("sell_clob_min_price") or 0.01)
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
        if not sell_window_open(now, end_ts):
            if intent.get("sell_scrap_rest_id"):
                _drop_scrap_rest(intent, cid, reason="window_end")
                dirty = True
            continue

        up_tok = str(intent.get("up_token") or "")
        dn_tok = str(intent.get("dn_token") or "")
        if not up_tok or not dn_tok:
            continue

        # Same-tick books feed persist_ready and FAK; do not refetch after
        # persist-ready. Parallel UP/DN cuts sequential REST wait on the
        # armed path. The ~8.6–10.5s gap was poll_s plus mint-path work.
        with _io_unlocked():
            (up_bid, up_sz, up_bids), (dn_bid, dn_sz, dn_bids) = _fetch_books(
                up_tok, dn_tok, min_bid_size
            )
        books = {"up": up_bids, "dn": dn_bids}
        ttm_s = (end_ts - now) if end_ts else None
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

        # Prefer redeem at ~$1. Cheap 0.99 only if loser sold ≤ cheap_gate AND
        # loser_fill + cheap_min > 1.0 (beats mint). Flat 1¢+99¢ waits for redeem.
        loser_px = intent.get("sell_limit")
        try:
            loser_px_f = float(loser_px) if loser_px is not None else None
        except (TypeError, ValueError):
            loser_px_f = None
        cheap_gate = float(cfg.get("sell_winner_cheap_if_loser_le") or 0.03)
        cheap_min = float(cfg.get("sell_winner_min_cheap") or 0.99)
        effective_winner_min, cheap_on, cheap_why = winner_cheap_decision(
            sold_loser,
            loser_px_f,
            winner_min=winner_min,
            cheap_gate=cheap_gate,
            cheap_min=cheap_min,
        )
        prev_cheap_why = intent.get("sell_winner_cheap_reason")
        if cheap_why != prev_cheap_why and cheap_why in {
            "flat_or_negative_edge",
            "positive_edge",
        }:
            combined = (
                None
                if loser_px_f is None
                else round(float(loser_px_f) + float(cheap_min), 4)
            )
            log_event(
                "sell_winner_cheap_allowed" if cheap_on else "sell_winner_cheap_denied",
                condition_id=cid,
                slug=intent.get("slug"),
                loser_px=loser_px_f,
                cheap_min=cheap_min,
                cheap_gate=cheap_gate,
                combined=combined,
                reason=cheap_why,
                effective_winner_min=effective_winner_min,
            )
        intent["sell_winner_cheap_reason"] = cheap_why

        winner = winner_cashout_leg(up_bid, dn_bid, effective_winner_min)
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
            w_bid = bids.get(winner)
            fire_action, fire_reason = sell_fire_decision(
                "winner",
                bid=w_bid,
                winner_min=effective_winner_min,
                cheap_on=cheap_on,
            )
            if fire_action != "fire":
                _apply_sell_fire_cancel(
                    intent,
                    path="winner",
                    action=fire_action,
                    reason=fire_reason,
                    bid=w_bid,
                    cid=cid,
                    extra={"cheap_on": cheap_on, "winner_min": effective_winner_min},
                )
            else:
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
                    # Live sized bid once allowed, clamped to CLOB max (0.99) so
                    # rich 0.995–0.999 books still fill instead of invalid-price.
                    live_px = float(bids[winner] or effective_winner_min)
                    posted, clamped, clamp_why = winner_sell_limit(
                        live_px, clob_max=clob_max, clob_min=clob_min
                    )
                    if clamped and live_px > posted + 1e-12:
                        log_event(
                            "sell_winner_limit_clamped",
                            condition_id=cid,
                            slug=intent.get("slug"),
                            live=live_px,
                            posted=posted,
                            reason=clamp_why or "clob_max",
                        )
                    with _io_unlocked():
                        sold_total, last_status, last_px = _run_fak_ladder(
                            w_tok,
                            size,
                            [posted],
                            dry_run=dry_run,
                            bid=live_px,
                            label=f"win {winner}",
                            slug=intent.get("slug"),
                            tol=tol,
                            depth_bids=books.get(winner),
                            depth_path="winner_cheap" if cheap_on else None,
                            depth_leg=winner,
                            ttm_s=ttm_s,
                            condition_id=cid,
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
                            f"{intent.get('slug')}\n{winner} x{sold_total:.1f} @>={effective_winner_min:.3f}",
                            priority="default",
                        )
                    elif sold_total >= tol:
                        intent["sell_winner_filled"] = float(
                            intent.get("sell_winner_filled") or 0
                        ) + sold_total

        # Held-leg dump: after loser is sold, if the remaining leg stays under
        # sell_dump_below for sell_dump_persist_s, live-bid FAK (not a "hedge").
        sold_dump = bool(intent.get("sold_dump") or intent.get("sold_winner"))
        dump_enabled = bool(cfg.get("sell_dump_enabled", True))
        dump_below = float(cfg.get("sell_dump_below") or 0.80)
        dump_persist_s = float(cfg.get("sell_dump_persist_s") or 2.0)
        dump_retries = int(cfg.get("sell_dump_fak_retries", 2))
        dump_ladder_step = float(cfg.get("sell_dump_ladder_step") or 0.04)
        dump_ladder_rungs = int(cfg.get("sell_dump_ladder_rungs", 4))
        sold_leg = intent.get("sold_leg")
        held = None
        if sold_leg == "up":
            held = "dn"
        elif sold_leg == "dn":
            held = "up"
        dump_bid = bids.get(held) if held else None
        dump_armed = (
            dump_enabled
            and sold_loser
            and not sold_dump
            and held is not None
            and dump_bid is not None
            and float(dump_bid) < dump_below - 1e-12
        )
        fire_d, armed_d, why_d = persist_ready(
            dump_armed,
            now_s=now,
            armed_ts=intent.get("sell_dump_armed_at"),
            persist_s=dump_persist_s,
        )
        intent["sell_dump_armed_at"] = armed_d
        if dump_armed and why_d in {"armed", "waiting"}:
            log_event(
                "sell_dump_persist",
                condition_id=cid,
                slug=intent.get("slug"),
                leg=held,
                why=why_d,
                bid=dump_bid,
                below=dump_below,
            )
        if fire_d and held and not cooling:
            fire_action, fire_reason = sell_fire_decision(
                "dump", bid=dump_bid, dump_below=dump_below,
            )
            if fire_action != "fire":
                _apply_sell_fire_cancel(
                    intent,
                    path="dump",
                    action=fire_action,
                    reason=fire_reason,
                    bid=dump_bid,
                    cid=cid,
                    extra={"below": dump_below},
                )
            else:
                d_tok = tokens[held]
                size, latch = _sell_inventory(
                    chain, ctf, funder_cs, d_tok, shares, tol,
                    "seen_dump_inventory", intent,
                )
                if latch == "await_inventory":
                    log_event(
                        "sell_skip_await_inventory",
                        condition_id=cid,
                        leg=held,
                        path="dump",
                    )
                elif latch == "already_flat":
                    intent["sold_dump"] = True
                    intent["sold_winner"] = True
                    intent["sell_dump_note"] = "already_flat"
                else:
                    live_px = float(dump_bid or 0)
                    intent["last_sell_attempt_at"] = now
                    sold_total, last_status, last_px, used_attempts, live_px = (
                        _run_dump_fak_with_refire(
                            token_id=d_tok,
                            size=size,
                            initial_bid=live_px,
                            initial_bids=books.get(held),
                            held=held,
                            slug=intent.get("slug"),
                            condition_id=cid,
                            ttm_s=ttm_s,
                            floor=floor,
                            min_bid_size=min_bid_size,
                            retries=dump_retries,
                            ladder_step=dump_ladder_step,
                            ladder_rungs=dump_ladder_rungs,
                            dry_run=dry_run,
                            tol=tol,
                        )
                    )
                    intent["sell_dump_attempts"] = int(
                        intent.get("sell_dump_attempts") or 0
                    ) + int(used_attempts)
                    intent["sell_dump_last_status"] = last_status
                    if dry_run or sold_total >= size - tol:
                        intent["sold_dump"] = True
                        intent["sold_winner"] = True
                        intent["sell_dump_filled"] = float(
                            intent.get("sell_dump_filled") or 0
                        ) + sold_total
                        intent["sell_dump_limit"] = last_px
                        if dry_run:
                            intent["sell_dump_dry"] = True
                        log_event(
                            "sell_dump_done",
                            condition_id=cid,
                            slug=intent.get("slug"),
                            leg=held,
                            sold=sold_total,
                            bid=dump_bid,
                            status=last_status,
                        )
                        notify(
                            "Mint held dump",
                            f"{intent.get('slug')}\n{held} x{sold_total:.1f} "
                            f"@<{dump_below:.2f} (live {live_px:.3f})",
                            priority="default",
                        )
                        console.print(
                            f"  [bold bright_yellow][DUMP OK][/] {held} {sold_total:.2f}  "
                            f"bid={live_px:.3f} (<{dump_below:.2f})"
                        )
                    elif sold_total >= tol:
                        intent["sell_dump_filled"] = float(
                            intent.get("sell_dump_filled") or 0
                        ) + sold_total

        prev_leg = intent.get("sell_loser_leg")
        if prev_leg not in ("up", "dn"):
            prev_leg = None
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

        depth_at_limit = 0.0
        if loser in ("up", "dn") and bids.get(loser) is not None:
            ladder_preview = loser_ladder_limits(
                thr, floor, float(bids[loser]), fak_px=fak_px,
            )
            if ladder_preview:
                depth_at_limit = float(
                    bid_fill_depth(
                        books.get(loser) or [], ladder_preview[0]
                    ).get("depth_at_limit")
                    or 0
                )
        remaining_shares = max(
            0.0, shares - float(intent.get("sell_filled") or 0)
        )
        loser_persist_s, persist_why = loser_scrap_persist_s(
            now_s=now,
            end_ts=end_ts,
            persist_s=persist_s,
            last_min_s=last_min_s,
            last_min_window_s=last_min_window_s,
            skip_ttm_s=skip_ttm_s,
            depth_at_limit=depth_at_limit,
            our_size=remaining_shares,
            skip_when_sized=skip_when_sized,
        )
        if loser_persist_s is None:
            continue
        prev_persist = intent.get("sell_persist_effective_s")
        if (
            prev_persist is not None
            and abs(float(prev_persist) - float(loser_persist_s)) > 1e-12
        ):
            log_event(
                "sell_persist_effective",
                condition_id=cid,
                slug=intent.get("slug"),
                ttm=round(end_ts - now, 3) if end_ts else None,
                effective_s=loser_persist_s,
                why=persist_why,
            )
        intent["sell_persist_effective_s"] = loser_persist_s
        if persist_why in {"late_skip", "sized_skip"} and (
            intent.get("sell_persist_skip_why") != persist_why
        ):
            log_event(
                "sell_persist_skip",
                condition_id=cid,
                slug=intent.get("slug"),
                why=persist_why,
                ttm=round(end_ts - now, 3) if end_ts else None,
                depth_at_limit=depth_at_limit,
                our_size=remaining_shares,
            )
        intent["sell_persist_skip_why"] = persist_why

        keep_empty, keep_leg = loser_empty_keep_qualify(
            armed_ts=intent.get("sell_loser_armed_at"),
            up_bid=up_bid,
            dn_bid=dn_bid,
            opposite_min=opp_min,
            prev_leg=prev_leg or loser,
            sold_loser=sold_loser,
        )
        # Rearm only when the latch is already gone; do not treat an empty
        # *opposite* book as keep (that is wick_unconfirmed / reset).
        fak_rearm = (
            intent.get("sell_loser_armed_at") is None
            and empty_fak_status(intent.get("sell_last_status"))
            and (up_bid is None or dn_bid is None)
        )
        fire_l, armed_l, why_l = loser_persist_ready(
            loser is not None and not sold_loser,
            now_s=now,
            armed_ts=intent.get("sell_loser_armed_at"),
            persist_s=loser_persist_s,
            last_status=intent.get("sell_last_status"),
            book_empty=keep_empty or fak_rearm,
        )
        intent["sell_loser_armed_at"] = armed_l
        if why_l == "reset":
            intent["sell_loser_leg"] = None
            intent["sell_oracle_edge_armed_at"] = None
        else:
            intent["sell_loser_leg"] = loser or keep_leg or prev_leg
        persist_leg = loser or intent.get("sell_loser_leg")

        late_window_s = float(cfg.get("sell_late_window_s", 120.0) or 0.0)
        edge_per_ttm = float(cfg.get("sell_oracle_edge_per_ttm", 1.5) or 0.0)
        edge_persist_s = float(cfg.get("sell_oracle_edge_persist_s", 3.0) or 0.0)
        stale_s = float(cfg.get("sell_oracle_stale_s", 5.0) or 0.0)
        floor_usd = float(cfg.get("sell_oracle_edge_floor_usd", 25.0) or 0.0)
        ttm_for_oracle = float(end_ts - now) if end_ts else None
        in_late = (
            ttm_for_oracle is not None
            and late_window_s > 0
            and float(ttm_for_oracle) <= late_window_s + 1e-12
            and float(ttm_for_oracle) > 0
        )
        oracle_fire = True
        edge_detail: dict = {}
        edge_why = "outside_late_window"
        oracle_block_why = "outside_late_window"
        if not in_late or sold_loser or persist_leg not in ("up", "dn"):
            intent["sell_oracle_edge_armed_at"] = None
        elif in_late:
            view = _oracle_bag_view(cid)
            age_s = (
                None
                if view.obs_ts is None
                else max(0.0, float(now) - float(view.obs_ts))
            )
            # Gate qualifies on the scrap leg even while CLOB arm waits.
            scrap_for_edge = loser or persist_leg
            edge_ok, edge_why, edge_detail = late_oracle_scrap_ok(
                ttm_s=ttm_for_oracle,
                scrap_leg=scrap_for_edge,
                twap_usd=view.twap,
                open_usd=view.open_usd,
                twap_age_s=age_s,
                late_window_s=late_window_s,
                edge_per_ttm=edge_per_ttm,
                floor_usd=floor_usd,
                stale_s=stale_s,
            )
            # Only accumulate edge persist while CLOB also still sees a loser.
            edge_qualify = bool(edge_ok) and loser is not None and not sold_loser
            oracle_fire, armed_o, why_o = late_oracle_edge_persist(
                edge_qualify,
                now_s=now,
                armed_ts=intent.get("sell_oracle_edge_armed_at"),
                persist_s=edge_persist_s,
            )
            intent["sell_oracle_edge_armed_at"] = armed_o
            oracle_block_why = edge_why if not edge_ok else why_o
            if edge_qualify and why_o in {"armed", "waiting"}:
                log_event(
                    "sell_loser_oracle_persist",
                    condition_id=cid,
                    slug=intent.get("slug"),
                    leg=scrap_for_edge,
                    why=why_o,
                    ttm=edge_detail.get("ttm"),
                    edge=edge_detail.get("edge"),
                    need=edge_detail.get("need"),
                    open_ref=edge_detail.get("open_usd"),
                    twap=edge_detail.get("twap"),
                    age_s=edge_detail.get("age_s"),
                )
            elif not edge_ok and loser is not None and not sold_loser:
                log_event(
                    "sell_loser_oracle_block",
                    condition_id=cid,
                    slug=intent.get("slug"),
                    leg=scrap_for_edge,
                    why=edge_why,
                    ttm=edge_detail.get("ttm"),
                    edge=edge_detail.get("edge"),
                    need=edge_detail.get("need"),
                    open_ref=edge_detail.get("open_usd"),
                    twap=edge_detail.get("twap"),
                    age_s=edge_detail.get("age_s"),
                )

        oracle_hard_block = bool(
            in_late and edge_why not in ("edge_ok", "outside_late_window")
        )
        oracle_blocks_new = bool(in_late and not oracle_fire)
        loser_qualifies = bool(loser is not None or keep_empty)
        _sync_scrap_rest(
            intent,
            cid,
            shares=shares,
            tol=tol,
            oracle_blocks=oracle_hard_block,
            loser_qualifies=loser_qualifies,
            window_open=True,
        )
        sold_loser = bool(intent.get("sold_loser") or intent.get("sold_leg"))

        if loser and why_l in {"ready", "immediate"}:
            loser_bid = float(bids[loser] or thr)
            preview = loser_ladder_limits(thr, floor, loser_bid, fak_px=fak_px)
            first_limit = preview[0] if preview else min(float(loser_bid), fak_px)
            _log_sell_book_depth(
                slug=intent.get("slug"),
                leg=loser,
                limit=first_limit,
                our_size=shares,
                bids=books.get(loser) or [],
                ttm_s=ttm_s,
                path="loser",
                phase="ready",
                condition_id=cid,
            )
        if loser and why_l in {"armed", "waiting"}:
            log_event(
                "sell_loser_persist",
                condition_id=cid,
                slug=intent.get("slug"),
                leg=loser,
                why=why_l,
                bid=bids.get(loser),
            )
        elif why_l in {"empty_fak_keep_arm", "empty_fak_rearm", "empty_keep_arm"}:
            log_event(
                "sell_loser_persist",
                condition_id=cid,
                slug=intent.get("slug"),
                leg=persist_leg,
                why=why_l,
                bid=bids.get(persist_leg) if persist_leg in bids else None,
            )

        if (
            fire_l
            and loser
            and not cooling
            and not intent.get("sold_loser")
            and not intent.get("sell_scrap_rest_id")
        ):
            opp_leg = "dn" if loser == "up" else "up"
            fire_action, fire_reason = sell_fire_decision(
                "loser",
                bid=bids.get(loser),
                opposite_bid=bids.get(opp_leg),
                threshold=thr,
                floor=floor,
                opposite_min=opp_min,
            )
            if fire_action != "fire":
                _apply_sell_fire_cancel(
                    intent,
                    path="loser",
                    action=fire_action,
                    reason=fire_reason,
                    bid=bids.get(loser),
                    cid=cid,
                    extra={"opposite_bid": bids.get(opp_leg), "threshold": thr},
                )
            elif in_late and not oracle_fire:
                log_event(
                    "sell_loser_oracle_block",
                    condition_id=cid,
                    slug=intent.get("slug"),
                    leg=loser,
                    why=oracle_block_why,
                    ttm=edge_detail.get("ttm"),
                    edge=edge_detail.get("edge"),
                    need=edge_detail.get("need"),
                    open_ref=edge_detail.get("open_usd"),
                    twap=edge_detail.get("twap"),
                    age_s=edge_detail.get("age_s"),
                    clob_ready=True,
                )
            else:
                if in_late:
                    log_event(
                        "sell_loser_oracle_ok",
                        condition_id=cid,
                        slug=intent.get("slug"),
                        leg=loser,
                        why="ready",
                        ttm=edge_detail.get("ttm"),
                        edge=edge_detail.get("edge"),
                        need=edge_detail.get("need"),
                        open_ref=edge_detail.get("open_usd"),
                        twap=edge_detail.get("twap"),
                        age_s=edge_detail.get("age_s"),
                    )
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
                    intent["sell_oracle_edge_armed_at"] = None
                else:
                    limits = loser_ladder_limits(
                        thr, floor, loser_bid, fak_px=fak_px,
                    )
                    post_size = loser_partial_fak_shares(
                        remaining=size, depth_at_limit=depth_at_limit,
                    )
                    intent["last_sell_attempt_at"] = now
                    with _io_unlocked():
                        sold_total, last_status, last_px = _run_fak_ladder(
                            l_tok, post_size, limits,
                            dry_run=dry_run,
                            bid=loser_bid,
                            label=loser,
                            slug=intent.get("slug"),
                            tol=tol,
                            depth_bids=books.get(loser),
                            depth_path="loser",
                            depth_leg=loser,
                            ttm_s=ttm_s,
                            condition_id=cid,
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
                        intent["sell_oracle_edge_armed_at"] = None
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
                    elif empty_fak_status(last_status):
                        _place_scrap_rest(
                            intent,
                            cid,
                            l_tok,
                            max(0.0, size - sold_total),
                            now=now,
                            end_ts=end_ts,
                            rest_px=rest_px,
                            rest_ahead=rest_ahead,
                            rest_enabled=rest_enabled,
                            dry_run=dry_run,
                            oracle_blocks=oracle_blocks_new,
                            loser_qualifies=True,
                            armed=intent.get("sell_loser_armed_at") is not None,
                            fak_miss=True,
                        )

        if (
            not intent.get("sold_loser")
            and not intent.get("sell_scrap_rest_id")
            and persist_leg in ("up", "dn")
            and why_l in {"empty_keep_arm", "empty_fak_keep_arm"}
        ):
            b_tok = tokens.get(persist_leg) or ""
            b_size, b_latch = _sell_inventory(
                chain, ctf, funder_cs, b_tok, shares, tol,
                "seen_loser_inventory", intent,
            )
            if b_latch == "already_flat":
                _mark_loser_sold(intent, persist_leg, note="already_flat")
            else:
                blind_fire, blind_why = loser_blind_fak_due(
                    why=why_l,
                    now_s=now,
                    last_blind_at=intent.get("sell_blind_last_at"),
                    backoff_s=blind_backoff,
                    sold_loser=False,
                    has_inventory=b_latch == "has_inventory",
                    rest_live=False,
                    oracle_blocks=oracle_blocks_new,
                    enabled=blind_enabled,
                )
                if blind_fire and b_tok:
                    intent["sell_blind_last_at"] = now
                    with _io_unlocked():
                        blind_sold, blind_status = _fak_sell(
                            b_tok, b_size, blind_px, dry_run=dry_run,
                        )
                    log_event(
                        "sell_scrap_blind",
                        condition_id=cid,
                        slug=intent.get("slug"),
                        leg=persist_leg,
                        price=blind_px,
                        size=b_size,
                        sold=blind_sold,
                        status=blind_status,
                        why=blind_why,
                    )
                    intent["sell_attempts"] = int(intent.get("sell_attempts") or 0) + 1
                    intent["sell_last_status"] = blind_status
                    if float(blind_sold or 0) >= tol:
                        intent["sell_filled"] = float(
                            intent.get("sell_filled") or 0
                        ) + float(blind_sold)
                        intent["sell_limit"] = blind_px
                    if float(blind_sold or 0) >= b_size - tol and not dry_run:
                        _mark_loser_sold(intent, persist_leg)
                        log_event(
                            "sell_loser_done",
                            condition_id=cid,
                            slug=intent.get("slug"),
                            leg=persist_leg,
                            sold=blind_sold,
                            status=blind_status,
                        )
                    elif empty_fak_status(blind_status):
                        _place_scrap_rest(
                            intent,
                            cid,
                            b_tok,
                            max(0.0, b_size - float(blind_sold or 0)),
                            now=now,
                            end_ts=end_ts,
                            rest_px=rest_px,
                            rest_ahead=rest_ahead,
                            rest_enabled=rest_enabled,
                            dry_run=dry_run,
                            oracle_blocks=oracle_blocks_new,
                            loser_qualifies=True,
                            armed=True,
                            fak_miss=True,
                        )
        elif (
            why_l in {"empty_fak_keep_arm", "empty_fak_rearm"}
            and not intent.get("sell_scrap_rest_id")
            and not intent.get("sold_loser")
            and persist_leg in ("up", "dn")
            and empty_fak_status(intent.get("sell_last_status"))
        ):
            _place_scrap_rest(
                intent,
                cid,
                tokens.get(persist_leg) or "",
                remaining_shares,
                now=now,
                end_ts=end_ts,
                rest_px=rest_px,
                rest_ahead=rest_ahead,
                rest_enabled=rest_enabled,
                dry_run=dry_run,
                oracle_blocks=oracle_blocks_new,
                loser_qualifies=loser_qualifies,
                armed=intent.get("sell_loser_armed_at") is not None,
                fak_miss=True,
            )

    if dirty:
        atomic_save(STATE_FILE, state)

def _claim_mint_intent(
    state: dict,
    condition_id: str,
    intent: dict,
    cfg: dict,
    now: float,
    candidate_start_ts: float,
) -> Optional[str]:
    """Under STATE_LOCK: refuse if already minted or slots full; else write intent."""
    if already_minted(state, condition_id, cfg, now):
        return "already_minted"
    if mint_slots_full(state, cfg, now, float(candidate_start_ts)):
        return "capped_open"
    store = _intent_store
    if store is not None:
        claimed = store.try_claim_condition(
            condition_id,
            intent,
            is_blocked=lambda current: already_minted(
                {"intents": {condition_id: current}} if current else {"intents": {}},
                condition_id,
                cfg,
                now,
            ),
        )
        if not claimed:
            return "already_minted"
        return None
    state.setdefault("intents", {})[condition_id] = intent
    return None


def run_sell_cycle(cfg: dict, state: dict, chain: ChainReader) -> str:
    """Sell-only tick. Never waits on Gamma / relayer / mint precheck."""
    if STOP_FILE.exists():
        write_loop_heartbeat("sell", "stopped")
        return "stopped"
    try:
        manage_sells(cfg, state, chain)
    except Exception as sell_exc:
        log_event("sell_cycle_error", error=str(sell_exc)[:240])
        write_loop_heartbeat("sell", "error", error=str(sell_exc)[:120])
        return "error"
    with STATE_LOCK:
        hot = skip_mint_discovery_for_sell(state, time.time())
    write_loop_heartbeat("sell", "hot" if hot else "idle")
    return "hot" if hot else "idle"


def run_mint_cycle(
    cfg: dict,
    state: dict,
    gateway: MarketGateway,
    chain: ChainReader,
) -> str:
    """Discover / reconcile / mint. Sell ticks live on the sibling loop."""
    now = time.time()
    if STOP_FILE.exists():
        write_loop_heartbeat("mint", "stopped")
        return "stopped"
    fail_stale_submitting_intents(state, cfg, now)

    funder = os.getenv("FUNDER_ADDRESS") or ""
    if funder:
        with STATE_LOCK:
            sell_armed = skip_mint_discovery_for_sell(state, now)
        reconcile_intents(
            state,
            cfg,
            chain,
            to_checksum_address(funder),
            now,
            skip_confirmed_inventory=sell_armed,
        )
        with STATE_LOCK:
            atomic_save(STATE_FILE, state)

    with STATE_LOCK:
        submitting = any(
            intent.get("status") == "submitting"
            for intent in state.get("intents", {}).values()
        )
    if submitting:
        write_loop_heartbeat("mint", "wait_submit")
        return "wait_submit"

    if not cfg.get("entry_enabled"):
        write_loop_heartbeat("mint", "disabled")
        return "disabled"

    markets = gateway.discover(list(cfg["series_slugs"]))
    candidates = eligible_markets(markets, cfg, now)
    if not candidates:
        write_loop_heartbeat("mint", "idle", markets=len(markets), eligible=0)
        return "idle"

    data_positions: Dict[str, float] = {}
    if funder:
        try:
            data_positions = gateway.positions(funder)
        except Exception as exc:
            log_event("positions_fetch_fail", error=str(exc)[:160])

    tol = float(cfg["position_tolerance"])
    pick: Optional[MintMarket] = None
    with STATE_LOCK:
        for market in candidates:
            if already_minted(state, market.condition_id, cfg, now):
                continue
            if float(data_positions.get(market.up_token, 0)) > tol:
                continue
            if float(data_positions.get(market.dn_token, 0)) > tol:
                continue
            pick = market
            break

        if pick is None:
            write_loop_heartbeat(
                "mint", "idle", markets=len(markets), eligible=len(candidates), reason="owned"
            )
            return "idle_owned"

        if mint_slots_full(state, cfg, now, float(pick.start_ts)):
            write_loop_heartbeat(
                "mint",
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
        dry_intent = {
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
        with STATE_LOCK:
            reason = _claim_mint_intent(
                state, pick.condition_id, dry_intent, cfg, now, float(pick.start_ts)
            )
            if reason is None:
                atomic_save(STATE_FILE, state)
        if reason is not None:
            write_loop_heartbeat("mint", reason, slug=pick.slug)
            return reason
        write_loop_heartbeat("mint", "dry_mint", slug=pick.slug)
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
            write_loop_heartbeat("mint", "no_balance", balance=balance)
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
    with STATE_LOCK:
        prev = state.get("intents", {}).get(pick.condition_id) or {}
        try:
            prev_attempts = int(prev.get("mint_attempts") or 0)
        except (TypeError, ValueError):
            prev_attempts = 0
        intent = {
            "created_at": float(prev.get("created_at") or now),
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
            "mint_attempts": prev_attempts + 1,
            "last_fail_ts": prev.get("last_fail_ts"),
            "errorMsg": prev.get("errorMsg"),
        }
        reason = _claim_mint_intent(
            state, pick.condition_id, intent, cfg, now, float(pick.start_ts)
        )
        if reason is None:
            atomic_save(STATE_FILE, state)
    if reason is not None:
        write_loop_heartbeat("mint", reason, slug=pick.slug)
        return reason

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
        mint_attempts=intent.get("mint_attempts"),
    )

    tx_id, err = submit_mint_batch(calls, metadata=f"mintbot:split:{pick.condition_id}:{int(now)}")
    with STATE_LOCK:
        intent = state["intents"][pick.condition_id]
        intent["updated_at"] = time.time()
        if not tx_id:
            mark_intent_failed(intent, time.time(), error_msg=str(err or ""))
            atomic_save(STATE_FILE, state)
            fail_msg = intent.get("errorMsg")
        else:
            intent["transaction_id"] = tx_id
            intent["submitted_at"] = time.time()
            intent["status"] = "pending"
            atomic_save(STATE_FILE, state)
            fail_msg = None
    if not tx_id:
        console.print(f"  [dim red][MINT FAIL][/] {err}")
        log_event(
            "mint_submit_fail",
            condition_id=pick.condition_id,
            error=err,
            errorMsg=fail_msg,
        )
        notify("Mint submit failed", f"{pick.slug}\n{err}", priority="high")
        write_loop_heartbeat("mint", "submit_fail")
        return "submit_fail"

    console.print(f"  [bold bright_green][MINT ▶][/] tx={tx_id[:18]}…")
    log_event("mint_submitted", condition_id=pick.condition_id, transaction_id=tx_id)
    notify("Mint submitted", f"{pick.slug}\n{shares:.0f} sets · {tx_id[:18]}…", priority="default")
    write_loop_heartbeat("mint", "submitted", slug=pick.slug)
    return "submitted"


def _reload_cfg(cfg_box: Dict[str, Any]) -> dict:
    try:
        loaded = load_strategy()
    except Exception as exc:
        log_event("strategy_reload_fail", error=str(exc)[:200])
        current = cfg_box.get("cfg") or {}
        loaded = {**current, "entry_enabled": False}
    cfg_box["cfg"] = loaded
    return loaded


def main() -> int:
    global _intent_store
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
    _intent_store = IntentStore(state, lock=STATE_LOCK)
    gateway = MarketGateway(
        gamma_url=str(cfg["gamma_url"]),
        data_api_url=str(cfg["data_api_url"]),
        discover_cache_s=8.0,
    )
    sell_chain = ChainReader(str(cfg["rpc_url"]))
    mint_chain = ChainReader(str(cfg["rpc_url"]))
    cfg_box: Dict[str, Any] = {"cfg": cfg}

    console.print(
        Panel(
            Align.center(
                "[bold bright_cyan]MINT DESK[/]\n"
                f"[dim]shares={cfg['shares']} · not-yet-open · opens within "
                f"{cfg['enter_max_ttm_min']}m · "
                f"dry_run={cfg['dry_run']} · entry_enabled={cfg['entry_enabled']}[/]\n"
                "[dim]atomic mint · loser sell ladder 3c->2c · keep winner[/]\n"
                "[dim]sell loop ⊥ mint/discover loop[/]",
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
        loops=("sell", "mint", "oracle"),
        oracle_log_enabled=bool(cfg.get("oracle_log_enabled", True)),
    )
    if cfg.get("oracle_log_enabled", True):
        console.print(
            "[dim]▶ oracle tape[/] logs/oracle_twap.jsonl  "
            "[dim](+ late loser-scrap veto ≤120s TTM)[/]"
        )

    def should_stop() -> bool:
        return _shutdown or STOP_FILE.exists()

    def sell_tick() -> None:
        current = _reload_cfg(cfg_box)
        status = run_sell_cycle(current, state, sell_chain)
        if status not in ("idle", "hot", "stopped"):
            log_event("sell_cycle", status=status)

    def mint_tick() -> None:
        current = _reload_cfg(cfg_box)
        status = run_mint_cycle(current, state, gateway, mint_chain)
        if status not in ("idle", "idle_owned", "disabled", "wait_submit"):
            log_event("cycle", status=status)

    def sell_sleep() -> float:
        current = cfg_box.get("cfg") or cfg
        with STATE_LOCK:
            return cycle_sleep_s(current, state, time.time())

    def mint_sleep() -> float:
        current = cfg_box.get("cfg") or cfg
        return mint_cycle_sleep_s(current)

    oracle = OracleLogService(ORACLE_LOG_FILE)
    _set_oracle_service(oracle)

    def oracle_tick() -> None:
        current = cfg_box.get("cfg") or {}
        enabled = bool(current.get("oracle_log_enabled", True))
        with STATE_LOCK:
            snap = snapshot_intents(state)
        oracle.tick(
            snap,
            time.time(),
            enabled=enabled,
            on_fail=lambda msg: log_event("oracle_log_fail", error=str(msg)[:240]),
        )

    def oracle_sleep() -> float:
        try:
            return float(oracle.sleep_s)
        except (TypeError, ValueError):
            return 5.0

    sell_thread, mint_thread = start_mint_sell_loops(
        sell_tick=sell_tick,
        sell_sleep_s=sell_sleep,
        mint_tick=mint_tick,
        mint_sleep_s=mint_sleep,
        should_stop=should_stop,
        on_sell_error=lambda exc: (
            log_event("sell_cycle_error", error=str(exc)[:300]),
            write_loop_heartbeat("sell", "error", error=str(exc)[:120]),
        ),
        on_mint_error=lambda exc: (
            log_event("cycle_error", error=str(exc)[:300]),
            console.print(f"[red]cycle_error[/] {exc}"),
            write_loop_heartbeat("mint", "error", error=str(exc)[:120]),
        ),
    )
    oracle_thread = threading.Thread(
        target=run_job_loop,
        kwargs={
            "name": "oracle",
            "tick": oracle_tick,
            "sleep_s": oracle_sleep,
            "should_stop": should_stop,
            "on_error": lambda exc: log_event(
                "oracle_log_fail", error=str(exc)[:240]
            ),
        },
        name="mintbot-oracle",
        daemon=True,
    )
    oracle_thread.start()
    while not should_stop():
        time.sleep(0.25)
    sell_thread.join(timeout=5)
    mint_thread.join(timeout=5)
    oracle_thread.join(timeout=5)

    console.print("[dim]mintbot stopped[/]")
    try:
        lock.close()
    except Exception:
        pass
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
