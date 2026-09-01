"""Second-account complement buyer.

Does not change 5m/15m hedge logic. After the first account confirms a
fill, this process (separate CLOB key / funder) watches the *other* token
and lifts it at ≥80¢ so a reversal is not a full −$10 if the sell-hedge
misses.

Requires ``.env.complement`` (second Polymarket account). Refuses to start
if that funder matches the primary ``.env`` wallet.
"""

import fcntl
import json
import logging
import math
import os
import shutil
import signal
import time
from datetime import datetime
from logging.handlers import RotatingFileHandler

import requests
from dotenv import load_dotenv
from rich.console import Console

from buy.book import best_from_levels
from buy.btc_price import SOURCE_TWAP_30, SOURCE_TWAP_60, get_btc_feed
from buy.clob_book_ws import get_book_feed
from buy.complement_gate import (
    arm_from_primary_meta,
    evaluate_complement,
    merge_armed,
    primary_and_complement_same_wallet,
)
from buy.live_journal import is_journal_event

HOST = "https://clob.polymarket.com"
CHAIN_ID = 137
STRATEGY_FILE = "strategy_complement.json"
STATE_FILE = "positions_complement.json"
HEARTBEAT_FILE = ".heartbeat_complement"
LOG_FILE = "complementbot.log"
JOURNAL_FILE = "complementbot.journal.jsonl"

_STRATEGY_DEFAULTS = {
    "entry_enabled": False,
    "dry_run": True,
    "primary_state_files": ["positions_buy5m.json", "positions_buy.json"],
    "primary_sources": ["5m", "15m"],
    "buy_min_price": 0.80,
    "buy_max_price": 0.99,
    "max_entry_spread": 0.05,
    "buy_max_spend": 16.0,
    "buy_max_shares": 20.0,
    "require_oracle": True,
    "min_underlying_edge_usd": 0.0,
    "poll_s": 0.01,
    "primary_grace_s": 45.0,
    "empty_fak_cooldown_s": 0.15,
    "max_retries": 3,
    "tick_size": "0.001",
}

console = Console()


