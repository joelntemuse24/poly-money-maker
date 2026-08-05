#!/usr/bin/env python3
"""
Backtest different sell_threshold / sell_window_min combinations
against tick-level bid data recorded by the shadow simulator.

Reads:
  - sim_data/15m-tick-study/ticks/*.jsonl  (per-market bid snapshots)
  - sim_data/15m-tick-study/results.jsonl  (market outcomes: winner, etc.)

For each (threshold, window) combo, replays the sell decision:
  - If seconds_left <= window*60 and a leg bid <= threshold, sell that leg
  - Simulates 2s execution latency (re-check bid after delay)
  - If sold leg == winner → reversal (loss = sell_price - 1.00 per share)
  - If sold leg == loser → correct (profit = sell_price per share)
  - If no sell → flat (redeem winner at $1.00, entry cost $1.00 → $0)

Outputs a table of results.
"""
import json
import os
import sys
import glob
from collections import defaultdict

TICKS_DIR = os.path.expanduser("~/poly-money-maker/sim_data/15m-tick-study/ticks")
RESULTS_FILE = os.path.expanduser("~/poly-money-maker/sim_data/15m-tick-study/results.jsonl")

# Backtest parameters
THRESHOLDS = [0.01, 0.015, 0.02, 0.025, 0.03, 0.04, 0.05]
WINDOWS_MIN = [1.5, 2.0, 3.0, 4.0, 5.0]
EXEC_LATENCY_S = 2.0  # simulate 2s between decision and fill
LASTCHANCE_S = 10.0   # final 10s last-chance window
SHARES = 5.0          # tick study was at 5 shares


def load_results():
    """Load results.jsonl → dict by condition_id"""
    results = {}
    if not os.path.exists(RESULTS_FILE):
        print(f"ERROR: {RESULTS_FILE} not found")
        sys.exit(1)
    with open(RESULTS_FILE) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
                cid = row.get("condition_id")
                if cid:
                    results[cid] = row
            except json.JSONDecodeError:
                continue
    return results


def load_ticks(condition_id):
    """Load tick data for a single market"""
    path = os.path.join(TICKS_DIR, f"{condition_id}.jsonl")
    if not os.path.exists(path):
        return None
    ticks = []
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                ticks.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return ticks if ticks else None


def simulate_sell(ticks, threshold, window_min, lastchance_threshold):
    """
    Replay sell decisions for one market.
    Returns (sold, sell_price, sell_leg, seconds_left_at_sell, reversal)
    or (False, None, None, None, False) if no sell.
    """
    window_s = window_min * 60.0
    sold = False
    sell_price = None
    sell_leg = None
    sell_seconds_left = None

    for i, tick in enumerate(ticks):
        sl = tick["seconds_left"]
        if sl > window_s:
            continue
        if sl <= 0:
            break

        up_bid = tick.get("up_bid")
        dn_bid = tick.get("dn_bid")
        if up_bid is None or dn_bid is None:
            continue

        up_size = tick.get("up_size", 0)
        dn_size = tick.get("dn_size", 0)

        # Normal threshold trigger
        up_trigger = up_size > 0 and up_bid <= threshold
        dn_trigger = dn_size > 0 and dn_bid <= threshold

        # Last-chance trigger in final seconds
        if sl <= LASTCHANCE_S and not up_trigger and not dn_trigger:
            confirmation = 1.0 - lastchance_threshold
            up_cand = up_size > 0 and up_bid is not None and up_bid < lastchance_threshold
            dn_cand = dn_size > 0 and dn_bid is not None and dn_bid < lastchance_threshold
            if up_cand and not dn_cand and dn_bid is not None and dn_bid >= confirmation:
                up_trigger = True
            elif dn_cand and not up_cand and up_bid is not None and up_bid >= confirmation:
                dn_trigger = True

        if not up_trigger and not dn_trigger:
            continue

        # Decide which leg to sell (sell the cheaper one)
        if up_trigger and dn_trigger:
            if up_bid <= dn_bid:
                leg, bid = "up", up_bid
            else:
                leg, bid = "down", dn_bid
        elif up_trigger:
            leg, bid = "up", up_bid
        else:
            leg, bid = "down", dn_bid

        # Simulate execution latency: look ahead in ticks for the bid after ~2s
        future_bid = bid
        for j in range(i + 1, len(ticks)):
            if ticks[j]["seconds_left"] <= sl - EXEC_LATENCY_S:
                if leg == "up":
                    future_bid = ticks[j].get("up_bid", bid)
                else:
                    future_bid = ticks[j].get("dn_bid", bid)
                break

        # Bounce cancel: if bid recovered above threshold after latency, skip
        cancel_thresh = threshold if sl > LASTCHANCE_S else lastchance_threshold
        if future_bid is not None and future_bid > cancel_thresh:
            continue  # bounce cancel, try again next tick

        sold = True
        sell_price = future_bid if future_bid is not None else bid
        sell_leg = leg
        sell_seconds_left = sl
        break

    return sold, sell_price, sell_leg, sell_seconds_left


