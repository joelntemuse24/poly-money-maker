# Hourly strategy coherence

`load_strategy` fail-closes on knob combos that cannot mean what they say.
Rules live in `buy/strategy_coherence.py` (pure; tests must not import
`buybothourly.py`).

The live VM is still the operational source of truth. This file records
tensions that validation cannot “fix” without changing the desk’s intent.
GitHub `strategy_buyhourly.json` remains the reviewed 2026-09-09 snapshot
(`dry_run=false` is not authorization to run it). **Do not copy that file
onto the VM.** Live strategy is a dirty working-tree file on the VM and is
the day-to-day source of truth. Live knobs in the tensions table are from
2026-09-13; the early-rich deploy table below is from a read-only check at
**2026-09-14 ~22:25 UTC**.

## Fail-closed rules

When `soft_edge_exit_enabled` and `underlying_gate_enabled`:

- `soft_edge_exit_max_usd` must be **strictly below** `min_underlying_edge_usd`.

`soft_edge_exit_max_usd` is the *entry* Binance-vs-PTB favor edge on the
held bag, the same units as the buy-side underlying floor. It is not a
dollar fill size. If max ≥ floor, every intentional fill is a soft-edge
dump candidate (Joel’s $40 floor / $50 max case).

When soft-edge is on, the exit bid must sit above the cheapest fill in
each enabled band:

- A22 on: `soft_edge_exit_bid > a22_min_price`
- B15 on: `soft_edge_exit_bid > buy_max_price`
- C5 on: `soft_edge_exit_bid > c5_min_price`

`soft_edge_has_price_edge(entry, exit_bid)` is false when
`entry >= exit_bid - epsilon` (no price edge, e.g. a 99¢ a22 ask vs a
95¢ exit). That is a per-fill check, not a load reject — a22’s
`high_buy_max_price` is 99¢ by design.

Hedge ladder (unchanged): `dump < qualify <= recovery_cancel <= 1`
(`hedge_toxic_bid_max == 0` disables dump and only requires
`qualify <= recovery_cancel`).

## Live tensions (2026-09-13)

These still load. They are desk choices, not validator bugs.

| Tension | Live knobs | Why it is awkward |
|---|---|---|
| Soft-edge is dead under the $40 floor | `soft_edge_exit_max_usd=7`, `min_underlying_edge_usd=40` | After the 50→7 cut, no legal buy (`edge ≥ $40`) can have `0 < entry_edge ≤ $7`. Soft-edge only fires on leftover / mis-stamped bags. |
| 99¢ a22 vs ~95¢ thesis | `a22_min_price=0.949`, `high_buy_max_price=0.99`, `a22_size_ref_price=0.95` | Size-to-ref aims at ~95¢, but the FAK cap still allows 96–99¢ prints. Soft-edge bid 0.95 has no price edge on those fills (and is size-gated off anyway). |
| TP +4¢ is useless on last-10m 99¢ fills | `take_profit_edge=0.04`, `take_profit_full_bid=0.999` | Half-TP needs bid ≥ VWAP+4¢. A last-10m 99¢ fill still needs the 99.9¢ full lock. **Early-rich bags** (stamped on fill) now full-sell at `early_rich_take_profit_bid` 0.99 instead; that knob does not change 0.999 for ordinary bags. |
| Hedge oracle $10 vs buy floor $40 | `hedge_oracle_min_edge_usd=10`, `min_underlying_edge_usd=40` | Entry demands a $40 BTC move; the hedge veto releases at $10. A fade from $40 to $11 still holds; $9 can sell. That is a looser exit oracle than the entry gate. |
| Soft-edge bid vs a22 band | `soft_edge_exit_bid=0.95` vs a22 `0.949–0.99` | Load allows it (`0.95 > 0.949`). Cheapest a22 has 0.1¢ of book edge; 99¢ fills would sell *below* entry if soft-edge were not already dead under the $40 floor. |
| Dump persist vs entry persist | dump 8s / 6s@5m / 2s@1m; entry 15s (a22) / 20s (b15) | Exits can arm faster than entries, especially in the last minute. Intentional, but easy to forget when raising entry persist. |

## Early-rich a22 vs old early_hot

`early_hot_defer_*` is a **half-cap**, not an early-open: it only fires when
`TTM > early_hot_defer_ttm_min` (15) *and* a22 is already in band
(`ttm <= a22_window_min`). Live last-10m a22 / last-15m b15 meant no sleeve
was open when early_hot could activate, so the day logged 0 early_hot events
while 98–99¢ last-10m a22 still filled.

Early-rich is a **separate additional gate** (does not replace last-10m a22):

| Knob | Default | Meaning |
|---|---|---|
| `b15_window_min` / `buy_window_min` | 20 | b15 90–94¢ band opens at 20 minutes (persist still `b15_entry_book_persist_s` ~20s) |
| `a22_window_min` | 10 | Normal a22 still last 10 minutes, floor `a22_min_price` ~0.949, persist `entry_book_persist_s` ~15s |
| `early_rich_a22_enabled` | true | Master switch |
| `early_rich_a22_window_min` | 20 | May open a22 *before* last-10m, while `a22_window_min < ttm <= 20` |
| `early_rich_a22_ask_min` | 0.97 | Inclusive floor for that overlay (up to `high_buy_max_price`) |
| `early_rich_a22_persist_s` | 90 | Favored-side ask must hold continuously; flicker below 97¢ clears the arm (`cond\|leg\|a22`) |
| `early_rich_take_profit_enabled` | true | Stamped early-rich bags full-sell at 99¢; does **not** lower live `take_profit_full_bid` (0.999) for last-10m a22 / b15 |
| `early_rich_take_profit_bid` | 0.99 | Full-lock threshold for those bags only |
| `early_rich_take_profit_persist_s` | 5 | Bid must hold at/above 99¢ this many seconds (same spirit as ordinary TP persist) |

