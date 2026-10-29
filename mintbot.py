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
tick at the new bid. ``sell_scrap_min_px`` (default null) is a scrap-only
floor: null or 0 keeps this chase, and a positive price skips the post
while the live bid or the scrap limit is under it (no FAK lifted to the
floor). Dump, kept, stop, and hedge sells do not read it.
``sell_scrap_early_threshold`` (default null) leaves that path in place.
When it is set, the first reading inside the scrap window latches the bag:
a cheaper bid already under ``sell_scrap_min_px`` stays on
``sell_threshold`` / the late pair, ``sell_opposite_min_old`` (or
``sell_opposite_min``), and the blind FAK, and the floor does not apply.
Any other open latches the early threshold with
``sell_opposite_min_early``, keeps the floor, and does not fire a blind
FAK. That choice is logged once as ``sell_scrap_mode``.
``shadow_early_scrap_enabled`` (default false) is a paper log of that
early rule. It does not post, and it does not change the live scrap.
``sell_scrap_fraction`` defaults to 1 (the whole
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
(~2s, or 0 to poll again immediately, allowed below the ``poll_s >= 1``
floor) while a bag is sell-hot
(loser armed, or loser sold and dump/winner not done, or a reclaim is
still watching). Mint keeps ``poll_s``. Persist defaults are 5/2/60.
Live JSON keys that already exist (threshold, persist) override these
defaults until the operator edits them.

Opt-in reclaim (``reclaim_enabled``, default false): after a both-sides
dump, buy about ``reclaim_usd`` of the first side whose book holds at
``reclaim_entry`` for ``reclaim_entry_persist_s``. The other side confirms
when its best ask is at or under ``reclaim_other_max`` or, when that is
null, ``1 - reclaim_entry``. There is no
ask-depth gate. The FAK is a market buy at
``min(ask + reclaim_slippage, reclaim_max_price)`` (default 3¢ over the
ask, hard cap 96¢), sized off that limit, with USDC truncated to cents.
A top-up whose ask is already above the cap logs
``reclaim_topup_skipped_cap`` and is not posted. Every send, first or
retry, re-checks that tick's books (entry, sister ask, fresh uncrossed
book, spread, and ask at or under the cap). A failed check does not send
and starts the persist hold again. ``reclaim_min_ttm_s`` (default 0, off)
skips entry with ``too_close`` when seconds-to-close is under it. When
``reclaim_hedge_enabled`` is on, the other side is FAK-bought in the same
moment as the main buy (a parallel post, not after the fill). A zero
or partial hedge fill refires on that same thread, up to
``reclaim_hedge_retries`` inside about a second, at the fresh ask with
no added slippage, and only while that ask is at or under
``reclaim_hedge_max_price``. The first hedge order still pays
``reclaim_hedge_slippage`` over the ask. The main buy does
not wait on those refires. A hedge
miss does not cancel the main. After the stop fires, the hedge is kept
only if its bid stays above ``reclaim_hedge_keep_above`` for
``reclaim_hedge_keep_persist_s``; otherwise it uses the dump FAK refire.
No stop, or a kept hedge, rides to resolution. Stop-sell
at ``reclaim_stop`` (default on) or hold to redeem. A missing or stale bid
does not clear the stop clock. Each dump or stop refire refetches that
leg and sells at the live bid. While the reclaim is open, or a dump or
stop sell is in progress, the next sequential mint waits until the
position is closed or the bag's window has ended. The watch stays on
``sell_armed_poll_s`` until that finishes. A position still held at
resolution is redeemed by the existing redeem thread.

A third loop records Chainlink BTC/USD 60s TWAP (Polymarket RTDS) to
``logs/oracle_twap.jsonl`` while a 15m bag is open. ``oracle_log_enabled``
defaults on. Scrap, dump, reclaim, and bag-end lines copy that thread's
in-memory price and strike. They do not wait on the socket, and no
trading decision reads the tape. ``sell_late_window_s`` defaults to 0,
which skips the late-window loser-scrap veto entirely.
``sell_oracle_edge_floor_usd``, ``sell_oracle_edge_per_ttm``, and
``sell_oracle_stale_s`` also default to 0, so setting the window back
above 0 does not restore the old $25 / 1.5×TTM / 5s-stale veto.
``sell_oracle_edge_persist_s`` stays 3s. If the feed
fails, the loop logs ``oracle_log_fail`` and trading continues.

Usage:
  # dry-run (default when strategy_mint.json has dry_run true / entry_enabled false)
  python mintbot.py

