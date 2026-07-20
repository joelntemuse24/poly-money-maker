import os
import re
import signal
import sys
import time
import json
import traceback
import logging
from logging.handlers import RotatingFileHandler
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
import requests
from concurrent.futures import ThreadPoolExecutor, as_completed
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
)
from py_clob_client_v2.order_builder.constants import SELL

console = Console()

load_dotenv()

HOST = "https://clob.polymarket.com"
DATA_API = "https://data-api.polymarket.com"
CHAIN_ID = 137
STATE_FILE = "positions.json"  # metadata cache only: redeem_submitted_at, entered_at
BTC_SLUG_PREFIX = "bitcoin-up-or-down"  # event/market slug filter for managed markets
BTC_SLUG_ALIASES = ("bitcoin-up-or-down", "btc-updown", "btc-updown-5m")
PUSD = "0xC011a7E12a19f7B1f670d46F03B03f3342E82DFB"
CTF = "0x4D97DCd97eC945f40cF65F87097ACe5EA0476045"

PRIVATE_KEY = os.getenv("PRIVATE_KEY")
FUNDER_ADDRESS = os.getenv("FUNDER_ADDRESS")
API_KEY = os.getenv("API_KEY")
API_SECRET = os.getenv("API_SECRET")
API_PASSPHRASE = os.getenv("API_PASSPHRASE")
RELAYER_URL = os.getenv("RELAYER_URL", "https://relayer-v2.polymarket.com")
# Defaults match the values that used to be hardcoded (already present in git
# history); override via env to use a rotated relayer key.
RELAYER_API_KEY = os.getenv("RELAYER_API_KEY", "019df62f-45bc-796e-975c-3f434472b163")
RELAYER_API_KEY_ADDRESS = os.getenv("RELAYER_API_KEY_ADDRESS", "0x42aec4505559c0613f7ce2541d9d29741bc5e195")

# ------------------------- STRATEGY CONFIG -------------------------
# Defaults — overridden by strategy.json if present (hot-reloaded each cycle)
_STRATEGY_DEFAULTS = {
    "sell_threshold": 0.10,
    "hedge_enabled": False,
    "hedge_threshold": 0.50,
    "sell_window_min": 0.75,           # last 45 seconds — sell window
    "sell_grace_s": 2,                # don't sell within 2s of first seeing a position
    "sell_cooldown_s": 3,             # 3s between sell attempts per leg
    "sell_lastchance_threshold": 0.35, # confirmed loser below 35¢ in final seconds
    "sell_lastchance_s": 10,           # last-chance window: final 10 seconds
    "redeem_throttle_s": 30,          # 30s between redeem attempts
    "max_redeem_age_days": 7,
    "dry_run": False,
    "poll_sell_window_s": 0.25,      # sub-second polling in sell window
    "positions_refresh_s": 2,        # refresh positions every N seconds (not every sub-second cycle)
    "balance_refresh_s": 15,         # refresh balance every N seconds
}
STRATEGY_FILE = "strategy.json"

_strat_cache = None
_strat_mtime = 0.0

def load_strategy():
    """Load strategy params from strategy.json, falling back to defaults.
    Caches result and only re-reads when the file's mtime changes."""
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
SELL_THRESHOLD = _strat["sell_threshold"]
HEDGE_ENABLED = _strat["hedge_enabled"]
HEDGE_THRESHOLD = _strat["hedge_threshold"]
SELL_WINDOW_MIN = _strat["sell_window_min"]
SELL_GRACE_S = _strat["sell_grace_s"]
SELL_COOLDOWN_S = _strat["sell_cooldown_s"]
SELL_LASTCHANCE_THRESHOLD = _strat["sell_lastchance_threshold"]
SELL_LASTCHANCE_S = _strat["sell_lastchance_s"]
REDEEM_THROTTLE_S = _strat["redeem_throttle_s"]
MAX_REDEEM_AGE_DAYS = _strat["max_redeem_age_days"]
DRY_RUN = _strat["dry_run"]
POLL_SELL_WINDOW_S = _strat["poll_sell_window_s"]
POSITIONS_REFRESH_S = _strat["positions_refresh_s"]
BALANCE_REFRESH_S = _strat["balance_refresh_s"]

# ------------------------- LOG ROTATION -------------------------
LOG_FILE = "bot.log"
LOG_MAX_BYTES = 5 * 1024 * 1024   # 5 MB per file
LOG_BACKUP_COUNT = 3              # keep bot.log, bot.log.1, bot.log.2, bot.log.3

_file_logger = logging.getLogger("polybot")
_file_logger.setLevel(logging.INFO)
_log_handler = RotatingFileHandler(LOG_FILE, maxBytes=LOG_MAX_BYTES, backupCount=LOG_BACKUP_COUNT)
_log_handler.setFormatter(logging.Formatter("%(message)s"))
_file_logger.addHandler(_log_handler)

# ------------------------- HEARTBEAT -------------------------
HEARTBEAT_FILE = ".heartbeat"

# ------------------------- P&L TRACKING -------------------------
PNL_FILE = "pnl.json"

# ------------------------- CLIENT SETUP -------------------------
if API_KEY and API_SECRET and API_PASSPHRASE:
    from py_clob_client_v2 import ApiCreds
    api_creds = ApiCreds(
        api_key=API_KEY,
        api_secret=API_SECRET,
        api_passphrase=API_PASSPHRASE,
    )
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

banner = Panel(
    Align.center(
        "[bold bright_green]██████╗ ████████╗ ██████╗[/]   [bright_yellow]//[/]  [bold white]EXIT DESK[/]\n"
        "[bold bright_green]██╔══██╗╚══██╔══╝██╔════╝[/]   [bright_yellow]//[/]  [dim]POLYMARKET CLOB · MATIC[/]\n"
        "[bold bright_green]██████╔╝   ██║   ██║     [/]   [bright_yellow]//[/]  [dim]SELL-SIDE EXECUTION ONLY[/]\n"
        "[bold bright_green]██╔══██╗   ██║   ██║     [/]   [bright_yellow]//[/]  [dim]BTC 5-MIN · 8\u00a2 LOSER TRIG[/]\n"
        "[bold bright_green]██████╔╝   ██║   ╚██████╗[/]   [bright_yellow]//[/]  STATUS: [bold bright_green]\u25cf ARMED[/]\n"
        "[bold bright_green]╚═════╝    ╚═╝    ╚═════╝[/]   [bright_yellow]//[/]  [dim]v9.0 \u00b7 5m \u00b7 sell-only \u00b7 data-api[/]",
        vertical="middle",
    ),
    title="[bold bright_yellow]▰▱▰▱  TRADING SYSTEM ONLINE  ▱▰▱▰[/]",
    subtitle="[dim]press Ctrl-C to disarm[/]",
    border_style="bright_green",
    box=box.HEAVY_EDGE,
    padding=(1, 4),
)
console.print(banner)
if DRY_RUN:
    console.print("[bold black on yellow] DRY-RUN [/] [yellow]no orders or on-chain txs will be sent · decisions are logged only[/]")

