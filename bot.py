import os
import time
import json
import traceback
import tempfile
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
    OrderArgs,
    OrderType,
    PartialCreateOrderOptions,
)
from py_clob_client_v2.order_builder.constants import BUY, SELL
from py_clob_client_v2.clob_types import OrderPayload

console = Console()

load_dotenv()

HOST = "https://clob.polymarket.com"
GAMMA_API = "https://gamma-api.polymarket.com"
CHAIN_ID = 137
STATE_FILE = "positions.json"
PENDING_FILE = "pending.json"
MAX_POSITIONS = 5
PENDING_TIMEOUT_MS = 5 * 60 * 1000  # 5 minutes

PRIVATE_KEY = os.getenv("PRIVATE_KEY")
FUNDER_ADDRESS = os.getenv("FUNDER_ADDRESS")
API_KEY = os.getenv("API_KEY")
API_SECRET = os.getenv("API_SECRET")
API_PASSPHRASE = os.getenv("API_PASSPHRASE")

# ------------------------- CLIENT SETUP -------------------------
if API_KEY and API_SECRET and API_PASSPHRASE:
    from py_clob_client_v2 import ApiCreds
    api_creds = ApiCreds(
        api_key=API_KEY,
        api_secret=API_SECRET,
        api_passphrase=API_PASSPHRASE,
    )
    console.print("[bold cyan]Using pre-generated API credentials[/]")
else:
    temp_client = ClobClient(host=HOST, key=PRIVATE_KEY, chain_id=CHAIN_ID)
    api_creds = temp_client.create_or_derive_api_key()
    console.print("[bold cyan]Auto-derived API credentials[/]")

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
    console.print("[dim green]Balance allowance synced[/]")
except Exception as e:
    console.print(f"[dim red]Balance sync warning: {e}[/]")

banner = Panel(
    Align.center(
        "[bold white]Polymarket BTC Straddle Bot v6[/]\n"
        "[dim]GTC Entry + FAK Exit + Order Tracking[/]",
        vertical="middle",
    ),
    title="[bold yellow]v6[/]",
    border_style="bright_yellow",
    box=box.HEAVY_EDGE,
    padding=(1, 4),
)
console.print(banner)

eoa_address = None
try:
    from eth_account import Account
    eoa_address = Account.from_key(PRIVATE_KEY).address
except Exception:
    pass

deposit_addr = "unknown"
try:
    bridge_res = requests.post(
        "https://bridge.polymarket.com/deposit",
        json={"address": FUNDER_ADDRESS},
        headers={"Content-Type": "application/json"},
        timeout=10,
    )
    if bridge_res.status_code == 201:
        deposit_addr = bridge_res.json().get("address", {}).get("evm", "unknown")
except Exception:
    pass

# ------------------------- HELPERS -------------------------

def safe_api_call(func, *args, **kwargs):
    """Rate-limited API wrapper."""
    time.sleep(0.3)
    try:
        return func(*args, **kwargs)
    except Exception as e:
        # Only print if it's not a common expected error
        err_str = str(e)
        if "order couldn't be fully filled" not in err_str and "not enough balance" not in err_str:
            console.print(f"  [dim red]API error: {err_str[:120]}[/]")
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


def show_wallet(pusd_bal):
    wallet_lines = f"  [bold]Proxy Wallet:[/] [cyan]{FUNDER_ADDRESS}[/]\n"
    wallet_lines += f"  [bold]pUSD Balance:[/] [{'bold green' if pusd_bal >= 2 else 'bold red'}]{pusd_bal:.2f} pUSD[/]\n"
    if eoa_address:
        wallet_lines += f"  [dim]EOA: {eoa_address}[/]\n"
    if deposit_addr != "unknown":
        wallet_lines += f"  [bold]Deposit:[/] [cyan]{deposit_addr}[/]\n"
    wallet_lines += "  [dim]Send USDC (Polygon) to deposit address above[/]"
    wallet_panel = Panel(
        wallet_lines,
        title="[bold yellow]Wallet Info[/]",
        border_style="yellow",
        box=box.ROUNDED,
    )
    console.print(wallet_panel)


