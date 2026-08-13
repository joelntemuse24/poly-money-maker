# Full-system audit & brainstorm — 2026-08-13

Read-only. No live JSON edits, no service restarts, no orders.
This cloud clone has **no SSH to `instance-20260516-185922`**. Live
`strategy_buy5m.json`, `buybot5m.log`, heartbeats, and pathlog ticks are
gitignored and were **not** on disk here. CI deploy to that VM has been
failing with `dial tcp *:22: i/o timeout` (runs `31655968716` at 00:54 UTC
and `31658074563` at 01:33 UTC). Where the report says “confirm on the VM,”
that is still required.

Public CLOB/Gamma were queried from this agent at ~01:59 UTC for live 5m
metadata (`itode`, tick, min size). That is not a substitute for the morning
log slice.

---

## 1. Executive

The 5m buy path in `buybot5m.py` **matches the intended strategy** on the
things that can lose money: FAK-only, force-REST entry (WS cannot arm a buy),
write-ahead `buy_uncertain` before POST, definitive 400s do not retry a second
full budget, empty FAKs **clear** quarantine and **do not** set `bought_token`,
hedge is REST two-sided fail-closed, toxic dump is a separate 65¢ entry-fill
path, GC never invents $1 par, redeem waits for relayer confirm **and** Data
API inventory gone. Example JSON is disarmed. The three bots still differ
correctly on window units, oracle source, and slug excludes.

**Unmatched-FAK verdict:** the bot is doing what it was told — it reaches
POST, the CLOB returns 400 `no orders found to match with FAK order`, it logs
`buy_attempt_rejected` / `buy_fail status=empty`, and it can try the same
market again after `buy_cooldown_s` (1 s). That is **not** a crash and **not**
a one-entry lockout. Live 5m BTC markets at audit time are `itode: true`
(250 ms taker delay, then re-validate), tick **0.01** (docs/code still say
5m is 0.001), and `min_order_size` **5 shares**. The bot posts a FAK **at the
touch ask** after two REST books, a CLOB balance refresh, a sign, and an
`fsync`. By the time matching runs (POST RTT + 250 ms delay) the 81–90¢ ask
is often gone. FAK is partial-fill-**if some match exists**; zero match →
this 400. Empty fail does **not** burn the 120 s window via `bought_token`.
Cooldown is only 1 s — the morning burst of rejects is evidence of retry, not
a silent one-shot skip.

This clone could not prove the VM process was started with
`dry_run: false` + `entry_enabled: true`. Operator observation (heartbeat
advancing, `buy_attempt` firing, inventory not increasing) is consistent with
a live armed 5m bot. 15m/hourly code is present but should stay stopped.

---

## 2. Contradictions table

