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
        "[bold white]Polymarket BTC Straddle Bot v7[/]\n"
        "[dim]Bug-fixed: GTC+FAK with partial-fill tracking[/]",
        vertical="middle",
    ),
    title="[bold yellow]v7[/]",
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
    time.sleep(0.3)
    try:
        return func(*args, **kwargs)
    except Exception as e:
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


# ------------------------- MARKET FETCH -------------------------

def get_active_btc_hourly_markets():
    now = time.time() * 1000
    try:
        res = requests.get(
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
    try:
        book = safe_api_call(client.get_order_book, token_id)
        asks = book.get("asks", [])
        if not asks:
            return None, 0.0
        best = min(asks, key=lambda x: float(x.get("price", 999)))
        return float(best.get("price", 0)), float(best.get("size", 0))
    except Exception:
        return None, 0.0


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


# ------------------------- ORDER HELPERS -------------------------

def extract_order_id(order_obj):
    if isinstance(order_obj, dict):
        return order_obj.get("orderID") or order_obj.get("id")
    return getattr(order_obj, "orderID", None) or getattr(order_obj, "id", None) or str(order_obj)


def get_order_details(order_id):
    """Return dict with size_matched and status, or None."""
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
    except Exception:
        return None


def is_filled(status):
    return status in ("FILLED", "MATCHED", "DONE", "FULLY_FILLED")


def cancel_order_safe(order_id):
    try:
        safe_api_call(client.cancel_order, OrderPayload(orderID=order_id))
        return True
    except Exception:
        return False


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
        log_id = (oid[:16] + "...") if oid and len(str(oid)) > 16 else str(oid)
        console.print(f"  [bold green]PLACED[/] {side.upper()} {size} @ {price:.3f}  id={log_id}")
        return order
    except Exception as e:
        console.print(f"  [bold red]PLACE FAILED[/] {side.upper()} {size} @ {price:.3f}: {e}")
        return None


def sell_with_retry(token_id, size, tick_size="0.01", max_retries=3):
    """FAK sell at best bid. Returns (sold_size, result) or (0, None)."""
    total_sold = 0
    for attempt in range(max_retries):
        remaining = size - total_sold
        if remaining < 1:
            break

        bid_price, bid_depth = get_book_bid(token_id)
        if bid_price is None:
            console.print(f"  [dim]No bids available[/]")
            break

        sell_size = remaining
        if bid_depth < remaining:
            sell_size = int(bid_depth)
            console.print(f"  [dim]Bid depth {bid_depth:.1f} < {remaining}, selling {sell_size}[/]")

        if sell_size < 1:
            break

        try:
            neg_risk = safe_api_call(client.get_neg_risk, token_id)
            result = safe_api_call(
                client.create_and_post_order,
                OrderArgs(token_id=token_id, price=bid_price, size=sell_size, side=SELL),
                options=PartialCreateOrderOptions(tick_size=tick_size, neg_risk=neg_risk),
                order_type=OrderType.FAK,
            )
            if result:
                # Verify actual matched amount
                oid = extract_order_id(result)
                details = get_order_details(oid) if oid else None
                matched = 0
                if details:
                    matched = int(details.get("size_matched", 0))
                if matched <= 0:
                    # API returned order obj but nothing filled — retry
                    console.print(f"  [dim]FAK returned but 0 matched, retrying[/]")
                    time.sleep(1)
                    continue

                total_sold += matched
                log_id = (oid[:16] + "...") if oid and len(str(oid)) > 16 else str(oid)
                console.print(f"  [bold green]SOLD[/] {matched} @ {bid_price:.3f}  id={log_id}")

                if total_sold >= size:
                    return total_sold, result
        except Exception as e:
            console.print(f"  [dim red]Sell attempt {attempt+1}/{max_retries} failed: {e}[/]")

        time.sleep(1)

    if total_sold > 0:
        return total_sold, {"partial": True, "sold": total_sold}
    console.print(f"  [bold red]SELL FAILED[/] 0/{size} sold")
    return 0, None


# ------------------------- REDEEM -------------------------

def redeem_positions(positions):
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
            if nonce_r.status_code != 200:
                console.print(f"  [dim red]Redeem nonce fetch failed: {nonce_r.status_code}[/]")
                continue
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
                tx_id = submit_r.json().get("transactionID") or "?"
                console.print(f"  [dim green]Redeem submitted for {mid[:20]}... tx={str(tx_id)[:20]}[/]")
                pos["redeemed"] = True
                save_json(STATE_FILE, positions)
            else:
                console.print(f"  [dim red]Redeem failed for {mid}: {submit_r.status_code} {submit_r.text[:100]}[/]")
        except Exception as e:
            console.print(f"  [dim red]Redeem error for {mid}: {e}[/]")


# ------------------------- MAIN LOOP -------------------------
positions = load_json(STATE_FILE)
pending = load_json(PENDING_FILE)
CYCLE = 0

while True:
    try:
        CYCLE += 1
        now_ms = time.time() * 1000
        now_str = datetime.now().strftime("%H:%M:%S")

        pusd_bal = get_balance()

        markets = get_active_btc_hourly_markets()
        market_by_id = {m["id"]: m for m in markets}

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

        # ================= REDEEM BEFORE CLEANUP =================
        redeem_positions(positions)

        # ================= CLEANUP: only delete redeemed positions =================
        for mid in list(positions.keys()):
            pos = positions.get(mid)
            if pos and pos.get("redeemed") and pos.get("end_ts") and now_ms > pos["end_ts"] + 300000:
                console.print(f"  [dim]Redeemed+expired market {mid[:20]}... removed[/]")
                del positions[mid]
                save_json(STATE_FILE, positions)

        # ================= PROCESS PENDING GTC ORDERS =================
        for mid in list(pending.keys()):
            p = pending[mid]
            age = now_ms - p.get("placed_at", 0)
            if age < 120_000:
                console.print(f"  [dim]Pending {mid[:20]}... age={age//1000}s, letting sit[/]")
                continue

            # Use get_order_details for actual size_matched, not just status
            yes_det = get_order_details(p["yes_order_id"])
            no_det = get_order_details(p["no_order_id"])

            yes_matched = int(yes_det.get("size_matched", 0)) if yes_det else 0
            no_matched = int(no_det.get("size_matched", 0)) if no_det else 0
            yes_status = yes_det.get("status", "UNKNOWN") if yes_det else "UNKNOWN"
            no_status = no_det.get("status", "UNKNOWN") if no_det else "UNKNOWN"

            console.print(f"  [dim]Pending {mid[:20]}... YES={yes_status} matched={yes_matched}  NO={no_status} matched={no_matched}[/]")

            both_filled = is_filled(yes_status) and is_filled(no_status)
            yes_has_fill = yes_matched > 0
            no_has_fill = no_matched > 0
            timed_out = now_ms > p.get("placed_at", 0) + PENDING_TIMEOUT_MS

            if both_filled:
                # Full straddle
                positions[mid] = {
                    "yes_size": yes_matched,
                    "no_size": no_matched,
                    "yes_remaining": yes_matched,
                    "no_remaining": no_matched,
                    "yes_token": p["yes_token"],
                    "no_token": p["no_token"],
                    "end_ts": p["end_ts"],
                    "question": p.get("question", ""),
                }
                save_json(STATE_FILE, positions)
                del pending[mid]
                save_json(PENDING_FILE, pending)
                console.print("  [bold green]FULL STRADDLE ENTERED[/]")

            elif yes_has_fill and not no_has_fill:
                cancel_order_safe(p["no_order_id"])
                if yes_matched >= p["yes_size"]:
                    # Fully filled on YES, flatten all
                    sold, _ = sell_with_retry(p["yes_token"], yes_matched)
                    if sold < yes_matched:
                        # Some unsold — track remainder
                        positions[mid] = {
                            "yes_size": yes_matched,
                            "no_size": 0,
                            "yes_remaining": yes_matched - sold,
                            "no_remaining": 0,
                            "yes_token": p["yes_token"],
                            "no_token": p["no_token"],
                            "end_ts": p["end_ts"],
                            "question": p.get("question", ""),
                        }
                        save_json(STATE_FILE, positions)
                else:
                    # Partial fill on YES
                    sold, _ = sell_with_retry(p["yes_token"], yes_matched)
                    if sold < yes_matched:
                        positions[mid] = {
                            "yes_size": yes_matched,
                            "no_size": 0,
                            "yes_remaining": yes_matched - sold,
                            "no_remaining": 0,
                            "yes_token": p["yes_token"],
                            "no_token": p["no_token"],
                            "end_ts": p["end_ts"],
                            "question": p.get("question", ""),
                        }
                        save_json(STATE_FILE, positions)
                del pending[mid]
                save_json(PENDING_FILE, pending)
                console.print("  [yellow]Partial - flattened YES[/]")

            elif no_has_fill and not yes_has_fill:
                cancel_order_safe(p["yes_order_id"])
                if no_matched >= p["no_size"]:
                    sold, _ = sell_with_retry(p["no_token"], no_matched)
                    if sold < no_matched:
                        positions[mid] = {
                            "yes_size": 0,
                            "no_size": no_matched,
                            "yes_remaining": 0,
                            "no_remaining": no_matched - sold,
                            "yes_token": p["yes_token"],
                            "no_token": p["no_token"],
                            "end_ts": p["end_ts"],
                            "question": p.get("question", ""),
                        }
                        save_json(STATE_FILE, positions)
                else:
                    sold, _ = sell_with_retry(p["no_token"], no_matched)
                    if sold < no_matched:
                        positions[mid] = {
                            "yes_size": 0,
                            "no_size": no_matched,
                            "yes_remaining": 0,
                            "no_remaining": no_matched - sold,
                            "yes_token": p["yes_token"],
                            "no_token": p["no_token"],
                            "end_ts": p["end_ts"],
                            "question": p.get("question", ""),
                        }
                        save_json(STATE_FILE, positions)
                del pending[mid]
                save_json(PENDING_FILE, pending)
                console.print("  [yellow]Partial - flattened NO[/]")

            elif timed_out:
                console.print(f"  [dim]Pending timeout - cancelling[/]")
                cancel_order_safe(p["yes_order_id"])
                cancel_order_safe(p["no_order_id"])
                # If anything filled during the timeout window, flatten it
                rem_yes = 0
                rem_no = 0
                if yes_matched > 0:
                    sold_y, _ = sell_with_retry(p["yes_token"], yes_matched)
                    rem_yes = yes_matched - sold_y
                if no_matched > 0:
                    sold_n, _ = sell_with_retry(p["no_token"], no_matched)
                    rem_no = no_matched - sold_n
                if rem_yes > 0 or rem_no > 0:
                    positions[mid] = {
                        "yes_size": yes_matched,
                        "no_size": no_matched,
                        "yes_remaining": rem_yes,
                        "no_remaining": rem_no,
                        "yes_token": p["yes_token"],
                        "no_token": p["no_token"],
                        "end_ts": p["end_ts"],
                        "question": p.get("question", ""),
                    }
                    save_json(STATE_FILE, positions)
                del pending[mid]
                save_json(PENDING_FILE, pending)

        # ================= SELL PHASE (iterates positions directly) =================
        for mid in list(positions.keys()):
            pos = positions[mid]
            end_ts = pos.get("end_ts", 0)
            minutes_left = (end_ts - now_ms) / 60000

            if minutes_left <= 0:
                continue

            yes_token = pos["yes_token"]
            no_token = pos["no_token"]
            yes_bid, _ = get_book_bid(yes_token)
            no_bid, _ = get_book_bid(no_token)
            if yes_bid is None or no_bid is None:
                continue

            yes_rem = pos.get("yes_remaining", 0)
            no_rem = pos.get("no_remaining", 0)

            sell_yes = yes_rem > 0 and (yes_bid <= 0.02 or (minutes_left <= 20 and yes_bid < 0.03))
            sell_no = no_rem > 0 and (no_bid <= 0.02 or (minutes_left <= 20 and no_bid < 0.03))

            if sell_yes or sell_no:
                q = pos.get("question", "Unknown")
                sell_panel = Panel(
                    f"  [white]{q}[/]\n"
                    f"  UP bid={yes_bid:.3f} (rem={yes_rem})  "
                    f"DOWN bid={no_bid:.3f} (rem={no_rem})  "
                    f"[yellow]{minutes_left:.1f}m left[/]",
                    title="[bold yellow]SELL CHECK[/]",
                    border_style="yellow",
                    box=box.ROUNDED,
                )
                console.print(sell_panel)

            if sell_yes:
                sold, _ = sell_with_retry(yes_token, yes_rem)
                if sold > 0:
                    pos["yes_remaining"] = max(0, yes_rem - sold)
                    save_json(STATE_FILE, positions)

            if sell_no:
                sold, _ = sell_with_retry(no_token, no_rem)
                if sold > 0:
                    pos["no_remaining"] = max(0, no_rem - sold)
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

                if round(yes_ask, 3) == round(no_ask, 3) and yes_ask <= 0.52:
                    min_size = int(market.get("orderMinSize", 1))
                    target_dollars = 1.0
                    size = max(1, round(target_dollars / yes_ask))
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

                    # Place YES first, then check if it filled before placing NO
                    yes_order = place_order(yes_token, yes_ask, size, BUY, OrderType.GTC, tick_size)
                    if yes_order:
                        time.sleep(0.5)
                        yes_oid = extract_order_id(yes_order)
                        yes_det = get_order_details(yes_oid)
                        yes_matched = int(yes_det.get("size_matched", 0)) if yes_det else 0

                        if yes_matched >= size:
                            # YES fully filled before NO placed — flatten immediately
                            console.print("  [yellow]YES filled before NO placed — flattening[/]")
                            sold, _ = sell_with_retry(yes_token, yes_matched)
                            cancel_order_safe(yes_oid)
                            if sold < yes_matched:
                                positions[mid] = {
                                    "yes_size": yes_matched,
                                    "no_size": 0,
                                    "yes_remaining": yes_matched - sold,
                                    "no_remaining": 0,
                                    "yes_token": yes_token,
                                    "no_token": no_token,
                                    "end_ts": end_ts,
                                    "question": market.get("question", ""),
                                }
                                save_json(STATE_FILE, positions)
                            continue

                        no_order = place_order(no_token, no_ask, size, BUY, OrderType.GTC, tick_size)
                        if no_order:
                            pending[mid] = {
                                "yes_order_id": yes_oid,
                                "no_order_id": extract_order_id(no_order),
                                "yes_token": yes_token,
                                "no_token": no_token,
                                "yes_size": size,
                                "no_size": size,
                                "placed_at": now_ms,
                                "end_ts": end_ts,
                                "question": market.get("question", ""),
                            }
                            save_json(PENDING_FILE, pending)
                        else:
                            # NO placement failed — check YES status, flatten if needed
                            yes_det2 = get_order_details(yes_oid)
                            yes_m2 = int(yes_det2.get("size_matched", 0)) if yes_det2 else 0
                            if yes_m2 > 0:
                                sold, _ = sell_with_retry(yes_token, yes_m2)
                                if sold < yes_m2:
                                    positions[mid] = {
                                        "yes_size": yes_m2,
                                        "no_size": 0,
                                        "yes_remaining": yes_m2 - sold,
                                        "no_remaining": 0,
                                        "yes_token": yes_token,
                                        "no_token": no_token,
                                        "end_ts": end_ts,
                                        "question": market.get("question", ""),
                                    }
                                    save_json(STATE_FILE, positions)
                            cancel_order_safe(yes_oid)
                    else:
                        console.print("  [dim]YES placement failed, skipping[/]")

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
