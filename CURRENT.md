# CURRENT.md — Live probe status

**Agents: read this after `AGENTS.md`.** Update this file when ops/strategy decisions change.
Do not put secrets, API keys, or live wallet material here.

Last updated: **2026-08-19** — keep the **$2.50 / 75–90¢** CLOB buy triggers
(band unchanged); pin FAK buys to share-capped limits at the quoted ask so leftover
USDC cannot walk into 9¢ junk. Hedge **trigger** is still 35/40; the sell follows
the live bid after that (no 32¢ FAK floor). Pause minting; pathlog records books
for entry backtests (14-day / 400 MB cap).

---

## What we’re doing

**Mint-only helper is paused.** Stop `polymintbot` and leave it disabled. Do not
mint complete sets. Operator still sells leftover mint inventory by hand.

**Active strategy:** the three CLOB buy bots with the **$2.50 widen-band
triggers** (not the old 98–99¢ probe):

| Knob | Value |
|---|---|
| `buy_budget` | **$2.50** / market |
| Ask band | **75–90¢** — trigger as soon as winning ask ≥ 75¢; 90¢ is a hard ceiling |
| Execution | FAK **limit** at the quoted ask, size `min(budget/ask, ask_size)` shares — **not** a USDC market order |
| GUI consensus | winner ≥ 70¢, loser ≤ 30¢ |
| Windows | 5m **120 s** · 15m **4.0 min** · hourly **13.0 min** |
| Hedge | **Trigger** bid ≤ **35¢** and ask ≤ **40¢**, spread ≤ 15¢ (real book, not a glitch). **Then sell at whatever the bid is** — no 32¢ floor. |
| Underlying edge | **$5** (5m) / **$10** (15m, hourly); side must match |
| `max_open_positions` | **0 = unlimited** |
| `toxic_force_exit_below` | **65¢** |

**Also running (no orders):** `pathlog.py` (`polypathlog`) writes one JSONL file
per market under `pathlog/ticks/` so we can ask “if we had bought at 80¢ with
2 minutes left, would we have won?”

---

## Pathlog / backtest

Recorder samples CLOB top-of-book ~1/s in the late window (whole 5m; last 8m of
15m; last 15m of hourly). After expiry it stamps `winner` from Gamma.

**Disk cap (small ~10GB VM):** keep ticks **14 days** and at most **400 MB**.
Oldest JSONL is deleted first; files written in the last 2 minutes are skipped.
Look for `pathlog_prune` in `pathlog.log`. This is **not** bot state — prune
**deletes** the files. **Export before that.**

On the VM:

```bash
sudo cp deploy/polypathlog.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now polypathlog
journalctl -u polypathlog -f

# Export regularly (copy CSVs off the VM; prune will delete the JSONL)
.venv/bin/python check_path_backtest.py --grid --budget 2.5 --series 5m --csv /tmp/hits.csv
.venv/bin/python check_path_backtest.py --export-market btc-updown-5m-1786528500 --csv /tmp/m.csv
scp ntemusejoel@<vm>:/tmp/hits.csv .
# or: scp -r ntemusejoel@<vm>:~/poly-money-maker/pathlog/ticks ./pathlog-export-$(date -u +%Y%m%d)
```

`--grid` is the Excel-shaped table: ask × seconds-left, hit count, win rate,
hypothetical $2.50 PnL (win = redeem $1, loss = −$2.50, no hedge model).

Kill switch: `touch STOP_PATHLOG`.

---

## Ops

- **VM:** `~/poly-money-maker` on `instance-20260516-185922`.
- **Mint:** `sudo systemctl stop polymintbot && sudo systemctl disable polymintbot`
- **Buy bots:** live `strategy_buy*.json` already had these 75–90 / $2.50 knobs
  before minting. After pull, confirm they still match the table above, then:
  ```bash
  cd ~/poly-money-maker && git pull
  sudo systemctl restart polybuybot polybuybot5m polybuybothourly
  ```
  Confirm `dry_run` / `entry_enabled` before restarting live.
- **Pathlog:** start `polypathlog` as above (no `.env` required).

---

## Open / next

- [x] Pause minting; keep $2.50 / 75–90¢ CLOB triggers.
- [x] Pin BUY FAKs to share-capped limits at the quoted ask (band unchanged).
- [x] Path recorder + `check_path_backtest.py` (first-touch ask × time-left).
- [x] Hedge FAK follows live bid after 35/40 integrity (no 32¢ fill refusal).
- [ ] On VM: stop mint, restart buy bots + pathlog after reviewing live JSON.
- [ ] Let pathlog collect resolved markets, then `--grid` / export CSV **off the VM** before prune.

---

## Agent instructions

1. Read `AGENTS.md` + this file before changing mint/buy/hedge logic.
2. Do **not** restart minting unless the operator asks.
3. Never truncate state/PnL/log files; never commit live strategy/state/`.env`.
   Pathlog ticks are **auto-pruned** (14d / 400 MB) — do not `rm` them by hand,
   but **do export** (`check_path_backtest.py --csv` or `scp` the ticks dir)
   before the cap deletes old JSONL.
4. When an ops decision changes, **update this file in the same PR/commit**.
