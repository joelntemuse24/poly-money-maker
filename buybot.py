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

console = Console()
load_dotenv()

HOST = "https://clob.polymarket.com"
DATA_API = "https://data-api.polymarket.com"
GAMMA_API = "https://gamma-api.polymarket.com"
CHAIN_ID = 137
STATE_FILE = "positions_buy.json"
PNL_FILE = "pnl_buy.json"
HEARTBEAT_FILE = ".heartbeat_buy"
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
    "buy_threshold": 0.97,
    "buy_max_price": 0.99,
    "hedge_enabled": True,
    "hedge_threshold": 0.65,
    "buy_window_min": 3.0,
    "buy_grace_s": 2,
    "buy_cooldown_s": 3,
    "shares": 13.0,
    "max_open_positions": 100,
    "max_open_notional": 10000.0,
    "max_daily_notional": 999999.0,
    "one_entry_per_market": True,
    "redeem_throttle_s": 30,
    "max_redeem_age_days": 7,
    "dry_run": False,
    "poll_buy_window_s": 0.1,
    "positions_refresh_s": 2,
    "balance_refresh_s": 15,
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
    except Exception as e:
        console.print(f"[bold red]▶ STRATEGY [WARN][/] [dim]failed to load {STRATEGY_FILE}: {e}[/]")
    _strat_cache = cfg
    _strat_mtime = mtime
    return cfg


