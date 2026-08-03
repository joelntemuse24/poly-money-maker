# PRODUCTION IMPLEMENTATION PROMPT — Polymarket BTC 15m Strategy

## CONTEXT

We run a Polymarket trading bot on a GCP VM that trades BTC "Up or Down" 15-minute
binary markets. The bot buys complete sets (1 UP + 1 DOWN = $1.00) and sells the
losing leg for pennies in the final 90 seconds before market expiry, then redeems
the winning leg at $1.00.

**The strategy edge:** Buy a set for ~$1.00, sell the loser for ~3¢, redeem the
winner for $1.00. Net profit ≈ 3¢ per market per share.

**The problem:** BTC can reverse in the final 90 seconds. When we sell the "loser"
at 4¢ and it turns out to be the winner, we lose ~$1.00 per share instead of
profiting 3¢. These "reversals" happen at a **2.9% rate** (21 in 729 markets) and
are getting worse. The reversal cost (~14¢/market) now exceeds sell revenue
(~12¢/market), making the strategy net negative.

## SIMULATION DATA (729 markets, 5 shares)

### Strategy performance
- **Sell rate:** 78% of markets successfully sell the loser
- **Win rate:** 75% (of resolved markets)
- **Avg sell price:** 3.1¢ (median 1¢, range 0.1¢–39¢)
- **Trigger miss rate:** 6.8% (triggered but fill failed)
- **Reversal rate:** 2.9% (21 reversals in 729 markets, increasing trend)
- **Mean PnL:** -$0.025/market (NEGATIVE — reversals exceed sell revenue)

### Sell price distribution (148 sells at 5 shares)
- 47% sold at ≤2¢ (0% reversal rate — these are safe)
- 14% sold at exactly 5¢ (the threshold trigger)
- 6% sold at 4¢ (where most reversals occur)
- 8% sold at 3-8¢
- 6% sold at 12-39¢ (last-chance sells, some reversals)

### Reversal analysis (21 reversals)
All 21 reversals sorted by time left at sell:

| Time left | Count | Sell price range | Notes |
|-----------|-------|------------------|-------|
| ≤10s      | 5     | 4¢–39¢           | Last-second BTC flips |
| 30-50s    | 5     | 4¢–20¢           | Mid-window reversals |
| 70-90s    | 11    | 1¢–7¢            | Early-window reversals |

**Key finding:** Reversals happen across the entire 90s sell window, not just
early. Even selling at 1¢ with 74s left can reverse. No time-based gate fixes this.

### Winner bid at sell time (328 correct sells)
- **97% of the time, the winner is at ≥90¢ when we sell the loser**
- **100% of the time, the winner is at ≥75¢**
- Never below 75¢, never below 50¢

### Hedge threshold analysis (tick study, 76 markets with bid path data)

| Threshold | Caught reversals | False hedges | False rate | Caught/False ratio |
|-----------|-----------------|-------------|------------|-------------------|
| **40¢**   | **4**           | **2**       | **33%**    | **2.0**           |
| 50¢       | 3               | 3           | 50%        | 1.0               |
| 60¢       | 4               | 3           | 43%        | 1.3               |
| 70¢       | 3               | 6           | 67%        | 0.5               |

**40¢ is the optimal hedge threshold.** Below 40¢, the winner is truly collapsing.
Above 40¢, the winner is just wobbling and likely to recover.

### Hedge simulation results (actual fills with latency)

| Sim | Threshold | Polling | Markets | Hedges | Caught | False | Mean PnL |
|-----|-----------|---------|---------|--------|--------|-------|----------|
| hedge40 | 40¢ | 0.5s | 250 | 7 | 4 | 3 | -$0.023 |
| hedge45 | 45¢ | 0.5s | 250 | 8 | 3 | 5 | -$0.024 |
| hedge65f | 65¢ | 0.1s | 262 | 11 | 3 | 8 | -$0.093 |
| hedge70f | 70¢ | 0.1s | 262 | 16 | 4 | 12 | -$0.107 |

**hedge40 at 0.5s polling was the best** but still negative because 0.5s polling
is too slow — by the time we detect the winner at 40¢, it's already at 25-30¢ and
we fill at a worse price. **0.1s polling should fix this.**

