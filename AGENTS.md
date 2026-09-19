# Agent guidance — Poly Money Maker

## Operational truth

The live VM is the source of truth. This repository snapshot was aligned
to the VM on 2026-09-19. Historical design documents describe older
buy/hedge deployments and are not current operational instructions.

**Live services:** `polymintbot` (`mintbot.py` + `strategy_mint.json`) and
`polypathlog` (`pathlog.py`, **15m only**). Atomic mint on
`btc-up-or-down-15m`. Buybots, complement, hedge, DangerZone, shadow
bots, and hourly-dense pathlog stay **off**.

Never infer service state from filenames or old documentation. Read
current configuration and read-only service status before operational
work.

## Safety boundaries

- Never read or commit .env files, credentials, private keys, or API secrets.
- Never start, stop, restart or signal live services without explicit operator authorization.
- Optional Cloud Agent SSH: when runtime secret `POLY_VM_SSH_KEY` is set, `ssh poly-vm` is read-only as `poly-auditor` (logs, strategy JSON, check scripts). See CLOUD_RESEARCH.md ("Cursor Cloud → poly-vm SSH"). Still never read `.env` or place live orders.
- Do not import `buybothourly.py`, `buybot.py`, `buybot5m.py`, or `mintbot.py` in tests: module initialization loads credentials, acquires a process lock and creates clients. Use pure `buy/` helpers or AST-extracted functions with stubs.
- Perform development and tests in an isolated clone. Never install into the live virtualenv or write live runtime state.
- Preserve confirmed order identity, durable uncertain-order state, exact financial evidence, and expiry checks. A matched status or a transient zero balance alone is not proof of settled execution.
- Shared `buy/` helpers still serve retired sibling bots and mint; run the regression suite after changes.

## Code and validation

`mintbot.py` is the live entry point. It mints complete sets on
**btc-up-or-down-15m** only (`strategy_mint.example.json` mirrors the
template: dry_run=true, entry_enabled=false, sell_enabled=false). Optional
loser-leg FAK (3c then 2c) is off until live `strategy_mint.json` enables
it. Do not import `mintbot.py` in tests.

`pathlog.py` records public CLOB books for **btc-up-or-down-15m** only.
Keep `deploy/polypathlog.service`. Do not start `pathlog_hourly_dense.py`
or add 5m/hourly back to `SERIES`.

Buy-side bots are **retired**. `buybot.py` (15m), `buybot5m.py` (5m), and
`buybothourly.py` (hourly) remain as source for tests and shared helpers.
Their unit files live under `archive/deploy/`. Do not enable `polybuybot`,
`polybuybot5m`, `polybuybothourly`, `polycomplement`, or DangerZone.
Historical probe JSON (`strategy_buy15m_probe.example.json`,
`strategy_buy5m_probe.example.json`) stays dry-run / entry-off.

`buy/entry_skip.py`, `buy/hedge_gate.py`, `buy/btc_price.py`,
`buy/clob_book_ws.py`, `buy/market.py`, `buy/depth_ladder.py`, and
`buy/strategy_coherence.py` still support those retired bots and tests.
`buy/chain.py` and `buy/contracts.py` serve mint. `buy/late_edge_bleed.py`
is post-hour analysis only (`check_late_edge_bleed.py`); it does not
change mint or pathlog.

Run tests with `python -m unittest discover -s tests -p 'test_*.py' -v`
in a disposable sandbox. Keep temporary files and Python caches in that
sandbox. Live `strategy_mint.json` is gitignored; the example is not
authorization to launch the bot.

## Repository policy

Do not add runtime configs, logs, backups, lock files, positions, P&L
state, journals, wallet exports, or virtualenvs. Knob JSON already
tracked in git (examples plus the reviewed hourly snapshot) is fine.
15m/5m live knob files stay gitignored.

The existing main-branch deployment workflow pulls code and installs
dependencies on the VM after merge. It does not restart services.
Creating a PR is not authorization to merge or deploy it. After a pull,
only `polymintbot` and `polypathlog` may be restarted, and only when the
operator asks.