# ------------------------- GRACEFUL SHUTDOWN -------------------------

_shutdown_requested = False


def _handle_shutdown(signum, frame):
    global _shutdown_requested
    sig_name = signal.Signals(signum).name
    console.print(f"\n[bold yellow]▶ {sig_name} received — finishing current cycle then exiting[/]")
    _shutdown_requested = True


signal.signal(signal.SIGTERM, _handle_shutdown)
signal.signal(signal.SIGINT, _handle_shutdown)

# Track positions with permanent redeem failures to avoid retry spam
_redeem_permanent_failures = set()

# ------------------------- NOTIFICATIONS -------------------------

NTFY_TOPIC = os.getenv("NTFY_TOPIC", "polybot-joel-btc")

def notify(title, message, priority="default"):
    """Send a push notification via ntfy.sh. Fire-and-forget."""
    try:
        requests.post(
            f"https://ntfy.sh/{NTFY_TOPIC}",
            data=message.encode("utf-8"),
            headers={"Title": title, "Priority": priority},
            timeout=5,
        )
    except Exception:
        pass  # notifications are best-effort, never crash the bot

notify("Polybot Started", f"Bot started at {datetime.now().strftime('%Y-%m-%d %H:%M:%S UTC')}", priority="high")
console.print(f"[bold bright_cyan]▶ NOTIFY[/] [dim]ntfy.sh topic: {NTFY_TOPIC}[/]")

# ------------------------- HELPERS -------------------------


def safe_api_call(func, *args, **kwargs):
    try:
        return func(*args, **kwargs)
    except Exception as e:
        err_str = str(e)
        if not any(s in err_str for s in ("order couldn't be fully filled", "not enough balance", "not found", "404")):
            console.print(f"  [bold red][API ERR][/] [dim]{err_str[:120]}[/]")
        raise


def get_balance():
    try:
        from py_clob_client_v2.client import BalanceAllowanceParams
        bal_info = safe_api_call(client.get_balance_allowance, BalanceAllowanceParams(asset_type="COLLATERAL"))
        if bal_info:
            return float(bal_info.get("balance", 0)) / 1_000_000
    except Exception:
        pass
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
    """Append a structured JSON log line to bot.log (with rotation)."""
    entry = {"ts": datetime.now().isoformat(), "event": event}
    entry.update(kwargs)
    _file_logger.info(json.dumps(entry))


def write_heartbeat():
    """Write current timestamp to heartbeat file for health monitoring."""
    try:
        with open(HEARTBEAT_FILE, "w") as f:
            json.dump({"ts": time.time(), "iso": datetime.now().isoformat(), "cycle": CYCLE}, f)
    except Exception:
        pass


def load_pnl():
    """Load cumulative P&L data."""
    if os.path.exists(PNL_FILE):
        try:
            with open(PNL_FILE, "r") as f:
                return json.load(f)
        except Exception:
            pass
    return {"trades": [], "summary": {"total_pnl": 0.0, "total_trades": 0, "wins": 0, "losses": 0}}


def record_pnl(condition_id, question, entry_cost, sell_proceeds, hedge_proceeds, outcome):
    """Record a completed trade's P&L."""
    pnl_data = load_pnl()
    net = sell_proceeds + hedge_proceeds - entry_cost
    trade = {
        "ts": datetime.now().isoformat(),
        "condition_id": condition_id,
        "question": question[:40],
        "entry_cost": round(entry_cost, 4),
        "sell_proceeds": round(sell_proceeds, 4),
        "hedge_proceeds": round(hedge_proceeds, 4),
        "net_pnl": round(net, 4),
        "outcome": outcome,
    }
    pnl_data["trades"].append(trade)
    s = pnl_data["summary"]
    s["total_pnl"] = round(s["total_pnl"] + net, 4)
    s["total_trades"] += 1
    if net >= 0:
        s["wins"] += 1
    else:
        s["losses"] += 1
    # Keep last 500 trades to prevent unbounded growth
    if len(pnl_data["trades"]) > 500:
        pnl_data["trades"] = pnl_data["trades"][-500:]
    atomic_save(PNL_FILE, pnl_data)
    return net


# ------------------------- POSITION DISCOVERY -------------------------

def get_user_positions():
    """Fetch user's current open positions from Polymarket data-api.
    Returns a list of position dicts on success, or None on failure."""
    try:
        res = requests.get(
            f"{DATA_API}/positions",
            params={"user": FUNDER_ADDRESS, "limit": 200},
            timeout=10,
        )
        res.raise_for_status()
        data = res.json() or []
        if not isinstance(data, list):
            return None
        return data
    except Exception as e:
        console.print(f"  [bold red][DATA-API FAIL][/] [dim]{e}[/]")
        return None


def check_token_balance(token_id):
    """Re-fetch positions and return the current size for a specific token.
    Returns the float balance, or None if lookup fails."""
    try:
        positions = get_user_positions()
        if positions is None:
            return None
        for p in positions:
            if p.get("asset") == token_id:
                return float(p.get("size", 0))
        return 0.0
    except Exception:
        return None


_ET = ZoneInfo("America/New_York")
_SLUG_TIME_RE = re.compile(r"(\d{1,2})(am|pm)-et$")


# Regex to detect duration marker in slugs like "btc-updown-5m-{ts}"
_SLUG_DURATION_RE = re.compile(r"-(\d+)m-")


