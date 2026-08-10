# Poly Money Maker — Technical Design Document

> **Audience:** A reader who is comfortable reading Python but sits somewhere between
> beginner and intermediate in software engineering. This document explains not just
> *what* the code does, but *why* it's written the way it is — naming conventions,
> design trade-offs, and the domain knowledge that informs every function.

---

## Table of Contents

1. [Project Overview](#1-project-overview)
2. [Domain Primer: Polymarket & Prediction Markets](#2-domain-primer-polymarket--prediction-markets)
3. [Architecture at a Glance](#3-architecture-at-a-glance)
4. [File Inventory](#4-file-inventory)
5. [Dependency Stack & Why Each Was Chosen](#5-dependency-stack--why-each-was-chosen)
6. [Configuration & Environment](#6-configuration--environment)
7. [Client Setup & Authentication](#7-client-setup--authentication)
8. [Graceful Shutdown & Notifications](#8-graceful-shutdown--notifications)
9. [Helper Functions — The Foundation Layer](#9-helper-functions--the-foundation-layer)
10. [Position Discovery — Finding What We Own](#10-position-discovery--finding-what-we-own)
11. [Pricing — Reading the Order Book](#11-pricing--reading-the-order-book)
12. [Order Execution — Selling the Loser Leg](#12-order-execution--selling-the-loser-leg)
13. [Redemption — Settling Resolved Markets](#13-redemption--settling-resolved-markets)
14. [The Main Loop — Putting It All Together](#14-the-main-loop--putting-it-all-together)
15. [The Hedge Phase — Cutting Losses on Reversals](#15-the-hedge-phase--cutting-losses-on-reversals)
16. [State Management & Persistence](#16-state-management--persistence)
17. [Error Handling Philosophy](#17-error-handling-philosophy)
18. [The Diagnostic Tool: `check_book.py`](#18-the-diagnostic-tool-check_bookpy)
19. [Live Shadow Simulator (`sim/`)](#19-live-shadow-simulator-sim)
20. [Atomic Mint Buyer (`buy/`) — UNUSED / LEGACY](#20-atomic-mint-buyer-buy--unused--legacy)
21. [Standalone Buy-Side Bots](#21-standalone-buy-side-bots-buybotpy--buybot5mpy--buybothourlypy)
22. [Glossary](#22-glossary)

---

## 1. Project Overview

**Poly Money Maker** runs two families of automated trading bots on Polymarket's
Bitcoin prediction markets — **three buy-side bots are live** on the VM
(2026-08-10). Sell-side and atomic mint services are installed but **inactive**.

#### Live now — Buy-Side Bots (Buy the Winning Leg)

- **`buybot.py`** (`polybuybot`) — **active**. 15-minute BTC markets. Final 3
  minutes, winning leg at 96–99¢, up to **$21**/market. Hedge 65¢, redeem at
  $1.00. No live `strategy_buy.json` → code defaults. State: `positions_buy.json`,
  `pnl_buy.json`, `buybot.log`, `.heartbeat_buy`.
- **`buybot5m.py`** (`polybuybot5m`) — **active**. 5-minute BTC markets. Final
  90 seconds, 96–99¢, up to **$8**/market. Hedge 65¢. No live `strategy_buy5m.json`
  → code defaults. State: `positions_buy5m.json`, `pnl_buy5m.json`,
  `buybot5m.log`, `.heartbeat_buy5m`.
- **`buybothourly.py`** (`polybuybothourly`) — **active**. Hourly BTC markets via
  Gamma series `btc-up-or-down-hourly`. Final 5 minutes, 96–99¢, up to **$24**/market.
  Hedge 65¢. No live `strategy_buyhourly.json` → code defaults. State:
  `positions_buyhourly.json`, `pnl_buyhourly.json`, `buybothourly.log`,
  `.heartbeat_buyhourly`.

#### Installed but inactive — Sell-Side Bots

Live strategy JSON exists on the VM, but systemd units are **inactive**:

- **`bot.py`** (`polybot`) — inactive. `strategy.json`: sell **3¢**, window **3 min**,
  hedge **40¢** (differs from code defaults of 5¢ / 90s).
- **`bot5m.py`** (`polybot5m`) — inactive. `strategy5m.json`: sell **2¢**, window
  opens **150s** out, hedge **40¢** in last **25s**.
- **`bothourly.py`** (`polybot-hourly`) — inactive. `strategy_hourly.json`: sell
  **5¢**, window **5 min**, hedge **65¢** (code default window is 90s).

#### Unused — Atomic Mint

`polybuy` inactive, `polybuy-hourly` inactive, `polybuy5m` **failed**. Configs
`strategy.buy*.json` still on disk (shares 26 / 10 / 30) but mint is not the live
entry path. See §20.

Active entry is the **standalone buy bots** only. Sell bots only matter if
re-enabled against leftover wallet inventory. All bots coordinate through Data API
holdings; they do not share writable state.

### The Core Thesis

In a binary prediction market ("Will Bitcoin go up or down this 5-minute
window?"), you buy **both** sides (UP and DOWN) as a pair — a "complete set."
One side will be worth $1.00 at resolution and the other $0.00. If you hold both,
you're guaranteed to redeem $1.00 per pair.

The profit comes from selling the **loser leg** — the side that's heading to $0.
If you can sell that loser leg for a few cents (e.g. 2–5¢ depending on timeframe)
before the market resolves, instead of letting it expire worthless, you lock in
extra profit on top of the $1.00 redemption. That's exactly what the sell bots
do: they watch the order book, and when the losing side's mid/bid drops to the
configured threshold, they fire a sell order.

### Reversal Hedge (Enabled in Production)

After selling the loser leg, the bot normally retains the other leg for its $1.00
redemption. Sell-side hedges are enabled in production at **40¢** (15m / 5m) or
**65¢** (hourly): if the held leg's bid collapses below the threshold (a genuine
reversal), the bot sells that leg too, capping the loss instead of riding it to
$0. The 5m sell bot only hedges in the **last 25 seconds**. Standalone buy-side
bots use a separate **65¢** hedge on the purchased winning leg. A low held-leg
bid alone is not always reliable evidence of a reversal, so sell-side thresholds
sit deliberately deep below fair value.

### What the Bots Do NOT Do

- **No market making.** They don't post resting orders or provide liquidity.
- **No price prediction.** They don't try to forecast BTC direction.
- **No portfolio rebalancing.** Each bot manages one specific market type.
- **No set-cost gate.** The sell-side bots do not reject expensive complete-set
  entries (e.g. combined avg price > $1.02). The buy-side bots buy at a fixed
  band (96–99¢), so entry cost is bounded by construction.

**Sell-side bots** are sell-only — they do not open new complete-set inventory
themselves (the `buy/` atomic mint path in §20 is **legacy / unused**). **Buy-side
bots** are buy-and-hold — they buy the winning leg at 96–99¢, hold to expiry, and
only exit via hedge (at 65¢ bid) or redemption. Neither family engages in
profit-taking sells.

This narrow scope is intentional — it keeps the codebase small, the failure
modes predictable, and the risk surface minimal.

---

## 2. Domain Primer: Polymarket & Prediction Markets

Before diving into code, you need to understand the domain. This section explains
the concepts the code is built around.

### 2.1 Polymarket

[Polymarket](https://polymarket.com) is a decentralised prediction market built on
Polygon (an Ethereum Layer 2). Users trade shares that represent outcomes of
real-world events. Each share pays $1.00 if its outcome is correct, and $0.00
otherwise.

### 2.2 Binary Markets and "Complete Sets"

A **binary market** has exactly two outcomes — in our case, "Bitcoin UP" and
"Bitcoin DOWN." Each outcome is represented by an **ERC-1155 token** (called a
"position token" or "outcome share"). These tokens trade on Polymarket's
**CLOB** (Central Limit Order Book).

A **complete set** is one UP token + one DOWN token for the same market. Together,
they're always worth exactly $1.00 at resolution (one wins, one loses). You can
**mint** a complete set by depositing $1.00 of USDC, or **redeem** a complete set
to get $1.00 back after the market resolves.

### 2.2.1 Entry Fill Quality (Legacy Atomic Mint)

Earlier production tried to assemble complete sets via two-leg CLOB buys (or
~50¢/side limits) so a set cost ~$1.00. Realized fills often ran above $1.00
(e.g. ~$1.045), which wrecked sell-side unit economics even when loser sells
worked. The `buy/` atomic mint path was built to deposit exactly `$1.00 × shares`
of pUSD on-chain and receive equal UP/DOWN — deterministic $1.00 set cost.

**That mint path is no longer in active use.** Live entry is the standalone buy
bots (§21), which buy only the winning leg at 96–99¢. The unit-economics notes
below remain useful context for any leftover complete-set inventory the sell
bots still manage, and if mint is ever re-enabled.

**If set cost ≠ $1.00, sell P&L depends on entry quality:**

- **Nominal limit price ≠ average fill price.** A resting 50¢ bid can still fill
  worse if the book walks, only one side fills at 50¢, or fees/slippage push the
  effective cost up. Historical trade exports showed complete sets often costing
  **~$1.045/share** (e.g. ~52.7¢ Up + ~51.8¢ Down), not $1.00.
- **Unit economics depend on set cost.** After selling the loser at price `p` and
  redeeming the winner at $1.00:

  ```
  PnL per share ≈ p + 1.00 − set_cost
                = p − (set_cost − 1.00)
  ```

  | Realized set cost | Break-even loser sell | Miss sell (p = 0) |
  |---|---|---|
  | **$1.00** (true 50/50) | **$0.00** | ~$0 (only opportunity cost) |
  | **$1.045** (historical) | **~$0.045** | **−4.5¢/share** locked in |

- **Why this matters more than polling.** Sub-second polling and a tight sell
  window only help *after* entry. If set cost is $1.045, many "successful" sells
  at 1–4¢ still lose money, and every missed sell burns the entry premium. If set
  cost is truly ~$1.00, those same outcomes are flat or
  profitable.
- **Operator checklist (any manual entry):** If a position was entered manually,
  verify `avg_up + avg_dn` (or total USDC in / shares) is **≤ ~$1.01–1.02**. Treat
  anything near $1.04+ as a failed entry fill, not a bot sell failure. Minted
  entries skip this concern entirely.
- **Automated mint is isolated from the bot.** `bot.py` still only records
  `pnl_entry_cost` from Data API `avgPrice` and never places entry orders. The
  `buy/` process closes the complete-set fill-quality gap by atomically
  converting pUSD into equal UP/DOWN inventory at a deterministic $1.00 set cost;
  it runs live under autonomous arming — see §20.

### 2.3 The CLOB (Central Limit Order Book)

Polymarket runs a proper order book — not an AMM (Automated Market Maker). This
means:

- **Bids** are buy orders waiting to be filled.
- **Asks** are sell orders waiting to be filled.
- The **best bid** is the highest price someone is willing to pay.
- The **best ask** is the lowest price someone is willing to sell at.

The bot reads this order book via the SDK to find the best bid before selling.

### 2.4 Key Identifiers

| Term | Meaning |
|---|---|
| **conditionId** | A `bytes32` hash that uniquely identifies a market's outcome condition on-chain. This is the primary key we use to group positions. |
| **token_id** (a.k.a. `asset`) | The ERC-1155 token ID for a specific outcome (e.g., the "UP" token). Each market has two. |
| **slug** | A human-readable URL fragment like `bitcoin-up-or-down-2024-06-24-5pm`. Used for filtering. |
| **funder address** | The Polymarket proxy wallet address that actually holds the funds. The EOA (externally owned account) signs transactions through this proxy. |

### 2.5 FAK Orders

The bot uses **FAK (Fill-And-Kill)** order type, also known as IOC
(Immediate-Or-Cancel). This means: *fill as much as you can right now at the
specified price, then cancel whatever remains.* This is critical for a sell-side
bot — we don't want resting sell orders that might sit unfilled while the market
moves against us. We want immediate execution or nothing.

### 2.6 Neg-Risk Markets

Some Polymarket markets use a "neg-risk" (negative risk) framework — a more
gas-efficient on-chain representation for multi-outcome markets. The bot needs to
know whether a token is neg-risk or not when constructing orders, because the
order-building logic differs.

---

## 3. Architecture at a Glance

Production runs **six** live trading bots on one GCP VM (plus optional shadow
sim). The atomic mint buyer (`polybuy` / `buy/runner.py`) is **not in active use**:

```
                        ┌─────────────────────────┐
                        │  polybuy (buy/runner.py)│
                        │  atomic mint — UNUSED   │
                        │  (legacy; kept in repo) │
                        └─────────────────────────┘
                                 ▼
┌──────────────────────────────────────────────────────────────────┐
│  SELL-SIDE BOTS (sell the loser leg)                              │
├──────────────────────────────────────────────────────────────────┤
│  polybot (bot.py) — 15m markets only                              │
│  discovers set → sells loser leg (5¢, final 90s, 0.1s poll)      │
│  → hedges reversal at 40¢ → redeems winner at $1.00 → pnl.json   │
├──────────────────────────────────────────────────────────────────┤
│  polybot5m (bot5m.py) — 5-minute markets                         │
│  discovers set → sells loser leg (2¢, final 150s, 0.1s poll)     │
│  → hedges reversal at 40¢ (last 25s only) → redeems → pnl.json   │
├──────────────────────────────────────────────────────────────────┤
│  polybot_hourly (bothourly.py) — hourly markets                   │
│  discovers set → sells loser leg (5¢, final 90s, 0.1s poll)      │
│  → hedges reversal at 65¢ → redeems winner at $1.00              │
│  → pnl_hourly.json                                                │
└──────────────────────────────────────────────────────────────────┘

┌──────────────────────────────────────────────────────────────────┐
│  BUY-SIDE BOTS (buy the winning leg at 96–99¢, hold to expiry)   │
├──────────────────────────────────────────────────────────────────┤
│  polybuybot (buybot.py) — 15m markets                             │
│  monitors order books → buys winning leg (96–99¢ ask, ≤$21) │
│  final 3min window → hedges at 65¢ → redeems → pnl_buy.json      │
├──────────────────────────────────────────────────────────────────┤
│  polybuybot5m (buybot5m.py) — 5-minute markets                   │
│  monitors order books → buys winning leg (96–99¢ ask, ≤$8)  │
│  final 90s window → hedges at 65¢ → redeems → pnl_buy5m.json     │
├──────────────────────────────────────────────────────────────────┤
│  polybuybothourly (buybothourly.py) — hourly markets              │
│  monitors order books → buys winning leg (96–99¢ ask, ≤$24) │
│  final 5min window → hedges at 65¢ → redeems → pnl_buyhourly.json│
└──────────────────────────────────────────────────────────────────┘
```

The relayer PROXY flow is documented in §20.9; the sell/hedge/redeem phases in
§12–§15; the standalone buy-side strategy in §21.

Internal structure of a sell bot (`bot.py`):

```
┌──────────────────────────────────────────────────────┐
│                      bot.py                          │
│                                                      │
│  ┌──────────┐   ┌───────────┐   ┌───────────────┐   │
│  │  Config  │──▶│  Client   │──▶│   Main Loop   │   │
│  │ (env +   │   │  Setup    │   │  (while not   │   │
│  │  consts) │   │ (auth +   │   │   shutdown)   │   │
│  │          │   │  funding) │   │               │   │
│  └──────────┘   └───────────┘   └───────┬───────┘   │
│                                          │           │
│                    ┌─────────────────────┼───────┐   │
│                    ▼                     ▼       │   │
│              ┌──────────┐        ┌───────────┐   │   │
│              │ Position │        │  Pricing  │   │   │
│              │ Discovery│        │ (order    │   │   │
│              │ (data-   │        │  book)    │   │   │
│              │  api)    │        └─────┬─────┘   │   │
│              └────┬─────┘              │         │   │
│                   │                    ▼         │   │
│                   ▼              ┌───────────┐   │   │
│              ┌──────────┐        │  Sell     │   │   │
│              │  State   │        │  Exec     │   │   │
│              │  Cache   │        │ (FAK mkt) │   │   │
│              │ (json)   │        └─────┬─────┘   │   │
│              └──────────┘              │         │   │
│                                        ▼         │   │
│                                  ┌───────────┐   │   │
│                                  │  Hedge    │   │   │
│                                  │  Phase    │   │   │
│                                  └─────┬─────┘   │   │
│                                        ▼         │   │
│                                  ┌───────────┐   │   │
│                                  │  Redeem   │   │   │
│                                  │ (relayer) │   │   │
│                                  └───────────┘   │   │
│                    ┌──────────────────────────────┘   │
│                    ▼                                  │
│              ┌──────────┐                             │
│              │  Logging │                             │
│              │ (bot.log)│                             │
│              └──────────┘                             │
└──────────────────────────────────────────────────────┘
```

Internal structure of a buy-side bot (`buybot.py` — same pattern for 5m/hourly):

```
┌──────────────────────────────────────────────────────┐
│                    buybot.py                         │
│                                                      │
│  ┌──────────┐   ┌───────────┐   ┌───────────────┐   │
│  │  Config  │──▶│  Client   │──▶│   Main Loop   │   │
│  │ (env +   │   │  Setup    │   │  (while not   │   │
│  │  consts) │   │ (auth +   │   │   shutdown)   │   │
│  │          │   │  funding) │   │               │   │
│  └──────────┘   └───────────┘   └───────┬───────┘   │
│                                          │           │
│                    ┌─────────────────────┼───────┐   │
│                    ▼                     ▼       │   │
│              ┌──────────┐        ┌───────────┐   │   │
│              │ Position │        │  Pricing  │   │   │
│              │ Discovery│        │ (order    │   │   │
│              │ (data-   │        │  book)    │   │   │
│              │  api)    │        └─────┬─────┘   │   │
│              └────┬─────┘              │         │   │
│                   │                    ▼         │   │
│                   ▼              ┌───────────┐   │   │
│              ┌──────────┐        │ Winner    │   │   │
│              │  State   │        │ Detection │   │   │
│              │  Cache   │        │ (mid cmp) │   │   │
│              │ (json)   │        └─────┬─────┘   │   │
│              └──────────┘              │         │   │
│                                        ▼         │   │
│                                  ┌───────────┐   │   │
│                                  │  Buy Exec │   │   │
│                                  │ (FAK mkt) │   │   │
│                                  └─────┬─────┘   │   │
│                                        ▼         │   │
│                                  ┌───────────┐   │   │
│                                  │  Hedge    │   │   │
│                                  │  (65¢)    │   │   │
│                                  └─────┬─────┘   │   │
│                                        ▼         │   │
│                                  ┌───────────┐   │   │
│                                  │  Redeem   │   │   │
│                                  │ (relayer) │   │   │
│                                  └───────────┘   │   │
│                    ┌──────────────────────────────┘   │
│                    ▼                                  │
│              ┌──────────┐                             │
│              │  Logging │                             │
│              │(buybot.log│                            │
│              └──────────┘                             │
└──────────────────────────────────────────────────────┘
```

         ┌──────────────────────┐
         │  sim/shadow.py       │  ← shadow simulators are currently STOPPED
         │  Live shadow sim     │     (disabled on GCP; see §19)
         │  writes sim_data/    │     never imports bot.py / never orders
         └──────────────────────┘

         ┌──────────────────────┐
         │  buy/runner.py       │  ← atomic mint — UNUSED (legacy)
         │  Atomic mint entry   │     kept in repo; not an active
         │  writes buy_data/    │     production entry path
         └──────────────────────┘

External Services:
  • Polymarket CLOB API  (clob.polymarket.com)    — order book, order submission
  • Polymarket Data API  (data-api.polymarket.com) — position tracking
  • Polymarket Gamma API (gamma-api.polymarket.com) — market discovery (polybuy)
  • Polymarket Relayer   (relayer-v2.polymarket.com) — on-chain mint + redemption
  • ntfy.sh              (ntfy.sh)                 — push notifications
  • Polygon blockchain    (chain ID 137)            — settlement layer
```

The architecture is a **single-file, single-process, polling loop** with
no separate dashboard. The main trading logic
is single-threaded for simplicity, but order book fetches use a background
thread pool for parallel I/O. There are no async frameworks, no message
queues, no databases.

- **Single file (`bot.py`)** keeps everything visible. You can read the entire bot
  top-to-bottom and understand the full flow without jumping between modules.
- **Single process** means no concurrency bugs on state, no race conditions on
  trading logic, no inter-process communication overhead. The only threads are
  read-only HTTP fetches for order books — they never mutate trading state.
- **Polling loop** with variable sleep (5s / 1s / 0.1s depending on time to
  expiry) is simple and robust. The BTC 15-minute markets move extremely fast
  in the final 90-second sell window, so the bot polls every 100ms there.
  Order book fetches are overlapped with sleep via a background thread
  pool, so the effective cycle time is `max(sleep, fetch_time)` instead of
  `sleep + fetch_time`. Polling is far easier to reason about than event-driven
  code.
- **Throttled data fetches** — balance and position queries are cached and only
  refreshed every 2s (positions) and 15s (balance), not every sub-second cycle.
  This reduces API calls from ~240/min to ~30/min (positions) and ~4/min
  (balance) during the sell window.
- **Shadow simulator (`sim/`)** paper-trades every configured BTC series
  using public order books and the sell policy. It never places orders, never
  imports `bot.py`, and writes only under `sim_data/` (see §19). The shadow
  services are currently stopped on the VM to keep API quota and CPU for the
  live services.
- **Atomic mint buyer (`buy/`)** discovers standard binary BTC markets and
  submits one atomic relayer batch containing exact pUSD approval plus CTF
  `splitPosition`. It never imports `bot.py`, never sells or redeems, owns only
  `buy_data/`, and is lower-priority than the sell bot (see §20). It runs live
  and autonomously: a cron job re-arms it and each arm permits exactly one mint.

---

## 4. File Inventory

| File | Purpose | Size |
|---|---|---|
| `bot.py` | The main 15m sell bot — all trading logic lives here | ~1444 lines |
| `bot5m.py` | The 5-minute sell bot — sell/hedge/redeem for 5m BTC markets | ~1397 lines |
| `bothourly.py` | The hourly sell bot — sell/hedge/redeem for hourly BTC markets (65¢ hedge) | ~1349 lines |
| `strategy.example.json` | Example strategy config for `bot.py` (template; may differ from code defaults) | — |
| `strategy5m.example.json` | Example strategy config for `bot5m.py` (2¢ threshold, 150s sell window, 25s hedge) | — |
| `strategy5m.json` | Live 5m strategy config (hot-reloaded, gitignored) | — |
| `strategy_hourly.example.json` | Example strategy config for `bothourly.py` (5¢ threshold, 90s sell window, 65¢ hedge) | — |
| `strategy_hourly.json` | Live hourly strategy config (hot-reloaded, gitignored) | — |
| **Buy-Side Bots (Standalone)** | | |
| `buybot.py` | 15m buy-side bot — buys winning leg at 96–99¢, ≤$21/market, hedges at 65¢ | ~1146 lines |
| `buybot5m.py` | 5m buy-side bot — buys winning leg at 96–99¢, ≤$8/market, 90s window | ~1141 lines |
| `buybothourly.py` | Hourly buy-side bot — buys winning leg at 96–99¢, ≤$24/market, 5min window | ~1144 lines |
| `strategy_buy.example.json` | Example buy-side config for `buybot.py` (96–99¢ band, ≤$21/market, 3min window) | — |
| `strategy_buy5m.example.json` | Example buy-side config for `buybot5m.py` (96–99¢ band, ≤$8/market, 90s window) | — |
| `strategy_buyhourly.example.json` | Example buy-side config for `buybothourly.py` (96–99¢ band, ≤$24/market, 5min window) | — |
| `check_book.py` | Diagnostic script for inspecting live order books | ~30 lines |
| `check_hourly_mint.py` | Diagnostic script for verifying hourly mint eligibility | ~167 lines |
| `backtest_sell_window.py` | Backtest different sell threshold/window combos against shadow data | ~211 lines |
| `sim/` | Live shadow simulator package (paper trade all BTC 5m markets) | package |
| `sim/shadow.py` | Shadow main loop — discover, paper enter, policy, FAK fills, settle | ~863 lines |
| `sim/policy.py` | Pure sell / last-chance / hedge decision logic | ~86 lines |
| `sim/fills.py` | FAK fill simulation against live bid depth | ~87 lines |
| `sim/discovery.py` | Gamma market discovery + parallel public book fetch | ~252 lines |
| `sim/store.py` | `sim_data/` persistence, prune, results summary | ~318 lines |
| `sim/config.py` | Sim paths, strategy defaults, isolation guards | ~181 lines |
| `sim/entry.py` | Paper entry logic for shadow simulator | — |
| `sim/analyze_history.py` | Calibrate `set_cost` from Polymarket history CSV | — |
| `sim/analyze_ticks.py` | Tick-level data analysis utilities | — |
| `sim/test_entry.py` | Unit tests for entry logic | — |
| `sim/test_policy.py` | Unit tests for sell policy logic | — |
| `sim/strategy.sim.json` | Shadow strategy + sim economics (not live `strategy.json`) | — |
| `sim/README.md` | Operator guide for the shadow simulator | — |
| `deploy/polyshadow*.service` | systemd unit templates for shadow simulator variants (mirror, hedge40, hedge45, etc.) | — |
| `buy/` | Isolated complete-set mint package: config, discovery, calldata, chain checks, relayer, state, runner | package |
| `buy/test_buy.py` | Atomic calldata and fail-closed buyer regression tests | — |
| `strategy.buy.example.json` | Live autonomous mint-buyer configuration template (15m series) | — |
| `strategy.buy.5m.example.json` | Example buy config for 5m series | — |
| `strategy.buy.hourly.example.json` | Example buy config for hourly series | — |
| `requirements.buy.txt` | Mint-buyer dependencies (`requests`, `eth-*`, pinned relayer SDK packages); separate from bot deploy | — |
| `deploy/polybot5m.service` | systemd unit for 5m sell bot (`bot5m.py`) | — |
| `deploy/polybot-hourly.service` | systemd unit for hourly sell bot (`bothourly.py`) | — |
| `deploy/polybuybot.service` | systemd unit for 15m standalone buy-side bot (`buybot.py`) | — |
| `deploy/polybuybot5m.service` | systemd unit for 5m standalone buy-side bot (`buybot5m.py`) | — |
| `deploy/polybuybothourly.service` | systemd unit for hourly standalone buy-side bot (`buybothourly.py`) | — |
| `requirements.txt` | Existing live bot and simulator dependencies | 8 lines |
| `strategy.example.json` | Example sell strategy config with all tunable parameters | — |
| `.github/workflows/deploy.yml` | CI/CD pipeline — auto-deploy on push to main | — |
| `.env` | Environment variables (secrets — gitignored) | — |
| `strategy.json` | Live strategy config (hot-reloaded each cycle, gitignored) | — |
| `PRODUCTION_PROMPT.md` | Production deployment prompt / runbook | — |
| `TECHNICAL_DESIGN.md` | This document — architecture & code walkthrough | — |
| `deploy/DISK_OPS.md` | Disk operations guide for the VM | — |
| `deploy/journald-size.conf` | journald log size limit config | — |
| `.gitignore` | Excludes secrets, state files, sim runtime data, Python artifacts | — |
| `positions.json` / `positions5m.json` / `positions_hourly.json` | Sell-side bot state caches (gitignored) | — |
| `pnl.json` / `pnl5m.json` / `pnl_hourly.json` | Sell-side bot P&L history (gitignored) | — |
| `bot.log` / `bot5m.log` / `bot_hourly.log` | Sell-side bot logs with rotation (gitignored) | — |
| `.heartbeat` / `.heartbeat5m` / `.heartbeat_hourly` | Sell-side bot heartbeats (gitignored) | — |
| `positions_buy.json` / `positions_buy5m.json` / `positions_buyhourly.json` | Buy-side bot state caches (gitignored) | — |
| `pnl_buy.json` / `pnl_buy5m.json` / `pnl_buyhourly.json` | Buy-side bot P&L history (gitignored) | — |
| `buybot.log` / `buybot5m.log` / `buybothourly.log` | Buy-side bot logs with rotation (gitignored) | — |
| `.heartbeat_buy` / `.heartbeat_buy5m` / `.heartbeat_buyhourly` | Buy-side bot heartbeats (gitignored) | — |
| `strategy_buy.json` / `strategy_buy5m.json` / `strategy_buyhourly.json` | Live buy-side strategy configs — hot-reloaded each cycle (gitignored) | — |
| `sim_data/` | Shadow runtime outputs only (gitignored) — never bot state | — |
| `buy_data/` | Buyer intents, dry plans, heartbeat, lock, arm/stop files, and rotating log (gitignored) | — |
| `strategy.buy.json` | 15m buyer runtime config (gitignored, VM-only) | — |
| `strategy.buy.5m.json` | 5m buyer runtime config (gitignored, VM-only) | — |
| `strategy.buy.hourly.json` | Hourly buyer runtime config (gitignored, VM-only) | — |
| `buy_data_5m/` | 5m buyer runtime data (gitignored, VM-only) | — |
| `buy_data_hourly/` | Hourly buyer runtime data (gitignored, VM-only) | — |

### Why a Single-File Bot?

For a bot of this size (~1000–1300 lines), splitting into modules would add import
overhead and cognitive load without meaningful benefit. The code is organised
internally with **section comment banners** (e.g., `# --- HELPER FUNCTIONS ---`)
that act as visual module boundaries.

---

## 5. Dependency Stack & Why Each Was Chosen

```@/c:/Users/ntemu/Downloads/poly money maker/requirements.txt:1-9
py_clob_client_v2
requests
python-dotenv
rich
web3
eth-account
eth-abi
eth-utils
```

| Package | What It Does | Why We Use It |
|---|---|---|
| `py_clob_client_v2` | Polymarket's official Python SDK for their CLOB | The only way to submit signed orders to Polymarket's order book. The `_v2` suffix indicates a fork or updated version of the upstream `py_clob_client`. |
| `requests` | HTTP client library | Used for direct REST calls to Polymarket's data-api, the ntfy.sh notification service, and the relayer. The SDK doesn't cover all endpoints we need. |
| `python-dotenv` | Loads `.env` files into `os.environ` | Keeps secrets out of the codebase. The bot reads API keys, private keys, and relayer config from environment variables. |
| `rich` | Terminal formatting library (tables, panels, colours) | Trading bots produce a *lot* of console output. `rich` makes it readable — coloured tables, boxed panels, and formatted numbers. Used for readable console output. |
| `web3` | Ethereum Python library | Used for blockchain interaction (though most on-chain work is done via the relayer). |
| `eth-account` | Ethereum account management | Used to derive the EOA address from the private key for relayer submissions. |
| `eth-abi` | Ethereum ABI encoding | Used to encode redemption calldata (the raw bytes that tell the smart contract what to do). |
| `eth-utils` | Ethereum utility functions | Provides `keccak` (for function selectors) and `to_checksum_address` (Ethereum addresses must be in EIP-55 checksum format). |
| `hexbytes` | Hex-string/bytes type used across the eth stack | `buy/relayer.py` imports `HexBytes` from here (older `eth_utils` versions don't re-export it). |
| `py-builder-relayer-client` / `py-builder-signing-sdk` | Pinned in `requirements.buy.txt` for provenance, but **no longer imported** | The mint relayer now replicates the SDK's PROXY signing flow directly with `eth-abi`/`eth-utils`/`eth-account` + `requests` (§20.9), using `RELAYER_API_KEY`/`RELAYER_API_KEY_ADDRESS` headers instead of builder credentials. |

### The `eth-*` Family

The last four packages (`web3`, `eth-account`, `eth-abi`, `eth-utils`) are only
used in the redemption path. They're needed because redemption isn't an SDK
operation — it's a raw on-chain transaction that we construct manually and submit
through Polymarket's relayer. This requires ABI-encoding function calls exactly
as the Ethereum Virtual Machine expects them.

---

## 6. Configuration & Environment

### 6.1 Environment Variables (`.env`)

The bot reads credentials and relayer configuration from environment variables:

| Variable | Purpose |
|---|---|
| `PRIVATE_KEY` | The EOA private key for signing transactions on Polygon. |
| `FUNDER_ADDRESS` | The Polymarket proxy wallet address that holds the USDC and position tokens. |
| `API_KEY` | CLOB API key (can be pre-generated or derived from the private key). |
| `API_SECRET` | CLOB API secret. |
| `API_PASSPHRASE` | CLOB API passphrase. |
| `RELAYER_URL` | Polymarket relayer base URL (defaults to `https://relayer-v2.polymarket.com`). |
| `RELAYER_API_KEY` | Relayer authentication key (has a hardcoded default from git history). |
| `RELAYER_API_KEY_ADDRESS` | Relayer key address (has a hardcoded default). |
| `NTFY_TOPIC` | ntfy.sh push notification topic (defaults to `polybot-joel-btc`). |
| ~~`BUILDER_API_KEY` / `BUILDER_SECRET` / `BUILDER_PASS_PHRASE`~~ | **No longer required.** The mint relayer uses the same `RELAYER_API_KEY` / `RELAYER_API_KEY_ADDRESS` headers as the sell bot. The `builder_*` kwargs on `MintRelayer.__init__` remain as no-ops for call-site compatibility. |

### 6.2 Strategy Constants (Hot-Reloaded from `strategy.json`)

Strategy parameters are loaded from `strategy.json` at the start of **every
cycle**. This allows you to change thresholds without restarting the bot — just
edit the file and the next tick picks up the new values.

Defaults are defined in `_STRATEGY_DEFAULTS` and used if the file is missing or
malformed:

```python
_STRATEGY_DEFAULTS = {
    "sell_threshold": 0.05,
    "hedge_enabled": True,
    "hedge_threshold": 0.40,
    "sell_window_min": 1.5,            # last 90 seconds — sell window
    "sell_grace_s": 2,                # don't sell within 2s of first seeing a position
    "sell_cooldown_s": 3,             # 3s between sell attempts per leg
    "sell_lastchance_threshold": 0.10, # only sell in final seconds if truly dead
    "sell_lastchance_s": 10,           # final 10-second fallback
    "sell_max_price": 0.055,           # hard cap: never sell above 5.5¢ (threshold + 0.5¢)
    "redeem_throttle_s": 30,          # 30s between redeem attempts
    "max_redeem_age_days": 7,
    "dry_run": False,
    "poll_sell_window_s": 0.1,       # 100ms polling in sell window
    "positions_refresh_s": 2,        # refresh positions every N seconds
    "balance_refresh_s": 15,         # refresh balance every N seconds
}
```

**Why hot-reload instead of hardcoded?** During live trading, you sometimes need
to adjust thresholds without downtime — e.g., tightening `sell_threshold` from
10¢ to 8¢ during volatile conditions, or enabling `dry_run` to debug without
restarting. The `load_strategy()` function reads the file each cycle, coerces
types safely (booleans use `str(v).lower() in ("1", "true", "yes")`), and falls
back to defaults on any parse error.

An example file is committed as `strategy.example.json` for reference.

| Constant | Default (production) | Meaning |
|---|---|---|
| `sell_threshold` | `0.05` (5 cents) | Sell a leg when its **mid-price** (average of best bid and best ask — the price shown on the Polymarket website) is at or below this price anywhere in the final 90-second sell window. Previously triggered on best bid alone, which caused false sells from thin 1-share bids with wide spreads. |
| `hedge_enabled` | `true` | Enables the reversal hedge (production). A low best bid near expiry can reflect spread or illiquidity rather than a true reversal, so the threshold is deep. |
| `hedge_threshold` | `0.40` (40 cents) | Held-leg bid threshold used only when `hedge_enabled` is true. The hedge sell's price limit is half this value (20¢). |
| `sell_window_min` | `1.5` minutes (90 seconds) | Only sell within the last 90 seconds before market expiry. This **time-gates** the sell trigger — even if the loser leg hits 5 cents with 3 minutes left, the bot waits until the final 90 seconds. |
| `sell_grace_s` | `2` seconds | When we first discover a new position, wait 2 seconds before selling. This prevents selling on the very first tick where data might be stale or incomplete. |
| `sell_cooldown_s` | `3` seconds | After selling a leg, wait 3 seconds before attempting another sell on the same leg. |
| `sell_lastchance_threshold` | `0.10` (10 cents) | In the final `sell_lastchance_s` seconds, consider a side below this only when the opposite bid confirms at or above `1 - sell_lastchance_threshold` (90¢). |
| `sell_lastchance_s` | `10` seconds | How many seconds before expiry the confirmed last-chance fallback activates. |
| `sell_max_price` | `0.055` (5.5 cents) | **Hard cap** on sell price. Inside `sell_market_with_retry`, the bot re-fetches the live bid on each retry attempt. If bid > `sell_max_price`, the attempt is skipped (`sell_skip_max_cap`). After 3 skipped attempts, the sell fails and the bot holds to redemption. Replaces the old post-latency bounce check. |
| `redeem_throttle_s` | `30` seconds | After submitting a redemption, wait 30 seconds before retrying. Redemptions are on-chain transactions that take time to confirm. |
| `max_redeem_age_days` | `7` days | Stop trying to redeem after 7 days past expiry. Old conditions may have been cleaned up on-chain and retries are pointless. |
| `dry_run` | `false` | When `true`, the bot logs decisions but doesn't send orders or transactions. Used for testing. |
| `poll_sell_window_s` | `0.1` seconds | Polling interval during the sell window (last 90s). 100ms polling catches rapid price movements; book fetches are overlapped with sleep so the effective cadence is `max(0.1s, fetch_time)`. |
| `positions_refresh_s` | `2` seconds | How often to refresh positions from the data-api. Between refreshs, cached data is used. Prevents excessive API calls during sub-second polling. |
| `balance_refresh_s` | `15` seconds | How often to refresh the USDC balance. Between refreshes, the last known value is displayed. |

### 6.2a Strategy Constants for `bot5m.py` (5-Minute Markets)

`bot5m.py` has its own independent strategy file (`strategy5m.json`, hot-reloaded
each cycle) with defaults tuned for the faster 5-minute market cadence:

```python
_STRATEGY_DEFAULTS = {
    "sell_threshold": 0.02,          # max sell price: never sell above 2¢
    "sell_window_s": 22,             # display-only: colours TTM red in final 22s
    "sell_start_s": 150,             # sell window opens 150s (2.5 min) before expiry
    "sell_start_price": 0.001,       # FAK order floor (min acceptable price)
    "sell_grace_s": 1,               # don't sell within 1s of first seeing a position
    "sell_cooldown_s": 1,            # 1s between sell attempts per leg
    "sell_max_price": 0.025,         # hard cap: never sell above 2.5¢ (threshold + 0.5¢)
    "hedge_enabled": True,           # hedge: sell held leg if reversal detected
    "hedge_threshold": 0.40,         # hedge if held leg bid drops below 40¢
    "hedge_start_s": 25,             # hedge only in last 25 seconds
    "redeem_throttle_s": 30,         # 30s between redeem attempts
    "max_redeem_age_days": 7,
    "dry_run": False,
    "poll_sell_window_s": 0.1,       # 0.1s polling in sell window
    "positions_refresh_s": 1,        # refresh positions every 1s
    "balance_refresh_s": 15,         # refresh balance every 15s
    "tick_size": "0.001",            # fallback tick if market tick lookup fails
}
```

| Constant | 5m Default | vs 15m (`bot.py`) | Rationale |
|---|---|---|---|
| `sell_threshold` | `0.02` (2¢) | 5¢ → 2¢ | 5m markets decay faster; loser is near-zero by 150s. Lower threshold avoids selling while outcome still uncertain. |
| `sell_start_s` | `150` (2.5 min) | 90s → 150s | 5m markets have thinner books; wider window captures more liquidity before books dry up. |
| `hedge_start_s` | `25` | Full window → 25s | 5m markets resolve quickly; restricting hedge to final 25s avoids false hedges from late-book volatility. |
| `sell_cooldown_s` | `1` | 3s → 1s | Faster cadence needed in 5m markets. |
| `sell_grace_s` | `1` | 2s → 1s | Shorter grace; 5m positions are time-critical. |
| `positions_refresh_s` | `1` | 2s → 1s | More frequent position refresh for faster market. |
| `tick_size` | `"0.001"` | `"0.01"` | 5m markets trade on 0.001 ticks (0.1¢ increments). |
| `sell_max_price` | `0.025` (2.5¢) | 5.5¢ → 2.5¢ | Tighter cap for 5m: threshold (2¢) + 0.5¢ buffer. |
| `poll_sell_window_s` | `0.1` | Same | 100ms polling in both bots. |

**Polling tiers in `bot5m.py`:** Since `sell_start_s` is 150, the variable sleep
logic (`_min_ttm_s <= SELL_START_S + 5`) means the bot polls at 0.1s whenever any
position is within 155 seconds of expiry. In 5-minute markets that expire every 5
minutes, this is effectively **always polling at 0.1s**. The 5s and 1s tiers are
dead code for 5m markets. This is intentional — Polymarket's `/book` endpoint
allows 1,500 requests per 10s (150/s), and the bot uses ~50 calls/s at 0.1s
polling, well within limits.

**`sell_window_s` (22) is display-only** — it colours the TTM column red in the
status table when within 22s of expiry. It does not affect the actual sell logic,
which is entirely controlled by `sell_start_s` (150s). The parameter is vestigial
from the old 30s design but kept for display consistency.

An example file is committed as `strategy5m.example.json` for reference.

### 6.3 Sell Thresholds

The normal threshold applies throughout the final 90-second sell window. The
last 10 seconds add a confirmed fallback for a losing side that remains above
the normal threshold:

```
     90s ─────────────────────────────── 0s (expiry)
     │          NORMAL: bid ≤ 5¢           │
                         10s ──────────── 0s
                         │ FALLBACK: bid <10¢
                         │ + opposite bid ≥90¢
```

- **Normal threshold (90→0 seconds):** Sell a held leg whenever its bid is at or
  below `sell_threshold` (5¢). There is no minimum-price floor, so available
  bids below 4¢ are eligible throughout the window.

- **Confirmed fallback (10→0 seconds):** If neither side met the normal 5¢
  threshold, sell a side below `sell_lastchance_threshold` (10¢) only when the
  opposite side's bid is at least 90¢. Thus 9¢/91¢ sells the 9¢ side, while
  12¢/88¢ does nothing. If both bids are low, or the opposite bid does not
  confirm, the bot skips the sale and records `sell_skip_ambiguous`.

- **Max price cap (`sell_max_price`):** The old post-latency bounce check has been
  replaced by a hard price cap inside `sell_market_with_retry`. On each retry
  attempt, the bot re-fetches the live bid. If bid > `sell_max_price` (default:
  threshold + 0.5¢), the attempt is skipped and logged as `sell_skip_max_cap`.
  After 3 skipped attempts, the sell fails and the bot holds to redemption. This
  prevents the worst-case scenario where a bouncing leg fills above the trigger
  threshold during the latency gap between trigger and execution.

  After each fill, the bot also verifies the actual fill price using
  `get_order_details`. If the fill price exceeds `sell_max_price`, a
  `sell_cap_breach` event is logged with the fill price, max price, and order ID.
  This post-fill verification catches any race condition where the bid bounces
  between the pre-check and the order execution.

Across cycles, once one leg is fully sold, the normal threshold preserves the
remaining leg for redemption. Only the explicitly enabled experimental hedge
can override this invariant.

### 6.4 Other Constants

```@/c:/Users/ntemu/Downloads/poly money maker/bot.py:33-42
HOST = "https://clob.polymarket.com"
DATA_API = "https://data-api.polymarket.com"
CHAIN_ID = 137
STATE_FILE = "positions.json"
BTC_SLUG_PREFIX = "btc-updown"  # 15m markets only
BTC_SLUG_ALIASES = ("btc-updown",)
BTC_SLUG_EXCLUDES = ("btc-updown-5m", "bitcoin-up-or-down")
PUSD = "0xC011a7E12a19f7B1f670d46F03B03f3342E82DFB"
CTF = "0x4D97DCd97eC945f40cF65F87097ACe5EA0476045"
```

- **`HOST`** — The CLOB API base URL for order book queries and order submission.
- **`DATA_API`** — A separate Polymarket API for position tracking.
- **`CHAIN_ID = 137`** — Polygon's chain ID.
- **`STATE_FILE`** — The JSON file where we persist metadata between cycles.
- **`BTC_SLUG_PREFIX`** / **`BTC_SLUG_ALIASES`** — Slug prefixes that identify BTC
  markets. `BTC_SLUG_ALIASES` is a **tuple** of accepted prefixes, because
  `btc-updown` is the slug prefix for 15-minute markets. `bitcoin-up-or-down`
  (hourly) and `btc-updown-5m` (5-minute) are excluded — those are managed by
  `bothourly.py` and `bot5m.py` respectively.
- **`PUSD`** — The Polymarket USDC (pUSD) contract address on Polygon. Used in
  redemption calldata.
- **`CTF`** — The **Conditional Token Framework** contract address. This is
  Polymarket's core smart contract that manages outcome tokens and redemptions.

---

## 7. Client Setup & Authentication

```@/c:/Users/ntemu/Downloads/poly money maker/bot.py:117-137
if API_KEY and API_SECRET and API_PASSPHRASE:
    from py_clob_client_v2 import ApiCreds
    api_creds = ApiCreds(
        api_key=API_KEY,
        api_secret=API_SECRET,
        api_passphrase=API_PASSPHRASE,
    )
    console.print("[bold bright_cyan]▶ AUTH[/] [dim]pre-generated API credentials loaded[/]")
else:
    temp_client = ClobClient(host=HOST, key=PRIVATE_KEY, chain_id=CHAIN_ID)
    api_creds = temp_client.create_or_derive_api_key()
    console.print("[bold bright_cyan]▶ AUTH[/] [dim]API credentials derived from private key[/]")

client = ClobClient(
    host=HOST,
    key=PRIVATE_KEY,
    chain_id=CHAIN_ID,
    creds=api_creds,
    signature_type=1,
    funder=FUNDER_ADDRESS,
)
```

### 7.1 The Two-Path Authentication

Polymarket's CLOB uses a three-part API credential system (key, secret, passphrase).
There are two ways to obtain these:

1. **Pre-generated** — you already have them stored in `.env`. This is the fast path.
2. **Derived** — the SDK can derive them from your private key by signing a
   challenge. This requires creating a temporary client first, calling
   `create_or_derive_api_key()`, then creating the real client with the derived
   credentials.

The `if/else` block handles both paths gracefully. The `console.print` lines give
immediate visual feedback about which path was taken — important for debugging
auth issues.

### 7.2 The `signature_type=1` and `funder` Parameters

- **`signature_type=1`** — This tells the SDK to use **Polymarket's proxy wallet
  signature type** (EIP-712 with a specific domain). Polymarket uses a proxy
  contract pattern: your EOA (externally owned account) signs, but the actual
  funds are held by a proxy contract (the "funder"). This is a security measure —
  the proxy can be rotated without changing your private key.

- **`funder=FUNDER_ADDRESS`** — The proxy contract address. All orders are
  executed from this address, not from the EOA directly.

### 7.3 Collateral Synchronisation

```@/c:/Users/ntemu/Downloads/poly money maker/bot.py:139-144
try:
    from py_clob_client_v2.client import BalanceAllowanceParams
    client.update_balance_allowance(BalanceAllowanceParams(asset_type="COLLATERAL"))
    console.print("[bold bright_green]▶ COLLATERAL[/] [dim]allowance synced · USDC.e armed[/]")
except Exception as e:
    console.print(f"[bold red]▶ COLLATERAL [WARN][/] [dim]{e}[/]")
```

Before trading, the bot syncs the USDC allowance with the CLOB contract. On
Polygon, ERC-20 tokens require an "allowance" — a pre-approved spending limit
that the smart contract can draw from. If it fails, we print a warning but don't
crash — the bot might still function for sell-only operations.

### 7.4 The Banner and DRY_RUN Flag

```@/c:/Users/ntemu/Downloads/poly money maker/bot.py:146-164
```

The ASCII art banner provides immediate visual confirmation that the bot started
successfully, along with version and configuration summary. When `DRY_RUN` is
`True`, a yellow warning banner is printed — no orders or on-chain transactions
will be sent, only log entries. This is the bot's safety mode for testing
strategy changes without risking funds.

---

## 8. Graceful Shutdown & Notifications

### 8.1 Signal Handling

```@/c:/Users/ntemu/Downloads/poly money maker/bot.py:168-179
_shutdown_requested = False


def _handle_shutdown(signum, frame):
    global _shutdown_requested
    sig_name = signal.Signals(signum).name
    console.print(f"\n[bold yellow]▶ {sig_name} received — finishing current cycle then exiting[/]")
    _shutdown_requested = True


signal.signal(signal.SIGTERM, _handle_shutdown)
signal.signal(signal.SIGINT, _handle_shutdown)
```

**What it does:** Registers a signal handler that sets a flag when the bot
receives SIGINT (Ctrl-C) or SIGTERM (from `systemctl stop`).

**Why not just let Ctrl-C kill the process?** If the bot is mid-cycle —
specifically mid-sell or mid-redeem — killing it abruptly could leave the state
file inconsistent or lose track of a partially filled order. The graceful
shutdown pattern works like this:

1. Signal received → `_shutdown_requested = True`
2. The main loop checks `while not _shutdown_requested:` at the top of each cycle
3. The current cycle finishes (including any sells or redeems in progress)
4. The loop exits cleanly
5. Final shutdown message is printed and `sys.exit(0)` is called

**Why `global _shutdown_requested`?** The `global` keyword tells Python that
we're modifying the module-level variable, not creating a local one. Without it,
`_shutdown_requested = True` inside the function would create a local variable
that shadows the global, and the main loop would never see the change.

**Why the leading underscore on `_handle_shutdown` and `_shutdown_requested`?**
The underscore convention signals "internal implementation detail — don't call
from outside." These exist solely to support the shutdown mechanism.

### 8.2 Permanent Redeem Failures

```@/c:/Users/ntemu/Downloads/poly money maker/bot.py:182
_redeem_permanent_failures = set()
```

A module-level **set** that tracks condition IDs where redemption permanently
failed (e.g., the proxy wallet doesn't support the operation, or the condition
is invalid). Once a condition is in this set, the bot never tries to redeem it
again. This prevents retry spam on conditions that will never succeed.

**Why a `set` instead of a `list`?** Sets provide O(1) membership testing
(`cond in _redeem_permanent_failures` is instant regardless of size). Lists
require O(n) scanning. For a small bot this difference is negligible, but using
the right data structure is good practice.

### 8.3 Push Notifications via ntfy.sh

```@/c:/Users/ntemu/Downloads/poly money maker/bot.py:186-201
NTFY_TOPIC = os.getenv("NTFY_TOPIC", "polybot-joel-btc")

def notify(title, message, priority="default"):
    """Send a push notification via ntfy.sh. Fire-and-forget."""
    try:
        requests.post(
            f"https://ntfy.sh/{NTFY_TOPIC}",
            data=message.encode("utf-8"),
            headers={"Title": title, "Priority": priority},
            timeout=5,
        )
    except Exception:
        pass  # notifications are best-effort, never crash the bot

notify("Polybot Started", f"Bot started at {datetime.now().strftime('%Y-%m-%d %H:%M:%S UTC')}", priority="high")
```

**What it does:** Sends push notifications to a phone via [ntfy.sh](https://ntfy.sh),
a free push notification service.

**Why notifications?** The bot runs unattended on a server. When something
important happens — a hedge fires (reversal detected), the bot starts, or an
error occurs — the operator needs to know immediately, not when they happen to
check the terminal.

**Why the bare `except Exception: pass`?** Notifications are **best-effort**. If
ntfy.sh is down, or the network is flaky, we don't want the bot to crash or even
log an error. The notification is a nice-to-have, not a critical path. The `pass`
silently swallows the exception.

**Why `timeout=5`?** If ntfy.sh is slow, we don't want to block the bot's cycle
for 30 seconds waiting. 5 seconds is generous for a push notification.

**When is `notify()` called?**

- **Bot started** — confirmation that the bot is running (priority: high)
- **Hedge fired** — reversal detected, cutting losses (priority: urgent)
- **Hedge ghost fill** — hedge confirmed via balance check (priority: urgent)
- **Book unavailable** — order book API unreachable for ~15 consecutive cycles (~15s at 1s polling)
  during sell window (priority: high)

The bot doesn't notify on normal sells — those are expected and frequent. Only
exceptional events warrant a push.

### 8.4 CI/CD Auto-Deploy Pipeline

The bot uses **GitHub Actions** for continuous deployment. When code is pushed to
`main` (specifically changes to `bot.py`, `bothourly.py`, `requirements.txt`, or
`strategy.json`), the pipeline SSHs into the GCP instance and deploys:

```yaml
# .github/workflows/deploy.yml
name: Deploy to GCP
on:
  push:
    branches: [main]
    paths: ['bot.py', 'bot5m.py', 'bothourly.py', 'buybot.py', 'buybot5m.py', 'buybothourly.py', 'requirements.txt', 'strategy.json', 'strategy_buy.json']
jobs:
  deploy:
    runs-on: ubuntu-latest
    steps:
      - name: Deploy via SSH
        uses: appleboy/ssh-action@v1
        with:
          host: ${{ secrets.GCP_HOST }}
          username: ${{ secrets.GCP_USER }}
          key: ${{ secrets.GCP_SSH_KEY }}
          script: |
            cd ~/poly-money-maker
            git pull origin main
            pip install -r requirements.txt --quiet
            sudo systemctl restart polybot
```

**Why SSH-based deployment?** For a single-instance bot, this is the simplest
approach: no container registry, no orchestrator, no rolling deploy needed.
The bot is a single process managed by systemd — restarting it takes < 2 seconds
and the state file (`positions.json`) survives restarts.

**Required GitHub secrets:** `GCP_HOST` (instance IP), `GCP_USER` (SSH
username), `GCP_SSH_KEY` (ED25519 private key for SSH auth).

**Important:** The CI/CD pipeline restarts `polybot` (15m sell bot) and
`polybot5m` (5m sell bot) automatically. The hourly sell bot (`polybot-hourly`)
and all six buy-side bots (both atomic mint and standalone) are **not**
automatically restarted by CI/CD. After a deploy, manually restart affected
bots on the VM (see §8.5.5).

### 8.5 VM Operations — What Runs Where and How to Manage It

The production system runs on a single GCP VM (`instance-20260516-185922`).
Understanding the split between **what's in the Git repository** (code + example
configs) and **what only exists on the VM** (live configs, state, runtime data)
is critical for operators and other agents working on this system.

#### 8.5.1 What Lives in the Git Repository (`git pull` brings these)

| File | Purpose |
|---|---|
| `bot.py` | 15m sell bot code |
| `bot5m.py` | 5m sell bot code |
| `bothourly.py` | Hourly sell bot code |
| `buybot.py` | 15m standalone buy-side bot code |
| `buybot5m.py` | 5m standalone buy-side bot code |
| `buybothourly.py` | Hourly standalone buy-side bot code |
| `buy/` package | Atomic mint buyer code (shared by all three mint buyers) |
| `sim/` package | Shadow simulator code |
| `strategy.example.json` | Example 15m sell strategy (template) |
| `strategy5m.example.json` | Example 5m sell strategy (template) |
| `strategy_hourly.example.json` | Example hourly sell strategy (template) |
| `strategy_buy.example.json` | Example 15m standalone buy strategy (template) |
| `strategy_buy5m.example.json` | Example 5m standalone buy strategy (template) |
| `strategy_buyhourly.example.json` | Example hourly standalone buy strategy (template) |
| `strategy.buy.example.json` | Example 15m atomic mint buy strategy (template) |
| `strategy.buy.5m.example.json` | Example 5m atomic mint buy strategy (template) |
| `strategy.buy.hourly.example.json` | Example hourly atomic mint buy strategy (template) |
| `TECHNICAL_DESIGN.md` | This document |
| `.github/workflows/deploy.yml` | CI/CD pipeline |
| `requirements.txt` / `requirements.buy.txt` | Python dependencies |

#### 8.5.2 What Only Exists on the VM (gitignored, never committed)

| File | Purpose | How to Change |
|---|---|---|
| `.env` | Secrets (private key, funder address, API keys) | Edit directly on VM |
| `strategy.json` | Live 15m sell strategy | Edit on VM; hot-reloaded each cycle |
| `strategy5m.json` | Live 5m sell strategy | Edit on VM; hot-reloaded each cycle |
| `strategy_hourly.json` | Live hourly sell strategy | Edit on VM; hot-reloaded each cycle |
| `strategy_buy.json` | Live 15m standalone buy strategy | Edit on VM; hot-reloaded each cycle |
| `strategy_buy5m.json` | Live 5m standalone buy strategy | Edit on VM; hot-reloaded each cycle |
| `strategy_buyhourly.json` | Live hourly standalone buy strategy | Edit on VM; hot-reloaded each cycle |
| `strategy.buy.json` | Live 15m atomic mint config | Edit on VM; requires buy bot restart |
| `strategy.buy.5m.json` | Live 5m atomic mint config | Edit on VM; requires buy bot restart |
| `strategy.buy.hourly.json` | Live hourly atomic mint config | Edit on VM; requires buy bot restart |
| `positions.json` / `positions5m.json` / `positions_hourly.json` | Sell-side bot state caches | Auto-generated, never edit |
| `pnl.json` / `pnl5m.json` / `pnl_hourly.json` | Sell-side bot P&L history | Auto-generated, never edit |
| `positions_buy.json` / `positions_buy5m.json` / `positions_buyhourly.json` | Buy-side bot state caches | Auto-generated, never edit |
| `pnl_buy.json` / `pnl_buy5m.json` / `pnl_buyhourly.json` | Buy-side bot P&L history | Auto-generated, never edit |
| `buy_data/` | 15m atomic mint runtime (ARM, lock, state, logs) | Auto-managed |
| `buy_data_5m/` | 5m atomic mint runtime | Auto-managed |
| `buy_data_hourly/` | Hourly atomic mint runtime | Auto-managed |
| `bot.log` / `bot5m.log` / `bot_hourly.log` | Sell-side bot logs | Auto-generated |
| `buybot.log` / `buybot5m.log` / `buybothourly.log` | Buy-side bot logs | Auto-generated |
| `.heartbeat` / `.heartbeat5m` / `.heartbeat_hourly` | Sell-side heartbeats | Auto-generated |
| `.heartbeat_buy` / `.heartbeat_buy5m` / `.heartbeat_buyhourly` | Buy-side heartbeats | Auto-generated |

#### 8.5.3 Running Processes on the VM

**Deploy snapshot 2026-08-10** (`git` `fdb0588` on VM at check time):

| Process | systemd | Status |
|---|---|---|
| `polybuybot` / `buybot.py` | `polybuybot` | **active** |
| `polybuybot5m` / `buybot5m.py` | `polybuybot5m` | **active** |
| `polybuybothourly` / `buybothourly.py` | `polybuybothourly` | **active** |
| `polybot` / `bot.py` | `polybot` | inactive |
| `polybot5m` / `bot5m.py` | `polybot5m` | inactive |
| `polybot-hourly` / `bothourly.py` | `polybot-hourly` | inactive |
| `polybuy` (mint 15m) | `polybuy` | inactive |
| `polybuy5m` (mint 5m) | `polybuy5m` | **failed** |
| `polybuy-hourly` (mint hourly) | `polybuy-hourly` | inactive |
| `polyshadow-mirror` | `polyshadow-mirror` | inactive |

Only the three standalone buy bots are live. Sell / mint / shadow units may still
be installed; they are not currently trading.

**Commands (when used):**

| Process | Command | Market |
|---|---|---|
| 15m buy | `.venv/bin/python buybot.py` | `btc-up-or-down-15m` |
| 5m buy | `.venv/bin/python buybot5m.py` | `btc-up-or-down-5m` |
| Hourly buy | `.venv/bin/python buybothourly.py` | `btc-up-or-down-hourly` |
| 15m sell (inactive) | `.venv/bin/python bot.py` | `btc-updown` |
| 5m sell (inactive) | `.venv/bin/python bot5m.py` | `btc-updown-5m` |
| Hourly sell (inactive) | `.venv/bin/python bothourly.py` | `bitcoin-up-or-down` |

Active buy bots are systemd-managed with `Restart=always`.

#### 8.5.4 How to Restart Each Bot

```bash
# Live stack (buy-side only as of 2026-08-10)
sudo systemctl restart polybuybot polybuybot5m polybuybothourly

# Sell-side (currently inactive — only if re-enabling)
sudo systemctl restart polybot polybot5m polybot-hourly
```

Do not restart mint services (`polybuy`, `polybuy5m`, `polybuy-hourly`) unless
deliberately re-enabling mint. `polybuy5m` was **failed** at last check.

#### 8.5.5 Changing Buy/Sell Parameters

**Sell strategy** (thresholds, windows, hedge): Edit the `strategy*.json` file
on the VM. Changes are hot-reloaded on the next tick — no restart needed.

**Standalone buy-side strategy** (buy threshold, buy_budget, window): Edit the
`strategy_buy*.json` file on the VM. Changes are hot-reloaded on the next tick —
no restart needed. All three standalone buy bots support hot-reload via
`load_strategy()`.

**Atomic mint strategy** (unused): `strategy.buy*.json` / `.venv-buy` path is
legacy. Only edit/restart if deliberately re-enabling mint.

**Code changes** (bot logic, bug fixes): Push to GitHub `main` branch. CI/CD
auto-deploys `bot.py` changes (restarts `polybot` only). For `bothourly.py` or
other files, SSH into the VM and run `git pull origin main`, then manually
restart affected bots.

#### 8.5.7 Production Strategy Parameters (as of 2026-08-10 deploy)

**Live buy-side (active services — no `strategy_buy*.json` on VM → code defaults)**

| Parameter | 5m (`buybot5m.py`) | 15m (`buybot.py`) | Hourly (`buybothourly.py`) |
|---|---|---|---|
| Status | **active** | **active** | **active** |
| Buy band | 96–99¢ ask | 96–99¢ ask | 96–99¢ ask |
| Buy budget (USD) | $8 | $21 | $24 |
| Buy window | last 90s | last 3 min | last 5 min |
| Hedge threshold | 65¢ | 65¢ | 65¢ |
| Series slug | `btc-up-or-down-5m` | `btc-up-or-down-15m` | `btc-up-or-down-hourly` |
| Tick size | 0.001 | 0.01 | 0.01 |
| Polling (buy window) | 0.1s | 0.1s | 0.1s |

**Sell-side on disk but inactive** (values from live `strategy*.json` on VM — what
would apply if the units were started; not code defaults)

| Parameter | 5m (`strategy5m.json`) | 15m (`strategy.json`) | Hourly (`strategy_hourly.json`) |
|---|---|---|---|
| Status | inactive | inactive | inactive |
| Sell threshold | 2¢ | 3¢ | 5¢ |
| Sell window | opens 150s before expiry | last 3 min | last 5 min |
| Hedge threshold | 40¢ | 40¢ | 65¢ |
| Hedge window | last 25s | (per strategy / code) | (per strategy / code) |

**Atomic mint** — inactive / failed; `strategy.buy*.json` still present with
shares 10 / 26 / 30 but **not live**. See §20.

### 8.6 Log Rotation

```python
LOG_FILE = "bot.log"
LOG_MAX_BYTES = 5 * 1024 * 1024   # 5 MB per file
LOG_BACKUP_COUNT = 3              # keep bot.log, bot.log.1, bot.log.2, bot.log.3

_file_logger = logging.getLogger("polybot")
_file_logger.setLevel(logging.INFO)
_log_handler = RotatingFileHandler(LOG_FILE, maxBytes=LOG_MAX_BYTES, backupCount=LOG_BACKUP_COUNT)
_log_handler.setFormatter(logging.Formatter("%(message)s"))
_file_logger.addHandler(_log_handler)
```

**Why log rotation?** Without it, `bot.log` grows indefinitely — at 5s ticks
with sells happening hourly, the log can reach 100+ MB within weeks. The
`RotatingFileHandler` from Python's `logging` module automatically rolls over
to `bot.log.1`, `.2`, `.3` when the file hits 5 MB, keeping total disk usage
under 20 MB.

### 8.7 Heartbeat File

Each cycle, the bot writes a JSON object with timestamp and tick count to
`.heartbeat`:

```@/c:/Users/ntemu/Downloads/poly money maker/bot.py:252-258
def write_heartbeat():
    """Write current timestamp to heartbeat file for health monitoring."""
    try:
        with open(HEARTBEAT_FILE, "w") as f:
            json.dump({"ts": time.time(), "iso": datetime.now().isoformat(), "cycle": CYCLE}, f)
    except Exception:
        pass
```

**Why a heartbeat file?** External monitoring (e.g., a cron job or systemd
watchdog) can check the file's modification time. If `.heartbeat` hasn't been
updated in > 30 seconds, the bot is frozen. This is simpler and more reliable
than parsing logs or checking process status — a hung Python process might still
show as "running" in `ps` but not actually be doing work.

### 8.7 P&L Tracking

The bot records a P&L entry for each completed trade when a position is
garbage-collected (GC'd — meaning it no longer appears in the data-api response).
The P&L system maintains a cumulative summary (total P&L, wins, losses) and a
list of individual trade records, capped at 500 to prevent unbounded growth:

```@/c:/Users/ntemu/Downloads/poly money maker/bot.py:272-298
def record_pnl(condition_id, question, entry_cost, sell_proceeds, hedge_proceeds, outcome):
    """Record a completed trade's P&L."""
    pnl_data = load_pnl()
    net = sell_proceeds + hedge_proceeds - entry_cost
    trade = {
        "ts": datetime.now().isoformat(),
        "condition_id": condition_id,
        "question": question[:40],
        "entry_cost": round(entry_cost, 4),
        "sell_proceeds": round(sell_proceeds, 4),
        "hedge_proceeds": round(hedge_proceeds, 4),
        "net_pnl": round(net, 4),
        "outcome": outcome,
    }
    pnl_data["trades"].append(trade)
    s = pnl_data["summary"]
    s["total_pnl"] = round(s["total_pnl"] + net, 4)
    s["total_trades"] += 1
    if net >= 0:
        s["wins"] += 1
    else:
        s["losses"] += 1
    # Keep last 500 trades to prevent unbounded growth
    if len(pnl_data["trades"]) > 500:
        pnl_data["trades"] = pnl_data["trades"][-500:]
    atomic_save(PNL_FILE, pnl_data)
    return net
```

**P&L fields tracked in `positions_meta`:**

| Field | Source | When Set |
|---|---|---|
| `pnl_entry_cost` | `size × avgPrice` from data-api | First sighting of position |
| `pnl_init_up_size` / `pnl_init_dn_size` | Initial share counts from data-api | First sighting |
| `pnl_sell_proceeds` | `sold × observed_bid` | After each sell fill |
| `pnl_hedge_proceeds` | `sold × observed_bid` | After each hedge fill |
| `pnl_redeem_value` | `shares × $1.00` (estimated from remaining holdings) | On GC or explicit redeem |

**Entry cost accuracy:** Uses the actual `avgPrice` field returned by the
data-api (typically ~$0.51-0.53 per share historically), not a $0.50 guess.
Total entry cost is `up_size × up_avgPrice + dn_size × dn_avgPrice`. The
**combined set cost** (`avg_up + avg_dn`) drives unit economics: at ~$1.045 the
strategy needs loser sells above ~4.5¢ to break even; at a true ~$1.00
(50¢+50¢ limits fully filled) break-even is ~$0. See §2.2.1 — entry fill quality
is the main open issue outside the sell bot.

**Redemption estimation:** When a position disappears from the data-api
(resolved and auto-redeemed by Polymarket), the bot estimates winner payout
from remaining holdings: `max(remaining_up, remaining_dn) × $1.00`. This
handles the common case where positions are redeemed by the protocol before
the bot's explicit `redeem_condition` call.

**Set-cost gate (not implemented):** The bot does not alert or skip when realized
set cost exceeds a threshold (e.g. $1.02). That remains operator responsibility
until automated. See §2.2.1.

---

## 9. Helper Functions — The Foundation Layer

These are the utility functions that the rest of the bot builds on. They handle
cross-cutting concerns: API safety, file I/O, logging, and balance queries.

### 9.1 `safe_api_call` — The Error Filter

```@/c:/Users/ntemu/Downloads/poly money maker/bot.py:206-213
def safe_api_call(func, *args, **kwargs):
    try:
        return func(*args, **kwargs)
    except Exception as e:
        err_str = str(e)
        if not any(s in err_str for s in ("order couldn't be fully filled", "not enough balance", "not found", "404")):
            console.print(f"  [bold red][API ERR][/] [dim]{err_str[:120]}[/]")
        raise
```

**What it does:** Wraps any API call with structured error handling.

**Why `*args, **kwargs`?** This is a **decorator-like wrapper**. By accepting
arbitrary positional and keyword arguments, it can wrap *any* SDK method without
knowing its signature. `func(*args, **kwargs)` forwards all arguments to the
underlying function.

**Why the error filtering?** Not all errors are worth printing. "Order couldn't
be fully filled" is expected with FAK orders (partial fills are normal). "Not
enough balance" and "404" are also expected during normal operation. Printing
these would flood the console with noise.

**Why does it `raise` after printing?** The function doesn't swallow errors — it
lets them propagate to the caller. The caller decides whether to retry, skip, or
abort. This function's job is just to *log*, not to handle recovery. Separation
of concerns.

### 9.2 `get_balance` — Reading USDC Balance

```@/c:/Users/ntemu/Downloads/poly money maker/bot.py:216-224
def get_balance():
    try:
        from py_clob_client_v2.client import BalanceAllowanceParams
        bal_info = safe_api_call(client.get_balance_allowance, BalanceAllowanceParams(asset_type="COLLATERAL"))
        if bal_info:
            return float(bal_info.get("balance", 0)) / 1_000_000
    except Exception:
        pass
    return 0.0
```

**Why the `/ 1_000_000`?** On Polygon, USDC uses **6 decimal places**. The API
returns the raw integer amount (e.g., `15000000` for $15.00). Dividing by
1,000,000 converts to human-readable dollars. The underscores in `1_000_000` are
Python's **digit separator syntax** (PEP 515) — purely cosmetic.

**Why the bare `except Exception: pass`?** If the balance query fails, we return
`0.0` rather than crashing. The balance is displayed in the UI but isn't critical
to the sell logic. This is a **defensive fallback** pattern.

**Why the local import?** `BalanceAllowanceParams` is imported inside the
function (a **lazy import**) — it defers the import until the function is
actually called, keeping the top-level imports clean.

### 9.3 `atomic_save` — Crash-Safe File Writes

```@/c:/Users/ntemu/Downloads/poly money maker/bot.py:227-231
def atomic_save(path, data):
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, path)
```

**Why is this necessary?** If you write directly to `positions.json` and the
process is killed mid-write, you end up with a corrupted, half-written file. On
the next startup, `json.load()` would throw and the bot would lose all its state.

**How does it work?** The **write-to-temp-then-rename** pattern:

1. Write the full data to `positions.json.tmp`.
2. Call `os.replace(tmp, path)` — this atomically replaces the old file with
   the new one. The file system either sees the old file or the new file, never
   a half-written mix.

### 9.4 `load_json` and `save_json` — Thin Wrappers

```@/c:/Users/ntemu/Downloads/poly money maker/bot.py:234-242
def load_json(path):
    if os.path.exists(path):
        with open(path, "r") as f:
            return json.load(f)
    return {}

def save_json(path, data):
    atomic_save(path, data)
```

`load_json` returns an empty dict `{}` if the file doesn't exist (e.g., on first
run). This is the **null object pattern** — instead of returning `None` and
forcing every caller to check, we return a valid-but-empty container. `save_json`
is a one-line wrapper around `atomic_save` for API symmetry.

### 9.5 `log_event` — Structured Logging

```@/c:/Users/ntemu/Downloads/poly money maker/bot.py:245-249
def log_event(event, **kwargs):
    """Append a structured JSON log line to bot.log (with rotation)."""
    entry = {"ts": datetime.now().isoformat(), "event": event}
    entry.update(kwargs)
    _file_logger.info(json.dumps(entry))
```

**Why JSON-lines instead of plain text?** Each line is a self-contained JSON
object (a format called [JSON Lines](https://jsonlines.org) or JSONL). This
makes the log **machine-parseable** — you can `cat bot.log | jq .` to filter,
search, and aggregate. Each entry includes a timestamp (`ts`), an event name,
and any additional fields passed as keyword arguments.

**How `**kwargs` works here:** When you call `log_event("sell_fill",
condition_id="0xabc", leg="up", sold=50, price=0.08)`, Python collects the
keyword arguments into a dict: `{"condition_id": "0xabc", "leg": "up",
"sold": 50, "price": 0.08}`. The `entry.update(kwargs)` line merges these into
the log entry. Different events can log different fields without changing the
function signature.

---

## 10. Position Discovery — Finding What We Own

The bot needs to know what positions it holds before it can manage them. This
section covers the data fetching, filtering, grouping, and expiry-time parsing
logic.

### 10.1 `get_user_positions` — Fetching from the Data API

```@/c:/Users/ntemu/Downloads/poly money maker/bot.py:303-319
def get_user_positions():
    """Fetch user's current open positions from Polymarket data-api.
    Returns a list of position dicts on success, or None on failure."""
    try:
        res = requests.get(
            f"{DATA_API}/positions",
            params={"user": FUNDER_ADDRESS, "limit": 200},
            timeout=10,
        )
        res.raise_for_status()
        data = res.json() or []
        if not isinstance(data, list):
            return None
        return data
    except Exception as e:
        console.print(f"  [bold red][DATA-API FAIL][/] [dim]{e}[/]")
        return None
```

**Why the data API and not the CLOB SDK?** The CLOB SDK is for order book
operations (placing orders, reading books). Position data — what you actually
hold — comes from a separate REST endpoint at `data-api.polymarket.com`. This
separation is common in exchange architectures: the trading engine and the
portfolio tracker are different services.

**Why `None` on failure instead of `[]`?** This is a critical distinction. An
empty list means "we successfully queried the API and we hold nothing." `None`
means "the query failed — we don't know what we hold." The main loop uses this
distinction to decide whether to garbage-collect stale metadata: it only GCs
when the query succeeded (`positions_raw is not None`).

**Why `timeout=10`?** Without a timeout, `requests.get()` would hang forever if
the API is unresponsive. 10 seconds is generous for a simple position query.

**Why `res.raise_for_status()`?** This raises an `HTTPError` if the response code
is 4xx or 5xx. Without it, a 500 error would silently return an error page as
"data," and the bot would try to parse it as a position list.

**Why `isinstance(data, list)`?** Defensive programming — if the API returns a
dict (e.g., an error envelope) instead of a list, we return `None` rather than
letting downstream code crash when it tries to iterate.

### 10.2 `check_token_balance` — Verifying Actual Holdings

```@/c:/Users/ntemu/Downloads/poly money maker/bot.py:322-335
def check_token_balance(token_id):
    """Re-fetch positions and return the current size for a specific token.
    Returns the float balance, or None if lookup fails."""
    try:
        positions = get_user_positions()
        if positions is None:
            return None
        for p in positions:
            if p.get("asset") == token_id:
                return float(p.get("size", 0))
        return 0.0
    except Exception:
        return None
```

**Why does this exist?** After a sell attempt that returns 0 confirmed fills,
the bot needs to check whether shares were actually sold but the order response
didn't report it (a "ghost fill"). This function re-queries the data API to get
the ground-truth balance. If the balance is lower than expected, the sell *did*
go through — the SDK just didn't tell us.

This is the **ghost fill detection** pattern, which we'll see in the sell phase.

### 10.3 `parse_position_end_dt` — Parsing Market Expiry from Slugs

```python
_ET = ZoneInfo("America/New_York")
_SLUG_TIME_RE = re.compile(r"(\d{1,2})(am|pm)-et$")
_SLUG_DURATION_RE = re.compile(r"-(\d+)m-")

def parse_position_end_dt(legs):
    # Strategy 1: Unix timestamp in slug (5-minute markets)
    for p in legs:
        for key in ("slug", "eventSlug"):
            slug = p.get(key) or ""
            tail = slug.rsplit("-", 1)[-1]
            if tail.isdigit():
                ts = int(tail)
                if ts > 1_700_000_000:
                    # Slug timestamp is the market START time.
                    # Detect duration from slug (e.g. "-5m-" → 5 minutes)
                    # and add it to get the actual end/expiry time.
                    dur_match = _SLUG_DURATION_RE.search(slug)
                    dur_min = int(dur_match.group(1)) if dur_match else 60
                    return datetime.fromtimestamp(ts) + timedelta(minutes=dur_min)

    # Strategy 2: Hour+AM/PM from slug + UTC date matching (hourly markets)
    for p in legs:
        for key in ("slug", "eventSlug"):
            slug = p.get(key) or ""
            m = _SLUG_TIME_RE.search(slug)
            if not m:
                continue
            hour_12 = int(m.group(1))
            ampm = m.group(2)
            # Convert to 24h; slug hour is window START, expiry = +1h
            hour_24 = (hour_12 % 12) + (12 if ampm == "pm" else 0)

            end_date_str = p.get("endDate") or ""
            end_date_utc = ...  # parse to UTC date

            # Try day_offset in (0, -1, 1) to find which date's
            # expiry falls on end_date's UTC calendar date
            for day_offset in (0, -1, 1):
                candidate = datetime(year, month, day + day_offset,
                                     hour_24, 0, tzinfo=_ET) + timedelta(hours=1)
                if candidate.astimezone(UTC).date() == end_date_utc:
                    return candidate.astimezone(tz=None).replace(tzinfo=None)
```

**What it does:** Determines when a market expires by parsing the slug and
matching it against the `endDate` from the API. This is the most critical
calculation in the bot — getting it wrong means the sell window opens at the
wrong time (or never).

**The two parsing strategies:**

1. **Unix timestamp in slug** — 5-minute market slugs (e.g.,
   `btc-updown-5m-1783261800`) end with a Unix timestamp representing the market
   **start** time (not end). The function detects the duration marker in the slug
   (`-5m-`) via `_SLUG_DURATION_RE` and adds it to the timestamp to compute the
   actual expiry. If no duration marker is found (e.g., an hourly market with a
   timestamp slug), it defaults to 60 minutes.

   **Critical detail:** The timestamp is the *start* time. A slug like
   `btc-updown-5m-1783261800` where `1783261800` = July 5, 10:30 AM ET means the
   market runs 10:30–10:35 AM. The function returns 10:35 AM (start + 5 min), not
   10:30 AM. Getting this wrong would make the sell window open 5 minutes early
   (PR #38 fixed this bug).

2. **Hour + AM/PM from slug with UTC date matching** — Hourly BTC market slugs
   end with a time like `5pm-et` (e.g., `bitcoin-up-or-down-2024-06-24-5pm-et`).
   We use a regex to extract the hour and AM/PM, convert to 24-hour format, then
   add 1 hour (slug is the window *start*; market *closes* 1 hour later).

   The critical challenge is determining the correct **calendar date** for the
   expiry. The API provides an `endDate` field in UTC (e.g., `2026-07-04`), but
   because ET is UTC-4 (EDT), evening markets cross midnight UTC. For example:
   - A "9PM ET" market expires at 10PM ET = 02:00 UTC **the next day**
   - A "5PM ET" market expires at 6PM ET = 22:00 UTC **the same day**

   The current approach (PR #35) is correct by construction: it tries
   `day_offset` values of 0, -1, and +1, computing the candidate expiry in ET,
   converting to UTC, and checking if the UTC calendar date matches `endDate`.
   Exactly one offset will match for any hour, both during EDT and EST.

**Why three regexes at module level?**
- `_SLUG_TIME_RE` — matches hourly slug tails like `5pm-et`
- `_SLUG_DURATION_RE` — matches duration markers like `-5m-` in 5-minute slugs

These are **compiled once** at import time and reused across all calls. The
leading underscore marks them as internal.

**Why the day_offset loop instead of naive date arithmetic?** Naive approaches
(like "just use the endDate as the ET date") fail for evening markets that cross
the UTC midnight boundary. The loop is a brute-force but provably correct
solution: since the expiry can only be on the endDate's UTC calendar date, and
the ET→UTC offset is at most ±1 day, trying 3 offsets guarantees finding it.

**Verified for all 24 hours:** The hourly approach has been tested against every
hourly slot (12AM through 11PM ET) in both EDT and EST. The 5-minute approach
has been verified against live slug data from the data-api.

### 10.4 `empty_opposite_leg` — Constructing Missing Legs

```@/c:/Users/ntemu/Downloads/poly money maker/bot.py:399-409
def empty_opposite_leg(source, outcome):
    return {
        "asset": source.get("oppositeAsset"),
        "size": 0,
        "outcome": outcome,
        "redeemable": False,
        "endDate": source.get("endDate"),
        "slug": source.get("slug"),
        "eventSlug": source.get("eventSlug"),
        "title": source.get("title"),
    }
```

**Why does this exist?** Sometimes the data API only returns one leg of a
position — for example, if you sold all your DOWN shares, the API might only
show the UP position. But the bot still needs to know about both legs to manage
the set properly. This function constructs a **synthetic empty leg** using
metadata from the existing leg (the `oppositeAsset` field tells us the token ID
of the other side).

### 10.5 `group_btc_complete_sets` — The Core Grouping Function

```@/c:/Users/ntemu/Downloads/poly money maker/bot.py:412-500
def group_btc_complete_sets(positions, positions_meta=None):
    """Filter to BTC markets, grouped by conditionId with UP/DOWN leg metadata.
    Includes single-leg positions so direct sells can still be managed."""
```

This is the most complex function in the discovery phase. It transforms a flat
list of position dicts into a sorted list of "managed sets" — each representing
a complete UP+DOWN pair for a BTC hourly market.

**Step 1: Filter to BTC markets**

```python
for p in positions:
    slug = (p.get("slug") or "").lower()
    event_slug = (p.get("eventSlug") or "").lower()
    if slug.startswith(BTC_SLUG_EXCLUDES) or event_slug.startswith(BTC_SLUG_EXCLUDES):
        continue
    if not (
        slug.startswith(BTC_SLUG_ALIASES)
        or event_slug.startswith(BTC_SLUG_ALIASES)
    ):
        continue
```

We check `slug` and `eventSlug` against the alias prefixes, after first
excluding slugs managed by other bots. The `startswith(BTC_SLUG_ALIASES)` call
works because `str.startswith()` accepts a **tuple** of prefixes — it returns
`True` if the string starts with *any* of them. This is a clean Python idiom
for multi-prefix matching.

**Step 2: Group by conditionId**

```python
by_cond.setdefault(cond, []).append(p)
```

`setdefault` is another Python dict idiom: if `cond` isn't in the dict, set it
to `[]` and return that new list. If it is, return the existing list. Then we
append the position. This groups all legs of the same market together.

**Step 3: Identify UP and DOWN legs**

```python
for p in legs:
    oc = (p.get("outcome") or "").lower()
    if oc in ("up", "yes"):
        up = p
    elif oc in ("down", "no"):
        dn = p
```

We accept both "up"/"down" and "yes"/"no" as outcome labels because Polymarket
has used both conventions. If only one leg exists, we construct the other using
`empty_opposite_leg`.

**Step 4: Parse expiry time**

```python
end_dt = parse_position_end_dt(legs)
if not end_dt:
    continue
end_ts = end_dt.timestamp() * 1000
```

If we can't determine the expiry, we skip this set — we can't manage what we
can't time.

**Step 5: Sort by expiry**

```python
sets.sort(key=lambda s: s["end_ts"])
```

Markets closest to expiry appear first. This is important because the main loop
processes sets in order — we want to handle the most urgent positions first.

**Step 6: Inject orphan legs from metadata**

```python
if positions_meta:
    existing_conds = {s["conditionId"] for s in sets}
    now_ms = time.time() * 1000
    for cond, meta in positions_meta.items():
        if cond in existing_conds:
            continue
        exp_up = meta.get("expected_up_size", 0)
        exp_dn = meta.get("expected_dn_size", 0)
        if exp_up <= 0 and exp_dn <= 0:
            continue
        ...
```

This is the **resilience backfill**. If we sold shares last cycle but the data
API hasn't caught up (or is temporarily down), the position might disappear from
the API response. But we still have metadata — token IDs, expected sizes, and
expiry — from the last successful cycle. We inject these as synthetic sets so
the bot can continue managing them.

If the market has already expired (`end_ts <= now_ms`), we zero out the expected
sizes and skip — no point managing an expired market.

---

## 11. Pricing — Reading the Order Book

### 11.1 `get_book_bid` / `get_book_quote` — Reading the Order Book

The bots use two functions to read the order book:

**`get_book_bid(token_id)`** returns `(best_bid_price, best_bid_size)` — used
inside `sell_market_with_retry` for the max-price cap check before each FAK
attempt.

**`get_book_quote(token_id)`** returns the full quote tuple:
`(bid_price, bid_size, ask_price, ask_size, mid_price)` — used by the sell
loop's background book fetch and for hedge decisions. The **mid-price**
((bid + ask) / 2) is what the Polymarket website displays as the market price.

**Why mid-price for the sell trigger?** Previously, the sell trigger fired when
the best bid alone was ≤ `SELL_THRESHOLD`. This caused a reversal on the 5m
market "BTC Up or Down — August 7, 5:20PM-5:25PM ET": a thin 1-share bid at
$0.018 on the Up side triggered a sell of 10 shares at $0.0186, while the ask
was still at $0.50 (mid = $0.26). The Up side won, costing the bot ~$9.80 in
lost redemption value. Switching to mid-price ensures the bot only sells when
both bid and ask confirm the leg is truly losing, ignoring stray thin bids.

Both functions use the same SDK-first + HTTP-fallback pattern: try
`client.get_order_book` via `safe_api_call`, and on SDK failure, fall back to a
plain `requests.get` to the public `/book` endpoint (no auth required).

**Why return a tuple `(price, size)`?** The caller needs both: the price
determines whether to sell, and the size determines how many shares we can
actually sell at that price (the "depth"). Returning a tuple is a lightweight
alternative to creating a dataclass — no extra class definition needed.

**Why `(None, 0.0)` on failure?** `None` for price means "no bid available" —
the caller checks `if bid is not None` before acting. This is the **null sentinel
pattern**: `None` is a signal value that means "data unavailable," distinct from
`0.0` which would mean "the best bid is $0.00" (a valid but different meaning).

### 11.2 `quote_leg` — Pricing a Single Leg

```@/c:/Users/ntemu/Downloads/poly money maker/bot.py:663-674
def quote_leg(bid):
    """Return (self_price, matched_price) for a single leg.

      - self_price:    leg's value used for sell/hedge decisions. Only uses bid
                       (actual buyers on the book). Returns None if no bid exists
                       — the bot will NOT attempt to sell into an empty book.
      - matched_price: realizable price for a direct sell (bid).
    """
    if bid is None:
        return None, None
    return float(bid), float(bid)
```

**Why a separate function if it just returns the bid twice?** This is an
**abstraction layer**. In a more complex bot, `self_price` and `matched_price`
might differ — for example, `self_price` could be a midpoint or a weighted
average, while `matched_price` would be the actual bid you'd get hit at. By
encapsulating the pricing logic in a function, the main loop doesn't need to know
*how* prices are computed — it just calls `quote_leg` and gets the numbers.

Currently both values are the same (the best bid), but the architecture is ready
for more sophisticated pricing without changing the call sites.

**Why `None` for an empty book?** The docstring is explicit: "the bot will NOT
attempt to sell into an empty book." If there are no buyers, selling is
impossible — the order would just be cancelled. Returning `None` makes this
impossible to accidentally ignore, because the caller must explicitly check for
`None` before using the price.

---

## 12. Order Execution — Selling the Loser Leg

### 12.1 `extract_order_id` — Handling Polymorphic Responses

```@/c:/Users/ntemu/Downloads/poly money maker/bot.py:520-523
def extract_order_id(order_obj):
    if isinstance(order_obj, dict):
        return order_obj.get("orderID") or order_obj.get("id")
    return getattr(order_obj, "orderID", None) or getattr(order_obj, "id", None) or (str(order_obj) if order_obj is not None else None)
```

**Why is this necessary?** The SDK's response type is inconsistent — sometimes
it returns a dict, sometimes an object with attributes. This function handles
both cases using `isinstance` to check the type and then using the appropriate
access method (`.get()` for dicts, `getattr()` for objects).

**Why the `or` chain?** The `or` operator in Python returns the first truthy
value. `order_obj.get("orderID") or order_obj.get("id")` tries `orderID` first;
if it's `None` or empty string (falsy), it falls back to `id`. This is a concise
way to handle multiple possible field names without nested `if/else`.

### 12.2 `get_order_details` — Querying Order Status

```@/c:/Users/ntemu/Downloads/poly money maker/bot.py:526-548
def get_order_details(order_id):
    """Return dict with size_matched and status, or None."""
    if not order_id:
        return None
    try:
        result = safe_api_call(client.get_order, order_id)
        if isinstance(result, dict):
            return {
                "status": result.get("status", "UNKNOWN"),
                "size_matched": float(result.get("size_matched", 0) or result.get("takerAmount", 0) or 0),
                "size": float(result.get("size", 0) or result.get("originalSize", 0) or result.get("makerAmount", 0) or 1),
            }
        return {
            "status": getattr(result, "status", "UNKNOWN"),
            "size_matched": float(getattr(result, "size_matched", 0) or 0),
            "size": float(getattr(result, "size", 1)),
        }
    except Exception as e:
        err = str(e).lower()
        if "not found" in err or "404" in err:
            return {"status": "NOT_FOUND"}
        return None
```

**Why the multiple `or` fallbacks for field names?** The API has changed field
names over time. `size_matched` might be `takerAmount` in some versions.
`size` might be `originalSize` or `makerAmount`. The `or` chain tries each in
priority order. This is **defensive programming against API drift** — the code
works regardless of which field name the current API version uses.

**Why return `{"status": "NOT_FOUND"}` on 404?** A 404 on an order query usually
means the FAK order was fully filled and then archived (removed from the active
order API). This is actually a *good* sign — it means the order completed. The
caller (`confirm_fill_size`) uses this to infer a full fill.

### 12.3 `confirm_fill_size` — Multi-Step Fill Verification

```@/c:/Users/ntemu/Downloads/poly money maker/bot.py:551-575
def confirm_fill_size(result, oid, requested):
    """Best-effort number of shares an order actually filled.

    Prefers an explicit size_matched on the response, otherwise verifies via the
    order endpoint (a 404/NOT_FOUND means the FAK was archived after filling, so the
    requested chunk filled). Returns 0 when the fill cannot be confirmed, so callers
    never assume a full fill on an ambiguous response -- assuming a full fill would
    over-report sells and silently skip real exits.
    """
```

**Why is this function so important?** When you submit a FAK sell order, the
response doesn't always tell you how many shares actually filled. Sometimes
`size_matched` is present and accurate. Sometimes it's 0 even though the order
did fill. Sometimes the response is just an order ID with no fill information.

The function follows a **verification cascade**:

1. **Check `size_matched` on the response** — if present and > 0, use it.
2. **Query the order endpoint** — if the order is `NOT_FOUND` (archived after
   filling), assume the full requested amount filled.
3. **Return 0 if unconfirmed** — the docstring is explicit: "callers never
   assume a full fill on an ambiguous response." This is a **conservative
   default** — it's better to under-report a fill (and retry next cycle) than to
   over-report (and think we've sold shares we still hold).

### 12.4 `sell_market_with_retry` — The Execution Engine

```python
def sell_market_with_retry(token_id, size, price_limit, tick_size="0.01", max_retries=3, max_price=None):
    total_sold = 0.0
    remaining = float(size)
    if DRY_RUN:
        price = max(float(price_limit or tick_size), float(tick_size))
        console.print(f"  [bold black on yellow][DRY SELL][/] would SELL {remaining:.4f} {str(token_id)[:12]}… @ ≥{price:.3f}")
        log_event("dry_sell", token_id=token_id, size=remaining, price_limit=price)
        return 0, None
    for attempt in range(max_retries):
        if remaining < 0.01:
            break
        price = max(float(price_limit or tick_size), float(tick_size))
        if max_price is not None:
            fresh_bid, _ = get_book_bid(token_id)
            if fresh_bid is not None:
                if fresh_bid > max_price:
                    log_event("sell_skip_max_cap", token_id=token_id, attempt=attempt+1,
                              bid=fresh_bid, max_price=max_price, remaining=remaining)
                    console.print(f"  [dim yellow][SKIP][/] bid {fresh_bid:.3f} > cap {max_price:.3f} · attempt {attempt+1}/{max_retries}")
                    time.sleep(1)
                    continue
                price = max(fresh_bid, float(tick_size))
        try:
            result = safe_api_call(
                client.create_and_post_market_order,
                MarketOrderArgs(token_id=token_id, amount=remaining, side=SELL, price=price),
                options=PartialCreateOrderOptions(tick_size=tick_size, neg_risk=False),
                order_type=OrderType.FAK,
            )
            if result:
                oid = extract_order_id(result)
                filled = float(confirm_fill_size(result, oid, remaining))
                if filled <= 0:
                    console.print("  [dim yellow][FAK NULL][/] [dim]0 confirmed fill · stopping to avoid double-sell[/]")
                    break
                total_sold += filled
                remaining -= filled
                console.print(f"  [bold green][EXIT FAK][/]{filled} @ ≥{price:.3f}  [dim]id={str(oid)[:16]}...[/]")
                if remaining < 0.01:
                    return total_sold, result
        except Exception as e:
            console.print(f"  [dim red]Market sell {attempt+1}/{max_retries} failed: {e}[/]")
        time.sleep(1)

    if total_sold > 0:
        return total_sold, {"partial": True, "sold": total_sold}
    console.print(f"  [bold red][EXIT FAIL][/] market sell 0/{size:.4f} cleared")
    return 0, None
```

**The DRY_RUN guard:** If `DRY_RUN` is `True`, the function logs what it *would*
do and returns `(0, None)` — no order is sent. This lets you test strategy logic
without risking funds.

**The retry loop:** FAK orders can partially fill. If we asked to sell 100
shares but only 60 filled, we have 40 remaining. The loop retries up to
`max_retries` times, each time:

1. Check if we're done (`remaining < 0.01` — less than 1 cent worth).
2. **Max price cap check:** If `max_price` is set, re-fetch the live bid. If bid >
   `max_price`, skip this attempt (`sell_skip_max_cap`), wait 1s, and retry. This
   prevents selling a bouncing leg above the trigger threshold. After 3 skipped
   attempts, the sell fails and the bot holds to redemption.
3. Submit a FAK market order for the remaining amount at the current bid (or
   `price_limit` if no `max_price` cap).
4. Confirm the fill size using `confirm_fill_size`.
5. If 0 confirmed fills, **stop immediately** — the comment says "stopping to
   avoid double-sell." If the order filled but we can't confirm it, retrying
   could sell the same shares twice. Better to stop and let the ghost fill
   detection in the main loop sort it out.
6. Update totals and continue if shares remain.

**Why `time.sleep(0.5)` between attempts?** A small delay to avoid hammering the
API. Reduced from 1s to 0.5s to tighten the window between the max-price cap
check and order submission, minimizing the race condition where a bid bounces
above the cap between the check and the fill.

**Why `MarketOrderArgs` instead of `OrderArgs`?** The current codebase uses
`MarketOrderArgs` — a market order variant that accepts a `price` parameter as a
price limit. This is different from a pure market order (which would sell at any
price) and from a limit order (which would rest on the book). It's a
**marketable limit order** — sell at the best available price, but not below the
specified limit. Combined with `OrderType.FAK`, this gives us: "sell immediately
at or above this price, cancel whatever doesn't fill."

**The `price` calculation:**

When `max_price` is set and the fresh bid is below the cap, the sell price is
set to the fresh bid (ensuring we sell at the current market price, not a stale
trigger price):

```python
price = max(fresh_bid, float(tick_size))
```

When `max_price` is not set (e.g. hedge sells), the price falls back to the
`price_limit` argument:

```python
price = max(float(price_limit or tick_size), float(tick_size))
```

This ensures the price limit is at least the tick size (the minimum price
increment on Polymarket, typically $0.01). If `price_limit` is `None` or 0, we
fall back to the tick size — effectively "sell at any price above $0.01."

**Why `max_price` replaces the old bounce check:** The previous design had a
separate bounce-check step in the main loop that re-fetched the bid before
calling `sell_market_with_retry`. If the bid bounced above `SELL_THRESHOLD`, the
sell was cancelled. However, there was still a latency gap between the bounce
check and the actual on-chain FAK execution where the bid could recover. The
`max_price` cap moves the check *inside* the retry loop, so it's evaluated on
every attempt, not just once before the first attempt. This closes the latency
gap — if the bid bounces on attempt 2, it's caught. Hedge sells do not use
`max_price` (they have their own `HEDGE_THRESHOLD` logic).

---

## 13. Redemption — Settling Resolved Markets

After a market expires, the winning side is worth $1.00 per share and the losing
side is worth $0. To claim the $1.00, you need to **redeem** your complete set
(one UP + one DOWN token) through Polymarket's smart contract. This is an on-chain
operation on Polygon.

### 13.1 Refactored Relayer Helpers

The current codebase has refactored the relayer submission into two reusable
functions:

```@/c:/Users/ntemu/Downloads/poly money maker/bot.py:619-660
def get_relayer_headers():
    if not RELAYER_API_KEY or not RELAYER_API_KEY_ADDRESS:
        console.print("[bold red]▶ RELAYER [WARN][/] [dim]RELAYER_API_KEY / RELAYER_API_KEY_ADDRESS not set · redeem will fail[/]")
    relayer_headers = {
        "Content-Type": "application/json",
        "RELAYER_API_KEY": RELAYER_API_KEY or "",
        "RELAYER_API_KEY_ADDRESS": RELAYER_API_KEY_ADDRESS or "",
    }
    return RELAYER_URL, relayer_headers


def submit_proxy_tx(target, data, tx_type="PROXY"):
    from eth_account import Account

    relayer_url, relayer_headers = get_relayer_headers()
    eoa = Account.from_key(PRIVATE_KEY).address
    nonce_r = requests.get(
        f"{relayer_url}/nonce",
        params={"address": eoa, "type": tx_type},
        headers=relayer_headers,
        timeout=10,
    )
    if nonce_r.status_code != 200:
        return None, f"nonce fetch fail HTTP {nonce_r.status_code}"
    body = {
        "type": tx_type,
        "from": eoa,
        "to": target,
        "nonce": nonce_r.json().get("nonce", "0"),
        "data": "0x" + data.hex(),
        "value": "0",
    }
    submit_r = requests.post(
        f"{relayer_url}/submit",
        json=body,
        headers=relayer_headers,
        timeout=10,
    )
    if submit_r.status_code == 200:
        return submit_r.json().get("transactionID") or "?", None
    return None, f"HTTP {submit_r.status_code} · {submit_r.text[:80]}"
```

**Why the refactor?** The previous version had all the relayer logic inlined in
`redeem_condition`. By extracting `get_relayer_headers` and `submit_proxy_tx`,
the code becomes:

- **Reusable** — any future on-chain operation (not just redemption) can use
  `submit_proxy_tx` without duplicating the nonce/submit logic.
- **Testable** — the helpers can be tested independently.
- **Readable** — `redeem_condition` becomes focused on *what* to submit, not
  *how* to submit it.

**Why return `(tx_id, error)` tuples?** Instead of raising exceptions or
returning `None`, the function returns a tuple: `(transaction_id, None)` on
success, `(None, error_string)` on failure. This is a **Result-style pattern**
(like Rust's `Result<T, E>`) — the caller gets both the value and the error
context without try/except boilerplate.

### 13.2 `redeem_condition` — Building the Redemption Calldata

```@/c:/Users/ntemu/Downloads/poly money maker/bot.py:678-716
def redeem_condition(condition_id, label=""):
    """Submit a redemption tx for a resolved Polymarket conditionId via the Polygon relayer.
    Returns the transactionID string on success, or None on failure."""
    if DRY_RUN:
        console.print(f"  [bold black on yellow][DRY SETTLE][/] would redeem · {label}")
        log_event("dry_redeem", condition_id=condition_id, label=label)
        return None
    try:
        from eth_abi import encode
        from eth_utils import keccak, to_checksum_address

        pUSD = to_checksum_address(PUSD)
        CTF_CONTRACT = to_checksum_address(CTF)
        proxy = to_checksum_address(FUNDER_ADDRESS)

        redeem_sel = keccak(b"redeemPositions(address,bytes32,bytes32,uint256[])")[:4]
        redeem_data = redeem_sel + encode(
            ["address", "bytes32", "bytes32", "uint256[]"],
            [pUSD, bytes(32), bytes.fromhex(condition_id.lower().removeprefix("0x")), [1, 2]],
        )

        execute_sel = keccak(b"execute(address,uint256,bytes)")[:4]
        proxy_data = execute_sel + encode(
            ["address", "uint256", "bytes"],
            [CTF_CONTRACT, 0, redeem_data],
        )

        tx_id, err = submit_proxy_tx(proxy, proxy_data)
        if tx_id:
            console.print(f"  [bold bright_green][SETTLE ▶][/] {label}  [dim]tx={str(tx_id)[:18]}…[/]")
            return tx_id
        console.print(f"  [dim red][SETTLE FAIL][/] {label}  [dim]{err}[/]")
        if err and ("proxyWallet" in err or "invalid" in err.lower()):
            _redeem_permanent_failures.add(condition_id)
            console.print(f"  [dim red][SETTLE SKIP][/] {label}  [dim]permanent failure — will not retry[/]")
        return None
    except Exception as e:
        console.print(f"  [dim red][SETTLE ERR][/] {label}  [dim]{e}[/]")
        return None
```

**The DRY_RUN guard:** Same pattern as the sell function — logs the intent but
doesn't submit.

**Function selectors:** `keccak(b"redeemPositions(...)")[:4]` computes the
4-byte function selector that the EVM uses to identify which smart contract
function to call. The `b"..."` prefix creates a bytes literal — Keccak operates
on bytes, not strings.

**ABI encoding:** `eth_abi.encode` produces the binary encoding that the EVM
expects. The arguments to `redeemPositions` are:

- `pUSD` — the collateral token address.
- `bytes(32)` — a zero-filled 32-byte placeholder (required by the function
  signature but not used in this context).
- `condition_id` — the market's condition ID, converted from hex string to raw
  bytes.
- `[1, 2]` — the outcome indices to redeem (1 = UP, 2 = DOWN).

**The proxy wrapper:** The `execute` function on the proxy contract takes a
target address (CTF), a value (0 ETH), and calldata (the redeem call). This is a
**meta-transaction** — we're wrapping one contract call inside another, because
the EOA signs but the proxy contract executes.

**Permanent failure detection:** If the relayer returns an error containing
"proxyWallet" or "invalid", the condition is added to
`_redeem_permanent_failures`. This prevents retrying conditions that will never
succeed (e.g., the proxy wallet doesn't support the operation).

**Why a relayer instead of submitting directly to Polygon?** Submitting a
transaction directly to Polygon requires paying gas (MATIC). Polymarket provides
a **relayer** — a service that submits the transaction on your behalf and pays
the gas. This is subsidised by Polymarket to encourage market resolution.

---

## 14. The Main Loop — Putting It All Together

The main loop is the heart of the bot. It's a `while not _shutdown_requested`
loop with variable sleep (5s / 1s / 0.25s depending on time to expiry), executing a
complete cycle of: collect pre-fetched order books → discover positions (throttled)
→ display status → redeem resolved → sell losers → hedge
→ kick off next cycle's book fetch in background → sleep.

### 14.1 Loop Structure

```python
positions_meta = load_json(STATE_FILE)
CYCLE = 0
_last_positions_refresh = 0.0
_last_balance_refresh = 0.0
_cached_managed_sets = []
_book_executor = ThreadPoolExecutor(max_workers=16)
_pending_book_futs = {}  # {future: token_id} — books fetched during previous sleep

while not _shutdown_requested:
    try:
        CYCLE += 1
        now_ms = time.time() * 1000
        now_str = datetime.now().strftime("%H:%M:%S")

        # Throttled balance and position refreshes — not every sub-second cycle
        _now_f = time.time()
        if _now_f - _last_balance_refresh >= BALANCE_REFRESH_S:
            pusd_bal = get_balance()
            _last_balance_refresh = _now_f

        if _now_f - _last_positions_refresh >= POSITIONS_REFRESH_S:
            positions_raw = get_user_positions()
            _cached_managed_sets = group_btc_complete_sets(positions_raw or [], positions_meta)
            _last_positions_refresh = _now_f
        else:
            positions_raw = None  # skip GC when not refreshing positions
        managed_sets = _cached_managed_sets
```

**`positions_meta = load_json(STATE_FILE)`** — Load the persisted metadata cache
before entering the loop. This survives restarts.

**`CYCLE = 0`** — A counter for visual tracking (`TICK #0001`, `TICK #0002`, ...).

**`_book_executor`** — A persistent `ThreadPoolExecutor` (16 workers) used to
fetch order books in parallel during sleep. It lives outside the loop so threads
aren't created/destroyed each cycle.

**`_pending_book_futs`** — A dict of `{future: token_id}` for book fetches
submitted at the end of the previous cycle. By the time the next cycle starts,
these futures are already complete — collecting their results is instant.

**Throttled refreshes** — Balance and positions are not fetched every cycle.
At 0.25s polling, fetching positions every cycle would mean 240 API calls/min.
Instead, positions refresh every 2s (30 calls/min) and balance every 15s
(4 calls/min). Between refreshes, cached data is used. When a sell fills, the
cached set's `size` field is updated immediately so the next sub-second cycle
sees the correct remaining size without needing a position refresh.

**`positions_raw = None`** between refreshes — The GC phase checks
`positions_raw is not None` before cleaning up stale metadata, so GC only runs
when we have fresh data.

### 14.2 Garbage Collection

```@/c:/Users/ntemu/Downloads/poly money maker/bot.py:765-792
        if positions_raw is not None:
            live_conds = {s["conditionId"] for s in managed_sets}
            stale_conds = [c for c in list(positions_meta.keys()) if c not in live_conds]
            for c in stale_conds:
                gc_meta = positions_meta[c]
                # Record P&L for completed trades before deleting metadata
                if "pnl_entry_cost" in gc_meta:
                    entry_cost = gc_meta.get("pnl_entry_cost", 0)
                    sell_proceeds = gc_meta.get("pnl_sell_proceeds", 0)
                    hedge_proceeds = gc_meta.get("pnl_hedge_proceeds", 0)
                    redeem_value = gc_meta.get("pnl_redeem_value", 0)
                    if redeem_value == 0:
                        rem_up = gc_meta.get("expected_up_size", 0) or gc_meta.get("pnl_init_up_size", 0)
                        rem_dn = gc_meta.get("expected_dn_size", 0) or gc_meta.get("pnl_init_dn_size", 0)
                        if rem_up > 0 or rem_dn > 0:
                            redeem_value = round(max(rem_up, rem_dn), 4)
                    total_return = sell_proceeds + hedge_proceeds + redeem_value
                    outcome = "hedge" if hedge_proceeds > 0 else ("win" if redeem_value > 0 else "flat")
                    net = record_pnl(c, gc_meta.get("question", "?"), entry_cost, sell_proceeds + redeem_value, hedge_proceeds, outcome)
                    log_event("pnl_recorded", condition_id=c, entry=entry_cost, returned=round(total_return, 4), net=round(net, 4), outcome=outcome)
                del positions_meta[c]
            if stale_conds:
                log_event("gc", stale_conditions=stale_conds)
                save_json(STATE_FILE, positions_meta)
```

Removes metadata for conditions we no longer hold. Only runs when the data API
query succeeded (`positions_raw is not None`). Before deleting, it records a
P&L entry — if the position had tracked entry costs, the bot calculates the
final return (sell proceeds + hedge proceeds + redeem value) and calls
`record_pnl()` to append to `pnl.json`. If no explicit redeem value was recorded
(e.g., the protocol auto-redeemed), it estimates from remaining holdings using
`max(remaining_up, remaining_dn)`.

**Why `list(positions_meta.keys())`?** We're deleting from the dict while
iterating — converting to a list first avoids `RuntimeError: dictionary changed
size during iteration`.

### 14.3 The Positions Table

The bot uses `rich.Table` to render a colour-coded status table. Each row shows:

- **INSTRUMENT** — the market question (truncated to 40 chars).
- **EXPIRY** — the market end time (HH:MM format).
- **TTM** — Time To Maturity, displayed in seconds when < 1 minute, otherwise in
  minutes. Colour-coded:
  - Red if < `SELL_WINDOW_MIN` (45 seconds, in the exit window).
  - Yellow if < 60 minutes (approaching the window).
  - Green if > 60 minutes (safe).
- **UP / DN** — share counts for each leg.
- **STATE** — one of:
  - `✓ REDEEM` (magenta) — market resolved, ready for redemption.
  - `· closed` (dim) — market expired but not yet redeemable.
  - `○ EXIT ≤10¢ · LAST <35¢` (red) — in the final 10 seconds, the normal threshold remains active and the confirmed fallback is available.
  - `○ EXIT ≤10¢` (yellow) — in the final 45-second window, sell at any bid ≤ 10¢.
  - `● WATCHING` (green) — holding, outside the sell window.

### 14.4 The Redeem Phase

```@/c:/Users/ntemu/Downloads/poly money maker/bot.py:850-874
        for s in managed_sets:
            cond = s["conditionId"]
            redeemable = bool(s["up"].get("redeemable")) or bool(s["dn"].get("redeemable"))
            if not redeemable:
                continue
            if now_ms - s["end_ts"] > MAX_REDEEM_AGE_DAYS * 86400 * 1000:
                continue
            if cond in _redeem_permanent_failures:
                continue
            meta = positions_meta.setdefault(cond, {})
            last = meta.get("redeem_submitted_at") or 0
            if now_ms - last < REDEEM_THROTTLE_S * 1000:
                continue  # already submitted, wait 5 min before retry
            tx = redeem_condition(cond, label=(s["question"] or "?")[:32])
            if tx:
                meta["redeem_submitted_at"] = now_ms
                # P&L: only the winning side resolves at $1/share; the loser
                # resolves at $0.  min(up, dn) complete sets pay $1 each, and
                # the excess shares on one side also pay $1 (they are the winner).
                remaining_up = float(s["up"].get("size", 0))
                remaining_dn = float(s["dn"].get("size", 0))
                meta["pnl_redeem_value"] = round(max(remaining_up, remaining_dn), 4)
                log_event("redeem_submit", condition_id=cond, tx_id=str(tx))
                save_json(STATE_FILE, positions_meta)
```

Three guard conditions before attempting redemption:

1. **`MAX_REDEEM_AGE_DAYS`** — Don't redeem conditions older than 7 days past
   expiry. They may have been cleaned up on-chain.
2. **`_redeem_permanent_failures`** — Skip conditions that previously returned a
   permanent error.
3. **`REDEEM_THROTTLE_S`** — Don't retry within 5 minutes of the last submission.

After a successful redeem submission, the bot records `pnl_redeem_value` in the
metadata cache using `max(remaining_up, remaining_dn)` — the winning side's
share count, which resolves at $1.00 per share. This value is later used by the
GC phase to compute the final P&L for the trade.

### 14.5 The Sell Phase — Trigger Evaluation

For each managed set, the bot determines whether to sell the UP leg, the DOWN
leg, or neither.

**Step 1: Record entry time**

```@/c:/Users/ntemu/Downloads/poly money maker/bot.py:889-906
            if "entered_at" not in meta:
                meta["entered_at"] = now_ms
                meta["up_token"] = up_token
                meta["dn_token"] = dn_token
                meta["question"] = s["question"]
                meta["end_date"] = s["up"].get("endDate") or s["dn"].get("endDate")
                # P&L: entry cost from actual avgPrice (data-api), fallback $0.50
                init_up = float(s["up"].get("size", 0))
                init_dn = float(s["dn"].get("size", 0))
                up_avg = float(s["up"].get("avgPrice", 0) or 0) or 0.50
                dn_avg = float(s["dn"].get("avgPrice", 0) or 0) or 0.50
                meta["pnl_entry_cost"] = round(init_up * up_avg + init_dn * dn_avg, 4)
                meta["pnl_init_up_size"] = init_up
                meta["pnl_init_dn_size"] = init_dn
                meta["pnl_sell_proceeds"] = 0.0
                meta["pnl_hedge_proceeds"] = 0.0
                meta["pnl_redeem_value"] = 0.0
                save_json(STATE_FILE, positions_meta)
```

The first time we see a position, we record when we entered, cache the token
IDs and market metadata (for backfill), and initialise all P&L tracking fields.
Entry cost is calculated from the actual `avgPrice` field returned by the
data-api (typically ~$0.51-0.53 per share), with a $0.50 fallback if `avgPrice`
is missing. Total entry cost is `up_size × up_avgPrice + dn_size × dn_avgPrice`.
This is the backfill data that `group_btc_complete_sets` uses if the API later
loses track of the position.

**Step 2: Grace period**

```python
if now_ms - meta["entered_at"] < SELL_GRACE_S * 1000:
    continue
```

Wait 2 seconds after first discovery before selling. Prevents acting on
potentially stale data from the very first tick.

**Step 2b: Sell window check**

```python
# Only sell in the last SELL_WINDOW_MIN minutes to reduce reversal risk
if minutes_left > SELL_WINDOW_MIN:
    continue
```

If the market has more than 45 seconds left until expiry, skip the sell phase
entirely for this set. This is the **reversal risk mitigation** — selling the
loser leg early (e.g., with 2 minutes left) locks in 8 cents but leaves you
exposed to the market flipping. By waiting until the final 45 seconds, the
outcome is nearly settled and reversal is unlikely.

**Step 3: Read pre-fetched best bids and price both legs**

Order books are fetched in parallel *before* the sell loop starts, using a
background thread pool that runs during the previous cycle's sleep. The results
are collected into `_book_cache` at the top of the cycle (see §14.1). The sell
loop reads from this cache — no HTTP calls happen during sell evaluation.

```python
            up_bid, _ = _book_cache.get(up_token, (None, 0.0)) if up_token else (None, 0.0)
            dn_bid, _ = _book_cache.get(dn_token, (None, 0.0)) if dn_token else (None, 0.0)

            # Alert if book fetch failed for either leg during the sell window
            if up_bid is None and up_size > 0:
                log_event("sell_skip_no_book", condition_id=cond, leg="up", minutes_left=round(minutes_left, 1))
            if dn_bid is None and dn_size > 0:
                log_event("sell_skip_no_book", condition_id=cond, leg="dn", minutes_left=round(minutes_left, 1))
            if up_bid is None and dn_bid is None and (up_size > 0 or dn_size > 0):
                _book_fail_count = meta.get("_book_fail_count", 0) + 1
                meta["_book_fail_count"] = _book_fail_count
                if _book_fail_count == 15:  # ~3.75s at 0.25s polling in sell window
                    _ttm_str = f"{max(minutes_left*60, 0):.0f}s" if minutes_left < 1 else f"{round(minutes_left)}m"
                    notify("\u26a0 Book Unavailable", f"{s['question']} \u2014 order book unreachable with {_ttm_str} left", priority="high")
            else:
                meta["_book_fail_count"] = 0

            up_price, up_matched_price = quote_leg(up_bid)
            dn_price, dn_matched_price = quote_leg(dn_bid)
```

Only markets in the sell window (≤ `SELL_WINDOW_MIN`) have their books fetched.
Markets outside the window are skipped entirely — no wasted API calls. Books
for all in-window markets are fetched simultaneously via `ThreadPoolExecutor`,
taking ~150ms total instead of ~150ms × N sequentially. `quote_leg` converts the
raw bid into the pricing tuple used for decisions.

**Step 4: Trigger evaluation**

The sell trigger fires on **mid-price** (the average of best bid and best ask —
the same price shown on the Polymarket website). This prevents false triggers
from thin bids with wide spreads. The background book fetch uses
`get_book_quote` which returns `(bid, bid_size, ask, ask_size, mid)` for each
leg. If mid-price is unavailable (no asks on the book), the trigger falls back
to the best bid.

```python
# Trigger on mid-price (what the website shows) instead of just bid.
up_trigger_price = up_mid if up_mid is not None else up_price
dn_trigger_price = dn_mid if dn_mid is not None else dn_price

up_trigger = up_size > 0 and up_trigger_price is not None and up_trigger_price <= SELL_THRESHOLD
dn_trigger = dn_size > 0 and dn_trigger_price is not None and dn_trigger_price <= SELL_THRESHOLD

if seconds_left <= SELL_LASTCHANCE_S and not up_trigger and not dn_trigger:
    confirmation_price = 1.0 - SELL_LASTCHANCE_THRESHOLD
    up_candidate = up_price is not None and up_price < SELL_LASTCHANCE_THRESHOLD
    dn_candidate = dn_price is not None and dn_price < SELL_LASTCHANCE_THRESHOLD
    if up_candidate and not dn_candidate and dn_price is not None and dn_price >= confirmation_price:
        up_trigger = True
    elif dn_candidate and not up_candidate and up_price is not None and up_price >= confirmation_price:
        dn_trigger = True

# Ambiguous books do not identify a loser reliably.
if up_trigger and dn_trigger:
    log_event("sell_skip_ambiguous", reason="both_legs_triggered")
    up_trigger = False
    dn_trigger = False
```

**The trigger logic:**

- **Normal threshold (45→0 seconds):** `mid_price <= SELL_THRESHOLD` (mid ≤10¢).
  Falls back to best bid if mid is unavailable. There is no lower price floor.

- **Confirmed fallback (10→0 seconds):** When neither normal trigger fired, a
  side below 35¢ is eligible only if the opposite bid is at least 65¢.

- **All triggers are gated by the sell window** — nothing happens until
  `minutes_left <= SELL_WINDOW_MIN` (0.75 min = 45 seconds).

**Mutual exclusion:** If both legs satisfy a normal trigger, the book is treated
as ambiguous and neither leg is sold. The bot records `sell_skip_ambiguous`
instead of guessing the loser from the lower bid.

**Step 5: Cooldown check**

```@/c:/Users/ntemu/Downloads/poly money maker/bot.py:959-960
            will_sell_up = sell_up and (now_ms - (meta.get("last_sell_up_at") or 0) >= SELL_COOLDOWN_S * 1000)
            will_sell_dn = sell_dn and (now_ms - (meta.get("last_sell_dn_at") or 0) >= SELL_COOLDOWN_S * 1000)
```

Even if the trigger fires, we check a per-leg 3-second cooldown.

**Step 6: Execute the sell with ghost fill detection**

```@/c:/Users/ntemu/Downloads/poly money maker/bot.py:978-1001
                else:
                    log_event("sell_attempt", condition_id=cond, leg="up", size=up_size, bid=up_bid, price_limit=up_price)
                    sold, _ = sell_market_with_retry(up_token, up_size, up_matched_price or SELL_THRESHOLD)
                    if sold > 0:
                        meta["last_sell_up_at"] = now_ms
                        up_size -= sold
                        meta["expected_up_size"] = up_size
                        meta["pnl_sell_proceeds"] = round(meta.get("pnl_sell_proceeds", 0) + sold * (up_price or 0), 4)
                        log_event("sell_fill", condition_id=cond, leg="up", sold=sold, remaining=up_size, price=up_price)
                        save_json(STATE_FILE, positions_meta)
                    else:
                        time.sleep(1)
                        actual_bal = check_token_balance(up_token)
                        if actual_bal is not None and actual_bal < up_size - 0.01:
                            ghost_sold = up_size - actual_bal
                            meta["last_sell_up_at"] = now_ms
                            meta["pnl_sell_proceeds"] = round(meta.get("pnl_sell_proceeds", 0) + ghost_sold * (up_price or 0), 4)
                            up_size = actual_bal
                            meta["expected_up_size"] = actual_bal
                            log_event("sell_ghost_fill", condition_id=cond, leg="up", sold=ghost_sold, remaining=actual_bal, price=up_price)
                            console.print(f"  [bold yellow][GHOST FILL][/] UP sell confirmed via balance check: {ghost_sold:.4f} sold")
                            save_json(STATE_FILE, positions_meta)
                        else:
                            log_event("sell_fail", condition_id=cond, leg="up", size=up_size, bid=up_bid, price_limit=up_price)
```

**The ghost fill detection flow:**

1. Call `sell_market_with_retry` — it returns `(sold, result)`.
2. If `sold > 0` — normal fill. Update metadata, log it, save state.
3. If `sold == 0` — the sell returned no confirmed fills. But maybe it *did* fill
   and the SDK just didn't report it. Wait 2 seconds, then re-query the data API
   for the actual token balance.
4. If `actual_bal < up_size - 0.01` — the balance dropped! The sell went through
   but wasn't reported. This is a **ghost fill**. We log it as `sell_ghost_fill`,
   update metadata, and save state.
5. If the balance matches — the sell genuinely failed. Log `sell_fail`.

**Why is ghost fill detection necessary?** The Polymarket SDK and API have known
inconsistencies in reporting fill amounts. A FAK order might fill completely, but
the response might show `size_matched: 0`. Without ghost fill detection, the bot
would think the sell failed and retry — potentially selling shares it no longer
has, or at minimum wasting cycles. The balance check is the **ground truth** —
the data API reflects actual on-chain holdings.

### 14.6 The Sleep — Variable Polling with Overlapped Book Fetch

```python
    # Variable polling: 5s >2min, 1s ≤2min, sub-second in sell window (≤45s)
    _now = time.time() * 1000
    _min_ttm = min((s["end_ts"] - _now) / 60000 for s in managed_sets) if managed_sets else 999
    if _min_ttm <= SELL_WINDOW_MIN:  # ≤45s — sub-second polling in sell window
        _sleep_s = POLL_SELL_WINDOW_S
    elif _min_ttm <= 2:              # ≤2min — poll every 1s
        _sleep_s = 1
    else:                            # >2min — poll every 5s
        _sleep_s = 5

    # Kick off next cycle's book fetch in the background BEFORE sleeping.
    # The HTTP requests run concurrently with sleep, so by the time the next
    # cycle starts, the books are already fetched and waiting.
    _next_now = time.time() * 1000 + _sleep_s * 1000
    for s in managed_sets:
        _ml = (s["end_ts"] - _next_now) / 60000
        if _ml <= 0 or _ml > SELL_WINDOW_MIN:
            continue
        # ... submit get_book_quote for each held leg to _book_executor ...

    console.print(f"[dim bright_black]· · ·  sleeping {_sleep_s}s  · · ·[/]")
    time.sleep(_sleep_s)
```

The bot uses **variable polling** that adapts to time-to-expiry:

| Time remaining | Sleep | Rationale |
|---|---|---|
| > 2 min | 5s | Market outcome still uncertain; no need for tight polling |
| 2 min → 45s | 1s | Approaching sell window; tighten polling |
| ≤ 45s (sell window) | 0.25s | Inside sell window; maximum reaction speed needed |

This gives ~180 cycles at 0.25s during the 45-second sell window, ensuring the
bot can react to rapid price movements in 5-minute markets.

**Overlapped fetching** — The key innovation is that order book fetches for the
*next* cycle are submitted to the background thread pool *before* sleeping. The
HTTP requests run concurrently with the sleep, so the effective cycle time is
`max(sleep_s, fetch_time)` instead of `sleep_s + fetch_time`. With 12 concurrent
markets (24 book fetches), this reduces cycle time from ~400ms (250ms sleep +
150ms fetch) to ~250ms — a 38% improvement. The sleep duration is computed
using a fresh `time.time()` call (not the cycle-start `now_ms`, which is
several seconds old by this point).

### 14.7 Graceful Shutdown Exit

```@/c:/Users/ntemu/Downloads/poly money maker/bot.py:1134-1136
console.print("[bold bright_green]▶ SHUTDOWN COMPLETE[/] [dim]state saved · exiting cleanly[/]")
log_event("shutdown", reason="signal")
sys.exit(0)
```

When `_shutdown_requested` becomes `True`, the loop exits and these lines run.
The shutdown is logged for audit purposes, and `sys.exit(0)` ensures a clean
exit code (0 = success) — important if the bot is managed by systemd, which uses
exit codes to determine restart behaviour.

---

## 15. The Hedge Phase — Cutting Losses on Reversals

The hedge code runs after the sell phase for each managed set. Production runs
with the hedge **enabled** (`hedge_enabled: true`).

### 15.1 Purpose and Risk

The hedge protects against a genuine reversal after one leg has already been
sold. It checks whether the remaining leg's bid is at or below
`HEDGE_THRESHOLD`, then sells that leg as an emergency exit — BTC can reverse
in the final 90 seconds, and without the hedge the "winner" we kept can expire
worthless.

A best bid near expiry is not a reliable probability estimate. Wide spreads and
thin books can put both best bids below 50¢ even though one outcome will shortly
redeem for $1.00. Automatically selling the held leg in that state can liquidate
the eventual winner at a large discount — which is why the production threshold
is a deep 40¢ rather than anything near fair value.

### 15.2 Hedge Configuration

```python
"hedge_enabled": True,
"hedge_threshold": 0.40,
```

When disabled, the normal sell logic preserves the remaining leg for resolution
and redemption after the other leg has been fully sold.

The hedge runs only inside the final sell window, sends an urgent notification,
and uses balance reconciliation for ambiguous responses. The hedge sell's price
limit is **half the threshold** (`HEDGE_THRESHOLD * 0.5` = 20¢) rather than the
old fixed 1¢ floor — a 1¢ FAK would fill at any residual bid even in a
momentarily empty book, while 20¢ still accepts every realistically available
bid when the held leg is trading below 40¢.

---

## 16. State Management & Persistence

### 16.1 What We Persist (and What We Don't)

The bot's state file (`positions.json`) is a **metadata cache** — it doesn't
store the actual position data (sizes, redeemable flags). Those come fresh from
the data API every cycle. We only persist things that the API *can't* tell us:

| Field | Purpose |
|---|---|
| `entered_at` | When we first saw this set. Used for the 2-second grace period. |
| `up_token` / `dn_token` | Token IDs for backfill when the API loses track. |
| `question` | Market name for display when backfilling. |
| `end_date` | Market expiry for backfill. |
| `expected_up_size` / `expected_dn_size` | Our estimate of remaining shares after a partial or ghost sell. |
| `last_sell_up_at` / `last_sell_dn_at` | Timestamps for the 5-second sell cooldown. |
| `redeem_submitted_at` | Timestamp for the 30-second redeem throttle. |
| `pnl_entry_cost` | Total entry cost (`size × avgPrice` per leg). |
| `pnl_init_up_size` / `pnl_init_dn_size` | Initial share counts at first sighting. |
| `pnl_sell_proceeds` | Accumulated sell revenue (loser leg). |
| `pnl_hedge_proceeds` | Accumulated hedge revenue (if reversal occurred). |
| `pnl_redeem_value` | Estimated/actual redemption value for winner leg. |

**Why not persist sizes?** Because the API is the source of truth for sizes. If
we cached sizes and the cache got out of sync (e.g., we sold shares but the
cache wasn't updated), we'd try to sell shares we don't have. By fetching sizes
fresh every cycle, we always work with reality.

**Why persist `expected_up_size`?** This is a *fallback* for when the API fails
or lags. After a sell, the API might not reflect the new balance immediately.
We store our estimate so that if the API drops the position entirely on the next
cycle, we can backfill with a reasonable approximation via the orphan injection
in `group_btc_complete_sets`.

### 16.2 The State File Lifecycle

```
Startup → load_json(STATE_FILE) → positions_meta populated
   ↓
Cycle 1 → discover new set → save entered_at, token IDs
   ↓
Cycle N → sell UP leg → save last_sell_up_at, expected_up_size
   ↓
Cycle N+1 → API loses position → backfill from saved metadata
   ↓
Cycle M → market expires, redeemed → GC removes entry
```

The state file is written with `atomic_save` (write-to-temp-then-rename), so
it's never in a corrupted state — even if the bot is killed mid-write.

---

## 17. Error Handling Philosophy

The bot's error handling follows a **defensive, fail-safe** philosophy.

### 17.1 Never Crash the Loop

```@/c:/Users/ntemu/Downloads/poly money maker/bot.py:1098-1106
    except Exception:
        log_event("cycle_error", traceback=traceback.format_exc())
        console.print(Panel(
            traceback.format_exc(),
            title="[bold bright_red]■■  SYSTEM FAULT  ■■[/]",
            subtitle="[dim]auto-restart in 5s \u00b7 cycle aborted[/]",
            border_style="bright_red",
            box=box.HEAVY_EDGE,
        ))
```

The entire main loop body is wrapped in `try/except Exception`. If anything goes
wrong, the cycle is aborted, the error is logged with full traceback, and the
bot sleeps 5 seconds before trying again. This ensures the bot **never
permanently dies** from a transient error.

**Why `except Exception` and not bare `except`?** Bare `except` catches
`SystemExit` and `KeyboardInterrupt` too, which would prevent the graceful
shutdown signal handler from working. `except Exception` catches all "normal"
exceptions while letting system signals through.

**Why `traceback.format_exc()`?** Returns the traceback as a string (instead of
printing to stderr), so we can both log it and display it in a `rich` Panel.

### 17.2 Fail Safe, Not Fail Fast

Each function makes a deliberate choice about what to do on failure:

| Function | On Failure | Why |
|---|---|---|
| `get_balance` | Return `0.0` | Balance is display-only; not critical for sells. |
| `get_user_positions` | Return `None` | Distinguishes "no positions" from "query failed." |
| `check_token_balance` | Return `None` | Caller skips ghost fill check if lookup fails. |
| `get_book_bid` | Return `(None, 0.0)` | `None` price means "skip this leg this cycle." |
| `get_book_quote` | Return `(None, 0.0, None, 0.0, None)` | `None` mid means "skip this leg this cycle." |
| `sell_market_with_retry` | Return `(0, None)` | No shares sold; caller tries ghost fill detection. |
| `redeem_condition` | Return `None` | No redemption submitted; retry next cycle. |
| `notify` | Silent `pass` | Notifications are best-effort, never crash. |

The pattern: **non-critical failures return a safe default; critical failures
propagate to the loop-level catch.** Nothing in the sell, hedge, or redeem path
is allowed to crash the bot — at worst, a cycle is skipped.

### 17.3 The Complete Audit Log

Every significant action is logged to `bot.log` (with log rotation via
`RotatingFileHandler` — see §8.5):

| Event | When | Key Fields |
|---|---|---|
| `sell_attempt` | Before submitting a sell order | `condition_id`, `leg`, `size`, `bid`, `price_limit` |
| `sell_fill` | After a successful sell | `condition_id`, `leg`, `sold`, `remaining`, `price` |
| `sell_ghost_fill` | Sell confirmed via balance check | `condition_id`, `leg`, `sold`, `remaining`, `price` |
| `sell_fail` | After a failed sell (0 filled, no ghost) | `condition_id`, `leg`, `size`, `bid`, `price_limit` |
| `sell_skip_no_book` | Order book unavailable during sell window | `condition_id`, `leg`, `minutes_left` |
| `hedge_attempt` | Before submitting a hedge sell | `condition_id`, `leg`, `size`, `bid`, `price_limit` |
| `hedge_fill` | After a successful hedge sell | `condition_id`, `leg`, `sold`, `remaining`, `price` |
| `hedge_ghost_fill` | Hedge confirmed via balance check | `condition_id`, `leg`, `sold`, `remaining`, `price` |
| `hedge_fail` | After a failed hedge sell | `condition_id`, `leg`, `size`, `bid` |
| `book_fetch_fail` | Order book API threw an exception | `token_id`, `error` |
| `redeem_submit` | After submitting a redemption | `condition_id`, `tx_id` |
| `pnl_recorded` | P&L entry written for a completed trade | `condition_id`, `entry`, `returned`, `net`, `outcome` |
| `dry_sell` | DRY_RUN sell simulation | `token_id`, `size`, `price_limit` |
| `dry_redeem` | DRY_RUN redeem simulation | `condition_id`, `label` |
| `gc` | After garbage-collecting stale metadata | `stale_conditions` |
| `cycle_error` | When the cycle catches an exception | `traceback` |
| `shutdown` | On graceful shutdown | `reason` |

This log is the bot's **audit trail**. If something goes wrong, you can
reconstruct exactly what happened by parsing `bot.log`.

---

## 18. The Diagnostic Tool: `check_book.py`

```@/c:/Users/ntemu/Downloads/poly money maker/check_book.py:1-32
import requests, json, time
from datetime import datetime

now = time.time() * 1000
r = requests.get('https://gamma-api.polymarket.com/events?series_slug=btc-up-or-down-hourly&active=true&closed=false&limit=20')
events = r.json()
for ev in events:
    m = ev['markets'][0]
    end = m.get('endDate') or m.get('end_date')
    end_ts = datetime.fromisoformat(end.replace('Z', '+00:00')).timestamp() * 1000
    mins = (end_ts - now) / 60000
    if 120 < mins < 180:
        tokens = json.loads(m['clobTokenIds'])
        print(f"Market: {m['question'][:40]}... mins={mins:.0f}")
        for i, t in enumerate(tokens):
            book = requests.get(f'https://clob.polymarket.com/book?token_id={t}', timeout=10).json()
            price_buy = requests.get(f'https://clob.polymarket.com/price?token_id={t}&side=BUY', timeout=10).json()
            price_sell = requests.get(f'https://clob.polymarket.com/price?token_id={t}&side=SELL', timeout=10).json()
            asks = book.get('asks', [])
            bids = book.get('bids', [])
            best_ask = min(asks, key=lambda x: float(x['price'])) if asks else None
            best_bid = max(bids, key=lambda x: float(x['price'])) if bids else None
            print(f"  Token {i}:")
            print(f"    get_price BUY:  {price_buy}")
            print(f"    get_price SELL: {price_sell}")
            print(f"    Best ask: {best_ask}")
            print(f"    Best bid: {best_bid}")
        break
```

**What it does:** A standalone diagnostic script that finds a BTC hourly market
expiring in 2-3 hours and prints the full order book state for both tokens.

**Why does this exist?** Before deploying the bot, or when debugging unexpected
behaviour, you need to verify that the API is returning sensible data. This
script is a **manual inspection tool** — you run it, look at the output, and
confirm that bids, asks, and prices look reasonable.

**How it differs from the bot:**

- Uses the **Gamma API** (`gamma-api.polymarket.com`) instead of the data API.
  The Gamma API is Polymarket's public market discovery API — it lists all
  active markets with their metadata.
- Queries the **price endpoint** (`/price?side=BUY` and `side=SELL`) in addition
  to the order book. The bot doesn't use the price endpoint — it only needs the
  best bid from the book. But for diagnostics, seeing both helps you understand
  the spread.
- Looks for markets 120-180 minutes out. This is the "sweet spot" for manual
  entry — far enough out that you have time to buy, close enough that the market
  is active.

**Why `json.loads(m['clobTokenIds'])`?** The Gamma API returns `clobTokenIds` as
a JSON-encoded string (e.g., `'["12345...", "67890..."]'`), not as a list. We
need to parse it with `json.loads` to get the actual list of token IDs. This is
a quirk of the Gamma API — it double-encodes some fields.

**Why no error handling?** This is a diagnostic script, not production code. If
it fails, you see the exception and fix the issue. Adding try/except would hide
the very errors you're trying to diagnose.

**Why `min(asks, ...)` for best ask but `max(bids, ...)` for best bid?** The
best ask is the *lowest* price a seller will accept (you want to buy cheap). The
best bid is the *highest* price a buyer will pay (you want to sell high). This
asymmetry is fundamental to how order books work.

---

## 19. Live Shadow Simulator (`sim/`)

The live bot only manages **positions you actually hold**. That yields few samples
per day and is a poor way to gain statistical confidence in sell/fill behaviour.
The **live shadow simulator** paper-trades **every** active BTC 5-minute market
using **real public order books**, applying the same sell policy as the bot, and
recording path + fill + PnL data under `sim_data/`.

### 20.1 Goals

- Paper-trade **all** markets in one or more series (default: **BTC 15m**;
  multi-series supported via `series_slugs`).
- Use **live** CLOB books (`GET /book`) — same public data the bot sees.
- Apply configurable sell / last-chance / hedge rules (mirror bot policy shape).
- Model **FAK fills** by walking real bid depth (not a free fill at best bid).
- Run **permanently beside** `polybot` on GCP with **zero shared state**.
- Never place real orders; never load `.env` or trading credentials.
- Stay **disk-light** on small VMs (ticks off by default; host journal capped).

### 20.2 Isolation Guarantees

| Shadow uses | Live bot uses (untouched) |
|---|---|
| `sim/strategy.sim.json` | `strategy.json` |
| `sim_data/*` only | `positions.json`, `pnl.json`, `bot.log`, `.env` |
| Public Gamma + CLOB HTTP | Same public APIs **plus** authenticated trading |

Hard rules enforced in code:

- **No `import bot`** — policy is reimplemented as pure functions in `sim/policy.py`.
- **Path guard** (`assert_path_safe`) — refuses writes to bot filenames / outside `sim_data/` or `sim/`.
- **Single-instance lock** — `sim_data/shadow.lock` (fcntl / msvcrt).
- **No secrets** — no API keys, no order endpoints, no relayer.
- **systemd caps** — `Nice=10`, `CPUQuota=40%`, `MemoryMax=400M` so live trading wins CPU/RAM.
  Disk is separate: journal caps + `sim` prune (see §19.8).

### 20.3 Cycle Flow

Each cycle of `python -m sim.shadow`:

1. **Discover** markets for one or more series (`sim.series_slugs`, e.g.
   `btc-up-or-down-15m` + `btc-up-or-down-hourly`) via Gamma (cached ~25s).
2. **Paper enter** markets with minutes-to-start in `[enter_min_ttm_min, enter_max_ttm_min]`
   (default **0–60 min before market start**) at deterministic mint cost
   (`sim.set_cost`, default **1.0** for atomic pUSD split).
   Start time is parsed from the slug timestamp (e.g. `btc-updown-15m-1784638800`),
   not Gamma's `startDate` field (which reflects listing time, not window start).
   Optional live-book entry model exists behind `use_live_entry_books` (off by default).
3. **Fetch books** for open paper positions with TTM ≤ `book_horizon_min`
   (default **2.5 min** — only poll books near expiry, matching the wider sell window).
4. **Evaluate policy** (`sim/policy.py`): threshold / window / last-chance / hedge.
   Current config: **5¢ threshold**, **90-second sell window**,
   **10-second last-chance** at 35¢ (see §20.9).
5. **Latency friction** (`exec_latency_s`, default 2s): after a sell decision,
   sleep 2 seconds then **re-fetch the book** before simulating the fill.
   This models the CLOB order-submission round-trip — the bid may have moved.
6. **Simulate FAK** (`sim/fills.py`, model `depth`): walk bids at/above limit.
   **Queue priority friction** (`exec_queue_fraction`, default 0.7) reduces
   available bid sizes by 30% before the walk — we sit behind resting orders.
   Partial or zero fill → MISS (same failure class as thin books near expiry).
7. **Mint timing friction** (`exec_mint_delay_s`, default 4s): entry is delayed
   by 4 seconds to model the on-chain approve+split transaction. If the market
   starts during the delay (mts < 0), the entry is skipped with a `MINT MISS` log.
8. **After expiry** resolve winner from bid dominance;
   `PnL = sell_proceeds + hedge + redeem − entry_cost`.
9. **Persist** results under `sim_data/<data_tag>/`; prune on a timer.
   State is only written when the position set changes (dedup via signature).

Adaptive sleep: far markets ~5s, near ~1s, inside sell window ~0.5s.
Config is cached and only reloaded when `strategy.sim.json` mtime changes.

### 20.4 Package Layout

| Module | Role |
|---|---|
| `sim/shadow.py` | Main loop, lock, heartbeat, enter/close, status |
| `sim/policy.py` | Pure decision function (sell / confirm / last-chance / hedge) |
| `sim/fills.py` | FAK fill models against a book snapshot |
| `sim/discovery.py` | Gamma discovery + parallel `/book` fetch + cache |
| `sim/store.py` | State / ticks / trades / results + disk prune |
| `sim/config.py` | Paths, defaults, `load_strategy` / `load_sim` |
| `sim/analyze_history.py` | Calibrate `set_cost` from trade export CSV |
| `sim/strategy.sim.json` | Tunables (strategy + sim economics) |
| `sim/strategy.sim.50.json` | 50-share scaling test config (parallel instance) |
| `sim/strategy.sim.limit5c.json` | 5¢ limit sell experiment (failed, archived) |
| `sim/strategy.sim.hedge40.json` | Hedge at 40c test config |
| `sim/strategy.sim.hedge45.json` | Hedge at 45c test config |
| `deploy/polyshadow50.service` | systemd unit for 50-share sim |
| `deploy/polyshadow-limit5c.service` | systemd unit for 5¢ limit sim (stopped) |
| `deploy/polyshadow-hedge40.service` | systemd unit for hedge-40c sim |
| `deploy/polyshadow-hedge45.service` | systemd unit for hedge-45c sim |

### 20.5 Outputs (`sim_data/`, gitignored)

| Path | Content |
|---|---|
| `shadow_state.json` | Open paper positions |
| `results.jsonl` | One line per completed market (PnL, fills, misses, bid depth) |
| `ticks/<conditionId>.jsonl` | Sampled bid path |
| `trades/<conditionId>.json` | Full trade record + events |
| `shadow.log` | Rotating operator log |
| `shadow.lock` | Single-instance lock |
| `shadow.heartbeat` | Last successful cycle timestamp |

### 20.6 Operations (GCP)

```bash
cd ~/poly-money-maker && git pull

USER=$(whoami); DIR=$(pwd)
sed -e "s|YOUR_USER|$USER|g" -e "s|/home/YOUR_USER/poly-money-maker|$DIR|g" \
  deploy/polyshadow.service | sudo tee /etc/systemd/system/polyshadow.service

sudo systemctl daemon-reload
sudo systemctl enable --now polyshadow
systemctl status polyshadow --no-pager
journalctl -u polyshadow -n 50 --no-pager
python -m sim.shadow --summary
```

Manual / local:

```bash
python -m sim.shadow          # continuous
python -m sim.shadow --once   # smoke test
python -m sim.shadow --summary
```

Do **not** run a second `bot.py`. Shadow is paper-only.

### 20.9 Current shadow experiment (live-bot mirror, mint entry)

As of July 2026 the permanent `polyshadow` run mirrors the live `polybot` sell
strategy and the `polybuy` mint entry timing. Tunables live only in
`sim/strategy.sim.json` (live `strategy.json` is untouched).

| Knob | Value | Notes |
|---|---|---|
| Series | `btc-up-or-down-15m` | 15-minute BTC up/down only |
| Entry timing | **0–60 min before start** | Parsed from slug timestamp, not Gamma `startDate` |
| Set cost | **1.0** | Atomic pUSD mint (not CLOB buy) |
| Sell threshold | **5¢** | Sell losing leg when best bid ≤ 5¢ (avoids reversal zone) |
| Sell window | **90 seconds** | Last 90s before expiry (`sell_window_min: 1.5`) |
| Last-chance | **10s @ 25¢** | Final 10s, sell if leg < 25¢ AND opposite ≥ 75¢ |
| Opposite confirm | **off** (`sell_confirm_opposite: 0.0`) | No opposite-leg confirmation gate |
| Book horizon | **2.5 min** | Only poll books for positions near expiry |
| Latency friction | **2s** (`exec_latency_s`) | Re-fetch book after sell decision to model CLOB round-trip |
| Queue priority | **70%** (`exec_queue_fraction`) | Reduce available bid sizes — behind resting orders |
| Mint delay | **4s** (`exec_mint_delay_s`) | On-chain approve+split delay; skip if market starts during mint |
| Data tag | `sim_data/15m-mint-live-strategy/` | Fresh folder for this experiment |

#### Prior experiments (archived data)

| Tag | Description |
|---|---|
| `sim_data/15m1h-8c-conf/` | 8¢ anytime + 70¢ opposite confirm (15m + hourly) |
| `sim_data/15m1h-8c-any/` | 8¢ anytime without confirm (baseline) |
| `sim_data/15m/` | Earlier 12¢ / 2min windowed run |

The 8¢ anytime experiment (~50 resolved markets) showed ~90% win rate but
~3 wipeouts at −$4.8 each dominated mean EV (~−$0.15/market). A subsequent
10¢/45s experiment (81 markets) showed **+$0.086/market** with only 1 wipeout
(1.2% vs 6% prior), 70% win rate, median sell at 2¢. The single wipeout sold
at 8¢ — still in the reversal zone — prompting the current 5¢/90s experiment
which lowers the threshold to avoid selling while the outcome is still
uncertain. Three execution frictions (latency, queue priority, mint delay)
are now simulated to provide a pessimistic estimate of live performance.
If the edge survives friction, the real live gap is small.

#### Scaling test (50 shares)

A **parallel sim instance** (`polyshadow50.service`) runs at **50 shares/side**
against the same live books to measure fill quality at scale. Config lives in
`sim/strategy.sim.50.json` with `data_tag=15m-mint-live-strategy-50sh` to keep
results isolated. The FAK fill model walks real bid depth, so at 50 shares it
hits deeper levels and reveals real slippage during the sell window.

Early results (179 resolved markets): **$0.124/market** mean PnL, 71% win rate,
2.7¢ avg sell price, 7.8% trigger miss rate. Compared to 5-share ($0.020/market
over 206 markets), scaling is **~62% linear** — reversals at 50 shares cost 10×
more, dragging the ratio below the first batch's 95%. Win rate is stable (70-71%)
across both sizes. Reversal rate is ~1.5-1.7%, the primary EV drag.

Compare 5-share vs 50-share results:

```bash
echo "=== 5 shares ===" && .venv/bin/python -m sim.shadow --summary
echo "=== 50 shares ===" && .venv/bin/python -m sim.shadow --config sim/strategy.sim.50.json --summary
```

If PnL scales linearly (50-share PnL ≈ 10× 5-share PnL), the order book has
ample depth and the strategy is scalable. If 50-share fill prices degrade
significantly, depth is a binding constraint.

#### 5¢ limit sell experiment (failed — killed 2026-07-29)

A **third sim** (`polyshadow-limit5c.service`) tested a 5¢ FAK limit sell
(`sell_limit_price: 0.05`) with 0.1s polling and 15m+hourly markets.

**Result (176 resolved markets): negative EV.**

| Metric | Market sell (5sh) | 5¢ limit |
|---|---|---|
| Win rate | 70% | **24%** |
| Trigger miss | 8% | **46%** |
| Avg sell price | 2.6¢ | 6.6¢ |
| Mean PnL | +$0.020 | **-$0.026** |

**Why it failed:** At 5¢ the outcome is still uncertain — the bid bounces back
~46% of the time (trigger miss), and when it does fill, the leg reverses often
enough that win rate collapses to 24%. The higher sell price (6.6¢ vs 2.6¢)
does not compensate for the reversals and missed sells.

**Conclusion:** Selling at the current market bid (avg 2.6¢) is better than
holding for 5¢. The low sell price is a feature, not a bug — it confirms the
leg is truly dead. The `sell_limit_price` config option remains in the codebase
for future experiments but is set to 0 (off) by default.

Service stopped and disabled. Config archived as `sim/strategy.sim.limit5c.json`.

#### Hedge experiment (40c / 45c — started 2026-07-29)

The primary EV drag is **reversals** (~1.5% of markets): we sell the loser at
~2.7¢, then the "winner" collapses and expires worthless — a ~$48.65 loss at
50 shares that wipes out ~36 good markets.

The hedge logic (`policy.py:44-50`) sells the remaining winner if its bid drops
below `hedge_threshold` after the loser is already sold. This caps the reversal
loss at ~(1 - threshold) × shares instead of the full entry cost.

Two parallel sims test different thresholds:

| Sim | Threshold | Reversal loss (50sh) | False hedge risk |
|---|---|---|---|
| `polyshadow-hedge40` | 40¢ | -$30 (sell winner at 40c) | Lower — winner must drop below 40c |
| `polyshadow-hedge45` | 45¢ | -$27.50 (sell winner at 45c) | Slightly higher — catches more reversals but more false hedges |

**False hedge risk:** If the winner temporarily dips below the threshold then
recovers to $1, we sold it for $0.40-0.45 instead of redeeming for $1.00 —
a $0.55-0.60 unnecessary loss. The 90-second sell window limits this exposure
since markets resolve quickly.

Configs: `sim/strategy.sim.hedge40.json`, `sim/strategy.sim.hedge45.json`.
Data tags: `15m-hedge40`, `15m-hedge45`.

```bash
echo "=== hedge40 ===" && .venv/bin/python -m sim.shadow --config sim/strategy.sim.hedge40.json --summary
echo "=== hedge45 ===" && .venv/bin/python -m sim.shadow --config sim/strategy.sim.hedge45.json --summary
```

Compare against the no-hedge baseline (`polyshadow`, 5 shares). If mean PnL
increases from $0.020 to $0.05+ per market, the hedge is net positive.

#### Policy module

`sim/policy.py` `evaluate()` is the single decision function. Unit tests:
`sim/test_policy.py`.

### 20.7 What Shadow Does Not Prove

- Relayer / redemption latency beyond the simulated mint delay — the 4s
  `exec_mint_delay_s` models the approve+split round-trip but not rare
  congestion or nonce-stall scenarios.
- Adverse selection beyond the queue-priority model — real CLOB orders may
  face additional slippage from market makers pulling bids.
- Perfect isolation from rate limits if both processes hammer the public API
  (mitigated by cache + book horizon + lower sell poll rate).
- Host disk capacity — the app cannot enlarge a 10GB GCP boot disk; operators must
  cap `/var/log` and monitor free space (see §19.8).

### 20.8 Disk capacity incident and prevention (2026-07)

#### What happened

On the GCP instance (~10GB root disk), the root filesystem hit **100% full**
(`No space left on device` / ENOSPC). Symptoms:

- `polyshadow` could not write state/ticks and error-looped.
- `git pull` failed (`unable to create temporary file`).
- Paper results for some 15m markets closed as `winner: null` with full-entry
  losses until the disk was freed — those rows are **infra failures**, not
  strategy edge.

#### Root cause (measured)

| Path | Approx size | Role |
|---|---|---|
| `/var/log/syslog` | **4.0 GB** | **Primary consumer** — sim stdout via systemd → syslog |
| `/var/log/syslog.1` | **948 MB** | Rotated previous syslog |
| `/var/log/btmp` | 12 MB | Failed SSH attempts |
| `/var/log/journal/` | 21 MB | journald (already capped) |
| `sim_data/` | **~11 MB** | shadow outputs — **not** the culprit |

The journald cap (`deploy/journald-size.conf`) was correctly applied, but
**rsyslog** had no size limit. Systemd routes sim stdout/stderr to both journald
and syslog, so the same log lines were written twice — journald stayed small
but syslog grew unbounded to ~5 GB over the weekend.

#### Fix applied (2026-07-27)

1. **Disk resized** from 10 GB → 20 GB via GCP console (stop/start, auto-expands).
2. **Truncated** `/var/log/syslog`, `syslog.1`, `btmp` to reclaim 5 GB.
3. **rsyslog size cap** — `/etc/rsyslog.d/99-size-limit.conf`:
   ```text
   $MaxFileSize 50M
   $MaxFiles 2
   ```
   Caps syslog at 100 MB total (2 × 50 MB rotated files).
4. **journald cap confirmed** — `/etc/systemd/journald.conf.d/99-size.conf`:
   ```ini
   [Journal]
   SystemMaxUse=100M
   MaxFileSec=3day
   ```

Combined log ceiling: ~200 MB (100 MB syslog + 100 MB journal). Cannot fill a
20 GB disk.

#### App-side prevention (in repo)

| Control | Where | Effect |
|---|---|---|
| `record_ticks: false` default | `sim/strategy.sim.json` | No dense bid-path files |
| Prune ticks 6h / trades 7d | `sim/store.py` | Age-based cleanup |
| `max_sim_data_mb` (~150) | config + prune | Hard size budget under `sim_data/` |
| `min_free_disk_mb` (~200) | config + prune | Emergency tick wipe if free space low |
| ENOSPC handling | `store.py` / `shadow.py` | Disable optional writes; no traceback spam |
| Unresolved PnL = 0 in summary | `finalize` + `summarize_results` | Disk outages do not fake full-entry strategy losses |
| Small rotating `shadow.log` | `shadow.py` | 1MB × 2 backups |

#### Host-side prevention (required on GCP)

| Control | Where | Effect |
|---|---|---|
| Journal size cap | `deploy/journald-size.conf` | Keep journal ≤ ~50MB |
| Ops runbook | `deploy/DISK_OPS.md` | Find `/var/log`, vacuum, recover |
| Prefer 20–30GB boot disk | GCP console | Headroom for OS + journal + bot |

Install journal cap once:

```bash
sudo mkdir -p /etc/systemd/journald.conf.d
sudo cp deploy/journald-size.conf /etc/systemd/journald.conf.d/size.conf
sudo systemctl restart systemd-journald
sudo journalctl --vacuum-size=50M
```

#### Operator check

```bash
df -h /
# Avail should stay well above 1G on a 10G disk; alert if < 500M
```

### 20.9 Memory and resource management (2026-07-31)

#### Problem

Running 5+ shadow sim services each with `MemoryMax=400M` exceeded the small
GCP VM's capacity. Combined footprint: 5 × 400M = 2.0 GB for sims alone, plus
the main bot, `polybuy`, GCP Ops Agent, journald, and the OS. This caused
VM-wide OOM kills.

Two code-level issues compounded the problem:

1. **Unbounded `ThreadPoolExecutor` queue in `bot.py`** — during the sell window,
   the bot submits order-book fetches every 250ms. Slow or stalled Polymarket
   responses caused queued work to accumulate without bound, growing memory.

2. **`summarize_results()` loaded entire `results.jsonl` into memory** — every
   sim called this during shutdown/restart, causing a memory spike under the
   service limit.

#### Fixes applied

**Code fixes:**

| Fix | File | Change |
|---|---|---|
| Bound book fetch queue | `bot.py` | `_MAX_PENDING_BOOKS=20`, skip duplicate tokens, cancel stale futures |
| Stream summary | `sim/store.py` | `summarize_results()` now streams line-by-line instead of loading full list |

**Systemd resource controls:**

| Control | Where | Effect |
|---|---|---|
| Shared slice | `deploy/polyshadow-slice.service` | `MemoryMax=800M` total for ALL sims combined |
| Per-service MemoryMax | all sim `.service` files | Reduced from 400M → 200M each |
| Per-service CPUQuota | all sim `.service` files | Reduced from 30-40% → 20% each |
| Restart rate limit | slice | `StartLimitBurst=10` per 300s — prevents crash loops |

**Memory budget on 2GB VM:**

| Component | Budget |
|---|---|
| OS + GCP agents | ~600M |
| `polybot` | ~200M |
| `polybuy` | ~200M |
| All sims (shared slice) | 800M |
| **Total** | **~1.8 GB** |

Install the slice:

```bash
sudo cp deploy/polyshadow-slice.service /etc/systemd/system/
sudo systemctl daemon-reload
# Restart all sim services to pick up slice + new MemoryMax
sudo systemctl restart polyshadow polyshadow50 polyshadow-hedge40 polyshadow-hedge45 polyshadow-tickstudy
```

#### Operator check

```bash
# VM-wide memory
free -h

# Per-service memory usage
systemd-cgtop --order=memory

# Check for OOM kills
sudo journalctl -k --since "24 hours ago" | grep -Ei 'oom|killed process'
sudo journalctl --since "24 hours ago" | grep -Ei 'memorymax|memory cgroup'
```

---

## 20. Atomic Mint Buyer (`buy/`) — UNUSED / LEGACY

> **Status (2026-08-10):** Atomic mint is **not in active use**. Live entry is
> the standalone buy bots (§21). This section documents the `buy/` package that
> remains in the repo (and may still have VM units/data) for recovery or a
> possible future re-enable — not current production procedure.

The `polybuy` process was designed to replace uncertain two-leg CLOB entry with
one atomic Conditional Token Framework split. It deposits pUSD and receives equal
UP and DOWN inventory for a standard binary BTC market. When it was live, a cron
job re-armed it at minutes 56, 11, 26, and 41 of each hour (4 min before each BTC
15-minute market start), yielding up to 4 mints per hour. Each arm permitted
exactly one mint attempt (§20.4).

```text
50.000000 pUSD
      |
      | approve adapter for exactly 50.000000 pUSD
      | splitPosition(pUSD, zero parent, conditionId, [1, 2], 50_000_000)
      v
50.000000 UP + 50.000000 DOWN
```

The approval and split are submitted as one relayer batch. If either call
reverts, the whole Polygon transaction reverts. This eliminates CLOB spread,
BUY taker fees, unequal fills, and cross-leg race risk. The resulting complete
set has deterministic collateral cost `$1.00 × shares`.

### 20.1 Process ownership and isolation

| Process | May do | Must never do | Writable state |
|---|---|---|---|
| `polybot` / `bot.py` | SELL, optional hedge, redeem | BUY or mint | existing bot files |
| `polyshadow` / `sim/` | public-data paper simulation | load secrets or transact | `sim_data/` |
| `polybuy` / `buy/` | atomic pUSD split only | SELL, hedge, redeem, edit bot/sim state | `buy_data/` |

The buyer never imports `bot.py` or `sim/`. Polymarket wallet holdings are the
only integration boundary: after a successful split, the Data API exposes the
UP/DOWN positions and the unchanged sell bot discovers them on its normal
refresh. There is no shared writable JSON protocol and no direct process call.

The buyer also has separate:

- Configuration: `strategy.buy.json` (gitignored).
- State: `buy_data/state.json`.
- Lock: `buy_data/polybuy.lock`.
- Heartbeat: `buy_data/heartbeat.json`.
- Rotating log: `buy_data/polybuy.log` (1 MB × three files).
- Kill switch: `buy_data/STOP`.
- Arm file: `buy_data/ARM`, refreshed by cron at minutes 56/11/26/41 (4×/hour,
  one per BTC 15-minute market); consumed one-shot on the first live cycle that
  sees it.

### 20.2 Package layout

| Module | Responsibility |
|---|---|
| `buy/config.py` | Fail-closed defaults, independent config parsing, paths, validation |
| `buy/market.py` | Gamma discovery, exact UP/DOWN mapping, Data API balances |
| `buy/chain.py` | Read-only Polygon RPC checks for pUSD, outcome slots, contracts, and ERC-1155 balances |
| `buy/contracts.py` | Pure ABI encoding for exact approval and standard-adapter `splitPosition` |
| `buy/relayer.py` | SDK-equivalent PROXY encoding, EIP-191 signing, and HTTP submission (§20.9); transaction-status lookup |
| `buy/store.py` | Durable atomic state writes, free-disk check, heartbeat, bounded history, process lock |
| `buy/runner.py` | Eligibility, caps, durable intent, submit/reconcile loop, CLI, notifications |
| `buy/test_buy.py` | Synthetic calldata and fail-closed lifecycle tests |

### 20.3 Cycle flow

Each `polybuy` cycle performs these steps in order:

1. Return immediately unless `buy.enabled` is true. `--plan` is the safe CLI
   override: it forces dry-run for one cycle but cannot enable submission.
2. Refuse entry if `buy_data/STOP` exists or free disk is below the configured
   floor (500 MB by default).
3. Reconcile existing relayer intents and on-chain inventory before discovery.
   An intent that was persisted as `submitting` but has no transaction ID is
   changed to `ambiguous`; all new entry freezes unless both on-chain balances
   conclusively prove that the requested mint occurred.
4. Discover configured Gamma series and keep only active, open, non-neg-risk
   markets whose **minutes to start** falls within `[enter_min_ttm_min,
   enter_max_ttm_min]` (default **0–60 min before market start**). Start time is
   parsed from the slug timestamp (e.g. `btc-updown-15m-1784638800`), not Gamma's
   `startDate` field. The `acceptingOrders` check is dropped because minting is
   on-chain and pre-start markets are not yet accepting CLOB orders.
5. Fetch current Data API holdings and apply one-entry, open-set, open-notional,
   daily-notional, and deterministic set-cost caps.
6. In dry-run, persist a bounded plan record only. No private key, relayer
   client, approval, or split call is used.
7. In live mode, require and consume a fresh arm **before any network or
   preflight work**, then require `PRIVATE_KEY` and `FUNDER_ADDRESS` and verify:
   the relayer's expected funder equals `FUNDER_ADDRESS`, pUSD and adapter
   contracts exist on-chain, `getOutcomeSlotCount(conditionId) == 2`, sufficient
   pUSD exists, and both on-chain token balances are still zero. An insufficient
   balance returns a clean `blocked/insufficient_balance` status rather than
   raising, so a temporary shortfall is a quiet skip, not a crash.
8. Persist a `submitting` intent with pre-mint balances **before** calling the
   relayer.
9. Submit exact approval + split as one PROXY batch and persist the returned
   transaction ID as `pending`. A failed preflight requires deliberate re-arming.
10. Poll relayer state across later cycles. `STATE_CONFIRMED` still waits for
    both on-chain balances to increase by the requested share amount before the
    intent becomes `confirmed`.

No automatic retry occurs after an ambiguous submit, failed transaction, wrong
wallet, RPC inconsistency, or inventory mismatch. This intentionally trades
availability for duplicate-mint safety.

### 20.4 Safety gates

Real mint submission requires all of the following simultaneously:

- Runtime config exists and sets `enabled: true`.
- Runtime config sets `dry_run: false`.
- `buy_data/STOP` does not exist.
- `buy_data/ARM` contains exactly `MINT_REAL_PUSD` and is younger than
  `arm_max_age_s` (1 hour in production).
- `PRIVATE_KEY` and `FUNDER_ADDRESS` are present. (Builder credentials are no
  longer used; the relayer authenticates with `RELAYER_API_KEY` /
  `RELAYER_API_KEY_ADDRESS` headers, §20.9.)
- Disk, contract, condition, pUSD balance, position,
  one-entry, open-set, open-notional, and daily-notional checks all pass.

**Autonomous arming.** Production replaces manual arming with a cron job:

```cron
*/55 * * * * echo "MINT_REAL_PUSD" > ~/poly-money-maker/buy_data/ARM
```

The arm is one-shot: it is deleted (`consume_arm`) at the start of the first
live cycle that sees it, before discovery, credential, wallet, RPC, or balance
checks — so one arm can never produce two mints, even across a process restart.
A failed preflight, a capped portfolio, or a lack of candidates consumes the
arm and waits for the next cron tick. This creates ~55-minute minimum spacing
between mint attempts (~26 attempts/day max), which in turn bounds spend to
~$520/day at 20 shares even though `max_daily_notional` is set high.

Only standard binary CTF markets are supported. Any market with `negRisk=true`
is rejected rather than routed to a different adapter. CLOB BUY fallback is not
implemented, so `polybuy` can never create one-leg inventory.

### 20.5 Durable intent states

| State | Meaning | New entry allowed? |
|---|---|---|
| `submitting` | Durable intent exists; submit call is in progress | No |
| `ambiguous` | Process cannot prove whether submit occurred | No; only conclusive complete on-chain inventory self-recovers |
| `pending` / `executed` / `mined` | Relayer transaction is progressing | No — counts toward the 3-open-set cap |
| `confirmed_waiting_inventory` | Relayer confirmed; balances not indexed/observed yet | No |
| `confirmed` | Both on-chain outcome balances increased as expected | Subject to portfolio caps |
| `failed` / `invalid` | Relayer reported terminal failure | Condition remains recorded; no blind retry |
| `completed` | Market ended and observed inventory is gone | Yes, subject to other caps |

`state.json` is written through a flushed temporary file followed by
`os.replace`. A failed state write aborts before submission. State history is
bounded, but active/ambiguous intents are never pruned merely to meet the cap.

### 20.6 Configuration

Copy `strategy.buy.example.json` to the gitignored `strategy.buy.json`. The
production values:

| Key | Production | Effect |
|---|---:|---|
| `enabled` | `true` | Buyer loop is active |
| `dry_run` | `false` | Live minting; relayer submission reachable |
| `entry_method` | `mint` | CLOB BUY is unsupported |
| `series_slugs` | `btc-up-or-down-15m` | Independent target-series allowlist |
| `shares` | `20.0` | pUSD deposited and shares minted per market ($20/set) |
| `enter_min_ttm_min` / `enter_max_ttm_min` | `0` / `60` | Minutes before market start; negative values (already started) are excluded |
| `max_set_cost` | `1.0` | Deterministic mint-cost gate; values below 1 are rejected as invalid config |
| `max_open_sets` | `3` | Maximum concurrent active conditions (owned on-chain + active intents) |
| `max_open_notional` | `60.0` | Maximum concurrent pUSD-equivalent exposure; AND-ed with `max_open_sets`, whichever binds first |
| `max_daily_notional` | `999999.0` | UTC-day submitted mint cap — effectively disabled; real pacing comes from the ~55-min arm cadence |
| `one_entry_per_market` | `true` | Any existing intent prevents another condition-level mint (condition_ids are unique per market instance, so old intents never block future markets) |
| `poll_s` | `15` | Low API/CPU duty cycle beside `polybot` |
| `min_free_disk_mb` | `500` | Entry fails closed before the historical ENOSPC range |
| `arm_max_age_s` | `3600` | One-shot arm validity; cron re-arms every 55 min so an arm never expires unused |
| `require_funder_match` | `true` | Relayer's expected funder must equal configured funder; `false` is rejected as invalid config |

Contract addresses are configurable for explicit upgrades, but defaults use the
current Polygon pUSD, CTF, and standard pUSD collateral-adapter addresses. They
must be rechecked against official Polymarket documentation before changing or
enabling live mode.

### 20.7 Commands and rollout

Install buyer-only dependencies in an independent environment so the existing
bot environment is not upgraded:

```bash
python3 -m venv .venv-buy
.venv-buy/bin/pip install -r requirements.buy.txt
cp strategy.buy.example.json strategy.buy.json
```

Safe local/GCP checks:

```bash
.venv-buy/bin/python -m buy --once
.venv-buy/bin/python -m buy --plan
.venv-buy/bin/python -m buy --status
.venv-buy/bin/python -m unittest buy.test_buy -v
```

`--once` obeys the disabled config. `--plan` forces one public-data dry plan and
cannot submit. Before considering live mode, run dry plans long enough to verify
series selection, start-time filtering, duplicate suppression, caps, Data API
holdings, RPC health, logs, heartbeat, disk use, and coexistence with both
current services.

The `deploy/polybuy*.service` templates run with `Nice=15`,
`CPUQuota=20%`, and `MemoryMax=200M`, below `polyshadow`, which is already below
`polybot`. They are intentionally excluded from the existing GitHub auto-deploy
workflow, so adding or changing buyer code cannot restart or redeploy the sell
bot.

Production operation is autonomous: `strategy.buy.json` sets `enabled=true` and
`dry_run=false`, and a user crontab re-arms every 55 minutes:

```cron
*/55 * * * * echo "MINT_REAL_PUSD" > ~/poly-money-maker/buy_data/ARM
```

The cadence itself is the capital control: at most one mint per arm, ~26 arms
per day, ~$520/day maximum spend at `shares=20`. To pause minting, create
`buy_data/STOP` (disables entry even if every other live gate is satisfied) or
remove the cron entry. To tighten spend, lower `shares` or slow the cron.

The guard logic was verified by audit (2026-08): `_portfolio_usage()` counts
both on-chain positions and active intents (covers Data API indexing lag);
the balance check reads the proxy wallet's pUSD via `balanceOf(funder)`, not
the EOA; `max_open_sets` and `max_open_notional` are AND-ed; the arm is consumed
atomically before any mint attempt; `eligible_markets()` excludes already-
started markets; and `one_entry_per_market` keys on condition_id, which is
unique per market instance.

### 20.8 What did not change

- No line in `bot.py`, `strategy.json`, sell execution, hedging, redemption,
  position discovery, or bot state was changed for minting.
- No line in `sim/` isolation, sell policy, disk pruning, `record_ticks`, or
  `sim_data/` ownership was weakened.
- `requirements.txt` and `.github/workflows/deploy.yml` remain unchanged;
  buyer dependencies and service installation are separate and manual.
- `polybuy` does not place CLOB orders, sell either leg, merge positions,
  redeem, or alter the seller's cooldown/position metadata.
- No runtime `strategy.buy.json`, arm file, or private key is committed to the
  repository.

### 20.9 The relayer PROXY transaction flow

`buy/relayer.py` was rewritten to bypass the `py_builder_relayer_client` SDK
(which requires builder credentials we don't hold) and instead replicate its
PROXY transaction flow directly over HTTP, using the same
`RELAYER_API_KEY` / `RELAYER_API_KEY_ADDRESS` header authentication as the sell
bot's redemption path. The implementation was audited line-by-line against the
official SDK source (`Polymarket/py-builder-relayer-client`).

**1. Batch encoding** — all calls for a mint (approve + splitPosition) are
encoded into a single `proxy((uint8,address,uint256,bytes)[])` call:

- Selector: `keccak(b"proxy((uint8,address,uint256,bytes)[])")[:4]`
- Each inner call is a tuple `(type_code, to, value, data)` with
  `type_code = 1` (`CallType.Call`; `0` is `Invalid`, `2` is `DelegateCall`)
- ABI-encoded with `eth_abi.encode(["(uint8,address,uint256,bytes)[]"], [tuples])`

**2. Relay payload** — `GET /relay-payload?address={eoa}&type=PROXY` returns the
`nonce` and the `relay` address. (The `/nonce` endpoint is for SAFE transactions
and does not return the relay address — using it was an early bug.)

**3. Struct hash and signature** — the message is a raw byte concatenation,
keccak-hashed, then EIP-191 signed (`encode_defunct` + `Account.sign_message`):

```
message = b"rlx:"
        + from(20)          # EOA
        + to(20)            # PROXY_FACTORY
        + data(variable)    # the batch calldata above, raw bytes
        + txFee(32)         # "0", big-endian
        + gasPrice(32)      # "0"
        + gasLimit(32)      # "500000" (DEFAULT_GAS_LIMIT; fits the hub's ~650k budget)
        + nonce(32)         # from the relay payload
        + relayHub(20)      # RELAY_HUB
        + relay(20)         # from the relay payload
struct_hash = keccak256(message)
```

**4. Submission** — `POST /submit` with exactly the SDK's `TransactionRequest`
fields:

```json
{
  "type": "PROXY",
  "from": "<eoa>",
  "to": "<PROXY_FACTORY>",
  "proxyWallet": "<CREATE2-derived proxy>",
  "data": "0x<batch calldata>",
  "nonce": "<nonce>",
  "signature": "0x<eip191 signature>",
  "signatureParams": {
    "gasPrice": "0", "gasLimit": "500000", "relayerFee": "0",
    "relayHub": "<RELAY_HUB>", "relay": "<relay>"
  },
  "metadata": "polybuy:mint:<condition_id>:<ts>"
}
```

**5. Proxy wallet derivation** — `proxyWallet` is the CREATE2 address the relayer
re-derives independently; a mismatch is rejected:

```
salt         = keccak256(encode_packed(["address"], [eoa]))
proxy_wallet = keccak256(0xff + PROXY_FACTORY + salt + PROXY_INIT_CODE_HASH)[-20:]
```

This must equal `FUNDER_ADDRESS` — the proxy wallet that actually holds the pUSD
and receives the minted UP/DOWN tokens.

**Contract constants (Polygon, chain 137):**

| Constant | Value |
|---|---|
| `PROXY_FACTORY` | `0xaB45c5A4B0c941a2F231C04C3f49182e1A254052` |
| `RELAY_HUB` | `0xD216153c06E857cD7f72665E0aF1d7D82172F494` |
| `PROXY_INIT_CODE_HASH` | `0xd21df8dc65880a8606f09fe0ce3df9b8869287ab0b058be05aa9e8af6330a00b` |
| `DEFAULT_GAS_LIMIT` | `500000` |

Two implementation gotchas, both learned the hard way:

- `HexBytes` must be imported from the **`hexbytes`** package, not `eth_utils`
  (older `eth_utils` versions, like the one in `.venv-buy`, don't re-export it).
- The first live mint confirmed end-to-end on 2026-08-03: relayer state
  `STATE_CONFIRMED`, `derivedMetadata` showing `ERC20 Approve` + `CTF Split`,
  20 sets for $20.00 pUSD.

---

## 21. Standalone Buy-Side Bots (`buybot.py` / `buybot5m.py` / `buybothourly.py`)

The standalone buy-side bots are a separate family from both the sell-side bots
(§1) and the atomic mint buyer (§20). They buy the **winning leg** of BTC
prediction markets at 96–99¢ and hold to expiry.

### 21.1 Strategy Overview

Unlike the sell-side bots (which sell the loser leg from a complete set) and the
atomic mint buyer (which mints complete sets at $1.00), the standalone buy bots:

1. **Discover markets** via Gamma API using the same series slugs as the sell bots
2. **Monitor order books** at 1s polling (0.1s inside the buy window)
3. **Detect the winning leg** by comparing mid prices (bid+ask)/2
4. **Buy the winning leg** via FAK market order when its ask is in 96–99¢
5. **Hold to expiry** — no profit-taking sells
6. **Hedge at 65¢** if the held leg's bid collapses (reversal protection)
7. **Redeem** winning positions after market resolution

### 21.2 Strategy Parameters

| Parameter | `buybot.py` (15m) | `buybot5m.py` (5m) | `buybothourly.py` (hourly) |
|---|---|---|---|
| Buy band | 96–99¢ ask | 96–99¢ ask | 96–99¢ ask |
| Buy window | last 3 minutes (180s) | last 90 seconds | last 5 minutes (300s) |
| Buy budget (USD) | $21 | $8 | $24 |
| Normal polling | 1s | 1s | 1s |
| Buy-window polling | 0.1s | 0.1s | 0.1s |
| Hedge threshold | 65¢ bid | 65¢ bid | 65¢ bid |
| Tick size | 0.01 | 0.001 | 0.01 |
| Series slug | `btc-up-or-down-15m` | `btc-up-or-down-5m` | `btc-up-or-down-hourly` |
| Slug prefix | `btc-updown` | `btc-updown-5m` | `bitcoin-up-or-down` |
| Slug excludes | `btc-updown-5m`, `bitcoin-up-or-down` | `bitcoin-up-or-down` | `btc-updown-5m`, `btc-updown` |

### 21.3 Winner Detection Logic

Winner detection uses the **same display price Polymarket shows in the UI**
([prices & orderbook docs](https://docs.polymarket.com/concepts/prices-orderbook)):

```
if best_bid and best_ask and (ask - bid) ≤ $0.10:
    gui_price = (bid + ask) / 2      # midpoint
else:
    gui_price = last_trade_price     # CLOB /last-trade-price
```

That is what a human sees as "97%" on the site. A wide book on the loser
(bid 5¢ / ask 97¢) does **not** display as ~51% — the UI switches to last
trade (often a few cents). The bot now follows that rule via
`polymarket_display_price()` + `get_last_trade_price()`.

```
if either gui_price is None → skip (buy_skip_incomplete_book)
if abs(up_gui - dn_gui) < min_bid_edge (0.05) → skip (ambiguous)

if up_gui > dn_gui → UP is winning
if dn_gui > up_gui → DOWN is winning
```

**Consensus gates (required before buy):**

- Winning GUI price ≥ `min_winner_bid` (0.90) — screen shows a clear favorite
- Losing GUI price ≤ `max_loser_bid` (0.10) — other side clearly losing
- Ask still in `[buy_threshold, buy_max_price]` (execution price, not display)

Skips log as `buy_skip_incomplete_book`, `buy_skip_ambiguous`, or
`buy_skip_no_consensus`.

**Why this matters:** Blind mid/`one-mid-None` logic bought stale ~97¢ asks on
the wrong leg when the true winner's asks were cleared. The GUI never looked
like that; matching the GUI display stops those fake "reversals."

Gamma `outcomePrices` is a lagged cache of similar numbers — fine for cards,
too slow/stale for the buy window. Live CLOB book + last trade is the source.

### 21.4 Buy Trigger

Once the winner passes GUI consensus, the bot checks:

```
BUY_THRESHOLD (0.96) ≤ winning_ask ≤ BUY_MAX_PRICE (0.99)
AND winning_gui ≥ min_winner_bid (0.90)
AND opposite_gui ≤ max_loser_bid (0.10)
```

Sizing uses **`buy_budget`** (USD), not a fixed share count. Each FAK attempt
buys `min(remaining_budget / ask, top_of_book_size)` shares so spent notional
stays ≤ the budget (defaults: $21 / $8 / $24 for 15m / 5m / hourly; sized for ~$100/day at $0.98 avg with full participation and no reversals).

The buy uses **ask price** (not bid, not mid) as both the trigger and the FAK
limit price. The `buy_market_with_retry()` function re-fetches the ask before
each attempt and skips if the ask has moved outside `[BUY_THRESHOLD, BUY_MAX_PRICE]`.
The band (`buy_threshold: 0.96` … `buy_max_price: 0.99`) aims to catch the
winner once it is nearly certain, preferably before it gaps to 99¢.

### 21.5 Buy Window Enforcement

| Bot | Window Check | Window Parameter |
|---|---|---|
| 15m (`buybot.py`) | `minutes_left > BUY_WINDOW_MIN` | `buy_window_min: 3.0` (minutes) |
| 5m (`buybot5m.py`) | `seconds_left > BUY_START_S` | `buy_start_s: 90` (seconds) |
| Hourly (`buybothourly.py`) | `minutes_left > BUY_WINDOW_MIN` | `buy_window_min: 5.0` (minutes) |

The 5m bot uses a seconds-based window (`buy_start_s`) while the 15m and hourly
use minutes-based (`buy_window_min`). This is because 5m markets have sub-2-minute
remaining times where minute granularity is too coarse.

### 21.6 One Entry Per Market

Each bot enforces `one_entry_per_market: True` by checking `meta.get("bought_token")`
in the state cache. Once a market is bought, the bot will never buy it again,
even if the position is later hedged or redeemed.

### 21.7 Hedge Logic

The hedge is identical across all three buy bots:

1. **Trigger:** Cached bid for the held token ≤ `HEDGE_THRESHOLD` (0.65)
2. **Fresh fetch:** Before acting, a fresh order book is fetched
3. **Bounce protection:** If `fresh_mid > HEDGE_THRESHOLD`, the hedge is cancelled
   (the bid recovered)
4. **Execution:** `sell_market_with_retry()` places a FAK sell at the bid price
5. **Ghost fill detection:** If the sell reports 0 fill but the on-chain balance
   decreased, the fill is recorded as a "ghost fill"

### 21.8 State Isolation

Each buy bot uses completely separate state files:

| Bot | State File | PNL File | Log File | Heartbeat |
|---|---|---|---|---|
| 15m | `positions_buy.json` | `pnl_buy.json` | `buybot.log` | `.heartbeat_buy` |
| 5m | `positions_buy5m.json` | `pnl_buy5m.json` | `buybot5m.log` | `.heartbeat_buy5m` |
| Hourly | `positions_buyhourly.json` | `pnl_buyhourly.json` | `buybothourly.log` | `.heartbeat_buyhourly` |

The three buy bots never share state with each other or with the sell-side bots.
Cross-bot interference is prevented by mutually exclusive slug exclusions.

### 21.9 Configuration Hot-Reload

All three buy bots support hot-reloading strategy parameters via
`load_strategy()`. The function watches the strategy JSON file's mtime and
reloads on change. Type casting is handled correctly: booleans have special
parsing (`"true"`, `"1"`, `"yes"`), and numeric values are cast via
`type(expected)(value)`.

### 21.10 Systemd Services

Each standalone buy bot has its own systemd service:

```
deploy/polybuybot.service        → buybot.py (15m)
deploy/polybuybot5m.service      → buybot5m.py (5m)
deploy/polybuybothourly.service  → buybothourly.py (hourly)
```

All use `Restart=always`, `RestartSec=5`, and run from the same
`WorkingDirectory` with the shared `.env` file. They use the main `.venv`
(not `.venv-buy` which is only for the atomic mint buyer).

### 21.11 Key Differences from Atomic Mint Buyer (legacy)

Atomic mint is **unused**; table kept for contrast with the live standalone bots.

| Aspect | Standalone Buy Bots (**live**) | Atomic Mint Buyer (`buy/` — **unused**) |
|---|---|---|
| Entry method | FAK market order on CLOB | On-chain `splitPosition` via relayer |
| What it buys | Winning leg only (UP or DOWN) | Both legs (complete set) |
| Entry cost | 96–99¢ per share (market price) | Exactly $1.00 per set (on-chain) |
| Requires arming | No (always running) | Yes (ARM file gating) |
| Polling | 1s / 0.1s | 15s |
| State files | `positions_buy*.json` | `buy_data*/state.json` |
| Venv | `.venv` | `.venv-buy` |

---

## 22. Glossary

| Term | Definition |
|---|---|
| **ABI** | Application Binary Interface — the encoding format for Ethereum smart contract calls. |
| **Best bid** | The highest price a buyer is willing to pay in the order book. |
| **Best ask** | The lowest price a seller is willing to accept in the order book. |
| **CLOB** | Central Limit Order Book — a trading system where bids and asks are matched by price-time priority. |
| **conditionId** | A `bytes32` hash uniquely identifying a Polymarket market's outcome condition on-chain. |
| **Complete set** | One UP token + one DOWN token for the same market. Always worth $1.00 at resolution. |
| **CTF** | Conditional Token Framework — Polymarket's core smart contract for outcome tokens and redemptions. |
| **EOA** | Externally Owned Account — an Ethereum account controlled by a private key (as opposed to a contract account). |
| **ERC-1155** | An Ethereum token standard that allows a single contract to manage multiple token types. Polymarket uses it for outcome shares. |
| **FAK** | Fill-And-Kill — an order type that fills what it can immediately, then cancels the rest. |
| **Funder** | The Polymarket proxy wallet address that holds the user's funds. |
| **Gamma API** | Polymarket's public market discovery API (`gamma-api.polymarket.com`). |
| **Ghost fill** | A sell order that filled on-chain but wasn't reported as filled by the SDK. Detected via balance re-check. |
| **Hedge** | Selling the held (winner) leg after a market reversal, to cut losses before the shares go to $0. |
| **Keccak-256** | The hash function used in Ethereum (a variant of SHA-3). |
| **Loser leg** | The side of a binary market that will resolve to $0. |
| **MarketOrderArgs** | SDK argument class for constructing market orders with a price limit. |
| **Neg-risk** | A gas-efficient on-chain representation for multi-outcome markets on Polymarket. |
| **Nonce** | A sequential number that prevents transaction replay attacks on Ethereum. |
| **ntfy.sh** | A free push notification service used for operator alerts. |
| **Polygon** | An Ethereum Layer 2 blockchain (chain ID 137) where Polymarket operates. |
| **Proxy wallet** | A smart contract wallet that executes transactions on behalf of an EOA. Polymarket uses this pattern for security. |
| **pUSD** | Polymarket's USDC token contract on Polygon. |
| **Redemption** | The process of returning a complete set (UP + DOWN tokens) to the smart contract to receive $1.00 USDC after market resolution. |
| **Relayer** | A Polymarket-operated service that submits on-chain transactions on behalf of users, paying the gas fees. |
| **Reversal** | When the market flips direction — the side that was winning starts losing. Triggers the hedge phase. |
| **Sell window** | The final N seconds before market expiry during which the bot is allowed to sell. Currently 180 seconds (`SELL_WINDOW_MIN` = 3.0 min). |
| **Shadow simulator** | Paper-trading process (`sim/shadow.py`) that applies a configurable sell policy to every market in a series (5m/15m) using public books; never places real orders. |
| **set_cost** | Complete-set entry cost per share used by the shadow sim (default ~1.043 from history). Live bot does not gate on this. |
| **polyshadow** | systemd service name for the permanent shadow simulator on GCP. |
| **polybuy** | Live autonomous service that atomically splits pUSD into complete sets; re-armed by cron (4×/hour for 15m, 1×/hour for hourly), one mint per arm. |
| **polybuybot** / **polybuybot5m** / **polybuybothourly** | Standalone buy-side bots that buy the winning leg at 96–99¢ ask, hold to expiry, and hedge at 65¢ (see §21). |
| **Atomic mint** | One relayer batch that approves exact pUSD and calls standard-adapter `splitPosition`, producing equal UP and DOWN inventory or reverting entirely. |
| **Buy-side bot** | A standalone bot (`buybot.py` / `buybot5m.py` / `buybothourly.py`) that buys the winning leg of a binary market via FAK market order, as opposed to minting complete sets. |
| **Winner detection** | The process of determining which leg (UP or DOWN) is winning by comparing mid prices: `(bid + ask) / 2`. Used by the standalone buy-side bots. |
| **ENOSPC** | OS errno 28 — no space left on device. On the bot VM (2026-07) this was caused mainly by `/var/log` growth, not by `sim_data`. |
| **Slug** | A human-readable URL fragment identifying a market (e.g., `btc-updown-5m-1783218000`). |
| **Tick size** | The minimum price increment for a market. On Polymarket, typically $0.01. |
| **Token ID** | The ERC-1155 token identifier for a specific outcome in a market. Each binary market has two (UP and DOWN). |
| **TTM** | Time To Maturity — how much time remains until the market expires. Displayed in seconds when under 1 minute, otherwise in minutes. |

---

*This document was last updated for the six-bot production architecture
(2026-08-09): three sell bots (`bot.py` for 15m, `bot5m.py` for 5m,
`bothourly.py` for hourly), three standalone buy-side bots (`buybot.py` for 15m,
`buybot5m.py` for 5m, `buybothourly.py` for hourly), and three atomic mint
buyers (one per series via `buy/` package) run concurrently on a single GCP VM.
The standalone buy bots purchase the winning leg at 96–99¢ ask price using mid-price
comparison, hold to expiry, hedge at 65¢, and redeem winners at $1.00 (see §21).
The atomic mint buyers (`polybuy`) mint complete sets live via an SDK-equivalent
relayer PROXY flow (§20.9), paced by systemd armer services. The sell bots sell
the loser leg at configured thresholds, hedge reversals, and redeem winners.
All six bot families use separate state files, logs, and heartbeats with mutually
exclusive slug exclusions to prevent cross-bot interference. The shadow
simulators (`sim/`, §19) are currently stopped on the VM. See §8.5 for a complete
guide to VM operations, including which files are in Git vs VM-only, how to
restart each bot, and how to change parameters.*