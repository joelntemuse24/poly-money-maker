# Agent guidance ? Poly Money Maker

## Operational truth

The live VM is the source of truth. This repository snapshot was synchronized on 2026-09-09; see CURRENT.md for its scope and hashes. Historical design documents describe older deployments and are not current operational instructions.

Hourly buy-side trading is the primary live system. Never infer service state from filenames or old documentation. Read current configuration and read-only service status before operational work.

## Safety boundaries

- Never read or commit .env files, credentials, private keys, or API secrets.
- Never start, stop, restart or signal live services without explicit operator authorization.
- Optional Cloud Agent SSH: runtime secret `POLY_VM_SSH_KEY` → `ssh poly-vm` as read-only `poly-auditor` (logs, strategy JSON, check scripts). Runtime secret `POLY_VM_SSH_OPERATOR_KEY` → `ssh poly-vm-rw` as `poly-operator`, which has no shell: allowlisted `who` / `status` / `git-status` / `pull` / `restart UNIT` / `write-strategy FILE` / `tail-log` only. `.env` ACL is deny. See CLOUD_RESEARCH.md ("Cursor Cloud → poly-vm SSH"). Still never read `.env`, never POST live orders, and never `pull` / `restart` / `write-strategy` unless the operator explicitly asks.
- Do not import buybothourly.py in tests: module initialization loads credentials, acquires a process lock and creates clients. Use pure buy/ helpers or AST-extracted functions with stubs.
- Perform development and tests in an isolated clone. Never install into the live virtualenv or write live runtime state.
- Preserve confirmed order identity, durable uncertain-order state, exact financial evidence, and expiry checks. A matched status or a transient zero balance alone is not proof of settled execution.
- Shared buy/ helpers also serve sibling bots; run the regression suite after changes.

## Code and validation

buybothourly.py is the hourly entry point. buy/entry_skip.py controls slice eligibility and caps; buy/hedge_gate.py contains exit helpers; buy/btc_price.py provides underlying prices; buy/clob_book_ws.py and buy/market.py provide books and discovery; buy/depth_ladder.py supports depth diagnostics. buy/strategy_coherence.py fail-closes nonsense hourly knob combos (soft-edge max vs buy floor, exit bid vs a22/b15 bands, dump < qualify <= recovery). See STRAT_COHERENCE.md for live tensions the validator does not rewrite. buy/late_edge_bleed.py is post-hour |live−PTB| late-vs-early analysis only (`check_late_edge_bleed.py`); it does not change entry or hedge.

buybot.py is the BTC 15m sibling. The $5 dry-run probe is `strategy_buy15m_probe.example.json` (`dry_run=true`, `entry_enabled=false`). See BUY15M.md. Do not enable `polybuybot` or flip those knobs live until Joel says. Do not import buybot.py in tests.

buybot5m.py is the BTC 5m sibling. The $5 dry-run probe is `strategy_buy5m_probe.example.json` (`dry_run=true`, `entry_enabled=false`). See BUY5M.md. Do not enable `polybuybot5m` or flip those knobs live until Joel says. Do not import buybot5m.py in tests. `strategy_buy5m.example.json` stays the historical last-120 75–90 paper template.

Run tests with python -m unittest discover -s tests -p 'test_*.py' -v in a disposable sandbox. Keep temporary files and Python caches in that sandbox. The hourly example mirrors captured strategy parameters with dry_run=true and entry_enabled=false. The separately committed live snapshot has dry_run=false; neither is authorization to launch the bot.

## Repository policy

This sync deliberately includes strategy_buyhourly.json as a reviewed non-secret snapshot so its bytes can be verified. Do not add other runtime configs, logs, backups, lock files, positions, P&L state, journals, wallet exports, or virtualenvs.

The existing main-branch deployment workflow pulls code and installs dependencies on the VM after merge. It does not restart services. Creating a PR is not authorization to merge or deploy it.
