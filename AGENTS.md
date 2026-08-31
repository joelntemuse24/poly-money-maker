# AGENTS.md — Poly Money Maker

Quick-reference for AI agents working on this codebase.
Full architecture — a guided tour of the live 5m bot, the `buy/` helpers,
and why the non-boilerplate code exists — is `TECHNICAL_DESIGN.md`.
**Live probe / ops decisions:** read `CURRENT.md` first (update it when strategy changes).

## Project at a Glance

**Live on the VM (after this change is deployed):** **5m buy bot**
(`polybuybot5m`) plus a no-order path recorder (`polypathlog`). 15m and
hourly buy services are **stopped**. Mint (`polymintbot`) is **paused**.

The 5m bot buys the winning leg of Polymarket BTC "Up or Down" markets
with **one $2.50** FAK. **Live since 27 Aug 2026 ~17:26Z** (confirmed
on the VM 31 Aug; knobs in live `strategy_buy5m.json`, **not** the
example file). Catalog: `docs/2026-08-31-last120-loss-catalog.md`.

| When (time to close) | Winning ask | Slice |
|---|---|---|
| Last **120s** (`TTM ≤ 120`) | **75–90¢** | **$2.50** late, FAK **90¢** |
| Same window | ≥90¢ | **off** (`late_90_start_s=0`) |
| TTM > 120s | none | early / ≥95 **off** |

`min_underlying_edge_usd` is **$0**. Code defaults in `buybot5m.py`
already look like last-120 / early-300 / edge $0; live JSON is the
overlay above. `BUY_HORIZON_S` is **120**. **Do not paste last-45 +
$25. Do not size up from $2.50.** See `CURRENT.md`.

Missed early does **not** become a $5 late buy — there is no early slice.
Same-leg add only (no straddle), and only if the late ask is ≥ **90¢**
(`add_min_price`). Flat late 75–90 is still a first entry. 91–99 is a
no on this overlay (`late_90` off). After a full hedge the market is done.
Normal hedge is **persist 5s @ 50/52** (GUI: held ≤ **52¢**, other ≥
**48¢**; not inverted 30/70). Then sell at the live bid while **< 53¢**
(fade through 50 still sells). Bid ≥ **53¢** (`hedge_recovery_cancel`)
holds and **clears persist**. Once holding, do **not persist-sell** while
live Chainlink TWAP is still on the held side of PTB
(`hedge_require_oracle`). Missing/stale BTC also holds. **Any live bag**
dumps bid-only at **≤32¢** even if BTC has not crossed yet
(`hedge_dump_ignore_oracle`). Do not sell 55–69 after persist. Winners
redeem at $1.00. **No profit-take sell.** See `CURRENT.md` for the active
probe knobs.

**Hourly (`polybuybothourly`) is still stopped.** Do **not** start it
unless the operator asks. 15m stays stopped.

| File | Service | Markets | Oracle | Budget | Window |
|---|---|---|---|---|---|
| `buybot.py` | `polybuybot` **stopped** | 15m | Chainlink TWAP 60s | $2.50 | final 4.0 min, 75–90¢, hedge 35/40 |
| `buybot5m.py` | `polybuybot5m` **live** | 5m | Chainlink TWAP 30s | $2.50 | last **120s** 75–90, edge **$0**; persist **5s @ 50/52** (dump 32¢) |
| `buybothourly.py` | `polybuybothourly` **stopped** | hourly | Binance BTCUSDT | $10 cap | last **20 min** 75–90¢; persist **5s @ 50/52** + oracle veto |
| `pathlog.py` | `polypathlog` **live** | all three | — (CLOB books only) | — | whole 5m; last 8m of 15m; last **20m** of hourly |

Plus: `check_book.py`, `check_participation.py`, `check_path_backtest.py`,
`check_fetch_trades.py`, `check_buy_skips.py`, `check_buy_rejects.py`,
`check_live_journal.py`, `check_edge_counterfactual.py`,
`check_reversal_features.py`, `check_last120_tick_autopsy.py`. Laptop glance
(not a bot): `widget/polydesk.py`.

## File Map

### Critical — changes here affect real money