def parse_position_end_dt(legs):
    for p in legs:
        for key in ("slug", "eventSlug"):
            slug = p.get(key) or ""
            tail = slug.rsplit("-", 1)[-1]
            if tail.isdigit():
                ts = int(tail)
                if ts > 1_700_000_000:
                    # Slug timestamp is the market START time.
                    # Detect duration from slug (e.g. "5m" → 5 minutes)
                    # and add it to get the actual end/expiry time.
                    dur_match = _SLUG_DURATION_RE.search(slug)
                    dur_min = int(dur_match.group(1)) if dur_match else 60
                    return datetime.fromtimestamp(ts) + timedelta(minutes=dur_min)

    # Extract hour+am/pm from slug (e.g. "…-12pm-et" → 12PM ET)
    slug_hour = None
    for p in legs:
        for key in ("slug", "eventSlug"):
            m = _SLUG_TIME_RE.search((p.get(key) or "").lower())
            if m:
                h = int(m.group(1))
                ampm = m.group(2)
                if ampm == "pm" and h != 12:
                    h += 12
                elif ampm == "am" and h == 12:
                    h = 0
                slug_hour = h
                break
        if slug_hour is not None:
            break

    for p in legs:
        end_date = p.get("endDate")
        if not end_date:
            continue
        try:
            base = datetime.fromisoformat(end_date.replace("Z", "+00:00"))
            if slug_hour is not None:
                # The endDate is the UTC calendar date on which the market
                # expires.  We know the ET hour from the slug.  Find the ET
                # date such that (slug_hour + 1h) in ET falls on the
                # endDate in UTC.  Try the endDate itself, then ±1 day.
                if base.tzinfo is None:
                    base = base.replace(tzinfo=ZoneInfo("UTC"))
                end_utc_date = base.astimezone(ZoneInfo("UTC")).date()
                for day_offset in (0, -1, 1):
                    candidate = datetime(
                        end_utc_date.year, end_utc_date.month, end_utc_date.day,
                        slug_hour, 0, 0, tzinfo=_ET,
                    ) + timedelta(days=day_offset, hours=1)
                    if candidate.astimezone(ZoneInfo("UTC")).date() == end_utc_date:
                        return candidate.astimezone(tz=None).replace(tzinfo=None)
                # Fallback: use endDate + slug offset without date matching
                base_et = base.astimezone(_ET)
                et_dt = base_et.replace(hour=slug_hour, minute=0, second=0, microsecond=0) + timedelta(hours=1)
                return et_dt.astimezone(tz=None).replace(tzinfo=None)
            return base
        except Exception:
            continue
    return None


def empty_opposite_leg(source, outcome):
    return {
        "asset": source.get("oppositeAsset"),
        "size": 0,
        "outcome": outcome,
        "redeemable": False,
        "endDate": source.get("endDate"),
        "slug": source.get("slug"),
        "eventSlug": source.get("eventSlug"),
        "title": source.get("title"),
    }


def group_btc_complete_sets(positions, positions_meta=None):
    """Filter to BTC markets, grouped by conditionId with UP/DOWN leg metadata.
    Includes single-leg positions so direct sells can still be managed."""
    by_cond = {}
    for p in positions:
        slug = (p.get("slug") or "").lower()
        event_slug = (p.get("eventSlug") or "").lower()
        title = (p.get("title") or "").lower()
        if not (
            slug.startswith(BTC_SLUG_ALIASES)
            or event_slug.startswith(BTC_SLUG_ALIASES)
            or "bitcoin up or down" in title
        ):
            continue
        cond = p.get("conditionId")
        if not cond:
            continue
        by_cond.setdefault(cond, []).append(p)

    sets = []
    for cond, legs in by_cond.items():
        up = None
        dn = None
        for p in legs:
            oc = (p.get("outcome") or "").lower()
            if oc in ("up", "yes"):
                up = p
            elif oc in ("down", "no"):
                dn = p
        if not up and dn and dn.get("oppositeAsset"):
            up = empty_opposite_leg(dn, "up")
        if not dn and up and up.get("oppositeAsset"):
            dn = empty_opposite_leg(up, "down")
        if not (up and dn):
            continue
        try:
            end_dt = parse_position_end_dt(legs)
            if not end_dt:
                continue
            end_ts = end_dt.timestamp() * 1000
        except Exception:
            continue
        sets.append({
            "conditionId": cond,
            "up": up,
            "dn": dn,
            "end_ts": end_ts,
            "end_dt": end_dt,
            "question": up.get("title") or dn.get("title") or "BTC Market",
        })
    sets.sort(key=lambda s: s["end_ts"])

    # Inject orphan legs from metadata: if we sold partially last cycle and
    # data-api hasn't caught up, keep the remaining size alive for this cycle.
    if positions_meta:
        existing_conds = {s["conditionId"] for s in sets}
        now_ms = time.time() * 1000
        for cond, meta in positions_meta.items():
            if cond in existing_conds:
                continue
            exp_up = meta.get("expected_up_size", 0)
            exp_dn = meta.get("expected_dn_size", 0)
            if exp_up <= 0 and exp_dn <= 0:
                continue
            up_token = meta.get("up_token")
            dn_token = meta.get("dn_token")
            if not up_token or not dn_token:
                continue
            try:
                end_date = meta.get("end_date", "")
                end_dt = datetime.fromisoformat(end_date.replace("Z", "+00:00"))
                end_ts = end_dt.timestamp() * 1000
            except Exception:
                continue
            if end_ts <= now_ms:
                meta["expected_up_size"] = 0
                meta["expected_dn_size"] = 0
                continue
            sets.append({
                "conditionId": cond,
                "up": {"asset": up_token, "size": exp_up, "outcome": "up", "redeemable": False},
                "dn": {"asset": dn_token, "size": exp_dn, "outcome": "down", "redeemable": False},
                "end_ts": end_ts,
                "end_dt": end_dt,
                "question": meta.get("question", "BTC Market"),
            })
        sets.sort(key=lambda s: s["end_ts"])

    return sets


# ------------------------- PRICING -------------------------

def get_book_bid(token_id):
    try:
        book = safe_api_call(client.get_order_book, token_id)
        bids = book.get("bids", [])
        if not bids:
            return None, 0.0
        best = max(bids, key=lambda x: float(x.get("price", 0)))
        return float(best.get("price", 0)), float(best.get("size", 0))
    except Exception as e:
        log_event("book_fetch_fail", token_id=token_id, error=str(e))
        return None, 0.0


# ------------------------- ORDER HELPERS -------------------------

def extract_order_id(order_obj):
    if isinstance(order_obj, dict):
        return order_obj.get("orderID") or order_obj.get("id")
    return getattr(order_obj, "orderID", None) or getattr(order_obj, "id", None) or (str(order_obj) if order_obj is not None else None)


def get_order_details(order_id):
    """Return dict with size_matched and status, or None."""
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
            # Order likely fully filled and removed from active API
            return {"status": "NOT_FOUND"}
        return None


def confirm_fill_size(result, oid, requested):
    """Best-effort number of shares an order actually filled.

    Prefers an explicit size_matched on the response, otherwise verifies via the
    order endpoint (a 404/NOT_FOUND means the FAK was archived after filling, so the
    requested chunk filled). Returns 0 when the fill cannot be confirmed, so callers
    never assume a full fill on an ambiguous response -- assuming a full fill would
    over-report sells and silently skip real exits.
    """
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


