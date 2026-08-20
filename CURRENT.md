# CURRENT.md — Live probe status

**Agents: read this after `AGENTS.md`.** Update this file when ops/strategy decisions change.
Do not put secrets, API keys, or live wallet material here.

Last updated: **2026-08-20** — **only the 5m CLOB bot is live.** Stop/disable
15m and hourly. Keep the **$2.50 / 75–90¢** triggers on 5m. Chainlink TWAP
gate is **$0** (any non-zero tick vs PTB; side must match; flat is still
refused). Buys are **limit
FAKs at the quoted ask** sized `budget/ask` (~3.3 shares at 75¢), clipped to
`buy_max_shares` **5** (buffer). Hard spend ceiling `buy_max_spend` **$3**.
Leftover USDC cannot walk into 9¢ junk. Displayed top size is **not** a cap.
Hedge **trigger** is still 35/40, then the **same GUI/last-trade consensus as
buy** (not a random TOB fill), then sell at the live bid. `toxic_fill` dumps
without GUI only while bid ≤ 35¢. Pause minting.
Pathlog still records all three series (no orders; 14-day / 400 MB cap).

---

## What we’re doing

**Mint-only helper is paused.** Stop `polymintbot` and leave it disabled. Do not
mint complete sets. Operator still sells leftover mint inventory by hand.

**15m and hourly CLOB bots are stopped.** Do not start `polybuybot` or
`polybuybothourly` unless the operator asks. Open 15m/hourly inventory (if any)
will not be auto-hedged or redeemed by those processes.

**Active strategy:** **5m only** (`polybuybot5m`) with the **$2.50 widen-band
triggers** (not the old 98–99¢ probe):

| Knob | Value |
|---|---|
| `buy_budget` | **$2.50** / market |
| `buy_max_spend` | **$3.00** hard ceiling (strategy is $2.50; never more than ~$3) |
| `buy_max_shares` | **5** buffer (~3.3 sh at $2.50/75¢) |
| Ask band | **75–90¢** — trigger as soon as winning ask ≥ 75¢; 90¢ is a hard ceiling |
| Execution | FAK **limit** at the quoted ask, size `min(budget/ask, buy_max_shares)`. A clean **unmatched 400** re-quotes up to **3** FAKs in one trigger; then **0.15 s** cooldown. Unclear POSTs still quarantine (no second $2.50). |
| GUI consensus | winner ≥ 70¢, loser ≤ 30¢ |
| Windows | 5m **120 s** (15m / hourly bots **not running**) |
| Hedge | **Trigger** bid ≤ **35¢** and ask ≤ **40¢**, spread ≤ 15¢, **plus** inverted buy GUI (held last trade ≤ 40¢, held GUI ≤ 30¢, other GUI ≥ 70¢). **Then sell at whatever the bid is** — no 32¢ floor. `toxic_fill` still dumps without GUI **only while held bid ≤ 35¢**; recovered books log `hedge_skip_toxic_recovered` and stay armed. |
| Underlying edge | **$0** (5m: any non-zero TWAP vs PTB) / **$10** (15m, hourly); side must match |
| `max_open_positions` | **0 = unlimited** |
| `toxic_force_exit_below` | **65¢** |

