"""Isolated BTC 5-minute policy; no mintbot import or import-time I/O."""
from __future__ import annotations

import inspect
import json
from copy import deepcopy
import math
import time
import threading
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional
from zoneinfo import ZoneInfo

from buy.fivemin_tape import RECORD_EVENTS
from buy.mint_sell import empty_fak_status

DEFAULTS = {
    "fivemin_book_ws": False, "fivemin_book_ws_stale_s": 1.0, "fivemin_poll_s": 0.2, "fivemin_active_window_s": 130,
    "fivemin_book_record": True, "fivemin_book_record_window_s": 150,
    "fivemin_enabled": False, "fivemin_series_slug": "btc-up-or-down-5m",
    "fivemin_binance_url": "wss://stream.binance.com:9443/ws/btcusdt@aggTrade",
    "fivemin_loser_empty_as_zero": True,
    "fivemin_nofill_retry_s": 2.0, "fivemin_max_attempts_per_market": 3,
    "fivemin_shares": 10, "fivemin_buy_limit": 0.99, "fivemin_gate_k": 1.0,
    "fivemin_gap_source": "twap", "fivemin_ttm_min_s": 0.0,
    "sniper_5m_enabled": False,
    "sniper_jump_usd": 25.0,
    "sniper_window_ms": 500,
    "sniper_min_gap_usd": 20.0,
    "sniper_max_ask": 0.97,
    "sniper_min_ask": 0.50,
    "sniper_ttm_min_s": 15,
    "sniper_ttm_max_s": 240,
    "sniper_shares": 10,
    "sniper_use_offset": False,
    "sniper_event_driven": True,
    "sniper_futures_ws_enabled": False,
    "sniper_futures_ws_url": "wss://fstream.binance.com/stream?streams=btcusdt@aggTrade/btcusdt@bookTicker",
    "fivemin_side98_enabled": False, "fivemin_side98_shares": 20, "fivemin_side98_price": 0.98,
    "fivemin_side98_min_gap_usd": 15.0, "fivemin_side98_max_ttc_s": 60,
    "fivemin_side98_max_adverse_30s_usd": 5.0,
    "fivemin_momentum_block_enabled": False, "fivemin_momentum_max_adverse_30s_usd": 15.0,
    "fivemin_momentum_gate_aware": False,
    "fivemin_loser_max": 0.10, "fivemin_loser_min": 0.01, "fivemin_fav_min": 0.90,
    "fivemin_max_ttm_s": 120, "fivemin_persist_s": 2.0, "fivemin_persist_last_s": 1.0,
    "fivemin_persist_last_window_s": 90, "fivemin_dump_held_below": 0.20,
    "fivemin_dump_other_min": 0.60, "fivemin_dump_persist_s": 3.0,
    "fivemin_dump_limit_offset": 0.20, "fivemin_dump_limit_floor": 0.05,
    "fivemin_min_notional_usd": 1.0, "fivemin_daily_stop_usd": -150.0,
    "fivemin_btc_max_age_s": 3.0, "fivemin_rtds_silent_s": 20.0,
    "fivemin_cash_buffer_usd": 5.0, "fivemin_book_stale_s": 30.0, "fivemin_dry_run": None,
    "fivemin_min_fill_px": 0.95, "fivemin_low_fill_exit": True,
}
EPS = 1e-9
STRIKE_GRACE_S = 5.0
# Empty-loser signal valid only if favoured side shows asks at <= cap, where the cap is the
# configured fivemin_buy_limit; #286 fired 5x live into empty asks, FAK rejected.
FIVEMIN_LOSER_EMPTY_MIN_ASK_SHARES = 1.0


def settings(cfg: dict) -> dict:
    return {**DEFAULTS, **cfg}


def dry_run(cfg: dict) -> bool:
    value = cfg.get("fivemin_dry_run")
    return bool(cfg.get("dry_run", True) if value is None else value)


def twap60(grid, g_end: int) -> Optional[float]:
    fast = getattr(grid, "twap60", None)
    if callable(fast):
        return fast(g_end)
    values = [grid.price_at(int(g_end) - 10 * j) for j in range(60)]
    return None if any(v is None for v in values) else sum(values) / 60


def market_offset(grid, start: float, strike: float) -> Optional[float]:
    value = twap60(grid, int(round(start * 10)))
    return None if value is None else strike - value


def proxy_price(grid, now: float, offset: Optional[float]) -> Optional[float]:
    if offset is None:
        return None
    value = twap60(grid, math.floor(now * 10) - 5)
    return None if value is None else value + offset


def gate_ratio(proxy: Optional[float], strike: float, favoured: str, ttm: float) -> Optional[float]:
    if proxy is None:
        return None
    return (1 if favoured == "up" else -1) * (proxy - strike) / max(ttm, 0.1)


def local_day(now: float) -> str:
    return datetime.fromtimestamp(now, ZoneInfo("Europe/Dublin")).date().isoformat()


def run_ready(since: Optional[float], ok: bool, now: float, need: float):
    if not ok:
        return None, False
    since = now if since is None else since
    return since, now - since + EPS >= max(0.0, need - 0.1)


def cash_free(balance: Optional[float], sampled_at: Optional[float], now: float,
              reserved: float, spends: list[dict]) -> Optional[float]:
    if balance is None or sampled_at is None or not 0 <= now - sampled_at <= 60:
        return None
    spent = sum(float(row["cost"]) for row in spends if float(row["ts"]) >= sampled_at)
    return balance - reserved - spent


def ask_levels(book: dict) -> list[list[float]]:
    levels = []
    for level in book.get("asks", []):
        if isinstance(level, dict):
            price, size = float(level["price"]), float(level["size"])
        else:
            price, size = map(float, level)
        if math.isfinite(price) and math.isfinite(size) and price > 0 and size > 0:
            levels.append([price, size])
    return sorted(levels, key=lambda level: level[0])


def ask_depth(book: dict, cap: float = 0.99) -> dict:
    levels = [level for level in ask_levels(book) if level[0] <= cap + EPS]
    cum = sum(size for price, size in levels)
    return {"asks_depth": levels,
            "cum_098": sum(size for price, size in levels if price <= 0.98),
            "cum_099": cum}


def decision_book_fields(book: dict, limit: float) -> dict:
    levels = ask_levels(book)
    return {"book_age_ms": book.get("age_ms"),
            "book_src": book.get("src", "rest"),
            "decision_ask_depth": round(sum(size for px, size in levels if px <= limit + EPS), 6),
            "decision_best_ask": levels[0][0] if levels else None}


def dry_fill(book: dict, shares: float, limit: float) -> dict:
    filled = notional = fees = 0.0
    for price, size in ask_levels(book):
        if price > limit or filled >= shares:
            break
        take = min(size, shares - filled)
        filled += take
        notional += take * price
        fees += take * 0.07 * price * (1 - price)
    return {"shares": filled, "cost": notional + fees, "fees": fees,
            "price": notional / filled if filled else 0.0,
            "status": "matched" if filled >= shares else "partial" if filled else "unfilled"}


def settlement_pnl(position: dict, winner: str) -> float:
    return float(position.get("proceeds", 0)) + (
        float(position.get("shares", 0)) if position["side"] == winner else 0.0
    ) - float(position.get("cost", 0))


def positions(row):
    return [p for p in (row.get("position"), (row.get("side98") or {}).get("position"),
                        (row.get("sniper_5m") or {}).get("position")) if p]


def pending_position(row):
    return any(not p.get("settled") or (
        p.get("shares", 0) > 0 and not p.get("redeem_enqueued") and not p.get("dry_run")
    ) for p in positions(row))


@dataclass
class FiveMinIO:
    buy: Callable
    sell: Callable
    reserves: Callable
    redeem: Callable
    save: Callable
    log: Callable = lambda *_a, **_k: None
    alert: Callable = lambda *_a, **_k: None
    gamma_winner: Callable = lambda *_a: None
    durable: Callable = lambda: True
    clock: Optional[Callable] = None
    label: Callable = lambda start: str(start)
    warmup: Optional[Callable] = None
    rest_book: Optional[Callable] = None
    live_book: Optional[Callable] = None


