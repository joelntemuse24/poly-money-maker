"""Isolated BTC 5-minute policy; no mintbot import or import-time I/O."""
from __future__ import annotations

import json
import math
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional
from zoneinfo import ZoneInfo

DEFAULTS = {
    "fivemin_enabled": False, "fivemin_series_slug": "btc-up-or-down-5m",
    "fivemin_shares": 25, "fivemin_buy_limit": 0.99, "fivemin_gate_k": 1.0,
    "fivemin_loser_max": 0.10, "fivemin_loser_min": 0.01, "fivemin_fav_min": 0.80,
    "fivemin_max_ttm_s": 210, "fivemin_persist_s": 3.0, "fivemin_persist_last_s": 2.0,
    "fivemin_persist_last_window_s": 90, "fivemin_dump_held_below": 0.40,
    "fivemin_dump_other_min": 0.70, "fivemin_dump_persist_s": 3.0,
    "fivemin_dump_limit_offset": 0.20, "fivemin_dump_limit_floor": 0.05,
    "fivemin_min_notional_usd": 1.0, "fivemin_daily_stop_usd": -30.0,
    "fivemin_cash_buffer_usd": 5.0, "fivemin_book_stale_s": 30.0, "fivemin_dry_run": None,
}
EPS = 1e-9


def settings(cfg: dict) -> dict:
    return {**DEFAULTS, **cfg}


def dry_run(cfg: dict) -> bool:
    value = cfg.get("fivemin_dry_run")
    return bool(cfg.get("dry_run", True) if value is None else value)


def twap60(grid, g_end: int) -> Optional[float]:
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


def settlement_pnl(position: dict, winner: str) -> float:
    return float(position.get("proceeds", 0)) + (
        float(position.get("shares", 0)) if position["side"] == winner else 0.0
    ) - float(position.get("cost", 0))


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


