import os
import signal
import sys
import time
import json
import traceback
import logging
from logging.handlers import RotatingFileHandler
from datetime import datetime, timezone
import requests
from concurrent.futures import ThreadPoolExecutor
from dotenv import load_dotenv
from rich.console import Console
from rich.table import Table
from rich.panel import Panel
from rich.align import Align
from rich import box

from py_clob_client_v2 import (
    ClobClient,
    MarketOrderArgs,
    OrderType,
    PartialCreateOrderOptions,
    ApiCreds,
)
from py_clob_client_v2.order_builder.constants import SELL, BUY

from buy.market import MarketGateway
from buy.btc_price import get_btc_feed, append_research, SOURCE_TWAP_60
from buy.clob_book_ws import get_book_feed

console = Console()
load_dotenv()

HOST = "https://clob.polymarket.com"
DATA_API = "https://data-api.polymarket.com"
GAMMA_API = "https://gamma-api.polymarket.com"
CHAIN_ID = 137
STATE_FILE = "positions_buy.json"
PNL_FILE = "pnl_buy.json"
HEARTBEAT_FILE = ".heartbeat_buy"
RESEARCH_FILE = "underlying_research_buy.jsonl"
PTB_STORE_FILE = "ptb_twap60_buy.json"
UNDERLYING_SOURCE = SOURCE_TWAP_60
SERIES_SLUG = "btc-up-or-down-15m"
SLUG_PREFIX = "btc-updown"
SLUG_EXCLUDES = ("btc-updown-5m", "bitcoin-up-or-down")
PUSD = "0xC011a7E12a19f7B1f670d46F03B03f3342E82DFB"
CTF = "0x4D97DCd97eC945f40cF65F87097ACe5EA0476045"

PRIVATE_KEY = os.getenv("PRIVATE_KEY")
FUNDER_ADDRESS = os.getenv("FUNDER_ADDRESS")
API_KEY = os.getenv("API_KEY")
API_SECRET = os.getenv("API_SECRET")
API_PASSPHRASE = os.getenv("API_PASSPHRASE")
RELAYER_URL = os.getenv("RELAYER_URL", "https://relayer-v2.polymarket.com")
RELAYER_API_KEY = os.getenv("RELAYER_API_KEY", "019df62f-45bc-796e-975c-3f434472b163")
RELAYER_API_KEY_ADDRESS = os.getenv("RELAYER_API_KEY_ADDRESS", "0x42aec4505559c0613f7ce2541d9d29741bc5e195")

# ------------------------- STRATEGY CONFIG -------------------------
_STRATEGY_DEFAULTS = {
    "buy_threshold": 0.96,
    "buy_max_price": 0.99,
    # Consensus on Polymarket GUI display price (mid if spread≤10¢ else last trade).
    "min_winner_bid": 0.90,
    "max_loser_bid": 0.10,
    "min_bid_edge": 0.05,
    # Skip buys unless live BTC is ≥ this many USD from the window Price To Beat,
    # and only allow the side matching that underlying move.
    "underlying_gate_enabled": True,
    "min_underlying_edge_usd": 10.0,
    "hedge_enabled": True,
    "hedge_threshold": 0.65,
    # FAK sell floor while hedging. Must be a valid tick multiple:
    # 15m/hourly tick=0.01 → 0.32; 5m tick=0.001 can use 0.325.
    "hedge_min_price": 0.32,
    # Undercut top bid by N ticks so FAK crosses a falling book during order RTT.
    "hedge_undercut_ticks": 2,
    # Skip extra REST bounce confirm when WS/cache quote is this fresh (seconds).
    "hedge_quote_max_age_s": 0.25,
    "hedge_retry_sleep_s": 0.05,
    "hedge_ghost_sleep_s": 0.4,
    # Entry: refuse wide books (ask≃97¢ over bid≃1¢ is not a real price).
    "max_entry_spread": 0.05,
    # Hedge: penny bids under a still-high ask are fake — require a tight book
    # and ask also collapsed (same lesson sell-side already learned on mids).
    "hedge_max_spread": 0.15,
    "hedge_require_ask_max": 0.70,
    "buy_window_min": 3.0,
    "buy_grace_s": 2,
    "buy_cooldown_s": 3,
    "buy_budget": 21.0,
    "max_open_positions": 100,
    "max_open_notional": 10000.0,
    "max_daily_notional": 999999.0,
    "one_entry_per_market": True,
    "redeem_throttle_s": 30,
    "max_redeem_age_days": 7,
    "dry_run": False,
    "poll_buy_window_s": 0.1,
    # Sub-second while any position is held (hedge path), independent of buy window.
    "poll_held_s": 0.05,
    "positions_refresh_s": 2,
    "balance_refresh_s": 15,
    # Throttle Rich position table while in hot mode (every N cycles).
    "ui_every_n_cycles": 5,
    "tick_size": "0.01",
}
STRATEGY_FILE = "strategy_buy.json"

_strat_cache = None
_strat_mtime = 0.0


def load_strategy():
    global _strat_cache, _strat_mtime
    try:
        mtime = os.path.getmtime(STRATEGY_FILE) if os.path.exists(STRATEGY_FILE) else 0
    except OSError:
        mtime = 0
    if _strat_cache is not None and mtime == _strat_mtime:
        return _strat_cache
    cfg = dict(_STRATEGY_DEFAULTS)
    try:
        if os.path.exists(STRATEGY_FILE):
            with open(STRATEGY_FILE, "r") as f:
                overrides = json.load(f)
            for k, v in overrides.items():
                if k in cfg:
                    expected = type(cfg[k])
                    if expected is bool:
                        cfg[k] = v if isinstance(v, bool) else str(v).lower() in ("1", "true", "yes")
                    else:
                        cfg[k] = expected(v)
            # Legacy alias: near $1 prices, "shares" was effectively a dollar budget
            if "buy_budget" not in overrides and "shares" in overrides:
                try:
                    cfg["buy_budget"] = float(overrides["shares"])
                except (TypeError, ValueError):
                    pass
    except Exception as e:
        console.print(f"[bold red]▶ STRATEGY [WARN][/] [dim]failed to load {STRATEGY_FILE}: {e}[/]")
    _strat_cache = cfg
    _strat_mtime = mtime
    return cfg


_strat = load_strategy()
BUY_THRESHOLD = _strat["buy_threshold"]
BUY_MAX_PRICE = _strat["buy_max_price"]
MIN_WINNER_BID = _strat["min_winner_bid"]
MAX_LOSER_BID = _strat["max_loser_bid"]
MIN_BID_EDGE = _strat["min_bid_edge"]
UNDERLYING_GATE_ENABLED = _strat["underlying_gate_enabled"]
MIN_UNDERLYING_EDGE_USD = _strat["min_underlying_edge_usd"]
HEDGE_ENABLED = _strat["hedge_enabled"]
HEDGE_THRESHOLD = _strat["hedge_threshold"]
HEDGE_MIN_PRICE = _strat["hedge_min_price"]
HEDGE_UNDERCUT_TICKS = _strat["hedge_undercut_ticks"]
HEDGE_QUOTE_MAX_AGE_S = _strat["hedge_quote_max_age_s"]
HEDGE_RETRY_SLEEP_S = _strat["hedge_retry_sleep_s"]
HEDGE_GHOST_SLEEP_S = _strat["hedge_ghost_sleep_s"]
MAX_ENTRY_SPREAD = _strat["max_entry_spread"]
HEDGE_MAX_SPREAD = _strat["hedge_max_spread"]
HEDGE_REQUIRE_ASK_MAX = _strat["hedge_require_ask_max"]
BUY_WINDOW_MIN = _strat["buy_window_min"]
BUY_GRACE_S = _strat["buy_grace_s"]
BUY_COOLDOWN_S = _strat["buy_cooldown_s"]
BUY_BUDGET = _strat["buy_budget"]
MAX_OPEN_POSITIONS = _strat["max_open_positions"]
MAX_OPEN_NOTIONAL = _strat["max_open_notional"]
MAX_DAILY_NOTIONAL = _strat["max_daily_notional"]
ONE_ENTRY_PER_MARKET = _strat["one_entry_per_market"]
REDEEM_THROTTLE_S = _strat["redeem_throttle_s"]
MAX_REDEEM_AGE_DAYS = _strat["max_redeem_age_days"]
DRY_RUN = _strat["dry_run"]
POLL_BUY_WINDOW_S = _strat["poll_buy_window_s"]
POLL_HELD_S = _strat["poll_held_s"]
POSITIONS_REFRESH_S = _strat["positions_refresh_s"]
BALANCE_REFRESH_S = _strat["balance_refresh_s"]
UI_EVERY_N_CYCLES = _strat["ui_every_n_cycles"]
TICK_SIZE_FALLBACK = _strat["tick_size"]

# ------------------------- LOG ROTATION -------------------------
LOG_FILE = "buybot.log"
LOG_MAX_BYTES = 5 * 1024 * 1024
LOG_BACKUP_COUNT = 3

_file_logger = logging.getLogger("buybot")
_file_logger.setLevel(logging.INFO)
_log_handler = RotatingFileHandler(LOG_FILE, maxBytes=LOG_MAX_BYTES, backupCount=LOG_BACKUP_COUNT)
_log_handler.setFormatter(logging.Formatter("%(message)s"))
_file_logger.addHandler(_log_handler)

NTFY_TOPIC = os.getenv("NTFY_TOPIC", "polybot-joel-btc")


def notify(title, message, priority="default"):
    try:
        requests.post(
            f"https://ntfy.sh/{NTFY_TOPIC}",
            data=message.encode("utf-8"),
            headers={"Title": title, "Priority": priority},
            timeout=5,
        )
    except Exception:
        pass


# ------------------------- HELPERS -------------------------

def safe_api_call(func, *args, **kwargs):
    try:
        return func(*args, **kwargs)
    except Exception as e:
        console.print(f"[dim red]API error: {e}[/]")
        raise


def get_balance():
    try:
        from py_clob_client_v2.client import BalanceAllowanceParams
        bal_info = safe_api_call(client.get_balance_allowance, BalanceAllowanceParams(asset_type="COLLATERAL"))
        return float(bal_info.balance) / 1e6 if hasattr(bal_info, "balance") else 0.0
    except Exception:
        return 0.0


def atomic_save(path, data):
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, path)


def load_json(path):
    if os.path.exists(path):
        with open(path, "r") as f:
            return json.load(f)
    return {}


def save_json(path, data):
    atomic_save(path, data)


def log_event(event, **kwargs):
    entry = {"ts": datetime.now().isoformat(), "event": event}
    entry.update(kwargs)
    _file_logger.info(json.dumps(entry))


def write_heartbeat():
    try:
        with open(HEARTBEAT_FILE, "w") as f:
            f.write(str(int(time.time())))
    except Exception:
        pass


# ------------------------- P&L -------------------------

def load_pnl():
    if os.path.exists(PNL_FILE):
        try:
            with open(PNL_FILE, "r") as f:
                return json.load(f)
        except Exception:
            pass
    return {"trades": [], "summary": {"total_pnl": 0.0, "total_trades": 0, "wins": 0, "losses": 0}}


