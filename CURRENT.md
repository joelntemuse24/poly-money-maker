# CURRENT.md — Live probe status

**Agents: read this after `AGENTS.md`.** Update this file when ops/strategy decisions change.
Do not put secrets, API keys, or live wallet material here.

Last updated: **2026-08-26** — hourly dump **≤32¢** is book-only
(`hedge_dump_ignore_oracle`); persist-50 still needs BTC against/flat.
Restart `polybuybothourly` after merge and patch live JSON dump/oracle
keys. Strategy otherwise unchanged from **2026-08-25**: **stop the 5m bot.
Live buying is hourly.**
One slice: **75–90¢ in the last 20 minutes** of the BTC hourly Up/Down
market, **$10** cap, FAK limit **90¢**. Hedge is **persist 5s @ 50/52**
(GUI held ≤ **52¢** / other ≥ **48¢** — not inverted 30/70), then sell at
the **live bid** while it is still **< 53¢** (no 2¢ undercut). A fade
through 50 after persist still sells (`hedge_sell_fade`). Bid ≥ **53¢**
(`hedge_recovery_cancel`) holds and clears persist — do **not** sell 55–69
the way 5m sold 70–84. **Once holding, do not persist-sell while live
Binance BTC is still on the held side of PTB** (`hedge_require_oracle`) —
a one-tick 50¢ CLOB dip is not a hedge if the resolution oracle still
agrees. Missing or stale BTC also holds persist. **Any live bag** dumps
bid-only while bid ≤ **32¢** even if BTC has not crossed yet. Underlying
*buy* edge stays **$10**. Tick **0.01**. Windows are **minutes**. Pause
minting.
Pathlog records all three series (hourly now samples the last **20 min**;
14-day / 400 MB cap).

**Why the 5m hedge disappointed:** the live 5m gate sometimes did what we
wanted and often did not. Volatility made that worse, but the knobs also
fought us:

1. **Fired on markets that then won** — persist was only **2s** at 70/72.
   A one-tick (or two-second) dip dumped a winner.
2. **Failed to fire, then we lost** — dump needed a **≤53¢** bid. The
   **(53, 70)** dead band held fading losers until they hit 53, and some
   never printed a dumpable book.
3. **Sold way above 53** — after persist, **70–84** live-bid fills were
   treated as correct (`hedge_recovery_cancel` was **85¢**). That is the
   opposite of “hedge at 53.”

Hourly is slower on purpose so a 5s persist has time to distinguish a dip
from a reversal. Recovery is **53¢** so persist-done does **not** sell
50–69. `sell_fade` sells a post-persist fade through 50 instead of waiting
for 35. **2026-08-26:** the live executor now actually posts that fade,
rechecks Binance/PTB and TTM immediately before every BUY/SELL POST or
retry, completes the 5s persist only on a still-qualified 50/52+GUI tick,
accepts the 50/48 hedge GUI boundary, and refuses re-entry after
`hedge_closed`. Python changes are not hot-reloaded.

**Hourly also keeps the later 5m execution fixes** (the old hourly path
was still instant 55/60, inverted GUI 30/70, undercut 2, incomplete-REST
skip). Those bugs are separate from the 5m *threshold* complaint.

---

## What we’re doing

**Mint-only helper is paused.** Stop `polymintbot` and leave it disabled. Do not
mint complete sets. Operator still sells leftover mint inventory by hand.

**5m and 15m CLOB bots are stopped.** Do **not** start `polybuybot5m` or
`polybuybot`. Open 5m inventory (if any) will not be auto-hedged by this
process — redeem leftovers by hand or leave them for $1 settlement.

**Active strategy:** **hourly only** (`polybuybothourly`) after the operator
copies knobs into live `strategy_buyhourly.json` and starts the unit.
Same-leg only. After `hedge_closed`, no re-buy. A/C slices are **off**
(`a22_window_min` / `c5_window_min` = 0). Do **not** re-enable >93 / >95.

| Knob | Value |
|---|---|
| `b15_window_min` / `buy_window_min` | **20** minutes |
| Ask band | **75–90¢** inclusive, FAK limit **90¢**. 91–99¢ is a no. |
| `b15_buy_budget` / `market_spend_cap` | **$10** per market |
| `buy_max_spend` / `buy_max_shares` | **$11** / **14** per FAK |
| `a22_window_min` / `c5_window_min` | **0** = disabled |
| Hedge qualify | bid ≤ **50¢**, ask ≤ **52¢**, spread ≤ 15¢, persist **5s** |
| Hedge GUI | held ≤ **52¢**, other ≥ **48¢** (complement of ask-max). Buy 70/30 unchanged. Last print ≤ 52¢. |
| Oracle while holding | Do **not persist-sell** if live BTC is still on the held side of PTB (any non-zero tick). Missing/stale feed holds persist. `hedge_skip_oracle_still_winning` / `hedge_skip_oracle`. |
| After persist | Sell at the live bid while **< 53¢**, including a fade through 50 (`hedge_sell_fade`). Bid ≥ **53¢** holds and clears persist. Do **not** sell 55–69. |
| Dump | Bid-only ≤ **32¢** even if BTC still agrees (`hedge_dump_ignore_oracle`). Wide 20/80 still dumps. Persist-50 does **not** get this bypass. |
| Execution | Sell at the **live bid**, undercut **0**. Unmatched 400 re-quotes; invalid-tick rebuilds at 0.01. Incomplete REST uses WS / last-good. |
| Underlying edge | **$10** (Binance BTCUSDT vs PTB); side must match |
| `max_open_positions` | **0 = unlimited** |
| `poll_buy_window_s` / `poll_held_s` | **0.01** on the live hourly WS book |