| # | Source A | Says | Source B / reality | Severity |
|---|---|---|---|---|
| 1 | `CURRENT.md` Ops | `systemctl restart polybuybot polybuybot5m polybuybothourly` | Live policy is **5m only**; 15m/hourly inactive. `DISK_OPS.md` recovery also `start`s all three. | **P0** — following the doc starts extra live bots on a shared wallet |
| 2 | `AGENTS.md` glance | “Live on the VM: three standalone buy bots” | Prompt / `CURRENT.md` Open: 5m + pathlog only | **P1** |
| 3 | Startup banners `buybot5m.py:2763-2764` (and 15m/hourly siblings) | `BUY-SIDE · 97¢ WINNER TRIG` / `HEDGE @ 65¢` | Live knobs: ask 75–90¢, hedge 35¢/40¢, toxic dump 65¢ | **P2** — operator/agent confusion, not a live knob |
| 4 | `TECHNICAL_DESIGN.md` §6 skip list | `buy_skip_underlying_edge` — live oracle &lt; **$10** from PTB | 5m default / example / `CURRENT.md`: **$5**. Code uses `MIN_UNDERLYING_EDGE_USD` | **P2** |
| 5 | `TECHNICAL_DESIGN.md` §14.4 landmine | “GC assumes `redeem_value = bought_size`” | `gc_par_redeem` (`buybot5m.py:1207-1211`) returns 0 unless redeem value was recorded. Credit path requires relayer `STATE_CONFIRMED`/`MINED` **and** Data API size ≤ 0.01 | **P1** — the landmine is stale; “fixing” toward it would be a real PnL bug |
| 6 | `TECHNICAL_DESIGN.md` §10 / AGENTS | 5m `tick_size` **0.001**; `hedge_min_price` ~0.32 | Live CLOB `clob-markets` for current `btc-updown-5m-*`: `mts=0.01`, `mos=5`, `itode=true`. Strategy JSON is forced to `"0.001"` (`EXPECTED_TICK_SIZE`). Orders use `get_tick_size_cached` (book/WS/CLOB), so POST tick can be 0.01 while docs/validator still think 0.001 | **P1** |
| 7 | `TECHNICAL_DESIGN.md` §11 | Heartbeat is a “monotonic tick counter” | `write_heartbeat` writes `int(time.time())` (`buybot5m.py:517-520`). AGENTS.md is right | **P3** |
| 8 | Example JSON | `entry_enabled: false`, `dry_run: true` | VM live file is gitignored; tests only assert `dry_run` (`test_examples_are_disarmed`), not `entry_enabled` | **P2** — agents “verifying” with examples |
| 9 | `buy_market_with_retry(..., max_retries=3)` | Sounds like 3 POSTs | Definitive HTTP 400/401/403/404/422 **break** (`2112-2118`). Unmatched FAK is one attempt. Tests mock `definitive_order_rejection` as False, so they do **not** cover the live 400 | **P2** (behavior is correct; the name lies) |
| 10 | Pathlog | Records 5m+15m+hourly books | 15m/hourly buy bots stopped | **P3** — good for research; easy to misread as “those bots are live” |
| 11 | `CURRENT.md` Open | Unchecked “stop mint, restart buy bots + pathlog” | Prompt: mint paused, 5m already firing | **P2** |
| 12 | Three copies | Near-identical | Window units correctly split (`buy_start_s` vs `buy_window_min`). 15m/hourly do **not** define `seconds_left`. Copy-paste NameError risk remains if someone ports 5m window checks | **P2** (latent) |
| 13 | Prompt A1.4 | Gamma stale → 120 s | `MarketGateway.stale_cache_s=120` returns old markets with `discovery_fresh=False`. Bot entries require `_discovery_fresh` and snapshot age ≤ `max(10, cache*2)` = **10 s** (`2940-2945`). Fail-closed for entries | **P3** — prompt overstated; code is tighter |
| 14 | CI `deploy.yml` | SSH `git pull` + pip; echo `systemctl is-active` | Last two `main` deploys: **SSH timeout**. VM code may be behind `main` (pathlog cap PR merged 01:33, deploy failed). Services are not restarted by CI (good) | **P1** ops |
| 15 | `CURRENT.md` scp user | `ntemusejoel@<vm>` | `TECHNICAL_DESIGN.md` path `/home/ntemusejoel/...`; service `User=ntemusejoel` | **P3** — username typo in CURRENT scp examples |

No P0 **code** bug found that double-buys a market, sells on a spoof book, or credits PnL without redeem confirm. The P0 is **ops docs that start the other bots**.

---

## 3. Silent gaps

Buy-path `continue`s with **no** `log_event` (5m, in order):

