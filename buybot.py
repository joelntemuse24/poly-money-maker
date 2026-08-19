import os
import fcntl
import signal
import sys
import time
import json
import math
import shutil
import threading
import traceback
import logging
from logging.handlers import RotatingFileHandler
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
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
    OrderArgs,
    OrderType,
    PartialCreateOrderOptions,
    ApiCreds,
    AssetType,
    BalanceAllowanceParams,
    TradeParams,
)
from py_clob_client_v2.order_builder.constants import SELL, BUY
from py_clob_client_v2.config import get_contract_config
from py_clob_client_v2.order_utils.exchange_order_builder_v2 import ExchangeOrderBuilderV2
from py_clob_client_v2.order_builder.builder import ROUNDING_CONFIG
from py_builder_relayer_client.builder.proxy import build_proxy_transaction_request
from py_builder_relayer_client.config import get_contract_config as get_relayer_contract_config
from py_builder_relayer_client.encode.proxy import encode_proxy_transaction_data
from py_builder_relayer_client.models import (
    CallType,
    ProxyTransaction,
    ProxyTransactionArgs,
)
from py_builder_relayer_client.signer import Signer as RelayerSigner
from py_builder_signing_sdk.config import BuilderConfig
from py_builder_signing_sdk.sdk_types import BuilderApiKeyCreds

from buy.market import MarketGateway, MintMarket
from buy.btc_price import get_btc_feed, append_research, SOURCE_TWAP_60
from buy.clob_book_ws import get_book_feed


class ImmediateResponseClobClient(ClobClient):
    """Return POST responses without the SDK's up-to-30s trade poll.

    Matching already happened server-side. The bot persists the signed order
    id before POST and performs its own settlement-finality checks, so waiting
    inside the SDK only serializes later hedge work.
    """

    def _resolve_transactions_hashes(self, response):
        return response


console = Console()
load_dotenv()


def acquire_process_lock(path):
    """Fail closed when another copy of this bot is already running."""
    lock_fh = open(path, "a+")
    try:
        fcntl.flock(lock_fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        console.print("[bold red]Another 15m buy bot process already holds the runtime lock.[/]")
        raise SystemExit(1)
    lock_fh.seek(0)
    lock_fh.truncate()
    lock_fh.write(str(os.getpid()))
    lock_fh.flush()
    return lock_fh


_PROCESS_LOCK_FH = acquire_process_lock("/tmp/poly-money-maker-buybot.lock")

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
# Relayer API key auth (Settings → API Keys → Relayer): key + owner address.
RELAYER_API_KEY = os.getenv("RELAYER_API_KEY")
RELAYER_API_KEY_ADDRESS = os.getenv("RELAYER_API_KEY_ADDRESS")
# Builder HMAC auth (Settings → Builders): key + secret + passphrase.
POLY_BUILDER_API_KEY = (
    os.getenv("POLY_BUILDER_API_KEY")
    or os.getenv("BUILDER_API_KEY")
)
POLY_BUILDER_SECRET = (
    os.getenv("POLY_BUILDER_SECRET")
    or os.getenv("BUILDER_SECRET")
)
POLY_BUILDER_PASSPHRASE = (
    os.getenv("POLY_BUILDER_PASSPHRASE")
    or os.getenv("BUILDER_PASSPHRASE")
    or os.getenv("BUILDER_PASS_PHRASE")
)
EXPECTED_TICK_SIZE = "0.01"

# CLOB market orders reject taker amounts with more than four decimal places.
# py-clob-client-v2 1.1.0 still exposes wider configs for sub-cent markets.
for _rounding in ROUNDING_CONFIG.values():
    if _rounding.amount > 4:
        _rounding.amount = 4

# ------------------------- STRATEGY CONFIG -------------------------
_STRATEGY_DEFAULTS = {
    # Explicit hot-reloadable entry arm. A missing/invalid file disables new
    # entries while existing positions continue through the hedge path.
    "entry_enabled": False,
    # Trigger at 75¢: buy as soon as the winning ask is ≥ 75¢, priced at the
    # live ask (so early catches ≈ 75¢). buy_max_price is a hard ceiling only —
    # never pay above 90¢; we do not wait for or target the top of the band.
    "buy_threshold": 0.75,
    "buy_max_price": 0.90,
    # Consensus on Polymarket GUI display price (mid if spread≤10¢ else last trade).
    # Tuned for the 75¢ band (old 92¢/10¢ gates could never arm a 75¢ ask).
    "min_winner_bid": 0.70,
    "max_loser_bid": 0.30,
    "min_bid_edge": 0.05,
    # Skip buys unless live BTC is ≥ this many USD from the window Price To Beat,
    # and only allow the side matching that underlying move.
    "underlying_gate_enabled": True,
    "min_underlying_edge_usd": 10.0,
    # Force-dump only when FAK avg is worse than this. Fills in
    # [toxic_force_exit_below, buy_threshold) stay on the normal hedge path.
    # Must be <= buy_threshold (validator); 65¢ ≈ walk well below the 75¢ floor.
    "toxic_force_exit_below": 0.65,
    "hedge_enabled": True,
    "hedge_threshold": 0.35,
    # Kept in config (live JSON still sends it). Not a FAK floor: after 35/40
    # integrity, the sell follows the live bid so a 20¢ print is not $0.
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
    "hedge_require_ask_max": 0.40,
    "buy_window_min": 4.0,
    "buy_grace_s": 2,
    "buy_cooldown_s": 3,
    "buy_budget": 2.5,
    # Hard ceiling on USDC sent per market. Strategy is $2.50; never more than $3.
    "buy_max_spend": 3.0,
    "max_open_positions": 0,  # 0 = unlimited
    "max_open_notional": 10000.0,
    "max_daily_notional": 999999.0,
    "one_entry_per_market": True,
    "redeem_throttle_s": 30,
    "max_redeem_age_days": 7,
    # Missing configuration must never arm real-money orders.
    "dry_run": True,
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
    exists = os.path.exists(STRATEGY_FILE)
    try:
        mtime = os.path.getmtime(STRATEGY_FILE) if exists else 0
    except OSError:
        mtime = 0
        exists = False

    def _entries_disabled(base):
        safe = dict(base)
        safe["entry_enabled"] = False
        # A config failure must not disable exits for already-held inventory.
        safe["hedge_enabled"] = True
        return safe

    if not exists:
        if _strat_cache is None:
            raise RuntimeError(
                f"strategy file {STRATEGY_FILE} is required at startup"
            )
        # Retain hedge parameters, but fail closed for new entries. Store the
        # safe snapshot too, otherwise an unchanged missing/bad file can return
        # the previously armed cache on the following cycle.
        cfg = _entries_disabled(_strat_cache or _STRATEGY_DEFAULTS)
        _strat_cache = cfg
        _strat_mtime = mtime
        return cfg
    if _strat_cache is not None and mtime == _strat_mtime:
        return _strat_cache
    cfg = dict(_STRATEGY_DEFAULTS)
    try:
        with open(STRATEGY_FILE, "r") as f:
            overrides = json.load(f)
        if not isinstance(overrides, dict):
            raise ValueError("strategy root must be an object")
        unknown = set(overrides) - set(cfg) - {"shares"}
        if unknown:
            raise ValueError(f"unknown strategy keys: {sorted(unknown)}")
        for k, v in overrides.items():
            if k not in cfg:
                continue
            expected = type(cfg[k])
            if expected is bool:
                if not isinstance(v, bool):
                    raise ValueError(f"{k} must be true or false")
                cfg[k] = v
            else:
                cfg[k] = expected(v)
        # Legacy alias: near $1 prices, "shares" was effectively a dollar budget
        if "buy_budget" not in overrides and "shares" in overrides:
            cfg["buy_budget"] = float(overrides["shares"])
        if not (0 < cfg["buy_threshold"] <= cfg["buy_max_price"] <= 1):
            raise ValueError("buy price band must satisfy 0 < threshold <= max <= 1")
        if not (0 < cfg["toxic_force_exit_below"] <= cfg["buy_threshold"]):
            raise ValueError("toxic_force_exit_below must satisfy 0 < below <= buy_threshold")
        if not (0 <= cfg["hedge_min_price"] <= cfg["hedge_threshold"] <= 1):
            raise ValueError("hedge prices must satisfy 0 <= min <= threshold <= 1")
        for key, value in cfg.items():
            if isinstance(value, bool) or key == "tick_size":
                continue
            if not math.isfinite(float(value)):
                raise ValueError(f"{key} must be finite")
        for key in (
            "min_winner_bid", "max_loser_bid", "min_bid_edge",
            "max_entry_spread", "hedge_max_spread", "hedge_require_ask_max",
        ):
            if not 0 <= float(cfg[key]) <= 1:
                raise ValueError(f"{key} must be between 0 and 1")
        if float(cfg["hedge_require_ask_max"]) < float(cfg["hedge_threshold"]):
            raise ValueError("hedge_require_ask_max must be >= hedge_threshold")
        if str(cfg["tick_size"]) not in {
            "0.1", "0.01", "0.005", "0.0025", "0.001", "0.0001",
        }:
            raise ValueError("tick_size is not supported by the CLOB")
        if str(cfg["tick_size"]) != EXPECTED_TICK_SIZE:
            raise ValueError(
                f"tick_size for this bot must be {EXPECTED_TICK_SIZE}"
            )
        if float(cfg["hedge_min_price"]) < float(cfg["tick_size"]):
            raise ValueError("hedge_min_price must be at least one tick")
        if not cfg["one_entry_per_market"]:
            raise ValueError("one_entry_per_market must remain true")
        if not cfg["dry_run"] and not cfg["hedge_enabled"]:
            raise ValueError("live mode requires hedge_enabled=true")
        for key in (
            "buy_budget", "buy_max_spend", "max_open_notional",
            "max_daily_notional", "poll_buy_window_s", "poll_held_s",
            "positions_refresh_s", "balance_refresh_s", "buy_window_min",
            "ui_every_n_cycles",
        ):
            if float(cfg[key]) <= 0:
                raise ValueError(f"{key} must be positive")
        if float(cfg["buy_max_spend"]) + 1e-9 < float(cfg["buy_budget"]):
            raise ValueError("buy_max_spend must be >= buy_budget")
        # 0 = unlimited open markets (probe: redeem lag must not freeze entries).
        if float(cfg["max_open_positions"]) < 0:
            raise ValueError("max_open_positions must be >= 0 (0 = unlimited)")
        for key in (
            "min_underlying_edge_usd", "hedge_undercut_ticks",
            "hedge_quote_max_age_s", "hedge_retry_sleep_s",
            "hedge_ghost_sleep_s", "buy_grace_s", "buy_cooldown_s",
            "redeem_throttle_s", "max_redeem_age_days",
        ):
            if float(cfg[key]) < 0:
                raise ValueError(f"{key} must be non-negative")
    except Exception as e:
        console.print(f"[bold red]▶ STRATEGY [WARN][/] [dim]failed to load {STRATEGY_FILE}: {e}[/]")
        if _strat_cache is None:
            raise RuntimeError(
                f"valid strategy file {STRATEGY_FILE} is required at startup"
            ) from e
        # Persist a disarmed cache for this mtime. Returning a temporary safe
        # copy while retaining the armed cache would re-arm one cycle later.
        cfg = _entries_disabled(_strat_cache or _STRATEGY_DEFAULTS)
    _strat_cache = cfg
    _strat_mtime = mtime
    return cfg


_strat = load_strategy()
ENTRY_ENABLED = _strat["entry_enabled"]
BUY_THRESHOLD = _strat["buy_threshold"]
BUY_MAX_PRICE = _strat["buy_max_price"]
MIN_WINNER_BID = _strat["min_winner_bid"]
MAX_LOSER_BID = _strat["max_loser_bid"]
MIN_BID_EDGE = _strat["min_bid_edge"]
UNDERLYING_GATE_ENABLED = _strat["underlying_gate_enabled"]
MIN_UNDERLYING_EDGE_USD = _strat["min_underlying_edge_usd"]
TOXIC_FORCE_EXIT_BELOW = _strat["toxic_force_exit_below"]
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
BUY_MAX_SPEND = _strat["buy_max_spend"]
MAX_OPEN_POSITIONS = _strat["max_open_positions"]
MAX_OPEN_NOTIONAL = _strat["max_open_notional"]
MAX_DAILY_NOTIONAL = _strat["max_daily_notional"]
ONE_ENTRY_PER_MARKET = _strat["one_entry_per_market"]
REDEEM_THROTTLE_S = _strat["redeem_throttle_s"]
MAX_REDEEM_AGE_DAYS = _strat["max_redeem_age_days"]
STARTUP_DRY_RUN = bool(_strat["dry_run"])
DRY_RUN = STARTUP_DRY_RUN
POLL_BUY_WINDOW_S = _strat["poll_buy_window_s"]
POLL_HELD_S = _strat["poll_held_s"]
POSITIONS_REFRESH_S = _strat["positions_refresh_s"]
BALANCE_REFRESH_S = _strat["balance_refresh_s"]
UI_EVERY_N_CYCLES = _strat["ui_every_n_cycles"]
TICK_SIZE_FALLBACK = _strat["tick_size"]

if DRY_RUN:
    STATE_FILE = "positions_buy.dryrun.json"
    PNL_FILE = "pnl_buy.dryrun.json"
    HEARTBEAT_FILE = ".heartbeat_buy_dryrun"
    RESEARCH_FILE = "underlying_research_buy_dryrun.jsonl"
    PTB_STORE_FILE = "ptb_twap60_buy_dryrun.json"

# ------------------------- LOG ROTATION -------------------------
LOG_FILE = "buybot.dryrun.log" if DRY_RUN else "buybot.log"
LOG_MAX_BYTES = 5 * 1024 * 1024
LOG_BACKUP_COUNT = 3

_file_logger = logging.getLogger("buybot")
_file_logger.setLevel(logging.INFO)
_log_handler = RotatingFileHandler(LOG_FILE, maxBytes=LOG_MAX_BYTES, backupCount=LOG_BACKUP_COUNT)
_log_handler.setFormatter(logging.Formatter("%(message)s"))
_file_logger.addHandler(_log_handler)

NTFY_TOPIC = os.getenv("NTFY_TOPIC", "polybot-joel-btc")
_notify_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="ntfy")


def _send_notification(title, message, priority):
    try:
        requests.post(
            f"https://ntfy.sh/{NTFY_TOPIC}",
            data=message.encode("utf-8"),
            headers={"Title": title, "Priority": priority},
            timeout=5,
        )
    except Exception:
        pass


def notify(title, message, priority="default"):
    """Queue notifications off the order/hedge critical path."""
    try:
        _notify_executor.submit(_send_notification, title, message, priority)
    except Exception:
        pass


# ------------------------- HELPERS -------------------------

_clob_lock = threading.RLock()


def safe_api_call(func, *args, **kwargs):
    """Serialize all shared ClobClient calls across background executors."""
    try:
        with _clob_lock:
            return func(*args, **kwargs)
    except Exception as e:
        console.print(f"[dim red]API error: {e}[/]")
        raise


def finite_float(value, *, minimum=None, maximum=None):
    """Parse an external numeric value and reject NaN/inf/out-of-range data."""
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(parsed):
        return None
    if minimum is not None and parsed < minimum:
        return None
    if maximum is not None and parsed > maximum:
        return None
    return parsed


def get_balance():
    try:
        bal_info = safe_api_call(
            client.get_balance_allowance,
            BalanceAllowanceParams(asset_type=AssetType.COLLATERAL),
        )
        raw = (
            bal_info.get("balance")
            if isinstance(bal_info, dict)
            else getattr(bal_info, "balance", None)
        )
        return _decode_clob_fixed6(raw)
    except Exception:
        return None


def _reject_json_constant(value):
    raise ValueError(f"non-finite JSON constant {value}")


def _parse_json_float(value):
    parsed = float(value)
    if not math.isfinite(parsed):
        raise ValueError(f"non-finite JSON number {value}")
    return parsed


def atomic_save(path, data):
    tmp = path + ".tmp"
    backup = path + ".bak"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2, allow_nan=False)
        f.flush()
        os.fsync(f.fileno())
    if os.path.exists(path):
        # Never replace a known-good recovery copy with a corrupt primary.
        # This matters after load_json() recovered from ``.bak``: a crash
        # during the next save must still leave at least one valid JSON file.
        primary_valid = False
        try:
            with open(path, "r") as current_f:
                primary_valid = isinstance(
                    json.load(
                        current_f,
                        parse_constant=_reject_json_constant,
                        parse_float=_parse_json_float,
                    ),
                    dict,
                )
        except Exception:
            primary_valid = False
        if primary_valid:
            backup_tmp = backup + ".tmp"
            shutil.copy2(path, backup_tmp)
            with open(backup_tmp, "rb") as backup_f:
                os.fsync(backup_f.fileno())
            os.replace(backup_tmp, backup)
    os.replace(tmp, path)
    parent = os.path.dirname(os.path.abspath(path)) or "."
    dir_fd = os.open(parent, os.O_RDONLY)
    try:
        os.fsync(dir_fd)
    finally:
        os.close(dir_fd)


def load_json(path, *, required=False):
    errors = []
    for candidate in (path, path + ".bak"):
        if not os.path.exists(candidate):
            continue
        try:
            with open(candidate, "r") as f:
                data = json.load(
                    f,
                    parse_constant=_reject_json_constant,
                    parse_float=_parse_json_float,
                )
            if not isinstance(data, dict):
                raise ValueError("JSON root must be an object")
            return data
        except Exception as exc:
            errors.append(f"{candidate}: {exc}")
    if errors:
        raise RuntimeError("state recovery failed: " + " | ".join(errors))
    if required:
        raise RuntimeError(
            f"required live state file {path} is missing; refuse to arm"
        )
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
    loaded = load_json(PNL_FILE)
    if loaded:
        return loaded
    return {
        "trades": [],
        "summary": {
            "total_pnl": 0.0,
            "total_trades": 0,
            "wins": 0,
            "losses": 0,
        },
    }


def record_pnl(condition_id, question, entry_cost, sell_proceeds, hedge_proceeds, outcome):
    pnl_data = load_pnl()
    for trade in pnl_data.get("trades", []):
        if trade.get("condition_id") == condition_id:
            return float(trade.get("net", 0) or 0)
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

POSITIONS_PAGE_SIZE = 500
POSITIONS_MAX_OFFSET = 10_000