**Also running (no orders):** `pathlog.py` (`polypathlog`) writes one JSONL file
per market under `pathlog/ticks/`. Hourly ticks now cover the last **20 min**.

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
- **Buy bots:** **hourly only.** Stop/disable 5m and 15m. After this branch
  merges, on the VM:
  ```bash
  sudo systemctl stop polybuybot polybuybot5m
  sudo systemctl disable polybuybot polybuybot5m
  cd ~/poly-money-maker && git pull
  python3 -c 'import json; from pathlib import Path; p=Path("strategy_buyhourly.json"); d=json.loads(p.read_text()); d["buy_window_min"]=20.0; d["a22_window_min"]=0.0; d["b15_window_min"]=20.0; d["c5_window_min"]=0.0; d["buy_threshold"]=0.75; d["buy_max_price"]=0.90; d["b15_buy_budget"]=10.0; d["market_spend_cap"]=10.0; d["buy_budget"]=10.0; d["buy_max_spend"]=11.0; d["buy_max_shares"]=14.0; d["hedge_threshold"]=0.50; d["hedge_require_ask_max"]=0.52; d["hedge_persist_s"]=5.0; d["hedge_toxic_bid_max"]=0.32; d["hedge_recovery_cancel"]=0.53; d["hedge_sell_fade"]=True; d["hedge_require_oracle"]=True; d["hedge_dump_ignore_oracle"]=True; d["hedge_oracle_min_edge_usd"]=0.0; d["hedge_undercut_ticks"]=0; d["poll_buy_window_s"]=0.01; d["poll_held_s"]=0.01; p.write_text(json.dumps(d, indent=2)+"\n"); print("window", d["b15_window_min"], "hedge", d["hedge_threshold"], d["hedge_require_ask_max"], "persist", d["hedge_persist_s"], "dump", d["hedge_toxic_bid_max"], "recovery", d["hedge_recovery_cancel"], "fade", d["hedge_sell_fade"], "oracle", d["hedge_require_oracle"], "dump_ignore_oracle", d["hedge_dump_ignore_oracle"], "undercut", d["hedge_undercut_ticks"], "dry_run", d.get("dry_run"), "entry", d.get("entry_enabled"))'
  sudo systemctl restart polypathlog
  sudo systemctl start polybuybothourly
  sudo systemctl enable polybuybothourly
  systemctl is-active polybuybot polybuybot5m polybuybothourly
  # expect: inactive  inactive  active
  ```
  Confirm `strategy_buyhourly.json` `dry_run` / `entry_enabled` **before**
  start. New Python needs an hourly restart. Live JSON **must** set the
  hedge keys or an old 55/60 file keeps instant 55/60 after hot reload.
  Pathlog last-20-min window needs `sudo systemctl restart polypathlog`.
  Do **not** start 5m, 15m, or mint.
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
      5m **code default** is now persist **2s @ 70/72** (toxic still **53¢**);
      hourly template is **55/60** (still stopped); 15m stays 35/40 (stopped).
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
- [ ] Cloud paper P&L: paste `CLOUD_RESEARCH.md` section 2. Score
      `--series hourly --template strategy_buyhourly.example.json --paper`.
      No `.env`.
- [ ] Let pathlog collect resolved **hourly** markets (last 20 min ticks),
      then `--anatomy` / `--compare` / `--sweep --series hourly` **off the VM**
      before prune.

---

## Agent instructions

1. Read `AGENTS.md` + this file before changing mint/buy/hedge logic.
2. Do **not** restart minting unless the operator asks. Do **not** start
   `polybuybot` / `polybuybot5m` unless the operator asks. Hourly is the
   live buy bot after the operator copies knobs and starts the unit.
3. Never truncate state/PnL/log files; never commit live strategy/state/`.env`.
   Pathlog ticks are **auto-pruned** (14d / 400 MB) — do not `rm` them by hand,
   but **do export** (`check_path_backtest.py --csv` or `scp` the ticks dir)
   before prune. Cloud research: `CLOUD_RESEARCH.md`.
4. When an ops decision changes, **update this file in the same PR/commit**.