# ------------------------- TOKEN & MARKET -------------------------

def get_yes_no_tokens(market):
    clob = market.get("clobTokenIds")
    if isinstance(clob, str):
        try:
            token_list = json.loads(clob)
            if isinstance(token_list, list) and len(token_list) == 2:
                return token_list[0], token_list[1]
        except Exception:
            pass
    if isinstance(clob, list) and len(clob) == 2:
        return clob[0], clob[1]
    raise Exception(f"Could not map YES/NO tokens for market {market['id']}")


def get_active_btc_hourly_markets():
    now = time.time() * 1000
    try:
        res = safe_api_call(
            requests.get,
            f"{GAMMA_API}/events",
            params={"series_slug": "btc-up-or-down-hourly", "active": "true", "closed": "false", "limit": 20},
            timeout=10,
        )
        res.raise_for_status()
        events = res.json()
        console.print(f"  [dim]API returned {len(events)} events[/]")
    except Exception as e:
        console.print(f"  [bold red]API fetch failed: {e}[/]")
        return []

    candidates = []
    for ev in events:
        markets = ev.get("markets", [])
        if not markets:
            continue
        m = markets[0]
        try:
            end_date = m.get("endDate") or m.get("end_date")
            if not end_date:
                continue
            end_ts = datetime.fromisoformat(end_date.replace("Z", "+00:00")).timestamp() * 1000
            if end_ts > now:
                candidates.append((end_ts, m))
        except Exception:
            continue

    candidates.sort(key=lambda x: x[0])
    console.print(f"  [dim]{len(candidates)} future markets found[/]")
    return [m for _, m in candidates]


# ------------------------- PRICING & DEPTH -------------------------

def get_book_ask(token_id):
    """Return (best_ask_price, best_ask_size)."""
    try:
        book = safe_api_call(client.get_order_book, token_id)
        asks = book.get("asks", [])
        if not asks:
            return None, 0.0
        best = min(asks, key=lambda x: float(x.get("price", 999)))
        return float(best.get("price", 0)), float(best.get("size", 0))
    except Exception as e:
        console.print(f"  [dim red]Book ask failed: {e}[/]")
        return None, 0.0


def get_book_bid(token_id):
    """Return (best_bid_price, best_bid_size)."""
    try:
        book = safe_api_call(client.get_order_book, token_id)
        bids = book.get("bids", [])
        if not bids:
            return None, 0.0
        best = max(bids, key=lambda x: float(x.get("price", 0)))
        return float(best.get("price", 0)), float(best.get("size", 0))
    except Exception as e:
        console.print(f"  [dim red]Book bid failed: {e}[/]")
        return None, 0.0


# ------------------------- ORDER PLACEMENT -------------------------

def extract_order_id(order_obj):
    if isinstance(order_obj, dict):
        return order_obj.get("orderID") or order_obj.get("id")
    return getattr(order_obj, "orderID", None) or getattr(order_obj, "id", None) or str(order_obj)


def place_order(token_id, price, size, side, order_type, tick_size="0.01"):
    try:
        neg_risk = safe_api_call(client.get_neg_risk, token_id)
        order = safe_api_call(
            client.create_and_post_order,
            OrderArgs(token_id=token_id, price=price, size=size, side=side),
            options=PartialCreateOrderOptions(tick_size=tick_size, neg_risk=neg_risk),
            order_type=order_type,
        )
        oid = extract_order_id(order)
        console.print(f"  [bold green]PLACED[/] {side.upper()} {size} @ {price:.3f}  id={oid[:16]}...")
        return order
    except Exception as e:
        console.print(f"  [bold red]PLACE FAILED[/] {side.upper()} {size} @ {price:.3f}: {e}")
        return None