def record_pnl(condition_id, question, entry_cost, sell_proceeds, hedge_proceeds, outcome):
    pnl_data = load_pnl()
    net = sell_proceeds + hedge_proceeds - entry_cost
    pnl_data["trades"].append({
        "condition_id": condition_id,
        "question": question,
        "entry_cost": round(entry_cost, 4),
        "sell_proceeds": round(sell_proceeds, 4),
        "hedge_proceeds": round(hedge_proceeds, 4),
        "net": round(net, 4),
        "outcome": outcome,
        "timestamp": datetime.now().isoformat(),
    })
    pnl_data["summary"]["total_pnl"] = round(pnl_data["summary"]["total_pnl"] + net, 4)
    pnl_data["summary"]["total_trades"] += 1
    if net > 0:
        pnl_data["summary"]["wins"] += 1
    else:
        pnl_data["summary"]["losses"] += 1
    if len(pnl_data["trades"]) > 500:
        pnl_data["trades"] = pnl_data["trades"][-500:]
    atomic_save(PNL_FILE, pnl_data)
    return net


# ------------------------- POSITIONS -------------------------

def get_user_positions():
    try:
        resp = requests.get(
            f"{DATA_API}/positions",
            params={"user": FUNDER_ADDRESS, "limit": 500},
            timeout=10,
        )
        resp.raise_for_status()
        return resp.json()
    except Exception as e:
        log_event("positions_fetch_fail", error=str(e)[:200])
        return None


def check_token_balance(token_id):
    try:
        resp = requests.get(
            f"{DATA_API}/positions",
            params={"user": FUNDER_ADDRESS, "limit": 500},
            timeout=10,
        )
        resp.raise_for_status()
        for p in resp.json():
            if p.get("asset") == token_id:
                return float(p.get("size", 0) or 0)
    except Exception:
        pass
    return None


def build_held_positions(positions_raw):
    """Return {conditionId: {"up": {asset, size, redeemable, avgPrice}, "dn": {...}}}."""
    held = {}
    for p in positions_raw or []:
        slug = (p.get("slug") or "").lower()
        event_slug = (p.get("eventSlug") or "").lower()
        if slug.startswith(SLUG_EXCLUDES) or event_slug.startswith(SLUG_EXCLUDES):
            continue
        if not (slug.startswith(SLUG_PREFIX) or event_slug.startswith(SLUG_PREFIX)):
            continue
        cond = p.get("conditionId")
        if not cond:
            continue
        oc = (p.get("outcome") or "").lower()
        if oc not in ("up", "down", "yes", "no"):
            continue
        leg = "up" if oc in ("up", "yes") else "down"
        held.setdefault(cond, {})[leg] = {
            "asset": p.get("asset"),
            "size": float(p.get("size", 0) or 0),
            "redeemable": bool(p.get("redeemable", False)),
            "avgPrice": float(p.get("avgPrice", 0) or 0),
        }
    return held


# ------------------------- PRICING -------------------------

def get_book_bid(token_id):
    try:
        book = safe_api_call(client.get_order_book, token_id)
        path = "sdk"
    except Exception as sdk_err:
        try:
            resp = requests.get(f"{HOST}/book", params={"token_id": token_id}, timeout=5)
            resp.raise_for_status()
            book = resp.json()
            path = "http"
            log_event("book_fetch_fallback_ok", token_id=token_id, sdk_error=str(sdk_err)[:200])
        except Exception as http_err:
            log_event("book_fetch_fail", token_id=token_id, sdk_error=str(sdk_err)[:200], http_error=str(http_err)[:200])
            return None, 0.0
    try:
        bids = book.get("bids", [])
        if not bids:
            return None, 0.0
        best = max(bids, key=lambda x: float(x.get("price", 0)))
        return float(best.get("price", 0)), float(best.get("size", 0))
    except Exception as e:
        log_event("book_fetch_fail", token_id=token_id, error=str(e), path=path)
        return None, 0.0


def get_book_quote(token_id):
    """Return (bid_price, bid_size, ask_price, ask_size, mid_price).
    mid_price is None when either side of the book is missing."""
    try:
        book = safe_api_call(client.get_order_book, token_id)
        path = "sdk"
    except Exception as sdk_err:
        try:
            resp = requests.get(f"{HOST}/book", params={"token_id": token_id}, timeout=5)
            resp.raise_for_status()
            book = resp.json()
            path = "http"
            log_event("book_quote_fallback_ok", token_id=token_id, sdk_error=str(sdk_err)[:200])
        except Exception as http_err:
            log_event("book_quote_fail", token_id=token_id, sdk_error=str(sdk_err)[:200], http_error=str(http_err)[:200])
            return None, 0.0, None, 0.0, None
    try:
        bids = book.get("bids", [])
        asks = book.get("asks", [])
        bid_price = None
        bid_size = 0.0
        ask_price = None
        ask_size = 0.0
        mid_price = None
        if bids:
            best_bid = max(bids, key=lambda x: float(x.get("price", 0)))
            bid_price = float(best_bid.get("price", 0))
            bid_size = float(best_bid.get("size", 0))
        if asks:
            best_ask = min(asks, key=lambda x: float(x.get("price", 0)))
            ask_price = float(best_ask.get("price", 0))
            ask_size = float(best_ask.get("size", 0))
        if bid_price is not None and ask_price is not None:
            mid_price = (bid_price + ask_price) / 2.0
        return bid_price, bid_size, ask_price, ask_size, mid_price
    except Exception as e:
        log_event("book_quote_fail", token_id=token_id, error=str(e), path=path)
        return None, 0.0, None, 0.0, None


_rest_quote_cache = {}
_REST_QUOTE_MIN_INTERVAL_S = 0.2
_REST_QUOTE_CACHE_MAX = 64
_last_trade_cache = {}
_LAST_TRADE_MIN_INTERVAL_S = 0.25


def prune_rest_caches(keep_tokens=None):
    """Drop REST cache entries for tokens we no longer watch (market rollover)."""
    keep = {str(t) for t in (keep_tokens or []) if t}
    if keep:
        for cache in (_rest_quote_cache, _last_trade_cache):
            for tid in list(cache.keys()):
                if tid not in keep:
                    cache.pop(tid, None)
    for cache in (_rest_quote_cache, _last_trade_cache):
        if len(cache) > _REST_QUOTE_CACHE_MAX:
            # Drop oldest by timestamp (value is (payload, ts))
            ordered = sorted(cache.items(), key=lambda kv: kv[1][1] if isinstance(kv[1], tuple) and len(kv[1]) > 1 else 0)
            for tid, _ in ordered[: max(0, len(cache) - _REST_QUOTE_CACHE_MAX)]:
                cache.pop(tid, None)


def get_quote_fast(token_id, max_age_s=2.0, prefer_rest=False, force_rest=False):
    """Prefer fresh CLOB WS top-of-book; REST fallback is rate-limited.

    force_rest=True: bypass WS and the 200ms REST cache — use only at order
    boundaries (buy/hedge confirm/sell retry) where a stale snapshot is unsafe.
    """
    if not prefer_rest and not force_rest:
        ws_q = get_book_feed().quote(token_id, max_age_s=max_age_s)
        if ws_q is not None and (ws_q[0] is not None or ws_q[2] is not None):
            return ws_q
    now = time.time()
    if not force_rest:
        cached = _rest_quote_cache.get(token_id)
        if cached is not None:
            q, ts = cached
            if (now - ts) < _REST_QUOTE_MIN_INTERVAL_S:
                return q
    q = get_book_quote(token_id)
    _rest_quote_cache[token_id] = (q, now)
    if len(_rest_quote_cache) > _REST_QUOTE_CACHE_MAX:
        prune_rest_caches()
    return q

    q = get_book_quote(token_id)
    _rest_quote_cache[token_id] = (q, now)
    if len(_rest_quote_cache) > _REST_QUOTE_CACHE_MAX:
        prune_rest_caches()
    return q


def hedge_sell_price(bid, tick_size, undercut_ticks, min_price):
    """FAK sell floor: undercut bid, align to tick, clamp to min_price."""
    tick = float(tick_size or TICK_SIZE_FALLBACK)
    if tick <= 0:
        tick = 0.01
    undercut = max(0, int(undercut_ticks)) * tick
    raw = float(bid or 0) - undercut
    # Floor to tick so CLOB never rejects for precision (e.g. 0.325 on 0.01).
    aligned = (int(raw / tick + 1e-12)) * tick
    floor = float(min_price or tick)
    floor_aligned = (int(floor / tick + 1e-12)) * tick
    if floor_aligned + 1e-12 < floor:
        # min_price wasn't on-tick; round UP to next valid tick for the floor.
        floor_aligned = (int(floor / tick + 1e-12) + 1) * tick
    return max(floor_aligned, tick, aligned)


# Polymarket UI display rule (docs.polymarket.com/concepts/prices-orderbook):
# show midpoint when bid-ask spread <= $0.10; otherwise show last traded price.
POLYMARKET_GUI_SPREAD = 0.10


def get_last_trade_price(token_id):
    """CLOB last-trade price for a token (what the UI falls back to on wide spreads)."""
    now = time.time()
    cached = _last_trade_cache.get(token_id)
    if cached is not None:
        price, ts = cached
        if (now - ts) < _LAST_TRADE_MIN_INTERVAL_S:
            return price
    try:
        resp = requests.get(f"{HOST}/last-trade-price", params={"token_id": token_id}, timeout=5)
        resp.raise_for_status()
        data = resp.json()
        price = data.get("price") if isinstance(data, dict) else None
        out = float(price) if price is not None else None
        _last_trade_cache[token_id] = (out, now)
        if len(_last_trade_cache) > _REST_QUOTE_CACHE_MAX:
            prune_rest_caches()
        return out
    except Exception as e:
        log_event("last_trade_fail", token_id=token_id, error=str(e)[:200])
        if cached is not None:
            return cached[0]
        return None


def polymarket_display_price(bid, ask, last_trade):
    """Probability a human sees on Polymarket for this outcome."""
    if bid is not None and ask is not None and (ask - bid) <= POLYMARKET_GUI_SPREAD + 1e-12:
        return (bid + ask) / 2.0
    return last_trade


def entry_book_ok(bid, ask, max_spread, min_bid):
    """True only when top-of-book is tight and bid supports the ask story.

    Wide books (ask 98¢ / bid 1¢) produce fake gate prices — last-trade GUI can
    still look like a winner while there is no real bid under the ask.
    """
    if bid is None or ask is None:
        return False, "missing_side"
    if ask < bid:
        return False, "crossed"
    spread = ask - bid
    if spread > float(max_spread) + 1e-12:
        return False, "wide_spread"
    if bid + 1e-12 < float(min_bid):
        return False, "bid_too_low"
    return True, "ok"


def hedge_book_ok(bid, ask, threshold, max_spread, require_ask_max):
    """True only when the held book actually collapsed — not a lone penny bid.

    A 1¢ bid under a 99¢ ask is illiquidity/spoof, not a reversal. Require:
      bid ≤ threshold, ask ≤ require_ask_max, and spread ≤ max_spread.
    """
    if bid is None or ask is None:
        return False, "missing_side"
    if bid > float(threshold) + 1e-12:
        return False, "bid_above"
    if ask > float(require_ask_max) + 1e-12:
        return False, "ask_too_high"
    if ask < bid:
        return False, "crossed"
    if (ask - bid) > float(max_spread) + 1e-12:
        return False, "wide_spread"
    return True, "ok"