| File | What it does | Mirrors |
|---|---|---|
| `buybot.py` (~5063 lines) | 15m buy bot | `buybot5m.py`, `buybothourly.py` |
| `buybot5m.py` (~5177 lines) | 5m buy bot (**live**) | `buybot.py`, `buybothourly.py` |
| `buybothourly.py` (~5061 lines) | Hourly buy bot (**stopped**) | `buybot.py`, `buybot5m.py` |
| `buy/market.py` | MarketGateway — Gamma discovery + metadata | — |
| `buy/btc_price.py` | Resolution-aligned BTC feeds (TWAP 30/60, Binance) + PTB | — |
| `buy/clob_book_ws.py` | CLOB market-channel WS top-of-book (buy/hedge speed path) | — |
| `buy/entry_skip.py` | **5m + hourly:** band union, skip labels, add-min, three-slice hourly budgets | not imported by 15m |
| `buy/hedge_gate.py` | 5m + hourly persist, recovery, fade, CLOB tick | not imported by 15m |
| `buy/book.py` | Shared TOB price+size parse (WS cache + pathlog) | — |

**Pattern:** The three bots are near-identical copies (slug, oracle, window
units, tick, defaults). A logic change to one usually needs the siblings.
**Exception:** 5m early/late bands (`BUY_HORIZON_S`, seconds) and hourly
three-slice bands (`BUY_HORIZON_MIN`, minutes) live in `buy/entry_skip.py`.
Do not copy 5m seconds-windows into 15m/hourly as minutes without converting,
and do not copy hourly `minutes_left` math into 5m (it must keep `seconds_left`).

`buy/chain.py` / `buy/contracts.py` are **mintbot only** (paused). Not on the
CLOB buy path.

Bots have **no** `if __name__ == "__main__"` — they run at import. Tests must
not `import buybot5m`. Import `buy/` / `check_*.py` / `pathlog.py`, or extract
functions with `ast` (`tests/test_buy_fill_shapes.py`).

### Safe to modify

| File | Purpose |
|---|---|
| `pathlog.py` | CLOB path recorder (no orders; TOB price **and** size) |
| `check_path_backtest.py` | Pathlog: grid, anatomy, compare, **paper hedge**, `--sweep`, `--hedge-sweep` (no orders) |
| `check_hedge_threshold.py` | Earlier-stop research: pathlog `--hedge-sweep` or public last-trade vs a history CSV |
| `check_reversal_features.py` | Research: Binance |dist|/vol/momentum vs 5m reversals (optional CLOB 75–90) |
| `CLOUD_RESEARCH.md` | Cloud prompts: paper P&L on public books + `--sweep` (no `.env`) |
| `check_book.py` | Diagnostic — inspect a live order book |
| `check_edge_counterfactual.py` | Diagnostic — resolution win rate if edge skips had filled |
| `check_participation.py` | Diagnostic — post-facto bought vs missed + band exposure |
| `check_fetch_trades.py` | Diagnostic — full-wallet Data API trade history → CSV |
| `check_buy_skips.py` | Diagnostic — why 5m did not buy (JSON log skip/attempt/fill counts) |
| `check_live_journal.py` | Replay the 5m live tape from JSONL hours later (no stream needed) |
| `check_last120_tick_autopsy.py` | Join live fills to ~1s pathlog (token_id / research slug) and score persist 0/1/2/5s @ 50/52 dump 32 |
| `buy/live_journal.py` | Tape event filter + line format (imported by 5m + the checker) |
| `check_buy_rejects.py` | Diagnostic — CLOB `invalid amounts` 400s that passed every gate |
| `widget/polydesk.py` | Local always-on-top Polymarket value / HOLDING glance (no orders) |
| `CURRENT.md` | Living ops/probe status — update when decisions change |
| `TECHNICAL_DESIGN.md` | Architecture for humans — update when the system changes |
| `strategy_buy*.example.json` | Buy-bot config templates — **not** loaded by bots |
| `mintbot.py` / `strategy_mint.example.json` | Paused mint helper — do not run live |
| `tests/test_*.py` | unittest suite (CI on PR + push to `main`) |

### Read-only / auto-generated — never edit