| Line | Condition | Why it stays unnoticed |
|---|---|---|
| 3882 | `held_size > 0.01` | Expected; looks like “did nothing” in window |
| 3884 | `seconds_left > BUY_START_S` | Most of the 5m; no `buy_skip_window` |
| 3886 | `not ENTRY_ENABLED` | Hot-reload disarm is invisible unless you know |
| 3888 | `not _discovery_fresh` | Gamma/cache stale; hedges continue |
| 3895 | Positions snapshot older than `max(5, refresh*3)` | Caps fail closed |
| 3901 | Balance snapshot older than `max(30, refresh*3)` | 45 s with default 15 s refresh |
| 3907 | `meta["buy_uncertain"]` | Ambiguous POST; logged earlier, silent each later cycle |
| 3911 | `bought_token` (one entry) | After a **fill**, not after empty |
| 3925 | `buy_grace_s` (1 s) after first sighting | No `buy_skip_grace` |
| 4180 | `not (up_buy or dn_buy)` | **Ask not in band** is silent. `buy_skip_no_consensus` only fires when winning **and** ask already in band |
| 4191 | `buy_cooldown_s` (1 s) | After every empty FAK; no `buy_skip_cooldown` |

Missing metrics (not logged today):

- No `buy_skip_fak_empty` **counter** / rate (events exist: `buy_attempt_rejected` + `buy_fail status=empty`)
- No time-from-gate-REST-to-POST histogram
- No `itode` / `min_order_size` / tick / ask_size / implied shares on `buy_attempt`
- No `buy_skip_min_size` (bot stores `min_order_size` on the book snapshot at 957–959 and **never reads it**)
- Prefetch/REST 429s only appear inside `book_quote_fail` if the SDK+HTTP both die
- Heartbeat is a unix timestamp, not a cycle counter — a wedged sleep still “advances” if the process is alive

---

## 4. Correctness findings

### A0. Config vs docs vs code

- `_STRATEGY_DEFAULTS` in `buybot5m.py:135-186` match `strategy_buy5m.example.json` and `CURRENT.md` knobs (75–90, $2.50, GUI 70/30/5, hedge 35/40/15, toxic 65, edge $5, window 120 s, poll 0.1/0.05, tick string `"0.001"`).
- `dry_run` is startup-only: `STARTUP_DRY_RUN` then `DRY_RUN = STARTUP_DRY_RUN` every cycle (`2872`). Hot reload cannot swap state files.
- Invalid hot reload: `_entries_disabled` sets `entry_enabled=False`, keeps `hedge_enabled=True` (`202-207`, `293-299`).
- Example JSON: `entry_enabled: false`, `dry_run: true`. Good.
- **Live `strategy_buy5m.json` was not readable in this environment.** Do not treat the example as armed.
- Banner 97¢/65¢ is cosmetic only.

### A1. Buy gate chain (market → POST)

Order in `buybot5m.py` after hedge work, **all** must pass before `buy_market_with_retry`:

1. `held_size > 0.01` → continue (silent)
2. `seconds_left > BUY_START_S` → continue (silent)
3. `ENTRY_ENABLED`
4. `_discovery_fresh` (10 s received bound, not 120 s)
5. Positions age ≤ `max(5, refresh*3)`
6. Balance age ≤ `max(30, refresh*3)`
7. `buy_uncertain` quarantine
8. `one_entry_per_market` + `bought_token` (forced true in `load_strategy`)
9. `buy_grace_s` after `entered_at`
10. Caps: `max_open_positions` (0 = unlimited), notional, daily, USDC — **logged**
11. Force-REST both legs + last-trade via `_entry_executor` (`4006-4017`). `get_quote_fast(..., True, True)` is `prefer_rest, force_rest`. WS cannot arm a buy.
12. Incomplete GUI → `buy_skip_incomplete_book`
13. Ambiguous GUI → `buy_skip_ambiguous`
14. Ask in `[buy_threshold, buy_max_price]` on the **winning** GUI leg
15. GUI consensus + `entry_book_ok` (bid ≥ 70¢, spread ≤ 5¢) else `buy_skip_no_consensus`
16. Underlying edge + side
17. Cooldown (silent)
18. `on_submit` write-ahead `buy_uncertain` + `save_json` **before** `post_order` (`2089-2110`, `4262-4297`)
19. Inside retry: **another** `force_rest` book; spend = `min(budget, ask * ask_size)`; FAK at **that** ask (`2004-2072`)