### Latency bounce problem
Two reversals were caused by a "latency bounce": the bot triggered a sell when the
bid was ≤5¢, but after the 2-second execution latency, the bid had bounced to 20¢.
The fill executed at 20¢ on a leg that was recovering — and it then won. The sell
should have been cancelled when the post-latency bid exceeded the threshold.

## WHAT TO IMPLEMENT

### 1. Update production strategy.json

```json
{
    "sell_threshold": 0.05,
    "hedge_enabled": true,
    "hedge_threshold": 0.40,
    "sell_window_min": 1.5,
    "sell_grace_s": 2,
    "sell_cooldown_s": 3,
    "sell_lastchance_threshold": 0.10,
    "sell_lastchance_s": 10,
    "redeem_throttle_s": 30,
    "max_redeem_age_days": 7,
    "dry_run": false,
    "poll_sell_window_s": 0.1,
    "positions_refresh_s": 2,
    "balance_refresh_s": 15
}
```

**Changes from current production config:**
- `hedge_enabled`: false → **true**
- `hedge_threshold`: 0.50 → **0.40**
- `sell_threshold`: 0.10 → **0.05** (already was 0.05 in sim, match it)
- `sell_window_min`: 0.75 → **1.5** (90-second window, matching sim)
- `sell_lastchance_threshold`: 0.35 → **0.10** (only sell in final 10s if truly dead)
- `poll_sell_window_s`: 0.25 → **0.1** (sub-second polling in sell window)

### 2. Add post-latency bid bounce cancel to bot.py

**File:** `bot.py`, in the sell phase (around line 1107-1212)

**Problem:** When the bot decides to sell at 4¢ and submits the order, there's a
real-world delay before the order reaches the exchange. The bid may have bounced
back to 20¢ by then. The current code fills at whatever the bid is — we need to
re-check the bid and cancel if it recovered.

**Implementation:** Before calling `sell_market_with_retry()`, re-fetch the current
bid for the leg being sold. If the current bid is now above the sell threshold
(5¢ for threshold sells, 10¢ for last-chance sells), **skip the sell** — the leg
recovered.

```python
# Before sell_market_with_retry for UP leg:
if will_sell_up:
    # Re-fetch current bid to check for bounce
    fresh_up_bid, _ = get_book_bid(up_token)
    if fresh_up_bid is not None and fresh_up_bid > SELL_THRESHOLD:
        log_event("sell_cancel_bounce", condition_id=cond, leg="up",
                  trigger_bid=up_price, current_bid=fresh_up_bid,
                  threshold=SELL_THRESHOLD, seconds_left=round(seconds_left, 3))
        console.print(f"  [dim][CANCEL][/] UP sell cancelled — bid bounced {up_price:.3f} → {fresh_up_bid:.3f}")
        up_trigger = False
        sell_up = False
        will_sell_up = False
    else:
        # Proceed with sell using the fresh bid
        up_price = fresh_up_bid if fresh_up_bid is not None else up_price
        # ... existing sell logic ...
```

Do the same for the DN leg. The key is: **re-fetch the bid right before selling,
and cancel if it bounced above threshold.**

### 3. Verify hedge logic in bot.py (lines 1214-1285)

The hedge logic already exists in bot.py and looks correct:
- Checks `HEDGE_ENABLED` and `HEDGE_THRESHOLD` from strategy config
- Detects when the held leg (winner) drops below `HEDGE_THRESHOLD`
- Sells the held leg with `sell_market_with_retry(token, size, 0.01)`
- Logs and notifies on hedge fire

**With `hedge_enabled: true` and `hedge_threshold: 0.40`, this will activate.**

**One concern:** The hedge uses `sell_market_with_retry(token, size, 0.01)` which
sets a 1¢ limit. At 0.1s polling, the bid should still be near 40¢ when we detect
it. But the limit of 0.01 means we'll accept any fill ≥ 1¢. Consider changing the
hedge limit to `HEDGE_THRESHOLD * 0.5` (20¢) so we don't get filled at 1¢ if the
bid drops fast between detection and execution:

```python
# Change hedge sell limit from 0.01 to a fraction of threshold
sold, _ = sell_market_with_retry(dn_token, dn_size, HEDGE_THRESHOLD * 0.5)
```

### 4. Verify 0.1s polling works in bot.py

The polling rate is controlled by `POLL_SELL_WINDOW_S` which is loaded from
`strategy.json` as `poll_sell_window_s`. The main loop (line 1298-1306) already
uses this:

```python
if _min_ttm <= SELL_WINDOW_MIN:
    _sleep_s = POLL_SELL_WINDOW_S
```

Setting `poll_sell_window_s: 0.1` in strategy.json should work without code changes.
**But verify that the book fetch can complete in 0.1s** — if the HTTP request takes
longer than 0.1s, the bot will fall behind. The pre-fetch mechanism (submitting
book fetches before sleeping) should handle this, but test it.

### 5. Stop all simulation services

```bash
sudo systemctl stop polyshadow polyshadow50 polyshadow-bounce \
  polyshadow-hedge70f polyshadow-hedge65f polyshadow-hedge70f50 \
  polyshadow-tickstudy polyshadow-hedge40 polyshadow-hedge45
sudo systemctl disable polyshadow polyshadow50 polyshadow-bounce \
  polyshadow-hedge70f polyshadow-hedge65f polyshadow-hedge70f50 \
  polyshadow-tickstudy polyshadow-hedge40 polyshadow-hedge45
```

### 6. Restart polybot with new config

```bash
# Update strategy.json on GCP
# Then restart:
sudo systemctl restart polybot
sudo systemctl status polybot --no-pager -l | head -20
```

## CONSTRAINTS

1. **Never break the live bot.** The bot is trading real money. Test changes
   carefully. The strategy.json is hot-reloaded every cycle — no restart needed
   for config changes. Code changes require a restart.

2. **The bot runs on a 2GB GCP VM.** Memory is tight. All sim services should be
   stopped before going live. The bot itself uses ~47MB.

3. **0.1s polling means 10 HTTP requests/second per market in the sell window.**
   With 3-5 markets in the sell window simultaneously, that's 30-50 requests/second.
   Polymarket's public API should handle this, but monitor for rate limiting.

4. **The hedge at 40¢ will false-trigger ~33% of the time.** Each false hedge
   costs ~$0.60/share (sell winner at 40¢ instead of redeem at $1). Each caught
   reversal saves ~$0.40/share (sell at 40¢ instead of $0). The 2:1 ratio means
   the hedge is net positive but not by much. Monitor hedge fire rate closely.

5. **The bounce cancel may reduce sell rate.** Some sells that would have filled
   at 5-20¢ will be cancelled because the bid bounced. This is intentional —
   those are the legs most likely to reverse. But it means less sell revenue.

## EXPECTED OUTCOMES

With all changes applied (hedge 40¢ + 0.1s polling + bounce cancel + 10¢ last-chance):

| Metric | Current | Expected |
|--------|---------|----------|
| Reversal rate | 2.9% | ~1.5% (bounce cancel + hedge) |
| Sell rate | 78% | ~72% (bounce cancel skips some) |
| Avg sell price | 3.1¢ | ~2.5¢ (fewer high-price sells) |
| Hedge fires | 0 | ~3% of markets |
| Hedge false rate | N/A | ~33% |
| Mean PnL (5sh) | -$0.025 | ~+$0.02–$0.04 |
| Daily PnL (5sh, 96 markets) | -$2.40 | +$2–$4 |

**At 50 shares:** ~$20–$40/day = $0.83–$1.67/hour
**At 100 shares:** ~$40–$80/day = $1.67–$3.33/hour (if depth holds)

## MONITORING

After going live, monitor:
1. `bot.log` for hedge_fire and sell_cancel_bounce events
2. `pnl.json` for daily PnL tracking
3. Reversal rate (sold leg == winner) — should drop from 2.9% to ~1.5%
4. Hedge false rate — if >50%, consider lowering threshold to 30¢
5. API rate limiting — if 0.1s polling causes 429s, increase to 0.15s

## FILE LOCATIONS

- **Bot code:** `bot.py` (1337 lines)
- **Strategy config:** `strategy.json` (hot-reloaded, on GCP at `~/poly-money-maker/strategy.json`)
- **Sim code (reference):** `sim/shadow.py`, `sim/policy.py`, `sim/store.py`
- **Sim configs:** `sim/strategy.sim.*.json`
- **Systemd services:** `deploy/polybot.service`, `deploy/polyshadow*.service`
- **Design doc:** `TECHNICAL_DESIGN.md`
- **State files:** `positions.json`, `pnl.json` (on GCP)
