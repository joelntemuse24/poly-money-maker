# CURRENT.md — Live probe status

**Agents: read this after `AGENTS.md`.** Update this file when ops/strategy decisions change.
Do not put secrets, API keys, or live wallet material here.

Last updated: **2026-08-27** — live 5m is still **last 45s + $25** (already
on the VM). Pathlog paper + Binance `$25` shows that combo is **empty/−EV
on restable CLOB books**. Analog +$1.53/h was implied 99/1 fills, not a
FAK. **Do not re-paste last-45+$25.** Hourly is stopped. Hedge stays
**persist 5s @ 50/52**, recovery **53¢**, dump **≤32¢**. **Do not add a
vol/momentum buy skip.**

**On the box now** (do not treat analog as CLOB evidence):

| Knob | Value |
|---|---|
| Entry time | last **45s** (`buy_start_s=45`, `late_90_start_s=45`) |
| Ask | **75–99¢** (late 75–90 + `late_90` overlay) |
| `min_underlying_edge_usd` | **$25** |
| Early / ≥95 | **off** |
| Size | **$2.50** |
| Hedge | persist 5s @ 50/52, dump 32, recovery 53 |
| Look / WS | `BUY_HORIZON_S` **45s** |

**Recommended next probe** (operator must paste; not live until then):
**last 120s, 75–90¢ only, `min_underlying_edge_usd=0`, `late_90` off,
early off, $2.50**, same hedge. Printed `horizon` must be **120**, `edge`
**0.0**, `late_90` **0**.

### Pathlog books (the FAK tape)

Same 5m ticks, ~**3552 markets / ~296 h**, paper 50/52 persist 5s, spread
≤5¢, budget **$10**, ask **75–90¢**. `--min-edge-usd 25` = Binance 1s
PTB/live (not Chainlink TWAP). `/tmp/pathlog_oracle/run.log` 27 Aug.

| Window | `$25` | Book hits | Keeps | WR | P&L | Hedges | $/h |
|---|---|---:|---:|---:|---:|---:|---:|
| last 45s | yes | 36 | **3** | 66.7% | **−$7.39** | 0 | **−$0.03** |
| last 90s | yes | 261 | 35 | 97.1% | +$56.66 | 0 | +$0.19 |
| last 120s | yes | 483 | 87 | 94.3% | +$107.31 | 1 | +$0.36 |
| last 45s | no | 38 | 38 | 94.6% | +$50.17 | 0 | +$0.17 |
| last 90s | no | 264 | 264 | 93.1% | +$297.52 | 0 | +$1.01 |
| last **120s** | **no** | 489 | **489** | **91.5%** | **+$467.78** | 4 | **+$1.58** |

`$25` ∩ last-45 restable books is 3 fills in 12 days (live 0 attempts).
`$25` ∩ last-120 keeps 18% of books and 23% of P&L — WR 91.5%→94.3%,
hourly rate **$1.58→$0.36**. Hedge almost never fires; reversals are in
the win rate. 8.5% flips is eatable at a 75–90¢ fill (no-hedge cap is
`1−fill`). Analog last45+$25 (~11 implied fills/h at ~91¢) is the 99/1
book this FAK cannot take.

**$1/h lever is size + last-120, not `$25`.** At **$2.50** last-120 no
edge is ~**+$0.41/h**. `$5` ~+$0.80/h. `$10` is the +$1.58 line (97–98%
of 4× $2.50; few zeros). Stay **$2.50** until this next paste is on the
box and filling. Do not enable `late_90` on the first switch (that ≥90
overlay was **not** in this paper).

Analog Binance-only first-touch (implied fill from `|dist|`, not a
restable ask) — keep for the 99/1 research, **do not pick live knobs
from it**:

| Sample | last45+$25 | same at $5 | flip | last120+$25 | early+$25 |
|---|---:|---:|---:|---:|---:|
| 72h 1s | +$1.53/h | +$3.06/h | 5.1% | −$0.27/h | −$3.17/h |
| 7d 1s | +$1.53/h | +$3.05/h | 4.8% | −$0.21/h | −$3.20/h |
| 14d 1s | +$1.41/h | +$2.83/h | 3.8% | +$0.41/h* | −$1.93/h |

