# Agent guidance — Poly Money Maker

## Operational truth

The live VM is the source of truth. This repository snapshot was aligned
to the VM on 2026-09-19. Historical buy/hedge documents are gone from
this tree; they are not current operational instructions.

**polymintbot is stopped** as of 2026-10-05 (inactive, still enabled at
boot). `mintbot.py` and `strategy_mint.json` are unchanged. Do not start
`polymintbot` unless the operator asks. `polypathlog` (`pathlog.py`, 15m
only) is intentionally retired: keep it stopped and disabled, and do not
revive it. Buybots, complementbot, hedge, DangerZone, shadow
bots, and hourly-dense pathlog stay **off**. Do not start them, and do
not add those sources back. `scrapbidder.py` /
`deploy/polyscrapbid.service` is an opt-in bids-only sister process
(wallet B). It is not a restore of `complementbot.py`. Leave it stopped
until the operator asks.

Never infer service state from filenames or old documentation. Read
current configuration and read-only service status before operational
work.

**Live knobs (operator-confirmed 2026-10-03 13:25 IST; details in
`CURRENT.md`).** The numbers in "Code and validation" below are **code
defaults** unless they say live. Live `strategy_mint.json` runs:

- **Bags:** 200 shares a side, with `mint_sequential` true (one bag at a
  time, minted 30s before to 240s after the open).
- **Redeem:** `redeem_enabled` true, `redeem_startup_sweep` false.
- **Loser scrap:** arms at ≤ 3¢, `sell_scrap_fraction` 0.5 (sell 100 /
  keep 100), one 1¢-floor FAK, only with ≤ 360s left. Persist 3s, or 2s
  within the last 90s.
