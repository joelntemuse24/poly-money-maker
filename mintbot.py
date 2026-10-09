#!/usr/bin/env python3
"""15m atomic mint: split pUSD into Up+Down complete sets.

No CLOB buys. No hedges. Discovers **btc-up-or-down-15m** only, mints
`shares` for markets that are **not yet open** (start_ts in the future)
and open within enter_max_ttm_min, if collateral is available.

Optional sell (``sell_enabled``, default off): arm a loser scrap when the
sized loser bid is ≤ ``sell_threshold`` (~2¢) and the opposite bid is ≥ ~90¢.
Persist ``sell_persist_s`` (~5s), or ``sell_persist_last_min_s`` (~2s) in
the last ``sell_persist_last_min_window_s`` (~60s). That last-minute wait
applies through market close. Sized depth does not skip
(``sell_persist_skip_when_sized`` default false). ``sell_scrap_max_ttm_s``
(code default 0, off; example 600) blocks arm, persist, and every scrap
fire while seconds-to-close is above the cutoff. Unknown time-to-end
leaves that gate open. Persist starts only once the gate is open. At fire,
``sell_scrap_sweep_enabled`` (default true) posts one FAK at the live loser
bid (capped at ``sell_threshold``, floored at ``sell_clob_min_price`` 1¢,
never clamped up to ``sell_floor``) for the scrap remainder and retries each
tick at the new bid. ``sell_scrap_fraction`` defaults to 1 (the whole
loser). Below 1, the first fire locks ``floor(held × fraction)`` and does
not sell the rest. False restores the 1¢ ladder from ``sell_fak_px``.
``sell_late_price_window_s`` (default 0) leaves that pair in place. When
it is positive and both late prices are set, a known ttm at or under the
window uses ``sell_threshold_late`` / ``sell_fak_px_late`` for the arm,
the persist check, and the FAK cap. Unknown ttm stays on the base pair.
This is not ``sell_late_window_s`` (the oracle veto, still off at 0).
Empty keep fires a blind 1¢ FAK (backoff ~3s) under that same active
threshold; the blind print stays ``sell_scrap_blind_px``.
A FAK miss rests a GTD/GTC sell at ``min(sell_scrap_rest_px, live or
last-seen loser bid)`` so a 1¢ book is not posted at the 2¢ print.
``sell_scrap_rest_px`` stays ~2¢. GTD only when expiration is at least
``sell_scrap_rest_min_ahead_s`` (~180s) ahead; otherwise GTC. Wallet A
never posts a resting bid. Keep the winner for redeem unless its bid reaches
~99.9¢. Off unless live
``strategy_mint.json`` turns it on. Sell and mint run as independent loops
so Gamma/relayer work cannot steal a dump tick (bag
``btc-updown-15m-1789905600``). The sell loop sleeps ``sell_armed_poll_s``
(~2s, allowed below the ``poll_s >= 1`` floor) while a bag is sell-hot
(loser armed, or loser sold and dump/winner not done, or a reclaim is
still watching). Mint keeps ``poll_s``. Persist defaults are 5/2/60.
Live JSON keys that already exist (threshold, persist) override these
defaults until the operator edits them.

Opt-in reclaim (``reclaim_enabled``, default false): after a both-sides
dump, buy about ``reclaim_usd`` of the first side whose book holds at
``reclaim_entry`` for ``reclaim_entry_persist_s``. The other side confirms
when its best ask is at or under ``1 - reclaim_entry``. There is no
ask-depth gate. The FAK is a market buy at
``min(ask + reclaim_slippage, reclaim_max_price)`` (default 3¢ over the
ask, hard cap 96¢), sized off that limit, with USDC truncated to cents.
A top-up whose ask is already above the cap logs
``reclaim_topup_skipped_cap`` and is not posted. Every send, first or
retry, re-checks that tick's books (entry, sister ask, fresh uncrossed
book, spread, and ask at or under the cap). A failed check does not send
and starts the persist hold again. ``reclaim_min_ttm_s`` (default 0, off)
skips entry with ``too_close`` when seconds-to-close is under it. Stop-sell
at ``reclaim_stop`` (default on) or hold to redeem. A missing or stale bid
does not clear the stop clock. Each dump or stop refire refetches that
leg and sells at the live bid. While the reclaim is open, or a dump or
stop sell is in progress, the next sequential mint waits until the
position is closed or the bag's window has ended. The watch stays on
``sell_armed_poll_s`` until that finishes. A position still held at
resolution is redeemed by the existing redeem thread.

A third loop records Chainlink BTC/USD 60s TWAP (Polymarket RTDS) to
``logs/oracle_twap.jsonl`` while a 15m bag is open. ``oracle_log_enabled``
defaults on (audit tape only). ``sell_late_window_s`` defaults to 0,
which skips the late-window loser-scrap veto entirely.
``sell_oracle_edge_floor_usd``, ``sell_oracle_edge_per_ttm``, and
``sell_oracle_stale_s`` also default to 0, so setting the window back
above 0 does not restore the old $25 / 1.5×TTM / 5s-stale veto.
``sell_oracle_edge_persist_s`` stays 3s. Mint, winner cash-out, and held
dump do not read the tape. If the feed
fails, the loop logs ``oracle_log_fail`` and trading continues.

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
import math
import os
import signal
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

import requests
from dotenv import load_dotenv
from eth_utils import to_checksum_address
from rich import box
from rich.align import Align
from rich.console import Console
from rich.panel import Panel

from buy.book import (
    best_ask_with_min_size,
    best_bid_with_min_size,
    bid_fill_depth,
    book_age_s,
)
from buy.chain import ChainReader, thread_session
from buy.contracts import ContractCall, build_atomic_mint_calls, build_redeem_calls
from buy.mint_gas import mint_gas_settings, validate_mint_gas
from buy.mint_redeem import RedeemDesk, RedeemIO, redeem_settings, validate_redeem
from buy.mint_sequence import (
    SeqWaits,
    seq_busy_bag,
    seq_eligible_markets,
    seq_late_markets,
    seq_settings,
    validate_seq,
)
from buy.market import MarketGateway, MintMarket
from buy.mint_loops import (
    IntentStore,
    held_forward_floor,
    mint_cash_block,
    chain_reconcile_action,
    pending_mint_reserve,
    persist_digest,
    run_job_loop,
    select_mint_candidate,
    start_mint_sell_loops,
)
from buy.oracle_log import OracleBagView, OracleLogService, snapshot_intents
from buy.whatsapp_notify import BagAlerts, WhatsAppNotifier
from buy.mint_sell import (
    buy_fill_vwap,
    classify_loser,
    cycle_sleep_s,
    dump_fast_retry_eligible,
    bag_risk_flush,
    bag_risk_observe,
    bag_risk_payload,
    dump_time_gate_open,
    effective_dump_persist_s,
    effective_loser_persist_s,
    fresh_bag_risk,
    empty_fak_status,
    inventory_latch,
    is_balance_allowance_reject,
    tracked_sell_size,
    advance_oracle_edge_arm,
    late_oracle_scrap_ok,
    loser_blind_fak_due,
    loser_empty_keep_qualify,
    loser_ladder_limits,
    kept_leg_below_winner_min,
    loser_partial_fak_shares,
    loser_scrap_post,
    normalize_scrap_fraction,
    scrap_order_shares,
    scrap_share_plan,
    scrap_target_met,
    sell_fill_vwap,
    record_fill_px,
    reclaim_arm_block,
    reclaim_buy_order_key,
    reclaim_buy_usdc,
    reclaim_entry_decision,
    reclaim_fak_status,
    reclaim_stop_decision,
    recorded_fill_px,
    cfg_seconds,
    sell_plan_banner,
    loser_persist_ready,
    loser_scrap_persist_s,
    mint_cycle_sleep_s,
    parse_sell_fill_shares,
    persist_ready,
    posted_order_id,
    rest_order_matched_shares,
    resting_tif,
    scrap_rest_action,
    scrap_active_prices,
    scrap_live_bid_limit,
    scrap_rest_px,
    scrap_time_gate_open,
    sell_fire_decision,
    sell_window_open,
    skip_mint_discovery_for_sell,
    winner_cashout_leg,
    winner_cheap_decision,
    winner_sell_limit,
    kept_loser_open,
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
    # 45m keeps the window after a bag booked near 30m out visible
    # (two 15m steps past a market that is about to open).
    "enter_max_ttm_min": 45.0,
    "mint_fail_cooldown_s": 30.0,
    "mint_submitting_timeout_s": 90.0,
    "mint_max_attempts": 3,
    # Signed relay gasLimit. eth_estimateGas of the factory batch plus
    # margin, else fallback. Both clamp to min(cap, relay-hub 650k).
    "mint_gas_margin": 0.15,
    "mint_gas_fallback": 650_000,
    "mint_gas_cap": 650_000,
    "series_slugs": [
        "btc-up-or-down-15m",
    ],
    "one_entry_per_market": True,
    "max_open_sets": 1,
    # Sequential bags (one bag of capital). Mint the next window only in
    # [start - lead, start + cutoff] and only once the previous bag's
    # winner is cashed or its window ended. Short cash waits; past the
    # cutoff the window is skipped (mint_seq_skip).
    "mint_sequential": False,
    "mint_seq_lead_s": 30.0,
    "mint_seq_cutoff_s": 240.0,
    # Auto-redeem resolved positions via the collateral adapter (relayer
    # PROXY batch, same signing as mint). Off unless set true.
    "redeem_enabled": False,
    "redeem_poll_s": 15.0,
    "redeem_min_after_end_s": 60.0,
    "redeem_retry_s": 60.0,
    "redeem_max_attempts": 6,
    "redeem_tx_timeout_s": 300.0,
    "redeem_startup_sweep": True,
    "redeem_min_payout_usd": 0.01,
    # WhatsApp alerts (CallMeBot). No-op unless CALLMEBOT_PHONE and
    # CALLMEBOT_APIKEY are set in the environment.
    "notify_scrap_whatsapp": False,
    "notify_dump_whatsapp": False,
    # Danger: after the loser scrap, the held winner's sized bid stays
    # under notify_danger_px for notify_danger_hold_s. One alert per bag.
    "notify_danger_whatsapp": True,
    "notify_danger_px": 0.70,
    "notify_danger_hold_s": 5.0,
    # False: a sold loser frees its mint slot even when a partial scrap
    # kept some loser shares. True: those kept shares hold the slot until
    # the window's grace ends (fewer mints, capped open exposure).
    "count_kept_loser_as_open": False,
    "poll_s": 5.0,
    "sell_armed_poll_s": 2.0,
    # CLOB /book and /books. A hung read costs one short tick.
    "book_timeout_s": 1.2,
    # Wallet pUSD while a sequential mint is waiting on cash.
    "mint_cash_poll_s": 2.0,
    # Chainlink 60s TWAP tape. The scrap veto is off unless
    # sell_late_window_s is positive.
    "oracle_log_enabled": False,
    "position_tolerance": 0.01,
    "require_accepting_orders": True,
    "sell_enabled": False,
    "sell_threshold": 0.02,
    "sell_fak_px": 0.02,
    # 0 keeps sell_threshold / sell_fak_px for the whole scrap window.
    # A positive window uses the late pair when ttm is known and <= it.
    # Unset late prices stay on the base pair. Not sell_late_window_s.
    "sell_threshold_late": None,
    "sell_fak_px_late": None,
    "sell_late_price_window_s": 0.0,
    # sell_floor is the cent-ladder / dump floor only. The loser sweep does
    # NOT use it: once triggered (loser <= sell_threshold, favourite >=
    # sell_opposite_min) the sweep FAK posts at the live loser bid, capped
    # at sell_threshold and floored at sell_clob_min_price (1c), and retries
    # each tick at the new bid until flat or the window closes.
    "sell_floor": 0.02,
    # One live-bid FAK for the full remainder. False restores the cent ladder.
    "sell_scrap_sweep_enabled": True,
    "sell_opposite_min": 0.90,
    "sell_persist_s": 5.0,
    "sell_persist_last_min_s": 2.0,
    "sell_persist_last_min_window_s": 60.0,
    "sell_persist_skip_when_sized": False,
    "sell_scrap_blind_enabled": True,
    "sell_scrap_blind_px": 0.01,
    "sell_scrap_blind_backoff_s": 3.0,
    "sell_scrap_rest_enabled": True,
    "sell_scrap_rest_px": 0.02,
    "sell_scrap_rest_min_ahead_s": 180.0,
    # 0 disables the scrap time-left gate (old behavior). The example sets 600:
    # arm, persist, and fire the loser scrap only when seconds-to-close is
    # at or under this. Unknown ttm (no end_ts) leaves the gate open.
    "sell_scrap_max_ttm_s": 0.0,
    # 1 scraps the whole loser. Below 1, the first fire locks
    # floor(held * fraction) and holds the remainder to resolution.
    "sell_scrap_fraction": 1.0,
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
    # Dump persist in the last window seconds; None follows sell_dump_persist_s.
    # Window 0 = off.
    "sell_dump_persist_last_min_s": None,
    "sell_dump_persist_last_min_window_s": 0.0,
    "sell_dump_fak_retries": 2,
    "sell_dump_ladder_step": 0.04,
    "sell_dump_ladder_rungs": 4,
    # 0 disables the time-left gate (old behavior). The example sets 240:
    # arm and fire the held dump only when seconds-to-close is at or under this.
    "sell_dump_max_ttm_s": 0.0,
    # When the held dump fills, also sell the kept scrap half (1c floor sweep).
    "sell_dump_also_kept": False,
    # Post-dump reclaim buy. Off until strategy_mint.json sets it true.
    # The stop stays on unless reclaim_stop_enabled is false.
    "reclaim_enabled": False,
    "reclaim_usd": 100.0,
    "reclaim_entry": 0.91,
    # Same default as the dump persist. Explicit 0 fires on the qualifying tick.
    "reclaim_entry_persist_s": 0.5,
    "reclaim_stop": 0.75,
    "reclaim_stop_enabled": True,
    "reclaim_stop_persist_s": 0.5,
    # FAK may pay this much over the ask, and never more than the cap.
    "reclaim_slippage": 0.03,
    "reclaim_max_price": 0.96,
    "reclaim_max_ttm_s": 0.0,
    # 0 leaves the close gate off. No reclaim entry when ttm is under this.
    "reclaim_min_ttm_s": 0.0,
    "sell_min_bid_size": 1.0,
    # 0 skips the late-window Chainlink veto on loser scrap.
    # Floor, per-TTM, and stale are 0 so a positive window does not
    # restore the old $25 / 1.5×TTM / 5s-stale veto. Edge persist stays 3s.
    "sell_late_window_s": 0.0,
    "sell_oracle_edge_per_ttm": 0.0,
    "sell_oracle_edge_persist_s": 3.0,
    "sell_oracle_stale_s": 0.0,
    "sell_oracle_edge_floor_usd": 0.0,
    # Any-time loser-scrap veto (not the late window above, not the dump):
    # block a scrap while the 60s TWAP or (scrap_oracle_veto_use_live) the
    # live Chainlink price, minus the strike, is not at least
    # scrap_oracle_veto_usd against the scrapped leg. A reading older than
    # scrap_oracle_veto_stale_s drops out; both stale, or no strike, falls
    # back to no veto.
    "scrap_oracle_veto_enabled": False,
    "scrap_oracle_veto_usd": 5.0,
    "scrap_oracle_veto_stale_s": 3.0,
    "scrap_oracle_veto_use_live": True,
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
# Mint and redeem share the signer's relayer nonce (/relay-payload then
# /submit). One submit at a time.
RELAY_SUBMIT_LOCK = threading.Lock()
_SEQ_WAITS = SeqWaits()
# Persist form of the last atomic_save that finished. None until load or
# the first successful save. Ticks compare against this, so a mutation
# that raised before the save is still written on the next tick.
_saved_persist_form: Any = None
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
# CALLMEBOT_PHONE / CALLMEBOT_APIKEY come from systemd EnvironmentFile=.env.
_BAG_ALERTS = BagAlerts(
    WhatsAppNotifier.from_env(os.environ, log=lambda event, **kw: log_event(event, **kw)),
    log=lambda event, **kw: log_event(event, **kw),
)


def _whatsapp(action: str, *args: Any, **kwargs: Any) -> None:
    """Scrap/dump WhatsApp alert hook. Non-blocking; never raises."""
    alerts = globals().get("_BAG_ALERTS")
    if alerts is None:
        return
    try:
        getattr(alerts, action)(*args, **kwargs)
    except Exception:
        return

def _signal_handler(signum, frame):
    global _shutdown
    _shutdown = True

def log_setup() -> None:
    import logging

    from buy.log_archive import ArchiveRotatingFileHandler

    logger = logging.getLogger("mintbot")
    logger.setLevel(logging.INFO)
    logger.handlers.clear()
    fmt = logging.Formatter("%(message)s")
    # Live file still rolls at 2 MB. Rotated bytes move to logs/archive
    # and are gzipped off the trading loop. Nothing in the archive is pruned.
    fh = ArchiveRotatingFileHandler(LOG_FILE, maxBytes=2_000_000)
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
    data = json.dumps(payload, separators=(",", ":"), sort_keys=True)
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
    remember_persisted_state(payload)


def remember_persisted_state(payload: dict) -> None:
    """Record the digest of a state that is now on disk."""
    global _saved_persist_form
    _saved_persist_form = persist_digest(payload if isinstance(payload, dict) else {})


def persist_dirty(state: dict) -> bool:
    """True when ``state`` differs from the last successful save.

    Compares the digest: live intents in full, terminal intents as id plus
    status, and any top-level keys other than ``intents``.
    """
    return persist_digest(state if isinstance(state, dict) else {}) != _saved_persist_form


def commit_state(state: dict, *, dirty: bool = False) -> bool:
    """Write positions when something persist-relevant is unsaved.

    ``dirty`` covers callers that already know they mutated state. The
    compare is against the last successful ``atomic_save``, not a copy
    taken at the start of this tick.
    """
    if not dirty and not persist_dirty(state):
        return False
    atomic_save(STATE_FILE, state)
    return True

def load_state() -> dict:
    if not STATE_FILE.exists():
        payload = {"intents": {}}
        remember_persisted_state(payload)
        return payload
    with open(STATE_FILE, encoding="utf-8") as handle:
        payload = json.load(handle)
    if not isinstance(payload, dict):
        raise ValueError("positions_mint.json must be an object")
    payload.setdefault("intents", {})
    remember_persisted_state(payload)
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
    validate_mint_gas(cfg)
    validate_seq(cfg)
    validate_redeem(cfg)
    if not cfg["series_slugs"]:
        raise ValueError("series_slugs must not be empty")
    if int(cfg["max_open_sets"]) < 1:
        raise ValueError("max_open_sets must be >= 1")
    if float(cfg["poll_s"]) < 1:
        raise ValueError("poll_s must be >= 1")
    if float(cfg.get("sell_armed_poll_s") or 0) < 0.2:
        raise ValueError("sell_armed_poll_s must be >= 0.2")
    for key, floor in (("book_timeout_s", 0.05), ("mint_cash_poll_s", 0.0)):
        raw = cfg.get(key)
        try:
            value = float(raw)
        except (TypeError, ValueError):
            raise ValueError(f"{key} must be >= {floor}")
        if value != value or value < floor or value == float("inf"):
            raise ValueError(f"{key} must be >= {floor}")
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
    fak_px = float(cfg.get("sell_fak_px", 0.02) or 0)
    if not (floor <= fak_px <= threshold):
        raise ValueError("sell_floor <= sell_fak_px <= sell_threshold must hold")
    late_window_raw = cfg.get("sell_late_price_window_s", 0.0)
    if late_window_raw is None:
        late_window = 0.0
    else:
        try:
            late_window = float(late_window_raw)
        except (TypeError, ValueError):
            raise ValueError("sell_late_price_window_s must be >= 0")
        if (
            late_window != late_window
            or late_window < 0
            or late_window == float("inf")
        ):
            raise ValueError("sell_late_price_window_s must be >= 0")
    late_thr_set = cfg.get("sell_threshold_late") is not None
    late_fak_set = cfg.get("sell_fak_px_late") is not None
    if late_thr_set != late_fak_set:
        raise ValueError(
            "sell_threshold_late and sell_fak_px_late must both be set"
        )
    if late_thr_set:
        try:
            late_thr = float(cfg.get("sell_threshold_late"))
            late_fak = float(cfg.get("sell_fak_px_late"))
        except (TypeError, ValueError):
            raise ValueError(
                "sell_floor <= sell_fak_px_late <= sell_threshold_late < "
                "sell_opposite_min must hold"
            )
        if (
            late_thr != late_thr
            or late_fak != late_fak
            or late_thr in (float("inf"), float("-inf"))
            or late_fak in (float("inf"), float("-inf"))
            or not (floor <= late_fak <= late_thr < opposite)
        ):
            raise ValueError(
                "sell_floor <= sell_fak_px_late <= sell_threshold_late < "
                "sell_opposite_min must hold"
            )
    if float(cfg.get("sell_scrap_blind_px") or 0) <= 0:
        raise ValueError("sell_scrap_blind_px must be > 0")
    if float(cfg.get("sell_scrap_rest_px") or 0) <= 0:
        raise ValueError("sell_scrap_rest_px must be > 0")
    if float(cfg.get("sell_scrap_blind_backoff_s") or 0) < 0:
        raise ValueError("sell_scrap_blind_backoff_s must be >= 0")
    for key in (
        "sell_dump_persist_s",
        "sell_dump_persist_last_min_s",
        "sell_dump_persist_last_min_window_s",
        "sell_cooldown_s",
        "sell_scrap_rest_min_ahead_s",
    ):
        raw = cfg.get(key)
        if raw is not None and float(raw) < 0:
            raise ValueError(f"{key} must be >= 0")
    if int(cfg.get("sell_dump_fak_retries") or 0) < 0:
        raise ValueError("sell_dump_fak_retries must be >= 0")
    if float(cfg.get("sell_dump_ladder_step") or 0) <= 0:
        raise ValueError("sell_dump_ladder_step must be > 0")
    if int(cfg.get("sell_dump_ladder_rungs") or 0) < 1:
        raise ValueError("sell_dump_ladder_rungs must be >= 1")
    if float(cfg.get("sell_dump_max_ttm_s") or 0) < 0:
        raise ValueError("sell_dump_max_ttm_s must be >= 0")
    if float(cfg.get("sell_scrap_max_ttm_s") or 0) < 0:
        raise ValueError("sell_scrap_max_ttm_s must be >= 0")
    try:
        scrap_fraction = float(cfg.get("sell_scrap_fraction", 1.0))
    except (TypeError, ValueError):
        raise ValueError("sell_scrap_fraction must be > 0 and <= 1")
    # NaN fails the ordered compare. Inf is > 1.
    if not (0 < scrap_fraction <= 1) or scrap_fraction != scrap_fraction:
        raise ValueError("sell_scrap_fraction must be > 0 and <= 1")
    if float(cfg.get("sell_min_bid_size") or 0) < 0:
        raise ValueError("sell_min_bid_size must be >= 0")
    if float(cfg.get("reclaim_usd") or 0) <= 0:
        raise ValueError("reclaim_usd must be > 0")
    try:
        reclaim_entry = float(cfg.get("reclaim_entry") or 0)
        reclaim_stop = float(cfg.get("reclaim_stop") or 0)
    except (TypeError, ValueError):
        raise ValueError("0 < reclaim_stop < reclaim_entry < 1 must hold")
    if not (0 < reclaim_stop < reclaim_entry < 1):
        raise ValueError("0 < reclaim_stop < reclaim_entry < 1 must hold")
    try:
        reclaim_slip = float(cfg.get("reclaim_slippage", 0.03))
        reclaim_cap = float(cfg.get("reclaim_max_price", 0.96))
    except (TypeError, ValueError):
        raise ValueError("reclaim_slippage must be >= 0 and 0 < reclaim_max_price < 1")
    # NaN fails the ordered compare. Inf is not a price.
    if reclaim_slip != reclaim_slip or reclaim_slip < 0 or reclaim_slip == float("inf"):
        raise ValueError("reclaim_slippage must be >= 0")
    if (
        reclaim_cap != reclaim_cap
        or reclaim_cap in (float("inf"), float("-inf"))
        or not (0 < reclaim_cap < 1)
    ):
        raise ValueError("0 < reclaim_max_price < 1 must hold")
    if reclaim_cap + 1e-12 < reclaim_entry:
        raise ValueError("reclaim_max_price must be >= reclaim_entry")
    # Explicit 0 is immediate (persist_ready), same as sell_persist_s and
    # sell_dump_persist_s. Negatives are rejected. sell_armed_poll_s stays
    # on its own >= 0.2 floor; that floor is the loop cadence, not a persist.
    for key in (
        "reclaim_entry_persist_s",
        "reclaim_stop_persist_s",
        "reclaim_max_ttm_s",
        "reclaim_min_ttm_s",
    ):
        raw = cfg.get(key)
        if raw is not None and float(raw) < 0:
            raise ValueError(f"{key} must be >= 0")

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

def open_intent_count(
    state: dict, now: float | None = None, cfg: dict | None = None,
) -> int:
    """Count bags that still block a new mint (unsold loser).

    Post-expiry redeem holds do not block. After the loser is sold we only
    hold the winner for redeem — that must not skip the next 15m window.
    ``count_kept_loser_as_open`` (default false) keeps a bag with kept
    partial-scrap loser shares counted until the window's grace ends.
    """
    now = time.time() if now is None else float(now)
    keep_blocks = bool((cfg or {}).get("count_kept_loser_as_open", False))
    n = 0
    for intent in state.get("intents", {}).values():
        if intent.get("status") not in ACTIVE_STATUSES:
            continue
        end_ts = float(intent.get("end_ts") or 0)
        if end_ts and now > end_ts + 120:
            continue
        if intent.get("sold_loser") or intent.get("sold_leg"):
            if not (keep_blocks and kept_loser_open(intent)):
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
    keep_blocks = bool(cfg.get("count_kept_loser_as_open", False))
    full = []
    for intent in state.get("intents", {}).values():
        if intent.get("status") not in ACTIVE_STATUSES:
            continue
        end_ts = float(intent.get("end_ts") or 0)
        if end_ts and now > end_ts + 120:
            continue
        if intent.get("sold_loser") or intent.get("sold_leg"):
            if not (keep_blocks and kept_loser_open(intent)):
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
    keep_blocks = bool(cfg.get("count_kept_loser_as_open", False))
    full = []
    for intent in state.get("intents", {}).values():
        if intent.get("status") not in ACTIVE_STATUSES:
            continue
        end_ts = float(intent.get("end_ts") or 0)
        if end_ts and now > end_ts + 120:
            continue
        if intent.get("sold_loser") or intent.get("sold_leg"):
            if not (keep_blocks and kept_loser_open(intent)):
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
    """True if this condition must not be minted.

    Confirmed / completed / in-flight intents stay blocked. A ``failed``
    intent is blocked during ``mint_fail_cooldown_s``. Once
    ``mint_attempts`` reaches ``mint_max_attempts`` it stays blocked for
    the rest of that condition. Below the cap it can remint after cooldown.
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
        cooldown = float(cfg.get("mint_fail_cooldown_s") or 30.0)
    except (TypeError, ValueError):
        cooldown = 30.0
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