def get_order_status(order_id):
    """Return status string or 'UNKNOWN'."""
    try:
        result = safe_api_call(client.get_order, order_id)
        if result is None:
            return "UNKNOWN"
        if isinstance(result, dict):
            return result.get("status", "UNKNOWN")
        return getattr(result, "status", "UNKNOWN")
    except Exception as e:
        if "not found" in str(e).lower() or "404" in str(e):
            return "NOT_FOUND"
        return "UNKNOWN"


def cancel_order_safe(order_id):
    try:
        safe_api_call(client.cancel_order, OrderPayload(orderID=order_id))
        return True
    except Exception:
        return False


def sell_with_retry(token_id, size, tick_size="0.01", max_retries=3):
    """FAK sell at best bid. Adjusts size to bid depth. Retries."""
    for attempt in range(max_retries):
        bid_price, bid_depth = get_book_bid(token_id)
        if bid_price is None:
            console.print(f"  [dim]No bids available[/]")
            return None

        sell_size = size
        if bid_depth < size:
            sell_size = int(bid_depth)
            console.print(f"  [dim]Bid depth {bid_depth:.1f} < {size}, selling {sell_size}[/]")
            if sell_size < 1:
                return None

        try:
            neg_risk = safe_api_call(client.get_neg_risk, token_id)
            result = safe_api_call(
                client.create_and_post_order,
                OrderArgs(token_id=token_id, price=bid_price, size=sell_size, side=SELL),
                options=PartialCreateOrderOptions(tick_size=tick_size, neg_risk=neg_risk),
                order_type=OrderType.FAK,
            )
            if result:
                oid = extract_order_id(result)
                console.print(f"  [bold green]SOLD[/] {sell_size} @ {bid_price:.3f}  id={oid[:16]}...")
                return result
        except Exception as e:
            console.print(f"  [dim red]Sell attempt {attempt+1}/{max_retries} failed: {e}[/]")

        time.sleep(1)

    console.print(f"  [bold red]SELL FAILED after {max_retries} retries[/]")
    return None


# ------------------------- MAIN LOOP -------------------------
positions = load_json(STATE_FILE)
pending = load_json(PENDING_FILE)
CYCLE = 0