Fills stamp `meta["early_rich_a22"]=true` so the held-bag loop can find them.
Half-TP is skipped on those bags (VWAP+4¢ cannot print on ~97¢). Soft-edge
does not cover them (edge floor $40 ≫ soft max). Last-10m a22 still waits
for `take_profit_full_bid` 0.999.

b15 stays allowed while early-rich a22 is active. Last-10m a22 does not
require 97¢ or 90s. Logs: `early_rich_a22_armed`, `early_rich_a22_waiting`,
`early_rich_a22_cleared`, `early_rich_a22_ready`, `early_rich_a22_fired`.
Early-hot half-cap is not applied inside the early-rich window.

## Deploy onto the live VM (already on GitHub main)

Checked read-only as `poly-auditor` on 2026-09-14 after the operator
merged `origin/main` (conflicts favored GitHub for `buybothourly.py`)
and restarted the hourly service:

- VM `HEAD` `8459e5e` (ahead of `origin/main` `c9e97c2` by older VM-only
  commits). **`buybothourly.py` sha256 `8c2a5594…` matches GitHub main.**
- Live `strategy_buyhourly.json` was **kept** (`a22 $180` / `b15 $50` /
  caps `$250` / `$255`). It is a dirty working-tree file. Do not replace
  it with GitHub’s Sep-9 snapshot.
- This tree still has **no** early-rich code (`early_rich_a22` count 0).

Apply path: pull **PR #165** onto this already-synced tree, then edit
the live JSON. Never `git checkout -- strategy_buyhourly.json`,
`git reset --hard`, or copy the GitHub snapshot over the VM file.

Live values to **keep**:

| Knob | Live VM 2026-09-14 |
|---|---|
| `a22_buy_budget` | 180 |
| `b15_buy_budget` | 50 |
| `buy_budget` | 50 |
| `market_spend_cap` / `buy_max_spend` | 250 / 255 |
| `buy_max_shares` | 280 |
| `a22_size_ref_price` / `b15_size_ref_price` | 0.92 / 0.90 |
| `a22_window_min` | 10 (already last-10m) |
| `a22_min_price` | 0.949 |
| `buy_threshold` / `buy_max_price` | 0.90 / 0.94 |
| `entry_book_persist_s` / `b15_entry_book_persist_s` | 15 / 20 |
| `min_underlying_edge_usd` | 40 |

Live values to **set** after the PR Python lands (JSON hot-reloads;
**Python changes need another authorized process reload**):

| Knob | Set on VM | Now |
|---|---|---|
| `b15_window_min` | **20** | 15 |
| `buy_window_min` | **20** | 15 |
| `early_rich_a22_enabled` | true | absent (code default true) |
| `early_rich_a22_ask_min` | 0.97 | absent |
| `early_rich_a22_persist_s` | 90 | absent |
| `early_rich_a22_window_min` | 20 | absent |
| `early_rich_take_profit_enabled` | true | absent (code default true) |
| `early_rich_take_profit_bid` | 0.99 | absent |
| `early_rich_take_profit_persist_s` | 5 | absent |

Missing `early_rich_*` keys are filled from `_STRATEGY_DEFAULTS`.
`b15_window_min` / `buy_window_min` are already in the live file, so
they will **not** change until edited.

Suggested order: copy the live JSON aside → `git fetch` + merge/cherry-pick
this stack (`buybothourly.py`, `buy/entry_skip.py`, `buy/hedge_gate.py`, tests, example) → confirm
`$180` / `$50` / `$250` / `$255` / `0.92` still on disk → set the knobs
→ reload the hourly service only when Joel authorizes it.

## What this repo does not change

- GitHub `strategy_buyhourly.json` stays the Sep-9 captured snapshot.
  New knobs live in `_STRATEGY_DEFAULTS` and `strategy_buyhourly.example.json`.
- Soft-edge keys stay code defaults (`enabled=false`) so older snapshots
  still load. Example `dry_run` / `entry_enabled` stay disarmed.
- Merge is not a restart and is not authorization to overwrite the VM.

## Late-window |edge| bleed (telemetry, not a knob)

`check_late_edge_bleed.py` measures whether the largest |live−PTB| fade toward
flat/flip sits in the last 2 minutes versus the prior 58. It does not change
entry, hedge, or size. On the VM after an hour:

```text
.venv/bin/python check_late_edge_bleed.py --last 12
.venv/bin/python check_late_edge_bleed.py --today --write
```

## How to check

```text
python -m unittest discover -s tests -p 'test_strategy_coherence.py' -v
python -m unittest discover -s tests -p 'test_late_edge_bleed.py' -v
```
