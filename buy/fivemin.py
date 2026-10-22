"""Isolated BTC 5-minute policy; no mintbot import or import-time I/O."""
from __future__ import annotations

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

DEFAULTS = {
    "fivemin_book_ws": False, "fivemin_book_ws_stale_s": 1.0, "fivemin_poll_s": 0.2, "fivemin_active_window_s": 130,
    "fivemin_enabled": False, "fivemin_series_slug": "btc-up-or-down-5m",
    "fivemin_binance_url": "wss://stream.binance.com:9443/ws/btcusdt@aggTrade",
    "fivemin_shares": 10, "fivemin_buy_limit": 0.99, "fivemin_gate_k": 1.0,
    "fivemin_side98_enabled": False, "fivemin_side98_shares": 20, "fivemin_side98_price": 0.98,
    "fivemin_side98_min_gap_usd": 15.0, "fivemin_side98_max_ttc_s": 60,
    "fivemin_side98_max_adverse_30s_usd": 5.0,
    "fivemin_loser_max": 0.10, "fivemin_loser_min": 0.01, "fivemin_fav_min": 0.90,
    "fivemin_max_ttm_s": 120, "fivemin_persist_s": 2.0, "fivemin_persist_last_s": 1.0,
    "fivemin_persist_last_window_s": 90, "fivemin_dump_held_below": 0.20,
    "fivemin_dump_other_min": 0.60, "fivemin_dump_persist_s": 3.0,
    "fivemin_dump_limit_offset": 0.20, "fivemin_dump_limit_floor": 0.05,
    "fivemin_min_notional_usd": 1.0, "fivemin_daily_stop_usd": -150.0,
    "fivemin_btc_max_age_s": 3.0, "fivemin_rtds_silent_s": 20.0,
    "fivemin_cash_buffer_usd": 5.0, "fivemin_book_stale_s": 30.0, "fivemin_dry_run": None,
}
EPS = 1e-9
STRIKE_GRACE_S = 5.0


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


