#!/usr/bin/env python3
"""Idle lockbot shell with legacy settlement and optional book logging.

Copy-trading strategies s1/s2/s3 removed by Joel 2026-10-05.
This process has no order client or entry path, including in live ledger mode.
"""
from __future__ import annotations

import argparse
import fcntl
import json
import os
import signal
import threading
import time
from pathlib import Path
from typing import Any, Optional

import requests

from buy.lock_book_log import BookSnapshotLogger
from buy.lock_bookws import ClobBookFeed
from buy.lock_config import apply_defaults, validate_config
from buy.lock_gates import settle_pnl
from buy.lock_markets import (
    DURATION_S, SPECS, LockMarket, enabled_keys, event_slug,
    market_fetch_due, parse_lock_event, parse_slug, window_starts,
)
from buy.lock_state import ledger_path, load_ledger, load_windows, reset_live_ledger, strike_for_position, window_file
from buy.oracle_log import RtdsTwapFeed, append_jsonl

ROOT = Path(__file__).resolve().parent
EXAMPLE_FILE = ROOT / "lockbot.example.json"
CONFIG_FILE = ROOT / "lockbot.json"
LOG_FILE = ROOT / "logs" / "lockbot.jsonl"
LOCK_FILE = ROOT / ".lockbot.lock"
STOP_FILE = ROOT / "STOP_LOCKBOT"
USER_AGENT = "poly-money-maker-lockbot/1.0"
_stop = threading.Event()


def _handle_signal(signum, frame) -> None:
    del signum, frame
    _stop.set()


def log_event(event: str, **kwargs: Any) -> None:
    row = {"ts": time.time(), "event": event, **kwargs}
    try:
        append_jsonl(LOG_FILE, row, max_bytes=20_000_000)
    except Exception:
        pass
    print(json.dumps(row, default=str, separators=(",", ":")), flush=True)


def atomic_save(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, separators=(",", ":"), sort_keys=True), encoding="utf-8")
    os.replace(temporary, path)


def resolve_config_path(explicit: Optional[str] = None) -> Path:
    if explicit:
        return Path(explicit)
    if CONFIG_FILE.exists():
        return CONFIG_FILE
    return EXAMPLE_FILE


def read_config(path: Path) -> dict:
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError("config must be an object")
    cfg = apply_defaults(raw)
    validate_config(cfg)
    return cfg


def _session() -> requests.Session:
    session = requests.Session()
    session.headers.update({"User-Agent": USER_AGENT, "Accept": "application/json"})
    return session


def fetch_market(session: requests.Session, gamma_url: str, slug: str) -> Optional[LockMarket]:
    response = session.get(
        f"{gamma_url.rstrip('/')}/events",
        params={"slug": slug},
        timeout=8,
    )
    response.raise_for_status()
    payload = response.json()
    event = payload[0] if isinstance(payload, list) and payload else payload
    return parse_lock_event(event)


