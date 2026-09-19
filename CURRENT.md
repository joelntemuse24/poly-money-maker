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
- Optional sell is **off** in the example (`sell_enabled=false`)
- Loser dump knobs (used only after Joel flips `sell_enabled` on the VM):
  arm at `sell_threshold` 0.03 while opposite ≥ `sell_opposite_min` 0.90,
  persist `sell_persist_s` 5s, then FAK to `sell_floor` 0.02. Winner
  stays for redeem at `sell_winner_min` 0.999. After a loser dump at
  ≤ 3¢, winner FAK may use the live sized bid ≥ 0.99. Sized bids need
  `sell_min_bid_size` 1.0. Live `strategy_mint.json` still has
  `sell_opposite_min` 0.5 until Joel raises it.

To enable sells on the VM (after this code is pulled, operator-only):
set `sell_enabled=true` in gitignored `strategy_mint.json`, set
`sell_opposite_min` to 0.90 (live is still 0.5), keep persist 5s /
threshold 0.03 / floor 0.02 / `sell_winner_min` 0.999, then restart
**only** `polymintbot` when the operator asks. Leave `sell_enabled=false`
in the committed example.

Live knobs are in gitignored `strategy_mint.json` on the VM.

## Deployment boundary

No live files or services are changed by preparation of this PR. The
existing deploy workflow runs on qualifying main-branch pushes and
performs git pull plus pip install on the VM. It must not restart
retired buy units. Merge only through the operator's process; do not
merge automatically. After a pull, restart **only** `polymintbot` and
`polypathlog` when the operator asks. Do not start
`pathlog_hourly_dense` or DangerZone.
