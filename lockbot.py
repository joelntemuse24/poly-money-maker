#!/usr/bin/env python3
"""TWAP-lock taker for Polymarket crypto up/down windows.

Separate from mintbot. It does not import mintbot, does not take the mint
lock, and does not read ``strategy_mint.json``. Default config is
``lockbot.example.json`` (dry_run true). A gitignored ``lockbot.json``
wins when it exists. Entries are hold-to-settlement; v1 never sells.

The decision tick reads the in-memory Chainlink path and the cached book.
Gamma, the CLOB book, and the order post are the only network calls, and
the order post happens after the decision.
"""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import signal
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Optional

import requests

from buy.lock_config import apply_defaults, validate_config
from buy.lock_engine import build_view
from buy.lock_gates import (
    day_pnl,
    dublin_day,
    evaluate_entry,
    loss_stop_active,
    open_exposure_usd,
    parse_levels,
    settle_pnl,
)
from buy.lock_markets import (
    DURATION_S,
    SPECS,
    LockMarket,
    enabled_keys,
    event_slug,
    parse_lock_event,
    symbols_for,
    window_starts,
)
from buy.lock_orders import build_clob_client, dispatch_buy, normalize_fill
from buy.mint_gas import mint_gas_settings
from buy.oracle_log import RtdsTwapFeed, append_jsonl, fetch_gamma_strike


ROOT = Path(__file__).resolve().parent
EXAMPLE_FILE = ROOT / "lockbot.example.json"
CONFIG_FILE = ROOT / "lockbot.json"
LOG_FILE = ROOT / "logs" / "lockbot.jsonl"
STATE_FILE = ROOT / "positions_lockbot.json"
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


def load_state() -> dict:
    if not STATE_FILE.exists():
        return {"positions": {}, "intents": {}, "redeems": {}}
    try:
        payload = json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"positions": {}, "intents": {}, "redeems": {}}
    if not isinstance(payload, dict):
        return {"positions": {}, "intents": {}, "redeems": {}}
    payload.setdefault("positions", {})
    payload.setdefault("intents", {})
    payload.setdefault("redeems", {})
    return payload


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
    for key in ("p", "ask", "edge", "limit", "notional", "ttm_s", "sigma", "live", "twap", "strike", "expected"):
        if isinstance(out.get(key), float):
            out[key] = round(out[key], 6)
    return out