while True:
    try:
        CYCLE += 1
        now_ms = time.time() * 1000
        now_str = datetime.now().strftime("%H:%M:%S")

        # Re-read balance every cycle
        pusd_bal = get_balance()

        markets = get_active_btc_hourly_markets()

        console.rule(
            f"[bold blue]CYCLE #{CYCLE}[/]  [dim]{now_str}[/]  "
            f"[green]{len(markets)} markets[/]  "
            f"[yellow]{len(positions)} pos[/]  "
            f"[cyan]{len(pending)} pending[/]  "
            f"[bold]{'${:.2f}'.format(pusd_bal)}[/]",
            style="blue",
        )

        # ================= MARKET TABLE =================
        if markets:
            table = Table(
                title="Active BTC Hourly Markets",
                box=box.ROUNDED,
                border_style="dim blue",
                title_style="bold cyan",
                show_lines=True,
            )
            table.add_column("Market", style="white", max_width=40)
            table.add_column("Ends", style="dim cyan")
            table.add_column("Min Left", justify="right")
            table.add_column("In Pos?", justify="center")

            for m in markets:
                try:
                    end_date = m.get("endDate") or m.get("end_date")
                    end_ts = datetime.fromisoformat(end_date.replace("Z", "+00:00")).timestamp() * 1000
                    mins = (end_ts - now_ms) / 60000
                    ends_str = datetime.fromisoformat(end_date.replace("Z", "+00:00")).strftime("%H:%M")
                    status = "[dim]-[/]"
                    if m["id"] in positions:
                        status = "[green]POS[/]"
                    elif m["id"] in pending:
                        status = "[yellow]PEND[/]"

                    if mins < 20:
                        mins_style = "bold red"
                    elif mins < 60:
                        mins_style = "yellow"
                    elif mins < 180:
                        mins_style = "green"
                    else:
                        mins_style = "dim"

                    table.add_row(m.get("question", "?")[:40], ends_str, f"[{mins_style}]{mins:.0f}m[/]", status)
                except Exception:
                    continue
            console.print(table)

        # ================= CLEANUP EXPIRED POSITIONS =================
        for mid in list(positions.keys()):
            pos = positions.get(mid)
            if pos and pos.get("end_ts") and now_ms > pos["end_ts"] + 300000:
                console.print(f"  [dim]Expired market {mid[:20]}... removed[/]")
                del positions[mid]
                save_json(STATE_FILE, positions)

        # ================= REDEEM SETTLED POSITIONS =================
        def redeem_positions():
            if not positions:
                return
            for mid in list(positions.keys()):
                pos = positions[mid]
                if pos.get("redeemed"):
                    continue
                try:
                    m_res = requests.get(f"{GAMMA_API}/markets/{mid}", timeout=10)
                    if m_res.status_code != 200:
                        continue
                    m_data = m_res.json()
                    if not m_data.get("closed"):
                        continue
                    condition_id = m_data.get("conditionId")
                    if not condition_id:
                        continue

                    from eth_abi import encode
                    from eth_utils import keccak, to_checksum_address
                    from eth_account import Account

                    pUSD = to_checksum_address("0xC011a7E12a19f7B1f670d46F03B03f3342E82DFB")
                    CTF = to_checksum_address("0x4D97DCd97eC945f40cF65F87097ACe5EA0476045")
                    proxy = to_checksum_address(FUNDER_ADDRESS)
                    eoa = Account.from_key(PRIVATE_KEY).address

                    redeem_sel = keccak(b"redeemPositions(address,bytes32,bytes32,uint256[])")[:4]
                    redeem_data = redeem_sel + encode(
                        ["address", "bytes32", "bytes32", "uint256[]"],
                        [pUSD, bytes(32), bytes.fromhex(condition_id.replace("0x", "")), [1, 2]],
                    )

                    execute_sel = keccak(b"execute(address,uint256,bytes)")[:4]
                    proxy_data = execute_sel + encode(
                        ["address", "uint256", "bytes"],
                        [CTF, 0, redeem_data],
                    )

                    relayer_url = "https://relayer-v2.polymarket.com"
                    relayer_headers = {
                        "Content-Type": "application/json",
                        "RELAYER_API_KEY": "019df62f-45bc-796e-975c-3f434472b163",
                        "RELAYER_API_KEY_ADDRESS": "0x42aec4505559c0613f7ce2541d9d29741bc5e195",
                    }

                    nonce_r = requests.get(
                        f"{relayer_url}/nonce",
                        params={"address": eoa, "type": "PROXY"},
                        headers=relayer_headers,
                        timeout=10,
                    )
                    nonce = nonce_r.json().get("nonce", "0")

                    body = {
                        "type": "PROXY",
                        "from": eoa,
                        "to": proxy,
                        "nonce": nonce,
                        "data": "0x" + proxy_data.hex(),
                        "value": "0",
                    }

                    submit_r = requests.post(
                        f"{relayer_url}/submit",
                        json=body,
                        headers=relayer_headers,
                        timeout=10,
                    )

                    if submit_r.status_code == 200:
                        tx_id = submit_r.json().get("transactionID")
                        console.print(f"  [dim green]Redeem submitted for {mid[:20]}... tx={tx_id[:20]}[/]")
                        pos["redeemed"] = True
                        save_json(STATE_FILE, positions)
                    else:
                        console.print(f"  [dim red]Redeem failed for {mid}: {submit_r.status_code} {submit_r.text[:100]}[/]")
                except Exception as e:
                    console.print(f"  [dim red]Redeem error for {mid}: {e}[/]")

        redeem_positions()

        # ================= PROCESS PENDING GTC ORDERS =================
        for mid in list(pending.keys()):
            p = pending[mid]
            age = now_ms - p.get("placed_at", 0)
            if age < 120_000:  # Let GTC orders sit for 2 minutes before acting
                console.print(f"  [dim]Pending {mid[:20]}... age={age//1000}s, letting sit[/]")
                continue

            yes_status = get_order_status(p["yes_order_id"])
            no_status = get_order_status(p["no_order_id"])

            yes_filled = yes_status in ("FILLED", "MATCHED", "DONE", "FULLY_FILLED")
            no_filled = no_status in ("FILLED", "MATCHED", "DONE", "FULLY_FILLED")

            console.print(f"  [dim]Pending {mid[:20]}... YES={yes_status} NO={no_status}[/]")

            if yes_filled and no_filled:
                # Full straddle achieved
                positions[mid] = {
                    "yes_size": p["yes_size"],
                    "no_size": p["no_size"],
                    "yes_sold": False,
                    "no_sold": False,
                    "end_ts": p["end_ts"],
                }
                save_json(STATE_FILE, positions)
                del pending[mid]
                save_json(PENDING_FILE, pending)
                console.print("  [bold green]FULL STRADDLE ENTERED[/]")

            elif yes_filled and not no_filled:
                cancel_order_safe(p["no_order_id"])
                sell_with_retry(p["yes_token"], p["yes_size"])
                del pending[mid]
                save_json(PENDING_FILE, pending)
                console.print("  [yellow]Partial - flattened YES[/]")

            elif no_filled and not yes_filled:
                cancel_order_safe(p["yes_order_id"])
                sell_with_retry(p["no_token"], p["no_size"])
                del pending[mid]
                save_json(PENDING_FILE, pending)
                console.print("  [yellow]Partial - flattened NO[/]")

            elif now_ms > p.get("placed_at", 0) + PENDING_TIMEOUT_MS:
                console.print(f"  [dim]Pending timeout ({PENDING_TIMEOUT_MS//60000}min) - cancelling[/]")
                cancel_order_safe(p["yes_order_id"])
                cancel_order_safe(p["no_order_id"])
                del pending[mid]
                save_json(PENDING_FILE, pending)

        # ================= SELL PHASE =================
        for market in markets:
            mid = market["id"]
            if mid not in positions:
                continue
            pos = positions[mid]

            end_date = market.get("endDate") or market.get("end_date")
            end_ts = datetime.fromisoformat(end_date.replace("Z", "+00:00")).timestamp() * 1000
            minutes_left = (end_ts - now_ms) / 60000

            if minutes_left <= 0:
                continue

            yes_token, no_token = get_yes_no_tokens(market)
            yes_ask, _ = get_book_ask(yes_token)
            no_ask, _ = get_book_ask(no_token)
            if yes_ask is None or no_ask is None:
                continue

            sell_yes = not pos["yes_sold"] and (yes_ask <= 0.02 or (minutes_left <= 20 and yes_ask < 0.03))
            sell_no = not pos["no_sold"] and (no_ask <= 0.02 or (minutes_left <= 20 and no_ask < 0.03))

            if sell_yes or sell_no:
                sell_panel = Panel(
                    f"  [white]{market['question']}[/]\n"
                    f"  UP=[{'bold green' if yes_ask > 0.5 else 'bold red'}]{yes_ask:.3f}[/]  "
                    f"DOWN=[{'bold green' if no_ask > 0.5 else 'bold red'}]{no_ask:.3f}[/]  "
                    f"[yellow]{minutes_left:.1f}m left[/]",
                    title="[bold yellow]SELL CHECK[/]",
                    border_style="yellow",
                    box=box.ROUNDED,
                )
                console.print(sell_panel)

            if sell_yes:
                result = sell_with_retry(yes_token, pos["yes_size"])
                if result:
                    pos["yes_sold"] = True

            if sell_no:
                result = sell_with_retry(no_token, pos["no_size"])
                if result:
                    pos["no_sold"] = True

            save_json(STATE_FILE, positions)

        # ================= BUY PHASE =================
        active_count = len(positions) + len(pending)
        if active_count >= MAX_POSITIONS:
            console.print(f"  [dim]Max positions reached ({active_count}/{MAX_POSITIONS})[/]")
        else:
            for market in markets:
                mid = market["id"]
                if mid in positions or mid in pending:
                    continue

                end_date = market.get("endDate") or market.get("end_date")
                end_ts = datetime.fromisoformat(end_date.replace("Z", "+00:00")).timestamp() * 1000
                minutes_ahead = (end_ts - now_ms) / 60000

                if not (60 < minutes_ahead < 300):
                    continue

                yes_token, no_token = get_yes_no_tokens(market)
                yes_ask, yes_depth = get_book_ask(yes_token)
                no_ask, no_depth = get_book_ask(no_token)
                if yes_ask is None or no_ask is None:
                    continue

                buy_panel = Panel(
                    f"  [white]{market['question']}[/]\n"
                    f"  UP={yes_ask:.3f} ({yes_depth:.1f} avail)  "
                    f"DOWN={no_ask:.3f} ({no_depth:.1f} avail)  "
                    f"[cyan]{minutes_ahead:.1f}m ahead[/]",
                    title="[bold green]BUY CHECK[/]",
                    border_style="green",
                    box=box.ROUNDED,
                )
                console.print(buy_panel)

                # Entry: equal prices, both <= 0.52
                if round(yes_ask, 3) == round(no_ask, 3) and yes_ask <= 0.52:
                    min_size = market.get("orderMinSize", 1)

                    # Target $1.00 per side
                    target_dollars = 1.0
                    size = max(1, round(target_dollars / yes_ask))

                    # Cap by balance
                    total_cost = size * (yes_ask + no_ask)
                    if total_cost > pusd_bal:
                        size = int(pusd_bal / (yes_ask + no_ask))
                        console.print(f"  [dim]Balance cap: size={size} (bal={pusd_bal:.2f}, need={total_cost:.2f})[/]")

                    if size < min_size:
                        console.print(f"  [dim]Size too small: {size} < {min_size}[/]")
                        continue

                    tick_size = str(market.get("orderPriceMinTickSize", "0.01"))
                    console.print(Panel(
                        f"  [bold white]{market['question']}[/]\n"
                        f"  Size: [bold yellow]{size}[/]  YES @ {yes_ask:.3f} + NO @ {no_ask:.3f}",
                        title="[bold bright_yellow]STRADDLE ENTRY[/]",
                        border_style="bright_yellow",
                        box=box.HEAVY_EDGE,
                    ))

                    yes_order = place_order(yes_token, yes_ask, size, BUY, OrderType.GTC, tick_size)
                    no_order = place_order(no_token, no_ask, size, BUY, OrderType.GTC, tick_size)

                    if yes_order and no_order:
                        pending[mid] = {
                            "yes_order_id": extract_order_id(yes_order),
                            "no_order_id": extract_order_id(no_order),
                            "yes_token": yes_token,
                            "no_token": no_token,
                            "yes_size": size,
                            "no_size": size,
                            "placed_at": now_ms,
                            "end_ts": end_ts,
                        }
                        save_json(PENDING_FILE, pending)
                    else:
                        # Clean up any placed order
                        if yes_order:
                            cancel_order_safe(extract_order_id(yes_order))
                        if no_order:
                            cancel_order_safe(extract_order_id(no_order))

                    time.sleep(2)

    except Exception:
        console.print(Panel(
            traceback.format_exc(),
            title="[bold red]CRITICAL ERROR[/]",
            border_style="red",
            box=box.HEAVY_EDGE,
        ))

    console.print("[dim]Waiting 30s...[/]")
    time.sleep(30)
