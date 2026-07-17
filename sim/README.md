# Live Shadow Simulator

Paper-trades BTC up/down markets using **live Polymarket order books**.
**Never places real orders.** Does **not** import `bot.py`. Safe beside `polybot`.

## Current experiment (15m)

Default config (`sim/strategy.sim.json`):

| Knob | Value |
|------|-------|
| Series | `btc-up-or-down-15m` |
| Sell window | last **2 minutes** |
| Sell threshold | **12¢** |
| Last-chance | last **10s**, bid **&lt;35¢** + opposite **≥65¢** |
| Data dir | `sim_data/15m/` (keeps old 5m results separate) |

## Why 15m

The old complete-set thesis assumed ~$1.00 entry. Realized set cost is often ~$1.043, so break-even needs meaningful loser sells. 15m markets may have deeper books and a longer exit window to test before changing the live 5m bot.

## Run

```bash
python -m sim.shadow
python -m sim.shadow --once
python -m sim.shadow --summary
```

## GCP

```bash
cd ~/poly-money-maker && git pull
# archive old 5m root results if present (optional)
# mv sim_data/results.jsonl sim_data/results_5m_archive.jsonl 2>/dev/null || true
sudo systemctl restart polyshadow
systemctl status polyshadow --no-pager
journalctl -u polyshadow -n 40 --no-pager
.venv/bin/python -m sim.shadow --summary
```

## Isolation

| Shadow | Live bot |
|--------|----------|
| `sim/strategy.sim.json` | `strategy.json` |
| `sim_data/<tag>/` | `positions.json`, `.env`, `bot.log` |
| public books only | real orders |

## Switch series later

Edit `sim/strategy.sim.json`:

```json
"series_slug": "btc-up-or-down-5m",
"data_tag": "5m"
```

Then restart `polyshadow`.
