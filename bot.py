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
MAX_POSITIONS = 2  # testing: $10 budget → 2 concurrent straddles at exchange-min size
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
        "[bold bright_green]██████╗ ████████╗ ██████╗[/]   [bright_yellow]//[/]  [bold white]STRADDLE DESK[/]\n"
        "[bold bright_green]██╔══██╗╚══██╔══╝██╔════╝[/]   [bright_yellow]//[/]  [dim]POLYMARKET CLOB · MATIC[/]\n"
        "[bold bright_green]██████╔╝   ██║   ██║     [/]   [bright_yellow]//[/]  [dim]GTC ENTRY · FAK FLATTEN[/]\n"
        "[bold bright_green]██╔══██╗   ██║   ██║     [/]   [bright_yellow]//[/]  [dim]Δ-NEUTRAL · HOURLY EXPIRY[/]\n"
        "[bold bright_green]██████╔╝   ██║   ╚██████╗[/]   [bright_yellow]//[/]  STATUS: [bold bright_green]● ARMED[/]\n"
        "[bold bright_green]╚═════╝    ╚═╝    ╚═════╝[/]   [bright_yellow]//[/]  [dim]v7.3 · partial-fill safe[/]",
        vertical="middle",
    ),
    title="[bold bright_yellow]▰▱▰▱  TRADING SYSTEM ONLINE  ▱▰▱▰[/]",
    subtitle="[dim]press Ctrl-C to disarm[/]",
    border_style="bright_green",
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
        console.print(f"  [dim cyan][SCAN][/] [dim]gamma returned {len(events)} events[/]")
    except Exception as e:
        console.print(f"  [bold red][SCAN FAIL][/] [dim]{e}[/]")
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
    console.print(f"  [dim cyan][SCAN][/] [dim]{len(candidates)} forward contracts in window[/]")
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


def cancel_order_safe(order_id):
    if not order_id:
        return True
    try:
        safe_api_call(client.cancel_order, OrderPayload(orderID=order_id))
        return True
    except Exception:
        return False


def matched_from_det(det, fallback_size):
    """Convert a get_order_details() return into an int matched size.
    NOT_FOUND is treated as fully filled at fallback_size (caller knows the cap)."""
    if not det:
        return 0
    if det.get("status") == "NOT_FOUND":
        return int(fallback_size)
    sm = det.get("size_matched", 0)
    return int(sm) if sm is not None else 0


def refetch_matched_after_cancel(order_id, prev_matched):
    """Re-read matched count after a cancel succeeded. Fills are frozen post-cancel.
    Returns int, or None on transient API error (caller should defer).
    NOT_FOUND post-cancel = archived ⇒ trust prev_matched (cancel killed the unfilled portion)."""
    if not order_id:
        return prev_matched
    det = get_order_details(order_id)
    if det is None:
        return None
    if det.get("status") == "NOT_FOUND":
        return prev_matched
    sm = det.get("size_matched", 0)
    new = int(sm) if sm is not None else 0
    return max(prev_matched, new)


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
        arrow = "▲" if side.upper() == "BUY" else "▼"
        console.print(f"  [bold bright_green][ORDER {arrow}][/] {side.upper():<4} {size:>3} @ {price:.3f}  [dim]id={log_id}[/]")
        return order
    except Exception as e:
        console.print(f"  [bold red][ORDER REJECT][/] {side.upper():<4} {size:>3} @ {price:.3f}  [dim]{e}[/]")
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
            console.print(f"  [dim yellow][BOOK][/] [dim]bid side empty[/]")
            break

        sell_size = remaining
        if bid_depth < remaining:
            sell_size = int(bid_depth)
            if sell_size < 1 and bid_depth > 0:
                sell_size = min(remaining, 1)
            console.print(f"  [dim yellow][DEPTH][/] [dim]bid={bid_depth:.1f} < req={remaining} · sizing to {sell_size}[/]")

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
                # Try to get matched amount directly from result first
                matched = 0
                if isinstance(result, dict):
                    matched = int(float(result.get("size_matched", 0) or result.get("makingAmount", 0) or result.get("takerAmount", 0) or 0))
                else:
                    matched = int(getattr(result, "size_matched", 0) or 0)

                # If result shows 0, verify via get_order_details (order may have been archived)
                if matched <= 0:
                    oid = extract_order_id(result)
                    details = get_order_details(oid) if oid else None
                    if details:
                        if details.get("status") == "NOT_FOUND":
                            # FAK was archived after fill — assume full fill for this chunk
                            matched = sell_size
                        else:
                            sm = details.get("size_matched", 0)
                            matched = int(sm) if sm is not None else 0

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
    console.print(f"  [bold red][EXIT FAIL][/] 0/{size} cleared")
    return 0, None