def fill_cost_usdc(result, filled, limit_price, spend_cap):
    """USDC spent on a BUY.

    CLOB v2 market BUY: makingAmount = USDC paid, takingAmount = shares received.
    Do not use closeness heuristics — USDC and share counts are often nearby
    numerically near $1 prices and misclassification invents fake avg fills.
    """
    filled = float(filled or 0)
    limit_price = float(limit_price or 0)
    spend_cap = float(spend_cap or 0)
    if filled <= 0:
        return 0.0
    d = _result_as_dict(result)
    for k in ("average_price", "avg_price"):
        if d.get(k) is not None:
            try:
                cost = filled * float(d[k])
                return min(spend_cap, cost) if spend_cap > 0 else cost
            except (TypeError, ValueError):
                pass
    try:
        making = d.get("makingAmount", d.get("making_amount"))
        if making is not None:
            making_f = float(making)
            # Fixed-point (1e6) only when absurdly large vs budget/shares.
            if making_f > 1000 and (spend_cap <= 0 or making_f > spend_cap * 50):
                making_f /= 1e6
            cost = making_f
            return min(spend_cap, cost) if spend_cap > 0 else cost
    except (TypeError, ValueError):
        pass
    cost = filled * limit_price
    return min(spend_cap, cost) if spend_cap > 0 else cost


# ------------------------- TICK SIZE -------------------------

_tick_size_cache = {}


def get_tick_size_cached(token_id):
    if token_id in _tick_size_cache:
        return _tick_size_cache[token_id]
    tick = str(TICK_SIZE_FALLBACK)
    try:
        result = client.get_tick_size(token_id)
        if result:
            tick = str(result)
    except Exception as e:
        log_event("tick_size_lookup_fail", token_id=token_id, error=str(e)[:200], fallback=tick)
    _tick_size_cache[token_id] = tick
    return tick


# ------------------------- ORDER HELPERS -------------------------

def extract_order_id(order_obj):
    if isinstance(order_obj, dict):
        return order_obj.get("orderID") or order_obj.get("id")
    return getattr(order_obj, "orderID", None) or getattr(order_obj, "id", None) or (str(order_obj) if order_obj is not None else None)


def get_order_details(order_id):
    if not order_id:
        return None
    try:
        result = safe_api_call(client.get_order, order_id)
        if isinstance(result, dict):
            return {
                "status": result.get("status", "UNKNOWN"),
                "size_matched": float(result.get("size_matched", 0) or result.get("takerAmount", 0) or 0),
                "size": float(result.get("size", 0) or result.get("originalSize", 0) or result.get("makerAmount", 0) or 1),
            }
        return {
            "status": getattr(result, "status", "UNKNOWN"),
            "size_matched": float(getattr(result, "size_matched", 0) or 0),
            "size": float(getattr(result, "size", 1)),
        }
    except Exception as e:
        err = str(e).lower()
        if "not found" in err or "404" in err:
            return {"status": "NOT_FOUND"}
        return None


def confirm_fill_size(result, oid, requested, *, wait_delayed_s=2.0, poll_s=0.25):
    """Return confirmed matched size.

    Polls while status is delayed/pending or matched is still 0. Never treats
    order 404 as a full fill. Callers still reconcile via balance for ghosts.
    """
    def _from_result(res):
        if isinstance(res, dict):
            sm = res.get("size_matched")
            return float(sm) if sm else 0.0
        return float(getattr(res, "size_matched", 0) or 0)

    matched = _from_result(result)
    if matched > 0:
        return matched
    if not oid:
        return 0.0

    deadline = time.time() + float(wait_delayed_s)
    last_status = None
    while True:
        details = get_order_details(oid)
        if details:
            last_status = str(details.get("status") or "")
            if last_status == "NOT_FOUND":
                return 0.0
            sm = details.get("size_matched", 0)
            matched = float(sm) if sm else 0.0
            if matched > 0:
                return matched
            st = last_status.lower()
            # Terminal empty — stop polling.
            if st in ("canceled", "cancelled", "rejected", "expired", "failed"):
                return 0.0
            # delayed / live / matched-unknown: keep waiting until deadline
            if st and st not in ("delayed", "pending", "live", "open", "unmatched", "unknown", ""):
                # Non-delayed unknown terminal-ish — one more chance then exit loop
                if time.time() >= deadline:
                    return matched
        if time.time() >= deadline:
            if last_status:
                log_event(
                    "order_confirm_timeout",
                    order_id=str(oid)[:24],
                    status=last_status,
                    requested=requested,
                )
            return matched
        time.sleep(float(poll_s))



def _result_as_dict(result):
    if isinstance(result, dict):
        return result
    if result is None:
        return {}
    out = {}
    for k in (
        "average_price", "avg_price", "price", "size_matched",
        "takingAmount", "taking_amount", "makingAmount", "making_amount",
    ):
        if hasattr(result, k):
            out[k] = getattr(result, k)
    if hasattr(result, "__dict__"):
        out.update({k: v for k, v in vars(result).items() if not k.startswith("_")})
    return out


def fill_proceeds(result, filled, limit_price):
    """USDC proceeds for a SELL fill. Prefer API avg/notional; else limit × size."""
    filled = float(filled or 0)
    limit_price = float(limit_price or 0)
    if filled <= 0:
        return 0.0
    d = _result_as_dict(result)
    for k in ("average_price", "avg_price"):
        if d.get(k) is not None:
            try:
                return filled * float(d[k])
            except (TypeError, ValueError):
                pass
    # Some CLOB responses expose making/taking amounts (shares vs USDC).
    try:
        taking = d.get("takingAmount", d.get("taking_amount"))
        making = d.get("makingAmount", d.get("making_amount"))
        if taking is not None and making is not None:
            taking_f = float(taking)
            making_f = float(making)
            # Heuristic: values >> size are likely 1e6 fixed-point.
            if taking_f > 1000 and making_f > 1000:
                taking_f /= 1e6
                making_f /= 1e6
            if making_f > 0 and abs(making_f - filled) / max(filled, 1e-9) < 0.25:
                return taking_f
            if taking_f > 0 and abs(taking_f - filled) / max(filled, 1e-9) < 0.25:
                return making_f
    except (TypeError, ValueError):
        pass
    return filled * limit_price


# ------------------------- BUY -------------------------