def fetch_all_position_rows(condition_id=None):
    """Fetch every Data API position page or raise; partial pages are unsafe."""
    rows = []
    offset = 0
    while offset <= POSITIONS_MAX_OFFSET:
        params = {
            "user": FUNDER_ADDRESS,
            "limit": POSITIONS_PAGE_SIZE,
            "offset": offset,
            "sizeThreshold": 0,
            "includeArchived": "true",
            "sortBy": "TOKENS",
            "sortDirection": "DESC",
        }
        if condition_id:
            params["market"] = str(condition_id)
        resp = requests.get(
            f"{DATA_API}/positions",
            params=params,
            timeout=10,
        )
        resp.raise_for_status()
        page = resp.json()
        if not isinstance(page, list):
            raise ValueError("positions response was not a list")
        rows.extend(page)
        if len(page) < POSITIONS_PAGE_SIZE:
            return rows
        offset += len(page)
    raise RuntimeError("positions pagination exceeded API offset limit")


def get_user_positions():
    try:
        return fetch_all_position_rows()
    except Exception as e:
        log_event("positions_fetch_fail", error=str(e)[:200])
        return None


def check_token_balance(token_id, condition_id=None):
    """Return share balance for token_id.

    On a successful Data API response, a missing token means **0**, not None.
    None is reserved for request/parse failure so callers can fail closed.
    """
    try:
        rows = fetch_all_position_rows(condition_id)
        for p in rows:
            if str(p.get("asset") or "") == str(token_id):
                return finite_float(p.get("size", 0), minimum=0)
        return 0.0
    except Exception:
        return None


def check_clob_token_balance(token_id, *, refresh=False):
    """Return the authenticated CLOB conditional-token balance in shares."""
    params = BalanceAllowanceParams(
        asset_type=AssetType.CONDITIONAL,
        token_id=str(token_id),
    )
    try:
        if refresh:
            client.update_balance_allowance(params)
        result = client.get_balance_allowance(params)
        raw = result.get("balance") if isinstance(result, dict) else getattr(result, "balance", None)
        balance = _decode_clob_fixed6(raw)
        return balance if balance is not None else None
    except Exception as exc:
        log_event(
            "conditional_balance_fail",
            token_id=str(token_id),
            error=str(exc)[:200],
        )
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
        cond = str(p.get("conditionId") or "")
        if not cond:
            continue
        oc = (p.get("outcome") or "").lower()
        if oc not in ("up", "down", "yes", "no"):
            continue
        if oc in ("up", "yes"):
            leg = "up"
        elif oc in ("down", "no"):
            leg = "dn"
        else:
            continue
        size = finite_float(p.get("size", 0), minimum=0)
        avg_price = finite_float(p.get("avgPrice", 0), minimum=0, maximum=1)
        asset = str(p.get("asset") or "")
        if size is None or avg_price is None or not asset:
            continue
        held.setdefault(cond, {})[leg] = {
            "asset": asset,
            "size": size,
            "redeemable": bool(p.get("redeemable", False)),
            "avgPrice": avg_price,
        }
    return held


def merge_tracked_positions(api_held, tracked_meta):
    """Keep confirmed local inventory hedgeable through Data API lag.

    The execution ledger is authoritative for bot-confirmed fills and sells.
    A successful but stale Data API omission must not erase a just-bought
    position from the hedge loop.
    """
    held = {
        str(cond): {
            str(leg): dict(info)
            for leg, info in (legs or {}).items()
            if isinstance(info, dict)
        }
        for cond, legs in (api_held or {}).items()
        if isinstance(legs, dict)
    }
    for cond, meta in tracked_meta.items():
        token = meta.get("bought_token")
        size_value = finite_float(meta.get("bought_size", 0), minimum=0)
        size = size_value if size_value is not None else 0.0
        if (
            not token
            or size <= 0.01
            or meta.get("hedge_closed")
            or meta.get("redeem_pending")
            or meta.get("redeem_confirmed")
        ):
            continue
        bought_leg = str(meta.get("bought_leg") or "").lower()
        if bought_leg == "up":
            leg = "up"
        elif bought_leg in {"down", "dn"}:
            leg = "dn"
        elif str(token) == str(meta.get("up_token") or ""):
            leg = "up"
        elif str(token) == str(meta.get("dn_token") or ""):
            leg = "dn"
        else:
            # Guessing the leg can make the bot sell the opposite token.
            continue
        current = held.setdefault(cond, {}).get(leg) or {}
        current_size = finite_float(current.get("size", 0), minimum=0) or 0.0
        if current_size + 0.01 < size:
            held[cond][leg] = {
                "asset": str(token),
                "size": size,
                "redeemable": bool(current.get("redeemable", False)),
                "avgPrice": float(
                    finite_float(current.get("avgPrice"), minimum=0, maximum=1)
                    or finite_float(meta.get("fill_price"), minimum=0, maximum=1)
                    or 0
                ),
            }
    return held


def add_tracked_market_stubs(markets, held, tracked_meta, now_s):
    """Add hedge/recovery metadata when Gamma discovery is unavailable.

    Exact-order recovery must continue after market expiry and even when an
    ambiguous BUY has not appeared in the positions API yet.
    """
    by_condition = {market.condition_id: market for market in markets}
    recovery_conditions = set(held)
    recovery_conditions.update(
        str(cond)
        for cond, meta in tracked_meta.items()
        if isinstance(meta, dict)
        and (meta.get("buy_uncertain") or meta.get("hedge_uncertain"))
    )
    for cond in recovery_conditions:
        if cond in by_condition:
            continue
        pos = held.get(cond, {})
        up = pos.get("up", {})
        dn = pos.get("dn", {})
        meta = tracked_meta.get(cond, {})
        uncertain = bool(
            meta.get("buy_uncertain") or meta.get("hedge_uncertain")
        )
        if (up.get("redeemable") or dn.get("redeemable")) and not uncertain:
            continue
        up_size = finite_float(up.get("size", 0), minimum=0) or 0.0
        dn_size = finite_float(dn.get("size", 0), minimum=0) or 0.0
        if max(up_size, dn_size) <= 0.01 and not uncertain:
            continue
        end_ts = finite_float(meta.get("end_ts", 0), minimum=0) or 0.0
        if end_ts <= 0 and meta.get("end_date"):
            try:
                end_ts = datetime.fromisoformat(
                    str(meta["end_date"]).replace("Z", "+00:00")
                ).timestamp()
            except (TypeError, ValueError):
                end_ts = 0.0
        if end_ts > 0 and end_ts <= now_s and not uncertain:
            continue
        if end_ts <= 0:
            # Preserve an actually-held position's hedge path. A pure recovery
            # stub stays expired so clearing its quarantine cannot arm entry.
            end_ts = (
                now_s + 86400
                if max(up_size, dn_size) > 0.01
                else now_s
            )
        up_token = str(meta.get("up_token") or up.get("asset") or "")
        dn_token = str(meta.get("dn_token") or dn.get("asset") or "")
        uncertain_token = str(
            meta.get("buy_uncertain_token")
            or meta.get("hedge_uncertain_token")
            or ""
        )
        uncertain_leg = str(
            meta.get("buy_uncertain_leg")
            or meta.get("bought_leg")
            or ""
        ).lower()
        if uncertain_token and uncertain_leg == "up" and not up_token:
            up_token = uncertain_token
        if uncertain_token and uncertain_leg in {"down", "dn"} and not dn_token:
            dn_token = uncertain_token
        by_condition[cond] = MintMarket(
            condition_id=str(cond),
            slug=str(meta.get("slug") or cond),
            question=str(meta.get("question") or f"Tracked position {cond}"),
            end_ts=end_ts,
            series_slug=SERIES_SLUG,
            up_token=up_token,
            dn_token=dn_token,
            active=True,
            closed=False,
            accepting_orders=True,
            neg_risk=False,
            start_ts=finite_float(meta.get("start_ts", 0), minimum=0) or 0.0,
        )
    return sorted(by_condition.values(), key=lambda market: market.end_ts)


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


_book_snapshot_meta = {}
_BOOK_SNAPSHOT_MAX_AGE_S = 2.0
_http_local = threading.local()


def _http_get(url, **kwargs):
    """Use one persistent Session per worker; requests.Session is not thread-safe."""
    session = getattr(_http_local, "session", None)
    if session is None:
        session = requests.Session()
        _http_local.session = session
    return session.get(url, **kwargs)


def _book_timestamp_s(raw):
    """Validate exchange time without treating an unchanged book as stale.

    A direct ``/book`` response is a fresh snapshot even when its timestamp is
    the time of the last book mutation. Quiet books can legitimately retain
    that timestamp for minutes or hours.
    """
    value = finite_float(raw, minimum=0)
    if value is None:
        return None
    if value > 10_000_000_000:
        value /= 1000.0
    now = time.time()
    if value > now + 2.0 or now - value > 30 * 86400:
        return None
    return value


def _valid_book_levels(levels):
    valid = []
    for level in levels or []:
        if not isinstance(level, dict):
            continue
        price = finite_float(level.get("price"), minimum=0, maximum=1)
        size = finite_float(level.get("size"), minimum=0)
        if price is None or size is None or not 0 < price < 1 or size <= 0:
            continue
        valid.append((price, size))
    return valid


def get_book_quote(token_id, expected_condition_id=None):
    """Return (bid_price, bid_size, ask_price, ask_size, mid_price).
    mid_price is None when either side of the book is missing."""
    try:
        book = safe_api_call(client.get_order_book, token_id)
        path = "sdk"
    except Exception as sdk_err:
        try:
            resp = _http_get(
                f"{HOST}/book", params={"token_id": token_id}, timeout=5,
            )
            resp.raise_for_status()
            book = resp.json()
            path = "http"
            log_event("book_quote_fallback_ok", token_id=token_id, sdk_error=str(sdk_err)[:200])
        except Exception as http_err:
            log_event("book_quote_fail", token_id=token_id, sdk_error=str(sdk_err)[:200], http_error=str(http_err)[:200])
            return None, 0.0, None, 0.0, None
    try:
        if not isinstance(book, dict):
            raise ValueError("book response was not an object")
        asset_id = str(book.get("asset_id") or "")
        market_id = str(book.get("market") or "")
        if not asset_id or asset_id != str(token_id):
            raise ValueError("book asset_id mismatch")
        if (
            expected_condition_id
            and (
                not market_id
                or market_id.lower() != str(expected_condition_id).lower()
            )
        ):
            raise ValueError("book market mismatch")
        server_ts = _book_timestamp_s(book.get("timestamp"))
        if server_ts is None:
            raise ValueError("book timestamp missing or stale")
        bids = _valid_book_levels(book.get("bids"))
        asks = _valid_book_levels(book.get("asks"))
        bid_price = None
        bid_size = 0.0
        ask_price = None
        ask_size = 0.0
        mid_price = None
        if bids:
            bid_price, bid_size = max(bids, key=lambda level: level[0])
        if asks:
            ask_price, ask_size = min(asks, key=lambda level: level[0])
        if bid_price is not None and ask_price is not None:
            mid_price = (bid_price + ask_price) / 2.0
        last_trade = finite_float(
            book.get("last_trade_price"), minimum=0, maximum=1,
        )
        min_order_size = finite_float(book.get("min_order_size"), minimum=0)
        tick_size = str(book.get("tick_size") or "")
        _book_snapshot_meta[str(token_id)] = {
            "asset_id": asset_id or str(token_id),
            "market": market_id,
            "server_ts": server_ts,
            "received_mono": time.monotonic(),
            "last_trade": last_trade,
            "min_order_size": min_order_size,
            "tick_size": tick_size,
            "hash": book.get("hash"),
        }
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


def get_quote_fast(
    token_id,
    max_age_s=2.0,
    prefer_rest=False,
    force_rest=False,
    expected_condition_id=None,
):
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
    q = get_book_quote(token_id, expected_condition_id=expected_condition_id)
    _rest_quote_cache[token_id] = (q, now)
    if len(_rest_quote_cache) > _REST_QUOTE_CACHE_MAX:
        prune_rest_caches()
    return q


def hedge_sell_price(bid, tick_size, undercut_ticks, min_price=None):
    """FAK sell at the live bid (undercut, tick-aligned).

    After 35/40 integrity, take whatever bid is there. min_price is ignored so
    a leftover 32¢ config cannot refuse a 20¢ or 1¢ print. One tick is only
    the exchange minimum, not a strategy floor.
    """
    tick = float(tick_size or TICK_SIZE_FALLBACK)
    if tick <= 0:
        tick = 0.01
    undercut = max(0, int(undercut_ticks)) * tick
    raw = float(bid or 0) - undercut
    aligned = (int(raw / tick + 1e-12)) * tick
    return max(tick, aligned)


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
        resp = _http_get(
            f"{HOST}/last-trade-price", params={"token_id": token_id}, timeout=5,
        )
        resp.raise_for_status()
        data = resp.json()
        price = data.get("price") if isinstance(data, dict) else None
        out = finite_float(price, minimum=0, maximum=1)
        if out is None:
            raise ValueError("last-trade response missing a valid price")
        _last_trade_cache[token_id] = (out, now)
        if len(_last_trade_cache) > _REST_QUOTE_CACHE_MAX:
            prune_rest_caches()
        return out
    except Exception as e:
        log_event("last_trade_fail", token_id=token_id, error=str(e)[:200])
        if cached is not None and now - cached[1] <= _BOOK_SNAPSHOT_MAX_AGE_S:
            return cached[0]
        return get_book_snapshot_last_trade(token_id)


def get_book_snapshot_last_trade(token_id):
    meta = _book_snapshot_meta.get(str(token_id)) or {}
    received = meta.get("received_mono")
    if received is None:
        return None
    age = time.monotonic() - float(received)
    if age < 0 or age > _BOOK_SNAPSHOT_MAX_AGE_S:
        return None
    return finite_float(meta.get("last_trade"), minimum=0, maximum=1)


def polymarket_display_price(bid, ask, last_trade):
    """Probability a human sees on Polymarket for this outcome."""
    bid = finite_float(bid, minimum=0, maximum=1)
    ask = finite_float(ask, minimum=0, maximum=1)
    last_trade = finite_float(last_trade, minimum=0, maximum=1)
    if bid is not None and ask is not None:
        if ask < bid:
            return None
        if (ask - bid) <= POLYMARKET_GUI_SPREAD + 1e-12:
            return (bid + ask) / 2.0
    return last_trade


def entry_book_ok(bid, ask, max_spread, min_bid):
    """True only when top-of-book is tight and bid supports the ask story.

    Wide books (ask 98¢ / bid 1¢) produce fake gate prices — last-trade GUI can
    still look like a winner while there is no real bid under the ask.
    """
    bid = finite_float(bid, minimum=0, maximum=1)
    ask = finite_float(ask, minimum=0, maximum=1)
    max_spread = finite_float(max_spread, minimum=0, maximum=1)
    min_bid = finite_float(min_bid, minimum=0, maximum=1)
    if bid is None or ask is None or max_spread is None or min_bid is None:
        return False, "missing_side"
    if ask < bid:
        return False, "crossed"
    spread = ask - bid
    if spread > max_spread + 1e-12:
        return False, "wide_spread"
    if bid + 1e-12 < min_bid:
        return False, "bid_too_low"
    return True, "ok"


def hedge_book_ok(bid, ask, threshold, max_spread, require_ask_max):
    """True only when the held book actually collapsed — not a lone penny bid.

    A 1¢ bid under a 99¢ ask is illiquidity/spoof, not a reversal. Require:
      bid ≤ threshold, ask ≤ require_ask_max, and spread ≤ max_spread.
    """
    bid = finite_float(bid, minimum=0, maximum=1)
    ask = finite_float(ask, minimum=0, maximum=1)
    threshold = finite_float(threshold, minimum=0, maximum=1)
    max_spread = finite_float(max_spread, minimum=0, maximum=1)
    require_ask_max = finite_float(require_ask_max, minimum=0, maximum=1)
    if (
        bid is None
        or ask is None
        or threshold is None
        or max_spread is None
        or require_ask_max is None
    ):
        return False, "missing_side"
    if bid > threshold + 1e-12:
        return False, "bid_above"
    if ask > require_ask_max + 1e-12:
        return False, "ask_too_high"
    if ask < bid:
        return False, "crossed"
    if (ask - bid) > max_spread + 1e-12:
        return False, "wide_spread"
    return True, "ok"



def quoted_buy_shares(budget, ask, ask_size=None):
    """Shares to buy at the quoted ask for this dollar budget.

    Posts a **limit** FAK at ``ask`` sized ``budget/ask``. Leftover USDC
    cannot walk cheaper levels (that was the 39-share / 6¢ blow-up).
    Displayed ``ask_size`` is not a cap — a thin top still posts the
    dollar size; unmatched remainder dies on the FAK.
    """
    budget = finite_float(budget, minimum=0)
    ask = finite_float(ask, minimum=0, maximum=1)
    if budget is None or ask is None:
        return 0.0
    if budget < 0.01 or ask <= 0:
        return 0.0
    shares = budget / ask
    # CLOB taker amounts allow at most four decimal places.
    shares = math.floor(shares * 10000 + 1e-12) / 10000
    return shares if shares >= 0.01 else 0.0


def buy_fill_walked(filled, quoted_shares, ratio=1.05):
    """True when confirmed shares exceed the quoted budget/ask size."""
    filled = finite_float(filled, minimum=0) or 0.0
    quoted = finite_float(quoted_shares, minimum=0) or 0.0
    if filled <= 0:
        return False
    if quoted < 0.01:
        return False
    return filled > quoted * float(ratio) + 1e-9


def classify_buy_fill(avg, filled, quoted_shares, min_price, toxic_below):
    """below_band / toxic_fill from true average and share-walk."""
    avg = finite_float(avg, minimum=0, maximum=1) or 0.0
    filled = finite_float(filled, minimum=0) or 0.0
    min_price = finite_float(min_price, minimum=0, maximum=1) or 0.0
    toxic_below = finite_float(toxic_below, minimum=0, maximum=1) or 0.0
    walked = buy_fill_walked(filled, quoted_shares)
    below_band = walked or (min_price > 0 and avg + 1e-9 < min_price)
    toxic = walked or (toxic_below > 0 and avg + 1e-9 < toxic_below)
    return below_band, toxic


def implied_buy_average(cost, shares, fallback_price=None):
    """USDC spent / shares. Extra shares from a walk do not cost extra gate-ask."""
    cost = finite_float(cost, minimum=0) or 0.0
    shares = finite_float(shares, minimum=0) or 0.0
    if shares <= 0:
        return finite_float(fallback_price, minimum=0, maximum=1) or 0.0
    if cost > 0:
        return cost / shares
    fb = finite_float(fallback_price, minimum=0, maximum=1)
    return float(fb or 0.0)