None of 1–17 can POST without the others. FAK requires 18–19.

**Empty FAK does not set `bought_token`.** `_persist_buy_fill` (sets `bought_token`) runs only when `total_bought > 0`. Empty 400: `break` → return `"empty"` → `_clear_buy_uncertain()` → `last_buy_at` → `buy_fail` (`4383-4389`). Next cycle: no `bought_token`, no quarantine, only 1 s cooldown. **Hypothesis “empty FAK burns the market” is rejected** for current knobs. It **is** a 1 s + full re-gate tax.

### A2. Book path

- `get_quote_fast` (`992-1019`): WS unless `prefer_rest`/`force_rest`; REST min interval **200 ms**; WS stale 2 s (`buy/clob_book_ws.py` `STALE_S`).
- Entry: four futures, then sequential `.result()` — books **overlap in the pool** but `get_book_quote` uses `safe_api_call` → `_clob_lock`, so the two force-REST books **serialize** on the SDK.
- GUI: mid if spread ≤ 10¢ else last trade (`1039-1094`). Last-trade cache 0.25 s; snapshot last trade only if snapshot ≤ 2 s.
- Prefetch: `_book_executor` 4 workers, max 30 pending (`4548-4551`); WS overlay 0.25 s held / 2 s otherwise (`3219-3231`). Stale-high WS cannot overwrite a held REST quote beyond 0.25 s. Prefetch is **not** `force_rest` and **cannot** arm a buy.
- WS reconnect **clears** quote cache (`buy/clob_book_ws.py:412-416`). Price-change events zero size when the top price moves (`178-180`, `216-225`) — REST required before sizing. Three bots = three WS connections; live should be one buy WS + pathlog has **no** WS (REST only).
- 5m strategy tick forced to `0.001`. **Live 5m CLOB tick is 0.01.** `hedge_min_price` 0.325 is a legal 0.001 tick; on a 0.01 book `hedge_sell_price` rounds the floor **up** to 0.33. FAK limit is the live ask, not 0.325.

### A3. FAK / empty / quarantine

- `ImmediateResponseClobClient` skips the SDK’s up-to-30 s trade poll (`55-64`).
- `definitive_order_rejection`: status in `{400,401,403,404,422}` → log `buy_attempt_rejected` → **break** (`1779-1781`, `2112-2118`). One POST per trigger for unmatched FAK.
- `status=empty` from the helper is `"empty"` (including explicit unmatched 200s, tested in `test_explicit_unmatched_zero_fill_is_terminal_empty`). Live symptom is **400 exception**, not a 200 `unmatched` body — same terminal empty, **untested**.
- Ghost `_reconcile_ghost` waits 0.4 / 1 / 2 s and only counts `delta > 0.01` vs baseline (`1974-1996`). It cannot invent size from a flat balance. **Not called** on definitive 400 (break before ghost).
- Fill avg &lt; `buy_threshold` → persist + `buy_fill_below_band`; avg &lt; 65¢ → `toxic_fill` (`2238-2243`, `4249`).
- `_clob_lock` serializes SDK `get_order_book` / create / post. `check_clob_token_balance` (`633-654`) does **not** take the lock and always `refresh=True` before the retry loop — two extra CLOB RTTs on the POST path, plus a thread-safety smell.
- Pathlog is a **separate process**; it does not share the lock. It does share the VM IP toward `clob.polymarket.com`.

Polymarket docs (`error-codes`, `order-lifecycle`): FAK “partially filled or killed **if no match is found**.” Partial fills exist when **some** match exists. Zero match → this 400. Crypto up/down: **taker delay 250 ms** (`itode`) then validation runs again. Live 5m `clob-markets/{conditionId}` returned `"itode": true` at 01:59 UTC.

### A4. Hedge vs toxic dump

Confirmed as specified:

| Mechanism | Trigger | Integrity |
|---|---|---|
| Normal hedge | bid ≤ 35¢ **and** `hedge_book_ok` (ask ≤ 40¢, spread ≤ 15¢) | force-REST; bounce cancel; no WS sell |
| `toxic_fill` | entry avg &lt; 65¢ | dump; no bounce; floor = one tick |

- WS peek: fresh bid **above** 35¢ skips REST (`3599-3605`). Age cap `hedge_quote_max_age_s=0.25`. Stale-high WS cannot skip. WS cannot sell.
- Incomplete REST → `hedge_skip_incomplete_rest`.
- Spoof penny bid → `hedge_skip_toxic_book`.
- `hedge_uncertain` continues until exact-order reconcile (`3572`).
- Data API zero → `hedge_ghost_unconfirmed`; `reconcile_hedge_sold` never promotes API-only sold.
- Redeem deferred while hedge inventory exists **or** entry window open (`3126-3147`). Status polls are on `_redeem_executor`.

### A5. Oracle / PTB

- 5m: `SOURCE_TWAP_30`. 15m: TWAP 60. Hourly: Binance. Not mixed.
- `LIVE_STALE_S = 5` → `live_stale` fail closed (`buy/btc_price.py:37`, `255-257`).
- PTB: nearest tick, `PTB_MAX_SKEW_MS = 2000`; missing/skewed → no `ok` PTB → `missing_ptb`.
- `edge_usd = live - ptb`; favored up if `edge > 0` else down.
- `edge == 0` fails even if min edge is 0 (`261-263`).
- `abs(edge) < min_edge` (`264-266`) — **absolute** edge. Down buy with `edge_usd ≈ -40` **passes** the $5 gate and the side gate (`favored == "down"`).
- PTB only if the process is up at window open; `capture_ptb` with empty ticks returns `ok: False` **without** poisoning the store (`192-195`).

### A6. State / GC / redeem / PnL

- `atomic_save`: tmp + fsync + bak + replace + dir fsync (`442-478`).
- Redeem credit: `redeem_confirmed` from `STATE_CONFIRMED`/`STATE_MINED` **and** remaining size ≤ 0.01 (`4454-4477`). `gc_par_redeem` never invents par.
- Uncertain quarantines excluded from GC (`2997-3003`, `1216-1220`).
- Lock: `/tmp/poly-money-maker-buybot5m.lock`.
- Heartbeat: unix seconds.

### A7. Three-file drift

| | 5m | 15m | Hourly |
|---|---|---|---|
| Window | `buy_start_s` / `seconds_left` | `buy_window_min` / `minutes_left` | same as 15m |
| Tick expected | 0.001 | 0.01 | 0.01 |
| Oracle | TWAP 30 | TWAP 60 | Binance |
| Prefix / excludes | `btc-updown-5m` / `bitcoin-up-or-down` | `btc-updown` / `btc-updown-5m`, `bitcoin-up-or-down` | `bitcoin-up-or-down` / both `btc-updown*` |
| `hedge_min_price` example | 0.325 | 0.32 | 0.32 |
| Fill/quarantine/hedge | same markers; tests assert write-ahead, `_entry_executor`, `_discovery_fresh` on all three | | |

Slug excludes are load-bearing **if** 15m is started. Stopped 15m cannot double-buy. **Do not** restart it from CURRENT.md.

### A8. Pathlog

- Separate process, no `.env`, no orders (`pathlog.py`, `deploy/polypathlog.service`).
- Startup log keys: `retain_s=RETAIN_S` = `14*24*3600` = **1209600**, `max_tick_bytes=400*1024*1024` = **419430400** (`66-67`, `445-450`). Could not confirm a live process printed them.
- REST `/book` ~1 Hz × 2 tokens × up to 3 series, 8 workers — same public CLOB as the 5m bot’s force-REST.
- Cannot match a FAK by itself. Can add RTT/429 during the last 120 s.
- `--grid` is first-touch ask × ttm, **no hedge model**, win = $1 redeem (`check_path_backtest.py:149-155`, `238-260`). Ticks were not in this clone, so `--grid` was not run.

