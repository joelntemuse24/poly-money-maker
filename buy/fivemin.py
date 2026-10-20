"""Isolated BTC 5-minute policy; no mintbot import or import-time I/O."""
from __future__ import annotations

import json
import math
import time
import threading
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional
from zoneinfo import ZoneInfo

DEFAULTS = {
    "fivemin_enabled": False, "fivemin_series_slug": "btc-up-or-down-5m",
    "fivemin_binance_url": "wss://stream.binance.com:9443/ws/btcusdt@aggTrade",
    "fivemin_shares": 10, "fivemin_buy_limit": 0.99, "fivemin_gate_k": 1.0,
    "fivemin_loser_max": 0.10, "fivemin_loser_min": 0.01, "fivemin_fav_min": 0.90,
    "fivemin_max_ttm_s": 120, "fivemin_persist_s": 2.0, "fivemin_persist_last_s": 1.0,
    "fivemin_persist_last_window_s": 90, "fivemin_dump_held_below": 0.20,
    "fivemin_dump_other_min": 0.60, "fivemin_dump_persist_s": 3.0,
    "fivemin_dump_limit_offset": 0.20, "fivemin_dump_limit_floor": 0.05,
    "fivemin_min_notional_usd": 1.0, "fivemin_daily_stop_usd": -150.0,
    "fivemin_btc_max_age_s": 2.0,
    "fivemin_cash_buffer_usd": 5.0, "fivemin_book_stale_s": 30.0, "fivemin_dry_run": None,
}
EPS = 1e-9


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
    label: Callable = lambda start: str(start)