def buy_market_with_retry(token_id, budget, max_price, tick_size="0.01", max_retries=3, min_price=0.0):
    """Spend up to `budget` dollars buying token_id at or below max_price via FAK.

    Returns (shares_bought, usdc_spent). CLOB market BUY `amount` is USDC notional
    (not shares). Cap notional to top-of-book ask size so we don't walk deeper
    levels. Order-boundary quotes use force_rest (no WS / no 200ms cache).

    IMPORTANT: any confirmed fill is returned and must be persisted by the caller.
    A below-band average is logged as buy_fill_below_band but NOT discarded —
    the shares are already ours (orphan inventory is worse than a bad average).
    """
    total_bought = 0.0
    spent = 0.0
    budget = float(budget)
    if DRY_RUN:
        console.print(f"  [bold black on yellow][DRY BUY][/] would SPEND ≤${budget:.2f} on {str(token_id)[:12]}… @ ≤{max_price:.3f}")
        log_event("dry_buy", token_id=token_id, budget=budget, max_price=max_price)
        return 0.0, 0.0
    for attempt in range(max_retries):
        remaining_budget = budget - spent
        if remaining_budget < 0.01:
            break
        fresh_bid, _, fresh_ask, fresh_ask_size, _ = get_quote_fast(
            token_id, prefer_rest=True, force_rest=True,
        )
        if fresh_ask is None:
            console.print(f"  [dim yellow][NO ASK][/] no asks available · attempt {attempt + 1}/{max_retries}")
            break
        if fresh_ask > max_price:
            console.print(f"  [dim yellow][SKIP][/] ask {fresh_ask:.3f} > cap {max_price:.3f} · attempt {attempt + 1}/{max_retries}")
            time.sleep(0.05)
            continue
        if fresh_ask < min_price:
            console.print(f"  [dim yellow][STOP][/] ask {fresh_ask:.3f} < min {min_price:.3f} · abort retries")
            log_event("buy_retry_stop_below_min", token_id=token_id, ask=fresh_ask, min_price=min_price, attempt=attempt + 1)
            break
        ok, why = entry_book_ok(fresh_bid, fresh_ask, MAX_ENTRY_SPREAD, MIN_WINNER_BID)
        if not ok:
            console.print(
                f"  [dim yellow][STOP][/] toxic book bid={fresh_bid} ask={fresh_ask} ({why}) · abort"
            )
            log_event(
                "buy_retry_stop_toxic_book", token_id=token_id, bid=fresh_bid, ask=fresh_ask,
                reason=why, max_spread=MAX_ENTRY_SPREAD, attempt=attempt + 1,
            )
            break
        if (fresh_ask_size or 0) < 0.01:
            console.print(f"  [dim yellow][NO SIZE][/] ask size {fresh_ask_size} · attempt {attempt + 1}/{max_retries}")
            time.sleep(0.05)
            continue
        book_notional = fresh_ask * float(fresh_ask_size)
        spend = min(remaining_budget, book_notional)
        if spend < 0.01:
            break
        max_shares = spend / float(min_price) if min_price > 0 else spend / fresh_ask
        price = fresh_ask
        bal_before = check_token_balance(token_id)
        try:
            result = safe_api_call(
                client.create_and_post_market_order,
                MarketOrderArgs(token_id=token_id, amount=spend, side=BUY, price=price),
                options=PartialCreateOrderOptions(tick_size=tick_size, neg_risk=False),
                order_type=OrderType.FAK,
            )
            if result:
                oid = extract_order_id(result)
                filled = float(confirm_fill_size(result, oid, spend / price if price else 0))
                fill_cost = 0.0
                if filled <= 0:
                    # Delayed/ghost: balance may move after the immediate response.
                    time.sleep(float(HEDGE_GHOST_SLEEP_S))
                    filled = float(confirm_fill_size(result, oid, spend / price if price else 0, wait_delayed_s=0.5))
                    if filled <= 0 and bal_before is not None:
                        bal_after = check_token_balance(token_id)
                        if bal_after is not None and bal_after > bal_before + 0.01:
                            filled = bal_after - bal_before
                            fill_cost = min(spend, filled * price)
                            log_event(
                                "buy_ghost_fill", token_id=token_id, filled=filled,
                                fill_cost=round(fill_cost, 4), ask=price, attempt=attempt + 1,
                            )
                    if filled <= 0:
                        console.print("  [dim yellow][FAK NULL][/] 0 confirmed fill · stopping")
                        break
                if fill_cost <= 0:
                    fill_cost = fill_cost_usdc(result, filled, price, spend)
                avg = (fill_cost / filled) if filled > 0 else 0.0
                below_band = (
                    filled > max_shares * 1.05 + 1e-9
                    or (min_price > 0 and avg + 1e-9 < float(min_price))
                )
                # ALWAYS accumulate confirmed inventory — discard creates orphans.
                total_bought += filled
                spent += fill_cost
                remaining_budget = budget - spent
                if below_band:
                    console.print(
                        f"  [bold red][BUY BELOW BAND][/] filled={filled:.4f} avg={avg:.3f} "
                        f"(min_px={min_price:.3f}) — persisting inventory"
                    )
                    log_event(
                        "buy_fill_below_band", token_id=token_id, filled=filled, avg_price=round(avg, 4),
                        fill_cost=round(fill_cost, 4), spend=round(spend, 4), max_shares=round(max_shares, 4),
                        min_price=min_price, ask=price, attempt=attempt + 1,
                    )
                    # Stop chasing; caller must still persist this fill.
                    break
                console.print(f"  [bold green][BUY FAK][/]{filled} @ avg {avg:.3f} (${fill_cost:.2f})  [dim]id={str(oid)[:16]}…[/]")
                log_event(
                    "buy_fill", token_id=token_id, filled=filled, price=price, avg_price=round(avg, 4),
                    spend=round(spend, 4), spent=round(spent, 4),
                    remaining_budget=round(remaining_budget, 4), attempt=attempt + 1,
                )
                if remaining_budget < 0.01:
                    return total_bought, spent
        except Exception as e:
            console.print(f"  [dim red]Market buy {attempt+1}/{max_retries} failed: {e}[/]")
            log_event("buy_attempt_error", token_id=token_id, error=str(e)[:200], attempt=attempt + 1)
            # Order may have posted before the exception — reconcile once.
            if bal_before is not None:
                time.sleep(float(HEDGE_GHOST_SLEEP_S))
                bal_after = check_token_balance(token_id)
                if bal_after is not None and bal_after > bal_before + 0.01:
                    filled = bal_after - bal_before
                    fill_cost = min(remaining_budget, filled * price)
                    total_bought += filled
                    spent += fill_cost
                    log_event(
                        "buy_ghost_fill", token_id=token_id, filled=filled,
                        fill_cost=round(fill_cost, 4), ask=price, attempt=attempt + 1, via="exception",
                    )
                    break
        time.sleep(0.05)

    if total_bought > 0:
        console.print(f"  [bold yellow][BUY PARTIAL][/]{total_bought:.4f} shares · ${spent:.2f}/${budget:.2f} spent")
        return total_bought, spent
    console.print(f"  [bold red][BUY FAIL][/] spent $0.00/${budget:.2f}")
    return 0.0, 0.0

    for attempt in range(max_retries):
        remaining_budget = budget - spent
        if remaining_budget < 0.01:
            break
        # REST book for execution — never size/price a live order off WS alone.
        fresh_bid, _, fresh_ask, fresh_ask_size, _ = get_quote_fast(token_id, prefer_rest=True)
        if fresh_ask is None:
            console.print(f"  [dim yellow][NO ASK][/] no asks available · attempt {attempt + 1}/{max_retries}")
            break
        if fresh_ask > max_price:
            console.print(f"  [dim yellow][SKIP][/] ask {fresh_ask:.3f} > cap {max_price:.3f} · attempt {attempt + 1}/{max_retries}")
            time.sleep(0.05)
            continue
        if fresh_ask < min_price:
            console.print(f"  [dim yellow][STOP][/] ask {fresh_ask:.3f} < min {min_price:.3f} · abort retries")
            log_event("buy_retry_stop_below_min", token_id=token_id, ask=fresh_ask, min_price=min_price, attempt=attempt + 1)
            break
        ok, why = entry_book_ok(fresh_bid, fresh_ask, MAX_ENTRY_SPREAD, MIN_WINNER_BID)
        if not ok:
            console.print(
                f"  [dim yellow][STOP][/] toxic book bid={fresh_bid} ask={fresh_ask} ({why}) · abort"
            )
            log_event(
                "buy_retry_stop_toxic_book", token_id=token_id, bid=fresh_bid, ask=fresh_ask,
                reason=why, max_spread=MAX_ENTRY_SPREAD, attempt=attempt + 1,
            )
            break
        if (fresh_ask_size or 0) < 0.01:
            console.print(f"  [dim yellow][NO SIZE][/] ask size {fresh_ask_size} · attempt {attempt + 1}/{max_retries}")
            time.sleep(0.05)
            continue
        book_notional = fresh_ask * float(fresh_ask_size)
        spend = min(remaining_budget, book_notional)
        if spend < 0.01:
            break
        # Hard share cap: never buy more shares than budget/min_price allows.
        max_shares = spend / float(min_price) if min_price > 0 else spend / fresh_ask
        price = fresh_ask
        try:
            result = safe_api_call(
                client.create_and_post_market_order,
                MarketOrderArgs(token_id=token_id, amount=spend, side=BUY, price=price),
                options=PartialCreateOrderOptions(tick_size=tick_size, neg_risk=False),
                order_type=OrderType.FAK,
            )
            if result:
                oid = extract_order_id(result)
                filled = float(confirm_fill_size(result, oid, spend / price if price else 0))
                if filled <= 0:
                    console.print("  [dim yellow][FAK NULL][/] 0 confirmed fill · stopping")
                    break
                fill_cost = fill_cost_usdc(result, filled, price, spend)
                avg = (fill_cost / filled) if filled > 0 else 0.0
                if filled > max_shares * 1.05 + 1e-9 or (min_price > 0 and avg + 1e-9 < float(min_price)):
                    console.print(
                        f"  [bold red][BUY REJECT][/] filled={filled:.4f} avg={avg:.3f} "
                        f"(cap shares≈{max_shares:.2f} min_px={min_price:.3f}) — fake fill"
                    )
                    log_event(
                        "buy_reject_bad_fill", token_id=token_id, filled=filled, avg_price=round(avg, 4),
                        fill_cost=round(fill_cost, 4), spend=round(spend, 4), max_shares=round(max_shares, 4),
                        min_price=min_price, ask=price, attempt=attempt + 1,
                    )
                    # Do not accumulate — treat as no fill this attempt.
                    break
                total_bought += filled
                spent += fill_cost
                remaining_budget = budget - spent
                console.print(f"  [bold green][BUY FAK][/]{filled} @ avg {avg:.3f} (${fill_cost:.2f})  [dim]id={str(oid)[:16]}…[/]")
                log_event(
                    "buy_fill", token_id=token_id, filled=filled, price=price, avg_price=round(avg, 4),
                    spend=round(spend, 4), spent=round(spent, 4),
                    remaining_budget=round(remaining_budget, 4), attempt=attempt + 1,
                )
                if remaining_budget < 0.01:
                    return total_bought, spent
        except Exception as e:
            console.print(f"  [dim red]Market buy {attempt+1}/{max_retries} failed: {e}[/]")
            log_event("buy_attempt_error", token_id=token_id, error=str(e)[:200], attempt=attempt + 1)
        time.sleep(0.05)

    if total_bought > 0:
        console.print(f"  [bold yellow][BUY PARTIAL][/]{total_bought:.4f} shares · ${spent:.2f}/${budget:.2f} spent")
        return total_bought, spent
    console.print(f"  [bold red][BUY FAIL][/] spent $0.00/${budget:.2f}")
    return 0.0, 0.0


# ------------------------- SELL (for hedge only) -------------------------

def sell_market_with_retry(
    token_id,
    size,
    price_limit,
    tick_size="0.01",
    max_retries=3,
    min_price=None,
    undercut_ticks=0,
    retry_sleep_s=0.05,
    refresh_quote=True,
    abort_above=None,
    require_ask_max=None,
    max_spread=None,
):
    """Sell `size` shares via FAK. Used for hedge exits only — no max_price cap.

    Each retry force-REST refreshes the full book and re-runs two-sided integrity
    when require_ask_max/max_spread are set. Incomplete REST fails closed (no WS
    fallback). `price` is the worst (lowest) price we will accept.

    Returns (total_sold, result, proceeds) where proceeds = sum(filled * price).
    """
    total_sold = 0.0
    total_proceeds = 0.0
    remaining = float(size)
    floor = float(min_price) if min_price is not None else float(tick_size)
    last_limit = float(price_limit) if price_limit is not None else floor
    if DRY_RUN:
        price = hedge_sell_price(price_limit, tick_size, undercut_ticks, floor)
        console.print(f"  [bold black on yellow][DRY SELL][/] would SELL {remaining:.4f} {str(token_id)[:12]}… @ ≥{price:.3f}")
        log_event("dry_sell", token_id=token_id, size=remaining, price_limit=price)
        return 0, None, 0.0
    for attempt in range(max_retries):
        if remaining < 0.01:
            break
        live_bid = price_limit
        live_ask = None
        if refresh_quote:
            qb, _, qa, _, _ = get_quote_fast(
                token_id, max_age_s=0.0, prefer_rest=True, force_rest=True,
            )
            if qb is None or qa is None:
                log_event(
                    "hedge_retry_incomplete_rest",
                    token_id=token_id,
                    live_bid=qb,
                    live_ask=qa,
                    attempt=attempt + 1,
                    sold_so_far=total_sold,
                )
                console.print("  [dim][CANCEL][/] hedge retry abort — incomplete REST book")
                break
            live_bid, live_ask = qb, qa
            if require_ask_max is not None and max_spread is not None and abort_above is not None:
                ok, why = hedge_book_ok(
                    live_bid, live_ask, abort_above, max_spread, require_ask_max,
                )
                if not ok:
                    log_event(
                        "hedge_retry_abort_integrity",
                        token_id=token_id,
                        live_bid=live_bid,
                        live_ask=live_ask,
                        reason=why,
                        attempt=attempt + 1,
                        sold_so_far=total_sold,
                    )
                    console.print(
                        f"  [dim][CANCEL][/] hedge retry abort — book integrity ({why}) "
                        f"bid={live_bid:.3f} ask={live_ask:.3f}"
                    )
                    break
        if abort_above is not None and live_bid is not None and live_bid > float(abort_above):
            log_event(
                "hedge_retry_abort_bounce",
                token_id=token_id,
                live_bid=live_bid,
                live_ask=live_ask,
                abort_above=abort_above,
                attempt=attempt + 1,
                sold_so_far=total_sold,
            )
            console.print(
                f"  [dim][CANCEL][/] hedge retry abort — bid recovered to {live_bid:.3f} > {float(abort_above):.3f}"
            )
            break
        price = hedge_sell_price(live_bid, tick_size, undercut_ticks, floor)
        last_limit = price
        try:
            result = safe_api_call(
                client.create_and_post_market_order,
                MarketOrderArgs(token_id=token_id, amount=remaining, side=SELL, price=price),
                options=PartialCreateOrderOptions(tick_size=tick_size, neg_risk=False),
                order_type=OrderType.FAK,
            )
            if result:
                oid = extract_order_id(result)
                filled = float(confirm_fill_size(result, oid, remaining))
                if filled <= 0:
                    console.print("  [dim yellow][FAK NULL][/] 0 confirmed fill · stopping")
                    break
                fills_px = fill_proceeds(result, filled, price)
                total_sold += filled
                total_proceeds += fills_px
                remaining -= filled
                console.print(f"  [bold green][EXIT FAK][/]{filled} @ ≥{price:.3f}  [dim]id={str(oid)[:16]}…[/]")
                log_event("sell_fill", token_id=token_id, filled=filled, price=price, remaining=remaining, attempt=attempt + 1)
                if remaining < 0.01:
                    return total_sold, result, total_proceeds
        except Exception as e:
            console.print(f"  [dim red]Market sell {attempt+1}/{max_retries} failed: {e}[/]")
        time.sleep(float(retry_sleep_s))

    if total_sold > 0:
        return total_sold, {"partial": True, "sold": total_sold, "last_limit": last_limit}, total_proceeds
    console.print(f"  [bold red][EXIT FAIL][/] market sell 0/{size:.4f} cleared")
    return 0, None, 0.0


# ------------------------- REDEEM -------------------------

_redeem_permanent_failures = set()


def get_relayer_headers():
    relayer_headers = {
        "Content-Type": "application/json",
        "RELAYER_API_KEY": RELAYER_API_KEY or "",
        "RELAYER_API_KEY_ADDRESS": RELAYER_API_KEY_ADDRESS or "",
    }
    return RELAYER_URL, relayer_headers


