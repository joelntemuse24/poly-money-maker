# AGENTS.md — Poly Money Maker

Quick-reference for AI agents working on this codebase.
Full architecture: `TECHNICAL_DESIGN.md`.
**Live probe / ops decisions:** read `CURRENT.md` first (update it when strategy changes).

## Project at a Glance

**Live on the VM:** **5m buy bot only** (`polybuybot5m`) plus a no-order path
recorder. 15m and hourly buy services are **stopped**. The 5m bot buys the
winning leg of Polymarket BTC "Up or Down" markets at **75–90¢** in the final
120s, or **above 90¢** in the first 3 minutes ($2.50 / market), hedges at
**35¢** on reversal, and redeems winners at $1.00. See `CURRENT.md` for the
active probe budget and knobs.

Mint-only helper (`mintbot.py`) is **paused** — do not run it live.

| File | Service | Markets | Oracle | Budget | Window |
|---|---|---|---|---|---|
| `buybot.py` | `polybuybot` | 15m | Chainlink TWAP 60s | $2.50 | final 4.0 min |
| `buybot5m.py` | `polybuybot5m` | 5m | Chainlink TWAP 30s | $2.50 | last 120s 75–90¢; first 3 min >90¢ |
| `buybothourly.py` | `polybuybothourly` | hourly | Binance BTCUSDT | $2.50 | final 13.0 min |
| `pathlog.py` | `polypathlog` | all three | — (CLOB books only) | — | late-window ticks |

Plus: `check_book.py`, `check_participation.py`, `check_path_backtest.py`, `check_fetch_trades.py`, `check_buy_skips.py`.
Local glance (not a bot): `widget/polydesk.py` — always-on-top balance / HOLDING.

## File Map

### Critical — changes here affect real money

| File | What it does | Mirrors |
|---|---|---|
| `buybot.py` | 15m buy bot (~1716 lines) | `buybot5m.py`, `buybothourly.py` |
| `buybot5m.py` | 5m buy bot (~1705 lines) | `buybot.py`, `buybothourly.py` |
| `buybothourly.py` | Hourly buy bot (~1707 lines) | `buybot.py`, `buybot5m.py` |
| `buy/market.py` | MarketGateway — Gamma discovery + market metadata | — |
| `buy/btc_price.py` | Resolution-aligned BTC feeds (TWAP 30/60, Binance) + PTB capture | — |
| `buy/clob_book_ws.py` | CLOB market-channel WS top-of-book (buy/hedge speed path) | — |

**Pattern:** The three bots are near-identical copies differing only in constants
(SLUG_PREFIX, SLUG_EXCLUDES, oracle source, buy budget/window, tick_size). A logic
change to one usually needs propagation to its siblings.

### Safe to modify

| File | Purpose |
|---|---|
| `pathlog.py` | CLOB path recorder (no orders; TOB price **and** size) |
| `check_path_backtest.py` | Pathlog: entry grid, anatomy, compare, **paper hedge**, template `--sweep` (no orders) |
| `CLOUD_RESEARCH.md` | Cloud prompts: live paper P&L via public books + `--sweep` (no `.env`) |
| `check_book.py` | Diagnostic — inspect a live order book |
| `check_edge_counterfactual.py` | Diagnostic — resolution win rate if edge skips had filled |
| `check_participation.py` | Diagnostic — post-facto bought vs missed + band exposure |
| `check_fetch_trades.py` | Diagnostic — full-wallet Data API trade history → CSV |
| `check_buy_skips.py` | Diagnostic — why 5m did not buy (JSON log skip/attempt/fill counts) |
| `widget/polydesk.py` | Local always-on-top Polymarket value / HOLDING glance (no orders) |
| `CURRENT.md` | Living ops/probe status — update when decisions change |
| `strategy_buy*.example.json` | Buy-bot config templates — not loaded by bots |
| `mintbot.py` / `strategy_mint.example.json` | Paused mint helper — do not run live |

### Read-only / auto-generated — never edit

- `positions_buy*.json`, `pnl_buy*.json` — bot state and P&L (auto-generated)
- `*.log` — structured JSON-line logs with rotation
- `.heartbeat*` — uptime tick counters
- `underlying_research_buy*.jsonl`, `ptb_*_buy*.json` — oracle/PTB decision audit
- `pathlog/ticks/*.jsonl` — recorded CLOB paths (**auto-pruned**: 14 days / 400 MB;
  export before prune deletes them — see below)
- `strategy_buy*.json` / `strategy_mint.json` (without `.example`) — live configs
- `positions_mint.json` — mint intents / daily spend
- `.env` — secrets (gitignored)

## Never Do