### A9. Tests vs reality

Covered: fill parsing, write-ahead, unmatched **200** → empty, hedge/entry book gates, GC guards, example `dry_run`, pathlog prune, backtest helpers, cross-bot markers.

**Not covered:** live FAK **400** unmatched (the actual exception), WS reconnect, `get_quote_fast` handoff, 429s, RTDS/PTB, relayer confirm, slug-exclusion integration, `itode`, `min_order_size` vs budget, `entry_enabled: false` in examples.

---

## 5. Fill-path timeline

Annotated sequence for “ask in band → FAK 400”. Delays are order-of-magnitude (CLOB RTT from a small GCP VM is typically ~50–200 ms; not measured here).

```
t+0.00  poll wake (100 ms sleep if already in window)
t+0.00  silent continues (window/grace/cooldown) — if any, loop ends here
t+0.00  ENTRY_ENABLED, discovery, positions/balance age
t+0.05  force-REST UP book     ─┐  _entry_executor, but
t+0.15  force-REST DOWN book   ─┘  _clob_lock serializes SDK /book
t+0.05  last-trade UP/DOWN (HTTP, no lock; overlap with books)
t+0.20  GUI + consensus + entry_book_ok + underlying (memory)
        log buy_attempt  ← gate ask, e.g. down @ 0.81–0.90
t+0.25  check_clob_token_balance(refresh=True):
          update_balance_allowance + get_balance_allowance  (no lock)
t+0.45  buy_market_with_retry: force-REST winning book AGAIN
        clip spend to ask_size × ask; price = that ask (touch)
t+0.55  create_market_order (sign, lock)
t+0.60  on_submit: atomic_save fsync + dir fsync  (must hit disk before POST)
t+0.70  post_order FAK
t+0.70  CLOB: itode hold 250 ms, then re-validate book
t+0.95  400 "no orders found to match with FAK order"
        buy_attempt_rejected, break, status=empty, clear quarantine,
        last_buy_at, buy_fail
t+1.95  cooldown elapsed; full chain repeats if still in band
```

**Why the ask is gone:** the gate REST and the POST REST are already one+ RTT apart; signing + fsync add more; **then** Polymarket waits 250 ms and looks again. Late-window 5m size at 81–90¢ is thin and moving. Posting **at** touch has zero extra tick of room after that delay. Price improvement still fills *through* the book if anything remains at or below the limit — a one-tick **higher** limit (still ≤ 90¢) would still pay the old ask if it is there, and still match if the ask lifted one tick.

**Size:** $2.50 @ 85¢ ≈ 2.94 shares. Live `mos=5`. If CLOB enforced min size, the 400 text would usually be `Size (...) lower than the minimum`. Operator quoted unmatched, so those POSTs likely **passed** size checks — still log `min_order_size` vs implied shares before changing budget. Clipping to 1 share at touch makes a FAK even easier to miss after delay.

**Tick:** posting GUI-ish 0.86 vs book 0.861 is unlikely on **current** 5m books (`mts=0.01`, prices `0.81`/`0.86`). More relevant if `get_tick_size_cached` fell back to `0.001` and the SDK signed a 3-decimal price; snapshot tick should prevent that after a force-REST.

**Lock/pathlog:** extra /book from pathlog (1 Hz × 6 books in a busy minute) can lengthen the SDK/HTTP queue. Unlikely to be the sole unmatched cause vs 250 ms delay + touch limit.

**Cooldown:** after empty, same market **can** retry inside 120 s. Morning 01:23–01:29 burst is that retry loop losing a race, not a lockout.

---

## 6. Brainstorm ranked

Do **not** implement until the operator picks a slice. Measure on VM logs + pathlog; no live knob change in this pass.

### P0 — would move fills vs empty

