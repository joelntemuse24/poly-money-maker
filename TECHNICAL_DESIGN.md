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
18. [The Dashboard — `dashboard.py`](#18-the-dashboard--dashboardpy)
19. [The Diagnostic Tool: `check_book.py`](#19-the-diagnostic-tool-check_bookpy)
20. [Glossary](#20-glossary)

---

## 1. Project Overview

**Poly Money Maker** is an automated **sell-side execution bot** for Polymarket's
Bitcoin hourly prediction markets. It does not buy positions — the operator enters
positions manually through Polymarket's web UI. The bot's job is to **monitor those
positions and automatically sell the "loser leg"** before the market expires, then
**redeem** any resolved positions back into USDC. It also includes a **hedge
mechanism** that cuts losses if the held leg reverses after the loser has been sold.

### The Core Thesis

In a binary prediction market ("Will Bitcoin go up or down this hour?"), you buy
**both** sides (UP and DOWN) as a pair — a "complete set." One side will be worth
$1.00 at resolution and the other $0.00. If you hold both, you're guaranteed to
redeem $1.00 per pair.

The profit comes from selling the **loser leg** — the side that's heading to $0.
If you can sell that loser leg for even 8 cents before the market resolves,
instead of letting it expire worthless, you lock in extra profit on top of the
$1.00 redemption. That's exactly what this bot does: it watches the order book,
and when the losing side's best bid drops to 8 cents, it fires a sell order.

### The Hedge Safety Net

After selling the loser leg, you're left holding only the winner. Normally this is
fine — the winner goes to $1.00. But if the market **reverses** (the side you
thought was winning starts losing), your remaining shares can go to $0. The hedge
phase watches for this: if the held leg's bid drops below 50 cents after the loser
was sold, the bot sells the held leg too, cutting losses before they compound.

### What the Bot Does NOT Do

- **No buying.** The bot is sell-only. Entry is manual.
- **No market making.** It doesn't post resting orders or provide liquidity.
- **No price prediction.** It doesn't try to forecast BTC direction.
- **No portfolio rebalancing.** It manages one specific market type (BTC hourly).

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
│              ┌──────────┐    ┌──────────────┐        │
│              │  Logging │    │  Dashboard   │        │
│              │ (bot.log)│    │  Status JSON │        │
│              └──────────┘    └──────────────┘        │
└──────────────────────────────────────────────────────┘

         ┌─────────────┐
         │ dashboard.py │  ← separate process, reads status JSON + bot.log
         │ Live viewer  │
         └─────────────┘

External Services:
  • Polymarket CLOB API  (clob.polymarket.com)    — order book, order submission
  • Polymarket Data API  (data-api.polymarket.com) — position tracking
  • Polymarket Relayer   (relayer-v2.polymarket.com) — on-chain redemption
  • ntfy.sh              (ntfy.sh)                 — push notifications
  • Polygon blockchain    (chain ID 137)            — settlement layer
```

The architecture is a **single-file, single-process, polling loop** with a
**separate dashboard viewer** that runs independently. There are no threads, no
async, no message queues, no databases.

- **Single file (`bot.py`)** keeps everything visible. You can read the entire bot
  top-to-bottom and understand the full flow without jumping between modules.
- **Single process** means no concurrency bugs, no race conditions on state, no
  inter-process communication overhead.
- **Polling loop** (5-second sleep between cycles) is simple and robust. The BTC
  hourly markets move fast enough to warrant a tight loop, and polling is far
  easier to reason about than event-driven code.
- **Separate dashboard (`dashboard.py`)** reads a status JSON file and log file
  that the bot writes each cycle. It's a read-only viewer — it doesn't affect the
  bot's operation and can be started/stopped independently.

---

## 4. File Inventory

| File | Purpose | Size |
|---|---|---|
| `bot.py` | The main bot — all trading logic lives here | ~928 lines |
| `dashboard.py` | Live terminal dashboard viewer (reads bot output) | ~244 lines |
| `check_book.py` | Diagnostic script for inspecting live order books | ~32 lines |
| `requirements.txt` | Python dependencies | 9 lines |
| `.env` | Environment variables (secrets — gitignored) | — |
| `.gitignore` | Excludes secrets, state files, and Python artifacts | — |
| `positions.json` | Runtime state cache (gitignored, auto-generated) | — |
| `bot.log` | Structured JSON-line log (gitignored, auto-generated) | — |
| `.dashboard_status.json` | Per-cycle status snapshot for dashboard (gitignored) | — |

### Why a Single-File Bot?

For a bot of this size (~930 lines), splitting into modules would add import
overhead and cognitive load without meaningful benefit. The code is organised
internally with **section comment banners** (e.g., `# --- HELPER FUNCTIONS ---`)
that act as visual module boundaries. The dashboard is separate because it's a
different concern — it's a viewer, not part of the trading engine.

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
| `rich` | Terminal formatting library (tables, panels, colours) | Trading bots produce a *lot* of console output. `rich` makes it readable — coloured tables, boxed panels, and formatted numbers. Both `bot.py` and `dashboard.py` use it. |
| `web3` | Ethereum Python library | Used for blockchain interaction (though most on-chain work is done via the relayer). |
| `eth-account` | Ethereum account management | Used to derive the EOA address from the private key for relayer submissions. |
| `eth-abi` | Ethereum ABI encoding | Used to encode redemption calldata (the raw bytes that tell the smart contract what to do). |
| `eth-utils` | Ethereum utility functions | Provides `keccak` (for function selectors) and `to_checksum_address` (Ethereum addresses must be in EIP-55 checksum format). |

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

### 6.2 Strategy Constants

Unlike the earlier version of this bot which used `os.getenv` for strategy
parameters, the current version uses **hardcoded constants**:

```@/c:/Users/ntemu/Downloads/poly money maker/bot.py:51-59
# ------------------------- STRATEGY CONFIG -------------------------
SELL_THRESHOLD = 0.08        # Sell the "loser" leg when per-share bid <= this
HEDGE_THRESHOLD = 0.50       # After selling loser, sell the held leg if it drops below this
SELL_WINDOW_MIN = 15         # Only sell in the last N minutes of the hour (reduces reversal risk)
SELL_GRACE_S = 10            # Wait N seconds after first seeing a position before selling
SELL_COOLDOWN_S = 30         # Min seconds between sell attempts on the same leg
REDEEM_THROTTLE_S = 300      # Min seconds between redemption retries
MAX_REDEEM_AGE_DAYS = 7      # Stop trying to redeem after N days
DRY_RUN = False
```

**Why hardcoded constants instead of env-based?** The strategy parameters are
tightly coupled to the bot's risk model. Changing them without understanding the
full flow could lead to unexpected losses. By hardcoding, we make it explicit
that these are the *design parameters* — not runtime-tunable knobs. If you want
to experiment, you edit the code (and hopefully read the comments first).

| Constant | Value | Meaning |
|---|---|---|
| `SELL_THRESHOLD` | `0.08` (8 cents) | If the loser leg's best bid drops to or below this price, sell it. Below 8 cents, the remaining value isn't worth the risk of holding. |
| `HEDGE_THRESHOLD` | `0.50` (50 cents) | After selling the loser, if the held (winner) leg drops below 50 cents, sell it too. This is the reversal protection — if the market flips, we cut losses at 50 cents rather than riding to $0. |
| `SELL_WINDOW_MIN` | `15` minutes | Only sell within the last 15 minutes before market expiry. This **time-gates** the sell trigger — even if the loser leg hits 8 cents with 2 hours left, the bot waits until the final 15 minutes. This reduces reversal risk: selling early locks in a few cents but exposes you to the market flipping; selling late means the outcome is nearly certain. |
| `SELL_GRACE_S` | `10` seconds | When we first discover a new position, wait 10 seconds before selling. This prevents selling on the very first tick where data might be stale or incomplete. |
| `SELL_COOLDOWN_S` | `30` seconds | After selling a leg, wait 30 seconds before attempting another sell on the same leg. Prevents hammering the API with rapid-fire orders. |
| `REDEEM_THROTTLE_S` | `300` seconds (5 min) | After submitting a redemption, wait 5 minutes before retrying. Redemptions are on-chain transactions that take time to confirm. |
| `MAX_REDEEM_AGE_DAYS` | `7` days | Stop trying to redeem after 7 days past expiry. Old conditions may have been cleaned up on-chain and retries are pointless. |
| `DRY_RUN` | `False` | When `True`, the bot logs decisions but doesn't send orders or transactions. Used for testing. |

### 6.3 Other Constants

```@/c:/Users/ntemu/Downloads/poly money maker/bot.py:31-38
HOST = "https://clob.polymarket.com"
DATA_API = "https://data-api.polymarket.com"
CHAIN_ID = 137
STATE_FILE = "positions.json"
BTC_SLUG_PREFIX = "bitcoin-up-or-down"
BTC_SLUG_ALIASES = ("bitcoin-up-or-down", "btc-updown")
PUSD = "0xC011a7E12a19f7B1f670d46F03B03f3342E82DFB"
CTF = "0x4D97DCd97eC945f40cF65F87097ACe5EA0476045"
```

- **`HOST`** — The CLOB API base URL for order book queries and order submission.
- **`DATA_API`** — A separate Polymarket API for position tracking.
- **`CHAIN_ID = 137`** — Polygon's chain ID.
- **`STATE_FILE`** — The JSON file where we persist metadata between cycles.
- **`BTC_SLUG_PREFIX`** / **`BTC_SLUG_ALIASES`** — Slug prefixes that identify BTC
  hourly markets. `BTC_SLUG_ALIASES` is a **tuple** of accepted prefixes, because
  Polymarket has used different slug formats over time (`bitcoin-up-or-down` and
  `btc-updown`). The tuple lets us match either one.
- **`PUSD`** — The Polymarket USDC (pUSD) contract address on Polygon. Used in
  redemption calldata.
- **`CTF`** — The **Conditional Token Framework** contract address. This is
  Polymarket's core smart contract that manages outcome tokens and redemptions.

---

## 7. Client Setup & Authentication

```@/c:/Users/ntemu/Downloads/poly money maker/bot.py:60-81
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

```@/c:/Users/ntemu/Downloads/poly money maker/bot.py:83-88
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

```@/c:/Users/ntemu/Downloads/poly money maker/bot.py:90-108
```

The ASCII art banner provides immediate visual confirmation that the bot started
successfully, along with version and configuration summary. When `DRY_RUN` is
`True`, a yellow warning banner is printed — no orders or on-chain transactions
will be sent, only log entries. This is the bot's safety mode for testing
strategy changes without risking funds.

---

## 8. Graceful Shutdown & Notifications

### 8.1 Signal Handling

```@/c:/Users/ntemu/Downloads/poly money maker/bot.py:110-123
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

```@/c:/Users/ntemu/Downloads/poly money maker/bot.py:125-126
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

```@/c:/Users/ntemu/Downloads/poly money maker/bot.py:130-145
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

The bot doesn't notify on normal sells — those are expected and frequent. Only
exceptional events warrant a push.

---

## 9. Helper Functions — The Foundation Layer

These are the utility functions that the rest of the bot builds on. They handle
cross-cutting concerns: API safety, file I/O, logging, and balance queries.

### 9.1 `safe_api_call` — The Error Filter

```@/c:/Users/ntemu/Downloads/poly money maker/bot.py:150-157
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

```@/c:/Users/ntemu/Downloads/poly money maker/bot.py:160-168
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

```@/c:/Users/ntemu/Downloads/poly money maker/bot.py:171-175
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

```@/c:/Users/ntemu/Downloads/poly money maker/bot.py:178-186
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

```@/c:/Users/ntemu/Downloads/poly money maker/bot.py:189-194
def log_event(event, **kwargs):
    """Append a structured JSON log line to bot.log."""
    entry = {"ts": datetime.now().isoformat(), "event": event}
    entry.update(kwargs)
    with open("bot.log", "a") as f:
        f.write(json.dumps(entry) + "\n")
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

```@/c:/Users/ntemu/Downloads/poly money maker/bot.py:199-215
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

```@/c:/Users/ntemu/Downloads/poly money maker/bot.py:218-230
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

```@/c:/Users/ntemu/Downloads/poly money maker/bot.py:233-277
_ET = ZoneInfo("America/New_York")
_SLUG_TIME_RE = re.compile(r"(\d{1,2})(am|pm)-et$")

def parse_position_end_dt(legs):
    for p in legs:
        for key in ("slug", "eventSlug"):
            slug = p.get(key) or ""
            tail = slug.rsplit("-", 1)[-1]
            if tail.isdigit():
                ts = int(tail)
                if ts > 1_700_000_000:
                    return datetime.fromtimestamp(ts)
    ...
```

**What it does:** Determines when a market expires by parsing the slug. This is
necessary because the `endDate` field from the API isn't always reliable —
sometimes it's missing, sometimes it's in the wrong timezone.

**The two parsing strategies:**

1. **Unix timestamp in slug** — Some slugs end with a Unix timestamp (e.g.,
   `bitcoin-up-or-down-1719259200`). We extract the last segment after `-`,
   check if it's a large number (> 1.7 billion = year 2024), and convert it
   directly.

2. **Hour + AM/PM from slug** — Other slugs end with a time like `12pm-et`
   (e.g., `bitcoin-up-or-down-2024-06-24-12pm-et`). We use a **regex**
   (`_SLUG_TIME_RE`) to extract the hour and AM/PM, convert to 24-hour format,
   then combine with the `endDate` to compute the actual expiry. The slug hour
   is the *start* of the hourly window; the market closes 1 hour later.

**Why `ZoneInfo("America/New_York")`?** BTC hourly markets on Polymarket use
Eastern Time (ET) for their slug times. We need to interpret "12pm ET" correctly
regardless of what timezone the server runs in. `ZoneInfo` provides proper
timezone-aware datetime handling.

**Why `_ET` and `_SLUG_TIME_RE` at module level?** These are **compiled once**
at import time and reused across all calls. Compiling a regex on every function
call would be wasteful. The leading underscore marks them as internal.

**Why `rsplit("-", 1)[-1]`?** `rsplit` splits from the right. `rsplit("-", 1)`
splits into at most 2 parts, taking only the last `-` separator. `[-1]` gets the
last segment. For `bitcoin-up-or-down-1719259200`, this returns `1719259200`.

### 10.4 `empty_opposite_leg` — Constructing Missing Legs

```@/c:/Users/ntemu/Downloads/poly money maker/bot.py:280-290
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

```@/c:/Users/ntemu/Downloads/poly money maker/bot.py:293-381
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
    title = (p.get("title") or "").lower()
    if not (
        slug.startswith(BTC_SLUG_ALIASES)
        or event_slug.startswith(BTC_SLUG_ALIASES)
        or "bitcoin up or down" in title
    ):
        continue
```

We check three fields (`slug`, `eventSlug`, `title`) against multiple patterns.
The `startswith(BTC_SLUG_ALIASES)` call works because `str.startswith()` accepts
a **tuple** of prefixes — it returns `True` if the string starts with *any* of
them. This is a clean Python idiom for multi-prefix matching.

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

### 11.1 `get_book_bid` — Finding the Best Buyer

```@/c:/Users/ntemu/Downloads/poly money maker/bot.py:386-395
def get_book_bid(token_id):
    try:
        book = safe_api_call(client.get_order_book, token_id)
        bids = book.get("bids", [])
        if not bids:
            return None, 0.0
        best = max(bids, key=lambda x: float(x.get("price", 0)))
        return float(best.get("price", 0)), float(best.get("size", 0))
    except Exception:
        return None, 0.0
```

**What it does:** Fetches the live order book for a token and returns the best
(highest) bid price and its available size.

**Why REST over SDK for the order book?** Actually, this *does* use the SDK
(`client.get_order_book`). The SDK wraps the CLOB's REST endpoint for order book
data. We use `safe_api_call` to get error filtering.

**Why `max(bids, key=...)`?** The `max()` function with a `key` argument finds
the element with the highest key value. Here, the key is the bid price. This is
more Pythonic than sorting the entire list and taking `[0]` — it's O(n) instead
of O(n log n), and it clearly expresses "find the maximum."

**Why return a tuple `(price, size)`?** The caller needs both: the price
determines whether to sell, and the size determines how many shares we can
actually sell at that price (the "depth"). Returning a tuple is a lightweight
alternative to creating a dataclass — no extra class definition needed.

**Why `(None, 0.0)` on failure?** `None` for price means "no bid available" —
the caller checks `if bid is not None` before acting. This is the **null sentinel
pattern**: `None` is a signal value that means "data unavailable," distinct from
`0.0` which would mean "the best bid is $0.00" (a valid but different meaning).

### 11.2 `quote_leg` — Pricing a Single Leg

```@/c:/Users/ntemu/Downloads/poly money maker/bot.py:543-553
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

```@/c:/Users/ntemu/Downloads/poly money maker/bot.py:400-403
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

```@/c:/Users/ntemu/Downloads/poly money maker/bot.py:406-428
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

```@/c:/Users/ntemu/Downloads/poly money maker/bot.py:431-455
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

```@/c:/Users/ntemu/Downloads/poly money maker/bot.py:458-495
def sell_market_with_retry(token_id, size, price_limit, tick_size="0.01", max_retries=3):
    total_sold = 0.0
    remaining = float(size)
    price = max(float(price_limit or tick_size), float(tick_size))
    if DRY_RUN:
        console.print(f"  [bold black on yellow][DRY SELL][/] would SELL {remaining:.4f} {str(token_id)[:12]}… @ ≥{price:.3f}")
        log_event("dry_sell", token_id=token_id, size=remaining, price_limit=price)
        return 0, None
    for attempt in range(max_retries):
        if remaining < 0.01:
            break
        try:
            neg_risk = safe_api_call(client.get_neg_risk, token_id)
            result = safe_api_call(
                client.create_and_post_market_order,
                MarketOrderArgs(token_id=token_id, amount=remaining, side=SELL, price=price),
                options=PartialCreateOrderOptions(tick_size=tick_size, neg_risk=neg_risk),
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
2. Query `get_neg_risk` — needed for the order options.
3. Submit a FAK market order for the remaining amount.
4. Confirm the fill size using `confirm_fill_size`.
5. If 0 confirmed fills, **stop immediately** — the comment says "stopping to
   avoid double-sell." If the order filled but we can't confirm it, retrying
   could sell the same shares twice. Better to stop and let the ghost fill
   detection in the main loop sort it out.
6. Update totals and continue if shares remain.

**Why `time.sleep(1)` between attempts?** A small delay to avoid hammering the
API. Even though we removed the global 300ms delay from `safe_api_call`, the
retry loop has its own 1-second pacing.

**Why `MarketOrderArgs` instead of `OrderArgs`?** The current codebase uses
`MarketOrderArgs` — a market order variant that accepts a `price` parameter as a
price limit. This is different from a pure market order (which would sell at any
price) and from a limit order (which would rest on the book). It's a
**marketable limit order** — sell at the best available price, but not below the
specified limit. Combined with `OrderType.FAK`, this gives us: "sell immediately
at or above this price, cancel whatever doesn't fill."

**The `price` calculation:**

```python
price = max(float(price_limit or tick_size), float(tick_size))
```

This ensures the price limit is at least the tick size (the minimum price
increment on Polymarket, typically $0.01). If `price_limit` is `None` or 0, we
fall back to the tick size — effectively "sell at any price above $0.01."

---

## 13. Redemption — Settling Resolved Markets

After a market expires, the winning side is worth $1.00 per share and the losing
side is worth $0. To claim the $1.00, you need to **redeem** your complete set
(one UP + one DOWN token) through Polymarket's smart contract. This is an on-chain
operation on Polygon.

### 13.1 Refactored Relayer Helpers

The current codebase has refactored the relayer submission into two reusable
functions:

```@/c:/Users/ntemu/Downloads/poly money maker/bot.py:499-539
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

```@/c:/Users/ntemu/Downloads/poly money maker/bot.py:558-596
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
loop that runs every 5 seconds, executing a complete cycle of: discover
positions → display status → redeem resolved → sell losers → hedge → write
dashboard status → sleep.

### 14.1 Loop Structure

```@/c:/Users/ntemu/Downloads/poly money maker/bot.py:606-617
positions_meta = load_json(STATE_FILE)
CYCLE = 0

while not _shutdown_requested:
    try:
        CYCLE += 1
        now_ms = time.time() * 1000
        now_str = datetime.now().strftime("%H:%M:%S")

        pusd_bal = get_balance()
        positions_raw = get_user_positions()
        managed_sets = group_btc_complete_sets(positions_raw or [], positions_meta)
```

**`positions_meta = load_json(STATE_FILE)`** — Load the persisted metadata cache
before entering the loop. This survives restarts.

**`CYCLE = 0`** — A counter for visual tracking (`TICK #0001`, `TICK #0002`, ...).

**`now_ms = time.time() * 1000`** — Current time in milliseconds (the API uses
millisecond timestamps).

**`positions_raw or []`** — If `get_user_positions()` returns `None` (API
failure), we pass an empty list. The grouping function will rely entirely on the
metadata cache for this cycle.

### 14.2 Garbage Collection

```@/c:/Users/ntemu/Downloads/poly money maker/bot.py:630-637
        if positions_raw is not None:
            live_conds = {s["conditionId"] for s in managed_sets}
            stale_conds = [c for c in list(positions_meta.keys()) if c not in live_conds]
            for c in stale_conds:
                del positions_meta[c]
            if stale_conds:
                log_event("gc", stale_conditions=stale_conds)
                save_json(STATE_FILE, positions_meta)
```

Removes metadata for conditions we no longer hold. Only runs when the data API
query succeeded (`positions_raw is not None`). **Why `list(positions_meta.keys())`?**
We're deleting from the dict while iterating — converting to a list first avoids
`RuntimeError: dictionary changed size during iteration`.

### 14.3 The Positions Table

The bot uses `rich.Table` to render a colour-coded dashboard. Each row shows:

- **INSTRUMENT** — the market question (truncated to 40 chars).
- **EXPIRY** — the market end time (HH:MM format).
- **TTM** — Time To Maturity in minutes, colour-coded:
  - Red if < `SELL_WINDOW_MIN` (15 minutes, in the exit window).
  - Yellow if < 60 minutes (approaching the window).
  - Green if > 60 minutes (safe).
- **UP / DN** — share counts for each leg.
- **STATE** — one of:
  - `✓ REDEEM` (magenta) — market resolved, ready for redemption.
  - `· closed` (dim) — market expired but not yet redeemable.
  - `○ EXIT WINDOW` (red) — in the final 15-minute sell window.
  - `● WATCHING` (green) — holding, outside the sell window.

### 14.4 The Redeem Phase

```@/c:/Users/ntemu/Downloads/poly money maker/bot.py:694-711
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
                continue
            tx = redeem_condition(cond, label=(s["question"] or "?")[:32])
            if tx:
                meta["redeem_submitted_at"] = now_ms
                log_event("redeem_submit", condition_id=cond, tx_id=str(tx))
                save_json(STATE_FILE, positions_meta)
```

Three guard conditions before attempting redemption:

1. **`MAX_REDEEM_AGE_DAYS`** — Don't redeem conditions older than 7 days past
   expiry. They may have been cleaned up on-chain.
2. **`_redeem_permanent_failures`** — Skip conditions that previously returned a
   permanent error.
3. **`REDEEM_THROTTLE_S`** — Don't retry within 5 minutes of the last submission.

### 14.5 The Sell Phase — Trigger Evaluation

For each managed set, the bot determines whether to sell the UP leg, the DOWN
leg, or neither.

**Step 1: Record entry time**

```python
if "entered_at" not in meta:
    meta["entered_at"] = now_ms
    meta["up_token"] = up_token
    meta["dn_token"] = dn_token
    meta["question"] = s["question"]
    meta["end_date"] = s["up"].get("endDate") or s["dn"].get("endDate")
    save_json(STATE_FILE, positions_meta)
```

The first time we see a position, we record when we entered and cache the token
IDs and market metadata. This is the backfill data that `group_btc_complete_sets`
uses if the API later loses track of the position.

**Step 2: Grace period**

```python
if now_ms - meta["entered_at"] < SELL_GRACE_S * 1000:
    continue
```

Wait 10 seconds after first discovery before selling. Prevents acting on
potentially stale data from the very first tick.

**Step 2b: Sell window check**

```python
# Only sell in the last SELL_WINDOW_MIN minutes to reduce reversal risk
if minutes_left > SELL_WINDOW_MIN:
    continue
```

If the market has more than 15 minutes left until expiry, skip the sell phase
entirely for this set. This is the **reversal risk mitigation** — selling the
loser leg early (e.g., with 2 hours left) locks in 8 cents but leaves you
exposed to the market flipping. By waiting until the final 15 minutes, the
outcome is nearly settled and reversal is unlikely.

**Step 3: Fetch best bids and price both legs**

```@/c:/Users/ntemu/Downloads/poly money maker/bot.py:740-744
            up_bid, _ = get_book_bid(up_token) if up_token else (None, 0.0)
            dn_bid, _ = get_book_bid(dn_token) if dn_token else (None, 0.0)

            up_price, up_matched_price = quote_leg(up_bid)
            dn_price, dn_matched_price = quote_leg(dn_bid)
```

We only fetch the order book for legs we actually hold. `quote_leg` converts the
raw bid into the pricing tuple used for decisions.

**Step 4: Trigger evaluation**

```@/c:/Users/ntemu/Downloads/poly money maker/bot.py:745-754
            up_trigger = up_size > 0 and up_price is not None and up_price <= SELL_THRESHOLD
            dn_trigger = dn_size > 0 and dn_price is not None and dn_price <= SELL_THRESHOLD

            # Guard: if both legs trigger, only sell the lower-priced one to ensure
            # we still hold a winner for the $1 payout at resolution.
            if up_trigger and dn_trigger:
                if up_price <= dn_price:
                    dn_trigger = False
                else:
                    up_trigger = False
```

**The trigger has two conditions:** Sell a leg if (1) we hold shares, there's a
bid, and the bid is at or below 8 cents, **and** (2) the market is in its final
15 minutes (`minutes_left <= SELL_WINDOW_MIN`). The time gate is enforced by
the check at the top of the sell phase:

```python
# Only sell in the last SELL_WINDOW_MIN minutes to reduce reversal risk
if minutes_left > SELL_WINDOW_MIN:
    continue
```

This means even if the loser leg hits 8 cents with 2 hours left, the bot waits.
Selling early locks in a few cents but exposes you to the market reversing —
the held "winner" could flip and go to $0. By waiting until the final 15
minutes, the outcome is nearly certain and reversal risk is minimal.

**Mutual exclusion:** If both legs trigger (both at ≤ 8 cents), only sell the one
with the lower bid (the bigger loser). We always keep the winning leg to redeem
$1.00 at resolution.

**Step 5: Cooldown check**

```@/c:/Users/ntemu/Downloads/poly money maker/bot.py:759-760
            will_sell_up = sell_up and (now_ms - (meta.get("last_sell_up_at") or 0) >= SELL_COOLDOWN_S * 1000)
            will_sell_dn = sell_dn and (now_ms - (meta.get("last_sell_dn_at") or 0) >= SELL_COOLDOWN_S * 1000)
```

Even if the trigger fires, we check a per-leg 30-second cooldown.

**Step 6: Execute the sell with ghost fill detection**

```@/c:/Users/ntemu/Downloads/poly money maker/bot.py:778-799
                else:
                    log_event("sell_attempt", condition_id=cond, leg="up", size=up_size, bid=up_bid, price_limit=up_price)
                    sold, _ = sell_market_with_retry(up_token, up_size, up_matched_price or SELL_THRESHOLD)
                    if sold > 0:
                        meta["last_sell_up_at"] = now_ms
                        up_size -= sold
                        meta["expected_up_size"] = up_size
                        log_event("sell_fill", condition_id=cond, leg="up", sold=sold, remaining=up_size, price=up_price)
                        save_json(STATE_FILE, positions_meta)
                    else:
                        time.sleep(2)
                        actual_bal = check_token_balance(up_token)
                        if actual_bal is not None and actual_bal < up_size - 0.01:
                            ghost_sold = up_size - actual_bal
                            meta["last_sell_up_at"] = now_ms
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

### 14.6 The Sleep

```@/c:/Users/ntemu/Downloads/poly money maker/bot.py:921-922
    console.print("[dim bright_black]· · ·  sleeping 5s  · · ·[/]")
    time.sleep(5)
```

The bot sleeps **5 seconds** between cycles (down from 30 seconds in the previous
version). This tighter loop is possible because:

- The sell trigger is purely price-based (no time window), so faster polling
  means we catch price drops sooner.
- `safe_api_call` no longer has a built-in delay, so API calls are faster.
- 5 seconds is fast enough to react to rapid price movements in the last minutes
  of a market, while still being reasonable for the API.

### 14.7 Dashboard Status Writing

```@/c:/Users/ntemu/Downloads/poly money maker/bot.py:901-919
    try:
        _dash_positions = []
        for s in managed_sets:
            mins = (s["end_ts"] - now_ms) / 60000
            _dash_positions.append({
                "question": (s["question"] or "?")[:40],
                "ttm_min": round(mins, 1),
                "up_size": float(s["up"].get("size", 0)),
                "dn_size": float(s["dn"].get("size", 0)),
                "up_token": s["up"].get("asset"),
                "dn_token": s["dn"].get("asset"),
                "redeemable": bool(s["up"].get("redeemable") or s["dn"].get("redeemable")),
            })
        with open(".dashboard_status.json", "w") as _df:
            json.dump({"cycle": CYCLE, "nav": pusd_bal, "ts": time.time(),
                       "positions": _dash_positions}, _df)
    except Exception:
        pass
```

Each cycle, the bot writes a JSON snapshot of its state to
`.dashboard_status.json`. This file is read by `dashboard.py` (section 18) to
render a live terminal dashboard. The write is wrapped in `try/except: pass`
because the dashboard is a non-critical feature — if the write fails, the bot
shouldn't care.

### 14.8 Graceful Shutdown Exit

```@/c:/Users/ntemu/Downloads/poly money maker/bot.py:924-927
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

The hedge phase is the most significant new feature in the current codebase. It
runs *after* the sell phase, for each managed set.

### 15.1 The Problem It Solves

After selling the loser leg, you hold only the "winner." For example, if BTC was
going up and you sold the DOWN leg at 8 cents, you now hold only UP shares.
Normally, UP goes to $1.00 at resolution and you redeem.

But what if the market **reverses**? BTC starts going down. The UP shares you're
holding start losing value — from 92 cents to 50 cents to 10 cents to $0. Without
a hedge, you've turned a profitable trade (sold DOWN for 8 cents + redeem UP for
$1.00 = $1.08) into a loss (sold DOWN for 8 cents + UP goes to $0 = $0.08).

### 15.2 The Hedge Logic

```@/c:/Users/ntemu/Downloads/poly money maker/bot.py:826-888
            # ================= HEDGE PHASE =================
            # If we already sold one leg (the "loser") and the held leg drops
            # below HEDGE_THRESHOLD, sell it too to limit reversal losses.
            loser_was_up = meta.get("last_sell_up_at") and up_size < 0.01 and dn_size >= 0.01
            loser_was_dn = meta.get("last_sell_dn_at") and dn_size < 0.01 and up_size >= 0.01

            if loser_was_up and dn_price is not None and dn_price <= HEDGE_THRESHOLD:
                # ... sell the DOWN (held) leg
            elif loser_was_dn and up_price is not None and up_price <= HEDGE_THRESHOLD:
                # ... sell the UP (held) leg
```

**How it works:**

1. **Detect that we sold a loser** — `loser_was_up` is True if we previously sold
   the UP leg (`last_sell_up_at` exists), UP size is now ~0, and we still hold
   DOWN shares. `loser_was_dn` is the mirror.

2. **Check if the held leg is collapsing** — if the held leg's bid drops to or
   below `HEDGE_THRESHOLD` (50 cents), the market has reversed. The winner is
   becoming the loser.

3. **Sell the held leg** — using `sell_market_with_retry` with a price limit of
   `$0.01` (sell at any price above 1 cent). This is a **panic exit** — we're not
   trying to get a good price, we're trying to get *something* before it goes to
   $0.

4. **Notify the operator** — `notify("HEDGE FIRED", ...)` sends an urgent push
   notification. A hedge firing is an exceptional event that warrants immediate
   attention.

5. **Ghost fill detection** — same pattern as the sell phase. If the hedge sell
   returns 0 confirmed fills, check the actual balance to detect ghost fills.

**Why `HEDGE_THRESHOLD = 0.50`?** At 50 cents, the market is saying there's a
50/50 chance of either outcome. If we're holding the side that was previously
winning but is now at 50 cents, the reversal is real. Selling at 50 cents
recovers half the $1.00 redemption value — much better than riding it to $0.

**Why sell at `$0.01` price limit?** The hedge is an emergency. We don't want to
miss a fill because we set the price limit too high. At 1 cent, we'll match any
bid on the book. The priority is exit, not price.

**Why the `elif`?** Only one hedge can fire per set per cycle — if the UP loser
was sold and DOWN is collapsing, we hedge DOWN. We don't also check UP (which has
size ~0 anyway). The `elif` makes this mutual exclusion explicit.

---

## 16. State Management & Persistence

### 16.1 What We Persist (and What We Don't)

The bot's state file (`positions.json`) is a **metadata cache** — it doesn't
store the actual position data (sizes, redeemable flags). Those come fresh from
the data API every cycle. We only persist things that the API *can't* tell us:

| Field | Purpose |
|---|---|
| `entered_at` | When we first saw this set. Used for the 10-second grace period. |
| `up_token` / `dn_token` | Token IDs for backfill when the API loses track. |
| `question` | Market name for display when backfilling. |
| `end_date` | Market expiry for backfill. |
| `expected_up_size` / `expected_dn_size` | Our estimate of remaining shares after a partial or ghost sell. |
| `last_sell_up_at` / `last_sell_dn_at` | Timestamps for the 30-second sell cooldown. |
| `redeem_submitted_at` | Timestamp for the 5-minute redeem throttle. |

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

```@/c:/Users/ntemu/Downloads/poly money maker/bot.py:890-898
    except Exception:
        log_event("cycle_error", traceback=traceback.format_exc())
        console.print(Panel(
            traceback.format_exc(),
            title="[bold bright_red]■■  SYSTEM FAULT  ■■[/]",
            subtitle="[dim]auto-restart in 5s · cycle aborted[/]",
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
| `sell_market_with_retry` | Return `(0, None)` | No shares sold; caller tries ghost fill detection. |
| `redeem_condition` | Return `None` | No redemption submitted; retry next cycle. |
| `notify` | Silent `pass` | Notifications are best-effort, never crash. |

The pattern: **non-critical failures return a safe default; critical failures
propagate to the loop-level catch.** Nothing in the sell, hedge, or redeem path
is allowed to crash the bot — at worst, a cycle is skipped.

### 17.3 The Complete Audit Log

Every significant action is logged to `bot.log`:

| Event | When | Key Fields |
|---|---|---|
| `sell_attempt` | Before submitting a sell order | `condition_id`, `leg`, `size`, `bid`, `price_limit` |
| `sell_fill` | After a successful sell | `condition_id`, `leg`, `sold`, `remaining`, `price` |
| `sell_ghost_fill` | Sell confirmed via balance check | `condition_id`, `leg`, `sold`, `remaining`, `price` |
| `sell_fail` | After a failed sell (0 filled, no ghost) | `condition_id`, `leg`, `size`, `bid`, `price_limit` |
| `hedge_attempt` | Before submitting a hedge sell | `condition_id`, `leg`, `size`, `bid`, `price_limit` |
| `hedge_fill` | After a successful hedge sell | `condition_id`, `leg`, `sold`, `remaining`, `price` |
| `hedge_ghost_fill` | Hedge confirmed via balance check | `condition_id`, `leg`, `sold`, `remaining`, `price` |
| `hedge_fail` | After a failed hedge sell | `condition_id`, `leg`, `size`, `bid` |
| `redeem_submit` | After submitting a redemption | `condition_id`, `tx_id` |
| `dry_sell` | DRY_RUN sell simulation | `token_id`, `size`, `price_limit` |
| `dry_redeem` | DRY_RUN redeem simulation | `condition_id`, `label` |
| `gc` | After garbage-collecting stale metadata | `stale_conditions` |
| `cycle_error` | When the cycle catches an exception | `traceback` |
| `shutdown` | On graceful shutdown | `reason` |

This log is the bot's **audit trail**. If something goes wrong, you can
reconstruct exactly what happened by parsing `bot.log`.

---

## 18. The Dashboard — `dashboard.py`

```@/c:/Users/ntemu/Downloads/poly money maker/dashboard.py:1-244
```

### 18.1 Purpose

The dashboard is a **separate, read-only process** that provides a live
terminal-based view of the bot's operation. It reads two files the bot writes
each cycle:

- `.dashboard_status.json` — current positions, NAV, cycle count
- `bot.log` — recent events (tail)

It does **not** communicate with the bot directly. There's no socket, no pipe,
no shared memory. The bot writes files; the dashboard reads them. This
**decoupled architecture** means:

- The dashboard can be started/stopped without affecting the bot.
- The dashboard can't accidentally crash the bot.
- The dashboard can run on a different machine (if the files are shared).

### 18.2 Key Functions

**`read_status()`** — Reads and parses `.dashboard_status.json`. Returns `None`
if the file doesn't exist or is invalid JSON (e.g., the bot hasn't written it
yet, or is mid-write).

**`tail_log(n=10)`** — Reads the last `n` lines of `bot.log`. Uses a
**seek-from-end** technique: seek to the end of the file, read the last 8KB,
split into lines, return the last `n`. This is efficient — it doesn't read the
entire log file, which could be megabytes.

**`format_event(line)`** — Parses a JSON log line and returns a `rich`-formatted
string. Different event types get different colours and labels:

| Event | Display |
|---|---|
| `sell_fill` | `[bold bright_yellow]SELL[/] UP 50.00 @ 0.080` |
| `hedge_fill` | `[bold bright_red]HEDGE[/] DN 100.00 @ 0.450` |
| `sell_ghost_fill` | `[bold yellow]GHOST[/] UP 50.00 confirmed` |
| `redeem_submit` | `[bright_magenta]REDEEM[/] submitted` |
| `cycle_error` | `[bold red]ERROR[/] <last traceback line>` |
| `shutdown` | `[bright_green]SHUTDOWN[/] clean exit` |

**`build_dashboard(status, events)`** — Composes the full dashboard layout using
`rich.Layout`:

- **Header** — status icon (LIVE/STALE), NAV, cycle count, active/redeem counts.
- **Positions table** — market name, TTM, UP/DN sizes, state (WATCHING/EXIT WINDOW/REDEEM).
- **Events panel** — last 10 formatted log events.
- **Footer** — data age and instructions.

**Stale detection:** If the status file is older than 15 seconds, the header
shows `STALE` in red instead of `LIVE` in green. This immediately alerts the
operator that the bot may have stopped writing (crashed, hung, or lost API
connectivity).

### 18.3 The `Live` Display

```python
with Live(console=console, refresh_per_second=2, screen=True) as live_display:
    while True:
        status = read_status()
        log_lines = tail_log(15)
        events = [format_event(line) for line in log_lines if format_event(line)]
        dashboard = build_dashboard(status, events)
        live_display.update(dashboard)
        time.sleep(0.5)
```

`rich.Live` with `screen=True` creates a **full-screen terminal display** that
updates in place (like `htop` or `top`). The dashboard refreshes twice per
second (every 0.5s sleep), reading fresh data from the files each time.

**Why `refresh_per_second=2`?** The bot writes status every 5 seconds, so
refreshing faster than 2Hz would show the same data multiple times. 2Hz is
smooth enough for human perception without wasting CPU.

**Why `except KeyboardInterrupt: break`?** Ctrl-C exits the dashboard but
doesn't affect the bot. The `with` block ensures the terminal is restored to
normal mode on exit.

---

## 19. The Diagnostic Tool: `check_book.py`

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

## 20. Glossary

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
| **Sell window** | The final N minutes before market expiry during which the bot is allowed to sell. Currently 15 minutes (`SELL_WINDOW_MIN`). |
| **Slug** | A human-readable URL fragment identifying a market (e.g., `bitcoin-up-or-down-2024-06-24-5pm`). |
| **Tick size** | The minimum price increment for a market. On Polymarket, typically $0.01. |
| **Token ID** | The ERC-1155 token identifier for a specific outcome in a market. Each binary market has two (UP and DOWN). |
| **TTM** | Time To Maturity — how many minutes remain until the market expires. |

---

*This document was generated as a technical design reference for the Poly Money
Maker trading bot. It reflects the current state of the codebase on the `main`
branch (commit `3e6d415` — "Restrict sells to last 15 minutes of the hour").*