def ask_depth(book: dict) -> dict:
    levels = [level for level in ask_levels(book) if level[0] <= 0.99]
    return {"asks_depth": levels,
            "cum_098": sum(size for price, size in levels if price <= 0.98),
            "cum_099": sum(size for price, size in levels)}


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
    return [p for p in (row.get("position"), (row.get("side98") or {}).get("position")) if p]


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
        g = math.floor(now * 10) - 5
        if g not in self._twap_cache:
            self._twap_cache = {g: twap60(grid, g)}
        value = self._twap_cache[g]
        latest = getattr(grid, "latest_exchange_ms", lambda: None)()
        max_age = float(cfg.get("fivemin_btc_max_age_s", 3.0))
        if latest is None or now * 1000.0 - latest > max_age * 1000.0:
            if now - self._last_btc_stale >= 60:
                self._last_btc_stale = now
                self.io.log("fivemin_btc_stale", age_s=None if latest is None else now - latest / 1000.0)
            return None
        return None if value is None or row.get("offset") is None else value + row["offset"]

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
            if held and not pending_position(row):
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
                               blocked_gate_below_k=run.get("blocked_gate_below_k", 0), miss=run.get("miss", {"up": {}, "down": {}}))
                if not pending_position(row):
                    self._active.discard(cid)
                continue
            if position and not position.get("settled") and now < row["end"]:
                self._manage(cfg, now, row, books.get(cid, {}))
            if row["entered"] or not row["start"] <= now < row["end"]:
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
            if ttm > cfg["fivemin_max_ttm_s"]:
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
            prefilter = any(
                bids[loser] is not None and bids[fav] is not None and
                cfg["fivemin_loser_min"] - EPS <= bids[loser] <= cfg["fivemin_loser_max"] + EPS and
                bids[fav] + EPS >= cfg["fivemin_fav_min"]
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
                bid, other = bids[loser], bids[fav]
                ok = bid is not None and other is not None and (
                    cfg["fivemin_loser_min"] - EPS <= bid <= cfg["fivemin_loser_max"] + EPS
                    and other + EPS >= cfg["fivemin_fav_min"]
                    and 0 < ttm <= cfg["fivemin_max_ttm_s"] + EPS)
                run[loser], persisted = run_ready(run.get(loser), ok, now, need)
                ratio = gate_ratio(proxy, strike, fav, ttm)
                if ok:
                    run["depth"] = ask_depth(quotes.get(fav, {}))
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
                self._eval(row, "enter", "signal", ttm, bids, ratio)
                self._enter(cfg, now, row, quotes, loser, fav, ratio, proxy, now - since)
            elif cfg["fivemin_side98_enabled"] and not row.get("side98"):
                self._side98_check(cfg, now, row, quotes, grid, ttm)

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
        latest = getattr(grid, "latest_exchange_ms", lambda: None)()
        if latest is None or now * 1000 - latest > cfg["fivemin_btc_max_age_s"] * 1000:
            return
        g = math.floor(now * 10) - 5
        spot, past = grid.price_at(g), grid.price_at(g - 300)
        if spot is None or past is None:
            return
        sign = 1 if fav == "up" else -1
        gap = sign * (spot + row["offset"] - row["strike"])
        mom30 = sign * (spot - past)
        if gap < cfg["fivemin_side98_min_gap_usd"] - EPS or mom30 < -cfg["fivemin_side98_max_adverse_30s_usd"] - EPS:
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
                    timing=result.get("timing"), gap=gap, mom30=mom30, ttm=row["end"] - now)
        tag = f"no fill @ {limit:.4f}" if kind == "fivemin_side98_nofill" else f"fill {filled:g} @ {avg:.4f}"
        self.alert(f"5m 98 SIDE ORDER {self.io.label(row['start'])} {fav.upper()} {tag}; ttm={row['end']-now:.1f}s gap=${gap:.2f} mom30=${mom30:.2f}",
                   cfg, kind)

    def _enter(self, cfg, now, row, quotes, loser, fav, ratio, proxy, persisted):
        decision_time = self.io.clock() if self.io.clock is not None else time.perf_counter()
        row["entered"] = True
        sent_now = decision_time if self.io.clock is not None else now

        def record_signal():
            self.save(force=True)
            log_books = deepcopy({side: quotes.get(side) or {} for side in ("up", "down")})
            for book in log_books.values():
                book["bids"] = book.get("bids", [])[:3]
                book["asks"] = ask_levels(book)[:3]
            self.io.log("fivemin_signal", ts=now, slug=row["slug"], condition_id=row["condition_id"],
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
                    price=avg, size=filled, usdc=cost, status=result.get("status"),
                    fees=result.get("fees"), dry_run=is_dry, latency_ms=latency_ms,
                    timing=result.get("timing"))
        if filled == 0 and not is_dry:
            self.io.log("fivemin_signal_nofill", slug=row["slug"], side=fav, limit=limit,
                        latency_ms=latency_ms, timing=result.get("timing"))
            self.alert(f"5m {row['slug']} {fav.upper()} no fill @ {limit:.4f}", cfg, "fivemin_signal_nofill")
        else:
            tag = f"realistic fill {filled:g} @ {avg:.4f} (of {shares:g}, book walk, fee {float(result.get('fees') or 0):.2f})" if is_dry else f"fill {filled:g} @ {avg:.4f}; {latency_ms:.0f}ms"
            self.alert(f"5m {self.io.label(row['start'])} {fav.upper()} {tag}; ttm={row['end']-now:.1f}s gate={ratio:.3f}", cfg, "fivemin_fill")

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
        self.io.log("fivemin_side98_settle" if p.get("kind") == "side98" else "fivemin_settle", slug=row["slug"], condition_id=row["condition_id"], winner=winner, source=source, pnl=pnl)
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
                 grid, binance, rtds, book_feed, *, clock=time.time):
        self.strategy, self.cfg = strategy, cfg
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
        self._markets = []
        self._balance = None
        self._gamma_pending = set()
        self._gamma_results = {}
        self.gamma_fetch = None
        self.rtds_last_sample = self.clock()
        self.rtds_last_reconnect = 0.0

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
                    sides[side] = {"bid": top.bid, "ask": top.ask, "bids": top.bids, "asks": top.asks}
                else:
                    bid_status = getattr(self.books, "reason", None) or getattr(self.books, "bid_status", None)
                    status = bid_status(token) if callable(bid_status) else "missing"
                    sides[side] = {"bid": None, "miss": status}
            quotes[row["condition_id"]] = sides
        self.strategy.io.gamma_winner = self.request_gamma
        self.strategy.tick(cfg, self.clock(), self.grid, quotes)

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
