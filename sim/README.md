# Live Shadow Simulator

Paper-trades **every** BTC 5-minute market using **live Polymarket order books**.  
**Never places real orders.** Does **not** import `bot.py`. Safe beside `polybot`.

## What this is

1. **Entry (simulated)** — complete set at calibrated `set_cost` (default **$1.043**/share)
2. **Live books (real)** — public CLOB `/book` only, no API keys
3. **Strategy** — same sell / last-chance / hedge rules as live bot
4. **Fills** — walk real bid depth (FAK model)
5. **Settlement** — winner from post-expiry book dominance → PnL

## Isolation (won't touch the bot)

| Shadow uses | Bot uses (untouched) |
|-------------|----------------------|
| `sim/strategy.sim.json` | `strategy.json` |
| `sim_data/*` | `positions.json`, `pnl.json`, `bot.log`, `.env` |
| public Gamma + CLOB | same public APIs + private trading |

Also: single-instance lock, Nice=10 + CPU/memory caps in systemd, discovery cache, book poll only when TTM ≤ 3 min.

## Run

```bash
# repo root, same venv is fine
python -m sim.shadow          # continuous
python -m sim.shadow --once   # smoke test
python -m sim.shadow --summary
```

## GCP permanent install (alongside polybot)

```bash
cd ~/poly-money-maker   # or your actual path
git pull

# edit user/path in the unit if needed
USER=$(whoami)
DIR=$(pwd)
sed -e "s|YOUR_USER|$USER|g" -e "s|/home/YOUR_USER/poly-money-maker|$DIR|g" \
  deploy/polyshadow.service | sudo tee /etc/systemd/system/polyshadow.service

sudo systemctl daemon-reload
sudo systemctl enable --now polyshadow

# verify
systemctl status polyshadow --no-pager
journalctl -u polyshadow -n 50 --no-pager
tail -f sim_data/shadow.log
python -m sim.shadow --summary
```

polybot stays as-is. Do **not** run a second `bot.py`.

## Config (`sim/strategy.sim.json`)

| Key | Meaning |
|-----|---------|
| `strategy.*` | Same knobs as live bot |
| `sim.set_cost` | Entry cost per share |
| `sim.shares` | Paper size per leg |
| `sim.fill_model` | `depth` / `best_bid` / `best_bid_partial` |
| `sim.poll_sell_s` | Poll inside sell window (default 0.35s) |
| `sim.book_horizon_min` | Only fetch books when TTM ≤ this (default 3) |

## Outputs (`sim_data/`)

| Path | Content |
|------|---------|
| `shadow_state.json` | Open paper positions |
| `results.jsonl` | One line per completed market |
| `ticks/<id>.jsonl` | Bid path samples |
| `trades/<id>.json` | Full trade record |
| `shadow.log` | Rotating log |
| `shadow.lock` | Single-instance lock |
| `shadow.heartbeat` | Last cycle timestamp |
