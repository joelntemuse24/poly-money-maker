# Poly Money Maker: technical design of the live 15m mint system

This document explains `mintbot.py` on `main` through **#258** (13 October 2026): discover the next BTC 15m Up/Down window, mint a complete set, scrap part of the loser, dump the held leg, optionally buy it back (reclaim) with a concurrent hedge, then redeem or stop. Hypothetical trades illustrate arithmetic; they are not performance claims.

Where a knob matters, the text gives the **code default** (`DEFAULTS` in `mintbot.py`). **Live** numbers are only the ones the operator supplied for this revision. They live in gitignored `strategy_mint.json` on the VM, which is not in this repo, so a key absent from that list is not restated as live. Live JSON wins for every key it sets. The repo never commits a live JSON. The 3 October 2026 read (200 shares, dump under 40¢) is history in the [changelog](#changelog), not the current reference.

Three documents have different jobs:

| Document | Question it answers |
|---|---|
| `CURRENT.md` | What is running today, with which settings? |
| `AGENTS.md` | What must a coding agent know before changing anything? |
| `TECHNICAL_DESIGN.md` | How is the system built, and why do its money paths work this way? |

This file replaces the earlier guided tour of `buybothourly.py` (hourly FAK entry / hedge / TP). That strategy is **retired**. Buybots, complement, DangerZone, shadow bots, and hourly-dense pathlog stay off. Do not start them from this document. This file lives on GitHub only; no deploy or VM sync step copies it anywhere.

Read Parts I and II straight through. Part III walks mint and sell. Part IV covers helpers. Part V covers operations and sharp edges.

## Contents

- [Part I — Picture](#part-i)
  - [The opportunity and its limits](#section-1)
  - [Processes, wallet identities and files](#section-2)
  - [Repository map and reading order](#section-3)
- [Part II — Ideas the code assumes](#part-ii)
  - [Complete sets, CTF split, and why mint ≠ buy](#section-4)
  - [Books, FAK, sized bids, and three prices](#section-5)
  - [Python shape: values, state and side effects](#section-6)
  - [External systems](#section-7)
  - [JSON as durable memory](#section-8)
- [Part III — Walking mintbot.py](#part-iii)
  - [Startup, lock, strategy load](#section-9)
  - [Two loops: sell vs mint/discover](#section-10)
  - [Chainlink TWAP tape (recording only)](#section-10c)
  - [Sync-loop audit](#section-10b)
  - [Eligibility: not-yet-open 15m windows](#section-11)
  - [Capacity: max_open_sets and adjacent lookahead](#section-12)
  - [already_minted: failed remint after cooldown](#section-13)
  - [Relay hub: internal transaction failure](#section-13b)
  - [Mint, line by line: precheck, reserve, claim, submit](#section-14)
  - [Reconcile: relayer state → inventory confirm](#section-15)
  - [Sell path overview](#section-16)
  - [Loser scrap: arm, persist, time gate, floor sweep](#section-17)
  - [Partial loser scrap: sell_scrap_fraction](#section-17b)
  - [Winner cash-out: held for redeem in practice](#section-18)
  - [Held-leg dump: trigger, persist, fresh-bid FAK refire, kept side](#section-19)
  - [Reclaim: entry, size, stop](#section-19b)
  - [Reclaim hedge: concurrent buy, retries, keep rule](#section-19c)
  - [post_dump_book journal](#section-19d)
  - [Live-bid FAK vs floor FAK](#section-20)
  - [Hypothetical lifecycle: 100-share bag, scrap 50 @ ~3¢, winner redeems](#section-21)
  - [Hypothetical lifecycle: dump under 67¢, kept half optional](#section-22)
  - [What this code does not prove](#section-23)
- [Part IV — The buy/ helpers and pathlog](#part-iv)
  - [Ownership map](#section-24)
  - [buy/mint_sell.py policy helpers](#section-25)
  - [buy/market.py, book.py, chain.py, contracts.py, mint_gas.py, log_archive.py](#section-26)
  - [pathlog.py: public book recorder](#section-27)
- [Part V — Operations, verification and sharp edges](#part-v)
  - [systemd units](#section-28)
  - [Knobs: every key, meaning, and default](#section-29)
  - [Deploy boundary and safe restart](#section-30)
  - [Journal events](#section-30b)
  - [Testing without constructing a live bot](#section-31)
  - [Landmines](#section-32)
  - [Glossary](#section-33)
  - [Source snapshot](#section-34)
- [Part VI — Sequences & redeem](#part-vi)
  - [End-to-end mint sequence](#section-35)
  - [Sell-side sequence (winner / dump / loser)](#section-36)
  - [Redeem path (what exists vs what does not)](#section-37)
  - [State after expiry](#section-38)
- [Changelog](#changelog)

<a id="part-i"></a>
# Part I — Picture

<a id="section-1"></a>
## The opportunity and its limits

Polymarket’s **BTC Up or Down 15m** markets are binary windows. Each window asks whether BTC finishes the quarter-hour up or down relative to that window’s open. Outcome tokens pay **$1** of collateral if that side wins and **$0** if it loses.

A **complete set** is one Up share plus one Down share for the same condition. On-chain Conditional Token Framework (CTF) mechanics let you **split** $N of collateral into N Up + N Down. That is a **mint**, not a directional buy. After the split you hold both legs. Economic edge then comes from:

1. Selling some or all of the **loser** cheaply once the book clearly prices it near zero (and the opposite near one), and
2. **Redeeming** the winner at $1 after resolution, when `redeem_enabled` is on. Code `sell_winner_min` is 0.999 ([§18](#section-18), [§37](#section-37)).

The current live reference is `shares=100`: about $100 mints 100 Up + 100 Down. Code default is 50.

- **Normal win.** With live `sell_scrap_fraction=0.5` the bot scraps 50 loser shares (a live-bid FAK that typically fills at 2–3¢, so about $1.50) and **keeps** the other 50 to resolution. If the favourite wins, the bag nets about +$1.50 before fees ($100 redeem + $1.50 scrap − $100 mint), and the kept 50 expire worthless.
- **Flip with no dump.** If the "loser" flips and wins, the kept 50 pay $50. That keep is paid-for insurance: it halves the scrap income in exchange for a floor under the flip case ([§17b](#section-17b)).
- **Dump.** A held bid under live `sell_dump_below` **0.67** for **0.5s** sells the held leg, and only while the other side's bid stays at or over `sell_dump_other_min` (code **0.30**; not in the 13 Oct list, so a pull takes that default). `sell_dump_also_kept` (code default false; not in the 13 Oct live list) can sell the kept half in the same event. `sell_dump_parallel_kept` (code default **true**) posts that kept leg together with the held leg ([§19](#section-19), [§22](#section-22)).
- **Reclaim.** After a both-sides dump, live `reclaim_enabled` buys about $2 of the first side that holds at 90¢. A concurrent hedge of the other side needs `reclaim_hedge_enabled`, which is not in the 13 Oct list (code default false) ([§19b](#section-19b), [§19c](#section-19c)).

**What this bot is not:** it is not the old hourly FAK entry bot. It does not chase 90–95¢ asks as its entry. The post-loser exit under `sell_dump_below` (code 80¢, live reference **67¢**) is a **held dump**, a sell. The later reclaim is a separate CLOB buy, and its hedge is a separate cheap-side buy ([§19c](#section-19c)). Wallet A does not rest a bid. Resting buys belong to wallet B (`scrapbidder`), which stays off.

**Risk concentration:** sequential mode (`mint_sequential`, code default false) mints one bag at a time, from 30s before the open to 240s after, and only once the previous bag's winner is sold or its window has ended — and, while a reclaim or hedge buy is still open, not until that position is closed ([§12](#section-12)). The 13 Oct live list does not restate `mint_sequential`; the code default is false, and the 3 Oct file had it true. Without sequential mode, `max_open_sets` (code 1) plus one adjacent-window exception caps capacity. A failed relayer mint is blocked for `mint_fail_cooldown_s` (30s) and gives up after `mint_max_attempts` (3) tries so a hot remint loop cannot run. A nearer window in that cooldown, or already at the attempt cap, does not idle the cycle: the next eligible future is selected in the same pass and `mint_attempt` is logged for that slug. After an active bag at start `T`, selection never goes backwards (`start_ts < T+900`). A zero-fail window is preferred over retrying a failed condition while a slot is free. A restart ghost intent stuck at `submitting` with no `transaction_id` is auto-failed after `mint_submitting_timeout_s` (default 90s, `0` disables) so `wait_submit` cannot wedge the desk forever. An operator restart waits for the safe window in [§30](#section-30). Scaling share count scales edge and left tail together.

<a id="section-2"></a>
## Processes, wallet identities and files

Live tree: `/home/ntemusejoel/poly-money-maker` on Google Cloud VM `poly-vm`.

| Unit | Program | Role | Observed |
|---|---|---|---|
| `polymintbot.service` | `mintbot.py` + gitignored `strategy_mint.json` | Atomic mint + sells + reclaim | **Stopped** as of 2026-10-05 (inactive, still enabled at boot). Do not start it unless the operator asks |
| `polypathlog.service` | `pathlog.py` | Public CLOB path recorder (no orders) | **retired**: inactive since 22 Sep 2026 20:00 UTC (clean exit). Still `enabled`, so it would start on reboot; the operator should `systemctl disable polypathlog` |
| `polyscrapbid.service` | `scrapbidder.py` (wallet B) | Opt-in sister bids | **inactive / disabled** — stays off |
| Retired buy / danger / shadow / dense pathlog units | — | — | **stopped / must stay off** |

Wallet roles (do not put secrets in this doc):

- **EOA / signer** — `PRIVATE_KEY` signs relayer PROXY batches and CLOB sells.
- **Funder / proxy** — `FUNDER_ADDRESS` is the Polymarket proxy that holds pUSD and outcome tokens (live watch address historically `0x8222…`).
- **Relayer auth** — API key headers must match the signer address.

Durable local files (gitignored where noted):

| Path | Purpose |
|---|---|
| `strategy_mint.json` | Live knobs (gitignored), re-read every loop tick |
| `strategy_mint.example.json` | Committed template (dry_run, entry and sell off) |
| `positions_mint.json` | Intent state machine + sell flags (compact JSON, rewritten only on real change) |
| `.heartbeat_mint` / `.heartbeat_pathlog` | Liveness stamps (mint file carries `sell` and `mint` parts) |
| `.mintbot.lock` / `.pathlog.lock` | Single-instance flock |
| `STOP_MINT` | If present, both mintbot loops exit and the process stops |
| `mintbot.log` / `pathlog.log` | Append logs; `mintbot.log` rolls at 2 MB into `logs/archive/` |
| `logs/archive/mintbot.log.<UTC stamp>.gz` | Rotated mintbot history, gzipped, never pruned (#219) |
| `logs/oracle_twap.jsonl` | Chainlink TWAP tape; rolls at 20 MB into `logs/archive/oracle_twap.jsonl.<UTC stamp>.gz`, never pruned (was 116 MB unrotated on 30 Sep; the first write after a restart on new code archives it) |
| `.env` | Secrets — never read into chat or commit |

<a id="section-3"></a>
## Repository map and reading order

```
mintbot.py              # live entry: discover → mint → reconcile → sell
pathlog.py              # 15m-only book recorder (service retired; research use only)
strategy_mint.json      # LIVE knobs (gitignored)
strategy_mint.example.json
positions_mint.json     # LIVE state (gitignored)
buy/
  mint_sell.py          # pure sell policy (persist, classify, scrap plan, gates, bag_risk)
  mint_loops.py         # concurrent sell vs mint jobs, intent claim, pending-cash reserve, persist digest
  mint_gas.py           # mint relay gas_limit (estimate + margin, fallback, 650k clamp)
  mint_sequence.py      # opt-in sequential bags: mint range, busy-bag gate, wait/skip bookkeeping
  mint_redeem.py        # opt-in auto-redeem job runner (resolution poll, relayer redeem, retries)
  market.py             # Gamma/CLOB discovery → MintMarket
  book.py               # sized top-of-book
  chain.py              # eth_call balances / prechecks, per-thread keep-alive sessions
  contracts.py          # approve + split calldata; pUSD transfer for the A→B top-up
  oracle_log.py         # Chainlink 60s TWAP tape + bag_view for the (off) late scrap veto
  log_archive.py        # rotate mintbot.log and the oracle tape into logs/archive, gzip off-thread
  sister_bid.py         # wallet B bid policy (scrapbidder only)
  sister_topup.py       # A→B pUSD top-up policy (scrapbidder only)
scrapbidder.py / sister_topup.py   # opt-in wallet B process + top-up script (off)
deploy/
  polymintbot.service
  polypathlog.service   # retired; kept for reference, keep disabled
  polyscrapbid.service  # opt-in, disabled
  DISK_OPS.md
tests/                  # unit tests; do not import mintbot.py wholesale
CURRENT.md / AGENTS.md / TECHNICAL_DESIGN.md
```

Reading order for a new engineer: this file Parts I–II, then `buy/mint_sell.py` and `buy/mint_loops.py`, then `manage_sells` / `run_sell_cycle` / `run_mint_cycle` in `mintbot.py`, then `CURRENT.md` for today’s knobs.

<a id="part-ii"></a>
# Part II — Ideas the code assumes

<a id="section-4"></a>
## Complete sets, CTF split, and why mint ≠ buy

On Polymarket (Polygon), collateral (pUSD) can be split through the CTF / adapter into a pair of outcome ERC-1155 positions for a `condition_id`. **Minting** creates both legs atomically relative to your inventory: after confirmation you should observe roughly `before + shares` on Up and Down.

Buying one outcome on the CLOB is a different trade: you pay the ask for one token and never receive the other. The mint bot’s edge thesis is “pay ~$1 for the pair, sell (part of) the trash, redeem the rest,” not “pick a side at 97¢.”

Gas and batching are abstracted by Polymarket’s **relayer**: the bot builds a PROXY transaction that includes approve plus split, submits it once with an explicit `gas_limit` ([§13b](#section-13b)), then polls relayer state until inventory shows up on-chain.

<a id="section-5"></a>
## Books, FAK, sized bids, and three prices

The CLOB is a central limit order book. A **bid** is what buyers will pay; an **ask** is what sellers want.

A **sized bid** in this codebase is the best bid that still has at least `sell_min_bid_size` (code 1.0; not in the 13 Oct list) size. Thin sub-share prints are ignored so dust at 2¢ does not arm a 100-share scrap. Depth beyond the top level is only logged (`sell_book_depth`), not gated on.

**FAK** (Fill And Kill) / marketable limit sell: take liquidity down to your limit; cancel the rest. A sell FAK matches the **best** bids first, so a FAK with a 1¢ limit against a book of 3¢ / 2¢ / 1¢ fills the 3¢ level first. The loser sweep relies on that ([§17](#section-17)).

Three prices that must not be conflated:

1. **Arm threshold** — loser ≤ `sell_threshold` (code 2¢, live reference **3¢**, and **4¢** when time-to-close is within the last 180s) with opposite ≥ 90¢, or held bid < `sell_dump_below` (code 80¢, live reference **67¢**) with the other side's bid ≥ `sell_dump_other_min` (code 30¢).
2. **Limit posted** — live sized loser bid for the loser sweep (capped at the active threshold, floored at `sell_clob_min_price` 1¢; not `sell_floor`); live sized bid for winner/dump; `min(ask + slippage, max price)` for a reclaim buy.
3. **Average fill** — what actually cleared (usually better than the limit). A sweep posts the live bid, which can be 1¢, and logs `avg_px` in `sell_scrap_sweep`.

**Live-bid FAK:** once a winner/dump path is allowed to fire, the limit is the current sized bid (winner clamped to 0.99), not a stale fixed 0.999 that Polymarket rejects when the book max is 0.99.

<a id="section-6"></a>
## Python shape: values, state and side effects

`buy/mint_sell.py` is intentionally pure-ish: given bids and knobs, return legs, persist decisions, scrap plans and ladder limits. No network. `buy/mint_loops.py` and `buy/mint_gas.py` follow the same rule.

`mintbot.py` owns side effects: HTTP to Gamma/CLOB/relayer, eth_calls, file IO, notifications, systemd process lifetime.

`positions_mint.json` is the durable state machine. Restart must not remint a market already `failed`/`confirmed`/`completed`, and must not forget `sold_loser` / `sold_leg` or a locked scrap target.

Four Python habits recur in the money paths. Each is a one-paragraph aside.

> **Aside — keyword-only arguments.** Many helpers put a bare `*` in the signature: `persist_ready(qualify, *, now_s, armed_ts, persist_s)` or `_fire_loser_scrap(*, token_id, size, floor, ...)`. Everything after `*` must be passed by name (`now_s=now`). The money helpers take several floats in a row (`now_s`, `armed_ts`, `persist_s`; or `floor`, `threshold`, `loser_bid`, `fak_px`). Positional calls would let a swapped pair type-check and run. Keyword-only makes the call site say which number is which.

> **Aside — `Optional` and `None`.** `Optional[float]` means "a float or `None`", and here `None` carries meaning. `up_bid=None` is "no sized bid" (empty book), which is not the same as a 0.0 bid: `classify_loser` treats a missing opposite bid as `wick_unconfirmed`. `sell_loser_armed_at=None` is "not armed". `sell_scrap_target is None` is "no partial plan locked yet". Watch the `float(cfg.get(k) or default)` idiom: `or` also replaces a **zero**. `sell_dump_persist_s`, `sell_cooldown_s` and `sell_scrap_rest_min_ahead_s` used to lose an explicit 0 that way; they now go through `cfg_seconds`, where only a missing, `null`, non-numeric or negative value falls back (negatives also fail validation). Keys read as `or 0.0` (the TTM gates, the oracle window) were always safe at zero.

> **Aside — `Decimal` vs `float`.** Trading math is plain `float` with small epsilons (`+ 1e-12` in comparisons, `round(x, 4)` on prices). CLOB prices sit on a 0.01 / 0.001 grid, so that is enough. Where exactness is a correctness property, the code converts to integers instead: `build_atomic_mint_calls` turns shares into six-decimal pUSD units with `int(round(shares * 1_000_000))` and refuses a value that does not map exactly. `scrap_share_plan` adds `1e-9` before `math.floor` so `100 × 0.5` cannot floor to 49 on a binary rounding hair. Only `buy/oracle_log.py` uses `Decimal` (`json.loads(..., parse_float=Decimal)`), so the audit tape stores oracle prints digit-for-digit.

> **Aside — dataclasses vs dicts.** Immutable value objects are `@dataclass(frozen=True)`: `MintMarket` (discovery), `ContractCall` (calldata), `MintGasPlan` (gas decision, with `relay_arg()` and `as_log()` methods), `OracleBagView`. Frozen means a market snapshot cannot be edited halfway through a tick. The **intent** is a plain `dict` on purpose: it round-trips to `positions_mint.json` as-is, and new keys (`sell_scrap_target`, `sold_dump_at`) appear without a schema migration. The cost is typo-prone `intent.get("...")` access.

<a id="section-7"></a>
## External systems

| System | Use |
|---|---|
| Gamma API | Discover 15m markets / tokens / times |
| CLOB API | Sized bids; FAK sells; resting scrap sells when `sell_scrap_rest_enabled` is on (code default true) |
| Data API | Optional position checks; `redeemable=true` startup sweep (opt-in redeem) |
| Relayer v2 | Submit PROXY mint batch (and opt-in redeem batch); poll `STATE_*` |
| Polygon RPC | CTF balances / inventory confirm; one `eth_estimateGas` per mint or redeem submit; `payoutDenominator` / `payoutNumerators` / `isApprovedForAll` for redeem |
| Polymarket RTDS | Recording-only Chainlink BTC/USD 60s TWAP (`crypto_prices_twap_sixty`) |
| Gamma `/events?slug=` (oracle tape) | Recording-only post-window check of `eventMetadata.priceToBeat` / `finalPrice` against the tape |
| ntfy (optional) | Operator push on mint/sell (thread pool, off the tick) |
| CallMeBot (optional) | WhatsApp danger-zone alert on a scrapped bag (opt-in: scrap fill, held dump fill); see below |

**WhatsApp alerts (`buy/whatsapp_notify.py`).** These need `CALLMEBOT_PHONE` (with country code, e.g. `+353…`) and `CALLMEBOT_APIKEY` in the process environment.
- **Danger zone (`notify_danger_whatsapp`, code default on).** The 13 Oct list does not restate it.
  - Watch starts only after the bag's loser scrap has filled (`sold_leg` set). Watch stops once the bag has dumped or sold its winner (`sold_dump` / `sold_winner`), or once the window has ended.
  - Each sell tick, `danger_tick` gets the same sized best bid on the held leg that the dump reads (`bids[held]`, one fetch per tick). The bid must stay under `notify_danger_px` (0.70) continuously for `notify_danger_hold_s` (5s). A bid at or above the line, or no sized bid, resets the timer.
  - It then logs `danger_zone` (`condition_id`, `slug`, `leg`, `bid`, `threshold`, `hold_s`, `below_s`, `ttm`, `held_shares`, `kept_shares`, `dump_below`, `whatsapp`, `dry_run`). This line is logged even when WhatsApp is off, unconfigured or in dry run.
  - WhatsApp gets one message per bag. The text uses the loaded knobs, for example a held bid under 70¢, the shares still held, and the dump line. `dump off` when `sell_dump_enabled` is false.
  - It is alert-only and never changes the dump or any order. Dedupe is per condition in process memory, so a restart during a still-live, still-low bag can alert once more.
- **Scrap fill (`notify_scrap_whatsapp`, default off).**
  - One message per bag when its loser scrap completes (every `_finish_scrap` path: sweep, ladder, blind, rest fill). If the window ends after a partial scrap, one "partial, window ended" message is sent within 60s of the end instead.
  - Example: `Mintbot scrap: 11:30 bag | sold 100 DN @ 0.02 ($2.00) | 3m12s left | kept 100 | UP bid 0.98`.
  - The price is `sell_fill_px` (falling back to `sell_limit`), the time is the window start in Europe/Dublin, and the bid is the opposite leg's last sized bid.
- **Dump fill (`notify_dump_whatsapp`, default off).** A `Mintbot DUMP: …` line when a held dump fills; the natural follow-up to a danger alert.
- Knobs are hot-reloaded each sell tick. A bad `notify_danger_px` (outside 0–1) or `notify_danger_hold_s` (outside 0–3600) falls back to 0.70 / 5. Dry run sends nothing. Missing env vars make it a no-op with one startup line, `notify_whatsapp_off` (`missing`); otherwise one `notify_whatsapp_on` (last 4 phone digits only).
- It never blocks trading. The sell loop only formats the text and does a non-blocking put onto a bounded queue (50, drop-oldest with `notify_dropped`). One daemon worker does `GET api.callmebot.com/whatsapp.php` with an 8s timeout and retries once after 5s on 429/5xx/no reply. It logs `notify_sent` or `notify_failed` (`status`, `attempts`; redacted error). A bad key comes back as HTTP 203 "APIKey is invalid" and is counted as failed. The API key, the phone and any URL are never logged. All hook code swallows its own exceptions.
- It only reads intent fields already in memory, writes nothing to intents, and dedupes per bag in process memory (a restart can re-send only for a partial scrap that ended in the last 60s).

HTTP goes through per-thread keep-alive `requests.Session` objects (`buy/chain.thread_session`; each `ChainReader` owns one). That was part of the CPU cut in #217.

<a id="section-8"></a>
## JSON as durable memory

Top-level `positions_mint.json` shape (illustration: a 100-share bag after a half scrap):

```json
{
  "intents": {
    "<condition_id>": {
      "status": "confirmed",
      "slug": "btc-updown-15m-…",
      "shares": 100.0,
      "start_ts": 1790754300.0,
      "end_ts": 1790755200.0,
      "up_token": "…",
      "dn_token": "…",
      "before_up": 0.0,
      "before_dn": 0.0,
      "transaction_id": "…",
      "relayer_state": "STATE_CONFIRMED",
      "mint_attempts": 1,
      "sold_loser": true,
      "sold_leg": "up",
      "sold_loser_at": 1790755042.8,
      "sell_filled": 50.0,
      "sell_limit": 0.03,
      "sell_fill_px": 0.03,
      "sell_fill_px_shares": 50.0,
      "sell_scrap_held": 100.0,
      "sell_scrap_fraction": 0.5,
      "sell_scrap_target": 50.0,
      "sell_scrap_keep": 50.0,
      "sell_scrap_outcome": "target_filled",
      "sold_winner": false,
      "sold_dump": false,
      "sell_dump_leg": null,
      "sell_loser_armed_at": 1790755037.1,
      "sell_winner_armed_at": null,
      "sell_dump_armed_at": null,
      "sell_scrap_rest_id": null,
      "chain_reconcile_done": false
    }
  }
}
```

Statuses move roughly: `submitting` → `pending`/`executed`/`mined` → `confirmed_waiting_inventory` → `confirmed` → `completed` (flat after the end), or `failed` on relayer failure. There is no daily-notional key any more; the cap was removed.

Saves are cheap and rare: `commit_state` compares a **digest** (`persist_digest` in `mint_loops.py`) against the last successful `atomic_save`. Cached bids (`last_up_bid`, …) and `updated_at` are ignored, and terminal intents contribute only id + status. A bid-only tick does not rewrite the file. A tick that mutated state and then raised is still saved next tick, because the compare is against the last write, not a per-tick snapshot (#217).

<a id="part-iii"></a>
# Part III — Walking mintbot.py

<a id="section-9"></a>
## Startup, lock, strategy load

`main()` installs signal handlers, acquires `.mintbot.lock`, loads `strategy_mint.json` merged over `DEFAULTS`, validates knobs, builds the market gateway and **two** `ChainReader`s (one per loop, one keep-alive session each), then starts three daemon threads: sell (`run_sell_cycle`), mint (`run_mint_cycle`) and the oracle tape.

`load_strategy` copies only keys that exist in `DEFAULTS`; anything else in the live file (for example a leftover `sell_persist_skip_ttm_s` or `sell_dump_tiers`) is ignored. `validate_strategy` enforces `sell_floor ≤ sell_threshold < sell_opposite_min < sell_winner_min < 1`, `sell_floor ≤ sell_fak_px ≤ sell_threshold`, the same chain for the late pair when both late prices are set, `0 < sell_scrap_fraction ≤ 1`, `0 < reclaim_stop < reclaim_entry < 1`, `reclaim_max_price ≥ reclaim_entry`, mint gas bounds, non-negative second knobs, and `poll_s ≥ 1`. `sell_armed_poll_s` must be ≥ 0.2. Explicit 0 on a seconds knob is kept (`cfg_seconds`); a negative is rejected.

Each loop calls `_reload_cfg`, which re-reads `strategy_mint.json` only when the file mtime changes, so a knob edit still takes effect on the next tick without a restart. If the reload fails validation, the loop logs `strategy_reload_fail` and keeps the **previous** config with `entry_enabled` forced false: sells continue, new mints stop.

Sleep cadence: the sell loop sleeps `poll_s`, or `min(poll_s, sell_armed_poll_s)` while any bag is sell-hot (loser armed, loser sold and dump/winner not done, or a reclaim still watching). Code defaults 5s / 2s. The 13 Oct live list does not restate either sleep. Mint always sleeps `poll_s`. Armed poll does **not** replace persist math. Shared intent writes take `STATE_LOCK`; book / FAK / Gamma / relayer / RPC stay outside that lock (`_io_unlocked`).

Incident `btc-updown-15m-1789905600`: after `loser_done`, next-window mint ran on the same thread and delayed the first dump look ~16s while UP cliffed 97→31. A skip-Gamma bandage (draft PR #193) is unnecessary once the loops are independent.

`dry_run=true` must not submit mints or live orders. Code defaults are `dry_run=true`, `entry_enabled=false`, `sell_enabled=false`. The 13 Oct live list does not restate those three flags.

<a id="section-10"></a>
## Two loops: sell vs mint/discover

Sell and mint are independent jobs (`buy/mint_loops.py`). They share `positions_mint.json` through `STATE_LOCK` / `IntentStore.try_claim_condition`. Neither loop awaits the other's I/O.

**Sell loop** (`run_sell_cycle`):

1. `manage_sells` if `sell_enabled` — books, persist, FAK. Releases the state lock around book GET / inventory RPC / FAK POST.
2. Heartbeat `hot` or `idle`. Sleep `cycle_sleep_s`.

**Mint loop** (`run_mint_cycle`):

1. Auto-fail stale `submitting` intents that still have no `transaction_id` after `mint_submitting_timeout_s`.
2. Reconcile open relayer intents / inventory (RPC outside the lock). `chain_reconcile_action` decides per bag: in-flight statuses always query; a live `confirmed` bag queries (skipped while a sell is hot, to save RPC); an **ended** `confirmed` bag is not queried until `end_ts + 180s`, gets one final read, then `chain_reconcile_done` stops further calls (#217).
3. If any `submitting` remains → wait; if entry disabled → return.
4. Discover series markets; filter eligible; `select_mint_candidate` skips `already_minted`, owned tokens, and backwards windows; `mint_slots_full` → capped.
5. Prechecks (no lock), including the **pending-cash reserve** ([§14](#section-14)); then claim `submitting` under the lock; `submit_mint_batch`; record pending/failed.

Without sequential mode (code default), `sold_loser` frees the `max_open_sets` slot, even when half the loser is kept. When `mint_sequential` is on, the gate is `seq_busy_bag` instead: a sold loser does not free it, and only `sold_winner` (also set by the held dump) or the window end does ([§12](#section-12)). `count_kept_loser_as_open: true` (opt-in, default false) keeps a bag with kept loser shares counted until `end_ts + 120s`, or until the kept leg itself is cashed as the winner: fewer mints, capped open exposure ([§12](#section-12)). In the non-sequential mode the mint loop can claim the adjacent window **while** the sell loop runs dump persist on the previous bag. Do not skip Gamma because a bag is hot.

<a id="section-10c"></a>
## Chainlink TWAP tape (recording only)

`buy/oracle_log.py` runs on a third thread (`mintbot-oracle`) so a slow RTDS or crypto-price GET cannot take a sell or mint tick. It watches intent snapshots for 15m bags (`submitting` through `completed`, not `failed`), from before the window opens until about two minutes after `end_ts`. Gamma reconciliation for a finished window runs later, from about 10 minutes after the end.

The live value is Polymarket's public RTDS relay of Chainlink's BTC/USD **60s TWAP** (`wss://ws-live-data.polymarket.com`, topic `crypto_prices_twap_sixty`). Direct Chainlink Data Streams would need credentials this bot does not use. Rows land in `logs/oracle_twap.jsonl` with `ts`, slug, `condition_id`, window start/end, `source`, `twap`, optional `open_ref`, and `notes`.

**Strike and close (30 Sep fix, #229).** Polymarket settles each 15m window by comparing the Chainlink BTC/USD **60s average** (TWAP) at the close against the same average at the open. The strike (`open_ref`) is the 60s TWAP sample stamped exactly at `window_start`; the close is the one stamped at `window_end`. Gamma's `eventMetadata.priceToBeat` for window N equals its `finalPrice` for N-1, and both equal that boundary sample. Before this fix the strike came from crypto-price `variant=fifteen` `openPrice`, which is a different boundary series: its open/close differ from `priceToBeat`/`finalPrice` by up to ~$40, and the errors chain (crypto open(N) − priceToBeat(N) = crypto close(N−1) − finalPrice(N−1)). That row was stamped `twap_ts=window_start`, so it looked like a boundary print. The tape also deduped samples by second, so a corrected value for the same second was dropped.

- `oracle_open_ref` rows carry `strike_source` (`rtds_twap_at_start`, `crypto_price_open`, or `gamma_price_to_beat`), `capture_delay_s`, and `previous` / `previous_source` when they replace an earlier value. `oracle_window_end` rows carry `close_source` (`rtds_twap_at_end` or `gamma_final_price`). `OracleBagView.open_source` exposes the strike source; the veto log lines print it as `open_source`.
- The RTDS subscribe burst replays about the last minute, so a reconnect shortly after the boundary still delivers the sample (`capture_delay_s` shows how late). A later message for the boundary second with a different value supersedes the first and logs `oracle_strike_revised` (`strike`, `previous`, `delta`). The same value again is a no-op. The tape dedupes on second **and** value, so a revision gets its own `oracle_twap` row.
- No boundary sample by `STRIKE_WAIT_S` (75s) while the window is live: one `oracle_strike_late`, then crypto-price `openPrice` as a labelled fallback (`crypto_price_open`). A boundary sample that still arrives replaces it. crypto-price's close is no longer used; a window without an end-boundary sample by `CLOSE_GRACE_S` (300s) logs `oracle_window_end_missed`.
- **Gamma reconciliation.** `GET gamma-api /events?slug=btc-updown-15m-<start>`, first at end + `GAMMA_FIRST_S` (600s, when `priceToBeat` is usually published) plus jitter, then every `GAMMA_RETRY_S` (120s) while unpublished, at most one request per oracle tick across all windows, and never past end + `GAMMA_GIVE_UP_S` (1h; `oracle_check_missed`). Logs `oracle_strike_check` / `oracle_close_check` (ours, source, official, `diff`, `match` within $0.01). On a mismatch it writes a corrected `oracle_open_ref` / `oracle_window_end` row from Gamma. This runs after the window, so it never reaches a live decision. Bags present after a restart are checked; windows that ended before it are not.

The thread wakes every second while a bag is open. Stored rows are 15s mid-window, 2s near the open and in the last three minutes, and 1s in the last minute and just after the end, so a cold gap cannot skip the open print or the last minute. Before each append, `append_jsonl` calls `log_archive.roll_if_over`: once the file would reach `TAPE_MAX_BYTES` (20 MB) it is renamed to `logs/archive/oracle_twap.jsonl.<UTC stamp>` and gzipped on the same background worker as `mintbot.log`. Archives are never pruned. Only the oracle thread writes this file, so the roll needs no lock. Anything tailing the tape must follow the rename.

**Feed health (30 Sep hardening).** On 29–30 Sep the RTDS socket twice stayed connected but silent (22 min and 100 min) and only recovered on its own, while `_note_silence` logged `stale twap age=Ns` every second. Now:

- **Ping/pong and watchdog.** `run_forever` uses protocol ping/pong (`FEED_PING_INTERVAL_S` 20s, `FEED_PING_TIMEOUT_S` 10s) alongside RTDS's text `PING`. While a bag is tracked, no new sample for `FEED_SILENT_RECONNECT_S` (45s) closes the socket (`oracle_feed_watchdog`). Repeated forced closes back off 45 → 90 → 120s (`FEED_WATCHDOG_CAP_S`); a sample resets that. The feed's own reconnect wait is 1s after a connection that delivered samples, else doubling to `FEED_BACKOFF_MAX_S` (30s). Each reconnect logs `oracle_feed_reconnect` (reason, `last_error`, attempt, backoff), throttled to one a minute with a `suppressed` count.
- **Hot span (#234).** The scrap oracle veto reads this feed, so while any tracked bag is within `FEED_HOT_TTM_S` (360s) of its end the feed runs in hot mode:
  - The watchdog fires after `FEED_SILENT_HOT_S` (5s) of silence and backs off 5 → 10s (`FEED_HOT_WATCHDOG_CAP_S`). `oracle_feed_watchdog` carries `hot`.
  - A connection that delivers nothing redials within `FEED_HOT_BACKOFF_S` (2s), not up to 30s.
  - The post-window Gamma audit, which is blocking HTTP on this thread, waits until no bag is hot.

  Every sample also carries `recv_ts`, the local wall time its frame arrived. `bag_view` exposes it and tape rows log it. The Chainlink `twap_ts` stamp trails arrival by about 1.5–2.5s, so receive time, not `twap_ts`, is the freshness clock.
- **Measured staleness before the hot span (280 windows, 30 Sep–3 Oct).** The stream ticks every second, and 1.07% of gaps are longer.
  - In the last 180s (tape write times), the in-memory value was more than 2s old 0.92% of the time, more than 3s old 0.50% and more than 10s old 0.09%.
  - Silent-socket episodes inside the last 6 minutes (`1791036000`, `1790829000`, `1790762400`) lasted 45–57s, because only the 45s watchdog ended them. The reconnect replay back-filled the missed seconds, so they are invisible in `twap_ts` cadence.
  - Hot mode cuts such an outage to about 6–8s, and the veto falls back (logged) for those seconds.
- **Stall lines.** A stale TWAP (> `STALE_AFTER_S` 20s) logs one `oracle_feed_stall` (`age_s`, `last_error`), a reminder at most every `STALL_REMIND_S` (60s), and one `oracle_feed_recovered` (`duration_s`). `oracle_log_fail` is now deduped per kind, not exact text.
- **HTTP.** crypto-price (strike fallback) and Gamma (reconciliation) each keep one request at a time per window, with 0–3s jitter (`HTTP_JITTER_S`) on the first. crypto-price retries every 20s; a 429 backs off 20 → 40 → 80 → 120s (`HTTP_BACKOFF_CAP_S`). Gamma retries every 120s; a 429 backs off 120 → 240 → 480 → 600s. Both honour a longer Retry-After. Each error kind (`http_400`, `http_429`, ...) is logged once per window per source via `oracle_log_fail`.

These rows go to the tape, and `mintbot` logs them through an `on_event` hook. None of this is read by mint, sell, winner or dump.

`oracle_log_enabled` defaults **false** (example false). The module comment that says the tape defaults on is stale; `DEFAULTS` and the example are false, and a missing live key stays off. When the feed is down the thread logs the stall and keeps going. Mint eligibility, winner cash-out, held dump, reclaim, and loser scrap do not read the tape. Since #245, `_scrap_oracle_gate` ignores the in-memory sample and always returns disabled ([§17](#section-17)). The late-window veto stays off while `sell_late_window_s` is **0** (code default). `sell_oracle_edge_floor_usd`, `sell_oracle_edge_per_ttm`, and `sell_oracle_stale_s` also default to **0**, so raising only the window does not restore the old $25 / 1.5×TTM / 5s-stale veto. `sell_oracle_edge_persist_s` defaults to **3** and only matters when that window is positive. If the veto is ever turned back on, #214 keeps its edge-persist clock running on the kept scrap leg while the loser book is momentarily empty, instead of resetting it.

<a id="section-10b"></a>
## Sync-loop audit (same class as mint stealing the dump cycle)

| Hole | Severity | Status |
|---|---|---|
| Serial `manage_sells → Gamma/mint/submit → sleep` (incident 1789905600, ~16s dump delay) | Critical | **Fixed** — two loops |
| Relayer poll + inventory RPC on the sell thread | High | **Fixed** — mint loop; I/O off the lock |
| Shared sleep after `loser_done` (dump went cold, `poll_s=5`) | High | **Fixed** — sell stays hot through dump/winner exit |
| Skip-mint-while-hot (draft PR #193) | Architecture reject | **Not used** — mint proceeds concurrently |
| `notify()` sync 5s ntfy POST on the tick | Medium | **Fixed** — thread pool |
| New TCP/TLS per HTTP call | Medium | **Fixed** (#217) — per-thread keep-alive sessions |
| `atomic_save` + fsync on every dirty sell tick | Low | **Fixed** (#217) — digest compare, bid-only ticks skip the write |
| Chain polls for ended bags every cycle | Low | **Fixed** (#217) — one final read at `end_ts + 180s` |
| Multi-intent serial in `manage_sells` (N dump FAK then N+1 books) | Medium | Leftover — one intent's FAK can delay the other's look |
| Sequential chain prechecks (5+ RPCs) | Low-Med | Leftover — mint-only latency |
| Relayer submit/poll timeouts 15–20s | Low-Med | Leftover — mint-only; sell continues |
| CLOB `update_balance_allowance` on every FAK | Low | **Fixed** — once per token after mint confirm; again only on a balance/allowance reject |
| Data API `positions` after Gamma | Low | **Fixed** — one check immediately before a live mint submit, not every tick |
| Sequential reconcile per pending intent | Low | Leftover — mint-only |
| pathlog JSONL / Gamma I/O | n/a | Separate process; does not block mintbot |
| Redeem vs sell | Separate thread | Opt-in `redeem_enabled` runs on `mintbot-redeem`; sells stop at `end_ts` and never wait on it |
| Sequential UP/DN `/book` | Already fixed | One `/books` POST, parallel GETs if that body fails; book timeout `book_timeout_s` (1.2s) |

<a id="section-11"></a>
## Eligibility: not-yet-open 15m windows

`eligible_markets` keeps markets that:

- have **not** started (`start_ts > now`),
- open within `(enter_min_ttm_min, enter_max_ttm_min]` minutes (default 0–45 so the window after a bag booked near 30m out stays visible — two 15m steps past a market that is about to open),
- are active, not closed, not neg-risk,
- optionally `accepting_orders`.

**The bot never mints a live (already open) window** in the default (non-sequential) mode. That is why missing the adjacent lookahead used to skip an entire quarter-hour: by the time the prior bag expired, the next market was already open and ineligible.

**Sequential mode (`mint_sequential`, code default false; `buy/mint_sequence.py`).** The 3 Oct file had it true. The 13 Oct list does not restate it. When the flag is on, `seq_eligible_markets` replaces `eligible_markets`:

- A market is eligible only in `[start - mint_seq_lead_s, start + mint_seq_cutoff_s]` (30s / 240s). A window that is already open is allowed up to the cutoff.
- It must also be active, not closed, not neg-risk, optionally `accepting_orders`, and not ended.
- `enter_*_ttm_min` is not read.

A split is price neutral, so minting at the open costs the same as 14 minutes earlier. The sell path keys off `end_ts` and the books, not the mint time.

<a id="section-12"></a>
## Capacity: max_open_sets and adjacent lookahead

`open_intent_count` counts intents in `ACTIVE_STATUSES` whose market has not been expired for >120s, **excluding** intents with `sold_loser` / `sold_leg`. Winner-only redeem holds, including a bag that kept half its loser, must not consume the mint slot. That is the default.

`count_kept_loser_as_open` (code default **false**, example false, not in the 13 Oct list) changes one thing: with it true, a sold-loser bag whose partial scrap kept shares (`kept_loser_open`: `sell_scrap_keep > 0`, and the kept leg has not been cashed as the winner) still counts toward `max_open_sets` in `open_intent_count`, `mint_slots_full` and `mint_discovery_capped` until the usual `end_ts + 120s` cutoff. A held-leg dump does not release it; the kept shares are still open. The trade-off: at `max_open_sets=2` and fraction 0.5, each scrapped bag keeps holding a slot, so fewer windows get minted (less scrap income), in exchange for never carrying more than `max_open_sets` bags with live loser exposure at once. The adjacent-window lookahead still applies.

`mint_slots_full(state, cfg, now, candidate_start_ts)`:

- If fewer than `max_open_sets` full bags → not full.
- Else allow **only** the adjacent next window: `soonest_full_end ≤ candidate_start < soonest_full_end + 900`.
- If we already hold that next window, or the candidate is further out → full.

Code default: `max_open_sets=1`. The 3 Oct file set 2 and ignored it because sequential mode was on. The 13 Oct list does not restate either key. With `1`, holding 1:30–1:45 still permits minting 1:45–2:00 beforehand; it does **not** permit minting 2:00–2:15 while 1:30 is still a full bag. A nearer candidate that does not fit the cap is skipped in the same pass so that adjacent window is still selected.

Because the adjacent lookahead mints about 14 minutes before the open, two bags overlap and tie up two bags of pUSD, even at `max_open_sets=1`. That overlap is why the 3 Oct file set sequential mode. The 13 Oct list does not restate the flag.

**Sequential capacity.** `mint_sequential` defaults false. The 3 Oct file had it true; the 13 Oct list does not restate it. With the flag on, `seq_busy_bag` replaces `mint_slots_full` in `select_mint_candidate` and in `_claim_mint_intent`.

- **Busy bag.** A busy bag is any other active, non-dry intent whose window has not ended and whose `sold_winner` is not set, or whose `seq_exit_open` is still true. The held dump also sets `sold_winner`. That flag stays true through the reclaim entry watch (`reclaim_hot`), a posted, in-flight, or uncertain buy, any `reclaim_filled` shares, the bought position, and a latched stop, until `reclaim_stopped` and the stop sell has returned. An open reclaim hedge buy stays busy even after that stop, including while a hedge refire is in flight (`reclaim_hedge_retrying`). A kept hedge does not. A sold loser alone does not free the gate, because the winner's capital is still in tokens. An ended window or a settled status does not stay busy. The mint cash check adds the open hedge's outstanding USDC, including a refire clip, to `pending_mint_reserve`.
- **Previous bag busy.** A busy bag gives `seq_wait_prev` and logs `mint_seq_wait_prev` (throttled to 30s per window).
- **Cash short.** Short cash (`mint_cash_block`, including `pending_reserve`) gives `seq_wait_cash` and logs `mint_seq_wait_cash`. It is retried every `poll_s`.
- **Skip.** After the cutoff, `_log_seq_skips` logs one `mint_seq_skip` per unminted live window, with the last wait reason and how long it waited.
- **Timeline.** A winner cashed before the end lets the next bag mint at start − 30s. An unsold winner releases the gate at `end_ts`, and the cash comes back by redeem.
  - On-chain resolution measured 53–87s after the end.
  - The relayer redeem adds about 30–60s.
  - The 240s cutoff covers that with margin.

In-memory wait records live in `_SEQ_WAITS`, so a restart can log one extra skip line.

<a id="section-13"></a>
## already_minted: failed remint after cooldown

```text
confirmed / completed / ACTIVE_STATUSES → always blocked
failed → blocked while now < last_fail_ts + mint_fail_cooldown_s (30s)
failed → blocked for the rest of that condition if mint_attempts >= mint_max_attempts (3)
failed → eligible again after cooldown if attempts remain
selection → skip start_ts < latest held bag + 900s; prefer a zero-fail window over a retry; otherwise the next future in the same pass
```

A relayer `STATE_FAILED` marks the intent `failed` and persists `errorMsg` / tx hash when the API returns them. Without counting `failed` at all, the bot reminted the same `condition_id` in a hot loop (seen on the 2:15 window). Incident `btc-updown-15m-1789805700` then showed the opposite bug: `failed` blocked forever, so after `STATE_FAILED` the desk sat idle for the rest of the 15m window. Cooldown + max attempts is the middle path.

<a id="section-13b"></a>
## Relay hub: internal transaction failure

The most common typed mint failure in the Sep 19–20 2026 trial was the opaque relayer `errorMsg` **`relay hub: internal transaction failure`**.

What it is (and is not):

- **Not** a declared Polymarket status-page outage (green while we saw it).
- Often the **outer** Polygon tx into RelayHub **succeeds**, while the **inner** relayed call reverts (`RelayedCallFailed`) — so no CTF inventory lands.
- In our cases it was **not** explained by low balance, market-not-ready, or duplicate submits (distinct tx hashes).
- Confirmed cause (Sep 2026 traces): the inner CTF ERC-1155 transfer runs out of gas. `submit_mint_batch` omitted `gas_limit`, so `build_proxy_transaction_request` signed the library default `DEFAULT_GAS_LIMIT` of 500_000. Position-id derivation loops ~15k gas per iteration; markets at 7+ iterations exceed that stipend. Retrying the same market fails the same way because the iteration count is a function of `conditionId`. `py_builder_relayer_client.gas` documents a relay-hub budget of ~650k total. The mint path now estimates the factory call and signs `gas_limit` with a 15% margin, falling back to 650k and clamping to `min(mint_gas_cap, 650000)` (#221).

Trial shape (order of magnitude, not a SLA claim):

- ~19 exact matches of that `errorMsg` over ~26h ≈ **6 windows × 3 retries** (+ one singleton), not nineteen independent daily sprays.
- Overall mint confirm rate ~**74%** (85/115); this typed fail ~**17%** of attempts in that window.
- Bot response: mark `failed`, persist `errorMsg`, wait `mint_fail_cooldown_s` (30s), remint up to `mint_max_attempts` (3), then skip that condition for the rest of its life. While it is cooling or exhausted, the same cycle mints the next eligible future instead of idling. `enter_max_ttm_min=45` keeps that next window visible after a bag booked about 30m out.

Operational stance: the reproducible fix is the explicit mint `gas_limit` above. Sister top-up stays on the library default; a single pUSD transfer is far under 500k. Do not estimate gas on the sell loop. The 13 Oct list does not name a `mint_gas_*` key. A missing key keeps the code default.

<a id="section-14"></a>
## Mint, line by line: precheck, reserve, claim, submit

This is the money path from "a candidate was picked" to "an intent is `pending`". Excerpts are trimmed from `run_mint_cycle`.

**1. Prechecks, no lock held.**

```python
if not chain.has_contract(str(cfg["pUSD_address"])): return "no_pusd_contract"
if not chain.has_contract(str(cfg["standard_adapter_address"])): return "no_adapter"
if chain.outcome_slot_count(str(cfg["ctf_address"]), pick.condition_id) != 2:
    return "not_binary"
balance = chain.pUSD_balance(str(cfg["pUSD_address"]), funder_cs)
```

Four eth_calls. They run without `STATE_LOCK`, so a slow RPC cannot stall the sell loop.

**2. Reserve cash already promised to in-flight mints (#212).**

```python
with STATE_LOCK:
    reserved = pending_mint_reserve(state)          # sum of shares for submitting/pending/executed/mined/confirmed_waiting_inventory
block = mint_cash_block(balance, shares, reserved)  # None when balance - reserved >= shares
```

The on-chain pUSD balance still shows cash that a submitted split has not consumed yet. In the old two-bag mode (`max_open_sets=2`, 100-share bags), two mints could be in flight within seconds of each other. Without the reserve, both saw $150 and both submitted $100, and one failed on-chain. Sequential mode, when the flag is on, mints one bag at a time. The reserve still guards a restart or a retry either way. `mint_cash_block` returns `reason="pending_reserve"` (log `mint_skip_pending_reserve`, heartbeat `pending_reserve`) when the shortfall is only the reservation, or `no_balance` when the wallet is simply short. A confirm, failure or stale-submit timeout drops the intent out of the reserved set.

**3. Existing inventory guard.** `before_up` / `before_dn` are read from chain; any balance above `position_tolerance` returns `existing_position`. Those two numbers are also stored on the intent: reconcile later confirms `observed ≥ before + shares`.

**4. Calldata.** `build_atomic_mint_calls(..., shares=100.0)` returns two frozen `ContractCall`s: `approve(adapter, 100_000_000)` and `splitPosition(pUSD, 0x0, conditionId, [1, 2], 100_000_000)`. `shares × 1_000_000` must be an exact integer ([§6](#section-6) aside).

**5. Claim under the lock.** A fresh intent dict with `status="submitting"`, `mint_attempts = prev + 1`, `transaction_id=None` goes through `_claim_mint_intent`: re-check `already_minted`, re-check `mint_slots_full`, then `IntentStore.try_claim_condition`. The re-checks matter because the lock was released during prechecks. `atomic_save` persists the claim **before** any network submit, so a crash mid-submit leaves a `submitting` ghost that the 90s timeout later fails.

**6. Submit.** `submit_mint_batch(calls, metadata=…, rpc=chain._rpc, gas_margin=…, gas_fallback=…, gas_cap=…)`:

1. Load `PRIVATE_KEY` / `FUNDER_ADDRESS`; refuse a relayer key address that does not match the signer.
2. `GET /relay-payload?type=PROXY` → nonce + relay address.
3. Encode the proxy calls.
4. `choose_mint_relay_gas`: one `eth_estimateGas` (signer → proxy factory). `plan_mint_gas` returns a frozen `MintGasPlan`: `ceil(estimate × 1.15)`, or `mint_gas_fallback` (650k) if the estimate failed, clamped to `min(mint_gas_cap, 650_000)`. `plan.relay_arg()` is the decimal string the relayer library wants; `plan.as_log()` is the log payload.
5. Build and sign the proxy request with `gas_limit=plan.relay_arg()`; require derived `proxyWallet == FUNDER_ADDRESS`.
6. `POST /submit` with relayer auth headers.
7. Return `(transactionID or None, error or None, gas_log)`.

**7. Record.** Success: `transaction_id`, `submitted_at`, `status="pending"`, save, log `mint_submitted` with `gas_limit`, `gas_estimate`, `gas_clamped`, `gas_source`. Failure: `mark_intent_failed` (sets `last_fail_ts`, `errorMsg`), save, log `mint_submit_fail` with the same gas fields, ntfy high priority.

<a id="section-15"></a>
## Reconcile: relayer state → inventory confirm

While status is submitting/pending/executed/mined, poll relayer:

- `STATE_FAILED` / `STATE_INVALID` → `failed` (persist `errorMsg` and tx hash; set `last_fail_ts`)
- `STATE_CONFIRMED` → `confirmed_waiting_inventory`
- mined/executed intermediate states update accordingly

Then eth_call CTF balances (subject to `chain_reconcile_action`, [§10](#section-10)). When Up and Down each reach `before + shares` within tolerance → `confirmed`, log `mint_confirmed`, notify. More than 120s after the end, a read that finds both legs flat marks `completed`. For ended `confirmed` bags that read happens once, at `end_ts + 180s`.

<a id="section-16"></a>
## Sell path overview

`manage_sells` runs only for intents in `confirmed`, `confirmed_waiting_inventory`, `mined` or `executed`, and only while `sell_window_open` (strictly before `end_ts`). For each intent it fetches sized Up/Down books once, caches the bids, then evaluates exits **in this order**:

1. **Winner cash-out** (rich bid path, [§18](#section-18))
2. **Held-leg dump** (only after loser sold; weak bid path, [§19](#section-19))
3. **Loser scrap** (cheap bid + rich opposite, [§17](#section-17) / [§17b](#section-17b))
4. **Reclaim** (only after a both-sides dump, and only when `reclaim_enabled`, [§19b](#section-19b))

`post_dump_book` is written after that work, from the books already fetched ([§19d](#section-19d)).

`sell_cooldown_s` (code 3s; not in the 13 Oct list) suppresses the next FAK after any attempt on that bag.

The first tick after the window closes does three things for that bag: cancel a live scrap rest (`window_end`), emit `sell_scrap_outcome window_end` if a partial plan never finished, and emit one **`bag_risk`** line. `bag_risk` is log-only (#222). It carries the scrapped leg, `ttm_at_scrap`, scrap average price, loser best-bid size at scrap, the held leg's minimum bid (and when), seconds the held bid spent below 0.80 / 0.65 / 0.50, the time-weighted mean `1 − held_bid` after scrap, and dump TTM / price if a dump fired. Its `partial` field means "this process first saw the bag already scrapped" (a restart mid-bag), **not** a partial scrap. The counters live in memory; a failure there is swallowed and never touches orders.

<a id="section-17"></a>
## Loser scrap: arm, persist, time gate, floor sweep

Values below are **code default / live**.

**Arm (`classify_loser`).** One leg's sized bid ≤ the active threshold and the other leg's sized bid ≥ `sell_opposite_min` (0.90). Code threshold is 0.02. The live reference is **0.03**, and **0.04 in the last 180s**. Both cheap → `both_cheap`, no arm. Cheap leg with a weak or missing opposite → `wick_unconfirmed`, no arm. `scrap_active_prices` picks the pair each tick: when `sell_late_price_window_s` is positive, both `sell_threshold_late` and `sell_fak_px_late` are set, and ttm is known and at or under the window, the arm, the persist check, and the sweep cap use the late pair. A zero window, a missing late price, or an unknown ttm stays on the base pair. The live reference names the late threshold (0.04) and the 180s window. The late FAK price has to be set as well or the pair never switches; that FAK price is not in the 13 Oct list. An arm that still qualifies under the new cap keeps `sell_loser_armed_at`. The blind print stays `sell_scrap_blind_px`. This window is not `sell_late_window_s` (the oracle veto).

**Time gate (#222).** `sell_scrap_max_ttm_s` (code **0**, off; example **600**). The 3 Oct file used 360; the 13 Oct list does not restate it. While seconds-to-close is above a positive cutoff, `scrap_time_gate_open` is false and the arm, the persist clock, and every scrap fire (sweep, blind, new rest) are blocked. A cheap leg seen while gated logs `sell_scrap_time_gated` (`condition_id`, `slug`, `leg`, `bid`, `ttm`, `cutoff`) at most once per 15s per condition/leg. Unknown TTM (no `end_ts`) leaves the gate **open**, the opposite of the dump gate. Because the arm itself is blocked, persist starts only once TTM ≤ cutoff: a bid that was cheap earlier still waits the full persist after the gate opens.

**Persist.** `loser_scrap_persist_s`: `sell_persist_s` (code **5**) normally, `sell_persist_last_min_s` (code **2**) when `0 < TTM ≤ sell_persist_last_min_window_s` (code **60**). The 3 Oct file used 3 / 2 / 90; the 13 Oct list does not restate them. That last-minute clock applies right through close; the old late-TTM skip is gone (#218) and a leftover `sell_persist_skip_ttm_s` is ignored. `sell_persist_skip_when_sized` (code false) would skip persist when depth at the first rung covers the order; it is off, so a sized book waits too. The effective value is recomputed each tick, so an arm started on the longer clock can fire on the shorter clock without resetting `sell_loser_armed_at`.

**Empty book keeps the arm.** `loser_persist_ready` wraps `persist_ready`. If the loser book vanishes after arm (opposite still ≥ min or also empty), or the last FAK came back `no orders found`, the arm survives as `empty_keep_arm` / `empty_fak_keep_arm` instead of resetting.

**Fire (only when `why_l` is `ready`/`immediate`, gate open, not cooling, not already sold, no live rest).**

```python
fire_action, fire_reason = sell_fire_decision(
    "loser", bid=bids.get(loser), opposite_bid=bids.get(opp_leg),
    threshold=thr, floor=floor, opposite_min=opp_min,
)
```

Persist-ready is not enough; the book is re-checked on the same tick's snapshot. Out of range logs `sell_cancel_out_of_range` and does not POST (empty loser book keeps the arm; a visible bid that left range resets it). In range:

```python
size, latch = _sell_inventory(chain, ctf, funder_cs, l_tok, shares, tol,
                              "seen_loser_inventory", intent)
```

`_sell_inventory` sizes from tracked inventory (minted shares minus recorded fills) and does not `balanceOf` before the post. `inventory_latch` still distinguishes `await_inventory` (not yet confirmed and no fill seen: skip) from `already_flat` (confirmed remainder under tolerance: finish without a POST) from `has_inventory`. A CLOB reject for balance/allowance refreshes allowance once, reads the chain once, resizes, and retries immediately. Then the size is clipped by the partial plan ([§17b](#section-17b)) and `_fire_loser_scrap` posts. There is no fixed sleep between FAK attempts. Each sell logs `trigger_to_send_ms` and `send_to_response_ms`.

**The order: one live-bid sweep.** `sell_scrap_sweep_enabled` defaults true. `loser_scrap_post(sweep=True, …)` returns one FAK at `scrap_live_bid_limit`: the live loser bid, capped at the active threshold (3¢, or 4¢ in the last 180s) and floored at `sell_clob_min_price` (1¢). `sell_floor` does not clamp it up. Bag `btc-updown-15m-1791215100` is why: a 9¢ floor posted `limit>=0.090` into an 8¢→1¢ book about 45 times. Because a sell FAK matches the best bids first, a 1¢ limit still takes 3¢ then 2¢ then 1¢ in one round trip. Each retry tick posts the new live bid. The fill is `sell_scrap_sweep` (`limit`, `size`, `avg_px`, `offered`, `status`), after one `sell_book_depth` line. A remainder waits for the next fire. With the flag false, `loser_ladder_limits` walks every 1¢ from `min(sell_fak_px, live bid)` down to `sell_floor`, each rung clipped to displayed depth. The flag is read every tick.

**Done.** `done = dry_run or balance_flat or sold_total >= post_size - tol`. On done, `_finish_scrap` sets `sold_loser=True`, `sold_leg`, `sold_loser_at` once, resets the oracle arm, and (if a plan exists) logs `sell_scrap_outcome`. `sell_filled` accumulates shares. `sell_limit` still stores the **last limit posted** (the live bid under sweep, which can be the 1¢ floor). The real price goes to `sell_fill_px`: `record_fill_px` folds each fill's `takingAmount / size` into a share-weighted average across sweep, ladder, blind and rest fills, with `sell_fill_px_shares` counting the priced shares. A reply with no `takingAmount` is skipped rather than guessed, and a rest fill (the order poll reports size, not price) is recorded at the rest's own limit. `recorded_fill_px(intent, "sell_fill_px", "sell_limit")` is what the cheap-winner gate and `bag_risk` read; it falls back to `sell_limit` only for state written before this field existed. Then `sell_loser_done` (now with `avg_px`) and ntfy.

**Misses.**

- Blind FAK: on `empty_keep_arm` / `empty_fak_keep_arm`, `loser_blind_fak_due` fires a FAK at `sell_scrap_blind_px` (0.01 / 0.01 default) no more often than `sell_scrap_blind_backoff_s` (3s), if inventory is present and no rest is live. Logs `sell_scrap_blind`. Same time gate and same partial size clip.
- Post-miss rest: after a FAK returns `no orders found` while still armed, `_place_scrap_rest` posts a resting SELL at `scrap_rest_px(sell_scrap_rest_px, live or last-seen loser bid)` = the lower of the two, GTD when expiry is ≥ `sell_scrap_rest_min_ahead_s` (180s) ahead, else GTC (`resting_tif`). It is cancelled on fill, window end, or the loser no longer qualifying. The `oracle_blocks` cancel is still in the rest action, and it does not fire: `_scrap_oracle_gate` always returns disabled. An empty book alone does not pull it. Code default `sell_scrap_rest_enabled` is **true** (rest ceiling 0.02). The 3 Oct file had the rest off; the 13 Oct list does not restate it.

**Late-window oracle veto: off.** `sell_late_window_s` is 0 in code (not in the 13 Oct list), so `in_late` is false and `late_oracle_scrap_ok` returns `outside_late_window`. The floor / per-TTM / stale knobs are 0 as well, so re-enabling only the window does not bring back the old $25 / 1.5×TTM / 5s-stale rule. The helper still implements that rule when the arguments are passed explicitly. With these values, CLOB gates only.

**Scrap oracle veto (#234, hard-off since #245).** `_scrap_oracle_gate` ignores `cfg` and always returns `(False, "disabled", {})`. The sell loop still calls it on the arm, again before the sweep, and again before a blind FAK, so `scrap_veto` is always false. The flag, the $5 margin, the live Chainlink print, and the stale fallback do not block the arm, the persist, the sweep, the blind FAK, or a rest, and they do not cancel a resting scrap. Setting `scrap_oracle_veto_enabled` true does not restore the block. The knobs stay in `DEFAULTS` (`enabled` false, `usd` 5.0, `stale_s` 3.0, `use_live` true) and are still loaded. The pure helper `scrap_oracle_veto` in `buy/mint_sell.py` still implements the old rule — Up blocked while `twap − strike > −threshold`, Down while that margin is `< +threshold`, a stale reading drops out — and the unit tests cover that helper. The sell path does not call it, so `scrap_oracle_veto` and `scrap_oracle_stale` are not emitted, and a scrap fill does not carry oracle margin fields from this gate. Dump, `sell_dump_also_kept`, winner, reclaim, and mint never used it. The late-window veto above stays off. The rule was added after `btc-updown-15m-1791039600` (3 Oct, 16:00–16:15 IST), when 100 Up were scrapped at 2–3¢ with 31s left while the 60s TWAP sat $0.42 above the strike. Up won, and the bag lost about $97. #245 then disabled the gate so a stale tape read could not sit on the sell tick.

Wallet A does not rest a bid. A reclaim FAK buy is the exception, and only after a both-sides dump ([§19b](#section-19b)).

<a id="section-17b"></a>
## Partial loser scrap: sell_scrap_fraction

`sell_scrap_fraction` (code **1.0**, live reference **0.5**), #223. At 1.0 nothing below applies and the whole loser is scrapped, same posts as before. Below 1.0 the bag scraps a locked share count and keeps the rest to resolution. At 100 shares and 0.5 that is sell 50, keep 50.

**Plan lock at the first fire.**

```python
def scrap_share_plan(held, fraction):
    if frac >= 1.0 - 1e-12:
        return held_f, 0.0
    target = float(math.floor(held_f * frac + 1e-9))   # whole shares
    return target, held_f - target
```

`_scrap_post_shares` calls `_lock_scrap_plan` the first time a scrap would post (sweep, ladder, blind, or rest). `held` is the tracked loser balance at that moment. At the live reference (100 shares, fraction 0.5) the lock writes `sell_scrap_target` 50, `sell_scrap_keep` 50, `sell_scrap_held` 100, `sell_scrap_fraction` 0.5 onto the intent and logs **`sell_scrap_plan`** once. Later fires reuse the stored numbers even if the fraction knob changes mid-bag.

**Order size on every fire.**

```python
def scrap_order_shares(*, target, keep, filled, inventory):
    remaining = max(0.0, target - filled)
    room = max(0.0, inventory - keep)
    return min(remaining, room)
```

Two caps: never more than what is left of the target, and never so much that the balance would dip into the keep. `filled` is `sell_filled`, updated by sweep, ladder, blind and rest fills.

**When the scrap counts as done.** `scrap_target_met` returns true when `filled ≥ target − tol` (`target_filled`) or a known balance is ≤ `keep + tol` (`balance_at_keep`, or `flat` when keep is 0). If a fire computes an order size under 0.01, the bag is finished without a POST using that reason. Either way `_finish_scrap` sets `sold_loser` / `sold_leg` exactly as a full scrap does, so the mint slot frees and the held dump becomes eligible.

**Outcome line.** `_log_scrap_outcome` writes **`sell_scrap_outcome`** once per bag that has a plan: `held`, `fraction`, `target`, `keep`, `filled`, `outcome` ∈ `target_filled`, `balance_at_keep`, `flat`, `rest_filled`, `dry_run`, or `window_end` (the window closed before the target filled). The 30 Sep log (100-share bags, fraction 0.5) showed the expected triple each bag: `sell_scrap_plan` (100 → 50/50), `sell_scrap_sweep` (size 50, avg 0.02–0.03, `matched`), `sell_scrap_outcome target_filled`. The current live reference is the same 100 / 50 / 50 shape. The 3 Oct 200-share file was 200 → 100/100.

**What happens to the kept shares.** Nothing sells them on the scrap, blind or rest paths. Two exits remain:

- **Held dump.** With `sell_dump_also_kept` on (#232), the held dump also sells them in the same event ([§19](#section-19)). Code default is false. The 3 Oct file had it true; the 13 Oct list does not restate it. With it off, the dump sells only the *other* leg.
- **Winner path.** The winner path can sell them only if that leg's sized bid reaches the base `sell_winner_min` (code 0.999). The cheap 0.99 winner gate never applies to them. `kept_leg_below_winner_min` enforces that and logs **`sell_keep_winner_blocked`** once per bag. Even at the base minimum, the winner FAK size on the kept leg is capped at `sell_scrap_keep`. Unless a dump fires, the kept shares sit to resolution: $0 if the favourite wins, $1 each if the scrapped leg flips and wins. Redeem, when enabled, then redeems them with the rest of the bag ([§37](#section-37)).

<a id="section-18"></a>
## Winner cash-out: held for redeem in practice

`sell_winner_min` is **0.999** in code. The 3 Oct file used 0.9995, which a 0.001 tick cannot print. Unconditional 0.99 cash-out was rejected: it cuts margin versus redeeming at $1.

**Cheap-loser gate.** If `sold_loser`, the recorded loser fill (`sell_fill_px`, else `sell_limit` for old state) ≤ `sell_winner_cheap_if_loser_le` (code 0.03) **and** that fill + `sell_winner_min_cheap` > 1.0, then `effective_winner_min = min(winner_min, sell_winner_min_cheap)`. The 3 Oct file set `sell_winner_cheap_if_loser_le = -1.0` (and `sell_winner_min_cheap = 0.999`), which forces `loser_above_cheap_gate`. That pair is not in the 13 Oct list, so the code defaults apply unless the live file still sets them. Before 30 Sep the gate compared the posted limit rather than the real fill ([§17](#section-17)).

**Fire.** `winner_cashout_leg` picks the unique leg whose sized bid is ≥ `effective_winner_min`. If that leg is the kept loser leg, the kept-leg block above applies. The same `persist_ready` clock (`sell_persist_s`) must hold, then `sell_fire_decision("winner")` re-checks. `_sell_inventory` gives the size (capped at `sell_scrap_keep` on the kept leg). The limit is the live bid clamped into the CLOB range:

```python
posted, clamped, why = winner_sell_limit(live_px, clob_max=0.99, clob_min=0.01)
```

A 0.99 sell FAK still fills resting 0.995–0.999 bids; `sell_winner_limit_clamped` is logged when live > posted. Full fill sets `sold_winner`.

**Practice.** Books near resolution quote on a 0.001 tick, so the highest possible sized bid is 0.999. Code `sell_winner_min` is 0.999, so a 0.999 print can fire. The 3 Oct file used 0.9995, which that tick cannot reach, and closed the cheap gate with `sell_winner_cheap_if_loser_le = -1`. Neither of those is in the 13 Oct list. After `end_ts` the sell loop stops. Redeem, when `redeem_enabled` is on, runs on `mintbot-redeem` from `end_ts + redeem_min_after_end_s` ([§37](#section-37)). The 13 Oct list does not restate `redeem_enabled` (code default false; the 3 Oct file had it true).

<a id="section-19"></a>
## Held-leg dump: trigger, persist, fresh-bid FAK refire, kept side

A sell-side circuit breaker added 19 Sep 2026. It is **not** the reclaim and **not** the reclaim hedge.

Preconditions (all required):

- `sell_dump_enabled` (code true)
- `sold_loser` (or truthy `sold_leg`); a partial scrap counts once its target is met
- not already `sold_dump` / `sold_winner`
- `sold_leg` is `"up"` or `"dn"`, so the held leg is the other one
- sized held bid is not `None` and `< sell_dump_below` (code **0.80**, live reference **0.67**)
- the other side's sized bid is `>= sell_dump_other_min` (code **0.30**) on that same tick. `0` or `null` disables the check. While it is on, a missing other bid does not qualify. The books are the ones already fetched for the tick.
- `dump_time_gate_open(ttm, sell_dump_max_ttm_s)` (code **0**, off; example **240**). With a positive cutoff, the dump arms and fires only when seconds-to-close ≤ cutoff. Above it, `sell_dump_armed_at` is not started and an in-progress persist is cleared, so the full persist must elapse again inside the window. Unknown TTM counts as **closed**. A blocked arm logs `sell_dump_time_gated` at most once per bag per 15s. The 13 Oct list does not restate this cutoff.
- both the held-bid trigger and the other-side minimum hold together for `sell_dump_persist_s` (code **2**, live reference **0.5**). If either fails on a tick, `sell_dump_armed_at` resets the same way a held-bid recovery does, and the full persist starts again only when both are true together. `sell_dump_persist` logs `other_bid` and `other_leg`. When the held bid is still under the trigger and the time gate is open but the other side does not confirm, the line is `why=other_low`. `sell_dump_persist_last_min_s` (code null, meaning the same as `sell_dump_persist_s`; live reference **0.5**) replaces that persist only when `0 < ttm ≤ sell_dump_persist_last_min_window_s`. The window's code default is **0**, which leaves the last-minute clock off. The 13 Oct list sets the last-minute persist and does not restate the window, so this document does not claim a live last-minute dump window. The choice is re-evaluated every tick through `effective_dump_persist_s`. `sell_dump_armed_at` is not reset when the clock changes. After market end it falls back to `sell_dump_persist_s`.
- at fire, `sell_fire_decision("dump")` still sees bid `< sell_dump_below`, else `sell_cancel_out_of_range`. It does not look at the other side again.
- not in sell cooldown

**FAK refire (`_run_dump_fak_with_refire`).** One live-bid FAK, then one fresh-bid FAK per retry. There is no price ladder on this path.

1. `_sell_inventory` on the held token. `already_flat` sets `sold_dump` / `sold_winner` with note `already_flat` and no `sell_dump_leg`.
2. First shot: one FAK at the fire-time sized bid. A single limit, the bid just read.
3. If shares remain, each retry **refetches that leg** and posts one FAK at the new best bid, up to `sell_dump_fak_retries` (code **2**) in the same tick. A partial fill (anything at or above tolerance) refires at once. A zero fill still needs `dump_fast_retry_eligible` (`no orders found`, or kill/cancel). `sell_dump_fast_refire_attempt` logs `sold` (shares already filled) and the new bid. Stop on an empty or non-positive book (`sell_dump_fast_refire_stop reason=empty_book`), a non-retryable zero fill (`non_retryable_status`), or exhausted retries. These refires are not re-checked against the TTM gate or `sell_dump_other_min`. There is no added sleep.
4. `sell_dump_ladder_step` (0.04) and `sell_dump_ladder_rungs` (4) are still validated and still passed into the function. The refire does **not** call `dump_retry_ladder_limits`. That helper remains for tests. A dump does not walk 4¢ rungs off one snapshot.
5. Full fill (or dry run): `sold_dump=True`, `sold_winner=True`, `sell_dump_leg = held`, `sold_dump_at`, `sell_dump_filled`, `sell_dump_limit` = last limit, and `sell_dump_fill_px` = share-weighted average of every priced fill. Log `sell_dump_done` (`hedge_leg`, `avg_px`). `bag_risk.dump_px` reads the average, falling back to the limit.

**Kept-side sell.** `sell_dump_also_kept` (code default **false**; read each tick). The 3 Oct file had it true. The 13 Oct list does not restate it, so a kept-side sale happens only when the live file sets the flag. It does not re-check `sell_dump_other_min`. The sale is the tracked kept shares once per bag: `min(sell_scrap_keep, balance of sold_leg)`, or `sell_scrap_keep` when there is no funder or CTF. Flat or zero is `outcome=nothing_kept`. The sale is the same fresh-bid refire, then **one** FAK at `max(0.01, sell_clob_min_price)` for any remainder. It records `sell_dump_kept_done`, `_planned`, `_filled`, `_fill_px`, `_limit`, `_attempts` and `_outcome` (`filled` / `partial` / `no_fill` / `dry_run` / `nothing_kept`), and `sell_dump_kept_sold` when fully sold. It logs `sell_dump_kept`. A partial remainder is not retried on later ticks. Turning the flag on after the dump has already filled does nothing.

`sell_dump_parallel_kept` (code default **true**; missing or null stays on; hot-reloaded) starts that kept sell at the instant the held dump fires. The kept worker thread posts its own first FAK and its own fresh-bid refires. The sell thread posts the held leg's first FAK immediately after the worker starts, without waiting for the kept leg. `sell_exit_inflight` stays set until both have returned. The worker does not write the intent. After the join, the sell thread merges the kept result, then the held result's `sold_dump` / `sell_dump_done` path, and only then can this tick reach reclaim. A miss on one leg does not cancel the other. There is one kept launch per bag (`sell_dump_kept_done`), so a later held retry does not sell the kept leg again. Set the flag false and the kept sell is the old path: `_sell_kept_after_dump` runs only after the held dump has filled.

At 100 shares and fraction 0.5 the bag holds 100 of the favourite and 50 of the scrapped leg. Selling both locks `−100 + scrap + 100·p + 50·q` with `q` near `1 − p`. With the flag off, the kept 50 stay to resolution ([§22](#section-22)).

**Not in this tree.** Tiered dump persistence (`sell_dump_tiers`) and the old post-dump kept stop are not keys in `DEFAULTS`. `load_strategy` drops them. The reclaim stop in [§19b](#section-19b) is the post-dump exit that is on `main`.

**Scope.** Full sets with neither leg sold never arm the dump. After `end_ts`, `manage_sells` skips the intent. `sell_dump_leg` is what wallet B would buy against; B stays off. The same refire is what the reclaim stop and a sold hedge use.

<a id="section-19b"></a>
## Reclaim: entry, size, stop

Opt-in (`reclaim_enabled`, code **false**, live reference **true**). After a both-sides dump, buy about `reclaim_usd` of the first side whose ask holds at `reclaim_entry`. `reclaim_arm_block` waits until `sold_dump` is set, and until `sell_dump_kept_done` when `sell_dump_also_kept` is on and a keep was owed. Those flags are written only after a parallel kept sell has joined, so reclaim does not arm while either leg is still posting. A kept half that was never sold does not arm (`kept_open`). The sell loop stays hot (`sell_armed_poll_s`) while `reclaim_hot`.

Live reference: entry **0.90**, persist **1.0s**, size **$2**, other-side ceiling **0.15**, stop **0.65**. Code defaults: entry 0.91, persist 0.5s, size $100, other-side ceiling null, stop 0.75 for 0.5s.

**Entry.** `reclaim_entry_decision` returns `buy`, `wait`, or `skip`. No network.

- The candidate is the leg whose best ask is at or over `reclaim_entry`. Higher ask first; a tie picks Up.
- The other side confirms when its best ask is at or under `reclaim_other_max`. Null keeps `1 − reclaim_entry` (0.91 → 0.09). Live 0.15 replaces that. An other ask above the ceiling is `sister_unconfirmed`.
- Both asks at or over entry is `both_rich`. This side under entry is `below_entry`.
- Each book must be present, uncrossed, unlocked (bid ≠ ask), and inside a 10¢ spread (`RECLAIM_MAX_SPREAD`). A timestamp older than 5s (`RECLAIM_BOOK_MAX_AGE_S`) is `stale_book`. There is no ask-depth gate and no last-trade tape.
- `reclaim_max_ttm_s` (code 0, off) uses the scrap time gate: unknown ttm stays open. `reclaim_min_ttm_s` (code 0, off) skips with `too_close` when a known ttm is under it. Unknown ttm stays open.
- Persist is `reclaim_entry_persist_s`. Explicit 0 fires on the qualifying tick. A failed re-check clears `reclaim_entry_armed_at`, so the next send needs a fresh hold. The leg can switch until the first post locks `reclaim_leg`.

**Every send, including a top-up,** re-checks that tick's books: entry, sister ask, fresh uncrossed book, spread, and ask at or under `reclaim_max_price`. A failed check does not send. An ask already above the cap on a top-up logs `reclaim_topup_skipped_cap` and is not posted.

**Price and size.** The FAK limit is `min(ask + reclaim_slippage, reclaim_max_price)`. Code default is 3¢ over the ask, hard cap 96¢, and the cap must be ≥ entry. The 13 Oct list does not restate slippage or the cap. The first clip is `floor(reclaim_usd / limit)` whole shares (`reclaim_share_size`), sized off that limit, not off the raw ask. USDC is truncated to cents (`reclaim_buy_usdc`); under one cent the amount is 0 and the buy is `size_zero`. The same price and size, once refused, is not posted again (`reclaim_fak_reject`). Timeouts and 5xx stay uncertain and are re-checked with a balance read; one zero read is not a miss.

The main clip does not bump shares up to a $1 notional. Polymarket rejects a marketable buy under about $1, and that refusal is the `reclaim_fak_reject` path. The $1 floor, and the 5-share floor, are applied on the hedge ([§19c](#section-19c)). At the live $2 and a limit near 90–96¢ the main clip is 2 whole shares, about $1.80–$1.92.

**Stop.** `reclaim_stop_enabled` defaults true. A sized bid at or under `reclaim_stop` for `reclaim_stop_persist_s` (code 0.75 / 0.5s; live stop **0.65**, persist not restated) sells with the dump FAK refire at the live bid, floored only at `sell_clob_min_price`. A missing or stale bid does not clear the clock. Once `reclaim_stop_latched`, later ticks chase the live bid with no new wait. A bid back above the stop before the latch resets the clock. `reclaim_stop_enabled` false holds to redeem (`reclaim_hold`). Shares still held at resolution go to the redeem thread. There is no merge and no resting bid.

Moving from the buy to the stop logs `reclaim_done` (filled shares, average price, cost, why the top-up stopped). A partial that no longer qualifies becomes a position and is stopped; it does not switch sides.

While the reclaim is open, or a dump or stop sell is in progress, sequential mode waits ([§12](#section-12)).

<a id="section-19c"></a>
## Reclaim hedge: concurrent buy, retries, keep rule

`reclaim_hedge_enabled` defaults **false**. It is not in the 13 Oct live list, so this document does not say the flag is on. The knobs that list does set, which match the code defaults, are `reclaim_hedge_usd` **1.0**, `reclaim_hedge_keep_above` **0.50** for `reclaim_hedge_keep_persist_s` **1s**, `reclaim_hedge_retries` **3**, and `reclaim_hedge_max_price` **0.15**. `reclaim_hedge_slippage` defaults to **0.02** and is not restated live.

When the flag is on, the other side is FAK-bought in the same moment as the main buy: a daemon thread, not a wait for the main fill. The main buy does not wait. A hedge miss does not cancel the main. The hedge is attempted once per bag (`reclaim_hedge_attempted`).

**First order (`reclaim_hedge_plan`).** Limit is `ask + reclaim_hedge_slippage`, and it must sit in (0, 1). Shares start at `floor(usd / limit)`, then rise to at least **5 shares** and until the cent-truncated USDC is at least **$1** (`RECLAIM_HEDGE_MIN_SHARES`, `RECLAIM_HEDGE_MIN_NOTIONAL`). `rounded_up` is true when that bump happens. A bad ask logs `reclaim_hedge_buy` with `sent=false` and does not invent a size. If a pUSD balance is already in memory and does not cover the main USDC plus the hedge USDC, the hedge is `cash_short` and is not posted. A missing balance does not block and does not trigger a wallet read on the order path.

**Retries (`_reclaim_hedge_run`).** A zero or partial fill refires on that same thread, up to `reclaim_hedge_retries`, and only while `now ≤ first_post + RECLAIM_HEDGE_RETRY_BUDGET_S` (**1.0s**, measured from the first post, not from each miss). Each refire fetches the other book and posts the unfilled shares at the **fresh ask with no added slippage**, and only while that ask is at or under `reclaim_hedge_max_price`. An ask above the cap stops the sequence (`above_cap`) and is logged. The unfilled size is still raised to the $1 and 5-share minimums, which can make the target larger than the first clip. `error:` and a balance/allowance reject do not refire. Each attempt logs `reclaim_hedge_retry`. The finish logs `reclaim_hedge_outcome` (`filled`, `partial`, `miss`, `above_cap`, `budget`, `uncertain`, `reject`, `no_ask`).

An in-flight refire (`reclaim_hedge_retrying`) still defers the next sequential mint, and its outstanding USDC stays in the mint cash reserve.

**Keep rule.** After the main stop has fired (`reclaim_stop_latched` or `reclaim_stopped`), `reclaim_hedge_keep_decision` keeps the hedge only when its sized bid stays **strictly above** `reclaim_hedge_keep_above` for `reclaim_hedge_keep_persist_s`. A bid at or under the line, a bid that leaves before the persist, or a missing bid, is `sell`. The sell is the dump FAK refire on the hedge token. A keep sets `reclaim_hedge_kept` and rides to resolution. No stop (`reclaim_stop_enabled` false), or a kept hedge, also rides to resolution. A kept hedge does not block the next mint. The decision logs `reclaim_hedge_keep_decision`; the sell logs `reclaim_hedge_sell`.

<a id="section-19d"></a>
## post_dump_book journal

`post_dump_log_enabled` defaults **true** and is **true** in the live reference. It hot-reloads. After `sold_dump`, and while the window is still open, mintbot journals `post_dump_book` about every **0.25s** (`POST_DUMP_BOOK_INTERVAL_S`) until `end_ts`.

Fields: `condition_id`, `slug`, `leg_dumped`, `up_bid`, `up_ask`, `dn_bid`, `dn_ask`, `ttm_s`, plus the top 5 bid and ask levels on each leg (`up_bids`, `up_asks`, `dn_bids`, `dn_asks` as `[price, size]` lists, best first) and `up_ask_near` / `dn_ask_near` (ask size within 3¢ of the best ask).

The sell tick reuses the books it already fetched and logs only after that bag's orders, reclaim, and hedge work. Depth is taken from those same ladders. A sampler thread (`_post_dump_logger`) fills the gaps between ticks and is the only place that reads the book for this journal. It does not place orders and does not change a decision. The cadence state lives in memory (`_post_dump_box`), not in `positions_mint.json`. A fetch error logs `post_dump_book_fail`. The flag off, a bag that has not dumped, a closed window, or a line that is not yet due is a skip.

`reclaim_buy` logs `ask_depth` and `hedge_ask_depth` (top 5 asks of the bought leg and the other leg) after the buy FAK, from the same tick's book. A sent `reclaim_hedge_buy` logs `ask_depth` for the hedge leg after that FAK. Neither read adds a book fetch.

<a id="section-20"></a>
## Live-bid FAK vs floor FAK

| Path | Limit choice | Why |
|---|---|---|
| Loser | One FAK at the live loser bid (≤ the active threshold, 3¢ or 4¢ in the last 180s, ≥ 1¢) for the scrap remainder, re-posted at the new bid each retry. Flag off: 1¢ ladder from `sell_fak_px` to the floor, clipped to top-rung depth. Blind FAK at 0.01. Post-miss rest at `min(sell_scrap_rest_px, live or last-seen bid)` when rest is enabled | A low limit still takes the better bids first, in one round trip |
| Winner (allowed) | `min(live sized bid, 0.99)` | Resting books quote 0.995–0.999; posting those limits is rejected (`max: 0.99`). A 0.99 FAK still fills the rich book |
| Held dump, kept half, reclaim stop, hedge sell | Current sized bid, then one fresh-bid FAK per retry. No 4¢ ladder | The book is moving; each retry reads it again |
| Reclaim buy | `min(ask + slippage, reclaim_max_price)`, whole shares off that limit | Pay a bounded amount over the ask. The hedge's first order adds its own slippage; its retries do not |

Observed failure modes: (1) winner armed at bid 0.99 but FAK posted 0.999 → `invalid price … max: 0.99`. (2) bag `btc-updown-15m-1789810200`: cheap gate open, Down sized 0.995–0.999, live-bid FAK without clamp → 12× same rejection, winner never sold.

<a id="section-21"></a>
## Hypothetical lifecycle: 100-share bag, scrap 50 @ ~3¢, winner redeems

Uses the 13 Oct live reference where it names a knob (`shares` 100, fraction 0.5, threshold 3¢). Fees ignored. The scrap time gate is the code default (off) unless the live file still sets one; the 3 Oct file used 360s and this walk-through shows that gate because it is the one the restart rule also uses.

1. **Mint.** pUSD covers $100. Prechecks pass. Mint 100 Up + 100 Down: **−$100.00**. Relayer confirms, reconcile sees 100/100 → `confirmed`.
2. **Window open.** Up drifts down. If `sell_scrap_max_ttm_s` is 360, a 3¢ Up bid at T−8m does not arm (`sell_scrap_time_gated`). With the code default 0, it can arm as soon as the book qualifies.
3. **Arm.** Up's sized bid is 0.03 and Down's is 0.96. `classify_loser` → `up`. Inside the last 180s the same arm would also accept a 4¢ Up bid.
4. **Persist.** Still 0.03 / 0.96 for `sell_persist_s` → `ready`. `sell_fire_decision` → `fire`.
5. **Plan lock.** `scrap_share_plan(100, 0.5)` → target 50, keep 50. `sell_scrap_plan` logged. Order size 50.
6. **Sweep.** One FAK SELL 50 Up at the live bid, capped at 3¢ (4¢ in the last 180s). Bids at 3¢ absorb it: `sell_scrap_sweep size=50 avg_px=0.03`: **+$1.50**.
   - `sell_filled=50`, `sell_fill_px=0.03`, `sold_loser`, `sold_leg="up"`, `sell_scrap_outcome target_filled`.
7. **Rest of the window.** Down (held 100) stays 0.95–0.99.
   - Dump needs a sized bid under 0.67. It does not fire.
   - The winner path needs the code `sell_winner_min` of 0.999 (the 3 Oct file used 0.9995). It does not fire.
   - The kept Up 50 is blocked from the cheap winner path.
8. **`end_ts`.** The sell window closes. `bag_risk` logs. With redeem enabled, the redeem thread submits after `end_ts + 60s` once the condition has resolved: Down 100 → **+$100.00**, Up 50 → $0. The intent becomes `completed`, `redeemed: true`.

| Line | Cash |
|---|---:|
| Mint 100 sets | −100.00 |
| Scrap 50 Up @ 0.03 | +1.50 |
| Redeem 100 Down | +100.00 |
| Kept 50 Up | 0.00 |
| **Net** | **+1.50** |

With `sell_scrap_fraction=1.0` the same bag would scrap 100 × 0.03 = $3.00 and net +$3.00. The keep costs $1.50 on a normal win. If the scrapped leg flips and wins, the kept 50 redeem $50 and the net is −$100 + $1.50 + $50 = **−$48.50**.

<a id="section-22"></a>
## Hypothetical lifecycle: dump under 67¢, kept half optional

Steps through the scrap are [§21](#section-21): −$100.00 mint, +$1.50 scrap, holding 100 Down and 50 Up.

1. Down's sized bid falls through 0.70 (danger alert, if that watch is on) and prints **0.66**, under the live 0.67 trigger, and the other side's bid is at or over `sell_dump_other_min`. `sell_dump_persist why=armed`. A later tick where only the other bid falls under that minimum logs `why=other_low` and clears the arm.
2. **0.5s later** (live `sell_dump_persist_s`) the bid is still 0.66 → `ready`. `_sell_inventory` → 100.
3. **Held dump.** The first FAK at 0.66 returns `no orders found`. The retry refetches the book and posts one FAK at the new bid, 0.64. It does not walk a 4¢ ladder. That FAK fills 100 at 0.64: **+$64.00**. `sold_dump`, `sell_dump_leg="dn"`, `sell_dump_done`. `post_dump_book` starts.
4. **Kept half, only if `sell_dump_also_kept` is true.** With `sell_dump_parallel_kept` (code default true) the kept 50 Up is posted at the same time as the held FAK, with its own refire, then one 1¢ FAK for a remainder. A 0.34 fill is **+$17.00**. The bag is flat. Reclaim waits until both legs have returned. With `sell_dump_also_kept` false (the code default, and not restated in the 13 Oct list) those 50 Up stay. With parallel kept false, the kept sale starts only after the held dump fills.

Net, mint −100 and scrap +1.50 in every row. The dump fill below is the 0.64 hypothetical, not a forecast.

| Final outcome | No dump | Dump held leg only | Dump + kept 50 @ 0.34 |
|---|---:|---:|---:|
| False alarm: Down wins | +1.50 | −34.50 | **−17.50** |
| Real flip: Up wins | −48.50 | +15.50 | **−17.50** |

- **No dump, false alarm:** redeem 100 Down, +100. **No dump, real flip:** redeem the kept 50 Up, +50.
- **Held leg only:** +64 from the dump, plus +50 from the kept Up only if Up wins.
- **Both legs sold:** +64 + 17, nothing left to redeem. The outcome no longer matters. In general that lock is `−98.50 + 100·p + 50·q`.

After a both-sides dump, reclaim can buy about $2 of the side that holds at 90¢ ([§19b](#section-19b)). That is a new position, not part of the dump arithmetic above. `bag_risk` still records `dump_px`, `dump_ttm`, and how long the held bid sat below 0.80 / 0.65 / 0.50.

<a id="section-23"></a>
## What this code does not prove

- That 2–4¢ loser bids with depth always exist when the opposite is ≥ 90¢.
- That every redeem lands first time. The redeem thread retries with backoff and logs `redeem_gave_up` (with an ntfy alert) after 6 attempts ([§37](#section-37)).
- That one bag at a time beats two overlapping bags. Sequential mode can skip a window when the redeem is late or the relayer `STATE_FAILED`s.
- That dumping at 67¢ (or the code 80¢) is optimal; it is an operator-chosen circuit breaker ([§22](#section-22)).
- That keeping 50% of the loser, selling it on every dump, or buying it back with reclaim, is optimal. `bag_risk`, `sell_scrap_outcome`, `sell_dump_kept`, `reclaim_done`, and `post_dump_book` exist to measure it.

<a id="part-iv"></a>
# Part IV — The buy/ helpers and pathlog

<a id="section-24"></a>
## Ownership map

| Module | Owner of |
|---|---|
| `mintbot.py` | Process, knobs merge, relayer, CLOB sells, state file |
| `buy/mint_loops.py` | Concurrent sell/mint job runner, same-slug claim, candidate selection, pending-cash reserve, ended-bag chain policy, persist digest |
| `buy/mint_sell.py` | Pure sell policy, scrap plan, dump persist, reclaim entry/stop/hedge sizing, `post_dump_book` cadence, `bag_risk` |
| `buy/mint_gas.py` | Mint relay `gas_limit` plan |
| `buy/mint_sequence.py` | Sequential mint range, busy-bag gate, wait / skip bookkeeping (opt-in) |
| `buy/mint_redeem.py` | Redeem jobs: resolution poll, relayer redeem, retry / give-up, startup sweep (opt-in) |
| `buy/market.py` | Discovery / `MintMarket` |
| `buy/book.py` | Sized BBO parse, depth snapshot |
| `buy/chain.py` | RPC reads, per-thread sessions |
| `buy/contracts.py` | Calldata for mint batch and the pUSD top-up transfer |
| `buy/oracle_log.py` | Chainlink 60s TWAP tape + `bag_view` for the (off) late scrap veto |
| `buy/log_archive.py` | `mintbot.log` rotation into `logs/archive/` |
| `buy/sister_bid.py` / `buy/sister_topup.py` | Wallet B policy (scrapbidder only; off) |
| `pathlog.py` | Separate process; read-only books |

<a id="section-25"></a>
## buy/mint_sell.py policy helpers

- `parse_sell_fill_shares` — share leg from CLOB response (not USDC `takingAmount`).
- `sell_fill_vwap` — average price from `takingAmount / shares` for `sell_scrap_sweep.avg_px`.
- `inventory_latch` — await vs already_flat vs has_inventory.
- `classify_loser` — which leg is loser / both_cheap / wick_unconfirmed.
- `persist_ready` — arm → waiting → ready over `persist_s` (resets when qualify drops).
- `effective_loser_persist_s` / `loser_scrap_persist_s` — 5s normally, 2s when `0 < TTM ≤ 60` (code defaults; live 3s, and 2s within the last 90s); `None` at/after `end_ts`; `sized_skip` only if that flag is on.
- `sell_window_open` — CLOB sells only while TTM is strictly positive.
- `scrap_time_gate_open` / `dump_time_gate_open` — TTM cutoffs; ≤ 0 disables; unknown TTM open for scrap, closed for dump.
- `loser_empty_keep_qualify` — armed + empty loser book (opposite still ok or also empty) should keep the arm.
- `loser_persist_ready` — persist_ready plus empty-book / empty-FAK keep/re-arm (`empty_keep_arm`).
- `sell_fire_decision` — last in-range check before FAK (`fire` / `cancel_reset` / `cancel_keep_arm`).
- `loser_scrap_post` / `scrap_live_bid_limit` — sweep plan (one limit at the live bid, capped at the active threshold, floored at 1¢) or ladder plan.
- `loser_ladder_limits` — every 1¢ from min(fak or threshold, bid) down to floor; the live bid alone when it is below the floor. Also used for the `depth_at_limit` preview and the `phase=ready` depth log, even in sweep mode.
- `loser_partial_fak_shares` — ladder-mode clip to displayed depth.
- `normalize_scrap_fraction` / `scrap_share_plan` / `scrap_order_shares` / `scrap_target_met` — partial scrap ([§17b](#section-17b)).
- `kept_leg_below_winner_min` — blocks the winner path on the kept leg below base `sell_winner_min`.
- `loser_blind_fak_due` — blind 1¢ FAK eligibility and backoff.
- `scrap_rest_action` / `scrap_rest_px` / `resting_tif` / `rest_order_matched_shares` / `posted_order_id` — post-miss rest lifecycle.
- `late_oracle_scrap_ok` / `advance_oracle_edge_arm` / `side_aware_oracle_edge_usd` — late-window loser-scrap veto. Off unless `sell_late_window_s` > 0.
- `scrap_oracle_settings` / `scrap_oracle_veto` — the #234 rule, still pure and tested. The sell path does not call it: `_scrap_oracle_gate` returns disabled (#245).
- `winner_cashout_leg` — unique leg whose sized bid ≥ winner_min.
- `winner_cheap_decision` — cheap min only if sold_loser, loser ≤ gate, and loser + cheap > $1.
- `winner_sell_limit` — clamp live-bid FAK into CLOB [0.01, 0.99].
- `effective_dump_persist_s` / `dump_fast_retry_eligible` — dump clock and which zero-fills may refire. `dump_retry_ladder_limits` is not used by the order path.
- `scrap_active_prices` — base vs late scrap threshold and FAK price.
- `reclaim_entry_decision` / `reclaim_entry_qualify` / `reclaim_complement_max` / `reclaim_buy_limit` / `reclaim_share_size` / `reclaim_buy_usdc` — reclaim entry, whole-share size, cent USDC.
- `reclaim_stop_decision` / `reclaim_arm_block` — stop clock and both-sides-dump gate.
- `reclaim_hedge_plan` / `reclaim_hedge_retry_plan` / `reclaim_hedge_retry_allowed` / `reclaim_hedge_keep_decision` / `reclaim_wallet_covers` — hedge size, $1 and 5-share floors, retry budget, keep rule.
- `post_dump_book_sample` / `post_dump_book_due` — journal cadence (`skip` / `emit` / `fetch`).
- `fresh_bag_risk` / `bag_risk_observe` / `bag_risk_flush` / `bag_risk_payload` — log-only `bag_risk`.
- `cycle_sleep_s` / `mint_cycle_sleep_s` / `sell_intent_hot` — loop cadence. Reclaim watching counts as hot.

`DEFAULT_SELL_KNOBS` mirrors mintbot sell knobs including dump keys.

<a id="section-26"></a>
## buy/market.py, book.py, chain.py, contracts.py, mint_gas.py, log_archive.py

Discovery builds `MintMarket` with `condition_id`, `up_token`, `dn_token`, `start_ts`, `end_ts`, `slug`, flags. Book helper returns best bid with minimum size and `bid_fill_depth` (cumulative bids at/through a FAK limit; mint logs `sell_book_depth`, does not gate on it). Chain helper reads ERC-1155 positions and pUSD balance and exposes `_rpc` for the one gas estimate. Contracts helper encodes the atomic mint path used by the relayer batch, the pUSD transfer the sister top-up uses, and the opt-in redeem batch (`setApprovalForAll` + adapter `redeemPositions`). Chain also reads the CTF payout vector and adapter approval for redeem. `mint_gas.py` turns an estimate into a clamped `MintGasPlan` ([§14](#section-14)). `log_archive.py` supplies `ArchiveRotatingFileHandler`: `mintbot.log` still rolls at 2 MB, but each roll is renamed to `logs/archive/mintbot.log.<UTC stamp>` (never clobbering) and gzipped on one background worker. Nothing there is pruned (#219). The same module exposes `roll_if_over`, which `buy/oracle_log.py` calls before each append so `logs/oracle_twap.jsonl` rolls into the same archive at 20 MB ([§10c](#section-10c)).

<a id="section-27"></a>
## pathlog.py: public book recorder

Separate systemd unit. `SERIES = ["btc-up-or-down-15m"]` only. Polls CLOB books, appends JSONL ticks under `pathlog/`, prunes by age/size (14 days / 400 MB), optionally records resolution. **No orders.** Used for research/backtests (`check_path_backtest.py`). Pathlog is not a trading input. **The recorder is intentionally retired.** It exited cleanly on 22 Sep 20:00 UTC and is not coming back. On 30 Sep 2026 the unit was still `enabled`, so the operator should run `sudo systemctl disable polypathlog` to keep a reboot from reviving it. The code and unit file stay in the repo unchanged, for old tick files and backtests.

<a id="part-v"></a>
# Part V — Operations, verification and sharp edges

<a id="section-28"></a>
## systemd units

`deploy/polymintbot.service` runs `.venv/bin/python mintbot.py` with `EnvironmentFile=.env`, `Restart=always`. That `.env` (`/home/ntemusejoel/poly-money-maker/.env` on the VM, gitignored) also carries the optional `CALLMEBOT_PHONE` / `CALLMEBOT_APIKEY`; env changes need a restart.

`deploy/polypathlog.service` runs `pathlog.py` (no env file required for public books). Retired: keep it stopped and disabled ([§27](#section-27)).

Lockbot/`deploy/polylockbot.service` is deleted. Do not reinstall or start it.

`deploy/polyscrapbid.service` is opt-in and stays disabled. It runs `scrapbidder.py` with `EnvironmentFile=.env.complement` only (not mintbot `.env`). Since #212, even with `bid_enabled` on, wallet B's 20-share post-scrap buy needs `scrap_hedge_enabled` (default **false**); the 10-share dump hedge after A sets `sell_dump_leg` needs `dump_hedge_enabled` (default true). Both use the FAK/rest notional band ($1.00–$1.50 by default). Markets A never held are not bid (`bid_absent_enabled` defaults false). There is no sister-miss dump. The A→B top-up (`sister_topup.py`, $5 of pUSD once per broke episode, reads `.env` itself) needs `topup_enabled` (default **false**). Scrapbidder re-reads mint intents after quoting books so a scrap during the pass is not planned from a stale snapshot (#203). It does not mint and does not FAK-sell. `bid_enabled` defaults false and `dry_run` defaults true. Do not commit `.env.complement`. Do not add `.env` to this unit. Same-wallet buyback is not implemented. Do not enable this unit unless the operator asks.

Never enable retired buy units (`polycomplement`, buybots, DangerZone, shadow) from memory of old docs. `polyscrapbid` is not a restore of `complementbot.py`.

<a id="section-29"></a>
## Knobs: every key, meaning, and default

"Code" is `DEFAULTS` in `mintbot.py`. "Example" is shown only when `strategy_mint.example.json` differs. "Live" is filled only for the operator's 13 Oct 2026 reference. Those values are in gitignored `strategy_mint.json` on the VM. A blank live cell means this revision does not restate that key. A key the live file omits takes the code default after a pull and restart. `load_strategy` ignores keys that are not in `DEFAULTS`. Do not edit the live file from this repo.

### Live reference (13 Oct 2026)

| Key | Live |
|---|---|
| `shares` | 100 |
| `sell_scrap_fraction` | 0.5 |
| `sell_threshold` | 0.03 |
| `sell_threshold_late` | 0.04 in the last 180s |
| `sell_dump_below` | 0.67 |
| `sell_dump_persist_s` | 0.5 |
| `sell_dump_persist_last_min_s` | 0.5 |
| `reclaim_enabled` | true |
| `reclaim_entry` | 0.90 |
| `reclaim_entry_persist_s` | 1.0 |
| `reclaim_stop` | 0.65 |
| `reclaim_usd` | 2.0 |
| `reclaim_other_max` | 0.15 |
| `reclaim_hedge_usd` | 1.0 |
| `reclaim_hedge_keep_above` | 0.50 |
| `reclaim_hedge_keep_persist_s` | 1 |
| `reclaim_hedge_retries` | 3 |
| `reclaim_hedge_max_price` | 0.15 |
| `post_dump_log_enabled` | true |

The late pair also needs `sell_fak_px_late` and `sell_late_price_window_s` (180). The list names the threshold and the window, not the late FAK price. `reclaim_hedge_enabled` is not in the list (code default false).

### Mint and process

| Key | Code | Meaning |
|---|---:|---|
| `entry_enabled` | false | Submit mints. False stops new mints; sells continue |
| `dry_run` | true | Log orders, send nothing |
| `shares` | 50 | Complete-set size. Live reference **100** (100 Up + 100 Down, about $100) |
| `enter_min_ttm_min` | 0 | Non-sequential: do not mint a window opening inside this many minutes |
| `enter_max_ttm_min` | 45 | Non-sequential: mint only windows opening within this many minutes |
| `mint_fail_cooldown_s` | 30 | Wait after a failed mint before that condition can retry |
| `mint_submitting_timeout_s` | 90 | Auto-fail a `submitting` intent that still has no `transaction_id`. 0 disables |
| `mint_max_attempts` | 3 | Give up on that condition after this many failures |
| `mint_gas_margin` | 0.15 | Multiply `eth_estimateGas` by 1 + margin |
| `mint_gas_fallback` | 650000 | Gas limit when the estimate fails |
| `mint_gas_cap` | 650000 | Clamp. Also clamped to the ~650k relay-hub budget |
| `series_slugs` | `btc-up-or-down-15m` | Only this series |
| `one_entry_per_market` | true | Do not mint a condition that already has an intent |
| `max_open_sets` | 1 | Open-bag cap when sequential mode is off, plus one adjacent window |
| `count_kept_loser_as_open` | false | Kept loser shares hold the slot until resolution |
| `mint_sequential` | false | One bag at a time. Ignores `max_open_sets` and the TTM band |
| `mint_seq_lead_s` | 30 | Sequential mint opens at start − lead |
| `mint_seq_cutoff_s` | 240 | Sequential mint closes at start + cutoff, then `mint_seq_skip` |
| `poll_s` | 5 | Mint sleep, and sell sleep when idle. Must be ≥ 1 |
| `sell_armed_poll_s` | 2 | Sell sleep while a bag is hot, including an open reclaim. Must be ≥ 0.2 |
| `book_timeout_s` | 1.2 | CLOB `/book` and `/books` timeout |
| `mint_cash_poll_s` | 2 | How often to re-read pUSD while a sequential mint waits on cash |
| `position_tolerance` | 0.01 | Share dust ignored for flat / filled |
| `require_accepting_orders` | true | Skip markets Gamma says are not accepting orders |

### Redeem and alerts

| Key | Code | Meaning |
|---|---:|---|
| `redeem_enabled` | false | Auto-redeem on the `mintbot-redeem` thread |
| `redeem_poll_s` | 15 | Redeem loop sleep. Floor 1 |
| `redeem_min_after_end_s` | 60 | Create a bag's redeem job this long after `end_ts` |
| `redeem_retry_s` | 60 | First retry delay. Doubles, cap 900s |
| `redeem_max_attempts` | 6 | Then `gave_up` and an ntfy alert |
| `redeem_tx_timeout_s` | 300 | Relayer wait before a submit is uncertain. Floor 30 |
| `redeem_startup_sweep` | true | One Data API `redeemable=true` pass per start. Neg-risk skipped |
| `redeem_min_payout_usd` | 0.01 | Held value under this is `no_winner`, no transaction |
| `notify_danger_whatsapp` | true | One WhatsApp when the held winner stays under the danger line. Needs `CALLMEBOT_*` |
| `notify_danger_px` | 0.70 | Danger line |
| `notify_danger_hold_s` | 5 | How long the bid must stay under the line |
| `notify_scrap_whatsapp` | false | WhatsApp on a scrap fill, or a partial at window end |
| `notify_dump_whatsapp` | false | WhatsApp on a dump fill |

### Scrap

| Key | Code | Meaning |
|---|---:|---|
| `sell_enabled` | false | Run `manage_sells` |
| `sell_threshold` | 0.02 | Loser arm ceiling. Live reference **0.03** |
| `sell_fak_px` | 0.02 | Top of the cent ladder when sweep is off. The sweep cap is the active threshold, not this |
| `sell_threshold_late` | null | Late arm ceiling. Live reference **0.04** |
| `sell_fak_px_late` | null | Late ladder top. Both late prices must be set or the base pair stays |
| `sell_late_price_window_s` | 0 | Use the late pair when ttm is known and ≤ this. 0 keeps the base pair. Not `sell_late_window_s`. Live reference: last **180s** |
| `sell_floor` | 0.02 | Ladder bottom only. The sweep does not use it |
| `sell_scrap_sweep_enabled` | true | One live-bid FAK. False restores the 1¢ ladder |
| `sell_opposite_min` | 0.90 | Opposite sized bid must be at least this rich |
| `sell_persist_s` | 5 | Loser and winner persist |
| `sell_persist_last_min_s` | 2 | Persist when ttm is inside the last-minute window, through the close |
| `sell_persist_last_min_window_s` | 60 | Length of that window. 0 disables the shorter clock |
| `sell_persist_skip_when_sized` | false | Do not skip persist just because the book is sized |
| `sell_scrap_max_ttm_s` | 0 | Scrap only in the last N seconds. 0 is off. Example **600**. Unknown ttm leaves the gate open |
| `sell_scrap_fraction` | 1.0 | `floor(held × fraction)` is the scrap; the rest is kept. Live reference **0.5** |
| `sell_scrap_blind_enabled` | true | Blind FAK when the book vanishes after arm |
| `sell_scrap_blind_px` | 0.01 | Blind FAK price |
| `sell_scrap_blind_backoff_s` | 3 | Minimum gap between blind FAKs |
| `sell_scrap_rest_enabled` | true | After a FAK miss, rest a GTD/GTC sell |
| `sell_scrap_rest_px` | 0.02 | Rest ceiling. Posted price is `min(this, live or last-seen bid)` |
| `sell_scrap_rest_min_ahead_s` | 180 | GTD only when expiry is at least this far ahead; otherwise GTC |
| `sell_cooldown_s` | 3 | Minimum gap between FAK attempts on a bag. Explicit 0 is kept |
| `sell_min_bid_size` | 1.0 | Ignore a top bid smaller than this |

### Winner, dump, journal

| Key | Code | Meaning |
|---|---:|---|
| `sell_winner_min` | 0.999 | Winner cash-out. A kept loser leg uses this, never the cheap gate |
| `sell_winner_cheap_if_loser_le` | 0.03 | Cheap winner only if the recorded loser fill is ≤ this |
| `sell_winner_min_cheap` | 0.99 | Cheap winner floor. Also requires fill + this > 1 |
| `sell_clob_max_price` | 0.99 | Winner FAK clamp. Richer books still fill a 0.99 limit |
| `sell_clob_min_price` | 0.01 | Floor for winner, sweep, dump, and stop limits |
| `sell_dump_enabled` | true | Held-leg dump |
| `sell_dump_below` | 0.80 | Arm when the held sized bid is under this. Live reference **0.67** |
| `sell_dump_other_min` | 0.30 | Other side's bid must stay at least this for the whole persist. 0 or null disables. Not in the 13 Oct list, so a pull takes 0.30 |
| `sell_dump_persist_s` | 2 | Dump persist. Live reference **0.5**. Explicit 0 fires on the qualifying tick |
| `sell_dump_persist_last_min_s` | null | Dump persist inside the last-minute window. Null follows `sell_dump_persist_s`. Live reference **0.5** |
| `sell_dump_persist_last_min_window_s` | 0 | That window. 0 leaves the last-minute clock off. Not in the 13 Oct list |
| `sell_dump_max_ttm_s` | 0 | Dump only in the last N seconds. 0 is off. Example **240**. Unknown ttm is closed |
| `sell_dump_also_kept` | false | Sell the kept scrap half once in the dump event, then one 1¢ FAK for a remainder |
| `sell_dump_parallel_kept` | true | Post that kept leg with the held leg. False waits until the held dump fills. Missing or null stays on |
| `sell_dump_fak_retries` | 2 | Extra fresh-bid FAKs after the first shot, in the same tick. Also used by the stop and the hedge sell |
| `sell_dump_ladder_step` | 0.04 | Still validated. The refire does not walk this ladder |
| `sell_dump_ladder_rungs` | 4 | Still validated. The refire does not walk this ladder |
| `post_dump_log_enabled` | true | Journal both books after a dump until the window ends. Live reference **true** |

### Reclaim and hedge

| Key | Code | Meaning |
|---|---:|---|
| `reclaim_enabled` | false | After a both-sides dump, FAK-buy a side that holds at the entry. Live reference **true** |
| `reclaim_usd` | 100 | Dollars for the first clip. Live reference **2.0** |
| `reclaim_entry` | 0.91 | This side's ask must be at or over this. Live reference **0.90** |
| `reclaim_entry_persist_s` | 0.5 | How long the entry must hold. Explicit 0 fires on the qualifying tick. Live reference **1.0** |
| `reclaim_other_max` | null | Other side's ask ceiling. Null means `1 − reclaim_entry`. Live reference **0.15** |
| `reclaim_slippage` | 0.03 | Added to the ask for the main FAK, then capped |
| `reclaim_max_price` | 0.96 | Hard cap on the main FAK. Must be ≥ entry. An ask already above it is not posted |
| `reclaim_max_ttm_s` | 0 | No reclaim entry while ttm is above this. 0 is off. Unknown ttm stays open |
| `reclaim_min_ttm_s` | 0 | No reclaim entry while ttm is under this (`too_close`). 0 is off |
| `reclaim_stop` | 0.75 | Stop when the bought side's bid is at or under this. Live reference **0.65** |
| `reclaim_stop_enabled` | true | False holds the position to redeem |
| `reclaim_stop_persist_s` | 0.5 | How long the stop bid must hold. A missing or stale bid does not clear the clock |
| `reclaim_hedge_enabled` | false | FAK-buy the other side at the same time as the main buy. Not in the 13 Oct list |
| `reclaim_hedge_usd` | 1.0 | Hedge dollars before the $1 and 5-share bump. Live reference **1.0** |
| `reclaim_hedge_slippage` | 0.02 | Added on the first hedge order only. Retries use the raw ask |
| `reclaim_hedge_max_price` | 0.15 | Retries stop when the fresh ask is above this. Live reference **0.15** |
| `reclaim_hedge_retries` | 3 | Refires after the first post, inside about 1s. Live reference **3** |
| `reclaim_hedge_keep_above` | 0.50 | After the stop, keep the hedge only while its bid is strictly above this. Live reference **0.50** |
| `reclaim_hedge_keep_persist_s` | 1.0 | How long that bid must hold. Live reference **1s**. Otherwise the hedge is sold with the dump refire |

### Oracle

| Key | Code | Meaning |
|---|---:|---|
| `oracle_log_enabled` | false | Chainlink 60s TWAP tape. Audit only. Example false |
| `sell_late_window_s` | 0 | Late-window scrap veto. 0 skips it. Not the late price window |
| `sell_oracle_edge_floor_usd` | 0 | Old veto floor. 0 does not restore the $25 rule |
| `sell_oracle_edge_per_ttm` | 0 | Old veto dollars per second of ttm |
| `sell_oracle_stale_s` | 0 | Old veto staleness. 0 does not restore the 5s rule |
| `sell_oracle_edge_persist_s` | 3 | How long the late-window edge must hold. Unused while the window is 0 |
| `scrap_oracle_veto_enabled` | false | Loaded. The sell path ignores it: `_scrap_oracle_gate` always returns disabled (#245) |
| `scrap_oracle_veto_usd` | 5.0 | Margin the pure helper uses. The sell path does not call that helper |
| `scrap_oracle_veto_stale_s` | 3.0 | Stale cutoff in the pure helper. Unused by the sell path |
| `scrap_oracle_veto_use_live` | true | Live-print flag in the pure helper. Unused by the sell path |

### Endpoints

| Key | Code | Meaning |
|---|---:|---|
| `rpc_url` | `https://polygon.drpc.org` | Polygon JSON-RPC |
| `gamma_url` | `https://gamma-api.polymarket.com` | Market discovery |
| `data_api_url` | `https://data-api.polymarket.com` | Redeemable sweep |
| `relayer_url` | `https://relayer-v2.polymarket.com` | PROXY submit |
| `chain_id` | 137 | Polygon |
| `pUSD_address` | `0xC011…E82DFB` | Collateral |
| `ctf_address` | `0x4D97…76045` | Conditional tokens |
| `standard_adapter_address` | `0xAdA1…cE1f` | Split and redeem adapter |

The sister process is off. Its knobs are in `strategy_scrapbid.example.json`.

<a id="section-30"></a>
## Deploy boundary and safe restart

Operational rule: **VM files win**. GitHub is backup/history. Live `strategy_mint.json` and `positions_mint.json` stay gitignored. Example knobs are not authorization to trade.

The `Deploy to GCP` workflow runs on pushes to `main` that touch `mintbot.py`, `pathlog.py`, `check_path_backtest.py`, `buy/**` or `requirements.txt`. It does `git pull` + `pip install` on the VM and **never restarts** a service. Docs-only changes (including this file) do not trigger it and are not synced anywhere. Creating a PR is not authorization to merge or to restart.

`polymintbot` is stopped as of 2026-10-05 (inactive, still enabled at boot). Do not start it unless the operator asks. A pull does not start it. `polypathlog` stays stopped and disabled. `polyscrapbid` stays stopped. Lockbot is deleted. Knob edits in the live JSON hot-reload on the next tick and do not need a restart. Code and `.env` do.

**Restart only between bags.** This is an operator rule. The process does not enforce it. `Restart=always` will still bring the unit back after a crash. An intentional restart waits until all of these are true:

1. **The live bag has minted.** Its intent is `confirmed` (inventory matched). It is not `submitting`, `pending`, `executed`, `mined`, or `confirmed_waiting_inventory`.
2. **The previous bag has redeemed.** Its redeem job is `done` (`redeemed: true`, or `nothing_held` when the bag was already flat). It is not `submitting`, `submitted`, or waiting on a relayer tx.
3. **More than 360 seconds left** on the live bag. That is outside a 360s scrap gate and outside the oracle feed's hot span, so the restart does not land inside an armed scrap.
4. **Nothing in flight.** No `sell_exit_inflight`. No reclaim buy posted, in flight, or uncertain. No hedge buy in flight, retrying, or uncertain. No FAK whose fill is still unknown. A resting scrap lives on the CLOB across the restart; the new process cancels it only on a later tick, so do not restart while one is working unless that is accepted.

`positions_mint.json` is what lets a crash resume. The safe window is so an operator restart does not have to.

<a id="section-30b"></a>
## Journal events

`log_event` writes one JSON object per line to `mintbot.log`: `ts`, `event`, then the fields. These are the event names on `main`. A failure inside `bag_risk` or a notify hook is swallowed and does not add an order.

**Process.** `startup` (includes `sell_plan`), `strategy_reload_fail`, `sell_cycle`, `sell_cycle_error`, `cycle`, `cycle_error`, `clob_client_ready`, `clob_client_init_fail`, `book_fetch_fail`.

**Mint.** `mint_attempt`, `mint_submitted`, `mint_submit_fail`, `mint_confirmed`, `mint_failed`, `mint_submitting_timeout_failed`, `mint_skip_balance`, `mint_skip_existing`, `mint_skip_not_binary`, `mint_skip_pending_reserve`, `mint_seq_wait_prev`, `mint_seq_wait_cash`, `mint_seq_skip`, `precheck_fail`, `positions_fetch_fail`, `relayer_status_fail`, `inventory_check_fail`, `dry_mint`.

**Scrap and winner.** `sell_loser_persist`, `sell_persist_effective`, `sell_persist_skip`, `sell_scrap_time_gated`, `sell_scrap_plan`, `sell_book_depth`, `sell_scrap_sweep`, `sell_scrap_ladder`, `sell_scrap_blind`, `sell_scrap_outcome`, `sell_loser_done`, `sell_cancel_out_of_range`, `sell_skip_await_inventory`, `sell_skip_both_cheap`, `sell_skip_wick_unconfirmed`, `sell_fak_result`, `sell_fak_fail`, `sell_balance_fail`, `sell_allowance_warn`, `sell_scrap_rest_place`, `sell_scrap_rest_result`, `sell_scrap_rest_fill`, `sell_scrap_rest_cancel`, `sell_scrap_rest_cancel_fail`, `sell_scrap_rest_fail`, `sell_scrap_rest_poll_fail`, `dry_scrap_rest`, `dry_sell`, `sell_winner_persist`, `sell_winner_cheap_allowed`, `sell_winner_limit_clamped`, `sell_winner_done`, `sell_keep_winner_blocked`, `sell_loser_oracle_block`, `sell_loser_oracle_ok`, `sell_loser_oracle_persist`.

**Dump.** `sell_dump_persist` (includes `other_bid`; `why=other_low` when that side blocks), `sell_dump_time_gated`, `sell_dump_fast_refire_attempt`, `sell_dump_fast_refire_stop`, `sell_dump_done`, `sell_dump_kept`, `post_dump_book`, `post_dump_book_fail`, `bag_risk`.

**Reclaim and hedge.** `reclaim_arm`, `reclaim_entry_persist`, `reclaim_skip`, `reclaim_buy`, `reclaim_buy_fill`, `reclaim_fak_result`, `reclaim_fak_fail`, `reclaim_fak_reject`, `reclaim_buy_uncertain`, `reclaim_buy_recovered`, `reclaim_buy_uncertain_cleared`, `reclaim_topup_skipped_cap`, `reclaim_done`, `reclaim_hold`, `reclaim_stop_persist`, `reclaim_stop`, `reclaim_stop_uncertain`, `reclaim_allowance_warn`, `reclaim_error`, `dry_buy`, `reclaim_hedge_buy`, `reclaim_hedge_retry`, `reclaim_hedge_outcome`, `reclaim_hedge_keep_decision`, `reclaim_hedge_sell`.

**Redeem.** `redeem_cycle`, `redeem_cycle_error`, `redeem_sweep`, `redeem_sweep_fail`, `redeem_wait_resolution`, `redeem_nothing_held`, `redeem_no_winner`, `redeem_dry_run`, `redeem_submitted`, `redeem_submit_fail`, `redeem_submit_uncertain`, `redeem_confirmed`, `redeem_gave_up`, `redeem_check_fail`.

**Oracle tape** (only while `oracle_log_enabled`). `oracle_twap`, `oracle_open_ref`, `oracle_window_end`, `oracle_strike_revised`, `oracle_strike_late`, `oracle_strike_check`, `oracle_close_check`, `oracle_check_missed`, `oracle_window_end_missed`, `oracle_feed_watchdog`, `oracle_feed_reconnect`, `oracle_feed_stall`, `oracle_feed_recovered`, `oracle_log_fail`.

**WhatsApp.** `danger_zone` (always, even when the send is off), `notify_whatsapp_on`, `notify_whatsapp_off`, `notify_sent`, `notify_failed`, `notify_dropped`.

<a id="section-31"></a>
## Testing without constructing a live bot

Do not import `mintbot.py` in unit tests (credentials, lock, clients). Test `buy/` helpers directly and AST-extract `mintbot.py` functions with stubs (`tests/test_mint_only_ops.py`, `tests/test_mint_cpu.py`). Run `python -m unittest discover -s tests -p 'test_*.py' -v` in a disposable sandbox.

<a id="section-32"></a>
## Landmines

1. **Failed remint storm** — `failed` stays in `already_minted` during cooldown and after `mint_max_attempts`.
2. **`relay hub: internal transaction failure`** — inner CTF split out of gas under the 500k library default. Fixed by the explicit `gas_limit`; watch `gas_source=fallback` or `gas_clamped=true` in `mint_submitted`. See [§13b](#section-13b).
3. **Skipping the next window** — without adjacent lookahead, `max_open_sets=1` + “never mint open markets” skips a quarter-hour.
4. **Winner at 0.999 on a 0.99 book** — live-bid FAK once allowed, then clamp to CLOB max 0.99 (do not POST 0.995–0.999).
5. **Dump without `sold_leg`** — held leg cannot be inferred; loser path must set `sold_leg`.
6. **Sells stop at `end_ts`** — no dump/cash-out after expiry in `manage_sells`; redeem is the remaining path.
7. **Importing mintbot in tests** — can take the flock or load `.env`.
8. **Confusing mint with buybot docs** — old hourly TDD describes a different money path.
9. **Re-serializing sell and mint** — do not fold them back into one `manage_sells → discover → sleep` cycle. That is the 1789905600 hole. Draft PR #193 skip-mint is not the fix.
10. **Stale local `poll_s` patch on the VM.** `main` validates `poll_s >= 1`. An uncommitted `mintbot.py` edit blocks the deploy workflow's `git pull`. Drop it (`git checkout -- mintbot.py`) before a pull. If `poll_s` is under 1, a restart fails `load_strategy` and systemd restart-loops.
11. **Zero seconds knobs** — resolved. `sell_dump_persist_s`, `sell_cooldown_s` and `sell_scrap_rest_min_ahead_s` go through `cfg_seconds`, so an explicit 0 is respected; `validate_strategy` rejects negatives. The 13 Oct list sets dump persist to 0.5 and does not restate cooldown or rest-ahead. Their code defaults are 3s and 180s.
12. **`sell_limit` is the last posted sweep limit.** Under the live-bid sweep that limit can be the 1¢ floor. The loser price that matters is `sell_fill_px` (share-weighted average fill). The cheap-winner gate and `bag_risk` read it first, and fall back to `sell_limit` only for intents written before the field existed. The dump has `sell_dump_fill_px` alongside `sell_dump_limit`.
13. **Oracle tape size** — resolved. `logs/oracle_twap.jsonl` rolls into `logs/archive/` at 20 MB and is gzipped in the background ([§10c](#section-10c)). The 116 MB live file is archived on the first append after the next restart; a `tail -f` must follow the rename (`tail -F`).
14. **Pathlog retired but enabled** — `polypathlog` has been inactive since 22 Sep and is not coming back, but `systemctl is-enabled` still said `enabled` on 30 Sep. Disable it so a reboot does not revive it ([§27](#section-27)).
15. **Startup banner** — resolved. `main()` prints `sell_plan_banner(cfg)` from the loaded strategy (sweep vs ladder, scrap fraction, TTM gates, dump threshold, winner floor), and the `startup` event carries the same `sell_plan` string.
16. **Silent RTDS socket** — resolved in code. A connected-but-silent feed is closed by the 45s watchdog (5s in a bag's last 360s since #234; three 45–57s silences fell inside that span on 30 Sep–3 Oct) and protocol ping/pong, and the stall logs once plus a reminder a minute instead of every second ([§10c](#section-10c)). The close now comes from the end-boundary RTDS sample, not crypto-price, so crypto-price 429s no longer drop `oracle_window_end` rows (8 of 226 on 29–30 Sep).
17. **A dump exits the kept leg only when `sell_dump_also_kept` is true.** Code default is false. Reading `sell_dump_done` alone misses `sell_dump_kept` (`sold`, `avg_px`, `outcome`). A `partial` or `no_fill` outcome leaves kept shares to resolution and is not retried.
18. **Unknown strategy keys are dropped.** `sell_dump_tiers` and any other key missing from `DEFAULTS` is ignored by `load_strategy`. The post-dump exit on `main` is reclaim, not a separate kept-stop key.
19. **Restart mid-bag.** `positions_mint.json` resumes a crash. An operator restart still waits for the window in [§30](#section-30): minted, previous bag redeemed, more than 360s left, nothing in flight.

<a id="section-33"></a>
## Glossary

| Term | Meaning here |
|---|---|
| Complete set | 1 Up + 1 Down for one condition |
| Mint / split | Collateral → both outcome tokens |
| Loser scrap | Sell the cheap leg (all, or a locked fraction) after the opposite is rich |
| Partial scrap / keep | `sell_scrap_fraction < 1`: scrap `floor(held × f)`, hold the rest to resolution |
| Live-bid sweep | One loser FAK at the live loser bid for the scrap remainder |
| Winner cash-out | Sell the rich leg at `sell_winner_min` (code 0.999). The 3 Oct file used 0.9995, which a 0.001 tick cannot print |
| Held dump | After loser sold, sell the held leg if its sized bid is under `sell_dump_below` (80¢ code, 67¢ in the 13 Oct reference) |
| Reclaim | After a both-sides dump, FAK-buy about `reclaim_usd` of a side that holds at `reclaim_entry` |
| Reclaim hedge | Optional concurrent FAK buy of the other side, retried at the fresh ask up to `reclaim_hedge_max_price` |
| `post_dump_book` | Both books and the top 5 levels each side, about every 0.25s, from the dump until the window ends. No orders |
| Redeem | Exchange winning tokens for $1 collateral after resolution, on `mintbot-redeem` when `redeem_enabled` is true (code default false; not in the 13 Oct list) |
| FAK | Fill-and-kill marketable limit |
| Sized bid | Best bid with minimum size |
| Adjacent lookahead | Mint the next 15m while still holding the current full bag. Sequential mode does not use it. Code `mint_sequential` is false; the 3 Oct file had it true; the 13 Oct list does not restate it |
| Sequential bags | One bag of capital: mint in [start − 30s, start + 240s] once the previous bag is cashed or ended. Code default off |
| Relayer PROXY | Polymarket-submitted batched tx from proxy wallet |
| Pending reserve | pUSD already committed to in-flight mints, subtracted before a new mint |
| `bag_risk` | One log line per bag at window close; audit only |

<a id="section-34"></a>
## Source snapshot

- Host: Google VM `poly-vm` (`/home/ntemusejoel/poly-money-maker`)
- Services: `polymintbot` stopped as of 2026-10-05 (inactive, still enabled at boot). `polypathlog` retired (stopped and disabled). `polyscrapbid` stays stopped. Lockbot is deleted
- Code: `main` through #258 (post-dump book journal), including #256 (reclaim other-side ceiling and concurrent hedge) and #257 (hedge refire)
- Primary sources: `mintbot.py` (~6957 lines), `buy/mint_sell.py` (~3122), `buy/mint_loops.py` (~544), `buy/mint_sequence.py` (~224), `buy/mint_redeem.py` (~633)
- Strategy: gitignored `strategy_mint.json`. The 13 Oct reference is the live column in [§29](#section-29). Other live keys are not restated
- Document date: 13 October 2026 (see [Changelog](#changelog))
- Prior revisions: 30 September 2026 (partial scrap, 100-share bag, floor sweep, TTM gates, relay gas, pending reserve, bag_risk); 19 September 2026 (sequences + redeem)
- Prior document replaced: hourly `buybothourly.py` guided tour (9 Sep 2026 era)

---


<a id="appendix-a"></a>
# Appendix A — Cycle pseudocode (faithful to live control flow)

```
sell loop (cycle_sleep_s: poll_s, or min(poll_s, sell_armed_poll_s) while sell-hot):
  cfg = load_strategy()                  # every tick; failure keeps old cfg, entry off
  manage_sells(cfg, state, chain)        # lock around intent writes; I/O unlocked
  commit_state if persist digest changed

mint loop (always poll_s):
  cfg = load_strategy()
  fail_stale_submitting_intents(...)
  reconcile_intents(...)                 # relayer/RPC outside lock; ended bags: one read at end+180s
  if any submitting: return "wait_submit"
  if not cfg.entry_enabled: return "disabled"
  markets = gateway.discover(cfg.series_slugs)
  candidates = eligible_markets(markets, cfg, now)   # NOT YET OPEN, within TTM band
               # mint_sequential (code default false; 3 Oct file true; not restated 13 Oct):
               # seq_eligible_markets → [start-30s, start+240s],
               # and seq_busy_bag replaces mint_slots_full below
  floor = latest active bag start + 900s, or none
  pick = first zero-fail candidate in start order where:
           start_ts >= floor                          # no backwards mint
           not already_minted(condition_id, now)      # cooldown, or attempts >= max
           and wallet does not already hold tokens
           and not mint_slots_full for that start     # adjacent window still allowed
  if none: repeat, allowing a failed condition under the attempt cap
  if every free candidate is over capacity: return "capped_open"
  if nothing free: return "idle"
  precheck contracts / binary / pUSD balance            # no lock
  if balance - pending_mint_reserve < shares: return "pending_reserve" or "no_balance"
  claim submitting under STATE_LOCK      # already_minted + slots + same-slug; saved before submit
  log mint_attempt for pick.slug
  tx_id, err, gas = submit_mint_batch(calls, rpc=chain._rpc)  # no lock; one eth_estimateGas
  persist pending / failed
```

<a id="appendix-b"></a>
# Appendix B — Sell decision table

Values are code default / live.

| Precondition | Persist | Action | Flags set |
|---|---|---|---|
| Loser sized bid ≤ active threshold (code 0.02, live 0.03, or 0.04 in the last 180s) AND opposite ≥ 0.90 AND not both cheap AND TTM gate open | code 5s | Lock plan if fraction < 1; one FAK at the live loser bid for `target − filled` | `sold_loser`, `sold_leg`, `sell_filled`, `sell_scrap_*` |
| Same, inside `sell_persist_last_min_window_s` (code 60s) | code 2s | Same | Same |
| Loser armed, book or FAK empty | kept | Blind FAK at 0.01 every ≥ 3s; rest after a miss when rest is enabled | Same on fill |
| Winner sized bid ≥ `sell_winner_min` (code 0.999), cheap gate if it applies | code 5s | Live-bid FAK clamped to 0.99; kept leg capped at keep | `sold_winner` |
| `sold_loser` AND held sized bid < `sell_dump_below` (code 0.80, live 0.67) AND other bid ≥ `sell_dump_other_min` (code 0.30; 0 or null off) AND dump TTM gate open | code 2s, live 0.5s, both conditions together | Held live-bid FAK and fresh-bid refires. Kept half, when `sell_dump_also_kept`, posts at the same time if `sell_dump_parallel_kept` (default true); otherwise only after the held fill. Neither re-checks the other side. Reclaim waits until both legs return | `sold_dump`, `sold_winner`, `sell_dump_leg`; `sell_dump_kept_*` |
| Both sides out AND `reclaim_enabled` AND ask holds `reclaim_entry` (live 0.90) with the other ask ≤ `reclaim_other_max` (live 0.15) | live 1.0s | FAK at `min(ask + slippage, cap)`, whole shares off that limit. Hedge in parallel when enabled | `reclaim_bought`, `reclaim_filled` |
| Reclaim bought AND bid ≤ `reclaim_stop` (live 0.65) | code 0.5s | Dump-style fresh-bid FAK. Hedge kept only if its bid stays above 0.50 for 1s | `reclaim_stopped` or `reclaim_hedge_kept` |
| `sold_dump` and window still open | 0.25s | `post_dump_book`. No order | — |
| `now ≥ end_ts` | — | No CLOB sells; cancel rest; `sell_scrap_outcome window_end` if unfinished; one `bag_risk` | (redeem outside this loop) |
| Within `sell_cooldown_s` of last attempt | — | Skip fire | — |

`effective_winner_min` formula:

```
effective = sell_winner_min                           # 0.999 code
loser_px = sell_fill_px, else sell_limit             # recorded average fill first
if sold_loser and loser_px <= sell_winner_cheap_if_loser_le      # -1.0 live: never
   and loser_px + sell_winner_min_cheap > 1.0:
    effective = min(effective, sell_winner_min_cheap)
```

<a id="appendix-c"></a>
# Appendix C — Intent status machine

```
submitting → pending → executed/mined → confirmed_waiting_inventory → confirmed
                                                                  ↘ completed (flat after end)
                 ↘ failed   (STATE_FAILED / STATE_INVALID / stale submitting; remint after cooldown, max 3)
```

`ACTIVE_STATUSES` (count toward open bags unless loser sold / expired+120s):
`submitting`, `pending`, `executed`, `mined`, `confirmed_waiting_inventory`, `confirmed`.
`PENDING_CASH_STATUSES` (reserve pUSD): the same set minus `confirmed`.

<a id="appendix-d"></a>
# Appendix D — Why adjacent lookahead exists

Timeline bug without lookahead (`max_open_sets=1`):

1. Mint window A (1:30–1:45) at 1:20. Intent confirmed; slot full.
2. At 1:32, window B (1:45–2:00) is eligible on time, but slot full → skip.
3. At 1:45+ε, A expires; slot frees. But B has **started**, and eligibility forbids open markets → B never minted.
4. Bot jumps to C (2:00–2:15).

Fix: while holding a full bag ending at `end_ts`, allow minting the candidate whose `start_ts` is exactly that adjacent boundary. Still forbid a second lookahead.

Combined with `sold_loser` freeing the slot mid-window, the bot can mint the next set after the loser scrap without waiting for expiry.

<a id="appendix-e"></a>
# Appendix E — Economic sketch (not a promise)

Assume the 13 Oct reference: `shares=100`, `sell_scrap_fraction=0.5`, scrap average 3¢, fees ignored. See [§21](#section-21) and [§22](#section-22).

| Path | Cash out | Comment |
|---|---:|---|
| Mint | −100.00 | Split collateral |
| Scrap 50 loser @ 0.03 | +1.50 | Live-bid sweep, avg fill |
| Redeem winner 100 | +100.00 | When redeem is on, after resolution |
| Kept 50 loser | 0.00 | Expires worthless on a normal win |
| **Net** | **+1.50** | Thin; fees can erase it |

| Path | Cash out | Comment |
|---|---:|---|
| Mint | −100.00 | |
| Scrap 50 @ 0.03 | +1.50 | |
| Dump 100 held @ 0.64 | +64.00 | Under the 0.67 trigger, after a fresh-bid refire |
| Sell kept 50 @ 0.34 | +17.00 | Only if `sell_dump_also_kept` |
| **Net if both sold** | **−17.50** | Same whichever leg wins |

| Path | Net |
|---|---:|
| No dump, favourite wins | +1.50 |
| No dump, scrapped leg wins (kept 50 redeem) | −48.50 |
| Dump held leg only, favourite then wins | −34.50 |
| Dump held leg only, scrapped leg wins | +15.50 |

<a id="appendix-f"></a>
# Appendix F — Operator checklist

1. `polymintbot` is stopped as of 2026-10-05 (inactive, still enabled at boot). Do not start it unless the operator asks. `polypathlog` should be `disabled` (retired). `polyscrapbid` stays stopped.
2. `jq . strategy_mint.json` → confirm `shares` (100 in the 13 Oct reference), `sell_scrap_fraction` (0.5), `sell_threshold` / `sell_threshold_late`, `sell_dump_below` (0.67), `sell_dump_persist_s` (0.5), and the reclaim keys in [§29](#section-29). Never commit this file.
3. `git status --short mintbot.py` → empty (the old local `poll_s` patch is upstream now; drop it before a pull).
4. Tail `mintbot.log` for `mint_confirmed`, `mint_submitted` (gas fields), `sell_scrap_plan`, `sell_scrap_sweep`, `sell_scrap_outcome`, `sell_dump_done`, `sell_dump_fast_refire_attempt`, `sell_dump_kept`, `post_dump_book`, `reclaim_buy`, `reclaim_done`, `reclaim_hedge_outcome`, `bag_risk`, `mint_failed`, `mint_seq_wait_cash` / `mint_seq_skip`, `redeem_confirmed` / `redeem_gave_up`.
5. After a `mint_failed`, expect a remint after 30s up to 3 attempts, then that slug is skipped.
6. Restart `polymintbot` only when the operator asks, and only between bags: the live bag has minted, the previous bag has redeemed, more than 360s left, nothing in flight ([§30](#section-30)). Lockbot is deleted. No local patch to re-apply.
7. GitHub sync is backup; VM remains SoT.




<a id="part-vi"></a>
# Part VI — Sequences and redeem

ASCII diagrams below are the PDF-safe form of sequence charts. They match `mintbot.py` on `main`. The mint diagram shows the default (non-sequential) selection. `mint_sequential` defaults false; the note under the diagram is the flag-on path. The 13 Oct list does not restate the flag.

<a id="section-35"></a>
## End-to-end mint sequence

```
operator/systemd          mintbot               Gamma/CLOB         Relayer            Polygon CTF
      |                      |                      |                  |                   |
      | start unit           |                      |                  |                   |
      |--------------------->|                      |                  |                   |
      |                      | load strategy_mint   |                  |                   |
      |                      | flock .mintbot.lock  |                  |                   |
      |                      |                      |                  |                   |
      |                      | sell loop ⊥ mint loop|                  |                   |
      |                      |---- manage_sells --->| sized bids       |                   |
      |                      |<---------------------|                  |                   |
      |                      |---- reconcile ------>|                  | get tx state      |
      |                      |                      |                  |------------------>| 
      |                      |                      |                  |   balances        |
      |                      |<----------------------------------------|-------------------|
      |                      |                      |                  |                   |
      |                      | discover series      |                  |                   |
      |                      |--------------------->|                  |                   |
      |                      | eligible: not open,  |                  |                   |
      |                      |   TTM in (0,45] min  |                  |                   |
      |                      | skip already_minted  |                  |                   |
      |                      |   (cooldown 30s, or  |                  |                   |
      |                      |    attempts >= 3)    |                  |                   |
      |                      | next future same pass|                  |                   |
      |                      | mint_slots_full?     |                  |                   |
      |                      |   allow adjacent     |                  |                   |
      |                      |   next window only   |                  |                   |
      |                      | balance - reserve    |                  |                   |
      |                      |   >= shares?         |                  |                   |
      |                      |                      |                  |                   |
      |                      | build approve+split  |                  |                   |
      |                      | eth_estimateGas ---------------------------------------------->|
      |                      |---------------------------------------->| PROXY submit      |
      |                      |   gas_limit=est×1.15 |                  |------------------>| 
      |                      | persist intent       |                  |                   |
      |                      |   status=pending     |                  |                   |
      |                      | poll until inventory |                  |                   |
      |                      |   matches shares     |                  |                   |
      |                      | status=confirmed     |                  |                   |
      | ntfy (optional)      |                      |                  |                   |
      |<---------------------|                      |                  |                   |
```

Notes:

1. **Sell and mint run concurrently**; neither waits for the other. In the default mode a mid-window loser fill (full or partial target) frees `max_open_sets` so the adjacent mint is allowed sooner.
2. **Default mode never mints an already-open window.** If adjacent lookahead fails, that quarter-hour is skipped forever for this bot.
   **Sequential mode** (code default off): eligibility is `[start − lead, start + cutoff]` (30s / 240s), so an open window can be minted up to the cutoff. The busy-bag gate replaces `mint_slots_full`, and short pUSD waits instead of skipping until the cutoff. An open reclaim or hedge buy keeps the previous bag busy.
3. Relayer `STATE_FAILED` → intent `failed` + `errorMsg` → cooldown 30s, then remint until `mint_max_attempts`. At the cap that condition is skipped for the rest of its life, and a later future is tried in the same cycle. The historical typed message is `relay hub: internal transaction failure` (see [§13b](#section-13b)).

<a id="section-36"></a>
## Sell-side sequence (winner / held dump / loser)

```
mintbot                 CLOB book              inventory latch           flags on intent
   |                        |                        |                        |
   | for each confirmed     |                        |                        |
   | intent with now<end    |                        |                        |
   |---- sized Up/Dn bids ->|                        |                        |
   |<-----------------------|                        |                        |
   |                        |                        |                        |
   | [A] winner path        |                        |                        |
   | effective_min =        |                        |                        |
   |   sell_winner_min      |                        |                        |
   |   (code 0.999)         |                        |                        |
   | kept leg < base min:   |                        |                        |
   |   blocked              |                        |                        |
   | if sized winner bid    |                        |                        |
   |   >= effective_min     |                        |                        |
   |   for persist_s:       |                        |                        |
   |---- FAK min(bid,.99) ->|                        |                        |
   |                        |                        |---- has shares? ------>|
   |                        |                        |                        | sold_winner
   |                        |                        |                        |
   | [B] held dump path     |                        |                        |
   | requires sold_loser    |                        |                        |
   | held = opposite of     |                        |                        |
   |   sold_leg             |                        |                        |
   | if ttm <= dump cutoff  |                        |                        |
   |   and held bid <       |                        |                        |
   |   dump_below for the   |                        |                        |
   |   dump persist:        |                        |                        |
   |---- live-bid FAK ----->|                        |                        |
   |   (+ fresh-bid refire) |                        |                        | sold_dump
   |                        |                        |                        | + sold_winner
   |                        |                        |                        | sell_dump_leg
   | also_kept, if set:     |                        |                        |
   |---- kept half FAK ---->|                        |                        | sell_dump_kept_*
   |   (+ refire, one 1c)   |                        |                        |
   |                        |                        |                        |
   | [C] loser path         |                        |                        |
   | ttm <= scrap cutoff,   |                        |                        |
   | loser<=threshold and   |                        |                        |
   | opposite>=0.90 for     |                        |                        |
   |   sell_persist_s       |                        |                        |
   |   (2s in the last-min  |                        |                        |
   |    window, code 60s)   |                        |                        |
   | lock target/keep once  |                        |                        |
   |---- FAK @ live bid --->|                        |                        |
   |   size target-filled   |                        |                        | sold_loser
   |                        |                        |                        | sold_leg=up|dn
   |                        |                        |                        | sell_filled
```

Ordering in code is **A → B → C**, then reclaim, then `post_dump_book`, inside one intent iteration. A bag that already sold the loser can cash out or dump the held leg before another loser attempt.

Cooldown: after any sell attempt, `sell_cooldown_s` (code 3s) suppresses the next fire.

<a id="section-37"></a>
## Redeem path — what exists vs what does not

### What “redeem” means

After a 15m window resolves, the winning outcome token can be **redeemed** through the CTF for ~$1 collateral per share. That is an on-chain call (or Polymarket UI / relayer helper), separate from CLOB sells.

Economically the mint thesis prefers:

1. Sell (part of) the loser for scraps (2–3¢),
2. **Redeem** winner at $1,

rather than selling the winner at 0.99 on the CLOB (which donates ~1¢ × shares plus fees versus redeem).

### What mintbot does today

| Step | Implemented? | Where |
|---|---|---|
| Stop CLOB sells after `end_ts` | **Yes** | `manage_sells` skips intents once `sell_window_open` is false |
| Keep winner (and kept loser) inventory unsold | **Yes** | no forced sell at expiry. Code `sell_winner_min` is 0.999; the 3 Oct file used 0.9995 |
| Free mint slot while winner sits for redeem | **Yes** | `sold_loser` excluded from open-slot count; also expiry+120s |
| Redeem resolved positions automatically | **Opt-in** (`redeem_enabled`, code default false; not in the 13 Oct list). Code `redeem_startup_sweep` defaults true | `buy/mint_redeem.py` `RedeemDesk`, thread `mintbot-redeem` |
| Relayer batch for redeem | **Opt-in** | `submit_redeem` → `submit_mint_batch` (same PROXY signer, gas plan, relayer auth) |
| Mark intent `redeemed` after payout | **Opt-in** | `redeemed: true`, `redeem_status`, status `completed` |
| Merge (Up + Down → pUSD before resolution) | **No** | not needed; complete sets are sold or redeemed |

Observation (30 Sep 2026, before `redeem_enabled` was turned on): the winning leg's pUSD was back in wallet A about **68s after `end_ts`**. The 3 Oct file had `redeem_enabled` true and `redeem_startup_sweep` false. The 13 Oct list does not restate either key, so this revision does not claim the desk is redeeming. Kept loser shares from a partial scrap are worthless on a normal win and pay $1 each on a flip.

### Auto-redeem (opt-in)

**Call.** The desk sends `redeemPositions(pUSD, bytes32(0), conditionId, [1, 2])` to the collateral adapter `standard_adapter_address`. This is the same target the mint splits through, and it is what the Polymarket SDK does for a non-neg-risk market. The adapter burns both outcome balances of the proxy wallet and pays the winning side in pUSD. If CTF `isApprovedForAll(proxy, adapter)` is false, the batch prepends `setApprovalForAll(adapter, true)`. Both calls go through `submit_mint_batch` as one PROXY transaction signed by the mint EOA.

**Gas.** The gas limit is one `eth_estimateGas` plus 15%, falling back to and capped at 650k. On-chain relay-hub redeems measured 365k–453k gas. Gas price is 0 for the signer, so the relayer pays; at ~330 gwei that is about 0.12–0.15 POL.

**Job lifecycle.** Each job lives in `positions_mint.json` → `redeems[condition_id]`.

```text
waiting ── legs flat on 2 reads ───────────────► done (nothing_held)
   │──── payoutDenominator == 0 ─► wait (poll_s; 5 min after 1h)
   │──── payout < min_payout ──────────────────► no_winner (no tx)
   │──── dry_run ─► log once, re-check every 5 min (no tx)
   └──── persist submitting ─► submit ─► submitted
                                    │ no tx id ─► retry (backoff)
submitted ── STATE_CONFIRMED and legs flat ────► done (redeemed)
   │──── STATE_FAILED / STATE_INVALID ─► retry (backoff)
   └──── no answer after tx_timeout ─► legs flat? done : retry
retry: next_at = now + retry_s * 2^(attempts-1), cap 900s;
       attempts >= redeem_max_attempts ─► gave_up (ntfy, manual redeem)
submitting found on restart: after tx_timeout ─► waiting (re-check, resubmit)
```

**Job sources.**
- **Bags.** Intents with status `mined` / `confirmed_waiting_inventory` / `confirmed`, not dry and not already redeemed or given up, from `end_ts + redeem_min_after_end_s`.
- **Startup sweep.** One Data API `/positions?redeemable=true` pass for the funder. Rows are grouped per condition, and Up is outcome index 0. Neg-risk rows are counted and skipped. A failed sweep retries every 5 minutes.

**Idempotency.**
- Each tick re-reads both CTF balances before any submit.
- A job never submits twice in a row without a failure or timeout first.
- A repeated `redeemPositions` burns a zero balance and pays zero, so an uncertain retry cannot double-pay.
- Final jobs (`done`, `no_winner`, `gave_up`) are pruned after two days. The intent flags stop them from being created again.

**Concurrency.**
- All I/O runs outside `STATE_LOCK`. Every job write saves state with `commit_state(dirty=True)`.
- `RELAY_SUBMIT_LOCK` serializes mint and redeem relayer submits, which share the EOA nonce from `/relay-payload`.
- The redeem thread does not write the heartbeat file, so its `ts` still tracks sell and mint.
- The sell loop never calls redeem code.

**Payout mapping.** The payout estimate assumes Up is CTF slot 0 (`clobTokenIds[0]`, indexSet 1), which is Polymarket's standard binary layout. The redeem call itself passes both index sets, so a wrong mapping would only affect the `no_winner` skip and the logged estimate.

### Why the design stops at “hold for redeem”

1. **Margin:** live-bid 0.99 cash-out was explicitly gated; unconditional 0.99 was rejected.
2. **Window boundary:** once `end_ts` passes, CLOB prices for that market become resolution-driven and the bot refuses further FAK risk.
3. **Scope control:** mint + sell-side pass is already enough surface area; auto-redeem adds another relayer/CTF path and failure mode (wrong condition index, partial redeem, gas/relayer auth).

With `redeem_enabled` false (the code default), redeem stays outside the running process, and mintbot is mint plus intra-window sell policy. The 3 Oct file had it on with `redeem_startup_sweep` false, so only bags that process minted got redeem jobs. A bag that dumped with `sell_dump_also_kept` is already flat and ends `nothing_held`.

<a id="section-38"></a>
## State after expiry

```
now < end_ts
  └─ manage_sells active (winner / dump / loser)

now >= end_ts
  ├─ manage_sells: cancel rest, sell_scrap_outcome window_end (if unfinished),
  │     one bag_risk line, then skip this intent
  ├─ open_intent_count: still counts full bag until end_ts+120
  │     unless sold_loser already cleared the slot
  ├─ after end_ts+120: intent no longer blocks mint capacity
  ├─ end_ts .. end_ts+180: reconcile does not chain-query a confirmed bag
  └─ at end_ts+180: one final balance read (chain_reconcile_done)
        if both legs flat → completed
        else stays confirmed; no further reads

redeem_enabled (opt-in), separate thread:
  end_ts+60: redeem job created; legs flat (2 reads) → completed
  resolved (~end_ts+53..87s) and value held → relayer redeem
  confirmed and legs flat → completed, redeemed: true
```

Adjacent mint may already have been submitted **before** expiry (lookahead). That is intentional and is the main fix for the “skipped 15m” bug.

# Changelog

- **2026-10-13** — `post_dump_book` also records the top 5 bid and ask levels on each leg (`up_bids`, `up_asks`, `dn_bids`, `dn_asks`) and the ask size within 3¢ of the best ask (`up_ask_near`, `dn_ask_near`). `reclaim_buy` records `ask_depth` and `hedge_ask_depth` after the buy FAK; a sent `reclaim_hedge_buy` records `ask_depth` after the hedge FAK. All of it comes from ladders already in hand. No extra book read and no change to an order.

- **2026-10-13** — When `sell_dump_also_kept` is on, `sell_dump_parallel_kept` (default true, hot-reloaded) posts the kept leg at the same time as the held leg. Each leg keeps its own fresh-bid refire. The intent is merged after both return, and reclaim does not arm before that. False restores the old order: the kept sell starts only after the held dump fills. One kept launch per bag.

- **2026-10-13** — Dump persist requires both sides on the same tick: held bid under `sell_dump_below` and the other side's bid at or over `sell_dump_other_min` (code default 0.30; 0 or null disables). Either miss clears `sell_dump_armed_at`. The check uses the books already fetched that tick. After the dump fires, held-side refires and the kept-side sell do not re-check it. `sell_dump_persist` logs the other bid and uses `why=other_low` when that side blocks. Not in the 13 Oct live list, so a pull takes 0.30.

- **2026-10-13** — Aligned this document to `main` through #258. Body text that still described the 3 Oct 200-share / 40¢ dump desk now follows the code defaults and the operator's 13 Oct live reference: 100 shares, scrap half at 3¢ and 4¢ in the last 180s, dump under 0.67 for 0.5s with a fresh-bid FAK refire, reclaim at 0.90 for 1s with a 0.15 other-side ceiling and a 0.65 stop, hedge retries at the fresh ask capped at 0.15, the hedge keep rule, `post_dump_book`, the between-bags restart rule, a journal-event list, and a knob catalog for every `DEFAULTS` key. `oracle_log_enabled` defaults false. Since #245, `_scrap_oracle_gate` always returns disabled, so `scrap_oracle_veto_enabled` does not block a scrap. Keys absent from the 13 Oct list are not restated as live.

- **2026-10-12** — After `sell_dump_done`, `post_dump_book` journals both legs' best bid and ask about every 0.25s until the window ends (`leg_dumped`, `up_bid`, `up_ask`, `dn_bid`, `dn_ask`, `ttm_s`). `post_dump_log_enabled` defaults true and hot-reloads. The sell tick reuses the books it already fetched and logs after orders. Between ticks a sampler thread reads the book; that read is not on the sell, reclaim, or hedge order path. The journal does not change a trading decision.

- **2026-10-11** — A zero or partial reclaim-hedge fill refires on the hedge thread, up to `reclaim_hedge_retries` (default 3) inside about a second, at the fresh ask with no slippage. An ask above `reclaim_hedge_max_price` (default 0.15) stops the retries and is logged. The first hedge order still pays `reclaim_hedge_slippage` over the ask. The unfilled size is still raised to the $1 and 5-share minimums. The main reclaim buy does not wait. An in-flight refire still defers the next mint and stays in the mint cash reserve. Each attempt logs `reclaim_hedge_retry`; the finish logs `reclaim_hedge_outcome`.

- **2026-10-11** — `reclaim_other_max` (default null) replaces the other-side ask ceiling `1 - reclaim_entry`. A null value keeps that ceiling. Opt-in `reclaim_hedge_enabled` (default false) FAK-buys the other side at the same time as the reclaim buy, at ask + `reclaim_hedge_slippage`, sized from `reclaim_hedge_usd` and raised to the $1 and 5-share minimums when needed. A hedge miss does not cancel the main. After the stop fires, the hedge is kept only if its bid stays above `reclaim_hedge_keep_above` for `reclaim_hedge_keep_persist_s`; otherwise it sells on the dump FAK path. No stop, or a kept hedge, rides to resolution. An open hedge buy defers the next mint, and its USDC is reserved in the mint cash check.

- **2026-10-09** — A sequential mint waits while a reclaim buy is still open, not only after `reclaim_bought`. The entry watch, a posted, in-flight, or uncertain buy, and any filled shares hold the next window until the reclaim is stopped and its stop sell has finished. A closed or settled bag does not.

- **2026-10-09** — Optional late scrap price. `sell_late_price_window_s` defaults to 0. When it is positive and both late prices are set, a known ttm at or under the window uses `sell_threshold_late` / `sell_fak_px_late` for the arm, the persist check, and the FAK cap. The choice is one compare per bag on the ttm the sell tick already has. Suggested live keys: base 0.03/0.03, late 0.04/0.04, window 180. `sell_late_window_s` stays the oracle veto and stays 0.

- **2026-10-09** — A dump or stop FAK that leaves shares unsold refires immediately at a freshly fetched bid, inside `sell_dump_fak_retries`. That covers a partial fill as well as the existing zero-fill miss. The same path serves the held dump, the kept-half dump, and the reclaim stop. Each attempt logs shares sold so far. No ladder and no added sleep.

- **2026-10-07** — Every reclaim send re-checks that tick's entry guards, including ask at or under `reclaim_max_price`. A failed check does not send and restarts the persist. An open reclaim, or a dump or stop sell in progress, defers the next sequential mint until the position is closed or the bag's window has ended. A missing or stale stop quote keeps the stop clock. Each dump or stop refire refetches that leg and sells at the live bid. `reclaim_min_ttm_s` defaults to 0; above 0, entry skips with `too_close`.

- **2026-10-07** — The reclaim FAK limit is `min(ask + reclaim_slippage, reclaim_max_price)`, default 3¢ over the observed ask and hard-capped at 96¢. The first clip is `floor(reclaim_usd / that limit)` so the cent-rounded USDC stays about $100. A top-up uses the same limit; an ask already above the cap logs `reclaim_topup_skipped_cap` and is not posted. Entry, sister confirm, spread, freshness, and the stop are unchanged.

- **2026-10-07** — Reclaim no longer requires the ask to cover `floor(reclaim_usd / ask)`, and it no longer uses `classify_loser` for the sister check. The other side confirms when its best ask is at or under `1 - reclaim_entry` (0.09 at 0.91). Books stay fresh, uncrossed, and within a 10¢ spread. There is no trade-print tape on the sell loop, so a last trade is not a second way to confirm.

- **2026-10-07** — Reclaim FAK buys are market orders sized in USDC truncated to cents, so the maker amount has at most two decimals at tick 0.01 and 0.001. A hard refusal logs `reclaim_fak_reject` and does not take the uncertain-fill path; the same price and size is not posted again. Moving from the buy to the stop logs `reclaim_done` (filled shares, average price, cost, why the top-up stopped). No added REST, sleep, or guard change.

- **2026-10-07 (not live until pull + operator restart)** — Opt-in reclaim after a both-sides dump. `reclaim_enabled` defaults false. Once `sold_dump` is set (and `sell_dump_kept_done` when a kept half applies), the sell loop stays on `sell_armed_poll_s` and FAK-buys about `reclaim_usd` ($100) of the first side that holds at `reclaim_entry` (0.91) for `reclaim_entry_persist_s` (default 0.5s, the same as the dump persist). Explicit 0 fires on the qualifying tick; a negative is rejected. The buy uses the scrap sister-book checks on the books already fetched that tick: no extra REST, no balance read, and no sleep before the FAK. `reclaim_stop_enabled` defaults true: a bid at or under 0.75 for 0.5s FAK-sells the position at the live bid, floored only at the 1¢ exchange minimum. A position still held at resolution uses the existing redeem. There is no merge and no resting bid.

- **2026-10-05** — Lockbot deleted entirely (code, tests, unit file). Copy-trading strategies already removed (#242). Mintbot left live for scrap.

- **2026-10-03 (PR #234, not live until pull + restart)** — Scrap oracle veto.
  - No loser scrap while the in-memory 60s TWAP or the live Chainlink price is within $5 of the strike or on the scrapped leg's side (`scrap_oracle_veto_enabled` true, `scrap_oracle_veto_usd` 5.0, `scrap_oracle_veto_stale_s` 3.0, `scrap_oracle_veto_use_live` true). It is re-checked every tick and right before the order is sent.
  - A stale live price leaves the average alone; both stale falls back to the old scrap. Either logs `scrap_oracle_stale`.
  - The RTDS feed gains `recv_ts` and a hot mode for the last 360s: a 5s watchdog, a 2s redial and the Gamma audit deferred. Dump is unchanged.
- **2026-10-03 13:25 IST** — Aligned to the live VM.
  - **Size and capital.** 200 shares a side (was 100). `mint_sequential` on: one bag at a time, minted in [start − 30s, start + 240s] once the previous winner is sold or its window has ended. Two-bag `max_open_sets` / lookahead is no longer the live mode.
  - **Redeem.** `redeem_enabled` on, `redeem_startup_sweep` off.
  - **Scrap.** Loser ≤ 3¢, fraction 0.5 (sell 100 / keep 100), one 1¢-floor FAK, only with ≤ 360s left. `sell_persist_s` cut from 5 to 3 on 3 Oct; last-minute persist 2s within a 90s window (was 60s).
  - **Dump.** Under 0.40 for 2s with ≤ 240s left. `sell_dump_also_kept` true since 10:31 IST (#232): the kept half is sold in the same event, capping a dump at roughly −$50 to −$70. Worked examples in §21, §22 and Appendix E are redone at 200/side.
  - **Unchanged.** `sell_winner_min` 0.9995, oracle veto off, WhatsApp danger alert on (70¢ for 5s), strike from the 60s TWAP at the open with the Gamma `priceToBeat` check (#229), settlement on the 60s Chainlink average.
  - **Not live.** `sell_dump_tiers` (#231) and the post-dump kept stop (#228) are unmerged.
- **2026-09-30** — Partial scrap, 100-share bag, floor sweep, TTM gates, relay gas, pending reserve, `bag_risk`, oracle strike fix.
- **2026-09-19** — Sequences and redeem chapter; buybot tour retired.

*End of technical design.*
