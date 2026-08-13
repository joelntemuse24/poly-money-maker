# CURRENT.md — Live probe status

**Agents: read this after `AGENTS.md`.** Update this file when ops/strategy decisions change.
Do not put secrets, API keys, or live wallet material here.

Last updated: **2026-08-13** — **$2.50 / 75–90¢ live on 5m only**; pathlog still
records 5m + 15m + hourly; minting paused.

---

## What we’re doing

**Mint-only helper is paused.** Stop `polymintbot` and leave it disabled. Do not
mint complete sets. Operator still sells leftover mint inventory by hand.

**Live $2.50 run: 5m only** (`polybuybot5m`). Same widen-band triggers as before.
**Do not start** `polybuybot` (15m) or `polybuybothourly`. Those stay stopped.

**Pathlog keeps polling all three cadences** (5m, 15m, hourly) so we still get
price paths on the markets we are not trading.

| Knob | 5m live |
|---|---|
| `buy_budget` | **$2.50** / market |
| Ask band | **75–90¢** — trigger as soon as winning ask ≥ 75¢; 90¢ is a hard ceiling |
| GUI consensus | winner ≥ 70¢, loser ≤ 30¢ |
| Window | **120 s** |
| Hedge | bid ≤ **35¢** and ask ≤ **40¢**, spread ≤ 15¢ |
| Underlying edge | **$5**; side must match |
| `max_open_positions` | **0 = unlimited** |
| `toxic_force_exit_below` | **65¢** |

**Also running (no orders):** `pathlog.py` (`polypathlog`) writes one JSONL file
per market under `pathlog/ticks/` so we can ask “if we had bought at 80¢ with
2 minutes left, would we have won?”

---

## Pathlog / backtest

Recorder samples CLOB top-of-book ~1/s in the late window (whole 5m; last 8m of
15m; last 15m of hourly). After expiry it stamps `winner` from Gamma.

On the VM:

```bash
sudo cp deploy/polypathlog.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now polypathlog
journalctl -u polypathlog -f

python check_path_backtest.py --ask-min 0.80 --ask-max 0.85 --ttm-max 120 --budget 2.5 --series 5m
python check_path_backtest.py --grid --budget 2.5 --series 5m
python check_path_backtest.py --grid --budget 2.5 --series 15m
python check_path_backtest.py --export-market btc-updown-5m-1786528500 --csv /tmp/m.csv
```

`--grid` is the Excel-shaped table: ask × seconds-left, hit count, win rate,
hypothetical $2.50 PnL (win = redeem $1, loss = −$2.50, no hedge model).

Kill switch: `touch STOP_PATHLOG`.

---

## Ops

- **VM:** `~/poly-money-maker` on `instance-20260516-185922`.
- **Mint:** `sudo systemctl stop polymintbot && sudo systemctl disable polymintbot`
- **15m / hourly buy bots:** stay **stopped + disabled** (pathlog still records them).
  ```bash
  sudo systemctl stop polybuybot polybuybothourly
  sudo systemctl disable polybuybot polybuybothourly
  ```
- **5m buy bot only** for the $2.50 run. Confirm `strategy_buy5m.json` is 75–90 /
  $2.50, `dry_run`/`entry_enabled` as intended, then:
  ```bash
  cd ~/poly-money-maker && git pull
  sudo systemctl restart polybuybot5m
  ```
- **Pathlog:** start `polypathlog` as above (no `.env` required). It records 5m,
  15m, and hourly even while only 5m is trading.

---

## Open / next

- [x] Pause minting; keep $2.50 / 75–90¢ CLOB triggers.
- [x] Path recorder + `check_path_backtest.py` (first-touch ask × time-left).
- [ ] On VM: stop mint + 15m/hourly buy bots; start pathlog; restart **5m only**.
- [ ] Let pathlog collect resolved markets, then run `--grid` before changing bands.

---

## Agent instructions

1. Read `AGENTS.md` + this file before changing mint/buy/hedge logic.
2. Do **not** restart minting or the 15m/hourly buy bots unless the operator asks.
3. Never truncate state/PnL/log/pathlog files; never commit live strategy/state/`.env`.
4. When an ops decision changes, **update this file in the same PR/commit**.
