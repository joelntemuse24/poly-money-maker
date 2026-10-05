#!/usr/bin/env python3
"""Two-strategy taker for Polymarket BTC up/down windows.

Separate from mintbot. It does not import mintbot, does not take the mint
lock, and does not read ``strategy_mint.json``. Default config is
``lockbot.example.json`` (dry_run true). A gitignored ``lockbot.json``
wins when it exists. Entries are hold-to-settlement; this process never sells.

Strategy 1 is the NIULAI4 ladder on BTC 15m and BTC 5m in the last minute.
Strategy 2 is the Binance 3-second move sniper on BTC 5m. The decision tick
reads the in-memory Chainlink path, the Binance trade, and the CLOB market
websocket. Gamma and the order post stay off that tick. The wallet tape
is off unless ``h2h_enabled`` is set. It never places or changes an order.
"""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import signal
import sys
import threading
import time
from collections import deque
from pathlib import Path
from typing import Any, Optional

import requests

from buy.lock_binance import BinanceTradeFeed
from buy.lock_book_log import BookSnapshotLogger
from buy.lock_bookws import ClobBookFeed
from buy.lock_config import apply_defaults, validate_config
from buy.lock_engine import build_view, s2_quote_view
from buy.lock_fair import side_z, signed_move
from buy.lock_gates import (
    day_pnl,
    dublin_day,
    evaluate_strategy1,
    evaluate_strategy2,
    loss_stop_active,
    open_exposure_usd,
    parse_levels,
    settle_pnl,
)
from buy.lock_orders import LivePoster, dispatch_buy, normalize_fill, open_live_client, warm_market
from buy.lock_paper import enqueue_paper, take_due, walk_late_book
from buy.lock_markets import (
    DURATION_S,
    SPECS,
    LockMarket,
    enabled_keys,
    event_slug,
    market_fetch_due,
    parse_lock_event,
    parse_slug,
    symbols_for,
    window_starts,
)
from buy.lock_state import (
    decision_wait_s,
    ledger_path,
    load_ledger,
    load_windows,
    reset_live_ledger,
    save_windows,
    strike_for_position,
    window_file,
)
from buy.lock_ws import wsaccel_available
from buy.lock_wallets import WALLETS, WalletTape, compare_fills
from buy.mint_gas import mint_gas_settings
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


def fetch_book(session: requests.Session, clob_url: str, token_id: str) -> dict:
    response = session.get(
        f"{clob_url.rstrip('/')}/book",
        params={"token_id": token_id},
        timeout=3,
    )
    response.raise_for_status()
    body = response.json()
    if not isinstance(body, dict):
        raise ValueError("book was not an object")
    return {
        "asks": parse_levels(body.get("asks"), "ask"),
        "bids": parse_levels(body.get("bids"), "bid"),
        "recv_ts": time.time(),
    }


def _top(levels: Any, n: int = 3) -> list:
    out = []
    for row in list(levels or [])[:n]:
        if isinstance(row, (tuple, list)) and len(row) >= 2:
            out.append([round(float(row[0]), 4), round(float(row[1]), 2)])
    return out


def _public_decision(decision: dict) -> dict:
    """Drop the full book. Keep the top of book for the log line."""
    skip = {"asks", "bids"}
    out = {key: value for key, value in decision.items() if key not in skip}
    out["ask_levels"] = _top(decision.get("asks"))
    out["bid_levels"] = _top(decision.get("bids"))
    for key in ("p", "q", "z", "z_side", "ask", "edge", "move", "limit", "notional", "ttm_s", "sigma", "live", "twap", "strike", "expected"):
        if isinstance(out.get(key), float):
            out[key] = round(out[key], 6)
    return out