\*14d analog last-120+$25 is +EV only with `$1` salvage.

**Why we left 70/72 persist-2s / recovery 85 / dump 53:**

1. **Fired on markets that then won** — persist was only **2s** at 70/72.
2. **Failed to fire, then we lost** — dump needed a **≤53¢** bid; the
   **(53, 70)** dead band held fading losers.
3. **Sold way above 53** — after persist, **70–84** fills were treated as
   correct (`hedge_recovery_cancel` was **85¢**).

Those knobs stay gone. Persist is **5s @ 50/52**, recovery **53¢**
(do not sell 55–69), dump **≤32¢** even if BTC has not crossed yet, and
persist-50 still needs the oracle against/flat.

**Next paste** (5m only; do not start hourly/15m/mint). `early_buy_start_s`
must stay ≥ `buy_start_s` and positive — **120 / 120** makes the early
window empty. `late_90_start_s=0` and `min_underlying_edge_usd=0`.
Confirm printed `dry_run` / `entry` **before** restart. Printed `horizon`
must be **120**, `edge` **0.0**, `late_90` **0**.

```bash
cd ~/poly-money-maker && git pull
sudo systemctl stop polybuybothourly polybuybot
sudo systemctl disable polybuybothourly polybuybot
python3 -c 'import json; from pathlib import Path; p=Path("strategy_buy5m.json"); d=json.loads(p.read_text()); d["hedge_threshold"]=0.50; d["hedge_require_ask_max"]=0.52; d["hedge_persist_s"]=5.0; d["hedge_toxic_bid_max"]=0.32; d["hedge_recovery_cancel"]=0.53; d["hedge_sell_fade"]=True; d["hedge_require_oracle"]=True; d["hedge_dump_ignore_oracle"]=True; d["hedge_oracle_min_edge_usd"]=0.0; d["hedge_undercut_ticks"]=0; d["hedge_min_price"]=0.32; d["buy_start_s"]=120; d["early_buy_start_s"]=120; d["early_95_start_s"]=0; d["early_95_min_s"]=0; d["late_90_start_s"]=0; d["min_underlying_edge_usd"]=0.0; d["add_min_price"]=0.90; d["buy_budget"]=2.5; d["late_buy_budget"]=2.5; d["buy_max_price"]=0.90; d["early_buy_max_price"]=0.99; d["buy_max_spend"]=3.0; d["buy_max_shares"]=5.0; d["poll_buy_window_s"]=0.01; d["poll_held_s"]=0.01; d["ui_every_n_cycles"]=50; p.write_text(json.dumps(d, indent=2)+"\n"); print("start", d["buy_start_s"], "early", d["early_buy_start_s"], "e95", d["early_95_start_s"], "e95min", d["early_95_min_s"], "edge", d["min_underlying_edge_usd"], "late_90", d["late_90_start_s"], "horizon", max(d["buy_start_s"], d["early_buy_start_s"], d["early_95_start_s"]), "hedge", d["hedge_threshold"], d["hedge_require_ask_max"], "persist", d["hedge_persist_s"], "dump", d["hedge_toxic_bid_max"], "recovery", d["hedge_recovery_cancel"], "dry_run", d.get("dry_run"), "entry", d.get("entry_enabled"))'
sudo systemctl restart polybuybot5m
sudo systemctl enable polybuybot5m
systemctl is-active polybuybot polybuybot5m polybuybothourly
# expect: inactive  active  inactive
```

---

## What we’re doing

**Mint-only helper is paused.** Stop `polymintbot` and leave it disabled. Do not
mint complete sets. Operator still sells leftover mint inventory by hand.

**15m and hourly CLOB bots are stopped.** Do **not** start `polybuybot` or
`polybuybothourly`.

**Active strategy:** **5m only** (`polybuybot5m`). Same-leg only. After
`hedge_closed`, no re-buy. Two $2.50 slices. **On the box until the
operator pastes last-120 / edge 0:**