def sell_market_with_retry(token_id, size, price_limit, tick_size="0.01", max_retries=3):
    total_sold = 0.0
    remaining = float(size)
    price = max(float(price_limit or tick_size), float(tick_size))
    if DRY_RUN:
        console.print(f"  [bold black on yellow][DRY SELL][/] would SELL {remaining:.4f} {str(token_id)[:12]}… @ ≥{price:.3f}")
        log_event("dry_sell", token_id=token_id, size=remaining, price_limit=price)
        return 0, None
    for attempt in range(max_retries):
        if remaining < 0.01:
            break
        try:
            neg_risk = safe_api_call(client.get_neg_risk, token_id)
            result = safe_api_call(
                client.create_and_post_market_order,
                MarketOrderArgs(token_id=token_id, amount=remaining, side=SELL, price=price),
                options=PartialCreateOrderOptions(tick_size=tick_size, neg_risk=neg_risk),
                order_type=OrderType.FAK,
            )
            if result:
                oid = extract_order_id(result)
                filled = float(confirm_fill_size(result, oid, remaining))
                if filled <= 0:
                    console.print("  [dim yellow][FAK NULL][/] [dim]0 confirmed fill · stopping to avoid double-sell[/]")
                    break
                total_sold += filled
                remaining -= filled
                console.print(f"  [bold green][EXIT FAK][/]{filled} @ ≥{price:.3f}  [dim]id={str(oid)[:16]}...[/]")
                if remaining < 0.01:
                    return total_sold, result
        except Exception as e:
            console.print(f"  [dim red]Market sell {attempt+1}/{max_retries} failed: {e}[/]")
        time.sleep(1)

    if total_sold > 0:
        return total_sold, {"partial": True, "sold": total_sold}
    console.print(f"  [bold red][EXIT FAIL][/] market sell 0/{size:.4f} cleared")
    return 0, None



def get_relayer_headers():
    if not RELAYER_API_KEY or not RELAYER_API_KEY_ADDRESS:
        console.print("[bold red]▶ RELAYER [WARN][/] [dim]RELAYER_API_KEY / RELAYER_API_KEY_ADDRESS not set · redeem will fail[/]")
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



def quote_leg(bid):
    """Return (self_price, matched_price) for a single leg.

      - self_price:    leg's value used for sell/hedge decisions. Only uses bid
                       (actual buyers on the book). Returns None if no bid exists
                       — the bot will NOT attempt to sell into an empty book.
      - matched_price: realizable price for a direct sell (bid).
    """
    if bid is None:
        return None, None
    return float(bid), float(bid)


# ------------------------- REDEEM -------------------------

def redeem_condition(condition_id, label=""):
    """Submit a redemption tx for a resolved Polymarket conditionId via the Polygon relayer.
    Returns the transactionID string on success, or None on failure."""
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
            console.print(f"  [bold bright_green][SETTLE \u25b6][/] {label}  [dim]tx={str(tx_id)[:18]}\u2026[/]")
            return tx_id
        console.print(f"  [dim red][SETTLE FAIL][/] {label}  [dim]{err}[/]")
        if err and ("proxyWallet" in err or "invalid" in err.lower()):
            _redeem_permanent_failures.add(condition_id)
            console.print(f"  [dim red][SETTLE SKIP][/] {label}  [dim]permanent failure — will not retry[/]")
        return None
    except Exception as e:
        console.print(f"  [dim red][SETTLE ERR][/] {label}  [dim]{e}[/]")
        return None


# ------------------------- MAIN LOOP -------------------------
# positions_meta is a metadata cache keyed by conditionId. The on-chain holdings
# (size, redeemable flag, etc.) come fresh from data-api each cycle. We only
# persist:
#   - entered_at: when we first saw this set (used for sell grace period)
#   - redeem_submitted_at: throttle redemption resubmissions
#   - last_sell_up_at / last_sell_dn_at: 30s post-sell cooldown per leg
positions_meta = load_json(STATE_FILE)
CYCLE = 0
_last_positions_refresh = 0.0
_last_balance_refresh = 0.0
_cached_managed_sets = []
_book_executor = ThreadPoolExecutor(max_workers=4)
_pending_book_futs = {}  # {future: token_id} — books fetched during previous sleep

