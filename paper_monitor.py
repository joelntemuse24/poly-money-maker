"""
Paper-trade monitor for BTC hourly markets.

Runs alongside the main bot, tracking what the strategy *would* do on
every hourly market — whether or not we actually hold a position. Logs
hypothetical P&L to paper_trades.log for backtesting analysis.

Market discovery: queries gamma-api /events list for recent BTC hourly
markets.  Price data comes from the same CLOB book/midpoint endpoints
as the real bot.
"""

import json
import os
import re
import time
from datetime import datetime
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

# How often to scan for new markets (seconds)
_DISCOVERY_INTERVAL = 300  # 5 minutes
# How many pages to fetch from the events list per discovery cycle
_DISCOVERY_PAGES = 5

_last_discovery_time = 0

# Regex: true hourly format ends with bare "8AM ET" or "11PM ET" (no colon)
_HOURLY_RE = re.compile(r",\s*\d{1,2}(AM|PM)\s+ET\s*$", re.IGNORECASE)


def _load_state():
    if os.path.exists(PAPER_STATE_FILE):
        with open(PAPER_STATE_FILE, "r") as f:
            return json.load(f)
    return {
        "markets": {},
        "stats": {"wins": 0, "losses": 0, "total_pnl": 0.0},
    }


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


def _get_book_bid(token_id):
    """Get best bid for a token from CLOB order book (same as bot)."""
    try:
        res = requests.get(
            f"{CLOB_API}/book",
            params={"token_id": token_id},
            timeout=2,
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
    """Get midpoint price for a token (same as bot)."""
    try:
        res = requests.get(
            f"{CLOB_API}/midpoint",
            params={"token_id": token_id},
            timeout=2,
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


def _is_btc_hourly(title):
    """Return True only for true 1-hour BTC markets like '… June 23, 4PM ET'.

    Rejects sub-hourly ranges ('3:40PM-3:45PM ET'), daily markets
    ('Bitcoin Up or Down on June 20?'), and non-BTC coins.
    """
    if "bitcoin up or down" not in title.lower():
        return False
    return bool(_HOURLY_RE.search(title))


def _discover_markets(state):
    """Query gamma-api /events list for recent BTC hourly markets.

    Fetches the most recent events (newest first) and registers any
    BTC hourly markets we haven't seen yet.  Only runs every
    _DISCOVERY_INTERVAL seconds.
    """
    global _last_discovery_time
    now = time.time()
    if now - _last_discovery_time < _DISCOVERY_INTERVAL:
        return
    _last_discovery_time = now

    found_any = False
    seen_ids = set()
    next_cursor = None

    for _ in range(_DISCOVERY_PAGES):
        try:
            params = {
                "limit": "100",
                "order": "id",
                "ascending": "false",
                "closed": "false",
            }
            if next_cursor is not None:
                params["id_lt"] = str(next_cursor)
            res = requests.get(
                f"{GAMMA_API}/events",
                params=params,
                timeout=5,
            )
            if res.status_code != 200:
                break
            events = res.json()
            if not isinstance(events, list) or not events:
                break
        except Exception:
            break

        for data in events:
            eid = data.get("id")
            if eid in seen_ids:
                continue
            seen_ids.add(eid)

            title = data.get("title") or ""
            if not _is_btc_hourly(title):
                continue

            markets = data.get("markets", [])
            if not markets:
                continue

            m = markets[0]
            condition_id = m.get("conditionId")
            if not condition_id or condition_id in state["markets"]:
                continue

            clob_ids = m.get("clobTokenIds")
            outcomes = m.get("outcomes", [])
            if isinstance(clob_ids, str):
                try:
                    clob_ids = json.loads(clob_ids)
                except (json.JSONDecodeError, TypeError):
                    continue
            if isinstance(outcomes, str):
                try:
                    outcomes = json.loads(outcomes)
                except (json.JSONDecodeError, TypeError):
                    outcomes = []
            if not clob_ids or len(clob_ids) < 2:
                continue

            up_token = None
            dn_token = None
            for i, outcome in enumerate(outcomes):
                if outcome.lower() in ("up", "yes"):
                    up_token = clob_ids[i]
                elif outcome.lower() in ("down", "no"):
                    dn_token = clob_ids[i]

            if not up_token or not dn_token:
                continue

            end_date_str = data.get("endDate") or m.get("endDate")
            if not end_date_str:
                continue
            try:
                end_dt = datetime.fromisoformat(end_date_str.replace("Z", "+00:00"))
                end_ts = end_dt.timestamp()
            except Exception:
                continue

            closed = data.get("closed", False) or m.get("closed", False)

            state["markets"][condition_id] = {
                "event_id": eid,
                "question": title,
                "condition_id": condition_id,
                "up_token": up_token,
                "dn_token": dn_token,
                "end_ts": end_ts,
                "first_seen": datetime.now().isoformat(),
                "status": "resolved" if closed else "monitoring",
                "sell_triggered": False,
                "sell_leg": None,
                "sell_price": None,
                "sell_time": None,
            }
            found_any = True

            if not closed:
                _log_paper(
                    "paper_market_discovered",
                    condition_id=condition_id,
                    question=title,
                    end_ts=end_ts,
                    event_id=eid,
                )

        # Advance pagination cursor
        min_id = min(e.get("id", 999999) for e in events)
        if next_cursor is not None and min_id >= next_cursor:
            break
        next_cursor = min_id

    if found_any:
        _save_state(state)




def _check_market_resolution(condition_id, entry, state):
    """Use the CLOB market endpoint to determine winner after close."""
    try:
        res = requests.get(
            f"{CLOB_API}/markets/{condition_id}",
            timeout=5,
        )
        if res.status_code != 200:
            return None
        data = res.json()
        if not data.get("closed"):
            return None
        tokens = data.get("tokens", [])
        for t in tokens:
            if t.get("winner"):
                outcome = (t.get("outcome") or "").lower()
                if outcome in ("up", "yes"):
                    return "up"
                elif outcome in ("down", "no"):
                    return "down"
        return None
    except Exception:
        return None


def run_paper_cycle(console=None):
    """Run one paper-monitoring cycle. Called from the main bot loop.

    Returns a brief status string for display, or None if nothing notable.
    """
    state = _load_state()
    now = datetime.now()
    now_ts = time.time()
    notable = []

    # Discover new markets (rate-limited internally)
    _discover_markets(state)

    # Check prices on active markets
    for cond_id, entry in list(state["markets"].items()):
        if entry.get("status") != "monitoring":
            continue

        end_ts = entry.get("end_ts", 0)
        if end_ts <= now_ts:
            # Market ended while we were monitoring — skip to resolution
            continue

        # Already triggered
        if entry.get("sell_triggered"):
            continue

        up_token = entry.get("up_token")
        dn_token = entry.get("dn_token")
        if not up_token or not dn_token:
            continue

        # Check prices using CLOB (same method as bot)
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
            entry["status"] = "pending_resolution"
            _log_paper(
                "paper_sell",
                condition_id=cond_id,
                question=entry["question"],
                leg=sell_leg,
                price=sell_price,
                ttm_min=round((end_ts - now_ts) / 60, 1),
            )
            notable.append(
                f"PAPER SELL {sell_leg.upper()} @ ${sell_price:.2f} on {entry['question']}"
            )

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

    for cond_id, entry in list(state["markets"].items()):
        end_ts = entry.get("end_ts", 0)
        if end_ts > now_ts:
            continue  # Still active

        if entry.get("status") == "resolved":
            # Already resolved, mark for cleanup if old (>2h)
            if now_ts - end_ts > 7200:
                to_remove.append(cond_id)
            continue

        if not entry.get("sell_triggered"):
            # Market ended without any leg hitting threshold — no trade
            # Only log if we were actively monitoring (not pre-closed discoveries)
            if entry.get("status") == "monitoring":
                _log_paper(
                    "paper_no_trade",
                    condition_id=cond_id,
                    question=entry.get("question"),
                    reason="no_leg_hit_threshold",
                )
            entry["status"] = "resolved"
            entry["pnl"] = 0.0
            continue

        # Determine winner via CLOB market endpoint
        winner = _check_market_resolution(cond_id, entry, state)

        if winner is None:
            # Try midpoint fallback (winner should be ~$1, loser ~$0)
            up_token = entry.get("up_token")
            dn_token = entry.get("dn_token")
            up_final = _get_midpoint(up_token) if up_token else None
            dn_final = _get_midpoint(dn_token) if dn_token else None

            if up_final is not None and dn_final is not None:
                winner = "up" if up_final > dn_final else "down"
            elif up_final is not None:
                winner = "up" if up_final > 0.5 else "down"
            elif dn_final is not None:
                winner = "down" if dn_final > 0.5 else "up"

        if winner is None:
            # Give it time — if >15 min past end, log as unresolved
            if now_ts - end_ts < 900:
                continue
            _log_paper(
                "paper_unresolved",
                condition_id=cond_id,
                question=entry.get("question"),
                sell_leg=entry.get("sell_leg"),
                sell_price=entry.get("sell_price"),
            )
            entry["status"] = "resolved"
            entry["pnl"] = None
            continue

        # Calculate P&L
        sell_leg = entry.get("sell_leg")
        sell_price = entry.get("sell_price", 0)
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
    active = sum(1 for m in state.get("markets", {}).values() if m.get("status") == "monitoring")
    if total == 0 and active == 0:
        return None
    parts = []
    if total > 0:
        win_rate = (wins / total) * 100
        parts.append(f"{wins}W-{losses}L ({win_rate:.0f}%)")
        parts.append(f"PnL: ${pnl:+.2f}")
    if active > 0:
        parts.append(f"Tracking: {active}")
    return " · ".join(parts)