class LockBot:
    def __init__(self, cfg: dict, *, config_path: Path) -> None:
        self.cfg = cfg
        self.config_path = config_path
        self.config_mtime = config_path.stat().st_mtime if config_path.exists() else 0.0
        dry = bool(cfg.get("dry_run", True))
        self.state_path = ledger_path(ROOT, dry)
        self.state = load_ledger(self.state_path, "paper" if dry else "live")
        self.session = _session()
        self.markets: dict[str, LockMarket] = {}
        self.market_at: dict[str, float] = {}
        self.books: dict[str, dict] = {}
        self.gamma_px: dict[str, float] = {}
        self.feeds: dict[str, RtdsTwapFeed] = {}
        self.eval_log: dict[str, tuple[float, str]] = {}
        self.cash: Optional[float] = None
        self.cash_at = 0.0
        self.client = None
        self._logged_resolution: set[str] = set()
        self.book_feed = ClobBookFeed()
        self.book_log = BookSnapshotLogger(ROOT, self.book_feed.book)
        self._apply_book_log(time.time())
        self.binance: Optional[BinanceTradeFeed] = None
        self.wallets = WalletTape(on_fill=self._on_wallet_fill)
        self.wallet_fills: list[dict] = []
        self.attempts: list[dict] = []
        self._compared: set[str] = set()
        self._compared_order: deque[str] = deque()
        self._wallet_kick = 0.0
        self.paper: list[dict] = []
        self.clip_at: dict[tuple[str, str], float] = {}
        self.window_path = window_file(ROOT)
        self.windows = load_windows(self.window_path, time.time())
        self.latched: dict[str, float] = {
            slug: float(row["strike"]) for slug, row in self.windows.items() if row.get("strike") is not None
        }
        self._latched_logged: set[str] = set(self.latched)
        self._recover_strikes_from_positions()
        self._warmed: set[str] = set()
        self.slow_at = 0.0
        self._status_at = 0.0
        self._lock = threading.RLock()
        self._wake = threading.Event()
        self.inflight: dict[str, dict] = dict(self.state.get("uncertain_orders", {}))
        self._results = LivePoster(self._finish_job)
        self._results.start()
        self._poster = LivePoster(self._post_job)
        self._poster.start()
        self._sigma_cache: dict[str, tuple] = {}
        self._risk_at = 0.0
        self._risk_marks: dict[str, float] = {}
        self._s1_at = 0.0
        self._s2_recv: Optional[float] = None
        self._redeem_started = False
        self._live_failed = False
        self._skip_log_at = 0.0

    def _on_trade(self, obs: float, recv: float, price: float) -> None:
        """Binance callback. It only wakes the decision thread."""
        del obs, recv, price
        self._wake.set()

    def reload(self) -> None:
        try:
            mtime = self.config_path.stat().st_mtime
        except OSError:
            return
        if mtime == self.config_mtime:
            return
        prev_dry = bool(self.cfg.get("dry_run", True))
        try:
            self.cfg = read_config(self.config_path)
            self.config_mtime = mtime
        except Exception as exc:
            log_event("config_reload_fail", error=str(exc)[:200])
            return
        now_dry = bool(self.cfg.get("dry_run", True))
        log_event("config_reloaded", path=str(self.config_path), dry_run=now_dry)
        self._apply_book_log(time.time())
        if prev_dry != now_dry:
            self._bind_ledger(now_dry)
            if now_dry:
                log_event("dry_run_on", detail="live posts stopped; paper ledger is active")
            else:
                self._activate_live()
        elif not now_dry and self.client is None:
            self._activate_live()

    def _bind_ledger(self, dry_run: bool) -> None:
        kind = "paper" if dry_run else "live"
        self.state_path = ledger_path(ROOT, dry_run)
        self.state = load_ledger(self.state_path, kind)
        if not dry_run:
            self.paper.clear()
        log_event("ledger", ledger=kind, path=str(self.state_path), positions=len(self.state.get("positions") or {}))

    def _activate_live(self) -> bool:
        """Build the order client the first time dry_run is false.

        Env is loaded at process start. If this fails, orders are skipped
        and the process has to be restarted. Flipping the file back to
        dry_run does not delete the live ledger.
        """
        self._poster.start()
        if self.client is None:
            client, err = open_live_client()
            if client is None:
                self._live_failed = True
                log_event("live_switch_fail", error=err, action="restart_required")
                return False
            self.client = client
            self._live_failed = False
            log_event("live_client_ready")
        self._refresh_cash(time.time(), force=True)
        self._start_redeem()
        return True

    def _start_redeem(self) -> None:
        if self._redeem_started or not self.cfg.get("redeem_enabled", True):
            return
        self._redeem_started = True
        threading.Thread(target=_redeem_loop, args=(self,), name="lockbot-redeem", daemon=True).start()

    def ensure_feeds(self) -> None:
        wanted = symbols_for(self.cfg)
        for symbol in wanted:
            if symbol in self.feeds:
                continue
            feed = RtdsTwapFeed(symbols=(symbol,), history_s=float(self.cfg.get("history_s") or 1200))
            feed.start()
            self.feeds[symbol] = feed
            log_event("oracle_subscribe", symbol=symbol, topics=["crypto_prices_twap_sixty", "crypto_prices_chainlink"])
        for symbol in list(self.feeds):
            if symbol not in wanted:
                self.feeds.pop(symbol).stop()
        self.book_feed.start()
        if self.cfg.get("strategy2_enabled", True):
            if self.binance is None:
                self.binance = BinanceTradeFeed(
                    history_s=float(self.cfg.get("history_s") or 1200),
                    on_trade=self._on_trade,
                )
                self.binance.start()
                log_event("binance_subscribe", urls=list(self.binance.urls))
        elif self.binance is not None:
            self.binance.stop()
        if self.cfg.get("h2h_enabled", False):
            if not self.wallets.running():
                self.wallets.start()
                log_event(
                    "wallet_subscribe",
                    topic="activity",
                    feed="trades",
                    filter="client_bytes",
                    wallets=sorted(WALLETS.values()),
                )
        elif self.wallets.running():
            self.wallets.stop()
            log_event("wallet_stopped", reason="h2h_disabled")

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

    def _interesting(self, now: float) -> list[LockMarket]:
        """Current and next windows. Token ids are subscribed as soon as Gamma resolves them."""
        out = []
        for market in self.markets.values():
            if market.key not in enabled_keys(self.cfg):
                continue
            if market.end_ts + 30 >= now:
                out.append(market)
        return out

    def _book(self, token: str) -> dict:
        row = self.book_feed.book(token)
        if row is not None:
            return row
        return self.books.get(token) or {}

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

    def warm_clients(self, now: float) -> None:
        if self.cfg.get("dry_run", True) or self.client is None:
            return
        for market in self._interesting(now):
            if market.condition_id in self._warmed:
                continue
            try:
                warm_market(self.client, market.condition_id)
                self._warmed.add(market.condition_id)
                log_event("market_warmed", slug=market.slug, condition_id=market.condition_id)
            except Exception as exc:
                log_event("market_warm_fail", slug=market.slug, error=str(exc)[:160])

    def _recover_strikes_from_positions(self) -> None:
        """A fill that already stored a strike can rebuild the window file."""
        changed = False
        for pos in (self.state.get("positions") or {}).values():
            if not isinstance(pos, dict):
                continue
            slug = str(pos.get("slug") or "")
            if not slug or slug in self.latched:
                continue
            strike = strike_for_position(pos, {})
            if strike is None:
                continue
            parsed = parse_slug(slug)
            start = parsed[2] if parsed else None
            duration = parsed[1] if parsed else str(pos.get("duration") or "")
            end = None
            if start is not None and duration in DURATION_S:
                end = float(start) + float(DURATION_S[duration])
            self.latched[slug] = strike
            self.windows[slug] = {
                "strike": strike,
                "start_ts": start,
                "end_ts": end,
                "asset": parsed[0] if parsed else pos.get("asset"),
                "duration": duration,
                "key": pos.get("lane"),
                "latched_at": time.time(),
                "source": "position",
            }
            self._latched_logged.add(slug)
            changed = True
        if not changed:
            return
        try:
            self.windows = save_windows(self.window_path, self.windows, time.time())
        except OSError:
            return

    def _stamp_open_strikes(self, slug: str, strike: float) -> None:
        """Write the latch onto open positions that were filled before it."""
        for pos in (self.state.get("positions") or {}).values():
            if not isinstance(pos, dict) or str(pos.get("slug") or "") != slug:
                continue
            if pos.get("settled_ts") or pos.get("strike") is not None:
                continue
            pos["strike"] = float(strike)

    def latch_strikes(self, now: float) -> None:
        """Remember the TWAP print at each window open, including a 5m open.

        The latch survives history rolloff. A sample within 1.25s of the
        open counts, which covers a print stamped on the neighbouring second.
        """
        tol = max(float(self.cfg.get("strike_tol_s") or 0.75), 1.25)
        from buy.lock_markets import boundary_price

        for market in self._interesting(now):
            official = self.gamma_px.get(market.slug, market.price_to_beat)
            if official is not None and now >= market.start_ts and self.latched.get(market.slug) != official:
                previous = self.latched.get(market.slug)
                if previous is not None and abs(previous - official) > 0.50:
                    log_event("strike_mismatch", slug=market.slug, latched=previous, official=official, delta=official - previous)
                self.latched[market.slug] = float(official)
                self.windows[market.slug] = {"strike": float(official), "start_ts": market.start_ts, "end_ts": market.end_ts, "latched_at": now, "source": "gamma"}
                for pos in self._positions_for(market.slug):
                    if not pos.get("settled_ts"):
                        pos["strike"] = float(official)
                save_windows(self.window_path, self.windows, now)
            if market.slug in self.latched or now + 1.0 < market.start_ts:
                continue
            feed = self.feeds.get(market.symbol)
            if feed is None:
                continue
            hist = feed.twap_history()
            price = boundary_price(hist, market.start_ts, tol_s=tol)
            if price is None:
                continue
            with self._lock:
                self.latched[market.slug] = float(price)
                self.windows[market.slug] = {
                    "strike": float(price),
                    "start_ts": market.start_ts,
                    "end_ts": market.end_ts,
                    "asset": market.asset,
                    "duration": market.duration,
                    "key": market.key,
                    "latched_at": now,
                    "source": "rtds",
                }
                self._stamp_open_strikes(market.slug, float(price))
            try:
                self.windows = save_windows(self.window_path, self.windows, now)
            except OSError as exc:
                log_event("window_save_fail", slug=market.slug, error=str(exc)[:160])
            if market.slug not in self._latched_logged:
                self._latched_logged.add(market.slug)
                log_event(
                    "strike_latched",
                    slug=market.slug,
                    asset=market.asset,
                    duration=market.duration,
                    strike=price,
                    start_ts=market.start_ts,
                )

    def _marks_cached(self, now: float) -> dict[str, float]:
        """Bid marks for the daily stop. Once a second, not once per aggTrade."""
        if self._risk_at > 0 and now - self._risk_at < 1.0:
            return self._risk_marks
        self._risk_marks = self._marks()
        self._risk_at = now
        return self._risk_marks

    def _marks(self) -> dict[str, float]:
        marks = {}
        for pos in self.state["positions"].values():
            if not isinstance(pos, dict) or pos.get("settled_ts"):
                continue
            token = str(pos.get("token_id") or "")
            book = self._book(token)
            bids = book.get("bids") or []
            if bids:
                marks[token] = float(bids[0][0])
                pos["last_mark"] = marks[token]
        return marks

    def _positions_for(self, slug: str) -> list[dict]:
        out = []
        for key, pos in self.state["positions"].items():
            if not isinstance(pos, dict):
                continue
            if pos.get("slug") == slug or key == slug:
                out.append(pos)
        return out

    def _strategy_spent(self, slug: str, strategy: str) -> float:
        total = 0.0
        for pos in self._positions_for(slug):
            if str(pos.get("strategy") or "s1") != strategy:
                continue
            total += float(pos.get("cost") or 0.0)
        for item in self.paper + list(self.inflight.values()):
            if item.get("slug") == slug and item.get("strategy") == strategy:
                total += float(item.get("notional") or 0.0)
        return total

    def _pending_usd(self) -> float:
        return sum(float(item.get("notional") or 0.0) for item in self.paper + list(self.inflight.values()))

    def _locked_side(self, slug: str) -> Optional[str]:
        lock = self.state.get("s1_locks", {}).get(slug)
        if lock:
            return lock.get("side")
        positions = [p for p in self._positions_for(slug) if p.get("strategy") == "s1" and float(p.get("shares") or 0) > 0]
        if positions:
            return str(max(positions, key=lambda p: p.get("last_fill_ts", p.get("opened_ts", 0))).get("side"))
        return None

    def _account(self, market: LockMarket, now: float) -> dict:
        with self._lock:
            return self._account_locked(market, now)

    def _account_locked(self, market: LockMarket, now: float) -> dict:
        spent_s1 = self._strategy_spent(market.slug, "s1")
        spent_s2 = self._strategy_spent(market.slug, "s2")
        exposure = open_exposure_usd(self.state["positions"].values()) + self._pending_usd()
        pnl = day_pnl(self.state["positions"].values(), now, self._marks_cached(now))
        today = dublin_day(now)
        stop = float(self.cfg.get("daily_loss_stop_usd") or 0)
        if pnl <= -abs(stop):
            self.state["loss_stop_day"] = today
        unknown = False
        if self.cfg.get("dry_run", True):
            cash = float(self.cfg.get("dry_run_cash_usd") or 0) - exposure
        elif self.cash is None:
            cash = 1e12
            unknown = True
        else:
            cash = float(self.cash) - self._pending_usd()
        return {
            "spent": spent_s1 + spent_s2,
            "spent_s1": spent_s1,
            "spent_s2": spent_s2,
            "spent_total": spent_s1 + spent_s2,
            "open_cost": exposure,
            "cash": float(cash),
            "locked_side": self._locked_side(market.slug),
            "s1_switches": self.state.get("s1_locks", {}).get(market.slug, {}).get("switches", max(0, len([p for p in self._positions_for(market.slug) if p.get("strategy") == "s1" and p.get("shares", 0) > 0]) - 1)),
            "last_s1_ts": self.clip_at.get((market.slug, "s1")),
            "last_s2_ts": self.clip_at.get((market.slug, "s2")),
            "loss_stopped": loss_stop_active(pnl, stop, self.state.get("loss_stop_day"), today),
            "day_pnl": pnl,
            "cash_unknown": unknown,
        }

    def _should_log(self, slug: str, reason: str, now: float, force: bool) -> bool:
        if force:
            return True
        interval = float(self.cfg.get("eval_log_s") or 30)
        prev = self.eval_log.get(slug)
        if prev is None or prev[1] != reason or now - prev[0] >= interval:
            return True
        return False

    def tick(self, now: Optional[float] = None) -> int:
        """Decision only. Gamma and strike HTTP run on ``lockbot-slow``."""
        now = time.time() if now is None else float(now)
        self._flush_paper(now)
        return self._fast(now)

    def _slow(self, now: float) -> None:
        self.reload()
        self.ensure_feeds()
        self.refresh_markets(now)
        self.latch_strikes(now)
        self.subscribe_books(now)
        self.warm_clients(now)
        if not self.cfg.get("dry_run", True):
            self._refresh_cash(now)
        for market in list(self.markets.values()):
            if market.key not in enabled_keys(self.cfg):
                continue
            if market.end_ts - now <= 0:
                self._settle(market, now)
        self._prune_markets(now)
        if self.cfg.get("h2h_enabled", False):
            self._kick_wallet_tape(now)
            self._emit_compares(now)
        self._status(now)

    def _prune_markets(self, now: float) -> None:
        """Drop windows that ended and have nothing left to settle."""
        with self._lock:
            drop = []
            for slug, market in self.markets.items():
                if market.end_ts + 120.0 >= now:
                    continue
                pending = False
                for pos in self._positions_for(slug):
                    if pos.get("settled_ts"):
                        continue
                    if float(pos.get("shares") or 0) > 0:
                        pending = True
                        break
                if not pending:
                    drop.append(slug)
            for slug in drop:
                self.markets.pop(slug, None)
                self.market_at.pop(slug, None)

    def _status(self, now: float) -> None:
        spot = time.time()
        if spot - self._status_at < 30:
            return
        self._status_at = spot
        latest = self.binance.latest() if self.binance is not None else None
        age = None if latest is None else spot - float(latest[1])
        book_stats = self.book_feed.stats()
        log_event(
            "feed_status",
            book_age_s=self.book_feed.age_s(spot),
            book_error=self.book_feed.last_error()[:160],
            book_reconnects=book_stats.get("reconnects"),
            book_tokens=book_stats.get("tokens"),
            book_parser=book_stats.get("parser"),
            book_log=self.book_log.stats() if self.book_log.enabled else None,
            binance_age_s=age,
            binance_px=None if latest is None else latest[2],
            binance_error=(self.binance.last_error()[:160] if self.binance is not None else ""),
            h2h_enabled=bool(self.cfg.get("h2h_enabled", False)),
            wallet_age_s=self.wallets.age_s(spot) if self.wallets.running() else None,
            wallet_fills=self.wallets.fills,
            wallet_error=self.wallets.last_error()[:160] if self.wallets.running() else "",
            markets=len(self.markets),
            paper=len(self.paper),
        )

    def _fast(self, now: float) -> int:
        """Strategy 2 runs when a new Binance trade is in. Strategy 1 runs once a second."""
        buys = 0
        with self._lock:
            markets = list(self.markets.values())
        s1_max = float(self.cfg.get("s1_tau_max") or 58)
        s1_min = float(self.cfg.get("s1_tau_min") or 1)
        s2_max = float(self.cfg.get("s2_tau_max") or 300)
        s2_min = float(self.cfg.get("s2_tau_min") or 5)
        s2_keys = {str(item) for item in (self.cfg.get("strategy2_markets") or ["btc_5m"])}
        enabled = set(enabled_keys(self.cfg))
        latest_recv = None
        if self.binance is not None:
            latest = self.binance.latest()
            if latest is not None:
                latest_recv = float(latest[1])
        new_trade = latest_recv is not None and latest_recv != self._s2_recv
        s1_due = now - self._s1_at >= 1.0
        if new_trade:
            self._s2_recv = latest_recv
        if s1_due:
            self._s1_at = now
        if not new_trade and not s1_due:
            return 0
        for market in markets:
            if market.key not in enabled:
                continue
            ttm = market.end_ts - now
            if ttm <= 0:
                continue
            want_s1 = s1_due and bool(self.cfg.get("strategy1_enabled", True)) and s1_min <= ttm <= s1_max
            want_s2 = new_trade and bool(self.cfg.get("strategy2_enabled", True)) and market.key in s2_keys and s2_min <= ttm <= s2_max
            if not want_s1 and not want_s2:
                continue
            # The q filter is off by default. Strategy 2 then only needs the
            # book and the Binance move, not a full Chainlink resample.
            q_on = self.cfg.get("s2_q_edge_min") not in (None, "")
            if want_s1 or (want_s2 and q_on):
                view = self._view(market, now)
            else:
                view = self._s2_view(market, now)
            if want_s1:
                buys += self._consider(market, view, self._account(market, now), now, "s1")
            if want_s2:
                buys += self._consider(market, view, self._account(market, now), now, "s2")
        return buys

    def _consider(self, market: LockMarket, view: dict, account: dict, now: float, strategy: str) -> int:
        started = time.perf_counter()
        if strategy == "s1":
            decision = evaluate_strategy1(view, account, self.cfg)
        else:
            decision = evaluate_strategy2(view, account, self.cfg)
        decision["eval_ms"] = (time.perf_counter() - started) * 1000.0
        if account.get("cash_unknown") and decision.get("action") == "buy":
            decision["action"] = "skip"
            decision["reason"] = "cash_unknown"
        force = decision.get("action") == "buy"
        log_key = f"{market.slug}|{strategy}"
        if force:
            self._act(market, decision, now)
        if self._should_log(log_key, str(decision.get("reason")), now, force):
            self.eval_log[log_key] = (now, str(decision.get("reason")))
            log_event("eval", **_public_decision(decision))
        if decision.get("action") != "buy":
            return 0
        return 1

    def _s2_view(self, market: LockMarket, now: float) -> dict:
        view = s2_quote_view(
            market,
            now=now,
            up_book=self._book(market.up_token),
            dn_book=self._book(market.dn_token),
            strike=self.latched.get(market.slug),
        )
        return self._attach_binance(view, market, now)

    def _view(self, market: LockMarket, now: float) -> dict:
        feed = self.feeds.get(market.symbol)
        live_hist = list(feed.live_history()) if feed is not None else []
        twap_hist = list(feed.twap_history()) if feed is not None else []
        latched = self.latched.get(market.slug)
        if latched is not None:
            twap_hist.append((market.start_ts, now, float(latched)))
        view = build_view(
            market,
            now=now,
            live_hist=live_hist,
            twap_hist=twap_hist,
            up_book=self._book(market.up_token),
            dn_book=self._book(market.dn_token),
            gamma_strike=self.gamma_px.get(market.slug, market.price_to_beat),
            cfg=self.cfg,
            market_enabled=True,
        )
        return self._attach_binance(view, market, now)

    def _attach_binance(self, view: dict, market: LockMarket, now: float) -> dict:
        feed = self.binance
        if feed is not None:
            latest = feed.latest()
            if latest is not None:
                obs, recv, px = latest
                view["binance_recv_ts"] = recv
                view["binance_px"] = px
                then = feed.price_at(float(obs) - float(self.cfg.get("s2_move_s") or 3))
                sigma, nrets, source = self._sigma_cached(feed, market)
                view["sigma1s"] = sigma
                view["sigma1s_n"] = nrets
                view["sigma_source"] = source
                if then is not None and sigma is not None:
                    view["binance_move"] = signed_move(px, then, sigma)
        expected = view.get("expected")
        strike = view.get("strike")
        sigma = view.get("sigma")
        if expected is not None and strike is not None and sigma is not None:
            try:
                scored = side_z(
                    strike=float(strike),
                    expected=float(expected),
                    sigma=float(sigma),
                    tau_s=max(market.end_ts - now, 0.0),
                    noise_frac=float(self.cfg.get("noise_frac") or 0.00002),
                )
                view["q_up"] = scored["q_up"]
            except (TypeError, ValueError):
                pass
        return view

    def _sigma_cached(self, feed: BinanceTradeFeed, market: LockMarket) -> tuple:
        """Sigma is stable once the pre-window sample is in. Do not recompute it per trade."""
        cached = self._sigma_cache.get(market.slug)
        now = time.time()
        if cached is not None:
            sigma, nrets, source, at = cached
            if source == "pre_window" or now - float(at) < 5.0:
                return sigma, nrets, source
        sigma, nrets, source = feed.sigma_before(
            market.start_ts,
            float(self.cfg.get("s2_sigma_window_s") or 300),
            min_n=int(self.cfg.get("s2_sigma_min_samples") or 60),
        )
        self._sigma_cache[market.slug] = (sigma, nrets, source, now)
        if len(self._sigma_cache) > 32:
            oldest = sorted(self._sigma_cache, key=lambda slug: self._sigma_cache[slug][3])[:8]
            for slug in oldest:
                self._sigma_cache.pop(slug, None)
        return sigma, nrets, source

    def _act(self, market: LockMarket, decision: dict, now: float) -> None:
        strategy = str(decision.get("strategy") or "s1")
        decision_ts = time.time()
        decision["decision_ts"] = decision_ts
        self.clip_at[(market.slug, strategy)] = decision_ts
        if self.cfg.get("dry_run", True):
            enqueue_paper(self.paper, decision, now=decision_ts)
        elif self.client is None:
            self._skip_live(market, strategy)
        else:
            reservation = f"{market.slug}|{strategy}|{time.monotonic_ns()}"
            decision["reservation"] = reservation
            with self._lock:
                self.inflight[reservation] = dict(decision)
            self._poster.submit((market, dict(decision), decision_ts))
        handoff = time.time()
        decision["handoff_ts"] = handoff
        decision["decision_to_handoff_ms"] = (handoff - decision_ts) * 1000.0
        recv = decision.get("binance_recv_ts")
        if recv is not None:
            try:
                decision["recv_to_decision_ms"] = (decision_ts - float(recv)) * 1000.0
                decision["recv_to_handoff_ms"] = (handoff - float(recv)) * 1000.0
            except (TypeError, ValueError):
                pass
        log_event("signal", **_public_decision(decision))
        self._remember_attempt(decision)

    def _skip_live(self, market: LockMarket, strategy: str) -> None:
        now = time.time()
        if now - self._skip_log_at < 30.0:
            return
        self._skip_log_at = now
        reason = "live_switch_fail" if self._live_failed else "no_clob_client"
        log_event(
            "order_skip",
            slug=market.slug,
            strategy=strategy,
            reason=reason,
            action="restart_required",
        )

    def _post_job(self, job: tuple) -> None:
        """Order thread. ``post_ts`` is stamped inside ``post_fak_buy`` before the sign."""
        market, decision, decision_ts = job
        reservation = decision["reservation"]
        with self._lock:
            self.inflight.pop(reservation, None)
            account = self._account_locked(market, time.time())
            from buy.lock_gates import _clip_room
            room, reason = _clip_room(account, self.cfg, strategy=decision.get("strategy", "s1"), market_key=market.key)
            if market.condition_id not in self._warmed or not self.cfg.get("enabled", True) or account["loss_stopped"] or account["cash_unknown"] or time.time() >= market.end_ts or reason or room + 1e-9 < decision["notional"]:
                self._results.submit((job, None, "cap_recheck"))
                return
            self.inflight[reservation] = dict(decision)
        try:
            raw = dispatch_buy(decision, dry_run=False, client=self.client)
        except Exception as exc:
            self._results.submit((job, None, str(exc)[:200]))
            return
        self._results.submit((job, raw, ""))

    def _finish_job(self, result: tuple) -> None:
        """Bookkeeping and retry scheduling run independently of the hot poster."""
        job, raw, error = result
        market, decision, decision_ts = job
        reservation = decision["reservation"]
        from buy.lock_orders import fak_no_match, kill_failure
        miss = fak_no_match(error or raw)
        if error:
            fatal = kill_failure(error)
            if miss or fatal or error == "cap_recheck":
                with self._lock:
                    self.inflight.pop(reservation, None)
            else:
                # Transport errors have an unknown execution outcome; retain the reserve.
                with self._lock:
                    self.state.setdefault("uncertain_orders", {})[reservation] = dict(decision)
                    atomic_save(self.state_path, self.state)
            log_event("order_miss" if miss else ("order_fail" if fatal else "order_uncertain"), slug=market.slug, strategy=decision.get("strategy"), error=error, kill_failure=fatal)
            if miss:
                self._retry_miss(market, decision)
            return
        post_ts = raw.get("post_ts") or decision_ts
        ack_ts = time.time()
        raw["decision_ts"] = decision_ts
        raw["post_ts"] = raw.get("post_ts") or post_ts
        raw["ack_ts"] = raw.get("ack_ts") or ack_ts
        raw["binance_recv_ts"] = decision.get("binance_recv_ts")
        raw["book_recv_ts"] = decision.get("book_recv_ts")
        raw["decision_to_post_ms"] = (float(raw["post_ts"]) - decision_ts) * 1000.0
        if decision.get("binance_recv_ts") is not None:
            raw["recv_to_decision_ms"] = (decision_ts - float(decision["binance_recv_ts"])) * 1000.0
            raw["recv_to_post_ms"] = (float(raw["post_ts"]) - float(decision["binance_recv_ts"])) * 1000.0
        fill = normalize_fill(raw, decision, self.cfg)
        if kill_failure(raw) and fill["shares"] <= 0:
            with self._lock:
                self.inflight.pop(reservation, None)
            log_event("order_fail", slug=market.slug, strategy=decision.get("strategy"), kill_failure=True, error=str(fill.get("raw_status")))
            return
        with self._lock:
            if fill["shares"] > 0:
                self._add_fill_locked(market, decision, fill, time.time())
                self.inflight.pop(reservation, None)
                self.state.get("uncertain_orders", {}).pop(reservation, None)
            elif miss:
                self.inflight.pop(reservation, None)
            else:
                self.state.setdefault("uncertain_orders", {})[reservation] = dict(decision)
                atomic_save(self.state_path, self.state)
        if fill["shares"] > 0:
            atomic_save(self.state_path, self.state)
        self._log_attempt(market, decision, fill, event="order_miss" if miss else "entry")
        if miss:
            self._retry_miss(market, decision)

    def _retry_miss(self, market: LockMarket, decision: dict) -> None:
        attempt = int(decision.get("retry", 0))
        if decision.get("strategy") != "s1" or attempt >= int(self.cfg.get("s1_fak_retries", 2)):
            return
        self._poster.submit_at(time.monotonic() + 0.3, (market, attempt), handler=self._run_retry)

    def _run_retry(self, job: tuple) -> None:
        """Recheck the signal on the hot poster after the miss backoff."""
        market, attempt = job
        now = time.time()
        account = self._account(market, now)
        account["last_s1_ts"] = None
        fresh = evaluate_strategy1(self._view(market, now), account, self.cfg)
        if fresh.get("action") == "buy":
            fresh["retry"] = attempt + 1
            self._act(market, fresh, now)

    def _flush_paper(self, now: float) -> None:
        latency = float(self.cfg.get("dry_run_latency_s") or 0.2)
        due, keep = take_due(self.paper, now, latency)
        self.paper = keep
        for item in due:
            token = str(item.get("token_id") or "")
            book = self._book(token)
            fill = walk_late_book(item, book.get("asks") or [], now=now)
            decision = dict(item.get("decision") or {})
            decision.setdefault("token_id", token)
            decision.setdefault("strategy", item.get("strategy"))
            decision.setdefault("side", item.get("side"))
            decision.setdefault("slug", item.get("slug"))
            norm = normalize_fill(fill, decision, self.cfg)
            market = self.markets.get(str(item.get("slug") or ""))
            self._log_attempt(market, decision, norm, event="paper_fill")
            if market is not None and norm["shares"] > 0:
                self._add_fill(market, decision, norm, now)

    def _log_attempt(self, market: Optional[LockMarket], decision: dict, fill: dict, *, event: str) -> None:
        log_event(
            event,
            slug=decision.get("slug") or (None if market is None else market.slug),
            asset=None if market is None else market.asset,
            duration=None if market is None else market.duration,
            lane=None if market is None else market.lane,
            strategy=decision.get("strategy"),
            condition_id=None if market is None else market.condition_id,
            side=decision.get("side"),
            p=decision.get("p"),
            q=decision.get("q"),
            z_side=decision.get("z_side"),
            ask=decision.get("ask"),
            edge=decision.get("edge"),
            move=decision.get("move"),
            limit=decision.get("limit"),
            ttm_s=decision.get("ttm_s"),
            strike=decision.get("strike"),
            sigma=decision.get("sigma"),
            expected=decision.get("expected"),
            shares=fill.get("shares"),
            cost=fill.get("cost"),
            vwap=fill.get("vwap"),
            fee=fill.get("fee"),
            dry_run=fill.get("dry_run"),
            posted=fill.get("posted"),
            reason=fill.get("reason"),
            decision_ts=fill.get("decision_ts") or decision.get("decision_ts"),
            post_ts=fill.get("post_ts"),
            ack_ts=fill.get("ack_ts"),
            binance_recv_ts=fill.get("binance_recv_ts") or decision.get("binance_recv_ts"),
            book_recv_ts=fill.get("book_recv_ts") or decision.get("book_recv_ts"),
            decision_to_post_ms=fill.get("decision_to_post_ms"),
            post_to_ack_ms=fill.get("post_to_ack_ms"),
            recv_to_decision_ms=fill.get("recv_to_decision_ms"),
            recv_to_post_ms=fill.get("recv_to_post_ms"),
            decision_to_order_ms=fill.get("decision_to_order_ms"),
            order_build_ts=fill.get("order_build_ts"),
            sign_ts=fill.get("sign_ts"),
            signed_ts=fill.get("signed_ts"),
            http_send_ts=fill.get("http_send_ts"),
            response_ts=fill.get("response_ts"),
            confirm_ts=fill.get("confirm_ts"),
            eval_ms=decision.get("eval_ms"),
        )
        self._stamp_attempt(decision, fill)

    def _add_fill(self, market: LockMarket, decision: dict, fill: dict, now: float) -> None:
        with self._lock:
            self._add_fill_locked(market, decision, fill, now)
        atomic_save(self.state_path, self.state)

    def _add_fill_locked(self, market: LockMarket, decision: dict, fill: dict, now: float) -> None:
        strategy = str(decision.get("strategy") or "s1")
        side = str(decision.get("side") or "")
        if strategy == "s1":
            locks = self.state.setdefault("s1_locks", {})
            previous = self._locked_side(market.slug)
            switches = locks.get(market.slug, {}).get("switches", 0)
            locks[market.slug] = {"side": side, "switches": switches + int(bool(previous and previous != side))}
        key = f"{market.slug}|{strategy}|{side}"
        pos = dict(self.state["positions"].get(key) or {})
        shares = float(pos.get("shares") or 0) + float(fill["shares"])
        cost = float(pos.get("cost") or 0) + float(fill["cost"])
        fee = float(pos.get("fee") or 0) + float(fill["fee"])
        entries = int(pos.get("entries") or 0) + 1
        token = decision.get("token_id")
        pos.update(
            {
                "slug": market.slug,
                "asset": market.asset,
                "duration": market.duration,
                "lane": market.lane,
                "strategy": strategy,
                "condition_id": market.condition_id,
                "side": side or pos.get("side"),
                "token_id": token or pos.get("token_id"),
                "up_token": market.up_token,
                "dn_token": market.dn_token,
                "shares": shares,
                "cost": cost,
                "fee": fee,
                "entries": entries,
                "strike": decision.get("strike") or pos.get("strike"),
                "end_ts": market.end_ts,
                "start_ts": market.start_ts,
                "opened_ts": pos.get("opened_ts") or now,
                "last_fill_ts": now,
                "dry_run": bool(self.cfg.get("dry_run", True)),
            }
        )
        self.state["positions"][key] = pos
        if not pos["dry_run"]:
            self.state["intents"][market.condition_id] = {
                "status": "confirmed",
                "dry_run": False,
                "end_ts": market.end_ts,
                "up_token": market.up_token,
                "dn_token": market.dn_token,
                "slug": market.slug,
                "condition_id": market.condition_id,
            }
        self._risk_at = 0.0
        if fill["shares"] > 0:
            log_event(
                "fill",
                slug=market.slug,
                asset=market.asset,
                duration=market.duration,
                lane=market.lane,
                strategy=strategy,
                side=pos.get("side"),
                shares=fill["shares"],
                cost=fill["cost"],
                vwap=fill["vwap"],
                fee=fill["fee"],
                spent=cost,
                dry_run=pos["dry_run"],
                decision_to_post_ms=fill.get("decision_to_post_ms"),
                recv_to_post_ms=fill.get("recv_to_post_ms"),
                decision_ts=fill.get("decision_ts"),
                post_ts=fill.get("post_ts"),
                ack_ts=fill.get("ack_ts"),
                binance_recv_ts=fill.get("binance_recv_ts"),
            )
            self._notify_fill(pos, fill)

    def _notify_fill(self, pos: dict, fill: dict) -> None:
        if not self.cfg.get("notify_entry_whatsapp"):
            return
        try:
            from buy.whatsapp_notify import WhatsAppNotifier

            notifier = getattr(self, "_whatsapp", None)
            if notifier is None:
                notifier = WhatsAppNotifier(os.getenv("CALLMEBOT_PHONE"), os.getenv("CALLMEBOT_APIKEY"), log=log_event)
                self._whatsapp = notifier
            notifier.send(
                f"lockbot {pos.get('slug')} {pos.get('side')} {fill['shares']:.1f} @ {fill.get('vwap')}",
                kind="lock_entry",
            )
        except Exception as exc:
            log_event("notify_fail", error=str(exc)[:160])

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
            strike = strike_for_position(pos, self.latched)
            if strike is not None and pos.get("strike") is None:
                pos["strike"] = float(strike)
                changed = True
            if final is None or strike is None:
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
            result = settle_pnl(
                side=str(pos.get("side") or ""),
                shares=float(pos["shares"]),
                cost=float(pos["cost"]),
                fee=float(pos.get("fee") or 0),
                final_twap=float(final),
                strike=float(strike),
            )
            pos.update(result)
            pos["final_twap"] = final
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
            self._risk_at = 0.0
            atomic_save(self.state_path, self.state)

    def _refresh_cash(self, now: float, *, force: bool = False) -> None:
        """Live cash is the funder pUSD balance. Dry-run never calls this."""
        if not force and now - self.cash_at < 15:
            return
        self.cash_at = now
        try:
            from buy.chain import ChainReader

            funder = os.getenv("FUNDER_ADDRESS") or ""
            if not funder:
                self.cash = None
                log_event("cash_balance", pusd=None, asset="pUSD", reason="no_funder")
                return
            chain = ChainReader(str(self.cfg.get("rpc_url")))
            self.cash = chain.pUSD_balance(str(self.cfg.get("pUSD_address")), funder)
            log_event("cash_balance", pusd=None if self.cash is None else round(float(self.cash), 4), asset="pUSD")
        except Exception as exc:
            log_event("cash_fail", error=str(exc)[:160])

    def _on_wallet_fill(self, fill: dict) -> None:
        """Record one watched fill. Does not read or write an order."""
        self._stamp_binance_move(fill)
        from buy.lock_wallets import fill_key

        fill["fill_id"] = fill.get("fill_id") or fill_key(fill)
        with self._lock:
            self.wallet_fills.append(dict(fill))
            if len(self.wallet_fills) > 5000:
                del self.wallet_fills[: len(self.wallet_fills) - 5000]
        log_event("wallet_fill", **fill)

    def _stamp_binance_move(self, fill: dict) -> None:
        """3-second Binance move at their fill time. Log only."""
        parsed = parse_slug(str(fill.get("slug") or ""))
        s2_keys = {str(item) for item in (self.cfg.get("strategy2_markets") or ["btc_5m"])}
        s2 = False
        start = None
        if parsed is not None:
            asset, duration, start = parsed
            s2 = f"{asset}_{duration}" in s2_keys
        fill["s2_market"] = s2
        if not s2:
            return
        fill["move_min"] = float(self.cfg.get("s2_move_sigma") or 2.0)
        feed = self.binance
        their_ts = fill.get("their_ts")
        if feed is None or start is None or their_ts is None:
            fill["binance_move"] = None
            return
        lookback = float(self.cfg.get("s2_move_s") or 3.0)
        now_px = feed.price_at(float(their_ts))
        then_px = feed.price_at(float(their_ts) - lookback)
        sigma, _nrets, _source = feed.sigma_before(
            float(start),
            float(self.cfg.get("s2_sigma_window_s") or 300),
            min_n=int(self.cfg.get("s2_sigma_min_samples") or 60),
        )
        move = None
        if now_px is not None and then_px is not None and sigma is not None:
            move = signed_move(now_px, then_px, sigma)
        fill["binance_move"] = None if move is None else round(float(move), 4)

    def _remember_attempt(self, decision: dict) -> None:
        decision_ts = decision.get("decision_ts")
        if decision_ts is None:
            return
        row = {
            "slug": decision.get("slug"),
            "strategy": decision.get("strategy"),
            "side": str(decision.get("side") or "").lower(),
            "decision_ts": float(decision_ts),
            "ask": decision.get("ask"),
            "limit": decision.get("limit"),
            "post_ts": None,
            "ack_ts": None,
            "vwap": None,
            "shares": None,
        }
        with self._lock:
            self.attempts.append(row)
            if len(self.attempts) > 2000:
                del self.attempts[: len(self.attempts) - 2000]

    def _stamp_attempt(self, decision: dict, fill: dict) -> None:
        decision_ts = fill.get("decision_ts") or decision.get("decision_ts")
        if decision_ts is None:
            return
        slug = decision.get("slug")
        strategy = decision.get("strategy")
        with self._lock:
            for row in reversed(self.attempts):
                if row.get("slug") != slug or row.get("strategy") != strategy:
                    continue
                if abs(float(row.get("decision_ts") or 0.0) - float(decision_ts)) > 1e-3:
                    continue
                row["post_ts"] = fill.get("post_ts")
                row["ack_ts"] = fill.get("ack_ts")
                row["vwap"] = fill.get("vwap")
                row["shares"] = fill.get("shares")
                if row.get("ask") is None:
                    row["ask"] = decision.get("ask")
                if row.get("limit") is None:
                    row["limit"] = decision.get("limit")
                return

    def _kick_wallet_tape(self, now: float) -> None:
        """Redial the activity socket when the firehose goes quiet."""
        age = self.wallets.age_s(now)
        if age is None or age <= 15.0 or now - self._wallet_kick < 15.0:
            return
        if not self.wallets.running():
            return
        self._wallet_kick = now
        log_event("wallet_reconnect", age_s=round(age, 1))
        self.wallets.request_reconnect()

    def _emit_compares(self, now: float) -> None:
        """Append one wallet_compare row after the pairing window has closed.

        A signal inside the window can still arrive, so an unpaired row
        waits. The summary recomputes the join from the log. This tape
        never feeds the strategy gates.
        """
        window = float(self.cfg.get("h2h_window_s") or 10.0)
        with self._lock:
            pending = [fill for fill in self.wallet_fills if fill.get("s2_market") and fill.get("binance_move") is None]
        for fill in pending:
            self._stamp_binance_move(fill)
        with self._lock:
            attempts = [dict(row) for row in self.attempts]
            fills = [dict(row) for row in self.wallet_fills]
            done = set(self._compared)
        fresh = []
        for case in compare_fills(attempts, fills, window_s=window):
            key = str(case.get("key") or "")
            if not key or key in done:
                continue
            their_ts = case.get("their_ts")
            recv = case.get("their_recv_ts")
            if their_ts is None:
                continue
            anchor = float(their_ts)
            if recv is not None:
                anchor = max(anchor, float(recv))
            if float(now) + 1e-9 < anchor + window:
                continue
            fresh.append(case)
        for case in fresh:
            key = str(case["key"])
            self._compared.add(key)
            self._compared_order.append(key)
            while len(self._compared_order) > 10000:
                self._compared.discard(self._compared_order.popleft())
            payload = {field: value for field, value in case.items() if field != "key"}
            log_event("wallet_compare", **payload)

    def close(self) -> None:
        self._wake.set()
        self._poster.stop()
        self._results.stop()
        for feed in self.feeds.values():
            feed.stop()
        self.book_feed.stop()
        self.book_log.close()
        self.wallets.stop()
        if self.binance is not None:
            self.binance.stop()