**Also running (no orders):** `pathlog.py` (`polypathlog`) writes one JSONL file
per market under `pathlog/ticks/` so we can ask “if we had bought at 80¢ with
2 minutes left, would we have won?” New ticks include displayed top size; the
backtest still *fills* `min(budget/ask, ask_size)` as a liquidity model (live
posts the full dollar size at that ask; unmatched remainder dies on the FAK).
Legacy ticks without size still assume a full fill at the best ask.

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
.venv/bin/python check_path_backtest.py --grid --budget 15 --series 5m
.venv/bin/python check_path_backtest.py --ask-min 0.75 --ask-max 0.90 --ttm-max 120 --budget 15 --series 5m --csv /tmp/hits_15.csv
.venv/bin/python check_path_backtest.py --export-market btc-updown-5m-1786528500 --csv /tmp/m.csv
scp ntemusejoel@<vm>:/tmp/hits.csv .
# or: scp -r ntemusejoel@<vm>:~/poly-money-maker/pathlog/ticks ./pathlog-export-$(date -u +%Y%m%d)
```

`--grid` is the Excel-shaped table: ask × seconds-left, hit count, **full /
partial / zero fills**, win rate, hypothetical PnL on **filled notional**
(win = redeem $1 per share, loss = −spent, no hedge model). Compare
`--budget 2.5` vs `--budget 15` on the same paths.

**Do not change live knobs by gut.** Pathlog records all three series whether
or not that bot is posting. Score the current rule and alternatives on the
same ticks, then change live JSON:

```bash
# Current 75–90¢ / last 120s vs earlier windows / wider bands
.venv/bin/python check_path_backtest.py --compare --series 5m --budget 2.5
.venv/bin/python check_path_backtest.py --compare --series 5m --budget 15

# Why we didn't buy: already decided at T-120 vs 50/50 until the end
.venv/bin/python check_path_backtest.py --anatomy --series 5m --ttm-max 120 --csv /tmp/anatomy.csv

