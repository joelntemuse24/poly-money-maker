# CURRENT.md — Live probe status

**Agents: read this after `AGENTS.md`.** Update this file when ops/strategy decisions change.
Do not put secrets, API keys, or live wallet material here.

Last updated: **2026-08-22** — **only the 5m CLOB bot is live.** It watches
the **current** 5m market (and a live hedge if we just bought). Polymarket’s
positions API still returns hundreds of old 5m rows (`WAIT 666` was that
list, not 666 live markets). The bot **throws those rows away** after
download. Do **not** delete `positions_buy5m.json` to “clear” them, and do
**not** try to sell dead 5m tokens (no CLOB book). Real leftovers redeem;
API ghosts get one `redeem_abandoned` skip. Look interval is **0.01s** on the live WS book (REST only when posting).
Wallet list refresh is 15s when not holding; the buy gate must allow that
age (a 5s “fresh” check would silently skip every spend between fetches).
Stop/disable 15m and hourly. Split the 5m stake into **two $2.50 slices** (**$5** if both
fill): **early ≥90¢ (to 99¢) in the first 3 minutes** and **≥95¢ overlay**
there, then **$2.50 only in the last 120s at the old 75–90¢ band**. Missed
early does **not** roll into late. Late **first** entry stays 75–90¢; a
same-leg add needs ask ≥ **90¢** (`add_min_price`). After a full hedge the
market is done (no other-leg chase). Normal hedge is **persist 2s at 70/72**
on the combined bag (GUI held ≤ **72¢** / other ≥ **28¢**). Instant 70 is
out — a one-tick dip dumps winners. `toxic_fill` still dumps **only while
bid ≤ 53¢**, no persist. Chainlink TWAP gate is **$0** (any non-zero tick vs
PTB; side must match; flat is still refused). Late FAKs **limit at 90¢**;
early FAKs **limit at 99¢**. Size starts at `budget/ask` and is **at least 3
shares** when `3 × limit` fits in `buy_max_spend` **$3 per FAK** (early
3.00 sh / $2.97 at 99¢, late 3.00 sh / $2.70 at 90¢; 75¢ ask can still be
~3.3 sh). Do not round down to 2.00 sh / $1.98. Displayed top size is
**not** a cap. **Do not pass `user_usdc_balance` on 5m BUY `OrderArgs`.** A
fake `$2.97` wallet makes the SDK shrink 3.00 @ 99¢ into maker `$2.9601`
and CLOB 400s `invalid amounts` (live 21 Aug: 521 attempts / 0 fills).
Pause minting. Pathlog still records all three series (no orders; 14-day /
400 MB cap). **New Python needs a 5m restart.** Live JSON already has
`hedge_threshold` / `hedge_require_ask_max` — leaving **53/55** there makes
persist wait 2s at 53/55, not 70/72. Patch those keys (or omit them) when
you restart.

---

## What we’re doing

**Mint-only helper is paused.** Stop `polymintbot` and leave it disabled. Do not
mint complete sets. Operator still sells leftover mint inventory by hand.

**15m and hourly CLOB bots are stopped.** Do not start `polybuybot` or
`polybuybothourly` unless the operator asks. Open 15m/hourly inventory (if any)
will not be auto-hedged or redeemed by those processes.

**Hourly template (code ready, service still stopped):** three slices, **$10
hard cap** per market (sum of fills), same-leg only, hedge **55/60** + inverted
GUI. Slice A (last 22 min, ask **> 93¢**) spends at most **$5** at a **99¢**
FAK. Slice B (last 15 min, ask **75–90¢**) spends remaining to $10 at **90¢**.
Slice C (last 5 min, ask **> 95¢**) spends remaining to $10 at **99¢**.
`buy_max_spend` **$11**, `buy_max_shares` **14**. Underlying edge stays
**$10**. Tick **0.01**. Windows are **minutes**. See
`strategy_buyhourly.example.json`.

**Active strategy:** **5m only** (`polybuybot5m`) with **two $2.50 slices**
(up to **$5** per market). Early: **≥90¢** in the first 3 minutes, **≥95¢**
overlay on that same window. Late: **75–90¢** in the last **120s** only
(do **not** buy 91–99¢ after T-120). Same-leg add only if ask ≥ **90¢**;
flat late 75–90 is still a first entry. After `hedge_closed`, no re-buy.
Normal hedge is **persist 2s @ 70/72** on the combined bag:

| Knob | Value |
|---|---|
| `buy_budget` | **$2.50** early slice (TTM > 120s) |
| `late_buy_budget` | **$2.50** last-120s slice (75–90¢). Missed early ≠ $5 late. |
| `buy_max_spend` | **$3.00** hard ceiling **per FAK** (not $6 across both slices) |
| `buy_max_shares` | **5** per FAK (~3.3 sh at $2.50/75¢). Two slices may exceed 5 shares total. |
| Ask band | Last **120s**: **75–90¢**, FAK limit **90¢**. First **3 min** (`120 < TTM ≤ 300`): **≥ 90¢** to 99¢, FAK limit **99¢**. **≥95¢** overlay on that early window only. `buy_max_price` **0.90** is the late cap **and** the early ≥90 floor. |
| `add_min_price` | **0.90** — same-leg late add only if ask ≥ 90¢. Flat first late 75–90 still allowed. Do **not** set `late_buy_budget` to 0 (that kills good first late entries). |
| Execution | Size `budget/ask`, **floor 3 shares** when `3 × limit ≤ $3`. Unmatched 400 re-quotes up to **3** FAKs; then **0.15 s** cooldown. Unclear POSTs still quarantine. |
| GUI consensus | winner ≥ 70¢, loser ≤ 30¢ |
| Windows | 5m **whole market**: early ≥90 / ≥95 for TTM (120, 300]; late 75–90 for TTM ≤ 120s (15m / hourly bots **not running**) |
| Hedge | **Qualify** bid ≤ **70¢** and ask ≤ **72¢**, spread ≤ 15¢, **plus** GUI held ≤ **72¢** / other ≥ **28¢** (not inverted 30/70). Last print ≤ 72¢. Must **stay qualified 2s** (`hedge_persist_s`; a bounce resets). Then sell at the live bid. Combined early+late inventory. Instant 70 is out. After a full dump, `hedge_closed` blocks any later buy on that market. `toxic_fill` arms only when FAK **average** < 65¢ (not extra shares vs a 99¢-sized quote) and still dumps without GUI / persist **only while held bid ≤ 53¢** (`hedge_toxic_bid_max`). |
| Underlying edge | **$0** (5m: any non-zero TWAP vs PTB) / **$10** (15m, hourly); side must match |
| `max_open_positions` | **0 = unlimited** |
| `toxic_force_exit_below` | **65¢** |
| `poll_buy_window_s` / `poll_held_s` | **0.01** on the **live 5m WS book**. REST only when a look says buy (then POST). Banner **POS** = live hedge only; **WAIT** should be **0** unless a *redeemable* leftover is still cashing out. |

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
  python3 -c 'import json; from pathlib import Path; p=Path("strategy_buy5m.json"); d=json.loads(p.read_text()); d["min_underlying_edge_usd"]=0.0; d["hedge_threshold"]=0.70; d["hedge_require_ask_max"]=0.72; d["hedge_persist_s"]=2.0; d["hedge_toxic_bid_max"]=0.53; d["add_min_price"]=0.90; d["buy_budget"]=2.5; d["late_buy_budget"]=2.5; d["buy_max_price"]=0.90; d["poll_buy_window_s"]=0.01; d["poll_held_s"]=0.01; d["ui_every_n_cycles"]=50; p.write_text(json.dumps(d, indent=2)+"\n"); print("budget", d["buy_budget"], "late", d["late_buy_budget"], "hedge", d["hedge_threshold"], d["hedge_require_ask_max"], "persist", d["hedge_persist_s"], "add_min", d["add_min_price"], "poll", d["poll_buy_window_s"], d["poll_held_s"])'
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

- [x] Pause minting; keep CLOB triggers (now two $2.50 slices, late 75–90).
- [x] **5m-only live trading** — 15m and hourly buy services stopped/disabled.
- [x] Pin BUY FAKs to **budget/ask limit** at the quoted ask (band unchanged;
  displayed top size is not a cap; `buy_max_spend` $3; `buy_max_shares` 5 buffer).
- [x] Path recorder + `check_path_backtest.py` (first-touch ask × time-left;
  ticks record TOB size; backtest is share-capped FAK, not infinite ask size).
- [x] Hedge FAK follows live bid after book integrity (no 32¢ fill refusal).
      5m **code default** is now persist **2s @ 70/72** (toxic still **53¢**);
      hourly template is **55/60** (still stopped); 15m stays 35/40 (stopped).
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
- [ ] **5m persist hedge 2s @ 70/72 + late add-min 90¢.** Code defaults:
      `hedge_threshold=0.70`, `hedge_require_ask_max=0.72`, `hedge_persist_s=2`,
      `hedge_toxic_bid_max=0.53`, `add_min_price=0.90`. No re-entry after
      `hedge_closed`. **Code change — restart required**, and live JSON
      **must** overwrite the old 53/55 keys (file wins over defaults):
      `git pull`, patch JSON with the snippet above, then
      `sudo systemctl restart polybuybot5m`. Banner `HEDGE ≤70¢`.
      `hedge_skip_persist` / `buy_skip_add_below_min` / `buy_skip_hedge_closed`
      are the new skip lines. Do **not** start 15m / hourly / mint.
- [ ] Hourly three-slice bot is in the repo (`buybothourly.py` + example JSON).
      **Do not start `polybuybothourly`.** Operator later: `git pull`, set live
      `strategy_buyhourly.json` (`dry_run` / `entry_enabled` only when they mean
      it), `sudo systemctl start polybuybothourly`. Leave 5m running. Do not
      start 15m or mint.
- [ ] Cloud paper P&L: paste `CLOUD_RESEARCH.md` section 2 (live `pathlog.py`
      + `--sweep --paper`, rank by `pnl_sum` vs `live_5m_paper`). No `.env`.
      Optional: attach `poly-research.zip` for the historical tape. Not live
      JSON and not `polybuybot5m`.
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
