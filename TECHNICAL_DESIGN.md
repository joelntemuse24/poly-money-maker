# Poly Money Maker — Technical Design Document

> **Audience:** A beginner-to-intermediate Python programmer who can read a
> script, a JSON file, and a systemd unit, and who needs to **operate or
> change a live trading bot without losing money**. Dense on purpose: the
> constraints are the design.
>
> **Three docs, three jobs:**
> - `CURRENT.md` — what is live on the VM *today* (probe knobs, stop/start).
> - `AGENTS.md` — agent quick-ref (never-do, file map, how to verify).
> - This file — how the system is built and why.

Live trading is **5-minute CLOB only** (`polybuybot5m`). 15m and hourly bots
exist in the repo and on disk but are **stopped**. Pathlog is running (no
orders). Mint is paused. Do not start the stopped units unless the operator
asks.

---

## Table of Contents

1. [What the System Does](#1-what-the-system-does)
2. [Live vs Repo at a Glance](#2-live-vs-repo-at-a-glance)
3. [Architecture](#3-architecture)
4. [Market Discovery](#4-market-discovery)
5. [Underlying Oracle Feeds & PTB](#5-underlying-oracle-feeds--ptb)
6. [The Buy Decision](#6-the-buy-decision)
7. [The Hedge (Sell-Only Exit)](#7-the-hedge-sell-only-exit)
8. [Redemption](#8-redemption)
9. [State, P&L, and Garbage Collection](#9-state-pl-and-garbage-collection)
10. [Configuration & Hot Reload](#10-configuration--hot-reload)
11. [Operations on the VM](#11-operations-on-the-vm)
12. [Error Handling Philosophy](#12-error-handling-philosophy)
13. [Latency, Disk, and Optional AI](#13-latency-disk-and-optional-ai)
14. [Landmines](#14-landmines)
15. [Testing & Research Tools](#15-testing--research-tools)
16. [Removed / Historical](#16-removed--historical)
17. [Glossary](#17-glossary)

---

## 1. What the System Does

Polymarket lists Bitcoin **"Up or Down"** binary markets. Each market asks:
will BTC be up or down versus a **Price To Beat** at the end of a window
(5 minutes, 15 minutes, or 1 hour)? A market has two **legs** (UP and DOWN
tokens). Exactly one leg **wins** and can be redeemed on-chain for **$1.00**;
the other is worth **$0**.

The bot does **not** make markets and does **not** leave resting orders. It
waits until the book already looks decided, **buys the winning leg** with a
Fill-And-Kill (FAK) limit at the quoted ask, and holds to redemption. If the
held book **actually collapses** (not a one-tick spoof), it **hedges** —
market-sells the position to bound the loss. There is **no profit-take sell**.
The only sells are hedge and `toxic_fill` dump. Everything else rides to $1.00
or $0.

**Why that shape:** a 5m BTC market that is already 80¢ / 20¢ with two minutes
left is usually “already decided.” Buying that favorite at 75–90¢ and redeeming
at $1.00 is ~10–25¢ per share on a $2.50 clip (~3.3 shares at 75¢). Early
**≥90¢ / ≥95¢** fills exist to catch markets that never dip into 75–90¢; their
edge to $1.00 is thin (a 95¢ fill makes ~5¢/share if it wins). Reversals still
happen; the hedge is the safety exit, not a second trading style.

**Economics (no reversal):** buy, redeem $1.00 → profit is `(1 − entry) × shares`.
**Economics (hedged reversal):** sell at the live collapsed bid → bounded loss
instead of riding the wrong side to $0.

Take-profit (“sell once we are up $0.30”) is **not** in the product. It was
evaluated and dropped: $0.30 is unreachable on ≥90¢ entries even at $1.00, and
cutting a still-winning 80¢→$1 ride is a different strategy.

---

## 2. Live vs Repo at a Glance

**Live on the GCP VM** (`instance-20260516-185922`, `~/poly-money-maker`,
user `ntemusejoel`): **`polybuybot5m` + `polypathlog`**. Small ~10GB e2 boot
disk. Python 3 venv at `.venv`. Secrets in gitignored `.env`.

| | 15m | **5m (live)** | Hourly |
|---|---|---|---|
| Script | `buybot.py` | `buybot5m.py` | `buybothourly.py` |
| systemd | `polybuybot` **stopped** | `polybuybot5m` **running** | `polybuybothourly` **stopped** |
| Series slug | `btc-up-or-down-15m` | `btc-up-or-down-5m` | `btc-up-or-down-hourly` |
| CLOB slug prefix / excludes | `btc-updown` excl. `btc-updown-5m`, `bitcoin-up-or-down` | `btc-updown-5m` excl. `bitcoin-up-or-down` | `bitcoin-up-or-down` excl. both `btc-updown*` |
| Resolution oracle | Chainlink BTC TWAP 60s | Chainlink BTC TWAP 30s | Binance BTCUSDT |
| Buy windows / ask | final 4.0 min, **75–90¢** | last **120s 75–90¢**; first **3 min ≥90–99¢**; first **4 min ≥95–99¢** | final 13.0 min, **75–90¢** |
| Budget / market | $2.50 | $2.50 (hard cap $3, share rail 5) | $2.50 |
| Hedge trigger | 35/40/15 + inverted GUI | **50/55/15** + inverted GUI | 35/40/15 + inverted GUI |
| Tick size | 0.01 | **0.001** | 0.01 |
| BTC gate vs PTB | $10 | **$0** (any non-zero tick; flat still refused) | $10 |
| Strategy / state / P&L / log | `strategy_buy.json`, `positions_buy.json`, `pnl_buy.json`, `buybot.log` | `strategy_buy5m.json`, `positions_buy5m.json`, `pnl_buy5m.json`, `buybot5m.log` | `…hourly…` |
| Heartbeat / research / PTB | `.heartbeat_buy`, `underlying_research_buy.jsonl`, `ptb_twap60_buy.json` | `.heartbeat_buy5m`, `…buy5m.jsonl`, `ptb_twap30_buy5m.json` | hourly twins |

Also on the VM: `polypathlog` writes `pathlog/ticks/*.jsonl` (no keys, no
orders). `polymintbot` is **paused/disabled**.

**In the repo but not live trading:** the 15m/hourly copies (kept in lockstep
for when they come back), `mintbot.py` (complete-set mint helper), `check_*.py`
diagnostics, `widget/polydesk.py` (laptop glance, public Data API only),
`tests/`, `CLOUD_RESEARCH.md` (cloud agents, no `.env`).

All strategy/state/log files live in the VM working directory. State, logs,
heartbeats, research JSONL, PTB stores, live `strategy_*.json` (no `.example`),
and `.env` are **gitignored**. Never commit or delete them. Pathlog ticks are
the one auto-pruned exception (14 days / 400 MB) — **export before prune**.

---

## 3. Architecture

```
 GCP VM (~10GB e2)
 ┌─────────────────────────────────────────────────────────────────┐
 │  systemd                                                        │
 │   polybuybot5m  ──► buybot5m.py     (LIVE orders)               │
 │   polypathlog   ──► pathlog.py      (GET books only)            │
 │   polybuybot    ──► buybot.py       STOPPED                     │
 │   polybuybothourly ──► buybothourly.py STOPPED                  │
 │   polymintbot   ──► mintbot.py      PAUSED                      │
 └────────────┬───────────────────────────────┬────────────────────┘
              │                               │
    ┌─────────v─────────┐           ┌─────────v──────────┐
    │ buy/market.py     │           │ buy/btc_price.py   │
    │ Gamma discovery   │           │ RTDS oracles + PTB │
    └─────────┬─────────┘           └────────────────────┘
              │
    ┌─────────v──────────┐   ┌───────────────────────────┐
    │ buy/clob_book_ws.py│   │ buy/book.py  (TOB parse)  │
    │ CLOB market WS TOB │   │ buy/entry_skip.py (5m     │
    └─────────┬──────────┘   │   band union, skip labels)│
              │              └───────────────────────────┘
              v
   Polymarket: Gamma | CLOB REST+WS | Data API | Relayer | RTDS
```

Laptop-only (not the VM): `widget/polydesk.py` polls public `/value` +
`/positions`. Cloud agents: same repo, **no `.env`**, paper-score pathlog
(`CLOUD_RESEARCH.md`).

### 3.1 Process model (read this before the 5,000-line bots)

Each buy bot is **one Python process**, **one infinite `while True` poll
loop**, started at **module import** (there is **no** `if __name__ ==
"__main__"`). You cannot `import buybot5m` — it would start trading. Tests
either import the small `buy/` modules and `check_*.py`, or **extract
function source with `ast`** from the bot file (`tests/test_buy_fill_shapes.py`).

There is **no asyncio, no database, no message queue**. The loop:

1. `load_strategy()` — re-read JSON if mtime changed (hot reload).
2. Write heartbeat.
3. Refresh Gamma markets + Data API positions (thread pool).
4. Point the CLOB book websocket at current token IDs.
5. Redeem + garbage-collect settled inventory.
6. **`for m in markets`** (held positions first): hedge if held; else buy
   gates if in window. A fault **inside** this loop logs `condition_id` and
   **continues**. A fault **outside** (refresh/GC/redeem/UI) still aborts the
   rest of **that** poll.
7. Rich UI every N cycles. Sleep `poll_held_s` (0.05s) while any position is
   held, `poll_buy_window_s` (0.1s) while a market is in the buy horizon with
   no inventory, else ~1s idle.

**Hot mode** is on when any position is open **or** any market is inside
`BUY_HORIZON_S`. On 5m that horizon is
`max(buy_start_s, early_buy_start_s, early_95_start_s)` (300s), so the bot
actually looks during the first 3–4 minutes, not only the last 120s.

**Threads** exist only as small pools: book REST, notifications (ntfy.sh),
redeem status. The CLOB book WS and BTC RTDS each run a **daemon thread**
with an in-memory cache. The main loop is still synchronous.

**Process lock:** `fcntl.flock` on `/tmp/poly-money-maker-buybot5m.lock` (and
siblings). Two live instances of the same bot would double-spend. Pathlog and
mint use repo-local lock files.

**FAK only.** Fill-And-Kill: take what is there at your limit, cancel the
rest. No maker quotes, no “leave 2 shares on the book.” Buys are **share-sized
limit FAKs at the quoted ask** (`budget/ask`, clipped by `buy_max_shares`).
Sells are share-denominated FAKs at the **live bid**. A CLOB **400 "no orders
found to match"** is proven empty: re-quote and POST again (up to 3) in the
same trigger, then `empty_fak_cooldown_s` (0.15s). Invalid-amount / auth 400s
and unclear POSTs do **not** retry a second budget.

### 3.2 Why three copies, not a library

`buybot.py` (~5063 lines), `buybot5m.py` (~5177), `buybothourly.py` (~5061)
are near-identical copies. They differ in slug prefix/excludes, oracle
constant, window units (**seconds on 5m, minutes on 15m/hourly**), tick size,
defaults (hedge 50/55 vs 35/40, BTC gate $0 vs $10), and — on 5m only — the
early-band union in `buy/entry_skip.py`.

A buy/hedge/quarantine bug in one copy **probably** exists in the other two.
Diff the siblings after a logic change. Do **not** “fix it properly” by
importing a bot module; there is no `main` guard. Shared code that is safe to
import lives in `buy/`.

### 3.3 Shared `buy/` package (importable)

| Module | Who uses it | Job |
|---|---|---|
| `buy/market.py` | all bots, pathlog, mint | `MarketGateway` + `MintMarket`: Gamma discovery, token IDs, end time, tick |
| `buy/btc_price.py` | buy bots only | RTDS websocket, PTB capture, `append_research` |
| `buy/clob_book_ws.py` | buy bots only | CLOB **market-channel** WS top-of-book cache (speed path) |
| `buy/book.py` | WS cache, pathlog, tests | Parse CLOB levels → best bid/ask **and displayed size** |
| `buy/entry_skip.py` | **5m bot + tests** | Band union, `ask_in_any_band`, skip labels. 15m/hourly do **not** import this |
| `buy/chain.py` / `buy/contracts.py` | **mintbot only** | Polygon `eth_call` + `splitPosition` calldata. Not on the CLOB buy path |

`buy/__init__.py` is just a version string.

### 3.4 Polymarket surfaces (the idiosyncrasies that matter)

The bots talk to **five different Polymarket systems**. Mixing them up is how
you sell on a spoofed 1¢ bid.

| Surface | URL-ish | What we use it for |
|---|---|---|
| **Gamma** | `https://gamma-api.polymarket.com` | Market catalog: slug, condition ID, token IDs, end time, later `winner` |
| **CLOB REST** | `https://clob.polymarket.com` | `/book` (force-fresh quote before buy/hedge), `/order` POST (FAK), balances |
| **CLOB WS** | `wss://ws-subscriptions-clob.polymarket.com/ws/market` | Fast top-of-book **arm**. Never sufficient to POST a buy or a normal hedge |
| **Data API** | positions / value / trades | Holdings across restart, GUI-ish last trade, redeemable flag, participation CSV |
| **RTDS** | `wss://ws-live-data.polymarket.com` | The **same** BTC series the market resolves on (TWAP 30 / TWAP 60 / Binance) |
| **Relayer** | Builder relayer | `redeemPositions` on Polygon via proxy; we do not broadcast our own txs |

**CLOB v2 amounts:** a BUY’s `makingAmount` is USDC paid, `takingAmount` is
shares received. Entry cost is USDC spent, **not** `shares × gate ask`.
Protocol sizing is picky: USDC **2 decimal places**, taker shares **4 dp**
(SDK also `round_down`s some sizes to 2 dp). Wrong rounding → HTTP 400
`invalid amounts` (`check_buy_rejects.py` counts those). Binary token prices
are in **(0, 1)** exclusive; 5m ticks **0.001**, 15m/hourly **0.01**.

**Ask ≠ tradable price.** A 97¢ ask over a 1¢ bid is a wide book, not a
97¢ favorite. Last-trade GUI can still look like a winner. Entry requires a
**tight REST book** (bid ≥ 70¢, spread ≤ 5¢). Hedge requires the **whole
book** to look lost, plus inverted GUI.

**Unmatched FAK:** posting at an ask that just vanished returns 400 “no orders
found to match.” That is empty, not “error, try a second $2.50.” Re-quote in
the same trigger; if inventory appeared anyway, that is a **ghost fill**.

**Proxy wallet:** `FUNDER_ADDRESS` is the Polymarket proxy; `PRIVATE_KEY` signs
CLOB orders and the relayer’s EIP-712 request. Builder/Relayer API keys
authenticate HTTP. The key does not pay gas itself.

### 3.5 JSON files are the database

`atomic_save()` writes `.tmp`, `flush` + `fsync`, keeps a validated `.bak`,
`os.replace`s onto the live file, then `fsync`s the directory. Non-finite
JSON is rejected. **Before every live POST** the bot writes a crash-durable
`buy_uncertain` / hedge-uncertain quarantine (signed order ID, token, amounts,
pre-submit balance). Ambiguous POSTs stay quarantined; GC never deletes
unresolved quarantine; recovery continues after expiry.

`dry_run: true` points state/P&L/heartbeat/research/PTB at `*.dryrun.*` paths
so a dry process cannot clobber live files. `dry_run` is **startup-only**
(it selects those paths). Other knobs hot-reload.

---

## 4. Market Discovery

Each cycle, `MarketGateway` (`buy/market.py`) lists active markets for that
bot’s series via Gamma, filtered by `SLUG_PREFIX` / `SLUG_EXCLUDES`.

`btc-updown` is a **prefix of** `btc-updown-5m`. If the 15m bot omitted the
5m exclude, both would buy the same market with separate $2.50 budgets.
**Never change prefix/excludes on one bot without checking the other two.**

Discovery yields: **condition ID** (on-chain market key, state dict key,
redeem argument), **UP/DOWN CLOB token IDs**, tick size, end time. Slugs look
like `btc-updown-5m-<unix_start_ts>`; start time is parsed from that suffix
when present so PTB can be captured at the open.

The bot also polls Data API **positions** so a restarted process can hedge and
redeem without re-buying. One entry per market is `meta["bought_token"]` in
that state cache (and the `buy_uncertain` quarantine).

Pathlog uses the same gateway for **all three** series, even when 15m/hourly
are not posting.

---

## 5. Underlying Oracle Feeds & PTB

Each market **resolves** against a specific oracle. Each bot streams **that
same feed** so it does not buy a CLOB “winner” the resolution oracle disagrees
with.

| Bot | Oracle | `buy/btc_price.py` constant | RTDS topic |
|---|---|---|---|
| 15m | Chainlink BTC/USD TWAP 60s | `SOURCE_TWAP_60` | `crypto_prices_twap_sixty` |
| 5m | Chainlink BTC/USD TWAP 30s | `SOURCE_TWAP_30` | `crypto_prices_twap_thirty` |
| Hourly | Binance BTCUSDT spot | `SOURCE_BINANCE` | `crypto_prices` (filter `btcusdt`) |

A background websocket to `wss://ws-live-data.polymarket.com` keeps ~3 hours
of ~1 Hz ticks (`RING_MAX_SAMPLES = 12_000`). `get_btc_feed(source)` is the
read API.

**Price To Beat (PTB):** resolution is close-oracle vs open-oracle. The bot
caches the nearest tick to market `start_ts` (≤2s skew) in `ptb_*_buy*.json`.
If the process missed the open, PTB is missing and **buys are refused**
(fail closed). Live 5m gate is **$0**: any non-zero TWAP tick vs PTB, side
must match, **flat still refused**. 15m/hourly default **$10**.

**Research audit:** every PTB capture and every buy decision (taken or
skipped) is appended to `underlying_research_buy*.jsonl` via
`append_research()`. Those files rotate at 50 MiB × 2 backups so a skip storm
cannot fill the disk. Gitignored.

Pathlog does **not** record BTC/PTB. Counterfactuals for the edge gate are
`check_edge_counterfactual.py`, not `--sweep`.

---

## 6. The Buy Decision

A buy fires only when **all** gates pass. 15m/hourly use one window (minutes)
and one band (75–90¢). 5m uses a **union of time×price bands** (seconds).

### 6.1 Gates that are the same on all three bots

1. **Window + one-shot.** Inside the buy window, past `buy_grace_s`, not
   already entered (`bought_token`), `buy_cooldown_s` elapsed,
   `entry_enabled`, not `dry_run`-blocked, USDC/risk caps OK.
2. **GUI consensus.** Polymarket display price (mid when spread ≤ 10¢, else
   last trade): winner ≥ `min_winner_bid` (70¢), loser ≤ `max_loser_bid`
   (30¢), gap ≥ `min_bid_edge` (5¢).
3. **Tight REST book (critical).** Winning leg **REST** top-of-book: bid ≥
   70¢ and `ask − bid` ≤ `max_entry_spread` (5¢). WS alone never arms entry.
   A high ask over a 1¢ bid is a fake price.
4. **Ask in an open band.** See §6.2. 15m/hourly: `buy_threshold`–
   `buy_max_price` (75–90¢) in the last 4.0 / 13.0 minutes.
5. **Underlying gate.** If enabled: live oracle far enough from PTB **and**
   book side matches the move. Missing/stale/flat → skip.
6. **Risk caps.** `buy_budget` $2.50, `buy_max_spend` $3, `buy_max_shares` 5,
   `max_open_positions` (probe **0 = unlimited**), notional caps, wallet USDC.

### 6.2 5m band union (`buy/entry_skip.py`)

`buybot5m.current_entry_bands(seconds_left)` wraps
`applicable_entry_bands` in `buy/entry_skip.py` and returns **every** band
open at that TTM (a market can match more than one). `ask_in_any_band` is
the gate. `select_entry_band` pins the FAK min/max to the **widest matching
band** (lowest retry floor) so a 95¢ print in the last 120s is not rejected
for being above the late 90¢ cap.

| Band | When (TTM = seconds left) | Winning ask |
|---|---|---|
| Late | `0 < TTM ≤ 120` | **75–90¢** inclusive |
| Early ≥90 | `120 < TTM ≤ 300` (first 3 min of the 5m) | **90–99¢** inclusive |
| Early ≥95 | `60 ≤ TTM ≤ 300` (first 4 min) | **95–99¢** inclusive |

**Overlap:** last 120s while TTM ≥ 60s → 75–90¢ **or** ≥95¢.
**Hole:** **91–94¢ in the last 120s** is not in any open band
(`ask_out_of_band`). For `TTM < 60` the ≥95 path is off (late 75–90¢ only).
At **TTM = 60s** exactly, ≥95 is still open.

Hot poll / WS subscribe / redeem-delay horizon is `BUY_HORIZON_S` (300s), not
`buy_start_s` alone. Otherwise the first three minutes would never be looked
at.

Knobs (5m defaults): `buy_start_s` 120, `early_buy_start_s` 300,
`early_buy_max_price` 0.99, `early_95_start_s` 300, `early_95_min_s` 60,
`early_95_min_price` 0.95. Live JSON may omit the early keys; code defaults
apply on hot reload. **Hedge keys must be in live JSON** (see §7 / §10).

### 6.3 Execution (all bots)

FAK **limit** buy sized in **shares** at the quoted ask:
`min(budget/ask, buy_max_shares)` via `OrderArgs` + `create_order`. **Not** a
USDC market order (those walk cheaper levels: gate 80¢, leftover cash lifts
9¢). **Not** capped to displayed top size. Thin tops log `[THIN ASK]` and
still post the dollar size; unmatched remainder dies on the FAK.

A 400 **"no orders found to match"** → re-quote (abort if the fresh ask left
the band) and POST again, up to 3, then 0.15s cooldown. Invalid-amount / auth
400s stop. Unclear POSTs quarantine — never a second full budget. A BUY limit
is a **maximum**; the exchange can price-improve.

Confirmed fills are **always persisted**. Average is **USDC / shares**. Below
band → `buy_fill_below_band` + `toxic_fill` if avg `< toxic_force_exit_below`
(65¢) **or** shares > 1.05× quoted size (`buy_fill_walk`). Delayed FAKs are
polled; zero confirms fall back to balance reconciliation (`buy_ghost_fill`).
Unmatched 400 + inventory appeared → `buy_ghost_fill` `via=unmatched_400_guard`
(no second FAK). Unmatched 400 + unreadable balance →
`buy_attempt_ambiguous` `via=unmatched_400_no_balance` (quarantine, no retry).

### 6.4 Skip / fill reasons (logs + research JSONL)

Throttled skip lines are **not** one event per market (often 8s).

| Event | Meaning |
|---|---|
| `buy_window` | First time this market entered a 5m buy window (`window=` late / early / early_95) |
| `buy_skip` `ask_below_band` / `ask_above_band` / `ask_out_of_band` / `no_ask` | In window, ask not in any **open** band |
| `buy_skip_ambiguous` | GUI prices too close |
| `buy_skip_no_consensus` | Ask in band; GUI or tight-book failed (`wide_spread`, `bid_too_low`, …) |
| `buy_skip_incomplete_book` | Missing GUI on a leg |
| `buy_skip_underlying_edge` | Missing/stale/flat vs PTB (5m $0 = any non-zero tick) |
| `buy_skip_underlying_side` | Book wants the opposite leg from the oracle move |
| `buy_skip_max_positions` | Only if `max_open_positions > 0` |
| `buy_fill_below_band` / `buy_fill_walk` / `buy_ghost_fill` / `buy_uncertain` | Fill / quarantine outcomes |

`check_buy_skips.py` tallies the live 5m JSON log. Counts ≠ unique markets.

---

## 7. The Hedge (Sell-Only Exit)

The bots **never take profit**. The only sell is the defensive hedge (or a
`toxic_fill` dump). Thresholds are **per bot**:

| | Bid ≤ | Ask ≤ | Spread ≤ | GUI |
|---|---|---|---|---|
| **5m live** | **50¢** | **55¢** | 15¢ | inverted buy GUI; held last trade ≤ 55¢ |
| 15m / hourly (stopped) | 35¢ | 40¢ | 15¢ | same idea; last trade ≤ 40¢ |

Write **`hedge_threshold` / `hedge_require_ask_max`**, not “35¢” as if it were
universal. Live 5m JSON **must set** `0.50` / `0.55`. Hot reload uses the file;
if live JSON still has `0.35` / `0.40`, the old hedge stays even after a
code pull. Code defaults apply only when the keys are **omitted**.

Pipeline (same on all bots; numbers from config):

- **Arm (normal):** WS/cache bid ≤ `hedge_threshold` while held. Peek only —
  never sufficient to sell.
- **Arm (`toxic_fill`):** entry average `< toxic_force_exit_below` (65¢) sets
  a flag on `meta`. Sell **only while held bid ≤ hedge_threshold**. A recovered
  97¢ book logs `hedge_skip_toxic_recovered` and stays armed. Do not wait for
  50/55/15 or GUI. Wide 1¢/99¢ still dumps **if bid is dead**. Milder
  below-band fills (e.g. 70¢) use the normal hedge.
- **Confirm:** `get_quote_fast(..., force_rest=True)`. Normal hedges skip if
  either side is missing (`hedge_skip_incomplete_rest`) — **no WS fallback**.
  Toxic dumps may sell on bid-only REST; no bid still skips. Fresh WS bid
  **above** threshold skips REST for **both** paths (toxic logs recovered).
  Normal path: bid bounced above threshold → `hedge_cancel_bounce`.
- **Book integrity (normal):** lone penny bid under a still-high ask is not a
  reversal (`hedge_skip_toxic_book`). Need bid ≤ threshold, ask ≤
  `hedge_require_ask_max`, spread ≤ `hedge_max_spread`.
- **GUI consensus (normal, `hedge_require_gui` default true):** invert the buy
  display rule. Held last print ≤ ask-max, held GUI ≤ 30¢, other GUI ≥ 70¢.
  A 48/52 (5m) or 32/38 (15m) book with last trade 85¢ →
  `hedge_skip_no_consensus`. Toxic skips GUI.
- **Execution:** FAK at the **live bid** (minus `hedge_undercut_ticks`). No
  “won't sell below 32¢.” 20¢ or 1¢ on a still-tight collapsed book is a fill.
  Retries force-REST; normal retries re-run two-sided integrity so a spoof
  1¢/99¢ still aborts. Toxic retries use `abort_above=None` but still honor
  the recovered-bid gate.
- **Outcome:** crash-durable order ID. Only settlement-confirmed fills shrink
  `bought_size` / add proceeds. Ambiguous → `hedge_uncertain` until exact-order
  reconciliation. Full exit sets `hedge_closed`. Data API balances never
  invent a sell fill.

`sell_market_with_retry` is **hedge-only**. Do not reuse it as a profit-take
without a new abort rule (you would need “abort if bid **dropped**,” the
opposite of hedge bounce).

---

## 8. Redemption

Winning shares are not sold. They redeem on-chain at $1.00 via Polymarket's
relayer:

1. Data API marks a leg `redeemable` after resolution.
2. Bot builds `redeemPositions(address,bytes32,bytes32,uint256[])` for the CTF
   (`0x4D97…6045`), uses `py-builder-relayer-client` to build/sign the PROXY
   request, checks the derived proxy equals `FUNDER_ADDRESS`, authenticates
   `POST /submit` with Relayer API keys (`RELAYER_API_KEY` +
   `RELAYER_API_KEY_ADDRESS`) **or** Builder HMAC (`POLY_BUILDER_*` via
   `py-builder-signing-sdk`).
3. Throttle per condition (`redeem_throttle_s` 30s); abandon after
   `max_redeem_age_days` (7). Permanent failures go on an in-memory blocklist
   so we do not burn gas on a reverting call.
4. Submit only sets `redeem_pending`. Poll `GET /transaction`. Par is credited
   only after `STATE_CONFIRMED` **and** a complete fresh Data API snapshot
   shows inventory gone. **A relayer submit is not P&L.** GC never invents par.

---

## 9. State, P&L, and Garbage Collection

**`positions_buy*.json`** — keyed by condition ID: question, end, tokens,
tick, `bought_token` / size / cost, hedge proceeds, `pnl_redeem_value`,
`redeem_submitted_at`, quarantine fields. This is how a restart resumes hedge
and redeem without a second buy. **Never delete or truncate.**

**`pnl_buy*.json`** — append-only settled rows: `entry_cost`, `redeem_value`,
`hedge_proceeds`, `net`, `outcome` (`win` / `hedge` / `loss`). `atomic_save()`.

**GC:** only terminal evidence (confirmed redeem value, or confirmed closed
hedge with no remainder) folds a market into P&L and drops it from state.
`record_pnl()` is idempotent by condition ID (crash between P&L and state
saves must not double-count). Unresolved `buy_uncertain` / `hedge_uncertain`
is never GC’d.

If `pnl_redeem_value` is missing, GC may fall back to `redeem_value =
bought_size` (par). That is correct **only** for winner-leg inventory. See
landmine 4.

---

## 10. Configuration & Hot Reload

**`.env`** (gitignored, VM only — never on a cloud agent):

| Variable | Purpose |
|---|---|
| `PRIVATE_KEY`, `FUNDER_ADDRESS` | Signer + Polymarket proxy/funder |
| `API_KEY`, `API_SECRET`, `API_PASSPHRASE` | CLOB L2 auth |
| `RELAYER_URL` | Redeem relayer (production default) |
| `RELAYER_API_KEY`, `RELAYER_API_KEY_ADDRESS` | Preferred redeem HTTP auth |
| `POLY_BUILDER_API_KEY`, `POLY_BUILDER_SECRET`, `POLY_BUILDER_PASSPHRASE` | Alternate Builder HMAC |

**Strategy JSON** is required at startup. Each cycle `load_strategy()` checks
mtime. Ordinary knobs take effect on the **next tick** with **no restart**.
Missing/malformed hot reload **disables new entries** and keeps last-known-good
**hedge** settings (held inventory must still be able to exit). Unknown keys
and invalid types/ranges are rejected.

Templates (safe to commit): `strategy_buy.example.json`,
`strategy_buy5m.example.json`, `strategy_buyhourly.example.json`. Live files
**without** `.example` are gitignored and are what systemd actually reads.

`entry_enabled` is the explicit arm for new buys. `dry_run: false` places
**real orders with no confirmation prompt.** Confirm `dry_run` /
`entry_enabled` before any manual `python buybot*.py`.

| Key | 5m default | 15m / hourly | Meaning |
|---|---|---|---|
| `buy_threshold` / `buy_max_price` | 0.75 / 0.90 | same | Late band (5m last 120s; others whole window) |
| `early_buy_start_s` / `early_buy_max_price` | 300 / 0.99 | — | 5m first 3 min: ask ≥ 90¢, cap 99¢ |
| `early_95_start_s` / `early_95_min_s` / `early_95_min_price` | 300 / 60 / 0.95 | — | 5m first 4 min: ask ≥ 95¢ |
| `min_winner_bid` / `max_loser_bid` / `min_bid_edge` | 0.70 / 0.30 / 0.05 | same | GUI consensus |
| `max_entry_spread` | 0.05 | same | Winner ask−bid at entry |
| `underlying_gate_enabled` / `min_underlying_edge_usd` | true / **0.0** | true / **10.0** | Oracle vs PTB |
| `toxic_force_exit_below` | 0.65 | same | Arm dump if FAK avg &lt; this |
| `hedge_threshold` / `hedge_require_ask_max` | **0.50 / 0.55** | **0.35 / 0.40** | Collapse trigger (ask max ≥ threshold) |
| `hedge_max_spread` / `hedge_require_gui` | 0.15 / true | same | Two-sided book + inverted GUI |
| `hedge_min_price` | 0.325 | 0.32 / 0.32 | Config leftover; **not** a FAK floor |
| `buy_start_s` / `buy_window_min` | 120 s | 4.0 min / 13.0 min | Late window |
| `buy_budget` / `buy_max_spend` / `buy_max_shares` | 2.5 / 3 / 5 | same | Size rails |
| `max_open_positions` | 0 (=unlimited) | same | Probe uses 0 |
| `empty_fak_cooldown_s` | 0.15 | same | After a fully empty trigger |
| `poll_buy_window_s` / `poll_held_s` | 0.1 / 0.05 | same | Hot-loop cadence |
| `tick_size` | `"0.001"` | `"0.01"` | Fallback if Gamma omits it |
| `entry_enabled` / `dry_run` | false / true | same | Arm / paper |

Code deploy ≠ live JSON. After merge: `git pull`, patch
`strategy_buy5m.json` hedge keys if needed, **`systemctl restart polybuybot5m`**.
CI does not restart. A merged bugfix is inert until that restart (13–19 Aug
`known_cost` stall).

---

## 11. Operations on the VM

**Host:** Google Compute Engine instance `instance-20260516-185922`,
`User=ntemusejoel`, `WorkingDirectory=/home/ntemusejoel/poly-money-maker`,
`ExecStart=.../.venv/bin/python buybot5m.py`, `EnvironmentFile=.../.env`,
`Restart=always`. Unit files: `deploy/*.service` (copy to
`/etc/systemd/system/`). Pathlog’s unit does **not** load `.env` (no keys).

```bash
systemctl is-active polybuybot polybuybot5m polybuybothourly polymintbot polypathlog
# expect today: inactive  active  inactive  inactive  active

sudo systemctl restart polybuybot5m    # after validating a 5m code pull
sudo journalctl -u polybuybot5m -f
```

**Do not** `systemctl start polybuybot` / `polybuybothourly` / `polymintbot`
unless the operator asks. Open 15m/hourly inventory is **not** hedged or
redeemed while those processes are down.

**CI deploy** (`.github/workflows/deploy.yml`): push to `main` touching
`buybot*.py`, `pathlog.py`, `check_path_backtest.py`, `buy/**`, or
`requirements.txt` → SSH `git pull` + `pip install`. **No systemd restart.**
A blind restart would start disabled units.

**Audit:**

- `tail -f buybot5m.log` — JSON lines, `RotatingFileHandler` (5 MB × backups).
  `cycle_error`, `buy_skip_*`, `hedge_attempt` / `hedge_fill`, `redeem_submit`,
  `pnl_recorded`.
- `cat .heartbeat_buy5m` — unix timestamp; if it stops, the loop is wedged.
- `positions_buy5m.json` / `pnl_buy5m.json` — open vs settled.
- `check_buy_skips.py --since …` — what **this process** logged.
- ntfy.sh fire-and-forget (`polybot-joel-btc`). Notification failures must
  never crash the bot.

**Disk:** July 2026 the ~10GB boot disk filled from **`/var/log` (~5GB)**, not
bot JSON (~5MB). Cap journal with `deploy/journald-size.conf` (see
`deploy/DISK_OPS.md`). Pathlog ticks are a **second** cap (14d / 400 MB) inside
`pathlog.py`. Export CSVs / `scp` `pathlog/ticks` **before** prune. Do not
`rm` ticks by hand. `STOP_PATHLOG` is the recorder kill switch.

---

## 12. Error Handling Philosophy

- **Never crash the process.** Unexpected exceptions → `cycle_error` with
  `{type}: {msg}` plus traceback. Per-market hedge+buy is wrapped; the poll
  continues. Outer `except` still covers refresh/GC/redeem/UI. Banner does
  **not** sleep 5s. `safe_api_call` turns 429/5xx/timeouts into retries/skips.
- **Fail closed, not fast.** Incomplete book, no GUI consensus, missing PTB,
  flat oracle, unclear POST → do nothing. Not trading is the default.
- **Persist fills that already happened.** A posted FAK cannot be “rejected”
  in software. Below-band buys are logged, written, `toxic_fill` armed, and
  dumped **only while bid ≤ hedge_threshold**. Crashes between POST and save
  recover via exact order ID, then trades, then stable balances.
- **Complete audit trail.** Structured log events + research JSONL for buy
  decisions.

**Worked incident (know this):** 13–19 Aug 2026 the live 5m process logged
**3294/3294** `cycle_error` as `NameError: known_cost`. The 5m/hourly copies
used `known_cost` before assignment inside `buy_uncertain` (`#80` /
`be22662`). That exception was the **outer** poll `except`, so **every later
market in that cycle skipped** (hedges included). Isolation (per-market
`try`) is what stops the *next* NameError from stalling the morning. The
assignment-order fix did nothing until **`systemctl restart polybuybot5m`**
at 09:42Z on 19 Aug — CI had already pulled the code.

---

## 13. Latency, Disk, and Optional AI

**Will a bigger VM make buys/hedges faster?** Usually no. The hot path is
network RTT to Polymarket, force-REST, `atomic_save` fsync, and SDK
sign/POST — not local CPU. Prefer a **non-burstable** instance in a region
with low measured latency to CLOB/Gamma, and keep hedge work off competing
I/O (pathlog’s 8 REST workers, log rotation). Some crypto markets also impose
an exchange taker delay no VM size can remove.

**Disk is the VM’s real constraint.** Journal first (`DISK_OPS.md`), pathlog
second (in-app prune), research JSONL third (50 MiB rotate). State/P&L are
tiny. Do not “fix disk” by truncating `positions_buy5m.json`.

**Can a small LLM validate each buy/hedge?** Not on the synchronous path.
Unbounded tail latency + a new failure mode. Deterministic gates stay
authoritative. If AI is wanted later: shadow scorer that **logs**
agree/disagree without delaying POST; optional **buy-only** veto after that;
**never** delay or veto hedges.

Cloud agents paper-score **public books** (`CLOUD_RESEARCH.md`). They never
receive `.env`, never load live `strategy_buy5m.json`, never `systemctl start`
a buy bot.

---

## 14. Landmines

1. **Three copies, not a library.** Fix `buybot5m.py` → diff `buybot.py` and
   `buybothourly.py`. `buy/entry_skip.py` is 5m-only; do not assume 15m has
   early bands.
2. **5m is seconds; 15m/hourly are minutes.** 5m must define `seconds_left`
   (not only `minutes_left`) or it NameErrors every cycle. `BUY_HORIZON_S`
   must include the early windows or the first 3 minutes are invisible.
3. **Slug excludes are load-bearing.** `btc-updown` prefixes `btc-updown-5m`.
4. **GC par fallback.** `redeem_value = bought_size` is only valid for
   winner-leg inventory.
5. **No module guard.** Never `import buybot`. Tests use `ast` extraction or
   import `buy/` / `check_*.py` / `pathlog.py`.
6. **Tick sizes.** 5m `0.001`; others `0.01`. `hedge_min_price` must be a
   valid tick multiple even though it is not a FAK floor.
7. **State files are sacred.** Never delete/truncate/commit live JSON, logs,
   `.env`. Export pathlog **before** prune.
8. **Ask ≠ price.** 97¢ ask / 1¢ bid is not a 97¢ favorite. Tight REST book
   required. Never trust last-trade GUI on a wide book.
9. **Bid-alone is not a reversal.** Normal hedge needs ask collapsed + GUI.
   `toxic_fill` may dump a wide 1¢/99¢ book **only if bid ≤ hedge_threshold**;
   a recovered 97¢ bid must not dump.
10. **`known_cost` before `spend_cap`.** See §12. `git pull` ≠ process restart.
11. **Live JSON vs code defaults.** Hedge 50/55 on 5m is a default **and**
    must be in `strategy_buy5m.json` or an old 35/40 file keeps the old
    hedge. Early-band keys may be omitted (defaults apply).
12. **`--series 5m` is not a substring.** `15m` contains the letters `5m`.
    `check_path_backtest.py` matches only 5-minute markets. Pathlog
    `--sweep` / `--compare` still score the **late** 75–90 / 120s keys from
    the example JSON plus paper hedge 50/55; they do **not** replay the
    early ≥90 / ≥95 union.

---

## 15. Testing & Research Tools

**CI** (`.github/workflows/test.yml`): Python 3.12, `pip install -r
requirements.txt`, `py_compile` the bots + pathlog + `buy/entry_skip.py` +
key `check_*.py`, then `python3 -m unittest discover -s tests -p 'test_*.py'`.

| Test | What it covers |
|---|---|
| `tests/test_buy_skips.py` | 5m band union / skip reasons (`buy/entry_skip.py`) |
| `tests/test_buy_fill_shapes.py` | Fill/GC/hedge helpers extracted from bot source |
| `tests/test_buy_rejects.py` | Invalid-amount 400 classification |
| `tests/test_path_backtest.py` | Grid/compare/paper helpers; example JSON hedge 0.50 |
| `tests/test_pathlog_prune.py` | Tick retention / lock |
| `tests/test_book.py` | `buy/book.py` level parse |
| `tests/test_fetch_trades.py` | Data API trade CSV merge |
| `tests/test_polydesk.py` | Widget parsing (no window) |

**Pathlog** (`pathlog.py`, `polypathlog`): ~1s CLOB TOB samples, price **and**
size, whole 5m / last 8m of 15m / last 15m of hourly. After expiry, stamps
`winner` from Gamma. No last-trade GUI, no BTC/PTB, no POST latency. Kill:
`touch STOP_PATHLOG`.

**`check_path_backtest.py`** (importable): `--grid` ask × TTM; `--anatomy`
“already decided at T-120 vs tight until the end”; `--compare` named presets;
`--paper` walks later ticks for a **template** hedge (5m example = 50/55/15 +
mid-as-GUI when spread ≤ 10¢); `--sweep` one-at-a-time variants from
`strategy_buy5m.example.json`. Backtest **fills** `min(budget/ask, ask_size)`;
live posts the full dollar size at that ask. Unresolved markets have no redeem
P&L.

**Other `check_*.py`:** `check_book.py` (ad-hoc book), `check_buy_skips.py`
(live log), `check_buy_rejects.py` (invalid-amount 400s),
`check_edge_counterfactual.py` (BTC-gate what-if), `check_fetch_trades.py`
(full-wallet Data API → CSV), `check_participation.py` (bought vs missed).

**Research loop before touching live JSON:** score `--compare --paper`,
`--sweep`, `--anatomy` on **exported** ticks (prune is permanent), then
`check_buy_skips.py` for what the process actually did. Pathlog cannot replay
GUI last-trade, Chainlink, or FAK RTT.

**Mint** (`mintbot.py`): approve + `splitPosition` for equal Up/Down
inventory. **Paused.** `buy/chain.py` + `buy/contracts.py` exist for that
path only. Do not restart `polymintbot` unless asked.

**Widget** (`widget/polydesk.py`): always-on-top mark-to-market / HOLDING on
a **laptop**. Public Data API. Not idle CLOB cash.

---

## 16. Removed / Historical

The repo previously had a sell-side bot family (`bot*.py`), an on-chain
atomic-mint **buyer** (`buy/runner.py` + extra relayer/contracts), a shadow
simulator (`sim/`), and their units. None ran in production; they were deleted
in the 2026-08 cleanup. Retrieve from git history if needed.

`mintbot.py` remains as a **paused** helper (not the deleted runner). The
supported live product is the CLOB buy path in §1–§8, currently 5m-only.

---

## 17. Glossary

| Term | Meaning |
|---|---|
| **Leg** | One side of a binary market (UP or DOWN token) |
| **Condition ID** | On-chain market id; key for state and redeem |
| **Token ID** | CLOB asset id for one leg |
| **CLOB** | Polymarket central limit order book (REST + WS) |
| **Gamma** | Market catalog / metadata / winner after resolve |
| **Data API** | Positions, value, trades, redeemable flag |
| **RTDS** | Polymarket live-data socket (Chainlink TWAP / Binance) |
| **FAK** | Fill-And-Kill: take now, cancel remainder; no resting order |
| **PTB** | Price To Beat — oracle at window open |
| **GUI consensus** | Winner/loser from display mid or last trade (70/30) |
| **Hedge** | Sell-only exit when the held book **and** GUI say that side lost (5m 50/55, 15m/hr 35/40) |
| **`toxic_fill`** | Flag: junk/walk fill; dump only while bid ≤ hedge threshold |
| **Redeem** | Relayer `redeemPositions` of a winning leg at $1.00 |
| **Hot mode** | 50–100 ms poll while held or inside `BUY_HORIZON_S` |
| **GC** | Fold terminal markets into `pnl_buy*.json` and drop state |
| **Quarantine** | `buy_uncertain` / `hedge_uncertain`: POST happened; outcome not settled |
| **Pathlog** | No-order TOB recorder; paper research, not the live bot |
| **Band union** | 5m: more than one ask window can be open at the same TTM |