- `positions_buy*.json`, `pnl_buy*.json` — bot state and P&L (auto-generated)
- `*.log` — structured JSON-line logs with rotation
- `*.journal.jsonl` — 5m money-path tape (`check_live_journal.py`); do not truncate
- `.heartbeat*` — uptime tick counters
- `underlying_research_buy*.jsonl`, `ptb_*_buy*.json` — oracle/PTB decision audit
- `pathlog/ticks/*.jsonl` — recorded CLOB paths (**auto-pruned**: 14 days / 400 MB;
  export before prune deletes them — see below)
- `strategy_buy*.json` / `strategy_mint.json` (without `.example`) — live configs
- `positions_mint.json` — mint intents / daily spend
- `.env` — secrets (gitignored)

## Never Do

- **Never delete or truncate state files** (`positions_buy*.json`, `pnl_buy*.json`,
  `positions_mint.json`, logs, `*.journal.jsonl`, heartbeats, research/PTB files). Pathlog ticks are
  the exception: `pathlog.py` auto-prunes `pathlog/ticks/*.jsonl` (14 days / 400 MB)
  so they fit the small VM. Do **not** `rm` them by hand. **Do export** them
  (CSV or `scp` the ticks dir) before prune deletes old JSONL.
- **Never change `SLUG_PREFIX` or `SLUG_EXCLUDES` without checking all three bots** —
  `btc-updown` is a prefix of `btc-updown-5m`; mismatched exclusions cause
  cross-bot interference on the same market.
- **Never run a bot with `dry_run: false` against production unless you mean it** —
  there is no confirmation prompt.
- **Never commit live `strategy_*.json`, `.env`, or state files** — they are
  gitignored but double-check before `git add .`.
- **Do not restart `polymintbot` unless the operator asks.**
- **Do not start `polybuybot` / `polybuybothourly` unless the operator asks** —
  live trading is 5m-only (`polybuybot5m`).
- **Do not add a profit-take sell** unless the operator asks. Hedge is the only
  sell path. Take-profit was evaluated and dropped (unreachable on ≥90¢ fills;
  cuts $1.00 rides on 75–85¢ fills).

## How to Verify a Change

```bash
# First verify the selected strategy file has dry_run=true and entry_enabled=false.
# Never assume these commands are dry-run merely because they are launched manually.
python buybot.py          # 15m, uses strategy_buy.json
python buybot5m.py        # 5m, uses strategy_buy5m.json
python buybothourly.py    # hr, uses strategy_buyhourly.json

python pathlog.py         # recorder only — no orders
python check_path_backtest.py --grid --budget 2.5 --series 5m
python check_path_backtest.py --grid --budget 15 --series 5m
python check_path_backtest.py --ask-min 0.75 --ask-max 0.99 --ttm-max 120 --budget 15 --series 5m --csv /tmp/hits_15.csv
python check_book.py

# CI-equivalent (no network to Polymarket required for unit tests)
python3 -m py_compile buybot.py buybot5m.py buybothourly.py pathlog.py \
  check_path_backtest.py mintbot.py buy/book.py buy/clob_book_ws.py \
  buy/entry_skip.py buy/hedge_gate.py buy/live_journal.py \
 check_fetch_trades.py check_participation.py check_buy_skips.py \
 check_live_journal.py check_reversal_features.py check_last120_tick_autopsy.py
python3 -m unittest discover -s tests -p 'test_*.py' -v

# Full wallet fills (past the UI ~500-row export). Wallet: --user or FUNDER_ADDRESS.
# CSV columns match load_csv_buys: timestamp (unix), action, usdcAmount, tokenAmount,
# marketName, tokenName. Re-run merges/dedupes. Do not commit exports/.
python check_fetch_trades.py --user 0xYOUR... --out exports/trades.csv
python check_participation.py --hours 72 --csv exports/trades.csv
```

**Export pathlog ticks off the VM.** The recorder deletes oldest JSONL after
14 days or 400 MB. Prune is permanent. On the VM:

```bash
.venv/bin/python check_path_backtest.py --grid --budget 2.5 --series 5m --csv /tmp/hits.csv
.venv/bin/python check_path_backtest.py --compare --series 5m --budget 2.5
.venv/bin/python check_path_backtest.py --compare --paper --series 5m --budget 2.5
.venv/bin/python check_path_backtest.py --sweep --series 5m
.venv/bin/python check_path_backtest.py --anatomy --series 5m --ttm-max 120 --csv /tmp/anatomy.csv
.venv/bin/python check_path_backtest.py --export-market <slug> --csv /tmp/m.csv
# then scp /tmp/hits.csv (or scp -r pathlog/ticks) off the box
```

Watch the console for `[DRY BUY]` / `[DRY SELL]` markers. Ctrl-C to stop.

`--sweep` reads **late** keys from `strategy_buy5m.example.json`
(**75–90 / last 45s / $2.50**; `live_5m_paper` is that file). Pathlog
cannot replay the **$25** `|TWAP−PTB|` gate — score that in
`check_reversal_features.py`. `window_120s` is an explicit variant.
`--compare` uses hardcoded late-band presets; `--paper` hedge knobs still
come from the example JSON (**50/52 persist 5s**, dump **32¢**). Neither
command replays the early ≥90 / ≥95 union **or** the two-slice $2.50+$2.50
add.
`--series 5m` matches **only** 5m (not 15m — the string `15m` contains `5m`).

### What to look for

Live 5m (after this change is deployed and `polybuybot5m` restarted):

- `buy_attempt` `band=late` / `late_90` / `early` / `early_95` and `slice=early|late` — which 5m window armed
- `hedge_attempt` / `hedge_fill` — persist **5s @ 50/52** then sell live bid **< 53¢**, including fade through 50. Dump ≤ **32¢** even if BTC still agrees. Bid ≥ **53¢** holds.
- `hedge_skip_recovery` — persist_done but held bid ≥ 53¢; HOLD and clear persist. Do **not** sell 55–69.
- `hedge_skip_oracle_still_winning` / `hedge_skip_oracle` — live TWAP still on held side of PTB, or feed missing/stale; do not persist-sell.
- `hedge_skip_persist` — 50/52 + GUI passed but has not stayed qualified for 5s.
- `buy_skip_other_leg` — late winner is the other side of an early fill (no straddle)
- `buy_skip_hedge_closed` — market already dumped; no re-entry.

Stopped hourly (do not start unless asked):

- `buy_attempt` `slice=b15` — last-20m 75–90¢ FAK at 90¢. A/C windows are 0.
- `[FAK EMPTY]` / `buy_attempt_rejected` or `sell_attempt_rejected` with `unmatched_retry: true` — empty FAK re-quoted in the same trigger (up to 3 POSTs). Live 21 Aug 16:00–00:34Z: **0 `hedge_fill`**, every sell was attempt-1 `400 no orders found to match` then `hedge_fail` (buys already retried; sells did not).
- `buy_ghost_fill` `via=unmatched_400_guard` — unmatched 400 but inventory appeared; no second FAK
- `buy_attempt_ambiguous` `via=unmatched_400_no_balance` — unmatched 400 and CLOB balance unreadable; quarantine, no retry
- `cycle_error` in logs — unhandled exception. The bot process stays up.
  A fault **inside** `for m in _loop_markets` logs `condition_id` and **continues
  to the next market** (held-first order unchanged). A fault **outside** that
  loop (refresh, GC, redeem, UI) still aborts the rest of that poll. Banner
  does **not** sleep 5s. Structured `error` field is the exception
  type+message; `check_buy_skips.py` prints the breakdown.
  Aug 13–19 live 5m: **3294/3294** were `NameError: known_cost` until the
  19 Aug 09:42 restart picked up #80 (that NameError aborted every later
  market in the same poll; isolation is the fix for the next one).
  20 Aug: a 0.05s sleep with **POS 704** was leftover unredeemed 5m shares
  (Data API), not 704 live markets. Banner **POS** is live hedges only;
  **WAIT** is dust. Look interval is **0.01s**. Live JSON poll keys
  hot-reload; the loop-body fix needs `sudo systemctl restart polybuybot5m`.
