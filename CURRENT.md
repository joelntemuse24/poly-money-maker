# CURRENT.md — Live probe status

**Agents: read this after `AGENTS.md`.** Update this file when ops/strategy decisions change.
Do not put secrets, API keys, or live wallet material here.

Last updated: **2026-09-03** — trading oracle is **last live BTC vs
window-open PTB** (never a 30s/60s TWAP). Overlay is **5m-only 75–90¢
$2.50** (last **120s** inclusive, FAK **90¢**). **15m stays off.**
Gamma is a background directory only: a known last-120s market with
tokens + `end_ts` already in `_cached_markets` no longer skips as
`stale_discovery` when the Gamma snapshot is older than
max(10s, 2×`discover_cache_s`). Unknown markets still cannot buy.
Hedge never waited on Gamma freshness. Live JSON overlay still wins
until the paste below. **Do not paste last-45 + $25.** Do **not**
start 15m, hourly, or mint. Do **not** start units from a cloud agent.
After this merge, the operator pastes 5m JSON, restarts
`polybuybot5m`, and restarts `polycomplement` on the VM. Do **not**
re-enable last-30s persist 58/60.
Clip size is **$2.50** per 5m fill. Complement is **2×** that clip
(spend ~$5) at ≥80¢ via Relayer + deposit wallet.

**Live combination after this paste + restart 5m only.**
Trading oracle is last live BTC vs window-open PTB (in code, not a JSON
knob). Pull + restart 5m picks it up. TWAP is not “live”.

### 5m (`polybuybot5m`)

| Knob | Value |
|---|---|
| Entry time | last **120s** (`buy_start_s=120`, `late_90_start_s=0`) |
| Ask | **75–90¢** (FAK **90¢**) |
| `min_underlying_edge_usd` | **$0** (any non-zero last print vs PTB; missing/flat skip) |
| Early / ≥95 | **off** (`early_buy_start_s=120`, `early_95_start_s=0`) |
| Size | **one $2.50** (`buy_budget=late_buy_budget=2.5`, spend **$3**, shares **5**) |
| Hedge | persist **1s** @ 50/52, dump **40¢** hold **2s**, flatten **<75¢** (avg <75 still arms toxic), recovery 53 |
| Last-30s ladder | **off** (`hedge_late_ttm_s=0`) |
| Look / WS | `BUY_HORIZON_S` **120s** |

### 15m (`polybuybot`) — **leave stopped**

Do **not** start. Last-3min 90–96 / $10 code stays in `buybot.py`.

**Paste + start (after `git pull`).** Live JSON overlays code defaults.
Print `dry_run` / `entry` before restart. Confirm `strategy_buy5m.json`
and `strategy_complement.json` spend **$5**. Then restart **5m only**.
15m / hourly / mint stay stopped. Restart `polycomplement` yourself
after Relayer is in `.env.complement` (not from a cloud agent).

```bash
cd ~/poly-money-maker && git pull
python3 -c '
import json
from pathlib import Path

def patch(path, updates):
    p = Path(path)
    d = json.loads(p.read_text())
    d.update(updates)
    p.write_text(json.dumps(d, indent=2) + "\n")
    print(path, {k: d.get(k) for k in updates})

patch("strategy_buy5m.json", {
    "buy_start_s": 120, "early_buy_start_s": 120, "early_95_start_s": 0,
    "early_95_min_s": 0, "late_90_start_s": 0,
    "buy_threshold": 0.75, "buy_max_price": 0.90,
    "early_buy_max_price": 0.99, "early_95_min_price": 0.95,
    "buy_budget": 2.5, "late_buy_budget": 2.5,
    "buy_max_spend": 3.0, "buy_max_shares": 5.0,
    "min_underlying_edge_usd": 0.0,
    "hedge_late_ttm_s": 0.0, "hedge_dump_persist_s": 2.0,
    "hedge_persist_s": 1.0, "hedge_toxic_bid_max": 0.40,
    "hedge_flatten_walks": True, "hedge_recovery_cancel": 0.53,
    "hedge_sell_fade": True, "hedge_require_oracle": True,
    "hedge_dump_ignore_oracle": True,
    "dry_run": False, "entry_enabled": True,
})
# Existing live complement JSON keeps $16 unless overwritten.
patch("strategy_complement.json", {
    "buy_min_price": 0.80, "buy_max_price": 0.99,
    "buy_max_spend": 5.0, "buy_max_shares": 8.0,
})
'
sudo systemctl restart polybuybot5m
systemctl is-active polybuybot polybuybot5m polybuybothourly
# expect: inactive active inactive
```

Do **not** paste last-45 + $25. Do **not** start 15m, hourly, or mint.
Do **not** re-paste last-120 `$10` edge 10.

First last-120 tape (27 Aug 17:26Z → 31 Aug 15:12 Dublin): **+$9**, WR ~**82%**,
take rate **411/1129 = 36%**, **73 walks**, **52 sells / 0 `hedge_fill`**
(sells ghost via `hedge_uncertain_resolved`). Catalog:
`docs/2026-08-31-last120-loss-catalog.md`. The `$10` / last-60 90–96
overlay is **retired** by this 75–90 $2.50 restore.