def reconcile_hedge_sold(held_size, confirmed_sold, confirmed_proceeds, api_bal, last_limit):
    """Merge CLOB-confirmed sells with a Data API balance without erasing confirms.

    Data API lag after a trade is normal. A single low balance must never add
    unconfirmed fills on top of CLOB confirms or reduce confirmed
    sold/proceeds. A possible full exit is only a ghost *candidate* — callers
    must require repeated stable zeros before promoting the unconfirmed tail.
    """
    held_size = float(held_size or 0)
    confirmed_sold = float(confirmed_sold or 0)
    confirmed_proceeds = float(confirmed_proceeds or 0)
    last_limit = float(last_limit or 0)
    if api_bal is None:
        rem = max(0.0, held_size - confirmed_sold)
        return {
            "effective_sold": confirmed_sold,
            "proceeds": confirmed_proceeds,
            "rem": 0.0 if rem < 0.01 else rem,
            "api_sold": None,
            "lag": False,
            "ghost_candidate": False,
            "balance_unverified": True,
        }
    api_bal = float(api_bal)
    api_sold = max(0.0, held_size - api_bal)
    lag = confirmed_sold > api_sold + 0.01
    # Never promote one Data API read into proceeds or a closed hedge. This is
    # especially important after a partial CLOB confirmation: a stale zero used
    # to fabricate the entire unconfirmed tail and disable future hedges.
    proceeds = confirmed_proceeds
    effective_sold = confirmed_sold
    rem = max(0.0, held_size - effective_sold)
    if rem < 0.01:
        rem = 0.0
    return {
        "effective_sold": effective_sold,
        "proceeds": proceeds,
        "rem": rem,
        "api_sold": api_sold,
        "lag": lag,
        "ghost_candidate": (
            api_bal < 0.01 and api_sold > confirmed_sold + 0.01
        ),
        "balance_unverified": False,
    }


def stable_zero_balances(reads):
    """True when every read is a successful near-zero (repeated ghost evidence)."""
    vals = list(reads)
    if len(vals) < 2:
        return False
    for b in vals:
        if b is None or float(b) > 0.01:
            return False
    return True


def gc_par_redeem(gc_meta, hedge_proceeds, redeem_value):
    """Return only explicitly verified redemption value; never invent par."""
    if float(redeem_value or 0) != 0:
        return float(redeem_value)
    return 0.0


def gc_can_finalize(gc_meta):
    """Only terminal execution evidence may delete durable market state."""
    if (
        gc_meta.get("buy_uncertain")
        or gc_meta.get("hedge_uncertain")
        or gc_meta.get("redeem_pending")
    ):
        return False
    if float(gc_meta.get("pnl_redeem_value", 0) or 0) > 0:
        return True
    # Markets that never submitted/finalized an entry have no execution state
    # to preserve once they leave discovery.
    if (
        not gc_meta.get("bought_token")
        and float(gc_meta.get("bought_size", 0) or 0) <= 0.01
        and float(gc_meta.get("pnl_entry_cost", 0) or 0) <= 0
    ):
        return True
    return bool(
        gc_meta.get("hedge_closed")
        and float(gc_meta.get("bought_size", 0) or 0) <= 0.01
    )


_trade_detail_cache = {}



def _fill_fee_usdc(filled, price, fee_schedule):
    """Calculate the CLOB V2 taker fee, rounded to protocol precision."""
    if not isinstance(fee_schedule, dict):
        return None
    shares = finite_float(filled, minimum=0)
    price_f = finite_float(price, minimum=0, maximum=1)
    rate = finite_float(fee_schedule.get("rate"), minimum=0, maximum=1)
    exponent = finite_float(
        fee_schedule.get("exponent"), minimum=0, maximum=10,
    )
    if None in (shares, price_f, rate, exponent):
        return None
    fee = shares * rate * ((price_f * (1 - price_f)) ** exponent)
    if not math.isfinite(fee) or fee < 0:
        return None
    return round(fee, 5)


def fill_cost_usdc(
    result, filled, limit_price, spend_cap, fee_schedule=None,
):
    """USDC spent on a BUY.

    CLOB v2 market BUY: makingAmount = USDC paid, takingAmount = shares received.
    Zero/missing makingAmount is treated as unavailable (common on delayed stubs),
    not as a free fill — fall back to limit × shares capped by spend.
    """
    filled = float(filled or 0)
    limit_price = float(limit_price or 0)
    spend_cap = float(spend_cap or 0)
    if filled <= 0:
        return 0.0
    d = _result_as_dict(result)
    gross = None
    for k in ("average_price", "avg_price"):
        if d.get(k) is not None:
            avg = finite_float(d[k], minimum=0, maximum=1)
            if avg is not None and avg > 0:
                gross = filled * avg
                break
    try:
        making = d.get("makingAmount", d.get("making_amount"))
        if gross is None and making is not None:
            making_f = _decode_clob_response_amount(
                making,
                expected=min(spend_cap, filled * limit_price)
                if spend_cap > 0 else filled * limit_price,
            ) or 0.0
            if making_f > 1e-12:
                gross = making_f
    except (TypeError, ValueError):
        pass
    if gross is None:
        gross = filled * limit_price
    execution_price = gross / filled if filled > 0 else limit_price
    trade_financials = _confirmed_trade_financials(
        d.get("tradeIDs") or d.get("trade_ids"),
        expected_size=filled,
        fee_schedule=fee_schedule,
    )
    if trade_financials is not None:
        gross = trade_financials["gross"]
        fee = trade_financials["fee"]
    else:
        fee = _fill_fee_usdc(filled, execution_price, fee_schedule)
    if fee is None:
        cost = gross
    else:
        cost = gross + fee
    return min(spend_cap, cost) if spend_cap > 0 else cost


# ------------------------- TICK SIZE -------------------------

_tick_size_cache = {}


def get_tick_size_cached(token_id):
    ws_tick = book_ws.tick_size(token_id)
    if ws_tick:
        _tick_size_cache[token_id] = ws_tick
        return ws_tick
    snapshot_tick = str(
        (_book_snapshot_meta.get(str(token_id)) or {}).get("tick_size") or ""
    )
    if snapshot_tick in {"0.1", "0.01", "0.005", "0.0025", "0.001", "0.0001"}:
        _tick_size_cache[token_id] = snapshot_tick
        return snapshot_tick
    if token_id in _tick_size_cache:
        return _tick_size_cache[token_id]
    tick = str(TICK_SIZE_FALLBACK)
    try:
        result = client.get_tick_size(token_id)
        if str(result) in {"0.1", "0.01", "0.005", "0.0025", "0.001", "0.0001"}:
            tick = str(result)
        elif result:
            raise ValueError(f"unsupported tick size {result}")
    except Exception as e:
        log_event("tick_size_lookup_fail", token_id=token_id, error=str(e)[:200], fallback=tick)
    _tick_size_cache[token_id] = tick
    return tick


# ------------------------- ORDER HELPERS -------------------------

def extract_order_id(order_obj):
    if isinstance(order_obj, dict):
        return order_obj.get("orderID") or order_obj.get("id")
    return getattr(order_obj, "orderID", None) or getattr(order_obj, "id", None) or (str(order_obj) if order_obj is not None else None)


def _decode_clob_fixed6(raw):
    """Decode a documented fixed-math CLOB amount with six decimals."""
    if raw is None or raw == "":
        return None
    try:
        value = Decimal(str(raw)) / Decimal(1_000_000)
    except (InvalidOperation, TypeError, ValueError):
        return None
    if not value.is_finite() or value < 0:
        return None
    return float(value)


def _decode_clob_response_amount(raw, *, expected=None):
    """Decode production responses that have appeared in two incompatible forms.

    The OpenAPI specifies fixed-six integer strings, while real v2 POST/trade
    responses have also returned human-unit decimals (for example ``"5.12"``).
    Select between those representations using the signed/requested amount.
    """
    if raw is None or raw == "":
        return None
    try:
        value = Decimal(str(raw))
    except (InvalidOperation, TypeError, ValueError):
        return None
    if not value.is_finite() or value < 0:
        return None
    human = float(value)
    fixed = float(value / Decimal(1_000_000))
    expected_f = finite_float(expected, minimum=0)
    if expected_f is not None and expected_f > 0:
        human_err = abs(human - expected_f) / expected_f
        fixed_err = abs(fixed - expected_f) / expected_f
        best, best_err = (
            (human, human_err) if human_err <= fixed_err else (fixed, fixed_err)
        )
        # Walks (27 sh vs a 3-sh quote) are far from expected — do not pick the
        # "less wrong" 1e-6-fixed candidate. Fall through to the heuristic.
        if best_err <= 0.5:
            return best
    raw_text = str(raw).strip().lower()
    if "." in raw_text or "e" in raw_text:
        return human
    # Positive integer values below one share cannot satisfy these bots' CLOB
    # minimums; treat small integers as human units and large ones as fixed-six.
    return fixed if abs(human) >= 10_000 else human


def _string_list(value):
    if value is None or value == "":
        return []
    if isinstance(value, (list, tuple, set)):
        return [str(item) for item in value if item]
    return [str(value)]


def _load_trade_details(trade_ids):
    """Load exact trade rows, caching only immutable terminal outcomes."""
    ids = _string_list(trade_ids)
    if not ids:
        return []
    details = []
    for trade_id in dict.fromkeys(ids):
        trade = _trade_detail_cache.get(trade_id)
        if trade is None:
            try:
                trades = safe_api_call(
                    client.get_trades,
                    TradeParams(id=trade_id),
                    only_first_page=True,
                )
            except Exception:
                return None
            trade = next(
                (
                    row for row in (trades or [])
                    if isinstance(row, dict) and str(row.get("id") or "") == trade_id
                ),
                None,
            )
            if trade is None:
                return None
            status = str(trade.get("status") or "").upper()
            if (
                status in {"FAILED", "TRADE_STATUS_FAILED"}
                or (
                    status in {"CONFIRMED", "TRADE_STATUS_CONFIRMED"}
                    and (
                        trade.get("transaction_hash")
                        or trade.get("transactionHash")
                    )
                )
            ):
                _trade_detail_cache[trade_id] = dict(trade)
        details.append(dict(trade))
    return details


def _confirmed_trade_financials(trade_ids, *, expected_size, fee_schedule):
    """Aggregate the terminal confirmed subset of an order's exact trades."""
    trades = _load_trade_details(trade_ids)
    if not trades:
        return None
    expected = finite_float(expected_size, minimum=0)
    confirmed = []
    for trade in trades:
        status = str(trade.get("status") or "").upper()
        if status in {"FAILED", "TRADE_STATUS_FAILED"}:
            continue
        if (
            status not in {"CONFIRMED", "TRADE_STATUS_CONFIRMED"}
            or not (trade.get("transaction_hash") or trade.get("transactionHash"))
        ):
            return None
        size = _decode_clob_response_amount(
            trade.get("size"), expected=expected,
        )
        price = finite_float(trade.get("price"), minimum=0, maximum=1)
        if size is None or size <= 0 or price is None or not 0 < price < 1:
            return None
        confirmed.append((size, price))
    if not confirmed:
        return None
    shares = sum(size for size, _ in confirmed)
    if expected is not None and expected > 0:
        tolerance = max(0.01, expected * 0.01)
        # Undersize vs quoted is still a decode-safety reject. Oversize is a
        # BUY walk (USDC market leftover / extra fills) — keep the true VWAP.
        if shares + 1e-9 < expected - tolerance:
            return None
    gross = sum(size * price for size, price in confirmed)
    fees = [
        _fill_fee_usdc(size, price, fee_schedule)
        for size, price in confirmed
    ]
    fee = None if any(item is None for item in fees) else sum(fees)
    return {"shares": shares, "gross": gross, "fee": fee}


def _trade_settlement_state(trade_ids):
    trades = _load_trade_details(trade_ids)
    if not trades:
        return "pending"
    terminal_states = []
    for trade in trades:
        status = str(trade.get("status") or "").upper()
        if status in {"FAILED", "TRADE_STATUS_FAILED"}:
            terminal_states.append("failed")
            continue
        if (
            status not in {"CONFIRMED", "TRADE_STATUS_CONFIRMED"}
            or not (trade.get("transaction_hash") or trade.get("transactionHash"))
        ):
            return "pending"
        terminal_states.append("confirmed")
    if terminal_states and all(state == "failed" for state in terminal_states):
        return "failed"
    if terminal_states and all(state == "confirmed" for state in terminal_states):
        return "confirmed"
    # Some matches settled and others failed. Never erase the confirmed subset
    # by classifying the whole order as failed.
    return "partial"


def get_order_details(order_id, expected_size=None):
    if not order_id:
        return None
    try:
        result = safe_api_call(client.get_order, order_id)
        if isinstance(result, dict):
            matched_raw = (
                result.get("size_matched")
                or 0
            )
            size_raw = (
                result.get("original_size")
                or result.get("originalSize")
                or result.get("size")
                or 1
            )
            return {
                "status": result.get("status", "UNKNOWN"),
                "size_matched": _decode_clob_response_amount(
                    matched_raw, expected=expected_size,
                ) or 0.0,
                "size": _decode_clob_response_amount(
                    size_raw, expected=expected_size,
                ) or 0.0,
                "trade_ids": _string_list(
                    result.get("associate_trades") or result.get("associateTrades")
                ),
                "asset_id": result.get("asset_id"),
                "market": result.get("market"),
                "side": result.get("side"),
                "price": finite_float(result.get("price"), minimum=0, maximum=1),
            }
        return {
            "status": getattr(result, "status", "UNKNOWN"),
            "size_matched": _decode_clob_response_amount(
                getattr(result, "size_matched", 0),
                expected=expected_size,
            ) or 0.0,
            "size": _decode_clob_response_amount(
                getattr(result, "original_size", getattr(result, "size", 0)),
                expected=expected_size,
            ) or 0.0,
            "trade_ids": _string_list(
                getattr(
                    result,
                    "associate_trades",
                    getattr(result, "associateTrades", []),
                )
            ),
            "asset_id": getattr(result, "asset_id", None),
            "market": getattr(result, "market", None),
            "side": getattr(result, "side", None),
            "price": finite_float(
                getattr(result, "price", None), minimum=0, maximum=1,
            ),
        }
    except Exception as e:
        err = str(e).lower()
        if "not found" in err or "404" in err:
            return {"status": "NOT_FOUND"}
        return None


def buy_shares_from_result(result, expected=None):
    """BUY shares from an immediate CLOB response (size_matched or takingAmount)."""
    d = _result_as_dict(result)
    sm = _decode_clob_response_amount(
        d.get("size_matched"), expected=expected,
    )
    if sm is not None and sm > 0:
        return sm
    # V2 market BUY: takingAmount = shares received.
    taking = _decode_clob_response_amount(
        d.get("takingAmount", d.get("taking_amount")),
        expected=expected,
    )
    if taking is not None and taking > 0:
        return taking
    return 0.0


def confirm_fill_size(result, oid, requested, *, wait_delayed_s=2.0, poll_s=0.25, side="BUY"):
    """Return confirmed matched size.

    Immediate making/taking amounts are fill evidence only for a terminal
    ``matched`` response. A ``delayed`` response can carry the full signed
    order amounts before any matching has happened.

    For non-terminal responses, only GET-order ``size_matched`` is trusted.
    Transient get_order 404s are polled through — not treated as terminal empty —
    because matched FAKs often 404 briefly before the order index catches up.
    """
    d = _result_as_dict(result)
    post_status = str(d.get("status") or "").strip().lower()
    matched = 0.0
    post_trade_ids = _string_list(d.get("tradeIDs") or d.get("trade_ids"))
    post_settlement = _trade_settlement_state(post_trade_ids)
    post_financials = None
    if post_settlement in {"confirmed", "partial"}:
        post_financials = _confirmed_trade_financials(
            post_trade_ids,
            expected_size=requested,
            fee_schedule=None,
        )
    if (
        post_status in {"matched", "order_status_matched"}
        and post_settlement in {"confirmed", "partial"}
    ):
        if post_financials is not None:
            matched = post_financials["shares"]
        elif post_settlement == "partial":
            matched = 0.0
        elif side == "BUY":
            matched = buy_shares_from_result(result, expected=requested)
        else:
            matched = _decode_clob_response_amount(
                d.get("size_matched"), expected=requested,
            ) or 0.0
            if matched <= 0:
                # SELL: makingAmount is shares sold.
                matched = _decode_clob_response_amount(
                    d.get("makingAmount", d.get("making_amount")),
                    expected=requested,
                ) or 0.0
    if matched > 0:
        matched = float(matched)
        if requested and matched > float(requested) * 1.01 + 1e-6:
            if str(side).upper() == "BUY":
                log_event(
                    "buy_fill_walk", order_id=str(oid or "")[:24],
                    matched=matched, requested=requested, source="post",
                )
                return matched
            log_event(
                "order_fill_size_invalid", order_id=str(oid or "")[:24],
                matched=matched, requested=requested, side=side, source="post",
            )
            return 0.0
        return matched
    if not oid:
        return 0.0

    deadline = time.time() + float(wait_delayed_s)
    last_status = None
    saw_not_found = False
    while True:
        details = get_order_details(oid, expected_size=requested)
        if details:
            last_status = str(details.get("status") or "")
            if last_status == "NOT_FOUND":
                # Transient index lag — keep polling until deadline.
                saw_not_found = True
            else:
                sm = details.get("size_matched", 0)
                settlement = _trade_settlement_state(details.get("trade_ids"))
                if settlement == "failed":
                    return 0.0
                trade_financials = None
                if settlement in {"confirmed", "partial"}:
                    trade_financials = _confirmed_trade_financials(
                        details.get("trade_ids"),
                        expected_size=sm or requested,
                        fee_schedule=None,
                    )
                if trade_financials is not None:
                    matched = trade_financials["shares"]
                elif sm and settlement == "confirmed":
                    matched = float(sm)
                else:
                    matched = 0.0
                if matched > 0:
                    if requested and matched > float(requested) * 1.01 + 1e-6:
                        if str(side).upper() == "BUY":
                            log_event(
                                "buy_fill_walk", order_id=str(oid)[:24],
                                matched=matched, requested=requested,
                                source="get_order",
                            )
                            return matched
                        log_event(
                            "order_fill_size_invalid", order_id=str(oid)[:24],
                            matched=matched, requested=requested, side=side,
                            source="get_order",
                        )
                        return 0.0
                    return matched
                st = last_status.lower()
                matched_hint = finite_float(sm, minimum=0) or 0.0
                if st in ("canceled", "cancelled", "rejected", "expired", "failed"):
                    # A canceled FAK can still have matched size whose trades
                    # have not reached CONFIRMED+tx finality. Never treat that
                    # as a proven empty fill — keep polling, then time out as
                    # unconfirmed (callers quarantine) instead of "empty".
                    if matched_hint > 0 or settlement == "pending":
                        pass
                    else:
                        return 0.0
        if time.time() >= deadline:
            if last_status or saw_not_found:
                log_event(
                    "order_confirm_timeout",
                    order_id=str(oid)[:24],
                    status=last_status or ("NOT_FOUND" if saw_not_found else None),
                    requested=requested,
                    side=side,
                )
            return float(matched or 0.0)
        time.sleep(float(poll_s))