class LockBot:
    def __init__(self, cfg: dict, *, config_path: Path) -> None:
        self.cfg = cfg
        self.config_path = config_path
        self.config_mtime = config_path.stat().st_mtime if config_path.exists() else 0.0
        self.state = load_state()
        self.session = _session()
        self.markets: dict[str, LockMarket] = {}
        self.market_at: dict[str, float] = {}
        self.books: dict[str, dict] = {}
        self.gamma_px: dict[str, float] = {}
        self.gamma_at: dict[str, float] = {}
        self.feeds: dict[str, RtdsTwapFeed] = {}
        self.eval_log: dict[str, tuple[float, str]] = {}
        self.cash: Optional[float] = None
        self.cash_at = 0.0
        self.client = None
        self._logged_resolution: set[str] = set()
        self._pool = ThreadPoolExecutor(max_workers=8, thread_name_prefix="lockbot-io")

    def reload(self) -> None:
        try:
            mtime = self.config_path.stat().st_mtime
        except OSError:
            return
        if mtime == self.config_mtime:
            return
        try:
            self.cfg = read_config(self.config_path)
            self.config_mtime = mtime
            log_event("config_reloaded", path=str(self.config_path), dry_run=bool(self.cfg.get("dry_run")))
        except Exception as exc:
            log_event("config_reload_fail", error=str(exc)[:200])

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

    def refresh_markets(self, now: float) -> None:
        gamma = str(self.cfg.get("gamma_url"))
        refresh = float(self.cfg.get("market_refresh_s") or 20)
        for key in enabled_keys(self.cfg):
            spec = SPECS[key]
            dur = DURATION_S[spec["duration"]]
            for start in window_starts(now, dur, ahead=1):
                slug = event_slug(spec["asset"], spec["duration"], start)
                if slug in self.markets and now - self.market_at.get(slug, 0) < refresh:
                    continue
                try:
                    market = fetch_market(self.session, gamma, slug)
                except Exception as exc:
                    log_event("market_fetch_fail", slug=slug, error=str(exc)[:160])
                    continue
                if market is None:
                    continue
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

    def refresh_gamma_strikes(self, now: float) -> None:
        interval = float(self.cfg.get("gamma_refresh_s") or 20)
        for market in self._interesting(now):
            if now - self.gamma_at.get(market.slug, 0) < interval:
                continue
            self.gamma_at[market.slug] = now
            try:
                strike = fetch_gamma_strike(market.slug)
            except Exception as exc:
                log_event("gamma_strike_fail", slug=market.slug, error=str(exc)[:160])
                continue
            if strike.price_to_beat:
                try:
                    self.gamma_px[market.slug] = float(strike.price_to_beat)
                except (TypeError, ValueError):
                    continue

    def _interesting(self, now: float) -> list[LockMarket]:
        warm = float(self.cfg.get("entry_window_s") or 60) + float(self.cfg.get("book_warm_s") or 15)
        out = []
        for market in self.markets.values():
            if market.key not in enabled_keys(self.cfg):
                continue
            ttm = market.end_ts - now
            if -30 <= ttm <= warm or 0 <= (market.start_ts - now) <= 30:
                out.append(market)
        return out

    def refresh_books(self, now: float) -> None:
        clob = str(self.cfg.get("clob_url"))
        jobs = []
        for market in self._interesting(now):
            ttm = market.end_ts - now
            warm = float(self.cfg.get("entry_window_s") or 60) + float(self.cfg.get("book_warm_s") or 15)
            if not (0 < ttm <= warm):
                continue
            for token in (market.up_token, market.dn_token):
                jobs.append(token)
        if not jobs:
            return

        def one(token: str) -> tuple[str, Optional[dict]]:
            try:
                return token, fetch_book(self.session, clob, token)
            except Exception:
                return token, None

        for token, book in self._pool.map(one, jobs):
            if book is not None:
                self.books[token] = book

    def _marks(self) -> dict[str, float]:
        marks = {}
        for pos in self.state["positions"].values():
            if not isinstance(pos, dict) or pos.get("settled_ts"):
                continue
            token = str(pos.get("token_id") or "")
            book = self.books.get(token) or {}
            bids = book.get("bids") or []
            if bids:
                marks[token] = float(bids[0][0])
                pos["last_mark"] = marks[token]
        return marks

    def _account(self, market: LockMarket, now: float) -> dict:
        pos = self.state["positions"].get(market.slug) or {}
        exposure = open_exposure_usd(self.state["positions"].values())
        pnl = day_pnl(self.state["positions"].values(), now, self._marks())
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
            cash = float(self.cash)
        return {
            "spent": float(pos.get("cost") or 0),
            "entries": int(pos.get("entries") or 0),
            "open_cost": exposure,
            "cash": float(cash),
            "loss_stopped": loss_stop_active(pnl, stop, self.state.get("loss_stop_day"), today),
            "day_pnl": pnl,
            "cash_unknown": unknown,
        }

    def _should_log(self, slug: str, reason: str, now: float, force: bool) -> bool:
        if force:
            return True
        interval = float(self.cfg.get("eval_log_s") or 5)
        prev = self.eval_log.get(slug)
        if prev is None or prev[1] != reason or now - prev[0] >= interval:
            return True
        return False

    def tick(self, now: Optional[float] = None) -> int:
        now = time.time() if now is None else float(now)
        self.reload()
        self.ensure_feeds()
        self.refresh_markets(now)
        self.refresh_gamma_strikes(now)
        self.refresh_books(now)
        if not self.cfg.get("dry_run", True):
            self._refresh_cash(now)
        buys = 0
        for market in list(self.markets.values()):
            ttm = market.end_ts - now
            if market.key not in enabled_keys(self.cfg):
                continue
            if ttm <= 0:
                self._settle(market, now)
                continue
            window = float(self.cfg.get("entry_window_s") or 60)
            if ttm > window:
                continue
            view = self._view(market, now)
            account = self._account(market, now)
            started = time.perf_counter()
            decision = evaluate_entry(view, account, self.cfg)
            decision["eval_ms"] = (time.perf_counter() - started) * 1000.0
            if account.get("cash_unknown") and decision.get("action") == "buy":
                decision["action"] = "skip"
                decision["reason"] = "cash_unknown"
            force = decision.get("action") == "buy"
            if self._should_log(market.slug, str(decision.get("reason")), now, force):
                self.eval_log[market.slug] = (now, str(decision.get("reason")))
                log_event("eval", **_public_decision(decision))
            if decision.get("action") == "buy":
                self._enter(market, decision, now)
                buys += 1
        return buys

    def _view(self, market: LockMarket, now: float) -> dict:
        feed = self.feeds.get(market.symbol)
        live_hist = feed.live_history() if feed is not None else []
        twap_hist = feed.twap_history() if feed is not None else []
        return build_view(
            market,
            now=now,
            live_hist=live_hist,
            twap_hist=twap_hist,
            up_book=self.books.get(market.up_token),
            dn_book=self.books.get(market.dn_token),
            gamma_strike=self.gamma_px.get(market.slug, market.price_to_beat),
            cfg=self.cfg,
            market_enabled=True,
        )

    def _enter(self, market: LockMarket, decision: dict, now: float) -> None:
        dry = bool(self.cfg.get("dry_run", True))
        if not dry and self.client is None:
            log_event("order_skip", slug=market.slug, reason="no_clob_client")
            return
        try:
            raw = dispatch_buy(decision, dry_run=dry, client=None if dry else self.client)
        except Exception as exc:
            log_event("order_fail", slug=market.slug, error=str(exc)[:200])
            return
        fill = normalize_fill(raw, decision, self.cfg)
        log_event(
            "entry",
            slug=market.slug,
            asset=market.asset,
            duration=market.duration,
            lane=market.lane,
            strategy="twap_lock",
            condition_id=market.condition_id,
            side=decision.get("side"),
            p=decision.get("p"),
            ask=decision.get("ask"),
            edge=decision.get("edge"),
            limit=decision.get("limit"),
            ttm_s=decision.get("ttm_s"),
            twap=decision.get("twap"),
            live=decision.get("live"),
            strike=decision.get("strike"),
            sigma=decision.get("sigma"),
            expected=decision.get("expected"),
            ask_levels=_top(decision.get("asks")),
            bid_levels=_top(decision.get("bids")),
            shares=fill["shares"],
            cost=fill["cost"],
            vwap=fill["vwap"],
            fee=fill["fee"],
            dry_run=fill["dry_run"],
            posted=fill["posted"],
            decision_to_order_ms=fill.get("decision_to_order_ms"),
            eval_ms=decision.get("eval_ms"),
            status=fill.get("raw_status"),
        )
        count = (fill["shares"] > 0) if dry else bool(fill["posted"])
        if fill["shares"] <= 0 and not count:
            return
        self._add_fill(market, decision, fill, now, count_entry=count or fill["shares"] > 0)

    def _add_fill(self, market: LockMarket, decision: dict, fill: dict, now: float, *, count_entry: bool) -> None:
        pos = dict(self.state["positions"].get(market.slug) or {})
        shares = float(pos.get("shares") or 0) + float(fill["shares"])
        cost = float(pos.get("cost") or 0) + float(fill["cost"])
        fee = float(pos.get("fee") or 0) + float(fill["fee"])
        entries = int(pos.get("entries") or 0) + (1 if count_entry else 0)
        token = decision.get("token_id")
        pos.update(
            {
                "slug": market.slug,
                "asset": market.asset,
                "duration": market.duration,
                "lane": market.lane,
                "strategy": "twap_lock",
                "condition_id": market.condition_id,
                "side": decision.get("side") or pos.get("side"),
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
                "dry_run": bool(self.cfg.get("dry_run", True)),
            }
        )
        self.state["positions"][market.slug] = pos
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
        atomic_save(STATE_FILE, self.state)
        if fill["shares"] > 0:
            log_event(
                "fill",
                slug=market.slug,
                asset=market.asset,
                duration=market.duration,
                lane=market.lane,
                side=pos.get("side"),
                shares=fill["shares"],
                cost=fill["cost"],
                vwap=fill["vwap"],
                fee=fill["fee"],
                spent=cost,
                dry_run=pos["dry_run"],
                decision_to_order_ms=fill.get("decision_to_order_ms"),
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
        pos = self.state["positions"].get(market.slug)
        if not isinstance(pos, dict) or pos.get("settled_ts") or float(pos.get("shares") or 0) <= 0:
            return
        if now < market.end_ts:
            return
        feed = self.feeds.get(market.symbol)
        twap_hist = feed.twap_history() if feed is not None else []
        from buy.lock_markets import boundary_price

        final = boundary_price(twap_hist, market.end_ts, tol_s=float(self.cfg.get("strike_tol_s") or 0.75))
        strike = pos.get("strike")
        if final is None or strike is None:
            if now - market.end_ts > 600 and not pos.get("settle_miss_logged"):
                pos["settle_miss_logged"] = True
                log_event("settlement_unknown", slug=market.slug, have_final=final is not None, have_strike=strike is not None)
                atomic_save(STATE_FILE, self.state)
            return
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
        self.state["positions"][market.slug] = pos
        atomic_save(STATE_FILE, self.state)
        log_event(
            "settlement",
            slug=market.slug,
            asset=market.asset,
            duration=market.duration,
            lane=market.lane,
            strategy="twap_lock",
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
                condition_id=market.condition_id,
                payout_est=round(result["payout"], 6),
                won=result["won"],
            )

    def _refresh_cash(self, now: float) -> None:
        if now - self.cash_at < 15:
            return
        self.cash_at = now
        try:
            from buy.chain import ChainReader

            funder = os.getenv("FUNDER_ADDRESS") or ""
            if not funder:
                self.cash = None
                return
            chain = ChainReader(str(self.cfg.get("rpc_url")))
            self.cash = chain.pUSD_balance(str(self.cfg.get("pUSD_address")), funder)
        except Exception as exc:
            log_event("cash_fail", error=str(exc)[:160])

    def close(self) -> None:
        for feed in self.feeds.values():
            feed.stop()
        self._pool.shutdown(wait=False, cancel_futures=True)


def _sleep_s(bot: LockBot, now: float) -> float:
    poll = float(bot.cfg.get("poll_s") or 1)
    warm = float(bot.cfg.get("entry_window_s") or 60) + float(bot.cfg.get("book_warm_s") or 15)
    hot = False
    wait = 5.0
    for market in bot.markets.values():
        ttm = market.end_ts - now
        if 0 < ttm <= warm:
            hot = True
        until = ttm - warm
        if until > 0:
            wait = min(wait, until)
    if hot:
        return max(0.2, poll)
    return max(poll, min(5.0, wait))


def _redeem_loop(bot: LockBot) -> None:
    """Live redeem on its own thread. Dry-run settlements are logged in-tick."""
    if bot.cfg.get("dry_run", True) or not bot.cfg.get("redeem_enabled", True):
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
        save=lambda state: atomic_save(STATE_FILE, state),
    )
    while not _stop.is_set() and not STOP_FILE.exists():
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
    args = parser.parse_args(argv)
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
        try:
            bot.client = build_clob_client()
        except Exception as exc:
            log_event("clob_client_fail", error=str(exc)[:200])
        threading.Thread(target=_redeem_loop, args=(bot,), name="lockbot-redeem", daemon=True).start()
    log_event(
        "startup",
        dry_run=bool(cfg.get("dry_run", True)),
        enabled=bool(cfg.get("enabled", True)),
        markets=enabled_keys(cfg),
        symbols=symbols_for(cfg),
        per_market_usd=cfg.get("per_market_usd"),
        config=str(path),
    )
    try:
        while not _stop.is_set() and not STOP_FILE.exists():
            started = time.time()
            try:
                bot.tick(started)
            except Exception as exc:
                log_event("tick_fail", error=str(exc)[:200])
            delay = _sleep_s(bot, time.time())
            _stop.wait(delay)
    finally:
        bot.close()
        log_event("shutdown")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