class Strategy:
    def __init__(self, io: FiveMinIO, state: Optional[dict] = None, *, startup_ts: Optional[float] = None):
        self.io = io
        self.startup_ts = time.time() if startup_ts is None else float(startup_ts)
        self._last_save = None
        self._save_dirty = False
        self._tick_now = None
        self._last_prune = 0.0
        self._last_btc_stale = 0.0
        self.state = state if state is not None else {}
        self.state.setdefault("markets", {})
        self.state.setdefault("day", "")
        self.state.setdefault("realized", 0.0)
        self.state.setdefault("paused_until_day", None)
        self.boundaries: dict[int, float] = {}
        self._near_boundaries: dict[int, tuple[float, float]] = {}
        self._active = set()
        for cid, row in list(self.state["markets"].items()):
            if row.get("skip_reason") == "startup" and row.get("end", 0) < self.startup_ts - 600 and not positions(row):
                del self.state["markets"][cid]
                self._save_dirty = True
                continue
            if float(row.get("start") or 0) < self.startup_ts and not positions(row):
                row["entered"] = True
                row.setdefault("skip_reason", "startup")
                self.io.log("fivemin_skip_startup_market", condition_id=cid,
                            slug=row.get("slug"), start=row.get("start"))
            if not positions(row) or pending_position(row):
                self._active.add(cid)
        self.runs: dict[str, dict] = {}
        self.balance = None
        self.sampled_at = None
        self.spends: list[dict] = []
        self._twap_cache = {}
        self._refetch_threads: list[threading.Thread] = []

    def _save_time(self):
        if self.io.clock is not None:
            return self.io.clock()
        return self._tick_now if self._tick_now is not None else time.time()

    def save(self, force=False):
        self._save_dirty = True
        now = self._save_time()
        if force or self._last_save is None or now - self._last_save >= 1.0:
            self.io.save(self.state)
            self._last_save = now
            self._save_dirty = False

    def alert(self, text: str, cfg: dict, kind: str):
        try:
            self.io.alert(("[DRY] " if dry_run(cfg) else "") + text, kind=kind)
        except Exception:
            pass

    def boundary_samples(self, samples):
        for sample in samples:
            try:
                obs = float(sample.obs_ts)
                stamp = round(obs)
                value = float(sample.twap)
                if not math.isfinite(value):
                    continue
                if abs(obs - stamp) <= 0.0005 and stamp % 300 == 0:
                    self.boundaries.setdefault(stamp, value)
                boundary = round(obs / 300) * 300
                diff = abs(obs - boundary)
                if diff <= 1.0 and (
                    boundary not in self._near_boundaries or diff < self._near_boundaries[boundary][0]
                ):
                    self._near_boundaries[boundary] = (diff, value)
            except (TypeError, ValueError, AttributeError):
                continue
        for cache in (self.boundaries, self._near_boundaries):
            for key in sorted(cache)[:-32]:
                del cache[key]

    def roll_day(self, now):
        day = local_day(now)
        if self.state["day"] != day:
            self.state.update(day=day, realized=0.0, paused_until_day=None)
            self.save()

    def sample_cash(self, now, balance):
        self.balance, self.sampled_at = float(balance), now
        self.spends = [row for row in self.spends if row["ts"] >= now]

    def add_market(self, market, now=None):
        now = time.time() if now is None else now
        try:
            start = int(str(market.slug).rsplit("-", 1)[1])
        except (IndexError, TypeError, ValueError):
            return
        if start + 300 <= now:
            return
        cid = str(market.condition_id)
        if cid not in self.state["markets"]:
            skipped = start < self.startup_ts
            self.state["markets"][cid] = {
                "condition_id": cid, "slug": market.slug, "start": start, "end": start + 300,
                "up_token": str(market.up_token), "dn_token": str(market.dn_token),
                "entered": skipped, "position": None,
            }
            self._active.add(cid)
            if skipped:
                self.state["markets"][cid]["skip_reason"] = "startup"
                self.io.log("fivemin_skip_startup_market", condition_id=cid, slug=market.slug, start=start)
            self.save()

    def _prune(self, now):
        if now - self._last_prune < 60:
            return
        self._last_prune = now
        removed = False
        for cid, row in list(self.state["markets"].items()):
            if row.get("end", 0) >= now - 600:
                continue
            if not pending_position(row):
                if not row.get("entered"):
                    self._eval(row, "skip", self.runs.get(cid, {}).get("last_reason", "never_in_window"), now - row.get("end", now), {})
                self.state["markets"].pop(cid, None)
                self._active.discard(cid)
                self.runs.pop(cid, None)
                removed = True
        if removed:
            self.save()

    def _proxy(self, grid, now, row, cfg):
        source = cfg.get("fivemin_gap_source", "twap")
        g = math.floor(now * 10) - 5
        if g not in self._twap_cache:
            self._twap_cache = {g: (twap60(grid, g), grid.price_at(g))}
        twap, spot = self._twap_cache[g]
        latest = getattr(grid, "latest_exchange_ms", lambda: None)()
        max_age = float(cfg.get("fivemin_btc_max_age_s", 3.0))
        if latest is None or now * 1000.0 - latest > max_age * 1000.0:
            if now - self._last_btc_stale >= 60:
                self._last_btc_stale = now
                self.io.log("fivemin_btc_stale", age_s=None if latest is None else now - latest / 1000.0)
            return None
        if source == "strike":
            return spot
        if row.get("offset") is None or (source == "twap" and twap is None):
            return None
        return (spot if source == "spot" else twap) + row["offset"]

    def _eval(self, row, decision, reason, ttm, bids, ratio=None, **extra):
        run = self.runs.setdefault(row["condition_id"], {})
        if run.get("eval_logged"):
            return
        run["eval_logged"] = True
        depth = run.get("depth", ask_depth({}))
        self.io.log("fivemin_eval", slug=row["slug"], decision=decision,
                    reason=reason, ttm=round(ttm, 3), bids=bids, ratio=ratio,
                    **depth, max_cum_098=run.get("max_cum_098", 0),
                    max_cum_099=run.get("max_cum_099", 0), **extra)

    def tick(self, cfg: dict, now: float, grid, books: dict):
        self._tick_now = now
        if self._save_dirty:
            self.save()
        cfg = settings(cfg)
        self._prune(now)
        self.roll_day(now)
        if self.state["realized"] <= cfg["fivemin_daily_stop_usd"] and self.state.get("paused_until_day") != self.state["day"]:
            self.state["paused_until_day"] = self.state["day"]
            self.save()
            self.io.log("fivemin_daily_stop", day=self.state["day"], pnl=self.state["realized"])
            self.alert(f"5m daily stop {self.state['day']}: {self.state['realized']:.2f}", cfg, "fivemin_daily_stop")
        for cid in list(self._active):
            row = self.state["markets"][cid]
            if row.get("start_twap") is None and now >= row["start"] + 1.0:
                start_twap = twap60(grid, int(row["start"] * 10))
                if start_twap is not None:
                    row["start_twap"] = start_twap
                    self.save()
            strike = row.get("strike")
            if strike is None and row["start"] in self.boundaries:
                row["strike"] = strike = self.boundaries[row["start"]]
                row["strike_source"] = "rtds_boundary"
                self.save()
            elif strike is None and now >= row["start"] + STRIKE_GRACE_S and row["start"] in self._near_boundaries:
                diff, strike = self._near_boundaries[row["start"]]
                row.update(strike=strike, strike_source="rtds_near")
                self.io.log("fivemin_strike_fallback", slug=row["slug"], diff_s=diff)
                self.save()
            if strike is not None and row.get("offset") is None:
                offset = strike - row["start_twap"] if row.get("start_twap") is not None else None
                if offset is not None:
                    row["offset"] = offset
                    self.save()
            position = row.get("position")
            held = positions(row)
            if now >= row["end"] and any(not p.get("settled") for p in held):
                winner, source = self._winner(now, row)
                if winner in ("up", "down"):
                    for p in held:
                        if not p.get("settled"):
                            self._settle(cfg, now, row, winner, source, p)
            if any(p.get("settled") for p in held):
                self._redeem_remainder(cfg, now, row)
            if held and not pending_position(row) and (
                now >= row["end"] or row.get("side98") or not cfg["fivemin_side98_enabled"]
            ) and (row.get("sniper_5m") or not cfg.get("sniper_5m_enabled", False)):
                self._active.discard(cid)
                continue
            if not held and now >= row["end"]:
                if not row.get("entered"):
                    run = self.runs.get(cid, {})
                    self._eval(row, "skip", run.get("last_reason", "never_in_window"),
                               run.get("last_ttm", row["end"] - now), run.get("last_bids", {}),
                               run.get("last_ratio"), close_ttm=round(row["end"] - now, 3),
                               in_window_ticks=run.get("in_window_ticks", 0), book_ticks=run.get("book_ticks", 0),
                               band_ticks=run.get("band_ticks", 0), persist_max_s=round(run.get("persist_max_s", 0), 3),
                               best_ratio=run.get("best_ratio"), blocked_btc_stale=run.get("blocked_btc_stale", 0),
                               blocked_no_strike=run.get("blocked_no_strike", 0), blocked_no_bid=run.get("blocked_no_bid", 0),
                               blocked_gate_below_k=run.get("blocked_gate_below_k", 0),
                               loser_empty_as_zero=run.get("loser_empty_as_zero", 0),
                               loser_empty_no_ask=run.get("loser_empty_no_ask", 0),
                               nofill_count=row.get("nofill_count", 0), main_attempts=row.get("main_attempts", 0),
                               miss=run.get("miss", {"up": {}, "down": {}}))
                if not pending_position(row):
                    self._active.discard(cid)
                continue
            if position and not position.get("settled") and now < row["end"]:
                self._manage(cfg, now, row, books.get(cid, {}))
            if (cfg.get("sniper_5m_enabled", False) and not row.get("sniper_5m")
                    and row["start"] <= now < row["end"]
                    and row.get("strike") is not None
                    and row.get("skip_reason") != "startup"):
                self._sniper_check(cfg, now, row, books.get(cid, {}), grid, row["end"] - now)
            if row["entered"] or not row["start"] <= now < row["end"]:
                if (row["entered"] and row["start"] <= now < row["end"]
                        and row.get("strike") is not None
                        and row.get("skip_reason") != "startup"
                        and cfg["fivemin_side98_enabled"] and not row.get("side98")):
                    self._side98_check(cfg, now, row, books.get(cid, {}), grid, row["end"] - now)
                continue
            if strike is None:
                run = self.runs.setdefault(cid, {})
                run["last_reason"] = "no_strike"
                ttm = row["end"] - now
                if ttm <= cfg["fivemin_max_ttm_s"]:
                    run["blocked_no_strike"] = run.get("blocked_no_strike", 0) + 1
                if now >= row["start"] + STRIKE_GRACE_S and not row.get("no_strike_logged"):
                    row["no_strike_logged"] = True
                    self.io.log("fivemin_no_strike", slug=row["slug"], condition_id=cid)
                    self.save()
                continue
            ttm = row["end"] - now
            quotes = books.get(cid, {})
            bids = {side: quotes.get(side, {}).get("bid") for side in ("up", "down")}
            run = self.runs.setdefault(cid, {})
            if ttm > cfg["fivemin_max_ttm_s"] or ttm <= cfg["fivemin_ttm_min_s"]:
                if cfg["fivemin_side98_enabled"] and not row.get("side98"):
                    self._side98_check(cfg, now, row, quotes, grid, ttm)
                continue
            fav = max((side for side in bids if bids[side] is not None),
                      key=lambda side: bids[side], default=None)
            run["depth"] = ask_depth(quotes.get(fav, {}))
            run.update(last_bids=bids, last_ttm=ttm, last_ratio=None)
            run["in_window_ticks"] = run.get("in_window_ticks", 0) + 1
            run["book_ticks"] = run.get("book_ticks", 0) + int(all(bid is not None for bid in bids.values()))
            run["blocked_no_bid"] = run.get("blocked_no_bid", 0) + int(any(bid is None for bid in bids.values()))
            missing = run.setdefault("miss", {"up": {}, "down": {}})
            for side in ("up", "down"):
                if bids[side] is None:
                    reason = quotes.get(side, {}).get("miss", "missing")
                    missing[side][reason] = missing[side].get(reason, 0) + 1
            empty_zero = {
                side for side, other in (("up", "down"), ("down", "up"))
                if cfg.get("fivemin_loser_empty_as_zero", True)
                and bids[side] is None
                and quotes.get(side, {}).get("miss") == "empty"
                and bids[other] is not None
                and bids[other] + EPS >= cfg["fivemin_fav_min"]
            }
            gate_bids = dict(bids)
            for side in empty_zero:
                gate_bids[side] = 0.0
            if empty_zero:
                run["loser_empty_as_zero"] = run.get("loser_empty_as_zero", 0) + 1
            prefilter = any(
                gate_bids[loser] is not None and gate_bids[fav] is not None and
                (loser in empty_zero or cfg["fivemin_loser_min"] - EPS <= gate_bids[loser] <= cfg["fivemin_loser_max"] + EPS) and
                gate_bids[fav] + EPS >= cfg["fivemin_fav_min"]
                for loser, fav in (("up", "down"), ("down", "up"))
            )
            if not prefilter:
                run["last_reason"] = "book_outside"
                if cfg["fivemin_side98_enabled"] and not row.get("side98"):
                    self._side98_check(cfg, now, row, quotes, grid, ttm)
                continue
            run["band_ticks"] = run.get("band_ticks", 0) + 1
            need = cfg["fivemin_persist_last_s"] if ttm <= cfg["fivemin_persist_last_window_s"] else cfg["fivemin_persist_s"]
            proxy = self._proxy(grid, now, row, cfg)
            signals = []
            blocked_btc_stale = blocked_gate_below_k = False
            for loser, fav in (("up", "down"), ("down", "up")):
                bid, other = gate_bids[loser], gate_bids[fav]
                ok = bid is not None and other is not None and (
                    (loser in empty_zero or cfg["fivemin_loser_min"] - EPS <= bid <= cfg["fivemin_loser_max"] + EPS)
                    and other + EPS >= cfg["fivemin_fav_min"]
                    and cfg["fivemin_ttm_min_s"] < ttm <= cfg["fivemin_max_ttm_s"] + EPS)
                run[loser], persisted = run_ready(run.get(loser), ok, now, need)
                ratio = gate_ratio(proxy, strike, fav, ttm)
                if ok:
                    run["depth"] = ask_depth(quotes.get(fav, {}), cap=float(cfg["fivemin_buy_limit"]))
                    for key in ("cum_098", "cum_099"):
                        run["max_" + key] = max(run.get("max_" + key, 0), run["depth"][key])
                    run["persist_max_s"] = max(run.get("persist_max_s", 0), now - run[loser])
                    run["last_ratio"] = ratio
                    if ratio is not None:
                        run["best_ratio"] = ratio if run.get("best_ratio") is None else max(run["best_ratio"], ratio)
                if ok and persisted:
                    blocked_btc_stale |= ratio is None
                    blocked_gate_below_k |= ratio is not None and ratio < cfg["fivemin_gate_k"]
                if persisted and ratio is not None and ratio + EPS >= cfg["fivemin_gate_k"]:
                    signals.append((run[loser], loser, fav, ratio))
                elif not persisted:
                    run["last_reason"] = "not_persisted"
                elif ratio is None:
                    run["last_reason"] = "btc_stale"
                else:
                    run["last_reason"] = "gate_below_k"
            run["blocked_btc_stale"] = run.get("blocked_btc_stale", 0) + int(blocked_btc_stale)
            run["blocked_gate_below_k"] = run.get("blocked_gate_below_k", 0) + int(blocked_gate_below_k and not signals)
            if signals:
                since, loser, fav, ratio = min(signals)
                momentum = self._btc_mom30(cfg, now, grid, fav)
                mom30 = None if momentum is None else momentum[1]
                adverse_exceeded = (cfg["fivemin_momentum_block_enabled"] and mom30 is not None
                                    and mom30 < -cfg["fivemin_momentum_max_adverse_30s_usd"] - EPS)
                blocked, headroom = adverse_exceeded, None
                if adverse_exceeded and cfg.get("fivemin_momentum_gate_aware", False):
                    # gate_ratio * ttm is the favoured-side proxy-minus-strike gap in USD.
                    gate_t = max(ttm, 0.1)
                    gate_gap = ratio * gate_t
                    headroom = gate_gap - (-mom30) - cfg["fivemin_gate_k"] * gate_t
                    blocked = headroom < 0
                if blocked:
                    run["last_reason"] = "momentum_block"
                    run["blocked_momentum"] = run.get("blocked_momentum", 0) + 1
                    if not row.get("momentum_block_logged"):
                        row["momentum_block_logged"] = True
                        spot, _ = momentum
                        offset = row.get("offset")
                        sign = 1 if fav == "up" else -1
                        self.io.log("fivemin_momentum_block", slug=row["slug"], condition_id=row["condition_id"],
                                    side=fav, adverse_30s_usd=-mom30,
                                    gap_usd=None if offset is None else sign * (spot + offset - strike),
                                    gate_ratio=ratio, ttc_s=ttm)
                    signals = []
                elif headroom is not None:
                    if not row.get("momentum_pass_headroom_logged"):
                        row["momentum_pass_headroom_logged"] = True
                        spot, _ = momentum
                        offset = row.get("offset")
                        sign = 1 if fav == "up" else -1
                        self.io.log("fivemin_momentum_pass_headroom", slug=row["slug"], condition_id=row["condition_id"],
                                    side=fav, adverse_30s_usd=-mom30,
                                    gap_usd=None if offset is None else sign * (spot + offset - strike),
                                    gate_ratio=ratio, ttc_s=ttm, headroom=headroom)
                else:
                    # Missing momentum data allows entry to preserve prior behavior.
                    if cfg["fivemin_momentum_block_enabled"] and mom30 is None and not row.get("momentum_data_missing_logged"):
                        row["momentum_data_missing_logged"] = True
                        self.io.log("fivemin_momentum_data_missing", slug=row["slug"], condition_id=row["condition_id"], side=fav)
            if signals:
                if loser in empty_zero:
                    depth = ask_depth(quotes.get(fav, {}), cap=float(cfg["fivemin_buy_limit"]))
                    if depth["cum_099"] < FIVEMIN_LOSER_EMPTY_MIN_ASK_SHARES:
                        run["loser_empty_no_ask"] = run.get("loser_empty_no_ask", 0) + 1
                        run["last_reason"] = "loser_empty_no_ask"
                        if cfg["fivemin_side98_enabled"] and not row.get("side98"):
                            self._side98_check(cfg, now, row, quotes, grid, ttm)
                        continue
                if row.get("main_attempts", 0) >= cfg["fivemin_max_attempts_per_market"]:
                    run["last_reason"] = "max_attempts"
                    if cfg["fivemin_side98_enabled"] and not row.get("side98"):
                        self._side98_check(cfg, now, row, quotes, grid, ttm)
                    continue
                if row.get("nofill_count", 0) and now - row.get("last_main_attempt_ts", float("-inf")) < cfg["fivemin_nofill_retry_s"]:
                    run["last_reason"] = "nofill_cooldown"
                    if cfg["fivemin_side98_enabled"] and not row.get("side98"):
                        self._side98_check(cfg, now, row, quotes, grid, ttm)
                    continue
                if loser in empty_zero:
                    self._eval(row, "enter", "signal_loser_empty_as_zero", ttm, bids, ratio,
                               loser_empty_as_zero=True)
                else:
                    self._eval(row, "enter", "signal", ttm, bids, ratio)
                self._enter(cfg, now, row, quotes, loser, fav, ratio, proxy, now - since, mom30)
            elif cfg["fivemin_side98_enabled"] and not row.get("side98"):
                self._side98_check(cfg, now, row, quotes, grid, ttm)

    def _btc_mom30(self, cfg, now, grid, fav):
        latest = getattr(grid, "latest_exchange_ms", lambda: None)()
        if latest is None or now * 1000 - latest > cfg["fivemin_btc_max_age_s"] * 1000:
            return None
        g = math.floor(now * 10) - 5
        spot, past = grid.price_at(g), grid.price_at(g - 300)
        if spot is None or past is None:
            return None
        sign = 1 if fav == "up" else -1
        return spot, sign * (spot - past)

    def _min_fill_blocked(self, cfg, row, quotes, side, kind) -> bool:
        floor = float(cfg.get("sniper_min_ask" if kind == "sniper_5m" else "fivemin_min_fill_px", 0))
        if floor <= 0:
            return False
        levels = ask_levels(quotes.get(side, {}))
        if not levels:
            return False
        best = levels[0][0]
        if best < floor - EPS:
            logged = self.runs.setdefault(row["condition_id"], {}).setdefault("min_fill_logged", {})
            if kind not in logged:
                logged[kind] = True
                self.io.log("fivemin_min_fill_skip", slug=row["slug"], condition_id=row["condition_id"],
                            side=side, kind=kind, best_ask=best, floor=floor,
                            depth=round(sum(size for _, size in levels), 6))
            return True
        return False

    def _side98_check(self, cfg, now, row, quotes, grid, ttm):
        if not 0 < ttm <= cfg["fivemin_side98_max_ttc_s"] + EPS:
            return
        if row.get("strike") is None or row.get("offset") is None:
            return
        fav = max((side for side in ("up", "down") if quotes.get(side, {}).get("bid") is not None),
                  key=lambda side: quotes[side]["bid"], default=None)
        levels = ask_levels(quotes.get(fav, {}))
        if not levels or abs(levels[0][0] - cfg["fivemin_side98_price"]) > EPS:
            return
        momentum = self._btc_mom30(cfg, now, grid, fav)
        if momentum is None:
            return
        spot, mom30 = momentum
        sign = 1 if fav == "up" else -1
        gap = sign * (spot + row["offset"] - row["strike"])
        if gap < cfg["fivemin_side98_min_gap_usd"] - EPS or mom30 < -cfg["fivemin_side98_max_adverse_30s_usd"] - EPS:
            return
        if self._min_fill_blocked(cfg, row, quotes, fav, "side98"):
            return
        self._side98_enter(cfg, now, row, quotes, fav, gap, mom30)

    def _side98_enter(self, cfg, now, row, quotes, fav, gap, mom30):
        decision_time = self.io.clock() if self.io.clock is not None else time.perf_counter()
        sent_now = decision_time if self.io.clock is not None else now
        row["side98"] = {"attempted": True, "side": fav, "attempted_at": now,
                         "gap": gap, "mom30": mom30}
        self.save(force=True)
        reason = None
        if sent_now >= row["end"]:
            reason = "expired"
        elif not cfg["fivemin_enabled"]:
            reason = "disabled"
        elif self.state.get("paused_until_day") == self.state["day"]:
            reason = "daily_stop"
        shares, limit = float(cfg["fivemin_side98_shares"]), float(cfg["fivemin_side98_price"])
        if reason is None:
            free = cash_free(self.balance, self.sampled_at, sent_now,
                             self.io.reserves(cfg) + cfg["fivemin_cash_buffer_usd"], self.spends)
            if free is None or free + EPS < shares * limit:
                reason = "cash"
        if reason:
            self.io.log("fivemin_side98_blocked", slug=row["slug"], reason=reason)
            return
        token = row["up_token"] if fav == "up" else row["dn_token"]
        is_dry = dry_run(cfg)
        try:
            result = self.io.buy(token, shares, limit, is_dry)
        except Exception as exc:
            self.io.log("fivemin_side98_order_error", slug=row["slug"], side=fav,
                        limit=limit, error=str(exc))
            self.alert(f"5m 98 SIDE ORDER {self.io.label(row['start'])} {fav.upper()} order error @ {limit:.4f}: {exc}",
                       cfg, "fivemin_side98_order_error")
            return
        response_time = self.io.clock() if self.io.clock is not None else time.perf_counter()
        latency_ms = (response_time - decision_time) * 1000
        if is_dry:
            result = dry_fill(quotes.get(fav, {}), shares, limit)
        filled = float(result.get("shares", 0))
        cost = float(result.get("cost", filled * limit))
        avg = result["price"] if is_dry else cost / filled if filled else 0.0
        row["side98"]["position"] = {
            "token": token, "side": fav, "shares": filled, "cost": cost, "avg_px": avg,
            "dumped": False, "proceeds": 0.0, "settled": False, "pnl": None,
            "status": result.get("status", "posted"), "dry_run": is_dry,
            "filled_at": now, "kind": "side98",
        }
        if cost > 0 and not is_dry:
            self.spends.append({"ts": now, "cost": cost})
        self.save(force=True)
        kind = "fivemin_side98_nofill" if filled == 0 and not is_dry else "fivemin_side98_fill"
        self.io.log(kind, slug=row["slug"], condition_id=row["condition_id"],
                    price=avg, size=filled, usdc=cost, status=result.get("status"),
                    fees=result.get("fees"), dry_run=is_dry, latency_ms=latency_ms,
                    timing=result.get("timing"), gap=gap, mom30=mom30, ttm=row["end"] - now,
                    **decision_book_fields(quotes.get(fav, {}), limit))
        if kind == "fivemin_side98_nofill":
            self._schedule_post_reject(row, fav, token, limit)
        tag = f"no fill @ {limit:.4f}" if kind == "fivemin_side98_nofill" else f"fill {filled:g} @ {avg:.4f}"
        self.alert(f"5m 98 SIDE ORDER {self.io.label(row['start'])} {fav.upper()} {tag}; ttm={row['end']-now:.1f}s gap=${gap:.2f} mom30=${mom30:.2f}",
                   cfg, kind)
        if filled > 0:
            self._low_fill_exit(cfg, now, row, row["side98"]["position"], quotes, "side98")

    def _sniper_check(self, cfg, now, row, quotes, grid, ttm):
        if not cfg.get("sniper_5m_enabled", False):
            return False
        if row.get("sniper_5m"):
            return False
        if row.get("strike") is None:
            return False
        if row.get("skip_reason") == "startup":
            return False
        if not (float(cfg["sniper_ttm_min_s"]) - EPS <= ttm <= float(cfg["sniper_ttm_max_s"]) + EPS):
            return False
        jump_fn = getattr(grid, "jump_in_window", None)
        info = jump_fn(float(cfg["sniper_window_ms"]), now_ms=now * 1000.0) if callable(jump_fn) else None
        if info is None:
            return False
        if now * 1000.0 - float(info["ts_ms"]) > float(cfg["fivemin_btc_max_age_s"]) * 1000.0 + EPS:
            return False
        spot_now = float(info["spot"])
        offset = float(row["offset"]) if (cfg.get("sniper_use_offset", False) and row.get("offset") is not None) else 0.0
        spot_ref = spot_now + offset
        strike = float(row["strike"])
        for side, jump_key, sign in (("up", "up_jump", 1.0), ("down", "down_jump", -1.0)):
            jump_usd = float(info[jump_key])
            gap_usd = sign * (spot_ref - strike)
            if jump_usd + EPS < float(cfg["sniper_jump_usd"]) or gap_usd + EPS < float(cfg["sniper_min_gap_usd"]):
                continue
            levels = ask_levels(quotes.get(side, {}))
            best_ask = levels[0][0] if levels else quotes.get(side, {}).get("ask")
            if best_ask is None:
                continue
            best_ask = float(best_ask)
            if not (float(cfg["sniper_min_ask"]) - EPS <= best_ask <= float(cfg["sniper_max_ask"]) + EPS):
                continue
            if self._min_fill_blocked(cfg, row, quotes, side, "sniper_5m"):
                continue
            self._sniper_enter(cfg, now, row, quotes, side, jump_usd, gap_usd, spot_now)
            return True
        return False

    def _sniper_enter(self, cfg, now, row, quotes, fav, jump_usd, gap_usd, spot_now):
        decision_time = self.io.clock() if self.io.clock is not None else time.perf_counter()
        sent_now = decision_time if self.io.clock is not None else now
        row["sniper_5m"] = {"attempted": True, "side": fav, "attempted_at": now,
                            "jump_usd": jump_usd, "gap_usd": gap_usd, "spot": spot_now}
        self.save(force=True)
        reason = None
        if sent_now >= row["end"]:
            reason = "expired"
        elif not cfg["fivemin_enabled"] and not cfg.get("sniper_5m_enabled"):
            reason = "disabled"
        elif self.state.get("paused_until_day") == self.state["day"]:
            reason = "daily_stop"
        shares, limit = float(cfg["sniper_shares"]), float(cfg["sniper_max_ask"])
        if reason is None:
            free = cash_free(self.balance, self.sampled_at, sent_now,
                             self.io.reserves(cfg) + cfg["fivemin_cash_buffer_usd"], self.spends)
            if free is None or free + EPS < shares * limit:
                reason = "cash"
        if reason:
            self.io.log("fivemin_sniper_blocked", slug=row["slug"], reason=reason)
            return
        token = row["up_token"] if fav == "up" else row["dn_token"]
        is_dry = dry_run(cfg)
        try:
            result = self.io.buy(token, shares, limit, is_dry)
        except Exception as exc:
            self.io.log("fivemin_sniper_order_error", slug=row["slug"], side=fav,
                        limit=limit, error=str(exc))
            self.alert(f"5m SNIPER ORDER {self.io.label(row['start'])} {fav.upper()} order error @ {limit:.4f}: {exc}",
                       cfg, "fivemin_sniper_order_error")
            return
        response_time = self.io.clock() if self.io.clock is not None else time.perf_counter()
        latency_ms = (response_time - decision_time) * 1000
        if is_dry:
            result = dry_fill(quotes.get(fav, {}), shares, limit)
        filled = float(result.get("shares", 0))
        cost = float(result.get("cost", filled * limit))
        avg = result["price"] if is_dry else cost / filled if filled else 0.0
        row["sniper_5m"]["position"] = {
            "token": token, "side": fav, "shares": filled, "cost": cost, "avg_px": avg,
            "dumped": False, "proceeds": 0.0, "settled": False, "pnl": None,
            "status": result.get("status", "posted"), "dry_run": is_dry,
            "filled_at": now, "kind": "sniper_5m",
        }
        if cost > 0 and not is_dry:
            self.spends.append({"ts": now, "cost": cost})
        self.save(force=True)
        kind = "fivemin_sniper_nofill" if filled == 0 and not is_dry else "fivemin_sniper_fill"
        self.io.log(kind, slug=row["slug"], condition_id=row["condition_id"], side=fav,
                    price=avg, size=filled, usdc=cost, status=result.get("status"),
                    latency_ms=latency_ms, timing=result.get("timing"),
                    jump_usd=jump_usd, gap_usd=gap_usd, spot=spot_now, ttm=row["end"] - now,
                    **decision_book_fields(quotes.get(fav, {}), limit))
        if filled > 0:
            self._low_fill_exit(cfg, now, row, row["sniper_5m"]["position"], quotes, "sniper_5m")

    def _low_fill_exit(self, cfg, now, row, p, quotes, kind):
        if not cfg.get("fivemin_low_fill_exit", True):
            return
        floor = float(cfg.get("sniper_min_ask" if kind == "sniper_5m" else "fivemin_min_fill_px", 0))
        if floor <= 0 or p["shares"] <= 0 or p["dumped"] or p["avg_px"] >= floor - EPS:
            return
        decision = quotes.get(p["side"], {})
        book = decision
        live = getattr(self.io, "live_book", None)
        if live is not None:
            try:
                fresh = live(p["token"])
            except Exception:
                fresh = None
            if fresh and fresh.get("bid") is not None:
                book = fresh
        if book.get("bid") is None:
            self.io.log("fivemin_low_fill_exit_skip", slug=row["slug"], condition_id=row["condition_id"],
                        kind=kind, side=p["side"], reason="no_bid")
            return
        p["dumped"] = True
        p["low_fill_exit"] = True
        self.save(force=True)
        shares = p["shares"]
        try:
            result = self.io.sell(row, p, book, cfg, now)
        except Exception as exc:
            p["dumped"] = False
            p["low_fill_exit_failed"] = True
            self.save(force=True)
            self.io.log("fivemin_low_fill_exit_error", slug=row["slug"], condition_id=row["condition_id"],
                        kind=kind, side=p["side"], error=str(exc))
            self.alert(f"5m {self.io.label(row['start'])} LOW FILL EXIT {kind} error: {exc}",
                       cfg, "fivemin_low_fill_exit_error")
            return
        sold = min(shares, max(0.0, float(result.get("shares", 0))))
        proceeds = float(result.get("proceeds", sold * float(result.get("price", book["bid"]))))
        p["shares"] -= sold
        p["proceeds"] += proceeds
        remaining = p["shares"]
        if remaining > 0.01:
            p["dumped"] = False
        self.save(force=True)
        pnl = p["proceeds"] - p["cost"]
        self.io.log("fivemin_low_fill_exit", slug=row["slug"], condition_id=row["condition_id"],
                    kind=kind, side=p["side"], avg_fill=p["avg_px"], floor=floor,
                    shares=sold, proceeds=proceeds, remaining=remaining, pnl=pnl,
                    book_src="live" if book is not decision else "decision",
                    status=result.get("status"))
        self.alert(f"5m {self.io.label(row['start'])} LOW FILL EXIT {kind} avg {p['avg_px']:.4f} < {floor:.2f}; "
                   f"sold {sold:g} for {proceeds:.2f}; P&L={pnl:.2f}",
                   {**cfg, "fivemin_dry_run": p.get("dry_run", dry_run(cfg))}, "fivemin_low_fill_exit")

    def _schedule_post_reject(self, row, fav, token, limit):
        fetch = getattr(self.io, "rest_book", None)
        if fetch is None:
            return

        def worker():
            try:
                started = time.perf_counter()
                book = fetch(token)
                refetch_ms = (time.perf_counter() - started) * 1000.0
                all_asks = ask_levels(book or {})
                below = [size for px, size in all_asks if px <= limit + EPS]
                self.io.log("fivemin_post_reject_book", slug=row["slug"],
                            condition_id=row["condition_id"], side=fav, token=token,
                            limit=limit,
                            post_reject_best_ask=all_asks[0][0] if all_asks else None,
                            post_reject_depth=round(sum(below), 6),
                            refetch_ms=round(refetch_ms, 1))
            except Exception:
                return

        thread = threading.Thread(target=worker, daemon=True, name="fivemin-post-reject-book")
        self._refetch_threads.append(thread)
        del self._refetch_threads[:-8]
        try:
            thread.start()
        except Exception:
            self._refetch_threads.remove(thread)

    def _enter(self, cfg, now, row, quotes, loser, fav, ratio, proxy, persisted, mom30=None):
        if self._min_fill_blocked(cfg, row, quotes, fav, "main"):
            return
        decision_time = self.io.clock() if self.io.clock is not None else time.perf_counter()
        row["entered"] = True
        row["main_attempts"] = row.get("main_attempts", 0) + 1
        row["last_main_attempt_ts"] = now
        sent_now = decision_time if self.io.clock is not None else now

        def record_signal():
            self.save(force=True)
            log_books = deepcopy({side: quotes.get(side) or {} for side in ("up", "down")})
            for book in log_books.values():
                book["bids"] = book.get("bids", [])[:3]
                book["asks"] = ask_levels(book)[:3]
            self.io.log("fivemin_signal", ts=now, slug=row["slug"], condition_id=row["condition_id"],
                        attempt=row["main_attempts"],
                        ttm=row["end"] - now, gate_ratio=ratio, proxy=proxy, strike=row["strike"],
                        offset=row.get("offset"), books=log_books, loser=loser,
                        persisted_s=persisted, **ask_depth(quotes.get(fav, {})))

        reason = None
        if sent_now >= row["end"]:
            reason = "expired"
        elif not cfg["fivemin_enabled"]:
            reason = "disabled"
        elif self.state.get("paused_until_day") == self.state["day"]:
            reason = "daily_stop"
        if reason:
            if reason == "daily_stop":
                self.runs.setdefault(row["condition_id"], {})["last_reason"] = "daily_stop"
            record_signal()
            self.io.log("fivemin_signal_blocked", slug=row["slug"], reason=reason)
            return
        shares, limit = float(cfg["fivemin_shares"]), float(cfg["fivemin_buy_limit"])
        reserved = self.io.reserves(cfg)
        free = cash_free(self.balance, self.sampled_at, sent_now, reserved + cfg["fivemin_cash_buffer_usd"], self.spends)
        if free is None or free + EPS < shares * limit:
            record_signal()
            self.io.log("fivemin_cash_skip", slug=row["slug"], free=free, required=shares * limit)
            return
        token = row["up_token"] if fav == "up" else row["dn_token"]
        is_dry = dry_run(cfg)
        error = None
        try:
            result = self.io.buy(token, shares, limit, is_dry)
        except Exception as exc:
            error = str(exc)
        response_time = self.io.clock() if self.io.clock is not None else time.perf_counter()
        latency_ms = (response_time - decision_time) * 1000
        if error is not None:
            record_signal()
            self.io.log("fivemin_order_error", slug=row["slug"], side=fav, limit=limit,
                        error=error, latency_ms=latency_ms)
            self.alert(f"5m {row['slug']} {fav.upper()} order error @ {limit:.4f}: {error}",
                       cfg, "fivemin_order_error")
            return
        if is_dry:
            result = dry_fill(quotes.get(fav, {}), shares, limit)
        filled = float(result.get("shares", 0))
        cost = float(result.get("cost", filled * limit))
        avg = result["price"] if is_dry else cost / filled if filled else 0.0
        status = result.get("status")
        retryable = (filled == 0 and not is_dry and isinstance(status, str)
                     and status.lower().startswith("reject:") and empty_fak_status(status))
        if filled == 0 and not is_dry:
            row["nofill_count"] = row.get("nofill_count", 0) + 1
        if retryable:
            row["entered"] = False
            self.runs.setdefault(row["condition_id"], {})["eval_logged"] = False
        else:
            row["position"] = {
                "token": token, "side": fav, "shares": filled, "cost": cost, "avg_px": avg,
                "dumped": False, "proceeds": 0.0, "settled": False, "pnl": None,
                "status": result.get("status", "posted"), "dry_run": is_dry,
                "filled_at": now,
            }
        if cost > 0 and not is_dry:
            self.spends.append({"ts": now, "cost": cost})
        record_signal()
        self.io.log("fivemin_fill", slug=row["slug"], condition_id=row["condition_id"],
                    attempt=row["main_attempts"],
                    price=avg, size=filled, usdc=cost, status=result.get("status"),
                    fees=result.get("fees"), dry_run=is_dry, latency_ms=latency_ms,
                    timing=result.get("timing"), btc_mom30_usd=mom30,
                    **decision_book_fields(quotes.get(fav, {}), limit))
        if filled == 0 and not is_dry:
            self.io.log("fivemin_signal_nofill", slug=row["slug"], side=fav, limit=limit,
                        attempt=row["main_attempts"], retryable=retryable,
                        latency_ms=latency_ms, timing=result.get("timing"), btc_mom30_usd=mom30,
                        **decision_book_fields(quotes.get(fav, {}), limit))
            self._schedule_post_reject(row, fav, token, limit)
            if row["nofill_count"] == 1:
                self.alert(f"5m {row['slug']} {fav.upper()} no fill @ {limit:.4f}", cfg, "fivemin_signal_nofill")
            self.save(force=True)
            return
        else:
            tag = f"realistic fill {filled:g} @ {avg:.4f} (of {shares:g}, book walk, fee {float(result.get('fees') or 0):.2f})" if is_dry else f"fill {filled:g} @ {avg:.4f}; {latency_ms:.0f}ms"
            self.alert(f"5m {self.io.label(row['start'])} {fav.upper()} {tag}; ttm={row['end']-now:.1f}s gate={ratio:.3f}", cfg, "fivemin_fill")
        if filled > 0:
            self._low_fill_exit(cfg, now, row, row["position"], quotes, "main")

    def _winner(self, now, row):
        winner = None
        source = "rtds_boundary"
        if row.get("strike") is not None and row["end"] in self.boundaries:
            winner = "up" if self.boundaries[row["end"]] >= row["strike"] else "down"
        elif row["end"] + 120 <= now <= row["end"] + 3600 and now >= row.get("gamma_next", 0):
            row["gamma_next"] = now + 30
            try:
                winner = self.io.gamma_winner(row["slug"])
            except Exception:
                winner = None
            source = "gamma_outcomePrices"
        return winner, source

    def _manage(self, cfg, now, row, quotes):
        p = row["position"]
        if now >= row["end"]:
            winner, source = self._winner(now, row)
            if winner in ("up", "down"):
                self._settle(cfg, now, row, winner, source)
            return
        if p["dumped"] or p["shares"] <= 0:
            return
        held, other = p["side"], "down" if p["side"] == "up" else "up"
        bid = quotes.get(held, {}).get("bid")
        opposite = quotes.get(other, {}).get("bid")
        ok = bid is not None and opposite is not None and bid < cfg["fivemin_dump_held_below"] - EPS and opposite + EPS >= cfg["fivemin_dump_other_min"]
        run = self.runs.setdefault(row["condition_id"], {})
        run["dump"], ready = run_ready(run.get("dump"), ok, now, cfg["fivemin_dump_persist_s"])
        if not ready:
            return
        if p["shares"] * bid + EPS < cfg["fivemin_min_notional_usd"]:
            if not p.get("notional_logged"):
                p["notional_logged"] = True
                self.io.log("fivemin_dump_skip_notional", slug=row["slug"], shares=p["shares"], bid=bid)
                self.save(force=True)
            return
        p["dumped"] = True
        self.save(force=True)
        if self.io.clock is not None and self.io.clock() >= row["end"]:
            return
        result = self.io.sell(row, p, quotes[held], cfg, now)
        sold = min(p["shares"], max(0.0, float(result.get("shares", 0))))
        proceeds = float(result.get("proceeds", sold * float(result.get("price", bid))))
        p["shares"] -= sold
        p["proceeds"] += proceeds
        self.save(force=True)
        self.io.log("fivemin_dump", slug=row["slug"], condition_id=row["condition_id"],
                    shares=sold, price=proceeds / sold if sold else None,
                    proceeds=proceeds, pnl=p["proceeds"] - p["cost"], status=result.get("status"))
        self.alert(f"5m {self.io.label(row['start'])} dump {sold:g} @ {proceeds/sold if sold else 0:.4f}; P&L={p['proceeds']-p['cost']:.2f}",
                   {**cfg, "fivemin_dry_run": p.get("dry_run", dry_run(cfg))}, "fivemin_dump")

    def _settle(self, cfg, now, row, winner, source, position=None):
        p = row["position"] if position is None else position
        if p.get("settled"):
            return
        pnl = settlement_pnl(p, winner)
        p.update(settled=True, pnl=pnl)
        self.state["realized"] += pnl
        stopped = self.state["realized"] <= cfg["fivemin_daily_stop_usd"] and self.state.get("paused_until_day") != self.state["day"]
        if stopped:
            self.state["paused_until_day"] = self.state["day"]
        self.save(force=True)
        if not self.io.durable():
            raise OSError("fivemin settlement not durable")
        kind = p.get("kind")
        event = ("fivemin_side98_settle" if kind == "side98"
                 else "fivemin_sniper_settle" if kind == "sniper_5m" else "fivemin_settle")
        self.io.log(event, slug=row["slug"], condition_id=row["condition_id"], winner=winner, source=source, pnl=pnl)
        self._redeem_remainder(cfg, now, row)
        if stopped:
            self.io.log("fivemin_daily_stop", day=self.state["day"], pnl=self.state["realized"])
            self.alert(f"5m daily stop {self.state['day']}: {self.state['realized']:.2f}", cfg, "fivemin_daily_stop")

    def _redeem_remainder(self, cfg, now, row):
        eligible = [p for p in positions(row) if p.get("settled") and p.get("shares", 0) > 0
                    and not p.get("dry_run") and not p.get("redeem_enqueued")]
        if not eligible:
            return
        if cfg.get("redeem_enabled"):
            if not any(p.get("redeem_enqueued") for p in positions(row)):
                self.io.redeem(row, now)
            for p in eligible:
                p["redeem_enqueued"] = True
            self.save(force=True)
        else:
            for p in eligible:
                if not p.get("redeem_disabled_logged"):
                    p["redeem_disabled_logged"] = True
                    self.io.log("fivemin_redeem_disabled", slug=row["slug"])
                    self.save(force=True)


