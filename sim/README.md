# Live Shadow Simulator

Paper-trades **every** BTC 5-minute market using **live Polymarket order books**.  
**Never places real orders.** Does **not** import `bot.py`.

## What this is (plain English)

1. **Entry (simulated, calibrated)**  
   When a new 5m market appears with ~1–4.5 minutes left, we pretend we bought a complete set (UP+DOWN) at your historical cost, default **`$1.045` per share** (from your trade export: ~4.5¢ over $1).

2. **Live data (real)**  
   For every open paper position we poll the public CLOB  
   `https://clob.polymarket.com/book?token_id=...`  
   — same books the real bot sees — for **both** legs, on **all** markets (not just ones you hold for real).

3. **Strategy (same rules as live bot)**  
   - Sell window: last **45s**  
   - Sell if best bid ≤ **10¢**  
   - Last **10s**: bid **&lt; 35¢** and opposite **≥ 65¢**  
   - Hedge: off by default  

4. **Fills (simulated against real depth)**  
   When the strategy would sell, we **walk the live bid book** like a FAK order:  
   fill what size is actually resting at/above the limit price.  
   If the book is empty or too thin → **MISS** (same class of failure you saw at expiry).

5. **Settlement**  
   After expiry we infer the winner from book dominance (e.g. one side ≥ 90¢) and  
   `PnL = sell_proceeds + hedge + redeem($1 on winner) − entry_cost`.

That gives you **12 markets/hour** of realistic path+fill data while the real bot runs separately.

## Run (local or GCP)

```bash
# from repo root, same venv is fine (only needs requests)
pip install requests

# optional: calibrate set_cost from your export
python -m sim.analyze_history "Polymarket-History-2026-07-15.csv"

# start live shadow (Ctrl-C to stop)
python -m sim.shadow

# one cycle smoke test
python -m sim.shadow --once

# results summary
python -m sim.shadow --summary
```

### GCP (alongside polybot, separate process)

```bash
cd ~/poly-money-maker
git pull   # after you push this
# optional systemd unit later; for now:
nohup .venv/bin/python -m sim.shadow >> sim_data/nohup.out 2>&1 &
```

Do **not** run this as a second `bot.py`. This module never trades.

## Config

Edit `sim/strategy.sim.json`:

| Key | Meaning |
|-----|---------|
| `strategy.*` | Same knobs as the live bot |
| `sim.set_cost` | Complete-set entry cost per share (default 1.045) |
| `sim.shares` | Paper size per leg (default 5) |
| `sim.fill_model` | `depth` (realistic) / `best_bid` / `best_bid_partial` |
| `sim.poll_sell_s` | 0.25s inside sell window |

## Outputs (`sim_data/`)

| Path | Content |
|------|---------|
| `shadow_state.json` | Open paper positions |
| `results.jsonl` | One line per completed market (PnL, fills, misses) |
| `ticks/<conditionId>.jsonl` | Bid time series for research |
| `trades/<conditionId>.json` | Full event log for one market |
| `shadow.log` | Human-readable log |

## How to read results

```bash
python -m sim.shadow --summary
```

- **mean_pnl > 0** at set_cost 1.045 under `fill_model=depth` → strategy looks viable  
- **trigger_miss_rate** high → detection works but books won’t fill (your FAK problem)  
- Compare `sell_avg_px` to your live loser sells (~7–8¢ historically)

## Important limitations

- Entry is **modeled** (fixed set_cost), not a live GTC fill path.  
- Resolution is inferred from post-expiry books (usually clear; rare ties logged).  
- Public REST books can lag a few hundred ms vs WebSocket — still far more realistic than mid-only history.  
- This is a **shadow** of sell policy, not a full portfolio risk system.

## Relation to the live bot

| | Live `bot.py` | Shadow `sim.shadow` |
|--|---------------|---------------------|
| Orders | Real FAK sells | Simulated only |
| Markets | Only ones you hold | **All** BTC 5m |
| Entry | Manual UI | Assumed set_cost |
| Books | CLOB SDK | Public `/book` REST |
| Risk | Real money | Zero |
