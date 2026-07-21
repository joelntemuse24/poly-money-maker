#!/usr/bin/env python3
"""Live shadow simulator — paper-trades BTC up/down markets using real books.

Series (5m / 15m / ...) and sell policy come from sim/strategy.sim.json.
Does NOT send orders. Does NOT import bot.py.
All state under sim_data/<data_tag>/ only. Safe to run beside polybot.
"""

from __future__ import annotations

import argparse
import atexit
import logging
import os
import signal
import sys
import time
from logging.handlers import RotatingFileHandler
from typing import Any, Dict, List, Optional

from . import config as cfg
from .config import (
    assert_path_safe,
    ensure_dirs,
    load_sim,
    load_strategy,
    series_slug_list,
)
from .discovery import Market, discover_btc_markets, fetch_books_parallel
from .entry import SetEntryEstimate, estimate_set_cost_from_books
from .fills import simulate_fak_sell
from .policy import evaluate
from .store import (
    append_result,
    append_tick,
    disk_usage_mb,
    is_disk_full,
    load_state,
    prune_old_files,
    save_state,
    save_trade,
    summarize_results,
)

_shutdown = False
_lock_fd = None


def _setup_log() -> logging.Logger:
    ensure_dirs()
    log = logging.getLogger("shadow")
    log.setLevel(logging.INFO)
    log.handlers.clear()
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s", "%H:%M:%S")
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    log.addHandler(sh)
    fh = RotatingFileHandler(cfg.LOG_FILE, maxBytes=1_000_000, backupCount=2)
    fh.setFormatter(fmt)
    log.addHandler(fh)
    return log


def _on_signal(signum, frame):
    global _shutdown
    _shutdown = True


