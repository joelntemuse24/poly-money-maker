# Hourly strategy coherence

`load_strategy` fail-closes on knob combos that cannot mean what they say.
Rules live in `buy/strategy_coherence.py` (pure; tests must not import
`buybothourly.py`).

The live VM is still the operational source of truth. This file records
tensions that validation cannot “fix” without changing the desk’s intent.
GitHub `strategy_buyhourly.json` remains the reviewed 2026-09-09 snapshot
(`dry_run=false` is not authorization to run it). Live knobs below are
from `/home/ntemusejoel/poly-money-maker/strategy_buyhourly.json` at
**2026-09-13 ~02:05 UTC**.

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
| TP +4¢ is useless on 99¢ fills | `take_profit_edge=0.04`, `take_profit_full_bid=0.999` | Half-TP needs bid ≥ VWAP+4¢. A 99¢ fill needs bid ≥ 1.03, which cannot print. Only the 99.9¢ full lock can exit those bags as TP. |
| Hedge oracle $10 vs buy floor $40 | `hedge_oracle_min_edge_usd=10`, `min_underlying_edge_usd=40` | Entry demands a $40 BTC move; the hedge veto releases at $10. A fade from $40 to $11 still holds; $9 can sell. That is a looser exit oracle than the entry gate. |
| Soft-edge bid vs a22 band | `soft_edge_exit_bid=0.95` vs a22 `0.949–0.99` | Load allows it (`0.95 > 0.949`). Cheapest a22 has 0.1¢ of book edge; 99¢ fills would sell *below* entry if soft-edge were not already dead under the $40 floor. |
| Dump persist vs entry persist | dump 8s / 6s@5m / 2s@1m; entry 15s (a22) / 20s (b15) | Exits can arm faster than entries, especially in the last minute. Intentional, but easy to forget when raising entry persist. |

## What this repo does not change

- Example / snapshot JSON bytes stay the Sep 9 captured shape
  (`dry_run` / `entry_enabled` differ only on the example). Soft-edge
  keys are code defaults (`enabled=false`) so those files still load.
- Live VM `buybothourly.py` already implements the soft-edge *exit
  loop*. This tree only adds keys + validation so the $40/$50 absurdity
  cannot load once that code is synced. Merge is not a restart.

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