while not _shutdown_requested:
    try:
        CYCLE += 1
        now_ms = time.time() * 1000
        now_str = datetime.now().strftime("%H:%M:%S")

        # Hot-reload strategy config from strategy.json (no restart needed)
        _strat = load_strategy()
        SELL_THRESHOLD = _strat["sell_threshold"]
        HEDGE_ENABLED = _strat["hedge_enabled"]
        HEDGE_THRESHOLD = _strat["hedge_threshold"]
        SELL_WINDOW_MIN = _strat["sell_window_min"]
        SELL_GRACE_S = _strat["sell_grace_s"]
        SELL_COOLDOWN_S = _strat["sell_cooldown_s"]
        SELL_LASTCHANCE_THRESHOLD = _strat["sell_lastchance_threshold"]
        SELL_LASTCHANCE_S = _strat["sell_lastchance_s"]
        REDEEM_THROTTLE_S = _strat["redeem_throttle_s"]
        MAX_REDEEM_AGE_DAYS = _strat["max_redeem_age_days"]
        DRY_RUN = _strat["dry_run"]
        POLL_SELL_WINDOW_S = _strat["poll_sell_window_s"]
        POSITIONS_REFRESH_S = _strat["positions_refresh_s"]
        BALANCE_REFRESH_S = _strat["balance_refresh_s"]

        write_heartbeat()

        _now_f = time.time()
        if _now_f - _last_balance_refresh >= BALANCE_REFRESH_S:
            pusd_bal = get_balance()
            _last_balance_refresh = _now_f

        if _now_f - _last_positions_refresh >= POSITIONS_REFRESH_S:
            positions_raw = get_user_positions()
            _cached_managed_sets = group_btc_complete_sets(positions_raw or [], positions_meta)
            _last_positions_refresh = _now_f
        else:
            positions_raw = None  # skip GC when not refreshing positions
        managed_sets = _cached_managed_sets

        console.rule(
            f"[bold bright_yellow]\u25b0 TICK #{CYCLE:04d}[/] [dim]\u00b7[/] [bright_white]{now_str}[/] [dim]\u00b7[/] "
            f"[bright_green]SETS[/] [bold]{len(managed_sets):>2}[/] [dim]\u00b7[/] "
            f"[bright_yellow]NAV[/] [bold]${pusd_bal:>7.2f}[/] [dim]\u25b0[/]",
            style="bright_yellow",
        )

        # ================= GC META CACHE =================
        # Drop metadata entries for conditions we no longer hold (sold out, expired+redeemed).
        # Only run GC when data-api fetch succeeded; skip on transient failures to avoid
        # wiping state during outages.
        if positions_raw is not None:
            live_conds = {s["conditionId"] for s in managed_sets}
            stale_conds = [c for c in list(positions_meta.keys()) if c not in live_conds]
            for c in stale_conds:
                gc_meta = positions_meta[c]
                # Record P&L for completed trades before deleting metadata
                if "pnl_entry_cost" in gc_meta:
                    entry_cost = gc_meta.get("pnl_entry_cost", 0)
                    sell_proceeds = gc_meta.get("pnl_sell_proceeds", 0)
                    hedge_proceeds = gc_meta.get("pnl_hedge_proceeds", 0)
                    redeem_value = gc_meta.get("pnl_redeem_value", 0)
                    # If no explicit redeem was recorded, estimate from remaining holdings.
                    # Winner resolves at $1/share; use tracked sizes (post-sell) or initial sizes.
                    if redeem_value == 0:
                        # Use expected sizes if non-zero, else fall back to init sizes
                        # (orphan expiry path may have zeroed expected_*_size)
                        rem_up = gc_meta.get("expected_up_size", 0) or gc_meta.get("pnl_init_up_size", 0)
                        rem_dn = gc_meta.get("expected_dn_size", 0) or gc_meta.get("pnl_init_dn_size", 0)
                        if rem_up > 0 or rem_dn > 0:
                            redeem_value = round(max(rem_up, rem_dn), 4)
                    total_return = sell_proceeds + hedge_proceeds + redeem_value
                    outcome = "hedge" if hedge_proceeds > 0 else ("win" if redeem_value > 0 else "flat")
                    net = record_pnl(c, gc_meta.get("question", "?"), entry_cost, sell_proceeds + redeem_value, hedge_proceeds, outcome)
                    log_event("pnl_recorded", condition_id=c, entry=entry_cost, returned=round(total_return, 4), net=round(net, 4), outcome=outcome)
                del positions_meta[c]
            if stale_conds:
                log_event("gc", stale_conditions=stale_conds)
                save_json(STATE_FILE, positions_meta)

        # ================= POSITIONS TABLE =================
        if managed_sets:
            table = Table(
                title="[bold bright_cyan]\u2261 MANAGED POSITIONS \u2261[/]  [dim]BTC HOURLY \u00b7 DATA-API FEED[/]",
                box=box.HEAVY_HEAD,
                border_style="bright_blue",
                title_style="bold bright_cyan",
                show_lines=True,
            )
            table.add_column("INSTRUMENT", style="white", max_width=40)
            table.add_column("EXPIRY", style="dim cyan", justify="center")
            table.add_column("TTM", justify="right")
            table.add_column("UP", justify="right")
            table.add_column("DN", justify="right")
            table.add_column("STATE", justify="center")

            for s in managed_sets:
                try:
                    end_dt = s["end_dt"]
                    mins = (s["end_ts"] - now_ms) / 60000
                    ends_str = end_dt.strftime("%H:%M")
                    up_sz = float(s["up"].get("size", 0))
                    dn_sz = float(s["dn"].get("size", 0))

                    if s["up"].get("redeemable") or s["dn"].get("redeemable"):
                        state = "[bold bright_magenta]\u2713 REDEEM[/]"
                    elif mins <= 0:
                        state = "[dim]\u00b7 closed[/]"
                    elif mins <= SELL_LASTCHANCE_S / 60:
                        state = f"[bold red]\u25cc EXIT \u2264{int(SELL_THRESHOLD*100)}\u00a2 \u00b7 LAST <{int(SELL_LASTCHANCE_THRESHOLD*100)}\u00a2[/]"
                    elif mins <= SELL_WINDOW_MIN:
                        state = f"[bold yellow]\u25cc EXIT \u2264{int(SELL_THRESHOLD*100)}\u00a2[/]"
                    else:
                        state = "[bold bright_green]\u25cf WATCHING[/]"

                    if mins < 1:
                        mins_style = "bold red"
                    elif mins < 60:
                        mins_style = "yellow"
                    else:
                        mins_style = "green"

                    if mins < 1:
                        ttm_str = f"{max(mins*60, 0):.0f}s"
                    else:
                        ttm_str = f"{mins:.0f}m"

                    table.add_row(
                        (s["question"] or "?")[:40],
                        ends_str,
                        f"[{mins_style}]{ttm_str}[/]",
                        f"{up_sz:.2f}",
                        f"{dn_sz:.2f}",
                        state,
                    )
                except Exception:
                    continue
            console.print(table)
        else:
            console.print("  [dim]\u00b7 no managed positions \u00b7 awaiting your manual buys on the UI \u00b7[/]")

        # ================= REDEEM =================
        for s in managed_sets:
            cond = s["conditionId"]
            redeemable = bool(s["up"].get("redeemable")) or bool(s["dn"].get("redeemable"))
            if not redeemable:
                continue
            if now_ms - s["end_ts"] > MAX_REDEEM_AGE_DAYS * 86400 * 1000:
                continue
            if cond in _redeem_permanent_failures:
                continue
            meta = positions_meta.setdefault(cond, {})
            last = meta.get("redeem_submitted_at") or 0
            if now_ms - last < REDEEM_THROTTLE_S * 1000:
                continue  # already submitted, wait 5 min before retry
            tx = redeem_condition(cond, label=(s["question"] or "?")[:32])
            if tx:
                meta["redeem_submitted_at"] = now_ms
                # P&L: only the winning side resolves at $1/share; the loser
                # resolves at $0.  min(up, dn) complete sets pay $1 each, and
                # the excess shares on one side also pay $1 (they are the winner).
                remaining_up = float(s["up"].get("size", 0))
                remaining_dn = float(s["dn"].get("size", 0))
                meta["pnl_redeem_value"] = round(max(remaining_up, remaining_dn), 4)
                log_event("redeem_submit", condition_id=cond, tx_id=str(tx))
                save_json(STATE_FILE, positions_meta)

        # ================= COLLECT PRE-FETCHED BOOKS =================
        # Books were fetched in the background during the previous cycle's sleep.
        # They should already be complete — just collect the results (instant).
        _book_cache = {}
        for _f, _t in list(_pending_book_futs.items()):
            try:
                _book_cache[_t] = _f.result(timeout=1)
            except Exception:
                _book_cache[_t] = (None, 0.0)
        _pending_book_futs = {}

        # ================= SELL PHASE =================
        for s in managed_sets:
            end_ts = s["end_ts"]
            minutes_left = (end_ts - now_ms) / 60000
            if minutes_left <= 0:
                continue
            cond = s["conditionId"]
            # Note: do NOT skip sell phase for _redeem_permanent_failures —
            # a bad redeem response should never prevent selling the loser.

            up_token = s["up"].get("asset")
            dn_token = s["dn"].get("asset")
            meta = positions_meta.setdefault(cond, {})
            if "entered_at" not in meta:
                meta["entered_at"] = now_ms
                meta["up_token"] = up_token
                meta["dn_token"] = dn_token
                meta["question"] = s["question"]
                meta["end_date"] = s["up"].get("endDate") or s["dn"].get("endDate")
                # P&L: entry cost from actual avgPrice (data-api), fallback $0.50
                init_up = float(s["up"].get("size", 0))
                init_dn = float(s["dn"].get("size", 0))
                up_avg = float(s["up"].get("avgPrice", 0) or 0) or 0.50
                dn_avg = float(s["dn"].get("avgPrice", 0) or 0) or 0.50
                meta["pnl_entry_cost"] = round(init_up * up_avg + init_dn * dn_avg, 4)
                meta["pnl_init_up_size"] = init_up
                meta["pnl_init_dn_size"] = init_dn
                meta["pnl_sell_proceeds"] = 0.0
                meta["pnl_hedge_proceeds"] = 0.0
                meta["pnl_redeem_value"] = 0.0
                save_json(STATE_FILE, positions_meta)
            if now_ms - meta["entered_at"] < SELL_GRACE_S * 1000:
                continue
            # Only sell in the last SELL_WINDOW_MIN minutes to reduce reversal risk
            if minutes_left > SELL_WINDOW_MIN:
                continue
            up_size = float(s["up"].get("size", 0))
            dn_size = float(s["dn"].get("size", 0))
            if up_size < 0.01 and dn_size < 0.01:
                continue

            up_bid, _ = _book_cache.get(up_token, (None, 0.0)) if up_token else (None, 0.0)
            dn_bid, _ = _book_cache.get(dn_token, (None, 0.0)) if dn_token else (None, 0.0)

            # Alert if book fetch failed for either leg during the sell window
            if up_bid is None and up_size > 0:
                log_event("sell_skip_no_book", condition_id=cond, leg="up", minutes_left=round(minutes_left, 1))
            if dn_bid is None and dn_size > 0:
                log_event("sell_skip_no_book", condition_id=cond, leg="dn", minutes_left=round(minutes_left, 1))
            if up_bid is None and dn_bid is None and (up_size > 0 or dn_size > 0):
                _book_fail_count = meta.get("_book_fail_count", 0) + 1
                meta["_book_fail_count"] = _book_fail_count
                if _book_fail_count == 15:  # ~15s at 1s polling in sell window
                    _ttm_str = f"{round(minutes_left*60)}s" if minutes_left < 1 else f"{round(minutes_left)}m"
                    notify("\u26a0 Book Unavailable", f"{s['question']} \u2014 order book unreachable with {_ttm_str} left", priority="high")
            else:
                meta["_book_fail_count"] = 0

            up_price, up_matched_price = quote_leg(up_bid)
            dn_price, dn_matched_price = quote_leg(dn_bid)

            # Sell at or below the configured threshold throughout the exit window.
            up_trigger = bool(
                up_size > 0 and up_price is not None and up_price <= SELL_THRESHOLD
            )
            dn_trigger = bool(
                dn_size > 0 and dn_price is not None and dn_price <= SELL_THRESHOLD
            )
            up_trigger_reason = "threshold" if up_trigger else None
            dn_trigger_reason = "threshold" if dn_trigger else None
            seconds_left = minutes_left * 60

            # In the final seconds, a higher-priced loser needs confirmation from
            # a strong opposite bid when neither side met the normal threshold.
            if seconds_left <= SELL_LASTCHANCE_S and not up_trigger and not dn_trigger:
                confirmation_price = 1.0 - SELL_LASTCHANCE_THRESHOLD
                up_candidate = (up_size > 0 and up_price is not None
                                and up_price < SELL_LASTCHANCE_THRESHOLD)
                dn_candidate = (dn_size > 0 and dn_price is not None
                                and dn_price < SELL_LASTCHANCE_THRESHOLD)
                if (up_candidate and not dn_candidate and dn_price is not None
                        and dn_price >= confirmation_price):
                    up_trigger = True
                    up_trigger_reason = "last_chance"
                elif (dn_candidate and not up_candidate and up_price is not None
                        and up_price >= confirmation_price):
                    dn_trigger = True
                    dn_trigger_reason = "last_chance"
                elif up_candidate or dn_candidate:
                    log_event(
                        "sell_skip_ambiguous",
                        condition_id=cond,
                        reason="last_chance_unconfirmed",
                        seconds_left=round(seconds_left, 3),
                        up_bid=up_bid,
                        dn_bid=dn_bid,
                    )

            # Two low bids indicate an ambiguous or illiquid book, not two losers.
            if up_trigger and dn_trigger:
                log_event(
                    "sell_skip_ambiguous",
                    condition_id=cond,
                    reason="both_legs_triggered",
                    seconds_left=round(seconds_left, 3),
                    up_bid=up_bid,
                    dn_bid=dn_bid,
                )
                up_trigger = False
                dn_trigger = False
                up_trigger_reason = None
                dn_trigger_reason = None

            # Once one leg is fully sold, preserve the other for redemption.
            preserve_up = bool(
                meta.get("last_sell_dn_at") and dn_size < 0.01 and up_size >= 0.01
            )
            preserve_dn = bool(
                meta.get("last_sell_up_at") and up_size < 0.01 and dn_size >= 0.01
            )
            if up_trigger and preserve_up:
                log_event(
                    "sell_skip_preserve_leg",
                    condition_id=cond,
                    leg="up",
                    trigger_reason=up_trigger_reason,
                    seconds_left=round(seconds_left, 3),
                    up_bid=up_bid,
                    dn_bid=dn_bid,
                )
                up_trigger = False
                up_trigger_reason = None
            if dn_trigger and preserve_dn:
                log_event(
                    "sell_skip_preserve_leg",
                    condition_id=cond,
                    leg="down",
                    trigger_reason=dn_trigger_reason,
                    seconds_left=round(seconds_left, 3),
                    up_bid=up_bid,
                    dn_bid=dn_bid,
                )
                dn_trigger = False
                dn_trigger_reason = None

            sell_up = up_trigger
            sell_dn = dn_trigger

            will_sell_up = sell_up and (now_ms - (meta.get("last_sell_up_at") or 0) >= SELL_COOLDOWN_S * 1000)
            will_sell_dn = sell_dn and (now_ms - (meta.get("last_sell_dn_at") or 0) >= SELL_COOLDOWN_S * 1000)

            if will_sell_up or will_sell_dn:
                up_bid_str = f"{up_price:.3f}" if up_price is not None else "  -  "
                dn_bid_str = f"{dn_price:.3f}" if dn_price is not None else "  -  "
                _ttm_disp = f"{minutes_left*60:>3.0f}s" if minutes_left < 1 else f"{minutes_left:>4.1f}m"
                console.print(Panel(
                    f"  [bright_white]{s['question']}[/]\n"
                    f"  [bright_green]UP[/]   px [bold]{up_bid_str}[/]  inv [bold]{up_size:>6.2f}[/]   \u2502   "
                    f"[bright_red]DN[/]  px [bold]{dn_bid_str}[/]  inv [bold]{dn_size:>6.2f}[/]   \u2502   "
                    f"[bold red]TTM {_ttm_disp}[/]",
                    title="[bold bright_yellow]\u25bc EXIT TRIGGER \u2014 LOSER LEG[/]",
                    border_style="bright_yellow",
                    box=box.HEAVY,
                ))

            if sell_up:
                if not will_sell_up:
                    console.print(f"  [dim][SKIP][/] [dim]UP sell suppressed · sold <{SELL_COOLDOWN_S:.0f}s ago[/]")
                else:
                    log_event(
                        "sell_attempt", condition_id=cond, leg="up", size=up_size,
                        bid=up_bid, price_limit=up_price,
                        trigger_reason=up_trigger_reason,
                        seconds_left=round(seconds_left, 3),
                        up_bid=up_bid, dn_bid=dn_bid,
                    )
                    sold, _ = sell_market_with_retry(up_token, up_size, up_matched_price or SELL_THRESHOLD)
                    if sold > 0:
                        meta["last_sell_up_at"] = now_ms
                        up_size -= sold
                        meta["expected_up_size"] = up_size
                        s["up"]["size"] = up_size
                        meta["pnl_sell_proceeds"] = round(meta.get("pnl_sell_proceeds", 0) + sold * (up_price or 0), 4)
                        log_event(
                            "sell_fill", condition_id=cond, leg="up", sold=sold,
                            remaining=up_size, price=up_price,
                            trigger_reason=up_trigger_reason,
                            seconds_left=round(seconds_left, 3),
                            up_bid=up_bid, dn_bid=dn_bid,
                        )
                        save_json(STATE_FILE, positions_meta)
                    else:
                        time.sleep(1)
                        actual_bal = check_token_balance(up_token)
                        if actual_bal is not None and actual_bal < up_size - 0.01:
                            ghost_sold = up_size - actual_bal
                            meta["last_sell_up_at"] = now_ms
                            meta["pnl_sell_proceeds"] = round(meta.get("pnl_sell_proceeds", 0) + ghost_sold * (up_price or 0), 4)
                            up_size = actual_bal
                            meta["expected_up_size"] = actual_bal
                            s["up"]["size"] = actual_bal
                            log_event(
                                "sell_ghost_fill", condition_id=cond, leg="up",
                                sold=ghost_sold, remaining=actual_bal, price=up_price,
                                trigger_reason=up_trigger_reason,
                                seconds_left=round(seconds_left, 3),
                                up_bid=up_bid, dn_bid=dn_bid,
                            )
                            console.print(f"  [bold yellow][GHOST FILL][/] UP sell confirmed via balance check: {ghost_sold:.4f} sold")
                            save_json(STATE_FILE, positions_meta)
                        else:
                            log_event(
                                "sell_fail", condition_id=cond, leg="up", size=up_size,
                                bid=up_bid, price_limit=up_price,
                                trigger_reason=up_trigger_reason,
                                seconds_left=round(seconds_left, 3),
                                up_bid=up_bid, dn_bid=dn_bid,
                            )
            if sell_dn:
                if not will_sell_dn:
                    console.print(f"  [dim][SKIP][/] [dim]DN sell suppressed · sold <{SELL_COOLDOWN_S:.0f}s ago[/]")
                else:
                    log_event(
                        "sell_attempt", condition_id=cond, leg="down", size=dn_size,
                        bid=dn_bid, price_limit=dn_price,
                        trigger_reason=dn_trigger_reason,
                        seconds_left=round(seconds_left, 3),
                        up_bid=up_bid, dn_bid=dn_bid,
                    )
                    sold, _ = sell_market_with_retry(dn_token, dn_size, dn_matched_price or SELL_THRESHOLD)
                    if sold > 0:
                        meta["last_sell_dn_at"] = now_ms
                        dn_size -= sold
                        meta["expected_dn_size"] = dn_size
                        s["dn"]["size"] = dn_size
                        meta["pnl_sell_proceeds"] = round(meta.get("pnl_sell_proceeds", 0) + sold * (dn_price or 0), 4)
                        log_event(
                            "sell_fill", condition_id=cond, leg="down", sold=sold,
                            remaining=dn_size, price=dn_price,
                            trigger_reason=dn_trigger_reason,
                            seconds_left=round(seconds_left, 3),
                            up_bid=up_bid, dn_bid=dn_bid,
                        )
                        save_json(STATE_FILE, positions_meta)
                    else:
                        time.sleep(1)
                        actual_bal = check_token_balance(dn_token)
                        if actual_bal is not None and actual_bal < dn_size - 0.01:
                            ghost_sold = dn_size - actual_bal
                            meta["last_sell_dn_at"] = now_ms
                            meta["pnl_sell_proceeds"] = round(meta.get("pnl_sell_proceeds", 0) + ghost_sold * (dn_price or 0), 4)
                            dn_size = actual_bal
                            meta["expected_dn_size"] = actual_bal
                            s["dn"]["size"] = actual_bal
                            log_event(
                                "sell_ghost_fill", condition_id=cond, leg="down",
                                sold=ghost_sold, remaining=actual_bal, price=dn_price,
                                trigger_reason=dn_trigger_reason,
                                seconds_left=round(seconds_left, 3),
                                up_bid=up_bid, dn_bid=dn_bid,
                            )
                            console.print(f"  [bold yellow][GHOST FILL][/] DN sell confirmed via balance check: {ghost_sold:.4f} sold")
                            save_json(STATE_FILE, positions_meta)
                        else:
                            log_event(
                                "sell_fail", condition_id=cond, leg="down", size=dn_size,
                                bid=dn_bid, price_limit=dn_price,
                                trigger_reason=dn_trigger_reason,
                                seconds_left=round(seconds_left, 3),
                                up_bid=up_bid, dn_bid=dn_bid,
                            )

            # ================= HEDGE PHASE =================
            # Disabled by default; strategy.json must explicitly enable it.
            # If one leg was sold and the held leg drops below HEDGE_THRESHOLD,
            # sell the held leg too to limit reversal losses.
            loser_was_up = preserve_dn
            loser_was_dn = preserve_up

            if HEDGE_ENABLED and loser_was_up and dn_price is not None and dn_price <= HEDGE_THRESHOLD:
                console.print(Panel(
                    f"  [bright_white]{s['question']}[/]\n"
                    f"  [bright_red]REVERSAL DETECTED[/] — DN (held leg) dropped to [bold]{dn_price:.3f}[/]  ·  "
                    f"[bold red]TTM {minutes_left:>4.1f}m[/]",
                    title="[bold bright_red]▼ HEDGE SELL — CUTTING LOSSES[/]",
                    border_style="bright_red",
                    box=box.HEAVY,
                ))
                log_event("hedge_attempt", condition_id=cond, leg="down", size=dn_size, bid=dn_bid, price_limit=dn_price)
                sold, _ = sell_market_with_retry(dn_token, dn_size, 0.01)
                if sold > 0:
                    meta["expected_dn_size"] = dn_size - sold
                    s["dn"]["size"] = dn_size - sold
                    meta["pnl_hedge_proceeds"] = round(meta.get("pnl_hedge_proceeds", 0) + sold * (dn_price or 0), 4)
                    log_event("hedge_fill", condition_id=cond, leg="down", sold=sold, remaining=dn_size - sold, price=dn_price)
                    notify("HEDGE FIRED", f"Reversal on {s['question']}\nSold DN at ~{dn_price:.3f} ({sold:.2f} shares)", priority="urgent")
                    save_json(STATE_FILE, positions_meta)
                else:
                    time.sleep(1)
                    actual_bal = check_token_balance(dn_token)
                    if actual_bal is not None and actual_bal < dn_size - 0.01:
                        ghost_sold = dn_size - actual_bal
                        meta["expected_dn_size"] = actual_bal
                        s["dn"]["size"] = actual_bal
                        meta["pnl_hedge_proceeds"] = round(meta.get("pnl_hedge_proceeds", 0) + ghost_sold * (dn_price or 0), 4)
                        log_event("hedge_ghost_fill", condition_id=cond, leg="down", sold=ghost_sold, remaining=actual_bal, price=dn_price)
                        notify("HEDGE FIRED (ghost)", f"Reversal on {s['question']}\nDN hedge ghost fill: {ghost_sold:.2f} shares", priority="urgent")
                        console.print(f"  [bold yellow][GHOST FILL][/] DN hedge confirmed via balance check: {ghost_sold:.4f} sold")
                        save_json(STATE_FILE, positions_meta)
                    else:
                        log_event("hedge_fail", condition_id=cond, leg="down", size=dn_size, bid=dn_bid)

            elif HEDGE_ENABLED and loser_was_dn and up_price is not None and up_price <= HEDGE_THRESHOLD:
                console.print(Panel(
                    f"  [bright_white]{s['question']}[/]\n"
                    f"  [bright_red]REVERSAL DETECTED[/] — UP (held leg) dropped to [bold]{up_price:.3f}[/]  ·  "
                    f"[bold red]TTM {minutes_left:>4.1f}m[/]",
                    title="[bold bright_red]▼ HEDGE SELL — CUTTING LOSSES[/]",
                    border_style="bright_red",
                    box=box.HEAVY,
                ))
                log_event("hedge_attempt", condition_id=cond, leg="up", size=up_size, bid=up_bid, price_limit=up_price)
                sold, _ = sell_market_with_retry(up_token, up_size, 0.01)
                if sold > 0:
                    meta["expected_up_size"] = up_size - sold
                    s["up"]["size"] = up_size - sold
                    meta["pnl_hedge_proceeds"] = round(meta.get("pnl_hedge_proceeds", 0) + sold * (up_price or 0), 4)
                    log_event("hedge_fill", condition_id=cond, leg="up", sold=sold, remaining=up_size - sold, price=up_price)
                    notify("HEDGE FIRED", f"Reversal on {s['question']}\nSold UP at ~{up_price:.3f} ({sold:.2f} shares)", priority="urgent")
                    save_json(STATE_FILE, positions_meta)
                else:
                    time.sleep(1)
                    actual_bal = check_token_balance(up_token)
                    if actual_bal is not None and actual_bal < up_size - 0.01:
                        ghost_sold = up_size - actual_bal
                        meta["expected_up_size"] = actual_bal
                        s["up"]["size"] = actual_bal
                        meta["pnl_hedge_proceeds"] = round(meta.get("pnl_hedge_proceeds", 0) + ghost_sold * (up_price or 0), 4)
                        log_event("hedge_ghost_fill", condition_id=cond, leg="up", sold=ghost_sold, remaining=actual_bal, price=up_price)
                        notify("HEDGE FIRED (ghost)", f"Reversal on {s['question']}\nUP hedge ghost fill: {ghost_sold:.2f} shares", priority="urgent")
                        console.print(f"  [bold yellow][GHOST FILL][/] UP hedge confirmed via balance check: {ghost_sold:.4f} sold")
                        save_json(STATE_FILE, positions_meta)
                    else:
                        log_event("hedge_fail", condition_id=cond, leg="up", size=up_size, bid=up_bid)

    except Exception:
        log_event("cycle_error", traceback=traceback.format_exc())
        console.print(Panel(
            traceback.format_exc(),
            title="[bold bright_red]\u25a0\u25a0  SYSTEM FAULT  \u25a0\u25a0[/]",
            subtitle="[dim]auto-restart in 5s \u00b7 cycle aborted[/]",
            border_style="bright_red",
            box=box.HEAVY_EDGE,
        ))


    # Variable polling: 5s >2min, 1s ≤2min, sub-second in sell window (≤45s)
    _now = time.time() * 1000
    _min_ttm = min((s["end_ts"] - _now) / 60000 for s in managed_sets) if managed_sets else 999
    if _min_ttm <= SELL_WINDOW_MIN:  # ≤45s — sub-second polling in sell window
        _sleep_s = POLL_SELL_WINDOW_S
    elif _min_ttm <= 2:              # ≤2min — poll every 1s
        _sleep_s = 1
    else:                            # >2min — poll every 5s
        _sleep_s = 5
    # ================= KICK OFF NEXT CYCLE'S BOOK FETCH =================
    # Start fetching books in the background NOW, so they're ready by the time
    # the next cycle starts after sleep. This overlaps HTTP latency with sleep,
    # making the effective cycle time = max(sleep_s, fetch_time) instead of
    # sleep_s + fetch_time.
    _next_now = time.time() * 1000 + _sleep_s * 1000  # predicted TTM at next cycle start
    for s in managed_sets:
        _ml = (s["end_ts"] - _next_now) / 60000
        if _ml <= 0 or _ml > SELL_WINDOW_MIN:
            continue
        _us = float(s["up"].get("size", 0))
        _ds = float(s["dn"].get("size", 0))
        if _us < 0.01 and _ds < 0.01:
            continue
        _ut = s["up"].get("asset")
        _dt = s["dn"].get("asset")
        if _ut and _us >= 0.01:
            _pending_book_futs[_book_executor.submit(get_book_bid, _ut)] = _ut
        if _dt and _ds >= 0.01:
            _pending_book_futs[_book_executor.submit(get_book_bid, _dt)] = _dt

    console.print(f"[dim bright_black]\u00b7 \u00b7 \u00b7  sleeping {_sleep_s}s  \u00b7 \u00b7 \u00b7[/]")
    time.sleep(_sleep_s)

# Graceful shutdown complete
console.print("[bold bright_green]▶ SHUTDOWN COMPLETE[/] [dim]state saved · exiting cleanly[/]")
log_event("shutdown", reason="signal")
sys.exit(0)