# ------------------------- REDEEM -------------------------

def redeem_positions(positions):
    if not positions:
        return
    for mid in list(positions.keys()):
        pos = positions[mid]
        if pos.get("redeemed"):
            continue
        if pos.get("redeem_submitted_at") and (time.time() * 1000 - pos["redeem_submitted_at"]) < 300_000:
            continue  # recently submitted, wait 5 min before retry
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
                console.print(f"  [dim red][REDEEM][/] [dim]nonce fetch fail HTTP {nonce_r.status_code}[/]")
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
                console.print(f"  [bold bright_green][SETTLE ▶][/] {mid[:18]}…  [dim]tx={str(tx_id)[:18]}…[/]")
                pos["redeem_submitted_at"] = time.time() * 1000
                save_json(STATE_FILE, positions)
            else:
                console.print(f"  [dim red][SETTLE FAIL][/] {mid[:18]}…  [dim]HTTP {submit_r.status_code} · {submit_r.text[:80]}[/]")
        except Exception as e:
            console.print(f"  [dim red][SETTLE ERR][/] {mid[:18]}…  [dim]{e}[/]")


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

        console.rule(
            f"[bold bright_yellow]▰ TICK #{CYCLE:04d}[/] [dim]·[/] [bright_white]{now_str}[/] [dim]·[/] "
            f"[bright_cyan]MKT[/] [bold]{len(markets):>2}[/] [dim]·[/] "
            f"[bright_green]POS[/] [bold]{len(positions):>2}[/] [dim]·[/] "
            f"[bright_magenta]PEND[/] [bold]{len(pending):>2}[/] [dim]·[/] "
            f"[bright_yellow]NAV[/] [bold]${pusd_bal:>7.2f}[/] [dim]▰[/]",
            style="bright_yellow",
        )

        # ================= MARKET TABLE =================
        if markets:
            table = Table(
                title="[bold bright_cyan]≡ ORDER BOOK SCANNER ≡[/]  [dim]BTC HOURLY · GAMMA FEED[/]",
                box=box.HEAVY_HEAD,
                border_style="bright_blue",
                title_style="bold bright_cyan",
                show_lines=True,
            )
            table.add_column("INSTRUMENT", style="white", max_width=40)
            table.add_column("EXPIRY", style="dim cyan", justify="center")
            table.add_column("TTM", justify="right")
            table.add_column("STATE", justify="center")

            for m in markets:
                try:
                    end_date = m.get("endDate") or m.get("end_date")
                    end_ts = datetime.fromisoformat(end_date.replace("Z", "+00:00")).timestamp() * 1000
                    mins = (end_ts - now_ms) / 60000
                    ends_str = datetime.fromisoformat(end_date.replace("Z", "+00:00")).strftime("%H:%M")
                    status = "[dim]-[/]"
                    if m["id"] in positions:
                        status = "[bold bright_green]● LONG[/]"
                    elif m["id"] in pending:
                        status = "[bold bright_yellow]◌ WORK[/]"

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

        # ================= CLEANUP: delete sold-out or redeemed+expired positions =================
        for mid in list(positions.keys()):
            pos = positions.get(mid)
            if not pos:
                continue
            # Remove fully liquidated positions
            yes_rem = pos.get("yes_remaining", 0)
            no_rem = pos.get("no_remaining", 0)
            if yes_rem == 0 and no_rem == 0 and not pos.get("redeemed") and not pos.get("redeem_submitted_at"):
                console.print(f"  [dim cyan][BOOK CLOSE][/] {mid[:18]}…  [dim]inventory cleared[/]")
                del positions[mid]
                save_json(STATE_FILE, positions)
                continue
            # Remove redeemed+expired positions (with grace period for on-chain settlement)
            if pos.get("redeemed") and pos.get("end_ts") and now_ms > pos["end_ts"] + 300000:
                console.print(f"  [dim cyan][BOOK CLOSE][/] {mid[:18]}…  [dim]settled · expired[/]")
                del positions[mid]
                save_json(STATE_FILE, positions)
                continue
            # Remove positions where redemption was submitted long ago
            if pos.get("redeem_submitted_at") and pos.get("end_ts") and now_ms > pos["end_ts"] + 3600_000:
                console.print(f"  [dim cyan][BOOK CLOSE][/] {mid[:18]}…  [dim]settle window elapsed[/]")
                del positions[mid]
                save_json(STATE_FILE, positions)

        # ================= PROCESS PENDING GTC ORDERS =================
        for mid in list(pending.keys()):
            p = pending[mid]
            age = now_ms - p.get("placed_at", 0)
            if age < 120_000:
                console.print(f"  [dim magenta][WORKING][/] {mid[:18]}…  [dim]age {age//1000}s · resting[/]")
                continue

            # Crash recovery: if already in positions, just drop stale pending
            if mid in positions:
                del pending[mid]
                save_json(PENDING_FILE, pending)
                continue

            # Use get_order_details for actual size_matched, not just status
            yes_oid = p.get("yes_order_id")
            no_oid = p.get("no_order_id")
            yes_det = get_order_details(yes_oid) if yes_oid else None
            no_det = get_order_details(no_oid) if no_oid else None

            # If either real order ID failed transiently, defer to next cycle
            yes_err = yes_oid and yes_det is None
            no_err = no_oid and no_det is None
            if yes_err or no_err:
                console.print(f"  [dim yellow][API DEFER][/] {mid[:18]}…  [dim]order detail unavail · retry[/]")
                continue

            yes_matched = matched_from_det(yes_det, p["yes_size"])
            no_matched = matched_from_det(no_det, p["no_size"])

            yes_status = yes_det.get("status", "UNKNOWN") if yes_det else "UNKNOWN"
            no_status = no_det.get("status", "UNKNOWN") if no_det else "UNKNOWN"

            console.print(f"  [dim magenta][WORKING][/] {mid[:18]}…  [bright_green]UP[/]:{yes_status}/{yes_matched}  [bright_red]DN[/]:{no_status}/{no_matched}")

            both_filled = (yes_matched >= p["yes_size"]) and (no_matched >= p["no_size"])
            timed_out = now_ms > p.get("placed_at", 0) + PENDING_TIMEOUT_MS

            # Promote without cancellation if both already at full size
            if both_filled:
                positions[mid] = {
                    "yes_size": yes_matched,
                    "no_size": no_matched,
                    "yes_remaining": yes_matched,
                    "no_remaining": no_matched,
                    "yes_token": p["yes_token"],
                    "no_token": p["no_token"],
                    "end_ts": p["end_ts"],
                    "question": p.get("question", ""),
                    "entered_at": now_ms,
                }
                save_json(STATE_FILE, positions)
                del pending[mid]
                save_json(PENDING_FILE, pending)
                console.print("  [bold bright_green]■ STRADDLE LIVE ■[/] [dim]both legs filled[/]")
                continue

            # Nothing filled and not yet timed out — keep waiting
            if yes_matched == 0 and no_matched == 0 and not timed_out:
                continue

            # Partial fill (one or both sides) OR timeout: cancel BOTH first to freeze fills,
            # then re-read matched amounts so the flatten/promote uses the true final values.
            if not cancel_order_safe(p["yes_order_id"]):
                console.print("  [bold red][KILL FAIL][/] UP leg · retry next tick")
                continue
            if not cancel_order_safe(p["no_order_id"]):
                console.print("  [bold red][KILL FAIL][/] DN leg · retry next tick")
                continue

            yes_matched_post = refetch_matched_after_cancel(p["yes_order_id"], yes_matched)
            no_matched_post = refetch_matched_after_cancel(p["no_order_id"], no_matched)
            if yes_matched_post is None or no_matched_post is None:
                console.print(f"  [dim yellow][API DEFER][/] {mid[:18]}…  [dim]post-kill refetch failed · retry[/]")
                continue
            yes_matched = yes_matched_post
            no_matched = no_matched_post

            # Re-classify based on post-cancel (final) matched counts
            both_full_post = yes_matched >= p["yes_size"] and no_matched >= p["no_size"]
            yes_has = yes_matched > 0
            no_has = no_matched > 0

            if both_full_post:
                positions[mid] = {
                    "yes_size": yes_matched,
                    "no_size": no_matched,
                    "yes_remaining": yes_matched,
                    "no_remaining": no_matched,
                    "yes_token": p["yes_token"],
                    "no_token": p["no_token"],
                    "end_ts": p["end_ts"],
                    "question": p.get("question", ""),
                    "entered_at": now_ms,
                }
                save_json(STATE_FILE, positions)
                del pending[mid]
                save_json(PENDING_FILE, pending)
                console.print("  [bold bright_green]■ STRADDLE LIVE ■[/] [dim]both legs filled post-kill[/]")
                continue

            if yes_has and no_has:
                # Both partial — promote with matched sizes; no flattening
                positions[mid] = {
                    "yes_size": yes_matched,
                    "no_size": no_matched,
                    "yes_remaining": yes_matched,
                    "no_remaining": no_matched,
                    "yes_token": p["yes_token"],
                    "no_token": p["no_token"],
                    "end_ts": p["end_ts"],
                    "question": p.get("question", ""),
                    "entered_at": now_ms,
                }
                save_json(STATE_FILE, positions)
                del pending[mid]
                save_json(PENDING_FILE, pending)
                console.print("  [bold bright_green]◐ STRADDLE LIVE ◐[/] [dim]both legs partial · sized to fills[/]")
                continue

            if yes_has:
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
                        "entered_at": now_ms,
                    }
                    save_json(STATE_FILE, positions)
                del pending[mid]
                save_json(PENDING_FILE, pending)
                console.print("  [bold bright_yellow]▲ ASYMMETRIC FILL[/] [dim]UP filled · flattened to flat[/]")
                continue

            if no_has:
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
                        "entered_at": now_ms,
                    }
                    save_json(STATE_FILE, positions)
                del pending[mid]
                save_json(PENDING_FILE, pending)
                console.print("  [bold bright_yellow]▼ ASYMMETRIC FILL[/] [dim]DN filled · flattened to flat[/]")
                continue

            # Timeout with zero fills on both sides — just clean up
            console.print(f"  [dim magenta][WORK EXPIRE][/] [dim]no fills · cleaned[/]")
            del pending[mid]
            save_json(PENDING_FILE, pending)

        # ================= SELL PHASE (iterates positions directly) =================
        for mid in list(positions.keys()):
            pos = positions[mid]
            end_ts = pos.get("end_ts", 0)
            minutes_left = (end_ts - now_ms) / 60000

            if minutes_left <= 0:
                continue

            # Grace: don't sell positions just inserted in this cycle (give book a moment)
            entered_at = pos.get("entered_at", 0)
            if entered_at and now_ms - entered_at < 30_000:
                continue

            yes_token = pos["yes_token"]
            no_token = pos["no_token"]
            yes_bid, _ = get_book_bid(yes_token)
            no_bid, _ = get_book_bid(no_token)
            if yes_bid is None or no_bid is None:
                continue

            yes_rem = pos.get("yes_remaining", 0)
            no_rem = pos.get("no_remaining", 0)

            sell_yes = yes_rem > 0 and (yes_bid <= 0.04 or (minutes_left <= 20 and yes_bid < 0.05))
            sell_no = no_rem > 0 and (no_bid <= 0.04 or (minutes_left <= 20 and no_bid < 0.05))

            if sell_yes or sell_no:
                q = pos.get("question", "Unknown")
                ttm_color = "bold red" if minutes_left <= 20 else "bright_yellow"
                sell_panel = Panel(
                    f"  [bright_white]{q}[/]\n"
                    f"  [bright_green]UP[/]   bid [bold]{yes_bid:.3f}[/]  inv [bold]{yes_rem:>3}[/]   \u2502   "
                    f"[bright_red]DN[/]  bid [bold]{no_bid:.3f}[/]  inv [bold]{no_rem:>3}[/]   \u2502   "
                    f"[{ttm_color}]TTM {minutes_left:>4.1f}m[/]",
                    title="[bold bright_yellow]\u25bc EXIT TRIGGER \u2014 RISK-OFF[/]",
                    border_style="bright_yellow",
                    box=box.HEAVY,
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
            console.print(f"  [dim yellow][BOOK FULL][/] [dim]exposure {active_count}/{MAX_POSITIONS} \u00b7 no new entries[/]")
        else:
            available_bal = pusd_bal
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

                joint = yes_ask + no_ask
                edge_bps = int((1.0 - joint) * 10000) if joint > 0 else 0
                edge_color = "bright_green" if edge_bps > 0 else "bright_red"
                buy_panel = Panel(
                    f"  [bright_white]{market['question']}[/]\n"
                    f"  [bright_green]UP[/]   ask [bold]{yes_ask:.3f}[/]  depth [bold]{yes_depth:>5.1f}[/]   \u2502   "
                    f"[bright_red]DN[/]  ask [bold]{no_ask:.3f}[/]  depth [bold]{no_depth:>5.1f}[/]\n"
                    f"  [dim]joint[/] [bold]{joint:.3f}[/]   [dim]edge[/] [{edge_color}]{edge_bps:+d} bps[/]   [dim]horizon[/] [bright_cyan]{minutes_ahead:>5.1f}m[/]",
                    title="[bold bright_green]\u25b2 ENTRY SCAN \u2014 STRADDLE QUOTE[/]",
                    border_style="bright_green",
                    box=box.HEAVY,
                )
                console.print(buy_panel)

                # Enter if sides are within 1c of each other AND joint <= 1.02
                # (4c loser-scrape covers up to 2c of joint slippage)
                if abs(yes_ask - no_ask) <= 0.01 and (yes_ask + no_ask) <= 1.02:
                    size = int(market.get("orderMinSize", 5))
                    total_cost = size * (yes_ask + no_ask)
                    MAX_STRADDLE_COST = 5.50
                    if total_cost > MAX_STRADDLE_COST:
                        console.print(f"  [dim yellow][COST CAP][/] [dim]${total_cost:.2f} > ${MAX_STRADDLE_COST:.2f} ceiling · skip[/]")
                        continue
                    if total_cost > available_bal:
                        console.print(f"  [dim yellow][MARGIN CAP][/] [dim]need ${total_cost:.2f} · NAV ${available_bal:.2f} · skip[/]")
                        continue

                    tick_size = str(market.get("orderPriceMinTickSize", "0.01"))
                    notional = size * (yes_ask + no_ask)
                    console.print(Panel(
                        f"  [bold bright_white]{market['question']}[/]\n"
                        f"  [bright_yellow]\u25cf SIZE[/] [bold]{size}[/]  \u2502  "
                        f"[bright_green]UP[/] @ [bold]{yes_ask:.3f}[/]  +  [bright_red]DN[/] @ [bold]{no_ask:.3f}[/]  \u2502  "
                        f"[dim]notional[/] [bold]${notional:.2f}[/]",
                        title="[bold bright_yellow]\u2261\u2261  POSITION OPEN  \u2261\u2261[/]",
                        subtitle="[dim]GTC \u00b7 both legs \u00b7 delta-neutral[/]",
                        border_style="bright_yellow",
                        box=box.HEAVY_EDGE,
                    ))

                    # Place YES first, then check if it filled before placing NO
                    yes_order = place_order(yes_token, yes_ask, size, BUY, OrderType.GTC, tick_size)
                    if not yes_order:
                        console.print("  [dim red][LEG FAIL][/] [dim]UP rejected \u00b7 abort entry[/]")
                        continue

                    yes_oid = extract_order_id(yes_order)
                    # Write preliminary pending immediately to prevent YES orphan on crash
                    pending[mid] = {
                        "yes_order_id": yes_oid,
                        "no_order_id": None,
                        "yes_token": yes_token,
                        "no_token": no_token,
                        "yes_size": size,
                        "no_size": size,
                        "placed_at": now_ms,
                        "end_ts": end_ts,
                        "question": market.get("question", ""),
                    }
                    save_json(PENDING_FILE, pending)

                    time.sleep(0.5)
                    yes_det = get_order_details(yes_oid)
                    yes_matched = 0
                    if yes_det:
                        if yes_det.get("status") == "NOT_FOUND":
                            yes_matched = size
                        else:
                            sm = yes_det.get("size_matched", 0)
                            yes_matched = int(sm) if sm is not None else 0

                    if yes_matched >= size:
                        # YES fully filled before NO placed — flatten immediately
                        # No cancel needed: yes_matched >= size means the GTC is exhausted/archived
                        console.print("  [bold bright_yellow][RACE FILL][/] [dim]UP filled pre-DN \u00b7 emergency flatten[/]")
                        sold, _ = sell_with_retry(yes_token, yes_matched)
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
                                "entered_at": now_ms,
                            }
                            save_json(STATE_FILE, positions)
                        del pending[mid]
                        save_json(PENDING_FILE, pending)
                        continue

                    no_order = place_order(no_token, no_ask, size, BUY, OrderType.GTC, tick_size)
                    if no_order:
                        pending[mid]["no_order_id"] = extract_order_id(no_order)
                        save_json(PENDING_FILE, pending)
                        available_bal -= size * (yes_ask + no_ask)
                    else:
                        # NO placement failed — check YES status, flatten if needed
                        yes_det2 = get_order_details(yes_oid)
                        yes_m2 = 0
                        if yes_det2:
                            if yes_det2.get("status") == "NOT_FOUND":
                                yes_m2 = size
                            else:
                                sm = yes_det2.get("size_matched", 0)
                                yes_m2 = int(sm) if sm is not None else 0
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
                                    "entered_at": now_ms,
                                }
                                save_json(STATE_FILE, positions)
                        if cancel_order_safe(yes_oid):
                            del pending[mid]
                            save_json(PENDING_FILE, pending)
                        else:
                            console.print("  [bold red][KILL FAIL][/] [dim]UP leg \u00b7 holding in pending[/]")

                    time.sleep(2)

    except Exception:
        console.print(Panel(
            traceback.format_exc(),
            title="[bold bright_red]\u25a0\u25a0  SYSTEM FAULT  \u25a0\u25a0[/]",
            subtitle="[dim]auto-restart in 30s \u00b7 cycle aborted[/]",
            border_style="bright_red",
            box=box.HEAVY_EDGE,
        ))

    console.print("[dim bright_black]\u00b7 \u00b7 \u00b7  sleeping 30s  \u00b7 \u00b7 \u00b7[/]")
    time.sleep(30)