Live requires strategy_mint.json with dry_run=false and entry_enabled=true.
Keep polybuybot* / polycomplement / DangerZone stopped. Do not start
pathlog_hourly_dense.
"""

from __future__ import annotations

import atexit
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
    ask_size_within,
    best_ask_with_min_size,
    best_bid_with_min_size,
    bid_fill_depth,
    book_age_s,
    book_top_levels,
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
    reclaim_hedge_cash_reserve,
    persist_digest,
    run_job_loop,
    select_mint_candidate,
    start_mint_sell_loops,
)
from buy.oracle_log import (
    OracleBagView,
    OracleLogService,
    oracle_mark_fields,
    snapshot_intents,
)
from buy.whatsapp_notify import BagAlerts, WhatsAppNotifier
from buy.mint_sell import (
    buy_fill_vwap,
    classify_loser,
    cycle_sleep_s,
    sell_intent_hot,
    ws_book_enabled,
    ws_book_mode,
    ws_book_stale_s,
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
    kept_dump_blocked,
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
    reclaim_hedge_keep_decision,
    reclaim_hedge_plan,
    reclaim_hedge_retry_allowed,
    reclaim_hedge_retry_plan,
    RECLAIM_HEDGE_RETRY_BUDGET_S,
    reclaim_stop_decision,
    reclaim_wallet_covers,
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
    apply_scrap_mode,
    lower_priced_side,
    scrap_active_prices,
    scrap_live_bid_limit,
    scrap_mode_for_open,
    scrap_mode_latch_due,
    scrap_limit_allowed,
    scrap_min_px,
    scrap_rest_px,
    scrap_time_gate_open,
    dump_refire_cap,
    dump_deep_limit,
    sell_dump_other_confirms,
    sell_fire_decision,
    sell_window_open,
    post_dump_book_sample,
    POST_DUMP_BOOK_INTERVAL_S,
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
    'fivemin_book_ws': False,
    'fivemin_book_ws_stale_s': 1.0,
    'fivemin_book_record': True,
    'fivemin_book_record_window_s': 150,
    'fivemin_poll_s': 0.2,
    'fivemin_active_window_s': 130,
    'fivemin_enabled': False,
    'fivemin_series_slug': 'btc-up-or-down-5m',
    'fivemin_binance_url': 'wss://stream.binance.com:9443/ws/btcusdt@aggTrade',
    'fivemin_btc_max_age_s': 3.0,
    'fivemin_loser_empty_as_zero': True,
    'fivemin_nofill_retry_s': 2.0,
    'fivemin_max_attempts_per_market': 3,
    'fivemin_shares': 10,
    'fivemin_buy_limit': 0.99,
    'fivemin_gate_k': 1.0,
    'fivemin_side98_enabled': False,
    'fivemin_momentum_block_enabled': False,
    'fivemin_momentum_gate_aware': False,
    'fivemin_momentum_max_adverse_30s_usd': 15.0,
    'fivemin_side98_shares': 20,
    'fivemin_side98_price': 0.98,
    'fivemin_side98_min_gap_usd': 15.0,
    'fivemin_side98_max_ttc_s': 60,
    'fivemin_side98_max_adverse_30s_usd': 5.0,
    'fivemin_loser_max': 0.1,
    'fivemin_loser_min': 0.01,
    'fivemin_fav_min': 0.9,
    'fivemin_max_ttm_s': 120,
    'fivemin_persist_s': 2.0,
    'fivemin_persist_last_s': 1.0,
    'fivemin_persist_last_window_s': 90,
    'fivemin_dump_held_below': 0.2,
    'fivemin_dump_other_min': 0.6,
    'fivemin_dump_persist_s': 3.0,
    'fivemin_dump_limit_offset': 0.2,
    'fivemin_dump_limit_floor': 0.05,
    'fivemin_min_notional_usd': 1.0,
    'fivemin_daily_stop_usd': -150.0,
    'fivemin_cash_buffer_usd': 5.0,
    'fivemin_book_stale_s': 30.0,
    'fivemin_rtds_silent_s': 20.0,
    'fivemin_dry_run': None,
    'fivemin_gap_source': 'twap',
    'fivemin_ttm_min_s': 0.0,
    'sniper_5m_enabled': False,
    'sniper_jump_usd': 25.0,
    'sniper_window_ms': 500,
    'sniper_min_gap_usd': 20.0,
    'sniper_max_ask': 0.97,
    'sniper_min_ask': 0.5,
    'sniper_ttm_min_s': 15,
    'sniper_ttm_max_s': 240,
    'sniper_shares': 10,
    'sniper_use_offset': True,
    'sniper_event_driven': True,
    'sniper_futures_ws_enabled': False,
    'sniper_futures_ws_url': 'wss://fstream.binance.com/stream?streams=btcusdt@aggTrade/btcusdt@bookTicker',
    'sniper_extra_ws_enabled': True,
    'sniper_extra_ws_urls': [
        'wss://stream.binance.com:9443/ws/btcusdt@bookTicker',
        'wss://stream.bybit.com/v5/public/linear',
        'wss://ws-feed.exchange.coinbase.com',
        'wss://www.deribit.com/ws/api/v2',
        'wss://ws.kraken.com/v2',
        'wss://ws.bitstamp.net',
    ],
    'sniper_max_book_age_ms': 1500.0,
    'fivemin_min_fill_px': 0.95,
    'fivemin_low_fill_exit': True,
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
    # Off until set. shadow (the default when on) still decides on REST
    # and logs the websocket difference. live uses the websocket bid
    # and logs REST on the following tick. Stale or crossed falls back
    # to the same REST poll as today.
    "ws_book_enabled": False,
    "ws_book_mode": "shadow",
    "ws_book_stale_s": 1.5,
    # Wallet pUSD while a sequential mint is waiting on cash.
    "mint_cash_poll_s": 2.0,
    # Chainlink 60s TWAP tape. The scrap veto is off unless
    # sell_late_window_s is positive.
    "oracle_log_enabled": True,
    # Last 360s of each open bag: one JSON line per sell tick in
    # logs/price_tape.jsonl. Off the journal. Does not place an order.
    "price_tape_enabled": True,
    "price_tape_window_s": 360.0,
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
    # Null or 0: loser scrap may post at the live bid down to 1c.
    # A positive price skips the scrap while the bid is under it.
    # Dump, kept, reclaim stop, and hedge sells ignore this.
    "sell_scrap_min_px": None,
    # Null: no per-bag latch. A positive price is the early-scrap arm.
    "sell_scrap_early_threshold": None,
    "sell_opposite_min_early": 0.75,
    # Null uses sell_opposite_min for a bag latched to the old rules.
    "sell_opposite_min_old": None,
    # Paper log only. False leaves the sell loop unchanged.
    "shadow_early_scrap_enabled": False,
    "shadow_early_threshold": 0.20,
    "shadow_early_min_px": 0.15,
    "shadow_early_opposite_min": 0.75,
    "shadow_early_fraction": 0.5,
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
    "sell_dump_dual_enabled": False,
    "sell_dump_dual_persist_s": 0.5,
    "sell_dump_dual_oracle_usd": 15.0,
    # Dump persist in the last window seconds; None follows sell_dump_persist_s.
    # Window 0 = off.
    "sell_dump_persist_last_min_s": None,
    "sell_dump_persist_last_min_window_s": 0.0,
    # 0 or null: after the dump fires, refire until sold or the window ends.
    "sell_dump_fak_retries": 0,
    "sell_dump_ladder_step": 0.04,
    "sell_dump_ladder_rungs": 4,
    # 0 disables the time-left gate (old behavior). The example sets 240:
    # arm and fire the held dump only when seconds-to-close is at or under this.
    "sell_dump_max_ttm_s": 0.0,
    # Other side's bid must stay at least this for the whole persist.
    # 0 or null disables. A dip resets sell_dump_armed_at.
    "sell_dump_other_min": 0.30,
    # When the held dump fills, also sell the kept scrap half (1c floor sweep).
    "sell_dump_also_kept": False,
    # Null sells that kept half whenever the held dump fires. A price in
    # (0, 1) sells it only when its best bid is strictly under that price
    # at the moment the kept sell starts. At or above, hold to redeem.
    "sell_dump_kept_max_px": None,
    # With also_kept, post the kept leg at the same time as the held leg.
    # False restores the old order: kept starts only after the held dump fills.
    "sell_dump_parallel_kept": True,
    # After sell_dump_done, journal both books until the window ends.
    "post_dump_log_enabled": True,
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
    # None keeps the other-side ceiling at 1 - reclaim_entry.
    "reclaim_other_max": None,
    # Cheap-side hedge, posted at the same time as the reclaim buy. Off.
    "reclaim_hedge_enabled": False,
    "reclaim_hedge_usd": 1.0,
    "reclaim_hedge_slippage": 0.02,
    "reclaim_hedge_keep_above": 0.50,
    "reclaim_hedge_keep_persist_s": 1.0,
    "reclaim_hedge_retries": 3,
    "reclaim_hedge_max_price": 0.15,
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
    "state_prune_completed_after_s": 21600,
    "rpc_url": "https://polygon.drpc.org",
    "gamma_url": "https://gamma-api.polymarket.com",
    "data_api_url": "https://data-api.polymarket.com",
    "relayer_url": "https://relayer-v2.polymarket.com",
    "chain_id": 137,
    "pUSD_address": "0xC011a7E12a19f7B1f670d46F03B03f3342E82DFB",
    "ctf_address": "0x4D97DCd97eC945f40cF65F87097ACe5EA0476045",
    "standard_adapter_address": "0xAdA100Db00Ca00073811820692005400218FcE1f",
    "presign_enabled": True,
    "presign_ttl_s": 240.0,
    "clob_keepalive_always_on": True,
    "clob_keepalive_interval_s": 12.0,
    "clob_probe_fastest_edge": False,
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
# Detached digest of the latest submitted snapshot (or synchronous save).
# A writer failure invalidates it so the next tick submits fresh state.
_saved_persist_form: Any = None
_STATE_WRITER = None
_HEARTBEAT_WRITER = None
_LOG_LISTENER = None
_OUTPUT_POOL = None
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


def _oracle_mark(condition_id: str, cfg: Optional[dict], now: float) -> dict:
    """Cache read for a log line. Disabled or any failure stays off the order."""
    try:
        enabled = True
        if isinstance(cfg, dict):
            enabled = bool(cfg.get("oracle_log_enabled", True))
        if not enabled:
            return oracle_mark_fields(None, now_s=now, enabled=False)
        return oracle_mark_fields(
            _oracle_bag_view(condition_id), now_s=now, enabled=True,
        )
    except Exception:
        return oracle_mark_fields(None, now_s=now, enabled=True)


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
    import queue
    from logging.handlers import QueueHandler, QueueListener

    global _LOG_LISTENER
    from buy.log_archive import ArchiveRotatingFileHandler

    logger = logging.getLogger("mintbot")
    logger.setLevel(logging.INFO)
    logger.handlers.clear()
    fmt = logging.Formatter("%(message)s")
    # Live file still rolls at 2 MB. Rotated bytes move to logs/archive
    # and are gzipped off the trading loop. Nothing in the archive is pruned.
    fh = ArchiveRotatingFileHandler(LOG_FILE, maxBytes=2_000_000)
    fh.setFormatter(fmt)
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    records = queue.Queue()
    logger.addHandler(QueueHandler(records))
    logger.propagate = False
    _LOG_LISTENER = QueueListener(records, fh, sh)
    _LOG_LISTENER.start()

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
    """Record a detached digest of the saved or submitted snapshot."""
    global _saved_persist_form
    from buy.state_writer import snapshot_state

    _saved_persist_form = persist_digest(snapshot_state(payload if isinstance(payload, dict) else {}))


def persist_dirty(state: dict) -> bool:
    """True when ``state`` differs from the latest submitted snapshot.

    Compares the digest: live intents in full, terminal intents as id plus
    status, and any top-level keys other than ``intents``.
    """
    return persist_digest(state if isinstance(state, dict) else {}) != _saved_persist_form


def commit_state(state: dict, *, dirty: bool = False, durable: bool = False):
    """Submit detached state; durable callers holding STATE_LOCK wait after release.

    Returns a sequence for a deferred durable wait, True for a queued save,
    or False when unchanged. Tests without a writer retain synchronous saves.
    """
    global _saved_persist_form
    writer = globals().get("_STATE_WRITER")
    if writer is None:
        if not dirty and not persist_dirty(state):
            return False
        atomic_save(STATE_FILE, state)
        return True
    from buy.state_writer import snapshot_state

    already_owned = STATE_LOCK._is_owned()
    with STATE_LOCK:
        if not dirty and not persist_dirty(state):
            return False
        snapshot = snapshot_state(state)
        _saved_persist_form = persist_digest(snapshot)
        seq = writer.submit(snapshot)
        if not seq:
            _saved_persist_form = None
            return False
    if durable:
        if already_owned:
            return seq
        if not writer.wait(seq):
            raise OSError("durable state save failed")
    return True


def _state_save_error(seq: int, exc: Exception) -> None:
    global _saved_persist_form
    with STATE_LOCK:
        _saved_persist_form = None


def _wait_state_save(seq) -> None:
    writer = globals().get("_STATE_WRITER")
    if writer is not None and seq and not writer.wait(seq):
        raise OSError("durable state save failed")


def _prune_state_tick(state: dict, cfg: dict, now: float, *, force: bool = False) -> None:
    writer = globals().get("_STATE_WRITER")
    if writer is None:
        return
    last = getattr(_prune_state_tick, "last", None)
    if not force and last is not None and now - last < 600:
        return
    _prune_state_tick.last = now
    from buy.state_prune import prune_state
    from buy.state_writer import snapshot_state

    with STATE_LOCK:
        rows = prune_state(state, now, cfg.get("state_prune_completed_after_s", 21600))
        if not rows:
            return
        snapshot = snapshot_state(state)
        global _saved_persist_form
        _saved_persist_form = persist_digest(snapshot)
        writer.submit(snapshot, archive_rows=rows)


def _console_print(*args, **kwargs) -> None:
    pool = globals().get("_OUTPUT_POOL")
    if pool is None:
        console.print(*args, **kwargs)
    else:
        pool.submit(console.print, *args, **kwargs)


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
    # Optional 5m configuration validates without importing its runtime module.
    if not isinstance(cfg.get("fivemin_enabled", False), bool):
        raise ValueError("fivemin_enabled must be boolean")
    if not isinstance(cfg.get("fivemin_side98_enabled", False), bool):
        raise ValueError("fivemin_side98_enabled must be boolean")
    if not isinstance(cfg.get("fivemin_loser_empty_as_zero", True), bool):
        raise ValueError("fivemin_loser_empty_as_zero must be boolean")
    if not isinstance(cfg.get("fivemin_momentum_block_enabled", False), bool):
        raise ValueError("fivemin_momentum_block_enabled must be boolean")
    if not isinstance(cfg.get("fivemin_momentum_gate_aware", False), bool):
        raise ValueError("fivemin_momentum_gate_aware must be boolean")
    if cfg.get("fivemin_dry_run") is not None and not isinstance(cfg["fivemin_dry_run"], bool):
        raise ValueError("fivemin_dry_run must be boolean or null")
    if not str(cfg.get("fivemin_series_slug", "btc-up-or-down-5m")).strip():
        raise ValueError("fivemin_series_slug must not be empty")
    if not str(cfg.get("fivemin_binance_url", "")).strip():
        raise ValueError("fivemin_binance_url must not be empty")
    if not isinstance(cfg.get("fivemin_book_ws", False), bool):
        raise ValueError("fivemin_book_ws must be boolean")
    if not isinstance(cfg.get("fivemin_book_record", True), bool):
        raise ValueError("fivemin_book_record must be boolean")
    if not 0 < float(cfg.get("fivemin_book_ws_stale_s", 1.0)) < float("inf"):
        raise ValueError("fivemin_book_ws_stale_s must be positive and finite")
    for key, default, low, high in (
        ("fivemin_nofill_retry_s", 2.0, 0, None),
        ("fivemin_max_attempts_per_market", 3, 1, 20),
        ("fivemin_rtds_silent_s", 20.0, 1, None),
        ("fivemin_poll_s", 0.2, 0.1, None), ("fivemin_active_window_s", 130, 0, 300),
        ("fivemin_shares", 10, 1, 200), ("fivemin_buy_limit", 0.99, 0.01, 0.995),
        ("fivemin_gate_k", 1.0, 0, None), ("fivemin_side98_shares", 20, 1, 200),
        ("fivemin_side98_price", 0.98, 0.01, 0.99),
        ("fivemin_side98_min_gap_usd", 15.0, 0, None),
        ("fivemin_side98_max_ttc_s", 60, 0, 300),
        ("fivemin_side98_max_adverse_30s_usd", 5.0, 0, None),
        ("fivemin_momentum_max_adverse_30s_usd", 15.0, 0, None),
        ("fivemin_loser_max", 0.10, 0, 1),
        ("fivemin_loser_min", 0.01, 0, 1), ("fivemin_fav_min", 0.90, 0, 1),
        ("fivemin_max_ttm_s", 120, 0, 300), ("fivemin_persist_s", 2, 0, None),
        ("fivemin_persist_last_s", 1, 0, None), ("fivemin_persist_last_window_s", 90, 0, 300),
        ("fivemin_dump_held_below", 0.20, 0, 1), ("fivemin_dump_other_min", 0.60, 0, 1),
        ("fivemin_dump_persist_s", 3, 0, None), ("fivemin_dump_limit_offset", 0.20, 0, 1),
        ("fivemin_dump_limit_floor", 0.05, 0.01, 0.99),
        ("fivemin_min_notional_usd", 1, 0, None), ("fivemin_daily_stop_usd", -150, None, 0),
        ("fivemin_cash_buffer_usd", 5, 0, None), ("fivemin_book_stale_s", 30, 0.01, None), ("fivemin_btc_max_age_s", 3.0, 0.01, None),
        ("fivemin_book_record_window_s", 150, 1, 300),
    ):
        value = float(cfg.get(key, default))
        if not (value == value and value not in (float("inf"), float("-inf"))) or (low is not None and value < low) or (high is not None and value > high):
            raise ValueError(f"invalid {key}")
        if key == "fivemin_max_attempts_per_market" and (isinstance(cfg.get(key, default), bool) or not value.is_integer()):
            raise ValueError(f"invalid {key}: must be int")
    if float(cfg.get("fivemin_loser_min", 0.01)) > float(cfg.get("fivemin_loser_max", 0.10)):
        raise ValueError("fivemin_loser_min must be <= fivemin_loser_max")
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
    armed_raw = cfg.get("sell_armed_poll_s")
    try:
        armed_poll = float(armed_raw)
    except (TypeError, ValueError):
        raise ValueError("sell_armed_poll_s must be >= 0")
    if armed_poll != armed_poll or armed_poll < 0 or armed_poll == float("inf"):
        raise ValueError("sell_armed_poll_s must be >= 0")
    if (
        cfg.get("ws_book_enabled") is True
        and str(cfg.get("ws_book_mode") or "shadow").strip().lower() == "live"
        and armed_poll < 0.05
    ):
        raise ValueError("ws_book_enabled requires sell_armed_poll_s >= 0.05")
    mode_raw = cfg.get("ws_book_mode", None)
    if mode_raw is not None and str(mode_raw).strip() != "":
        if str(mode_raw).strip().lower() not in ("shadow", "live"):
            raise ValueError("ws_book_mode must be shadow or live")
    stale_raw = cfg.get("ws_book_stale_s", 1.5)
    try:
        ws_stale = float(stale_raw)
    except (TypeError, ValueError):
        raise ValueError("ws_book_stale_s must be > 0")
    if ws_stale != ws_stale or ws_stale <= 0 or ws_stale == float("inf"):
        raise ValueError("ws_book_stale_s must be > 0")
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
    kept_max_raw = cfg.get("sell_dump_kept_max_px", None)
    if kept_max_raw not in (None, ""):
        try:
            kept_max = float(kept_max_raw)
        except (TypeError, ValueError):
            raise ValueError("sell_dump_kept_max_px must be null or in (0, 1)")
        if (
            kept_max != kept_max
            or kept_max <= 0
            or kept_max >= 1
            or kept_max == float("inf")
        ):
            raise ValueError("sell_dump_kept_max_px must be null or in (0, 1)")
    other_min_raw = cfg.get("sell_dump_other_min", 0.30)
    if other_min_raw is not None and other_min_raw != "":
        try:
            other_min = float(other_min_raw)
        except (TypeError, ValueError):
            raise ValueError("sell_dump_other_min must be null, 0, or 0 < price < 1")
        if (
            other_min != other_min
            or other_min in (float("inf"), float("-inf"))
            or other_min < 0
            or other_min >= 1
        ):
            raise ValueError("sell_dump_other_min must be null, 0, or 0 < price < 1")
    if float(cfg.get("sell_scrap_max_ttm_s") or 0) < 0:
        raise ValueError("sell_scrap_max_ttm_s must be >= 0")
    try:
        scrap_fraction = float(cfg.get("sell_scrap_fraction", 1.0))
    except (TypeError, ValueError):
        raise ValueError("sell_scrap_fraction must be > 0 and <= 1")
    # NaN fails the ordered compare. Inf is > 1.
    if not (0 < scrap_fraction <= 1) or scrap_fraction != scrap_fraction:
        raise ValueError("sell_scrap_fraction must be > 0 and <= 1")
    scrap_floor_raw = cfg.get("sell_scrap_min_px", None)
    if scrap_floor_raw is not None and scrap_floor_raw != "":
        try:
            scrap_floor_px = float(scrap_floor_raw)
        except (TypeError, ValueError):
            raise ValueError("sell_scrap_min_px must be null, 0, or 0 < price < 1")
        if (
            scrap_floor_px != scrap_floor_px
            or scrap_floor_px in (float("inf"), float("-inf"))
            or scrap_floor_px < 0
            or scrap_floor_px >= 1
        ):
            raise ValueError("sell_scrap_min_px must be null, 0, or 0 < price < 1")
    for px_name in (
        "sell_scrap_early_threshold",
        "sell_opposite_min_early",
        "sell_opposite_min_old",
    ):
        px_raw = cfg.get(px_name, None)
        if px_raw is None or px_raw == "":
            continue
        try:
            px_value = float(px_raw)
        except (TypeError, ValueError):
            raise ValueError(f"{px_name} must be null, 0, or 0 < price < 1")
        if (
            px_value != px_value
            or px_value in (float("inf"), float("-inf"))
            or px_value < 0
            or px_value >= 1
        ):
            raise ValueError(f"{px_name} must be null, 0, or 0 < price < 1")
    for px_name in (
        "shadow_early_threshold",
        "shadow_early_min_px",
        "shadow_early_opposite_min",
    ):
        px_raw = cfg.get(px_name, None)
        if px_raw is None or px_raw == "":
            continue
        try:
            px_value = float(px_raw)
        except (TypeError, ValueError):
            raise ValueError(f"{px_name} must be empty or 0 < price < 1")
        if (
            px_value != px_value
            or px_value in (float("inf"), float("-inf"))
            or not (0 < px_value < 1)
        ):
            raise ValueError(f"{px_name} must be empty or 0 < price < 1")
    fraction_raw = cfg.get("shadow_early_fraction", 0.5)
    if fraction_raw is not None and fraction_raw != "":
        try:
            shadow_fraction = float(fraction_raw)
        except (TypeError, ValueError):
            raise ValueError("shadow_early_fraction must be empty or 0 < fraction <= 1")
        if (
            shadow_fraction != shadow_fraction
            or shadow_fraction in (float("inf"), float("-inf"))
            or not (0 < shadow_fraction <= 1)
        ):
            raise ValueError("shadow_early_fraction must be empty or 0 < fraction <= 1")
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
    # sell_dump_persist_s. Negatives are rejected. sell_armed_poll_s is
    # its own cadence and may be 0; that is not a persist.
    for key in (
        "reclaim_entry_persist_s",
        "reclaim_stop_persist_s",
        "reclaim_max_ttm_s",
        "reclaim_min_ttm_s",
        "reclaim_hedge_keep_persist_s",
    ):
        raw = cfg.get(key)
        if raw is not None and float(raw) < 0:
            raise ValueError(f"{key} must be >= 0")
    other_max = cfg.get("reclaim_other_max", None)
    if other_max is not None and other_max != "":
        try:
            other_cap = float(other_max)
        except (TypeError, ValueError):
            raise ValueError("reclaim_other_max must be null or 0 < price < 1")
        if (
            other_cap != other_cap
            or other_cap in (float("inf"), float("-inf"))
            or not (0 < other_cap < 1)
        ):
            raise ValueError("reclaim_other_max must be null or 0 < price < 1")
    try:
        hedge_usd = float(cfg.get("reclaim_hedge_usd", 1.0))
        hedge_slip = float(cfg.get("reclaim_hedge_slippage", 0.02))
        hedge_keep = float(cfg.get("reclaim_hedge_keep_above", 0.50))
        hedge_cap = float(cfg.get("reclaim_hedge_max_price", 0.15))
        hedge_retries = int(cfg.get("reclaim_hedge_retries", 3))
    except (TypeError, ValueError):
        raise ValueError("reclaim hedge knobs must be numeric")
    if hedge_usd != hedge_usd or hedge_usd <= 0 or hedge_usd == float("inf"):
        raise ValueError("reclaim_hedge_usd must be > 0")
    if hedge_slip != hedge_slip or hedge_slip < 0 or hedge_slip == float("inf"):
        raise ValueError("reclaim_hedge_slippage must be >= 0")
    if (
        hedge_keep != hedge_keep
        or hedge_keep in (float("inf"), float("-inf"))
        or not (0 < hedge_keep < 1)
    ):
        raise ValueError("0 < reclaim_hedge_keep_above < 1 must hold")
    if (
        hedge_cap != hedge_cap
        or hedge_cap in (float("inf"), float("-inf"))
        or not (0 < hedge_cap < 1)
    ):
        raise ValueError("0 < reclaim_hedge_max_price < 1 must hold")
    if hedge_retries < 0:
        raise ValueError("reclaim_hedge_retries must be >= 0")

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
            if globals().get("_STATE_WRITER") is None:
                atomic_save(STATE_FILE, state)
            else:
                seq = commit_state(state, dirty=True, durable=True)
    if dirty and globals().get("_STATE_WRITER") is not None:
        _wait_state_save(seq)
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
                    globals().get("_console_print", console.print)(
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
        writer = globals().get("_HEARTBEAT_WRITER")
        if writer is not None:
            writer.submit(payload)
            return
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
_fak_market_cache: dict[str, tuple[str, bool]] = {}
_fak_warmup_attempted: dict[str, None] = {}
_fak_warmup_lock = threading.Lock()
_fak_warmup_client_attempted = False
_fak_warmup_warn_at = None
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


def _fetch_book(token_id: str, min_size: float, fail: Optional[dict] = None):
    """REST `/book` → sized bid and ask, raw levels, and book age if stamped.

    A failure sets ``fail['failed']`` when the caller passed a dict. The
    note itself stays on the sell thread: these reads run on the pool.
    """
    try:
        response = thread_session("clob_book").get(
            "https://clob.polymarket.com/book",
            params={"token_id": str(token_id)},
            timeout=_book_timeout_s(),
        )
        if response.status_code != 200:
            if isinstance(fail, dict):
                fail["failed"] = True
                fail["error"] = f"http_{response.status_code}"
            return None, 0.0, [], None, 0.0, [], None
        return _parse_book_payload(response.json(), min_size)
    except Exception as exc:
        if isinstance(fail, dict):
            fail["failed"] = True
            fail["error"] = str(exc)
        log_event("book_fetch_fail", token_id=str(token_id)[:18], error=str(exc)[:160])
        return None, 0.0, [], None, 0.0, [], None


def _fetch_books_parallel(up_tok: str, dn_tok: str, min_size: float, fail: Optional[dict] = None):
    up_fail, dn_fail = {}, {}
    fut_up = _book_pool.submit(_fetch_book, up_tok, min_size, up_fail)
    fut_dn = _book_pool.submit(_fetch_book, dn_tok, min_size, dn_fail)
    rows = fut_up.result(), fut_dn.result()
    if isinstance(fail, dict):
        for side, side_fail in (("up", up_fail), ("down", dn_fail)):
            if side_fail.get("failed"):
                fail["failed"] = True
                fail[side] = side_fail["error"]
                fail["error"] = side_fail["error"]
    return rows


def _fetch_books(up_tok: str, dn_tok: str, min_size: float, fail: Optional[dict] = None):
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
        rows = (
            _parse_book_payload(up_book, min_size),
            _parse_book_payload(dn_book, min_size),
        )
        _note_book_fetch_ok()
        return rows
    except Exception as exc:
        if isinstance(exc, (requests.Timeout, TimeoutError)):
            if isinstance(fail, dict):
                fail.update(failed=True, error=str(exc), up="timeout", down="timeout")
            _note_book_fetch_failed()
            log_event(
                "book_fetch_fail",
                token_id="books",
                error=str(exc)[:160],
            )
            empty = (None, 0.0, [], None, 0.0, [], None)
            return empty, empty
        pair_fail = {}
        rows = _fetch_books_parallel(up_tok, dn_tok, min_size, pair_fail)
        if isinstance(fail, dict):
            fail.update(pair_fail)
        if pair_fail.get("failed"):
            _note_book_fetch_failed()
        else:
            _note_book_fetch_ok()
        return rows


def _book_pace():
    """Per-thread book pacing. Created here so extracted tests see it.

    A pool worker's note stays on that worker. The sell thread notes
    once per ``_fetch_books`` call, so a 429 cannot be cleared by another
    thread and cannot be counted twice for one pair.
    """
    local = globals().get("_BOOK_PACE_LOCAL")
    if local is None:
        local = threading.local()
        globals()["_BOOK_PACE_LOCAL"] = local
    state = getattr(local, "state", None)
    if state is None:
        state = {
            "streak": 0,
            "backoff": 0.0,
            "ws_tick": False,
            "missing_token": False,
        }
        local.state = state
    return state


def _note_book_fetch_failed() -> None:
    from buy.mint_sell import book_fail_backoff

    state = _book_pace()
    streak = int(state.get("streak") or 0) + 1
    state["streak"] = streak
    state["backoff"] = book_fail_backoff(streak)


def _note_book_fetch_ok() -> None:
    state = _book_pace()
    state["streak"] = 0
    state["backoff"] = 0.0


def _note_ws_book_tick() -> None:
    _book_pace()["ws_tick"] = True


def _note_missing_token() -> None:
    _book_pace()["missing_token"] = True


def take_book_pace() -> tuple:
    """``(backoff, ws_tick, missing_token)`` for this thread's sell sleep.

    The failure streak stays until a successful book. The backoff value
    and the two flags are consumed.
    """
    state = _book_pace()
    try:
        backoff = float(state.get("backoff") or 0.0)
    except (TypeError, ValueError):
        backoff = 0.0
    ws_tick = bool(state.get("ws_tick"))
    missing = bool(state.get("missing_token"))
    state["backoff"] = 0.0
    state["ws_tick"] = False
    state["missing_token"] = False
    return backoff, ws_tick, missing


def _book_feed_box() -> dict:
    box = globals().get("_BOOK_FEED_BOX")
    if box is None:
        box = {"feed": None}
        globals()["_BOOK_FEED_BOX"] = box
    return box


def _set_book_tick(meta: dict) -> None:
    globals()["_BOOK_TICK"] = meta


def _sync_book_feed(cfg: dict, state: dict) -> None:
    """Subscribe the current bag and the next. Off leaves the socket down."""
    if not ws_book_enabled(cfg):
        box = globals().get("_BOOK_FEED_BOX")
        feed = box.get("feed") if isinstance(box, dict) else None
        if feed is not None:
            feed.stop()
        return
    from buy.clob_book_ws import ClobBookFeed, watch_token_ids

    box = _book_feed_box()
    feed = box.get("feed")
    if feed is None:
        feed = ClobBookFeed(
            on_error=lambda msg, delay: log_event(
                "ws_book_error", error=str(msg)[:200], backoff_s=delay,
            ),
        )
        box["feed"] = feed
    feed.set_stale_s(ws_book_stale_s(cfg))
    feed.set_min_size(float(cfg.get("sell_min_bid_size") or 1.0))
    feed.set_tokens(watch_token_ids(state.get("intents") or {}, time.time()))
    feed.start()


def _mono_now() -> float:
    fn = getattr(time, "monotonic", None)
    if not callable(fn):
        return float(time.time())
    return float(fn())


def _books_for_sell(cfg: dict, up_tok: str, dn_tok: str, min_size: float):
    """Book rows for one sell tick.

    Disabled is REST, unchanged. ``shadow`` (the default when the socket
    is on) also decides on REST and records the websocket diff. ``live``
    uses the websocket when both legs are fresh and not crossed, and
    leaves the REST comparison for the next tick.
    """
    if not ws_book_enabled(cfg):
        up_row, dn_row = _fetch_books(up_tok, dn_tok, min_size)
        return up_row, dn_row, {"source": "rest", "legs": {}}
    from buy.clob_book_ws import ws_book_row

    mode = ws_book_mode(cfg)
    feed = _book_feed_box().get("feed")
    up_top = feed.usable(up_tok) if feed is not None else None
    dn_top = feed.usable(dn_tok) if feed is not None else None
    both = up_top is not None and dn_top is not None
    if mode == "live" and both:
        _note_book_fetch_ok()
        _note_ws_book_tick()
        ages = [up_top.age, dn_top.age]
        known = [age for age in ages if age is not None]
        meta = {
            "source": "ws",
            "legs": {
                "up": {"ws": up_top.bid, "rest": None},
                "dn": {"ws": dn_top.bid, "rest": None},
            },
            "rest_fetch_s": None,
            "lead_s": None,
            "ws_age_s": max(known) if known else None,
        }
        return ws_book_row(up_top), ws_book_row(dn_top), meta
    started = _mono_now()
    up_row, dn_row = _fetch_books(up_tok, dn_tok, min_size)
    elapsed = max(0.0, _mono_now() - started)
    up_bid = up_row[0] if isinstance(up_row, (tuple, list)) and up_row else None
    dn_bid = dn_row[0] if isinstance(dn_row, (tuple, list)) and dn_row else None
    if feed is not None:
        feed.remember_rest(up_tok, up_bid)
        feed.remember_rest(dn_tok, dn_bid)
        legs = {
            "up": {"ws": feed.cached_bid(up_tok), "rest": up_bid},
            "dn": {"ws": feed.cached_bid(dn_tok), "rest": dn_bid},
        }
    else:
        legs = {
            "up": {"ws": None, "rest": up_bid},
            "dn": {"ws": None, "rest": dn_bid},
        }
    ages = []
    if up_top is not None and up_top.age is not None:
        ages.append(up_top.age)
    if dn_top is not None and dn_top.age is not None:
        ages.append(dn_top.age)
    meta = {
        "source": "rest",
        "legs": legs,
        "shadow": mode == "shadow",
        "rest_fetch_s": elapsed,
        "lead_s": elapsed if both else None,
        "ws_age_s": max(ages) if ages else None,
    }
    return up_row, dn_row, meta


def _log_book_source(
    cfg: dict,
    *,
    path: str,
    leg: Any,
    bid: Any,
    cid: Any,
    slug: Any,
) -> None:
    """One line when a scrap, dump, kept, or reclaim trigger uses a book.

    Disabled stays silent so the journal matches today.
    """
    if not ws_book_enabled(cfg):
        return
    from buy.clob_book_ws import book_diff_fields

    meta = globals().get("_BOOK_TICK") or {}
    legs = meta.get("legs") if isinstance(meta, dict) else {}
    side = legs.get(leg) if isinstance(legs, dict) else None
    if not isinstance(side, dict):
        side = {}
    extra = {}
    if isinstance(meta, dict):
        for key in ("rest_fetch_s", "lead_s", "ws_age_s"):
            if key in meta:
                extra[key] = meta.get(key)
    log_event(
        "book_source",
        condition_id=cid,
        slug=slug,
        path=path,
        leg=leg,
        bid=bid,
        book_source=meta.get("source") if isinstance(meta, dict) else None,
        **book_diff_fields(side.get("ws"), side.get("rest")),
        **extra,
    )


def _log_book_shadow(cid: Any, slug: Any, meta: dict) -> None:
    """One comparison per bag per tick while the socket is in shadow."""
    if not isinstance(meta, dict) or not meta.get("shadow"):
        return
    from buy.clob_book_ws import book_diff_fields

    legs = meta.get("legs") if isinstance(meta.get("legs"), dict) else {}
    up = legs.get("up") if isinstance(legs.get("up"), dict) else {}
    dn = legs.get("dn") if isinstance(legs.get("dn"), dict) else {}
    up_diff = book_diff_fields(up.get("ws"), up.get("rest"))
    dn_diff = book_diff_fields(dn.get("ws"), dn.get("rest"))
    log_event(
        "book_shadow",
        condition_id=cid,
        slug=slug,
        rest_fetch_s=meta.get("rest_fetch_s"),
        lead_s=meta.get("lead_s"),
        ws_age_s=meta.get("ws_age_s"),
        up_ws=up_diff["ws_bid"],
        up_rest=up_diff["rest_bid"],
        up_diff=up_diff["bid_diff"],
        dn_ws=dn_diff["ws_bid"],
        dn_rest=dn_diff["rest_bid"],
        dn_diff=dn_diff["bid_diff"],
    )


def _follow_box() -> dict:
    box = globals().get("_WS_FOLLOW_BOX")
    if not isinstance(box, dict):
        box = {"pending": [], "next": []}
        globals()["_WS_FOLLOW_BOX"] = box
    return box


def _queue_live_followup(item: dict) -> None:
    box = _follow_box()
    nxt = box.get("next")
    if not isinstance(nxt, list):
        nxt = []
        box["next"] = nxt
    nxt.append(item)


def _flush_deferred_rest(cfg: dict) -> None:
    """Log the previous live tick's websocket bids against REST.

    The order on that tick already used the websocket. This fetch runs
    after the orders, on the following pass, so it cannot delay them.
    """
    box = _follow_box()
    pending = box.get("pending") if isinstance(box.get("pending"), list) else []
    nxt = box.get("next") if isinstance(box.get("next"), list) else []
    box["pending"] = list(nxt)
    box["next"] = []
    if ws_book_mode(cfg) != "live":
        box["pending"] = []
        return
    for item in pending:
        if not isinstance(item, dict):
            continue
        up_tok = str(item.get("up_tok") or "")
        dn_tok = str(item.get("dn_tok") or "")
        if not up_tok or not dn_tok:
            continue
        try:
            min_size = float(item.get("min_size") or 1.0)
        except (TypeError, ValueError):
            min_size = 1.0
        up_row, dn_row = _fetch_books(up_tok, dn_tok, min_size)
        rows = {"up": up_row, "dn": dn_row}
        ws_bids = {"up": item.get("up_ws"), "dn": item.get("dn_ws")}
        from buy.clob_book_ws import book_diff_fields

        for leg in ("up", "dn"):
            row = rows[leg]
            rest = row[0] if isinstance(row, (tuple, list)) and row else None
            log_event(
                "book_source",
                condition_id=item.get("cid"),
                slug=item.get("slug"),
                path="ws_followup",
                leg=leg,
                bid=ws_bids[leg],
                book_source="ws",
                **book_diff_fields(ws_bids[leg], rest),
            )


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

def _configure_clob_http_client() -> Any:
    """Install one HTTP/2 client for the CLOB helper module at startup."""
    import httpx
    from py_clob_client_v2.http_helpers import helpers

    try:
        from buy.presign import (
            configure_low_latency_sockets,
            select_fastest_cf_edge,
            set_order_http_client,
        )

        configure_low_latency_sockets()
        if os.getenv("CLOB_PROBE_CF_EDGE", "1") == "1":
            select_fastest_cf_edge()
    except Exception:
        set_order_http_client = None  # type: ignore
    client = httpx.Client(
        http2=True,
        limits=httpx.Limits(keepalive_expiry=120),
    )
    helpers._http_client = client
    try:
        order_client = httpx.Client(
            http2=True,
            limits=httpx.Limits(keepalive_expiry=120),
        )
        if callable(set_order_http_client):
            set_order_http_client(order_client)
    except Exception:
        pass
    return client


def _start_clob_keepalive(state: dict, cfg: Optional[dict] = None) -> None:
    """Keep the CLOB connection warm around active sell windows or always-on heartbeat."""
    client = _configure_clob_http_client()
    host = str(getattr(client, "host", "https://clob.polymarket.com")).rstrip("/")

    def _run() -> None:
        last_ping = 0.0
        while not _shutdown:
            try:
                now = time.time()
                intents = list(state.get("intents", {}).values())
                active = any(
                    any(intent.get(key) for key in (
                        "sell_loser_armed_at",
                        "sell_dump_armed_at",
                        "sell_winner_armed_at",
                    ))
                    or 0 <= float(intent.get("end_ts") or 0) - now <= 360
                    for intent in intents
                )
                active_cfg = cfg if isinstance(cfg, dict) else (state.get("cfg") if isinstance(state.get("cfg"), dict) else (globals().get("_keepalive_cfg") or {}))
                always_on = bool(
                    active_cfg.get("clob_keepalive_always_on")
                    or active_cfg.get("sniper_5m_enabled")
                    or state.get("clob_keepalive_always_on")
                )
                interval_s = float(active_cfg.get("clob_keepalive_interval_s", 12.0) or 12.0)
                if active or (always_on and now - last_ping >= interval_s):
                    clob = globals().get("_clob_client")
                    clob_host = str(getattr(clob, "host", host)).rstrip("/")
                    client.get(f"{clob_host}/time", timeout=1.0)
                    try:
                        from buy.presign import get_order_http_client, last_order_activity_ts

                        order_hc = get_order_http_client()
                        if (
                            order_hc is not None
                            and order_hc is not client
                            and callable(getattr(order_hc, "get", None))
                            and now - float(last_order_activity_ts() or 0.0) >= interval_s
                        ):
                            order_hc.get(f"{clob_host}/time", timeout=1.0)
                    except Exception:
                        pass
                    last_ping = now
            except Exception:
                pass
            time.sleep(3.0)

    threading.Thread(target=_run, name="mintbot-clob-keepalive", daemon=True).start()


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
        box = {
            "tokens": set(),
            "collateral": False,
            "warmed": set(),
            "order_options": {},
        }
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


def _schedule_order_prewarm(
    up_token: str, dn_token: str, *, presign_specs: Optional[list] = None
) -> None:
    """Tick size, neg-risk, one allowance refresh, and optional pre-signing per token."""
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
        specs = presign_specs if presign_specs is not None else box.get("presign_specs")
        for token_id in tokens:
            if token_id not in warmed:
                try:
                    tick_size = client.get_tick_size(token_id)
                    resolve_version = getattr(client, "_ClobClient__resolve_version", None)
                    version = int(resolve_version() if resolve_version is not None else 2)
                    neg_risk = False if version == 3 else bool(client.get_neg_risk(token_id))
                    box.setdefault("order_options", {})[token_id] = (tick_size, neg_risk, version)
                    warmed.add(token_id)
                except Exception:
                    pass
                _refresh_conditional_allowance(token_id, force=False)
            if specs:
                try:
                    from buy.presign import get_presign_cache

                    opt = box.get("order_options", {}).get(token_id, ("0.01", False, 2))
                    for spec in specs:
                        get_presign_cache().presign_market_order(
                            client,
                            token_id,
                            str(spec.get("side", "BUY")),
                            float(spec["amount"]),
                            float(spec["price"]),
                            tick_size=str(opt[0]),
                            neg_risk=bool(opt[1]),
                            version=int(opt[2]),
                            order_type=str(spec.get("order_type", "FAK")),
                        )
                except Exception:
                    pass
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
    from buy.mint_sell import floor_sell_shares

    retry = floor_sell_shares(min(float(size), sellable))
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
    decision_at: Optional[float] = None,
) -> Tuple[float, str]:
    """One FAK. On a balance/allowance reject, refresh, resize, retry once."""
    sold, status = _fak_sell(
        token_id, size, price, dry_run, capture=capture, decision_at=decision_at,
    )
    if dry_run or float(sold or 0) >= float(tol):
        return sold, status
    if not is_balance_allowance_reject(status):
        return sold, status
    retry, latch = _resize_after_balance_reject(token_id, size, keep=keep, tol=tol)
    if latch == "already_flat" or retry < float(tol):
        return 0.0, "already_flat"
    return _fak_sell(
        token_id, retry, price, dry_run, capture=capture, decision_at=decision_at,
    )


def _fak_sell(
    token_id: str,
    size: float,
    price: float,
    dry_run: bool,
    capture: Optional[list] = None,
    decision_at: Optional[float] = None,
):
    size = float(size)
    price = float(price)
    if size < 0.01 or price <= 0:
        return 0.0, "bad_args"
    if dry_run:
        globals().get("_console_print", console.print)(
            f"  [bold black on yellow][DRY SELL][/] {size:.2f} @ >={price:.3f} "
            f"token={str(token_id)[:12]}..."
        )
        log_event("dry_sell", token_id=str(token_id), size=size, price=price)
        return 0.0, "dry"
    client = _get_clob_client()
    if client is None:
        return 0.0, f"no_clob:{_clob_init_error or 'unknown'}"
    triggered = time.perf_counter()
    send_at = None
    try:
        from py_clob_client_v2 import MarketOrderArgs, OrderType
        from py_clob_client_v2.clob_types import CreateOrderOptions
        from py_clob_client_v2.constants import BYTES32_ZERO
        from py_clob_client_v2.order_builder.constants import SELL
        from py_clob_client_v2.utilities import price_valid

        order_options = _allowance_box().setdefault("order_options", {})
        options = order_options.get(str(token_id))
        if options is None:
            tick_size = client.get_tick_size(str(token_id))
            resolve_version = getattr(client, "_ClobClient__resolve_version", None)
            version = int(resolve_version() if resolve_version is not None else 2)
            neg_risk = False if version == 3 else bool(client.get_neg_risk(str(token_id)))
            options = (tick_size, neg_risk, version)
            order_options[str(token_id)] = options
            log_event(
                "sell_order_options_cold",
                token_id=str(token_id),
                tick_size=tick_size,
                neg_risk=neg_risk,
                version=version,
            )
        tick_size, neg_risk, version = options
        if not price_valid(price, tick_size):
            return 0.0, "bad_args"
        order_args = MarketOrderArgs(
            token_id=str(token_id),
            amount=size,
            side=SELL,
            price=price,
            order_type=OrderType.FAK,
        )
        builder_config = getattr(client, "builder_config", None)
        if builder_config and getattr(builder_config, "builder_code", None):
            code = builder_config.builder_code
            current = getattr(order_args, "builder_code", None)
            if not current or current == BYTES32_ZERO:
                order_args.builder_code = code
        from buy.presign import fast_post_order, get_presign_cache

        entry = get_presign_cache().pop(client, str(token_id), "SELL", size, price, "FAK")
        if entry is not None:
            signed = entry.signed_order
        else:
            # fee_rate_bps uses the same builder default (0) as _fak_buy.
            signed = client.builder.build_market_order(
                order_args,
                CreateOrderOptions(tick_size=tick_size, neg_risk=neg_risk),
                version=version,
            )
        send_at = time.perf_counter()
        result = fast_post_order(client, signed, entry=entry, order_type=OrderType.FAK)
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
            "decision_to_send_ms": (
                round((send_at - decision_at) * 1000.0, 3)
                if decision_at is not None else None
            ),
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
            trigger_to_send_ms=(
                round((send_at - triggered) * 1000.0, 3) if send_at is not None else None
            ),
            send_to_response_ms=(
                round((responded - send_at) * 1000.0, 3) if send_at is not None else None
            ),
            decision_to_send_ms=(
                round((send_at - decision_at) * 1000.0, 3)
                if send_at is not None and decision_at is not None else None
            ),
        )
        return 0.0, f"error:{str(exc)[:80]}"


def _fak_warmup_sync(token_ids):
    """Warm entry metadata once per token within the recent 64-token window."""
    global _fak_warmup_client_attempted, _fak_warmup_warn_at
    with _fak_warmup_lock:
        tokens = list(dict.fromkeys(str(token) for token in token_ids
                                   if str(token) not in _fak_warmup_attempted
                                   and str(token) not in _fak_market_cache))
        if not tokens and _fak_warmup_client_attempted:
            return
        _fak_warmup_client_attempted = True

        def warn(exc):
            global _fak_warmup_warn_at
            now = time.monotonic()
            if _fak_warmup_warn_at is None or now - _fak_warmup_warn_at >= 300:
                _fak_warmup_warn_at = now
                log_event("clob_warmup_fail", level="warning", error=str(exc)[:200])

        for token in tokens:
            _fak_warmup_attempted[token] = None
        while len(_fak_warmup_attempted) > 64:
            oldest = next(iter(_fak_warmup_attempted))
            del _fak_warmup_attempted[oldest]
            _fak_market_cache.pop(oldest, None)
        try:
            client = _get_clob_client()
            if client is None:
                raise RuntimeError(_clob_init_error or "CLOB client unavailable")
            resolve_version = getattr(client, "_ClobClient__resolve_version", None)
            version = int(resolve_version() if resolve_version is not None else 2)
        except Exception as exc:
            warn(exc)
            return
        for token in tokens:
            if token not in _fak_warmup_attempted:
                continue
            try:
                tick_size = client.get_tick_size(token)
                neg_risk = False if version == 3 else bool(client.get_neg_risk(token))
                _fak_market_cache[token] = (tick_size, neg_risk)
                if type(client).__name__ == "ClobClient":
                    acfg = globals().get("_keepalive_cfg") or {}
                    if acfg.get("presign_enabled", True) and acfg.get("sniper_5m_enabled", False):
                        from buy.presign import get_presign_cache
                        s_shares = float(acfg.get("sniper_shares", 10))
                        s_limit = float(acfg.get("sniper_max_ask", 0.97))
                        s_usdc = reclaim_buy_usdc(s_shares, s_limit)
                        get_presign_cache().presign_market_order(
                            client, token, "BUY", s_usdc, s_limit,
                            tick_size=str(tick_size), neg_risk=bool(neg_risk), version=int(version),
                        )
                        log_event("presign_warmed", token_id=str(token)[:18], usdc=s_usdc, price=s_limit)
            except Exception as exc:
                warn(exc)


def _fak_warmup_tokens(token_ids):
    """Live-only callers schedule metadata work off the strategy tick."""
    threading.Thread(target=_fak_warmup_sync, args=(tuple(token_ids),),
                     name="mintbot-fivemin-clob-warmup", daemon=True).start()


def _fak_buy(
    token_id: str,
    shares: float,
    price: float,
    dry_run: bool,
    capture: Optional[list] = None,
    *,
    timing: Optional[dict] = None,
    post_meta: Optional[dict] = None,
):
    """One FAK buy. ``shares`` sizes the clip; the post is a market BUY in USDC.

    The dollar amount is ``reclaim_buy_usdc`` (whole cents) and ``price`` is
    the max. Signing uses the market-order builder, the same shape as a
    sell FAK, so the maker amount has at most two USDC decimals. Tick size,
    exchange version, and neg-risk are the lookups ``create_order`` already
    does. ``create_market_order``'s market-info prefetch is not called.
    Dry-run returns before any client call. No sleep.
    """
    from time import perf_counter

    timings = timing if timing is not None else {}
    timings.update({step: 0.0 for step in
                    ("client_init", "tick_size", "version", "neg_risk", "sign", "post")})
    timings["retry"] = {"count": 0, "ms": 0.0}
    retry_started = None

    @contextmanager
    def measure(step):
        started = perf_counter()
        try:
            yield
        finally:
            timings[step] += (perf_counter() - started) * 1000

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
    with measure("client_init"):
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
        cached = globals().get("_fak_market_cache", {}).get(str(token_id))
        with measure("tick_size"):
            tick_size = cached[0] if cached is not None else client.get_tick_size(str(token_id))
        if not price_valid(price, tick_size):
            return 0.0, "bad_args"
        with measure("version"):
            resolve_version = getattr(client, "_ClobClient__resolve_version", None)
            version = int(resolve_version() if resolve_version is not None else 2)
        with measure("neg_risk"):
            neg_risk = cached[1] if cached is not None else (
                False if version == 3 else bool(client.get_neg_risk(str(token_id))))
        builder_config = getattr(client, "builder_config", None)
        if builder_config and getattr(builder_config, "builder_code", None):
            code = builder_config.builder_code
            current = getattr(order_args, "builder_code", None)
            if not current or current == BYTES32_ZERO:
                order_args.builder_code = code
        from buy.presign import fast_post_order, get_presign_cache

        refreshed = False
        while True:
            with measure("sign"):
                entry = (
                    get_presign_cache().pop(client, str(token_id), "BUY", usdc, price, "FAK")
                    if not refreshed
                    else None
                )
                if entry is not None:
                    signed = entry.signed_order
                else:
                    signed = client.builder.build_market_order(
                        order_args,
                        CreateOrderOptions(tick_size=tick_size, neg_risk=neg_risk),
                        version=version,
                    )
            if post_meta is not None:
                post_meta["sign_done_ts"] = time.time()
                post_meta["presign_hit"] = entry is not None
            try:
                with measure("post"):
                    result = fast_post_order(client, signed, entry=entry, order_type=OrderType.FAK,
                                             post_meta=post_meta)
            except Exception as exc:
                status = reclaim_fak_status(exc)
                if not refreshed and is_balance_allowance_reject(status):
                    refreshed = True
                    timings["retry"]["count"] += 1
                    retry_started = perf_counter()
                    _refresh_collateral_allowance(force=True)
                    continue
                if post_meta is not None and post_meta.get("resp_complete_ts") is None:
                    post_meta["resp_complete_ts"] = time.time()
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
                timings["retry"]["count"] += 1
                retry_started = perf_counter()
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
    finally:
        if retry_started is not None:
            timings["retry"]["ms"] = (perf_counter() - retry_started) * 1000


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
    decision_at: Optional[float] = None,
) -> Tuple[float, str, Optional[float]]:
    """FAK each limit in turn. ``fills`` collects ``(shares, avg_px)`` per post.

    No sleep between rungs or after the last miss. The caller's refire
    fetches a fresh book and posts immediately. A balance/allowance reject
    refreshes allowance, reads the chain once, and retries that rung once
    at the floored remainder.
    """
    from buy.mint_sell import floor_sell_shares

    sold_total = 0.0
    last_status = "none"
    last_px: Optional[float] = None
    for use_px in limits:
        remaining = floor_sell_shares(size - sold_total)
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
        globals().get("_console_print", console.print)(
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
            decision_at=decision_at,
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
    min_post: Optional[float] = None,
    decision_at: Optional[float] = None,
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
        min_post=min_post,
    )
    if not plan["limits"]:
        return 0.0, "below_floor", None, False
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
        globals().get("_console_print", console.print)(
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
            decision_at=decision_at,
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
        decision_at=decision_at,
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
    end_ts: Optional[float] = None,
    decision_at: Optional[float] = None,
    limit_offset: float = 0.0,
    limit_floor: float = 0.0,
) -> Tuple[float, str, Optional[float], int, float]:
    """Held-dump path: first live-bid FAK, then fresh-bid FAKs with no sleep.

    Each retry refetches that leg and posts one FAK at the new best bid.
    It does not walk a ladder off the first snapshot. A partial fill
    refires at once at ``floor(target - filled)``. A zero fill still needs
    ``dump_fast_retry_eligible`` (empty book or kill/cancel), except a
    balance/allowance reject: that refetches the token balance and posts
    ``floor(min(remaining, balance))`` immediately. Shares the wallet no
    longer holds are counted as sold with no price, so the average stays
    on real fills. A missing balance read, or a read that does not shrink
    the order, ends this pass (the in-rung resize already tried once) so
    the loop cannot spin. An empty refetch is not a stop: the next read is
    immediate. ``retries`` <= 0 or None keeps going until the size is sold
    or ``end_ts`` has passed. A positive ``retries`` is a cap on those
    refires. ``fills`` collects ``(shares, avg_px)`` across every post.
    """
    from buy.mint_sell import dump_deep_limit, is_balance_allowance_reject, sell_remaining_shares

    live_bid = float(initial_bid or 0.0)
    initial_limits = [
        dump_deep_limit(live_bid, offset=limit_offset, floor=limit_floor, clob_min=floor)
        if limit_offset > 0 else round(live_bid, 4)
    ] if live_bid > 0 else []
    sold_total = 0.0
    last_status = "none"
    last_px: Optional[float] = None
    attempts = 0
    if not initial_limits:
        return sold_total, "bad_bid", last_px, attempts, live_bid
    opening = sell_remaining_shares(size, 0.0)
    if opening < 0.01:
        return sold_total, "bad_size", last_px, attempts, live_bid

    log_event(
        "sell_dump_limit",
        condition_id=condition_id,
        slug=slug,
        leg=held,
        bid=live_bid,
        limit=initial_limits[0],
        offset=limit_offset,
        floor=limit_floor,
    )
    with _io_unlocked():
        sold, last_status, last_px = _run_fak_ladder(
            token_id,
            opening,
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
            decision_at=decision_at,
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
        # Balance rejects are not empty-book misses. Keep selling the
        # remainder the wallet actually holds; do not stop the dump on them.
        if is_balance_allowance_reject(status):
            return True
        return bool(dump_fast_retry_eligible(sold=got, status=status, tol=tol))

    if retries is None or retries == "":
        cap = None
    else:
        try:
            cap_n = int(retries)
        except (TypeError, ValueError):
            cap_n = 0
        cap = None if cap_n <= 0 else cap_n

    def _window_open() -> bool:
        if end_ts is None or end_ts == "":
            return True
        try:
            end = float(end_ts)
        except (TypeError, ValueError):
            return True
        if end != end or end <= 0 or end == float("inf"):
            return True
        try:
            now = float(time.time())
        except (TypeError, ValueError):
            return True
        if now != now:
            return True
        return now < end

    refires = 0
    balance_applied = False
    while not dry_run and sold_total < size - tol:
        if not _keep_refiring(sold, last_status):
            if refires:
                log_event(
                    "sell_dump_fast_refire_stop",
                    condition_id=condition_id,
                    slug=slug,
                    leg=held,
                    retry=refires,
                    attempts=attempts,
                    reason="non_retryable_status",
                    status=last_status,
                    sold=float(sold or 0.0),
                )
            break
        if not _window_open():
            log_event(
                "sell_dump_fast_refire_stop",
                condition_id=condition_id,
                slug=slug,
                leg=held,
                retry=refires,
                attempts=attempts,
                reason="window_end",
                sold=round(sold_total, 4),
            )
            break
        if cap is not None and refires >= cap:
            log_event(
                "sell_dump_fast_refire_stop",
                condition_id=condition_id,
                slug=slug,
                leg=held,
                retry=refires,
                attempts=attempts,
                reason="retry_cap",
                sold=round(sold_total, 4),
            )
            break
        post = sell_remaining_shares(size, sold_total)
        if is_balance_allowance_reject(last_status) and not balance_applied:
            balance_applied = True
            bal = _read_sell_balance(token_id)
            try:
                bal_f = float(bal) if bal is not None else None
            except (TypeError, ValueError):
                bal_f = None
            if bal_f is None or bal_f != bal_f:
                log_event(
                    "sell_dump_fast_refire_stop",
                    condition_id=condition_id,
                    slug=slug,
                    leg=held,
                    retry=refires,
                    attempts=attempts,
                    reason="balance_unknown",
                    status=last_status,
                    sold=round(sold_total, 4),
                )
                break
            if bal_f < 0.01:
                # One short read is not a fill. Leave the counted shares as
                # they are and let the caller treat the wallet as flat.
                last_status = "already_flat"
                log_event(
                    "sell_dump_fast_refire_stop",
                    condition_id=condition_id,
                    slug=slug,
                    leg=held,
                    retry=refires,
                    attempts=attempts,
                    reason="balance_flat",
                    status=last_status,
                    sold=round(sold_total, 4),
                    balance=bal_f,
                )
                break
            capped = sell_remaining_shares(size, sold_total, bal_f)
            if capped + 1e-9 >= post:
                # The wallet still shows the size we just failed to post.
                # Another identical post would spin the sell thread.
                log_event(
                    "sell_dump_fast_refire_stop",
                    condition_id=condition_id,
                    slug=slug,
                    leg=held,
                    retry=refires,
                    attempts=attempts,
                    reason="balance_unchanged",
                    status=last_status,
                    sold=round(sold_total, 4),
                    balance=bal_f,
                )
                break
            gap = post - capped
            if gap >= 0.01:
                # Tokens already gone, with no price in the reply. Count
                # them so the next post is the remainder, not the original
                # target. The fill average ignores this unpriced gap.
                sold_total += gap
            post = capped
        if post < 0.01:
            break
        refires += 1
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
            # The bid is gone. Read again immediately. Do not wait for the
            # next sell pass, and do not treat an empty book as done.
            continue
        # One FAK at the bid just fetched. The next retry fetches again.
        live_bid = fresh_bid
        limits = [
            dump_deep_limit(live_bid, offset=limit_offset, floor=limit_floor, clob_min=floor)
            if limit_offset > 0 else round(live_bid, 4)
        ]
        log_event(
            "sell_dump_fast_refire_attempt",
            condition_id=condition_id,
            slug=slug,
            leg=held,
            retry=refires,
            attempts=attempts + 1,
            bid=live_bid,
            limits=limits,
            limit=limits[0],
            sold=round(sold_total, 4),
        )
        with _io_unlocked():
            sold, last_status, last_px = _run_fak_ladder(
                token_id,
                post,
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
                decision_at=decision_at,
            )
        attempts += 1
        sold_total += float(sold or 0.0)
        balance_applied = False
    return sold_total, last_status, last_px, attempts, live_bid


def _reclaim_dump_retries(cfg: dict) -> int:
    """Finite refire count for the reclaim stop and the hedge sell.

    The dump treats 0/null as unlimited. Those two sells keep a positive
    cap: the configured count, or 2 when that count is missing or not positive.
    """
    cap = dump_refire_cap(cfg.get("sell_dump_fak_retries", 2))
    return 2 if cap is None else cap



def _dump_parallel_kept(cfg: dict) -> bool:
    """True unless ``sell_dump_parallel_kept`` is explicitly off.

    Missing or null keeps the default (post the kept leg with the held leg).
    """
    raw = cfg.get("sell_dump_parallel_kept", True)
    if raw is None or raw == "":
        return True
    return bool(raw)


def _prepare_kept_dump(
    *,
    intent: dict,
    kept: str,
    tokens: dict,
    tol: float,
    chain: ChainReader,
    ctf: str,
    funder_cs: Optional[str],
) -> dict:
    """Size the kept sell on the caller. No order."""
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
    return {
        "leg": kept,
        "keep": keep,
        "token": token,
        "size": float(size),
        "latch": latch,
        "planned": round(float(size), 4),
    }


def _empty_kept_result(prep: dict) -> dict:
    return {
        "leg": prep.get("leg"),
        "keep": prep.get("keep"),
        "planned": prep.get("planned"),
        "latch": prep.get("latch") or "no_keep",
        "sold": 0.0,
        "swept": 0.0,
        "fills": [],
        "bid": None,
        "limit": None,
        "attempts": 0,
        "status": prep.get("latch") or "no_keep",
        "outcome": "nothing_kept",
        "remaining": 0.0,
        "posted": False,
    }


def _post_kept_dump(
    prep: dict,
    *,
    cfg: dict,
    cid: str,
    slug: Any,
    bids: dict,
    books: dict,
    ttm_s: Optional[float],
    min_bid_size: float,
    retries: int,
    ladder_step: float,
    ladder_rungs: int,
    dry_run: bool,
    tol: float,
    end_ts: Optional[float] = None,
    decision_at: Optional[float] = None,
) -> dict:
    """Kept-leg FAK, fresh-bid refire, and one floor sweep. Does not write the intent.

    ``sell_dump_kept_max_px`` is read once here. At or above that bid the
    kept shares are not posted; the held leg is unaffected.
    """
    size = float(prep.get("size") or 0.0)
    if size < tol:
        return _empty_kept_result(prep)
    kept = prep.get("leg")
    kept_bid = bids.get(kept) if isinstance(bids, dict) else None
    if kept_dump_blocked(kept_bid, cfg.get("sell_dump_kept_max_px", None)):
        return {
            "leg": kept,
            "keep": prep.get("keep"),
            "planned": prep.get("planned"),
            "latch": prep.get("latch"),
            "sold": 0.0,
            "swept": 0.0,
            "fills": [],
            "bid": kept_bid,
            "limit": None,
            "attempts": 0,
            "status": "held_above_max",
            "outcome": "held_above_max",
            "remaining": float(size),
            "posted": False,
            "max_px": cfg.get("sell_dump_kept_max_px", None),
        }
    token = prep.get("token")
    floor = max(0.01, float(cfg.get("sell_clob_min_price") or 0.01))
    kept_fills: list = []
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
        end_ts=end_ts,
        decision_at=decision_at,
    )
    sold_total = float(sold_total or 0.0)
    swept = 0.0
    if (
        not dry_run
        and size - sold_total >= tol
        and last_status != "already_flat"
    ):
        from buy.mint_sell import sell_remaining_shares

        sweep_size = sell_remaining_shares(size, sold_total)
        with _io_unlocked():
            swept, sweep_status, sweep_px = _run_fak_ladder(
                token,
                sweep_size,
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
                decision_at=decision_at,
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
    return {
        "leg": kept,
        "keep": prep.get("keep"),
        "planned": prep.get("planned"),
        "latch": prep.get("latch"),
        "sold": sold_total,
        "swept": float(swept or 0.0),
        "fills": kept_fills,
        "bid": live_bid,
        "limit": last_px,
        "attempts": int(attempts),
        "status": last_status,
        "outcome": outcome,
        "remaining": remaining,
        "posted": True,
    }


def _apply_kept_dump_result(intent: dict, cid: str, result: dict) -> None:
    """Write one kept-dump result onto the intent. Caller holds the sell thread."""
    slug = intent.get("slug")
    kept = result.get("leg")
    planned = result.get("planned")
    keep = result.get("keep")
    intent["sell_dump_kept_done"] = True
    intent["sell_dump_kept_planned"] = planned
    if result.get("outcome") == "held_above_max":
        intent["sell_dump_kept_outcome"] = "held_above_max"
        intent["sell_dump_kept_cap_hold"] = True
        intent["sell_dump_kept_leg"] = kept
        intent["sell_dump_kept_sold"] = False
        intent.setdefault("sell_dump_kept_filled", 0.0)
        log_event(
            "sell_dump_kept",
            condition_id=cid,
            slug=slug,
            leg=kept,
            planned=planned,
            keep=keep,
            sold=0.0,
            avg_px=None,
            bid=result.get("bid"),
            max_px=result.get("max_px"),
            status="held_above_max",
            outcome="held_above_max",
            remaining=round(float(result.get("remaining") or 0.0), 4),
        )
        globals().get("_console_print", console.print)(
            f"  [bold bright_yellow][DUMP KEPT HELD][/] {kept} "
            f"bid={result.get('bid')} >= {result.get('max_px')}"
        )
        return
    if not result.get("posted"):
        if intent.get("sell_dump_kept_cap_hold") and result.get("latch") == "already_flat":
            intent["sell_dump_kept_cap_hold"] = False
            intent["sell_dump_kept_sold"] = True
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
            status=result.get("status"),
            outcome="nothing_kept",
            remaining=0.0,
        )
        return
    sold_total = float(result.get("sold") or 0.0)
    outcome = result.get("outcome")
    avg_px = record_fill_px(intent, "sell_dump_kept_fill_px", result.get("fills") or [])
    intent["sell_dump_kept_leg"] = kept
    intent["sell_dump_kept_filled"] = round(
        float(intent.get("sell_dump_kept_filled") or 0.0) + sold_total, 6
    )
    intent["sell_dump_kept_limit"] = result.get("limit")
    intent["sell_dump_kept_attempts"] = int(result.get("attempts") or 0)
    intent["sell_dump_kept_outcome"] = outcome
    if outcome in {"filled", "dry_run"} or (
        intent.get("sell_dump_kept_cap_hold") and outcome == "nothing_kept"
    ):
        if intent.get("sell_dump_kept_cap_hold"):
            intent["sell_dump_kept_cap_hold"] = False
        intent["sell_dump_kept_sold"] = True
    log_event(
        "sell_dump_kept",
        condition_id=cid,
        slug=slug,
        leg=kept,
        planned=planned,
        keep=keep,
        sold=round(sold_total, 4),
        swept=round(float(result.get("swept") or 0.0), 4),
        fills=len(result.get("fills") or []),
        avg_px=avg_px,
        bid=result.get("bid"),
        limit=result.get("limit"),
        attempts=int(result.get("attempts") or 0),
        status=result.get("status"),
        outcome=outcome,
        remaining=round(float(result.get("remaining") or 0.0), 4),
    )
    globals().get("_console_print", console.print)(
        f"  [bold bright_yellow][DUMP KEPT {str(outcome).upper()}][/] {kept} "
        f"{sold_total:.2f}/{float(result.get('planned') or 0.0):.2f}  avg={avg_px}"
    )


def _start_parallel_kept(
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
    retries: Any,
    ladder_step: float,
    ladder_rungs: int,
    dry_run: bool,
    tol: float,
    chain: ChainReader,
    ctf: str,
    funder_cs: Optional[str],
    end_ts: Optional[float] = None,
    decision_at: Optional[float] = None,
) -> Tuple[Optional[threading.Thread], dict]:
    """Size the kept leg, then start its sell. The caller posts the held leg next.

    The worker does not write ``intent``. A zero keep is a result with no thread.
    """
    prep = _prepare_kept_dump(
        intent=intent,
        kept=kept,
        tokens=tokens,
        tol=tol,
        chain=chain,
        ctf=ctf,
        funder_cs=funder_cs,
    )
    box: dict = {"prep": prep}
    if float(prep["size"]) < tol:
        box["result"] = _empty_kept_result(prep)
        return None, box
    slug = intent.get("slug")
    _log_book_source(
        cfg, path="kept", leg=kept, bid=bids.get(kept) if isinstance(bids, dict) else None,
        cid=cid, slug=slug,
    )

    def _run() -> None:
        try:
            box["result"] = _post_kept_dump(
                prep,
                cfg=cfg,
                cid=cid,
                slug=slug,
                bids=bids,
                books=books,
                ttm_s=ttm_s,
                min_bid_size=min_bid_size,
                retries=retries,
                ladder_step=ladder_step,
                ladder_rungs=ladder_rungs,
                dry_run=dry_run,
                tol=tol,
                end_ts=end_ts,
                decision_at=decision_at,
            )
        except Exception as exc:
            failed = _empty_kept_result(prep)
            failed["posted"] = True
            failed["outcome"] = "no_fill"
            failed["status"] = f"error:{str(exc)[:160]}"
            failed["remaining"] = float(prep["size"])
            box["result"] = failed

    worker = threading.Thread(target=_run, name="dump-kept", daemon=True)
    worker.start()
    return worker, box


def _finish_parallel_kept(intent: dict, cid: str, box: Optional[dict]) -> None:
    """Join already happened. Merge the kept result once."""
    if not box or box.get("applied"):
        return
    box["applied"] = True
    result = box.get("result")
    if result is None:
        result = _empty_kept_result(box.get("prep") or {})
        result["posted"] = True
        result["outcome"] = "no_fill"
        result["status"] = "error:kept_missing"
        result["remaining"] = float((box.get("prep") or {}).get("size") or 0.0)
    _apply_kept_dump_result(intent, cid, result)


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
    retries: Any,
    ladder_step: float,
    ladder_rungs: int,
    dry_run: bool,
    tol: float,
    chain: ChainReader,
    ctf: str,
    funder_cs: Optional[str],
    end_ts: Optional[float] = None,
    decision_at: Optional[float] = None,
) -> None:
    """``sell_dump_also_kept`` when the kept leg waits for the held fill.

    Same live-bid FAK + fast refire as the held dump, then one FAK at the
    1c floor for any remainder. Cap-held leftovers can retry on later ticks.
    """
    already_exit = bool(intent.get("sell_exit_inflight"))
    intent["sell_exit_inflight"] = True
    try:
        prep = _prepare_kept_dump(
            intent=intent,
            kept=kept,
            tokens=tokens,
            tol=tol,
            chain=chain,
            ctf=ctf,
            funder_cs=funder_cs,
        )
        if float(prep.get("size") or 0.0) >= tol:
            _log_book_source(
                cfg, path="kept", leg=kept,
                bid=bids.get(kept) if isinstance(bids, dict) else None,
                cid=cid, slug=intent.get("slug"),
            )
        result = _post_kept_dump(
            prep,
            cfg=cfg,
            cid=cid,
            slug=intent.get("slug"),
            bids=bids,
            books=books,
            ttm_s=ttm_s,
            min_bid_size=min_bid_size,
            retries=retries,
            ladder_step=ladder_step,
            ladder_rungs=ladder_rungs,
            dry_run=dry_run,
            tol=tol,
            end_ts=end_ts,
            decision_at=decision_at,
        )
        _apply_kept_dump_result(intent, cid, result)
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
    bid: Optional[float] = None,
    scrap_floor: Optional[float] = None,
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
    if not scrap_limit_allowed(bid, rest_px, scrap_floor):
        _log_sell_scrap_floor_skip(
            condition_id=cid,
            slug=intent.get("slug"),
            leg=intent.get("sell_loser_leg") or intent.get("sold_leg"),
            bid=bid,
            floor=scrap_floor,
            now_s=now,
        )
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
    scrap_floor: Optional[float] = None,
    now_s: float = 0.0,
    bid: Optional[float] = None,
) -> None:
    oid = intent.get("sell_scrap_rest_id")
    if oid and scrap_floor is not None:
        try:
            posted = float(intent.get("sell_scrap_rest_px") or 0.0)
        except (TypeError, ValueError):
            posted = 0.0
        if posted + 1e-12 < float(scrap_floor):
            _drop_scrap_rest(intent, cid, reason="below_floor")
            _log_sell_scrap_floor_skip(
                condition_id=cid,
                slug=intent.get("slug"),
                leg=intent.get("sell_loser_leg"),
                bid=bid,
                floor=scrap_floor,
                now_s=now_s,
            )
            return
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
    cfg: Optional[dict] = None,
    now: Optional[float] = None,
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
        **_oracle_mark(cid, cfg, time.time() if now is None else now),
    )
    return float(target), float(keep)


def _scrap_post_shares(
    intent: dict,
    inventory: float,
    fraction: float,
    cid: str,
    leg: Optional[str],
    threshold: Optional[float] = None,
    cfg: Optional[dict] = None,
    now: Optional[float] = None,
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
        cfg=cfg,
        now=now,
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


def _log_sell_scrap_floor_skip(
    *,
    condition_id: str,
    slug: Any,
    leg: Any,
    bid: Any,
    floor: Any,
    now_s: float,
    interval_s: float = 15.0,
) -> bool:
    """Log ``sell_scrap_floor_skip`` at most once per leg per ``interval_s``."""
    stamps = getattr(_log_sell_scrap_floor_skip, "_last_at", None)
    if not isinstance(stamps, dict):
        stamps = {}
        setattr(_log_sell_scrap_floor_skip, "_last_at", stamps)
    key = f"{condition_id}:{leg}"
    prev = stamps.get(key)
    if prev is not None and float(now_s) + 1e-12 < float(prev) + float(interval_s):
        return False
    stamps[key] = float(now_s)
    try:
        bid_out = None if bid is None else round(float(bid), 4)
    except (TypeError, ValueError):
        bid_out = None
    try:
        floor_out = None if floor is None else round(float(floor), 4)
    except (TypeError, ValueError):
        floor_out = None
    log_event(
        "sell_scrap_floor_skip",
        condition_id=condition_id,
        slug=slug,
        leg=leg,
        bid=bid_out,
        floor=floor_out,
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


def _close_bag_risk(
    condition_id: str, intent: dict, *, now: float, cfg: Optional[dict] = None,
) -> None:
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
            **_oracle_mark(condition_id, cfg, now),
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



def _cached_pusd(chain: Any) -> Optional[float]:
    """pUSD already read by the mint loop. No new wallet call."""
    sample = getattr(chain, "_pusd_sample", None)
    if not isinstance(sample, dict):
        return None
    try:
        bal = float(sample.get("balance"))
    except (TypeError, ValueError):
        return None
    if bal != bal or bal < 0 or bal == float("inf"):
        return None
    return bal


def _reclaim_hedge_run(
    *,
    intent: dict,
    cid: str,
    plan: dict,
    token: str,
    dry_run: bool,
    max_price: float,
    retries: int,
    min_bid_size: float,
    tol: float,
    ask_levels: Any = None,
) -> None:
    """Post the hedge FAK, then refire a miss on this thread. The main buy does not wait.

    A zero or partial fill refetches the other book and posts the unfilled
    shares at that ask, with no slippage, up to ``retries`` inside
    ``RECLAIM_HEDGE_RETRY_BUDGET_S`` of the first post. An ask above
    ``max_price`` stops the sequence. The first post keeps its own limit.
    ``error:`` and a balance/allowance reject do not refire. This thread
    never calls ``_io_unlocked``.
    """
    started = time.time()
    try:
        goal = float(plan.get("shares") or 0.0)
    except (TypeError, ValueError):
        goal = 0.0
    filled = 0.0
    status_s = "error:missing"
    outcome = "miss"
    attempts = 0
    last_limit = plan.get("limit")
    last_usdc = plan.get("usdc")
    tol_f = float(tol or 0.0)
    try:
        try:
            bought, status_s = _fak_buy(token, goal, float(plan["limit"]), dry_run)
            filled = float(bought or 0.0)
        except Exception as exc:
            filled = 0.0
            status_s = f"error:{str(exc)[:120]}"
        status_s = str(status_s or "")
        if dry_run or status_s == "dry":
            outcome = "filled"
        elif status_s.startswith("error"):
            outcome = "uncertain"
        elif is_balance_allowance_reject(status_s):
            outcome = "reject"
        else:
            while filled + tol_f < goal and reclaim_hedge_retry_allowed(
                started, time.time(), attempts, int(retries),
            ):
                with STATE_LOCK:
                    intent["reclaim_hedge_retrying"] = True
                ask = None
                try:
                    _bid, _bsz, _bids, ask, _asz, _asks, _age = _book_quote(
                        _fetch_book(token, min_bid_size)
                    )
                except Exception:
                    ask = None
                remaining = max(0.0, goal - filled)
                resized = reclaim_hedge_retry_plan(ask, remaining, max_price)
                attempts += 1
                log_event(
                    "reclaim_hedge_retry",
                    condition_id=cid,
                    slug=intent.get("slug"),
                    leg=intent.get("reclaim_hedge_leg"),
                    attempt=attempts,
                    px=resized.get("limit"),
                    ask=ask,
                    filled=filled,
                    remaining=remaining,
                    status=str(resized.get("reason") or ""),
                    shares=resized.get("shares"),
                    usdc=resized.get("usdc"),
                )
                reason = str(resized.get("reason") or "")
                if not resized.get("ok"):
                    if reason == "above_cap":
                        outcome = "above_cap"
                        break
                    if reason == "no_ask":
                        outcome = "no_ask"
                        continue
                    outcome = reason or "skip"
                    break
                goal = filled + float(resized["shares"])
                last_limit = resized.get("limit")
                last_usdc = resized.get("usdc")
                with STATE_LOCK:
                    intent["reclaim_hedge_usdc"] = last_usdc
                    intent["reclaim_hedge_target"] = goal
                    intent["reclaim_hedge_limit"] = last_limit
                    intent["reclaim_hedge_retrying"] = True
                if time.time() > started + float(RECLAIM_HEDGE_RETRY_BUDGET_S) + 1e-9:
                    outcome = "budget"
                    break
                try:
                    bought, status_s = _fak_buy(
                        token, float(resized["shares"]), float(resized["limit"]), dry_run,
                    )
                    filled += float(bought or 0.0)
                except Exception as exc:
                    status_s = f"error:{str(exc)[:120]}"
                status_s = str(status_s or "")
                if status_s.startswith("error"):
                    outcome = "uncertain"
                    break
                if is_balance_allowance_reject(status_s):
                    outcome = "reject"
                    break
            if filled + tol_f >= goal:
                outcome = "filled"
            elif outcome in ("above_cap", "budget", "uncertain", "reject"):
                pass
            elif outcome == "no_ask" and filled <= tol_f:
                pass
            elif time.time() > started + float(RECLAIM_HEDGE_RETRY_BUDGET_S) + 1e-9:
                outcome = "budget"
            else:
                outcome = "miss" if filled <= tol_f else "partial"
        noted = dict(plan)
        noted["shares"] = goal
        noted["limit"] = last_limit
        noted["usdc"] = last_usdc
        with STATE_LOCK:
            intent["reclaim_hedge_retrying"] = False
            if filled > 0 and status_s.startswith("error"):
                intent["reclaim_hedge_filled"] = filled
            _reclaim_note_hedge(
                intent, cid, noted, (filled, status_s),
                dry_run=dry_run, ask_levels=ask_levels,
            )
        log_event(
            "reclaim_hedge_outcome",
            condition_id=cid,
            slug=intent.get("slug"),
            leg=intent.get("reclaim_hedge_leg"),
            outcome=outcome,
            attempt=attempts,
            px=last_limit,
            filled=intent.get("reclaim_hedge_filled"),
            remaining=max(0.0, goal - filled),
            status=status_s,
        )
    except Exception as exc:
        with STATE_LOCK:
            intent["reclaim_hedge_retrying"] = False
            intent["reclaim_hedge_buy_inflight"] = False
            intent["reclaim_hedge_buy_uncertain"] = False
            intent["reclaim_hedge_buy_done"] = True
        log_event(
            "reclaim_hedge_buy",
            condition_id=cid,
            slug=intent.get("slug"),
            leg=intent.get("reclaim_hedge_leg"),
            sent=True,
            size=plan.get("shares"),
            px=plan.get("limit"),
            filled=filled,
            status=f"error:{str(exc)[:120]}",
            ask_depth=book_top_levels(ask_levels, "ask"),
        )
        log_event(
            "reclaim_hedge_outcome",
            condition_id=cid,
            slug=intent.get("slug"),
            leg=intent.get("reclaim_hedge_leg"),
            outcome="error",
            attempt=attempts,
            filled=filled,
            status=f"error:{str(exc)[:120]}",
        )


def _reclaim_note_hedge(
    intent: dict,
    cid: str,
    plan: dict,
    result: Any,
    *,
    dry_run: bool,
    ask_levels: Any = None,
) -> None:
    """Record the hedge post. Never changes the main reclaim buy."""
    bought, status = (0.0, "error:missing")
    if isinstance(result, tuple) and len(result) >= 2:
        bought, status = result[0], result[1]
    status_s = str(status or "")
    try:
        filled = float(bought or 0.0)
    except (TypeError, ValueError):
        filled = 0.0
    shares = float(plan.get("shares") or 0.0)
    limit = plan.get("limit")
    intent["reclaim_hedge_buy_inflight"] = False
    if dry_run or status_s == "dry":
        intent["reclaim_hedge_filled"] = shares
        intent["reclaim_hedge_buy_px"] = limit
        intent["reclaim_hedge_buy_done"] = True
        intent["reclaim_hedge_buy_uncertain"] = False
        filled = shares
        status_s = "dry"
    elif status_s.startswith("error"):
        intent["reclaim_hedge_buy_uncertain"] = True
        intent["reclaim_hedge_buy_done"] = False
    elif status_s.startswith("reject") or filled <= 0:
        intent["reclaim_hedge_buy_done"] = True
        intent["reclaim_hedge_buy_uncertain"] = False
        intent["reclaim_hedge_filled"] = max(0.0, filled)
    else:
        intent["reclaim_hedge_filled"] = filled
        intent["reclaim_hedge_buy_px"] = limit
        intent["reclaim_hedge_buy_done"] = True
        intent["reclaim_hedge_buy_uncertain"] = False
    log_event(
        "reclaim_hedge_buy",
        condition_id=cid,
        slug=intent.get("slug"),
        leg=intent.get("reclaim_hedge_leg"),
        sent=True,
        size=shares,
        px=limit,
        usdc=plan.get("usdc"),
        filled=filled,
        status=status_s,
        rounded_up=bool(plan.get("rounded_up")),
        raw_shares=plan.get("raw_shares"),
        dry_run=dry_run,
        ask_depth=book_top_levels(ask_levels, "ask"),
    )


def _reclaim_recover_hedge(
    intent: dict,
    *,
    cid: str,
    tokens: dict,
    tol: float,
    chain: ChainReader,
    ctf: str,
    funder_cs: Optional[str],
) -> None:
    """One balance read after an uncertain hedge. A single zero is not proof."""
    if not intent.get("reclaim_hedge_buy_uncertain"):
        return
    leg = intent.get("reclaim_hedge_leg")
    token = tokens.get(leg) if leg in ("up", "dn") else None
    if not token:
        return
    target = float(intent.get("reclaim_hedge_target") or 0.0)
    _bal, latch = _sell_inventory(
        chain, ctf, funder_cs, token, max(target, 0.0), tol,
        "seen_reclaim_hedge_inventory", intent,
        read_chain=True,
    )
    if latch == "has_inventory":
        intent["reclaim_hedge_filled"] = max(
            float(intent.get("reclaim_hedge_filled") or 0.0),
            float(_bal or 0.0),
        )
        intent["reclaim_hedge_buy_inflight"] = False
        intent["reclaim_hedge_buy_uncertain"] = False
        intent["reclaim_hedge_buy_done"] = True
        intent["reclaim_hedge_buy_flat_reads"] = 0
        log_event(
            "reclaim_hedge_buy",
            condition_id=cid,
            slug=intent.get("slug"),
            leg=leg,
            sent=True,
            size=target,
            px=intent.get("reclaim_hedge_limit"),
            filled=intent.get("reclaim_hedge_filled"),
            status="recovered",
        )
        return
    if latch != "already_flat":
        return
    flats = int(intent.get("reclaim_hedge_buy_flat_reads") or 0) + 1
    intent["reclaim_hedge_buy_flat_reads"] = flats
    if flats < 2:
        return
    intent["reclaim_hedge_buy_inflight"] = False
    intent["reclaim_hedge_buy_uncertain"] = False
    intent["reclaim_hedge_buy_done"] = True
    log_event(
        "reclaim_hedge_buy",
        condition_id=cid,
        slug=intent.get("slug"),
        leg=leg,
        sent=True,
        size=target,
        filled=0.0,
        status="uncertain_cleared",
        reads=flats,
    )


def _reclaim_hedge_watching(intent: dict) -> bool:
    """True while the hedge buy is open or the post-stop decision is not done."""
    if (
        intent.get("reclaim_hedge_buy_inflight")
        or intent.get("reclaim_hedge_retrying")
        or intent.get("reclaim_hedge_buy_uncertain")
    ):
        return True
    if intent.get("reclaim_hedge_kept") or intent.get("reclaim_hedge_sold"):
        return False
    try:
        filled = float(intent.get("reclaim_hedge_filled") or 0.0)
    except (TypeError, ValueError):
        filled = 0.0
    if filled <= 0:
        return False
    return bool(intent.get("reclaim_stop_latched") or intent.get("reclaim_stopped"))


def _reclaim_hedge_after_stop(
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
    min_bid_size: float,
    clob_min: float,
) -> None:
    """Keep or sell the hedge once the main stop has fired.

    A hedge miss, or no hedge, does nothing. The keep clock is the hedge
    bid already on this tick. The sell is the dump FAK refire.
    """
    try:
        if intent.get("reclaim_hedge_kept") or intent.get("reclaim_hedge_sold"):
            return
        if not (intent.get("reclaim_stop_latched") or intent.get("reclaim_stopped")):
            return
        if (
            intent.get("reclaim_hedge_buy_inflight")
            or intent.get("reclaim_hedge_retrying")
            or intent.get("reclaim_hedge_buy_uncertain")
        ):
            intent["reclaim_hot"] = True
            return
        try:
            filled = float(intent.get("reclaim_hedge_filled") or 0.0)
            sold_already = float(intent.get("reclaim_hedge_sold_shares") or 0.0)
        except (TypeError, ValueError):
            return
        remaining = max(0.0, filled - sold_already)
        if remaining < float(tol):
            return
        leg = intent.get("reclaim_hedge_leg")
        if leg not in ("up", "dn"):
            return
        keep_raw = cfg.get("reclaim_hedge_keep_above", 0.50)
        keep_above = 0.50 if keep_raw is None else float(keep_raw)
        decision = reclaim_hedge_keep_decision(
            now_s=now,
            bid=bids.get(leg),
            armed_ts=intent.get("reclaim_hedge_keep_armed_at"),
            keep_above=keep_above,
            persist_s=cfg_seconds(cfg, "reclaim_hedge_keep_persist_s", 1.0),
        )
        intent["reclaim_hedge_keep_armed_at"] = decision.get("armed_ts")
        log_event(
            "reclaim_hedge_keep_decision",
            condition_id=cid,
            slug=intent.get("slug"),
            leg=leg,
            action=decision.get("action"),
            reason=decision.get("reason"),
            bid=decision.get("bid"),
            keep_above=keep_above,
            persist_s=cfg_seconds(cfg, "reclaim_hedge_keep_persist_s", 1.0),
        )
        action = decision.get("action")
        if action == "wait":
            intent["reclaim_hot"] = True
            return
        if action == "keep":
            intent["reclaim_hedge_kept"] = True
            if intent.get("reclaim_stopped"):
                intent["reclaim_hot"] = False
            return
        token = str(tokens.get(leg) or "")
        if not token:
            intent["reclaim_hot"] = True
            return
        try:
            live = float(bids.get(leg) or 0.0)
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
                    size=remaining,
                    initial_bid=live,
                    initial_bids=[],
                    held=leg,
                    slug=intent.get("slug"),
                    condition_id=cid,
                    ttm_s=ttm_s,
                    floor=floor,
                    min_bid_size=min_bid_size,
                    retries=_reclaim_dump_retries(cfg),
                    ladder_step=float(cfg.get("sell_dump_ladder_step") or 0.04),
                    ladder_rungs=int(cfg.get("sell_dump_ladder_rungs") or 1),
                    dry_run=dry_run,
                    tol=tol,
                    fills=fills,
                )
            else:
                sold, status = _sell_fak_with_fallback(
                    token, remaining, floor, dry_run, None, keep=0.0, tol=tol,
                )
                last_px = floor
                sold = float(sold or 0.0)
        sold_f = float(sold or 0.0)
        if sold_f > 0:
            intent["reclaim_hedge_sold_shares"] = sold_already + sold_f
            record_fill_px(
                intent, "reclaim_hedge_sell_px", fills or [(sold_f, last_px)],
            )
        done = bool(dry_run) or (
            float(intent.get("reclaim_hedge_sold_shares") or 0.0) + tol >= filled
        ) or (str(status) == "already_flat" and sold_f < tol and not dry_run)
        log_event(
            "reclaim_hedge_sell",
            condition_id=cid,
            slug=intent.get("slug"),
            leg=leg,
            bid=bids.get(leg),
            limit=last_px,
            sold=sold_f,
            planned=remaining,
            filled=filled,
            status=status,
            dry_run=dry_run,
            outcome="done" if done else "partial",
        )
        if done:
            if dry_run:
                intent["reclaim_hedge_sold_shares"] = filled
            intent["reclaim_hedge_sold"] = True
            if intent.get("reclaim_stopped"):
                intent["reclaim_hot"] = False
        else:
            intent["reclaim_hot"] = True
    except Exception as exc:
        log_event(
            "reclaim_hedge_sell",
            condition_id=cid,
            slug=intent.get("slug"),
            status=f"error:{str(exc)[:160]}",
            outcome="error",
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
        _reclaim_hedge_after_stop(
            cfg=cfg, intent=intent, cid=cid, now=now, ttm_s=ttm_s,
            bids=bids, tokens=tokens, dry_run=dry_run, tol=tol,
            min_bid_size=min_bid_size, clob_min=clob_min,
        )
        return
    token = str(tokens.get(leg) or "")
    if not token:
        _reclaim_hedge_after_stop(
            cfg=cfg, intent=intent, cid=cid, now=now, ttm_s=ttm_s,
            bids=bids, tokens=tokens, dry_run=dry_run, tol=tol,
            min_bid_size=min_bid_size, clob_min=clob_min,
        )
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
        _reclaim_hedge_after_stop(
            cfg=cfg, intent=intent, cid=cid, now=now, ttm_s=ttm_s,
            bids=bids, tokens=tokens, dry_run=dry_run, tol=tol,
            min_bid_size=min_bid_size, clob_min=clob_min,
        )
        return
    size = remaining
    if latch == "has_inventory":
        size = min(remaining, float(inv or 0.0))
    if size < 0.01:
        _reclaim_hedge_after_stop(
            cfg=cfg, intent=intent, cid=cid, now=now, ttm_s=ttm_s,
            bids=bids, tokens=tokens, dry_run=dry_run, tol=tol,
            min_bid_size=min_bid_size, clob_min=clob_min,
        )
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
                retries=_reclaim_dump_retries(cfg),
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
        _reclaim_hedge_after_stop(
            cfg=cfg, intent=intent, cid=cid, now=now, ttm_s=ttm_s,
            bids=bids, tokens=tokens, dry_run=dry_run, tol=tol,
            min_bid_size=min_bid_size, clob_min=clob_min,
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
        _reclaim_hedge_after_stop(
            cfg=cfg, intent=intent, cid=cid, now=now, ttm_s=ttm_s,
            bids=bids, tokens=tokens, dry_run=dry_run, tol=tol,
            min_bid_size=min_bid_size, clob_min=clob_min,
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
    _reclaim_hedge_after_stop(
        cfg=cfg, intent=intent, cid=cid, now=now, ttm_s=ttm_s,
        bids=bids, tokens=tokens, dry_run=dry_run, tol=tol,
        min_bid_size=min_bid_size, clob_min=clob_min,
    )


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
    ask_levels: Optional[dict] = None,
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
    if intent.get("reclaim_hedge_buy_uncertain"):
        _reclaim_recover_hedge(
            intent, cid=cid, tokens=tokens, tol=tol,
            chain=chain, ctf=ctf, funder_cs=funder_cs,
        )
    if intent.get("reclaim_stopped"):
        _reclaim_hedge_after_stop(
            cfg=cfg, intent=intent, cid=cid, now=now, ttm_s=ttm_s,
            bids=bids, tokens=tokens, dry_run=dry_run, tol=tol,
            min_bid_size=min_bid_size, clob_min=clob_min,
        )
        intent["reclaim_hot"] = bool(_reclaim_hedge_watching(intent))
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
        other_max=cfg.get("reclaim_other_max", None),
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
    _log_book_source(
        cfg, path="reclaim", leg=decision.get("leg"),
        bid=bids.get(decision.get("leg")),
        cid=cid, slug=intent.get("slug"),
    )
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
    other = "dn" if leg == "up" else "up"
    hedge_plan = None
    hedge_token = ""
    if cfg.get("reclaim_hedge_enabled") and not intent.get("reclaim_hedge_attempted"):
        intent["reclaim_hedge_attempted"] = True
        try:
            slip_raw = cfg.get("reclaim_hedge_slippage", 0.02)
            hedge_slip = 0.02 if slip_raw is None else float(slip_raw)
            usd_raw = cfg.get("reclaim_hedge_usd", 1.0)
            hedge_usd = 1.0 if usd_raw is None else float(usd_raw)
            plan = reclaim_hedge_plan(asks_px.get(other), hedge_usd, hedge_slip)
            hedge_token = str(tokens.get(other) or "")
            try:
                cap_raw = cfg.get("reclaim_hedge_max_price", 0.15)
                hedge_cap = 0.15 if cap_raw is None else float(cap_raw)
            except (TypeError, ValueError):
                hedge_cap = 0.15
            if (
                hedge_cap != hedge_cap
                or hedge_cap in (float("inf"), float("-inf"))
                or not (0 < hedge_cap < 1)
            ):
                hedge_cap = 0.15
            try:
                retry_raw = cfg.get("reclaim_hedge_retries", 3)
                hedge_retries = 3 if retry_raw is None else int(retry_raw)
            except (TypeError, ValueError):
                hedge_retries = 3
            if hedge_retries < 0:
                hedge_retries = 0
            if not plan.get("ok") or not hedge_token:
                intent["reclaim_hedge_buy_done"] = True
                log_event(
                    "reclaim_hedge_buy",
                    condition_id=cid,
                    slug=intent.get("slug"),
                    leg=other,
                    sent=False,
                    size=plan.get("shares"),
                    px=plan.get("limit"),
                    usdc=plan.get("usdc"),
                    filled=0.0,
                    status=str(plan.get("reason") or "no_token"),
                    rounded_up=bool(plan.get("rounded_up")),
                )
            elif not reclaim_wallet_covers(
                _cached_pusd(chain), usdc, float(plan.get("usdc") or 0.0),
            ):
                intent["reclaim_hedge_buy_done"] = True
                log_event(
                    "reclaim_hedge_buy",
                    condition_id=cid,
                    slug=intent.get("slug"),
                    leg=other,
                    sent=False,
                    size=plan.get("shares"),
                    px=plan.get("limit"),
                    usdc=plan.get("usdc"),
                    filled=0.0,
                    status="cash_short",
                    need=round(usdc + float(plan.get("usdc") or 0.0), 4),
                    balance=_cached_pusd(chain),
                    rounded_up=bool(plan.get("rounded_up")),
                )
            else:
                intent["reclaim_hedge_leg"] = other
                intent["reclaim_hedge_usdc"] = plan.get("usdc")
                intent["reclaim_hedge_target"] = plan.get("shares")
                intent["reclaim_hedge_limit"] = plan.get("limit")
                intent["reclaim_hedge_buy_posted"] = True
                intent["reclaim_hedge_buy_inflight"] = True
                intent["reclaim_hedge_buy_done"] = False
                hedge_plan = plan
                hedge_cap_box = hedge_cap
                hedge_retries_box = hedge_retries
        except Exception as exc:
            hedge_plan = None
            intent["reclaim_hedge_buy_done"] = True
            intent["reclaim_hedge_buy_inflight"] = False
            log_event(
                "reclaim_hedge_buy",
                condition_id=cid,
                slug=intent.get("slug"),
                leg=other,
                sent=False,
                filled=0.0,
                status=f"error:{str(exc)[:120]}",
            )
    captured: list = []
    with _io_unlocked():
        if hedge_plan is not None:
            threading.Thread(
                target=_reclaim_hedge_run,
                kwargs={
                    "intent": intent,
                    "cid": cid,
                    "plan": hedge_plan,
                    "token": hedge_token,
                    "dry_run": dry_run,
                    "max_price": hedge_cap_box,
                    "retries": hedge_retries_box,
                    "min_bid_size": min_bid_size,
                    "tol": tol,
                    "ask_levels": (
                        ask_levels.get(other)
                        if isinstance(ask_levels, dict)
                        else None
                    ),
                },
                name="reclaim-hedge",
                daemon=True,
            ).start()
        bought, status = _fak_buy(
            str(token), shares, float(limit), dry_run, capture=captured,
        )
    status_s = str(status or "")
    levels = ask_levels if isinstance(ask_levels, dict) else {}
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
        "ask_depth": book_top_levels(levels.get(leg), "ask"),
        "hedge_ask_depth": book_top_levels(levels.get(other), "ask"),
    }
    post_fields.update(_oracle_mark(cid, cfg, time.time()))
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


def _post_dump_box() -> dict:
    """In-memory journal state. Not written to positions_mint.json."""
    box = getattr(_post_dump_box, "box", None)
    if box is None:
        box = {
            "snaps": {},
            "lock": threading.Lock(),
            "started": False,
            "cfg": {},
            "wake": threading.Event(),
            "stop": False,
            "thread": None,
        }
        _post_dump_box.box = box
    return box


def _post_dump_side(bids: Any, asks: Any) -> tuple:
    """Top-5 ladders and ask size within 3¢, from levels already parsed."""
    return (
        book_top_levels(bids, "bid"),
        book_top_levels(asks, "ask"),
        ask_size_within(asks),
    )


def _post_dump_quote(row: Any) -> tuple:
    bid, _bsz, bids, ask, _asz, asks, _age = _book_quote(row)
    return (bid, ask, *_post_dump_side(bids, asks))


def _compact_snap_depth(snap: dict) -> None:
    """Turn raw ladders into the top-5 log fields. Caller holds the box lock."""
    if snap.get("depth_ready"):
        return
    if "up_bids_raw" not in snap and snap.get("up_bids") is not None:
        snap["depth_ready"] = True
        return
    up_bids, up_asks, up_near = _post_dump_side(
        snap.get("up_bids_raw"), snap.get("up_asks_raw"),
    )
    dn_bids, dn_asks, dn_near = _post_dump_side(
        snap.get("dn_bids_raw"), snap.get("dn_asks_raw"),
    )
    snap["up_bids"] = up_bids
    snap["up_asks"] = up_asks
    snap["dn_bids"] = dn_bids
    snap["dn_asks"] = dn_asks
    snap["up_ask_near"] = up_near
    snap["dn_ask_near"] = dn_near
    snap["depth_ready"] = True


def _post_dump_fields(snap: dict, cid: str, ttm: Any) -> dict:
    return {
        "condition_id": cid,
        "slug": snap.get("slug"),
        "leg_dumped": snap.get("leg_dumped"),
        "up_bid": snap.get("up_bid"),
        "up_ask": snap.get("up_ask"),
        "dn_bid": snap.get("dn_bid"),
        "dn_ask": snap.get("dn_ask"),
        "up_bids": snap.get("up_bids") or [],
        "up_asks": snap.get("up_asks") or [],
        "dn_bids": snap.get("dn_bids") or [],
        "dn_asks": snap.get("dn_asks") or [],
        "up_ask_near": snap.get("up_ask_near"),
        "dn_ask_near": snap.get("dn_ask_near"),
        "ttm_s": ttm,
    }


def _observe_post_dump_book(
    *,
    cfg: dict,
    intent: dict,
    cid: str,
    now: float,
    end_ts: float,
    ttm_s: Optional[float],
    up_bid: Any,
    up_ask: Any,
    dn_bid: Any,
    dn_ask: Any,
    up_tok: str,
    dn_tok: str,
    min_bid_size: float,
    up_bids: Any = None,
    up_asks: Any = None,
    dn_bids: Any = None,
    dn_asks: Any = None,
) -> None:
    """Journal the books this tick already fetched. No network, no orders.

    Runs after the bag's sell, reclaim, and hedge work. A fresher off-path
    sample is left in place. The 0.25s gap between sell ticks is filled by
    ``_post_dump_logger``, which is the only caller allowed to fetch.
    """
    if not intent.get("sold_dump"):
        return
    if not sell_window_open(now, end_ts):
        return
    if not bool(cfg.get("post_dump_log_enabled", True)):
        return
    box = _post_dump_box()
    with box["lock"]:
        prev = box["snaps"].get(cid) or {}
        try:
            prev_book = float(prev.get("book_ts"))
        except (TypeError, ValueError):
            prev_book = None
        if prev_book is not None and prev_book > float(now) + 1e-9:
            prev["enabled"] = True
            prev["end_ts"] = end_ts
            prev["leg_dumped"] = intent.get("sell_dump_leg")
            prev["slug"] = intent.get("slug")
            box["snaps"][cid] = prev
        else:
            up_top_bids, up_top_asks, up_near = _post_dump_side(up_bids, up_asks)
            dn_top_bids, dn_top_asks, dn_near = _post_dump_side(dn_bids, dn_asks)
            box["snaps"][cid] = {
                "enabled": True,
                "sold_dump": True,
                "end_ts": end_ts,
                "slug": intent.get("slug"),
                "leg_dumped": intent.get("sell_dump_leg"),
                "up_bid": up_bid,
                "up_ask": up_ask,
                "dn_bid": dn_bid,
                "dn_ask": dn_ask,
                "up_bids": up_top_bids,
                "up_asks": up_top_asks,
                "dn_bids": dn_top_bids,
                "dn_asks": dn_top_asks,
                "up_ask_near": up_near,
                "dn_ask_near": dn_near,
                "book_ts": now,
                "up_token": up_tok,
                "dn_token": dn_tok,
                "min_bid_size": min_bid_size,
                "logged_at": prev.get("logged_at"),
            }
    _post_dump_book_emit(cid, now, allow_fetch=False, ttm_s=ttm_s)


def _post_dump_book_emit(
    cid: str,
    now: float,
    *,
    allow_fetch: bool,
    ttm_s: Optional[float] = None,
) -> None:
    """Write one ``post_dump_book`` line, or fetch only when ``allow_fetch``."""
    box = _post_dump_box()
    fetch_args = None
    row = None
    with box["lock"]:
        snap = box["snaps"].get(cid)
        if not snap:
            return
        action = post_dump_book_sample(
            enabled=bool(snap.get("enabled")),
            sold_dump=bool(snap.get("sold_dump")),
            now_s=now,
            end_ts=float(snap.get("end_ts") or 0),
            last_logged_at=snap.get("logged_at"),
            book_ts=snap.get("book_ts"),
        )
        if action == "skip":
            if not sell_window_open(now, float(snap.get("end_ts") or 0)):
                box["snaps"].pop(cid, None)
            return
        if action == "fetch":
            if not allow_fetch:
                return
            fetch_args = (
                str(snap.get("up_token") or ""),
                str(snap.get("dn_token") or ""),
                float(snap.get("min_bid_size") or 1.0),
                snap.get("leg_dumped"),
                snap.get("slug"),
                float(snap.get("end_ts") or 0),
            )
        else:
            end = float(snap.get("end_ts") or 0)
            logged_ttm = ttm_s
            if logged_ttm is None and end:
                logged_ttm = end - float(now)
            _compact_snap_depth(snap)
            row = _post_dump_fields(snap, cid, logged_ttm)
            snap["logged_at"] = now
    if row is not None:
        log_event("post_dump_book", **row)
        return
    if fetch_args is None:
        return
    up_tok, dn_tok, min_size, leg, _slug, end = fetch_args
    if not up_tok or not dn_tok:
        return
    try:
        up_row, dn_row = _fetch_books(up_tok, dn_tok, min_size)
    except Exception as exc:
        log_event(
            "post_dump_book_fail",
            condition_id=cid,
            error=str(exc)[:160],
        )
        return
    up_bid, up_ask, up_bids, up_asks, up_near = _post_dump_quote(up_row)
    dn_bid, dn_ask, dn_bids, dn_asks, dn_near = _post_dump_quote(dn_row)
    ttm = (end - float(now)) if end else None
    row = None
    with box["lock"]:
        snap = box["snaps"].get(cid)
        if not snap:
            return
        # A sell tick that landed while we were reading wins if it is newer.
        try:
            current = float(snap.get("book_ts"))
        except (TypeError, ValueError):
            current = None
        if current is not None and current > float(now) + 1e-9:
            return
        snap["up_bid"] = up_bid
        snap["up_ask"] = up_ask
        snap["dn_bid"] = dn_bid
        snap["dn_ask"] = dn_ask
        snap["up_bids"] = up_bids
        snap["up_asks"] = up_asks
        snap["dn_bids"] = dn_bids
        snap["dn_asks"] = dn_asks
        snap["up_ask_near"] = up_near
        snap["dn_ask_near"] = dn_near
        snap["book_ts"] = now
        snap["logged_at"] = now
        snap["leg_dumped"] = leg
        row = _post_dump_fields(snap, cid, ttm)
    if row is not None:
        log_event("post_dump_book", **row)


def _post_dump_book_pass(now: Optional[float] = None) -> None:
    """One off-path sample of every dumped bag. Never called from an order."""
    if now is None:
        now = time.time()
    box = _post_dump_box()
    cfg = box.get("cfg") or {}
    if not bool(cfg.get("post_dump_log_enabled", True)):
        return
    with box["lock"]:
        ids = list(box["snaps"])
    for cid in ids:
        _post_dump_book_emit(cid, float(now), allow_fetch=True)


def _post_dump_logger() -> None:
    """Sample immediately, then wait 0.25s or until a dump arms another read.

    ``Event.wait`` is the delay. This thread is the only one that fetches
    for the journal. It never places an order.
    """
    box = _post_dump_box()
    while not box.get("stop") and not globals().get("_shutdown"):
        try:
            _post_dump_book_pass()
        except Exception as exc:
            log_event("post_dump_book_fail", error=str(exc)[:160])
        if box.get("stop") or globals().get("_shutdown"):
            return
        box["wake"].wait(POST_DUMP_BOOK_INTERVAL_S)
        box["wake"].clear()


def _ensure_post_dump_logger() -> None:
    """Start the sampler if it is not already running."""
    box = _post_dump_box()
    with box["lock"]:
        thread = box.get("thread")
        if thread is not None and thread.is_alive():
            return
        box["stop"] = False
        box["started"] = True
        thread = threading.Thread(
            target=_post_dump_logger, name="post-dump-book", daemon=True,
        )
        box["thread"] = thread
    thread.start()


def _arm_post_dump_book(
    *,
    cfg: dict,
    intent: dict,
    cid: str,
    now: float,
    end_ts: float,
    up_bid: Any,
    up_ask: Any,
    dn_bid: Any,
    dn_ask: Any,
    up_tok: str,
    dn_tok: str,
    min_bid_size: float,
    leg_dumped: Optional[str] = None,
    up_bids: Any = None,
    up_asks: Any = None,
    dn_bids: Any = None,
    dn_asks: Any = None,
) -> None:
    """Hand the fire-time books to the sampler. No fetch and no log here.

    Called when the dump fires, before either leg's FAK. The sampler thread
    writes ``post_dump_book`` from these ladders and keeps reading until
    the window ends. The sell thread does not wait for that write.
    """
    if not bool(cfg.get("post_dump_log_enabled", True)):
        return
    box = _post_dump_box()
    with box["lock"]:
        prev = box["snaps"].get(cid) or {}
        box["snaps"][cid] = {
            "enabled": True,
            "sold_dump": True,
            "end_ts": end_ts,
            "slug": intent.get("slug"),
            "leg_dumped": leg_dumped,
            "up_bid": up_bid,
            "up_ask": up_ask,
            "dn_bid": dn_bid,
            "dn_ask": dn_ask,
            "up_bids_raw": up_bids,
            "up_asks_raw": up_asks,
            "dn_bids_raw": dn_bids,
            "dn_asks_raw": dn_asks,
            "depth_ready": False,
            "book_ts": now,
            "up_token": up_tok,
            "dn_token": dn_tok,
            "min_bid_size": min_bid_size,
            "logged_at": prev.get("logged_at"),
        }
        box["cfg"] = cfg
    _ensure_post_dump_logger()
    box["wake"].set()


def _shadow_bind():
    """Point the paper scrap at its jsonl file. No orders."""
    from buy import shadow_early_scrap as shadow

    path = getattr(_shadow_bind, "log_path", None)
    if path is None:
        path = REPO / "logs" / "shadow_early_scrap.jsonl"
        _shadow_bind.log_path = path
    shadow.set_log_path(path)
    shadow.set_logger(log_event)
    return shadow


def _price_tape_tick(cfg: dict, **fields: Any) -> None:
    """Queue one price-tape line from this tick's books. Never raises.

    The oracle mark is the in-memory cache. There is no book refetch and
    no sleep. A full queue or a writer error drops the line.
    """
    try:
        from buy import price_tape

        if not price_tape.tape_due(
            fields.get("ttm_s"),
            price_tape.window_s(cfg),
            enabled_flag=price_tape.enabled(cfg),
        ):
            return
        path = getattr(_price_tape_tick, "log_path", None)
        if path is None:
            path = REPO / "logs" / "price_tape.jsonl"
            _price_tape_tick.log_path = path
        price_tape.set_log_path(path)
        try:
            now_f = float(fields.get("ts"))
        except (TypeError, ValueError):
            now_f = time.time()
        fields["oracle"] = _oracle_mark(
            str(fields.get("condition_id") or ""), cfg, now_f,
        )
        price_tape.note(**fields)
    except Exception:
        return


def _shadow_early_tick(cfg: dict, **fields: Any) -> None:
    """Paper early-scrap tick. Failures are swallowed and no order is sent."""
    try:
        if not bool(cfg.get("shadow_early_scrap_enabled", False)):
            return
        shadow = _shadow_bind()
        for event, payload in shadow.observe(cfg, **fields):
            log_event(event, **payload)
    except Exception:
        return


def _shadow_early_close(cfg: dict, **fields: Any) -> None:
    """One paper result when the sell window ends. Log only."""
    try:
        if not bool(cfg.get("shadow_early_scrap_enabled", False)):
            return
        shadow = _shadow_bind()
        for event, payload in shadow.close_bag(cfg, **fields):
            log_event(event, **payload)
    except Exception:
        return


def _manage_sells_locked(cfg: dict, state: dict, chain: ChainReader) -> None:
    from math import isfinite
    from time import perf_counter

    now = time.time()
    _post_dump_box()["cfg"] = cfg
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
    scrap_floor = scrap_min_px(cfg.get("sell_scrap_min_px", None))
    base_opp_min = opp_min
    base_blind_enabled = blind_enabled
    base_scrap_floor = scrap_floor
    early_scrap = scrap_min_px(cfg.get("sell_scrap_early_threshold", None))
    min_bid_size = float(cfg.get("sell_min_bid_size") or 1.0)
    tol = float(cfg.get("position_tolerance") or 0.01)
    scrap_fraction = normalize_scrap_fraction(cfg.get("sell_scrap_fraction", 1.0))
    dry_run = bool(cfg.get("dry_run"))
    funder = os.getenv("FUNDER_ADDRESS") or ""
    funder_cs = to_checksum_address(funder) if funder else None
    _bind_sell_chain(chain, str(cfg.get("ctf_address") or ""), funder_cs)
    _apply_book_timeout(cfg)
    _sync_book_feed(cfg, state)
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
            _close_bag_risk(cid, intent, now=now, cfg=cfg)
            _shadow_early_close(
                cfg,
                condition_id=cid,
                slug=intent.get("slug"),
                intent=intent,
                now_s=now,
            )
            continue

        up_tok = str(intent.get("up_token") or "")
        dn_tok = str(intent.get("dn_token") or "")
        if not up_tok or not dn_tok:
            if sell_intent_hot(intent, now):
                _note_missing_token()
            continue

        # Same-tick books feed persist_ready and FAK; do not refetch after
        # persist-ready. Parallel UP/DN cuts sequential REST wait on the
        # armed path. The ~8.6–10.5s gap was poll_s plus mint-path work.
        with _io_unlocked():
            up_row, dn_row, book_meta = _books_for_sell(
                cfg, up_tok, dn_tok, min_bid_size,
            )
        _set_book_tick(book_meta)
        if book_meta.get("source") == "ws":
            legs = book_meta.get("legs") if isinstance(book_meta.get("legs"), dict) else {}
            up_leg = legs.get("up") if isinstance(legs.get("up"), dict) else {}
            dn_leg = legs.get("dn") if isinstance(legs.get("dn"), dict) else {}
            _queue_live_followup({
                "cid": cid,
                "slug": intent.get("slug"),
                "up_tok": up_tok,
                "dn_tok": dn_tok,
                "up_ws": up_leg.get("ws"),
                "dn_ws": dn_leg.get("ws"),
                "min_size": min_bid_size,
            })
        _log_book_shadow(cid, intent.get("slug"), book_meta)
        (
            up_bid, up_sz, up_bids, up_ask, _up_ask_sz, _up_asks, up_age,
        ) = _book_quote(up_row)
        (
            dn_bid, dn_sz, dn_bids, dn_ask, _dn_ask_sz, _dn_asks, dn_age,
        ) = _book_quote(dn_row)
        _whatsapp("note_bids", cid, up_bid, dn_bid)
        books = {"up": up_bids, "dn": dn_bids}
        ttm_s = (end_ts - now) if end_ts else None
        decision_at = perf_counter()
        # Per bag, per tick. Dump and winner do not read this pair.
        thr, fak_px, scrap_price_late = scrap_active_prices(cfg, ttm_s)
        # Early latch is off when the threshold is null. While it is on,
        # reset the shared locals so one bag's mode cannot leak, then
        # latch once on the first known ttm inside the scrap window.
        if early_scrap is not None:
            opp_min = base_opp_min
            blind_enabled = base_blind_enabled
            scrap_floor = base_scrap_floor
            stored_mode = intent.get("sell_scrap_mode")
            if scrap_mode_latch_due(
                ttm_s, cfg.get("sell_scrap_max_ttm_s"), stored_mode,
            ):
                stored_mode = scrap_mode_for_open(
                    up_bid, dn_bid, base_scrap_floor,
                )
                intent["sell_scrap_mode"] = stored_mode
                dirty = True
                side = lower_priced_side(up_bid, dn_bid)
                log_event(
                    "sell_scrap_mode",
                    condition_id=cid,
                    slug=intent.get("slug"),
                    mode=stored_mode,
                    leg=None if side is None else side[0],
                    loser_bid=None if side is None else side[1],
                    other_bid=None if side is None else side[2],
                    ttm=ttm_s,
                )
            applied = apply_scrap_mode(cfg, stored_mode, ttm_s)
            if applied is not None:
                thr = applied["threshold"]
                fak_px = applied["fak_px"]
                scrap_price_late = applied["late"]
                opp_min = applied["opposite_min"]
                blind_enabled = applied["blind"]
                scrap_floor = applied["floor"]
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
        _price_tape_tick(
            cfg,
            ts=now,
            slug=intent.get("slug"),
            condition_id=cid,
            ttm_s=ttm_s,
            up_bid=up_bid,
            up_ask=up_ask,
            dn_bid=dn_bid,
            dn_ask=dn_ask,
            up_bid_sz=up_sz,
            up_ask_sz=_up_ask_sz,
            dn_bid_sz=dn_sz,
            dn_ask_sz=_dn_ask_sz,
        )

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
            decision_at = perf_counter()
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
                            decision_at=decision_at,
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

        # Held-leg dump: after loser is sold, live-bid FAK once the held bid
        # has stayed under sell_dump_below AND the other side's bid has stayed
        # at or over sell_dump_other_min for sell_dump_persist_s. Either miss
        # resets the arm. Refires and the kept sell do not re-check.
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
        dump_retries = dump_refire_cap(cfg.get("sell_dump_fak_retries", 0))
        try:
            dump_limit_offset = float(cfg.get("sell_dump_limit_offset", 0.20))
            if not isfinite(dump_limit_offset):
                dump_limit_offset = 0.20
        except (TypeError, ValueError):
            dump_limit_offset = 0.20
        try:
            dump_limit_floor = float(cfg.get("sell_dump_limit_floor", 0.05))
            if not isfinite(dump_limit_floor):
                dump_limit_floor = 0.05
        except (TypeError, ValueError):
            dump_limit_floor = 0.05
        dump_ladder_step = float(cfg.get("sell_dump_ladder_step") or 0.04)
        dump_ladder_rungs = int(cfg.get("sell_dump_ladder_rungs", 4))
        # 0 or missing: no time gate (old behavior). Ladder rungs after the
        # first submitted order stay inside _run_dump_fak_with_refire and
        # are not re-checked against this cutoff.
        dump_max_ttm_s = float(cfg.get("sell_dump_max_ttm_s") or 0.0)
        sold_leg = intent.get("sold_leg")
        if (
            cfg.get("sell_dump_also_kept", False)
            and intent.get("sold_dump")
            and (
                intent.get("sell_dump_kept_cap_hold")
                or intent.get("sell_dump_kept_outcome") == "held_above_max"
            )
            and not intent.get("sell_exit_inflight")
            and sold_leg in tokens
            and kept_loser_open(intent)
            and not kept_dump_blocked(bids.get(sold_leg), cfg.get("sell_dump_kept_max_px"))
        ):
            # Preserve cap holds loaded from state written before the intent flag.
            intent["sell_dump_kept_cap_hold"] = True
            # A cap hold stays eligible each tick, independently of sell cooldown.
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
                end_ts=end_ts,
                decision_at=decision_at,
            )
            dirty = True
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
        other_leg = "dn" if held == "up" else ("up" if held == "dn" else None)
        other_bid = bids.get(other_leg) if other_leg else None
        # Missing key stays 0.30. Explicit 0 or null disables. Do not use `or`.
        other_min = cfg.get("sell_dump_other_min", 0.30)
        other_ok = sell_dump_other_confirms(other_bid, other_min)
        dump_armed = dump_price_ok and dump_ttm_ok and other_ok
        fire_d, armed_d, why_d = persist_ready(
            dump_armed,
            now_s=now,
            armed_ts=intent.get("sell_dump_armed_at"),
            persist_s=dump_persist_s,
        )
        intent["sell_dump_armed_at"] = armed_d
        if dump_price_ok and dump_ttm_ok and not other_ok:
            log_event(
                "sell_dump_persist",
                condition_id=cid,
                slug=intent.get("slug"),
                leg=held,
                why="other_low",
                bid=dump_bid,
                other_leg=other_leg,
                other_bid=other_bid,
                other_min=other_min,
                below=dump_below,
                persist_s=dump_persist_s,
            )
        elif dump_armed and why_d in {"armed", "waiting"}:
            arm_mark = (
                _oracle_mark(cid, cfg, time.time()) if why_d == "armed" else {}
            )
            log_event(
                "sell_dump_persist",
                condition_id=cid,
                slug=intent.get("slug"),
                leg=held,
                why=why_d,
                bid=dump_bid,
                other_leg=other_leg,
                other_bid=other_bid,
                other_min=other_min,
                below=dump_below,
                persist_s=dump_persist_s,
                **arm_mark,
            )
        if fire_d and held and not cooling:
            decision_at = perf_counter()
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
                _log_book_source(
                    cfg, path="dump", leg=held, bid=dump_bid,
                    cid=cid, slug=intent.get("slug"),
                )
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
                    first_limit = (
                        dump_deep_limit(
                            live_px, offset=dump_limit_offset,
                            floor=dump_limit_floor, clob_min=floor,
                        ) if dump_limit_offset > 0 else round(live_px, 4)
                    )
                    dump_fills: list = []
                    _arm_post_dump_book(
                        cfg=cfg,
                        intent=intent,
                        cid=cid,
                        now=now,
                        end_ts=end_ts,
                        up_bid=up_bid,
                        up_ask=up_ask,
                        dn_bid=dn_bid,
                        dn_ask=dn_ask,
                        up_tok=up_tok,
                        dn_tok=dn_tok,
                        min_bid_size=min_bid_size,
                        leg_dumped=held,
                        up_bids=up_bids,
                        up_asks=_up_asks,
                        dn_bids=dn_bids,
                        dn_asks=_dn_asks,
                    )
                    also_kept = bool(cfg.get("sell_dump_also_kept", False))
                    launch_kept = (
                        also_kept
                        and _dump_parallel_kept(cfg)
                        and not intent.get("sell_dump_kept_done")
                    )
                    kept_thread = None
                    kept_box = None
                    with _mark_sell_exit(intent):
                        if launch_kept:
                            # Size locally, start the kept FAK, then post the
                            # held FAK on this thread. No join before that post.
                            kept_thread, kept_box = _start_parallel_kept(
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
                                end_ts=end_ts,
                                decision_at=decision_at,
                            )
                        try:
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
                                    limit_offset=dump_limit_offset,
                                    limit_floor=dump_limit_floor,
                                    end_ts=end_ts,
                                    decision_at=decision_at,
                                )
                            )
                        finally:
                            if kept_thread is not None:
                                kept_thread.join()
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
                        filled_total = float(
                            intent.get("sell_dump_filled") or 0
                        ) + sold_total
                        intent["sell_dump_filled"] = filled_total
                        intent["sell_dump_limit"] = last_px
                        if dry_run:
                            intent["sell_dump_dry"] = True
                        log_event(
                            "sell_dump_done",
                            condition_id=cid,
                            slug=intent.get("slug"),
                            leg=held,
                            hedge_leg="dn" if held == "up" else "up",
                            sold=round(filled_total, 4),
                            avg_px=intent.get("sell_dump_fill_px"),
                            limit=intent["sell_dump_limit"],
                            first_limit=first_limit,
                            bid=dump_bid,
                            status=last_status,
                            **_oracle_mark(cid, cfg, time.time()),
                        )
                        _whatsapp("dump_filled", intent, cid, now=now, leg=held)
                        notify(
                            "Mint held dump",
                            f"{intent.get('slug')}\n{held} x{filled_total:.1f} "
                            f"@<{dump_below:.2f} (live {live_px:.3f})",
                            priority="default",
                        )
                        globals().get("_console_print", console.print)(
                            f"  [bold bright_yellow][DUMP OK][/] {held} {filled_total:.2f}  "
                            f"bid={live_px:.3f} (<{dump_below:.2f})"
                        )
                        if (
                            also_kept
                            and not launch_kept
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
                                end_ts=end_ts,
                                decision_at=decision_at,
                            )
                    elif sold_total >= tol:
                        intent["sell_dump_filled"] = float(
                            intent.get("sell_dump_filled") or 0
                        ) + sold_total
                    # Parallel kept started with the held FAK. Merge only
                    # after both legs have returned, before reclaim on this tick.
                    if launch_kept:
                        _finish_parallel_kept(intent, cid, kept_box)

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
            scrap_floor=scrap_floor,
            now_s=now,
            bid=bids.get(loser) if loser in ("up", "dn") else None,
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
            decision_at = perf_counter()
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
                _log_book_source(
                    cfg, path="scrap", leg=loser, bid=bids.get(loser),
                    cid=cid, slug=intent.get("slug"),
                )
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
                raw_bid = bids.get(loser)
                try:
                    live_bid = None if raw_bid is None else float(raw_bid)
                except (TypeError, ValueError):
                    live_bid = None
                loser_bid = float(raw_bid or thr)
                sweep_on = bool(cfg.get("sell_scrap_sweep_enabled", True))
                if sweep_on:
                    floor_limit = scrap_live_bid_limit(
                        loser_bid, thr, min_px=clob_min,
                    )
                else:
                    floor_rungs = loser_ladder_limits(
                        thr, floor, loser_bid, fak_px=fak_px, min_post=scrap_floor,
                    )
                    floor_limit = floor_rungs[0] if floor_rungs else None
                if not scrap_limit_allowed(live_bid, floor_limit, scrap_floor):
                    _log_sell_scrap_floor_skip(
                        condition_id=cid,
                        slug=intent.get("slug"),
                        leg=loser,
                        bid=live_bid,
                        floor=scrap_floor,
                        now_s=now,
                    )
                    size, latch = 0.0, "floor_skip"
                    l_tok = ""
                else:
                    l_tok = tokens[loser]
                    size, latch = _sell_inventory(
                        chain, ctf, funder_cs, l_tok, shares, tol,
                        "seen_loser_inventory", intent,
                    )
                if latch == "floor_skip":
                    pass
                elif latch == "await_inventory":
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
                            threshold=thr, cfg=cfg, now=now,
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
                                    min_post=scrap_floor,
                                    decision_at=decision_at,
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
                            logged_sold = float(intent.get("sell_filled") or 0)
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
                                sold=logged_sold,
                                avg_px=intent.get("sell_fill_px"),
                                bid=loser_bid,
                                status=last_status,
                                **done_extra,
                                **oracle_fields,
                                **_oracle_mark(cid, cfg, time.time()),
                            )
                            notify(
                                "Mint loser sold",
                                f"{intent.get('slug')}\n{loser} x{logged_sold:.1f} "
                                f"@bid<={thr:.2f} avg={intent.get('sell_fill_px')}",
                                priority="default",
                            )
                            globals().get("_console_print", console.print)(
                                f"  [bold bright_green][SELL OK][/] {loser} {logged_sold:.2f}  "
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
                                bid=_observed_loser_bid(loser, bids, seen_bids),
                                scrap_floor=scrap_floor,
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
                    threshold=thr, cfg=cfg, now=now,
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
                if (
                    blind_fire
                    and b_tok
                    and not scrap_limit_allowed(
                        bids.get(persist_leg), blind_px, scrap_floor,
                    )
                ):
                    _log_sell_scrap_floor_skip(
                        condition_id=cid,
                        slug=intent.get("slug"),
                        leg=persist_leg,
                        bid=bids.get(persist_leg),
                        floor=scrap_floor,
                        now_s=now,
                    )
                    blind_fire = False
                if blind_fire and b_tok:
                    decision_at = perf_counter()
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
                            decision_at=decision_at,
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
                            sold=float(intent.get("sell_filled") or 0),
                            status=blind_status,
                            **blind_fields,
                            **_oracle_mark(cid, cfg, time.time()),
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
                            bid=_observed_loser_bid(persist_leg, bids, seen_bids),
                            scrap_floor=scrap_floor,
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
                        cfg=cfg,
                        now=now,
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
                bid=_observed_loser_bid(persist_leg, bids, seen_bids),
                scrap_floor=scrap_floor,
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
                ask_levels={"up": _up_asks, "dn": _dn_asks},
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
        _observe_post_dump_book(
            cfg=cfg,
            intent=intent,
            cid=cid,
            now=now,
            end_ts=end_ts,
            ttm_s=ttm_s,
            up_bid=up_bid,
            up_ask=up_ask,
            dn_bid=dn_bid,
            dn_ask=dn_ask,
            up_tok=up_tok,
            dn_tok=dn_tok,
            min_bid_size=min_bid_size,
            up_bids=up_bids,
            up_asks=_up_asks,
            dn_bids=dn_bids,
            dn_asks=_dn_asks,
        )
        _shadow_early_tick(
            cfg,
            condition_id=cid,
            slug=intent.get("slug"),
            now_s=now,
            end_ts=end_ts,
            ttm_s=ttm_s,
            shares=shares,
            up_bid=up_bid,
            dn_bid=dn_bid,
            up_bids=up_bids,
            dn_bids=dn_bids,
            up_bid_size=up_sz,
            dn_bid_size=dn_sz,
        )

    _flush_deferred_rest(cfg)
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
        _note_book_fetch_failed()
        log_event("sell_cycle_error", error=str(sell_exc)[:240])
        write_loop_heartbeat("sell", "error", error=str(sell_exc)[:120])
        return "error"
    finally:
        _ensure_post_dump_logger()
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
    if not cfg.get("entry_enabled"):
        write_loop_heartbeat("mint", "disabled")
        return "disabled"
    if globals().get("_STATE_WRITER") is not None:
        _prune_state_tick(state, cfg, now)
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
        globals().get("_console_print", console.print)(
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
                if globals().get("_STATE_WRITER") is None:
                    atomic_save(STATE_FILE, state)
                else:
                    seq = commit_state(state, dirty=True, durable=True)
        if reason is None and globals().get("_STATE_WRITER") is not None:
            _wait_state_save(seq)
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
            reserved = pending_mint_reserve(state) + reclaim_hedge_cash_reserve(state)
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
            globals().get("_console_print", console.print)(
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
            globals().get("_console_print", console.print)(
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
            if globals().get("_STATE_WRITER") is None:
                atomic_save(STATE_FILE, state)
            else:
                seq = commit_state(state, dirty=True, durable=True)
    if reason is None and globals().get("_STATE_WRITER") is not None:
        _wait_state_save(seq)
    if reason is not None:
        write_loop_heartbeat("mint", reason, slug=pick.slug)
        return reason

    globals().get("_console_print", console.print)(
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
            fail_msg = intent.get("errorMsg")
        else:
            intent["transaction_id"] = tx_id
            intent["submitted_at"] = time.time()
            intent["status"] = "pending"
            fail_msg = None
        if globals().get("_STATE_WRITER") is None:
            atomic_save(STATE_FILE, state)
        else:
            seq = commit_state(state, dirty=True, durable=True)
    if globals().get("_STATE_WRITER") is not None:
        _wait_state_save(seq)
    if not tx_id:
        globals().get("_console_print", console.print)(f"  [dim red][MINT FAIL][/] {err}")
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

    globals().get("_console_print", console.print)(f"  [bold bright_green][MINT ▶][/] tx={tx_id[:18]}…")
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
    globals()["_keepalive_cfg"] = loaded
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

    def submit(cid: str, approve: bool):
        # RedeemDesk queued its submitting job before releasing its state lock.
        writer = globals().get("_STATE_WRITER")
        if writer is not None and not writer.flush():
            return None, "durable redeem claim save failed", {}
        return submit_redeem(cfg(), chain, cid, approve)

    io = RedeemIO(
        payout_denominator=lambda cid: chain.payout_denominator(ctf(), cid),
        payout_numerator=lambda cid, index: chain.payout_numerator(ctf(), cid, index),
        balance=lambda token: chain.position_balance(ctf(), funder, token),
        is_approved=lambda: chain.is_approved_for_all(
            ctf(), funder, str(cfg()["standard_adapter_address"])
        ),
        submit=submit,
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



def _scale_fill_amount(raw_val: Any, expected_max: float = 0.0) -> float:
    """Parse a CLOB fill amount, dividing by 1e6 only for unscaled integer base units."""
    if raw_val is None or raw_val == "":
        return 0.0
    try:
        val = float(raw_val)
    except (TypeError, ValueError):
        return 0.0
    if not math.isfinite(val) or val <= 0:
        return 0.0
    raw_text = str(raw_val).strip().lower()
    if "." in raw_text or "e" in raw_text:
        return val
    if val > max(float(expected_max or 0.0) * 100.0, 10_000.0):
        return val / 1_000_000.0
    return val


def _fivemin_buy(token: str, shares: float, limit: float, dry: bool, *,
                 post_meta: Optional[dict] = None) -> dict:
    captured = []
    timing = {}
    bought, status = _fak_buy(token, shares, limit, dry, capture=captured, timing=timing,
                              post_meta=post_meta)
    if dry:
        bought = shares
    if float(bought or 0.0) < 0.01 and not dry:
        return {"shares": 0.0, "cost": 0.0, "status": status, "fees": None, "timing": timing}
    raw = captured[0] if captured else {}
    avg = buy_fill_vwap(raw, bought)
    cost = float(bought) * (avg if avg is not None else limit)
    if bought > 0 and raw.get("makingAmount") is not None:
        try:
            reported_cost = float(raw["makingAmount"])
            if (
                math.isfinite(reported_cost)
                and 0 < reported_cost <= shares * limit + 0.01
                and 0.01 <= (reported_cost / float(bought)) <= 1.0
            ):
                cost = reported_cost
        except (TypeError, ValueError, ZeroDivisionError):
            pass
    return {"shares": float(bought), "cost": cost, "status": status,
            "fees": raw.get("fee") or raw.get("feeAmount"), "timing": timing}


def _fivemin_sell(row: dict, position: dict, book: dict, cfg: dict, now: float) -> dict:
    fills = []
    sold, status, limit, attempts, bid = _run_dump_fak_with_refire(
        token_id=position["token"], size=position["shares"], initial_bid=book["bid"],
        initial_bids=book.get("bids", []), held=position["side"], slug=row["slug"],
        condition_id=row["condition_id"], ttm_s=row["end"] - now,
        floor=float(cfg.get("sell_clob_min_price", 0.01)), min_bid_size=0.0,
        retries=cfg.get("sell_dump_fak_retries", 0),
        ladder_step=float(cfg.get("sell_dump_ladder_step", 0.04)),
        ladder_rungs=int(cfg.get("sell_dump_ladder_rungs", 4)), dry_run=position["dry_run"],
        tol=0.01, fills=fills, end_ts=row["end"],
        limit_offset=float(cfg["fivemin_dump_limit_offset"]),
        limit_floor=float(cfg["fivemin_dump_limit_floor"]),
    )
    if position["dry_run"]:
        sold = position["shares"]
    average = record_fill_px({}, "price", fills)
    price = average if average is not None else float(limit or book["bid"])
    return {"shares": sold, "proceeds": sold * price, "price": price, "status": status}


def _fivemin_reserves(state: dict, cfg: dict) -> float:
    with STATE_LOCK:
        reserve = pending_mint_reserve(state) + reclaim_hedge_cash_reserve(state)
    if cfg.get("entry_enabled") and not cfg.get("dry_run", True):
        reserve += float(cfg["shares"])
    return reserve


def _fivemin_redeem(state: dict, row: dict, now: float) -> None:
    from buy.mint_redeem import new_job

    with STATE_LOCK:
        jobs = state.setdefault("redeems", {})
        if row["condition_id"] not in jobs:
            jobs[row["condition_id"]] = new_job(
                row["condition_id"], row["up_token"], row["dn_token"], now,
                slug=row["slug"], end_ts=row["end"], source="fivemin",
            )
            commit_state(state, dirty=True)


def _fivemin_label(start: float) -> str:
    from buy.whatsapp_notify import bag_start_label
    return bag_start_label(start)


def _fivemin_alert(text: str, *, kind: str) -> None:
    try:
        _BAG_ALERTS.notifier.send(text, kind=kind)
    except Exception:
        pass


def _fivemin_gamma_winner(cfg: dict, slug: str):
    response = thread_session("fivemin-gamma").get(
        str(cfg["gamma_url"]).rstrip("/") + "/events", params={"slug": slug}, timeout=5,
    )
    response.raise_for_status()
    events = response.json()
    if isinstance(events, dict):
        events = [events]
    for event in events:
        for market in event.get("markets", []):
            prices = market.get("outcomePrices", [])
            outcomes = market.get("outcomes", ["Up", "Down"])
            if isinstance(prices, str):
                prices = json.loads(prices)
            if isinstance(outcomes, str):
                outcomes = json.loads(outcomes)
            for label, price in zip(outcomes, prices):
                if float(price) >= 1 - 1e-9 and str(label).lower() in ("up", "down"):
                    return str(label).lower()
    return None


def main() -> int:
    global _intent_store, _STATE_WRITER, _HEARTBEAT_WRITER, _OUTPUT_POOL
    signal.signal(signal.SIGINT, _signal_handler)
    signal.signal(signal.SIGTERM, _signal_handler)
    log_setup()
    lock = acquire_lock()

    try:
        cfg = load_strategy()
        _apply_book_timeout(cfg)
    except Exception as exc:
        globals().get("_console_print", console.print)(f"[bold red]strategy load failed:[/] {exc}")
        return 1

    state = load_state()
    from buy.state_writer import StateWriter

    _STATE_WRITER = StateWriter(
        STATE_FILE, archive_path=REPO / "logs" / "state_archive.jsonl",
        on_error=_state_save_error, log=log_event,
    )
    _HEARTBEAT_WRITER = StateWriter(HEARTBEAT_FILE, log=log_event)
    _OUTPUT_POOL = ThreadPoolExecutor(max_workers=1, thread_name_prefix="mintbot-console")
    atexit.register(_STATE_WRITER.flush, timeout=10)
    atexit.register(_HEARTBEAT_WRITER.flush, timeout=10)
    _prune_state_tick(state, cfg, time.time(), force=True)
    globals()["_keepalive_cfg"] = cfg
    _start_clob_keepalive(state)
    for intent in state.get("intents", {}).values():
        if intent.get("status") in ACTIVE_STATUSES and float(intent.get("end_ts") or 0) > time.time():
            _schedule_order_prewarm(
                str(intent.get("up_token") or ""), str(intent.get("dn_token") or ""),
            )
    _intent_store = IntentStore(state, lock=STATE_LOCK)
    gateway = MarketGateway(
        gamma_url=str(cfg["gamma_url"]),
        data_api_url=str(cfg["data_api_url"]),
        discover_cache_s=8.0,
    )
    sell_chain = ChainReader(str(cfg["rpc_url"]))
    mint_chain = ChainReader(str(cfg["rpc_url"]))
    cfg_box: Dict[str, Any] = {"cfg": cfg}

    globals().get("_console_print", console.print)(
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
        globals().get("_console_print", console.print)("[bold black on yellow]▶ DRY RUN[/] [dim]no relayer submits[/]")
    if not cfg["entry_enabled"]:
        globals().get("_console_print", console.print)("[bold yellow]▶ ENTRY OFF[/] [dim]set entry_enabled=true to mint[/]")

    log_event(
        "startup",
        dry_run=cfg["dry_run"],
        entry_enabled=cfg["entry_enabled"],
        shares=cfg["shares"],
        max_ttm=cfg["enter_max_ttm_min"],
        series=cfg["series_slugs"],
        loops=("sell", "mint", "oracle", "redeem"),
        oracle_log_enabled=bool(cfg.get("oracle_log_enabled", True)),
        sell_plan=sell_plan_banner(cfg),
        mint_sequential=seq_settings(cfg)[0],
        mint_seq_lead_s=seq_settings(cfg)[1],
        mint_seq_cutoff_s=seq_settings(cfg)[2],
        redeem_enabled=redeem_settings(cfg).enabled,
    )
    _whatsapp("startup", cfg)
    if cfg.get("oracle_log_enabled", True):
        late_s = float(cfg.get("sell_late_window_s") or 0.0)
        if late_s > 0:
            veto = f"[dim](+ late loser-scrap veto ≤{late_s:g}s TTM)[/]"
        else:
            veto = "[dim](audit only; scrap veto off)[/]"
        globals().get("_console_print", console.print)(
            "[dim]▶ oracle tape[/] logs/oracle_twap.jsonl  " + veto
        )

    stop_check = {"at": None, "exists": False}

    def should_stop() -> bool:
        if _shutdown:
            return True
        now = time.monotonic()
        if stop_check["at"] is None or now - stop_check["at"] >= 1.0:
            stop_check["exists"] = STOP_FILE.exists()
            stop_check["at"] = now
        return stop_check["exists"]

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
        backoff, ws_tick, missing = take_book_pace()
        with STATE_LOCK:
            return cycle_sleep_s(
                current,
                state,
                time.time(),
                fetch_backoff=backoff,
                ws_book=ws_tick,
                missing_token=missing,
            )

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
            globals().get("_console_print", console.print)(f"[red]cycle_error[/] {exc}"),
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
    if cfg.get("fivemin_enabled", False) or cfg.get("sniper_5m_enabled", False):
        from buy.fivemin import FiveMinIO, Runner, Strategy, dry_run as fivemin_dry_run, load_state as load_fivemin_state, settings
        from buy.binance_trades import BinanceTrades, PriceGrid
        from buy.clob_book_ws import ClobBookFeed
        from buy.fivemin_books import RestBookFeed, WsFallbackBookFeed
        from buy.fivemin_tape import BookTape
        from buy.oracle_log import RtdsTwapFeed
        from buy.state_writer import snapshot_state

        def run_fivemin() -> None:
            path = REPO / "logs" / "fivemin_state.json"
            path.parent.mkdir(parents=True, exist_ok=True)
            writer = StateWriter(path, log=log_event)
            runner = None
            try:
                current_cfg = lambda: cfg_box.get("cfg") or cfg
                io = FiveMinIO(
                    buy=_fivemin_buy, sell=_fivemin_sell,
                    reserves=lambda current: _fivemin_reserves(state, current),
                    redeem=lambda row, now: _fivemin_redeem(state, row, now),
                    save=lambda payload: writer.submit(snapshot_state(payload)),
                    durable=lambda: writer.flush(timeout=10), log=log_event, alert=_fivemin_alert,
                    clock=time.time, label=_fivemin_label,
                    gamma_winner=lambda slug: _fivemin_gamma_winner(current_cfg(), slug),
                    warmup=_fak_warmup_tokens,
                )
                if not fivemin_dry_run(current_cfg()):
                    io.warmup([])
                grid = PriceGrid()
                ws_error_at = None

                def fivemin_ws_error(error, delay):
                    nonlocal ws_error_at
                    now = time.monotonic()
                    if ws_error_at is None or now - ws_error_at >= 30:
                        ws_error_at = now
                        log_event("fivemin_ws_error", error=str(error)[:240], retry_s=delay)

                sniper_cfg = settings(current_cfg())
                extra_urls: list[str] = []
                if sniper_cfg.get("sniper_extra_ws_enabled", True):
                    for u in sniper_cfg.get("sniper_extra_ws_urls") or []:
                        if u and str(u) not in extra_urls:
                            extra_urls.append(str(u))
                if sniper_cfg.get("sniper_futures_ws_enabled") and sniper_cfg.get("sniper_futures_ws_url"):
                    fu = str(sniper_cfg["sniper_futures_ws_url"])
                    if fu not in extra_urls:
                        extra_urls.append(fu)
                runner = Runner(
                    Strategy(io, load_fivemin_state(path)), current_cfg,
                    MarketGateway(gamma_url=str(cfg["gamma_url"]), data_api_url=str(cfg["data_api_url"]), discover_cache_s=20),
                    ChainReader(str(cfg["rpc_url"])), to_checksum_address(funder_env) if funder_env else "",
                    grid, BinanceTrades(
                        grid, url=str(current_cfg().get("fivemin_binance_url")),
                        on_error=lambda error: log_event("fivemin_binance_error", error=str(error)[:240]),
                        on_event=lambda event, **fields: log_event(event, **fields),
                        on_tick=lambda ms, px: runner.on_binance_tick(ms, px) if runner is not None else None,
                        extra_urls=extra_urls,
                    ), RtdsTwapFeed(), (
                        WsFallbackBookFeed(
                            ClobBookFeed(on_error=fivemin_ws_error, custom_features=False,
                                         token_msg_fresh=True),
                            RestBookFeed(fetch_pair=lambda u, d, fail=None: _fetch_books(u, d, 0.0, fail), log=log_event),
                            ws_stale_s=lambda: float(settings(current_cfg())["fivemin_book_ws_stale_s"]),
                            log=log_event,
                        ) if cfg.get("fivemin_book_ws", False) else
                        RestBookFeed(fetch_pair=lambda u, d, fail=None: _fetch_books(u, d, 0.0, fail), log=log_event)
                    ),
                    tape=BookTape(REPO / "logs" / "fivemin_books", log=log_event),
                )
                runner.gamma_fetch = lambda slug: _fivemin_gamma_winner(current_cfg(), slug)
                runner.start()
                run_job_loop(name="fivemin", tick=runner.tick, sleep_s=runner.sleep_s,
                             should_stop=should_stop,
                             on_error=lambda exc: log_event("fivemin_error", error=str(exc)[:240]))
            finally:
                if runner is not None:
                    runner.stop()
                writer.stop(timeout=10)

        threading.Thread(target=run_fivemin, name="mintbot-fivemin", daemon=True).start()
    while not should_stop():
        time.sleep(0.25)
    sell_thread.join(timeout=15)
    mint_thread.join(timeout=15)
    oracle_thread.join(timeout=15)
    redeem_thread.join(timeout=15)

    if not _STATE_WRITER.stop(timeout=10):
        log_event("state_save_fail", error="shutdown state flush incomplete")
    _HEARTBEAT_WRITER.stop(timeout=10)
    globals().get("_console_print", console.print)("[dim]mintbot stopped[/]")
    _OUTPUT_POOL.shutdown(wait=True)
    if _LOG_LISTENER is not None:
        _LOG_LISTENER.stop()
    try:
        lock.close()
    except Exception:
        pass
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