def acquire_process_lock(path):
    lock_fh = open(path, "a+")
    try:
        fcntl.flock(lock_fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        console.print("[bold red]Another complement bot already holds the runtime lock.[/]")
        raise SystemExit(1)
    lock_fh.seek(0)
    lock_fh.truncate()
    lock_fh.write(str(os.getpid()))
    lock_fh.flush()
    return lock_fh


_PROCESS_LOCK_FH = acquire_process_lock("/tmp/poly-money-maker-complement.lock")


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


def load_json(path):
    if not os.path.exists(path):
        return {}
    with open(path, "r") as f:
        data = json.load(
            f,
            parse_constant=_reject_json_constant,
            parse_float=_parse_json_float,
        )
    if not isinstance(data, dict):
        raise ValueError(f"{path} root must be an object")
    return data


def load_strategy():
    cfg = dict(_STRATEGY_DEFAULTS)
    if not os.path.exists(STRATEGY_FILE):
        raise RuntimeError(f"strategy file {STRATEGY_FILE} is required at startup")
    with open(STRATEGY_FILE, "r") as f:
        overrides = json.load(f)
    if not isinstance(overrides, dict):
        raise ValueError("strategy root must be an object")
    unknown = set(overrides) - set(cfg)
    if unknown:
        raise ValueError(f"unknown strategy keys: {sorted(unknown)}")
    for key, value in overrides.items():
        expected = type(cfg[key])
        if expected is bool:
            if not isinstance(value, bool):
                raise ValueError(f"{key} must be true or false")
            cfg[key] = value
        elif expected is list:
            if not isinstance(value, list):
                raise ValueError(f"{key} must be a list")
            cfg[key] = [str(item) for item in value]
        else:
            cfg[key] = expected(value)
    if not (0 < float(cfg["buy_min_price"]) <= float(cfg["buy_max_price"]) <= 1):
        raise ValueError("complement band must satisfy 0 < min <= max <= 1")
    return cfg


_file_logger = logging.getLogger("complementbot")
_file_logger.setLevel(logging.INFO)
_file_handler = RotatingFileHandler(LOG_FILE, maxBytes=5_000_000, backupCount=4)
_file_handler.setFormatter(logging.Formatter("%(message)s"))
_file_logger.addHandler(_file_handler)
_file_logger.propagate = False

_journal_logger = logging.getLogger("complement_journal")
_journal_logger.setLevel(logging.INFO)
_journal_handler = RotatingFileHandler(JOURNAL_FILE, maxBytes=5_000_000, backupCount=8)
_journal_handler.setFormatter(logging.Formatter("%(message)s"))
_journal_logger.addHandler(_journal_handler)
_journal_logger.propagate = False


def log_event(event, **kwargs):
    entry = {"ts": datetime.now().isoformat(), "event": event}
    entry.update(kwargs)
    try:
        line = json.dumps(entry, default=str)
    except (TypeError, ValueError):
        line = json.dumps({"ts": entry["ts"], "event": str(event)})
    _file_logger.info(line)
    if is_journal_event(event) or str(event).startswith("complement_"):
        try:
            _journal_logger.info(line)
        except Exception:
            pass


# Primary .env only to compare funders. Complement keys must come from
# .env.complement so this process cannot silently trade the live desk.
load_dotenv(".env")
PRIMARY_FUNDER = os.getenv("FUNDER_ADDRESS")
if not os.path.exists(".env.complement"):
    console.print("[bold red]complementbot requires .env.complement (second account).[/]")
    raise SystemExit(1)
load_dotenv(".env.complement", override=True)

PRIVATE_KEY = os.getenv("PRIVATE_KEY")
FUNDER_ADDRESS = os.getenv("FUNDER_ADDRESS")
API_KEY = os.getenv("API_KEY")
API_SECRET = os.getenv("API_SECRET")
API_PASSPHRASE = os.getenv("API_PASSPHRASE")

if primary_and_complement_same_wallet(PRIMARY_FUNDER, FUNDER_ADDRESS):
    console.print(
        "[bold red]Complement funder matches the primary wallet. "
        "Refusing to start — use a second Polymarket account in .env.complement.[/]"
    )
    raise SystemExit(1)
if not PRIVATE_KEY or not FUNDER_ADDRESS:
    console.print("[bold red].env.complement is missing PRIVATE_KEY or FUNDER_ADDRESS.[/]")
    raise SystemExit(1)

from py_clob_client_v2 import (  # noqa: E402
    ApiCreds,
    ClobClient,
    OrderArgs,
    OrderType,
    PartialCreateOrderOptions,
)
from py_clob_client_v2.order_builder.constants import BUY  # noqa: E402

if API_KEY and API_SECRET and API_PASSPHRASE:
    api_creds = ApiCreds(
        api_key=API_KEY, api_secret=API_SECRET, api_passphrase=API_PASSPHRASE,
    )
else:
    api_creds = ClobClient(
        host=HOST, key=PRIVATE_KEY, chain_id=CHAIN_ID,
    ).create_or_derive_api_key()

client = ClobClient(
    host=HOST,
    key=PRIVATE_KEY,
    chain_id=CHAIN_ID,
    creds=api_creds,
    signature_type=1,
    funder=FUNDER_ADDRESS,
    retry_on_error=False,
)

_strat = load_strategy()
DRY_RUN = bool(_strat["dry_run"])
if DRY_RUN:
    STATE_FILE = "positions_complement.dryrun.json"

book_ws = get_book_feed()
btc_5m = get_btc_feed(SOURCE_TWAP_30, "ptb_twap30_buy5m.json")
btc_15m = get_btc_feed(SOURCE_TWAP_60, "ptb_twap60_buy.json")
console.print(
    f"[bold bright_cyan]▶ COMPLEMENT[/] second account {FUNDER_ADDRESS[:8]}… "
    f"dry_run={DRY_RUN} entry={_strat['entry_enabled']}"
)


def _http_book(token_id):
    try:
        resp = requests.get(
            f"{HOST}/book",
            params={"token_id": token_id},
            timeout=4,
        )
        resp.raise_for_status()
        book = resp.json()
    except Exception as exc:
        log_event("complement_book_fail", token_id=token_id, error=str(exc)[:200])
        return None, 0.0, None, 0.0
    if not isinstance(book, dict):
        return None, 0.0, None, 0.0
    bid, bid_sz = best_from_levels(book.get("bids"), "bid")
    ask, ask_sz = best_from_levels(book.get("asks"), "ask")
    return bid, bid_sz, ask, ask_sz


def quote_other(token_id):
    cached = book_ws.quote(token_id)
    if cached is not None:
        bid, _bs, ask, _as, _mid = cached
        if ask is not None:
            return bid, ask
    bid, _bs, ask, _as = _http_book(token_id)
    return bid, ask


def oracle_favors(armed):
    feed = btc_15m if armed.source == "15m" else btc_5m
    start_ts = float(armed.start_ts or 0)
    if start_ts <= 0 and armed.end_ts > 0:
        start_ts = armed.end_ts - (15 * 60 if armed.source == "15m" else 5 * 60)
    if start_ts <= 0:
        return False
    chk = feed.underlying_check(start_ts, float(_strat["min_underlying_edge_usd"]))
    return bool(chk.get("ok") and str(chk.get("favored") or "") == armed.other_leg)


def unmatched_fak(exc):
    return "no orders found to match" in str(exc or "").lower()


def post_complement(token_id, shares, limit, tick_size):
    if DRY_RUN or not _strat["entry_enabled"]:
        log_event(
            "dry_buy",
            token_id=token_id,
            shares=shares,
            limit=limit,
            dry_run=DRY_RUN,
            entry_enabled=_strat["entry_enabled"],
        )
        console.print(
            f"  [yellow][DRY COMPLEMENT][/] {shares:.2f} sh @ {limit:.2f} {token_id[:10]}…"
        )
        return True, shares, "dry"
    last_err = ""
    tick = str(tick_size)
    for attempt in range(int(_strat["max_retries"])):
        try:
            signed = client.create_order(
                OrderArgs(
                    token_id=token_id,
                    price=float(limit),
                    size=float(shares),
                    side=BUY,
                ),
                options=PartialCreateOrderOptions(tick_size=tick, neg_risk=False),
            )
            client.post_order(signed, order_type=OrderType.FAK)
            log_event(
                "complement_attempt",
                token_id=token_id,
                shares=shares,
                limit=limit,
                attempt=attempt + 1,
            )
            return True, shares, "posted"
        except Exception as exc:
            last_err = str(exc)[:200]
            text = last_err.lower()
            if "invalid tick size" in text and tick == "0.001":
                tick = "0.01"
                continue
            if unmatched_fak(exc) and attempt + 1 < int(_strat["max_retries"]):
                time.sleep(float(_strat["empty_fak_cooldown_s"]))
                continue
            log_event(
                "complement_attempt_rejected",
                token_id=token_id,
                error=last_err,
                attempt=attempt + 1,
            )
            return False, 0.0, last_err
    return False, 0.0, last_err


_shutdown = False


def _handle_shutdown(signum, _frame):
    global _shutdown
    _shutdown = True
    console.print(f"\n[bold yellow]▶ {signal.Signals(signum).name} — exiting after this look[/]")


signal.signal(signal.SIGINT, _handle_shutdown)
signal.signal(signal.SIGTERM, _handle_shutdown)

state = load_json(STATE_FILE)
_skip_mono = {}


def skip_throttled(cid, reason, **kwargs):
    key = (str(cid), str(reason))
    now = time.monotonic()
    last = _skip_mono.get(key, 0.0)
    if now - last < 8.0:
        return
    _skip_mono[key] = now
    log_event("complement_skip", condition_id=cid, reason=reason, **kwargs)


console.print("[bold bright_green]▶ COMPLEMENT ONLINE[/] watching primary fills · other ask ≥80¢")

while not _shutdown:
    try:
        _strat = load_strategy()
    except Exception as exc:
        log_event("cycle_error", error=f"strategy:{exc}"[:200])
        time.sleep(1.0)
        continue
    now_s = time.time()
    batches = []
    files = list(_strat["primary_state_files"])
    sources = list(_strat["primary_sources"])
    for idx, path in enumerate(files):
        source = sources[idx] if idx < len(sources) else f"p{idx}"
        try:
            blob = load_json(path) if os.path.exists(path) else {}
        except Exception as exc:
            log_event("complement_primary_read_fail", path=path, error=str(exc)[:200])
            blob = {}
        batches.append(
            arm_from_primary_meta(
                blob,
                source=source,
                now_s=now_s,
                grace_s=float(_strat["primary_grace_s"]),
            )
        )
    armed = merge_armed(batches)
    book_ws.set_tokens(row.other_token for row in armed)
    for row in armed:
        cid = row.condition_id
        own = state.get(cid) if isinstance(state.get(cid), dict) else {}
        already = bool(own.get("bought_token")) and float(own.get("bought_size") or 0) > 0.01
        bid, ask = quote_other(row.other_token)
        oracle_ok = True
        if _strat["require_oracle"]:
            oracle_ok = oracle_favors(row)
        fire, why, shares = evaluate_complement(
            other_ask=ask,
            other_bid=bid,
            held_shares=row.held_shares,
            already_bought=already,
            primary_still_holding=True,
            oracle_favors_other=oracle_ok,
            min_price=float(_strat["buy_min_price"]),
            max_price=float(_strat["buy_max_price"]),
            max_spread=float(_strat["max_entry_spread"]),
            spend_cap=float(_strat["buy_max_spend"]),
            share_cap=float(_strat["buy_max_shares"]),
            require_oracle=bool(_strat["require_oracle"]),
        )
        if not fire:
            skip_throttled(
                cid, why, source=row.source, other_leg=row.other_leg,
                ask=ask, bid=bid, held=row.held_shares,
            )
            continue
        if not _strat["entry_enabled"] and not DRY_RUN:
            skip_throttled(cid, "entry_disabled")
            continue
        ok, filled, detail = post_complement(
            row.other_token,
            shares,
            float(_strat["buy_max_price"]),
            _strat["tick_size"],
        )
        if ok:
            state[cid] = {
                "bought_token": row.other_token,
                "bought_leg": row.other_leg,
                "bought_size": float(filled),
                "source": row.source,
                "primary_held": row.held_token,
                "slug": row.slug,
                "filled_at": now_s,
            }
            atomic_save(STATE_FILE, state)
            log_event(
                "complement_fill",
                condition_id=cid,
                token_id=row.other_token,
                shares=filled,
                via=detail,
                source=row.source,
            )
            console.print(
                f"  [bold green][COMPLEMENT][/] {row.other_leg} {filled:.2f} sh "
                f"{row.slug} ({row.source})"
            )
    try:
        with open(HEARTBEAT_FILE, "w") as hb:
            hb.write(str(int(now_s)))
    except Exception:
        pass
    sleep_s = float(_strat["poll_s"]) if armed else 0.25
    time.sleep(max(0.01, sleep_s))