class Strategy:
    def __init__(self, io: FiveMinIO, state: Optional[dict] = None):
        self.io = io
        self.state = state if state is not None else {}
        self.state.setdefault("markets", {})
        self.state.setdefault("day", "")
        self.state.setdefault("realized", 0.0)
        self.state.setdefault("paused_until_day", None)
        self.boundaries: dict[int, float] = {}
        self._active = {
            cid for cid, row in self.state["markets"].items()
            if not (row.get("position") or {}).get("settled")
            or ((row.get("position") or {}).get("shares", 0) > 0
                and not (row.get("position") or {}).get("redeem_enqueued")
                and not (row.get("position") or {}).get("dry_run"))
        }
        self.runs: dict[str, dict] = {}
        self.balance = None
        self.sampled_at = None
        self.spends: list[dict] = []
        self._twap_cache = {}

    def save(self):
        self.io.save(self.state)

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
                if abs(obs - stamp) <= 0.0005 and stamp % 300 == 0 and math.isfinite(value):
                    self.boundaries.setdefault(stamp, value)
            except (TypeError, ValueError, AttributeError):
                continue
        for key in sorted(self.boundaries)[:-32]:
            del self.boundaries[key]

    def roll_day(self, now):
        day = local_day(now)
        if self.state["day"] != day:
            self.state.update(day=day, realized=0.0, paused_until_day=None)
            self.save()

    def sample_cash(self, now, balance):
        self.balance, self.sampled_at = float(balance), now
        self.spends = [row for row in self.spends if row["ts"] >= now]

    def add_market(self, market):
        cid = str(market.condition_id)
        if cid not in self.state["markets"]:
            start = int(str(market.slug).rsplit("-", 1)[1])
            self.state["markets"][cid] = {
                "condition_id": cid, "slug": market.slug, "start": start, "end": start + 300,
                "up_token": str(market.up_token), "dn_token": str(market.dn_token),
                "entered": False, "position": None,
            }
            self._active.add(cid)
            self.save()

    def _proxy(self, grid, now, row):
        g = math.floor(now * 10) - 5
        if g not in self._twap_cache:
            self._twap_cache = {g: twap60(grid, g)}
        value = self._twap_cache[g]
        return None if value is None or row.get("offset") is None else value + row["offset"]

    def tick(self, cfg: dict, now: float, grid, books: dict):
        cfg = settings(cfg)
        self.roll_day(now)
        if self.state["realized"] <= cfg["fivemin_daily_stop_usd"] and self.state.get("paused_until_day") != self.state["day"]:
            self.state["paused_until_day"] = self.state["day"]
            self.save()
            self.io.log("fivemin_daily_stop", day=self.state["day"], pnl=self.state["realized"])
            self.alert(f"5m daily stop {self.state['day']}: {self.state['realized']:.2f}", cfg, "fivemin_daily_stop")
        for cid in list(self._active):
            row = self.state["markets"][cid]
            if row.get("start_twap") is None and now >= row["start"]:
                start_twap = twap60(grid, int(row["start"] * 10))
                if start_twap is not None:
                    row["start_twap"] = start_twap
                    self.save()
            strike = row.get("strike")
            if strike is None and row["start"] in self.boundaries:
                row["strike"] = strike = self.boundaries[row["start"]]
                self.save()
            if strike is not None and row.get("offset") is None:
                offset = strike - row["start_twap"] if row.get("start_twap") is not None else None
                if offset is not None:
                    row["offset"] = offset
                    self.save()
            position = row.get("position")
            if position and position.get("settled"):
                self._redeem_remainder(cfg, now, row)
                if position.get("redeem_enqueued") or position.get("dry_run") or position["shares"] <= 0:
                    self._active.discard(cid)
                continue
            if not position and now >= row["end"]:
                self._active.discard(cid)
                continue
            if position and not position.get("settled"):
                self._manage(cfg, now, row, books.get(cid, {}))
            if row["entered"] or not row["start"] <= now < row["end"]:
                continue
            if strike is None:
                if not row.get("no_strike_logged"):
                    row["no_strike_logged"] = True
                    self.io.log("fivemin_no_strike", slug=row["slug"], condition_id=cid)
                    self.save()
                continue
            ttm = row["end"] - now
            quotes = books.get(cid, {})
            bids = {side: quotes.get(side, {}).get("bid") for side in ("up", "down")}
            run = self.runs.setdefault(cid, {})
            need = cfg["fivemin_persist_last_s"] if ttm <= cfg["fivemin_persist_last_window_s"] else cfg["fivemin_persist_s"]
            proxy = self._proxy(grid, now, row)
            signals = []
            for loser, fav in (("up", "down"), ("down", "up")):
                bid, other = bids[loser], bids[fav]
                ok = bid is not None and other is not None and (
                    cfg["fivemin_loser_min"] - EPS <= bid <= cfg["fivemin_loser_max"] + EPS
                    and other + EPS >= cfg["fivemin_fav_min"]
                    and 0 < ttm <= cfg["fivemin_max_ttm_s"] + EPS)
                run[loser], persisted = run_ready(run.get(loser), ok, now, need)
                ratio = gate_ratio(proxy, strike, fav, ttm)
                if persisted and ratio is not None and ratio + EPS >= cfg["fivemin_gate_k"]:
                    signals.append((run[loser], loser, fav, ratio))
            if signals:
                since, loser, fav, ratio = min(signals)
                self._enter(cfg, now, row, quotes, loser, fav, ratio, proxy, now - since)

    def _enter(self, cfg, now, row, quotes, loser, fav, ratio, proxy, persisted):
        row["entered"] = True
        self.save()
        if not self.io.durable():
            raise OSError("fivemin entry flag not durable")
        sent_now = self.io.clock() if self.io.clock is not None else now
        if sent_now >= row["end"]:
            self.io.log("fivemin_signal_blocked", slug=row["slug"], reason="expired")
            return
        self.io.log("fivemin_signal", ts=now, slug=row["slug"], condition_id=row["condition_id"],
                    ttm=row["end"] - now, gate_ratio=ratio, proxy=proxy, strike=row["strike"],
                    offset=row.get("offset"), books=quotes, loser=loser, persisted_s=persisted)
        reason = None
        if not cfg["fivemin_enabled"]:
            reason = "disabled"
        elif self.state.get("paused_until_day") == self.state["day"]:
            reason = "daily_stop"
        if reason:
            self.io.log("fivemin_signal_blocked", slug=row["slug"], reason=reason)
            return
        shares, limit = float(cfg["fivemin_shares"]), float(cfg["fivemin_buy_limit"])
        reserved = self.io.reserves(cfg)
        free = cash_free(self.balance, self.sampled_at, sent_now, reserved + cfg["fivemin_cash_buffer_usd"], self.spends)
        if free is None or free + EPS < shares * limit:
            self.io.log("fivemin_cash_skip", slug=row["slug"], free=free, required=shares * limit)
            return
        token = row["up_token"] if fav == "up" else row["dn_token"]
        result = self.io.buy(token, shares, limit, dry_run(cfg))
        filled = float(result.get("shares", 0))
        cost = float(result.get("cost", filled * limit))
        avg = cost / filled if filled else limit
        row["position"] = {
            "token": token, "side": fav, "shares": filled, "cost": cost, "avg_px": avg,
            "dumped": False, "proceeds": 0.0, "settled": False, "pnl": None,
            "status": result.get("status", "posted"), "dry_run": dry_run(cfg),
            "filled_at": now,
        }
        if cost > 0 and not dry_run(cfg):
            self.spends.append({"ts": now, "cost": cost})
        self.save()
        self.io.log("fivemin_fill", slug=row["slug"], condition_id=row["condition_id"],
                    price=avg, size=filled, usdc=cost, status=result.get("status"), fees=result.get("fees"))
        if filled > 0:
            self.alert(f"5m {row['start']} {fav.upper()} fill {filled:g} @ {avg:.4f}; ttm={row['end']-now:.1f}s gate={ratio:.3f}", cfg, "fivemin_fill")

    def _manage(self, cfg, now, row, quotes):
        p = row["position"]
        if now >= row["end"]:
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
                self.save()
            return
        p["dumped"] = True
        self.save()
        if not self.io.durable():
            raise OSError("fivemin dump flag not durable")
        if self.io.clock is not None and self.io.clock() >= row["end"]:
            return
        result = self.io.sell(row, p, quotes[held], cfg, now)
        sold = min(p["shares"], max(0.0, float(result.get("shares", 0))))
        proceeds = float(result.get("proceeds", sold * float(result.get("price", bid))))
        p["shares"] -= sold
        p["proceeds"] += proceeds
        self.save()
        self.io.log("fivemin_dump", slug=row["slug"], condition_id=row["condition_id"],
                    shares=sold, price=proceeds / sold if sold else None,
                    proceeds=proceeds, pnl=p["proceeds"] - p["cost"], status=result.get("status"))
        self.alert(f"5m {row['start']} dump {sold:g} @ {proceeds/sold if sold else 0:.4f}; P&L={p['proceeds']-p['cost']:.2f}",
                   {**cfg, "fivemin_dry_run": p.get("dry_run", dry_run(cfg))}, "fivemin_dump")

    def _settle(self, cfg, now, row, winner, source):
        p = row["position"]
        pnl = settlement_pnl(p, winner)
        p.update(settled=True, pnl=pnl)
        self.state["realized"] += pnl
        stopped = self.state["realized"] <= cfg["fivemin_daily_stop_usd"] and self.state.get("paused_until_day") != self.state["day"]
        if stopped:
            self.state["paused_until_day"] = self.state["day"]
        self.save()
        if not self.io.durable():
            raise OSError("fivemin settlement not durable")
        self.io.log("fivemin_settle", slug=row["slug"], condition_id=row["condition_id"], winner=winner, source=source, pnl=pnl)
        self._redeem_remainder(cfg, now, row)
        if stopped:
            self.io.log("fivemin_daily_stop", day=self.state["day"], pnl=self.state["realized"])
            self.alert(f"5m daily stop {self.state['day']}: {self.state['realized']:.2f}", cfg, "fivemin_daily_stop")

    def _redeem_remainder(self, cfg, now, row):
        p = row["position"]
        if p["shares"] <= 0 or p.get("dry_run") or p.get("redeem_enqueued"):
            return
        if cfg.get("redeem_enabled"):
            self.io.redeem(row, now)
            p["redeem_enqueued"] = True
            self.save()
        elif not p.get("redeem_disabled_logged"):
            p["redeem_disabled_logged"] = True
            self.io.log("fivemin_redeem_disabled", slug=row["slug"])
            self.save()


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

    def start(self):
        self.binance.start()
        self.rtds.start()
        self.books.start()

    def stop(self):
        self.binance.stop()
        self.rtds.stop()
        self.books.stop()

    def tick(self):
        now, cfg = self.clock(), settings(self.cfg())
        self.strategy.boundary_samples(self.rtds.drain())
        if now >= self.next_discover:
            self.next_discover = now + 20
            markets = self.gateway.discover([cfg["fivemin_series_slug"]])
            available = [m for m in markets if m.active and not m.closed and not m.neg_risk]
            live = [m for m in available if int(m.slug.rsplit("-", 1)[1]) <= now < int(m.slug.rsplit("-", 1)[1]) + 300]
            future = sorted((m for m in available if int(m.slug.rsplit("-", 1)[1]) > now), key=lambda m: m.start_ts)
            chosen_markets = ([max(live, key=lambda m: m.start_ts)] if live else []) + future[:1 if live else 2]
            for market in chosen_markets:
                self.strategy.add_market(market)
        rows = [self.strategy.state["markets"][cid] for cid in self.strategy._active]
        current = [row for row in rows if row["start"] <= now < row["end"]]
        upcoming = sorted((row for row in rows if row["start"] > now), key=lambda row: row["start"])
        self.current = max(current, key=lambda row: row["start"]) if current else (upcoming[0] if upcoming else None)
        chosen = ([self.current] if self.current else []) + [row for row in upcoming if row is not self.current][:1]
        self.books.set_tokens([token for row in chosen for token in (row["up_token"], row["dn_token"])])
        self.books.set_stale_s(cfg["fivemin_book_stale_s"])
        self.books.set_min_size(0.0)
        if now >= self.next_cash:
            self.next_cash = now + 30
            try:
                self.strategy.sample_cash(now, self.chain.pUSD_balance(cfg["pUSD_address"], self.funder))
            except Exception:
                pass
        quotes = {}
        for row in chosen:
            sides = {}
            for side, token in (("up", row["up_token"]), ("down", row["dn_token"])):
                top = self.books.usable(token)
                if top is not None:
                    sides[side] = {"bid": top.bid, "ask": top.ask, "bids": top.bids[:3], "asks": top.asks[:3]}
            quotes[row["condition_id"]] = sides
        self.strategy.tick(cfg, self.clock(), self.grid, quotes)

    def sleep_s(self):
        now = self.clock()
        return 0.1 if self.current and self.current["start"] - 65 <= now <= self.current["end"] + 5 else 1.0


def load_state(path: Path) -> dict:
    if not path.exists():
        return {}
    with path.open(encoding="utf-8") as handle:
        state = json.load(handle)
    if not isinstance(state, dict):
        raise ValueError("fivemin state must be an object")
    return state