**31 Aug ~20:53Z reversal + participation paste:** SESSION TAPE **n=0**
and participation **0 `csv` sources** were a join miss (generic title /
unused `slug`), not zero fills. 5m autopsy: **585/1151 “bought”**
is bot log (`buy_attempt` included), not the **411** wallet fills.
**409** misses were above-ceiling (99/1). The only live-shaped combo
cell is `live_late_7590` (**17.1% flip / 82.9% WR**). Script used to
print last-45+$25 as RECOMMENDATION — that line is now “do not paste”.
See catalog §7.

**31 Aug ~21:30Z SESSION TAPE n=437** (re-fetched CSV through 20:39Z;
titles were range-shaped so join worked). `session_pnl=−1139` /
`redeem=0` / 383 `open` at −$2.70 was **CLOB-only accounting** — Data
API `/trades` has no Redeem. Flip-by-`|dist|` at fill is real (0–5
**47%**, 5–10 **34%**, 20–25 **14%**). See catalog §8.

**31 Aug ~22:08Z paper-credit re-run** (VM stash + `git pull` to
`7cb783c`, same CSV). Banner:
`paper_win=288 paper_loss=71 hedge=54 other=24 redeem=0
session_pnl=-163.16`. Winners are **+$0.30 to +$1.21**, not −$2.70.
**Do not treat −$163 as live P&L.** 24 `open` bags sit before Binance
1s (`1787867518`) and still count −spend (~**−$65** of the −$163). GATE
featured 403: `paper_pnl_kept=−85.12` / **−$0.86/h**. Live overlay recap
is still ~**+$9** / **+$0.10–$0.12/h**. Fill×TTM: walks **<70¢** WR
**34%**; 75–90 WR **60–76%**; TTM 60–90 best (**70.9%**). SESSION replay
`last45_e25` keep **6 / −$3.46**. GATE `|dist|≥25` keep 132 / −$5 at
1.3/h — **do not paste last-45+$25 or GATE ≥25**.

---

## What we’re doing

**Mint-only helper is paused.** Stop `polymintbot` and leave it disabled. Do not
mint complete sets. Operator still sells leftover mint inventory by hand.

**Hourly CLOB bot is stopped.** Do **not** start `polybuybothourly`.

**Active strategy:** **5m only** (`polybuybot5m`). 15m stays stopped.
Same-leg only. After `hedge_closed`, no re-buy. Early slice **off**
(one $2.50, not two).

| Knob | 5m | 15m |
|---|---|---|
| Window | last **120s**, **75–90¢**, FAK **90¢**, `$2.50` | **stopped** |
| Last-45 ≥90 overlay | **off** (`late_90_start_s=0`) | n/a |
| Early / ≥95 | **off** | n/a |
| Hedge qualify | persist **1s @ 50/52** | inverted **35/40** (not running) |
| Dump | Bid-only ≤ **40¢** after **2s** (`hedge_dump_persist_s`) | n/a |
| Flatten walks | avg **<75¢** arms toxic; sell while bid **<75¢** | n/a |
| Underlying buy edge | **$0** | n/a |
| `max_open_positions` | **0 = unlimited** | n/a |
| poll | **0.01** | n/a |

**Also running (no orders):** `pathlog.py` (`polypathlog`) writes one JSONL file
per market under `pathlog/ticks/`.

### Complement (`polycomplement`) — **restart after this merge** (not from the cloud VM)

Second Polymarket account. First-account 5m/15m hedge is **unchanged**.
After a confirmed primary fill, this process watches the **other** token
and lifts it at **80–99¢** (FAK 99¢, **2×** share-match, spend cap **$5**,
oracle must favor that side). If the primary already sold (`hedge_closed`),
it does not buy.

Needs `.env.complement` with a **different** `FUNDER_ADDRESS` than `.env`.
Put `RELAYER_API_KEY` and `RELAYER_ADDRESS` in **`.env.complement`**
(not the primary `.env` — systemd `EnvironmentFile` is only the
complement file). Same wallet is a hard refuse (the live 5m loop would
see those shares and sell them). Compare funders from the **files**,
not `os.environ` — systemd `EnvironmentFile=.env.complement` pre-sets
`FUNDER_ADDRESS` so `load_dotenv(".env")` would compare the complement
wallet to itself. The Magic proxy `0xCfF52577…` is also a hard refuse
(CLOB 400 `maker address not allowed` even with Relayer).