- `hedge_tick_retry` — CLOB rejected a too-fine tick (`invalid tick size (0.001), minimum is 0.01`); same trigger rebuilds at 0.01. Pre-fix this was `[EXIT FAIL]` / `sell_build_rejected` and the dump never sold (22 Aug 11:40).
- `hedge_skip_toxic_book` — bid dipped but ask/spread still say "not reversed"
- `hedge_skip_no_consensus` — 50/52 book passed but GUI/last-trade still fail (held last print ≤ 52¢, held GUI ≤ 52¢, other GUI ≥ 48¢). Dump ≤32 skips this veto.
- `hedge_skip_toxic_recovered` — `toxic_fill` is armed but held bid > `hedge_toxic_bid_max` (32¢ winner book); dump stays armed, no sell
- `hedge_skip_incomplete_rest` — no REST/WS/last-good bid. Incomplete REST must **not** skip a dump; use WS/last-good
- `hedge_skip_dead_band` — persist done or qualify fired but bid is in (32¢, 50¢) **and** `hedge_sell_fade` is off; live 5m fade sells that band after persist
- `buy_skip_ambiguous` — GUI display prices too close (throttled 8s; **not** one event per market)
- `buy_skip_no_consensus` — ask in band but GUI/tight-book gate failed
- `buy_fill_below_band` — fill avg outside the open band; inventory persisted and `toxic_fill` armed (also if avg < 65¢)
- `buy_fill_walk` — confirmed shares > 1.05 × posted FAK size (normal when a 99¢/90¢ limit fills cheaper). Does **not** arm `toxic_fill` by itself
- `buy_ghost_fill` — balance reconciliation after null/delayed BUY confirm
- `buy_uncertain` — POST outcome unresolved; durable token/baseline quarantine blocks re-buy
- `buy_skip_incomplete_book` — missing GUI price on a leg (no mid and no last trade)
- `buy_skip_underlying_edge` — underlying gate failed (missing/stale/flat vs PTB; live 5m is **$25**)
- `buy_skip_underlying_side` — book wants the opposite leg from the underlying move
- `buy_window` — market first entered a 5m buy window (late 75–90 or last-45 ≥90; early/≥95 are off; one line per market)
- `buy_skip` `ask_below_band` / `ask_above_band` / `ask_out_of_band` / `no_ask` / `stale_positions` / `no_quote` / `stale_discovery` — in window, winning ask not in any open band, or the look had no book / stale wallet snapshot (throttled 8s)
- `buy_skip_add_below_min` — same-leg late add but ask < `add_min_price` (90¢). Flat first late 75–90 still buys.
- `buy_skip_hedge_closed` — this market already dumped; no other-leg chase and no re-entry
- `buy_skip_max_positions` — only if `max_open_positions > 0` (probe uses **0 = unlimited**)
- `buy_attempt_rejected` / skip reason `invalid_amount` — CLOB 400 `invalid
  amounts` (maker USDC must be **2** dp). 5m must **not** pass
  `user_usdc_balance` on BUY `OrderArgs`: a fake `$2.97` wallet makes the
  SDK shrink 3.00 @ 99¢ into `$2.9601`. `check_buy_rejects.py` counts these.
- `pathlog_prune` — oldest tick JSONL removed (14d / 400 MB cap); export first
- Banner `POS` — live hedges only. `WAIT` should be **0** unless a *redeemable*
  leftover is still cashing out. `WAIT 666` meant old Data API rows, not
  666 live markets. After `drop_wallet_dust`, those rows are thrown away.
- `sleeping 0.01s` — look interval after a short cycle. Skip ticks read WS
  only; REST is for a would-buy confirm + POST. If wall clock is still
  seconds, leftover bags or HTTP are still on the look (do not “fix”
  that by sleeping less)
- `[SETTLE SKIP] redeem skipped: zero position balance` — WAIT drain of a
  leftover 5m bag with nothing on-chain; blacklisted (`redeem_abandoned` in
  state, kept across restarts). Do **not** delete `positions_buy5m.json`.
  Do **not** try to sell them (no CLOB book). Real leftover CTF shares redeem
  at $1; ghosts stay in the Data API until Polymarket drops them.
- CLOB `404` `No orderbook exists` — a dead token is still on the REST/WS
  quote path (clockless `bought_token` must not be treated as a live hedge)

## Research loop (before changing live knobs)

