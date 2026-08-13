# Full-system audit & brainstorm prompt

**How to use this:** paste the whole file (or “Mission” through “Output”) into a new
agent session as the task. The agent must **audit first**, then **brainstorm**.
Do **not** change live strategy, restart mint, or POST orders unless the operator
explicitly says so after reading the report.

Last calibrated: **2026-08-13**. Re-read `CURRENT.md` on the VM; if it disagrees
with this file, **CURRENT.md + live `strategy_buy5m.json` win**.

---

## Mission

Make the **$2.50 late-window CLOB buy** of Polymarket BTC Up/Down actually work:
buy the winning leg in the 75–90¢ band, hedge only on a real reversal, redeem
winners at $1.00. Right now the 5m bot is **alive and firing**, but many FAKs
come back empty (`no orders found to match`). That is the central live symptom.

You must:

1. Verify every relevant part of the codebase **does what the strategy believes**.
2. Find **silent or unnoticed contradictions** (docs vs code vs example JSON vs
   live JSON vs the three bot copies vs unlogged `continue`s).
3. Propose **process, fill-speed, polling, and infrastructure** improvements
   grounded in this repo and Polymarket’s surfaces — not generic trading advice.
4. Rank what would actually move the fundamental aim (fills at a good price that
   still win) vs what is polish.

Success is a written report with evidence (file:line, log events, config keys).
Code changes are **out of scope until the operator approves** a slice.

---

## Non-goals / safety

- Do **not** start `polymintbot` or 15m/hourly buy bots unless asked.
- Do **not** set `dry_run: false` or `entry_enabled: true` in examples.
- Do **not** truncate `positions_buy*.json`, `pnl_buy*.json`, logs, heartbeats,
  PTB/research files, or live `strategy_*.json`.
- Do **not** `rm` `pathlog/ticks` (auto-pruned 14d / 400 MB; **export first**).
- Do **not** commit `.env` or live strategy/state.
- Buy-bot scripts have **no** `if __name__ == "__main__"` — never `import buybot*`.
- Never treat `strategy_buy*.example.json` as what the VM is running.

---

## Live truth (2026-08-13, VM `instance-20260516-185922`)

Confirm on the box; do not assume this aged well.

| Unit | Expected |
|---|---|
| `polybuybot5m` | **active** — only live money |
| `polypathlog` | **active** — no orders; 5m+15m+hourly ticks |
| `polybuybot` / `polybuybothourly` | **inactive** |
| `polymintbot` | **inactive** (paused; leftover mint sold by hand) |

**Intended 5m knobs** (`CURRENT.md` + `strategy_buy5m.example.json`; live file is
gitignored — **diff it on the VM**):

| Knob | Value |
|---|---|
| `buy_budget` | $2.50 |
| Ask band | 75–90¢ (75¢ = trigger, 90¢ = ceiling; FAK at **live** ask) |
| GUI | winner ≥ 70¢, loser ≤ 30¢, gap ≥ 5¢ |
| Tight book | winner bid ≥ 70¢, spread ≤ 5¢ (`max_entry_spread`) |
| Window | last **120 s** (`buy_start_s`) |
| Hedge | bid ≤ 35¢ **and** ask ≤ 40¢, spread ≤ 15¢ |
| `toxic_force_exit_below` | 65¢ (entry-fill dump, **not** the hedge trigger) |
| Underlying | Chainlink TWAP 30s, `min_underlying_edge_usd` $5, side must match |
| `max_open_positions` | 0 = unlimited |
| Tick | **0.001** |
| Poll | 0.1 s in window, 0.05 s while held, 1 s idle |

**Observed the same morning (not a crash):**

```
buy_attempt → buy_attempt_rejected
  PolyApiException 400: "no orders found to match with FAK order..."
buy_fail status=empty
```

Examples: down @ 81–90¢ (01:23–01:29 UTC), up @ 86–87¢ (01:33 UTC). GUI/consensus
looked fine. Heartbeat advancing. `gc` after each market. **Process healthy,
inventory not increasing.** Treat unmatched FAK as a first-class audit object,
not “bot broken.”

Pathlog startup must show `"retain_s": 1209600, "max_tick_bytes": 419430400`.

---

## Read order

