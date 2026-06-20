"""
Paper-trade monitor for BTC hourly markets.

Runs alongside the main bot, tracking what the strategy *would* do on
every hourly market — whether or not we actually hold a position. Logs
hypothetical P&L to paper_trades.log for backtesting analysis.
"""

import json
import os
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import requests

GAMMA_API = "https://gamma-api.polymarket.com"
CLOB_API = "https://clob.polymarket.com"
PAPER_STATE_FILE = "paper_state.json"
PAPER_LOG_FILE = "paper_trades.log"

_ET = ZoneInfo("America/New_York")

# Match the real bot's threshold
PAPER_THRESHOLD = float(os.getenv("SELL_THRESHOLD", "0.08"))
# Assumed cost per side when entering a position (both sides cost ~$1 total)
ENTRY_COST = 1.00


def _load_state():
    if os.path.exists(PAPER_STATE_FILE):
        with open(PAPER_STATE_FILE, "r") as f:
            return json.load(f)
    return {"markets": {}, "stats": {"wins": 0, "losses": 0, "total_pnl": 0.0}}


def _save_state(state):
    tmp = PAPER_STATE_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(state, f, indent=2)
    os.replace(tmp, PAPER_STATE_FILE)


def _log_paper(event, **kwargs):
    entry = {"ts": datetime.now().isoformat(), "event": event}
    entry.update(kwargs)
    with open(PAPER_LOG_FILE, "a") as f:
        f.write(json.dumps(entry) + "\n")


def fetch_active_btc_hourly_markets():
    """Fetch active BTC hourly markets from Polymarket gamma-api."""
    try:
        res = requests.get(
            f"{GAMMA_API}/markets",
            params={
                "closed": "false",
                "limit": 50,
            },
            timeout=10,
        )
        res.raise_for_status()
        markets = res.json() or []
        # Filter to BTC hourly markets
        btc_markets = []
        for m in markets:
            question = (m.get("question") or "").lower()
            slug = (m.get("slug") or "").lower()
            group_slug = (m.get("groupItemTitle") or m.get("eventSlug") or "").lower()
            if any(
                kw in question or kw in slug or kw in group_slug
                for kw in ("bitcoin up or down", "btc up or down", "bitcoin-up-or-down", "btc-updown")
            ):
                btc_markets.append(m)
        return btc_markets
    except Exception:
        return []


def _get_book_bid(token_id):
    """Get best bid for a token from CLOB."""
    try:
        res = requests.get(
            f"{CLOB_API}/book",
            params={"token_id": token_id},
            timeout=5,
        )
        if res.status_code != 200:
            return None
        data = res.json()
        bids = data.get("bids", [])
        if not bids:
            return None
        best = max(bids, key=lambda x: float(x.get("price", 0)))
        return float(best.get("price", 0))
    except Exception:
        return None


def _get_midpoint(token_id):
    """Get midpoint price for a token."""
    try:
        res = requests.get(
            f"{CLOB_API}/midpoint",
            params={"token_id": token_id},
            timeout=5,
        )
        if res.status_code != 200:
            return None
        data = res.json()
        mid = data.get("mid")
        if mid is not None:
            return float(mid)
        return None
    except Exception:
        return None


def _get_price(token_id):
    """Get the effective price (bid preferred, fallback to midpoint)."""
    bid = _get_book_bid(token_id)
    if bid is not None:
        return bid
    return _get_midpoint(token_id)