def _result_as_dict(result):
    if isinstance(result, dict):
        return result
    if result is None:
        return {}
    out = {}
    for k in (
        "status", "success", "errorMsg", "orderID",
        "transactionsHashes", "tradeIDs",
        "average_price", "avg_price", "price", "size_matched",
        "takingAmount", "taking_amount", "makingAmount", "making_amount",
    ):
        if hasattr(result, k):
            out[k] = getattr(result, k)
    if hasattr(result, "__dict__"):
        out.update({k: v for k, v in vars(result).items() if not k.startswith("_")})
    return out


def fill_proceeds(result, filled, limit_price, fee_schedule=None):
    """Net USDC proceeds for a SELL fill, including the taker fee."""
    filled = finite_float(filled, minimum=0) or 0.0
    limit_price = finite_float(limit_price, minimum=0, maximum=1) or 0.0
    if filled <= 0 or limit_price <= 0:
        return 0.0
    d = _result_as_dict(result)
    gross = None
    for k in ("average_price", "avg_price"):
        if d.get(k) is not None:
            avg_price = finite_float(d[k], minimum=0, maximum=1)
            if avg_price is not None and avg_price > 0:
                gross = filled * avg_price
                break
    # Some CLOB responses expose making/taking amounts (shares vs USDC).
    try:
        taking = d.get("takingAmount", d.get("taking_amount"))
        making = d.get("makingAmount", d.get("making_amount"))
        if gross is None and taking is not None and making is not None:
            taking_f = _decode_clob_response_amount(
                taking, expected=filled * limit_price,
            ) or 0.0
            making_f = _decode_clob_response_amount(
                making, expected=filled,
            ) or 0.0
            if making_f > 0 and abs(making_f - filled) / max(filled, 1e-9) < 0.25:
                gross = taking_f
            elif taking_f > 0 and abs(taking_f - filled) / max(filled, 1e-9) < 0.25:
                gross = making_f
    except (TypeError, ValueError):
        pass
    if gross is None:
        gross = filled * limit_price
    execution_price = gross / filled
    trade_financials = _confirmed_trade_financials(
        d.get("tradeIDs") or d.get("trade_ids"),
        expected_size=filled,
        fee_schedule=fee_schedule,
    )
    if trade_financials is not None:
        gross = trade_financials["gross"]
        fee = trade_financials["fee"]
    else:
        fee = _fill_fee_usdc(filled, execution_price, fee_schedule)
    return max(0.0, gross - fee) if fee is not None else gross


def signed_order_id(signed_order, *, neg_risk=False):
    """Compute the deterministic v2 order hash before any POST."""
    config = get_contract_config(CHAIN_ID)
    exchange = (
        config.neg_risk_exchange_v2 if neg_risk else config.exchange_v2
    )
    builder = ExchangeOrderBuilderV2(exchange, CHAIN_ID, client.signer)
    typed_data = builder.build_order_typed_data(signed_order)
    return builder.build_order_hash(typed_data)


def definitive_order_rejection(exc):
    """True only when the server explicitly rejected without accepting a POST."""
    status = getattr(exc, "status_code", None)
    return isinstance(status, int) and status in {400, 401, 403, 404, 422}


def inspect_uncertain_order(
    order_id,
    *,
    side,
    requested,
    token_id=None,
    condition_id=None,
    limit_price=0.0,
    fee_schedule=None,
    spend_cap=None,
    trade_ids=None,
):
    """Reconcile one exact signed order without creating a replacement order."""
    details = get_order_details(order_id, expected_size=requested)
    if not details:
        return {"state": "pending"}
    status = str(details.get("status") or "").lower()
    if status == "not_found":
        return {"state": "not_found"}
    if (
        token_id
        and details.get("asset_id")
        and str(details["asset_id"]) != str(token_id)
    ):
        return {"state": "identity_mismatch"}
    if (
        condition_id
        and details.get("market")
        and str(details["market"]).lower() != str(condition_id).lower()
    ):
        return {"state": "identity_mismatch"}
    if details.get("side") and str(details["side"]).upper() != str(side).upper():
        return {"state": "identity_mismatch"}
    resolved_trade_ids = _string_list(details.get("trade_ids")) or _string_list(
        trade_ids
    )
    settlement = _trade_settlement_state(resolved_trade_ids)
    if settlement == "failed":
        return {"state": "failed"}
    filled = finite_float(details.get("size_matched"), minimum=0) or 0.0
    trade_financials = None
    if settlement in {"confirmed", "partial"}:
        trade_financials = _confirmed_trade_financials(
            resolved_trade_ids,
            expected_size=filled or requested,
            fee_schedule=fee_schedule,
        )
        if trade_financials is not None:
            filled = trade_financials["shares"]
        elif settlement == "partial":
            return {"state": "identity_mismatch"}
    requested_f = finite_float(requested, minimum=0) or 0.0
    buy_walk = (
        str(side).upper() == "BUY"
        and requested_f > 0
        and filled > requested_f * 1.01 + 1e-6
    )
    if requested_f <= 0 or (filled > requested_f * 1.01 + 1e-6 and not buy_walk):
        return {"state": "identity_mismatch"}
    if settlement in {"confirmed", "partial"} and filled > 0:
        price = (
            finite_float(details.get("price"), minimum=0, maximum=1)
            or finite_float(limit_price, minimum=0, maximum=1)
            or 0.0
        )
        confirmed_filled = filled if buy_walk else min(requested_f, filled)
        if trade_financials is not None:
            gross = trade_financials["gross"]
            fee = trade_financials["fee"]
        else:
            gross = confirmed_filled * price
            fee = _fill_fee_usdc(confirmed_filled, price, fee_schedule)
        if str(side).upper() == "BUY":
            cap = finite_float(spend_cap, minimum=0)
            if trade_financials is not None:
                value = gross if fee is None else gross + fee
            elif buy_walk and cap is not None and cap > 0:
                # No trade VWAP: walked fills spent the posted USDC, not
                # shares × gate ask.
                value = cap
            elif fee is None and fee_schedule is not None:
                value = cap if cap is not None else gross
            else:
                value = gross + (fee or 0.0)
            if cap is not None and cap > 0:
                value = min(cap, value)
        else:
            value = max(0.0, gross - fee) if fee is not None else gross
        return {
            "state": "confirmed",
            "filled": confirmed_filled,
            "value": value,
        }
    terminal_empty = (
        "invalid" in status
        or "cancel" in status
        or status in {"rejected", "expired", "failed", "unmatched"}
    )
    if terminal_empty and filled <= 0:
        return {"state": "empty"}
    return {"state": "pending", "matched": filled}


_BUY_UNCERTAIN_KEYS = (
    "buy_uncertain",
    "buy_uncertain_at",
    "buy_uncertain_token",
    "buy_uncertain_leg",
    "buy_uncertain_baseline",
    "buy_uncertain_attempt",
    "buy_uncertain_spend",
    "buy_uncertain_price",
    "buy_uncertain_order_id",
    "buy_uncertain_order_size",
    "buy_uncertain_trade_ids",
    "buy_uncertain_known_size",
    "buy_uncertain_known_cost",
    "buy_uncertain_observed_size",
    "buy_uncertain_observed_at",
    "buy_uncertain_observed_count",
)

_HEDGE_UNCERTAIN_KEYS = (
    "hedge_uncertain",
    "hedge_uncertain_at",
    "hedge_uncertain_token",
    "hedge_uncertain_attempt",
    "hedge_uncertain_remaining",
    "hedge_uncertain_price",
    "hedge_uncertain_confirmed_sold",
    "hedge_uncertain_confirmed_proceeds",
    "hedge_uncertain_order_id",
    "hedge_uncertain_order_size",
    "hedge_uncertain_trade_ids",
    "hedge_uncertain_status",
    "hedge_uncertain_sold_before",
    "hedge_uncertain_proceeds_before",
    "hedge_uncertain_position_size",
    "hedge_uncertain_pnl_before",
)


def clear_uncertain_fields(meta, keys):
    for key in keys:
        meta.pop(key, None)


# ------------------------- BUY -------------------------

