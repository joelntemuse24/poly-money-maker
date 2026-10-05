# Poly Money Maker: technical design of the live 15m mint system

This document explains the system as it runs on **3 October 2026** (live knobs confirmed by the operator at 13:25 IST). It follows `mintbot.py`, the live atomic-mint trader, from discovering the next BTC 15m Up/Down window through a confirmed complete-set mint, the loser scrap (whole or partial), the held-leg dump, winner cash-out or redeem, and the local state that survives a restart. Python excerpts are from `main` after the 30 Sep follow-up fixes (recorded fill prices, oracle tape rotation, startup banner, `count_kept_loser_as_open`, explicit-zero seconds knobs, `poll_s ≥ 1`). The live VM runs `main` through #232: sequential bags and auto-redeem (#230) and `sell_dump_also_kept` (#232, live since 10:31 IST on 3 Oct). The [changelog](#changelog) lists what changed in this revision. Hypothetical trades illustrate arithmetic; they are not performance claims.

Where a knob matters, the text gives the **code default** (`DEFAULTS` in `mintbot.py`) and the **live value** from the VM's gitignored `strategy_mint.json`. Keys the operator confirmed on 3 Oct 2026 13:25 IST are marked as such in [§29](#section-29); the rest are from the 30 Sep read. Live JSON wins for every key it sets. The repo never commits a live JSON.

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
  - [Held-leg dump: under sell_dump_below (80¢ code, 40¢ live)](#section-19)
  - [Live-bid FAK vs floor FAK](#section-20)
  - [Hypothetical lifecycle: 200-share bag, scrap 100 @ ~3¢, winner redeems](#section-21)
  - [Hypothetical lifecycle: dump at ~31¢, with the kept half sold too](#section-22)
  - [What this code does not prove](#section-23)
- [Part IV — The buy/ helpers and pathlog](#part-iv)
  - [Ownership map](#section-24)
  - [buy/mint_sell.py policy helpers](#section-25)
  - [buy/market.py, book.py, chain.py, contracts.py, mint_gas.py, log_archive.py](#section-26)
  - [pathlog.py: public book recorder](#section-27)
- [Part V — Operations, verification and sharp edges](#part-v)
  - [systemd units](#section-28)
  - [Knobs: code default vs live (3 Oct 2026)](#section-29)
  - [Deploy boundary (VM is source of truth)](#section-30)
  - [Testing without constructing a live bot](#section-31)
  - [Landmines](#section-32)
  - [Glossary](#section-33)
  - [Source snapshot](#section-34)
- [Part VI — Sequences & redeem](#part-vi)
  - [End-to-end mint sequence](#section-35)
  - [Sell-side sequence (winner / dump / loser)](#section-36)
  - [Redeem path (what exists vs what does not)](#section-37)
  - [State after expiry](#section-38)
- [Part VII — Lockbot](#part-vii)
  - [TWAP-lock taker](#section-39)
- [Changelog](#changelog)

<a id="part-i"></a>
# Part I — Picture

<a id="section-1"></a>
## The opportunity and its limits

Polymarket’s **BTC Up or Down 15m** markets are binary windows. Each window asks whether BTC finishes the quarter-hour up or down relative to that window’s open. Outcome tokens pay **$1** of collateral if that side wins and **$0** if it loses.

A **complete set** is one Up share plus one Down share for the same condition. On-chain Conditional Token Framework (CTF) mechanics let you **split** $N of collateral into N Up + N Down. That is a **mint**, not a directional buy. After the split you hold both legs. Economic edge then comes from:

1. Selling some or all of the **loser** cheaply once the book clearly prices it near zero (and the opposite near one), and
2. **Redeeming** the winner at $1 after resolution. The live `sell_winner_min` of 0.9995 means the CLOB cash-out path practically never fires ([§18](#section-18)). Live, mintbot's own `mintbot-redeem` thread submits that redeem ([§37](#section-37)).

Live size is `shares=200`: about $200 mints 200 Up + 200 Down.

- **Normal win.** With `sell_scrap_fraction=0.5` the bot scraps 100 loser shares (a floor FAK that typically fills at 2–3¢, so about $3) and **keeps** the other 100 to resolution. If the favourite wins, the bag nets about +$3 before fees ($200 redeem + $3 scrap − $200 mint), and the kept 100 expire worthless.
- **Flip with no dump.** If the "loser" flips and wins, the kept 100 pay $100. That keep is paid-for insurance: it halves the scrap income in exchange for a floor under the flip case ([§17b](#section-17b)).
- **Dump.** Live, a held dump also sells the kept 100 (`sell_dump_also_kept`), so a dumped bag exits flat at roughly −$50 to −$70 whichever side wins ([§22](#section-22)).

**What this bot is not:** it is not the old hourly FAK entry bot. It does not chase 90–95¢ asks on one side with an oracle. It does not “hedge” by buying the opposite leg after entry. The post-loser exit under `sell_dump_below` (code 80¢, live **40¢**) is deliberately named a **held dump / sell-side pass**, not a hedge.

**Risk concentration:** live runs **one bag at a time** (`mint_sequential` true). The next window is minted from 30s before its open to 240s after, and only once the previous bag's winner is sold or its window has ended ([§12](#section-12)). Without sequential mode, `max_open_sets` (code 1) plus one adjacent-window exception caps capacity; the live file still sets 2, but sequential mode ignores it. A failed relayer mint is blocked for `mint_fail_cooldown_s` (30s) and gives up after `mint_max_attempts` (3) tries so a hot remint loop cannot run. A nearer window in that cooldown, or already at the attempt cap, does not idle the cycle: the next eligible future is selected in the same pass and `mint_attempt` is logged for that slug. After an active bag at start `T`, selection never goes backwards (`start_ts < T+900`). A zero-fail window is preferred over retrying a failed condition while a slot is free. A restart ghost intent stuck at `submitting` with no `transaction_id` is auto-failed after `mint_submitting_timeout_s` (default 90s, `0` disables) so `wait_submit` cannot wedge the desk forever. At 200 shares a single dump event, false or real, costs about $50–$70 with the kept half sold too ([§22](#section-22)). Scaling share count scales edge and left tail together.

<a id="section-2"></a>
## Processes, wallet identities and files

Live tree: `/home/ntemusejoel/poly-money-maker` on Google Cloud VM `poly-vm`.

| Unit | Program | Role | Observed |
|---|---|---|---|
| `polymintbot.service` | `mintbot.py` + gitignored `strategy_mint.json` | Atomic mint + sells | **stopped** as of 2026-10-05: inactive, still enabled at boot. Code and `strategy_mint.json` are unchanged. Do not start it unless the operator asks |
| `polylockbot.service` | `lockbot.py` + gitignored `lockbot.json` | BTC 5m/15m taker, hold to settlement | **installed, enabled, dry_run** since 2026-10-05 00:01:40 UTC (`ded7bf2`). No live orders |
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
| `lockbot.example.json` | Lockbot template. `dry_run` true. Not authorization to go live |
| `lockbot.json` | Lockbot knobs on the VM (gitignored). Hot-reloaded |
| `positions_lockbot.json` | Paper ledger (gitignored) |
| `positions_lockbot_live.json` | Live ledger (gitignored). Loss, exposure, and spend when `dry_run` is false |
| `STOP_LOCKBOT` | If present, lockbot exits |
| `.lockbot.lock` | Single-instance flock |
| `logs/lockbot.jsonl` | Lockbot log, rolls at 20 MB |
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

A **sized bid** in this codebase is the best bid that still has at least `sell_min_bid_size` (1.0 code and live) size. Thin sub-share prints are ignored so dust at 2¢ does not arm a 100-share scrap. Depth beyond the top level is only logged (`sell_book_depth`), not gated on.

**FAK** (Fill And Kill) / marketable limit sell: take liquidity down to your limit; cancel the rest. A sell FAK matches the **best** bids first, so a FAK with a 1¢ limit against a book of 3¢ / 2¢ / 1¢ fills the 3¢ level first. The loser sweep relies on that ([§17](#section-17)).

Three prices that must not be conflated:

1. **Arm threshold** — loser ≤ `sell_threshold` (code 2¢, live **3¢**) with opposite ≥ 90¢, or held bid < `sell_dump_below` (code 80¢, live **40¢**).
2. **Limit posted** — `sell_floor` for the loser sweep (code 2¢, live **1¢**); live sized bid for winner/dump.
3. **Average fill** — what actually cleared (usually better than the limit). Live sweeps post 1¢ and log `avg_px` 0.02–0.03 in `sell_scrap_sweep`.

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
| CLOB API | Sized bids; FAK sells; resting scrap sells (off live) |
| Data API | Optional position checks; `redeemable=true` startup sweep (opt-in redeem) |
| Relayer v2 | Submit PROXY mint batch (and opt-in redeem batch); poll `STATE_*` |
| Polygon RPC | CTF balances / inventory confirm; one `eth_estimateGas` per mint or redeem submit; `payoutDenominator` / `payoutNumerators` / `isApprovedForAll` for redeem |
| Polymarket RTDS | Recording-only Chainlink BTC/USD 60s TWAP (`crypto_prices_twap_sixty`) |
| Gamma `/events?slug=` (oracle tape) | Recording-only post-window check of `eventMetadata.priceToBeat` / `finalPrice` against the tape |
| ntfy (optional) | Operator push on mint/sell (thread pool, off the tick) |
| CallMeBot (optional) | WhatsApp danger-zone alert on a scrapped bag (opt-in: scrap fill, held dump fill); see below |

**WhatsApp alerts (`buy/whatsapp_notify.py`).** These need `CALLMEBOT_PHONE` (with country code, e.g. `+353…`) and `CALLMEBOT_APIKEY` in the process environment.
- **Danger zone (`notify_danger_whatsapp`, default on, on live).**
  - Watch starts only after the bag's loser scrap has filled (`sold_leg` set). Watch stops once the bag has dumped or sold its winner (`sold_dump` / `sold_winner`), or once the window has ended.
  - Each sell tick, `danger_tick` gets the same sized best bid on the held leg that the dump reads (`bids[held]`, one fetch per tick). The bid must stay under `notify_danger_px` (0.70) continuously for `notify_danger_hold_s` (5s). A bid at or above the line, or no sized bid, resets the timer.
  - It then logs `danger_zone` (`condition_id`, `slug`, `leg`, `bid`, `threshold`, `hold_s`, `below_s`, `ttm`, `held_shares`, `kept_shares`, `dump_below`, `whatsapp`, `dry_run`). This line is logged even when WhatsApp is off, unconfigured or in dry run.
  - WhatsApp gets one message per bag, e.g. `Mintbot DANGER: 11:30 bag | UP bid 0.66 <70c for 5s | 3m12s left | holding 200 UP + 100 DN | dump arms <40c in last 240s` (`dump off` when `sell_dump_enabled` is false).
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

Top-level `positions_mint.json` shape (one live 200-share bag after a partial scrap):

```json
{
  "intents": {
    "<condition_id>": {
      "status": "confirmed",
      "slug": "btc-updown-15m-…",
      "shares": 200.0,
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
      "sell_filled": 100.0,
      "sell_limit": 0.01,
      "sell_fill_px": 0.03,
      "sell_fill_px_shares": 100.0,
      "sell_scrap_held": 200.0,
      "sell_scrap_fraction": 0.5,
      "sell_scrap_target": 100.0,
      "sell_scrap_keep": 100.0,
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

`load_strategy` copies only keys that exist in `DEFAULTS`; anything else in the live file (for example the leftover `sell_persist_skip_ttm_s`) is ignored. `validate_strategy` enforces `sell_floor ≤ sell_threshold < sell_opposite_min < sell_winner_min < 1`, `sell_floor ≤ sell_fak_px ≤ sell_threshold`, `0 < sell_scrap_fraction ≤ 1`, mint gas bounds, `sell_dump_persist_s` / `sell_cooldown_s` / `sell_scrap_rest_min_ahead_s` ≥ 0, and `poll_s ≥ 1` (it was 2 until 30 Sep; the VM carried a local patch for live `poll_s: 1`, [§32](#section-32)).

Every tick of both loops re-reads the strategy file (`_reload_cfg`), so a knob edit takes effect within a second or two without a restart. If the reload fails validation, the loop logs `strategy_reload_fail` and keeps the **previous** config with `entry_enabled` forced false: sells continue, new mints stop.

Sleep cadence: the sell loop sleeps `poll_s`, or `min(poll_s, sell_armed_poll_s)` while any bag is sell-hot (loser armed, or loser sold and dump/winner not done). Code defaults 5s / 2s; live **1s / 1s**. Mint always sleeps `poll_s`. Armed poll does **not** replace persist math. Shared intent writes take `STATE_LOCK`; book / FAK / Gamma / relayer / RPC stay outside that lock (`_io_unlocked`).

Incident `btc-updown-15m-1789905600`: after `loser_done`, next-window mint ran on the same thread and delayed the first dump look ~16s while UP cliffed 97→31. A skip-Gamma bandage (draft PR #193) is unnecessary once the loops are independent.

`dry_run=true` must not submit mints or live sells. Live VM has `dry_run=false`, `entry_enabled=true`, `sell_enabled=true`.

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

Without sequential mode (code default), `sold_loser` frees the `max_open_sets` slot, even when half the loser is kept. **Live runs `mint_sequential`**, where the gate is `seq_busy_bag` instead: a sold loser does not free it, and only `sold_winner` (also set by the held dump) or the window end does ([§12](#section-12)). `count_kept_loser_as_open: true` (opt-in, default false) keeps a bag with kept loser shares counted until `end_ts + 120s`, or until the kept leg itself is cashed as the winner: fewer mints, capped open exposure ([§12](#section-12)). In the non-sequential mode the mint loop can claim the adjacent window **while** the sell loop runs dump persist on the previous bag. Do not skip Gamma because a bag is hot.

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

`oracle_log_enabled` defaults true (live true). When the feed is down the thread logs the stall and keeps going. Mint eligibility, winner cash-out, and held dump do not read the tape. Loser scrap reads only the in-memory feed sample, for the scrap oracle veto (#234, §17). It never reads the tape file. The late-window veto stays off while `sell_late_window_s` is **0** (code default and live). `sell_oracle_edge_floor_usd`, `sell_oracle_edge_per_ttm`, and `sell_oracle_stale_s` also default to **0** (live 0), so raising only the window does not restore the old $25 / 1.5×TTM / 5s-stale veto. `sell_oracle_edge_persist_s` is **3** in code and **0** in the live file; it only matters when the window is positive. If the veto is ever turned back on, #214 keeps its edge-persist clock running on the kept scrap leg while the loser book is momentarily empty, instead of resetting it. The tape stays on for audit.

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
| CLOB `update_balance_allowance` on every FAK | Low | Leftover — extra ~100ms on fire |
| Data API `positions` after Gamma | Low | Leftover — mint-only |
| Sequential reconcile per pending intent | Low | Leftover — mint-only |
| pathlog JSONL / Gamma I/O | n/a | Separate process; does not block mintbot |
| Redeem vs sell | Separate thread | Opt-in `redeem_enabled` runs on `mintbot-redeem`; sells stop at `end_ts` and never wait on it |
| Sequential UP/DN `/book` | Already fixed | Parallel `ThreadPoolExecutor` |

<a id="section-11"></a>
## Eligibility: not-yet-open 15m windows

`eligible_markets` keeps markets that:

- have **not** started (`start_ts > now`),
- open within `(enter_min_ttm_min, enter_max_ttm_min]` minutes (default 0–45 so the window after a bag booked near 30m out stays visible — two 15m steps past a market that is about to open),
- are active, not closed, not neg-risk,
- optionally `accepting_orders`.

**The bot never mints a live (already open) window** in the default (non-sequential) mode. That is why missing the adjacent lookahead used to skip an entire quarter-hour: by the time the prior bag expired, the next market was already open and ineligible.

**Sequential mode (`mint_sequential`, opt-in in code, on live; `buy/mint_sequence.py`).** `seq_eligible_markets` replaces `eligible_markets`:

- A market is eligible only in `[start - mint_seq_lead_s, start + mint_seq_cutoff_s]` (30s / 240s). A window that is already open is allowed up to the cutoff.
- It must also be active, not closed, not neg-risk, optionally `accepting_orders`, and not ended.
- `enter_*_ttm_min` is not read.

A split is price neutral, so minting at the open costs the same as 14 minutes earlier. The sell path keys off `end_ts` and the books, not the mint time.

<a id="section-12"></a>
## Capacity: max_open_sets and adjacent lookahead

`open_intent_count` counts intents in `ACTIVE_STATUSES` whose market has not been expired for >120s, **excluding** intents with `sold_loser` / `sold_leg`. Winner-only redeem holds, including a bag that kept half its loser, must not consume the mint slot. That is the default.

`count_kept_loser_as_open` (code default **false**, example false, not in the live file) changes one thing: with it true, a sold-loser bag whose partial scrap kept shares (`kept_loser_open`: `sell_scrap_keep > 0`, and the kept leg has not been cashed as the winner) still counts toward `max_open_sets` in `open_intent_count`, `mint_slots_full` and `mint_discovery_capped` until the usual `end_ts + 120s` cutoff. A held-leg dump does not release it; the kept shares are still open. The trade-off: at `max_open_sets=2` and fraction 0.5, each scrapped bag keeps holding a slot, so fewer windows get minted (less scrap income), in exchange for never carrying more than `max_open_sets` bags with live loser exposure at once. The adjacent-window lookahead still applies.

`mint_slots_full(state, cfg, now, candidate_start_ts)`:

- If fewer than `max_open_sets` full bags → not full.
- Else allow **only** the adjacent next window: `soonest_full_end ≤ candidate_start < soonest_full_end + 900`.
- If we already hold that next window, or the candidate is further out → full.

Code default: `max_open_sets=1`. The live file still sets **2** (since 23 Sep 2026), but `mint_sequential` is on live, so neither the slot count nor the adjacent lookahead is used. With `1`, holding 1:30–1:45 still permits minting 1:45–2:00 beforehand; it does **not** permit minting 2:00–2:15 while 1:30 is still a full bag. A nearer candidate that does not fit the cap is skipped in the same pass so that adjacent window is still selected.

Because the adjacent lookahead mints about 14 minutes before the open, two bags overlap and tie up two bags of pUSD, even at `max_open_sets=1`. That overlap is why live moved to sequential mode.

**Sequential capacity (live).** With `mint_sequential` on, `seq_busy_bag` replaces `mint_slots_full` in `select_mint_candidate` and in `_claim_mint_intent`.

- **Busy bag.** A busy bag is any other active, non-dry intent whose window has not ended and whose `sold_winner` is not set. The held dump also sets `sold_winner`. A sold loser alone does not free the gate, because the winner's capital is still in tokens.
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

Operational stance: the reproducible fix is the explicit mint `gas_limit` above. Sister top-up stays on the library default; a single pUSD transfer is far under 500k. Do not estimate gas on the sell loop. The live JSON sets no `mint_gas_*` key, so code defaults apply.

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

The on-chain pUSD balance still shows cash that a submitted split has not consumed yet. In the old two-bag mode (`max_open_sets=2`, 100-share bags), two mints could be in flight within seconds of each other. Without the reserve, both saw $150 and both submitted $100, and one failed on-chain. Live sequential mode mints one bag at a time, but the reserve still guards a restart or a retry. `mint_cash_block` returns `reason="pending_reserve"` (log `mint_skip_pending_reserve`, heartbeat `pending_reserve`) when the shortfall is only the reservation, or `no_balance` when the wallet is simply short. A confirm, failure or stale-submit timeout drops the intent out of the reserved set.

**3. Existing inventory guard.** `before_up` / `before_dn` are read from chain; any balance above `position_tolerance` returns `existing_position`. Those two numbers are also stored on the intent: reconcile later confirms `observed ≥ before + shares`.

**4. Calldata.** `build_atomic_mint_calls(pUSD_address=…, adapter_address=…, condition_id=…, shares=200.0)` returns two frozen `ContractCall`s: `approve(adapter, 200_000_000)` and `splitPosition(pUSD, 0x0, conditionId, [1, 2], 200_000_000)`. `200.0 × 1_000_000` is checked to be an exact integer ([§6](#section-6) aside).

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

`manage_sells` runs only for intents in `confirmed`, `confirmed_waiting_inventory`, `mined` or `executed`, and only while `sell_window_open` (strictly before `end_ts`). For each intent it fetches sized Up/Down bids in parallel, caches them, then evaluates three exits **in this order**:

1. **Winner cash-out** (rich bid path, [§18](#section-18))
2. **Held-leg dump** (only after loser sold; weak bid path, [§19](#section-19))
3. **Loser scrap** (cheap bid + rich opposite, [§17](#section-17) / [§17b](#section-17b))

`sell_cooldown_s` (3s code and live) suppresses the next FAK after any attempt on that bag.

The first tick after the window closes does three things for that bag: cancel a live scrap rest (`window_end`), emit `sell_scrap_outcome window_end` if a partial plan never finished, and emit one **`bag_risk`** line. `bag_risk` is log-only (#222). It carries the scrapped leg, `ttm_at_scrap`, scrap average price, loser best-bid size at scrap, the held leg's minimum bid (and when), seconds the held bid spent below 0.80 / 0.65 / 0.50, the time-weighted mean `1 − held_bid` after scrap, and dump TTM / price if a dump fired. Its `partial` field means "this process first saw the bag already scrapped" (a restart mid-bag), **not** a partial scrap. The counters live in memory; a failure there is swallowed and never touches orders.

<a id="section-17"></a>
## Loser scrap: arm, persist, time gate, floor sweep

Values below are **code default / live**.

**Arm (`classify_loser`).** One leg's sized bid ≤ `sell_threshold` (0.02 / **0.03**) and the other leg's sized bid ≥ `sell_opposite_min` (0.90 / 0.90). Both cheap → `both_cheap`, no arm. Cheap leg with a weak or missing opposite → `wick_unconfirmed`, no arm.

**Time gate (#222).** `sell_scrap_max_ttm_s` (0 / **360**; example 600). While seconds-to-close is above the cutoff, `scrap_time_gate_open` is false and the arm, the persist clock, and every scrap fire (sweep, blind, new rest) are blocked. A cheap leg seen while gated logs `sell_scrap_time_gated` (`condition_id`, `slug`, `leg`, `bid`, `ttm`, `cutoff`) at most once per 15s per condition/leg. Unknown TTM (no `end_ts`) leaves the gate **open**, the opposite of the dump gate. Because the arm itself is blocked, persist starts only once TTM ≤ cutoff: a bid that was cheap at T−8m still waits the full persist after T−6m. Live, no loser is sold before the last six minutes.

**Persist.** `loser_scrap_persist_s`: `sell_persist_s` (5 / **3**; live cut from 5 to 3 on 3 Oct) normally, `sell_persist_last_min_s` (2 / 2) when `0 < TTM ≤ sell_persist_last_min_window_s` (60 / **90**). That 2s clock applies right through close; the old late-TTM skip is gone (#218) and a leftover `sell_persist_skip_ttm_s` in the live file is ignored. `sell_persist_skip_when_sized` (false / false) would skip persist when depth at the first rung covers the order; it is off, so a sized book waits too. The effective value is recomputed each tick, so an arm started on the 3s clock (live) can fire on the 2s clock without resetting `sell_loser_armed_at`.

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

`_sell_inventory` reads the on-chain balance (lock released) and returns `(min(shares, balance), latch)`. `inventory_latch` distinguishes `await_inventory` (zero before any inventory was ever seen: the split may still be settling, so skip) from `already_flat` (zero after inventory was seen: finish without a POST) from `has_inventory`. Then the size is clipped by the partial plan ([§17b](#section-17b)) and `_fire_loser_scrap` posts.

**The order: one floor sweep (#216).** `sell_scrap_sweep_enabled` (true / true: the live file does not set it). `loser_scrap_post(sweep=True, …)` returns `{"mode": "sweep", "limits": [sell_floor], "size": remaining}`: one FAK at `sell_floor` (0.02 / **0.01**) for the whole remaining scrap size, not clipped to top-of-book depth. Because a sell FAK matches the best bids first, the live 1¢ sweep takes the 3¢ level, then 2¢, then 1¢ in one round trip. That is how the live "3¢ → 2¢ → 1¢" scrap happens: it is one order, not three. The fill is logged as `sell_scrap_sweep` (`limit`, `size`, `avg_px` from `takingAmount / size`, `offered`, `status`), preceded by one `sell_book_depth` line for the fire. After the POST the balance is re-read; a remainder waits for the next fire. With the flag false, `loser_ladder_limits` walks every 1¢ from `min(sell_fak_px, live bid)` down to the floor (#215), each rung clipped to displayed top-rung depth (`loser_partial_fak_shares`); live that would be 3¢, 2¢, 1¢ as separate FAKs. The flag is read every tick.

**Done.** `done = dry_run or balance_flat or sold_total >= post_size - tol`. On done, `_finish_scrap` sets `sold_loser=True`, `sold_leg`, `sold_loser_at` once, resets the oracle arm, and (if a plan exists) logs `sell_scrap_outcome`. `sell_filled` accumulates shares. `sell_limit` still stores the **last limit posted** (the floor, under sweep). The real price goes to `sell_fill_px`: `record_fill_px` folds each fill's `takingAmount / size` into a share-weighted average across sweep, ladder, blind and rest fills, with `sell_fill_px_shares` counting the priced shares. A reply with no `takingAmount` is skipped rather than guessed, and a rest fill (the order poll reports size, not price) is recorded at the rest's own limit. `recorded_fill_px(intent, "sell_fill_px", "sell_limit")` is what the cheap-winner gate and `bag_risk` read; it falls back to `sell_limit` only for state written before this field existed. Then `sell_loser_done` (now with `avg_px`) and ntfy.

**Misses.**

- Blind FAK: on `empty_keep_arm` / `empty_fak_keep_arm`, `loser_blind_fak_due` fires a FAK at `sell_scrap_blind_px` (0.01 / 0.01 default) no more often than `sell_scrap_blind_backoff_s` (3s), if inventory is present and no rest is live. Logs `sell_scrap_blind`. Same time gate and same partial size clip.
- Post-miss rest: after a FAK returns `no orders found` while still armed, `_place_scrap_rest` posts a resting SELL at `scrap_rest_px(sell_scrap_rest_px, live or last-seen loser bid)` = the lower of the two, GTD when expiry is ≥ `sell_scrap_rest_min_ahead_s` (180s) ahead, else GTC (`resting_tif`). It is cancelled on fill, window end, loser no longer qualifying, or a hard oracle block when that veto is on. An empty book alone does not pull it. **Live has `sell_scrap_rest_enabled=false`**, so no rest is ever placed; code default is true (rest px 0.02 code / 0.01 live).

**Late-window oracle veto: off.** `sell_late_window_s` is 0 in code and live, so `in_late` is always false and `late_oracle_scrap_ok` would return `outside_late_window`. The floor / per-TTM / stale knobs are 0 as well, so re-enabling only the window does not bring back the old $25 / 1.5×TTM / 5s-stale rule. The helper still implements that rule when the arguments are passed explicitly. With these values, CLOB gates only.

**Scrap oracle veto (#234, on in code; takes effect live after a pull and restart).** A separate, any-time check that blocks a loser scrap while the oracle still favours that leg. It was added after `btc-updown-15m-1791039600` (3 Oct, 16:00–16:15 IST): 100 Up were scrapped at 2–3¢ with 31s left while the 60s TWAP sat $0.42 above the strike. Up won, and the bag lost about $97.

- `margin = twap − strike`. The TWAP is the latest RTDS 60s sample, and the strike is the bag's `open_ref` (`OracleLogService.bag_view`). Scrapping Up is blocked while `margin > −scrap_oracle_veto_usd`. Scrapping Down is blocked while `margin < +scrap_oracle_veto_usd`. Exactly $5 against goes ahead.
- Live price: with `scrap_oracle_veto_use_live` (true), the same rule also runs on `live_margin = live_price − strike`. `live_price` is the latest Chainlink BTC/USD print from the `crypto_prices_chainlink` topic on the same RTDS websocket (about one a second). A scrap is blocked if either margin says the leg is winning or within $5, so it only goes ahead when both are more than $5 against it.
- Knobs: `scrap_oracle_veto_enabled` (true), `scrap_oracle_veto_usd` (5.0), `scrap_oracle_veto_stale_s` (3.0) and `scrap_oracle_veto_use_live` (true). They are hot-reloaded, and a bad or negative value falls back to the default.
- It is ANDed into the scrap time gate (`scrap_ok = scrap_ttm_ok and not veto`). It therefore blocks the arm, the persist, the sweep or ladder fire, the blind FAK and a new post-miss rest. It also cancels a resting scrap sell (`oracle_blocks`).
- A block resets `sell_loser_armed_at`, so once the TWAP is more than $5 against the leg the scrap still waits its full persist, inside the 360s cutoff and the 3¢ trigger.
- `_scrap_oracle_gate` runs again on its own right before `_fire_loser_scrap` and before the blind `_fak_sell`.
- No I/O: it reads the TWAP sample and the live print the `oracle-rtds` websocket thread holds in memory. Measured cost with both is about 7µs per call (p99 about 9µs), against about 6µs for the average alone.
- Fallback: a reading is stale if it is older than `scrap_oracle_veto_stale_s` by local receive time (`recv_ts`), its Chainlink stamp is more than 10s old, or it is missing. A stale live price leaves the average to decide alone (`fallback` `twap_only`); a stale average leaves the live price alone (`live_only`). If both are stale, or the strike is missing, there is no veto and the scrap runs as before (`fallback` `none`).
- Logs:
  - `scrap_oracle_veto` on a block (`slug`, `side`, `bid`, `ttm`, `twap`, `strike`, `margin`, `live_price`, `live_margin`, `threshold`, `why`, `basis`, `phase` arm/fire/blind, `age_s`, `live_age_s`). `why` is `oracle_favors_leg` / `within_threshold` for the average, or `live_favors_leg` / `live_within_threshold` for the live price. At most once per 5s per bag.
  - `scrap_oracle_stale` (`level` WARNING, `reason`, `fallback`, `age_s`, `live_age_s`) whenever a reading drops out, with the same throttle.
  - A scrap that goes through carries `oracle_margin` / `oracle_twap` / `oracle_live_price` / `oracle_live_margin` / `oracle_strike` / `oracle_why` / `oracle_basis` on `sell_scrap_sweep`, `sell_scrap_blind` and `sell_loser_done`, plus `intent.sell_scrap_oracle_margin` and `intent.sell_scrap_oracle_live_margin`.
  - Each `oracle_twap` tape row also carries the live print held at write time (`live_price`, `live_ts`).
- Dump, `sell_dump_also_kept`, winner and mint never call it. The late-window veto above stays off.

Wallet A never posts a bid.

<a id="section-17b"></a>
## Partial loser scrap: sell_scrap_fraction

`sell_scrap_fraction` (1.0 / **0.5**), #223. At 1.0 nothing below applies and the whole loser is scrapped, same posts as before. Below 1.0 the bag scraps a locked share count and keeps the rest to resolution.

**Plan lock at the first fire.**

```python
def scrap_share_plan(held, fraction):
    if frac >= 1.0 - 1e-12:
        return held_f, 0.0
    target = float(math.floor(held_f * frac + 1e-9))   # whole shares
    return target, held_f - target
```

`_scrap_post_shares` calls `_lock_scrap_plan` the first time a scrap would post (sweep, ladder, blind, or rest). `held` is the on-chain loser balance from `_sell_inventory` at that moment (200 live). The lock writes `sell_scrap_target` (100), `sell_scrap_keep` (100), `sell_scrap_held` (200), `sell_scrap_fraction` (0.5) onto the intent and logs **`sell_scrap_plan`** once. Later fires reuse the stored numbers even if the fraction knob changes mid-bag.

**Order size on every fire.**

```python
def scrap_order_shares(*, target, keep, filled, inventory):
    remaining = max(0.0, target - filled)
    room = max(0.0, inventory - keep)
    return min(remaining, room)
```

Two caps: never more than what is left of the target, and never so much that the balance would dip into the keep. `filled` is `sell_filled`, updated by sweep, ladder, blind and rest fills.

**When the scrap counts as done.** `scrap_target_met` returns true when `filled ≥ target − tol` (`target_filled`) or a known balance is ≤ `keep + tol` (`balance_at_keep`, or `flat` when keep is 0). If a fire computes an order size under 0.01, the bag is finished without a POST using that reason. Either way `_finish_scrap` sets `sold_loser` / `sold_leg` exactly as a full scrap does, so the mint slot frees and the held dump becomes eligible.

**Outcome line.** `_log_scrap_outcome` writes **`sell_scrap_outcome`** once per bag that has a plan: `held`, `fraction`, `target`, `keep`, `filled`, `outcome` ∈ `target_filled`, `balance_at_keep`, `flat`, `rest_filled`, `dry_run`, or `window_end` (the window closed before the target filled). The 30 Sep log (100-share bags) showed the expected triple each bag: `sell_scrap_plan` (100 → 50/50), `sell_scrap_sweep` (limit 0.01, size 50, avg 0.02–0.03, `matched`), `sell_scrap_outcome target_filled`. At the live 200 shares the same triple reads 200 → 100/100 and a sweep of 100.

**What happens to the kept shares.** Nothing sells them on the scrap, blind or rest paths. Two exits remain:

- **Held dump.** With `sell_dump_also_kept` on (live since 3 Oct, #232), the held dump also sells them in the same event ([§19](#section-19)). With it off, the dump sells only the *other* leg.
- **Winner path.** The winner path can sell them only if that leg's sized bid reaches the base `sell_winner_min` (0.999 / **0.9995**); the cheap 0.99 winner gate never applies to them. `kept_leg_below_winner_min` enforces that and logs **`sell_keep_winner_blocked`** once per bag. Even at the base minimum, the winner FAK size on the kept leg is capped at `sell_scrap_keep`. In practice, unless a dump fires, the kept 100 sit to resolution: $0 if the favourite wins, $100 if the scrapped leg flips and wins. The live redeem thread then redeems them with the rest of the bag ([§37](#section-37)).

<a id="section-18"></a>
## Winner cash-out: held for redeem in practice

`sell_winner_min` is 0.999 in code and **0.9995** live. Unconditional 0.99 cash-out was rejected: it cuts margin versus redeeming at $1.

**Cheap-loser gate.** If `sold_loser`, the recorded loser fill (`sell_fill_px`, else `sell_limit` for old state) ≤ `sell_winner_cheap_if_loser_le` (0.03 code) **and** that fill + `sell_winner_min_cheap` > 1.0, then `effective_winner_min = min(winner_min, sell_winner_min_cheap)`. Live sets `sell_winner_cheap_if_loser_le = -1.0` (and `sell_winner_min_cheap = 0.999`), so `winner_cheap_decision` always returns `loser_above_cheap_gate`: the cheap path is off whatever the fill. Before 30 Sep the gate compared the 1¢ floor limit rather than the real 2–3¢ fill ([§17](#section-17)); with the code defaults that made a 2¢ scrap look like 1¢ + 99¢ = flat and kept the cheap path shut.

**Fire.** `winner_cashout_leg` picks the unique leg whose sized bid is ≥ `effective_winner_min`. If that leg is the kept loser leg, the kept-leg block above applies. The same `persist_ready` clock (`sell_persist_s`) must hold, then `sell_fire_decision("winner")` re-checks. `_sell_inventory` gives the size (capped at `sell_scrap_keep` on the kept leg). The limit is the live bid clamped into the CLOB range:

```python
posted, clamped, why = winner_sell_limit(live_px, clob_max=0.99, clob_min=0.01)
```

A 0.99 sell FAK still fills resting 0.995–0.999 bids; `sell_winner_limit_clamped` is logged when live > posted. Full fill sets `sold_winner`.

**Live reality.** Books near resolution quote on a 0.001 tick, so the highest possible sized bid is 0.999, which is under 0.9995. With the cheap gate closed, the winner path does not fire and the winner is held. After `end_ts` the sell loop stops. Live, `redeem_enabled` is on, so the `mintbot-redeem` thread redeems the bag from `end_ts + 60s`, once the condition has resolved on-chain (measured 53–87s after the end) ([§37](#section-37)).

<a id="section-19"></a>
## Held-leg dump: under sell_dump_below (80¢ code, 40¢ live)

A sell-side circuit breaker added 19 Sep 2026. It is **not** a hedge.

Preconditions (all required), code default / live:

- `sell_dump_enabled` (true / true)
- `sold_loser` (or truthy `sold_leg`); a partial scrap counts once its target is met
- not already `sold_dump` / `sold_winner`
- `sold_leg` is `"up"` or `"dn"`, so the held leg is the other one
- sized held bid is not `None` and `< sell_dump_below` (0.80 / **0.40**)
- `dump_time_gate_open(ttm, sell_dump_max_ttm_s)` (0 / **240**; example 240; #220). With a positive cutoff, the dump arms and fires only when seconds-to-close ≤ cutoff. Above it, `sell_dump_armed_at` is not started and an in-progress persist is cleared, so the full persist must elapse again inside the window. Unknown TTM counts as **closed** here. A blocked arm logs `sell_dump_time_gated` at most once per bag per 15s.
- the condition persists `sell_dump_persist_s` (2 / 2)
- at fire, `sell_fire_decision("dump")` still sees bid `< sell_dump_below`, else `sell_cancel_out_of_range`
- not in sell cooldown

Live, that reads: in the last four minutes, if the leg we still hold 200 of has a sized bid under 40¢ for 2s, sell all 200, and then the 100 kept shares of the other leg (`sell_dump_also_kept`).

Action (`_run_dump_fak_with_refire`):

1. `_sell_inventory` on the held token → size (200). `already_flat` sets `sold_dump` / `sold_winner` with note `already_flat` and no `sell_dump_leg`.
2. First shot: one **live-bid FAK** at the fire-time sized bid.
3. If that returns zero fill with `no orders found` or a kill/cancel status (`dump_fast_retry_eligible`), re-fetch the held book and fire `dump_retry_ladder_limits(fresh_bid, floor=sell_floor, step=0.04, max_rungs=4)` — for example 0.31, 0.27, 0.23, 0.19 — up to `sell_dump_fak_retries` (2) times in the same tick. Stop on an empty book (`sell_dump_fast_refire_stop reason=empty_book`), a non-retryable status, or exhausted retries. These refires are not re-checked against the TTM gate.
4. Full fill (or dry run): `sold_dump=True`, `sold_winner=True`, `sell_dump_leg = held`, `sold_dump_at`, `sell_dump_filled`, `sell_dump_limit = last limit`, and `sell_dump_fill_px` = share-weighted average of every priced dump fill (first shot and refire rungs); log `sell_dump_done` (with `hedge_leg` and `avg_px`), ntfy. `bag_risk.dump_px` reads the average, falling back to the limit.
5. `sell_dump_also_kept` (false / **true**; live since 10:31 IST on 3 Oct, #232; read from cfg each tick): in that same full-fill branch, `_sell_kept_after_dump` sells the kept scrap half once per bag. The size is `min(sell_scrap_keep, on-chain balance of sold_leg)`. Without a funder or CTF, it uses `sell_scrap_keep`. Flat or zero is a no-op (`outcome=nothing_kept`). The sale is the same `_run_dump_fak_with_refire` on the kept token with the ladder floored at `max(0.01, sell_clob_min_price)`, then one FAK at that 1¢ floor for any remainder. It records `sell_dump_kept_done`, `_planned`, `_filled`, `_fill_px`, `_limit`, `_attempts` and `_outcome` (`filled` / `partial` / `no_fill` / `dry_run` / `nothing_kept`), and `sell_dump_kept_sold` when fully sold. That last flag closes `kept_loser_open`. It logs `sell_dump_kept`. A partial remainder is not retried on later ticks, and enabling the flag after the dump does nothing.

**Why sell the kept half too (operator's rationale).** After a 0.5 scrap at 200 a side, the bag holds 200 of the held leg and 100 of the kept leg. The two legs' bids sum to about $1. Selling both at the dump therefore locks a net of about `−197 + 200·p + 100·(1 − p − spread)` for a held-leg fill `p`, whichever side finally wins. That is roughly **−$50 to −$70** at the 30–45¢ fills a 40¢ trigger produces.

- **Without the kept sale.** The same dump is −$135 on a false alarm and −$35 on a real flip ([§22](#section-22)).
- **The trade.** The flag gives up a little average EV, because the kept leg's flip payout is gone, in exchange for a bounded worst case on every dump event.

**Not adopted.** Tiered dump persistence (`sell_dump_tiers`, unmerged PR #231) and the post-dump kept stop (unmerged PR #228) are not in `main` and not live. The live dump is the single 0.40 / 2s / ≤240s rule above.

**Scope:** only the held leg after a loser fill, plus the kept part of a partial scrap when `sell_dump_also_kept` is on (live). Full sets with neither leg sold never arm the dump. After `end_ts`, `manage_sells` skips the intent. A sister miss does not dump the held leg. `sell_dump_leg` exists so wallet B could buy the other leg; B is disabled live. If B were ever enabled together with `sell_dump_also_kept`, B would be buying the leg A just sold.

<a id="section-20"></a>
## Live-bid FAK vs floor FAK

| Path | Limit choice | Why |
|---|---|---|
| Loser | One FAK at `sell_floor` (0.02 code / 0.01 live) for the scrap remainder. Flag off: 1¢ ladder from `sell_fak_px` to the floor, clipped to top-rung depth. Blind FAK at 0.01. Post-miss rest at `min(sell_scrap_rest_px, live or last-seen bid)` (rest off live) | A floor FAK still takes 3¢ then 2¢ bids first, in one round trip, without clipping to top-of-book depth |
| Winner (allowed) | `min(live sized bid, 0.99)` | Resting books quote 0.995–0.999; posting those limits is rejected (`max: 0.99`). A 0.99 FAK still fills the rich book |
| Held dump | Current sized bid, then a 4¢-step retry ladder toward the floor on a zero-fill miss | Same rejection class; dump fires precisely when bid is *weak* and moving |

Observed failure modes: (1) winner armed at bid 0.99 but FAK posted 0.999 → `invalid price … max: 0.99`. (2) bag `btc-updown-15m-1789810200`: cheap gate open, Down sized 0.995–0.999, live-bid FAK without clamp → 12× same rejection, winner never sold.

<a id="section-21"></a>
## Hypothetical lifecycle: 200-share bag, scrap 100 @ ~3¢, winner redeems

Live knobs throughout (3 Oct 2026). Fees ignored.

1. **Mint (T−30s to T+240s).** Sequential mode: the previous bag's winner is sold or its window has ended, and pUSD covers $200. Prechecks pass. Mint 200 Up + 200 Down: **−$200.00**. Relayer confirms, reconcile sees 200/200 → `confirmed`.
2. **Window open.** Up drifts down. At T−8m Up's sized bid is 0.03 and Down's 0.96. Up is a loser by price, but TTM 480 > 360: no arm, `sell_scrap_time_gated` every 15s.
3. **T−6m (TTM 360).** The gate opens. `classify_loser` → `up`. `sell_loser_armed_at = now`, `sell_loser_persist why=armed`.
4. **3s later**, still 0.03 / 0.96 → `ready`. `sell_fire_decision` → `fire`. `_sell_inventory` → 200 held, `has_inventory`.
5. **Plan lock.** `scrap_share_plan(200, 0.5)` → target 100, keep 100. `sell_scrap_plan` logged. `scrap_order_shares(target=100, keep=100, filled=0, inventory=200)` → 100.
6. **Sweep.** One FAK SELL 100 Up @ limit 0.01. Bids at 3¢ absorb it: `sell_scrap_sweep size=100 avg_px=0.03`: **+$3.00**.
   - The bag records `sell_filled=100`, `sell_limit=0.01` and `sell_fill_px=0.03`.
   - Done → `sold_loser`, `sold_leg="up"`, `sell_scrap_outcome target_filled`, `sell_loser_done`.
   - The bag still blocks the next mint (sequential gate) until its winner is sold or the window ends.
7. **Rest of the window.** Down (held 200) stays 0.95–0.99.
   - The danger alert needs < 0.70 for 5s, and the dump needs < 0.40 inside the last 240s: neither happens.
   - The winner path needs ≥ 0.9995, so it never fires.
   - The kept Up 100 is blocked from any sale.
8. **`end_ts`.** The sell window closes. `bag_risk` logs `scrap_avg_px 0.03`, `min_held_bid ≈ 0.95`, all `sec_below_*` 0, `dump_fired false`. The sequential gate releases. The next window opens now and mints as soon as pUSD covers $200. With one bag of capital that is normally just after this bag's redeem lands (`mint_seq_wait_cash` until then), inside the 240s cutoff.
9. **≈ end + 60–90s.** The redeem thread sees the condition resolved and submits `redeemPositions`: Down 200 → **+$200.00**, Up 100 → $0. The intent becomes `completed`, `redeemed: true`.

| Line | Cash |
|---|---:|
| Mint 200 sets | −200.00 |
| Scrap 100 Up @ 0.03 | +3.00 |
| Redeem 200 Down | +200.00 |
| Kept 100 Up | 0.00 |
| **Net** | **+3.00** |

With `sell_scrap_fraction=1.0` the same bag would scrap 200 × 0.03 = $6.00 and net +$6.00. The keep costs $3.00 on a normal win.

<a id="section-22"></a>
## Hypothetical lifecycle: dump at ~31¢, with the kept half sold too

Steps 1–6 as above: −$200.00 mint, +$3.00 scrap, holding 200 Down and 100 Up.

1. **T−3m20s (TTM 200, inside the 240s dump gate).** BTC spikes. Down's sized bid falls 0.60 → 0.38 → 0.33, and the danger alert has already fired under 0.70. At < 0.40 the dump arms (`sell_dump_persist why=armed`).
2. **2s later**, bid 0.33 → `ready`, `sell_fire_decision` → `fire`. `_sell_inventory` → 200.
3. **Held dump.** The first live-bid FAK at 0.33 returns `no orders found` (the 0.33 bid was pulled). Retry: fresh bid 0.31 → ladder `[0.31, 0.27, 0.23, 0.19]`. The first rung fills all 200 at 0.31: **+$62.00**. `sold_dump`, `sell_dump_leg="dn"`, `sell_dump_done`.
4. **Kept half (`sell_dump_also_kept`).** In the same event, `_sell_kept_after_dump` sells the kept 100 Up at its live bid, about 0.68 (Up + Down bids ≈ $1 less the spread): **+$68.00**. `sell_dump_kept outcome=filled`. The bag is flat; its redeem job ends `nothing_held`.

Net in each case, depending on what finally wins:

| Final outcome | No dump | Dump held leg only (flag off) | Dump + kept half (**live**) |
|---|---:|---:|---:|
| False alarm: Down (held) wins | +3.00 | −135.00 | **−67.00** |
| Real flip: Up (scrapped) wins | −97.00 | −35.00 | **−67.00** |

How the cells add up (mint −200, scrap +3 in every case):

- **No dump, false alarm:** redeem 200 Down, +200. **No dump, real flip:** redeem the kept 100 Up, +100.
- **Held leg only:** +62 from the dump, plus +100 from the kept Up only if Up wins.
- **Dump + kept half:** +62 + 68, with nothing left to redeem.

With the kept half sold, the outcome no longer matters: the dump event costs about $67 here. In general it locks `−197 + 200·p + 100·q` with `q ≈ 1 − p`, which is about −$50 to −$70 for fills of 30–45¢.

That is the operator's trade: a false alarm costs more than a perfect dump would, and the kept leg's flip payout is given up. In exchange no dump event can reach the −$135 tail. The 40¢ / 2s / last-240s trigger is unchanged. `bag_risk` records `dump_px`, `dump_ttm` and how long the held bid sat below 0.80 / 0.65 / 0.50, which is the data for re-tuning it.

<a id="section-23"></a>
## What this code does not prove

- That 2–3¢ loser bids with depth always exist when the opposite is ≥ 90¢ inside the last six minutes.
- That every redeem lands first time. The live redeem thread retries with backoff and logs `redeem_gave_up` (with an ntfy alert) after 6 attempts ([§37](#section-37)).
- That one bag at a time (sequential, live) beats two overlapping bags. It halves capital at risk, and it can skip a window when the redeem is late or the relayer `STATE_FAILED`s.
- That dumping at 40¢ (or 80¢) is optimal; it is an operator-chosen circuit breaker ([§22](#section-22)).
- That keeping 50% of the loser, or selling it with every dump (`sell_dump_also_kept`), is optimal. `bag_risk`, `sell_scrap_outcome` and `sell_dump_kept` exist to measure it.

<a id="part-iv"></a>
# Part IV — The buy/ helpers and pathlog

<a id="section-24"></a>
## Ownership map

| Module | Owner of |
|---|---|
| `mintbot.py` | Process, knobs merge, relayer, CLOB sells, state file |
| `buy/mint_loops.py` | Concurrent sell/mint job runner, same-slug claim, candidate selection, pending-cash reserve, ended-bag chain policy, persist digest |
| `buy/mint_sell.py` | Pure sell policy, scrap plan, TTM gates, `bag_risk` counters |
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
- `loser_scrap_post` — sweep plan (one limit at the floor, full remainder) or ladder plan (cent rungs, depth-clipped).
- `loser_ladder_limits` — every 1¢ from min(fak or threshold, bid) down to floor; the live bid alone when it is below the floor. Also used for the `depth_at_limit` preview and the `phase=ready` depth log, even in sweep mode.
- `loser_partial_fak_shares` — ladder-mode clip to displayed depth.
- `normalize_scrap_fraction` / `scrap_share_plan` / `scrap_order_shares` / `scrap_target_met` — partial scrap ([§17b](#section-17b)).
- `kept_leg_below_winner_min` — blocks the winner path on the kept leg below base `sell_winner_min`.
- `loser_blind_fak_due` — blind 1¢ FAK eligibility and backoff.
- `scrap_rest_action` / `scrap_rest_px` / `resting_tif` / `rest_order_matched_shares` / `posted_order_id` — post-miss rest lifecycle.
- `late_oracle_scrap_ok` / `advance_oracle_edge_arm` / `side_aware_oracle_edge_usd` — late-window loser-scrap veto. Off unless `sell_late_window_s` > 0.
- `scrap_oracle_settings` / `scrap_oracle_veto` — any-time scrap oracle veto (#234). Pure: `(block, why, detail)` from TWAP, optional live price, strike, receive ages and threshold.
- `winner_cashout_leg` — unique leg whose sized bid ≥ winner_min.
- `winner_cheap_decision` — cheap min only if sold_loser, loser ≤ gate, and loser + cheap > $1.
- `winner_sell_limit` — clamp live-bid FAK into CLOB [0.01, 0.99].
- `dump_fast_retry_eligible` / `dump_retry_ladder_limits` — held-dump refire.
- `fresh_bag_risk` / `bag_risk_observe` / `bag_risk_flush` / `bag_risk_payload` — log-only `bag_risk`.
- `cycle_sleep_s` / `mint_cycle_sleep_s` / `sell_intent_hot` — loop cadence.

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

`deploy/polylockbot.service` runs `lockbot.py` with the same `.env`. It is installed and enabled on the VM and stays in dry-run until `lockbot.json` sets `dry_run` false and the process is reloaded or restarted. See [§39](#section-39).

`deploy/polyscrapbid.service` is opt-in and stays disabled. It runs `scrapbidder.py` with `EnvironmentFile=.env.complement` only (not mintbot `.env`). Since #212, even with `bid_enabled` on, wallet B's 20-share post-scrap buy needs `scrap_hedge_enabled` (default **false**); the 10-share dump hedge after A sets `sell_dump_leg` needs `dump_hedge_enabled` (default true). Both use the FAK/rest notional band ($1.00–$1.50 by default). Markets A never held are not bid (`bid_absent_enabled` defaults false). There is no sister-miss dump. The A→B top-up (`sister_topup.py`, $5 of pUSD once per broke episode, reads `.env` itself) needs `topup_enabled` (default **false**). Scrapbidder re-reads mint intents after quoting books so a scrap during the pass is not planned from a stale snapshot (#203). It does not mint and does not FAK-sell. `bid_enabled` defaults false and `dry_run` defaults true. Do not commit `.env.complement`. Do not add `.env` to this unit. Same-wallet buyback is not implemented. Do not enable this unit unless the operator asks.

Never enable retired buy units (`polycomplement`, buybots, DangerZone, shadow) from memory of old docs. `polyscrapbid` is not a restore of `complementbot.py`.

<a id="section-29"></a>
## Knobs: code default vs live (3 Oct 2026)

"Code" is `DEFAULTS` in `mintbot.py` on `main`. "Example" is `strategy_mint.example.json` where it differs. "Live" is the VM's gitignored `strategy_mint.json`. Rows marked † were confirmed by the operator on 3 Oct 2026 13:25 IST; the rest are from the 30 Sep read. "—" means the key is absent and the code default applies.

| Knob | Code | Example | Live | Meaning |
|---|---:|---:|---:|---|
| `entry_enabled` / `dry_run` | false / true | false / true | **true / false** | Mint for real |
| `shares` | 50 | 50 | **200** † | Complete-set size ($ per mint, 200 Up + 200 Down) |
| `enter_min_ttm_min` / `enter_max_ttm_min` | 0 / 45 | | 0 / 45 | Mint only windows opening within 45m |
| `max_open_sets` | 1 | 1 | 2 (ignored: sequential on) | Capacity (plus adjacent rule) when sequential is off |
| `count_kept_loser_as_open` | false | false | — (false) | Kept loser shares hold the slot until resolution |
| `mint_sequential` / `mint_seq_lead_s` / `mint_seq_cutoff_s` | false / 30 / 240 | false / 30 / 240 | **true** / 30 / 240 † | One bag of capital: mint in [start − lead, start + cutoff] once the previous bag is cashed or ended; wait on cash, skip past cutoff |
| `redeem_enabled` | false | false | **true** † | Auto-redeem resolved positions on the `mintbot-redeem` thread |
| `redeem_poll_s` / `redeem_min_after_end_s` / `redeem_retry_s` / `redeem_max_attempts` / `redeem_tx_timeout_s` | 15 / 60 / 60 / 6 / 300 | same | — | Redeem cadence, start delay after `end_ts`, backoff base (doubling, cap 900s), give-up count, relayer timeout |
| `redeem_startup_sweep` / `redeem_min_payout_usd` | true / 0.01 | same | **false** † / — | One Data API redeemable sweep per start; skip worthless conditions |
| `notify_danger_whatsapp` / `notify_danger_px` / `notify_danger_hold_s` | true / 0.70 / 5 | true / 0.70 / 5 | on / 0.70 / 5 † | WhatsApp danger alert: held winner bid under the line for the hold, after the scrap (needs `CALLMEBOT_*` env) |
| `notify_scrap_whatsapp` / `notify_dump_whatsapp` | false / false | false / false | — | WhatsApp alert on scrap / dump fill |
| `mint_fail_cooldown_s` / `mint_max_attempts` | 30 / 3 | | 30 / 3 | Remint policy |
| `mint_submitting_timeout_s` | 90 | | 90 | Auto-fail tx-less `submitting` |
| `mint_gas_margin` / `_fallback` / `_cap` | 0.15 / 650000 / 650000 | same | — | Relay gas plan |
| `poll_s` | 5 (floor 1) | 5 | **1** | Mint sleep; sell sleep when idle |
| `sell_armed_poll_s` | 2 | 2 | **1** | Sell sleep while hot |
| `sell_enabled` | false | false | **true** | Run `manage_sells` |
| `sell_threshold` | 0.02 | 0.02 | **0.03** † | Loser arm ceiling |
| `sell_fak_px` | 0.02 | 0.02 | **0.03** | Top ladder rung (ladder mode only) |
| `sell_floor` | 0.02 | 0.02 | **0.01** † | Sweep limit; ladder bottom; dump ladder floor |
| `sell_scrap_sweep_enabled` | true | true | — (true) | One floor FAK vs cent ladder |
| `sell_opposite_min` | 0.90 | | 0.90 | Opposite must be rich |
| `sell_persist_s` / `_last_min_s` / `_last_min_window_s` | 5 / 2 / 60 | | **3** / 2 / **90** † | Loser (and winner) persist; live `sell_persist_s` was 5 until 3 Oct |
| `sell_persist_skip_when_sized` | false | | false | Sized skip off |
| `sell_scrap_max_ttm_s` | 0 (off) | 600 | **360** † | Scrap only in the last N seconds |
| `sell_scrap_fraction` | 1.0 | 1.0 | **0.5** † | Scrap `floor(held × f)`, keep the rest (100 / 100 live) |
| `sell_scrap_blind_enabled` / `_px` / `_backoff_s` | true / 0.01 / 3 | | true / — / — | Blind FAK on empty keep |
| `sell_scrap_rest_enabled` | true | true | **false** | Post-miss resting sell |
| `sell_scrap_rest_px` / `_min_ahead_s` | 0.02 / 180 | | 0.01 / 180 | Rest ceiling; GTD vs GTC |
| `sell_cooldown_s` | 3 | | 3 | Between FAK attempts |
| `sell_winner_min` | 0.999 | | **0.9995** † | Winner cash-out floor (unreachable on a 0.001 tick) |
| `sell_winner_cheap_if_loser_le` / `sell_winner_min_cheap` | 0.03 / 0.99 | | **−1.0 / 0.999** | Cheap winner gate (closed live) |
| `sell_clob_max_price` / `_min_price` | 0.99 / 0.01 | | — | Winner limit clamp |
| `sell_dump_enabled` | true | | true | Held dump on |
| `sell_dump_below` | 0.80 | 0.80 | **0.40** † | Dump arm threshold |
| `sell_dump_persist_s` | 2 | | 2 † | Dump persist |
| `sell_dump_max_ttm_s` | 0 (off) | 240 | **240** † | Dump only in the last N seconds |
| `sell_dump_also_kept` | false | false | **true** † (since 10:31 IST, #232) | Dump also sells the kept scrap half (1¢ floor), so the bag exits flat |
| `sell_dump_tiers` | — | — | — | Not in `main`. Exists only in unmerged PR #231; not adopted |
| `sell_dump_fak_retries` / `_ladder_step` / `_ladder_rungs` | 2 / 0.04 / 4 | | 2 / 0.04 / 4 | Refire after a zero-fill miss |
| `sell_min_bid_size` | 1.0 | | 1.0 | Sized-bid minimum |
| `sell_late_window_s` | 0 | 0 | 0 † | Oracle veto off |
| `sell_oracle_edge_floor_usd` / `_per_ttm` / `_stale_s` | 0 / 0 / 0 | | 0 / 0 / 0 | Old veto knobs zeroed |
| `sell_oracle_edge_persist_s` | 3 | 3 | **0** | Only used when the veto is on |
| `scrap_oracle_veto_enabled` / `_usd` / `_stale_s` / `_use_live` | true / 5.0 / 3.0 / true | true / 5.0 / 3.0 / true | — (code default after pull + restart) | Scrap oracle veto (#234): no scrap while the TWAP or the live Chainlink price is within $5 of the strike or on the scrapped leg's side |
| `oracle_log_enabled` | true | true | true | Audit tape |
| `sell_persist_skip_ttm_s` | — | — | 0 | Leftover; ignored (not in `DEFAULTS`) |

Live JSON still overrides every key it sets; keys it omits take the code default after a pull and restart. Do not edit live JSON from this repo. The sister process is off; its knobs are in `strategy_scrapbid.example.json`.

<a id="section-30"></a>
## Deploy boundary (VM is source of truth)

Operational rule: **VM files win**. GitHub is backup/history. Live `strategy_mint.json` and `positions_mint.json` stay gitignored. Example knobs are not authorization to trade.

The `Deploy to GCP` workflow runs on pushes to `main` that touch `mintbot.py`, `pathlog.py`, `check_path_backtest.py`, `buy/**` or `requirements.txt`. It does `git pull` + `pip install` on the VM and never restarts a service. Docs-only changes (including this file) do not trigger it and are not synced anywhere.

After code pull: `polymintbot` is stopped (inactive, still enabled). Do not start it unless the operator asks. Restart `polylockbot` only when the operator asks; a pull does not restart it. The `poll_s >= 1` floor is now on `main`, so the VM's old local `mintbot.py` patch must be dropped (`git checkout -- mintbot.py`) before the pull, or `git pull` refuses to merge over it ([§32](#section-32) item 10). `polypathlog` is retired. Leave `polyscrapbid` stopped until the operator asks to start it.

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
10. **Stale local `poll_s` patch on the VM.** Live `strategy_mint.json` sets `poll_s: 1`. `main` validates `poll_s >= 1`, so an uncommitted `mintbot.py` edit for it is redundant. Since the VM now runs `main` through #232, it should be gone. If one ever reappears it blocks the deploy workflow's `git pull`, so drop it (`git checkout -- mintbot.py`). If live `poll_s` ever drops below 1, a restart fails `load_strategy` and systemd restart-loops.
11. **Zero seconds knobs** — resolved. `sell_dump_persist_s`, `sell_cooldown_s` and `sell_scrap_rest_min_ahead_s` go through `cfg_seconds`, so an explicit 0 is respected; `validate_strategy` rejects negatives. Live sets 2 / 3 / 180, so nothing changes today ([§6](#section-6) aside).
12. **`sell_limit` is the floor under sweep** — still true, and kept for compatibility. The loser price that matters is `sell_fill_px` (share-weighted average fill). The cheap-winner gate and `bag_risk` read it first, and fall back to `sell_limit` only for intents written before the field existed. The dump has `sell_dump_fill_px` alongside `sell_dump_limit`.
13. **Oracle tape size** — resolved. `logs/oracle_twap.jsonl` rolls into `logs/archive/` at 20 MB and is gzipped in the background ([§10c](#section-10c)). The 116 MB live file is archived on the first append after the next restart; a `tail -f` must follow the rename (`tail -F`).
14. **Pathlog retired but enabled** — `polypathlog` has been inactive since 22 Sep and is not coming back, but `systemctl is-enabled` still said `enabled` on 30 Sep. Disable it so a reboot does not revive it ([§27](#section-27)).
15. **Startup banner** — resolved. `main()` prints `sell_plan_banner(cfg)` from the loaded strategy (sweep vs ladder, scrap fraction, TTM gates, dump threshold, winner floor), and the `startup` event carries the same `sell_plan` string.
16. **Silent RTDS socket** — resolved in code. A connected-but-silent feed is closed by the 45s watchdog (5s in a bag's last 360s since #234; three 45–57s silences fell inside that span on 30 Sep–3 Oct) and protocol ping/pong, and the stall logs once plus a reminder a minute instead of every second ([§10c](#section-10c)). The close now comes from the end-boundary RTDS sample, not crypto-price, so crypto-price 429s no longer drop `oracle_window_end` rows (8 of 226 on 29–30 Sep).
17. **A dump now exits both legs.** With `sell_dump_also_kept` true (live), a dumped bag is flat, and its redeem job ends `nothing_held`. Reading `sell_dump_done` alone undercounts the dump event; add `sell_dump_kept` (`sold`, `avg_px`, `outcome`). A `partial` or `no_fill` outcome leaves kept shares to resolution and is not retried.
18. **Unmerged dump PRs.** `sell_dump_tiers` (PR #231) and the post-dump kept stop (PR #228) are not on `main`. Setting `sell_dump_tiers` in the live file does nothing, because `load_strategy` drops keys missing from `DEFAULTS`.

<a id="section-33"></a>
## Glossary

| Term | Meaning here |
|---|---|
| Complete set | 1 Up + 1 Down for one condition |
| Mint / split | Collateral → both outcome tokens |
| Loser scrap | Sell the cheap leg (all, or a locked fraction) after the opposite is rich |
| Partial scrap / keep | `sell_scrap_fraction < 1`: scrap `floor(held × f)`, hold the rest to resolution |
| Floor sweep | One loser FAK at `sell_floor` for the scrap remainder |
| Winner cash-out | Sell rich leg near $1 (gated; unreachable live) |
| Held dump | After loser sold, sell the held leg if weak (< 80¢ code, < 40¢ live) |
| Redeem | Exchange winning tokens for $1 collateral after resolution (`mintbot-redeem` thread, on live) |
| FAK | Fill-and-kill marketable limit |
| Sized bid | Best bid with minimum size |
| Adjacent lookahead | Mint next 15m while still holding current full bag (not used live: sequential mode) |
| Sequential bags | One bag of capital: mint in [start − 30s, start + 240s] once the previous bag is cashed or ended (live) |
| Relayer PROXY | Polymarket-submitted batched tx from proxy wallet |
| Pending reserve | pUSD already committed to in-flight mints, subtracted before a new mint |
| `bag_risk` | One log line per bag at window close; audit only |

<a id="section-34"></a>
## Source snapshot

- Host: Google VM `poly-vm` (`/home/ntemusejoel/poly-money-maker`)
- Services (30 Sep): `polymintbot` active; `polypathlog` retired (inactive, still enabled; should be disabled); `polyscrapbid` disabled
- Code: VM on `main` through #232 (`sell_dump_also_kept`, live 10:31 IST on 3 Oct), including #229 (strike from the open TWAP sample + Gamma check) and #230 (sequential bags + auto-redeem). Unmerged and not live: #228 (post-dump kept stop), #231 (`sell_dump_tiers`)
- Primary sources: `mintbot.py` (~3843 lines), `buy/mint_sell.py` (~1444), `buy/mint_loops.py` (~506), `pathlog.py` (~527)
- Strategy: gitignored `strategy_mint.json` as tabulated in §29 (operator-confirmed keys as of 3 Oct 2026 13:25 IST)
- Document date: 3 October 2026 (rev: 200-share sequential bag, auto-redeem on, persist 3s / 90s window, dump also sells the kept half; see [Changelog](#changelog))
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
               # live (mint_sequential): seq_eligible_markets → [start-30s, start+240s],
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
| Loser sized bid ≤ `sell_threshold` (0.02 / 0.03) AND opposite ≥ 0.90 AND not both cheap AND TTM ≤ `sell_scrap_max_ttm_s` (off / 360) | 5s / 3s | Lock plan if fraction < 1; one FAK at `sell_floor` (0.02 / 0.01) for `target − filled`; cancel if out of range at fire | `sold_loser`, `sold_leg`, `sell_filled`, `sell_scrap_*` |
| Same, TTM ≤ `sell_persist_last_min_window_s` (60 / 90) | 2s | Same | Same |
| Loser armed, book or FAK empty | kept | Blind FAK at 0.01 every ≥ 3s; rest after a miss (off live) | Same on fill |
| Winner sized bid ≥ effective_winner_min (0.999 / 0.9995; cheap 0.99 gate closed live) | 5s / 3s | Live-bid FAK clamped to 0.99; kept leg capped at keep; cancel if bid dropped | `sold_winner` |
| `sold_loser` AND held sized bid < `sell_dump_below` (0.80 / 0.40) AND TTM ≤ `sell_dump_max_ttm_s` (off / 240) | 2s | Live-bid FAK; on zero-fill miss, re-check + 4¢-step ladder retries; cancel if bid ≥ below. With `sell_dump_also_kept` (off / **on**): then sell the kept half the same way, plus one 1¢ FAK for the remainder | `sold_dump`, `sold_winner`, `sell_dump_leg`; `sell_dump_kept_*` |
| `now ≥ end_ts` | — | No CLOB sells; cancel rest; `sell_scrap_outcome window_end` if unfinished; one `bag_risk` | (redeem outside this loop) |
| Within `sell_cooldown_s` of last attempt | — | Skip fire | — |

`effective_winner_min` formula:

```
effective = sell_winner_min                           # 0.999 code, 0.9995 live
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

Assume the live `shares=200`, `sell_scrap_fraction=0.5`, scrap average 3¢, fees ignored.

| Path | Cash out | Comment |
|---|---:|---|
| Mint | −200.00 | Split collateral |
| Scrap 100 loser @ 0.03 | +3.00 | Floor sweep, avg fill |
| Redeem winner 200 | +200.00 | `mintbot-redeem`, after resolution |
| Kept 100 loser | 0.00 | Expires worthless |
| **Net** | **+3.00** | Thin; fees can erase it |

| Path | Cash out | Comment |
|---|---:|---|
| Mint | −200.00 | |
| Scrap 100 @ 0.03 | +3.00 | |
| Dump 200 held @ 0.31 | +62.00 | |
| Sell kept 100 @ ~0.68 | +68.00 | `sell_dump_also_kept` (live) |
| **Net** | **−67.00** | Same whichever leg wins; nothing left to redeem |

| Path (flag off, for comparison) | Net |
|---|---:|
| False dump, held leg then wins | −135.00 |
| True flip, kept 100 redeem $100 | −35.00 |

<a id="appendix-f"></a>
# Appendix F — Operator checklist

1. `systemctl is-active polymintbot` → active. `systemctl is-enabled polypathlog` → should be `disabled` (retired).
2. `jq . strategy_mint.json` → confirm `shares` (200), `mint_sequential`, `redeem_*`, sell_*, dump_* (including `sell_dump_also_kept`), `sell_scrap_fraction` (never commit this file).
3. `git status --short mintbot.py` → empty (the old local `poll_s` patch is upstream now; drop it before a pull).
4. Tail `mintbot.log` for `mint_confirmed`, `mint_submitted` (gas fields), `mint_skip_pending_reserve`, `sell_scrap_plan`, `sell_scrap_sweep`, `sell_scrap_outcome`, `sell_dump_done`, `sell_dump_kept`, `bag_risk`, `mint_failed`, `mint_seq_wait_cash` / `mint_seq_skip`, `redeem_confirmed` / `redeem_gave_up`.
5. After a `mint_failed`, expect a remint after 30s up to 3 attempts, then that slug is skipped.
6. Code change on VM → restart `polylockbot` only when the operator asks. `polymintbot` stays stopped unless the operator asks to start it. No local patch to re-apply.
7. GitHub sync is backup; VM remains SoT.




<a id="part-vi"></a>
# Part VI — Sequences and redeem

ASCII diagrams below are the PDF-safe form of sequence charts. They match `mintbot.py` on `main`. The mint diagram shows the default (non-sequential) selection; live runs `mint_sequential`, noted under the diagram.

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
   **Live (sequential):** eligibility is `[start − 30s, start + 240s]`, so an open window can be minted up to 240s in. The busy-bag gate (previous winner unsold and window not ended) replaces `mint_slots_full`, and short pUSD waits instead of skipping until the cutoff.
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
   |   (0.9995 live; cheap  |                        |                        |
   |    gate closed live)   |                        |                        |
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
   |   dump_below for 2s:   |                        |                        |
   |---- live-bid FAK ----->|                        |                        |
   |   (+ refire ladder)    |                        |                        | sold_dump
   |                        |                        |                        | + sold_winner
   |                        |                        |                        | sell_dump_leg
   | also_kept (live): sell |                        |                        |
   |---- kept half FAK ---->|                        |                        | sell_dump_kept_*
   |   (+ refire, 1c sweep) |                        |                        |
   |                        |                        |                        |
   | [C] loser path         |                        |                        |
   | ttm <= scrap cutoff,   |                        |                        |
   | loser<=threshold and   |                        |                        |
   | opposite>=0.90 for 3s  |                        |                        |
   |   live (2s, last 90s)  |                        |                        |
   | lock target/keep once  |                        |                        |
   |---- FAK @ floor ------>|                        |                        |
   |   size target-filled   |                        |                        | sold_loser
   |                        |                        |                        | sold_leg=up|dn
   |                        |                        |                        | sell_filled
```

Ordering in code is **A → B → C** inside one intent iteration. That matters: a bag that already sold the loser can cash out or dump the held leg before another loser attempt (loser already flagged off).

Cooldown: after any sell attempt, `sell_cooldown_s` (3s) suppresses the next fire.

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
| Keep winner (and kept loser) inventory unsold | **Yes** | no forced sell at expiry; live winner min is unreachable |
| Free mint slot while winner sits for redeem | **Yes** | `sold_loser` excluded from open-slot count; also expiry+120s |
| Redeem resolved positions automatically | **Opt-in** (`redeem_enabled`, default false; **on live**, startup sweep off) | `buy/mint_redeem.py` `RedeemDesk`, thread `mintbot-redeem` |
| Relayer batch for redeem | **Opt-in** | `submit_redeem` → `submit_mint_batch` (same PROXY signer, gas plan, relayer auth) |
| Mark intent `redeemed` after payout | **Opt-in** | `redeemed: true`, `redeem_status`, status `completed` |
| Merge (Up + Down → pUSD before resolution) | **No** | not needed; complete sets are sold or redeemed |

Observation (30 Sep 2026, before `redeem_enabled` was turned on): the winning leg's pUSD was back in wallet A about **68s after `end_ts`**. Live now has `redeem_enabled` true, so mintbot submits the redeem itself. Kept loser shares from a partial scrap are worthless on a normal win and pay $1 each on a flip.

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

With `redeem_enabled` false (the code default), redeem stays **outside this repo**, and mintbot is **mint + intra-window sell policy**. Live has it **on** with `redeem_startup_sweep` false, so only bags this process minted get redeem jobs. A bag that dumped with `sell_dump_also_kept` is already flat and ends `nothing_held`.

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

<a id="part-vii"></a>
# Part VII — Lockbot

<a id="section-39"></a>
## TWAP-lock taker

`polymintbot` is stopped (inactive, still enabled at boot, code still in the repo). `lockbot.py` is the process under test. It does not import mintbot, does not take `.mintbot.lock`, and does not read `strategy_mint.json`. It buys and holds. It never sells.

### Architecture

Two loops share one process and one `RLock` for state. Neither loop does the other's HTTP.

| Thread | Name | Work |
|---|---|---|
| Decision | main | Blocks on a `threading.Event`. A Binance trade sets it. Otherwise it wakes for the next strategy-1 second, the next paper fill, or `poll_s` (1s). It does not spin on `fast_poll_s`. |
| Slow | `lockbot-slow` | Gamma discovery, strike latch, book subscribe, client warm, pUSD cash, settlement, wallet compare, `feed_status`. Cadence `poll_s`. |
| Book read | `lockbot-book` | CLOB market websocket. `recv` only queues the raw frame. |
| Book apply | `lockbot-book-apply` | Parses with `orjson` when that module imports, otherwise the stdlib `json`. Applies deltas to price→size maps. Sorts only when a ladder is read. |
| Book ping | `lockbot-book-ping` | Ping every 10s. If `sock` is `None`, it does nothing. |
| Binance | `lockbot-binance` | `btcusdt@trade`. The callback sets the decision event and returns. |
| Orders | `lockbot-orders` | Live FAK posts. The decision thread enqueues and does not wait. |
| Redeem | `lockbot-redeem` | Started when the process is live. Idle while `dry_run` is true. |
| Wallets | `lockbot-wallets` | RTDS activity tape. Log only. |

`fast_poll_s` (0.05) remains in the config so an old file still validates. The decision wait does not use it.

Strategy 2 reads the latest trade when the event fires, not once per queued print. A burst coalesces into one evaluation. Sigma for a window is computed once it is a full pre-window sample, then cached. Until then it refreshes at most every 5s. The 3-second price is the last trade at or before that time, scanned from the end of the history.

Live orders are handed to `LivePoster`. `post_fak_buy` stamps `post_ts` before `create_market_order`. `warm_market` has already cached tick size, neg-risk, and fee on the slow thread. The summary reports `recv_to_decision_ms` and `recv_to_handoff_ms` from the signal, and `recv_to_post_ms` from `entry` (live) or `paper_fill` (the delayed book walk). Those are not added together.

### Books

The socket is `wss://ws-subscriptions-clob.polymarket.com/ws/market`. The first frame is `{"type":"market","assets_ids":[...]}`. Later adds are `{"operation":"subscribe","assets_ids":[...]}`. Ids that leave the set are `{"operation":"unsubscribe","assets_ids":[...]}`.

The set is the token ids of the **current and next BTC 5m and BTC 15m windows** only (up to eight tokens). ETH, SOL, and XRP are not subscribed. When a window rolls, the expired ids are unsubscribed.

A `book` event replaces that token's map. A `price_change` writes one level; size 0 deletes it. `BUY` is the bid and `SELL` is the ask. `book()` returns asks ascending and bids descending.

Reconnects use backoff from 0.5s to 15s, then resubscribe the full wanted set. A silent socket (no frame for 15s) drops itself on the reader thread. No other thread calls `close`. Every send and close checks that the websocket and its `sock` are not `None`. `feed_status` includes `book_age_s`, `book_reconnects`, `book_tokens`, and `book_parser`.

### Strategy 1 (NIULAI4)

Every second from tau 58 to tau 1 on BTC 15m and BTC 5m. The side is the sign of projected TWAP minus strike (the elapsed-TWAP expectation below; a tie stays Up). Inside the minute, `sd` is `sqrt(sigma^2 * max(tau, 0.5)^3 / 10800 + (0.00002 K)^2)` and `q = Phi(z_side)`. A FAK buys when `z_side >= Z`, `ask <= Pmax`, and `q - ask - fee >= edge_min`. Defaults: 15m Z=0, Pmax=0.97, edge_min=0; 5m Z=0.25, Pmax=0.90, edge_min=0. `ask_min` is 0.02 and `max_pay` is 0.97. Clips are `clip_usd` (5) until `strategy1_market_usd` (20), including partial fills. The first fill with shares locks the side; the other side is never bought in that market. An ask above `max_pay` is skipped. The order is not posted at a lower limit.

### Strategy 2 (R2e)

BTC 5m only, tau 5 to 300, on each Binance trade (or one evaluation for a burst). `move` is the Binance BTCUSDT change over `s2_move_s` (3) divided by `sigma1s`, the population std of 1-second returns in the 300 seconds before the open (else a rolling 300-second window). `|move| >= s2_move_sigma` (2) buys that side, the direction of the move, when the ask is in `[ask_min, s2_ask_max]` (0.02–0.98) and still at or under `max_pay`. `s2_q_edge_min` null disables the optional `q - ask` filter (no fee in that filter). Clips are $5 up to `strategy2_market_usd` (20), with `s2_clip_cooldown_s` (1). It does not lock a side. It holds to settlement and never sells.

### Settlement model

Polymarket crypto up/down windows resolve on the Chainlink 60-second TWAP at the window end, against the strike (the same TWAP at the open, Gamma `priceToBeat`). In the last 60 seconds part of that average is already fixed. `buy/lock_fair.py` follows `q1e_lock_chainlink`: the known seconds stay, the remaining `tau - 1` seconds are filled with the live Chainlink print, and

`E[F] = (known_sum + S * (tau - 1)) / 60`, `Var = sigma^2 * tau^3 / 10800`.

`sigma` is the max of the 5-minute and 15-minute standard deviation of 1-second live returns, floored at `1e-9`. Up's win probability is the normal CDF of `(E[F] - K) / sqrt(Var + (0.00002 K)^2)`. The path is already Chainlink, so the Binance basis in the research script is 0. Before the last minute the expectation is the live price and the variance scale is `tau - 40`, matching q1e, but strategy 1 entries are the last 58 seconds. BTC 5m uses the same `btc/usd` Chainlink stream. The strike is latched at that window's open, within 1.25s. A missing strike, or an RTDS open that disagrees with Gamma `priceToBeat`, blocks strategy 1. Up wins ties (`final TWAP >= strike`).

### Risk

| Knob | Example default | What it does |
|---|---|---|
| `enabled` | true | False stops new entries. Redeem and logs continue. Hot-reloaded. |
| `dry_run` | true | True does not post. See going live below. |
| `clip_usd` | 5 | One order's dollars, before the caps. |
| `strategy1_market_usd` | 20 | Strategy 1 spend in one market. |
| `strategy2_market_usd` | 20 | Strategy 2 spend in one market. |
| `combined_per_market_usd` | 40 | Both strategies together, unless a market rule overrides it. |
| `market_rules.<key>.combined_usd` | unset | Optional. `btc_5m` and `btc_15m` each replace the global combined cap. |
| `max_open_exposure_usd` | 60 | Open cost plus reserved paper notional. |
| `daily_loss_stop_usd` | 60 | Dublin-day realized plus mark-to-bid. Latches until the next Europe/Dublin day. |
| `min_cash_buffer_usd` | 5 | Cash left after the order. |
| `dry_run_cash_usd` | 500 | Paper cash. Live mode does not use it. |
| `stale_price_s`, `stale_book_s`, `stale_binance_s` | 2 | Local receive time. |
| `dry_run_latency_s` | 0.20 | Paper fill walks the book this long after the decision. |
| `h2h_window_s` | 10 | Wallet pairing window. |

The intended live overlay, not written into the example and not applied on the VM, is `clip_usd` 5, `strategy1_market_usd` 5, `strategy2_market_usd` 5, `market_rules.btc_5m.combined_usd` 10, `market_rules.btc_15m.combined_usd` 5, `daily_loss_stop_usd` 15, then `dry_run` false.

Live cash is `ChainReader.pUSD_balance` of `FUNDER_ADDRESS`. That is the proxy's pUSD collateral (about $233 at the 2026-10-05 deploy; native USDC and USDC.e were 0). It is logged as `cash_balance` when the client comes up and then about every 15s. Unknown cash skips buys (`cash_unknown`). Dry-run cash stays `dry_run_cash_usd` minus exposure.

A zero paper fill (`book_moved`) does not lock a side and does not spend the live ledger. Paper notional is reserved until the delayed walk.

### Ledgers

| Mode | File | Counters |
|---|---|---|
| `dry_run` true | `positions_lockbot.json` | Paper loss, exposure, spend. |
| `dry_run` false | `positions_lockbot_live.json` | Live loss, exposure, spend. The paper file is not read. |

Positions are keyed `slug|strategy|side`. `python lockbot.py --reset-live` replaces the live file with an empty book and does not open the paper file. It takes `.lockbot.lock`, so it exits if lockbot is already running. Stop the service, reset, then start.

### Wallet tape

A second RTDS socket on `wss://ws-live-data.polymarket.com` subscribes to `activity/trades` and records fills by NIULAI4 (`0x44832d0d2ec11187c1e77d786feb15f6a50254c6`), asdaefef (`0x75cc3b63a2f2423085e10706c78b494017b93ce1`), and dvasdkasodk (`0x5d4aba8ad45bb5eab3499a0294b42da5d1e455d3`) in BTC 5m and 15m. Each fill pairs with our nearest same-outcome signal inside `h2h_window_s` (10s) and stays unpaired otherwise. The paired row stores our signal, post, and ack times, their price, size, and timestamp, the signed gap (`us_minus_them_s` = our decision time minus their payload timestamp; negative means we were first), and our price minus their price. On BTC 5m it also stores the Binance 3-second move at their fill. `trigger_fired` is true only when that move clears `s2_move_sigma` and an s2 signal on that side exists inside the window. `lockbot_summary.py` prints the share of their fills we also signalled, the median and p90 of the signed gap, the price difference, and the unpaired split: `missed` when the move cleared the bar, `no_move` when it did not. The tape is log-only. The gates do not read it.

### Markets and feeds

Confirmed on Gamma 2026-10-04. Event slug `{asset}-updown-{5m|15m}-{start_ts}`, series `{asset}-up-or-down-{5m|15m}`. BTC 15m and BTC 5m are on. ETH/SOL/XRP 15m and 5m stay off. A `resolutionSource` without `twap-60s` is skipped even if the flag is on. One `RtdsTwapFeed` per enabled symbol (`btc/usd` for the defaults) subscribes to `crypto_prices_twap_sixty` and `crypto_prices_chainlink`. The Binance feed tries `stream.binance.com` then `data-stream.binance.vision`, stream `btcusdt@trade`.

### Deploy and operations

Unit file: `deploy/polylockbot.service`. User `ntemusejoel`, working directory `/home/ntemusejoel/poly-money-maker`, `EnvironmentFile` the gitignored `.env`, `ExecStart` `.venv/bin/python lockbot.py`, `Restart=always`, `RestartSec=5`. The VM already has this unit installed and enabled. This repo does not restart it.

Config: `lockbot.json` if it exists, otherwise `lockbot.example.json`. The example stays `dry_run: true`. The VM file is gitignored. Hot reload watches mtime.

Kill switch: `enabled: false` stops new entries and is hot-reloaded. Creating `STOP_LOCKBOT` in the repo directory makes the loops exit. Deleting that file does not start the process; systemd restarts it because `Restart=always`, so remove the file before expecting a clean start, or stop the unit.

Going live:

1. Stop is optional if the hot reload succeeds. Prefer a restart so the operator can see `live_client_ready` and `cash_balance` in the startup log.
2. In `lockbot.json` set the $5 / $15 overlay above and `dry_run` to false. Do not commit that file.
3. On a running process, saving the file is enough: reload builds the CLOB client with `open_live_client()` from the environment systemd already injected, logs `live_client_ready`, logs pUSD, switches to `positions_lockbot_live.json`, and starts `lockbot-redeem`.
4. If the build fails (`PRIVATE_KEY` or `FUNDER_ADDRESS` missing, or an exception), the log is `live_switch_fail` with `action` `restart_required`. No order is posted. The env is loaded only at process start, so a failed switch needs a restart after the environment is fixed. `order_skip` repeats that reason at most every 30s.
5. Reset a live book that should not carry over: stop lockbot, `python lockbot.py --reset-live`, start it.

Going back to paper: set `dry_run` true and save. Posts stop, the paper ledger is loaded again, and the live file is left on disk. Redeem ticks no-op while `dry_run` is true.

`orjson` is optional. The VM was not given a new install. If `import orjson` fails, parsing uses the stdlib and `book_parser` in `feed_status` says `json`.

State events in `logs/lockbot.jsonl`: eval, signal, paper_fill, entry, fill, settlement, redeem, wallet_fill, wallet_compare, feed_status, cash_balance, live_switch_fail, live_client_ready, ledger. `lockbot_summary.py` prints P&L, paper-fill counts, latency, and the wallet comparison. Live redeem reuses `RedeemDesk` through `buy/relay_batch.py`. Dry-run logs `redeem_dry_run` and sends nothing.

Creating a pull request does not merge it and does not restart `polylockbot` or `polymintbot`.

<a id="changelog"></a>
# Changelog

- **2026-10-05** — Lockbot live blockers, not merged. The CLOB feed subscribes to the current and next BTC 5m/15m tokens, unsubscribes the rest, parses off the receive thread, and answers pings without touching a missing socket. Strategy 2 evaluates on Binance trades. The decision thread blocks on that event. Paper and live ledgers are separate files. `market_rules.*.combined_usd` overrides the global combined cap. `dry_run` false builds the order client on reload or logs `live_switch_fail` / `restart_required`. Live cash is pUSD. `polymintbot` is stopped on the VM (inactive, still enabled). Lockbot on the VM stays dry-run.
- **2026-10-04** — Lockbot head-to-head pairing (PR #235, merged as `ded7bf2`). A watched fill matches our nearest same-outcome signal inside `h2h_window_s` (10s). BTC 5m rows record the Binance 3s move and whether s2 fired on it. The summary reports the signalled share, the signed gap, the price difference, and missed versus no-move. Combined cap is $40 so each strategy keeps its $20. Mintbot is unchanged.
- **2026-10-04** — Lockbot wallet tape (PR #235, not merged). Log-only RTDS `activity/trades` comparison against NIULAI4, asdaefef, and dvasdkasodk on BTC 5m/15m. No order uses the tape. Mintbot is unchanged.
- **2026-10-04** — Lockbot strategy update (PR #235, not merged). Strategy 1 is the NIULAI4 BTC ladder (15m Z=0/Pmax=0.97, 5m Z=0.25/Pmax=0.90, $5 clips, side lock). Strategy 2 is the BTC 5m Binance-move sniper. Alts off. Combined cap $20, exposure $60, dry-run latency fill. Mintbot is unchanged.
- **2026-10-04** — Lockbot. Separate taker (`lockbot.py`), dry-run by default, not deployed. Mintbot is unchanged.
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