- **Never delete or truncate state files** (`positions_buy*.json`, `pnl_buy*.json`,
  `positions_mint.json`, logs, heartbeats, research/PTB files). Pathlog ticks are
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
python check_path_backtest.py --ask-min 0.75 --ask-max 0.90 --ttm-max 120 --budget 15 --series 5m --csv /tmp/hits_15.csv
python check_book.py

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

### What to look for

- `[DRY BUY]` / `[DRY SELL]` in dry-run — confirms trigger logic fires
- `[FAK EMPTY]` / `buy_attempt_rejected` with `unmatched_retry: true` — empty FAK re-quoted in the same trigger (up to 3 POSTs)
- `buy_ghost_fill` `via=unmatched_400_guard` — unmatched 400 but inventory appeared; no second FAK
- `buy_attempt_ambiguous` `via=unmatched_400_no_balance` — unmatched 400 and CLOB balance unreadable; quarantine, no retry
- `cycle_error` in logs — unhandled exception. The bot process stays up.
  A fault **inside** `for m in markets` logs `condition_id` and **continues
  to the next market** (held-first order unchanged). A fault **outside** that
  loop (refresh, GC, redeem, UI) still aborts the rest of that poll. Banner
  does **not** sleep 5s. Structured `error` field is the exception
  type+message; `check_buy_skips.py` prints the breakdown.
  Aug 13–19 live 5m: **3294/3294** were `NameError: known_cost` until the
  19 Aug 09:42 restart picked up #80 (that NameError aborted every later
  market in the same poll; isolation is the fix for the next one).
- `hedge_attempt` / `hedge_fill` — hedge fired after force-fresh REST + book integrity + GUI consensus (normal path)
- `hedge_skip_toxic_book` — bid dipped but ask/spread still say "not reversed"
- `hedge_skip_no_consensus` — 35/40 book passed but GUI/last-trade still say the held side has not actually fallen (same class of check as buy)
- `hedge_skip_toxic_recovered` — `toxic_fill` is armed but held bid > 35¢ (winner book); dump stays armed, no sell
- `hedge_skip_incomplete_rest` — REST missing a side; fail closed (no WS sell)
- `buy_skip_ambiguous` — GUI display prices too close (throttled 8s; **not** one event per market)
- `buy_skip_no_consensus` — ask in band but GUI/tight-book gate failed
- `buy_fill_below_band` — fill avg below band; inventory persisted + `toxic_fill` force-exit
- `buy_fill_walk` — confirmed BUY shares exceeded the quoted budget/ask size (junk walk)
- `buy_ghost_fill` — balance reconciliation after null/delayed BUY confirm
- `buy_uncertain` — POST outcome unresolved; durable token/baseline quarantine blocks re-buy
- `buy_skip_incomplete_book` — missing GUI price on a leg (no mid and no last trade)
- `buy_skip_underlying_edge` — underlying gate failed (missing/stale/flat vs PTB; 5m is **$0** = any non-zero tick)
- `buy_skip_underlying_side` — book wants the opposite leg from the underlying move
- `buy_window` — market first entered a 5m buy window (late 120s or early >90¢; one line per market)
- `buy_skip` `ask_below_band` / `ask_above_band` / `no_ask` — in window, winning ask not 75–90¢ (throttled 8s)
- `buy_skip_max_positions` — only if `max_open_positions > 0` (probe uses **0 = unlimited**)
- `pathlog_prune` — oldest tick JSONL removed (14d / 400 MB cap); export first

## Research loop (before changing live knobs)

Live JSON is one point in the space. Pathlog records **all three** series even
when 15m/hourly are not posting. Score alternatives on those ticks **before**
editing `strategy_buy5m.json`:

```bash
python check_path_backtest.py --compare --series 5m --budget 2.5
python check_path_backtest.py --compare --paper --series 5m --budget 2.5
python check_path_backtest.py --sweep --series 5m
python check_path_backtest.py --anatomy --series 5m --ttm-max 120 --csv /tmp/anatomy.csv
python check_path_backtest.py --grid --budget 2.5 --series 5m
python check_buy_skips.py --since 2026-08-19T08:02:00
```

`--sweep` scores the live 5m **example** template (75–90 / 120s / $2.50) plus
one-at-a-time window/band/size variants, with a **paper** hedge on recorded
books (not live). `--anatomy` answers “already decided at T-120 vs 50/50 until
the end.” `--compare` is the named-preset table; add `--paper` to walk later
ticks for 35/40/15 + GUI-proxy. `--grid` is ask × time. **`--series 5m` matches
only 5m** (not 15m). Pathlog cannot replay last-trade, BTC/PTB, or POST latency.
Cloud agents: `CLOUD_RESEARCH.md`.

## Key Conventions

- **All bots are single-file polling loops** — no `asyncio` or database. Small
  thread pools isolate book, refresh, notification, and redemption-status I/O.
  Section comment banners (`# --- PRICING ---`) act as visual boundaries.