- **Dump:** under 0.40 for 2s with ≤ 240s left, and
  `sell_dump_also_kept` true (#232).
- **Unchanged:** `sell_winner_min` 0.9995, oracle veto off, WhatsApp
  danger alert on (70¢ for 5s).
- **Not live:** `sell_dump_tiers` (#231) and the post-dump kept stop
  (#228) are unmerged.

**Lockbot deleted.** `lockbot.py`, lock modules, tests, example config,
and `deploy/polylockbot.service` are removed. On the VM the unit is
stopped, disabled, and the unit file removed. Do not restore lockbot
unless the operator asks. Mintbot/scrap config untouched.

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
Loser scrap: sized opposite bid ≥ `sell_opposite_min` (~0.90), loser ≤
`sell_threshold` (0.02) **arms**. Persist `sell_persist_s` (5s), or
`sell_persist_last_min_s` (2s) when time-to-end is within
`sell_persist_last_min_window_s` (~60s). That 2s last-minute persist
applies through market close. `sell_persist_skip_when_sized` defaults
false, so a sized book still waits the full persist.
`sell_scrap_max_ttm_s` (code default 0, example 600) blocks arm, persist,
and every loser-scrap fire (sweep FAK, blind FAK, post-miss rest) while
seconds-to-close is above the cutoff. Unknown ttm leaves that gate open.
Persist starts only once ttm is at or under the cutoff, so a cheap bid
from earlier still waits the full persist. While gated, log
`sell_scrap_time_gated` (`condition_id`, `slug`, `leg`, `bid`, `ttm`,
`cutoff`) at most once per 15s per condition/leg. Then re-check in-range
at fire, `sell_scrap_sweep_enabled` (default true) posts one FAK at
`sell_floor` for the scrap remainder. `sell_scrap_fraction` defaults to
1.0 (the whole loser, same posts as before). Below 1, the first scrap
fire locks `target = floor(held loser shares × fraction)` and
`keep = held - target` on the bag. Sweep, ladder, blind, and resting
scrap orders post `target - filled` only. The loser is sold once that
target fills within tolerance, or the balance is at or under
`keep + tolerance`. Kept shares are not scrapped. They are dumped only
when `sell_dump_also_kept` is on (on live). Otherwise they cash
out only at `sell_winner_min` (the cheap 0.99 winner path does not
apply to the kept leg) or stay until resolution. Resolved positions are
redeemed only when the opt-in `redeem_enabled` is on (see below). The book still fills
higher bids first. Set the flag false to restore the 1¢ ladder from
`sell_fak_px` down to the floor, clipped to top-rung depth. The flag is
read on each sell tick. Do not post the sweep above the floor. Empty FAK or a vanished loser book after arm keeps `armed_ts`.
On `empty_keep_arm` / `empty_fak_keep_arm`, fire a blind 1¢ FAK
(`sell_scrap_blind_px`, backoff `sell_scrap_blind_backoff_s` ~3s). After
the first FAK miss while still armed, rest a GTD/GTC sell. The posted
price is `min(sell_scrap_rest_px, live or last-seen loser bid)` so a 1¢
book is not left at the 2¢ print. `sell_scrap_rest_px` stays 0.02.
GTD only when expiration is at least `sell_scrap_rest_min_ahead_s`
(~180s, Polymarket's floor) ahead; otherwise GTC. Cancel that rest on fill, window
end, loser no longer qualifies, or a hard late-window oracle block when
that veto is on. An empty book alone does not pull a rest that still has
edge. Out of range at fire logs `sell_cancel_out_of_range` and does not
POST. **Late-window oracle veto is off by default:** `sell_late_window_s`
= 0 skips the whole block (`in_late` requires the window > 0, and
`late_oracle_scrap_ok` returns `outside_late_window`). The edge knobs
`sell_oracle_edge_floor_usd`, `sell_oracle_edge_per_ttm`, and
`sell_oracle_stale_s` also default to 0, so setting only the window back
above 0 does not restore the old $25 / 1.5×TTM / 5s-stale veto.
`sell_oracle_edge_persist_s` stays 3. `oracle_log_enabled` stays true; the
tape is audit-only. With these defaults the late veto adds nothing.
**Scrap oracle veto (#234) is separate and on by default:**
`scrap_oracle_veto_enabled` true, `scrap_oracle_veto_usd` 5.0,
`scrap_oracle_veto_stale_s` 3.0, `scrap_oracle_veto_use_live` true
(hot-reloaded). With `margin = twap − strike` and `live_margin =
live_price − strike` (in-memory RTDS 60s sample, the live
`crypto_prices_chainlink` print from the same websocket, and the bag's
`open_ref`, read through `bag_view` with no I/O), scrapping Up is blocked
while either margin > −5 and scrapping Down while either < +5. It is ANDed into the scrap time gate, so
it covers arm, persist, sweep, blind, and rest, and it cancels a resting
scrap. It is re-checked right before the FAK, and a block resets the arm.
A live price older than 3s by `recv_ts` leaves the average alone; both
stale, or a missing strike, means no veto (log `scrap_oracle_stale`).
Blocks log `scrap_oracle_veto` (with `live_price` / `live_margin`) at
most once per 5s per bag. Scrap fills carry `oracle_margin` and
`oracle_live_margin`. Never apply it to the
dump, `sell_dump_also_kept`, winner, or mint.
Winner cash-out is a separate path at `sell_winner_min` (~0.999).
Live-bid FAK the winner, then clamp `limit = min(live_sized_bid,
sell_clob_max_price=0.99)` (floor `sell_clob_min_price=0.01`) so rich
0.995–0.999 books fill; log `sell_winner_limit_clamped` when live >
posted. Do not import `mintbot.py` in tests.

Mint relay gas: `submit_mint_batch` sets `ProxyTransactionArgs.gas_limit`
from one `eth_estimateGas` of the encoded batch (signer → proxy factory)
plus `mint_gas_margin` (default 0.15). If estimation fails, use
`mint_gas_fallback` (650000). Clamp to `min(mint_gas_cap, 650000)`.
`py_builder_relayer_client.gas` documents a ~650k relay-hub budget;
omitting `gas_limit` signs the library default 500000, and high-iteration
splits out-of-gas inside that stipend. Log `gas_limit` and `gas_estimate`
on `mint_submitted` and `mint_submit_fail`. The estimate runs only on the
mint and redeem submit paths. Keys absent from live `strategy_mint.json` keep these
defaults. Do not add the estimate to the sell loop.

Sequential bags (`buy/mint_sequence.py`) are opt-in (on live as of
2026-10-03): `mint_sequential` defaults false, with `mint_seq_lead_s` 30 and `mint_seq_cutoff_s` 240.
When on:

- The next window mints only in `[start - lead, start + cutoff]`. The
  14-minute lookahead and `max_open_sets` are not used.
- It mints only when no other live, non-dry, active bag still lacks
  `sold_winner`. The held dump also sets `sold_winner`. A bag whose
  window has ended no longer blocks.
- `seq_busy_bag` is checked both in `select_mint_candidate` and again in
  `_claim_mint_intent`.
- Short pUSD (`mint_cash_block`, including `pending_reserve`) is a wait:
  `mint_seq_wait_cash`, throttled to 30s, retried every mint tick. A
  previous bag still live logs `mint_seq_wait_prev`. Past the cutoff the
  window is skipped once with `mint_seq_skip`.
- Sell, scrap, and dump do not read mint time.

With the flag off, eligibility and capacity are byte-for-byte the old
path.

Auto-redeem (`buy/mint_redeem.py`) is also opt-in: `redeem_enabled`
defaults false (live: true, with `redeem_startup_sweep` false).

- **Thread.** It runs on its own `mintbot-redeem` thread, never on the
  sell loop, and writes no heartbeat. Jobs live in `positions_mint.json`
  → `redeems`. They come from landed bags at `end_ts +
  redeem_min_after_end_s` (60) and from one Data API `redeemable` sweep
  per start (`redeem_startup_sweep`, default true; neg-risk skipped).
- **Per job:**
  - Both legs flat on two reads → `nothing_held`. One zero read is not
    proof.
  - `payoutDenominator` 0 → wait.
  - Held value under `redeem_min_payout_usd` → `no_winner`, no transaction.
  - Otherwise persist `submitting`, then submit one relayer PROXY batch:
    optional CTF `setApprovalForAll(adapter)`, then adapter
    `redeemPositions(pUSD, 0, conditionId, [1,2])`. It goes through
    `submit_mint_batch` under `RELAY_SUBMIT_LOCK`, the same lock the mint
    submit holds, because both share the signer nonce.
  - Confirmed with legs flat → `done`. The intent becomes `completed` with
    `redeemed: true`.
- **Failure handling.** Failed, invalid, or timed-out
  (`redeem_tx_timeout_s` 300) submits back off from `redeem_retry_s` (60),
  doubling to a 900s cap. After `redeem_max_attempts` (6) the job is
  `gave_up`, with an ntfy alert. A crash in `submitting` is re-checked
  after the timeout.
- **Dry run.** `dry_run` logs `redeem_dry_run` and sends nothing.
- **No merge.**

`pathlog.py` records public CLOB books for **btc-up-or-down-15m** only.
It is retired: keep `deploy/polypathlog.service` in the repo but stopped
and disabled on the VM. Do not start it or `pathlog_hourly_dense.py`, and
do not add 5m/hourly back to `SERIES`.

Loser and dump fills record the real share-weighted average fill in
`sell_fill_px` / `sell_dump_fill_px`; `sell_limit` / `sell_dump_limit`
stay the last posted limit (the floor under sweep). The cheap-winner gate
and `bag_risk` read the fill first. `count_kept_loser_as_open` (default
false) keeps a bag with kept loser shares counted toward `max_open_sets`
until resolution. `sell_dump_persist_s`, `sell_cooldown_s` and
`sell_scrap_rest_min_ahead_s` respect an explicit 0. `poll_s` must be
≥ 1.

`mintbot.py` runs sell and mint as independent loops so Gamma/relayer
work cannot steal a dump tick. Do not re-serialize them into one
`manage_sells → discover → sleep` cycle. Persist / last-min / window
defaults are 5/2/60 (dump persist stays 2s); `sell_armed_poll_s` is
sell-loop cadence only. Live `strategy_mint.json` still wins for keys it
already sets. Code defaults arm at `sell_threshold` 0.02, print
`sell_fak_px` / `sell_scrap_rest_px` 0.02, `sell_scrap_rest_min_ahead_s`
180, persist 5 / 2, sized-skip off,
`sell_late_window_s` 0, floor / per-TTM / stale edge knobs 0,
`sell_oracle_edge_persist_s` 3, `sell_dump_max_ttm_s` 0 (example 240),
and `sell_scrap_max_ttm_s` 0 (example 600).
`oracle_log_enabled` stays true. New keys absent from the live file take these
defaults after the operator pulls and restarts. Do not edit live JSON
from this repo.

Wallet A (mintbot) never posts a bid. There is no same-wallet buyback.
`scrapbidder.py` is a separate process for wallet B. Cap is 20 shares
and `bid_max_px` 0.05 (`bid_rest_px` 0.05). With `scrap_hedge_enabled`
(default false), after A `sold_loser` on leg L,
B immediately FAK-buys 20 shares at the live ask, as long as the ask is
≤ `bid_fak_max_notional / shares` (1.50/20 = 7.5¢). If `shares * ask` is
under `bid_fak_min_notional` ($1), the FAK limit is raised to
`1/shares` (5¢) so the order notional clears $1; the fill is still the
cheaper ask. FAK only when that limit × shares is in [$1.00, $1.50].
If the take cannot fire, B rests a GTD/GTC BUY at `bid_rest_px` (5¢)
only when that rest is strictly below the ask (or the ask is missing).
A rest at or above the ask is skipped (`cross_ask`); it would be
marketable and rejected under $1. There is no escalate ladder. GTD
is posted when expiration is ≥ ~180s ahead; inside that, post GTC and
still cancel by T−`cancel_ttm_s`. A's `sell_scrap_rest_id` does not
block that leg, and the last-`active_ttm_s` (~180s) window does not apply
to that post-scrap hedge. Still cancel by T−`cancel_ttm_s` (~20s). The
winner leg A still holds stays blocked. Markets A never held are not
bid (`bid_absent_enabled` defaults false). There is no sister-miss held
dump. The normal held dump under `sell_dump_below` still applies. When
`sell_dump_max_ttm_s` > 0 it arms and fires only if seconds-to-close is
at or under that cutoff (example 240; code default 0 leaves the gate
off and keeps the old dump). A dip that starts before the cutoff must
still persist the full `sell_dump_persist_s` after entering it. Ladder
retries after the dump has fired are not re-checked.
`sell_dump_also_kept` (default false and false in the example; **true
live** since 10:31 IST on 2026-10-03)
also exits the kept scrap half in the same dump event. Once the held dump
fills, mintbot sells `min(sell_scrap_keep, on-chain balance)` of the
scrapped leg with the dump's live-bid FAK and refire. It then sends one
FAK at the 1¢ floor for any remainder and logs `sell_dump_kept`
(planned, sold, avg_px, outcome). This runs once per bag. Kept 0 is a
logged no-op. Turning the flag on after a dump does not sell later. When
that dump fills, mintbot sets `sell_dump_leg` and B FAK-buys
`dump_hedge_shares` (10) of the other leg, with its own
`dump_hedge_fak_min_notional` / `dump_hedge_fak_max_notional` (default
$1.00–$1.50, so 10 shares price in 10¢–15¢, or a non-crossing rest at
`dump_hedge_rest_px` 10¢). That clip is separate from the 20-share scrap
bid. With `topup_enabled` (default false), if B's pUSD is under
`topup_need_usd` (~$1.50) or a place fails
balance/allowance, `sister_topup.py` moves `topup_usd` ($5) of pUSD from
A's proxy to B's deposit wallet once per broke episode. If sold_loser on L
has no B bid or fill and the window is still open past cancel, log
`scrapbid_miss` (condition, leg, ttm, age) after ~10s, throttled ~30s.
Poll drops to `poll_hot_s` (~1s) while that gap is open. Each pass
re-reads mint intents after quoting, and quotes only open sister orders,
the late `active_ttm_s` window, or a sold leg — not every future market.
It never mints and never FAK-sells. `bid_enabled` defaults false and
`dry_run` defaults true.
Credentials stay in gitignored `.env.complement` (POLY_1271 / deposit
wallet). Refuse the mintbot funder. Do not put `.env.complement` values
in git. `deploy/polyscrapbid.service` stays off until the operator asks.

Shared `buy/` helpers exist for mint, pathlog, and the recording-only oracle tape:

- `buy/book.py` — CLOB top-of-book parsing (`pathlog`, mint sized bids)
- `buy/mint_sell.py` — sell fill parse, inventory latch, arm/persist.
  Also the log-only `bag_risk` counters (held-bid min, seconds below
  0.80/0.65/0.50, time-weighted `1 - bid` after scrap, scrap ttm / avg
  fill / loser best-bid size, dump ttm and price). `mintbot` emits one
  `bag_risk` line when the sell window closes. A failure there is
  swallowed. It does not change orders.
- `buy/mint_gas.py` — mint relay `gas_limit` (estimate + margin, 650k fallback, clamp to the ~650k hub budget). Mint submit only.
- `buy/mint_loops.py` — concurrent sell vs mint job runner + intent claim
- `buy/mint_sequence.py` — opt-in sequential mint range, busy-bag gate, wait/skip bookkeeping
- `buy/mint_redeem.py` — opt-in redeem job runner (`RedeemDesk`) with injected chain/relayer I/O
- `buy/market.py` — Gamma/CLOB discovery (`mintbot`, `pathlog`)
- `buy/chain.py` — Polygon eth_call prechecks (`mintbot`)
- `buy/contracts.py` — atomic mint calldata (`mintbot`), the opt-in redeem batch, and the pUSD transfer used by the A→B top-up
- `buy/oracle_log.py` — Chainlink BTC/USD 60s TWAP tape
  (`logs/oracle_twap.jsonl`). `oracle_log_enabled` stays on. The
  loser-scrap veto stays off (`sell_late_window_s` 0, and the floor /
  per-TTM / stale edge keys 0). `sell_oracle_edge_persist_s` stays 3.
 The tape file is audit-only.
 Not an input to mint, winner, or dump. The scrap oracle veto reads the
 feed's in-memory sample (`recv_ts` = local arrival). In a bag's last 360s
 the feed is hot: 5s silence watchdog, 2s redial, Gamma audit deferred. It rolls at 20 MB into
  `logs/archive/` (gzipped) via `buy/log_archive.roll_if_over`. A feed
  silent for 45s is reconnected (ping/pong on); stalls log once, a
  reminder a minute, and on recovery. The strike (`open_ref`) and close
  are the TWAP samples stamped exactly at window start / end (Gamma
  `priceToBeat` / `finalPrice`), labelled `strike_source` /
  `close_source`. crypto-price `openPrice` is only a labelled fallback
  after 75s with no boundary sample (a different series, up to ~$40 off).
  From ~10 min after the end, one Gamma `/events?slug=` request per tick
  (retry 2 min, stop at 1h) checks and corrects the tape. Never poll
  Gamma per second. Logging only.
- `buy/sister_bid.py` — wallet B buy policy. Post-scrap hedge (only
  with `scrap_hedge_enabled`, default false): FAK at the
  live ask, limit clipped into [`bid_fak_min_notional/shares`,
  `bid_fak_max_notional/shares`] (5¢–7.5¢ at 20 shares, $1.00–$1.50).
  Otherwise rest at `bid_rest_px` (5¢) only if that rest does not cross
  the ask. GTD when expiration is ≥ ~180s ahead, else GTC. No escalate
  ladder. A's rest and the 180s window do not block that leg; cancel
  near expiry. After A sets `sell_dump_leg`, B buys 10 shares of the
  other leg (`plan_dump_hedges`). `scrapbidder.py` posts both.
- `buy/whatsapp_notify.py` — CallMeBot WhatsApp alerts. Default on:
  `notify_danger_whatsapp`. After the loser scrap has filled, the held
  winner's sized bid (the dump's price) must stay under `notify_danger_px`
  (0.70) for `notify_danger_hold_s` (5s). A bid at or above the line
  resets the timer. It sends one message per bag, never after a
  dump/winner sale or window end. It always logs `danger_zone`.
  Default off: `notify_scrap_whatsapp` (scrap fill / partial window end)
  and `notify_dump_whatsapp` (dump fill). `CALLMEBOT_PHONE` /
  `CALLMEBOT_APIKEY` come from the mintbot `.env`; missing vars mean a
  no-op with one startup line. Bounded queue + one worker thread; never
  blocks the sell loop; never logs the key or the URL. Alerts only.
- `buy/sister_topup.py` — one $5 pUSD top-up from A to B per broke episode.
  `sister_topup.py` submits the PROXY batch. Scrapbidder spawns it.

Lockbot is deleted (entrypoint, `buy/lock_*`, `buy/relay_batch.py`,
summary script, example JSON, lockbot tests, and `polylockbot.service`).
Do not restore it. Mintbot and scrapbidder stay untouched.

Do not restore retired buybot modules (`entry_skip`, `hedge_gate`,
`btc_price`, `clob_book_ws`, `depth_ladder`, `strategy_coherence`,
`entry_rest_gtd`, complementbot, probe/journal helpers).

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
only `polymintbot` may be restarted, and only when the operator asks.
`polypathlog` is retired. `polyscrapbid` stays stopped until the operator asks to
start it. Lockbot/`polylockbot` is deleted; do not reinstall or start it.