def buy_market_with_retry(
    token_id,
    budget,
    max_price,
    tick_size="0.01",
    max_retries=3,
    min_price=0.0,
    on_fill=None,
    on_submit=None,
    condition_id=None,
    pre_submit=None,
):
    """Buy token_id via FAK, sized in shares at the quoted ask.

    Sends ``budget/ask`` shares as a **limit** FAK at the live ask (capped
    by ``buy_max_spend``). This is not a USDC market order: leftover dollars
    would walk cheaper asks (9¢ junk under an 80¢ quote). Displayed top size
    does not shrink the order — a thin book fills what it can at that ask.

    Band is still min_price–max_price; this only pins execution to an
    in-band ask. Max shares at $2.50 / 75¢ is ~3.3, never a 39-share 6¢ bag.

    Returns (shares_bought, usdc_spent, status). status is filled|ambiguous|empty|aborted|persist_fail|dry.
    spent may be estimated when the exchange
    omits makingAmount (delayed stubs); shares > 0 always means inventory exists.

    on_fill(total_bought, total_spent) is invoked after every confirmed attempt so
    the caller can durable-save state before the next retry / crash. If on_fill
    raises, no further orders are posted.

    on_submit(baseline, attempt, spend, price, intent) is durable write-ahead
    state. It runs before every POST so a process crash cannot lose an in-flight
    attempt. ``intent`` includes the deterministic signed order id and amounts.

    Ambiguous POST outcomes (exception, falsy response, or non-terminal response
    with no confirmed fill) reconcile once and STOP — never retry with the full
    budget (accepted-then-timeout double spend).
    """
    total_bought = 0.0
    spent = 0.0
    budget = float(budget)
    if DRY_RUN:
        console.print(
            f"  [bold black on yellow][DRY BUY][/] would BUY ≤{budget:.2f} USDC of "
            f"{str(token_id)[:12]}… at the quoted ask (band {min_price:.3f}–{max_price:.3f})"
        )
        log_event("dry_buy", token_id=token_id, budget=budget, max_price=max_price, min_price=min_price)
        return 0.0, 0.0, "dry"
    fee_schedule = None

    def _token_balance():
        clob_balance = check_clob_token_balance(token_id, refresh=True)
        if clob_balance is not None:
            return clob_balance
        if condition_id:
            return check_token_balance(token_id, condition_id)
        return check_token_balance(token_id)

    bal_baseline = _token_balance()
    if bal_baseline is None:
        # Cannot ghost-reconcile safely without a baseline — refuse to post.
        log_event("buy_abort_no_baseline", token_id=token_id)
        console.print("  [bold red][BUY ABORT][/] positions baseline unavailable")
        return 0.0, 0.0, "aborted"

    persist_failed = False
    max_shares = 0.0

    def _persist():
        nonlocal persist_failed
        if not on_fill or total_bought <= 0:
            return
        try:
            on_fill(total_bought, spent)
        except Exception as e:
            persist_failed = True
            log_event("buy_on_fill_fail", error=str(e)[:200], bought=total_bought, spent=spent)
            console.print(f"  [bold red][PERSIST FAIL][/] aborting further buys: {e}")
            raise

    def _reconcile_ghost(spend_cap, price, attempt, via):
        """Pull delayed inventory once. Returns (filled, fill_cost) or (0, 0)."""
        for wait_s in (float(HEDGE_GHOST_SLEEP_S), 1.0, 2.0):
            time.sleep(wait_s)
            bal_after = _token_balance()
            if bal_after is None:
                continue
            expected = float(bal_baseline) + total_bought
            delta = bal_after - expected
            if delta > 0.01:
                # Walked bags spent the posted USDC, not extra shares × gate ask.
                if buy_fill_walked(delta, max_shares):
                    fill_cost = float(spend_cap)
                else:
                    gross = delta * float(price)
                    fee = _fill_fee_usdc(delta, price, fee_schedule)
                    fill_cost = min(
                        float(spend_cap),
                        gross + (fee or 0.0),
                    )
                log_event(
                    "buy_ghost_fill", token_id=token_id, filled=delta,
                    fill_cost=round(fill_cost, 4), ask=price, attempt=attempt,
                    bal_after=bal_after, expected=expected, via=via,
                    quoted_shares=max_shares,
                )
                return delta, fill_cost
        return 0.0, 0.0

    for attempt in range(max_retries):
        if persist_failed:
            break
        remaining_budget = budget - spent
        if remaining_budget < 0.01:
            break
        fresh_bid, _, fresh_ask, fresh_ask_size, _ = get_quote_fast(
            token_id,
            prefer_rest=True,
            force_rest=True,
            expected_condition_id=condition_id,
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
        fresh_ask_size = finite_float(fresh_ask_size, minimum=0)
        if fresh_ask_size is None or fresh_ask_size < 0.01:
            console.print(
                f"  [dim yellow][THIN ASK][/] displayed size {fresh_ask_size} · "
                f"posting budget/ask anyway · attempt {attempt + 1}/{max_retries}"
            )
        shares = quoted_buy_shares(remaining_budget, fresh_ask, fresh_ask_size)
        if shares < 0.01:
            break
        price = fresh_ask
        spend = shares * price
        max_shares = shares
        ambiguous = False
        result = None
        if pre_submit:
            try:
                allowed, reason = pre_submit(
                    float(fresh_bid), float(fresh_ask), attempt + 1,
                )
            except Exception as exc:
                allowed, reason = False, f"validator_error:{exc}"
            if not allowed:
                log_event(
                    "buy_pre_submit_rejected",
                    token_id=token_id,
                    condition_id=condition_id,
                    attempt=attempt + 1,
                    reason=str(reason)[:160],
                )
                break
        try:
            signed_order = safe_api_call(
                client.create_order,
                OrderArgs(
                    token_id=token_id,
                    price=price,
                    size=shares,
                    side=BUY,
                    user_usdc_balance=remaining_budget,
                ),
                options=PartialCreateOrderOptions(
                    tick_size=tick_size, neg_risk=False,
                ),
            )
            expected_order_id = signed_order_id(signed_order, neg_risk=False)
            intent = {
                "order_id": expected_order_id,
                "token_id": str(token_id),
                "side": "BUY",
                "maker_amount": str(signed_order.makerAmount),
                "taker_amount": str(signed_order.takerAmount),
                "timestamp": str(signed_order.timestamp),
                "quoted_shares": shares,
            }
        except Exception as e:
            log_event(
                "buy_build_rejected", token_id=token_id, error=str(e)[:200],
                attempt=attempt + 1, spend=round(spend, 4),
            )
            break
        if on_submit:
            try:
                on_submit(
                    float(bal_baseline),
                    attempt + 1,
                    float(spend),
                    float(price),
                    intent,
                )
            except Exception as e:
                log_event(
                    "buy_on_submit_fail", token_id=token_id, error=str(e)[:200],
                    attempt=attempt + 1, spend=round(spend, 4),
                )
                console.print(f"  [bold red][PERSIST FAIL][/] BUY not submitted: {e}")
                return total_bought, spent, "persist_fail"
        try:
            result = safe_api_call(
                client.post_order,
                signed_order,
                order_type=OrderType.FAK,
            )
        except Exception as e:
            if definitive_order_rejection(e):
                console.print(
                    f"  [dim yellow][BUY REJECTED][/] explicit HTTP rejection: {e}"
                )
                log_event(
                    "buy_attempt_rejected", token_id=token_id,
                    order_id=expected_order_id, error=str(e)[:200],
                    attempt=attempt + 1, spend=round(spend, 4),
                )
                break
            console.print(f"  [dim red]Market buy {attempt+1}/{max_retries} failed: {e}[/]")
            log_event(
                "buy_attempt_ambiguous", token_id=token_id, error=str(e)[:200],
                attempt=attempt + 1, spend=round(spend, 4),
            )
            ambiguous = True

        if ambiguous or not result:
            if not result and not ambiguous:
                log_event(
                    "buy_attempt_falsy", token_id=token_id, attempt=attempt + 1,
                    spend=round(spend, 4),
                )
            # Accepted-then-timeout / falsy: reconcile once, NEVER re-post budget.
            filled, fill_cost = _reconcile_ghost(
                remaining_budget, price, attempt + 1, via="ambiguous_post",
            )
            if filled > 0:
                total_bought += filled
                spent += fill_cost
                try:
                    _persist()
                except Exception:
                    pass
            console.print("  [bold yellow][BUY STOP][/] ambiguous POST — no further retries")
            return total_bought, spent, "ambiguous"

        oid = extract_order_id(result)
        if oid and str(oid).lower() != str(expected_order_id).lower():
            log_event(
                "buy_order_id_mismatch",
                expected_order_id=expected_order_id,
                response_order_id=oid,
                token_id=token_id,
            )
            return total_bought, spent, "ambiguous"
        oid = oid or expected_order_id
        response_status = str(_result_as_dict(result).get("status") or "").strip().lower()
        terminal_filled = {"matched", "order_status_matched"}
        terminal_empty = {
            "canceled", "cancelled", "rejected", "expired", "failed", "unmatched",
            "order_status_invalid", "order_status_canceled",
            "order_status_canceled_market_resolved",
        }
        filled = float(
            confirm_fill_size(result, oid, max_shares, side="BUY")
        )
        fill_cost = 0.0
        if filled <= 0:
            time.sleep(float(HEDGE_GHOST_SLEEP_S))
            filled = float(
                confirm_fill_size(
                    result, oid, max_shares,
                    wait_delayed_s=1.0, side="BUY",
                )
            )
        if filled <= 0:
            filled, fill_cost = _reconcile_ghost(
                remaining_budget, price, attempt + 1, via="null_confirm",
            )
        if filled <= 0:
            if response_status in terminal_empty:
                console.print(
                    f"  [dim yellow][FAK NULL][/] explicit {response_status or 'empty'} "
                    "with 0 confirmed fill · stopping"
                )
                break
            log_event(
                "buy_attempt_ambiguous_status", token_id=token_id,
                status=response_status or None, order_id=oid,
                attempt=attempt + 1, spend=round(spend, 4),
            )
            console.print(
                "  [bold yellow][BUY STOP][/] non-terminal 0-fill response — quarantined"
            )
            return total_bought, spent, "ambiguous"
        if fill_cost <= 0:
            fill_cost = fill_cost_usdc(
                result,
                filled,
                price,
                remaining_budget,
                fee_schedule=fee_schedule,
            )
        if fill_cost <= 0:
            if buy_fill_walked(filled, max_shares):
                fill_cost = min(spend, remaining_budget)
            else:
                fill_cost = min(spend, filled * price)
            log_event(
                "buy_fill_cost_estimated", token_id=token_id, filled=filled,
                fill_cost=round(fill_cost, 4), ask=price, attempt=attempt + 1,
            )
        avg = implied_buy_average(fill_cost, filled, price)
        below_band, force_exit = classify_buy_fill(
            avg, filled, max_shares, min_price, TOXIC_FORCE_EXIT_BELOW,
        )
        total_bought += filled
        spent += fill_cost
        remaining_budget = budget - spent
        try:
            _persist()
        except Exception:
            break
        if response_status not in terminal_filled:
            log_event(
                "buy_partial_ambiguous_status", token_id=token_id,
                status=response_status or None, order_id=oid,
                filled=filled, spent=round(spent, 4), attempt=attempt + 1,
            )
            console.print(
                "  [bold yellow][BUY STOP][/] positive non-terminal fill — quarantined"
            )
            return total_bought, spent, "ambiguous"
        if below_band:
            console.print(
                f"  [bold red][BUY BELOW BAND][/] filled={filled:.4f} avg={avg:.3f} "
                f"(min_px={min_price:.3f}) — "
                + (
                    "toxic_fill armed (force exit, no ride)"
                    if force_exit
                    else f"above {float(TOXIC_FORCE_EXIT_BELOW):.2f} dump floor — normal hedge"
                )
            )
            log_event(
                "buy_fill_below_band", token_id=token_id, filled=filled, avg_price=round(avg, 4),
                fill_cost=round(fill_cost, 4), spend=round(spend, 4), max_shares=round(max_shares, 4),
                min_price=min_price, ask=price, attempt=attempt + 1,
            )
            break
        console.print(f"  [bold green][BUY FAK][/]{filled} @ avg {avg:.3f} (${fill_cost:.2f})  [dim]id={str(oid)[:16]}…[/]")
        log_event(
            "buy_fill", token_id=token_id, filled=filled, price=price, avg_price=round(avg, 4),
            spend=round(spend, 4), spent=round(spent, 4),
            remaining_budget=round(remaining_budget, 4), attempt=attempt + 1,
        )
        if remaining_budget < 0.01:
            return total_bought, spent, "filled"
        time.sleep(0.05)

    if persist_failed and total_bought > 0:
        return total_bought, spent, "persist_fail"
    if total_bought > 0:
        console.print(f"  [bold yellow][BUY DONE][/]{total_bought:.4f} shares · ${spent:.2f}/${budget:.2f} spent")
        return total_bought, spent, "filled"
    console.print(f"  [bold red][BUY FAIL][/] spent $0.00/${budget:.2f}")
    return 0.0, 0.0, "empty"


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
    on_submit=None,
    on_fill=None,
    condition_id=None,
    initial_quote=None,
):
    """Sell `size` shares via FAK. Used for hedge exits only — no max_price cap.

    Each retry force-REST refreshes the full book and re-runs two-sided integrity
    when require_ask_max/max_spread are set. Incomplete REST fails closed (no WS
    fallback). `price` is the worst (lowest) price we will accept.

    Returns (total_sold, result, proceeds) where result["bot_status"] is
    filled|empty|ambiguous|persist_fail|dry. Unknown POST outcomes stop retries.
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
        return 0, {"bot_status": "dry", "last_limit": price}, 0.0
    fee_schedule = None
    for attempt in range(max_retries):
        if remaining < 0.01:
            break
        live_bid = price_limit
        live_ask = None
        if attempt == 0 and initial_quote is not None:
            try:
                live_bid, live_ask = initial_quote
            except (TypeError, ValueError):
                live_bid, live_ask = None, None
            if require_ask_max is not None and max_spread is not None and abort_above is not None:
                ok, why = hedge_book_ok(
                    live_bid, live_ask, abort_above, max_spread, require_ask_max,
                )
                if not ok:
                    log_event(
                        "hedge_initial_quote_invalid",
                        token_id=token_id,
                        live_bid=live_bid,
                        live_ask=live_ask,
                        reason=why,
                    )
                    break
        elif refresh_quote:
            qb, _, qa, _, _ = get_quote_fast(
                token_id,
                max_age_s=0.0,
                prefer_rest=True,
                force_rest=True,
                expected_condition_id=condition_id,
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
            signed_order = safe_api_call(
                client.create_market_order,
                MarketOrderArgs(
                    token_id=token_id,
                    amount=remaining,
                    side=SELL,
                    price=price,
                ),
                options=PartialCreateOrderOptions(
                    tick_size=tick_size, neg_risk=False,
                ),
            )
            expected_order_id = signed_order_id(signed_order, neg_risk=False)
            intent = {
                "order_id": expected_order_id,
                "token_id": str(token_id),
                "side": "SELL",
                "maker_amount": str(signed_order.makerAmount),
                "taker_amount": str(signed_order.takerAmount),
                "timestamp": str(signed_order.timestamp),
            }
        except Exception as e:
            log_event(
                "sell_build_rejected", token_id=token_id,
                error=str(e)[:200], attempt=attempt + 1,
            )
            break
        if on_submit:
            try:
                on_submit(
                    float(total_sold), float(total_proceeds), float(remaining),
                    attempt + 1, float(price), intent,
                )
            except Exception as e:
                log_event(
                    "sell_on_submit_fail", token_id=token_id,
                    error=str(e)[:200], attempt=attempt + 1,
                )
                return total_sold, {
                    "bot_status": "persist_fail", "last_limit": last_limit,
                }, total_proceeds
        try:
            result = safe_api_call(
                client.post_order,
                signed_order,
                order_type=OrderType.FAK,
            )
            if result:
                oid = extract_order_id(result)
                if oid and str(oid).lower() != str(expected_order_id).lower():
                    return total_sold, {
                        "bot_status": "ambiguous",
                        "last_limit": last_limit,
                        "order_id": expected_order_id,
                        "status": "order_id_mismatch",
                    }, total_proceeds
                oid = oid or expected_order_id
                response_status = str(
                    _result_as_dict(result).get("status") or ""
                ).strip().lower()
                filled = float(confirm_fill_size(result, oid, remaining, side="SELL"))
                if filled <= 0:
                    terminal_empty = {
                        "canceled", "cancelled", "rejected", "expired", "failed",
                        "unmatched", "order_status_invalid", "order_status_canceled",
                        "order_status_canceled_market_resolved",
                    }
                    if response_status in terminal_empty:
                        console.print(
                            f"  [dim yellow][FAK NULL][/] explicit {response_status} "
                            "with 0 confirmed fill"
                        )
                        break
                    return total_sold, {
                        "bot_status": "ambiguous", "last_limit": last_limit,
                        "order_id": oid, "status": response_status,
                    }, total_proceeds
                fills_px = fill_proceeds(
                    result, filled, price, fee_schedule=fee_schedule,
                )
                total_sold += filled
                total_proceeds += fills_px
                remaining -= filled
                if on_fill:
                    try:
                        on_fill(float(total_sold), float(total_proceeds))
                    except Exception as e:
                        log_event(
                            "sell_on_fill_fail", token_id=token_id,
                            error=str(e)[:200], sold=total_sold,
                            proceeds=total_proceeds,
                        )
                        return total_sold, {
                            "bot_status": "persist_fail", "last_limit": last_limit,
                            "order_id": oid,
                        }, total_proceeds
                console.print(f"  [bold green][EXIT FAK][/]{filled} @ ≥{price:.3f}  [dim]id={str(oid)[:16]}…[/]")
                log_event("sell_fill", token_id=token_id, filled=filled, price=price, remaining=remaining, attempt=attempt + 1)
                if response_status not in {"matched", "order_status_matched"}:
                    return total_sold, {
                        "bot_status": "ambiguous", "last_limit": last_limit,
                        "order_id": oid, "status": response_status,
                    }, total_proceeds
                if remaining < 0.01:
                    out = dict(result) if isinstance(result, dict) else {}
                    out.update({"bot_status": "filled", "last_limit": last_limit})
                    return total_sold, out, total_proceeds
            else:
                # Falsy / success:false POST — never treat as proven empty.
                result_d = _result_as_dict(result)
                trade_ids = _string_list(
                    result_d.get("tradeIDs") or result_d.get("trade_ids")
                )
                return total_sold, {
                    "bot_status": "ambiguous",
                    "last_limit": last_limit,
                    "order_id": expected_order_id,
                    "status": (
                        "success_false"
                        if result_d.get("success") is False
                        else "falsy"
                    ),
                    "trade_ids": trade_ids,
                }, total_proceeds
        except Exception as e:
            if definitive_order_rejection(e):
                log_event(
                    "sell_attempt_rejected", token_id=token_id,
                    order_id=expected_order_id, error=str(e)[:200],
                    attempt=attempt + 1, remaining=remaining,
                )
                break
            console.print(f"  [dim red]Market sell {attempt+1}/{max_retries} failed: {e}[/]")
            log_event(
                "sell_attempt_ambiguous", token_id=token_id,
                error=str(e)[:200], attempt=attempt + 1,
                remaining=remaining,
            )
            return total_sold, {
                "bot_status": "ambiguous", "last_limit": last_limit,
                "order_id": expected_order_id,
            }, total_proceeds
        time.sleep(float(retry_sleep_s))

    if total_sold > 0:
        return total_sold, {
            "partial": True, "sold": total_sold, "last_limit": last_limit,
            "bot_status": "filled",
        }, total_proceeds
    console.print(f"  [bold red][EXIT FAIL][/] market sell 0/{size:.4f} cleared")
    # Preserve last_limit so ghost reconciliation prices at the actual retry floor.
    return 0, {"sold": 0, "last_limit": last_limit, "bot_status": "empty"}, 0.0


# ------------------------- REDEEM -------------------------

_redeem_permanent_failures = set()


def get_relayer_headers(body):
    """Auth headers for relayer submit: Relayer API key OR Builder HMAC."""
    # Prefer Relayer API key auth (Settings → API Keys) when present.
    if RELAYER_API_KEY and RELAYER_API_KEY_ADDRESS:
        return {
            "Content-Type": "application/json",
            "RELAYER_API_KEY": str(RELAYER_API_KEY),
            "RELAYER_API_KEY_ADDRESS": str(RELAYER_API_KEY_ADDRESS),
        }
    if not (
        POLY_BUILDER_API_KEY
        and POLY_BUILDER_SECRET
        and POLY_BUILDER_PASSPHRASE
    ):
        return None
    config = BuilderConfig(
        local_builder_creds=BuilderApiKeyCreds(
            key=POLY_BUILDER_API_KEY,
            secret=POLY_BUILDER_SECRET,
            passphrase=POLY_BUILDER_PASSPHRASE,
        )
    )
    payload = config.generate_builder_headers(
        "POST", "/submit", str(body),
    )
    if payload is None:
        return None
    return {"Content-Type": "application/json", **payload.to_dict()}


def get_relayer_transaction(transaction_id):
    """Return a relayer transaction record, or None on a transient failure."""
    if not transaction_id:
        return None
    try:
        response = requests.get(
            f"{RELAYER_URL.rstrip('/')}/transaction",
            params={"id": transaction_id},
            timeout=5,
        )
        response.raise_for_status()
        payload = response.json()
        if isinstance(payload, list):
            return payload[0] if payload and isinstance(payload[0], dict) else None
        return payload if isinstance(payload, dict) else None
    except Exception as e:
        log_event(
            "redeem_status_fail",
            transaction_id=str(transaction_id)[:36],
            error=str(e)[:200],
        )
        return None


def submit_proxy_tx(target, data, tx_type="PROXY"):
    """Build, sign, and submit a current Relayer v2 PROXY transaction."""
    if tx_type != "PROXY":
        return None, f"unsupported relayer transaction type {tx_type}"
    if not PRIVATE_KEY or not FUNDER_ADDRESS:
        return None, "missing PRIVATE_KEY or FUNDER_ADDRESS"
    has_relayer_key = bool(RELAYER_API_KEY and RELAYER_API_KEY_ADDRESS)
    has_builder = bool(
        POLY_BUILDER_API_KEY
        and POLY_BUILDER_SECRET
        and POLY_BUILDER_PASSPHRASE
    )
    if not (has_relayer_key or has_builder):
        return None, "missing Polymarket relayer/builder credentials"

    try:
        relayer_url = RELAYER_URL.rstrip("/")
        signer = RelayerSigner(PRIVATE_KEY, CHAIN_ID)
        eoa = signer.address()
        if (
            has_relayer_key
            and str(RELAYER_API_KEY_ADDRESS).lower() != str(eoa).lower()
        ):
            return None, (
                "RELAYER_API_KEY_ADDRESS does not match PRIVATE_KEY signer"
            )
        nonce_r = requests.get(
            f"{relayer_url}/relay-payload",
            params={"address": eoa, "type": tx_type},
            timeout=10,
        )
        if nonce_r.status_code != 200:
            return None, f"relay payload fetch fail HTTP {nonce_r.status_code}"
        relay_payload = nonce_r.json()
        if not isinstance(relay_payload, dict):
            return None, "invalid relay payload"
        nonce = relay_payload.get("nonce")
        relay = relay_payload.get("address")
        if nonce is None or not relay:
            return None, "relay payload missing nonce/address"

        data_hex = data if isinstance(data, str) else "0x" + bytes(data).hex()
        config = get_relayer_contract_config(CHAIN_ID)
        encoded_data = encode_proxy_transaction_data([
            ProxyTransaction(
                to=str(target),
                type_code=CallType.Call,
                data=data_hex,
                value="0",
            )
        ])
        request = build_proxy_transaction_request(
            signer=signer,
            args=ProxyTransactionArgs(
                from_address=eoa,
                nonce=str(nonce),
                gas_price="0",
                data=encoded_data,
                relay=str(relay),
            ),
            config=config,
            metadata="poly-money-maker redeem",
        )
        body = request.to_dict()
        if str(body.get("proxyWallet") or "").lower() != str(FUNDER_ADDRESS).lower():
            return None, "derived proxyWallet does not match FUNDER_ADDRESS"
        relayer_headers = get_relayer_headers(body)
        if relayer_headers is None:
            return None, "could not generate relayer authentication headers"

        submit_r = requests.post(
            f"{relayer_url}/submit",
            json=body,
            headers=relayer_headers,
            timeout=10,
        )
        if submit_r.status_code == 200:
            payload = submit_r.json()
            transaction_id = (
                payload.get("transactionID")
                if isinstance(payload, dict)
                else None
            )
            if transaction_id:
                return str(transaction_id), None
            return None, "relayer response missing transactionID"
        return None, f"HTTP {submit_r.status_code} · {submit_r.text[:80]}"
    except Exception as exc:
        return None, f"relayer request failed: {str(exc)[:160]}"


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
        redeem_sel = keccak(b"redeemPositions(address,bytes32,bytes32,uint256[])")[:4]
        redeem_data = redeem_sel + encode(
            ["address", "bytes32", "bytes32", "uint256[]"],
            [pUSD, bytes(32), bytes.fromhex(condition_id.lower().removeprefix("0x")), [1, 2]],
        )
        tx_id, err = submit_proxy_tx(CTF_CONTRACT, redeem_data)
        if tx_id:
            console.print(f"  [bold bright_green][SETTLE ▶][/] {label}  [dim]tx={str(tx_id)[:18]}…[/]")
            return tx_id
        console.print(f"  [dim red][SETTLE FAIL][/] {label}  [dim]{err}[/]")
        # Blacklist definitive auth failures and empty/already-settled redeems.
        # Transient 429/5xx must remain retryable.
        err_l = (err or "").lower()
        if err and (
            "proxywallet" in err_l
            or "invalid api key" in err_l
            or "invalid authorization" in err_l
            or "unauthorized" in err_l
            or "relayer_api_key_address does not match" in err_l
            or "precheck_skipped" in err_l
            or "redeem skipped: zero" in err_l
        ):
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

client = ImmediateResponseClobClient(
    host=HOST,
    key=PRIVATE_KEY,
    chain_id=CHAIN_ID,
    creds=api_creds,
    signature_type=1,
    funder=FUNDER_ADDRESS,
    retry_on_error=False,
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

positions_meta = load_json(STATE_FILE, required=not DRY_RUN)
CYCLE = 0
_last_positions_refresh = 0.0
_last_balance_refresh = 0.0
_last_markets_refresh = 0.0
pusd_bal = 0.0
_cached_positions = merge_tracked_positions({}, positions_meta)
_cached_markets = []
_cached_discovery_fresh = False
_positions_received_mono = 0.0
_balance_received_mono = 0.0
_markets_received_mono = 0.0
_book_executor = ThreadPoolExecutor(max_workers=4)
_entry_executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="entry")
_pending_book_futs = {}
_io_executor = ThreadPoolExecutor(max_workers=3, thread_name_prefix="refresh")
_positions_future = None
_balance_future = None
_markets_future = None
_redeem_executor = ThreadPoolExecutor(max_workers=1)
_redeem_status_futures = {}


def _discover_markets_snapshot():
    markets = market_gateway.discover([SERIES_SLUG])
    return markets, bool(market_gateway.discovery_fresh)


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
        markets = [
            m for m in _cached_markets
            if m.active and not m.closed and not m.neg_risk and m.end_ts > now_s
        ]
        held = _cached_positions

        # Hot-reload strategy
        _strat = load_strategy()
        ENTRY_ENABLED = _strat["entry_enabled"]
        BUY_THRESHOLD = _strat["buy_threshold"]
        BUY_MAX_PRICE = _strat["buy_max_price"]
        MIN_WINNER_BID = _strat["min_winner_bid"]
        MAX_LOSER_BID = _strat["max_loser_bid"]
        MIN_BID_EDGE = _strat["min_bid_edge"]
        UNDERLYING_GATE_ENABLED = _strat["underlying_gate_enabled"]
        MIN_UNDERLYING_EDGE_USD = _strat["min_underlying_edge_usd"]
        TOXIC_FORCE_EXIT_BELOW = _strat["toxic_force_exit_below"]
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
        BUY_MAX_SPEND = _strat["buy_max_spend"]
        MAX_OPEN_POSITIONS = _strat["max_open_positions"]
        MAX_OPEN_NOTIONAL = _strat["max_open_notional"]
        MAX_DAILY_NOTIONAL = _strat["max_daily_notional"]
        ONE_ENTRY_PER_MARKET = _strat["one_entry_per_market"]
        REDEEM_THROTTLE_S = _strat["redeem_throttle_s"]
        MAX_REDEEM_AGE_DAYS = _strat["max_redeem_age_days"]
        DRY_RUN = STARTUP_DRY_RUN  # arming/disarming requires restart (state paths differ)
        POLL_BUY_WINDOW_S = _strat["poll_buy_window_s"]
        POLL_HELD_S = _strat["poll_held_s"]
        POSITIONS_REFRESH_S = _strat["positions_refresh_s"]
        BALANCE_REFRESH_S = _strat["balance_refresh_s"]
        UI_EVERY_N_CYCLES = _strat["ui_every_n_cycles"]
        TICK_SIZE_FALLBACK = _strat["tick_size"]

        write_heartbeat()

        # Consume background refreshes without putting unrelated network RTTs
        # ahead of the hedge path.
        _now_mono = time.monotonic()
        positions_raw = None
        if _positions_future is not None and _positions_future.done():
            try:
                positions_raw = _positions_future.result()
                if positions_raw is not None:
                    _cached_positions = merge_tracked_positions(
                        build_held_positions(positions_raw), positions_meta,
                    )
                    _positions_received_mono = _now_mono
            except Exception as e:
                log_event("positions_refresh_fail", error=str(e)[:200])
            _positions_future = None
        if _balance_future is not None and _balance_future.done():
            try:
                refreshed_balance = _balance_future.result()
                if refreshed_balance is not None:
                    pusd_bal = float(refreshed_balance)
                    _balance_received_mono = _now_mono
            except Exception as e:
                log_event("balance_refresh_fail", error=str(e)[:200])
            _balance_future = None
        if _markets_future is not None and _markets_future.done():
            try:
                refreshed_markets, refreshed_fresh = _markets_future.result()
                if refreshed_markets:
                    _cached_markets = refreshed_markets
                    _markets_received_mono = _now_mono
                _cached_discovery_fresh = bool(refreshed_fresh)
            except Exception as e:
                _cached_discovery_fresh = False
                log_event("discover_fail", error=str(e)[:200])
            _markets_future = None

        # Schedule due refreshes after consuming results. Each category has its
        # own worker so a slow Gamma/Data endpoint cannot starve the others.
        if (
            _balance_future is None
            and _now_mono - _last_balance_refresh >= BALANCE_REFRESH_S
        ):
            _balance_future = _io_executor.submit(get_balance)
            _last_balance_refresh = _now_mono
        if (
            _positions_future is None
            and _now_mono - _last_positions_refresh >= POSITIONS_REFRESH_S
        ):
            _positions_future = _io_executor.submit(get_user_positions)
            _last_positions_refresh = _now_mono
        if (
            _markets_future is None
            and _now_mono - _last_markets_refresh
            >= float(market_gateway.discover_cache_s)
        ):
            _markets_future = _io_executor.submit(_discover_markets_snapshot)
            _last_markets_refresh = _now_mono

        _discovery_fresh = bool(
            _cached_discovery_fresh
            and _markets_received_mono > 0
            and _now_mono - _markets_received_mono
            <= max(10.0, float(market_gateway.discover_cache_s) * 2)
        )
        markets = [
            m for m in _cached_markets
            if m.active and not m.closed and not m.neg_risk and m.end_ts > now_s
        ]
        held = _cached_positions
        markets = add_tracked_market_stubs(markets, held, positions_meta, now_s)
        held_conditions = {
            cond for cond, pos in held.items()
            if max(
                float(pos.get("up", {}).get("size", 0) or 0),
                float(pos.get("dn", {}).get("size", 0) or 0),
            ) > 0.01
        }
        # Never let an unheld market's entry I/O run before an active hedge.
        markets.sort(key=lambda market: (
            market.condition_id not in held_conditions,
            not bool(
                positions_meta.get(market.condition_id, {}).get("buy_uncertain")
                or positions_meta.get(market.condition_id, {}).get("hedge_uncertain")
            ),
            market.end_ts,
        ))
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
            # An ambiguous BUY is durable recovery state, not ordinary stale
            # metadata. Keep it until its exact token/baseline is reconciled or
            # an operator clears it; discovery/Data API omissions must not erase
            # the quarantine and permit a second order.
            stale_conds = [
                c for c in list(positions_meta.keys())
                if (
                    c not in live_conds
                    and not positions_meta[c].get("buy_uncertain")
                    and not positions_meta[c].get("hedge_uncertain")
                    and gc_can_finalize(positions_meta[c])
                )
            ]
            finalized_conds = []
            for c in stale_conds:
                gc_meta = positions_meta[c]
                try:
                    if gc_meta.get("bought_token"):
                        entry_cost = gc_meta.get("pnl_entry_cost", 0)
                        hedge_proceeds = gc_meta.get("pnl_hedge_proceeds", 0)
                        redeem_value = gc_par_redeem(
                            gc_meta, hedge_proceeds, gc_meta.get("pnl_redeem_value", 0),
                        )
                        outcome = (
                            "hedge" if float(hedge_proceeds or 0) > 0 and float(redeem_value or 0) <= 0
                            else ("win" if float(redeem_value or 0) > 0 else "loss")
                        )
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
                except Exception as exc:
                    # Accounting must never starve the hedge loop. Retain the
                    # durable market state and retry finalization later.
                    log_event(
                        "gc_finalize_fail",
                        condition_id=c,
                        error=str(exc)[:200],
                    )
                    continue
                del positions_meta[c]
                finalized_conds.append(c)
            if finalized_conds:
                log_event("gc", stale_conditions=finalized_conds)
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
        _has_active_hedge_inventory = any(
            max(
                float(pos.get("up", {}).get("size", 0) or 0),
                float(pos.get("dn", {}).get("size", 0) or 0),
            ) > 0.01
            and not (
                pos.get("up", {}).get("redeemable")
                or pos.get("dn", {}).get("redeemable")
            )
            for pos in held.values()
        )
        _entry_window_open = bool(
            ENTRY_ENABLED
            and _discovery_fresh
            and any(
                0 < market.end_ts - now_s <= float(BUY_WINDOW_MIN) * 60
                for market in markets
            )
        )
        for cond, pos in held.items():
            if _has_active_hedge_inventory or _entry_window_open:
                break  # redemption HTTP must never delay a hedge or live entry
            up_redeemable = pos.get("up", {}).get("redeemable", False)
            dn_redeemable = pos.get("dn", {}).get("redeemable", False)
            if not (up_redeemable or dn_redeemable):
                continue
            if cond in _redeem_permanent_failures:
                continue
            meta = positions_meta.setdefault(cond, {})
            if meta.get("buy_uncertain") or meta.get("hedge_uncertain"):
                continue  # reconcile execution and entry cost before redemption
            if meta.get("redeem_pending"):
                continue  # submission is not settlement; operator/receipt reconciliation required
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
            # Write-ahead before relayer POST so a crash cannot lose the intent
            # and submit a duplicate redeem on restart.
            meta["redeem_intent_at"] = now_ms
            meta["redeem_expected_value"] = round(
                max(
                    float(pos.get("up", {}).get("size", 0) or 0),
                    float(pos.get("dn", {}).get("size", 0) or 0),
                ),
                4,
            )
            save_json(STATE_FILE, positions_meta)
            tx = redeem_condition(cond, label=(meta.get("question", "?"))[:32])
            # Always stamp the attempt so REDEEM_THROTTLE_S applies on failures
            # too (otherwise zero-payout 400s spam the relayer every cycle).
            meta["redeem_submitted_at"] = now_ms
            if tx:
                meta["redeem_pending"] = True
                meta["redeem_tx_id"] = str(tx)
                meta.pop("redeem_intent_at", None)
                log_event("redeem_submit", condition_id=cond, tx_id=str(tx))
                save_json(STATE_FILE, positions_meta)
            else:
                # Leave intent so a later cycle can retry unless permanently
                # blacklisted by redeem_condition.
                meta.pop("redeem_pending", None)
                meta.pop("redeem_tx_id", None)
                if cond in _redeem_permanent_failures:
                    meta.pop("redeem_intent_at", None)
                    meta["redeem_abandoned"] = True
                save_json(STATE_FILE, positions_meta)

        # ================= COLLECT PRE-FETCHED BOOKS =================
        _book_cache = {}
        _still_pending_book_futs = {}
        for _f, _t in list(_pending_book_futs.items()):
            if not _f.done():
                _still_pending_book_futs[_f] = _t
                continue
            try:
                _book_cache[_t] = _f.result()
            except Exception:
                _book_cache[_t] = (None, 0.0, None, 0.0, None)
        _pending_book_futs = _still_pending_book_futs
        # Overlay WS top-of-book. Held tokens use hedge_quote_max_age_s so a
        # stale-high WS quote cannot overwrite REST and suppress hedge checks.
        _held_tokens = set()
        for _p in held.values():
            for _leg in ("up", "dn"):
                _info = _p.get(_leg) or {}
                if float(_info.get("size", 0) or 0) > 0.01 and _info.get("asset"):
                    _held_tokens.add(str(_info["asset"]))
        for _t in list(_book_cache.keys()):
            _age = float(HEDGE_QUOTE_MAX_AGE_S) if str(_t) in _held_tokens else 2.0
            _wq = book_ws.quote(_t, max_age_s=_age)
            if _wq is not None and (_wq[0] is not None or _wq[2] is not None):
                _book_cache[_t] = _wq

        # ================= HEDGE + BUY PHASE =================
        for m in markets:
            end_ts_ms = m.end_ts * 1000
            minutes_left = (end_ts_ms - now_ms) / 60000

            cond = m.condition_id
            pos = held.get(cond, {})
            up_size = pos.get("up", {}).get("size", 0)
            dn_size = pos.get("dn", {}).get("size", 0)
            held_size = max(up_size, dn_size)
            held_info = (
                pos.get("up", {})
                if up_size > 0.01
                else (pos.get("dn", {}) if dn_size > 0.01 else {})
            )
            held_token = held_info.get("asset")
            held_leg = "up" if up_size > 0.01 else ("down" if dn_size > 0.01 else None)

            meta = positions_meta.setdefault(cond, {})
            tracked_token = meta.get("bought_token")
            tracked_leg = str(meta.get("bought_leg") or "").lower()
            # Durable entry identity wins over Data API leg size when both
            # legs briefly appear held (partial hedge lag, wrong-leg dust).
            if tracked_token:
                up_asset = str((pos.get("up") or {}).get("asset") or "")
                dn_asset = str((pos.get("dn") or {}).get("asset") or "")
                if up_asset and str(tracked_token) == up_asset and up_size > 0.01:
                    held_token = up_asset
                    held_leg = "up"
                    held_size = up_size
                    held_info = pos.get("up", {})
                elif dn_asset and str(tracked_token) == dn_asset and dn_size > 0.01:
                    held_token = dn_asset
                    held_leg = "down"
                    held_size = dn_size
                    held_info = pos.get("dn", {})
                elif up_size > 0.01 and dn_size > 0.01:
                    log_event(
                        "hedge_skip_ambiguous_legs",
                        condition_id=cond,
                        bought_token=tracked_token,
                        up_size=up_size,
                        dn_size=dn_size,
                    )
                    held_token = None
                    held_leg = None
                    held_size = 0.0
                elif tracked_leg == "up" and up_size > 0.01:
                    held_token = up_asset or tracked_token
                    held_leg = "up"
                    held_size = up_size
                    held_info = pos.get("up", {})
                elif tracked_leg in {"down", "dn"} and dn_size > 0.01:
                    held_token = dn_asset or tracked_token
                    held_leg = "down"
                    held_size = dn_size
                    held_info = pos.get("dn", {})
            if (
                held_token
                and tracked_token
                and str(held_token) == str(tracked_token)
                and "bought_size" in meta
                and not meta.get("buy_uncertain")
            ):
                tracked_size = max(0.0, float(meta.get("bought_size") or 0))
                if held_size > tracked_size + 0.01:
                    # Data API can lag a confirmed partial SELL and show the old
                    # larger balance. Never resurrect inventory above the
                    # durable local remainder.
                    held_size = tracked_size
                    if held_leg == "up":
                        up_size = tracked_size
                    elif held_leg == "down":
                        dn_size = tracked_size

            # Resolve an ambiguous BUY by its deterministic signed order id
            # before consulting eventually-consistent balance snapshots.
            uncertain_token = meta.get("buy_uncertain_token")
            uncertain_baseline = float(meta.get("buy_uncertain_baseline") or 0)
            uncertain_order_id = meta.get("buy_uncertain_order_id")
            if meta.get("buy_uncertain") and uncertain_order_id:
                known_size = float(meta.get("buy_uncertain_known_size") or 0)
                known_cost = float(meta.get("buy_uncertain_known_cost") or 0)
                requested = (
                    finite_float(meta.get("quoted_buy_shares"), minimum=0)
                    or finite_float(meta.get("buy_uncertain_order_size"), minimum=0)
                    or (
                        float(meta.get("buy_uncertain_spend") or 0)
                        / max(float(meta.get("buy_uncertain_price") or 0), 1e-9)
                    )
                )
                inspected = inspect_uncertain_order(
                    uncertain_order_id,
                    side="BUY",
                    requested=requested,
                    token_id=uncertain_token,
                    condition_id=cond,
                    limit_price=meta.get("buy_uncertain_price", 0),
                    spend_cap=max(
                        0.0,
                        min(float(BUY_BUDGET), float(BUY_MAX_SPEND)) - known_cost,
                    ),
                    trade_ids=meta.get("buy_uncertain_trade_ids"),
                )
                inspected_state = inspected["state"]
                if inspected_state == "confirmed":
                    resolved_size = known_size + float(inspected["filled"])
                    resolved_cost = known_cost + float(inspected["value"])
                    current_size = float(meta.get("bought_size") or 0)
                    current_cost = float(meta.get("pnl_entry_cost") or 0)
                    # on_fill may already have persisted this exact fill.
                    if current_size >= resolved_size - 0.01:
                        resolved_size = current_size
                        resolved_cost = max(resolved_cost, current_cost)
                    avg_fill = implied_buy_average(
                        resolved_cost, resolved_size, meta.get("buy_uncertain_price"),
                    )
                    quoted = meta.get("quoted_buy_shares")
                    _, force_exit = classify_buy_fill(
                        avg_fill, resolved_size, quoted,
                        BUY_THRESHOLD, TOXIC_FORCE_EXIT_BELOW,
                    )
                    meta["bought_token"] = uncertain_token
                    meta["bought_leg"] = (
                        meta.get("buy_uncertain_leg")
                        or held_leg
                        or meta.get("bought_leg")
                    )
                    meta["bought_size"] = resolved_size
                    meta["fill_price"] = round(avg_fill, 4)
                    meta["pnl_entry_cost"] = round(resolved_cost, 4)
                    meta["toxic_fill"] = bool(force_exit or meta.get("toxic_fill"))
                    leg_key = (
                        "up"
                        if str(meta["bought_leg"]).lower() == "up"
                        else "dn"
                    )
                    _cached_positions.setdefault(cond, {})[leg_key] = {
                        "asset": str(uncertain_token),
                        "size": resolved_size,
                        "redeemable": False,
                        "avgPrice": avg_fill,
                    }
                    clear_uncertain_fields(meta, _BUY_UNCERTAIN_KEYS)
                    log_event(
                        "buy_uncertain_resolved",
                        condition_id=cond,
                        token_id=uncertain_token,
                        order_id=uncertain_order_id,
                        size=resolved_size,
                        entry_cost=meta["pnl_entry_cost"],
                        via="exact_order",
                    )
                    save_json(STATE_FILE, positions_meta)
                elif inspected_state in {"empty", "failed"}:
                    clear_uncertain_fields(meta, _BUY_UNCERTAIN_KEYS)
                    log_event(
                        "buy_uncertain_empty",
                        condition_id=cond,
                        token_id=uncertain_token,
                        order_id=uncertain_order_id,
                        state=inspected_state,
                    )
                    save_json(STATE_FILE, positions_meta)
                elif inspected_state == "identity_mismatch":
                    log_event(
                        "buy_uncertain_identity_mismatch",
                        condition_id=cond,
                        token_id=uncertain_token,
                        order_id=uncertain_order_id,
                    )

            # Older write-ahead state may not contain an order id. Promote only
            # after repeated, stable, complete Data API observations.
            if (
                meta.get("buy_uncertain")
                and positions_raw is not None
                and held_size > 0.01
                and held_token
                and uncertain_token
                and str(held_token) == str(uncertain_token)
                and held_size > uncertain_baseline + 0.01
            ):
                uncertain_filled = held_size - uncertain_baseline
                observed_ms = time.time() * 1000
                last_observed_ms = float(meta.get("buy_uncertain_observed_at") or 0)
                if observed_ms - last_observed_ms >= float(POSITIONS_REFRESH_S) * 1000:
                    prior_observed = meta.get("buy_uncertain_observed_size")
                    stable = (
                        prior_observed is not None
                        and abs(float(prior_observed) - uncertain_filled) <= 0.01
                    )
                    meta["buy_uncertain_observed_size"] = uncertain_filled
                    meta["buy_uncertain_observed_at"] = observed_ms
                    meta["buy_uncertain_observed_count"] = (
                        int(meta.get("buy_uncertain_observed_count") or 0) + 1
                        if stable else 1
                    )
                    save_json(STATE_FILE, positions_meta)
                uncertain_age_s = (
                    observed_ms - float(meta.get("buy_uncertain_at") or observed_ms)
                ) / 1000
                if (
                    int(meta.get("buy_uncertain_observed_count") or 0) >= 2
                    and uncertain_age_s >= max(10.0, 2 * float(POSITIONS_REFRESH_S))
                ):
                    avg_est = float(
                        meta.get("buy_uncertain_price")
                        or meta.get("fill_price")
                        or BUY_THRESHOLD
                    )
                    known_size = float(meta.get("bought_size") or 0)
                    known_cost = float(meta.get("pnl_entry_cost") or 0)
                    resolved_size = held_size
                    extra_size = max(0.0, resolved_size - known_size)
                    spend_est = float(meta.get("buy_uncertain_spend") or 0)
                    # Walked inventory did not cost extra_size × gate ask — same USDC.
                    entry_cost = known_cost if known_cost > 0 else spend_est
                    if entry_cost <= 0:
                        entry_cost = min(
                            float(BUY_BUDGET),
                            float(BUY_MAX_SPEND),
                            resolved_size * avg_est,
                        )
                    quoted = meta.get("quoted_buy_shares")
                    avg_fill = implied_buy_average(entry_cost, resolved_size, avg_est)
                    _, force_exit = classify_buy_fill(
                        avg_fill, resolved_size, quoted, BUY_THRESHOLD, TOXIC_FORCE_EXIT_BELOW,
                    )
                    meta["bought_token"] = held_token
                    meta["bought_leg"] = held_leg or meta.get("buy_uncertain_leg")
                    meta["bought_size"] = resolved_size
                    meta["fill_price"] = round(avg_fill, 4)
                    meta["pnl_entry_cost"] = round(entry_cost, 4)
                    meta["toxic_fill"] = bool(force_exit or meta.get("toxic_fill"))
                    clear_uncertain_fields(meta, _BUY_UNCERTAIN_KEYS)
                    log_event(
                        "buy_uncertain_resolved", condition_id=cond, token_id=held_token,
                        size=resolved_size, extra_size=extra_size,
                        entry_cost=meta["pnl_entry_cost"],
                        baseline=uncertain_baseline, via="stable_held",
                    )
                    save_json(STATE_FILE, positions_meta)

            # An accepted-then-timeout SELL may already have consumed shares.
            # Reconcile the exact order; pending/unknown outcomes remain
            # quarantined, while confirmed/terminal-empty outcomes recover.
            if meta.get("hedge_uncertain"):
                hedge_order_id = meta.get("hedge_uncertain_order_id")
                hedge_state = "pending"
                inspected = None
                if hedge_order_id:
                    inspected = inspect_uncertain_order(
                        hedge_order_id,
                        side="SELL",
                        requested=(
                            finite_float(
                                meta.get("hedge_uncertain_order_size"), minimum=0,
                            )
                            or finite_float(
                                meta.get("hedge_uncertain_remaining"), minimum=0,
                            )
                            or held_size
                        ),
                        token_id=meta.get("hedge_uncertain_token"),
                        condition_id=cond,
                        limit_price=meta.get("hedge_uncertain_price", 0),
                            trade_ids=meta.get("hedge_uncertain_trade_ids"),
                    )
                    hedge_state = inspected["state"]
                elif meta.get("hedge_uncertain_status") == "persist_fail":
                    # The durable pre-submit callback failed, so post_order was
                    # never called and there is no exchange order to reconcile.
                    hedge_state = "empty"

                if hedge_state == "confirmed":
                    sold_before = float(
                        meta.get("hedge_uncertain_sold_before") or 0
                    )
                    proceeds_before = float(
                        meta.get("hedge_uncertain_proceeds_before") or 0
                    )
                    position_size = float(
                        meta.get("hedge_uncertain_position_size")
                        or meta.get("bought_size")
                        or held_size
                    )
                    pnl_before = float(
                        meta.get("hedge_uncertain_pnl_before")
                        or meta.get("pnl_hedge_proceeds")
                        or 0
                    )
                    total_sold = sold_before + float(inspected["filled"])
                    total_proceeds = proceeds_before + float(inspected["value"])
                    rem = max(0.0, position_size - total_sold)
                    if rem < 0.01:
                        rem = 0.0
                        meta["hedge_closed"] = True
                    meta["bought_size"] = rem
                    meta["pnl_hedge_proceeds"] = round(
                        max(
                            float(meta.get("pnl_hedge_proceeds") or 0),
                            pnl_before + total_proceeds,
                        ),
                        4,
                    )
                    leg_key = "up" if held_leg == "up" else "dn"
                    if (
                        cond in _cached_positions
                        and leg_key in _cached_positions[cond]
                    ):
                        _cached_positions[cond][leg_key]["size"] = rem
                    clear_uncertain_fields(meta, _HEDGE_UNCERTAIN_KEYS)
                    log_event(
                        "hedge_uncertain_resolved",
                        condition_id=cond,
                        token_id=held_token,
                        order_id=hedge_order_id,
                        sold=total_sold,
                        remaining=rem,
                        via="exact_order",
                    )
                    save_json(STATE_FILE, positions_meta)
                    continue
                if hedge_state in {"empty", "failed"}:
                    clear_uncertain_fields(meta, _HEDGE_UNCERTAIN_KEYS)
                    log_event(
                        "hedge_uncertain_empty",
                        condition_id=cond,
                        token_id=held_token,
                        order_id=hedge_order_id,
                        state=hedge_state,
                    )
                    save_json(STATE_FILE, positions_meta)
                else:
                    if hedge_state == "identity_mismatch":
                        notify(
                            "HEDGE IDENTITY MISMATCH",
                            f"{m.question}\nOrder {hedge_order_id} did not match "
                            "the persisted hedge intent",
                            priority="urgent",
                        )
                    if CYCLE % max(
                        1, int(5 / max(float(POLL_HELD_S), 0.01))
                    ) == 0:
                        log_event(
                            "hedge_uncertain_pending",
                            condition_id=cond,
                            token_id=meta.get("hedge_uncertain_token"),
                            order_id=hedge_order_id,
                            state=hedge_state,
                            confirmed_sold=meta.get(
                                "hedge_uncertain_confirmed_sold", 0,
                            ),
                        )
                    continue

            # Recovery above must run even after expiry. New hedge/order actions
            # remain forbidden once the market window has closed.
            if minutes_left <= 0:
                continue

            if held_size > 0.01 and not meta.get("hedge_closed"):
                quoted = meta.get("quoted_buy_shares")
                cost = float(meta.get("pnl_entry_cost") or meta.get("buy_uncertain_spend") or 0)
                implied = implied_buy_average(
                    cost, held_size, meta.get("fill_price") or meta.get("buy_uncertain_price"),
                )
                _, force_exit = classify_buy_fill(
                    implied, held_size, quoted, BUY_THRESHOLD, TOXIC_FORCE_EXIT_BELOW,
                )
                if force_exit and not meta.get("toxic_fill"):
                    meta["toxic_fill"] = True
                    log_event(
                        "toxic_fill_armed_from_inventory",
                        condition_id=cond,
                        held_size=held_size,
                        implied_avg=round(implied, 4),
                        quoted_shares=quoted,
                    )
                    save_json(STATE_FILE, positions_meta)

            # --- HEDGE CHECK (for held positions) ---
            if held_token and held_size > 0.01 and HEDGE_ENABLED and not meta.get("hedge_closed"):
                # FAK avg < toxic_force_exit_below sets toxic_fill: dump ASAP at any
                # bid (no ride to $1, no bounce cancel, no hedge_min_price floor).
                # Mild below-band fills (≥ that floor) use the normal hedge path.
                toxic_fill = bool(meta.get("toxic_fill"))
                # Skip REST only when a *fresh* WS bid is clearly above threshold.
                # Stale-high WS must not suppress the check (hedge_quote_max_age_s).
                quote_age = book_ws.quote_age(held_token)
                ws_fresh = quote_age is not None and quote_age <= float(HEDGE_QUOTE_MAX_AGE_S)
                peek_bid = None
                if ws_fresh:
                    _wq = book_ws.quote(held_token, max_age_s=float(HEDGE_QUOTE_MAX_AGE_S))
                    if _wq is not None:
                        peek_bid = _wq[0]
                cached_quote = _book_cache.get(held_token)
                cached_bid = (cached_quote[0] if cached_quote else None)
                if cached_bid is None:
                    cached_bid = peek_bid

                if (
                    not toxic_fill
                    and peek_bid is not None
                    and peek_bid > HEDGE_THRESHOLD
                    and ws_fresh
                ):
                    pass  # fresh WS still above threshold
                else:
                    fresh_bid, _, fresh_ask, _, fresh_mid = get_quote_fast(
                        held_token,
                        max_age_s=0.0,
                        prefer_rest=True,
                        force_rest=True,
                        expected_condition_id=cond,
                    )
                    if fresh_bid is None and (not toxic_fill or fresh_ask is None):
                        log_event(
                            "hedge_skip_incomplete_rest", condition_id=cond, leg=held_leg,
                            trigger_bid=cached_bid, current_bid=fresh_bid, current_ask=fresh_ask,
                            current_mid=fresh_mid,
                            quote_age_s=None if quote_age is None else round(quote_age, 3),
                            toxic_fill=toxic_fill,
                        )
                    elif toxic_fill and fresh_bid is None:
                        log_event(
                            "hedge_skip_incomplete_rest", condition_id=cond, leg=held_leg,
                            trigger_bid=cached_bid, current_bid=fresh_bid, current_ask=fresh_ask,
                            current_mid=fresh_mid,
                            quote_age_s=None if quote_age is None else round(quote_age, 3),
                            toxic_fill=True, reason="no_bid",
                        )
                    elif (not toxic_fill) and (fresh_bid is None or fresh_ask is None):
                        log_event(
                            "hedge_skip_incomplete_rest", condition_id=cond, leg=held_leg,
                            trigger_bid=cached_bid, current_bid=fresh_bid, current_ask=fresh_ask,
                            current_mid=fresh_mid,
                            quote_age_s=None if quote_age is None else round(quote_age, 3),
                            toxic_fill=False,
                        )
                    elif not toxic_fill and fresh_bid > HEDGE_THRESHOLD:
                        log_event(
                            "hedge_cancel_bounce", condition_id=cond, leg=held_leg,
                            trigger_bid=cached_bid, current_bid=fresh_bid,
                            current_ask=fresh_ask, current_mid=fresh_mid,
                            threshold=HEDGE_THRESHOLD,
                        )
                        console.print(
                            f"  [dim][CANCEL][/] {held_leg.upper()} hedge cancelled — bid bounced "
                            f"{(cached_bid if cached_bid is not None else fresh_bid):.3f} → {fresh_bid:.3f}"
                        )
                    else:
                        hedge_bid = fresh_bid
                        hedge_ask = fresh_ask
                        if toxic_fill:
                            ok, why = True, "toxic_force_exit"
                        else:
                            ok, why = hedge_book_ok(
                                hedge_bid, hedge_ask, HEDGE_THRESHOLD, HEDGE_MAX_SPREAD, HEDGE_REQUIRE_ASK_MAX,
                            )
                        if not ok:
                            meta["hedge_blocked_toxic"] = True
                            save_json(STATE_FILE, positions_meta)
                            log_event(
                                "hedge_skip_toxic_book", condition_id=cond, leg=held_leg,
                                bid=hedge_bid, ask=hedge_ask, mid=fresh_mid, reason=why,
                                threshold=HEDGE_THRESHOLD, max_spread=HEDGE_MAX_SPREAD,
                                require_ask_max=HEDGE_REQUIRE_ASK_MAX,
                                trigger_bid=cached_bid,
                            )
                        elif toxic_fill or hedge_bid <= HEDGE_THRESHOLD:
                            hedge_tick = get_tick_size_cached(held_token)
                            # 35/40 already said this book is a real reversal.
                            # Sell at the live bid — 20¢, 5¢, one tick, whatever
                            # is there. No strategy "won't take less than X".
                            hedge_floor = float(hedge_tick)
                            sell_floor = hedge_sell_price(
                                hedge_bid, hedge_tick, HEDGE_UNDERCUT_TICKS, hedge_floor,
                            )
                            hedge_title = (
                                "[bold bright_red]▼ TOXIC FILL — FORCE EXIT[/]"
                                if toxic_fill
                                else "[bold bright_red]▼ HEDGE SELL — CUTTING LOSSES[/]"
                            )
                            hedge_label = (
                                "TOXIC FILL EXIT" if toxic_fill else "REVERSAL DETECTED"
                            )
                            console.print(Panel(
                                f"  [bright_white]{m.question}[/]\n"
                                f"  [bright_red]{hedge_label}[/] — {held_leg.upper()} "
                                f"bid [bold]{hedge_bid:.3f}[/] ask [bold]{(hedge_ask or 0):.3f}[/]  ·  "
                                f"FAK ≥{sell_floor:.3f}  ·  [bold red]TTM {minutes_left:>4.1f}m[/]",
                                title=hedge_title,
                                border_style="bright_red",
                                box=box.HEAVY,
                            ))
                            meta["hedge_attempted"] = True
                            save_json(STATE_FILE, positions_meta)
                            log_event(
                                "hedge_attempt", condition_id=cond, leg=held_leg, size=held_size,
                                bid=hedge_bid, ask=hedge_ask, mid=fresh_mid, price_limit=sell_floor,
                                quote_age_s=None if quote_age is None else round(quote_age, 3),
                                ws_fast_path=False,
                                toxic_fill=toxic_fill,
                                hedge_floor=hedge_floor,
                            )
                            prior_hedge_proceeds = float(
                                meta.get("pnl_hedge_proceeds", 0) or 0
                            )

                            def _clear_hedge_uncertain():
                                clear_uncertain_fields(
                                    meta, _HEDGE_UNCERTAIN_KEYS,
                                )

                            def _persist_hedge_submit(
                                sold_total, proceeds_total, remaining_size,
                                attempt_no, submit_price, intent,
                            ):
                                meta["hedge_uncertain"] = True
                                meta["hedge_uncertain_at"] = time.time() * 1000
                                meta["hedge_uncertain_token"] = held_token
                                meta["hedge_uncertain_attempt"] = int(attempt_no)
                                meta["hedge_uncertain_remaining"] = float(remaining_size)
                                meta["hedge_uncertain_price"] = float(submit_price)
                                meta["hedge_uncertain_confirmed_sold"] = float(sold_total)
                                meta["hedge_uncertain_confirmed_proceeds"] = float(proceeds_total)
                                meta["hedge_uncertain_order_id"] = intent["order_id"]
                                meta["hedge_uncertain_order_size"] = (
                                    _decode_clob_fixed6(intent.get("maker_amount"))
                                    or float(remaining_size)
                                )
                                trade_ids = _string_list(
                                    intent.get("trade_ids")
                                    or intent.get("tradeIDs")
                                )
                                if trade_ids:
                                    meta["hedge_uncertain_trade_ids"] = trade_ids
                                meta["hedge_uncertain_sold_before"] = float(sold_total)
                                meta["hedge_uncertain_proceeds_before"] = float(
                                    proceeds_total
                                )
                                meta["hedge_uncertain_position_size"] = float(
                                    held_size
                                )
                                meta["hedge_uncertain_pnl_before"] = float(
                                    prior_hedge_proceeds
                                )
                                save_json(STATE_FILE, positions_meta)

                            def _persist_hedge_fill(sold_total, proceeds_total):
                                rem_now = max(0.0, held_size - float(sold_total))
                                if rem_now < 0.01:
                                    rem_now = 0.0
                                    meta["hedge_closed"] = True
                                meta["bought_size"] = rem_now
                                meta["pnl_hedge_proceeds"] = round(
                                    prior_hedge_proceeds + float(proceeds_total), 4
                                )
                                meta["hedge_uncertain_confirmed_sold"] = float(sold_total)
                                meta["hedge_uncertain_confirmed_proceeds"] = float(proceeds_total)
                                meta["hedge_uncertain_remaining"] = rem_now
                                leg_key = "up" if held_leg == "up" else "dn"
                                if cond in _cached_positions and leg_key in _cached_positions[cond]:
                                    _cached_positions[cond][leg_key]["size"] = rem_now
                                save_json(STATE_FILE, positions_meta)

                            sold, sell_res, hedge_proceeds = sell_market_with_retry(
                                held_token,
                                held_size,
                                hedge_bid,
                                tick_size=hedge_tick,
                                min_price=hedge_floor,
                                undercut_ticks=HEDGE_UNDERCUT_TICKS,
                                retry_sleep_s=HEDGE_RETRY_SLEEP_S,
                                # Toxic: never abort on bounce / integrity — dump inventory.
                                abort_above=None if toxic_fill else HEDGE_THRESHOLD,
                                require_ask_max=None if toxic_fill else HEDGE_REQUIRE_ASK_MAX,
                                max_spread=None if toxic_fill else HEDGE_MAX_SPREAD,
                                on_submit=_persist_hedge_submit,
                                on_fill=_persist_hedge_fill,
                                condition_id=cond,
                                initial_quote=(hedge_bid, hedge_ask),
                            )
                            sell_status = (
                                sell_res.get("bot_status")
                                if isinstance(sell_res, dict) else None
                            )
                            if sell_status == "ambiguous":
                                if isinstance(sell_res, dict):
                                    if sell_res.get("order_id"):
                                        meta["hedge_uncertain_order_id"] = (
                                            sell_res["order_id"]
                                        )
                                    trade_ids = _string_list(
                                        sell_res.get("trade_ids")
                                    )
                                    if trade_ids:
                                        meta["hedge_uncertain_trade_ids"] = (
                                            trade_ids
                                        )
                                    meta["hedge_uncertain_status"] = (
                                        sell_res.get("status") or "ambiguous"
                                    )
                                meta["hedge_uncertain_confirmed_sold"] = float(sold)
                                meta["hedge_uncertain_confirmed_proceeds"] = float(hedge_proceeds)
                                meta["hedge_uncertain_remaining"] = max(
                                    0.0, held_size - float(sold),
                                )
                                save_json(STATE_FILE, positions_meta)
                                notify(
                                    "HEDGE UNCERTAIN",
                                    f"{m.question}\nSELL outcome unresolved; quarantined "
                                    "until the exact order is reconciled",
                                    priority="urgent",
                                )
                            elif sell_status == "persist_fail":
                                # on_submit failed before post_order. Its callback
                                # may have mutated in-memory metadata, but no
                                # exchange order exists, so quarantine is unsafe.
                                _clear_hedge_uncertain()
                                log_event(
                                    "hedge_submit_persist_fail",
                                    condition_id=cond,
                                    token_id=held_token,
                                )
                                save_json(STATE_FILE, positions_meta)
                            else:
                                _clear_hedge_uncertain()
                            last_limit = sell_floor
                            if isinstance(sell_res, dict) and sell_res.get("last_limit") is not None:
                                last_limit = float(sell_res["last_limit"])
                            # Reconcile with Data API — one read never adds/erases confirms.
                            time.sleep(HEDGE_GHOST_SLEEP_S)
                            actual_bal = check_token_balance(held_token, cond)
                            rec = reconcile_hedge_sold(
                                held_size, sold, hedge_proceeds, actual_bal, last_limit,
                            )
                            if rec["ghost_candidate"]:
                                # Data API is eventually consistent. Even
                                # repeated omissions cannot prove a sale; only
                                # CLOB/order evidence may close the hedge.
                                log_event(
                                    "hedge_ghost_unconfirmed", condition_id=cond,
                                    leg=held_leg, size=held_size,
                                    confirmed_sold=float(sold),
                                    api_bal=None if actual_bal is None else float(actual_bal),
                                )
                            if rec["balance_unverified"]:
                                log_event(
                                    "hedge_balance_fail", condition_id=cond, leg=held_leg,
                                    size=held_size, bid=hedge_bid, ask=hedge_ask, sold=sold,
                                )
                            if rec["lag"]:
                                log_event(
                                    "hedge_balance_lag", condition_id=cond, leg=held_leg,
                                    confirmed_sold=float(sold), api_sold=rec["api_sold"],
                                    api_bal=None if actual_bal is None else float(actual_bal),
                                )
                            if rec["effective_sold"] > 0.01:
                                rem = rec["rem"]
                                meta["pnl_hedge_proceeds"] = round(
                                    prior_hedge_proceeds + rec["proceeds"], 4
                                )
                                if rem < 0.01:
                                    meta["hedge_closed"] = True
                                    rem = 0.0
                                meta["bought_size"] = rem
                                leg_key = "up" if held_leg == "up" else "dn"
                                if cond in _cached_positions and leg_key in _cached_positions[cond]:
                                    _cached_positions[cond][leg_key]["size"] = rem
                                fill_px = (
                                    (rec["proceeds"] / rec["effective_sold"])
                                    if rec["effective_sold"] > 0 else last_limit
                                )
                                log_event(
                                    "hedge_fill", condition_id=cond, leg=held_leg,
                                    sold=rec["effective_sold"], remaining=rem, price=fill_px,
                                    proceeds=round(rec["proceeds"], 4),
                                    mid=fresh_mid, ask=hedge_ask,
                                    hedge_closed=bool(meta.get("hedge_closed")),
                                    balance_unverified=bool(rec["balance_unverified"]),
                                )
                                save_json(STATE_FILE, positions_meta)
                                notify(
                                    "HEDGE FIRED" if rem < 0.01 else "HEDGE PARTIAL",
                                    f"Reversal on {m.question}\nSold {held_leg.upper()} at ~{fill_px:.3f} "
                                    f"({rec['effective_sold']:.2f} shares, rem {rem:.2f})",
                                    priority="urgent",
                                )
                            else:
                                if sell_status not in ("ambiguous", "persist_fail"):
                                    save_json(STATE_FILE, positions_meta)
                                log_event(
                                    "hedge_fail", condition_id=cond, leg=held_leg, size=held_size,
                                    bid=hedge_bid, ask=hedge_ask, mid=fresh_mid,
                                )

            # --- BUY CHECK (for markets we don't hold) ---
            if held_size > 0.01:
                continue  # already hold this market
            if minutes_left > BUY_WINDOW_MIN:
                continue  # not in buy window yet
            if not ENTRY_ENABLED:
                continue  # explicit operator arm is required for every live entry
            if not _discovery_fresh:
                continue  # stale Gamma metadata is hedge-only
            if (
                _positions_received_mono <= 0
                or _now_mono - _positions_received_mono
                > max(5.0, float(POSITIONS_REFRESH_S) * 3)
            ):
                continue  # open-position/notional checks require a recent snapshot
            if (
                _balance_received_mono <= 0
                or _now_mono - _balance_received_mono
                > max(30.0, float(BALANCE_REFRESH_S) * 3)
            ):
                continue  # never spend against an unknown/stale collateral balance

            # Quarantine is unconditional, including when an earlier attempt in
            # the same call already set bought_token. Stable held-state
            # reconciliation above resolves it; this path never posts again.
            if meta.get("buy_uncertain"):
                continue

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

            # Risk caps (in dollars / active markets). Redeemable Data-API
            # leftovers are settlement backlog — they must NOT consume the
            # max_open_positions budget or a redeem lag freezes all entries
            # (silent continue). Only non-redeemable size counts as open risk.
            open_conditions = set()
            for c, p in held.items():
                up_sz = float(p.get("up", {}).get("size", 0) or 0)
                dn_sz = float(p.get("dn", {}).get("size", 0) or 0)
                up_live = up_sz > 0.01 and not p.get("up", {}).get("redeemable")
                dn_live = dn_sz > 0.01 and not p.get("dn", {}).get("redeemable")
                if up_live or dn_live:
                    open_conditions.add(c)
            open_conditions |= {
                c for c, pm in positions_meta.items() if pm.get("buy_uncertain")
            }
            open_count = len(open_conditions)
            open_notional = sum(
                float(pm.get("pnl_entry_cost", 0) or 0)
                + (
                    float(pm.get("buy_uncertain_spend", 0) or 0)
                    if pm.get("buy_uncertain") else 0
                )
                for pm in positions_meta.values()
                if pm.get("bought_token") or pm.get("buy_uncertain")
            )
            daily_notional = sum(
                float(pm.get("pnl_entry_cost", 0) or 0)
                + (
                    float(pm.get("buy_uncertain_spend", 0) or 0)
                    if pm.get("buy_uncertain") else 0
                )
                for pm in positions_meta.values()
                if (pm.get("bought_token") or pm.get("buy_uncertain"))
                and pm.get("entered_at", 0) >= _today_start_ms()
            )
            est_cost = min(float(BUY_BUDGET), float(BUY_MAX_SPEND))
            if MAX_OPEN_POSITIONS > 0 and open_count >= MAX_OPEN_POSITIONS:
                log_event(
                    "buy_skip_max_positions",
                    condition_id=cond,
                    open_count=open_count,
                    max_open_positions=MAX_OPEN_POSITIONS,
                    held_reported=sum(
                        1 for p in held.values()
                        if max(
                            float(p.get("up", {}).get("size", 0) or 0),
                            float(p.get("dn", {}).get("size", 0) or 0),
                        ) > 0.01
                    ),
                )
                continue
            if open_notional + est_cost > MAX_OPEN_NOTIONAL + 1e-9:
                log_event(
                    "buy_skip_max_notional",
                    condition_id=cond,
                    open_notional=round(open_notional, 4),
                    max_open_notional=MAX_OPEN_NOTIONAL,
                    budget=est_cost,
                )
                continue
            if daily_notional + est_cost > MAX_DAILY_NOTIONAL + 1e-9:
                log_event(
                    "buy_skip_max_daily_notional",
                    condition_id=cond,
                    daily_notional=round(daily_notional, 4),
                    max_daily_notional=MAX_DAILY_NOTIONAL,
                    budget=est_cost,
                )
                continue
            if float(pusd_bal or 0) + 1e-9 < est_cost:
                log_event(
                    "buy_skip_balance", condition_id=cond,
                    balance=pusd_bal, budget=est_cost,
                )
                continue

            # Fresh REST book + last trade in parallel.
            # Entry decisions must not trust WS alone (stale/phantom sizes).
            # GUI display = mid if spread ≤ 10¢ else last trade.
            fut_up = _entry_executor.submit(
                get_quote_fast, m.up_token, 2.0, True, True,
            )  # prefer_rest, force_rest — no 200ms cache at entry gate
            fut_dn = _entry_executor.submit(
                get_quote_fast, m.dn_token, 2.0, True, True,
            )
            fut_ul = _entry_executor.submit(get_last_trade_price, m.up_token)
            fut_dl = _entry_executor.submit(get_last_trade_price, m.dn_token)
            up_bid, _, up_ask, _, up_mid = fut_up.result()
            dn_bid, _, dn_ask, _, dn_mid = fut_dn.result()
            up_last = fut_ul.result()
            dn_last = fut_dl.result()
            if up_last is None:
                up_last = get_book_snapshot_last_trade(m.up_token)
            if dn_last is None:
                dn_last = get_book_snapshot_last_trade(m.dn_token)
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
            spend_usd = min(float(BUY_BUDGET), float(BUY_MAX_SPEND))
            if uchk is None and m.start_ts:
                uchk = btc_feed.underlying_check(m.start_ts, 0)  # snapshot only
            log_event(
                "buy_attempt", condition_id=cond, leg=buy_leg, budget=spend_usd,
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

            def _persist_buy_fill(filled_total, spent_total):
                # Durable save after every confirmed attempt (crash between retries).
                avg = (spent_total / filled_total) if filled_total > 0 else float(buy_ask or 0)
                if spent_total <= 0 and filled_total > 0:
                    spent_total = min(
                        float(spend_usd),
                        filled_total * float(buy_ask or BUY_THRESHOLD),
                    )
                    avg = spent_total / filled_total if filled_total else avg
                quoted = meta.get("quoted_buy_shares")
                _, force_exit = classify_buy_fill(
                    avg, filled_total, quoted, BUY_THRESHOLD, TOXIC_FORCE_EXIT_BELOW,
                )
                meta["last_buy_at"] = time.time() * 1000
                meta["bought_token"] = buy_token
                meta["bought_leg"] = buy_leg
                meta["bought_size"] = filled_total
                meta["fill_price"] = round(avg, 4)
                meta["pnl_entry_cost"] = round(spent_total, 4)
                meta["toxic_fill"] = bool(force_exit)
                leg_key = "up" if buy_leg == "up" else "dn"
                _cached_positions.setdefault(cond, {})[leg_key] = {
                    "asset": buy_token,
                    "size": float(filled_total),
                    "redeemable": False,
                    "avgPrice": float(avg),
                }
                save_json(STATE_FILE, positions_meta)

            def _clear_buy_uncertain():
                clear_uncertain_fields(meta, _BUY_UNCERTAIN_KEYS)

            def _persist_buy_submit(
                baseline, attempt, spend_cap, submit_price, intent,
            ):
                # Write-ahead quarantine: this must reach disk before the POST.
                # A crash or accepted-then-timeout can then never unlock a
                # duplicate full-budget order on restart.
                submit_ms = time.time() * 1000
                meta["buy_uncertain"] = True
                meta["buy_uncertain_at"] = submit_ms
                meta["buy_uncertain_token"] = buy_token
                meta["buy_uncertain_leg"] = buy_leg
                meta["buy_uncertain_baseline"] = float(baseline)
                meta["buy_uncertain_attempt"] = int(attempt)
                meta["buy_uncertain_spend"] = round(float(spend_cap), 4)
                meta["buy_uncertain_price"] = round(float(submit_price), 4)
                meta["buy_uncertain_order_id"] = intent["order_id"]
                # Never decode taker_amount as share size — BUY taker can be
                # USDC and a 2.50 "share" quote false-toxics a real 3-sh fill.
                quoted_shares = finite_float(intent.get("quoted_shares"), minimum=0)
                if quoted_shares is None or quoted_shares < 0.01:
                    quoted_shares = (
                        float(spend_cap) / max(float(submit_price), 1e-9)
                    )
                meta["quoted_buy_shares"] = float(quoted_shares)
                meta["buy_uncertain_order_size"] = float(quoted_shares)
                trade_ids = _string_list(
                    intent.get("trade_ids") or intent.get("tradeIDs")
                )
                if trade_ids:
                    meta["buy_uncertain_trade_ids"] = trade_ids
                meta["buy_uncertain_known_size"] = float(
                    meta.get("bought_size") or 0
                )
                meta["buy_uncertain_known_cost"] = float(
                    meta.get("pnl_entry_cost") or 0
                )
                meta["last_buy_at"] = submit_ms
                save_json(STATE_FILE, positions_meta)

            bought, spent, buy_status = buy_market_with_retry(
                buy_token, spend_usd, BUY_MAX_PRICE, tick_size=tick, min_price=BUY_THRESHOLD,
                on_fill=_persist_buy_fill, on_submit=_persist_buy_submit,
                condition_id=cond,
            )
            wall_ms = time.time() * 1000
            if bought > 0:
                if buy_status != "ambiguous":
                    _clear_buy_uncertain()
                if spent <= 0:
                    spent = min(
                        float(spend_usd),
                        bought * float(buy_ask or BUY_THRESHOLD),
                    )
                avg_fill = implied_buy_average(spent, bought, buy_ask)
                quoted = meta.get("quoted_buy_shares")
                _, force_exit = classify_buy_fill(
                    avg_fill, bought, quoted, BUY_THRESHOLD, TOXIC_FORCE_EXIT_BELOW,
                )
                meta["last_buy_at"] = wall_ms
                meta["bought_token"] = buy_token
                meta["bought_leg"] = buy_leg
                meta["bought_size"] = bought
                meta["fill_price"] = round(avg_fill, 4)
                meta["pnl_entry_cost"] = round(spent, 4)
                meta["toxic_fill"] = bool(force_exit)
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
                    toxic_fill=bool(meta.get("toxic_fill")),
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
                    "toxic_fill": bool(meta.get("toxic_fill")),
                })
                if meta.get("toxic_fill"):
                    notify(
                        "TOXIC BUY — FORCE EXIT",
                        f"{m.question}\nBelow-band fill {buy_leg.upper()} at "
                        f"~{meta['fill_price']:.3f} ({bought:.2f} shares, ${spent:.2f})\n"
                        "Will not ride to $1 — dumping at next usable bid",
                        priority="urgent",
                    )
                else:
                    notify(
                        "BUY FILLED / UNCERTAIN" if buy_status == "ambiguous" else "BUY FILLED",
                        f"{m.question}\nBought {buy_leg.upper()} at ~{meta['fill_price']:.3f} "
                        f"({bought:.2f} shares, ${spent:.2f})"
                        + ("\nFinal POST unresolved; market remains quarantined" if buy_status == "ambiguous" else ""),
                        priority="urgent" if buy_status == "ambiguous" else "high",
                    )
                save_json(STATE_FILE, positions_meta)
            elif buy_status == "ambiguous":
                # on_submit already durably wrote the baseline and token before
                # the POST. Refresh only audit fields; do not discard baseline.
                meta["buy_uncertain"] = True
                meta["buy_uncertain_at"] = wall_ms
                meta["buy_uncertain_token"] = buy_token
                meta["buy_uncertain_leg"] = buy_leg
                meta["last_buy_at"] = wall_ms
                log_event(
                    "buy_uncertain", condition_id=cond, leg=buy_leg, budget=spend_usd,
                    ask=buy_ask, token_id=buy_token,
                )
                notify(
                    "BUY UNCERTAIN",
                    f"{m.question}\nAmbiguous POST on {buy_leg.upper()} — quarantined until reconciled",
                    priority="urgent",
                )
                save_json(STATE_FILE, positions_meta)
            else:
                # Every submitted attempt was explicitly terminal (or no POST
                # happened), so remove the temporary write-ahead quarantine.
                _clear_buy_uncertain()
                meta["last_buy_at"] = wall_ms
                log_event("buy_fail", condition_id=cond, leg=buy_leg, budget=spend_usd, ask=buy_ask, status=buy_status)
                save_json(STATE_FILE, positions_meta)

        # ================= REDEEM STATUS (ASYNC / LOW PRIORITY) =================
        _redeem_dirty = False
        for _future, _cond in list(_redeem_status_futures.items()):
            if not _future.done():
                continue
            _redeem_status_futures.pop(_future, None)
            _meta = positions_meta.get(_cond)
            if not _meta or not _meta.get("redeem_pending"):
                continue
            try:
                _tx_record = _future.result()
            except Exception:
                _tx_record = None
            _meta["redeem_status_checked_at"] = now_ms
            if not isinstance(_tx_record, dict):
                _redeem_dirty = True
                continue
            _tx_state = str(_tx_record.get("state") or "").upper()
            _meta["redeem_tx_state"] = _tx_state
            if _tx_record.get("transactionHash"):
                _meta["redeem_tx_hash"] = str(_tx_record["transactionHash"])
            if _tx_state in {"STATE_CONFIRMED", "STATE_MINED"}:
                _meta["redeem_confirmed"] = True
                _meta["redeem_confirmed_at"] = now_ms
                log_event(
                    "redeem_confirmed",
                    condition_id=_cond,
                    tx_id=_meta.get("redeem_tx_id"),
                    tx_hash=_meta.get("redeem_tx_hash"),
                    state=_tx_state,
                )
            elif _tx_state == "STATE_FAILED":
                log_event(
                    "redeem_terminal_fail",
                    condition_id=_cond,
                    tx_id=_meta.get("redeem_tx_id"),
                    state=_tx_state,
                )
                for _key in (
                    "redeem_pending", "redeem_tx_id", "redeem_tx_state",
                    "redeem_confirmed", "redeem_confirmed_at",
                    "redeem_tx_hash", "redeem_expected_value",
                    "redeem_intent_at",
                ):
                    _meta.pop(_key, None)
            elif _tx_state == "STATE_INVALID":
                # Relayer "invalid" is often a transient classification error
                # (payload/status race). Clear pending so we can retry, but do
                # not permanently blacklist from this status alone.
                log_event(
                    "redeem_invalid_retryable",
                    condition_id=_cond,
                    tx_id=_meta.get("redeem_tx_id"),
                    state=_tx_state,
                )
                for _key in (
                    "redeem_pending", "redeem_tx_id", "redeem_tx_state",
                    "redeem_confirmed", "redeem_confirmed_at",
                    "redeem_tx_hash",
                ):
                    _meta.pop(_key, None)
            _redeem_dirty = True

        # Credit par only after both relayer confirmation and a complete fresh
        # Data API snapshot showing that the redeemed inventory disappeared.
        if positions_raw is not None:
            for _cond, _meta in positions_meta.items():
                if not (
                    _meta.get("redeem_pending")
                    and _meta.get("redeem_confirmed")
                ):
                    continue
                _pos = held.get(_cond, {})
                _remaining = max(
                    float(_pos.get("up", {}).get("size", 0) or 0),
                    float(_pos.get("dn", {}).get("size", 0) or 0),
                )
                if _remaining > 0.01:
                    continue
                _redeem_value = float(
                    _meta.get("redeem_expected_value")
                    or _meta.get("bought_size")
                    or 0
                )
                if _redeem_value <= 0:
                    continue
                _meta["pnl_redeem_value"] = round(_redeem_value, 4)
                _meta["bought_size"] = 0.0
                _meta.pop("redeem_pending", None)
                log_event(
                    "redeem_settled",
                    condition_id=_cond,
                    tx_id=_meta.get("redeem_tx_id"),
                    redeem_value=_meta["pnl_redeem_value"],
                )
                _redeem_dirty = True

        _pending_redeem_conditions = set(_redeem_status_futures.values())
        for _cond, _meta in positions_meta.items():
            if not _meta.get("redeem_pending") or _cond in _pending_redeem_conditions:
                continue
            _last_check = float(_meta.get("redeem_status_checked_at") or 0)
            if now_ms - _last_check < REDEEM_THROTTLE_S * 1000:
                continue
            _tx_id = _meta.get("redeem_tx_id")
            if _tx_id:
                _future = _redeem_executor.submit(get_relayer_transaction, _tx_id)
                _redeem_status_futures[_future] = _cond
                _meta["redeem_status_checked_at"] = now_ms
                _redeem_dirty = True
        if _redeem_dirty:
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