_strat = load_strategy()
BUY_THRESHOLD = _strat["buy_threshold"]
HEDGE_ENABLED = _strat["hedge_enabled"]
HEDGE_THRESHOLD = _strat["hedge_threshold"]
BUY_WINDOW_MIN = _strat["buy_window_min"]
BUY_GRACE_S = _strat["buy_grace_s"]
BUY_COOLDOWN_S = _strat["buy_cooldown_s"]
SHARES = _strat["shares"]
MAX_OPEN_POSITIONS = _strat["max_open_positions"]
MAX_OPEN_NOTIONAL = _strat["max_open_notional"]
MAX_DAILY_NOTIONAL = _strat["max_daily_notional"]
ONE_ENTRY_PER_MARKET = _strat["one_entry_per_market"]
REDEEM_THROTTLE_S = _strat["redeem_throttle_s"]
MAX_REDEEM_AGE_DAYS = _strat["max_redeem_age_days"]
DRY_RUN = _strat["dry_run"]
POLL_BUY_WINDOW_S = _strat["poll_buy_window_s"]
POSITIONS_REFRESH_S = _strat["positions_refresh_s"]
BALANCE_REFRESH_S = _strat["balance_refresh_s"]
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
    mid_price is None when no asks exist — callers must NOT trigger on None."""
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
        if not bids:
            return None, 0.0, None, 0.0, None
        best_bid = max(bids, key=lambda x: float(x.get("price", 0)))
        bid_price = float(best_bid.get("price", 0))
        bid_size = float(best_bid.get("size", 0))
        if asks:
            best_ask = min(asks, key=lambda x: float(x.get("price", 0)))
            ask_price = float(best_ask.get("price", 0))
            ask_size = float(best_ask.get("size", 0))
            mid_price = (bid_price + ask_price) / 2.0
        else:
            ask_price = None
            ask_size = 0.0
            mid_price = None
        return bid_price, bid_size, ask_price, ask_size, mid_price
    except Exception as e:
        log_event("book_quote_fail", token_id=token_id, error=str(e), path=path)
        return None, 0.0, None, 0.0, None


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


def confirm_fill_size(result, oid, requested):
    matched = 0.0
    if isinstance(result, dict):
        sm = result.get("size_matched")
        if sm:
            matched = float(sm)
    else:
        matched = float(getattr(result, "size_matched", 0) or 0)
    if matched <= 0 and oid:
        details = get_order_details(oid)
        if details:
            if details.get("status") == "NOT_FOUND":
                matched = float(requested)
            else:
                sm = details.get("size_matched", 0)
                matched = float(sm) if sm else 0.0
    return matched


# ------------------------- BUY -------------------------

def buy_market_with_retry(token_id, size, max_price, tick_size="0.01", max_retries=3, min_price=0.0):
    """Buy `size` shares of token_id at or below max_price via FAK market order.
    Caps each attempt at the current best ask size to avoid walking the book."""
    total_bought = 0.0
    remaining = float(size)
    if DRY_RUN:
        console.print(f"  [bold black on yellow][DRY BUY][/] would BUY {remaining:.4f} {str(token_id)[:12]}… @ ≤{max_price:.3f}")
        log_event("dry_buy", token_id=token_id, size=remaining, max_price=max_price)
        return 0.0
    for attempt in range(max_retries):
        if remaining < 0.01:
            break
        _, _, fresh_ask, fresh_ask_size, _ = get_book_quote(token_id)
        if fresh_ask is None:
            console.print(f"  [dim yellow][NO ASK][/] no asks available · attempt {attempt + 1}/{max_retries}")
            break
        if fresh_ask > max_price:
            console.print(f"  [dim yellow][SKIP][/] ask {fresh_ask:.3f} > cap {max_price:.3f} · attempt {attempt + 1}/{max_retries}")
            time.sleep(0.5)
            continue
        if fresh_ask < min_price:
            console.print(f"  [dim yellow][SKIP][/] ask {fresh_ask:.3f} < min {min_price:.3f} · attempt {attempt + 1}/{max_retries}")
            time.sleep(0.5)
            continue
        buy_size = min(remaining, fresh_ask_size)
        price = fresh_ask
        try:
            result = safe_api_call(
                client.create_and_post_market_order,
                MarketOrderArgs(token_id=token_id, amount=buy_size, side=BUY, price=price),
                options=PartialCreateOrderOptions(tick_size=tick_size, neg_risk=False),
                order_type=OrderType.FAK,
            )
            if result:
                oid = extract_order_id(result)
                filled = float(confirm_fill_size(result, oid, buy_size))
                if filled <= 0:
                    console.print("  [dim yellow][FAK NULL][/] 0 confirmed fill · stopping")
                    break
                total_bought += filled
                remaining -= filled
                console.print(f"  [bold green][BUY FAK][/]{filled} @ {price:.3f}  [dim]id={str(oid)[:16]}…[/]")
                log_event("buy_fill", token_id=token_id, filled=filled, price=price, remaining=remaining, attempt=attempt + 1)
                if remaining < 0.01:
                    return total_bought
        except Exception as e:
            console.print(f"  [dim red]Market buy {attempt+1}/{max_retries} failed: {e}[/]")
            log_event("buy_attempt_error", token_id=token_id, error=str(e)[:200], attempt=attempt + 1)
        time.sleep(0.5)

    if total_bought > 0:
        console.print(f"  [bold yellow][BUY PARTIAL][/]{total_bought:.4f}/{size:.4f} filled")
        return total_bought
    console.print(f"  [bold red][BUY FAIL][/] market buy 0/{size:.4f} filled")
    return 0.0


# ------------------------- SELL (for hedge only) -------------------------

def sell_market_with_retry(token_id, size, price_limit, tick_size="0.01", max_retries=3):
    """Sell `size` shares via FAK. Used for hedge exits only — no max_price cap."""
    total_sold = 0.0
    remaining = float(size)
    if DRY_RUN:
        price = max(float(price_limit or tick_size), float(tick_size))
        console.print(f"  [bold black on yellow][DRY SELL][/] would SELL {remaining:.4f} {str(token_id)[:12]}… @ ≥{price:.3f}")
        log_event("dry_sell", token_id=token_id, size=remaining, price_limit=price)
        return 0, None
    for attempt in range(max_retries):
        if remaining < 0.01:
            break
        price = max(float(price_limit or tick_size), float(tick_size))
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
                total_sold += filled
                remaining -= filled
                console.print(f"  [bold green][EXIT FAK][/]{filled} @ ≥{price:.3f}  [dim]id={str(oid)[:16]}…[/]")
                log_event("sell_fill", token_id=token_id, filled=filled, price=price, remaining=remaining, attempt=attempt + 1)
                if remaining < 0.01:
                    return total_sold, result
        except Exception as e:
            console.print(f"  [dim red]Market sell {attempt+1}/{max_retries} failed: {e}[/]")
        time.sleep(0.5)

    if total_sold > 0:
        return total_sold, {"partial": True, "sold": total_sold}
    console.print(f"  [bold red][EXIT FAIL][/] market sell 0/{size:.4f} cleared")
    return 0, None


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

market_gateway = MarketGateway(gamma_url=GAMMA_API, data_api_url=DATA_API)

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

        # Hot-reload strategy
        _strat = load_strategy()
        BUY_THRESHOLD = _strat["buy_threshold"]
        BUY_MAX_PRICE = _strat["buy_max_price"]
        HEDGE_ENABLED = _strat["hedge_enabled"]
        HEDGE_THRESHOLD = _strat["hedge_threshold"]
        BUY_WINDOW_MIN = _strat["buy_window_min"]
        BUY_GRACE_S = _strat["buy_grace_s"]
        BUY_COOLDOWN_S = _strat["buy_cooldown_s"]
        SHARES = _strat["shares"]
        MAX_OPEN_POSITIONS = _strat["max_open_positions"]
        MAX_OPEN_NOTIONAL = _strat["max_open_notional"]
        MAX_DAILY_NOTIONAL = _strat["max_daily_notional"]
        ONE_ENTRY_PER_MARKET = _strat["one_entry_per_market"]
        REDEEM_THROTTLE_S = _strat["redeem_throttle_s"]
        MAX_REDEEM_AGE_DAYS = _strat["max_redeem_age_days"]
        DRY_RUN = _strat["dry_run"]
        POLL_BUY_WINDOW_S = _strat["poll_buy_window_s"]
        POSITIONS_REFRESH_S = _strat["positions_refresh_s"]
        BALANCE_REFRESH_S = _strat["balance_refresh_s"]
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

        console.rule(
            f"[bold bright_yellow]▲ TICK #{CYCLE:04d}[/] [dim]·[/] [bright_white]{now_str}[/] [dim]·[/] "
            f"[bright_green]MKT[/] [bold]{len(markets):>2}[/] [dim]·[/] "
            f"[bright_cyan]POS[/] [bold]{sum(1 for p in held.values() if max(p.get('up',{}).get('size',0), p.get('dn',{}).get('size',0)) > 0.01):>2}[/] [dim]·[/] "
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
                    if redeem_value == 0:
                        rem = gc_meta.get("bought_size", 0)
                        if rem > 0:
                            redeem_value = round(rem, 4)
                    outcome = "hedge" if hedge_proceeds > 0 else ("win" if redeem_value > 0 else "loss")
                    net = record_pnl(c, gc_meta.get("question", "?"), entry_cost, redeem_value, hedge_proceeds, outcome)
                    log_event("pnl_recorded", condition_id=c, entry=entry_cost, hedge=hedge_proceeds, redeem=redeem_value, net=round(net, 4), outcome=outcome)
                del positions_meta[c]
            if stale_conds:
                log_event("gc", stale_conditions=stale_conds)
                save_json(STATE_FILE, positions_meta)

        # ================= POSITIONS TABLE =================
        if held:
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
            if held_token and held_size > 0.01 and HEDGE_ENABLED:
                cached_quote = _book_cache.get(held_token, (None, 0.0, None, 0.0, None))
                cached_bid = cached_quote[0]
                if cached_bid is not None and cached_bid <= HEDGE_THRESHOLD:
                    # Fresh fetch before acting
                    fresh_bid, _, fresh_ask, _, fresh_mid = get_book_quote(held_token)
                    if fresh_mid is not None and fresh_mid > HEDGE_THRESHOLD:
                        log_event(
                            "hedge_cancel_bounce", condition_id=cond, leg=held_leg,
                            trigger_bid=cached_bid, current_bid=fresh_bid,
                            current_mid=fresh_mid, threshold=HEDGE_THRESHOLD,
                        )
                        console.print(
                            f"  [dim][CANCEL][/] {held_leg.upper()} hedge cancelled — mid bounced {cached_bid:.3f} → {fresh_mid:.3f}"
                        )
                    else:
                        hedge_bid = fresh_bid if fresh_bid is not None else cached_bid
                        hedge_tick = get_tick_size_cached(held_token)
                        console.print(Panel(
                            f"  [bright_white]{m.question}[/]\n"
                            f"  [bright_red]REVERSAL DETECTED[/] — {held_leg.upper()} dropped to [bold]{hedge_bid:.3f}[/]  ·  "
                            f"[bold red]TTM {minutes_left:>4.1f}m[/]",
                            title="[bold bright_red]▼ HEDGE SELL — CUTTING LOSSES[/]",
                            border_style="bright_red",
                            box=box.HEAVY,
                        ))
                        log_event("hedge_attempt", condition_id=cond, leg=held_leg, size=held_size, bid=hedge_bid, mid=fresh_mid)
                        sold, _ = sell_market_with_retry(held_token, held_size, hedge_bid, tick_size=hedge_tick)
                        if sold > 0:
                            meta["pnl_hedge_proceeds"] = round(meta.get("pnl_hedge_proceeds", 0) + sold * (hedge_bid or 0), 4)
                            log_event("hedge_fill", condition_id=cond, leg=held_leg, sold=sold, price=hedge_bid, mid=fresh_mid)
                            notify("HEDGE FIRED", f"Reversal on {m.question}\nSold {held_leg.upper()} at ~{hedge_bid:.3f} ({sold:.2f} shares)", priority="urgent")
                            save_json(STATE_FILE, positions_meta)
                        else:
                            time.sleep(1)
                            actual_bal = check_token_balance(held_token)
                            if actual_bal is not None and actual_bal < held_size - 0.01:
                                ghost_sold = held_size - actual_bal
                                meta["pnl_hedge_proceeds"] = round(meta.get("pnl_hedge_proceeds", 0) + ghost_sold * (hedge_bid or 0), 4)
                                log_event("hedge_ghost_fill", condition_id=cond, leg=held_leg, sold=ghost_sold, price=hedge_bid, mid=fresh_mid)
                                notify("HEDGE FIRED (ghost)", f"Reversal on {m.question}\n{held_leg.upper()} hedge ghost: {ghost_sold:.2f} shares", priority="urgent")
                                console.print(f"  [bold yellow][GHOST FILL][/] {held_leg.upper()} hedge confirmed: {ghost_sold:.4f} sold")
                                save_json(STATE_FILE, positions_meta)
                            else:
                                log_event("hedge_fail", condition_id=cond, leg=held_leg, size=held_size, bid=hedge_bid, mid=fresh_mid)

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
            est_cost = SHARES * BUY_THRESHOLD
            if open_count >= MAX_OPEN_POSITIONS:
                continue
            if open_notional + est_cost > MAX_OPEN_NOTIONAL + 1e-9:
                continue
            if daily_notional + est_cost > MAX_DAILY_NOTIONAL + 1e-9:
                continue

            # Fresh book quotes — no stale cache for buy decisions
            up_bid, _, up_ask, _, up_mid = get_book_quote(m.up_token)
            dn_bid, _, dn_ask, _, dn_mid = get_book_quote(m.dn_token)

            if up_mid is None and dn_mid is None:
                continue  # no sentiment

            # Determine winner by mid
            up_winning = up_mid is not None and dn_mid is not None and up_mid > dn_mid
            dn_winning = up_mid is not None and dn_mid is not None and dn_mid > up_mid

            # Handle one-mid-None edge case
            if up_mid is not None and dn_mid is None:
                up_winning = up_mid > 0.50
            if dn_mid is not None and up_mid is None:
                dn_winning = dn_mid > 0.50

            # Ambiguous — mids too close
            if up_mid is not None and dn_mid is not None and abs(up_mid - dn_mid) < 0.01:
                log_event("buy_skip_ambiguous", condition_id=cond, up_mid=up_mid, dn_mid=dn_mid)
                continue

            up_buy = up_winning and up_ask is not None and BUY_THRESHOLD <= up_ask <= BUY_MAX_PRICE
            dn_buy = dn_winning and dn_ask is not None and BUY_THRESHOLD <= dn_ask <= BUY_MAX_PRICE

            if not (up_buy or dn_buy):
                continue

            buy_token = m.up_token if up_buy else m.dn_token
            buy_ask = up_ask if up_buy else dn_ask
            buy_leg = "up" if up_buy else "down"

            # Cooldown
            last_buy_at = meta.get("last_buy_at") or 0
            if now_ms - last_buy_at < BUY_COOLDOWN_S * 1000:
                continue

            _ttm_disp = f"{minutes_left*60:>3.0f}s" if minutes_left < 1 else f"{minutes_left:>4.1f}m"
            console.print(Panel(
                f"  [bright_white]{m.question}[/]\n"
                f"  [bright_green]UP[/]   mid [bold]{up_mid or 0:.3f}[/]  ask [bold]{up_ask or 0:.3f}[/]   │   "
                f"[bright_red]DN[/]  mid [bold]{dn_mid or 0:.3f}[/]  ask [bold]{dn_ask or 0:.3f}[/]   │   "
                f"[bold green]TTM {_ttm_disp}[/]",
                title=f"[bold bright_green]▲ BUY TRIGGER — {buy_leg.upper()} LEG[/]",
                border_style="bright_green",
                box=box.HEAVY,
            ))

            tick = get_tick_size_cached(buy_token)
            log_event(
                "buy_attempt", condition_id=cond, leg=buy_leg, shares=SHARES,
                ask=buy_ask, threshold=BUY_THRESHOLD, minutes_left=round(minutes_left, 2),
            )

            bought = buy_market_with_retry(buy_token, SHARES, BUY_MAX_PRICE, tick_size=tick, min_price=BUY_THRESHOLD)
            if bought > 0:
                meta["last_buy_at"] = now_ms
                meta["bought_token"] = buy_token
                meta["bought_leg"] = buy_leg
                meta["bought_size"] = bought
                meta["fill_price"] = buy_ask
                meta["pnl_entry_cost"] = round(bought * buy_ask, 4)
                log_event(
                    "buy_success", condition_id=cond, leg=buy_leg, bought=bought,
                    price=buy_ask, entry_cost=meta["pnl_entry_cost"],
                )
                notify(
                    "BUY FILLED",
                    f"{m.question}\nBought {buy_leg.upper()} at ~{buy_ask:.3f} ({bought:.2f} shares)",
                    priority="high",
                )
                save_json(STATE_FILE, positions_meta)
            else:
                meta["last_buy_at"] = now_ms
                log_event("buy_fail", condition_id=cond, leg=buy_leg, shares=SHARES, ask=buy_ask)
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

    # Variable polling: 1s normal, sub-second in buy window
    _now = time.time() * 1000
    _min_ttm = min((m.end_ts * 1000 - _now) / 60000 for m in markets) if markets else 999
    if _min_ttm <= BUY_WINDOW_MIN:
        _sleep_s = POLL_BUY_WINDOW_S
    else:
        _sleep_s = 1

    # ================= KICK OFF NEXT CYCLE'S BOOK FETCH =================
    _next_now = time.time() * 1000 + _sleep_s * 1000
    _pending_tokens = set(_pending_book_futs.values())
    _MAX_PENDING_BOOKS = 30
    for m in markets:
        _ml = (m.end_ts * 1000 - _next_now) / 60000
        pos = held.get(m.condition_id, {})
        has_pos = max(pos.get("up", {}).get("size", 0), pos.get("dn", {}).get("size", 0)) > 0.01
        in_window = _ml > 0 and _ml <= BUY_WINDOW_MIN + 0.5
        if not (in_window or has_pos):
            continue
        for token in (m.up_token, m.dn_token):
            if token and token not in _pending_tokens and len(_pending_book_futs) < _MAX_PENDING_BOOKS:
                _pending_book_futs[_book_executor.submit(get_book_quote, token)] = token
                _pending_tokens.add(token)

    console.print(f"[dim bright_black]· · ·  sleeping {_sleep_s}s  · · ·[/]")
    time.sleep(_sleep_s)

# Graceful shutdown
console.print("[bold bright_green]▶ SHUTDOWN COMPLETE[/] [dim]state saved · exiting cleanly[/]")
log_event("shutdown", reason="signal")
sys.exit(0)
