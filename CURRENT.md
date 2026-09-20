# Operational snapshot — 19 September 2026

Source: live Google VM (`/home/ntemusejoel/poly-money-maker`). VM is the
source of truth.

**Live money path:** atomic mint on **15m only** (`polymintbot` /
`mintbot.py` + gitignored `strategy_mint.json`). **Live recorder:**
`polypathlog` / `pathlog.py` with series `btc-up-or-down-15m` only.

Buybots, complement, hedge, DangerZone, shadow bots, and hourly-dense
pathlog are **stopped / retired**. Do not start them.

## Live mint knobs (gitignored `strategy_mint.json`)

- Series: `btc-up-or-down-15m` only
- `shares`: 5 ($5 trial complete set)
- `entry_enabled`: true · `dry_run`: false
- Enter when window opens within 30 minutes and is **not yet open**
- `max_open_sets`: 1 with **adjacent-window lookahead**; `sold_loser` frees the slot
- `already_minted` blocks confirmed/in-flight; `failed` remints after `mint_fail_cooldown_s` (90s) up to `mint_max_attempts` (3)
- Sells on:
  - Loser: opposite ≥ 0.90, loser ≤ 0.03 persist 9s (5s in last 60s before end_ts), FAK 0.03 → 0.02
  - Winner: prefer 0.999 / redeem; allow 0.99 live-bid FAK only if loser sold ≤ 0.03 and loser+0.99 > $1
  - Held dump: after loser sold, if held sized bid < 0.80 for 5s → live-bid FAK
- Cycle sleep: live `poll_s=5`. Code default `sell_armed_poll_s=2` while sell is hot: loser persist arm, or after `sold_loser` until dump/winner exit (`sold_dump` / `sold_winner`). Persist 9/5/60 and dump persist 5s unchanged. Skip Gamma/mint while hot (do not require `capped_open` — post-loser mint raced dump on bag `btc-updown-15m-1789905600`).

See `TECHNICAL_DESIGN.md` for the full guided tour.

## Deploy boundary

Copy VM → GitHub for backup. Do not blindly merge GitHub onto the VM.
Restart **only** `polymintbot` and `polypathlog` when the operator asks.