def submit_mint_batch(
    calls: List[ContractCall],
    metadata: str,
    *,
    rpc=None,
    gas_margin: float = 0.15,
    gas_fallback: int = 650_000,
    gas_cap: int = 650_000,
) -> Tuple[Optional[str], Optional[str], dict]:
    """Submit approve+split as one PROXY batch via Polymarket relayer.

    ``rpc`` is ``(method, params) -> result`` and is used once, here, for
    ``eth_estimateGas`` of the encoded batch. The sell loop does not call
    this function. The third return value is the gas log fields.
    """
    from buy.mint_gas import choose_mint_relay_gas

    private_key = os.getenv("PRIVATE_KEY") or ""
    funder = os.getenv("FUNDER_ADDRESS") or ""
    if not private_key or not funder:
        return None, "missing PRIVATE_KEY or FUNDER_ADDRESS", {}

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
            return None, "RELAYER_API_KEY_ADDRESS does not match PRIVATE_KEY signer", {}

        nonce_r = requests.get(
            f"{relayer_url}/relay-payload",
            params={"address": eoa, "type": "PROXY"},
            timeout=15,
        )
        if nonce_r.status_code != 200:
            return None, f"relay payload fetch fail HTTP {nonce_r.status_code}", {}
        relay_payload = nonce_r.json()
        if not isinstance(relay_payload, dict):
            return None, "invalid relay payload", {}
        nonce = relay_payload.get("nonce")
        relay = relay_payload.get("address")
        if nonce is None or not relay:
            return None, "relay payload missing nonce/address", {}

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
        gas_plan = choose_mint_relay_gas(
            rpc,
            from_address=eoa,
            to=config.proxy_factory,
            data=encoded_data,
            margin=gas_margin,
            fallback=gas_fallback,
            cap=gas_cap,
        )
        gas_log = gas_plan.as_log()
        request = build_proxy_transaction_request(
            signer=signer,
            args=ProxyTransactionArgs(
                from_address=eoa,
                nonce=str(nonce),
                gas_price="0",
                data=encoded_data,
                relay=str(relay),
                gas_limit=gas_plan.relay_arg(),
            ),
            config=config,
            metadata=metadata,
        )
        body = request.to_dict()
        if str(body.get("proxyWallet") or "").lower() != str(funder).lower():
            return None, "derived proxyWallet does not match FUNDER_ADDRESS", gas_log
        headers = get_relayer_headers(body)
        if headers is None:
            return None, "could not generate relayer authentication headers", gas_log
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
                return str(tx_id), None, gas_log
            return None, "relayer response missing transactionID", gas_log
        return None, f"HTTP {submit_r.status_code} · {submit_r.text[:120]}", gas_log
    except Exception as exc:
        return None, f"relayer request failed: {str(exc)[:200]}", {}