| # | Idea | Mechanism | Fill/latency | Risk | Measure | Change type |
|---|---|---|---|---|---|---|
| 1 | **Log the race** | Add `buy_attempt` fields: `itode`, tick, `min_order_size`, ask_size, spend, implied shares, `t_gate_rest`, `t_post`; histogram `t_post - t_gate`; count `buy_fail empty` per market | Zero fill change; makes the next hour diagnosable | None | grep next window | Code (approval) |
| 2 | **Limit one tick above touch** | FAK buy price = `min(buy_max_price, snap_tick(ask + N*tick))` with N=1 (maybe 2). Still ≤ 90¢. Pays old ask if it stays (price improvement); survives one-tick lift during itode | Directly attacks unmatched-after-250 ms | Slightly worse fill when book is stable; never above ceiling | empty-FAK rate vs fill avg in logs | Code |
| 3 | **Confirm min size vs $2.50** | If any 400s are `Size lower than the minimum: 5`, $2.50 cannot buy 5 shares in-band (need ≥ $3.75 @ 75¢ / $4.50 @ 90¢) **or** skip instead of POST | If size-rejects exist, this is participation, not speed | Larger notional / more reversal $ | grep 400 bodies | Knob **after** evidence; do not retune now |
| 4 | **Drop POST-path balance refresh** | Baseline from the already-fresh Data API snapshot; CLOB refresh only on ghost/ambiguous | Saves 1–2 RTTs before the second book | Weaker ghost baseline if Data API lags | time-to-POST histogram | Code |
| 5 | **Skip last-trade when mid exists and spread ≤ 10¢** | GUI already uses mid; last-trade HTTP is wasted | Saves a pair of GETs every cycle | Incomplete GUI if mid then flips wide in the same cycle — fail closed already handles None | skip rate + time-to-POST | Code |
| 6 | **WS-trigger “maybe buy”** | In-window, WS ask in band **arms** the existing REST gate + POST (no 100 ms wait to notice). Keep force-REST at POST | Faster to first POST; does not remove itode | More REST load; must not POST on WS alone | empty rate, REST count | Code |
| 7 | **Pathlog off `/book` in last 120 s of 5m** or move pathlog to market WS | Stops competing with force-REST | Maybe tens of ms; research cost | Blind backtest during the money window unless WS records | 429/`book_quote_fail` vs empty-FAK | Ops + small code |

### P1 — process / infra

| # | Idea | Notes |
|---|---|---|
| 8 | Fix **CURRENT.md / DISK_OPS** restart-all-three | P0 ops footgun; safe doc slice |
| 9 | Fix CI SSH timeout | Deploys since 00:54 UTC never pulled; cannot confirm `systemctl is-active` from GitHub |
| 10 | HTTP `POST /books` batch for the entry pair, **outside** `_clob_lock` | Docs: batch book endpoint exists. Keep SDK lock for sign/POST |
| 11 | Log silent skips (`buy_skip_window/grace/cooldown/discovery/stale_positions`) at most once per N seconds per reason | Observability; not fills |
| 12 | After-empty: no extra cooldown (already 1 s) — **not** a market-burn fix. Optional: skip `buy_cooldown_s` when last status was `empty` | Tiny; measure first |
| 13 | Colocate / non-burstable VM | §13 already: CPU does not remove CLOB RTT or itode. Measure `ping`/`curl -w time_starttransfer` to `clob.polymarket.com` before buying a bigger box |

### Later / research (pathlog `--grid`, no live retune)

- Trigger 75¢ vs waiting 85¢: more size vs worse price / more reversals. `--grid` asks already include 0.70–0.98 × 30–240 s. **No hedge model** — do not treat PnL_sum as live EV.
- Window 90 vs 120 vs 180 on 5m.
- Edge $5 vs $0 vs $10: `check_edge_counterfactual.py` (resolution only, no hedge).
- Budget vs clip-to-touch: more fills, less size — blocked by `mos=5` if that is real.