def main():
    print("Loading results...")
    results = load_results()
    print(f"  {len(results)} markets in results.jsonl")

    # Load all tick files
    tick_files = glob.glob(os.path.join(TICKS_DIR, "*.jsonl"))
    print(f"  {len(tick_files)} tick files found")

    # Build market list: only markets that have both ticks and results
    markets = []
    for tf in tick_files:
        cid = os.path.basename(tf).replace(".jsonl", "")
        if cid in results:
            ticks = load_ticks(cid)
            if ticks and len(ticks) > 1:
                res = results[cid]
                winner = res.get("winner")
                if winner is not None:
                    markets.append((cid, ticks, winner, res))

    print(f"  {len(markets)} markets with both ticks + winner data\n")

    if not markets:
        print("No usable markets found.")
        return

    # Header
    print(f"{'Threshold':>10} {'Window':>7} | {'Sells':>5} {'Rate':>5} {'AvgPx':>6} "
          f"{'Rev':>4} {'RevRate':>6} | {'SellRev':>8} {'RevLoss':>9} {'NetPnL':>8} {'PerMkt':>7}")
    print("-" * 95)

    best_pnl = -999
    best_combo = None

    for threshold in THRESHOLDS:
        for window in WINDOWS_MIN:
            total_sells = 0
            total_reversals = 0
            sell_revenue = 0.0
            reversal_loss = 0.0
            sell_prices = []

            for cid, ticks, winner, res in markets:
                # Use same lastchance threshold as the main threshold
                sold, price, leg, sl = simulate_sell(
                    ticks, threshold, window, threshold
                )

                if not sold:
                    continue

                total_sells += 1
                sell_prices.append(price)
                sell_revenue += price * SHARES

                # Check if reversal (sold leg was the winner)
                if (leg == "up" and winner == "up") or (leg == "down" and winner == "dn"):
                    total_reversals += 1
                    # Reversal: sold winner for `price`, redeem loser for $0
                    # Loss = (price - 1.00) * shares + (entry cost already paid)
                    reversal_loss += (1.0 - price) * SHARES
                # else: correct sell, profit = price * shares (entry cost = $1.00, redeem = $1.00)

            total_markets = len(markets)
            sell_rate = total_sells / total_markets if total_markets else 0
            avg_px = sum(sell_prices) / len(sell_prices) if sell_prices else 0
            rev_rate = total_reversals / total_sells if total_sells else 0
            net_pnl = sell_revenue - reversal_loss
            per_mkt = net_pnl / total_markets if total_markets else 0

            print(f"{threshold*100:>8.1f}c {window:>5.1f}m | {total_sells:>5} {sell_rate:>5.1%} "
                  f"{avg_px:>6.3f} {total_reversals:>4} {rev_rate:>6.1%} | "
                  f"${sell_revenue:>7.2f} ${reversal_loss:>8.2f} ${net_pnl:>7.2f} ${per_mkt:>6.3f}")

            if net_pnl > best_pnl:
                best_pnl = net_pnl
                best_combo = (threshold, window, total_sells, total_reversals, net_pnl)

    print("-" * 95)
    if best_combo:
        print(f"\nBest: {best_combo[0]*100:.1f}c threshold, {best_combo[1]}m window "
              f"→ {best_combo[2]} sells, {best_combo[3]} reversals, "
              f"net ${best_combo[4]:.2f} across {len(markets)} markets "
              f"(${best_combo[4]/len(markets):.3f}/market)")

    # Also show: for the best combo, detail the reversal sells
    if best_combo:
        print(f"\n--- Reversal detail for {best_combo[0]*100:.1f}c / {best_combo[1]}m ---")
        for cid, ticks, winner, res in markets:
            sold, price, leg, sl = simulate_sell(
                ticks, best_combo[0], best_combo[1], best_combo[0]
            )
            if sold and ((leg == "up" and winner == "up") or (leg == "down" and winner == "dn")):
                q = res.get("question", "?")[:45]
                print(f"  REVERSAL: {q}  leg={leg} price={price:.3f} sl={sl:.1f}s winner={winner}")


if __name__ == "__main__":
    main()