def get_relayer_transaction(relayer_url: str, transaction_id: str) -> Optional[dict]:
    try:
        response = thread_session("relayer").get(
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
            # Ended confirmed bags are not polled until one read after the
            # grace, which can still flip an empty bag to completed.
            # chain_reconcile_done stops further eth_calls. In-flight statuses
            # keep querying so pending cash can release. A confirmed bag whose
            # window is still open is not balanceOf'd (tracked fills size the
            # sells). skip_confirmed_inventory still skips a confirmed bag
            # when a sell is hot and a query would otherwise run.
            action = chain_reconcile_action(intent, now)
            if action == "skip":
                continue
            if action != "final" and skip_confirmed_inventory and status == "confirmed":
                continue
            up_tok = str(intent.get("up_token") or up_tok)
            dn_tok = str(intent.get("dn_token") or dn_tok)
            final_read = action == "final"
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
            if final_read:
                intent["chain_reconcile_done"] = True
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
                    # Off the sell path. First order of the bag should not
                    # pay tick-size / neg-risk / allowance lookups.
                    try:
                        _schedule_order_prewarm(
                            str(intent.get("up_token") or up_tok),
                            str(intent.get("dn_token") or dn_tok),
                        )
                    except NameError:
                        pass
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

def _book_quote(row: Any):
    """``(bid, bid_sz, bids, ask, ask_sz, asks, age_s)`` from one fetch row.

    A 3-tuple from an older stub leaves the ask unknown. ``age_s`` is None
    when the payload had no timestamp (the row is still this tick's fetch).
    """
    if not isinstance(row, (tuple, list)):
        return None, 0.0, [], None, 0.0, [], None
    bid = row[0] if len(row) > 0 else None
    bid_sz = row[1] if len(row) > 1 else 0.0
    bids = row[2] if len(row) > 2 else []
    ask = row[3] if len(row) > 3 else None
    ask_sz = row[4] if len(row) > 4 else 0.0
    asks = row[5] if len(row) > 5 else []
    age = row[6] if len(row) > 6 else None
    return bid, bid_sz, bids or [], ask, ask_sz, asks or [], age


def _book_timeout_s() -> float:
    """CLOB book HTTP timeout. Default 1.2s so a hung read is one short tick."""
    raw = getattr(_book_timeout_s, "seconds", 1.2)
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return 1.2
    if value != value or value <= 0 or value == float("inf"):
        return 1.2
    return value


def _apply_book_timeout(cfg: Optional[dict]) -> None:
    if not isinstance(cfg, dict):
        return
    try:
        _book_timeout_s.seconds = float(cfg.get("book_timeout_s") or 1.2)
    except (TypeError, ValueError):
        _book_timeout_s.seconds = 1.2


def _parse_book_payload(payload: Any, min_size: float):
    book = payload if isinstance(payload, dict) else {}
    bids = book.get("bids") or []
    asks = book.get("asks") or []
    price, size = best_bid_with_min_size(bids, min_size=min_size)
    ask_px, ask_sz = best_ask_with_min_size(asks, min_size=min_size)
    age = book_age_s(book.get("timestamp"), time.time())
    return price, size, bids, ask_px, ask_sz, asks, age


def _fetch_book(token_id: str, min_size: float):
    """REST `/book` → sized bid and ask, raw levels, and book age if stamped."""
    try:
        response = thread_session("clob_book").get(
            "https://clob.polymarket.com/book",
            params={"token_id": str(token_id)},
            timeout=_book_timeout_s(),
        )
        if response.status_code != 200:
            return None, 0.0, [], None, 0.0, [], None
        return _parse_book_payload(response.json(), min_size)
    except Exception as exc:
        log_event("book_fetch_fail", token_id=str(token_id)[:18], error=str(exc)[:160])
        return None, 0.0, [], None, 0.0, [], None


def _fetch_books_parallel(up_tok: str, dn_tok: str, min_size: float):
    fut_up = _book_pool.submit(_fetch_book, up_tok, min_size)
    fut_dn = _book_pool.submit(_fetch_book, dn_tok, min_size)
    return fut_up.result(), fut_dn.result()


def _fetch_books(up_tok: str, dn_tok: str, min_size: float):
    """Both sides in one CLOB `/books` POST.

    A timeout returns empty books (one short tick, no second fetch). Any
    other error, or a body that does not carry both tokens, falls back to
    the two parallel `/book` GETs.
    """
    timeout = _book_timeout_s()
    try:
        response = thread_session("clob_book").post(
            "https://clob.polymarket.com/books",
            json=[{"token_id": str(up_tok)}, {"token_id": str(dn_tok)}],
            timeout=timeout,
        )
        if response.status_code != 200:
            raise RuntimeError(f"books_http_{response.status_code}")
        payload = response.json()
        if not isinstance(payload, list):
            raise RuntimeError("books_not_list")
        by_id = {}
        for book in payload:
            if not isinstance(book, dict):
                continue
            asset = str(book.get("asset_id") or book.get("token_id") or "")
            if asset:
                by_id[asset] = book
        up_book = by_id.get(str(up_tok))
        dn_book = by_id.get(str(dn_tok))
        if up_book is None or dn_book is None:
            raise RuntimeError("books_missing_side")
        return (
            _parse_book_payload(up_book, min_size),
            _parse_book_payload(dn_book, min_size),
        )
    except Exception as exc:
        if isinstance(exc, (requests.Timeout, TimeoutError)):
            log_event(
                "book_fetch_fail",
                token_id="books",
                error=str(exc)[:160],
            )
            empty = (None, 0.0, [], None, 0.0, [], None)
            return empty, empty
        return _fetch_books_parallel(up_tok, dn_tok, min_size)


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
            _allowance_box()["collateral"] = True
        except Exception:
            pass
        _clob_client = client
        log_event("clob_client_ready")
        return _clob_client
    except Exception as exc:
        _clob_init_error = str(exc)[:200]
        log_event("clob_client_init_fail", error=_clob_init_error)
        return None

def _allowance_box() -> dict:
    box = getattr(_allowance_box, "data", None)
    if not isinstance(box, dict):
        box = {"tokens": set(), "collateral": False, "warmed": set()}
        _allowance_box.data = box
    return box


def _bind_sell_chain(chain: Any, ctf: str, funder: Optional[str]) -> None:
    """Chain reader for the balance-reject fallback. Not used before a post."""
    _bind_sell_chain.slot = (chain, str(ctf or ""), funder)
    _bind_sell_chain.last_balance = None


def _read_sell_balance(token_id: str) -> Optional[float]:
    slot = getattr(_bind_sell_chain, "slot", None)
    if not slot or not token_id:
        return None
    chain, ctf, funder = slot
    if not ctf or not funder:
        return None
    try:
        with _io_unlocked():
            bal = chain.position_balance(ctf, funder, token_id)
        value = float(bal)
    except Exception as exc:
        log_event("sell_balance_fail", error=str(exc)[:160])
        return None
    if value != value:
        return None
    _bind_sell_chain.last_balance = value
    return value


def _refresh_conditional_allowance(token_id: str, *, force: bool) -> None:
    box = _allowance_box()
    key = str(token_id or "")
    if not key:
        return
    if not force and key in box["tokens"]:
        return
    try:
        client = _get_clob_client()
    except Exception:
        return
    if client is None:
        return
    try:
        from py_clob_client_v2 import AssetType, BalanceAllowanceParams

        client.update_balance_allowance(
            BalanceAllowanceParams(asset_type=AssetType.CONDITIONAL, token_id=key)
        )
        box["tokens"].add(key)
    except Exception as exc:
        log_event("sell_allowance_warn", error=str(exc)[:160])


def _refresh_collateral_allowance(*, force: bool) -> None:
    box = _allowance_box()
    if not force and box.get("collateral"):
        return
    try:
        client = _get_clob_client()
    except Exception:
        return
    if client is None:
        return
    try:
        from py_clob_client_v2 import AssetType, BalanceAllowanceParams

        client.update_balance_allowance(
            BalanceAllowanceParams(asset_type=AssetType.COLLATERAL)
        )
        box["collateral"] = True
    except Exception as exc:
        log_event("reclaim_allowance_warn", error=str(exc)[:160])


def _schedule_order_prewarm(up_token: str, dn_token: str) -> None:
    """Tick size, neg-risk, and one allowance refresh per token. Not on the sell tick."""
    tokens = [str(token) for token in (up_token, dn_token) if token]
    if not tokens:
        return

    def _run() -> None:
        try:
            client = _get_clob_client()
        except Exception:
            return
        if client is None:
            return
        box = _allowance_box()
        warmed = box.setdefault("warmed", set())
        for token_id in tokens:
            if token_id in warmed:
                continue
            try:
                client.get_tick_size(token_id)
            except Exception:
                pass
            try:
                getter = getattr(client, "get_neg_risk", None)
                if getter is not None:
                    getter(token_id)
            except Exception:
                pass
            _refresh_conditional_allowance(token_id, force=False)
            warmed.add(token_id)
        _refresh_collateral_allowance(force=False)

    threading.Thread(target=_run, name="mintbot-prewarm", daemon=True).start()


def _resize_after_balance_reject(
    token_id: str,
    size: float,
    *,
    keep: float,
    tol: float,
) -> Tuple[float, str]:
    """One allowance refresh and one chain read after a balance reject.

    Returns ``(retry_size, latch)``. ``already_flat`` means do not post again.
    A missing chain read retries the same size (the refresh may be enough).
    """
    _refresh_conditional_allowance(token_id, force=True)
    bal = _read_sell_balance(token_id)
    if bal is None:
        return float(size), "unknown"
    try:
        keep_f = float(keep or 0.0)
    except (TypeError, ValueError):
        keep_f = 0.0
    if keep_f != keep_f or keep_f < 0:
        keep_f = 0.0
    sellable = float(bal) - keep_f if keep_f > 1e-12 else float(bal)
    if sellable < 0:
        sellable = 0.0
    retry = min(float(size), sellable)
    if retry < float(tol):
        return 0.0, "already_flat"
    return retry, "has_inventory"


def _sell_fak_with_fallback(
    token_id: str,
    size: float,
    price: float,
    dry_run: bool,
    capture: Optional[list],
    *,
    keep: float,
    tol: float,
) -> Tuple[float, str]:
    """One FAK. On a balance/allowance reject, refresh, resize, retry once."""
    sold, status = _fak_sell(token_id, size, price, dry_run, capture=capture)
    if dry_run or float(sold or 0) >= float(tol):
        return sold, status
    if not is_balance_allowance_reject(status):
        return sold, status
    retry, latch = _resize_after_balance_reject(token_id, size, keep=keep, tol=tol)
    if latch == "already_flat" or retry < float(tol):
        return 0.0, "already_flat"
    return _fak_sell(token_id, retry, price, dry_run, capture=capture)


def _fak_sell(
    token_id: str,
    size: float,
    price: float,
    dry_run: bool,
    capture: Optional[list] = None,
):
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
    triggered = time.perf_counter()
    try:
        from py_clob_client_v2 import MarketOrderArgs, OrderType
        from py_clob_client_v2.order_builder.constants import SELL

        signed = client.create_market_order(
            MarketOrderArgs(
                token_id=str(token_id),
                amount=size,
                side=SELL,
                price=price,
            )
        )
        send_at = time.perf_counter()
        result = client.post_order(signed, order_type=OrderType.FAK)
        responded = time.perf_counter()
        sold = 0.0
        status = "posted"
        if isinstance(result, dict):
            status = str(result.get("status") or "posted")
            err = result.get("error") or result.get("errorMsg")
            if err and is_balance_allowance_reject(err):
                status = f"error:{err}"
            sold = parse_sell_fill_shares(result, size)
            if capture is not None and not is_balance_allowance_reject(status):
                capture.append(result)
        latency = {
            "trigger_to_send_ms": round((send_at - triggered) * 1000.0, 3),
            "send_to_response_ms": round((responded - send_at) * 1000.0, 3),
        }
        log_event(
            "sell_fak_result",
            token_id=str(token_id),
            size=size,
            price=price,
            sold=sold,
            status=status,
            raw=str(result)[:240] if result is not None else None,
            **latency,
        )
        return sold, status
    except Exception as exc:
        responded = time.perf_counter()
        log_event(
            "sell_fak_fail",
            token_id=str(token_id)[:18],
            error=str(exc)[:200],
            trigger_to_send_ms=round((responded - triggered) * 1000.0, 3),
            send_to_response_ms=None,
        )
        return 0.0, f"error:{str(exc)[:80]}"


def _fak_buy(
    token_id: str,
    shares: float,
    price: float,
    dry_run: bool,
    capture: Optional[list] = None,
):
    """One FAK buy. ``shares`` sizes the clip; the post is a market BUY in USDC.

    The dollar amount is ``reclaim_buy_usdc`` (whole cents) and ``price`` is
    the max. Signing uses the market-order builder, the same shape as a
    sell FAK, so the maker amount has at most two USDC decimals. Tick size,
    exchange version, and neg-risk are the lookups ``create_order`` already
    does. ``create_market_order``'s market-info prefetch is not called.
    Dry-run returns before any client call. No sleep.
    """
    shares = float(shares)
    price = float(price)
    usdc = reclaim_buy_usdc(shares, price)
    if shares < 0.01 or usdc < 0.01 or not (0 < price < 1):
        return 0.0, "bad_args"
    if dry_run:
        log_event(
            "dry_buy", token_id=str(token_id), size=shares, price=price, usdc=usdc,
        )
        return 0.0, "dry"
    client = _get_clob_client()
    if client is None:
        return 0.0, f"no_clob:{_clob_init_error or 'unknown'}"
    try:
        from py_clob_client_v2 import MarketOrderArgs, OrderType
        from py_clob_client_v2.clob_types import CreateOrderOptions
        from py_clob_client_v2.constants import BYTES32_ZERO
        from py_clob_client_v2.order_builder.constants import BUY
        from py_clob_client_v2.utilities import price_valid
        from buy.sister_bid import buy_matched_shares

        order_args = MarketOrderArgs(
            token_id=str(token_id),
            amount=usdc,
            side=BUY,
            price=price,
            order_type=OrderType.FAK,
        )
        tick_size = client.get_tick_size(str(token_id))
        if not price_valid(price, tick_size):
            return 0.0, "bad_args"
        resolve_version = getattr(client, "_ClobClient__resolve_version", None)
        version = int(resolve_version() if resolve_version is not None else 2)
        neg_risk = False if version == 3 else bool(client.get_neg_risk(str(token_id)))
        builder_config = getattr(client, "builder_config", None)
        if builder_config and getattr(builder_config, "builder_code", None):
            code = builder_config.builder_code
            current = getattr(order_args, "builder_code", None)
            if not current or current == BYTES32_ZERO:
                order_args.builder_code = code
        refreshed = False
        while True:
            signed = client.builder.build_market_order(
                order_args,
                CreateOrderOptions(tick_size=tick_size, neg_risk=neg_risk),
                version=version,
            )
            try:
                result = client.post_order(signed, order_type=OrderType.FAK)
            except Exception as exc:
                status = reclaim_fak_status(exc)
                if not refreshed and is_balance_allowance_reject(status):
                    refreshed = True
                    _refresh_collateral_allowance(force=True)
                    continue
                log_event(
                    "reclaim_fak_fail",
                    token_id=str(token_id)[:18],
                    error=str(exc)[:200],
                )
                return 0.0, status
            bought = 0.0
            status = "posted"
            if isinstance(result, dict):
                status = str(result.get("status") or "posted")
                err = result.get("error") or result.get("errorMsg")
                if err and is_balance_allowance_reject(err):
                    status = f"error:{err}"
                bought = buy_matched_shares(result, shares)
                if capture is not None and not is_balance_allowance_reject(status):
                    capture.append(result)
            if not refreshed and is_balance_allowance_reject(status):
                refreshed = True
                _refresh_collateral_allowance(force=True)
                continue
            log_event(
                "reclaim_fak_result",
                token_id=str(token_id),
                size=shares,
                price=price,
                usdc=usdc,
                bought=bought,
                status=status,
                raw=str(result)[:240] if result is not None else None,
            )
            return bought, status
    except Exception as exc:
        log_event("reclaim_fak_fail", token_id=str(token_id)[:18], error=str(exc)[:200])
        return 0.0, reclaim_fak_status(exc)


def _sell_inventory(
    chain: ChainReader,
    ctf: str,
    funder_cs: Optional[str],
    token_id: str,
    shares: float,
    tol: float,
    seen_key: str,
    intent: dict,
    *,
    read_chain: bool = False,
) -> Tuple[float, str]:
    """Tracked size (minted minus recorded fills). No chain read on the sell path.

    ``read_chain`` is the uncertain-buy recovery only. A sell that the
    exchange rejects for balance reads the chain once in
    ``_resize_after_balance_reject``, not here.
    """
    if not read_chain:
        return tracked_sell_size(intent, shares, seen_key=seen_key, tol=tol)
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
    fills: Optional[list] = None,
    inventory_keep: float = 0.0,
) -> Tuple[float, str, Optional[float]]:
    """FAK each limit in turn. ``fills`` collects ``(shares, avg_px)`` per post.

    No sleep between rungs or after the last miss. The caller's refire
    fetches a fresh book and posts immediately. A balance/allowance reject
    refreshes allowance, reads the chain once, and retries that rung once.
    """
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
        captured: list = []
        sold, last_status = _sell_fak_with_fallback(
            token_id,
            remaining,
            use_px,
            dry_run,
            captured,
            keep=inventory_keep,
            tol=tol,
        )
        sold_total += float(sold or 0)
        last_px = float(use_px)
        if fills is not None and float(sold or 0) > 0:
            fills.append(
                (float(sold), sell_fill_vwap(captured[0] if captured else None, sold))
            )
        if dry_run or sold_total >= size - tol or last_status == "already_flat":
            break
    return sold_total, last_status, last_px


def _fire_loser_scrap(
    *,
    token_id: str,
    size: float,
    floor: float,
    threshold: float,
    loser_bid: float,
    fak_px: float,
    depth_at_limit: Optional[float],
    sweep: bool,
    dry_run: bool,
    tol: float,
    bids: Any,
    slug: Any,
    leg: Any,
    ttm_s: Optional[float],
    condition_id: Any,
    chain: ChainReader,
    ctf: str,
    funder_cs: Optional[str],
    intent: dict,
    shares: float,
    fills: Optional[list] = None,
    log_extra: Optional[dict] = None,
    min_px: float = 0.01,
) -> Tuple[float, str, Optional[float], bool]:
    """One live-bid sweep FAK, or the clipped cent ladder when sweep is off.

    The sweep limit is the live loser bid (capped at ``threshold``, floored
    at ``min_px``), never clamped up to ``sell_floor``.

    Returns sold shares, status, last limit, and whether the refreshed
    balance is already flat. A partial sweep does not post another rung.
    ``fills`` collects ``(shares, avg_px)``; the limit is not the fill price.
    """
    plan = loser_scrap_post(
        sweep=sweep,
        remaining=size,
        floor=floor,
        threshold=threshold,
        loser_bid=loser_bid,
        fak_px=fak_px,
        depth_at_limit=depth_at_limit,
        min_px=min_px,
    )
    if plan["mode"] == "sweep":
        limit = float(plan["limits"][0]) if plan["limits"] else round(float(min_px), 4)
        post_size = float(plan["size"])
        _log_sell_book_depth(
            slug=slug,
            leg=leg,
            limit=limit,
            our_size=post_size,
            bids=bids or [],
            ttm_s=ttm_s,
            path="loser",
            phase="fak",
            condition_id=condition_id,
        )
        console.print(
            f"  [bold bright_yellow][SELL {str(leg).upper()}][/] {slug}  "
            f"bid={loser_bid:.3f}  limit>={limit:.3f}  size={post_size:.2f}"
        )
        captured: list = []
        try:
            scrap_keep = float(intent.get("sell_scrap_keep") or 0.0)
        except (TypeError, ValueError):
            scrap_keep = 0.0
        sold, status = _sell_fak_with_fallback(
            token_id,
            post_size,
            limit,
            dry_run,
            captured,
            keep=scrap_keep,
            tol=tol,
        )
        raw = captured[0] if captured else None
        avg = sell_fill_vwap(raw, sold)
        if fills is not None and float(sold or 0) > 0:
            fills.append((float(sold), avg))
        log_event(
            "sell_scrap_sweep",
            condition_id=condition_id,
            slug=slug,
            leg=leg,
            limit=round(limit, 4),
            size=round(float(sold or 0), 4),
            avg_px=avg,
            offered=round(post_size, 4),
            status=status,
            threshold=round(float(threshold), 4),
            **(log_extra or {}),
        )
        try:
            if float(sold or 0) > 0 or dry_run:
                _remember_scrap_fill(
                    condition_id,
                    avg_px=avg if avg is not None else limit,
                    best_bid_size=_scrap_best_bid_size(bids),
                )
        except Exception:
            pass
        _refreshed, latch = _sell_inventory(
            chain, ctf, funder_cs, token_id, shares, tol,
            "seen_loser_inventory", intent,
        )
        flat = latch == "already_flat" or status == "already_flat"
        return float(sold or 0), status, limit, flat
    log_event(
        "sell_scrap_ladder",
        condition_id=condition_id,
        slug=slug,
        leg=leg,
        threshold=round(float(threshold), 4),
        fak_px=round(float(fak_px), 4),
        **(log_extra or {}),
    )
    ladder_fills: list = []
    sold, status, last_px = _run_fak_ladder(
        token_id,
        float(plan["size"]),
        plan["limits"],
        dry_run=dry_run,
        bid=loser_bid,
        label=str(leg),
        slug=slug,
        tol=tol,
        depth_bids=bids,
        depth_path="loser",
        depth_leg=leg,
        ttm_s=ttm_s,
        condition_id=condition_id,
        fills=ladder_fills,
        inventory_keep=float(intent.get("sell_scrap_keep") or 0.0),
    )
    if fills is not None:
        fills.extend(ladder_fills)
    try:
        if float(sold or 0) > 0 or dry_run:
            ladder_avg = record_fill_px({}, "px", ladder_fills)
            _remember_scrap_fill(
                condition_id,
                avg_px=ladder_avg if ladder_avg is not None else last_px,
                best_bid_size=_scrap_best_bid_size(bids),
            )
    except Exception:
        pass
    return float(sold or 0), status, last_px, False


@contextmanager
def _mark_sell_exit(intent: dict):
    """Keep the next sequential mint waiting while this dump or stop posts.

    Nested calls (kept half inside the held dump) leave the flag set until
    the outermost sell returns. The sell thread drops ``STATE_LOCK`` inside
    the FAK, so the flag has to be on before that release.
    """
    already = bool(intent.get("sell_exit_inflight"))
    intent["sell_exit_inflight"] = True
    try:
        yield
    finally:
        if not already:
            intent["sell_exit_inflight"] = False


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
    fills: Optional[list] = None,
) -> Tuple[float, str, Optional[float], int, float]:
    """Held-dump path: first live-bid FAK, then one fresh-bid FAK per retry.

    Each retry refetches that leg and posts a single FAK at the new best
    bid. It does not walk a ladder off the first snapshot. A partial fill
    refires at once, inside ``retries``. A zero fill still needs
    ``dump_fast_retry_eligible`` (empty book or kill/cancel). ``fills``
    collects ``(shares, avg_px)`` across every post.
    """
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
            fills=fills,
        )
    attempts += 1
    sold_total += float(sold or 0.0)

    def _keep_refiring(posted: float, status: Any) -> bool:
        # A partial (anything at or above tolerance) still has shares out.
        # Refire those now. A zero fill keeps the old empty/kill rule.
        try:
            got = float(posted or 0.0)
        except (TypeError, ValueError):
            got = 0.0
        if got + 1e-12 >= float(tol):
            return True
        return bool(dump_fast_retry_eligible(sold=got, status=status, tol=tol))

    if dry_run or sold_total >= size - tol:
        return sold_total, last_status, last_px, attempts, live_bid
    if not _keep_refiring(sold, last_status):
        return sold_total, last_status, last_px, attempts, live_bid

    for retry_idx in range(max(0, int(retries or 0))):
        with _io_unlocked():
            # Tolerate a 3-tuple stub. Ask fields are not used on this retry.
            row = _fetch_book(token_id, min_bid_size)
            if isinstance(row, (tuple, list)):
                retry_bid = row[0] if len(row) > 0 else None
                _retry_sz = row[1] if len(row) > 1 else 0.0
                retry_bids = row[2] if len(row) > 2 else []
            else:
                retry_bid, _retry_sz, retry_bids = None, 0.0, []
        if retry_bid is None:
            fresh_bid = None
        else:
            try:
                fresh_bid = float(retry_bid)
            except (TypeError, ValueError):
                fresh_bid = 0.0
        if fresh_bid is None or fresh_bid != fresh_bid or fresh_bid <= 0:
            log_event(
                "sell_dump_fast_refire_stop",
                condition_id=condition_id,
                slug=slug,
                leg=held,
                retry=retry_idx + 1,
                attempts=attempts,
                reason="empty_book",
                bid=fresh_bid,
            )
            break
        # One FAK at the bid just fetched. The next retry fetches again.
        live_bid = fresh_bid
        limits = [round(live_bid, 4)]
        log_event(
            "sell_dump_fast_refire_attempt",
            condition_id=condition_id,
            slug=slug,
            leg=held,
            retry=retry_idx + 1,
            attempts=attempts + 1,
            bid=live_bid,
            limits=limits,
            sold=round(sold_total, 4),
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
                fills=fills,
            )
        attempts += 1
        sold_total += float(sold or 0.0)
        if dry_run or sold_total >= size - tol:
            break
        if not _keep_refiring(sold, last_status):
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