1. `CURRENT.md` (ops truth)
2. `AGENTS.md` + this file
3. `TECHNICAL_DESIGN.md` §§6–9, 12–14 (buy, hedge, redeem, errors, latency, landmines)
4. Live `strategy_buy5m.json` on the VM vs `strategy_buy5m.example.json`
5. `buybot5m.py` (live path) then diff vs `buybot.py` / `buybothourly.py`
6. `buy/clob_book_ws.py`, `buy/btc_price.py`, `buy/market.py`
7. `pathlog.py`, `check_path_backtest.py`, `check_participation.py`
8. `tests/test_buy_fill_shapes.py` (policy that is already encoded)
9. `deploy/*.service`, `.github/workflows/deploy.yml`, `deploy/DISK_OPS.md`

---

## Part A — Correctness audit (must pass)

Work through every subsection. For each: **what the strategy intends**, **what
the code does**, **how you know** (citation), **fail-closed or fail-open**,
**unlogged skip?**

### A0. Live config vs docs vs code

- Diff VM `strategy_buy5m.json` against `CURRENT.md` and `_STRATEGY_DEFAULTS` /
  `strategy_buy5m.example.json`.
- `dry_run` is **startup-only** (`STARTUP_DRY_RUN`). Hot-reload cannot arm/disarm
  live vs dry state files. Confirm the running process was started with the
  intended pair (`entry_enabled` + `dry_run`).
- Invalid hot reload: entries off, hedge params kept. Confirm that still holds.
- Example JSON is disarmed (`entry_enabled: false`, `dry_run: true`) — good.
  Banner comments in `buybot5m.py` (old “97¢ / HEDGE @ 65¢”) must not be treated
  as live knobs.
- `CURRENT.md` Ops still says `systemctl restart polybuybot polybuybot5m
  polybuybothourly`. Live policy is **5m only**. Flag leftover “restart all three”
  text as a contradiction that can start unwanted bots.

### A1. Buy gate chain (market → POST)

In `buybot5m.py` the per-market loop is ~gates then `buy_market_with_retry`.
Walk **in order**. Confirm none of these can fire a FAK without the others:

1. `held_size > 0.01` → no second buy (often **silent**)
2. `seconds_left > BUY_START_S` → out of window (**silent**)
3. `ENTRY_ENABLED`
4. `_discovery_fresh` (Gamma cache 5 s; stale → no new entries, hedges continue)
5. Positions snapshot age ≤ `max(5, positions_refresh_s*3)`
6. Balance snapshot age ≤ `max(30, balance_refresh_s*3)`
7. `meta["buy_uncertain"]` quarantine
8. `one_entry_per_market` + `bought_token`
9. `buy_grace_s` (5m default 1 s) after first sighting
10. `max_open_positions` / notional / daily / USDC balance
11. **Force-REST** books + last-trade (entry must **not** trust WS alone)
12. Incomplete GUI (`buy_skip_incomplete_book`)
13. Ambiguous GUI (`buy_skip_ambiguous`, `min_bid_edge`)
14. Ask in `[buy_threshold, buy_max_price]` on the **winning** leg
15. GUI consensus + `entry_book_ok` (tight REST book) else `buy_skip_no_consensus`
16. Underlying edge + side (`buy_skip_underlying_edge` / `_side`)
17. Cooldown `buy_cooldown_s`
18. Write-ahead `buy_uncertain` + `save_json` **before** POST
19. FAK priced at **fresh REST ask**, size clipped to visible ask size × budget

Pay special attention to **silent continues** (no `log_event`). Those are how
issues stay unnoticed. List every buy-path `continue` that does not log.

### A2. Book path: WS vs REST vs GUI

- `get_quote_fast`: WS first unless `prefer_rest` / `force_rest`; REST min interval
  **200 ms**; WS stale **2 s**.
- Entry: `force_rest=True` on both legs + last-trade. Confirm sequential `.result()`
  vs true overlap (`_entry_executor`).
- GUI: mid if spread ≤ 10¢ else last trade (`POLYMARKET_GUI_SPREAD`). Last-trade
  cache 0.25 s; book-snapshot last trade only if snapshot ≤ 2 s.
- Prefetch: `_book_executor` (4 workers), up to 30 pending; WS overlay 0.25 s for
  held tokens, 2 s otherwise. Confirm stale-high WS **cannot** suppress a needed
  hedge (see A4) and **cannot** arm a buy.
- `buy/clob_book_ws.py`: reconnect clears cache; price-change events may zero size
  — REST required before sizing. Three buy bots = **three WS connections**.
- Tick size 0.001 enforced in `load_strategy` for 5m. Confirm FAK prices snap to
  tick and `hedge_min_price` 0.325 is a legal tick.

### A3. FAK execution, empty fills, quarantine