| Knob | Value |
|---|---|
| Late window | last **45s**, **75–90¢**, FAK **90¢**, `$2.50` |
| Last-45 overlay | last **45s**, **≥90¢**, FAK **99¢**, still the late `$2.50` |
| Early / ≥95 | **off** |
| `add_min_price` | **90¢** for a same-leg late add |
| Hedge qualify | bid ≤ **50¢**, ask ≤ **52¢**, spread ≤ 15¢, persist **5s** |
| Hedge GUI | held ≤ **52¢**, other ≥ **48¢**. Buy 70/30 unchanged. Last print ≤ 52¢. |
| Oracle while holding | Do **not persist-sell** if live Chainlink TWAP is still on the held side of PTB. Missing/stale feed holds. |
| After persist | Sell at the live bid while **< 53¢**, including a fade through 50 (`hedge_sell_fade`). Bid ≥ **53¢** holds and clears persist. |
| Dump | Bid-only ≤ **32¢** even if BTC still agrees (`hedge_dump_ignore_oracle`). Persist-50 does **not** get this bypass. |
| Underlying buy edge | **$25** (`|TWAP−PTB|`); side must match. Pathlog: this ∩ restable last-45 is empty. |
| `BUY_HORIZON_S` | **45s** (WS from ~T-75) |
| `max_open_positions` | **0 = unlimited** |
| `poll_buy_window_s` / `poll_held_s` | **0.01** on the live 5m WS book |

**Also running (no orders):** `pathlog.py` (`polypathlog`) writes one JSONL file
per market under `pathlog/ticks/`.

---

## Pathlog / backtest

Recorder samples CLOB top-of-book ~1/s in the late window (whole 5m; last 8m of
15m; last 20m of hourly). After expiry it stamps `winner` from Gamma.

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
.venv/bin/python check_path_backtest.py --ask-min 0.75 --ask-max 0.99 --ttm-max 120 --budget 15 --series 5m --csv /tmp/hits_15.csv
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
# Current late 75–90¢ / last 120s vs earlier windows / wider bands
.venv/bin/python check_path_backtest.py --compare --series 5m --budget 2.5
.venv/bin/python check_path_backtest.py --compare --series 5m --budget 15

# Why we didn't buy: already decided at T-120 vs 50/50 until the end
.venv/bin/python check_path_backtest.py --anatomy --series 5m --ttm-max 120 --csv /tmp/anatomy.csv

# Live skip reasons (not a backtest — what this process actually logged)
.venv/bin/python check_buy_skips.py --since "$(date -u -d '6 hours ago' '+%Y-%m-%dT%H:%M:%S')"