def _sell_kept_after_dump(
    *,
    cfg: dict,
    intent: dict,
    cid: str,
    kept: str,
    tokens: dict,
    bids: dict,
    books: dict,
    ttm_s: Optional[float],
    min_bid_size: float,
    retries: int,
    ladder_step: float,
    ladder_rungs: int,
    dry_run: bool,
    tol: float,
    chain: ChainReader,
    ctf: str,
    funder_cs: Optional[str],
) -> None:
    """``sell_dump_also_kept``: exit the kept scrap half in the dump event.

    Same live-bid FAK + fast refire as the held dump, then one FAK at the
    1c floor for any remainder. Runs once per bag (``sell_dump_kept_done``).
    """
    already_exit = bool(intent.get("sell_exit_inflight"))
    intent["sell_exit_inflight"] = True
    try:
        intent["sell_dump_kept_done"] = True
        slug = intent.get("slug")
        floor = max(0.01, float(cfg.get("sell_clob_min_price") or 0.01))
        try:
            keep = float(intent.get("sell_scrap_keep") or 0.0)
        except (TypeError, ValueError):
            keep = 0.0
        if not math.isfinite(keep) or keep < 0:
            keep = 0.0
        token = tokens.get(kept)
        size = 0.0
        latch = "no_keep"
        if keep >= tol and token:
            size, latch = _sell_inventory(
                chain, ctf, funder_cs, token, keep, tol,
                "seen_kept_inventory", intent,
            )
            if latch in {"already_flat", "await_inventory"}:
                size = 0.0
        planned = round(float(size), 4)
        intent["sell_dump_kept_planned"] = planned
        if size < tol:
            intent["sell_dump_kept_outcome"] = "nothing_kept"
            log_event(
                "sell_dump_kept",
                condition_id=cid,
                slug=slug,
                leg=kept,
                planned=planned,
                keep=keep,
                sold=0.0,
                avg_px=None,
                status=latch,
                outcome="nothing_kept",
                remaining=0.0,
            )
            return

        kept_fills: list = []
        with _mark_sell_exit(intent):
            sold_total, last_status, last_px, attempts, live_bid = _run_dump_fak_with_refire(
                token_id=token,
                size=size,
                initial_bid=float(bids.get(kept) or 0.0),
                initial_bids=books.get(kept),
                held=kept,
                slug=slug,
                condition_id=cid,
                ttm_s=ttm_s,
                floor=floor,
                min_bid_size=min_bid_size,
                retries=retries,
                ladder_step=ladder_step,
                ladder_rungs=ladder_rungs,
                dry_run=dry_run,
                tol=tol,
                fills=kept_fills,
            )
            sold_total = float(sold_total or 0.0)
            swept = 0.0
            if (
                not dry_run
                and size - sold_total >= tol
                and last_status != "already_flat"
            ):
                with _io_unlocked():
                    swept, sweep_status, sweep_px = _run_fak_ladder(
                        token,
                        size - sold_total,
                        [round(floor, 4)],
                        dry_run=dry_run,
                        bid=live_bid,
                        label=f"dump kept {kept}",
                        slug=slug,
                        tol=tol,
                        depth_bids=books.get(kept),
                        depth_path="dump_kept",
                        depth_leg=kept,
                        ttm_s=ttm_s,
                        condition_id=cid,
                        fills=kept_fills,
                    )
                attempts += 1
                sold_total += float(swept or 0.0)
                last_status, last_px = sweep_status, sweep_px
        remaining = max(0.0, size - sold_total)
        if dry_run:
            outcome = "dry_run"
        elif last_status == "already_flat" and sold_total < tol:
            outcome = "nothing_kept"
        elif remaining < tol:
            outcome = "filled"
        elif sold_total >= tol:
            outcome = "partial"
        else:
            outcome = "no_fill"
        avg_px = record_fill_px(intent, "sell_dump_kept_fill_px", kept_fills)
        intent["sell_dump_kept_leg"] = kept
        intent["sell_dump_kept_filled"] = round(sold_total, 6)
        intent["sell_dump_kept_limit"] = last_px
        intent["sell_dump_kept_attempts"] = int(attempts)
        intent["sell_dump_kept_outcome"] = outcome
        if outcome in {"filled", "dry_run"}:
            intent["sell_dump_kept_sold"] = True
        log_event(
            "sell_dump_kept",
            condition_id=cid,
            slug=slug,
            leg=kept,
            planned=planned,
            keep=keep,
            sold=round(sold_total, 4),
            swept=round(float(swept or 0.0), 4),
            fills=len(kept_fills),
            avg_px=avg_px,
            bid=live_bid,
            limit=last_px,
            attempts=int(attempts),
            status=last_status,
            outcome=outcome,
            remaining=round(remaining, 4),
        )
        console.print(
            f"  [bold bright_yellow][DUMP KEPT {outcome.upper()}][/] {kept} "
            f"{sold_total:.2f}/{size:.2f}  avg={avg_px}"
        )
    finally:
        if not already_exit:
            intent["sell_exit_inflight"] = False


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
        from py_clob_client_v2 import OrderArgs, OrderType
        from py_clob_client_v2.order_builder.constants import SELL

        order_type = OrderType.GTD if str(tif) == "GTD" else OrderType.GTC
        refreshed = False
        while True:
            triggered = time.perf_counter()
            signed = client.create_order(
                OrderArgs(
                    token_id=str(token_id),
                    price=price,
                    size=size,
                    side=SELL,
                    expiration=int(expiration or 0),
                )
            )
            try:
                send_at = time.perf_counter()
                result = client.post_order(signed, order_type=order_type)
                responded = time.perf_counter()
            except Exception as exc:
                status = f"error:{str(exc)[:80]}"
                if not refreshed and is_balance_allowance_reject(status):
                    refreshed = True
                    retry, latch = _resize_after_balance_reject(
                        token_id, size, keep=0.0, tol=0.01,
                    )
                    if latch == "already_flat" or retry < 0.01:
                        return "", "already_flat"
                    size = retry
                    continue
                log_event("sell_scrap_rest_fail", error=str(exc)[:200])
                return "", status
            status = "posted"
            if isinstance(result, dict):
                status = str(result.get("status") or "posted")
                err = result.get("error") or result.get("errorMsg")
                if err and is_balance_allowance_reject(err):
                    status = f"error:{err}"
            if not refreshed and is_balance_allowance_reject(status):
                refreshed = True
                retry, latch = _resize_after_balance_reject(
                    token_id, size, keep=0.0, tol=0.01,
                )
                if latch == "already_flat" or retry < 0.01:
                    return "", "already_flat"
                size = retry
                continue
            oid = posted_order_id(result) or ""
            log_event(
                "sell_scrap_rest_result",
                token_id=str(token_id),
                size=size,
                price=price,
                tif=tif,
                order_id=oid,
                status=status,
                trigger_to_send_ms=round((send_at - triggered) * 1000.0, 3),
                send_to_response_ms=round((responded - send_at) * 1000.0, 3),
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
                # The order poll reports matched size, not price; the rest's
                # own limit is the best available (lower-bound) fill price.
                record_fill_px(
                    intent, "sell_fill_px", [(delta, intent.get("sell_scrap_rest_px"))],
                )
    stored_target = intent.get("sell_scrap_target")
    if stored_target is not None:
        try:
            goal = float(stored_target)
        except (TypeError, ValueError):
            goal = float(shares or 0)
        goal_met = float(intent.get("sell_filled") or 0) + 1e-12 >= goal - float(tol)
    else:
        goal_met = shares > 0 and float(intent.get("sell_filled") or 0) >= shares - tol
    if status == "filled" or goal_met:
        leg = intent.get("sell_loser_leg")
        _finish_scrap(
            intent,
            cid,
            leg if leg in ("up", "dn") else None,
            outcome="rest_filled" if status == "filled" else "target_filled",
        )
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


def _note_loser_sold(intent: dict, leg: Optional[str] = None, *, note: str = "") -> None:
    """Mark the scrap filled and start the sister-hedge clock once."""
    intent["sold_loser"] = True
    if leg in ("up", "dn"):
        intent["sold_leg"] = leg
    intent["sell_oracle_edge_armed_at"] = None
    if note:
        intent["sell_note"] = note
    if not intent.get("sold_loser_at"):
        intent["sold_loser_at"] = time.time()


def _mark_loser_sold(intent: dict, leg: str, *, note: str = "") -> None:
    _note_loser_sold(intent, leg, note=note)


def _uses_scrap_plan(intent: dict, fraction: float) -> bool:
    """True when this bag scraps a locked target instead of the full balance."""
    if intent.get("sell_scrap_target") is not None:
        return True
    return float(fraction) < 1.0 - 1e-12


def _lock_scrap_plan(
    intent: dict,
    *,
    held: float,
    fraction: float,
    cid: str,
    leg: Optional[str],
    threshold: Optional[float] = None,
) -> Tuple[float, float]:
    """Fix ``(target, keep)`` on the first scrap post. Later fires reuse it."""
    existing = intent.get("sell_scrap_target")
    if existing is not None:
        try:
            return float(existing), float(intent.get("sell_scrap_keep") or 0.0)
        except (TypeError, ValueError):
            pass
    target, keep = scrap_share_plan(held, fraction)
    intent["sell_scrap_target"] = float(target)
    intent["sell_scrap_keep"] = float(keep)
    intent["sell_scrap_held"] = float(held)
    intent["sell_scrap_fraction"] = float(fraction)
    log_event(
        "sell_scrap_plan",
        condition_id=cid,
        slug=intent.get("slug"),
        leg=leg,
        held=round(float(held), 6),
        fraction=float(fraction),
        target=float(target),
        keep=round(float(keep), 6),
        threshold=None if threshold is None else round(float(threshold), 4),
    )
    return float(target), float(keep)


def _scrap_post_shares(
    intent: dict,
    inventory: float,
    fraction: float,
    cid: str,
    leg: Optional[str],
    threshold: Optional[float] = None,
) -> float:
    """Shares to offer. Fraction 1 with no plan returns ``inventory``."""
    if not _uses_scrap_plan(intent, fraction):
        return float(inventory)
    _lock_scrap_plan(
        intent,
        held=float(inventory),
        fraction=fraction,
        cid=cid,
        leg=leg,
        threshold=threshold,
    )
    return scrap_order_shares(
        target=float(intent.get("sell_scrap_target") or 0.0),
        keep=float(intent.get("sell_scrap_keep") or 0.0),
        filled=float(intent.get("sell_filled") or 0.0),
        inventory=float(inventory),
    )


def _log_scrap_outcome(
    intent: dict,
    cid: str,
    outcome: str,
    leg: Optional[str] = None,
) -> None:
    """One audit line per bag once a partial scrap plan exists."""
    if intent.get("sell_scrap_outcome"):
        return
    if intent.get("sell_scrap_target") is None:
        return
    intent["sell_scrap_outcome"] = str(outcome or "")
    log_event(
        "sell_scrap_outcome",
        condition_id=cid,
        slug=intent.get("slug"),
        leg=leg or intent.get("sold_leg") or intent.get("sell_loser_leg"),
        held=intent.get("sell_scrap_held"),
        fraction=intent.get("sell_scrap_fraction"),
        target=intent.get("sell_scrap_target"),
        keep=intent.get("sell_scrap_keep"),
        filled=float(intent.get("sell_filled") or 0.0),
        outcome=intent["sell_scrap_outcome"],
    )


def _finish_scrap(
    intent: dict,
    cid: str,
    leg: Optional[str],
    *,
    note: str = "",
    outcome: str = "sold",
) -> None:
    _note_loser_sold(intent, leg if leg in ("up", "dn") else None, note=note)
    _log_scrap_outcome(intent, cid, outcome, leg=leg)
    _whatsapp("scrap_filled", intent, cid, now=time.time(), leg=leg)


def _log_sell_dump_time_gated(
    *,
    condition_id: str,
    slug: Any,
    leg: Any,
    bid: Any,
    ttm: Optional[float],
    cutoff: float,
    now_s: float,
    interval_s: float = 15.0,
) -> bool:
    """Log ``sell_dump_time_gated`` at most once per bag per ``interval_s``.

    Returns True when an event was emitted. The stamp is in-process only.
    """
    stamps = getattr(_log_sell_dump_time_gated, "_last_at", None)
    if not isinstance(stamps, dict):
        stamps = {}
        setattr(_log_sell_dump_time_gated, "_last_at", stamps)
    key = str(condition_id)
    prev = stamps.get(key)
    if prev is not None and float(now_s) + 1e-12 < float(prev) + float(interval_s):
        return False
    stamps[key] = float(now_s)
    log_event(
        "sell_dump_time_gated",
        condition_id=condition_id,
        slug=slug,
        leg=leg,
        bid=bid,
        ttm=None if ttm is None else round(float(ttm), 3),
        cutoff=float(cutoff),
    )
    return True


def _log_sell_scrap_time_gated(
    *,
    condition_id: str,
    slug: Any,
    leg: Any,
    bid: Any,
    ttm: Optional[float],
    cutoff: float,
    now_s: float,
    interval_s: float = 15.0,
) -> bool:
    """Log ``sell_scrap_time_gated`` at most once per leg per ``interval_s``.

    Returns True when an event was emitted. The stamp is in-process only.
    """
    stamps = getattr(_log_sell_scrap_time_gated, "_last_at", None)
    if not isinstance(stamps, dict):
        stamps = {}
        setattr(_log_sell_scrap_time_gated, "_last_at", stamps)
    key = f"{condition_id}:{leg}"
    prev = stamps.get(key)
    if prev is not None and float(now_s) + 1e-12 < float(prev) + float(interval_s):
        return False
    stamps[key] = float(now_s)
    log_event(
        "sell_scrap_time_gated",
        condition_id=condition_id,
        slug=slug,
        leg=leg,
        bid=bid,
        ttm=None if ttm is None else round(float(ttm), 3),
        cutoff=float(cutoff),
    )
    return True


def _scrap_oracle_log_due(kind: str, condition_id: str, now_s: float, interval_s: float) -> bool:
    stamps = getattr(_scrap_oracle_log_due, "_last_at", None)
    if not isinstance(stamps, dict):
        stamps = {}
        setattr(_scrap_oracle_log_due, "_last_at", stamps)
    key = f"{kind}:{condition_id}"
    prev = stamps.get(key)
    if prev is not None and float(now_s) + 1e-12 < float(prev) + float(interval_s):
        return False
    stamps[key] = float(now_s)
    return True


def _scrap_oracle_gate(
    cfg: dict,
    condition_id: str,
    leg: Any,
    *,
    now_s: float,
    slug: Any,
    bid: Any,
    ttm: Optional[float],
    phase: str,
    log_interval_s: float = 5.0,
) -> Tuple[bool, str, dict]:
    """The live scrap oracle veto is disabled to avoid a costly stale tape read."""
    return False, "disabled", {}


def _scrap_oracle_fields(detail: Optional[dict]) -> dict:
    """Margin fields for scrap fill events (empty when the veto did not run)."""
    if not detail:
        return {}
    return {
        "oracle_margin": detail.get("margin"),
        "oracle_twap": detail.get("twap"),
        "oracle_live_price": detail.get("live_price"),
        "oracle_live_margin": detail.get("live_margin"),
        "oracle_strike": detail.get("strike"),
        "oracle_why": detail.get("why"),
        "oracle_basis": detail.get("basis"),
        "oracle_age_s": detail.get("age_s"),
        "oracle_live_age_s": detail.get("live_age_s"),
    }


def _bag_risk_rows() -> dict:
    rows = getattr(_bag_risk_rows, "_rows", None)
    if not isinstance(rows, dict):
        rows = {}
        setattr(_bag_risk_rows, "_rows", rows)
    return rows


def _bag_risk_emitted() -> set:
    done = getattr(_bag_risk_emitted, "_done", None)
    if not isinstance(done, set):
        done = set()
        setattr(_bag_risk_emitted, "_done", done)
    return done


def _remember_scrap_fill(
    condition_id: Any,
    *,
    avg_px: Any,
    best_bid_size: Any,
) -> None:
    """Stash this tick's scrap fill for the log-only bag_risk line."""
    try:
        slot = getattr(_remember_scrap_fill, "_fills", None)
        if not isinstance(slot, dict):
            slot = {}
            setattr(_remember_scrap_fill, "_fills", slot)
        slot[str(condition_id)] = {
            "avg_px": avg_px,
            "best_bid_size": best_bid_size,
        }
    except Exception:
        return


def _take_scrap_fill(condition_id: Any) -> dict:
    try:
        slot = getattr(_remember_scrap_fill, "_fills", None)
        if not isinstance(slot, dict):
            return {}
        got = slot.pop(str(condition_id), None)
        return got if isinstance(got, dict) else {}
    except Exception:
        return {}


def _scrap_best_bid_size(bids: Any) -> Optional[float]:
    try:
        snap = bid_fill_depth(bids or [], None)
    except Exception:
        return None
    if snap.get("best_bid") is None:
        return None
    try:
        size = float(snap.get("best_bid_size") or 0.0)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(size):
        return None
    return size


def _ttm_at(end_ts: Any, at_ts: Any) -> Optional[float]:
    try:
        end = float(end_ts or 0)
        at = float(at_ts)
    except (TypeError, ValueError):
        return None
    if end <= 0 or not math.isfinite(end) or not math.isfinite(at):
        return None
    return end - at


def _bag_risk_ensure(condition_id: str, *, sold_at_start: bool) -> None:
    """Open an in-memory risk row the first tick this process sees the bag."""
    try:
        rows = _bag_risk_rows()
        if condition_id in rows:
            return
        rec = fresh_bag_risk()
        if sold_at_start:
            rec["partial"] = True
        rows[condition_id] = rec
    except Exception:
        return


def _note_bag_risk(
    condition_id: str,
    intent: dict,
    *,
    now: float,
    end_ts: float,
    ttm_s: Optional[float],
    bids: dict,
    books: dict,
) -> None:
    """Update bag_risk from bids this tick already fetched. Never trades."""
    try:
        rows = _bag_risk_rows()
        rec = rows.get(condition_id)
        if not isinstance(rec, dict):
            return
        sold_leg = intent.get("sold_leg")
        sold = bool(intent.get("sold_loser") or sold_leg)
        held = None
        if sold_leg == "up":
            held = "dn"
        elif sold_leg == "dn":
            held = "up"
        held_bid = bids.get(held) if held else None
        scrap_px = None
        scrap_ttm = None
        loser_size = None
        if sold and not rec.get("scrap_seen"):
            if rec.get("partial"):
                scrap_px = recorded_fill_px(intent, "sell_fill_px", "sell_limit")
                scrap_ttm = _ttm_at(end_ts, intent.get("sold_loser_at"))
            else:
                fill = _take_scrap_fill(condition_id)
                # Whole-bag average first; the stash only holds the last tick.
                scrap_px = intent.get("sell_fill_px")
                if scrap_px is None:
                    scrap_px = fill.get("avg_px")
                if scrap_px is None:
                    scrap_px = intent.get("sell_limit")
                scrap_ttm = ttm_s
                loser_size = fill.get("best_bid_size")
                if loser_size is None and sold_leg in ("up", "dn"):
                    loser_size = _scrap_best_bid_size(books.get(sold_leg))
        dump_fired = bool(intent.get("sold_dump"))
        dump_px = None
        dump_ttm = None
        if dump_fired and not rec.get("dump_seen"):
            dump_px = recorded_fill_px(intent, "sell_dump_fill_px", "sell_dump_limit")
            if rec.get("partial") and intent.get("sold_dump_at"):
                dump_ttm = _ttm_at(end_ts, intent.get("sold_dump_at"))
            else:
                dump_ttm = ttm_s
        bag_risk_observe(
            rec,
            now_s=now,
            ttm_s=ttm_s,
            sold_loser=sold,
            sold_leg=sold_leg if sold_leg in ("up", "dn") else None,
            scrap_avg_px=scrap_px,
            loser_best_bid_size=loser_size,
            scrap_ttm=scrap_ttm,
            held_bid=held_bid,
            dump_fired=dump_fired,
            dump_px=dump_px,
            dump_ttm=dump_ttm,
        )
    except Exception:
        return


def _close_bag_risk(condition_id: str, intent: dict, *, now: float) -> None:
    """Emit one ``bag_risk`` line when the sell window closes. Log only."""
    try:
        done = _bag_risk_emitted()
        if condition_id in done:
            return
        rec = _bag_risk_rows().get(condition_id)
        if not isinstance(rec, dict):
            return
        bag_risk_flush(rec, now_s=now)
        shares = intent.get("shares")
        log_event(
            "bag_risk",
            **bag_risk_payload(
                rec,
                condition_id=condition_id,
                slug=intent.get("slug"),
                shares=shares,
            ),
        )
        done.add(condition_id)
        _bag_risk_rows().pop(condition_id, None)
    except Exception:
        return


def _reclaim_skip(intent: dict, cid: str, reason: str, **fields: Any) -> None:
    """Log ``reclaim_skip`` once per reason. A later reason logs again."""
    if intent.get("reclaim_skip_reason") == reason:
        return
    intent["reclaim_skip_reason"] = reason
    log_event(
        "reclaim_skip",
        condition_id=cid,
        slug=intent.get("slug"),
        reason=reason,
        **fields,
    )


def _reclaim_quote_fields(
    decision: dict,
    *,
    bids: dict,
    asks_px: dict,
    ttm_s: Optional[float],
) -> dict:
    leg = decision.get("leg")
    other = {"up": "dn", "dn": "up"}.get(leg or "")
    return {
        "leg": leg,
        "bid": bids.get(leg) if leg else None,
        "ask": asks_px.get(leg) if leg else None,
        "other_bid": bids.get(other) if other else None,
        "other_ask": asks_px.get(other) if other else None,
        "ttm": ttm_s,
        "dumped_leg": bool(decision.get("dumped_leg")),
    }


def _reclaim_finish_hot(intent: dict, *, stop_enabled: bool) -> None:
    """Stay on ``sell_armed_poll_s`` until the buy is done and the stop is not live."""
    if intent.get("reclaim_stopped"):
        intent["reclaim_hot"] = False
        return
    if intent.get("reclaim_bought") and not stop_enabled:
        intent["reclaim_hot"] = False
        return
    intent["reclaim_hot"] = True


def _reclaim_hold(intent: dict, cid: str) -> None:
    if intent.get("reclaim_hold_logged"):
        return
    intent["reclaim_hold_logged"] = True
    log_event(
        "reclaim_hold",
        condition_id=cid,
        slug=intent.get("slug"),
        leg=intent.get("reclaim_leg"),
        filled=intent.get("reclaim_filled"),
    )


def _reclaim_finish_buy(
    intent: dict,
    cid: str,
    *,
    reason: str,
    cfg: dict,
    now: float,
    ttm_s: Optional[float],
    bids: dict,
    tokens: dict,
    dry_run: bool,
    tol: float,
    chain: ChainReader,
    ctf: str,
    funder_cs: Optional[str],
    min_bid_size: float,
    clob_min: float,
    stop_enabled: bool,
    ages: Optional[dict] = None,
) -> None:
    """Buy phase is over. Log ``reclaim_done`` once, then the stop or the hold."""
    intent["reclaim_bought"] = True
    intent["reclaim_buy_inflight"] = False
    intent["reclaim_buy_uncertain"] = False
    if not intent.get("reclaim_done_logged"):
        intent["reclaim_done_logged"] = True
        filled = float(intent.get("reclaim_filled") or 0.0)
        avg = intent.get("reclaim_buy_px")
        cost = None
        try:
            if avg is not None and filled > 0:
                cost = round(float(avg) * filled, 4)
        except (TypeError, ValueError):
            cost = None
        log_event(
            "reclaim_done",
            condition_id=cid,
            slug=intent.get("slug"),
            leg=intent.get("reclaim_leg"),
            filled=filled,
            avg_px=avg,
            cost=cost,
            reason=str(reason),
            target=intent.get("reclaim_target"),
        )
    _reclaim_finish_hot(intent, stop_enabled=stop_enabled)
    if stop_enabled:
        _reclaim_stop_tick(
            cfg=cfg, intent=intent, cid=cid, now=now, ttm_s=ttm_s,
            bids=bids, tokens=tokens, dry_run=dry_run, tol=tol,
            chain=chain, ctf=ctf, funder_cs=funder_cs,
            min_bid_size=min_bid_size, clob_min=clob_min,
            stop_enabled=True, ages=ages,
        )
    else:
        _reclaim_hold(intent, cid)


def _reclaim_recover_buy(
    intent: dict,
    *,
    cid: str,
    tokens: dict,
    tol: float,
    chain: ChainReader,
    ctf: str,
    funder_cs: Optional[str],
) -> None:
    """One balance read after an uncertain post. A single zero is not a fill or a miss."""
    if not intent.get("reclaim_buy_uncertain"):
        return
    leg = intent.get("reclaim_leg")
    token = tokens.get(leg) if leg in ("up", "dn") else None
    if not token:
        return
    target = float(intent.get("reclaim_target") or 0.0)
    _bal, latch = _sell_inventory(
        chain, ctf, funder_cs, token, max(target, 0.0), tol,
        "seen_reclaim_buy_inventory", intent,
        read_chain=True,
    )
    if latch == "has_inventory":
        seen = float(intent.get("reclaim_filled") or 0.0)
        # ``_sell_inventory`` returns min(target, balance) once shares are visible.
        intent["reclaim_filled"] = max(seen, float(_bal or 0.0))
        intent["reclaim_buy_inflight"] = False
        intent["reclaim_buy_uncertain"] = False
        intent["reclaim_buy_flat_reads"] = 0
        if target > 0 and float(intent["reclaim_filled"]) + tol >= target:
            intent["reclaim_bought"] = True
        log_event(
            "reclaim_buy_recovered",
            condition_id=cid,
            slug=intent.get("slug"),
            leg=leg,
            filled=intent.get("reclaim_filled"),
        )
        return
    if latch != "already_flat":
        return
    flats = int(intent.get("reclaim_buy_flat_reads") or 0) + 1
    intent["reclaim_buy_flat_reads"] = flats
    if flats < 2:
        return
    intent["reclaim_buy_inflight"] = False
    intent["reclaim_buy_uncertain"] = False
    log_event(
        "reclaim_buy_uncertain_cleared",
        condition_id=cid,
        slug=intent.get("slug"),
        leg=leg,
        reads=flats,
    )


def _reclaim_stop_tick(
    *,
    cfg: dict,
    intent: dict,
    cid: str,
    now: float,
    ttm_s: Optional[float],
    bids: dict,
    tokens: dict,
    dry_run: bool,
    tol: float,
    chain: ChainReader,
    ctf: str,
    funder_cs: Optional[str],
    min_bid_size: float,
    clob_min: float,
    stop_enabled: bool,
    ages: Optional[dict] = None,
) -> None:
    """Stop sell. Inventory read, then one live-bid FAK. No sleep before that post."""
    if not stop_enabled:
        _reclaim_hold(intent, cid)
        intent["reclaim_hot"] = False
        return
    leg = intent.get("reclaim_leg")
    if leg not in ("up", "dn"):
        return
    bid = bids.get(leg)
    book_age = ages.get(leg) if isinstance(ages, dict) else None
    decision = reclaim_stop_decision(
        now_s=now,
        stop=float(cfg.get("reclaim_stop") or 0.75),
        persist_s=cfg_seconds(cfg, "reclaim_stop_persist_s", 0.5),
        armed_ts=intent.get("reclaim_stop_armed_at"),
        bid=bid,
        latched=bool(intent.get("reclaim_stop_latched")),
        stop_enabled=True,
        book_age_s=book_age,
    )
    intent["reclaim_stop_armed_at"] = decision.get("armed_ts")
    action = decision.get("action")
    if action == "wait":
        log_event(
            "reclaim_stop_persist",
            condition_id=cid,
            slug=intent.get("slug"),
            leg=leg,
            why=decision.get("reason"),
            bid=bid,
            stop=cfg.get("reclaim_stop"),
            persist_s=cfg_seconds(cfg, "reclaim_stop_persist_s", 0.5),
        )
        return
    if action != "sell":
        return
    intent["reclaim_stop_latched"] = True
    intent.pop("reclaim_skip_reason", None)
    filled = float(intent.get("reclaim_filled") or 0.0)
    sold_already = float(intent.get("reclaim_sold") or 0.0)
    remaining = max(0.0, filled - sold_already)
    if remaining < 0.01:
        intent["reclaim_stopped"] = True
        intent["reclaim_hot"] = False
        return
    token = str(tokens.get(leg) or "")
    if not token:
        return
    # Cap by a known balance. A lagging zero must not delay the stop.
    inv, latch = _sell_inventory(
        chain, ctf, funder_cs, token, remaining, tol,
        "seen_reclaim_inventory", intent,
    )
    if latch == "already_flat":
        intent["reclaim_stopped"] = True
        intent["reclaim_hot"] = False
        intent["reclaim_stop_note"] = "already_flat"
        log_event(
            "reclaim_stop",
            condition_id=cid,
            slug=intent.get("slug"),
            leg=leg,
            outcome="already_flat",
            dry_run=dry_run,
        )
        return
    size = remaining
    if latch == "has_inventory":
        size = min(remaining, float(inv or 0.0))
    if size < 0.01:
        return
    try:
        live = float(bid) if bid is not None else 0.0
    except (TypeError, ValueError):
        live = 0.0
    floor = float(clob_min or 0.01)
    if live > 0 and live < floor:
        live = floor
    fills: list = []
    with _mark_sell_exit(intent):
        if live > 0:
            sold, status, last_px, _attempts, live = _run_dump_fak_with_refire(
                token_id=token,
                size=size,
                initial_bid=live,
                initial_bids=[],
                held=leg,
                slug=intent.get("slug"),
                condition_id=cid,
                ttm_s=ttm_s,
                floor=floor,
                min_bid_size=min_bid_size,
                retries=int(cfg.get("sell_dump_fak_retries") or 0),
                ladder_step=float(cfg.get("sell_dump_ladder_step") or 0.04),
                ladder_rungs=int(cfg.get("sell_dump_ladder_rungs") or 1),
                dry_run=dry_run,
                tol=tol,
                fills=fills,
            )
        else:
            sold, status = _sell_fak_with_fallback(
                token, size, floor, dry_run, None, keep=0.0, tol=tol,
            )
            last_px = floor
            sold = float(sold or 0.0)
    if str(status) == "already_flat" and float(sold or 0.0) < tol and not dry_run:
        intent["reclaim_stopped"] = True
        intent["reclaim_hot"] = False
        intent["reclaim_stop_note"] = "already_flat"
        log_event(
            "reclaim_stop",
            condition_id=cid,
            slug=intent.get("slug"),
            leg=leg,
            outcome="already_flat",
            dry_run=dry_run,
        )
        return
    intent["reclaim_stop_limit"] = last_px
    intent["reclaim_stop_status"] = status
    if float(sold or 0.0) > 0:
        intent["reclaim_sold"] = sold_already + float(sold)
        record_fill_px(intent, "reclaim_stop_px", fills or [(float(sold), last_px)])
    done = bool(dry_run) or float(intent.get("reclaim_sold") or 0.0) + tol >= filled
    if str(status).startswith("error") and float(sold or 0.0) <= 0 and not dry_run:
        log_event(
            "reclaim_stop_uncertain",
            condition_id=cid,
            slug=intent.get("slug"),
            leg=leg,
            status=status,
            bid=bid,
        )
        return
    log_event(
        "reclaim_stop",
        condition_id=cid,
        slug=intent.get("slug"),
        leg=leg,
        bid=bid,
        limit=last_px,
        sold=float(sold or 0.0),
        planned=size,
        avg_px=intent.get("reclaim_stop_px"),
        status=status,
        dry_run=dry_run,
        outcome="done" if done else "partial",
    )
    if done:
        if dry_run:
            intent["reclaim_sold"] = filled
            intent["reclaim_stop_dry"] = True
        intent["reclaim_stopped"] = True
        intent["reclaim_hot"] = False


def _reclaim_tick(
    *,
    cfg: dict,
    intent: dict,
    cid: str,
    now: float,
    end_ts: float,
    ttm_s: Optional[float],
    bids: dict,
    asks_px: dict,
    ages: dict,
    tokens: dict,
    dry_run: bool,
    tol: float,
    chain: ChainReader,
    ctf: str,
    funder_cs: Optional[str],
    min_bid_size: float,
    clob_min: float,
) -> None:
    """Reclaim buy and stop on books this tick already fetched.

    No sleep, no second ``/book``, and no balance read before the buy FAK.
    The entry and stop clocks are ``persist_ready``: the first tick at or
    after the window posts. Scheduling lag is one ``sell_armed_poll_s``.
    """
    if not cfg.get("reclaim_enabled"):
        if intent.get("reclaim_hot"):
            intent["reclaim_hot"] = False
        return
    stop_enabled = bool(cfg.get("reclaim_stop_enabled", True))
    block = reclaim_arm_block(
        intent,
        also_kept=bool(cfg.get("sell_dump_also_kept", False)),
        tol=tol,
    )
    if block == "kept_pending":
        intent["reclaim_hot"] = True
        _reclaim_skip(intent, cid, "kept_pending", dumped_leg=False, ttm=ttm_s)
        return
    if block:
        if intent.get("reclaim_hot"):
            intent["reclaim_hot"] = False
        if block != "no_dump" or intent.get("sold_loser") or intent.get("sold_leg"):
            _reclaim_skip(intent, cid, block, dumped_leg=False, ttm=ttm_s)
        return
    if not intent.get("reclaim_armed"):
        intent["reclaim_armed"] = True
        intent["reclaim_armed_at"] = now
        log_event(
            "reclaim_arm",
            condition_id=cid,
            slug=intent.get("slug"),
            sell_dump_leg=intent.get("sell_dump_leg"),
            kept_done=bool(intent.get("sell_dump_kept_done")),
        )
    if intent.get("reclaim_stopped"):
        intent["reclaim_hot"] = False
        return
    if intent.get("reclaim_bought"):
        _reclaim_finish_hot(intent, stop_enabled=stop_enabled)
        if stop_enabled:
            _reclaim_stop_tick(
                cfg=cfg, intent=intent, cid=cid, now=now, ttm_s=ttm_s,
                bids=bids, tokens=tokens, dry_run=dry_run, tol=tol,
                chain=chain, ctf=ctf, funder_cs=funder_cs,
                min_bid_size=min_bid_size, clob_min=clob_min,
                stop_enabled=True, ages=ages,
            )
        else:
            _reclaim_hold(intent, cid)
            intent["reclaim_hot"] = False
        return
    intent["reclaim_hot"] = True
    locked = intent.get("reclaim_leg") if intent.get("reclaim_buy_posted") else None
    decision = reclaim_entry_decision(
        now_s=now,
        end_ts=float(end_ts or 0),
        ttm_s=ttm_s,
        max_ttm_s=cfg_seconds(cfg, "reclaim_max_ttm_s", 0.0),
        min_ttm_s=cfg_seconds(cfg, "reclaim_min_ttm_s", 0.0),
        entry=float(cfg.get("reclaim_entry") or 0.91),
        usd=float(cfg.get("reclaim_usd") or 100.0),
        persist_s=cfg_seconds(cfg, "reclaim_entry_persist_s", 0.5),
        slippage=float(
            0.03 if cfg.get("reclaim_slippage") is None else cfg.get("reclaim_slippage")
        ),
        max_price=float(
            0.96 if cfg.get("reclaim_max_price") is None else cfg.get("reclaim_max_price")
        ),
        armed_ts=intent.get("reclaim_entry_armed_at"),
        armed_leg=intent.get("reclaim_entry_leg"),
        locked_leg=locked if locked in ("up", "dn") else None,
        filled=float(intent.get("reclaim_filled") or 0.0),
        target=intent.get("reclaim_target"),
        up_bid=bids.get("up"),
        up_ask=asks_px.get("up"),
        dn_bid=bids.get("dn"),
        dn_ask=asks_px.get("dn"),
        up_age=ages.get("up"),
        dn_age=ages.get("dn"),
        inflight=bool(
            intent.get("reclaim_buy_inflight") or intent.get("reclaim_buy_uncertain")
        ),
        dumped_legs=(intent.get("sell_dump_leg"), intent.get("sell_dump_kept_leg")),
    )
    if decision.get("reason") != "inflight":
        intent["reclaim_entry_armed_at"] = decision.get("armed_ts")
        intent["reclaim_entry_leg"] = decision.get("armed_leg")
    action = decision.get("action")
    if action == "skip" and decision.get("reason") == "inflight":
        _reclaim_recover_buy(
            intent, cid=cid, tokens=tokens, tol=tol,
            chain=chain, ctf=ctf, funder_cs=funder_cs,
        )
        _reclaim_skip(
            intent, cid, "inflight",
            **_reclaim_quote_fields(
                decision, bids=bids, asks_px=asks_px, ttm_s=ttm_s,
            ),
        )
        if intent.get("reclaim_bought"):
            _reclaim_finish_buy(
                intent, cid, reason="recovered",
                cfg=cfg, now=now, ttm_s=ttm_s, bids=bids, tokens=tokens,
                dry_run=dry_run, tol=tol, chain=chain, ctf=ctf,
                funder_cs=funder_cs, min_bid_size=min_bid_size,
                clob_min=clob_min, stop_enabled=stop_enabled, ages=ages,
            )
        return
    if action == "wait":
        intent.pop("reclaim_skip_reason", None)
        log_event(
            "reclaim_entry_persist",
            condition_id=cid,
            slug=intent.get("slug"),
            leg=decision.get("leg"),
            why=decision.get("reason"),
            bid=bids.get(decision.get("leg")),
            ask=asks_px.get(decision.get("leg")),
            persist_s=cfg_seconds(cfg, "reclaim_entry_persist_s", 0.5),
        )
        return
    if decision.get("reason") == "above_cap" and intent.get("reclaim_buy_posted"):
        # Stay in the buy. The persist clock was cleared, so a later ask
        # at or under the cap has to hold again before the next send.
        if intent.get("reclaim_skip_reason") != "above_cap":
            intent["reclaim_skip_reason"] = "above_cap"
            cap = cfg.get("reclaim_max_price", 0.96)
            log_event(
                "reclaim_topup_skipped_cap",
                condition_id=cid,
                slug=intent.get("slug"),
                leg=decision.get("leg"),
                ask=asks_px.get(decision.get("leg")),
                cap=float(cap) if cap is not None else 0.96,
            )
        return
    if action != "buy":
        held_now = float(intent.get("reclaim_filled") or 0.0)
        # A partial that no longer qualifies is a position: stop it, do not
        # switch sides or wait for the entry to come back.
        if decision.get("reason") == "filled" or held_now > 0:
            _reclaim_finish_buy(
                intent, cid,
                reason=str(decision.get("reason") or "held"),
                cfg=cfg, now=now, ttm_s=ttm_s, bids=bids, tokens=tokens,
                dry_run=dry_run, tol=tol, chain=chain, ctf=ctf,
                funder_cs=funder_cs, min_bid_size=min_bid_size,
                clob_min=clob_min, stop_enabled=stop_enabled, ages=ages,
            )
            return
        _reclaim_skip(
            intent, cid, str(decision.get("reason") or "skip"),
            **_reclaim_quote_fields(
                decision, bids=bids, asks_px=asks_px, ttm_s=ttm_s,
            ),
        )
        return
    leg = decision.get("leg")
    shares = float(decision.get("shares") or 0.0)
    limit = decision.get("limit")
    token = tokens.get(leg) if leg in ("up", "dn") else None
    if leg not in ("up", "dn") or not token or limit is None or shares < 1:
        _reclaim_skip(
            intent, cid, "no_token",
            **_reclaim_quote_fields(
                decision, bids=bids, asks_px=asks_px, ttm_s=ttm_s,
            ),
        )
        return
    usdc = reclaim_buy_usdc(shares, float(limit))
    order_key = reclaim_buy_order_key(shares, float(limit))
    rejected = list(intent.get("reclaim_buy_rejects") or [])
    if order_key in rejected:
        # Same price and size already refused. Do not post it again.
        if float(intent.get("reclaim_filled") or 0.0) > 0:
            _reclaim_finish_buy(
                intent, cid, reason="rejected",
                cfg=cfg, now=now, ttm_s=ttm_s, bids=bids, tokens=tokens,
                dry_run=dry_run, tol=tol, chain=chain, ctf=ctf,
                funder_cs=funder_cs, min_bid_size=min_bid_size,
                clob_min=clob_min, stop_enabled=stop_enabled, ages=ages,
            )
        else:
            _reclaim_skip(
                intent, cid, "rejected",
                **_reclaim_quote_fields(
                    decision, bids=bids, asks_px=asks_px, ttm_s=ttm_s,
                ),
            )
        return
    # In memory only. The FAK is the next call: no commit, sleep, book, or balance.
    if intent.get("reclaim_target") is None:
        intent["reclaim_target"] = shares
    intent["reclaim_leg"] = leg
    intent["reclaim_entry_px"] = float(limit)
    intent["reclaim_buy_posted"] = True
    intent["reclaim_buy_inflight"] = True
    intent.pop("reclaim_skip_reason", None)
    captured: list = []
    with _io_unlocked():
        bought, status = _fak_buy(
            str(token), shares, float(limit), dry_run, capture=captured,
        )
    status_s = str(status or "")
    post_fields = {
        "condition_id": cid,
        "slug": intent.get("slug"),
        "leg": leg,
        "shares": shares,
        "price": float(limit),
        "usdc": usdc,
        "bought": float(bought or 0.0),
        "status": status_s,
        "dry_run": dry_run,
        "dumped_leg": bool(decision.get("dumped_leg")),
    }
    if dry_run or status_s == "dry":
        intent["reclaim_filled"] = float(intent.get("reclaim_target") or shares)
        intent["reclaim_buy_px"] = float(limit)
        intent["reclaim_buy_dry"] = True
        log_event("reclaim_buy", **post_fields)
        _reclaim_finish_buy(
            intent, cid, reason="dry_run",
            cfg=cfg, now=now, ttm_s=ttm_s, bids=bids, tokens=tokens,
            dry_run=dry_run, tol=tol, chain=chain, ctf=ctf,
            funder_cs=funder_cs, min_bid_size=min_bid_size,
            clob_min=clob_min, stop_enabled=stop_enabled, ages=ages,
        )
        return
    if status_s.startswith("reject"):
        intent["reclaim_buy_inflight"] = False
        intent["reclaim_buy_uncertain"] = False
        if order_key not in rejected:
            rejected.append(order_key)
        intent["reclaim_buy_rejects"] = rejected[-8:]
        log_event("reclaim_fak_reject", error=status_s, **post_fields)
        return
    if status_s.startswith("error"):
        intent["reclaim_buy_uncertain"] = True
        log_event("reclaim_buy_uncertain", **post_fields)
        return
    intent["reclaim_buy_inflight"] = False
    intent["reclaim_buy_uncertain"] = False
    if status_s.startswith("no_clob") or status_s == "bad_args":
        log_event("reclaim_buy", **post_fields)
        return
    if float(bought or 0.0) > 0:
        intent["reclaim_filled"] = float(intent.get("reclaim_filled") or 0.0) + float(bought)
        avg = buy_fill_vwap(captured[0] if captured else None, float(bought))
        record_fill_px(
            intent, "reclaim_buy_px",
            [(float(bought), avg if avg is not None else float(limit))],
        )
        post_fields["avg_px"] = intent.get("reclaim_buy_px")
        post_fields["filled"] = intent.get("reclaim_filled")
        log_event("reclaim_buy", **post_fields)
        log_event(
            "reclaim_buy_fill",
            condition_id=cid,
            slug=intent.get("slug"),
            leg=leg,
            bought=float(bought),
            avg_px=intent.get("reclaim_buy_px"),
        )
    else:
        log_event("reclaim_buy", **post_fields)
    target = float(intent.get("reclaim_target") or shares)
    if float(intent.get("reclaim_filled") or 0.0) + tol >= target:
        _reclaim_finish_buy(
            intent, cid, reason="filled",
            cfg=cfg, now=now, ttm_s=ttm_s, bids=bids, tokens=tokens,
            dry_run=dry_run, tol=tol, chain=chain, ctf=ctf,
            funder_cs=funder_cs, min_bid_size=min_bid_size,
            clob_min=clob_min, stop_enabled=stop_enabled, ages=ages,
        )


def manage_sells(cfg: dict, state: dict, chain: ChainReader) -> None:
    """Loser scrap: arm ≤2¢, FAK at 2¢ or the live bid; winner; held dump."""
    if not cfg.get("sell_enabled"):
        return
    STATE_LOCK.acquire()
    try:
        _manage_sells_locked(cfg, state, chain)
    finally:
        STATE_LOCK.release()


def _manage_sells_locked(cfg: dict, state: dict, chain: ChainReader) -> None:
    now = time.time()
    _whatsapp("configure", cfg)
    floor = float(cfg.get("sell_floor") or 0.02)
    opp_min = float(cfg.get("sell_opposite_min") or 0.90)
    persist_s = float(cfg.get("sell_persist_s") or 0.0)
    last_min_s = float(cfg.get("sell_persist_last_min_s", 2.0))
    last_min_window_s = float(cfg.get("sell_persist_last_min_window_s", 60.0))
    skip_when_sized = bool(cfg.get("sell_persist_skip_when_sized", False))
    blind_enabled = bool(cfg.get("sell_scrap_blind_enabled", True))
    blind_px = float(cfg.get("sell_scrap_blind_px", 0.01) or 0.01)
    blind_backoff = float(cfg.get("sell_scrap_blind_backoff_s", 3.0) or 0.0)
    rest_enabled = bool(cfg.get("sell_scrap_rest_enabled", True))
    rest_px = float(cfg.get("sell_scrap_rest_px", 0.02) or 0.02)
    rest_ahead = cfg_seconds(cfg, "sell_scrap_rest_min_ahead_s", 180.0)
    cooldown = cfg_seconds(cfg, "sell_cooldown_s", 3.0)
    winner_min = float(cfg.get("sell_winner_min") or 0.999)
    clob_max = float(cfg.get("sell_clob_max_price") or 0.99)
    clob_min = float(cfg.get("sell_clob_min_price") or 0.01)
    min_bid_size = float(cfg.get("sell_min_bid_size") or 1.0)
    tol = float(cfg.get("position_tolerance") or 0.01)
    scrap_fraction = normalize_scrap_fraction(cfg.get("sell_scrap_fraction", 1.0))
    dry_run = bool(cfg.get("dry_run"))
    funder = os.getenv("FUNDER_ADDRESS") or ""
    funder_cs = to_checksum_address(funder) if funder else None
    _bind_sell_chain(chain, str(cfg.get("ctf_address") or ""), funder_cs)
    _apply_book_timeout(cfg)
    dirty = False
    ctf = str(cfg.get("ctf_address") or "")

    def _observed_loser_bid(leg: Optional[str], live: dict, seen: dict) -> Optional[float]:
        """Live loser bid, else the last positive bid from the prior tick."""
        if leg not in ("up", "dn"):
            return None
        for value in (live.get(leg), seen.get(leg)):
            try:
                px = float(value)
            except (TypeError, ValueError):
                continue
            if px > 0:
                return px
        return None

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
            _log_scrap_outcome(intent, cid, "window_end")
            _whatsapp("scrap_filled", intent, cid, now=now, window_end=True)
            _close_bag_risk(cid, intent, now=now)
            continue

        up_tok = str(intent.get("up_token") or "")
        dn_tok = str(intent.get("dn_token") or "")
        if not up_tok or not dn_tok:
            continue

        # Same-tick books feed persist_ready and FAK; do not refetch after
        # persist-ready. Parallel UP/DN cuts sequential REST wait on the
        # armed path. The ~8.6–10.5s gap was poll_s plus mint-path work.
        with _io_unlocked():
            up_row, dn_row = _fetch_books(up_tok, dn_tok, min_bid_size)
        (
            up_bid, up_sz, up_bids, up_ask, _up_ask_sz, _up_asks, up_age,
        ) = _book_quote(up_row)
        (
            dn_bid, dn_sz, dn_bids, dn_ask, _dn_ask_sz, _dn_asks, dn_age,
        ) = _book_quote(dn_row)
        _whatsapp("note_bids", cid, up_bid, dn_bid)
        books = {"up": up_bids, "dn": dn_bids}
        ttm_s = (end_ts - now) if end_ts else None
        # Per bag, per tick. Dump and winner do not read this pair.
        thr, fak_px, scrap_price_late = scrap_active_prices(cfg, ttm_s)
        # Keep the previous positive print so an empty book after a FAK
        # miss still rests at the bid that armed the scrap, not 2¢.
        seen_bids = {
            "up": intent.get("last_up_bid"),
            "dn": intent.get("last_dn_bid"),
        }
        # In-memory fallback for scrap_rest_px after a missed FAK.
        # These prints alone must not rewrite positions_mint.json.
        intent["last_up_bid"] = up_bid
        intent["last_dn_bid"] = dn_bid
        intent["last_up_bid_size"] = up_sz
        intent["last_dn_bid_size"] = dn_sz
        intent["updated_at"] = now

        last = float(intent.get("last_sell_attempt_at") or 0)
        cooling = bool(last and now - last < cooldown)
        tokens = {"up": up_tok, "dn": dn_tok}
        bids = {"up": up_bid, "dn": dn_bid}
        shares = float(intent.get("shares") or cfg["shares"])
        sold_loser = bool(intent.get("sold_loser") or intent.get("sold_leg"))
        danger_held = {"up": "dn", "dn": "up"}.get(str(intent.get("sold_leg") or ""))
        _whatsapp(
            "danger_tick",
            intent,
            cid,
            now=now,
            held=danger_held,
            bid=bids.get(danger_held) if danger_held else None,
        )
        _bag_risk_ensure(cid, sold_at_start=sold_loser)
        if sold_loser and not intent.get("sold_loser_at"):
            prior = intent.get("last_sell_attempt_at")
            try:
                intent["sold_loser_at"] = float(prior) if prior not in (None, "") else now
            except (TypeError, ValueError):
                intent["sold_loser_at"] = now
        sold_winner = bool(intent.get("sold_winner"))

        # Prefer redeem at ~$1. Cheap 0.99 only if loser sold ≤ cheap_gate AND
        # loser_fill + cheap_min > 1.0 (beats mint). Flat 1¢+99¢ waits for redeem.
        loser_px_f = recorded_fill_px(intent, "sell_fill_px", "sell_limit")
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
        if kept_leg_below_winner_min(
            winner,
            bids.get(winner) if winner else None,
            sold_leg=intent.get("sold_leg"),
            keep=float(intent.get("sell_scrap_keep") or 0.0),
            winner_min=winner_min,
        ):
            if not intent.get("sell_keep_winner_blocked"):
                intent["sell_keep_winner_blocked"] = True
                log_event(
                    "sell_keep_winner_blocked",
                    condition_id=cid,
                    slug=intent.get("slug"),
                    leg=winner,
                    bid=bids.get(winner) if winner else None,
                    winner_min=winner_min,
                    effective_winner_min=effective_winner_min,
                    keep=intent.get("sell_scrap_keep"),
                )
            winner = None
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
                keep_cap = float(intent.get("sell_scrap_keep") or 0.0)
                if keep_cap > 1e-12 and winner == intent.get("sold_leg"):
                    size = min(float(size), keep_cap)
                if latch == "await_inventory":
                    log_event(
                        "sell_skip_await_inventory",
                        condition_id=cid,
                        leg=winner,
                        path="winner",
                    )
                elif latch == "already_flat":
                    intent["sold_winner"] = True
                    intent["sell_winner_leg"] = winner
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
                    if (
                        not dry_run
                        and last_status == "already_flat"
                        and sold_total < tol
                    ):
                        intent["sold_winner"] = True
                        intent["sell_winner_leg"] = winner
                        intent["sell_winner_note"] = "already_flat"
                    elif dry_run or sold_total >= size - tol:
                        intent["sold_winner"] = True
                        intent["sell_winner_leg"] = winner
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
        dump_persist_base = cfg_seconds(cfg, "sell_dump_persist_s", 2.0)
        # Re-evaluated every tick; armed_ts is never reset when the clock switches.
        dump_persist_s = effective_dump_persist_s(
            now_s=now,
            end_ts=end_ts,
            persist_s=dump_persist_base,
            last_min_s=cfg_seconds(
                cfg, "sell_dump_persist_last_min_s", dump_persist_base
            ),
            last_min_window_s=cfg_seconds(
                cfg, "sell_dump_persist_last_min_window_s", 0.0
            ),
        )
        dump_retries = int(cfg.get("sell_dump_fak_retries", 2))
        dump_ladder_step = float(cfg.get("sell_dump_ladder_step") or 0.04)
        dump_ladder_rungs = int(cfg.get("sell_dump_ladder_rungs", 4))
        # 0 or missing: no time gate (old behavior). Ladder rungs after the
        # first submitted order stay inside _run_dump_fak_with_refire and
        # are not re-checked against this cutoff.
        dump_max_ttm_s = float(cfg.get("sell_dump_max_ttm_s") or 0.0)
        sold_leg = intent.get("sold_leg")
        held = None
        if sold_leg == "up":
            held = "dn"
        elif sold_leg == "dn":
            held = "up"
        dump_bid = bids.get(held) if held else None
        dump_price_ok = (
            dump_enabled
            and sold_loser
            and not sold_dump
            and held is not None
            and dump_bid is not None
            and float(dump_bid) < dump_below - 1e-12
        )
        dump_ttm_ok = dump_time_gate_open(ttm_s, dump_max_ttm_s)
        if dump_price_ok and not dump_ttm_ok:
            _log_sell_dump_time_gated(
                condition_id=cid,
                slug=intent.get("slug"),
                leg=held,
                bid=dump_bid,
                ttm=ttm_s,
                cutoff=dump_max_ttm_s,
                now_s=now,
            )
        dump_armed = dump_price_ok and dump_ttm_ok
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
                persist_s=dump_persist_s,
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
                    dump_fills: list = []
                    with _mark_sell_exit(intent):
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
                                fills=dump_fills,
                            )
                        )
                    record_fill_px(intent, "sell_dump_fill_px", dump_fills)
                    intent["sell_dump_attempts"] = int(
                        intent.get("sell_dump_attempts") or 0
                    ) + int(used_attempts)
                    intent["sell_dump_last_status"] = last_status
                    if (
                        not dry_run
                        and last_status == "already_flat"
                        and sold_total < tol
                    ):
                        intent["sold_dump"] = True
                        intent["sold_winner"] = True
                        intent["sell_dump_note"] = "already_flat"
                    elif dry_run or sold_total >= size - tol or (
                        not dry_run and last_status == "already_flat"
                    ):
                        intent["sold_dump"] = True
                        intent["sold_winner"] = True
                        # Normal held dump only. Sister B buys the other leg.
                        intent["sell_dump_leg"] = held
                        if not intent.get("sold_dump_at"):
                            intent["sold_dump_at"] = now
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
                            hedge_leg="dn" if held == "up" else "up",
                            sold=sold_total,
                            avg_px=intent.get("sell_dump_fill_px"),
                            bid=dump_bid,
                            status=last_status,
                        )
                        _whatsapp("dump_filled", intent, cid, now=now, leg=held)
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
                        if (
                            bool(cfg.get("sell_dump_also_kept", False))
                            and not intent.get("sell_dump_kept_done")
                        ):
                            _sell_kept_after_dump(
                                cfg=cfg,
                                intent=intent,
                                cid=cid,
                                kept=sold_leg,
                                tokens=tokens,
                                bids=bids,
                                books=books,
                                ttm_s=ttm_s,
                                min_bid_size=min_bid_size,
                                retries=dump_retries,
                                ladder_step=dump_ladder_step,
                                ladder_rungs=dump_ladder_rungs,
                                dry_run=dry_run,
                                tol=tol,
                                chain=chain,
                                ctf=ctf,
                                funder_cs=funder_cs,
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
        filled_scrap = float(intent.get("sell_filled") or 0)
        stored_target = intent.get("sell_scrap_target")
        if stored_target is not None:
            try:
                remaining_shares = max(0.0, float(stored_target) - filled_scrap)
            except (TypeError, ValueError):
                remaining_shares = max(0.0, shares - filled_scrap)
        elif scrap_fraction < 1.0 - 1e-12:
            planned_target, _planned_keep = scrap_share_plan(shares, scrap_fraction)
            remaining_shares = max(0.0, planned_target - filled_scrap)
        else:
            remaining_shares = max(0.0, shares - filled_scrap)
        loser_persist_s, persist_why = loser_scrap_persist_s(
            now_s=now,
            end_ts=end_ts,
            persist_s=persist_s,
            last_min_s=last_min_s,
            last_min_window_s=last_min_window_s,
            depth_at_limit=depth_at_limit,
            our_size=remaining_shares,
            skip_when_sized=skip_when_sized,
        )
        if loser_persist_s is None:
            _note_bag_risk(
                cid, intent, now=now, end_ts=end_ts, ttm_s=ttm_s,
                bids=bids, books=books,
            )
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
        if persist_why == "sized_skip" and (
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
        # 0 or missing: no scrap time gate (old behavior). Unknown ttm
        # stays open. Blind, sweep, and post-miss rest only run after this
        # arm, so a closed gate blocks those fires too. Dump keeps its own
        # cutoff. A cheap bid from before the gate still waits full persist.
        scrap_max_ttm_s = float(cfg.get("sell_scrap_max_ttm_s") or 0.0)
        scrap_ttm_ok = scrap_time_gate_open(ttm_s, scrap_max_ttm_s)
        if not scrap_ttm_ok and not sold_loser:
            for leg_name in ("up", "dn"):
                leg_bid = bids.get(leg_name)
                try:
                    cheap = (
                        leg_bid is not None and float(leg_bid) <= thr + 1e-12
                    )
                except (TypeError, ValueError):
                    cheap = False
                if cheap:
                    _log_sell_scrap_time_gated(
                        condition_id=cid,
                        slug=intent.get("slug"),
                        leg=leg_name,
                        bid=leg_bid,
                        ttm=ttm_s,
                        cutoff=scrap_max_ttm_s,
                        now_s=now,
                    )
        # Scrap oracle veto: re-read every tick from memory. A block resets
        # the arm like the time gate, so once the TWAP is clearly against
        # the leg the scrap still waits its full persist.
        veto_leg = loser or keep_leg or prev_leg
        scrap_veto = False
        scrap_oracle: dict = {}
        if (
            scrap_ttm_ok
            and not sold_loser
            and veto_leg in ("up", "dn")
            and (
                loser is not None
                or keep_empty
                or fak_rearm
                or intent.get("sell_scrap_rest_id")
            )
        ):
            scrap_veto, _veto_why, scrap_oracle = _scrap_oracle_gate(
                cfg,
                cid,
                veto_leg,
                now_s=now,
                slug=intent.get("slug"),
                bid=bids.get(veto_leg),
                ttm=ttm_s,
                phase="arm",
            )
        scrap_ok = scrap_ttm_ok and not scrap_veto
        fire_l, armed_l, why_l = loser_persist_ready(
            loser is not None and not sold_loser and scrap_ok,
            now_s=now,
            armed_ts=intent.get("sell_loser_armed_at"),
            persist_s=loser_persist_s,
            last_status=intent.get("sell_last_status"),
            book_empty=(keep_empty or fak_rearm) and scrap_ok,
        )
        intent["sell_loser_armed_at"] = armed_l
        if why_l == "reset":
            # CLOB disarm only. An empty loser book must not wipe the
            # oracle persist clock; advance_oracle_edge_arm owns that.
            intent["sell_loser_leg"] = None
        else:
            intent["sell_loser_leg"] = loser or keep_leg or prev_leg
        persist_leg = loser or intent.get("sell_loser_leg")

        # 0 (the default) skips the veto. A missing key stays off.
        late_window_s = float(cfg.get("sell_late_window_s", 0.0) or 0.0)
        edge_per_ttm = float(cfg.get("sell_oracle_edge_per_ttm", 0.0) or 0.0)
        edge_persist_s = float(cfg.get("sell_oracle_edge_persist_s", 3.0) or 0.0)
        stale_s = float(cfg.get("sell_oracle_stale_s", 0.0) or 0.0)
        floor_usd = float(cfg.get("sell_oracle_edge_floor_usd", 0.0) or 0.0)
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
            intent["sell_oracle_edge_leg"] = None
        elif in_late:
            view = _oracle_bag_view(cid)
            age_s = (
                None
                if view.obs_ts is None
                else max(0.0, float(now) - float(view.obs_ts))
            )
            # Kept leg still counts while the loser bid has flickered off.
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
            edge_detail["open_source"] = getattr(view, "open_source", None)
            oracle_fire, armed_o, why_o, armed_leg = advance_oracle_edge_arm(
                edge_ok=bool(edge_ok),
                sold_loser=sold_loser,
                scrap_leg=scrap_for_edge,
                now_s=now,
                armed_ts=intent.get("sell_oracle_edge_armed_at"),
                armed_leg=intent.get("sell_oracle_edge_leg"),
                persist_s=edge_persist_s,
                in_late=True,
            )
            intent["sell_oracle_edge_armed_at"] = armed_o
            intent["sell_oracle_edge_leg"] = armed_leg
            oracle_block_why = edge_why if not edge_ok else why_o
            edge_qualify = bool(edge_ok) and not sold_loser
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
                    open_source=edge_detail.get("open_source"),
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
                    open_source=edge_detail.get("open_source"),
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
            oracle_blocks=oracle_hard_block or scrap_veto,
            loser_qualifies=loser_qualifies,
            window_open=True,
        )
        sold_loser = bool(intent.get("sold_loser") or intent.get("sold_leg"))

        if loser and why_l in {"ready", "immediate"}:
            loser_bid = float(bids[loser] or thr)
            if bool(cfg.get("sell_scrap_sweep_enabled", True)):
                first_limit = scrap_live_bid_limit(loser_bid, thr, min_px=clob_min)
            else:
                preview = loser_ladder_limits(thr, floor, loser_bid, fak_px=fak_px)
                first_limit = preview[0] if preview else min(float(loser_bid), fak_px)
            _log_sell_book_depth(
                slug=intent.get("slug"),
                leg=loser,
                limit=first_limit,
                our_size=(
                    remaining_shares
                    if _uses_scrap_plan(intent, scrap_fraction)
                    else shares
                ),
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
                threshold=thr,
                late_price=scrap_price_late,
            )
        elif why_l in {"empty_fak_keep_arm", "empty_fak_rearm", "empty_keep_arm"}:
            log_event(
                "sell_loser_persist",
                condition_id=cid,
                slug=intent.get("slug"),
                leg=persist_leg,
                why=why_l,
                bid=bids.get(persist_leg) if persist_leg in bids else None,
                threshold=thr,
                late_price=scrap_price_late,
            )

        if (
            fire_l
            and scrap_ok
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
                    open_source=edge_detail.get("open_source"),
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
                        open_source=edge_detail.get("open_source"),
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
                    _finish_scrap(
                        intent, cid, loser, note="already_flat", outcome="flat",
                    )
                else:
                    sweep_on = bool(cfg.get("sell_scrap_sweep_enabled", True))
                    intent["last_sell_attempt_at"] = now
                    post_size = float(size)
                    if _uses_scrap_plan(intent, scrap_fraction):
                        post_size = _scrap_post_shares(
                            intent, size, scrap_fraction, cid, loser,
                            threshold=thr,
                        )
                    plan_done = (
                        _uses_scrap_plan(intent, scrap_fraction)
                        and post_size < 0.01
                    )
                    fire_veto = False
                    fire_oracle = scrap_oracle
                    if not plan_done:
                        fire_veto, _fire_why, fire_oracle = _scrap_oracle_gate(
                            cfg,
                            cid,
                            loser,
                            now_s=time.time(),
                            slug=intent.get("slug"),
                            bid=loser_bid,
                            ttm=ttm_s,
                            phase="fire",
                        )
                    if plan_done:
                        _met, why = scrap_target_met(
                            filled=float(intent.get("sell_filled") or 0),
                            target=float(intent.get("sell_scrap_target") or 0),
                            keep=float(intent.get("sell_scrap_keep") or 0),
                            tol=tol,
                            balance=float(size),
                        )
                        _finish_scrap(
                            intent,
                            cid,
                            loser,
                            note=why or "scrap_keep",
                            outcome=why or "target_filled",
                        )
                    elif fire_veto:
                        intent["sell_loser_armed_at"] = None
                    else:
                        oracle_fields = dict(_scrap_oracle_fields(fire_oracle))
                        if "oracle_margin" in oracle_fields:
                            intent["sell_scrap_oracle_margin"] = oracle_fields["oracle_margin"]
                            intent["sell_scrap_oracle_live_margin"] = oracle_fields["oracle_live_margin"]
                        oracle_fields["late_price"] = scrap_price_late
                        scrap_fills: list = []
                        with _io_unlocked():
                            sold_total, last_status, last_px, balance_flat = (
                                _fire_loser_scrap(
                                    token_id=l_tok,
                                    size=post_size,
                                    floor=floor,
                                    threshold=thr,
                                    loser_bid=loser_bid,
                                    fak_px=fak_px,
                                    depth_at_limit=depth_at_limit,
                                    sweep=sweep_on,
                                    dry_run=dry_run,
                                    tol=tol,
                                    bids=books.get(loser),
                                    slug=intent.get("slug"),
                                    leg=loser,
                                    ttm_s=ttm_s,
                                    condition_id=cid,
                                    chain=chain,
                                    ctf=ctf,
                                    funder_cs=funder_cs,
                                    intent=intent,
                                    shares=shares,
                                    fills=scrap_fills,
                                    log_extra=oracle_fields,
                                    min_px=clob_min,
                                )
                            )
                        record_fill_px(intent, "sell_fill_px", scrap_fills)
                        intent["sell_attempts"] = int(intent.get("sell_attempts") or 0) + 1
                        intent["sell_last_status"] = last_status
                        done = (
                            dry_run
                            or balance_flat
                            or last_status == "already_flat"
                            or sold_total >= post_size - tol
                        )
                        if sold_total >= tol or dry_run:
                            intent["sell_filled"] = float(
                                intent.get("sell_filled") or 0
                            ) + sold_total
                            intent["sell_limit"] = last_px
                        if done:
                            outcome = (
                                "dry_run" if dry_run
                                else "target_filled"
                            )
                            if not dry_run and (
                                balance_flat or last_status == "already_flat"
                            ):
                                _met, why_flat = scrap_target_met(
                                    filled=float(intent.get("sell_filled") or 0),
                                    target=float(intent.get("sell_scrap_target") or 0),
                                    keep=float(intent.get("sell_scrap_keep") or 0),
                                    tol=tol,
                                    balance=getattr(_bind_sell_chain, "last_balance", None),
                                )
                                outcome = why_flat or "flat"
                            _finish_scrap(intent, cid, loser, outcome=outcome)
                            if dry_run:
                                intent["sell_dry"] = True
                            done_extra = {}
                            if intent.get("sell_scrap_target") is not None:
                                done_extra = {
                                    "target": intent.get("sell_scrap_target"),
                                    "keep": intent.get("sell_scrap_keep"),
                                    "filled": float(intent.get("sell_filled") or 0),
                                }
                            log_event(
                                "sell_loser_done",
                                condition_id=cid,
                                slug=intent.get("slug"),
                                leg=loser,
                                sold=sold_total,
                                avg_px=intent.get("sell_fill_px"),
                                bid=loser_bid,
                                status=last_status,
                                **done_extra,
                                **oracle_fields,
                            )
                            notify(
                                "Mint loser sold",
                                f"{intent.get('slug')}\n{loser} x{sold_total:.1f} "
                                f"@bid<={thr:.2f} avg={intent.get('sell_fill_px')}",
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
                                max(0.0, post_size - sold_total),
                                now=now,
                                end_ts=end_ts,
                                rest_px=scrap_rest_px(
                                    rest_px, _observed_loser_bid(loser, bids, seen_bids)
                                ),
                                rest_ahead=rest_ahead,
                                rest_enabled=rest_enabled,
                                dry_run=dry_run,
                                oracle_blocks=oracle_blocks_new,
                                loser_qualifies=True,
                                armed=intent.get("sell_loser_armed_at") is not None,
                                fak_miss=True,
                            )

        if (
            scrap_ok
            and not intent.get("sold_loser")
            and not intent.get("sell_scrap_rest_id")
            and persist_leg in ("up", "dn")
            and why_l in {"empty_keep_arm", "empty_fak_keep_arm"}
        ):
            b_tok = tokens.get(persist_leg) or ""
            b_size, b_latch = _sell_inventory(
                chain, ctf, funder_cs, b_tok, shares, tol,
                "seen_loser_inventory", intent,
            )
            scrap_kept = False
            if b_latch != "already_flat" and _uses_scrap_plan(intent, scrap_fraction):
                clipped = _scrap_post_shares(
                    intent, b_size, scrap_fraction, cid, persist_leg,
                    threshold=thr,
                )
                if clipped < 0.01:
                    _met, why = scrap_target_met(
                        filled=float(intent.get("sell_filled") or 0),
                        target=float(intent.get("sell_scrap_target") or 0),
                        keep=float(intent.get("sell_scrap_keep") or 0),
                        tol=tol,
                        balance=float(b_size),
                    )
                    _finish_scrap(
                        intent,
                        cid,
                        persist_leg,
                        note=why or "scrap_keep",
                        outcome=why or "target_filled",
                    )
                    scrap_kept = True
                else:
                    b_size = clipped
            if b_latch == "already_flat":
                _finish_scrap(
                    intent, cid, persist_leg, note="already_flat", outcome="flat",
                )
            elif scrap_kept:
                pass
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
                blind_oracle = scrap_oracle
                if blind_fire and b_tok:
                    blind_veto, _blind_vwhy, blind_oracle = _scrap_oracle_gate(
                        cfg,
                        cid,
                        persist_leg,
                        now_s=time.time(),
                        slug=intent.get("slug"),
                        bid=bids.get(persist_leg),
                        ttm=ttm_s,
                        phase="blind",
                    )
                    if blind_veto:
                        blind_fire = False
                        intent["sell_loser_armed_at"] = None
                if blind_fire and b_tok:
                    intent["sell_blind_last_at"] = now
                    blind_fields = _scrap_oracle_fields(blind_oracle)
                    if blind_fields:
                        intent["sell_scrap_oracle_margin"] = blind_fields["oracle_margin"]
                        intent["sell_scrap_oracle_live_margin"] = blind_fields["oracle_live_margin"]
                    blind_raw: list = []
                    with _io_unlocked():
                        blind_sold, blind_status = _sell_fak_with_fallback(
                            b_tok,
                            b_size,
                            blind_px,
                            dry_run,
                            blind_raw,
                            keep=float(intent.get("sell_scrap_keep") or 0.0),
                            tol=tol,
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
                        threshold=thr,
                        late_price=scrap_price_late,
                        **blind_fields,
                    )
                    intent["sell_attempts"] = int(intent.get("sell_attempts") or 0) + 1
                    intent["sell_last_status"] = blind_status
                    if float(blind_sold or 0) >= tol:
                        intent["sell_filled"] = float(
                            intent.get("sell_filled") or 0
                        ) + float(blind_sold)
                        intent["sell_limit"] = blind_px
                        record_fill_px(
                            intent,
                            "sell_fill_px",
                            [(
                                float(blind_sold),
                                sell_fill_vwap(
                                    blind_raw[0] if blind_raw else None, blind_sold,
                                ),
                            )],
                        )
                    if blind_status == "already_flat" and float(blind_sold or 0) < tol and not dry_run:
                        _met, why_flat = scrap_target_met(
                            filled=float(intent.get("sell_filled") or 0),
                            target=float(intent.get("sell_scrap_target") or 0),
                            keep=float(intent.get("sell_scrap_keep") or 0),
                            tol=tol,
                            balance=getattr(_bind_sell_chain, "last_balance", None),
                        )
                        _finish_scrap(
                            intent, cid, persist_leg,
                            note="already_flat",
                            outcome=why_flat or "flat",
                        )
                    elif float(blind_sold or 0) >= b_size - tol and not dry_run:
                        _finish_scrap(
                            intent, cid, persist_leg, outcome="target_filled",
                        )
                        log_event(
                            "sell_loser_done",
                            condition_id=cid,
                            slug=intent.get("slug"),
                            leg=persist_leg,
                            sold=blind_sold,
                            status=blind_status,
                            **blind_fields,
                        )
                    elif empty_fak_status(blind_status):
                        _place_scrap_rest(
                            intent,
                            cid,
                            b_tok,
                            max(0.0, b_size - float(blind_sold or 0)),
                            now=now,
                            end_ts=end_ts,
                            rest_px=scrap_rest_px(
                                rest_px,
                                _observed_loser_bid(persist_leg, bids, seen_bids),
                            ),
                            rest_ahead=rest_ahead,
                            rest_enabled=rest_enabled,
                            dry_run=dry_run,
                            oracle_blocks=oracle_blocks_new,
                            loser_qualifies=True,
                            armed=True,
                            fak_miss=True,
                        )
        elif (
            scrap_ok
            and why_l in {"empty_fak_keep_arm", "empty_fak_rearm"}
            and not intent.get("sell_scrap_rest_id")
            and not intent.get("sold_loser")
            and persist_leg in ("up", "dn")
            and empty_fak_status(intent.get("sell_last_status"))
        ):
            rest_size = remaining_shares
            if _uses_scrap_plan(intent, scrap_fraction):
                if intent.get("sell_scrap_target") is None:
                    _lock_scrap_plan(
                        intent,
                        held=shares,
                        fraction=scrap_fraction,
                        cid=cid,
                        leg=persist_leg,
                        threshold=thr,
                    )
                rest_size = max(
                    0.0,
                    float(intent.get("sell_scrap_target") or 0)
                    - float(intent.get("sell_filled") or 0),
                )
            _place_scrap_rest(
                intent,
                cid,
                tokens.get(persist_leg) or "",
                rest_size,
                now=now,
                end_ts=end_ts,
                rest_px=scrap_rest_px(
                    rest_px, _observed_loser_bid(persist_leg, bids, seen_bids)
                ),
                rest_ahead=rest_ahead,
                rest_enabled=rest_enabled,
                dry_run=dry_run,
                oracle_blocks=oracle_blocks_new,
                loser_qualifies=loser_qualifies,
                armed=intent.get("sell_loser_armed_at") is not None,
                fak_miss=True,
            )

        try:
            _reclaim_tick(
                cfg=cfg,
                intent=intent,
                cid=cid,
                now=now,
                end_ts=end_ts,
                ttm_s=ttm_s,
                bids=bids,
                asks_px={"up": up_ask, "dn": dn_ask},
                ages={"up": up_age, "dn": dn_age},
                tokens=tokens,
                dry_run=dry_run,
                tol=tol,
                chain=chain,
                ctf=ctf,
                funder_cs=funder_cs,
                min_bid_size=min_bid_size,
                clob_min=clob_min,
            )
        except Exception as exc:
            log_event("reclaim_error", condition_id=cid, error=str(exc)[:200])

        _note_bag_risk(
            cid, intent, now=now, end_ts=end_ts, ttm_s=ttm_s,
            bids=bids, books=books,
        )

    commit_state(state, dirty=dirty)

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
    if seq_settings(cfg)[0]:
        if seq_busy_bag(state, now, ACTIVE_STATUSES, exclude=condition_id) is not None:
            return "seq_wait_prev"
    elif mint_slots_full(state, cfg, now, float(candidate_start_ts)):
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


def _log_seq_skips(markets: List[MintMarket], state: dict, cfg: dict, now: float) -> None:
    """One ``mint_seq_skip`` per live window that passed its cutoff unminted."""
    for market in seq_late_markets(markets, cfg, now):
        with STATE_LOCK:
            intent = (state.get("intents") or {}).get(market.condition_id) or {}
            held = isinstance(intent, dict) and intent.get("status") in (
                ACTIVE_STATUSES | {"completed"}
            )
        if held:
            continue
        wait = _SEQ_WAITS.take_skip(market.condition_id)
        if wait is None:
            continue
        log_event(
            "mint_seq_skip",
            condition_id=market.condition_id,
            slug=market.slug,
            start_ts=market.start_ts,
            since_start_s=round(now - float(market.start_ts), 1),
            cutoff_s=seq_settings(cfg)[2],
            last_wait=wait.get("reason") or "none",
            waited_s=round(now - float(wait["first"]), 1) if wait.get("first") else 0.0,
        )


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
            commit_state(state)

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
    seq_on = seq_settings(cfg)[0]
    if seq_on:
        _log_seq_skips(markets, state, cfg, now)
        candidates = seq_eligible_markets(markets, cfg, now)
    else:
        candidates = eligible_markets(markets, cfg, now)
    if not candidates:
        write_loop_heartbeat("mint", "idle", markets=len(markets), eligible=0)
        return "idle"

    # Data-api /positions is not polled every tick (it was the 429 source).
    # One check runs only once a candidate is about to be submitted.
    tol = float(cfg["position_tolerance"])
    with STATE_LOCK:
        def _fail_attempts(condition_id: str) -> int:
            intent = (state.get("intents") or {}).get(condition_id) or {}
            if not isinstance(intent, dict) or intent.get("status") != "failed":
                return 0
            try:
                return int(intent.get("mint_attempts") or 0)
            except (TypeError, ValueError):
                return 0

        pick, status = select_mint_candidate(
            candidates,
            is_blocked=lambda condition_id: already_minted(state, condition_id, cfg, now),
            is_owned=lambda market: False,
            slots_full=lambda market: (
                seq_busy_bag(state, now, ACTIVE_STATUSES, exclude=market.condition_id) is not None
                if seq_on
                else mint_slots_full(state, cfg, now, float(market.start_ts))
            ),
            min_start_ts=held_forward_floor(state, now, ACTIVE_STATUSES),
            fail_attempts=_fail_attempts,
        )

        if seq_on and status == "capped" and pick is not None:
            busy = seq_busy_bag(state, now, ACTIVE_STATUSES, exclude=pick.condition_id)
            busy_cid, busy_intent = busy if busy is not None else ("", {})
            if _SEQ_WAITS.note_wait(pick.condition_id, "prev_bag", now):
                log_event(
                    "mint_seq_wait_prev",
                    condition_id=pick.condition_id,
                    slug=pick.slug,
                    start_ts=pick.start_ts,
                    since_start_s=round(now - float(pick.start_ts), 1),
                    prev_condition_id=busy_cid,
                    prev_slug=busy_intent.get("slug"),
                    prev_end_ts=busy_intent.get("end_ts"),
                )
            write_loop_heartbeat("mint", "seq_wait_prev", next_start=float(pick.start_ts))
            return "seq_wait_prev"

        if status == "capped" and pick is not None:
            write_loop_heartbeat(
                "mint",
                "capped_open",
                open=open_intent_count(state, now, cfg),
                next_start=float(pick.start_ts),
            )
            return "capped_open"

        if status != "pick" or pick is None:
            write_loop_heartbeat(
                "mint", "idle", markets=len(markets), eligible=len(candidates), reason="owned"
            )
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
        code_cache = getattr(chain, "_code_ok", None)
        if not isinstance(code_cache, dict):
            code_cache = {}
            try:
                chain._code_ok = code_cache
            except Exception:
                pass
        outcome_cache = getattr(chain, "_outcome_count", None)
        if not isinstance(outcome_cache, dict):
            outcome_cache = {}
            try:
                chain._outcome_count = outcome_cache
            except Exception:
                pass
        pusd_addr = str(cfg["pUSD_address"])
        adapter_addr = str(cfg["standard_adapter_address"])
        if pusd_addr not in code_cache:
            if not chain.has_contract(pusd_addr):
                return "no_pusd_contract"
            code_cache[pusd_addr] = True
        elif not code_cache[pusd_addr]:
            return "no_pusd_contract"
        if adapter_addr not in code_cache:
            if not chain.has_contract(adapter_addr):
                return "no_adapter"
            code_cache[adapter_addr] = True
        elif not code_cache[adapter_addr]:
            return "no_adapter"
        outcome_key = str(pick.condition_id)
        if outcome_key in outcome_cache:
            outcome_n = outcome_cache[outcome_key]
        else:
            outcome_n = chain.outcome_slot_count(str(cfg["ctf_address"]), pick.condition_id)
            if outcome_n == 2:
                outcome_cache[outcome_key] = outcome_n
        if outcome_n != 2:
            log_event("mint_skip_not_binary", condition_id=pick.condition_id, slug=pick.slug)
            return "not_binary"
        try:
            cash_poll_s = float(cfg.get("mint_cash_poll_s") or 2.0)
        except (TypeError, ValueError):
            cash_poll_s = 2.0
        if cash_poll_s != cash_poll_s or cash_poll_s < 0 or cash_poll_s == float("inf"):
            cash_poll_s = 2.0
        sample = getattr(chain, "_pusd_sample", None)
        force_cash = False
        redeems = state.get("redeems") or {}
        if isinstance(redeems, dict) and isinstance(sample, dict):
            sample_ts = float(sample.get("ts") or 0.0)
            for job in redeems.values():
                if not isinstance(job, dict):
                    continue
                if job.get("status") != "done" or job.get("reason") != "redeemed":
                    continue
                try:
                    updated = float(job.get("updated_at") or 0.0)
                except (TypeError, ValueError):
                    updated = 0.0
                if updated > sample_ts:
                    force_cash = True
                    break
        if (
            isinstance(sample, dict)
            and not force_cash
            and sample.get("key") == funder_cs
            and sample.get("balance") is not None
            and now - float(sample.get("ts") or 0.0) < cash_poll_s
        ):
            balance = float(sample["balance"])
        else:
            balance = chain.pUSD_balance(pusd_addr, funder_cs)
            try:
                chain._pusd_sample = {
                    "ts": now,
                    "balance": balance,
                    "key": funder_cs,
                }
            except Exception:
                pass
        with STATE_LOCK:
            reserved = pending_mint_reserve(state)
        block = mint_cash_block(balance, shares, reserved)
        if seq_on and block is not None:
            if _SEQ_WAITS.note_wait(pick.condition_id, "cash", now):
                log_event(
                    "mint_seq_wait_cash",
                    condition_id=pick.condition_id,
                    slug=pick.slug,
                    cash_reason=block["reason"],
                    balance=block["balance"],
                    reserved=block["reserved"],
                    free=block["free"],
                    need=block["need"],
                    since_start_s=round(now - float(pick.start_ts), 1),
                    waited_s=round(_SEQ_WAITS.waited_s(pick.condition_id, now), 1),
                )
            write_loop_heartbeat(
                "mint", "seq_wait_cash", balance=block["balance"], need=block["need"]
            )
            return "seq_wait_cash"
        if block is not None and block["reason"] == "pending_reserve":
            console.print(
                "  [dim red][SKIP][/] pending reserve  "
                f"bal={block['balance']:.2f} reserved={block['reserved']:.2f} "
                f"free={block['free']:.2f} need={block['need']:.2f}"
            )
            log_event(
                "mint_skip_pending_reserve",
                balance=block["balance"],
                reserved=block["reserved"],
                free=block["free"],
                need=block["need"],
                slug=pick.slug,
            )
            write_loop_heartbeat(
                "mint",
                "pending_reserve",
                balance=block["balance"],
                reserved=block["reserved"],
                free=block["free"],
                need=block["need"],
            )
            return "pending_reserve"
        if block is not None:
            console.print(
                f"  [dim red][SKIP][/] insufficient pUSD  bal={balance:.2f} need={shares:.2f}"
            )
            log_event("mint_skip_balance", balance=balance, need=shares)
            write_loop_heartbeat("mint", "no_balance", balance=balance)
            return "no_balance"
        held_positions: Dict[str, float] = {}
        try:
            held_positions = gateway.positions(funder) or {}
        except Exception as exc:
            log_event("positions_fetch_fail", error=str(exc)[:160])
            held_positions = {}
        if (
            float(held_positions.get(pick.up_token, 0) or 0) > tol
            or float(held_positions.get(pick.dn_token, 0) or 0) > tol
        ):
            log_event(
                "mint_skip_existing",
                condition_id=pick.condition_id,
                slug=pick.slug,
                source="positions",
            )
            return "existing_position"
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
        sequential=seq_on,
        seq_waited_s=round(_SEQ_WAITS.waited_s(pick.condition_id, now), 1) if seq_on else None,
    )
    _SEQ_WAITS.minted(pick.condition_id)

    gas_margin, gas_fallback, gas_cap = mint_gas_settings(cfg)
    with RELAY_SUBMIT_LOCK:
        tx_id, err, gas = submit_mint_batch(
            calls,
            metadata=f"mintbot:split:{pick.condition_id}:{int(now)}",
            rpc=chain._rpc,
            gas_margin=gas_margin,
            gas_fallback=gas_fallback,
            gas_cap=gas_cap,
        )
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
        log_event("mint_submit_fail",
            condition_id=pick.condition_id,
            error=err,
            errorMsg=fail_msg,
            gas_limit=gas.get("gas_limit"),
            gas_estimate=gas.get("gas_estimate"),
            gas_clamped=gas.get("gas_clamped"),
            gas_source=gas.get("gas_source"),
            gas_cap=gas.get("gas_cap"),
            gas_hub_max=gas.get("gas_hub_max"),
        )
        notify("Mint submit failed", f"{pick.slug}\n{err}", priority="high")
        write_loop_heartbeat("mint", "submit_fail")
        return "submit_fail"

    console.print(f"  [bold bright_green][MINT ▶][/] tx={tx_id[:18]}…")
    log_event("mint_submitted",
        condition_id=pick.condition_id,
        transaction_id=tx_id,
        gas_limit=gas.get("gas_limit"),
        gas_estimate=gas.get("gas_estimate"),
        gas_clamped=gas.get("gas_clamped"),
        gas_source=gas.get("gas_source"),
        gas_cap=gas.get("gas_cap"),
        gas_hub_max=gas.get("gas_hub_max"),
    )
    notify("Mint submitted", f"{pick.slug}\n{shares:.0f} sets · {tx_id[:18]}…", priority="default")
    write_loop_heartbeat("mint", "submitted", slug=pick.slug)
    return "submitted"


def _reload_cfg(cfg_box: Dict[str, Any]) -> dict:
    """Reload strategy_mint.json only when its mtime or size changes.

    A missing stat still loads, so a replaced file is not stuck on the
    previous config. Hot reload stays: the next tick after a write sees
    the new stamp and picks the edit up. An unchanged file is not reread.
    """
    stamp = None
    try:
        info = STRATEGY_FILE.stat()
        stamp = (int(info.st_mtime_ns), int(info.st_size))
    except OSError:
        stamp = None
    cached = cfg_box.get("cfg")
    if isinstance(cached, dict) and stamp is not None and stamp == cfg_box.get("_mtime_ns"):
        return cached
    try:
        loaded = load_strategy()
    except Exception as exc:
        log_event("strategy_reload_fail", error=str(exc)[:200])
        current = cfg_box.get("cfg") or {}
        loaded = {**current, "entry_enabled": False}
    cfg_box["cfg"] = loaded
    if stamp is not None:
        cfg_box["_mtime_ns"] = stamp
    try:
        _apply_book_timeout(loaded)
    except NameError:
        pass
    return loaded


def submit_redeem(cfg: dict, chain: ChainReader, condition_id: str, approve_adapter: bool):
    """Relayer PROXY batch: optional CTF approval, then adapter.redeemPositions."""
    calls = build_redeem_calls(
        pUSD_address=str(cfg["pUSD_address"]),
        adapter_address=str(cfg["standard_adapter_address"]),
        ctf_address=str(cfg["ctf_address"]),
        condition_id=condition_id,
        approve_adapter=approve_adapter,
    )
    gas_margin, gas_fallback, gas_cap = mint_gas_settings(cfg)
    with RELAY_SUBMIT_LOCK:
        return submit_mint_batch(
            calls,
            metadata=f"mintbot:redeem:{condition_id}:{int(time.time())}",
            rpc=chain._rpc,
            gas_margin=gas_margin,
            gas_fallback=gas_fallback,
            gas_cap=gas_cap,
        )


def build_redeem_desk(
    cfg_box: Dict[str, Any],
    chain: ChainReader,
    gateway: MarketGateway,
    funder: str,
) -> RedeemDesk:
    def cfg() -> dict:
        return cfg_box.get("cfg") or {}

    def ctf() -> str:
        return str(cfg()["ctf_address"])

    io = RedeemIO(
        payout_denominator=lambda cid: chain.payout_denominator(ctf(), cid),
        payout_numerator=lambda cid, index: chain.payout_numerator(ctf(), cid, index),
        balance=lambda token: chain.position_balance(ctf(), funder, token),
        is_approved=lambda: chain.is_approved_for_all(
            ctf(), funder, str(cfg()["standard_adapter_address"])
        ),
        submit=lambda cid, approve: submit_redeem(cfg(), chain, cid, approve),
        relayer_status=lambda tx_id: get_relayer_transaction(str(cfg()["relayer_url"]), tx_id),
        log=log_event,
        notify=lambda title, message: notify(title, message, priority="high"),
        positions=lambda: gateway.redeemable_positions(funder),
    )
    return RedeemDesk(io, lock=STATE_LOCK, save=lambda s: commit_state(s, dirty=True))


def run_redeem_cycle(cfg: dict, state: dict, desk: Optional[RedeemDesk]) -> str:
    """Redeem tick on its own thread. Never touches the sell or mint loop.

    No heartbeat write: the heartbeat ``ts`` must keep tracking sell/mint.
    """
    if STOP_FILE.exists():
        return "stopped"
    if desk is None:
        return "no_funder"
    result = desk.tick(state, cfg, time.time())
    return str(result.get("status") or "ok")


def main() -> int:
    global _intent_store
    signal.signal(signal.SIGINT, _signal_handler)
    signal.signal(signal.SIGTERM, _signal_handler)
    log_setup()
    lock = acquire_lock()

    try:
        cfg = load_strategy()
        _apply_book_timeout(cfg)
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
                f"[dim]atomic mint · {sell_plan_banner(cfg)}[/]\n"
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
        loops=("sell", "mint", "oracle", "redeem"),
        oracle_log_enabled=bool(cfg.get("oracle_log_enabled", False)),
        sell_plan=sell_plan_banner(cfg),
        mint_sequential=seq_settings(cfg)[0],
        mint_seq_lead_s=seq_settings(cfg)[1],
        mint_seq_cutoff_s=seq_settings(cfg)[2],
        redeem_enabled=redeem_settings(cfg).enabled,
    )
    _whatsapp("startup", cfg)
    if cfg.get("oracle_log_enabled", False):
        late_s = float(cfg.get("sell_late_window_s") or 0.0)
        if late_s > 0:
            veto = f"[dim](+ late loser-scrap veto ≤{late_s:g}s TTM)[/]"
        else:
            veto = "[dim](audit only; scrap veto off)[/]"
        console.print(
            "[dim]▶ oracle tape[/] logs/oracle_twap.jsonl  " + veto
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
        enabled = bool(current.get("oracle_log_enabled", False))
        with STATE_LOCK:
            snap = snapshot_intents(state)
        oracle.tick(
            snap,
            time.time(),
            enabled=enabled,
            on_fail=lambda msg: log_event("oracle_log_fail", error=str(msg)[:240]),
            on_event=lambda name, fields: log_event(name, **fields),
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

    funder_env = os.getenv("FUNDER_ADDRESS") or ""
    redeem_desk = (
        build_redeem_desk(
            cfg_box,
            ChainReader(str(cfg["rpc_url"])),
            MarketGateway(
                gamma_url=str(cfg["gamma_url"]),
                data_api_url=str(cfg["data_api_url"]),
            ),
            to_checksum_address(funder_env),
        )
        if funder_env
        else None
    )

    def redeem_tick() -> None:
        current = cfg_box.get("cfg") or cfg
        status = run_redeem_cycle(current, state, redeem_desk)
        if status not in ("ok", "disabled", "stopped", "no_funder"):
            log_event("redeem_cycle", status=status)

    def redeem_sleep() -> float:
        current = cfg_box.get("cfg") or cfg
        settings = redeem_settings(current)
        return settings.poll_s if settings.enabled else 30.0

    redeem_thread = threading.Thread(
        target=run_job_loop,
        kwargs={
            "name": "redeem",
            "tick": redeem_tick,
            "sleep_s": redeem_sleep,
            "should_stop": should_stop,
            "on_error": lambda exc: log_event("redeem_cycle_error", error=str(exc)[:300]),
        },
        name="mintbot-redeem",
        daemon=True,
    )
    redeem_thread.start()
    while not should_stop():
        time.sleep(0.25)
    sell_thread.join(timeout=5)
    mint_thread.join(timeout=5)
    oracle_thread.join(timeout=5)
    redeem_thread.join(timeout=5)

    console.print("[dim]mintbot stopped[/]")
    try:
        lock.close()
    except Exception:
        pass
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
