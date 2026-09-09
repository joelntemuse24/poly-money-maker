# Operational snapshot ? 2026-09-09

Source: /home/ntemusejoel/poly-money-maker on the live VM. This is a source/configuration sync, not a strategy change or deployment. GitHub previously lagged the VM. Historical documents are not operational truth.

Hourly polybuybothourly.service is the primary live money path; polypathlog.service records market paths. The audit observed 5m, 15m and complement inactive. Recheck read-only service status before any operational decision; this document is a dated snapshot.

## Captured configuration

- a22: last 20 minutes, minimum ask 0.949, $40 reference budget, 8-second book persistence, reference price 0.95.
- b15: last 20 minutes, ask 0.90?0.94, $6 reference budget, 20-second book persistence, reference price 0.90.
- c5 disabled. One entry per named slice; both enabled slices can enter a market.
- Share-target sizing enabled. Market spend cap $48.50; buy_max_spend $49; buy_max_shares 55.
- Hedge threshold 0.60, oracle required, minimum oracle edge $10, dump persistence 8 seconds. Consult code for the exact oracle and toxic-dump exceptions.
- TP enabled: half at remaining-basis VWAP + 0.04, persistence 5 seconds; full-lock trigger 0.999. Trigger price and submitted limit are distinct.
- dry_run=false. This is a configuration snapshot, not permission to run it.

## Byte-identical truth files

| File | SHA-256 |
|---|---|
| buybothourly.py | 99266a0f1222095af9f4ea9f4c4470ceb3b5452b8ae078e0573c83b770d0cfff |
| buy/hedge_gate.py | 36db2ed1e5923e0eb3534e6e07a48c1609d9bd0773ee360b08eac392626f8283 |
| buy/entry_skip.py | ac6417e66f11e811d717a3ea93ef51b5a93531fae46058d801ccf34a88fb272b |
| buy/btc_price.py | 10a043d2fdd6f8e52a65a9d20d40355a9cded827ca30629351ac3ab314028aca |
| strategy_buyhourly.json | bebb505e1140fced394a1cccb90bbc91f80d0ccf880bc489837cec476d6897b9 |

strategy_buyhourly.example.json mirrors the captured strategy parameters with dry_run=true and entry_enabled=false; strategy_buyhourly.json preserves the exact live bytes. Code under buy/ and matching tests were copied from the VM, including depth_ladder.py. No audit-proposed refactors were applied as part of this sync. Source equality does not certify profitability, execution safety or passing tests; validation is reported separately in the PR.

## Deployment boundary

No live files or services are changed by preparation of this PR. The existing deploy workflow runs on qualifying main-branch pushes and performs git pull plus pip install on the VM. Merge only through the operator's deployment process; do not merge automatically.