def run_paper_cycle(console=None):
    """Run one paper-monitoring cycle. Called from the main bot loop.
    
    Returns a brief status string for display, or None if nothing notable.
    """
    state = _load_state()
    markets = fetch_active_btc_hourly_markets()
    now = datetime.now()
    now_ts = time.time()
    notable = []

    for m in markets:
        condition_id = m.get("conditionId") or m.get("condition_id")
        if not condition_id:
            continue

        # Get token IDs for both outcomes
        tokens = m.get("tokens") or []
        if not tokens or len(tokens) < 2:
            # Try to get from clobTokenIds
            clob_ids = m.get("clobTokenIds")
            if clob_ids and len(clob_ids) >= 2:
                tokens = [
                    {"token_id": clob_ids[0], "outcome": "Yes"},
                    {"token_id": clob_ids[1], "outcome": "No"},
                ]
            else:
                continue

        up_token = None
        dn_token = None
        for t in tokens:
            outcome = (t.get("outcome") or "").lower()
            token_id = t.get("token_id") or t.get("tokenId")
            if outcome in ("yes", "up"):
                up_token = token_id
            elif outcome in ("no", "down"):
                dn_token = token_id

        if not up_token or not dn_token:
            continue

        # Parse end time
        end_date_str = m.get("endDate") or m.get("end_date_iso")
        if not end_date_str:
            continue
        try:
            end_dt = datetime.fromisoformat(end_date_str.replace("Z", "+00:00"))
            end_ts = end_dt.timestamp()
        except Exception:
            continue

        # Skip markets that already ended
        if end_ts < now_ts:
            # Check if we have a pending paper trade to resolve
            if condition_id in state["markets"]:
                entry = state["markets"][condition_id]
                if entry.get("status") == "pending_sell":
                    # Market ended — we had a trigger but couldn't verify outcome yet
                    # Mark as needing resolution
                    entry["status"] = "awaiting_resolution"
                    _save_state(state)
            continue

        # Initialize tracking for new markets
        if condition_id not in state["markets"]:
            state["markets"][condition_id] = {
                "question": m.get("question") or "BTC Hourly",
                "condition_id": condition_id,
                "up_token": up_token,
                "dn_token": dn_token,
                "end_ts": end_ts,
                "first_seen": now.isoformat(),
                "status": "monitoring",  # monitoring → triggered → resolved
                "sell_triggered": False,
                "sell_leg": None,
                "sell_price": None,
                "sell_time": None,
            }

        entry = state["markets"][condition_id]

        # Skip if already triggered or resolved
        if entry.get("sell_triggered"):
            continue

        # Check prices
        up_price = _get_price(up_token)
        dn_price = _get_price(dn_token)

        # Check if either leg hits threshold
        sell_leg = None
        sell_price = None

        if up_price is not None and up_price <= PAPER_THRESHOLD:
            sell_leg = "up"
            sell_price = up_price
        if dn_price is not None and dn_price <= PAPER_THRESHOLD:
            if sell_leg is None or (sell_price is not None and dn_price < sell_price):
                sell_leg = "down"
                sell_price = dn_price

        if sell_leg:
            entry["sell_triggered"] = True
            entry["sell_leg"] = sell_leg
            entry["sell_price"] = sell_price
            entry["sell_time"] = now.isoformat()
            entry["status"] = "pending_sell"
            _log_paper(
                "paper_sell",
                condition_id=condition_id,
                question=entry["question"],
                leg=sell_leg,
                price=sell_price,
                ttm_min=round((end_ts - now_ts) / 60, 1),
            )
            notable.append(f"PAPER SELL {sell_leg.upper()} @ {sell_price:.2f}¢ on {entry['question']}")

    # Resolve completed markets
    _resolve_completed(state)

    _save_state(state)

    if notable and console:
        for n in notable:
            console.print(f"  [dim cyan][PAPER] {n}[/]")

    return notable[0] if notable else None