def submit_proxy_tx(target, data, tx_type="PROXY"):
    from eth_account import Account
    relayer_url, relayer_headers = get_relayer_headers()
    eoa = Account.from_key(PRIVATE_KEY).address
    nonce_r = requests.get(
        f"{relayer_url}/nonce",
        params={"address": eoa, "type": tx_type},
        headers=relayer_headers,
        timeout=10,
    )
    if nonce_r.status_code != 200:
        return None, f"nonce fetch fail HTTP {nonce_r.status_code}"
    body = {
        "type": tx_type,
        "from": eoa,
        "to": target,
        "nonce": nonce_r.json().get("nonce", "0"),
        "data": "0x" + data.hex(),
        "value": "0",
    }
    submit_r = requests.post(
        f"{relayer_url}/submit",
        json=body,
        headers=relayer_headers,
        timeout=10,
    )
    if submit_r.status_code == 200:
        return submit_r.json().get("transactionID") or "?", None
    return None, f"HTTP {submit_r.status_code} · {submit_r.text[:80]}"


def redeem_condition(condition_id, label=""):
    if DRY_RUN:
        console.print(f"  [bold black on yellow][DRY SETTLE][/] would redeem · {label}")
        log_event("dry_redeem", condition_id=condition_id, label=label)
        return None
    try:
        from eth_abi import encode
        from eth_utils import keccak, to_checksum_address

        pUSD = to_checksum_address(PUSD)
        CTF_CONTRACT = to_checksum_address(CTF)
        proxy = to_checksum_address(FUNDER_ADDRESS)

        redeem_sel = keccak(b"redeemPositions(address,bytes32,bytes32,uint256[])")[:4]
        redeem_data = redeem_sel + encode(
            ["address", "bytes32", "bytes32", "uint256[]"],
            [pUSD, bytes(32), bytes.fromhex(condition_id.lower().removeprefix("0x")), [1, 2]],
        )

        execute_sel = keccak(b"execute(address,uint256,bytes)")[:4]
        proxy_data = execute_sel + encode(
            ["address", "uint256", "bytes"],
            [CTF_CONTRACT, 0, redeem_data],
        )

        tx_id, err = submit_proxy_tx(proxy, proxy_data)
        if tx_id:
            console.print(f"  [bold bright_green][SETTLE ▶][/] {label}  [dim]tx={str(tx_id)[:18]}…[/]")
            return tx_id
        console.print(f"  [dim red][SETTLE FAIL][/] {label}  [dim]{err}[/]")
        if err and ("proxyWallet" in err or "invalid" in err.lower()):
            _redeem_permanent_failures.add(condition_id)
            console.print(f"  [dim red][SETTLE SKIP][/] {label}  [dim]permanent failure — will not retry[/]")
        return None
    except Exception as e:
        console.print(f"  [dim red][SETTLE ERR][/] {label}  [dim]{e}[/]")
        return None


# ------------------------- CLIENT SETUP -------------------------

if API_KEY and API_SECRET and API_PASSPHRASE:
    api_creds = ApiCreds(api_key=API_KEY, api_secret=API_SECRET, api_passphrase=API_PASSPHRASE)
    console.print("[bold bright_cyan]▶ AUTH[/] [dim]pre-generated API credentials loaded[/]")
else:
    temp_client = ClobClient(host=HOST, key=PRIVATE_KEY, chain_id=CHAIN_ID)
    api_creds = temp_client.create_or_derive_api_key()
    console.print("[bold bright_cyan]▶ AUTH[/] [dim]API credentials derived from private key[/]")

client = ClobClient(
    host=HOST,
    key=PRIVATE_KEY,
    chain_id=CHAIN_ID,
    creds=api_creds,
    signature_type=1,
    funder=FUNDER_ADDRESS,
)

try:
    from py_clob_client_v2.client import BalanceAllowanceParams
    client.update_balance_allowance(BalanceAllowanceParams(asset_type="COLLATERAL"))
    console.print("[bold bright_green]▶ COLLATERAL[/] [dim]allowance synced · USDC.e armed[/]")
except Exception as e:
    console.print(f"[bold red]▶ COLLATERAL [WARN][/] [dim]{e}[/]")

market_gateway = MarketGateway(gamma_url=GAMMA_API, data_api_url=DATA_API, discover_cache_s=5.0)
btc_feed = get_btc_feed(UNDERLYING_SOURCE, PTB_STORE_FILE)
book_ws = get_book_feed()
console.print("[bold bright_cyan]▶ BOOK WS[/] [dim]CLOB market channel armed · held/buy tokens[/]")

banner = Panel(
    Align.center(
        "[bold bright_green]██████╗ ████████╗ ██████╗[/]   [bright_yellow]//[/]  [bold white]BUY DESK[/]\n"
        "[bold bright_green]██╔══██╗╚══██╔══╝██╔════╝[/]   [bright_yellow]//[/]  [dim]POLYMARKET CLOB · MATIC[/]\n"
        "[bold bright_green]██████╔╝   ██║   ██║     [/]   [bright_yellow]//[/]  [dim]BUY-SIDE · 97¢ WINNER TRIG[/]\n"
        "[bold bright_green]██╔══██╗   ██║   ██║     [/]   [bright_yellow]//[/]  [dim]BTC 15M · HEDGE @ 65¢[/]\n"
        "[bold bright_green]██████╔╝   ██║   ╚██████╗[/]   [bright_yellow]//[/]  STATUS: [bold bright_green]● ARMED[/]\n"
        "[bold bright_green]╚═════╝    ╚═╝    ╚═════╝[/]   [bright_yellow]//[/]  [dim]v1.0 · buy · gamma discovery[/]",
        vertical="middle",
    ),
    title="[bold bright_yellow]▰▱▰▱  BUY SYSTEM ONLINE  ▱▰▱▰[/]",
    subtitle="[dim]press Ctrl-C to disarm[/]",
    border_style="bright_green",
    box=box.HEAVY_EDGE,
    padding=(1, 4),
)
console.print(banner)
if DRY_RUN:
    console.print("[bold black on yellow]▶ DRY RUN[/] [dim]no real orders will be placed[/]")

# ------------------------- SHUTDOWN -------------------------

_shutdown_requested = False


def _handle_shutdown(signum, frame):
    global _shutdown_requested
    _shutdown_requested = True
    sig_name = signal.Signals(signum).name
    console.print(f"\n[bold yellow]▶ {sig_name} received — finishing current cycle then exiting[/]")


signal.signal(signal.SIGINT, _handle_shutdown)
signal.signal(signal.SIGTERM, _handle_shutdown)

# ------------------------- MAIN LOOP -------------------------

positions_meta = load_json(STATE_FILE)
CYCLE = 0
_last_positions_refresh = 0.0
_last_balance_refresh = 0.0
_cached_positions = {}
_cached_markets = []
_book_executor = ThreadPoolExecutor(max_workers=4)
_pending_book_futs = {}


def _today_start_ms():
    now = datetime.fromtimestamp(time.time(), tz=timezone.utc)
    return datetime(now.year, now.month, now.day, tzinfo=timezone.utc).timestamp() * 1000


