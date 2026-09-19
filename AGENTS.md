# Agent guidance — Poly Money Maker

## Operational truth

The live VM is the source of truth. This repository snapshot was aligned
to the VM on 2026-09-19. Historical buy/hedge documents are gone from
this tree; they are not current operational instructions.

**Live services:** `polymintbot` (`mintbot.py` + `strategy_mint.json`) and
`polypathlog` (`pathlog.py`, **15m only**). Atomic mint on
`btc-up-or-down-15m`. Buybots, complement, hedge, DangerZone, shadow
bots, and hourly-dense pathlog stay **off**. Do not start them, and do
not add their sources back.

Never infer service state from filenames or old documentation. Read
current configuration and read-only service status before operational
work.

## Safety boundaries

- Never read or commit .env files, credentials, private keys, or API secrets.
- Never start, stop, restart or signal live services without explicit operator authorization.
- Optional Cloud Agent SSH: when runtime secret `POLY_VM_SSH_KEY` is set, `ssh poly-vm` is read-only as `poly-auditor` (logs, strategy JSON, check scripts). See CLOUD_RESEARCH.md ("Cursor Cloud → poly-vm SSH"). Still never read `.env` or place live orders.
- Do not import `mintbot.py` in tests: module initialization loads credentials, acquires a process lock and creates clients. Use AST-extracted functions with stubs, or the pure `buy/` helpers mint/pathlog actually import.
- Perform development and tests in an isolated clone. Never install into the live virtualenv or write live runtime state.
- Preserve confirmed order identity, durable uncertain-order state, exact financial evidence, and expiry checks. A matched status or a transient zero balance alone is not proof of settled execution.

## Code and validation

`mintbot.py` is the live entry point. It mints complete sets on
**btc-up-or-down-15m** only (`strategy_mint.example.json` mirrors the
template: dry_run=true, entry_enabled=false, sell_enabled=false). Optional
sell stays off until live `strategy_mint.json` sets `sell_enabled=true`.
Loser dump: sized opposite bid ≥ `sell_opposite_min` (~0.90), loser ≤
`sell_threshold` (0.03) persists `sell_persist_s` (~9s), or
`sell_persist_last_min_s` (~5s) when time-to-end is within
`sell_persist_last_min_window_s` (~60s), then FAK
threshold → `sell_floor` (0.02) when the live sized bid is ≥ floor, or
at the live bid if it is below the floor (empty FAK keeps the arm).
Winner cash-out is a separate path at `sell_winner_min` (~0.999). Do not
import `mintbot.py` in tests.

`pathlog.py` records public CLOB books for **btc-up-or-down-15m** only.
Keep `deploy/polypathlog.service`. Do not start `pathlog_hourly_dense.py`
or add 5m/hourly back to `SERIES`.

Shared `buy/` helpers exist only for mint and pathlog:

- `buy/book.py` — CLOB top-of-book parsing (`pathlog`, mint sized bids)
- `buy/mint_sell.py` — sell fill parse, inventory latch, arm/persist
- `buy/market.py` — Gamma/CLOB discovery (`mintbot`, `pathlog`)
- `buy/chain.py` — Polygon eth_call prechecks (`mintbot`)
- `buy/contracts.py` — atomic mint calldata (`mintbot`)

Do not restore retired buybot modules (`entry_skip`, `hedge_gate`,
`btc_price`, `clob_book_ws`, `depth_ladder`, `strategy_coherence`,
`entry_rest_gtd`, complement/probe/journal helpers).

Run tests with `python -m unittest discover -s tests -p 'test_*.py' -v`
in a disposable sandbox. Keep temporary files and Python caches in that
sandbox. Live `strategy_mint.json` is gitignored; the example is not
authorization to launch the bot.

## Repository policy

Do not add runtime configs, logs, backups, lock files, positions, P&L
state, journals, wallet exports, or virtualenvs. Knob JSON already
tracked in git (`strategy_mint.example.json`) is fine. Live mint/buy
knob files stay gitignored.

The existing main-branch deployment workflow pulls code and installs
dependencies on the VM after merge. It does not restart services.
Creating a PR is not authorization to merge or deploy it. After a pull,
only `polymintbot` and `polypathlog` may be restarted, and only when the
operator asks.