def _resolve_completed(state):
    """Check for markets that have ended and resolve their paper P&L."""
    now_ts = time.time()
    to_remove = []

    for cond_id, entry in state["markets"].items():
        end_ts = entry.get("end_ts", 0)
        if end_ts > now_ts:
            continue  # Still active

        if entry.get("status") == "resolved":
            # Already resolved, mark for cleanup if old (>1h)
            if now_ts - end_ts > 3600:
                to_remove.append(cond_id)
            continue

        if not entry.get("sell_triggered"):
            # Market ended without any leg hitting threshold — no trade
            _log_paper(
                "paper_no_trade",
                condition_id=cond_id,
                question=entry.get("question"),
                reason="no_leg_hit_threshold",
            )
            entry["status"] = "resolved"
            entry["pnl"] = 0.0
            if now_ts - end_ts > 3600:
                to_remove.append(cond_id)
            continue

        # We have a triggered sell — determine outcome
        # The sell leg is what we sold (assumed loser). The other leg is held to maturity.
        # If our sell was correct (we sold the actual loser):
        #   P&L = $1.00 (winner payout) + sell_price (loser sale) - $1.00 (entry cost)
        #       = sell_price (net profit per $1 invested)
        # If our sell was WRONG (we sold the actual winner):
        #   P&L = sell_price (from selling winner) + $0 (loser held) - $1.00 (entry cost)
        #       = sell_price - $1.00 (net loss)

        # To determine which side won, check the final prices.
        # After resolution, the winning side should be at $1 and loser at $0.
        # We can check via the order book or just use the resolution data.
        up_token = entry.get("up_token")
        dn_token = entry.get("dn_token")
        sell_leg = entry.get("sell_leg")
        sell_price = entry.get("sell_price", 0)

        # Try to get final prices (winner should be ~$1, loser ~$0)
        up_final = _get_midpoint(up_token)
        dn_final = _get_midpoint(dn_token)

        # If we can't determine the outcome yet (API might lag), skip
        if up_final is None and dn_final is None:
            # Give it some time after end — if >10 min past end, assume we can't resolve
            if now_ts - end_ts < 600:
                continue
            # Can't determine — log as unresolved
            _log_paper(
                "paper_unresolved",
                condition_id=cond_id,
                question=entry.get("question"),
                sell_leg=sell_leg,
                sell_price=sell_price,
            )
            entry["status"] = "resolved"
            entry["pnl"] = None
            to_remove.append(cond_id)
            continue

        # Determine winner: the side with higher final price won
        winner = None
        if up_final is not None and dn_final is not None:
            winner = "up" if up_final > dn_final else "down"
        elif up_final is not None:
            winner = "up" if up_final > 0.5 else "down"
        elif dn_final is not None:
            winner = "down" if dn_final > 0.5 else "up"

        if winner is None:
            entry["status"] = "resolved"
            entry["pnl"] = None
            to_remove.append(cond_id)
            continue

        # Calculate P&L
        correct_sell = (sell_leg != winner)  # We sold the loser = correct
        if correct_sell:
            # Sold loser at sell_price, held winner to $1
            pnl = sell_price + 1.00 - ENTRY_COST  # e.g. 0.08 + 1.00 - 1.00 = +0.08
        else:
            # Sold winner at sell_price, held loser to $0
            pnl = sell_price + 0.00 - ENTRY_COST  # e.g. 0.08 + 0.00 - 1.00 = -0.92

        entry["status"] = "resolved"
        entry["pnl"] = pnl
        entry["winner"] = winner
        entry["correct"] = correct_sell

        state["stats"]["total_pnl"] = state["stats"].get("total_pnl", 0) + pnl
        if correct_sell:
            state["stats"]["wins"] = state["stats"].get("wins", 0) + 1
        else:
            state["stats"]["losses"] = state["stats"].get("losses", 0) + 1

        _log_paper(
            "paper_resolved",
            condition_id=cond_id,
            question=entry.get("question"),
            sell_leg=sell_leg,
            sell_price=sell_price,
            winner=winner,
            correct=correct_sell,
            pnl=round(pnl, 4),
            cumulative_pnl=round(state["stats"]["total_pnl"], 4),
            record=f"{state['stats']['wins']}W-{state['stats']['losses']}L",
        )

        if now_ts - end_ts > 3600:
            to_remove.append(cond_id)

    # Clean up old resolved entries
    for cond_id in to_remove:
        del state["markets"][cond_id]


def get_paper_summary():
    """Return a one-line summary of paper trading stats."""
    state = _load_state()
    stats = state.get("stats", {})
    wins = stats.get("wins", 0)
    losses = stats.get("losses", 0)
    total = wins + losses
    pnl = stats.get("total_pnl", 0)
    if total == 0:
        return None
    win_rate = (wins / total) * 100
    return f"{wins}W-{losses}L ({win_rate:.0f}%) · PnL: ${pnl:+.2f}"
