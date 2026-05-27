import os
import time
import json
import traceback
from datetime import datetime
import requests
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
)
from py_clob_client_v2.order_builder.constants import BUY, SELL

console = Console()

load_dotenv()

HOST = "https://clob.polymarket.com"
DATA_API = "https://data-api.polymarket.com"
CHAIN_ID = 137
STATE_FILE = "positions.json"  # metadata cache only: redeem_submitted_at, entered_at
BTC_SLUG_PREFIX = "bitcoin-up-or-down"  # event/market slug filter for managed markets
BTC_SLUG_ALIASES = ("bitcoin-up-or-down", "btc-updown")
PUSD = "0xC011a7E12a19f7B1f670d46F03B03f3342E82DFB"
CTF = "0x4D97DCd97eC945f40cF65F87097ACe5EA0476045"
CTF_COLLATERAL_ADAPTER = "0xAdA100Db00Ca00073811820692005400218FcE1f"

PRIVATE_KEY = os.getenv("PRIVATE_KEY")
FUNDER_ADDRESS = os.getenv("FUNDER_ADDRESS")
API_KEY = os.getenv("API_KEY")
API_SECRET = os.getenv("API_SECRET")
API_PASSPHRASE = os.getenv("API_PASSPHRASE")

# ------------------------- STRATEGY CONFIG -------------------------
SELL_THRESHOLD = float(os.getenv("SELL_THRESHOLD", "0.04"))
EXIT_WINDOW_MIN = float(os.getenv("EXIT_WINDOW_MIN", "20"))
FALLBACK_THRESHOLD = float(os.getenv("FALLBACK_THRESHOLD", "0.10"))
FALLBACK_WINDOW_MIN = float(os.getenv("FALLBACK_WINDOW_MIN", "1.5"))
SELL_GRACE_S = float(os.getenv("SELL_GRACE_S", "30"))
SELL_COOLDOWN_S = float(os.getenv("SELL_COOLDOWN_S", "30"))
REDEEM_THROTTLE_S = float(os.getenv("REDEEM_THROTTLE_S", "300"))
COMPLEMENT_MAX_ASK = float(os.getenv("COMPLEMENT_MAX_ASK", "0.99"))
MAX_REDEEM_AGE_DAYS = float(os.getenv("MAX_REDEEM_AGE_DAYS", "7"))

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
        "[bold bright_green]██╔══██╗   ██║   ██║     [/]   [bright_yellow]//[/]  [dim]BTC HOURLY · 4\u00a2 LOSER TRIG[/]\n"
        "[bold bright_green]██████╔╝   ██║   ╚██████╗[/]   [bright_yellow]//[/]  STATUS: [bold bright_green]\u25cf ARMED[/]\n"
        "[bold bright_green]╚═════╝    ╚═╝    ╚═════╝[/]   [bright_yellow]//[/]  [dim]v8.0 \u00b7 sell-only \u00b7 data-api[/]",
        vertical="middle",
    ),
    title="[bold bright_yellow]▰▱▰▱  TRADING SYSTEM ONLINE  ▱▰▱▰[/]",
    subtitle="[dim]press Ctrl-C to disarm[/]",
    border_style="bright_green",
    box=box.HEAVY_EDGE,
    padding=(1, 4),
)
console.print(banner)

# ------------------------- HELPERS -------------------------


def safe_api_call(func, *args, **kwargs):
    time.sleep(0.3)
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
    """Append a structured JSON log line to bot.log."""
    entry = {"ts": datetime.now().isoformat(), "event": event}
    entry.update(kwargs)
    with open("bot.log", "a") as f:
        f.write(json.dumps(entry) + "\n")


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