def _wait_s(bot: LockBot, now: float) -> float:
    """How long the decision thread blocks. A Binance trade wakes it earlier.

    Strategy 1 is checked once a second while its window is open. Strategy 2
    does not poll. ``fast_poll_s`` is not used as a spin.
    """
    poll = float(bot.cfg.get("poll_s") or 1.0)
    s1_max = float(bot.cfg.get("s1_tau_max") or 58)
    s1_min = float(bot.cfg.get("s1_tau_min") or 1)
    enabled = set(enabled_keys(bot.cfg))
    s1_hot = False
    window_in = None
    for market in bot.markets.values():
        if market.key not in enabled:
            continue
        ttm = market.end_ts - now
        if bot.cfg.get("strategy1_enabled", True) and s1_min <= ttm <= s1_max:
            s1_hot = True
        until = ttm - s1_max
        if until > 0:
            window_in = until if window_in is None else min(window_in, until)
    s1_next = (bot._s1_at + 1.0) if s1_hot else None
    paper_next = None
    if bot.paper:
        latency = float(bot.cfg.get("dry_run_latency_s") or 0.2)
        paper_next = min(float(item.get("decision_ts") or now) for item in bot.paper) + latency
    return decision_wait_s(now, poll_s=poll, s1_next=s1_next, paper_next=paper_next, window_in=window_in)