This is the live pain. Trace `buy_market_with_retry` + `ImmediateResponseClobClient`:

- FAK only; SDK trade-poll bypassed (bot owns confirm).
- `max_retries=3` but **definitive 400** (`buy_attempt_rejected`, including
  unmatched FAK) **must not retry** with a second full budget.
- `status=empty` / `unmatched` → `buy_fail` + `_clear_buy_uncertain`. Confirm
  that does **not** leave a false `bought_token` and does **not** block the next
  market — but **does** block re-buy of the **same** market if `bought_token` was
  set incorrectly. Prove it from code.
- Ambiguous POST → quarantine stays; inspect exact order id on later cycles.
- Ghost path: `_reconcile_ghost` waits 0.4/1/2 s — can it invent size?
- Fill avg &lt; `buy_threshold` → persist inventory + `buy_fill_below_band`;
  avg &lt; `toxic_force_exit_below` → `toxic_fill` force-exit (not ride to $1).
- All CLOB SDK calls serialized on `_clob_lock`. Estimate stall if pathlog +
  Data API + book REST pile up on the same process.

**Question the unmatched FAK must answer:** at POST time, was the ask already gone
(REST snapshot stale by one RTT), size too large vs remaining depth, price not
tick-aligned, or the book never had size at that ask? Use pathlog ticks for the
same slugs around 01:23–01:33 UTC 2026-08-13 if still on disk.

### A4. Hedge vs toxic dump

Two different mechanisms — docs sometimes collapse them into “hedge at 65¢”:

| Mechanism | Trigger | Integrity |
|---|---|---|
| Normal hedge | bid ≤ 35¢ **and** ask ≤ 40¢, spread ≤ 15¢ | force-REST two-sided; bounce cancel |
| `toxic_fill` | **entry** avg &lt; 65¢ | dump now; no bounce cancel; floor = one tick |

Confirm:

- WS may **arm** a check (`hedge_quote_max_age_s=0.25`); WS bid **above** 35¢
  skips REST (must not hide a dump we need; must not sell on WS alone).
- Incomplete REST → `hedge_skip_incomplete_rest`, **no WS sell**.
- Spoof penny bid under high ask → `hedge_skip_toxic_book`.
- `hedge_uncertain` blocks replacement sells until exact-order reconcile.
- Data API zero alone never closes a hedge (`hedge_ghost_unconfirmed`).
- Redeem is deferred while hedge inventory exists or entry window is open.

### A5. Oracle / PTB (`buy/btc_price.py`)

- 5m: TWAP 30s. 15m: TWAP 60s. Hourly: Binance BTCUSDT. Do not mix.
- Live stale **5 s** → fail closed (`live_stale`).
- PTB: nearest tick to `start_ts`, skew ≤ 2 s; missing PTB → no entry.
- `edge_usd = live - ptb`; favored up if edge &gt; 0 else down.
- **`edge == 0` fails even if `min_underlying_edge_usd` is 0.**
- Negative edge on a **down** buy is correct (BTC below PTB). The 01:28 attempts
  had `edge_usd ≈ -40` and `leg: down` — confirm the side gate would pass and
  the $5 minimum is on **absolute** edge.
- PTB capture only if the process is up at window open. Restart mid-market →
  `missing_ptb` until next market.

### A6. State, GC, redeem, PnL

- Atomic `save_json`: tmp + fsync + replace + dir fsync **before** POST.
- Redeem: relayer `STATE_CONFIRMED`/`MINED` **and** Data API inventory gone
  (≤ 0.01). GC never invents $1 par (`gc_par_redeem`).
- Uncertain quarantines survive expiry; GC must not wipe them.
- Process lock `/tmp/poly-money-maker-buybot5m.lock`.
- Heartbeat `.heartbeat_buy5m` is a unix timestamp rewritten each cycle.

### A7. Three-file drift + slugs

Diff `buybot5m.py` vs `buybot.py` vs `buybothourly.py` for:

- Window units (`buy_start_s` vs `buy_window_min`) — copy-paste NameError risk
- Tick size / `hedge_min_price`
- Oracle source
- `SLUG_PREFIX` / `SLUG_EXCLUDES` (`btc-updown` is a prefix of `btc-updown-5m`)
- Fill/quarantine/hedge policy (tests in `test_buy_fill_shapes.py` already
  assert some cross-bot markers — use them, then find gaps)

### A8. Pathlog vs live trading