def parse_position_end_dt(legs):
    for p in legs:
        for key in ("slug", "eventSlug"):
            slug = p.get(key) or ""
            tail = slug.rsplit("-", 1)[-1]
            if tail.isdigit():
                ts = int(tail)
                if ts > 1_700_000_000:
                    return datetime.fromtimestamp(ts)

    for p in legs:
        end_date = p.get("endDate")
        if not end_date:
            continue
        try:
            return datetime.fromisoformat(end_date.replace("Z", "+00:00"))
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
        now_ms = time.time() * 1000
        minutes_from_end = (end_ts - now_ms) / 60000
        if minutes_from_end <= -120:
            continue
        age_ms = now_ms - end_ts
        redeemable = bool(up.get("redeemable")) or bool(dn.get("redeemable"))
        if age_ms > MAX_REDEEM_AGE_DAYS * 86400 * 1000 and redeemable:
            continue
        sets.append({
            "conditionId": cond,
            "up": up,
            "dn": dn,
            "end_ts": end_ts,
            "end_dt": end_dt,
            "question": up.get("title") or dn.get("title") or "BTC Hourly",
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
                "question": meta.get("question", "BTC Hourly"),
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
    except Exception:
        return None, 0.0


def get_book_ask(token_id):
    try:
        book = safe_api_call(client.get_order_book, token_id)
        asks = book.get("asks", [])
        if not asks:
            return None, 0.0
        best = min(asks, key=lambda x: float(x.get("price", 1)))
        return float(best.get("price", 0)), float(best.get("size", 0))
    except Exception:
        return None, 0.0


def get_midpoint(token_id):
    try:
        price = safe_api_call(client.get_midpoint, token_id)
        if isinstance(price, dict):
            price = price.get("mid")
        return float(price), 0.0
    except Exception:
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


_neg_risk_cache = {}


def sell_with_retry(token_id, size, tick_size="0.01", max_retries=3):
    """FAK sell at best bid. Returns (sold_size, result) or (0, None)."""
    total_sold = 0
    for attempt in range(max_retries):
        remaining = size - total_sold
        if remaining < 1:
            break

        bid_price, bid_depth = get_book_bid(token_id)
        if bid_price is None:
            console.print(f"  [dim yellow][BOOK][/] [dim]bid side empty[/]")
            break

        sell_size = remaining
        if bid_depth < remaining:
            sell_size = round(bid_depth)
            if sell_size < 1 and bid_depth > 0:
                sell_size = min(remaining, 1)
            console.print(f"  [dim yellow][DEPTH][/] [dim]bid={bid_depth:.1f} < req={remaining} · sizing to {sell_size}[/]")

        if sell_size < 1:
            break

        try:
            if token_id not in _neg_risk_cache:
                _neg_risk_cache[token_id] = safe_api_call(client.get_neg_risk, token_id)
            neg_risk = _neg_risk_cache[token_id]
            result = safe_api_call(
                client.create_and_post_order,
                OrderArgs(token_id=token_id, price=bid_price, size=sell_size, side=SELL),
                options=PartialCreateOrderOptions(tick_size=tick_size, neg_risk=neg_risk),
                order_type=OrderType.FAK,
            )
            if result:
                oid = extract_order_id(result)
                # Try to get matched amount directly from result first
                matched = 0
                if isinstance(result, dict):
                    matched = round(float(result.get("size_matched", 0) or result.get("makingAmount", 0) or result.get("takerAmount", 0) or 0))
                else:
                    matched = round(float(getattr(result, "size_matched", 0) or 0))

                # If result shows 0, verify via get_order_details (order may have been archived)
                if matched <= 0:
                    details = get_order_details(oid) if oid else None
                    if details:
                        if details.get("status") == "NOT_FOUND":
                            # FAK was archived after fill — assume full fill for this chunk
                            matched = sell_size
                        else:
                            sm = details.get("size_matched", 0)
                            matched = round(float(sm)) if sm is not None else 0

                if matched <= 0:
                    # API returned order obj but nothing filled — retry
                    console.print(f"  [dim yellow][FAK NULL][/] [dim]0 matched · retrying[/]")
                    time.sleep(1)
                    continue

                total_sold += matched
                log_id = (oid[:16] + "...") if oid and len(str(oid)) > 16 else str(oid)
                console.print(f"  [bold bright_green][FILL ▼][/] SELL {matched:>3} @ {bid_price:.3f}  [dim]id={log_id}[/]")

                if total_sold >= size:
                    return total_sold, result
        except Exception as e:
            console.print(f"  [dim red][FAK FAIL {attempt+1}/{max_retries}][/] [dim]{e}[/]")

        time.sleep(1)

    if total_sold > 0:
        return total_sold, {"partial": True, "sold": total_sold}
    console.print(f"  [bold red][EXIT FAIL][/] limit sell 0/{size} cleared")
    return 0, None


def sell_market_with_retry(token_id, size, price_limit, tick_size="0.01", max_retries=3):
    total_sold = 0
    remaining = int(size)
    price = max(float(price_limit or tick_size), float(tick_size))
    for attempt in range(max_retries):
        if remaining < 1:
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
                filled = remaining
                if isinstance(result, dict):
                    filled = int(float(result.get("size_matched") or result.get("makingAmount") or remaining))
                total_sold += filled
                remaining -= filled
                console.print(f"  [bold green][EXIT FAK][/]{filled} @ ≥{price:.3f}  [dim]id={str(oid)[:16]}...[/]")
                if remaining < 1:
                    return total_sold, result
        except Exception as e:
            console.print(f"  [dim red]Market sell {attempt+1}/{max_retries} failed: {e}[/]")
        time.sleep(1)

    if total_sold > 0:
        return total_sold, {"partial": True, "sold": total_sold}
    console.print(f"  [bold red][EXIT FAIL][/] market sell 0/{size} cleared")
    return 0, None


def buy_market_with_retry(token_id, size, price_limit, tick_size="0.01", max_retries=3):
    total_bought = 0
    remaining = int(size)
    price = min(max(float(price_limit or 1.0), float(tick_size)), 1.0)
    for attempt in range(max_retries):
        if remaining < 1:
            break
        try:
            neg_risk = safe_api_call(client.get_neg_risk, token_id)
            usdc_amount = remaining * price
            result = safe_api_call(
                client.create_and_post_market_order,
                MarketOrderArgs(token_id=token_id, amount=usdc_amount, side=BUY, price=price),
                options=PartialCreateOrderOptions(tick_size=tick_size, neg_risk=neg_risk),
                order_type=OrderType.FAK,
            )
            if result:
                oid = extract_order_id(result)
                filled = remaining
                if isinstance(result, dict):
                    filled = int(float(result.get("size_matched") or result.get("makingAmount") or remaining))
                total_bought += filled
                remaining -= filled
                console.print(f"  [bold green][COMP BUY][/]{filled} @ ≤{price:.3f}  [dim]id={str(oid)[:16]}...[/]")
                if remaining < 1:
                    return total_bought, result
        except Exception as e:
            console.print(f"  [dim red]Complement buy {attempt+1}/{max_retries} failed: {e}[/]")
        time.sleep(1)

    if total_bought > 0:
        return total_bought, {"partial": True, "bought": total_bought}
    console.print(f"  [bold red][COMP BUY FAIL][/] market buy 0/{size} cleared")
    return 0, None


def get_relayer_headers():
    relayer_url = "https://relayer-v2.polymarket.com"
    relayer_headers = {
        "Content-Type": "application/json",
        "RELAYER_API_KEY": "019df62f-45bc-796e-975c-3f434472b163",
        "RELAYER_API_KEY_ADDRESS": "0x42aec4505559c0613f7ce2541d9d29741bc5e195",
    }
    return relayer_url, relayer_headers


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


def merge_complete_set(condition_id, amount, label=""):
    try:
        from eth_abi import encode
        from eth_utils import keccak, to_checksum_address

        merge_size = int(amount)
        if merge_size < 1:
            return None
        pUSD = to_checksum_address(PUSD)
        adapter = to_checksum_address(CTF_COLLATERAL_ADAPTER)
        merge_sel = keccak(b"mergePositions(address,bytes32,bytes32,uint256[],uint256)")[:4]
        merge_data = merge_sel + encode(
            ["address", "bytes32", "bytes32", "uint256[]", "uint256"],
            [pUSD, bytes(32), bytes.fromhex(condition_id.lower().removeprefix("0x")), [1, 2], merge_size * 1_000_000],
        )
        execute_sel = keccak(b"execute(address,uint256,bytes)")[:4]
        proxy_data = execute_sel + encode(
            ["address", "uint256", "bytes"],
            [adapter, 0, merge_data],
        )
        tx_id, err = submit_proxy_tx(to_checksum_address(FUNDER_ADDRESS), proxy_data)
        if tx_id:
            console.print(f"  [bold bright_green][MERGE ▶][/] {label}  [dim]tx={str(tx_id)[:18]}…[/]")
            return tx_id
        console.print(f"  [dim red][MERGE FAIL][/] {label}  [dim]{err}[/]")
        return None
    except Exception as e:
        console.print(f"  [dim red][MERGE ERR][/] {label}  [dim]{e}[/]")
        return None


def quote_complete_set_exit(trigger_bid, other_bid, trigger_mid, other_mid):
    matched = None if trigger_bid is None else float(trigger_bid)
    other = other_bid if other_bid is not None else other_mid
    other = 0.0 if other is None else float(other)
    merged = max(0.0, 1.0 - other)
    return max(matched or 0.0, merged), matched, merged


# ------------------------- REDEEM -------------------------

def redeem_condition(condition_id, label=""):
    """Submit a redemption tx for a resolved Polymarket conditionId via the Polygon relayer.
    Returns the transactionID string on success, or None on failure."""
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
        return None
    except Exception as e:
        console.print(f"  [dim red][SETTLE ERR][/] {label}  [dim]{e}[/]")
        return None


# ------------------------- MAIN LOOP -------------------------
# positions_meta is a metadata cache keyed by conditionId. The on-chain holdings
# (size, redeemable flag, etc.) come fresh from data-api each cycle. We only
# persist:
#   - entered_at: when we first saw this set (used for 30s sell grace)
#   - redeem_submitted_at: throttle redemption resubmissions
#   - last_sell_up_at / last_sell_dn_at: 30s post-sell cooldown per leg
positions_meta = load_json(STATE_FILE)
CYCLE = 0

while True:
    try:
        CYCLE += 1
        now_ms = time.time() * 1000
        now_str = datetime.now().strftime("%H:%M:%S")

        pusd_bal = get_balance()
        positions_raw = get_user_positions()
        managed_sets = group_btc_complete_sets(positions_raw or [], positions_meta)

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
                    up_sz = round(float(s["up"].get("size", 0)))
                    dn_sz = round(float(s["dn"].get("size", 0)))

                    if s["up"].get("redeemable") or s["dn"].get("redeemable"):
                        state = "[bold bright_magenta]\u2713 REDEEM[/]"
                    elif mins <= 0:
                        state = "[dim]\u00b7 closed[/]"
                    elif mins <= EXIT_WINDOW_MIN:
                        state = "[bold red]\u25cc EXIT WINDOW[/]"
                    else:
                        state = "[bold bright_green]\u25cf HOLD[/]"

                    if mins < 20:
                        mins_style = "bold red"
                    elif mins < 60:
                        mins_style = "yellow"
                    else:
                        mins_style = "green"

                    table.add_row(
                        (s["question"] or "?")[:40],
                        ends_str,
                        f"[{mins_style}]{mins:.0f}m[/]",
                        f"{up_sz}",
                        f"{dn_sz}",
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
            meta = positions_meta.setdefault(cond, {})
            last = meta.get("redeem_submitted_at") or 0
            if now_ms - last < REDEEM_THROTTLE_S * 1000:
                continue  # already submitted, wait 5 min before retry
            tx = redeem_condition(cond, label=(s["question"] or "?")[:32])
            if tx:
                meta["redeem_submitted_at"] = now_ms
                log_event("redeem_submit", condition_id=cond, tx_id=str(tx))
                save_json(STATE_FILE, positions_meta)

        # ================= SELL PHASE =================
        for s in managed_sets:
            end_ts = s["end_ts"]
            minutes_left = (end_ts - now_ms) / 60000
            if minutes_left <= 0:
                continue

            cond = s["conditionId"]
            up_token = s["up"].get("asset")
            dn_token = s["dn"].get("asset")
            meta = positions_meta.setdefault(cond, {})
            if "entered_at" not in meta:
                meta["entered_at"] = now_ms
                meta["up_token"] = up_token
                meta["dn_token"] = dn_token
                meta["question"] = s["question"]
                meta["end_date"] = s["up"].get("endDate") or s["dn"].get("endDate")
                save_json(STATE_FILE, positions_meta)
            if now_ms - meta["entered_at"] < SELL_GRACE_S * 1000:
                continue
            up_size = round(float(s["up"].get("size", 0)))
            dn_size = round(float(s["dn"].get("size", 0)))
            if up_size < 1 and dn_size < 1:
                continue

            up_bid, _ = get_book_bid(up_token) if up_token else (None, 0.0)
            dn_bid, _ = get_book_bid(dn_token) if dn_token else (None, 0.0)
            up_ask, _ = get_book_ask(up_token) if up_token else (None, 0.0)
            dn_ask, _ = get_book_ask(dn_token) if dn_token else (None, 0.0)
            up_mid, _ = get_midpoint(up_token) if up_token else (None, 0.0)
            dn_mid, _ = get_midpoint(dn_token) if dn_token else (None, 0.0)

            # Strategy: in the last 20 minutes, sell whichever side has dropped to 4c
            # (the loser leg). Hold the other side to expiry for the $1 payout.
            up_price, up_matched_price, up_merge_price = quote_complete_set_exit(up_bid, dn_bid, up_mid, dn_mid)
            dn_price, dn_matched_price, dn_merge_price = quote_complete_set_exit(dn_bid, up_bid, dn_mid, up_mid)
            up_trigger = up_size > 0 and minutes_left <= EXIT_WINDOW_MIN and up_price is not None and up_price <= SELL_THRESHOLD
            dn_trigger = dn_size > 0 and minutes_left <= EXIT_WINDOW_MIN and dn_price is not None and dn_price <= SELL_THRESHOLD

            if minutes_left <= FALLBACK_WINDOW_MIN:
                up_trigger = up_trigger or (up_size > 0 and up_price is not None and up_price <= FALLBACK_THRESHOLD)
                dn_trigger = dn_trigger or (dn_size > 0 and dn_price is not None and dn_price <= FALLBACK_THRESHOLD)

            # Guard: if both legs trigger, only sell the lower-priced one to ensure
            # we still hold a winner for the $1 payout at resolution.
            if up_trigger and dn_trigger:
                if up_price <= dn_price:
                    dn_trigger = False
                else:
                    up_trigger = False

            sell_up = up_trigger
            sell_dn = dn_trigger

            will_sell_up = sell_up and (now_ms - (meta.get("last_sell_up_at") or 0) >= SELL_COOLDOWN_S * 1000)
            will_sell_dn = sell_dn and (now_ms - (meta.get("last_sell_dn_at") or 0) >= SELL_COOLDOWN_S * 1000)
            merge_amount = min(up_size, dn_size)
            up_complement_exit = None if dn_ask is None else max(0.0, 1.0 - dn_ask)
            dn_complement_exit = None if up_ask is None else max(0.0, 1.0 - up_ask)
            up_single_leg_merge = (
                will_sell_up
                and merge_amount < 1
                and dn_token
                and dn_ask is not None
                and dn_ask <= COMPLEMENT_MAX_ASK
                and up_complement_exit >= (up_matched_price or 0.0)
            )
            dn_single_leg_merge = (
                will_sell_dn
                and merge_amount < 1
                and up_token
                and up_ask is not None
                and up_ask <= COMPLEMENT_MAX_ASK
                and dn_complement_exit >= (dn_matched_price or 0.0)
            )
            merge_trigger = (
                merge_amount >= 1 and will_sell_up and up_merge_price >= (up_matched_price or 0.0)
            ) or (
                merge_amount >= 1 and will_sell_dn and dn_merge_price >= (dn_matched_price or 0.0)
            )

            if will_sell_up or will_sell_dn:
                up_bid_str = f"{up_price:.3f}" if up_price is not None else "  -  "
                dn_bid_str = f"{dn_price:.3f}" if dn_price is not None else "  -  "
                console.print(Panel(
                    f"  [bright_white]{s['question']}[/]\n"
                    f"  [bright_green]UP[/]   px [bold]{up_bid_str}[/]  inv [bold]{up_size:>3}[/]   \u2502   "
                    f"[bright_red]DN[/]  px [bold]{dn_bid_str}[/]  inv [bold]{dn_size:>3}[/]   \u2502   "
                    f"[bold red]TTM {minutes_left:>4.1f}m[/]",
                    title="[bold bright_yellow]\u25bc EXIT TRIGGER \u2014 LOSER LEG[/]",
                    border_style="bright_yellow",
                    box=box.HEAVY,
                ))

            if merge_trigger and merge_amount >= 1:
                log_event("merge_attempt", condition_id=cond, size=merge_amount)
                tx = merge_complete_set(cond, merge_amount, label=s["question"][:52])
                if tx:
                    meta["last_sell_up_at"] = now_ms
                    meta["last_sell_dn_at"] = now_ms
                    log_event("merge_submitted", condition_id=cond, size=merge_amount, tx=tx)
                    save_json(STATE_FILE, positions_meta)
                continue

            if up_single_leg_merge:
                log_event("complement_merge_attempt", condition_id=cond, leg="up", size=up_size, complement="down", ask=dn_ask, effective_exit=up_complement_exit)
                bought, _ = buy_market_with_retry(dn_token, up_size, dn_ask)
                if bought > 0:
                    tx = merge_complete_set(cond, min(up_size, bought), label=s["question"][:52])
                    if tx:
                        meta["last_sell_up_at"] = now_ms
                        meta["last_sell_dn_at"] = now_ms
                        meta["expected_up_size"] = max(0, up_size - bought)
                        log_event("complement_merge_submitted", condition_id=cond, leg="up", complement_bought=bought, tx=tx)
                        save_json(STATE_FILE, positions_meta)
                        continue
                    log_event("complement_merge_fail", condition_id=cond, leg="up", complement_bought=bought)
                else:
                    log_event("complement_buy_fail", condition_id=cond, leg="up", size=up_size, complement="down", ask=dn_ask, effective_exit=up_complement_exit)

            if dn_single_leg_merge:
                log_event("complement_merge_attempt", condition_id=cond, leg="down", size=dn_size, complement="up", ask=up_ask, effective_exit=dn_complement_exit)
                bought, _ = buy_market_with_retry(up_token, dn_size, up_ask)
                if bought > 0:
                    tx = merge_complete_set(cond, min(dn_size, bought), label=s["question"][:52])
                    if tx:
                        meta["last_sell_up_at"] = now_ms
                        meta["last_sell_dn_at"] = now_ms
                        meta["expected_dn_size"] = max(0, dn_size - bought)
                        log_event("complement_merge_submitted", condition_id=cond, leg="down", complement_bought=bought, tx=tx)
                        save_json(STATE_FILE, positions_meta)
                        continue
                    log_event("complement_merge_fail", condition_id=cond, leg="down", complement_bought=bought)
                else:
                    log_event("complement_buy_fail", condition_id=cond, leg="down", size=dn_size, complement="up", ask=up_ask, effective_exit=dn_complement_exit)

            if sell_up:
                if not will_sell_up:
                    console.print(f"  [dim][SKIP][/] [dim]UP sell suppressed · sold <{SELL_COOLDOWN_S:.0f}s ago[/]")
                else:
                    log_event("sell_attempt", condition_id=cond, leg="up", size=up_size, bid=up_bid, price_limit=up_price)
                    sold, _ = sell_market_with_retry(up_token, up_size, up_price or SELL_THRESHOLD)
                    if sold > 0:
                        meta["last_sell_up_at"] = now_ms
                        meta["expected_up_size"] = up_size - sold
                        log_event("sell_fill", condition_id=cond, leg="up", sold=sold, remaining=up_size - sold, price=up_price)
                        save_json(STATE_FILE, positions_meta)
                    else:
                        log_event("sell_fail", condition_id=cond, leg="up", size=up_size, bid=up_bid, price_limit=up_price)
            if sell_dn:
                if not will_sell_dn:
                    console.print(f"  [dim][SKIP][/] [dim]DN sell suppressed · sold <{SELL_COOLDOWN_S:.0f}s ago[/]")
                else:
                    log_event("sell_attempt", condition_id=cond, leg="down", size=dn_size, bid=dn_bid, price_limit=dn_price)
                    sold, _ = sell_market_with_retry(dn_token, dn_size, dn_price or SELL_THRESHOLD)
                    if sold > 0:
                        meta["last_sell_dn_at"] = now_ms
                        meta["expected_dn_size"] = dn_size - sold
                        log_event("sell_fill", condition_id=cond, leg="down", sold=sold, remaining=dn_size - sold, price=dn_price)
                        save_json(STATE_FILE, positions_meta)
                    else:
                        log_event("sell_fail", condition_id=cond, leg="down", size=dn_size, bid=dn_bid, price_limit=dn_price)

    except Exception:
        log_event("cycle_error", traceback=traceback.format_exc())
        console.print(Panel(
            traceback.format_exc(),
            title="[bold bright_red]\u25a0\u25a0  SYSTEM FAULT  \u25a0\u25a0[/]",
            subtitle="[dim]auto-restart in 30s \u00b7 cycle aborted[/]",
            border_style="bright_red",
            box=box.HEAVY_EDGE,
        ))

    console.print("[dim bright_black]\u00b7 \u00b7 \u00b7  sleeping 30s  \u00b7 \u00b7 \u00b7[/]")
    time.sleep(30)