- **Hot-reload:** Strategy JSON files are re-read every cycle via `load_strategy()`.
  Changes take effect on the next tick — no restart needed.
- **Atomic durable save:** State and P&L files flush + `fsync` the `.tmp`, replace
  it, then `fsync` the parent directory before an order can proceed.
- **Fail-closed startup:** A valid strategy file is required at startup. A
  missing/malformed hot reload disables new entries while retaining the
  last-known-good hedge parameters. A per-bot process lock prevents duplicate
  live instances.
- **FAK orders only:** All orders are Fill-And-Kill — no resting orders, no market
  making. Buys are **limit** FAKs at the quoted ask sized `budget/ask`
  (hard `buy_max_spend` $3, `buy_max_shares` 5; not displayed top size); sells stay
  share-denominated market FAKs. A 400 **"no orders found to match"** re-quotes and
  POSTs again (up to 3) in the same trigger; invalid-amount / auth 400s and unclear
  POSTs do not. After a fully empty trigger, wait `empty_fak_cooldown_s` (0.15 s).
- **Hedge is sell-only exit:** The bots never profit-take. The only sell path is the
  hedge: REST shows bid ≤ 35¢ **and** ask ≤ 40¢ with tight spread, **and**
  Polymarket GUI + last trade agree the held side actually lost (held last
  print ≤ 40¢, held GUI ≤ 30¢, other GUI ≥ 70¢ — same display rule as buy).
  A random TOB clip is not enough; a last print of 85¢ on a 32/38 book will
  `hedge_skip_no_consensus`. `toxic_fill` stays armed and still skips GUI /
  35/40/15, but **sells only while held bid ≤ 35¢**. A recovered 97¢ book
  logs `hedge_skip_toxic_recovered` and rides; a 6¢ junk bid (even under a
  99¢ ask) still dumps on bid-only REST. Fresh WS bid > 35¢ skips REST for
  both normal and toxic. After a dump is allowed, the FAK sells at the
  **live bid** even if it is 20¢. Everything else rides to redemption at
  $1.00. WS may *arm* a hedge check; normal sells need two-sided REST.
- **Tick sizes:** 5m markets use `0.001`, 15m and hourly use `0.01`.
- **One entry per market:** Enforced via `meta.get("bought_token")` in state cache.
- **Notifications:** Fire-and-forget via ntfy.sh (topic `polybot-joel-btc`).
  Never let notification failures crash the bot.

## Landmines

1. **The three bots are near-identical copies, not shared modules.** A bug fix in
   `buybot.py` probably also applies to `buybot5m.py` and `buybothourly.py`.
2. **The 5m bot uses seconds-based window checks** (`buy_start_s = 120` late
   75–90¢; `early_buy_start_s = 300` for ask > 90¢) while the
   15m and hourly bots use minutes (`buy_window_min = 4.0 / 13.0`). Don't mix them
   when propagating changes. The 5m loop must define `seconds_left` (not only
   `minutes_left`) or it NameErrors every cycle.
3. **Settlement is confirmation-gated.** A relayer submission is not P&L.
   Redemption is credited only after relayer confirmation and a complete Data API
   snapshot shows the inventory gone; GC never invents par value.
4. **No `if __name__ == "__main__"` guard** — buy-bot scripts execute at module
   level. They cannot be imported. `pathlog.py`, `check_path_backtest.py`, and
   `check_fetch_trades.py` can.

## Systemd Services (in `deploy/`)

| Service file | Bot |
|---|---|
| `polybuybot.service` | `buybot.py` (15m) |
| `polybuybot5m.service` | `buybot5m.py` (5m) |
| `polybuybothourly.service` | `buybothourly.py` (hourly) |
| `polypathlog.service` | `pathlog.py` (CLOB path recorder; no orders) |
| `polymintbot.service` | `mintbot.py` (**paused**) |

CI: pushes to `main` touching the buy bots, `pathlog.py`, `buy/`, or
`requirements.txt` deploy to the VM via SSH (`git pull` + `pip install` only).
Services are **not** auto-restarted — start/restart deliberately after
validation (`systemctl start` / `restart`). A merged NameError fix does
nothing until `polybuybot5m` is restarted (13–19 Aug `known_cost` stall).

## Dependencies

```
py_clob_client_v2   # Polymarket CLOB SDK
py-builder-relayer-client  # Relayer proxy transaction builder/signing
py-builder-signing-sdk     # POLY_BUILDER_* HMAC authentication headers
requests            # HTTP for Data API, Gamma API, ntfy, relayer
python-dotenv       # .env loading
rich                # Terminal UI (tables, panels)
eth-abi, eth-utils, eth-account  # Redeem calldata encoding
websocket-client    # CLOB market-channel WS (buy/clob_book_ws.py)
web3                # transitive of the CLOB client stack
```