- Separate process, no orders, no `.env`.
- REST `/book` ~1 Hz × 2 tokens × up to 3 series, 8 workers — **same VM IP** as
  the buy bot’s force-REST entry/hedge calls.
- Could add RTT/429; cannot match a FAK by itself. Still ask: should pathlog
  move to **WS** so it stops competing on `/book` during the 5m window?
- Prune 14d / 400 MB; export CSVs off-box. Do not confuse with bot state.

### A9. Tests vs reality

Covered: fill parsing, write-ahead, hedge/entry book gates, GC guards, example
JSON disarmed, pathlog prune, backtest helpers.

**Not covered (call out, don’t silently “fix” by adding huge mocks unless asked):**
live FAK unmatched, WS reconnect, `get_quote_fast` handoff, 429s, RTDS/PTB,
relayer confirm, slug-exclusion integration.

---

## Part B — Contradiction hunt (explicit table)

Build a table: **Source A says X / Source B says Y / Reality / Severity**.

Known suspects to confirm or retire:

1. `CURRENT.md` “restart all three buy bots” vs 5m-only live.
2. Code banners / leftover “HEDGE @ 65¢” vs `hedge_threshold` 35¢ vs
   `toxic_force_exit_below` 65¢.
3. `TECHNICAL_DESIGN.md` skip text still saying “live oracle &lt; $10 from PTB”
   while 5m is $5.
4. AGENTS.md glance table listing 15m/hourly as “live on the VM.”
5. Silent `continue` vs logged `buy_skip_*` — operator cannot see why a cycle
   did nothing.
6. Example JSON disarmed vs VM live armed — agents “verifying” with examples.
7. `max_retries=3` vs definitive unmatched 400 (must be 1 attempt).
8. Pathlog records 15m/hourly while those buy bots are stopped — good for
   research, easy to misread as “those bots are live.”
9. Three copies: a 5m-only fix not propagated (or worse, 15m minutes logic
   copied into 5m).

If you find a contradiction that can **start extra bots, double-buy a market,
sell on a spoof book, or credit PnL without redeem confirm**, mark it **P0**.

---

## Part C — Brainstorm (do not implement yet)

Aim: **more good fills, fewer empty FAKs, no safety regressions.**

For every idea: mechanism, expected fill/latency effect, risk (double-spend,
toxic inventory, 429s), how to measure on this VM, whether it needs a code
change vs a knob vs ops. Prefer experiments that pathlog / logs can score.

### C1. Why FAKs miss (priority)

Hypothesis stack — accept/reject with evidence:

1. **RTT:** force-REST ask is already lifted by POST time (one CLOB RTT + fsync
   + sign). WS-trigger + REST-confirm-at-POST vs today’s poll + REST-quartet +
   REST-again-in-retry.
2. **Size vs depth:** $2.50 at 85¢ needs ~2.94 shares; book shows 1 share at
   touch. FAK kills rather than partial? (CLOB FAK is partial-fill-or-kill if
   *no* match — partials may exist if *some* match. Confirm from SDK/docs/logs
   `matched` vs `unmatched`.)
3. **Tick / price:** posting at a GUI-ish 0.86 when book ask is 0.861.
4. **Rate limit / lock:** `_clob_lock` + pathlog `/book` delay the snapshot.
5. **Cooldown / one-entry:** after empty fail, can the same market retry inside
   the 120 s window? If `bought_token` or cooldown blocks, empty FAK = missed
   market even though liquidity returns 2 s later. **This is a likely silent
   killer — prove it.**
6. **Band too high:** 81–90¢ is the thin part of the book; 75¢ prints have more
   size. Use `check_path_backtest.py --grid` + `--series 5m`.

### C2. Polling vs event-driven

Today: 100 ms poll in window, then several REST round-trips, then POST.
`TECHNICAL_DESIGN.md` §13: bigger VM does **not** fix CLOB RTT.

Brainstorm (keep fail-closed REST at POST):

- WS-triggered “maybe buy” → existing REST gates → POST
- Parallelize the entry quartet (2 books + 2 last-trades) with no sequential wait
- Skip last-trade when mid exists and spread ≤ 10¢
- Shorter `buy_grace_s` / `buy_cooldown_s` after **empty** fails only
- Prefetch force-REST *during* the 100 ms sleep, not after

Hedge must stay latency-critical: never add AI, ntfy, or redeem I/O on that path
(already fire-and-forget / deferred — verify nothing regressed).

### C3. Polymarket infrastructure

Map what we use and what we underuse:

| Surface | Role now | Question |
|---|---|---|
| CLOB REST `/book` | Truth for entry/hedge/POST | Batch? WS enough until POST? |
| CLOB WS market channel | Monitor / hedge peek | Subscribe-only pathlog? Event-driven entry? |
| `/last-trade-price` | GUI fallback | Needed every cycle? |
| Gamma | Discovery | 5m window vs 5–10 s cache + 120 s stale |
| Data API positions | Caps, GC, redeem | 1 s refresh vs 3 s entry gate |
| Relayer v2 | Redeem only (mint paused) | Don’t let redeem contend with hedge |
| Builder HMAC | Relayer auth | — |
| RTDS Chainlink TWAP | Edge/side | 5 s stale; PTB miss on restart |
| User WS / trade channel | **Not used** | Faster fill confirm than REST poll? |

Do **not** propose GTC/resting bids (strategy is FAK-only by design) unless you
argue a new strategy with kill-switch and inventory risk spelled out.

### C4. Our infrastructure

- One small ~10 GB Debian VM, journal 50 MB, pathlog 400 MB.
- CI `git pull` + `pip install` — **does not restart** units (good).
- Four potential CLOB clients; live is 5m + pathlog.
- Region/RTT to `clob.polymarket.com` vs CPU — measure, don’t guess.
- ntfy failures must never block (confirm executor).
- Shared wallet with paused mint: leftover CTF inventory is **not** auto-sold
  by buy bots.

Ideas to evaluate, not rubber-stamp: pathlog on WS; stop pathlog during the
last 120 s of each 5m (research cost vs fill cost); colocated/non-burstable
VM; **do not** merge three bots into one process unless the audit shows
lock/RTT contention is the unmatched cause.

### C5. Strategy (knobs vs logic)

Only after A/B/C1. Possible research questions for pathlog `--grid`:

- Trigger 75¢ vs waiting for 85¢ (more size vs worse price / more reversals)
- Window 90 s vs 120 s vs 180 s on 5m
- GUI 70/30 vs tighter
- Edge $5 vs $0 vs $10 (counterfactual: `check_edge_counterfactual.py`)
- Budget $2.50 vs clipping to top-of-book size (more fills, less size)

Do not retune live JSON in this pass.

---

## Part D — Evidence to collect (read-only on the VM)

```bash
systemctl is-active polybuybot5m polybuybot polybuybothourly polypathlog polymintbot
python3 -m json.tool strategy_buy5m.json | head
cat .heartbeat_buy5m; sleep 2; cat .heartbeat_buy5m
grep -E '"event": "(cycle_error|buy_attempt|buy_attempt_rejected|buy_fail|buy_fill|buy_uncertain|buy_ghost|buy_skip_|hedge_|pathlog_prune)"' buybot5m.log | tail -80

# empty-FAK rate
grep buy_attempt_rejected buybot5m.log | tail -20
grep '"status": "empty"' buybot5m.log | wc -l

.venv/bin/python check_path_backtest.py --grid --budget 2.5 --series 5m --csv /tmp/hits.csv
```

If you SSH: never restart services; never edit live JSON.

---

## Output format

Write a single report with these headings:

1. **Executive** — is the 5m bot correct and live? unmatched-FAK verdict in one
   paragraph.
2. **Contradictions table** — source / says / reality / severity (P0–P3).
3. **Silent gaps** — unlogged continues, missing metrics (e.g. no
   `buy_skip_fak_empty` counter, no time-from-gate-to-POST histogram).
4. **Correctness findings** — gate/hedge/oracle/state; file:line.
5. **Fill-path timeline** — one annotated sequence from “ask in band” to FAK
   400, with estimated delays (poll, REST×N, fsync, sign, RTT).
6. **Brainstorm ranked** — P0 experiments vs later; each with measure + risk.
7. **Doc fixes** (safe) vs **code fixes** (need approval) vs **do not touch**.
8. **Open questions** for the operator (max 5).

Tone: specific, skeptical of docs, kind to fail-closed design. Do not propose
rewriting the three bots into a framework in this pass unless a P0 bug is
literally unfixable without it.

---

## If you are allowed a tiny follow-up PR after the report

Only with operator OK, and only one slice, prefer in this order:

1. Doc contradictions (`CURRENT.md` 5m-only restart; 65¢ vs 35¢ wording).
2. Log silent buy-path skips (so the next unmatched hour is diagnosable).
3. After-empty retry policy if the audit proves cooldown/`bought_token` burns
   the market on unmatched FAK.

No live knob changes, no mint, no 15m/hourly start.