class LockBot:
    def __init__(self, cfg: dict, *, config_path: Path) -> None:
        self.cfg = cfg
        self.config_path = config_path
        self.config_mtime = config_path.stat().st_mtime if config_path.exists() else 0.0
        self._lock = threading.RLock()
        self.session = _session()
        self.markets: dict[str, LockMarket] = {}
        self.market_at: dict[str, float] = {}
        self._settlement_cache: dict[str, tuple[float, Optional[LockMarket]]] = {}
        self.gamma_px: dict[str, float] = {}
        self.feeds: dict[str, RtdsTwapFeed] = {}
        self._logged_resolution: set[str] = set()
        self.windows = load_windows(window_file(ROOT), time.time())
        self.latched = {slug: float(row["strike"]) for slug, row in self.windows.items()}
        self.book_feed = ClobBookFeed()
        self.book_log = BookSnapshotLogger(ROOT, self.book_feed.book)
        self._bind_ledger(bool(cfg.get("dry_run", True)))
        self._apply_book_log(time.time())

    def _bind_ledger(self, dry_run: bool) -> None:
        kind = "paper" if dry_run else "live"
        with self._lock:
            self.state_path = ledger_path(ROOT, dry_run)
            self.state = load_ledger(self.state_path, kind)
            self._settlement_cache.clear()
        log_event("ledger", ledger=kind, path=str(self.state_path), positions=len(self.state["positions"]))

    def reload(self) -> None:
        try:
            mtime = self.config_path.stat().st_mtime
            if mtime == self.config_mtime:
                return
            cfg = read_config(self.config_path)
        except Exception as exc:
            log_event("config_reload_fail", error=str(exc)[:200])
            return
        previous = bool(self.cfg.get("dry_run", True))
        self.cfg = cfg
        self.config_mtime = mtime
        if previous != bool(cfg["dry_run"]):
            self._bind_ledger(bool(cfg["dry_run"]))
        self._apply_book_log(time.time())
        log_event("config_reloaded", path=str(self.config_path), dry_run=cfg["dry_run"], mode="idle")

    def ensure_feeds(self) -> None:
        wanted = set()
        for pos in self.state["positions"].values():
            if not isinstance(pos, dict) or pos.get("settled_ts") or float(pos.get("shares") or 0) <= 0:
                continue
            parsed = parse_slug(str(pos.get("slug") or ""))
            if parsed:
                wanted.add(f"{parsed[0]}/usd")
        for symbol in wanted - self.feeds.keys():
            feed = RtdsTwapFeed(symbols=(symbol,), history_s=float(self.cfg["history_s"]))
            feed.start()
            self.feeds[symbol] = feed
        for symbol in self.feeds.keys() - wanted:
            self.feeds.pop(symbol).stop()
        if self.cfg["book_log_enabled"]:
            self.book_feed.start()
        else:
            self.book_feed.set_tokens([])
            self.book_feed.stop()

    def tick(self, now: Optional[float] = None) -> int:
        now = time.time() if now is None else now
        self.reload()
        self.ensure_feeds()
        self._settle_pending(now)
        if self.cfg["book_log_enabled"]:
            self.refresh_markets(now)
            self.subscribe_books(now)
        for slug, market in list(self.markets.items()):
            if market.end_ts + 30 < now:
                self.markets.pop(slug, None)
                self.market_at.pop(slug, None)
                self.gamma_px.pop(slug, None)
                self._logged_resolution.discard(slug)
        return 0

    def close(self) -> None:
        for feed in self.feeds.values():
            feed.stop()
        self.book_feed.stop()
        self.book_log.close()
        self.session.close()

    def refresh_markets(self, now: float) -> None:
        gamma = str(self.cfg.get("gamma_url"))
        refresh = float(self.cfg.get("market_refresh_s") or 20)
        for key in enabled_keys(self.cfg):
            spec = SPECS[key]
            dur = DURATION_S[spec["duration"]]
            for start in window_starts(now, dur, ahead=1):
                slug = event_slug(spec["asset"], spec["duration"], start)
                existing = self.markets.get(slug)
                have_strike = (
                    existing is not None and existing.price_to_beat is not None
                ) or self.gamma_px.get(slug) is not None
                # One /events fetch. While priceToBeat is missing, use the
                # shorter gamma interval. There is no second strike request.
                cadence = refresh if have_strike else float(self.cfg.get("gamma_refresh_s") or refresh)
                if not market_fetch_due(
                    now=now,
                    fetched_at=self.market_at.get(slug, 0.0),
                    have_market=existing is not None,
                    have_strike=have_strike,
                    refresh_s=cadence,
                ):
                    continue
                try:
                    market = fetch_market(self.session, gamma, slug)
                except Exception as exc:
                    log_event("market_fetch_fail", slug=slug, error=str(exc)[:160])
                    continue
                if market is None:
                    continue
                with self._lock:
                    self.markets[slug] = market
                    self.market_at[slug] = now
                if not market.resolution_ok and slug not in self._logged_resolution:
                    self._logged_resolution.add(slug)
                    log_event(
                        "resolution_unsupported",
                        slug=slug,
                        asset=market.asset,
                        duration=market.duration,
                        resolution_source=market.resolution_source,
                    )
                if market.price_to_beat is not None:
                    self.gamma_px[slug] = market.price_to_beat

    def _book_markets(self, now: float) -> list[LockMarket]:
        """Current and next BTC 5m and 15m windows only. Expired ids drop off."""
        keys = {"btc_5m", "btc_15m"} & set(enabled_keys(self.cfg))
        out: list[LockMarket] = []
        seen: set[str] = set()
        for key in sorted(keys):
            spec = SPECS[key]
            starts = set(window_starts(now, DURATION_S[spec["duration"]], ahead=1))
            for market in self.markets.values():
                if market.key != key or market.start_ts not in starts or market.slug in seen:
                    continue
                seen.add(market.slug)
                out.append(market)
        return out

    def _apply_book_log(self, now: float) -> None:
        """Hot-reload the ``book_log_*`` knobs. The feed hook stays unset while off."""
        self.book_log.configure(self.cfg)
        if self.book_log.enabled:
            self.book_log.set_markets(self._book_markets(now))
            self.book_feed.touch_hook = self.book_log.on_touch
        else:
            self.book_feed.touch_hook = None

    def subscribe_books(self, now: float) -> None:
        tokens: list[str] = []
        markets = self._book_markets(now)
        for market in markets:
            tokens.append(market.up_token)
            tokens.append(market.dn_token)
        self.book_feed.set_tokens(tokens)
        if self.book_log.enabled:
            self.book_log.set_markets(markets)

    def _positions_for(self, slug: str) -> list[dict]:
        out = []
        for key, pos in self.state["positions"].items():
            if not isinstance(pos, dict):
                continue
            if pos.get("slug") == slug or key == slug:
                out.append(pos)
        return out

    def _settle_pending(self, now: float) -> None:
        """Retry expired ledger slugs; fetch Gamma outside the ledger lock."""
        from buy.lock_markets import boundary_price

        with self._lock:
            pending = {}
            for pos in self.state["positions"].values():
                if not isinstance(pos, dict) or pos.get("settled_ts"):
                    continue
                if pos.get("end_ts") is None or float(pos["end_ts"]) > now:
                    continue
                if float(pos.get("shares") or 0) <= 0:
                    continue
                slug = str(pos.get("slug") or "")
                if slug:
                    pending.setdefault(slug, []).append(dict(pos))
        for slug in list(self._settlement_cache):
            if slug not in pending:
                self._settlement_cache.pop(slug, None)
        cadence = max(20.0, float(self.cfg.get("market_refresh_s") or 20))
        tol = max(float(self.cfg.get("strike_tol_s") or 0.75), 1.25)
        for slug, rows in pending.items():
            fetched_at, cached = self._settlement_cache.get(slug, (None, None))
            market = cached or self.markets.get(slug)
            if market is None:
                pos = rows[0]
                parsed = parse_slug(slug)
                asset, duration, start = parsed or (
                    str(pos.get("asset") or "btc"),
                    str(pos.get("duration") or "5m"),
                    float(pos.get("start_ts") or 0),
                )
                key = f"{asset}_{duration}"
                market = LockMarket(
                    asset=asset, duration=duration, lane=str(pos.get("lane") or key),
                    key=key, symbol=f"{asset}/usd", slug=slug, series_slug="",
                    condition_id=str(pos.get("condition_id") or ""), question="",
                    start_ts=start, end_ts=float(pos["end_ts"]),
                    up_token=str(pos.get("up_token") or ""),
                    dn_token=str(pos.get("dn_token") or ""),
                    resolution_source="", price_to_beat=None,
                    resolution_ok=False, accepting_orders=False,
                )
            feed = self.feeds.get(market.symbol)
            hist = feed.twap_history() if feed is not None else []
            final = boundary_price(hist, market.end_ts, tol_s=tol)
            need_gamma = final is None or any(
                self._settlement_strike(row, market, hist, tol) is None for row in rows
            )
            # A miss never stops retries. HTTP is limited per slug, even on failure.
            if need_gamma and market.resolved_winner is None and (
                fetched_at is None or now - fetched_at >= cadence
            ):
                fetched = None
                try:
                    fetched = fetch_market(self.session, str(self.cfg.get("gamma_url")), slug)
                except Exception as exc:
                    log_event("settlement_fetch_fail", slug=slug, error=str(exc)[:160])
                self._settlement_cache[slug] = (now, fetched or cached)
                if fetched is not None:
                    market = fetched
            self._settle(market, now)

    def _settlement_strike(self, pos: dict, market: LockMarket, hist: list, tol: float) -> Optional[float]:
        from buy.lock_markets import boundary_price

        strike = strike_for_position(pos, self.latched)
        if strike is not None:
            return strike
        row = self.windows.get(market.slug) or {}
        for value in (market.price_to_beat, self.gamma_px.get(market.slug), row.get("strike")):
            strike = strike_for_position({"strike": value}, {})
            if strike is not None:
                return strike
        return boundary_price(hist, market.start_ts, tol_s=tol)

    def _settle(self, market: LockMarket, now: float) -> None:
        with self._lock:
            self._settle_locked(market, now)

    def _settle_locked(self, market: LockMarket, now: float) -> None:
        if now < market.end_ts:
            return
        rows = self._positions_for(market.slug)
        if not rows:
            return
        feed = self.feeds.get(market.symbol)
        twap_hist = feed.twap_history() if feed is not None else []
        from buy.lock_markets import boundary_price

        tol = max(float(self.cfg.get("strike_tol_s") or 0.75), 1.25)
        final = boundary_price(twap_hist, market.end_ts, tol_s=tol)
        changed = False
        for pos in rows:
            if pos.get("settled_ts") or float(pos.get("shares") or 0) <= 0:
                continue
            strike = self._settlement_strike(pos, market, twap_hist, tol)
            if strike is not None and pos.get("strike") is None:
                pos["strike"] = float(strike)
                changed = True
            have_twap = final is not None and strike is not None
            if not have_twap and market.resolved_winner is None:
                if now - market.end_ts > 600 and not pos.get("settle_miss_logged"):
                    pos["settle_miss_logged"] = True
                    changed = True
                    log_event(
                        "settlement_unknown",
                        slug=market.slug,
                        strategy=pos.get("strategy"),
                        side=pos.get("side"),
                        have_final=final is not None,
                        have_strike=strike is not None,
                    )
                continue
            if have_twap:
                result = settle_pnl(
                    side=str(pos.get("side") or ""), shares=float(pos["shares"]),
                    cost=float(pos["cost"]), fee=float(pos.get("fee") or 0),
                    final_twap=float(final), strike=float(strike),
                )
                source = "twap"
            else:
                won = str(pos.get("side") or "") == market.resolved_winner
                payout = float(pos["shares"]) if won else 0.0
                result = {
                    "won": won, "up_wins": market.resolved_winner == "up",
                    "payout": payout,
                    "pnl": payout - float(pos["cost"]) - float(pos.get("fee") or 0),
                }
                source = "gamma"
            pos.update(result)
            pos["final_twap"] = final
            pos["settlement_source"] = source
            pos["settled_ts"] = now
            changed = True
            log_event(
                "settlement",
                slug=market.slug,
                asset=market.asset,
                duration=market.duration,
                lane=market.lane,
                strategy=pos.get("strategy"),
                condition_id=market.condition_id,
                side=pos.get("side"),
                settlement_source=source,
                won=result["won"],
                pnl=round(result["pnl"], 6),
                payout=round(result["payout"], 6),
                cost=pos["cost"],
                fee=pos.get("fee"),
                shares=pos["shares"],
                strike=strike,
                final_twap=final,
                dry_run=pos.get("dry_run"),
            )
            if pos.get("dry_run"):
                log_event(
                    "redeem_dry_run",
                    slug=market.slug,
                    strategy=pos.get("strategy"),
                    side=pos.get("side"),
                    condition_id=market.condition_id,
                    payout_est=round(result["payout"], 6),
                    won=result["won"],
                )
        if changed:
            atomic_save(self.state_path, self.state)


