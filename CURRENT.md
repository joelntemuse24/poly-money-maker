# Operational snapshot — 5 October 2026

Source: operator (Joel) statement of the live system on 5 Oct 2026, plus
`main` @ `14e119c`. The live Google VM (`poly-vm`,
`/home/ntemusejoel/poly-money-maker`) is the source of truth. This file
was written without touching the VM. Rows marked "(not re-confirmed
5 Oct)" carry the last confirmed value (3 Oct or 30 Sep) because the
operator did not restate them today.

## What is on and off

| Process | State 5 Oct 2026 |
|---|---|
| `polymintbot` (`mintbot.py`) | **LIVE.** `entry_enabled` true, `dry_run` false |
| `polylockbot` (`lockbot.py`) | **Lockbot removed Oct 5.** Copy-trading strategies s1/s2/s3 are deleted from `main` (PR #242). What remains is an removed entirely (Oct 5). The unit is **to be disabled** |
| `polyscrapbid` (`scrapbidder.py`, wallet B) | **OFF** |
| `polypathlog` (`pathlog.py`) | Retired since 22 Sep 2026. The operator should `sudo systemctl disable polypathlog` if it is still enabled |
| Buybots, complement, hedge, DangerZone, shadow bots, hourly-dense pathlog | Retired. Do not start them |

There is no copy-trading strategy and no wallet-follow strategy. The
operator abandoned the NIULAI4 copy. Do not document or restart it as a
live strategy.

## Operator aim (not a code limit)

- About **10–12 scraps a day, aiming at about $100** (operator's aim).
- **No daily cap exists in code.** Mintbot mints every eligible window
  that the sequential gate and cash allow.
- Joel distrusts optimistic backtests. The aim is a target, not a proven
  expected value. See the arithmetic in `TECHNICAL_DESIGN.md` §21.

## Wallet

About **$226** pUSD earlier on 5 Oct 2026. This is a snapshot, not a knob.

## Live mint knobs (gitignored `strategy_mint.json`)

Operator-stated on 5 Oct 2026:

| Knob | Live value |
|---|---|
| Series | `btc-up-or-down-15m` only |
| `shares` | **100** ($100 complete set: 100 Up + 100 Down) |
| `entry_enabled` / `dry_run` | true / false |
| `mint_sequential` | **true** (one bag at a time) |
| Scrap arm | opposite (favourite) ≥ **90¢** (`sell_opposite_min` 0.90), only in the **last 5 minutes** (`sell_scrap_max_ttm_s` **300**) |
| `sell_dump_enabled` | **false**. No held dump |
| Scrap oracle veto | **on**: `scrap_oracle_veto_enabled` true, `scrap_oracle_veto_usd` **5.0** |
| `notify_scrap_whatsapp` | **true** |
| `notify_dump_whatsapp` | false |
| `notify_danger_whatsapp` | false |

Carried forward, not restated today:

| Knob | Value | Last confirmed |
|---|---|---|
| `sell_enabled` | true | 30 Sep |
| `sell_threshold` (loser arm ceiling) | 0.03 | 3 Oct (not re-confirmed 5 Oct) |
| `sell_floor` (sweep limit) | 0.01 | 3 Oct (not re-confirmed 5 Oct) |
| `sell_fak_px` | 0.03 (ladder mode only) | 30 Sep (not re-confirmed 5 Oct) |
| `sell_scrap_sweep_enabled` | true (code default, not in file) | 30 Sep |
| `sell_persist_s` / `_last_min_s` / `_last_min_window_s` | 3 / 2 / 90 | 3 Oct (not re-confirmed 5 Oct) |
| `sell_scrap_fraction` | 0.5 (scrap 50, keep 50 at 100 shares) | 3 Oct (not re-confirmed 5 Oct) |
| `sell_scrap_rest_enabled` | false | 30 Sep (not re-confirmed 5 Oct) |
| `sell_winner_min` | 0.9995 (unreachable on a 0.001 tick) | 3 Oct (not re-confirmed 5 Oct) |
| `sell_winner_cheap_if_loser_le` | −1.0 (cheap winner gate closed) | 30 Sep (not re-confirmed 5 Oct) |
| `redeem_enabled` / `redeem_startup_sweep` | true / false | 3 Oct (not re-confirmed 5 Oct) |
| `mint_seq_lead_s` / `mint_seq_cutoff_s` | 30 / 240 (code defaults) | 3 Oct |
| `scrap_oracle_veto_stale_s` / `_use_live` | 3.0 / true (code defaults) | not set in the file as of 3 Oct |
| `sell_late_window_s` | 0 (late-window veto off) | 3 Oct |
| `poll_s` / `sell_armed_poll_s` | 1 / 1 | 30 Sep |
| `sell_dump_below` / `_persist_s` / `_max_ttm_s` / `sell_dump_also_kept` | 0.40 / 2 / 240 / true | 3 Oct. **Inert** while `sell_dump_enabled` is false |
| `notify_danger_px` / `_hold_s` | 0.70 / 5 | 3 Oct. WhatsApp danger alert is off; the `danger_zone` log line still appears |

`load_strategy` overlays only keys present in `DEFAULTS`. Keys the live
file omits take the code default. Do not edit the live file from git.

## The live money path

With the dump off, each bag has one path:

1. **Mint.** Sequential mode mints the next window from 30s before its
   open to 240s after it, once the previous bag's window has ended (or
   its winner was sold, which does not happen at 0.9995). A $100 split
   gives 100 Up + 100 Down.
2. **Scrap the loser near the close.** Inside the last 300s, if one leg's
   sized bid is ≤ 3¢ and the other is ≥ 90¢ for the persist (3s, or 2s in
   the last 90s), and the scrap oracle veto allows it, mintbot sends one
   FAK at the 1¢ floor for the scrap target. Sell FAKs fill the best bids
   first, so the average fill is usually 2–3¢. At fraction 0.5 the target
   is 50 shares and 50 are kept.
3. **Hold the winner to redeem.** The winner path needs a bid ≥ 0.9995
   and does not fire. The sell loop stops at `end_ts`. The
   `mintbot-redeem` thread redeems the bag from `end_ts + 60s`, once the
   condition has resolved on-chain.

Kept loser shares ride to resolution. They pay $0 on a normal win and
$1 each if the scrapped leg flips and wins. With the dump off nothing
sells them early.

Bags with no qualifying scrap (no leg ≤ 3¢ with the other ≥ 90¢ inside
the last 300s, or the veto blocks) hold both legs to resolution and
redeem at $100: flat before fees.

## Scrap oracle veto (on)

- Mintbot blocks a loser scrap while the in-memory Chainlink 60s TWAP or
  the live Chainlink price is within $5 of the strike or on the scrapped
  leg's side. Up is blocked while either `price − strike > −5`, and
  Down while either is `< +5`.
- It is re-checked every tick and right before the order goes out. A
  block resets the persist and logs `scrap_oracle_veto` (at most once per
  5s per bag).
- A live price older than 3s leaves the TWAP to decide alone. If both
  are stale, or the strike is missing, there is no veto and the scrap
  runs (`scrap_oracle_stale`).
- The strike is the Chainlink 60s TWAP sample at the window open,
  checked against Polymarket's `priceToBeat`. Polymarket settles each
  window on the same 60s average at the close.

## WhatsApp (CallMeBot)

- **Scrap fill: on.** One message per bag when its loser scrap completes,
  for example `Mintbot scrap: 11:30 bag | sold 50 DN @ 0.03 ($1.50) |
  4m02s left | kept 50 | UP bid 0.97`. If the window ends after a partial
  scrap, one "partial, window ended" message is sent instead.
- **Dump fill: off.** There is no dump to report.
- **Danger zone: off.** The `danger_zone` log line is still written when
  the held bid sits under 70¢ for 5s after a scrap, with `whatsapp=false`.
- `CALLMEBOT_PHONE` / `CALLMEBOT_APIKEY` live in the VM's `.env`. Alerts
  run on a background worker and never block the sell loop.

## Sequential bags and redeem

- One bag at a time. A bag stops blocking the next mint when its window
  ends. `max_open_sets` and the 14-minute lookahead are ignored.
- Short pUSD inside the mint range is a wait (`mint_seq_wait_cash`). Past
  the 240s cutoff the window is skipped (`mint_seq_skip`).
- At $100 bags and a wallet near $226, the next bag can mint at the open
  from free cash. The previous bag's redeem normally lands 1–3 minutes
  after its end, well before the following boundary.
- The redeem thread handles only bags this process minted
  (`redeem_startup_sweep` false). It retries with backoff and logs
  `redeem_gave_up` with an ntfy alert after 6 attempts.

## Lockbot (removed Oct 5)

PR #242 deleted the copy-trading strategies s1/s2/s3 and their
wallet-copy, NIULAI4-follow, entry, order and retry machinery.
`lockbot.py` on `main` is an removed entirely (Oct 5): no order client, no
entry path, `enabled` false and `dry_run` true by default. It only
settles old ledger positions and can log BTC books if
`book_log_enabled` is set. The `polylockbot` unit is to be disabled by
the operator. Do not reintroduce lockbot strategies.

## Sister scrap bidder (off)

`scrapbidder.py` / `polyscrapbid.service` is an opt-in bids-only process
for wallet B. It is off and stays off unless the operator asks. Its
knobs are in `strategy_scrapbid.example.json`; the design is in
`TECHNICAL_DESIGN.md` §28.

## Chainlink TWAP tape (audit only)

`oracle_log_enabled` stays on. Mintbot appends `logs/oracle_twap.jsonl`
while a bag is open and rolls it at 20 MB into `logs/archive/`. The
scrap oracle veto reads the feed's in-memory sample, never this file.
Mint, winner and redeem do not read the tape.

## Deploy boundary

Copy VM → GitHub for backup. Do not blindly merge GitHub onto the VM.
The deploy workflow pulls code and installs dependencies; it never
restarts a service. Restart `polymintbot` only when the operator asks.
Leave `polyscrapbid` off. Disabling `polylockbot` is the operator's job.
Creating a PR is not a merge, a deploy or a restart.

## Changelog

- **2026-10-05** — Rewritten for the live system.
  - polymintbot **LIVE** at 100 shares, sequential, scrap only in the last
    300s at opposite ≥ 90¢, scrap oracle veto on ($5), held dump off.
  - WhatsApp: scrap alert on; dump and danger alerts off.
  - Lockbot deleted Oct 5 (#242 strategies gone, #243 full delete): removed entirely (Oct 5), unit to be
    disabled. No copy-trading. scrapbidder off.
  - Knobs the operator did not restate are carried from 3 Oct / 30 Sep and
    marked "not re-confirmed 5 Oct".
- **2026-10-03 13:25 IST** — 200 shares, sequential + redeem on, persist
  3s / 90s window, `sell_dump_also_kept` on (#232). Superseded.