# The live tape 4–5 hours later (no stream). Dedicated journal after the
# tick-fix restart; older sessions still read buybot5m.log.
.venv/bin/python check_live_journal.py --hours 5
# Exact Rich console, only while journald still has it (50 MB / 7d):
#   journalctl -u polybuybot5m --since "5 hours ago" --no-pager
```

`--anatomy` buckets: `decided_before_in_band` / `above_band` / `below_band`
(clear winner before the window), `tight_through_window` (never 5¢ apart),
`cleared_in_window` (first became obvious only after T-120). `--series 5m`
is **only** 5-minute markets (`15m` contains the letters `5m` — do not use a
raw substring). Pathlog has no last-trade GUI. Join Binance `|BTC−PTB|` with
`--min-edge-usd 25` on `check_path_backtest.py` (not Chainlink TWAP). Live skip
reasons still need `check_buy_skips.py`.

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
- **Buy bots:** **5m only.** Stop/disable hourly and 15m. After **#130**
  merges, paste the **same block as the top of this file** (last **45s** +
  edge **$25** + hedge 50/52). Confirm printed `horizon` **45**, `edge` **25**,
  `e95` **0**, `e95min` **0**, and `dry_run` / `entry` **before** restart.
  New Python needs a 5m restart. Live JSON **must** set the hedge and
  entry keys or an old 120/300/$0 file keeps the old windows after hot
  reload. Do **not** start 15m, hourly, or mint.
- **Pathlog:** start `polypathlog` as above (no `.env` required).

---

## Open / next

- [x] Pause minting; keep CLOB triggers (now two $2.50 slices, late 75–90).
- [x] **5m-only live trading** — 15m and hourly buy services stopped/disabled.
- [x] Pin BUY FAKs to **budget/ask limit** at the quoted ask (band unchanged;
  displayed top size is not a cap; `buy_max_spend` $3; `buy_max_shares` 5 buffer).
- [x] Path recorder + `check_path_backtest.py` (first-touch ask × time-left;
  ticks record TOB size; backtest is share-capped FAK, not infinite ask size).
- [x] Hedge FAK follows live bid after book integrity (no 32¢ fill refusal).
      Replaced later by persist 70/72, then by persist **5s @ 50/52** dump
      **32¢** (this return-to-5m change). 15m stays 35/40 (stopped).
- [x] **5m hedge unmatched-400 retry + sell at the 0.001 bid** (#115). Live
      21 Aug 16:00–00:34Z: **0 `hedge_fill`**. Every sell was attempt-1
      `400 no orders found to match` at `price_limit` = bid − 2¢ (CLOB tick
      0.01 × undercut 2). Merged on main. Needs the same 5m restart as the
      persist/add-min change below.
- [x] On VM: pathlog restarted onto size-aware ticks; 15m/hourly buy bots stopped.
- [x] 5m underlying gate **$0** (any non-zero TWAP vs PTB; side must match).
- [x] Pathlog `--anatomy` / `--compare` so window/band/size alts are scored offline.
- [x] 5m `known_cost` NameError (#80) — **code** fixed 13 Aug; live process
      needed restart. Confirm `check_buy_skips.py --since 2026-08-19T09:42:23`
      shows **0** `NameError` cycle_errors.
- [x] Faster **proven-empty** FAK retries (unmatched 400 only; 0.15 s empty cooldown).
      After merge: `git pull` + `sudo systemctl restart polybuybot5m` (5m only).
      Live JSON does **not** need a new key — `empty_fak_cooldown_s` defaults to 0.15.
- [x] 5m BUY FAKs size `budget/ask` and limit at the **open band max**
      (99¢ early, then 90¢ late after the two-slice change).
- [x] Faster look (0.01s) + drop leftover wallet dust — operator pulled
      `bd607db` / #108 and restarted **polybuybot5m** 21 Aug 2026 **08:08Z**.
      Banner `WAIT 8`, `sleeping 0.01s`, NAV ~$147. Dust drop worked.
- [x] **5m hedge bid 53¢** (`hedge_threshold=0.53`, ask max still **55¢**) —
      merged and was live. Replaced by persist 70/72 below (toxic dump stays 53¢).
- [x] **5m hedge GUI matches ask-max** (not inverted 30/70). Held display ≤
      ask-max, other ≥ complement. Buy 70/30 unchanged.
- [x] **5m persist hedge 2s @ 70/72 + late add-min 90¢.** Live process
      restarted **2026-08-22 03:16Z**. Banner `HEDGE ≤70¢`.
      `hedge_skip_persist` / `buy_skip_add_below_min` / `buy_skip_hedge_closed`
      are the skip lines. Do **not** start 15m / hourly / mint.
- [x] **5m hedge CLOB tick + live tape.** Honor market tick (0.01 when
      CLOB says so); `hedge_tick_retry` rebuilds at the stated minimum.
      Journal: `check_live_journal.py --hours 5`.
- [x] **22 Aug 2026 CURRENT rails actually fire.** 09:35 / 11:25 87¢
      late bags held to ~0 because dump was `toxic_fill`-only (avg 87¢).
      11:20 93¢ late TTM 116 POSTed (Gamma end vs slug+300; early 99¢
      FAK), then unmatched 47/55 → `hedge_fail` idle. Dead-band 58–61
      sells. Early 85–89 after restart. Fix: slug TTM + POST recheck;
      dump **any** bag ≤53¢ (bid-only); hold (53, 70); persist then
      live bid including 74–80; unmatched / invalid tick retry until
      flat or bid recovers; incomplete REST uses WS/last-good; ghosts
      do not stamp the slice or `hedge_closed`. **Restart required**
      after merge (`git pull` then `sudo systemctl restart polybuybot5m`).
      Confirm `dry_run` / `entry_enabled`. Do **not** start 15m /
      hourly / mint. Do **not** invent a 75/50 strategy.
- [x] **5m persist recovery-cancel 85¢.** After `persist_done`, 70–84 still
      sells at the live bid. Bid ≥ **85¢** (`hedge_recovery_cancel`) holds
      and **clears persist** (`hedge_skip_recovery`) so a 90–99¢ rally is
      not sold-then-won. Dump ≤53 and dead band (53, 70) unchanged.
      After merge: `git pull`, patch live JSON (or omit the key; default
      0.85), `sudo systemctl restart polybuybot5m`. Confirm `dry_run` /
      `entry_enabled`. Do **not** start 15m / hourly / mint.
- [x] **Hourly last-20m 75–90 + persist hedge 50/52.** Operator asked to
      stop 5m and buy a 75 in the last 20 minutes of the hourly market,
      hedge at 50. A/C slices off. 5m hedge complaint (fired on winners,
      missed dumps, sold 70–84) is **not** copied: persist **5s**,
      recovery **53¢**, `hedge_sell_fade` so a post-persist fade through
      50 still sells, and **`hedge_require_oracle`**: once holding, do not
      sell while live BTC is still on the held side of PTB (or the feed is
      missing/stale). Execution still uses `evaluate_held_bag` (undercut 0,
      dump ≤35 after oracle against/flat, unmatched/tick retry). After merge:
      `git pull`, patch live `strategy_buyhourly.json`, stop 5m, start hourly,
      restart pathlog. Confirm `dry_run` / `entry_enabled`.
- [x] **Back to $2.50 5m + last-45 ≥90 + false-hedge crackdown.** Operator
      asked to leave hourly and restart 5m two-slice $2.50 with the hourly
      hedge (persist 5s @ 50/52, recovery 53, fade, oracle on persist, dump
      ≤32 ignore oracle) and allow ≥90¢ in the last 45 seconds. 91–99 still
      a no at TTM 46–120. After merge: `git pull`, patch live
      `strategy_buy5m.json`, stop hourly, restart 5m. Confirm `dry_run` /
      `entry_enabled`. Do **not** start 15m / hourly / mint.
- [ ] Cloud paper P&L on **pathlog ~1s ticks** (not 1-minute CLOB candles):
      `--series 5m --template strategy_buy5m.example.json --paper`, plus
      last-45s `--ask-min 0.90 --ask-max 0.99 --ttm-max 45`. Export ticks
      off the VM before prune. No `.env`.
- [x] 27 Aug reversal-feature tape (`check_reversal_features.py`): no vol
      skip. Combo is **last 45s + `min_edge` $25**, early off, hedge
      unchanged, size $2.50 (then $5). Confirmed on **72h / 7d / 14d 1s**
      (closeTime stamps). Example JSON + the paste at the top of this file
      hold the knobs. $20–40 *bucket* 25% flip is not eatable at 85¢;
      last-120 + $25 is still −EV on 72h/7d 1s. Merge **#130**, then paste.

---

## Agent instructions

1. Read `AGENTS.md` + this file before changing mint/buy/hedge logic.
2. Do **not** restart minting unless the operator asks. Do **not** start
   `polybuybot` / `polybuybothourly` unless the operator asks. 5m is the
   live buy bot after the operator patches knobs and restarts the unit.
3. Never truncate state/PnL/log files; never commit live strategy/state/`.env`.
   Pathlog ticks are **auto-pruned** (14d / 400 MB) — do not `rm` them by hand,
   but **do export** (`check_path_backtest.py --csv` or `scp` the ticks dir)
   before prune. Cloud research: `CLOUD_RESEARCH.md`.
4. When an ops decision changes, **update this file in the same PR/commit**.
