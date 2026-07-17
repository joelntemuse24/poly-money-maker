# Live Shadow Simulator

Paper-trades BTC up/down markets using **live Polymarket order books**.
**Never places real orders.** Does **not** import `bot.py`. Safe beside `polybot`.

## Current experiment (15m)

| Knob | Value |
|------|-------|
| Series | `btc-up-or-down-15m` |
| Sell window | last **2 minutes** |
| Sell threshold | **12¢** |
| Last-chance | last **10s**, bid **&lt;35¢** + opposite **≥65¢** |
| Data dir | `sim_data/15m/` |

## Disk safety

- `record_ticks: false` by default (results.jsonl only)
- Prune ticks after **6h**, trades **7d**
- Cap `sim_data/` ~**150MB**; want ~**200MB** free
- On ENOSPC: stop ticks/trades, prune, no traceback spam
- Unresolved markets do not count as full-entry strategy losses in summary

### If the VM disk is full

```bash
sudo systemctl stop polyshadow
df -h
du -sh sim_data/* sim_data/15m/* 2>/dev/null | sort -h
rm -rf sim_data/15m/ticks/* sim_data/*/ticks/* 2>/dev/null
find sim_data -name '*.log*' -type f -delete
sudo journalctl --vacuum-size=50M
df -h .
git pull
sudo systemctl start polyshadow
journalctl -u polyshadow -n 30 --no-pager
# expect: DISK free=... record_ticks=False
```

## Run

```bash
python -m sim.shadow
python -m sim.shadow --once
python -m sim.shadow --summary
```

## GCP

```bash
cd ~/poly-money-maker && git pull
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