while not _shutdown_requested:
    try:
        CYCLE += 1
        now_ms = time.time() * 1000
        now_s = time.time()
        now_str = datetime.now().strftime("%H:%M:%S")
        _show_ui = True
        markets = [m for m in _cached_markets if m.active and not m.closed and not m.neg_risk]
        held = _cached_positions

        # Hot-reload strategy
        _strat = load_strategy()
        BUY_THRESHOLD = _strat["buy_threshold"]
        BUY_MAX_PRICE = _strat["buy_max_price"]
        MIN_WINNER_BID = _strat["min_winner_bid"]
        MAX_LOSER_BID = _strat["max_loser_bid"]
        MIN_BID_EDGE = _strat["min_bid_edge"]
        UNDERLYING_GATE_ENABLED = _strat["underlying_gate_enabled"]
        MIN_UNDERLYING_EDGE_USD = _strat["min_underlying_edge_usd"]
        HEDGE_ENABLED = _strat["hedge_enabled"]
        HEDGE_THRESHOLD = _strat["hedge_threshold"]
        HEDGE_MIN_PRICE = _strat["hedge_min_price"]
        HEDGE_UNDERCUT_TICKS = _strat["hedge_undercut_ticks"]
        HEDGE_QUOTE_MAX_AGE_S = _strat["hedge_quote_max_age_s"]
        HEDGE_RETRY_SLEEP_S = _strat["hedge_retry_sleep_s"]
        HEDGE_GHOST_SLEEP_S = _strat["hedge_ghost_sleep_s"]
        MAX_ENTRY_SPREAD = _strat["max_entry_spread"]
        HEDGE_MAX_SPREAD = _strat["hedge_max_spread"]
        HEDGE_REQUIRE_ASK_MAX = _strat["hedge_require_ask_max"]
        BUY_WINDOW_MIN = _strat["buy_window_min"]
        BUY_GRACE_S = _strat["buy_grace_s"]
        BUY_COOLDOWN_S = _strat["buy_cooldown_s"]
        BUY_BUDGET = _strat["buy_budget"]
        MAX_OPEN_POSITIONS = _strat["max_open_positions"]
        MAX_OPEN_NOTIONAL = _strat["max_open_notional"]
        MAX_DAILY_NOTIONAL = _strat["max_daily_notional"]
        ONE_ENTRY_PER_MARKET = _strat["one_entry_per_market"]
        REDEEM_THROTTLE_S = _strat["redeem_throttle_s"]
        MAX_REDEEM_AGE_DAYS = _strat["max_redeem_age_days"]
        DRY_RUN = _strat["dry_run"]
        POLL_BUY_WINDOW_S = _strat["poll_buy_window_s"]
        POLL_HELD_S = _strat["poll_held_s"]
        POSITIONS_REFRESH_S = _strat["positions_refresh_s"]
        BALANCE_REFRESH_S = _strat["balance_refresh_s"]
        UI_EVERY_N_CYCLES = _strat["ui_every_n_cycles"]
        TICK_SIZE_FALLBACK = _strat["tick_size"]

        write_heartbeat()

        _now_f = time.time()
        if _now_f - _last_balance_refresh >= BALANCE_REFRESH_S:
            pusd_bal = get_balance()
            _last_balance_refresh = _now_f

        if _now_f - _last_positions_refresh >= POSITIONS_REFRESH_S:
            positions_raw = get_user_positions()
            _cached_positions = build_held_positions(positions_raw) if positions_raw is not None else _cached_positions
            _last_positions_refresh = _now_f
        else:
            positions_raw = None

        # Discover markets (cached 25s inside MarketGateway)
        try:
            _cached_markets = market_gateway.discover([SERIES_SLUG])
        except Exception as e:
            log_event("discover_fail", error=str(e)[:200])
        markets = [m for m in _cached_markets if m.active and not m.closed and not m.neg_risk]
        held = _cached_positions
        _open_pos_n = sum(
            1 for p in held.values()
            if max(p.get("up", {}).get("size", 0), p.get("dn", {}).get("size", 0)) > 0.01
        )
        _min_ttm_now = min((m.end_ts * 1000 - now_ms) / 60000 for m in markets) if markets else 999
        _hot_mode = _open_pos_n > 0 or _min_ttm_now <= BUY_WINDOW_MIN
        _show_ui = (not _hot_mode) or (CYCLE % max(1, int(UI_EVERY_N_CYCLES)) == 0)

        if _show_ui:
            console.rule(
                f"[bold bright_yellow]▲ TICK #{CYCLE:04d}[/] [dim]·[/] [bright_white]{now_str}[/] [dim]·[/] "
                f"[bright_green]MKT[/] [bold]{len(markets):>2}[/] [dim]·[/] "
                f"[bright_cyan]POS[/] [bold]{_open_pos_n:>2}[/] [dim]·[/] "
                f"[bright_yellow]NAV[/] [bold]${pusd_bal:>7.2f}[/] [dim]▲[/]",
                style="bright_yellow",
            )

        # ================= GC META CACHE =================
        if positions_raw is not None:
            live_conds = {
                cond for cond, p in held.items()
                if max(p.get("up", {}).get("size", 0), p.get("dn", {}).get("size", 0)) > 0.01
                or p.get("up", {}).get("redeemable") or p.get("dn", {}).get("redeemable")
            }
            live_conds |= {m.condition_id for m in markets}
            stale_conds = [c for c in list(positions_meta.keys()) if c not in live_conds]
            for c in stale_conds:
                gc_meta = positions_meta[c]
                if gc_meta.get("bought_token"):
                    entry_cost = gc_meta.get("pnl_entry_cost", 0)
                    hedge_proceeds = gc_meta.get("pnl_hedge_proceeds", 0)
                    redeem_value = gc_meta.get("pnl_redeem_value", 0)
                    if redeem_value == 0 and not gc_meta.get("hedge_closed"):
                        # Only assume par redemption when we still held to resolution.
                        rem = gc_meta.get("bought_size", 0)
                        if rem > 0:
                            redeem_value = round(rem, 4)
                    outcome = "hedge" if hedge_proceeds > 0 else ("win" if redeem_value > 0 else "loss")
                    net = record_pnl(c, gc_meta.get("question", "?"), entry_cost, redeem_value, hedge_proceeds, outcome)
                    log_event("pnl_recorded", condition_id=c, entry=entry_cost, hedge=hedge_proceeds, redeem=redeem_value, net=round(net, 4), outcome=outcome)
                    append_research(RESEARCH_FILE, {
                        "event": "resolved",
                        "condition_id": c,
                        "slug": gc_meta.get("slug"),
                        "question": gc_meta.get("question"),
                        "start_ts": gc_meta.get("start_ts"),
                        "end_ts": gc_meta.get("end_ts"),
                        "bought_leg": gc_meta.get("bought_leg"),
                        "fill_price": gc_meta.get("fill_price"),
                        "ptb": gc_meta.get("ptb"),
                        "ptb_source": gc_meta.get("ptb_source"),
                        "entry_live_btc": gc_meta.get("entry_live_btc"),
                        "entry_edge_usd": gc_meta.get("entry_edge_usd"),
                        "outcome": outcome,
                        "entry_cost": entry_cost,
                        "hedge_proceeds": hedge_proceeds,
                        "redeem_value": redeem_value,
                        "net": round(net, 4),
                    })
                del positions_meta[c]
            if stale_conds:
                log_event("gc", stale_conditions=stale_conds)
                save_json(STATE_FILE, positions_meta)

        # ================= POSITIONS TABLE =================
        if held and _show_ui:
            table = Table(
                title="[bold bright_cyan]≡ HELD POSITIONS ≡[/]  [dim]BTC 15M · BUY-SIDE[/]",
                box=box.HEAVY_HEAD,
                border_style="bright_blue",
                title_style="bold bright_cyan",
                show_lines=True,
            )
            table.add_column("INSTRUMENT", style="white", max_width=40)
            table.add_column("TTM", justify="right")
            table.add_column("LEG", justify="center")
            table.add_column("SIZE", justify="right")
            table.add_column("ENTRY", justify="right")
            table.add_column("STATE", justify="center")

            for cond, pos in held.items():
                try:
                    up_p = pos.get("up", {})
                    dn_p = pos.get("dn", {})
                    up_sz = up_p.get("size", 0)
                    dn_sz = dn_p.get("size", 0)
                    held_size = max(up_sz, dn_sz)
                    if held_size < 0.01 and not up_p.get("redeemable") and not dn_p.get("redeemable"):
                        continue
                    meta = positions_meta.get(cond, {})
                    leg = "up" if up_sz > 0.01 else "down" if dn_sz > 0.01 else "—"
                    entry = meta.get("fill_price", 0)
                    # Find end_ts from discovered markets or meta
                    m = next((m for m in markets if m.condition_id == cond), None)
                    if m:
                        mins = (m.end_ts * 1000 - now_ms) / 60000
                    else:
                        end_date = meta.get("end_date", "")
                        mins = -1
                        if end_date:
                            try:
                                end_dt = datetime.fromisoformat(end_date.replace("Z", "+00:00"))
                                mins = (end_dt.timestamp() * 1000 - now_ms) / 60000
                            except Exception:
                                pass

                    if up_p.get("redeemable") or dn_p.get("redeemable"):
                        state = "[bold bright_magenta]✓ REDEEM[/]"
                    elif mins <= 0:
                        state = "[dim]· closed[/]"
                    elif mins <= BUY_WINDOW_MIN:
                        state = f"[bold yellow]● HELD · HEDGE ≤{int(HEDGE_THRESHOLD*100)}¢[/]"
                    else:
                        state = "[bold bright_green]● HELD[/]"

                    if mins < 1:
                        ttm_str = f"{max(mins*60, 0):.0f}s"
                    elif mins > 0:
                        ttm_str = f"{mins:.0f}m"
                    else:
                        ttm_str = "—"

                    table.add_row(
                        (meta.get("question", "?"))[:40],
                        ttm_str,
                        leg.upper(),
                        f"{held_size:.2f}",
                        f"{entry:.3f}" if entry else "—",
                        state,
                    )
                except Exception:
                    continue
            console.print(table)

        # ================= REDEEM PHASE =================
        for cond, pos in held.items():
            up_redeemable = pos.get("up", {}).get("redeemable", False)
            dn_redeemable = pos.get("dn", {}).get("redeemable", False)
            if not (up_redeemable or dn_redeemable):
                continue
            if cond in _redeem_permanent_failures:
                continue
            meta = positions_meta.setdefault(cond, {})
            last = meta.get("redeem_submitted_at") or 0
            if now_ms - last < REDEEM_THROTTLE_S * 1000:
                continue
            # Check max redeem age
            m = next((m for m in markets if m.condition_id == cond), None)
            if m:
                end_ts_ms = m.end_ts * 1000
            else:
                end_date = meta.get("end_date", "")
                end_ts_ms = now_ms
                if end_date:
                    try:
                        end_dt = datetime.fromisoformat(end_date.replace("Z", "+00:00"))
                        end_ts_ms = end_dt.timestamp() * 1000
                    except Exception:
                        pass
            if now_ms - end_ts_ms > MAX_REDEEM_AGE_DAYS * 86400 * 1000:
                continue
            tx = redeem_condition(cond, label=(meta.get("question", "?"))[:32])
            if tx:
                meta["redeem_submitted_at"] = now_ms
                held_size = max(pos.get("up", {}).get("size", 0), pos.get("dn", {}).get("size", 0))
                meta["pnl_redeem_value"] = round(held_size, 4)
                log_event("redeem_submit", condition_id=cond, tx_id=str(tx))
                save_json(STATE_FILE, positions_meta)

        # ================= COLLECT PRE-FETCHED BOOKS =================
        _book_cache = {}
        for _f, _t in list(_pending_book_futs.items()):
            try:
                _book_cache[_t] = _f.result(timeout=1)
            except Exception:
                _book_cache[_t] = (None, 0.0, None, 0.0, None)
                _f.cancel()
        _pending_book_futs = {}
        # Overlay fresher WS top-of-book (held + buy-window tokens).
        for _t in list(_book_cache.keys()):
            _wq = book_ws.quote(_t, max_age_s=2.0)
            if _wq is not None and (_wq[0] is not None or _wq[2] is not None):
                _book_cache[_t] = _wq

        # ================= HEDGE + BUY PHASE =================
        for m in markets:
            end_ts_ms = m.end_ts * 1000
            minutes_left = (end_ts_ms - now_ms) / 60000
            if minutes_left <= 0:
                continue

            cond = m.condition_id
            pos = held.get(cond, {})
            up_size = pos.get("up", {}).get("size", 0)
            dn_size = pos.get("dn", {}).get("size", 0)
            held_size = max(up_size, dn_size)
            held_token = m.up_token if up_size > 0.01 else (m.dn_token if dn_size > 0.01 else None)
            held_leg = "up" if up_size > 0.01 else ("down" if dn_size > 0.01 else None)

            meta = positions_meta.setdefault(cond, {})

            # --- HEDGE CHECK (for held positions) ---
            if held_token and held_size > 0.01 and HEDGE_ENABLED and not meta.get("hedge_closed"):
                # Fast WS/cache peek only arms the path; REST + book integrity decide.
                cached_quote = _book_cache.get(held_token)
                if cached_quote is None:
                    cached_quote = get_quote_fast(held_token, max_age_s=2.0)
                    _book_cache[held_token] = cached_quote
                cached_bid = cached_quote[0]
                if cached_bid is not None and cached_bid <= HEDGE_THRESHOLD:
                    quote_age = book_ws.quote_age(held_token)
                    # Force-fresh REST full book. Incomplete REST fails closed —
                    # never substitute WS/cache for a missing side.
                    fresh_bid, _, fresh_ask, _, fresh_mid = get_quote_fast(
                        held_token, max_age_s=0.0, prefer_rest=True, force_rest=True,
                    )
                    if fresh_bid is None or fresh_ask is None:
                        log_event(
                            "hedge_skip_incomplete_rest", condition_id=cond, leg=held_leg,
                            trigger_bid=cached_bid, current_bid=fresh_bid, current_ask=fresh_ask,
                            current_mid=fresh_mid,
                        )
                    elif fresh_bid > HEDGE_THRESHOLD:
                        log_event(
                            "hedge_cancel_bounce", condition_id=cond, leg=held_leg,
                            trigger_bid=cached_bid, current_bid=fresh_bid,
                            current_ask=fresh_ask, current_mid=fresh_mid,
                            threshold=HEDGE_THRESHOLD,
                        )
                        console.print(
                            f"  [dim][CANCEL][/] {held_leg.upper()} hedge cancelled — bid bounced {cached_bid:.3f} → {fresh_bid:.3f}"
                        )
                    else:
                        hedge_bid = fresh_bid
                        hedge_ask = fresh_ask
                        ok, why = hedge_book_ok(
                            hedge_bid, hedge_ask, HEDGE_THRESHOLD, HEDGE_MAX_SPREAD, HEDGE_REQUIRE_ASK_MAX,
                        )
                        if not ok:
                            log_event(
                                "hedge_skip_toxic_book", condition_id=cond, leg=held_leg,
                                bid=hedge_bid, ask=hedge_ask, mid=fresh_mid, reason=why,
                                threshold=HEDGE_THRESHOLD, max_spread=HEDGE_MAX_SPREAD,
                                require_ask_max=HEDGE_REQUIRE_ASK_MAX,
                                trigger_bid=cached_bid,
                            )
                        elif hedge_bid <= HEDGE_THRESHOLD:
                            hedge_tick = get_tick_size_cached(held_token)
                            sell_floor = hedge_sell_price(
                                hedge_bid, hedge_tick, HEDGE_UNDERCUT_TICKS, HEDGE_MIN_PRICE,
                            )
                            console.print(Panel(
                                f"  [bright_white]{m.question}[/]\n"
                                f"  [bright_red]REVERSAL DETECTED[/] — {held_leg.upper()} "
                                f"bid [bold]{hedge_bid:.3f}[/] ask [bold]{(hedge_ask or 0):.3f}[/]  ·  "
                                f"FAK ≥{sell_floor:.3f}  ·  [bold red]TTM {minutes_left:>4.1f}m[/]",
                                title="[bold bright_red]▼ HEDGE SELL — CUTTING LOSSES[/]",
                                border_style="bright_red",
                                box=box.HEAVY,
                            ))
                            log_event(
                                "hedge_attempt", condition_id=cond, leg=held_leg, size=held_size,
                                bid=hedge_bid, ask=hedge_ask, mid=fresh_mid, price_limit=sell_floor,
                                quote_age_s=None if quote_age is None else round(quote_age, 3),
                                ws_fast_path=False,
                            )
                            sold, sell_res, hedge_proceeds = sell_market_with_retry(
                                held_token,
                                held_size,
                                hedge_bid,
                                tick_size=hedge_tick,
                                min_price=HEDGE_MIN_PRICE,
                                undercut_ticks=HEDGE_UNDERCUT_TICKS,
                                retry_sleep_s=HEDGE_RETRY_SLEEP_S,
                                abort_above=HEDGE_THRESHOLD,
                                require_ask_max=HEDGE_REQUIRE_ASK_MAX,
                                max_spread=HEDGE_MAX_SPREAD,
                            )
                            if sold > 0:
                                fill_px = (hedge_proceeds / sold) if sold > 0 else sell_floor
                                meta["pnl_hedge_proceeds"] = round(meta.get("pnl_hedge_proceeds", 0) + hedge_proceeds, 4)
                                remainder = max(0.0, held_size - sold)
                                leg_key = "up" if held_leg == "up" else "dn"
                                if remainder < 0.01:
                                    meta["hedge_closed"] = True
                                    remainder = 0.0
                                meta["bought_size"] = remainder
                                if cond in _cached_positions and leg_key in _cached_positions[cond]:
                                    _cached_positions[cond][leg_key]["size"] = remainder
                                log_event(
                                    "hedge_fill", condition_id=cond, leg=held_leg, sold=sold,
                                    remaining=remainder, price=fill_px, proceeds=round(hedge_proceeds, 4),
                                    mid=fresh_mid, ask=hedge_ask, hedge_closed=bool(meta.get("hedge_closed")),
                                )
                                notify(
                                    "HEDGE FIRED" if remainder < 0.01 else "HEDGE PARTIAL",
                                    f"Reversal on {m.question}\nSold {held_leg.upper()} at ~{fill_px:.3f} "
                                    f"({sold:.2f} shares, rem {remainder:.2f})",
                                    priority="urgent",
                                )
                                save_json(STATE_FILE, positions_meta)
                            else:
                                time.sleep(HEDGE_GHOST_SLEEP_S)
                                actual_bal = check_token_balance(held_token)
                                if actual_bal is not None and actual_bal < held_size - 0.01:
                                    ghost_sold = held_size - actual_bal
                                    ghost_px = sell_floor
                                    if isinstance(sell_res, dict) and sell_res.get("last_limit") is not None:
                                        ghost_px = float(sell_res["last_limit"])
                                    ghost_proceeds = ghost_sold * ghost_px
                                    meta["pnl_hedge_proceeds"] = round(meta.get("pnl_hedge_proceeds", 0) + ghost_proceeds, 4)
                                    rem = float(actual_bal)
                                    if rem < 0.01:
                                        meta["hedge_closed"] = True
                                        rem = 0.0
                                    meta["bought_size"] = rem
                                    log_event(
                                        "hedge_ghost_fill", condition_id=cond, leg=held_leg,
                                        sold=ghost_sold, remaining=rem, price=sell_floor, mid=fresh_mid,
                                        hedge_closed=bool(meta.get("hedge_closed")),
                                    )
                                    notify(
                                        "HEDGE FIRED (ghost)" if rem < 0.01 else "HEDGE PARTIAL (ghost)",
                                        f"Reversal on {m.question}\n{held_leg.upper()} hedge ghost: "
                                        f"{ghost_sold:.2f} sold, rem {rem:.2f}",
                                        priority="urgent",
                                    )
                                    console.print(
                                        f"  [bold yellow][GHOST FILL][/] {held_leg.upper()} hedge confirmed: "
                                        f"{ghost_sold:.4f} sold · rem {rem:.4f}"
                                    )
                                    if cond in _cached_positions:
                                        leg_key = "up" if held_leg == "up" else "dn"
                                        if leg_key in _cached_positions[cond]:
                                            _cached_positions[cond][leg_key]["size"] = rem
                                    save_json(STATE_FILE, positions_meta)
                                else:
                                    log_event(
                                        "hedge_fail", condition_id=cond, leg=held_leg, size=held_size,
                                        bid=hedge_bid, ask=hedge_ask, mid=fresh_mid,
                                    )

            # --- BUY CHECK (for markets we don't hold) ---
            if held_size > 0.01:
                continue  # already hold this market
            if minutes_left > BUY_WINDOW_MIN:
                continue  # not in buy window yet

            # One entry per market
            if ONE_ENTRY_PER_MARKET and meta.get("bought_token"):
                continue

            # Initialize meta for first sighting
            if "entered_at" not in meta:
                meta["entered_at"] = now_ms
                meta["up_token"] = m.up_token
                meta["dn_token"] = m.dn_token
                meta["question"] = m.question
                meta["end_date"] = datetime.fromtimestamp(m.end_ts, tz=datetime.now().astimezone().tzinfo).isoformat()
                meta["pnl_entry_cost"] = 0.0
                meta["pnl_hedge_proceeds"] = 0.0
                meta["pnl_redeem_value"] = 0.0
                save_json(STATE_FILE, positions_meta)
            if now_ms - meta["entered_at"] < BUY_GRACE_S * 1000:
                continue

            # Notional caps (in dollars, not shares)
            open_count = sum(
                1 for c, p in held.items()
                if max(p.get("up", {}).get("size", 0), p.get("dn", {}).get("size", 0)) > 0.01
            )
            open_notional = sum(
                pm.get("pnl_entry_cost", 0)
                for c, pm in positions_meta.items()
                if pm.get("bought_token")
            )
            daily_notional = sum(
                pm.get("pnl_entry_cost", 0)
                for c, pm in positions_meta.items()
                if pm.get("bought_token") and pm.get("entered_at", 0) >= _today_start_ms()
            )
            est_cost = BUY_BUDGET
            if open_count >= MAX_OPEN_POSITIONS:
                continue
            if open_notional + est_cost > MAX_OPEN_NOTIONAL + 1e-9:
                continue
            if daily_notional + est_cost > MAX_DAILY_NOTIONAL + 1e-9:
                continue

            # Fresh REST book + last trade in parallel.
            # Entry decisions must not trust WS alone (stale/phantom sizes).
            # GUI display = mid if spread ≤ 10¢ else last trade.
            fut_up = _book_executor.submit(
                get_quote_fast, m.up_token, 2.0, True, True,
            )  # prefer_rest, force_rest — no 200ms cache at entry gate
            fut_dn = _book_executor.submit(
                get_quote_fast, m.dn_token, 2.0, True, True,
            )
            fut_ul = _book_executor.submit(get_last_trade_price, m.up_token)
            fut_dl = _book_executor.submit(get_last_trade_price, m.dn_token)
            up_bid, _, up_ask, _, up_mid = fut_up.result()
            dn_bid, _, dn_ask, _, dn_mid = fut_dn.result()
            up_last = fut_ul.result()
            dn_last = fut_dl.result()
            up_gui = polymarket_display_price(up_bid, up_ask, up_last)
            dn_gui = polymarket_display_price(dn_bid, dn_ask, dn_last)

            # Lock Chainlink PTB as soon as the window is open (memory/disk only).
            if m.start_ts and time.time() >= m.start_ts:
                ptb_rec = btc_feed.capture_ptb(m.start_ts)
                if ptb_rec and ptb_rec.get("ok") and not meta.get("ptb"):
                    meta["ptb"] = ptb_rec.get("ptb")
                    meta["ptb_source"] = ptb_rec.get("source")
                    meta["ptb_tick_ts_ms"] = ptb_rec.get("ptb_tick_ts_ms")
                    meta["start_ts"] = m.start_ts
                    meta["end_ts"] = m.end_ts
                    meta["slug"] = getattr(m, "slug", None)
                    append_research(RESEARCH_FILE, {
                        "event": "ptb_capture",
                        "condition_id": cond,
                        "slug": meta.get("slug"),
                        "question": m.question,
                        "start_ts": m.start_ts,
                        "end_ts": m.end_ts,
                        **{k: ptb_rec.get(k) for k in ("ptb", "source", "ptb_tick_ts_ms", "ptb_skew_ms", "ok", "feed", "feed_label", "resolution_url")},
                    })
                    save_json(STATE_FILE, positions_meta)

            if up_gui is None or dn_gui is None:
                log_event(
                    "buy_skip_incomplete_book",
                    condition_id=cond,
                    up_bid=up_bid, dn_bid=dn_bid, up_ask=up_ask, dn_ask=dn_ask,
                    up_last=up_last, dn_last=dn_last, up_gui=up_gui, dn_gui=dn_gui,
                )
                continue

            # Winner by GUI display price (not raw mid — wide spreads poison mid).
            gui_edge = abs(up_gui - dn_gui)
            if gui_edge < MIN_BID_EDGE:
                log_event(
                    "buy_skip_ambiguous",
                    condition_id=cond,
                    up_gui=up_gui, dn_gui=dn_gui, gui_edge=round(gui_edge, 4),
                    up_bid=up_bid, dn_bid=dn_bid, up_ask=up_ask, dn_ask=dn_ask,
                )
                continue

            up_winning = up_gui > dn_gui
            dn_winning = dn_gui > up_gui

            # Ask in band + GUI consensus + tight real book (bid under the ask).
            up_ask_ok = up_ask is not None and BUY_THRESHOLD <= up_ask <= BUY_MAX_PRICE
            dn_ask_ok = dn_ask is not None and BUY_THRESHOLD <= dn_ask <= BUY_MAX_PRICE
            up_book_ok, up_book_why = entry_book_ok(up_bid, up_ask, MAX_ENTRY_SPREAD, MIN_WINNER_BID)
            dn_book_ok, dn_book_why = entry_book_ok(dn_bid, dn_ask, MAX_ENTRY_SPREAD, MIN_WINNER_BID)
            up_consensus = (
                up_gui is not None and dn_gui is not None
                and up_gui >= MIN_WINNER_BID and dn_gui <= MAX_LOSER_BID
                and up_book_ok
            )
            dn_consensus = (
                up_gui is not None and dn_gui is not None
                and dn_gui >= MIN_WINNER_BID and up_gui <= MAX_LOSER_BID
                and dn_book_ok
            )

            if up_winning and up_ask_ok and not up_consensus:
                log_event(
                    "buy_skip_no_consensus",
                    condition_id=cond, leg="up",
                    up_gui=up_gui, dn_gui=dn_gui, up_ask=up_ask,
                    up_bid=up_bid, dn_bid=dn_bid, up_last=up_last, dn_last=dn_last,
                    up_book_ok=up_book_ok, up_book_why=up_book_why,
                    max_entry_spread=MAX_ENTRY_SPREAD,
                    min_winner_bid=MIN_WINNER_BID, max_loser_bid=MAX_LOSER_BID,
                )
            if dn_winning and dn_ask_ok and not dn_consensus:
                log_event(
                    "buy_skip_no_consensus",
                    condition_id=cond, leg="down",
                    up_gui=up_gui, dn_gui=dn_gui, dn_ask=dn_ask,
                    up_bid=up_bid, dn_bid=dn_bid, up_last=up_last, dn_last=dn_last,
                    dn_book_ok=dn_book_ok, dn_book_why=dn_book_why,
                    max_entry_spread=MAX_ENTRY_SPREAD,
                    min_winner_bid=MIN_WINNER_BID, max_loser_bid=MAX_LOSER_BID,
                )

            up_buy = up_winning and up_ask_ok and up_consensus
            dn_buy = dn_winning and dn_ask_ok and dn_consensus

            # Underlying BTC vs Price To Beat. When enabled, always run the check —
            # even if min_edge is 0 (still fail-closed on flat/missing/stale).
            uchk = None
            if UNDERLYING_GATE_ENABLED and (up_buy or dn_buy):
                uchk = btc_feed.underlying_check(m.start_ts, MIN_UNDERLYING_EDGE_USD)
                favored = uchk.get("favored")
                if not uchk.get("ok") or not favored:
                    log_event(
                        "buy_skip_underlying_edge",
                        condition_id=cond,
                        ptb=uchk.get("ptb"),
                        live_btc=uchk.get("live_btc"),
                        edge_usd=None if uchk.get("edge_usd") is None else round(uchk["edge_usd"], 2),
                        min_edge_usd=MIN_UNDERLYING_EDGE_USD,
                        ptb_source=uchk.get("ptb_source"),
                        live_source=uchk.get("live_source"),
                        live_age_s=uchk.get("live_age_s"),
                        reason=uchk.get("reason"),
                        up_gui=up_gui,
                        dn_gui=dn_gui,
                    )
                    append_research(RESEARCH_FILE, {
                        "event": "decision_skip_underlying",
                        "condition_id": cond,
                        "slug": getattr(m, "slug", None),
                        "question": m.question,
                        "start_ts": m.start_ts,
                        "end_ts": m.end_ts,
                        "up_gui": up_gui,
                        "dn_gui": dn_gui,
                        "up_ask": up_ask,
                        "dn_ask": dn_ask,
                        **{k: uchk.get(k) for k in (
                            "ptb", "live_btc", "edge_usd", "favored", "ptb_source",
                            "live_source", "live_age_s", "ptb_skew_ms", "reason", "ok",
                            "feed", "feed_label", "resolution_url",
                        )},
                    })
                    continue
                if favored == "up":
                    dn_buy = False
                else:
                    up_buy = False
                if not (up_buy or dn_buy):
                    log_event(
                        "buy_skip_underlying_side",
                        condition_id=cond,
                        favored=favored,
                        ptb=uchk.get("ptb"),
                        live_btc=uchk.get("live_btc"),
                        edge_usd=round(uchk["edge_usd"], 2),
                        up_gui=up_gui,
                        dn_gui=dn_gui,
                    )
                    append_research(RESEARCH_FILE, {
                        "event": "decision_skip_side",
                        "condition_id": cond,
                        "slug": getattr(m, "slug", None),
                        "question": m.question,
                        "start_ts": m.start_ts,
                        "end_ts": m.end_ts,
                        "up_gui": up_gui,
                        "dn_gui": dn_gui,
                        "book_wanted": "up" if up_winning else "down",
                        **{k: uchk.get(k) for k in (
                            "ptb", "live_btc", "edge_usd", "favored", "ptb_source",
                            "live_source", "live_age_s", "ptb_skew_ms",
                            "feed", "feed_label", "resolution_url",
                        )},
                    })
                    continue

            if not (up_buy or dn_buy):
                continue

            buy_token = m.up_token if up_buy else m.dn_token
            buy_ask = up_ask if up_buy else dn_ask
            buy_leg = "up" if up_buy else "down"
            buy_gui = up_gui if up_buy else dn_gui

            # Cooldown
            last_buy_at = meta.get("last_buy_at") or 0
            if now_ms - last_buy_at < BUY_COOLDOWN_S * 1000:
                continue

            _ttm_disp = f"{minutes_left*60:>3.0f}s" if minutes_left < 1 else f"{minutes_left:>4.1f}m"
            console.print(Panel(
                f"  [bright_white]{m.question}[/]\n"
                f"  [bright_green]UP[/]   gui [bold]{up_gui:.3f}[/]  ask [bold]{up_ask or 0:.3f}[/]   │   "
                f"[bright_red]DN[/]  gui [bold]{dn_gui:.3f}[/]  ask [bold]{dn_ask or 0:.3f}[/]   │   "
                f"[bold green]TTM {_ttm_disp}[/]",
                title=f"[bold bright_green]▲ BUY TRIGGER — {buy_leg.upper()} LEG[/]",
                border_style="bright_green",
                box=box.HEAVY,
            ))

            tick = get_tick_size_cached(buy_token)
            if uchk is None and m.start_ts:
                uchk = btc_feed.underlying_check(m.start_ts, 0)  # snapshot only
            log_event(
                "buy_attempt", condition_id=cond, leg=buy_leg, budget=BUY_BUDGET,
                ask=buy_ask, gui=buy_gui, up_gui=up_gui, dn_gui=dn_gui,
                threshold=BUY_THRESHOLD, minutes_left=round(minutes_left, 2),
                ptb=(uchk or {}).get("ptb"),
                live_btc=(uchk or {}).get("live_btc"),
                edge_usd=(uchk or {}).get("edge_usd"),
                ptb_source=(uchk or {}).get("ptb_source"),
                live_source=(uchk or {}).get("live_source"),
            )
            append_research(RESEARCH_FILE, {
                "event": "buy_attempt",
                "condition_id": cond,
                "slug": getattr(m, "slug", None),
                "question": m.question,
                "start_ts": m.start_ts,
                "end_ts": m.end_ts,
                "leg": buy_leg,
                "ask": buy_ask,
                "gui": buy_gui,
                "up_gui": up_gui,
                "dn_gui": dn_gui,
                "minutes_left": round(minutes_left, 2),
                **{k: (uchk or {}).get(k) for k in (
                    "ptb", "live_btc", "edge_usd", "favored", "ptb_source",
                    "live_source", "live_age_s", "ptb_skew_ms",
                    "feed", "feed_label", "resolution_url",
                )},
            })

            bought, spent = buy_market_with_retry(buy_token, BUY_BUDGET, BUY_MAX_PRICE, tick_size=tick, min_price=BUY_THRESHOLD)
            if bought > 0 and spent > 0:
                avg_fill = spent / bought
                meta["last_buy_at"] = now_ms
                meta["bought_token"] = buy_token
                meta["bought_leg"] = buy_leg
                meta["bought_size"] = bought
                meta["fill_price"] = round(avg_fill, 4)
                meta["pnl_entry_cost"] = round(spent, 4)
                meta["toxic_fill"] = bool(avg_fill + 1e-9 < float(BUY_THRESHOLD))
                if uchk:
                    meta["ptb"] = uchk.get("ptb")
                    meta["ptb_source"] = uchk.get("ptb_source")
                    meta["entry_live_btc"] = uchk.get("live_btc")
                    meta["entry_edge_usd"] = uchk.get("edge_usd")
                log_event(
                    "buy_success", condition_id=cond, leg=buy_leg, bought=bought,
                    price=meta["fill_price"], ask_gate=buy_ask, entry_cost=meta["pnl_entry_cost"],
                    ptb=meta.get("ptb"), live_btc=meta.get("entry_live_btc"),
                    edge_usd=meta.get("entry_edge_usd"),
                )
                append_research(RESEARCH_FILE, {
                    "event": "buy_fill",
                    "condition_id": cond,
                    "slug": getattr(m, "slug", None),
                    "question": m.question,
                    "start_ts": m.start_ts,
                    "end_ts": m.end_ts,
                    "leg": buy_leg,
                    "bought": bought,
                    "price": meta["fill_price"],
                    "ask_gate": buy_ask,
                    "entry_cost": meta["pnl_entry_cost"],
                    "ptb": meta.get("ptb"),
                    "live_btc": meta.get("entry_live_btc"),
                    "edge_usd": meta.get("entry_edge_usd"),
                    "ptb_source": meta.get("ptb_source"),
                })
                notify(
                    "BUY FILLED",
                    f"{m.question}\nBought {buy_leg.upper()} at ~{meta['fill_price']:.3f} ({bought:.2f} shares, ${spent:.2f})",
                    priority="high",
                )
                save_json(STATE_FILE, positions_meta)
            else:
                meta["last_buy_at"] = now_ms
                log_event("buy_fail", condition_id=cond, leg=buy_leg, budget=BUY_BUDGET, ask=buy_ask)
                save_json(STATE_FILE, positions_meta)

    except Exception:
        log_event("cycle_error", traceback=traceback.format_exc())
        console.print(Panel(
            traceback.format_exc(),
            title="[bold bright_red]■■  SYSTEM FAULT  ■■[/]",
            subtitle="[dim]auto-restart in 5s · cycle aborted[/]",
            border_style="bright_red",
            box=box.HEAVY_EDGE,
        ))

    # Variable polling: sub-second in buy window OR while holding (hedge path)
    _now = time.time() * 1000
    _min_ttm = min((m.end_ts * 1000 - _now) / 60000 for m in markets) if markets else 999
    _has_held = any(
        max(p.get("up", {}).get("size", 0), p.get("dn", {}).get("size", 0)) > 0.01
        for p in held.values()
    )
    if _has_held:
        _sleep_s = min(POLL_HELD_S, POLL_BUY_WINDOW_S)
    elif _min_ttm <= BUY_WINDOW_MIN:
        _sleep_s = POLL_BUY_WINDOW_S
    else:
        _sleep_s = 1

    # ================= KICK OFF NEXT CYCLE'S BOOK FETCH =================
    _next_now = time.time() * 1000 + _sleep_s * 1000
    _pending_tokens = set(_pending_book_futs.values())
    _MAX_PENDING_BOOKS = 30
    _watch_tokens = set()
    for m in markets:
        _ml = (m.end_ts * 1000 - _next_now) / 60000
        pos = held.get(m.condition_id, {})
        has_pos = max(pos.get("up", {}).get("size", 0), pos.get("dn", {}).get("size", 0)) > 0.01
        in_window = _ml > 0 and _ml <= BUY_WINDOW_MIN + 0.5
        if not (in_window or has_pos):
            continue
        for token in (m.up_token, m.dn_token):
            if not token:
                continue
            _watch_tokens.add(token)
            if has_pos and (
                (pos.get("up", {}).get("size", 0) > 0.01 and token == m.up_token)
                or (pos.get("dn", {}).get("size", 0) > 0.01 and token == m.dn_token)
            ):
                # Always watch the held leg tightly
                _watch_tokens.add(token)
            if token not in _pending_tokens and len(_pending_book_futs) < _MAX_PENDING_BOOKS:
                _pending_book_futs[_book_executor.submit(get_quote_fast, token)] = token
                _pending_tokens.add(token)
    book_ws.set_tokens(_watch_tokens)
    prune_rest_caches(keep_tokens=_watch_tokens)

    if _show_ui:
        console.print(f"[dim bright_black]· · ·  sleeping {_sleep_s}s  · · ·[/]")
    time.sleep(_sleep_s)

# Graceful shutdown
console.print("[bold bright_green]▶ SHUTDOWN COMPLETE[/] [dim]state saved · exiting cleanly[/]")
log_event("shutdown", reason="signal")
sys.exit(0)