class Runner:
    """Own discovery, feeds and cash polling on the isolated 5m loop."""
    def __init__(self, strategy: Strategy, cfg: Callable, gateway, chain, funder: str,
                 grid, binance, rtds, book_feed, *, clock=time.time, tape=None):
        self.strategy, self.cfg = strategy, cfg
        self.tape = tape
        self.gateway, self.chain, self.funder = gateway, chain, funder
        self.grid, self.binance, self.rtds, self.books = grid, binance, rtds, book_feed
        self.clock = clock
        self.next_discover = 0.0
        self.next_cash = 0.0
        self.current = None
        self._last_markets = None
        self._last_poll = {}
        self._warmup_tokens = frozenset()
        self._io_stop = threading.Event()
        self._io_thread = None
        self._io_lock = threading.Lock()
        self._tick_lock = threading.RLock()
        self._markets = []
        self._balance = None
        self._gamma_pending = set()
        self._gamma_results = {}
        self.gamma_fetch = None
        self.rtds_last_sample = self.clock()
        self.rtds_last_reconnect = 0.0
        try:
            if hasattr(self.binance, "_on_tick") and getattr(self.binance, "_on_tick", None) is None:
                self.binance._on_tick = self.on_binance_tick
        except Exception:
            pass
        try:
            self.strategy.io.rest_book = self._rest_refetch_book
            self.strategy.io.live_book = self._live_book
            if tape is not None:
                orig = self.strategy.io.log

                def tee(event, **fields):
                    orig(event, **fields)
                    if event in RECORD_EVENTS:
                        try:
                            tape.event(event, fields)
                        except Exception:
                            pass

                self.strategy.io.log = tee
        except Exception:
            pass

    def on_binance_tick(self, exchange_ms: float, price: float):
        cfg = settings(self.cfg())
        if not cfg.get("sniper_5m_enabled", False) or not cfg.get("sniper_event_driven", True):
            return
        row = self.current
        if row is None or row.get("sniper_5m") or row.get("strike") is None:
            return
        now = self.clock()
        if not row["start"] <= now < row["end"] or row.get("skip_reason") == "startup":
            return
        jump_fn = getattr(self.grid, "jump_in_window", None)
        if not callable(jump_fn):
            return
        try:
            info = jump_fn(float(cfg["sniper_window_ms"]))
        except Exception:
            return
        if info is None:
            return
        threshold = float(cfg["sniper_jump_usd"]) - EPS
        if float(info["up_jump"]) < threshold and float(info["down_jump"]) < threshold:
            return
        if not self._tick_lock.acquire(blocking=False):
            return
        try:
            sides = {}
            for side, token in (("up", row["up_token"]), ("down", row["dn_token"])):
                try:
                    top = self.books.usable(token)
                except Exception:
                    top = None
                if top is not None:
                    sides[side] = {"bid": top.bid, "ask": top.ask, "bids": top.bids, "asks": top.asks,
                                   "src": "live"}
            self.strategy._sniper_check(cfg, now, row, sides, self.grid, row["end"] - now)
        except Exception as exc:
            self._on_error("fivemin_sniper_tick_error", exc)
        finally:
            self._tick_lock.release()

    def _live_book(self, token):
        try:
            ws = getattr(self.books, "ws", None)
            src = ws if ws is not None else self.books
            top = src.usable(token)
        except Exception:
            return None
        if top is None or top.bid is None:
            return None
        return {"bid": top.bid, "ask": top.ask, "bids": top.bids, "asks": top.asks, "src": "live"}

    def _rest_refetch_book(self, token):
        rest = getattr(self.books, "rest", None)
        fetch = getattr(rest if rest is not None else self.books, "fetch_pair", None)
        if fetch is None:
            return None
        row = next((r for r in self.strategy.state["markets"].values()
                    if token in (r.get("up_token"), r.get("dn_token"))), None)
        if row is None:
            return None
        fail = {}
        up_tok, dn_tok = row.get("up_token"), row.get("dn_token")
        try:
            accepts_fail = inspect.signature(fetch).bind(None, None, fail={}) is not None
        except (TypeError, ValueError):
            accepts_fail = False
        try:
            rows = fetch(up_tok, dn_tok, fail=fail) if accepts_fail else fetch(up_tok, dn_tok)
        except Exception:
            return None
        if not isinstance(rows, (tuple, list)) or len(rows) < 2 or fail.get("failed"):
            return None
        entry = rows[0] if token == up_tok else rows[1]
        if isinstance(entry, dict) and entry.get("failed"):
            return None
        try:
            _bid, _size, bids, _ask, _ask_size, asks, _age = entry
        except (TypeError, ValueError):
            return None
        return {"bids": bids, "asks": asks}

    def start(self):
        self._io_stop.clear()
        self.binance.start()
        self.rtds.start()
        self.books.start()
        self._io_thread = threading.Thread(target=self._io_run, name="mintbot-fivemin-io", daemon=True)
        self._io_thread.start()

    def stop(self):
        self._io_stop.set()
        if self._io_thread is not None:
            self._io_thread.join(timeout=5)
        self.binance.stop()
        self.rtds.stop()
        self.books.stop()
        if self.tape is not None:
            try:
                self.tape.close()
            except Exception:
                pass

    def request_gamma(self, slug):
        with self._io_lock:
            if slug in self._gamma_results:
                return self._gamma_results.pop(slug)
            self._gamma_pending.add(slug)
        return None

    def _io_run(self):
        while not self._io_stop.is_set():
            self._io_once(self.clock())
            self._io_stop.wait(0.1)

    def _io_once(self, now):
        """One IO-thread pass: discovery, balance, Gamma requests, RTDS watchdog."""
        cfg = settings(self.cfg())
        if now >= self.next_discover:
            self.next_discover = now + 20
            try:
                markets = self.gateway.discover([cfg["fivemin_series_slug"]])
                with self._io_lock:
                    self._markets = list(markets)
            except Exception as exc:
                self._on_error("fivemin_discovery_error", exc)
        if now >= self.next_cash:
            self.next_cash = now + 30
            try:
                balance = self.chain.pUSD_balance(cfg["pUSD_address"], self.funder)
                with self._io_lock:
                    self._balance = (now, balance)
            except Exception as exc:
                self._on_error("fivemin_balance_error", exc)
        with self._io_lock:
            requests = list(self._gamma_pending)
            self._gamma_pending.clear()
        for slug in requests:
            try:
                result = self.gamma_fetch(slug) if self.gamma_fetch else None
            except Exception as exc:
                self._on_error("fivemin_gamma_error", exc)
                result = None
            with self._io_lock:
                self._gamma_results[slug] = result
        silent_s = float(cfg["fivemin_rtds_silent_s"])
        if now - self.rtds_last_sample >= silent_s and now - self.rtds_last_reconnect >= silent_s:
            try:
                self.rtds.reconnect("fivemin_silent")
            except Exception:
                pass
            self.rtds_last_reconnect = now
            self._on_error("fivemin_rtds_stall", f"fivemin_silent silent_s={silent_s:g}")

    def _on_error(self, event, exc):
        try: self.strategy.io.log(event, error=str(exc)[:240])
        except Exception: pass

    def tick(self):
        with self._tick_lock:
            self._tick_once()

    def _tick_once(self):
        now, cfg = self.clock(), settings(self.cfg())
        samples = list(self.rtds.drain())
        if samples:
            self.rtds_last_sample = now
        self.strategy.boundary_samples(samples)
        with self._io_lock:
            markets = self._markets
            balance = self._balance
            self._balance = None
        if markets is not self._last_markets:
            self._last_markets = markets
            for market in markets:
                if market.active and not market.closed and not market.neg_risk and str(market.condition_id) not in self.strategy.state["markets"]:
                    self.strategy.add_market(market, now=now)
        if balance is not None:
            self.strategy.sample_cash(*balance)
        self._last_poll = {cid: stamp for cid, stamp in self._last_poll.items()
                           if cid in self.strategy.state["markets"]}
        rows = [self.strategy.state["markets"][cid] for cid in self.strategy._active]
        current = [row for row in rows if row["start"] <= now < row["end"]]
        upcoming = sorted((row for row in rows if row["start"] > now), key=lambda row: row["start"])
        self.current = max(current, key=lambda row: row["start"]) if current else (upcoming[0] if upcoming else None)
        chosen = ([self.current] if self.current else []) + [row for row in upcoming if row is not self.current][:1]
        # Restored open positions remain managed outside their entry window.
        for row in rows:
            if self._has_position(row) and row not in chosen:
                chosen.append(row)
        chosen = [row for row in chosen if self._market_active(row, now, cfg)]
        tokens = [token for row in chosen for token in (row["up_token"], row["dn_token"])]
        self.books.set_tokens(tokens)
        if not dry_run(cfg) and self.strategy.io.warmup is not None:
            token_set = frozenset(tokens)
            if token_set != self._warmup_tokens:
                self._warmup_tokens = token_set
                self.strategy.io.warmup(tokens)
        self.books.set_stale_s(cfg["fivemin_book_stale_s"])
        self.books.set_min_size(0.0)
        quotes = {}
        for row in chosen:
            cid = row["condition_id"]
            poll = getattr(self.books, "poll", None)
            if callable(poll) and now - self._last_poll.get(cid, float("-inf")) + EPS >= float(cfg["fivemin_poll_s"]):
                self._last_poll[cid] = now
                poll(now, row["up_token"], row["dn_token"])
            sides = {}
            for side, token in (("up", row["up_token"]), ("down", row["dn_token"])):
                top = self.books.usable(token)
                if top is not None:
                    source = getattr(self.books, "source", None)
                    src = source(token) if callable(source) else None
                    sides[side] = {"bid": top.bid, "ask": top.ask, "bids": top.bids, "asks": top.asks,
                                   "src": src if isinstance(src, str) else "rest",
                                   "age_ms": None if getattr(top, "age", None) is None
                                   else round(top.age * 1000.0, 1)}
                else:
                    bid_status = getattr(self.books, "reason", None) or getattr(self.books, "bid_status", None)
                    status = bid_status(token) if callable(bid_status) else "missing"
                    sides[side] = {"bid": None, "miss": status}
            quotes[row["condition_id"]] = sides
        self.strategy.io.gamma_winner = self.request_gamma
        self.strategy.tick(cfg, self.clock(), self.grid, quotes)
        if self.tape is not None:
            try:
                self.tape.sample(cfg, now, chosen, quotes, self.grid, self.strategy)
                self.tape.prune(now)
            except Exception:
                pass

    @staticmethod
    def _has_position(row):
        return pending_position(row)

    def _market_active(self, row, now, cfg):
        return 0 < row["end"] - now <= float(cfg["fivemin_active_window_s"]) or self._has_position(row)

    def sleep_s(self):
        now = self.clock()
        cfg = settings(self.cfg())
        for cid in self.strategy._active:
            row = self.strategy.state["markets"][cid]
            if self._has_position(row) or 0 < row["end"] - now <= float(cfg["fivemin_active_window_s"]) + 1:
                return max(0.1, float(cfg["fivemin_poll_s"]))
        return 1.0


def load_state(path: Path) -> dict:
    if not path.exists():
        return {}
    with path.open(encoding="utf-8") as handle:
        state = json.load(handle)
    if not isinstance(state, dict):
        raise ValueError("fivemin state must be an object")
    return state
