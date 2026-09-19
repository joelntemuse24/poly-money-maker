# Operational snapshot — 2026-09-19

Source: live Google VM (`/home/ntemusejoel/poly-money-maker`). VM is the
source of truth. This is a source/configuration sync, not a deployment
and not permission to start or restart services.

**Live money path:** atomic mint on **15m only** (`polymintbot` /
`mintbot.py` + gitignored `strategy_mint.json`). **Live recorder:**
`polypathlog` / `pathlog.py` with `SERIES = ["btc-up-or-down-15m"]`.

Buybots (`polybuybot`, `polybuybot5m`, `polybuybothourly`), complement,
hedge, DangerZone, shadow bots, and hourly-dense pathlog are **stopped /
retired**. Their Python sources and systemd units are not in this tree.
Do not start them. Recheck read-only service status before any
operational decision.

## Live mint template (example file)

`strategy_mint.example.json` is the committed dry-run template
(`dry_run=true`, `entry_enabled=false`, `sell_enabled=false`):

- Series: `btc-up-or-down-15m` only
- Shares: 50
- Enter when the window opens within 16 minutes and is not yet open
- `max_open_sets`: 1 (expired redeem holds do not consume this cap)
- Optional loser sell ladder 3c → 2c, opposite bid ≥ 50c — off in the example

Live knobs are in gitignored `strategy_mint.json` on the VM.

## Deployment boundary

No live files or services are changed by preparation of this PR. The
existing deploy workflow runs on qualifying main-branch pushes and
performs git pull plus pip install on the VM. It must not restart
retired buy units. Merge only through the operator's process; do not
merge automatically. After a pull, restart **only** `polymintbot` and
`polypathlog` when the operator asks. Do not start
`pathlog_hourly_dense` or DangerZone.