# Live skip reasons (not a backtest — what this process actually logged)
.venv/bin/python check_buy_skips.py --since "$(date -u -d '6 hours ago' '+%Y-%m-%dT%H:%M:%S')"
```

`--anatomy` buckets: `decided_before_in_band` / `above_band` / `below_band`
(clear winner before the window), `tight_through_window` (never 5¢ apart),
`cleared_in_window` (first became obvious only after T-120). `--series 5m`
is **only** 5-minute markets (`15m` contains the letters `5m` — do not use a
raw substring). Pathlog has no last-trade GUI and no BTC/PTB — those still
need `check_buy_skips.py` / `check_edge_counterfactual.py`.

Kill switch: `touch STOP_PATHLOG`.

---

## Cycle abort (Aug 13–19) — now isolated per market

Since **2026-08-13T04:33Z** the 5m log has **3294 `cycle_error`s**, all one
exception: `NameError: name 'known_cost' is not defined`. Disk was not full.
That exception was the outer poll `except`: **the rest of that cycle’s buys
and hedges were skipped.** One quarantined `buy_uncertain` market was enough.
Hedge/buy work is now wrapped **per market**; a later `cycle_error` logs
`condition_id` and continues the poll. Outer `cycle_error` still means
refresh/GC/redeem/UI failed, not “remaining markets skipped.”

The assignment-order fix is **#80** (`be22662`, merged 05:28Z the same morning).
CI `git pull`s bot code but **does not restart systemd**. The running 5m
process kept the old bytecode until the operator restart at **09:42Z on
2026-08-19**. After that restart, `known_cost` is assigned before `spend_cap`
uses it. Confirm with:

```bash
# After 09:42Z restart — expect 0 NameError lines
.venv/bin/python check_buy_skips.py --since 2026-08-19T09:42:23
```

`buy_attempt_rejected` (828 since Aug 13) is a **separate** bucket (invalid
amount / HTTP 400), not this NameError.

---

## Ops

- **VM:** `~/poly-money-maker` on `instance-20260516-185922`.
- **Mint:** `sudo systemctl stop polymintbot && sudo systemctl disable polymintbot`
- **Buy bots:** **5m only.** Leave 15m/hourly stopped. `buy_max_spend` /
  `buy_max_shares` may be omitted from live JSON — defaults **$3** / **5 shares**
  apply. Live JSON **must** set `min_underlying_edge_usd` to **0.0** (hot
  reload, no restart). Code default is 0.0 only when the key is omitted.
  After pulling bot code, restart **5m only**:
  ```bash
  sudo systemctl stop polybuybot polybuybothourly
  sudo systemctl disable polybuybot polybuybothourly
  cd ~/poly-money-maker && git pull
  python3 -c 'import json; from pathlib import Path; p=Path("strategy_buy5m.json"); d=json.loads(p.read_text()); d["min_underlying_edge_usd"]=0.0; p.write_text(json.dumps(d, indent=2)+"\n"); print("min_underlying_edge_usd", d["min_underlying_edge_usd"])'
  sudo systemctl restart polybuybot5m
  sudo systemctl enable polybuybot5m
  systemctl is-active polybuybot polybuybot5m polybuybothourly
  # expect: inactive  active  inactive
  .venv/bin/python check_buy_skips.py --since "$(date -u -d '2 hours ago' '+%Y-%m-%dT%H:%M:%S')"
  ```
  Confirm `strategy_buy5m.json` `dry_run` / `entry_enabled` before the 5m restart.
  Do **not** start 15m or hourly unless the operator asks.
- **Pathlog:** start `polypathlog` as above (no `.env` required).

---

## Open / next

- [x] Pause minting; keep $2.50 / 75–90¢ CLOB triggers.
- [x] **5m-only live trading** — 15m and hourly buy services stopped/disabled.
- [x] Pin BUY FAKs to **budget/ask limit** at the quoted ask (band unchanged;
  displayed top size is not a cap; `buy_max_spend` $3; `buy_max_shares` 5 buffer).
- [x] Path recorder + `check_path_backtest.py` (first-touch ask × time-left;
  ticks record TOB size; backtest is share-capped FAK, not infinite ask size).
- [x] Hedge FAK follows live bid after 35/40 integrity (no 32¢ fill refusal).
- [x] On VM: pathlog restarted onto size-aware ticks; 15m/hourly buy bots stopped.
- [x] 5m underlying gate **$0** (any non-zero TWAP vs PTB; side must match).
- [x] Pathlog `--anatomy` / `--compare` so window/band/size alts are scored offline.
- [x] 5m `known_cost` NameError (#80) — **code** fixed 13 Aug; live process
      needed restart. Confirm `check_buy_skips.py --since 2026-08-19T09:42:23`
      shows **0** `NameError` cycle_errors.
- [x] Faster **proven-empty** FAK retries (unmatched 400 only; 0.15 s empty cooldown).
      After merge: `git pull` + `sudo systemctl restart polybuybot5m` (5m only).
      Live JSON does **not** need a new key — `empty_fak_cooldown_s` defaults to 0.15.
- [ ] After merge: `git pull` then `sudo systemctl restart polybuybot5m` only
      (toxic recovered-book skip + per-market `cycle_error` isolation). Watch
      `hedge_skip_toxic_recovered`, `hedge_skip_no_consensus`, `cycle_error`
      **with** `condition_id` and later `buy_fill`s in the same second.
- [ ] Snapshot `pathlog/ticks` (+ `buybot5m.log`) into a Cursor Cloud
      Environment and run the prompts in `CLOUD_RESEARCH.md` (`--sweep`,
      paper hedge). Not live. Refresh the snapshot before prune.
- [ ] Let pathlog collect resolved markets, then `--anatomy` / `--compare` / `--sweep` **off the VM** before prune.

---

## Agent instructions

1. Read `AGENTS.md` + this file before changing mint/buy/hedge logic.
2. Do **not** restart minting unless the operator asks. Do **not** start
   `polybuybot` / `polybuybothourly` unless the operator asks.
3. Never truncate state/PnL/log files; never commit live strategy/state/`.env`.
   Pathlog ticks are **auto-pruned** (14d / 400 MB) — do not `rm` them by hand,
   but **do export** (`check_path_backtest.py --csv` or `scp` the ticks dir)
   before prune. Cloud research: `CLOUD_RESEARCH.md`.
4. When an ops decision changes, **update this file in the same PR/commit**.