**CLOB client (2026-09-03):** this account is a **deposit wallet**
(`signature_type=3` / POLY_1271), not Magic/proxy type 1. Complement
**only** now builds `ComplementDepositClobClient` →
`polymarket.SecureClient.create(private_key=…, wallet=COMPLEMENT_WALLET
or FUNDER_ADDRESS, api_key=RelayerApiKey(key=RELAYER_API_KEY,
address=RELAYER_ADDRESS))`. Relayer env is required; missing Relayer
fails closed (no type-1 fallback). Live funder must be the deposit
wallet `0x2b2D1dA1a49E8BF73EbBC3EAC35D79cc88cd4ad2` (cash may still
sit on the Magic proxy until Joel moves it). Size is **2×** the
primary bag, spend cap **$5** (2× the 5m $2.50 clip), other-leg floor
**≥80¢**. Gamma may still classify that address `POLY_PROXY`; either
wallet type is allowed when `inner.wallet` equals the funder. 5m/15m/hourly
stay py-clob type 1. **Unset** any `API_KEY` / `API_SECRET` /
`API_PASSPHRASE` trio in `.env.complement` (do not print the values).
Optional `COMPLEMENT_WALLET` overrides `FUNDER_ADDRESS` without another
code PR.

A 1¢ FAK that does not fill is enough to prove maker is allowed: CLOB
must not 400 `maker address not allowed` or `signer address has to be
the address of the API KEY`. Empty FAK (`no orders found to match`) is
success for that probe.

Do **not** start `polybuybot` / `polybuybot5m` from this change. After
merge on the VM: `git pull`, `.venv/bin/pip install -r requirements.txt`,
paste complement spend **$5** / **8** shares (script above — do not
rely on “copy if missing”), then `sudo systemctl restart polycomplement`.
Cloud agents must not start it. Hourly/mint stay off.
Keep `dry_run: true` until you want live other-leg lifts;
`entry_enabled: true` / `dry_run: false` for real 80¢ FAKs. A 1¢ probe
can be a one-off outside the bot.

POST never invents a full fill (`size_matched` / GET-order only). A
write-ahead `buy_uncertain` hits disk **before** the FAK. Empty / reject
cools 1s / 2s so a miss does not retry every 0.01s look. Leftover
quarantine resolves from wallet delta or empties after 5s if balances
are flat; unread balance stays in-flight (no second POST).

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
- **Buy bots:** **5m only.** Stop/disable 15m, hourly, and mint. Live JSON
  is 75–90 $2.50 last-120 after the paste above. **Do not paste
  last-45 + $25.** Do **not** start 15m, hourly, or mint.
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
- [x] 5m underlying gate **$0** (any non-zero last print vs PTB; side must match).
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
- [x] Cloud paper P&L / last-45+$25 probe: last-45+$25 was empty / −EV
      live. Operator pasted last-120 / edge $0 on **27 Aug ~17:26Z**.
- [x] 27 Aug reversal-feature tape: no vol skip (still).
- [x] **31 Aug last-120 tape** — barely +EV (+$9 / ~94h). Do not size
      up. Catalog `docs/2026-08-31-last120-loss-catalog.md`. Highest
      leverage is loser exits (held-to-zero + dump-at-1¢), not more
      entries. Sell unmatched retry already exists; `hedge_fill` was
      missing on the ghost-resolve path.
- [x] **B+C live paste + 5m restart** — 31 Aug ~22:25Z. Printed persist
      1.0 / dump 0.4 / toxic 0.75 / flatten True / start 120 / edge 0 /
      dry_run False / entry True. Services inactive / active / inactive.
- [x] **Paste last-120 + `$10`** — 31 Aug ~22:58Z. Printed persist
      1.0 / dump 0.4 / flatten True / start 120 / edge 10.0 /
      dry_run False / entry True. Services inactive / active /
      inactive.
- [x] **92¢ $10 production** — paste + start 5m last-60 90–92 + 15m
      last-3min 90–92 (#144, 1 Sep).
- [x] **Complement second account** — code on main (#146). Do **not**
      start until `.env.complement` is a different funder. Copy example
      JSON, keep dry_run until funded. Primary 5m/15m hedge unchanged.
- [ ] **5m 75–90 $2.50 restore + Relayer complement (this PR).** After
      merge: paste 5m last-120 75–90 $2.50 dump-hold 2s **and**
      complement `$5` / 8 shares, `sudo systemctl restart
      polybuybot5m`. Expect **inactive active inactive**. Do not start
      15m, hourly, or mint. Operator puts Relayer in `.env.complement`
      and restarts `polycomplement` on the VM (deposit wallet
      `0x2b2D…`, not the Magic proxy). Measure: `buy_attempt` band=late
      75–90 TTM≤120, $2.50 FAK 90¢; complement 2× ≥80 spend ≤$5;
      `cycle_error` 0.

---

## Agent instructions

1. Read `AGENTS.md` + this file before changing mint/buy/hedge logic.
2. Do **not** restart minting unless the operator asks. Do **not** start
   `polybuybothourly` or `polybuybot` unless the operator asks. Do **not**
   start `polycomplement` without a second-account `.env.complement`.
   Live buy bot is **5m only** (75–90 $2.50 last **120s**). Do **not**
   paste last-45 + $25.
3. Never truncate state/PnL/log files; never commit live strategy/state/`.env`.
   Pathlog ticks are **auto-pruned** (14d / 400 MB) — do not `rm` them by hand,
   but **do export** (`check_path_backtest.py --csv` or `scp` the ticks dir)
   before prune. Cloud research: `CLOUD_RESEARCH.md`.
4. When an ops decision changes, **update this file in the same PR/commit**.