class Strategy:
    def __init__(self, io: FiveMinIO, state: Optional[dict] = None, *, startup_ts: Optional[float] = None):
        self.io = io
        self.startup_ts = time.time() if startup_ts is None else float(startup_ts)
        self._last_prune = 0.0
        self._last_btc_stale = 0.0
        self.state = state if state is not None else {}
        self.state.setdefault("markets", {})
        self.state.setdefault("day", "")
        self.state.setdefault("realized", 0.0)
        self.state.setdefault("paused_until_day", None)
        self.boundaries: dict[int, float] = {}
        self._active = set()
        for cid, row in self.state["markets"].items():
            position = row.get("position")
            if float(row.get("start") or 0) < self.startup_ts and position is None:
                row["entered"] = True
                row.setdefault("skip_reason", "startup")
                self.io.log("fivemin_skip_startup_market", condition_id=cid,
                            slug=row.get("slug"), start=row.get("start"))
            if not position or not position.get("settled") or (
                position.get("shares", 0) > 0 and not position.get("redeem_enqueued")
                and not position.get("dry_run")
            ):
                self._active.add(cid)
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
            if row.get("end", 0) >= now - 6 * 3600:
                continue
            position = row.get("position")
            if position is None or (position.get("settled") and (
                position.get("redeem_enqueued") or position.get("shares", 0) <= 0 or position.get("dry_run")
            )):
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
            self._twap_cache[g] = twap60(grid, g)
        value = self._twap_cache[g]
        latest = getattr(grid, "latest_exchange_ms", lambda: None)()
        max_age = float(cfg.get("fivemin_btc_max_age_s", 2.0))
        if latest is None or now * 1000.0 - latest > max_age * 1000.0:
            if now - self._last_btc_stale >= 60:
                self._last_btc_stale = now
                self.io.log("fivemin_btc_stale", age_s=None if latest is None else now - latest / 1000.0)
            return None
        return None if value is None or row.get("offset") is None else value + row["offset"]

    def _eval(self, row, decision, reason, ttm, bids, ratio=None):
        run = self.runs.setdefault(row["condition_id"], {})
        if run.get("eval_logged"):
            return
        run["eval_logged"] = True
        self.io.log("fivemin_eval", slug=row["slug"], decision=decision,
                    reason=reason, ttm=round(ttm, 3), bids=bids, ratio=ratio)

    def tick(self, cfg: dict, now: float, grid, books: dict):
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
                if not row.get("entered"):
                    self._eval(row, "skip", self.runs.get(cid, {}).get("last_reason", "never_in_window"), row["end"] - now, {})
                self._active.discard(cid)
                continue
            if position and not position.get("settled"):
                self._manage(cfg, now, row, books.get(cid, {}))
            if row["entered"] or not row["start"] <= now < row["end"]:
                continue
            if strike is None:
                if not row.get("no_strike_logged"):
                    row["no_strike_logged"] = True
                    self.runs.setdefault(cid, {})["last_reason"] = "no_strike"
                    self.io.log("fivemin_no_strike", slug=row["slug"], condition_id=cid)
                    self.save()
                continue
            ttm = row["end"] - now
            quotes = books.get(cid, {})
            bids = {side: quotes.get(side, {}).get("bid") for side in ("up", "down")}
            run = self.runs.setdefault(cid, {})
            if ttm <= 0 or ttm > cfg["fivemin_max_ttm_s"]:
                if ttm <= 0:
                    self._eval(row, "skip", run.get("last_reason", "window_end"), ttm, bids)
                continue
            prefilter = any(
                bids[loser] is not None and bids[fav] is not None and
                cfg["fivemin_loser_min"] - EPS <= bids[loser] <= cfg["fivemin_loser_max"] + EPS and
                bids[fav] + EPS >= cfg["fivemin_fav_min"]
                for loser, fav in (("up", "down"), ("down", "up"))
            )
            if not prefilter:
                run["last_reason"] = "book_outside"
                continue
            need = cfg["fivemin_persist_last_s"] if ttm <= cfg["fivemin_persist_last_window_s"] else cfg["fivemin_persist_s"]
            proxy = self._proxy(grid, now, row, cfg)
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
                elif not persisted:
                    run["last_reason"] = "not_persisted"
                elif ratio is None:
                    run["last_reason"] = "btc_stale"
                else:
                    run["last_reason"] = "gate_below_k"
            if signals:
                since, loser, fav, ratio = min(signals)
                self._eval(row, "enter", "signal", ttm, bids, ratio)
                self._enter(cfg, now, row, quotes, loser, fav, ratio, proxy, now - since)

    def _enter(self, cfg, now, row, quotes, loser, fav, ratio, proxy, persisted):
        row["entered"] = True
        self.save()
        sent_now = self.io.clock() if self.io.clock is not None else now
        if sent_now >= row["end"]:
            self.io.log("fivemin_signal_blocked", slug=row["slug"], reason="expired")
            return
        log_books = {side: {**(quotes.get(side) or {}),
                            "bids": (quotes.get(side) or {}).get("bids", [])[:3],
                            "asks": (quotes.get(side) or {}).get("asks", [])[:3]}
                     for side in ("up", "down")}
        self.io.log("fivemin_signal", ts=now, slug=row["slug"], condition_id=row["condition_id"],
                    ttm=row["end"] - now, gate_ratio=ratio, proxy=proxy, strike=row["strike"],
                    offset=row.get("offset"), books=log_books, loser=loser, persisted_s=persisted)
        reason = None
        if not cfg["fivemin_enabled"]:
            reason = "disabled"
        elif self.state.get("paused_until_day") == self.state["day"]:
            reason = "daily_stop"
        if reason:
            if reason == "daily_stop":
                self.runs.setdefault(row["condition_id"], {})["last_reason"] = "daily_stop"
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
            self.alert(f"5m {self.io.label(row['start'])} {fav.upper()} fill {filled:g} @ {avg:.4f}; ttm={row['end']-now:.1f}s gate={ratio:.3f}", cfg, "fivemin_fill")

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
        self.alert(f"5m {self.io.label(row['start'])} dump {sold:g} @ {proceeds/sold if sold else 0:.4f}; P&L={p['proceeds']-p['cost']:.2f}",
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
        if now - self.rtds_last_sample >= 60 and now - self.rtds_last_reconnect >= 60:
            try:
                self.rtds.reconnect("fivemin_silent")
            except Exception:
                pass
            self.rtds_last_reconnect = now
            self._on_error("fivemin_rtds_stall", "fivemin_silent")

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
            markets = list(self._markets)
            balance = self._balance
            self._balance = None
        available = [m for m in markets if m.active and not m.closed and not m.neg_risk]
        for market in available:
            self.strategy.add_market(market, now=now)
        if balance is not None:
            self.strategy.sample_cash(*balance)
        rows = [self.strategy.state["markets"][cid] for cid in self.strategy._active]
        current = [row for row in rows if row["start"] <= now < row["end"]]
        upcoming = sorted((row for row in rows if row["start"] > now), key=lambda row: row["start"])
        self.current = max(current, key=lambda row: row["start"]) if current else (upcoming[0] if upcoming else None)
        chosen = ([self.current] if self.current else []) + [row for row in upcoming if row is not self.current][:1]
        self.books.set_tokens([token for row in chosen for token in (row["up_token"], row["dn_token"])])
        self.books.set_stale_s(cfg["fivemin_book_stale_s"])
        self.books.set_min_size(0.0)
        quotes = {}
        for row in chosen:
            sides = {}
            for side, token in (("up", row["up_token"]), ("down", row["dn_token"])):
                top = self.books.usable(token)
                if top is not None:
                    sides[side] = {"bid": top.bid, "ask": top.ask, "bids": top.bids, "asks": top.asks}
            quotes[row["condition_id"]] = sides
        self.strategy.io.gamma_winner = self.request_gamma
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