def acquire_lock() -> None:
    """Single-instance lock under sim_data/. Prevents two shadow processes."""
    global _lock_fd
    ensure_dirs()
    assert_path_safe(cfg.LOCK_FILE)
    fd = open(cfg.LOCK_FILE, "a+")
    try:
        if os.name == "nt":
            import msvcrt

            fd.seek(0)
            msvcrt.locking(fd.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(fd.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except (OSError, BlockingIOError) as e:
        fd.close()
        raise SystemExit(f"Another shadow instance is already running ({cfg.LOCK_FILE}): {e}")
    fd.seek(0)
    fd.truncate()
    fd.write(str(os.getpid()))
    fd.flush()
    _lock_fd = fd

    def _release():
        global _lock_fd
        if _lock_fd is None:
            return
        try:
            if os.name == "nt":
                import msvcrt

                _lock_fd.seek(0)
                msvcrt.locking(_lock_fd.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(_lock_fd.fileno(), fcntl.LOCK_UN)
        except Exception:
            pass
        try:
            _lock_fd.close()
        except Exception:
            pass
        _lock_fd = None

    atexit.register(_release)


def write_heartbeat() -> None:
    try:
        assert_path_safe(cfg.HEARTBEAT_FILE)
        with open(cfg.HEARTBEAT_FILE, "w", encoding="utf-8") as f:
            f.write(str(time.time()))
    except Exception:
        pass


def new_position(
    market: Market,
    sim: dict,
    now: float,
    entry: Optional[SetEntryEstimate] = None,
) -> dict:
    shares = float(sim["shares"])
    set_cost = float(entry.set_cost) if entry and entry.set_cost is not None else float(sim["set_cost"])
    position = {
        "condition_id": market.condition_id,
        "slug": market.slug,
        "question": market.question,
        "end_ts": market.end_ts,
        "series_slug": getattr(market, "series_slug", "") or "",
        "up_token": market.up_token,
        "dn_token": market.dn_token,
        "entered_at": now,
        "set_cost": set_cost,
        "shares": shares,
        "entry_cost_total": set_cost * shares,
        "up_size": shares,
        "dn_size": shares,
        "sold_up": False,
        "sold_dn": False,
        "sell_proceeds": 0.0,
        "hedge_proceeds": 0.0,
        "sell_filled": 0.0,
        "sell_avg_px": 0.0,
        "sell_leg": None,
        "sell_reason": None,
        "sell_seconds_left": None,
        "sell_up_bid": None,
        "sell_dn_bid": None,
        "triggered": False,
        "trigger_attempts": 0,
        "fill_fails": 0,
        "last_sell_attempt_at": 0.0,
        "last_tick_at": 0.0,
        "events": [],
        "status": "open",
        "winner": None,
        "redeem_value": 0.0,
        "pnl": None,
        "last_up_bid": None,
        "last_dn_bid": None,
    }
    if entry is not None:
        position.update(
            {
                "entry_model": "live_books",
                "entry_up_px": entry.up.avg_price,
                "entry_dn_px": entry.dn.avg_price,
                "entry_set_cost": entry.set_cost,
                "entry_filled_up": entry.up.filled,
                "entry_filled_dn": entry.dn.filled,
                "entry_imbalance": entry.imbalance,
                "entry_up_levels": entry.up.levels_used,
                "entry_dn_levels": entry.dn.levels_used,
            }
        )
    return position


def try_resolve(pos: dict, up_bid: Optional[float], dn_bid: Optional[float], sim: dict) -> bool:
    edge = float(sim["resolve_bid_edge"])
    if up_bid is not None and up_bid >= edge and (dn_bid is None or dn_bid <= 1.0 - edge + 0.05):
        pos["winner"] = "up"
        return True
    if dn_bid is not None and dn_bid >= edge and (up_bid is None or up_bid <= 1.0 - edge + 0.05):
        pos["winner"] = "dn"
        return True
    if up_bid is not None and dn_bid is not None:
        if up_bid >= 0.90 and dn_bid <= 0.15:
            pos["winner"] = "up"
            return True
        if dn_bid >= 0.90 and up_bid <= 0.15:
            pos["winner"] = "dn"
            return True
    return False


def finalize(pos: dict, log: logging.Logger) -> dict:
    up = float(pos["up_size"])
    dn = float(pos["dn_size"])
    winner = pos.get("winner")
    resolved = winner is not None or (up < 0.01 and dn < 0.01)
    redeem = 0.0
    if winner == "up" and up > 0:
        redeem = up * 1.0
    elif winner == "dn" and dn > 0:
        redeem = dn * 1.0
    elif winner is None and up < 0.01 and dn < 0.01:
        redeem = 0.0

    pos["redeem_value"] = redeem
    pos["resolved"] = bool(resolved)
    # Unresolved (no winner, still holding legs) is infra/data failure — do not
    # charge full entry as strategy PnL (was poisoning summaries after ENOSPC).
    if not resolved and winner is None and (up >= 0.01 or dn >= 0.01):
        pnl = 0.0
        pos["status"] = "unresolved"
    else:
        pnl = (
            float(pos["sell_proceeds"])
            + float(pos["hedge_proceeds"])
            + redeem
            - float(pos["entry_cost_total"])
        )
        pos["status"] = "closed"
    pos["pnl"] = round(pnl, 6)
    pos["closed_at"] = time.time()

    events = pos.get("events") or []
    if len(events) > 50:
        pos["events"] = events[-50:]

    row = {
        "ts": time.time(),
        "condition_id": pos["condition_id"],
        "slug": pos["slug"],
        "series_slug": pos.get("series_slug"),
        "question": pos["question"],
        "set_cost": pos["set_cost"],
        "shares": pos["shares"],
        "entry_cost_total": pos["entry_cost_total"],
        "sell_leg": pos.get("sell_leg"),
        "sell_reason": pos.get("sell_reason"),
        "sell_filled": pos.get("sell_filled"),
        "sell_avg_px": pos.get("sell_avg_px"),
        "sell_seconds_left": pos.get("sell_seconds_left"),
        "sell_up_bid": pos.get("sell_up_bid"),
        "sell_dn_bid": pos.get("sell_dn_bid"),
        "sell_proceeds": pos.get("sell_proceeds"),
        "hedge_proceeds": pos.get("hedge_proceeds"),
        "triggered": pos.get("triggered"),
        "trigger_attempts": pos.get("trigger_attempts"),
        "fill_fails": pos.get("fill_fails"),
        "winner": winner,
        "resolved": bool(resolved),
        "redeem_value": redeem,
        "pnl": pos["pnl"],
        "final_up": up,
        "final_dn": dn,
    }
    if pos.get("entry_model"):
        for key in (
            "entry_model",
            "entry_up_px",
            "entry_dn_px",
            "entry_set_cost",
            "entry_filled_up",
            "entry_filled_dn",
            "entry_imbalance",
            "entry_up_levels",
            "entry_dn_levels",
        ):
            row[key] = pos.get(key)
    append_result(row)
    save_trade(pos["condition_id"], pos)
    log.info(
        "CLOSED %s pnl=%.4f sell=%s@%.3f x%.2f redeem=%.2f winner=%s resolved=%s fails=%s sell_bids=U%s/D%s",
        pos["slug"],
        pos["pnl"],
        pos.get("sell_leg"),
        float(pos.get("sell_avg_px") or 0),
        float(pos.get("sell_filled") or 0),
        redeem,
        winner,
        resolved,
        pos.get("fill_fails"),
        pos.get("sell_up_bid"),
        pos.get("sell_dn_bid"),
    )
    return row


def apply_decision(pos, decision, up_book, dn_book, sim, strategy, now, seconds_left, log):
    if decision.action == "none":
        return

    cooldown = float(strategy["sell_cooldown_s"])
    if now - float(pos.get("last_sell_attempt_at") or 0) < cooldown:
        return

    pos["triggered"] = True
    pos["trigger_attempts"] = int(pos.get("trigger_attempts") or 0) + 1
    pos["last_sell_attempt_at"] = now

    if decision.action in ("sell_up", "hedge_up"):
        leg, size, book = "up", float(pos["up_size"]), up_book
    else:
        leg, size, book = "dn", float(pos["dn_size"]), dn_book

    if size < 0.01 or book is None:
        return

    limit = decision.limit_price
    fill = simulate_fak_sell(
        size=size,
        bids=book.bids,
        limit_price=limit,
        model=sim["fill_model"],
        slippage=float(sim["fill_slippage"]),
        seconds_left=seconds_left,
        no_fill_after_s=float(sim["no_fill_after_s"]),
    )

    max_ev = int(sim.get("max_events_per_pos", 200))
    evt = {
        "ts": now,
        "seconds_left": round(seconds_left, 3),
        "action": decision.action,
        "reason": decision.reason,
        "limit": limit,
        "best_bid": book.best_bid,
        "best_bid_size": book.best_bid_size,
        "filled": fill.filled,
        "avg_price": fill.avg_price,
        "fill_reason": fill.reason,
        "levels_used": fill.levels_used,
    }
    events = pos.setdefault("events", [])
    events.append(evt)
    if len(events) > max_ev:
        pos["events"] = events[-max_ev:]

    if fill.filled <= 0:
        pos["fill_fails"] = int(pos.get("fill_fails") or 0) + 1
        log.info(
            "MISS  %s %s reason=%s bid=%s ttm=%.1fs fail=%s",
            pos["slug"],
            decision.action,
            decision.reason,
            book.best_bid,
            seconds_left,
            fill.reason,
        )
        return

    if leg == "up":
        pos["up_size"] = max(0.0, float(pos["up_size"]) - fill.filled)
        if pos["up_size"] < 0.01:
            pos["sold_up"] = True
            pos["up_size"] = 0.0
    else:
        pos["dn_size"] = max(0.0, float(pos["dn_size"]) - fill.filled)
        if pos["dn_size"] < 0.01:
            pos["sold_dn"] = True
            pos["dn_size"] = 0.0

    if decision.action.startswith("hedge"):
        pos["hedge_proceeds"] = float(pos["hedge_proceeds"]) + fill.notional
    else:
        prev_f = float(pos.get("sell_filled") or 0)
        prev_n = float(pos.get("sell_proceeds") or 0)
        new_f = prev_f + fill.filled
        new_n = prev_n + fill.notional
        pos["sell_filled"] = new_f
        pos["sell_proceeds"] = new_n
        pos["sell_avg_px"] = new_n / new_f if new_f else 0.0
        pos["sell_leg"] = leg
        pos["sell_reason"] = decision.reason
        pos["sell_seconds_left"] = round(seconds_left, 3)
        pos["sell_up_bid"] = up_book.best_bid if up_book else None
        pos["sell_dn_bid"] = dn_book.best_bid if dn_book else None

    log.info(
        "FILL  %s %s %.4f@%.4f (%s) ttm=%.1fs bids=U%s/D%s rem_up=%.2f rem_dn=%.2f",
        pos["slug"],
        decision.action,
        fill.filled,
        fill.avg_price,
        decision.reason,
        seconds_left,
        up_book.best_bid if up_book else None,
        dn_book.best_bid if dn_book else None,
        pos["up_size"],
        pos["dn_size"],
    )


def choose_sleep(positions: Dict[str, dict], markets: List[Market], sim: dict, strategy: dict) -> float:
    now = time.time()
    sell_w = float(strategy["sell_window_min"])
    min_ttm = 999.0
    for m in markets:
        min_ttm = min(min_ttm, m.seconds_left(now) / 60.0)
    for p in positions.values():
        if p.get("status") != "open":
            continue
        min_ttm = min(min_ttm, (float(p["end_ts"]) - now) / 60.0)
    if min_ttm <= sell_w:
        return float(sim["poll_sell_s"])
    if min_ttm <= 2.0:
        return float(sim["poll_near_s"])
    return float(sim["poll_far_s"])


def should_record_tick(pos: dict, seconds_left: float, strategy: dict, sim: dict, now: float) -> bool:
    if not bool(sim.get("record_ticks", False)):
        return False
    if is_disk_full():
        return False
    sell_w_s = float(strategy["sell_window_min"]) * 60.0
    last = float(pos.get("last_tick_at") or 0)
    if seconds_left <= sell_w_s:
        min_gap = float(sim.get("tick_sample_sell_s", 5.0))
    elif seconds_left <= 120:
        min_gap = float(sim.get("tick_sample_near_s", 10.0))
    else:
        min_gap = float(sim.get("tick_sample_far_s", 30.0))
    return (now - last) >= min_gap


def run_cycle(state: dict, strategy: dict, sim: dict, log: logging.Logger) -> None:
    now = time.time()
    positions: Dict[str, dict] = state.setdefault("positions", {})

    try:
        markets = discover_btc_markets(
            series_slugs=series_slug_list(sim),
            horizon_min=float(sim["discover_horizon_min"]),
            lookback_min=2.0,
            cache_s=float(sim.get("discover_refresh_s", 25.0)),
        )
    except Exception as e:
        log.error("discover failed: %s", e)
        return

    enter_max = float(sim["enter_max_ttm_min"])
    enter_min = float(sim["enter_min_ttm_min"])
    entry_books = {}
    if bool(sim.get("use_live_entry_books", False)):
        attempts = state.setdefault("entry_attempts", {})
        retry_s = float(sim.get("entry_retry_s", 15.0))
        candidates = []
        entry_token_ids = set()
        for m in markets:
            mts = m.minutes_to_start(now)
            last_attempt = attempts.get(m.condition_id) or {}
            if m.condition_id in positions or not (enter_min <= mts <= enter_max):
                continue
            if now - float(last_attempt.get("ts") or 0) < retry_s:
                continue
            candidates.append(m)
            entry_token_ids.add(m.up_token)
            entry_token_ids.add(m.dn_token)
        entry_books = fetch_books_parallel(
            list(entry_token_ids),
            max_workers=int(sim.get("book_workers", 6)),
        )
        for m in candidates:
            up_book = entry_books.get(m.up_token)
            dn_book = entry_books.get(m.dn_token)
            estimate = estimate_set_cost_from_books(
                shares=float(sim["shares"]),
                up_asks=up_book.asks if up_book and up_book.ok else [],
                dn_asks=dn_book.asks if dn_book and dn_book.ok else [],
                max_set_cost=float(sim["max_set_cost"]),
                limit_price=float(sim["entry_limit_price"]),
                model=str(sim["entry_fill_model"]),
                slippage=float(sim["entry_slippage"]),
            )
            attempts[m.condition_id] = {
                "ts": now,
                "slug": m.slug,
                "reason": estimate.reason,
                "entry_up_px": estimate.up.avg_price,
                "entry_dn_px": estimate.dn.avg_price,
                "entry_set_cost": estimate.set_cost,
                "entry_filled_up": estimate.up.filled,
                "entry_filled_dn": estimate.dn.filled,
                "entry_imbalance": estimate.imbalance,
            }
            if not estimate.admissible:
                log.info(
                    "ENTRY SKIP %s reason=%s up=%.2f dn=%.2f set_cost=%s",
                    m.slug,
                    estimate.reason,
                    estimate.up.filled,
                    estimate.dn.filled,
                    f"{estimate.set_cost:.3f}" if estimate.set_cost is not None else "-",
                )
                continue
            positions[m.condition_id] = new_position(m, sim, now, estimate)
            log.info(
                "ENTER %s mts=%.1fm set_cost=%.3f shares=%.1f model=live_books",
                m.slug,
                m.minutes_to_start(now),
                estimate.set_cost,
                sim["shares"],
            )
        max_attempts = int(sim.get("max_entry_attempts", 500))
        if len(attempts) > max_attempts:
            oldest = sorted(attempts, key=lambda cid: float(attempts[cid].get("ts") or 0))
            for condition_id in oldest[: len(attempts) - max_attempts]:
                attempts.pop(condition_id, None)
    else:
        for m in markets:
            mts = m.minutes_to_start(now)
            if m.condition_id in positions:
                continue
            if enter_min <= mts <= enter_max:
                positions[m.condition_id] = new_position(m, sim, now)
                log.info(
                    "ENTER %s mts=%.1fm set_cost=%.3f shares=%.1f",
                    m.slug,
                    mts,
                    sim["set_cost"],
                    sim["shares"],
                )

    # Only poll books for positions near decision horizon (or already expired)
    book_horizon_s = float(sim.get("book_horizon_min", 3.0)) * 60.0
    token_ids = set()
    active_markets = {m.condition_id: m for m in markets}
    for cid, pos in list(positions.items()):
        if pos.get("status") != "open":
            continue
        if cid in active_markets:
            pos["end_ts"] = active_markets[cid].end_ts
        ttm = float(pos["end_ts"]) - now
        if ttm <= book_horizon_s:
            token_ids.add(pos["up_token"])
            token_ids.add(pos["dn_token"])

    books = dict(entry_books)
    missing_token_ids = token_ids.difference(books)
    books.update(
        fetch_books_parallel(
            list(missing_token_ids),
            max_workers=int(sim.get("book_workers", 6)),
        )
    )

    closed_ids = []
    for cid, pos in list(positions.items()):
        if pos.get("status") != "open":
            continue

        seconds_left = float(pos["end_ts"]) - now
        # Far from window: skip book work this cycle
        if seconds_left > book_horizon_s:
            continue

        up_book = books.get(pos["up_token"])
        dn_book = books.get(pos["dn_token"])
        up_bid = up_book.best_bid if up_book and up_book.ok else None
        dn_bid = dn_book.best_bid if dn_book and dn_book.ok else None
        pos["last_up_bid"] = up_bid
        pos["last_dn_bid"] = dn_bid

        if should_record_tick(pos, seconds_left, strategy, sim, now):
            tick = {
                "ts": now,
                "seconds_left": round(seconds_left, 3),
                "up_bid": up_bid,
                "dn_bid": dn_bid,
                "up_bid_sz": up_book.best_bid_size if up_book else None,
                "dn_bid_sz": dn_book.best_bid_size if dn_book else None,
                "up_size": pos["up_size"],
                "dn_size": pos["dn_size"],
            }
            append_tick(cid, tick)
            pos["last_tick_at"] = now

        if seconds_left > 0:
            if now - float(pos["entered_at"]) < float(strategy["sell_grace_s"]):
                continue
            decision = evaluate(
                seconds_left=seconds_left,
                up_bid=up_bid,
                dn_bid=dn_bid,
                up_size=float(pos["up_size"]),
                dn_size=float(pos["dn_size"]),
                sold_up=bool(pos["sold_up"]),
                sold_dn=bool(pos["sold_dn"]),
                strategy=strategy,
            )
            if decision.reason == "threshold_unconfirmed":
                # Low-noise: only when a leg is actually soft (already decided)
                if int(pos.get("trigger_attempts") or 0) % 20 == 0:
                    log.info(
                        "WAIT  %s threshold_unconfirmed bids=U%s/D%s thr=%.2f conf=%.2f ttm=%.1fs",
                        pos["slug"],
                        up_bid,
                        dn_bid,
                        float(strategy["sell_threshold"]),
                        float(strategy.get("sell_confirm_opposite") or 0),
                        seconds_left,
                    )
                pos["trigger_attempts"] = int(pos.get("trigger_attempts") or 0) + 1
            apply_decision(
                pos, decision, up_book, dn_book, sim, strategy, now, seconds_left, log
            )
            continue

        post = float(sim["post_expiry_record_s"])
        if try_resolve(pos, up_bid, dn_bid, sim):
            finalize(pos, log)
            closed_ids.append(cid)
        elif -seconds_left > post:
            if pos["sold_up"] and not pos["sold_dn"] and float(pos["dn_size"]) >= 0.01:
                pos["winner"] = "dn"
            elif pos["sold_dn"] and not pos["sold_up"] and float(pos["up_size"]) >= 0.01:
                pos["winner"] = "up"
            elif float(pos["up_size"]) < 0.01 and float(pos["dn_size"]) < 0.01:
                pos["winner"] = None
            else:
                pos["winner"] = None
                log.warning("UNRESOLVED %s up_bid=%s dn_bid=%s", pos["slug"], up_bid, dn_bid)
            finalize(pos, log)
            closed_ids.append(cid)

    for cid in closed_ids:
        state.setdefault("completed", []).append(cid)
        positions.pop(cid, None)

    # Cap completed id list
    max_c = int(sim.get("max_completed_ids", 500))
    completed = state.get("completed") or []
    if len(completed) > max_c:
        state["completed"] = completed[-max_c:]

    state["last_cycle_at"] = now
    state["n_open"] = sum(1 for p in positions.values() if p.get("status") == "open")
    state["n_markets_seen"] = len(markets)
    save_state(state)
    write_heartbeat()


def print_status(state: dict, log: logging.Logger) -> None:
    positions = state.get("positions") or {}
    open_pos = [p for p in positions.values() if p.get("status") == "open"]
    now = time.time()
    log.info(
        "STATUS open=%d completed=%d",
        len(open_pos),
        len(state.get("completed") or []),
    )
    for p in sorted(open_pos, key=lambda x: x["end_ts"])[:12]:
        ttm = float(p["end_ts"]) - now
        log.info(
            "  %s ttm=%5.1fs up=%.2f dn=%.2f bidU=%s bidD=%s soldU=%s soldD=%s",
            p["slug"],
            ttm,
            p["up_size"],
            p["dn_size"],
            p.get("last_up_bid"),
            p.get("last_dn_bid"),
            p.get("sold_up"),
            p.get("sold_dn"),
        )


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Live shadow simulator (no real orders)")
    parser.add_argument("--once", action="store_true", help="Run a single cycle and exit")
    parser.add_argument("--summary", action="store_true", help="Print results summary and exit")
    parser.add_argument("--config", default=None, help="Path to strategy.sim.json")
    args = parser.parse_args(argv)

    strategy = load_strategy(args.config)
    sim = load_sim(args.config)
    ensure_dirs()

    log = _setup_log()
    if args.summary:
        s = summarize_results()
        print(json_dumps(s))
        return 0

    acquire_lock()

    signal.signal(signal.SIGINT, _on_signal)
    signal.signal(signal.SIGTERM, _on_signal)

    log.info(
        "SHADOW START series=%s tag=%s strategy=%s sim_fill=%s set_cost=%.3f shares=%.1f window=%.0fs thr=%.2f",
        ",".join(series_slug_list(sim)),
        sim.get("data_tag"),
        {
            k: strategy[k]
            for k in ("sell_threshold", "sell_window_min", "hedge_enabled", "sell_lastchance_s")
        },
        sim["fill_model"],
        sim["set_cost"],
        sim["shares"],
        strategy["sell_window_min"] * 60,
        strategy["sell_threshold"],
    )
    log.info("NO REAL ORDERS - public books only - data dir %s", sim.get("data_dir"))

    try:
        pruned0 = prune_old_files(sim)
        free_mb, total_mb = disk_usage_mb(sim.get("data_dir") or cfg.DATA_DIR)
        log.info(
            "DISK free=%.0fMB total=%.0fMB sim_data=%.1fMB record_ticks=%s prune=%s",
            free_mb,
            total_mb,
            pruned0.get("sim_data_mb", 0),
            sim.get("record_ticks"),
            {
                k: pruned0[k]
                for k in (
                    "removed_ticks",
                    "removed_trades",
                    "freed_for_cap",
                    "disk_full_flag",
                )
                if k in pruned0
            },
        )
        if free_mb < float(sim.get("min_free_disk_mb", 200)):
            log.error(
                "Low disk (%.0f MB free). Cleaned ticks; free space on the VM before trusting results.",
                free_mb,
            )
    except Exception:
        log.exception("startup prune failed")

    state = load_state()
    cycles = 0
    last_status = 0.0
    last_prune = 0.0
    last_disk_err_log = 0.0

    while not _shutdown:
        strategy = load_strategy(args.config)
        sim = load_sim(args.config)
        try:
            if time.time() - last_prune >= float(sim.get("prune_every_s", 120)):
                pruned = prune_old_files(sim)
                if (
                    pruned.get("removed_ticks")
                    or pruned.get("removed_trades")
                    or pruned.get("freed_for_cap")
                    or pruned.get("disk_full_flag")
                ):
                    log.info("PRUNE %s", pruned)
                last_prune = time.time()

            run_cycle(state, strategy, sim, log)
            cycles += 1
            if time.time() - last_status >= 15:
                print_status(state, log)
                if is_disk_full():
                    free_mb, _ = disk_usage_mb(sim.get("data_dir") or cfg.DATA_DIR)
                    log.warning("disk_full=1 free=%.0fMB (ticks off)", free_mb)
                last_status = time.time()
        except OSError as e:
            if getattr(e, "errno", None) == 28 or "No space left" in str(e):
                from .store import mark_disk_full

                mark_disk_full("cycle")
                try:
                    prune_old_files(sim)
                except Exception:
                    pass
                now = time.time()
                if now - last_disk_err_log > 60:
                    log.error("cycle blocked by disk full: %s", e)
                    last_disk_err_log = now
                time.sleep(5.0)
            else:
                log.exception("cycle error")
        except Exception:
            log.exception("cycle error")

        if args.once:
            break

        try:
            mkts = discover_btc_markets(
                series_slugs=series_slug_list(sim),
                horizon_min=float(sim["discover_horizon_min"]),
                lookback_min=1.0,
                cache_s=float(sim.get("discover_refresh_s", 25.0)),
            )
            sleep_s = choose_sleep(state.get("positions") or {}, mkts, sim, strategy)
        except Exception:
            sleep_s = float(sim["poll_far_s"])
        if is_disk_full():
            sleep_s = max(sleep_s, 5.0)

        end = time.time() + sleep_s
        while time.time() < end and not _shutdown:
            time.sleep(min(0.2, max(0.0, end - time.time())))

    save_state(state)
    log.info("SHADOW STOP cycles=%d summary=%s", cycles, summarize_results())
    return 0


def json_dumps(obj: Any) -> str:
    import json

    return json.dumps(obj, indent=2)


if __name__ == "__main__":
    raise SystemExit(main())