**Do not** switch to GTC/resting bids in this strategy without a new kill-switch and inventory story. FAK-only is load-bearing.

**Do not** merge the three bots into one process unless lock/RTT is proven to be the unmatched cause. Live is already one buy process + pathlog. itode is exchange-side.

---

## 7. Doc fixes vs code fixes vs do not touch

### Doc fixes (safe after operator OK — first slice)

- `CURRENT.md` Ops: restart **`polybuybot5m` only**; mint stays stopped; pathlog separate.
- `DISK_OPS.md` recovery: do not `start` 15m/hourly.
- `AGENTS.md` glance: 5m + pathlog live; 15m/hourly installed but inactive.
- `TECHNICAL_DESIGN.md`: skip text $5 on 5m; delete/replace §14.4 par-GC landmine; heartbeat = unix time; 5m **live tick is 0.01 / itode / mos=5** until proven otherwise; banners are not knobs.
- Startup ASCII 97¢ / HEDGE @ 65¢ → 75–90¢ / hedge 35¢ (toxic 65¢).
- `CURRENT.md` scp user `ntemusejoel`.
- Tests: assert example `entry_enabled: false`.

### Code fixes (need approval, prefer this order)

1. Log silent buy-path skips + FAK telemetry (`itode`, tick, min size, ask size, gate-to-POST ms).
2. After evidence: FAK limit = ask + 1 tick (≤ `buy_max_price`).
3. After-empty retry only if logs show cooldown actually wasting remaining size (today: 1 s — weak).
4. Skip last-trade when mid is valid; stop CLOB balance refresh on the happy POST path.
5. Pathlog WS or pause REST in the 5m window.

### Do not touch (this pass)

- Live `strategy_buy5m.json` knobs, `dry_run`, `entry_enabled`
- `polymintbot` / leftover CTF (buy bots will not sell mint inventory)
- Start 15m or hourly
- Merge three bots into a framework
- GTC / resting orders
- Truncate state, logs, heartbeats, PTB, research, pathlog ticks
- Commit `.env` or live strategy/state

---

## 8. Open questions (max 5)

1. On the VM, what are live `dry_run` / `entry_enabled` / `buy_budget` / `tick_size` in `strategy_buy5m.json`, and was the process started after the last JSON change? (Hot reload cannot arm dry_run.)
2. Of the morning 400s, is the **full** `error` string only unmatched FAK, or are there also `Size lower than the minimum: 5` / tick-size rejects?
3. May we add skip + FAK telemetry (no behavior change) as the first code slice?
4. If `mos=5` is enforced, is the operator willing to raise budget so 5 shares fit in-band, or should we skip rather than POST undersized FAKs?
5. Pathlog: keep competing on `/book` during the last 120 s for research, or pause/WS it until empty-FAK rate drops?

---

## Appendix — evidence this agent could / could not collect

| Requested | Result |
|---|---|
| `systemctl is-active` | **Not available.** CI SSH timed out 00:54 and 01:33 UTC. No VM login from this clone. |
| Live `strategy_buy5m.json` | **Not in workspace** (gitignored). Example + `_STRATEGY_DEFAULTS` agree with CURRENT.md. |
| Heartbeat advance | **Not on disk.** |
| `buybot5m.log` greps | **Not on disk.** Relied on operator 01:23–01:33 UTC narrative. |
| Pathlog `--grid` | No `pathlog/ticks` here. |
| Pathlog startup `retain_s` / `max_tick_bytes` | **Code** confirms 1209600 / 419430400. Live stdout not seen. |
| Public CLOB 5m metadata ~01:59 UTC | `itode=true`, `mts=0.01`, `mos=5` on several consecutive `btc-updown-5m-*` slots; 15m sample also `itode true`, `mos 5`, `mts 0.01`. |
| Gamma `series_slug` | Returns a mix of Dec 2025 leftovers and current windows (100 events, 86 with `end` in the future). Bot filters `end_ts > now`. Fragile but currently includes live markets. |