def _slow_loop(bot: LockBot) -> None:
    """Gamma, strike latch, and settlement. Never on the decision tick."""
    while not _stop.is_set() and not STOP_FILE.exists():
        try:
            bot._slow(time.time())
        except Exception as exc:
            log_event("slow_fail", error=str(exc)[:200])
        _stop.wait(float(bot.cfg.get("poll_s") or 1))


def _redeem_loop(bot: LockBot) -> None:
    """Live redeem on its own thread. Dry-run settlements are logged in-tick."""
    if not bot.cfg.get("redeem_enabled", True):
        return
    from buy.chain import ChainReader
    from buy.contracts import build_redeem_calls
    from buy.market import MarketGateway
    from buy.mint_redeem import RedeemDesk, RedeemIO
    from buy.relay_batch import fetch_relayer_transaction, submit_proxy_batch

    funder = os.getenv("FUNDER_ADDRESS") or ""
    if not funder:
        log_event("redeem_skip", reason="no_funder")
        return
    chain = ChainReader(str(bot.cfg.get("rpc_url")))
    gateway = MarketGateway(
        gamma_url=str(bot.cfg.get("gamma_url")),
        data_api_url=str(bot.cfg.get("data_api_url")),
    )
    lock = threading.RLock()

    def submit(cid: str, approve: bool):
        calls = build_redeem_calls(
            pUSD_address=str(bot.cfg["pUSD_address"]),
            adapter_address=str(bot.cfg["standard_adapter_address"]),
            ctf_address=str(bot.cfg["ctf_address"]),
            condition_id=cid,
            approve_adapter=approve,
        )
        margin, fallback, cap = mint_gas_settings(bot.cfg)
        return submit_proxy_batch(
            calls,
            metadata=f"lockbot:redeem:{cid}:{int(time.time())}",
            rpc=chain._rpc,
            gas_margin=margin,
            gas_fallback=fallback,
            gas_cap=cap,
        )

    desk = RedeemDesk(
        RedeemIO(
            payout_denominator=lambda cid: chain.payout_denominator(str(bot.cfg["ctf_address"]), cid),
            payout_numerator=lambda cid, index: chain.payout_numerator(str(bot.cfg["ctf_address"]), cid, index),
            balance=lambda token: chain.position_balance(str(bot.cfg["ctf_address"]), funder, token),
            is_approved=lambda: chain.is_approved_for_all(
                str(bot.cfg["ctf_address"]), funder, str(bot.cfg["standard_adapter_address"])
            ),
            submit=submit,
            relayer_status=lambda tx: fetch_relayer_transaction(str(bot.cfg.get("relayer_url")), tx),
            log=log_event,
            positions=lambda: gateway.redeemable_positions(funder),
        ),
        lock=lock,
        save=lambda state: atomic_save(bot.state_path, state),
    )
    while not _stop.is_set() and not STOP_FILE.exists():
        if bot.cfg.get("dry_run", True):
            _stop.wait(float(bot.cfg.get("redeem_poll_s") or 15))
            continue
        try:
            desk.tick(bot.state, bot.cfg, time.time())
        except Exception as exc:
            log_event("redeem_tick_fail", error=str(exc)[:200])
        _stop.wait(float(bot.cfg.get("redeem_poll_s") or 15))


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
    parser = argparse.ArgumentParser(description="TWAP-lock taker (dry-run by default)")
    parser.add_argument("--config", default="", help="knob file; default lockbot.json or the example")
    parser.add_argument(
        "--reset-live",
        action="store_true",
        help="zero positions_lockbot_live.json and exit; does not touch the paper ledger",
    )
    args = parser.parse_args(argv)
    if args.reset_live:
        hold = acquire_lock()
        path = reset_live_ledger(ROOT)
        print(json.dumps({"event": "live_ledger_reset", "path": str(path)}, separators=(",", ":")), flush=True)
        del hold
        return 0
    # Default 5ms lets a busy websocket thread hold the GIL across a
    # strategy-2 wake. 1ms is the longest the decision thread should wait
    # behind one slice of feed parsing.
    sys.setswitchinterval(0.001)
    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)
    try:
        from dotenv import load_dotenv

        load_dotenv(ROOT / ".env")
    except Exception:
        pass
    path = resolve_config_path(args.config or None)
    try:
        cfg = read_config(path)
    except Exception as exc:
        print(f"config failed: {exc}", flush=True)
        return 1
    hold = acquire_lock()
    del hold
    bot = LockBot(cfg, config_path=path)
    if not cfg.get("dry_run", True):
        bot._activate_live()
    log_event(
        "startup",
        dry_run=bool(cfg.get("dry_run", True)),
        enabled=bool(cfg.get("enabled", True)),
        strategy1_enabled=bool(cfg.get("strategy1_enabled", True)),
        strategy2_enabled=bool(cfg.get("strategy2_enabled", True)),
        markets=enabled_keys(cfg),
        symbols=symbols_for(cfg),
        per_market_usd=cfg.get("combined_per_market_usd"),
        strategy1_market_usd=cfg.get("strategy1_market_usd"),
        strategy2_market_usd=cfg.get("strategy2_market_usd"),
        h2h_window_s=cfg.get("h2h_window_s"),
        h2h_enabled=bool(cfg.get("h2h_enabled", False)),
        switchinterval_s=sys.getswitchinterval(),
        wsaccel=wsaccel_available(),
        windows=len(bot.windows),
        clip_usd=cfg.get("clip_usd"),
        max_open_exposure_usd=cfg.get("max_open_exposure_usd"),
        ledger=str(bot.state_path),
        config=str(path),
    )
    try:
        bot._slow(time.time())
        threading.Thread(target=_slow_loop, args=(bot,), name="lockbot-slow", daemon=True).start()
        while not _stop.is_set() and not STOP_FILE.exists():
            delay = _wait_s(bot, time.time())
            bot._wake.wait(delay)
            bot._wake.clear()
            if _stop.is_set() or STOP_FILE.exists():
                break
            try:
                bot.tick(time.time())
            except Exception as exc:
                log_event("tick_fail", error=str(exc)[:200])
    finally:
        bot.close()
        log_event("shutdown")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
