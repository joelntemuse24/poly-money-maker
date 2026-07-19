# Live Shadow Simulator

Paper-trades BTC up/down markets using **live Polymarket order books**.
**Never places real orders.** Does **not** import `bot.py`. Safe beside `polybot`.

## Current experiment (anytime 8¢ — 15m + hourly)

| Knob | Value |
|------|-------|
| Series | `btc-up-or-down-15m` **and** `btc-up-or-down-hourly` |
| Sell threshold | **8¢** anytime a leg bids ≤ 8¢ |
| Sell window | **120 min** (effectively full market; not last-N-only) |
| Last-chance | **off** (`sell_lastchance_s: 0`) |
| Book horizon | **120 min** (poll books for open positions) |
| Data dir | `sim_data/15m1h-8c-any/` (separate from prior 12¢/2min run) |

Prior 15m @ 12¢ / last-2min results remain in `sim_data/15m/`.

## Disk safety (app + host)

**App (`sim/`):**
- `record_ticks: false` by default (results.jsonl only)
- Prune ticks after **6h**, trades **7d**
- Cap `sim_data/` ~**150MB**; want ~**200MB** free
- On ENOSPC: stop ticks/trades, prune, no traceback spam
- Unresolved markets do not count as full-entry strategy losses in summary

**Host (GCP):** the July 2026 outage was **`/var/log` ≈ 5GB**, not `sim_data`.
Install journal caps from `deploy/journald-size.conf` — see `deploy/DISK_OPS.md`.

### If the VM disk is full

```bash
sudo systemctl stop polyshadow
df -h
sudo du -xh / --max-depth=1 2>/dev/null | sort -h
sudo du -xh /var/log --max-depth=2 2>/dev/null | sort -h | tail -20
sudo journalctl --vacuum-size=20M
sudo find /var/log -type f -size +50M -exec truncate -s 0 {} \;
sudo apt-get clean
df -h /
git pull
sudo systemctl start polyshadow
journalctl -u polyshadow -n 30 --no-pager
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
sudo mkdir -p /etc/systemd/journald.conf.d
sudo cp deploy/journald-size.conf /etc/systemd/journald.conf.d/size.conf
sudo systemctl restart systemd-journald
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