Live JSON is one point in the space. Pathlog records **all three** series even
when 15m/hourly are not posting. Score alternatives on those ticks **before**
editing live `strategy_buy5m.json` (commands default to the 5m example
template; pathlog is ~1s TOB, not 1-minute CLOB candles):

```bash
python check_path_backtest.py --compare --series 5m --budget 2.5
python check_path_backtest.py --compare --paper --series 5m --budget 2.5
python check_path_backtest.py --sweep --series 5m
python check_path_backtest.py --hedge-sweep --series 5m --budget 2.5
python check_hedge_threshold.py --csv exports/trades.csv --hours 15
python check_path_backtest.py --anatomy --series 5m --ttm-max 120 --csv /tmp/anatomy.csv
python check_path_backtest.py --grid --budget 2.5 --series 5m
python check_buy_skips.py --since 2026-08-19T08:02:00
python check_live_journal.py --hours 5
python check_last120_tick_autopsy.py --since 2026-08-27T17:26:00
python check_reversal_features.py --hours 72
python check_reversal_features.py --hours 168
python check_reversal_features.py --hours 336
python check_reversal_features.py --csv exports/trades.csv --restart-utc 2026-08-27T08:57:16
```

`--sweep` scores the 5m **example** late template (**75–90 / last 45s /
$2.50** in `strategy_buy5m.example.json`; the **$25** edge is not in
pathlog) plus one-at-a-time window/band/size variants (`window_120s` is
included; the template's own TTM is not duplicated).
Paper hedge knobs come from that JSON (**50/52 persist 5s**, dump **32¢**,
mid-as-GUI when spread ≤ 10¢, held ≤ 52¢ / other ≥ 48¢; paper honors
`hedge_persist_s`). It does **not**
union the early ≥90 / ≥95 windows, the last-45s ≥90 overlay, or two $2.50
slices. Score last-45 90–99 separately with `--ask-min 0.90 --ask-max 0.99
--ttm-max 45 --budget 2.5 --series 5m --paper`.
`--hedge-sweep` keeps that late first touch and varies the **stop** (32/50/53
plus persist) and prints `winner_dumps` vs `loser_hedges`.
`--anatomy` answers “already decided at T-120 vs
50/50 until the end.” `--compare` is the named-preset table; add `--paper` to
walk later ticks. `--grid` is ask × time. **`--series 5m` matches only 5m**
(not 15m). Pathlog cannot replay last-trade, BTC/PTB, or POST latency.
Cloud agents: `CLOUD_RESEARCH.md`.

## Key Conventions

- **All bots are single-file polling loops** — no `asyncio` or database. Small
  thread pools isolate book, refresh, notification, and redemption-status I/O.
  CLOB book WS and BTC RTDS each have a daemon thread + in-memory cache.
  Section comment banners (`# --- PRICING ---`) act as visual boundaries.
  Process lock: `/tmp/poly-money-maker-buybot5m.lock` (and siblings).
- **Hot-reload:** Strategy JSON files are re-read every cycle via `load_strategy()`.
  Changes take effect on the next tick — no restart needed. `dry_run` is
  startup-only (selects `*.dryrun.*` state paths).
- **5m look-ahead:** `BUY_HORIZON_S = max(buy_start_s, early_buy_start_s,
  early_95_start_s)` (**45s** after the live paste: 45/45/0). Hot poll /
  WS subscribe from ~T-75. Code defaults stay 120/300/300 until JSON
  overlays them. `early_95_start_s=0` is allowed (disable ≥95); it used
  to fail `must be positive` and take 5m down. Pair with `early_95_min_s=0`
  so leftover `min_s=60` cannot disable entries.
  Sleep is **0.01s** in that window or while a **live** hedge is open.
  That look reads the live WS book. REST is only when the look says buy
  (confirm + POST). Data API leftover 5m shares (hundreds) are **dropped**
  after download — they must not be stubbed, printed, REST-quoted, or shown
  as WAIT. Live JSON `poll_*` hot-reloads; the loop-body filter needs a 5m restart.
- **Atomic durable save:** State and P&L files flush + `fsync` the `.tmp`, replace
  it, then `fsync` the parent directory before an order can proceed.
- **Fail-closed startup:** A valid strategy file is required at startup. A
  missing/malformed hot reload disables new entries while retaining the
  last-known-good hedge parameters. A per-bot process lock prevents duplicate
  live instances.
- **FAK orders only:** All orders are Fill-And-Kill — no resting orders, no market
  making. 5m buys are **limit** FAKs at the **open band max** (late **90¢**,
  last-45 ≥90 **99¢**) sized `budget/ask` per slice, **at least 3 shares** when
  `3 × limit` fits in `buy_max_spend` $3 (3.00 sh / $2.97 at 99¢, not 2.00 /
  $1.98; `buy_max_shares` 5; not displayed top size). Early ≥90 is **off**. Hourly (stopped) uses
  the same limit-FAK sizer: A/C at **99¢**, B at **90¢**,
  `buy_max_spend` **$11**, `buy_max_shares` **14**. Hourly template is B-only
  (A/C windows **0**). 15m (stopped) still pins
  the limit to the quoted ask. Sells stay
  share-denominated market FAKs. A 400 **"no orders found to match"** re-quotes and
  POSTs again (up to 3) in the same trigger; invalid-amount / auth 400s and unclear
  POSTs do not. After a fully empty trigger, wait `empty_fak_cooldown_s` (0.15 s).
- **Hedge is sell-only exit:** The bots never profit-take. The only sell path is the
  hedge: REST shows bid ≤ threshold **and** ask ≤ require_ask_max with tight
  spread, **and** Polymarket GUI + last trade agree with that book, **and**
  that qualify holds for `hedge_persist_s` (5m **5s**). Live 5m: held last print
  ≤ **52¢**, held GUI ≤ **52¢**, other GUI ≥ **48¢** (complement of ask-max).
  Instant 70/2s-at-70 is out — those dumped winners. Buy 70/30 is unchanged.
  15m (stopped) still invert 70/30. Live 5m book qualify is **persist 5s @
  50/52**; toxic dump **32¢** book-only (`hedge_dump_ignore_oracle`);
  recovery **53¢**; `hedge_sell_fade`; persist still needs Chainlink TWAP
  against/flat (`hedge_require_oracle`). Stopped hourly template is the same
  50/52 persist with dump **35¢** still oracle-gated. 15m remains 35/40.
  A random TOB clip is not enough; a last print of 85¢ on a 48/51 book will
  `hedge_skip_no_consensus`. **Any live bag** dumps bid-only while held bid
  ≤ **32¢** (no GUI veto; not only `toxic_fill`). After persist, sell at the
  live bid while **< 53¢** (including a fade through 50). Bid ≥ **53¢**
  (`hedge_recovery_cancel`) holds and **clears persist**
  (`hedge_skip_recovery`) — do not sell 55–69 because persist stuck.
  Everything else rides to redemption at $1.00. WS may *arm* a hedge
  check; normal sells need two-sided REST. After `hedge_closed`, no later
  buy on that market. **Live `strategy_buy5m.json` must set the hedge
  keys** (50/52 persist 5s, dump 32, recovery 53, `hedge_sell_fade`,
  `hedge_require_oracle`, `hedge_dump_ignore_oracle`, `late_90_start_s` 45)
  or an old 70/72/85 file keeps the old qualify after hot reload.
- **Tick sizes:** 5m *default* is `0.001`, but some 5m books are `0.01`.
  Hedge FAKs must use the CLOB minimum. 15m and hourly stay `0.01`.
- **One fill per slice:** 5m `early_bought` / `late_bought`. Hourly
  `t22_bought` / `t15_bought` / `t5_bought` (same-leg add up to a **$10**
  spend cap; slice A never more than **$5**). `bought_token` still means the
  held leg. The other leg is `buy_skip_other_leg`. `one_entry_per_market`
  stays true.
- **Notifications:** Fire-and-forget via ntfy.sh (topic `polybot-joel-btc`).
  Never let notification failures crash the bot.
- **GCP VM:** `~/poly-money-maker` on a small ~10GB e2 disk. Journal filled it
  once (`deploy/DISK_OPS.md`). Pathlog is capped in-app. CI `git pull`s; it
  does **not** restart systemd.

## Landmines

1. **The three bots are near-identical copies, not shared modules.** A bug fix in
   `buybot.py` probably also applies to `buybot5m.py` and `buybothourly.py`.
   `buy/entry_skip.py` and `buy/hedge_gate.py` are imported by **5m and hourly**,
   not 15m. Persist completes only on a still-qualified 50/52+GUI tick — do not
   pre-complete `persist_done` from elapsed wall clock alone.
2. **The 5m bot uses seconds-based window checks** (live paste:
   `buy_start_s = 45` late 75–90¢; `late_90_start_s = 45` for ask ≥ 90¢;
   `early_buy_start_s = 45` so early is off; `early_95_start_s = 0`).
   Code defaults stay 120/300/300 until JSON overlays them. 15m uses
   minutes (`buy_window_min = 4.0`) and hourly uses minutes (template:
   `b15_window_min = 20`, A/C windows **0**; code still documents 22/15/5).
   Don't mix them when propagating changes. The 5m loop must define
   `seconds_left` (not only `minutes_left`) or it NameErrors every cycle.
   Hourly must **not** define 5m's `seconds_left = (end_ts_ms - now_ms) / 1000`.
3. **Settlement is confirmation-gated.** A relayer submission is not P&L.
   Redemption is credited only after relayer confirmation and a complete Data API
   snapshot shows the inventory gone; GC never invents par value.
4. **No `if __name__ == "__main__"` guard** — buy-bot scripts execute at module
   level. They cannot be imported. `pathlog.py`, `check_path_backtest.py`,
   `check_fetch_trades.py`, and `check_live_journal.py` can.
5. **Ask ≠ price.** 97¢ ask over 1¢ bid is not a tradable favorite. Entry needs
   a tight REST book; hedge needs the whole book + GUI to look lost.
6. **POS ≠ wallet bags.** Data API returns every unredeemed 5m share. A 0.01s
   sleep with a 5s wall clock means the cycle walked dust (Rich table, REST
   404s, fake stubs). Do not "fix" that by sleeping less.
7. **Pathlog resolve must not Gamma-poll the whole 14d retain dir every 1s
   cycle.** Unresolved JSONL that Gamma never marks 0.99/0.01 stay pending
   forever. VM 31 Aug overlay `mean_ticks=1.0` — sampling starved. Cap
   (`RESOLVE_MAX_PER_CYCLE=8`, `RESOLVE_LOOKBACK_S=6h`). Restart
   `polypathlog` only (no orders) after that fix; do not restart 5m.

## Systemd Services (in `deploy/`)

| Service file | Bot | Live? |
|---|---|---|
| `polybuybot.service` | `buybot.py` (15m) | **stopped** |
| `polybuybot5m.service` | `buybot5m.py` (5m) | **yes** |
| `polybuybothourly.service` | `buybothourly.py` (hourly) | **stopped** |
| `polypathlog.service` | `pathlog.py` (CLOB path recorder; no orders) | **yes** |
| `polymintbot.service` | `mintbot.py` | **paused** |

CI: pushes to `main` touching the buy bots, `pathlog.py`,
`check_path_backtest.py`, `buy/`, or `requirements.txt` deploy to the VM via
SSH (`git pull` + `pip install` only). Services are **not** auto-restarted —
start/restart deliberately after validation (`systemctl start` / `restart`).

Live overlay is already last-120 / edge $0 (27 Aug 17:26Z). **Do not
paste last-45 + $25.** **Do not paste a new `strategy_buy5m.json`
overlay or restart** unless the operator asks after they buy a
recommendation. See `CURRENT.md` and
`docs/2026-08-31-last120-loss-catalog.md`. Do **not** start 15m,
hourly, or mint.

## Dependencies

```
py_clob_client_v2            # Polymarket CLOB SDK
py-builder-relayer-client    # Relayer proxy transaction builder/signing
py-builder-signing-sdk       # POLY_BUILDER_* HMAC authentication headers
requests                     # HTTP for Data API, Gamma API, ntfy, relayer
python-dotenv                # .env loading
rich                         # Terminal UI (tables, panels)
eth-abi, eth-utils, eth-account  # Redeem calldata encoding
websocket-client             # CLOB market-channel WS (buy/clob_book_ws.py)
web3                         # transitive of the CLOB client stack
```