def acquire_lock():
    LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)
    handle = open(LOCK_FILE, "w", encoding="utf-8")
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        print("another lockbot holds the lock", flush=True)
        raise SystemExit(1)
    handle.write(str(os.getpid()))
    handle.flush()
    return handle


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Idle lockbot settlement shell (dry-run by default)")
    parser.add_argument("--config", default="", help="default lockbot.json or the example")
    parser.add_argument("--reset-live", action="store_true", help="zero only the live ledger and exit")
    args = parser.parse_args(argv)
    if args.reset_live:
        hold = acquire_lock()
        path = reset_live_ledger(ROOT)
        print(json.dumps({"event": "live_ledger_reset", "path": str(path)}), flush=True)
        hold.close()
        return 0
    path = resolve_config_path(args.config or None)
    try:
        cfg = read_config(path)
    except Exception as exc:
        print(f"config failed: {exc}", flush=True)
        return 1
    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)
    hold = acquire_lock()
    bot = LockBot(cfg, config_path=path)
    log_event("startup", mode="idle", detail="copy-trading strategies s1/s2/s3 removed by Joel 2026-10-05; orders unavailable", dry_run=cfg["dry_run"], ledger=str(bot.state_path), config=str(path))
    try:
        while not _stop.is_set() and not STOP_FILE.exists():
            try:
                bot.tick()
            except Exception as exc:
                log_event("tick_fail", error=str(exc)[:200])
            _stop.wait(float(bot.cfg["poll_s"]))
    finally:
        bot.close()
        hold.close()
        log_event("shutdown")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
