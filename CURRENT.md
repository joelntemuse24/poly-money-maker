# Operational snapshot — live knobs as of 5 October 2026

Source: live Google VM (`/home/ntemusejoel/poly-money-maker`). VM is the
source of truth. Mint knobs below were confirmed by the operator on
3 Oct 2026 at 13:25 IST (07:55 UTC). Rows marked "(30 Sep read)" were last
read from the VM on 30 Sep 2026 and were not re-confirmed on 3 Oct.

**polymintbot is live** for scrap (operator 2026-10-05). Do not stop it
or change `strategy_mint.json` unless the operator asks. This doc's mint
knob rows below remain the last confirmed snapshot.

**Lockbot deleted (2026-10-05).** Entry point, lock modules, tests,
example config, and `deploy/polylockbot.service` removed from the repo.
On the VM: unit stopped, disabled, unit file removed; no `lockbot.py`
process. Do not restore or start lockbot.

**Recorder retired:** `polypathlog` / `pathlog.py` (15m only) stopped on
22 Sep 2026 and is intentionally not restarted. The unit was still
`enabled` on 30 Sep; the operator should `sudo systemctl disable polypathlog`.

Buybots, complement, hedge, DangerZone, shadow bots, and hourly-dense
pathlog are **stopped / retired**. Do not start them.

## Live mint knobs (gitignored `strategy_mint.json`, 3 Oct 2026)

- Series: `btc-up-or-down-15m` only
- `shares`: **200** ($200 complete set: 200 Up + 200 Down)
- `entry_enabled`: true · `dry_run`: false (30 Sep read)
- **One bag at a time:** `mint_sequential` **true** (lead 30s / cutoff 240s, the code defaults).
  - The next window is minted only between 30s before its open and 240s after it.
  - It is minted only when no other bag is still open. A bag stays open until its winner is sold (`sold_winner`, which the held dump also sets) or its window ends.
  - Short pUSD is a wait (`mint_seq_wait_cash`) until the previous bag's cash is back, normally from the redeem.
  - `max_open_sets`, `enter_*_ttm_min` and the 14-minute adjacent lookahead are **ignored** while sequential is on. The older two-bag mode (`max_open_sets` 2 with lookahead) is no longer how the desk runs.
- **Auto-redeem:** `redeem_enabled` **true** and `redeem_startup_sweep` **false**. The `mintbot-redeem` thread redeems each landed bag from `end_ts + 60s`. There is no Data API sweep of older positions at start. Details are in the next section.
- `already_minted` blocks confirmed/in-flight; `failed` remints after `mint_fail_cooldown_s` (30s) up to `mint_max_attempts` (3), then that condition is skipped for the rest of its life
- `submitting` intents without a relayer `transaction_id` auto-fail after `mint_submitting_timeout_s` (90s by default, 0 disables) so restart ghosts cannot pin `wait_submit`
- Sells on:
  - **Loser scrap.**
    - **Arm:** opposite ≥ 0.90 and loser ≤ **0.03** (`sell_threshold`), only when seconds-to-close ≤ `sell_scrap_max_ttm_s` **360**.
    - **Persist:** `sell_persist_s` **3.0** (was 5 until 3 Oct). Within `sell_persist_last_min_window_s` **90** of the end, the persist is `sell_persist_last_min_s` **2.0**.
    - **Fire:** at fire it re-checks the range; out of range logs `sell_cancel_out_of_range` and does not POST. One FAK at the **live loser bid** (capped at `sell_threshold`, floored at `sell_clob_min_price` 1¢; `sell_floor` no longer clamps the sweep). A remainder retries next tick at the new live bid, so the scrap chases a falling bid instead of freezing at a fixed limit (bag `btc-updown-15m-1791215100`).
    - **Partial scrap:** `sell_scrap_fraction` **0.5**. The first fire locks target **100** / keep **100** of the 200 held (`sell_scrap_plan`, `sell_scrap_outcome`). The kept 100 ride to resolution unless the held dump fires (see `sell_dump_also_kept`).
    - Post-miss rest is off (`sell_scrap_rest_enabled` false). **The late-window oracle veto is off** (`sell_late_window_s` 0).
    - **Scrap oracle veto is disabled in the live sell path.** The pure helper retains these settings for tests: `scrap_oracle_veto_enabled` **false**, `scrap_oracle_veto_usd` **5.0**, `scrap_oracle_veto_stale_s` **3.0** and `scrap_oracle_veto_use_live` **true**.
      - The live sell path does not read the in-memory TWAP or live Chainlink price for a scrap veto.
      - The live gate returns `disabled` without reading the oracle bag view.
      - The pure helper still covers stale-reading behavior in unit tests.
      - Dump is untouched.
  - **Winner:** `sell_winner_min` **0.9995** and the cheap gate is closed (`sell_winner_cheap_if_loser_le` −1). The winner is held and redeemed by mintbot's own redeem thread.
  - **Held dump:**
    - After the loser is sold, if the held sized bid is < **0.40** (`sell_dump_below`) for **2s** (`sell_dump_persist_s`) with ≤ **240s** left (`sell_dump_max_ttm_s`), it sells the whole held leg.
    - Optional last-minute persist: `sell_dump_persist_last_min_s` (default = `sell_dump_persist_s`) applies when ≤ `sell_dump_persist_last_min_window_s` seconds remain (default 0 = off). The arm is not reset when the clock switches. Live will set `last_min_s=1.0`, `window=120` after merge; until then live stays at 2s.
    - The first shot is a live-bid FAK. If that returns no-match / kill with zero fill, it re-checks and fast re-fires a short descending ladder toward `sell_floor` (`sell_dump_fak_retries=2`, `sell_dump_ladder_step=0.04`, `sell_dump_ladder_rungs=4`).
    - Above 240s left the dump does not arm, and an in-progress persist is cleared. A blocked arm logs `sell_dump_time_gated` at most once per bag per 15s.
  - **`sell_dump_also_kept` true** (PR #232, live since 10:31 IST on 3 Oct):
    - In the same dump event, mintbot also sells the kept scrap half (`min(sell_scrap_keep, balance)` of the scrapped leg, normally 100). It uses the dump's live-bid FAK and refire, then one 1¢ FAK for any remainder.
    - The bag fully exits and leaves nothing to redeem. It runs once per bag and logs `sell_dump_kept`.
    - Operator rationale: the two legs' prices sum to about $1, so selling both caps every dump event at roughly **−$50 to −$70** at 200/side, whether it is a real reversal or a false dump. Without it, a false dump loses about $135 and a true flip about $35 (worked example in `TECHNICAL_DESIGN.md` §22). This gives up a little average EV for a bounded worst case.
  - **Not live:** `sell_dump_tiers` (tiered dump persist) exists only in unmerged PR #231 and is **not adopted**. The PR #228 post-dump kept stop is **not merged**. The dump is the single 0.40 / 2s / 240s rule above.
  - **Reclaim is off until the operator turns it on.** After a both-sides dump (`sold_dump`, and `sell_dump_kept_done` when `sell_dump_also_kept` applies), the sell loop can FAK-buy about $100 of the first side that holds at or above 91¢ for 0.5s (the same default as the dump persist). The other side has to confirm, the same way the scrap requires a favourite. A 75¢ stop (on by default, 0.5s) then sells that position at the live bid. `reclaim_enabled` defaults **false** in code and in `strategy_mint.example.json`. To enable, add the keys to gitignored `strategy_mint.json` (this repo does not edit that file). The new code needs one operator restart of `polymintbot` after the pull; later knob changes hot-reload on the next sell tick. Suggested keys: `reclaim_enabled` true, `reclaim_usd` 100, `reclaim_entry` 0.91, `reclaim_entry_persist_s` 0.5, `reclaim_stop` 0.75, `reclaim_stop_enabled` true, `reclaim_stop_persist_s` 0.5. Explicit `0` on either persist fires on the tick the condition is first seen. A negative is rejected. From the dump until the reclaim finishes, the loop stays on `sell_armed_poll_s`. Shares still held at resolution go through the existing redeem (`end_ts` + 60s). The reclaim FAK is a market buy: share size times the ask, truncated to whole cents, so the USDC maker amount has at most two decimals. A definite refusal (HTTP 400, invalid amounts, not enough balance) logs `reclaim_fak_reject` and is not an uncertain fill; that same price and size is not posted again. Timeouts still take the uncertain path. Leaving the buy for the stop logs `reclaim_done` with filled shares, average price, cost, and why the top-up stopped. Do not restart from this tree.
- **WhatsApp alerts (CallMeBot).** The danger-zone alert is **on** (`notify_danger_whatsapp`).
  - After the loser scrap fills, if the held winner's bid stays under `notify_danger_px` (0.70) for `notify_danger_hold_s` (5s), it sends one message per bag.
  - It never fires after a dump or winner sale, or after the window ends. It always logs `danger_zone`.
  - The scrap-fill alert (`notify_scrap_whatsapp`) and the held-dump alert (`notify_dump_whatsapp`) are both off by default.
  - `CALLMEBOT_PHONE` / `CALLMEBOT_APIKEY` live in the VM's `.env` (systemd `EnvironmentFile`). The alert runs on a background worker only, and logs `notify_sent` / `notify_failed` without the key.
- **Strike and settlement.** The strike is the Chainlink BTC/USD 60s average (TWAP) sample at the window open, self-checked against Polymarket's published `priceToBeat` (PR #229). Polymarket settles each 15m window on that same 60s Chainlink average at the close. The tape file is audit-only. After PR #234 is pulled, the scrap oracle veto reads the same feed's in-memory average and live price, not the file.
- **Loops.** The sell loop (`manage_sells`) and the mint/discover loop run concurrently. Live `poll_s` and `sell_armed_poll_s` are both **1** (30 Sep read). `main` validates `poll_s >= 1`, so the VM's old local `mintbot.py` floor patch is redundant; if it is still present, drop it (`git checkout -- mintbot.py`) before a pull. **The live JSON is untouched** by this repo; only the operator edits it.

## Repo code defaults (23 September 2026)

`strategy_mint.example.json` and `mintbot` `DEFAULTS`:

- `enter_max_ttm_min` **45**. A bag booked about 30m out still leaves the following 15m window inside the lookahead. `mint_max_attempts` **3**. `mint_fail_cooldown_s` **30**.
- `sell_threshold` **0.02**. Print is FAK `sell_fak_px` **0.02**, equal to `sell_floor` **0.02**, or the live bid when the book is thinner. Post-miss rest ceiling `sell_scrap_rest_px` stays **0.02**. The posted rest is `min(sell_scrap_rest_px, live or last-seen loser bid)` so a 1¢ book is not left at 2¢. GTD only when expiration is at least `sell_scrap_rest_min_ahead_s` (**180s**) ahead; otherwise GTC. `validate_strategy` requires `sell_floor` ≤ `sell_fak_px` ≤ `sell_threshold`.
- `sell_persist_s` **5**, `sell_persist_last_min_s` **2**, window **60s** (code defaults; live is 3 / 2 / 90). Dump persist stays 2s.
- `sell_persist_skip_when_sized` **false**. A sized book waits the full persist. The 2s last-minute persist applies through market close. There is no late TTM skip.
- `sell_late_window_s` **0** skips the late Chainlink scrap veto. `sell_oracle_edge_floor_usd`, `sell_oracle_edge_per_ttm`, and `sell_oracle_stale_s` are **0**, so raising only the window does not restore the old $25 / 1.5×TTM / 5s-stale veto. `sell_oracle_edge_persist_s` stays **3**. `oracle_log_enabled` stays false (audit tape).
- `scrap_oracle_veto_enabled` **false**, `scrap_oracle_veto_usd` **5.0**, `scrap_oracle_veto_stale_s` **3.0** and `scrap_oracle_veto_use_live` **true** (#234). These are the any-time loser-scrap oracle veto described in the live section. It is separate from the late-window veto and is never applied to the dump.
- The sister-miss held dump is gone. No `sell_dump_if_sister_miss_s`. The bid-under-`sell_dump_below` (code 0.80, live 0.40) persist dump is unchanged when `sell_dump_max_ttm_s` is 0 (code default). The example sets **240**: arm and fire only with that many seconds left or fewer. A filled normal dump sets `sell_dump_leg` (the leg A sold). Inventory already flat does not.
- `sell_scrap_max_ttm_s` is **0** in code (gate off) and **600** in the example. Above that cutoff the loser scrap does not arm, persist, or fire (sweep, blind, or a new rest). Unknown ttm leaves the gate open. Persist starts only after the gate opens. A blocked cheap bid logs `sell_scrap_time_gated` at most once per 15s per leg. The dump cutoff is separate. `bag_risk` is one log line at window close and does not trade.
- Wallet A never posts a bid. Same-wallet buyback is not implemented.

`load_strategy` overlays only keys already present in live `strategy_mint.json`. Keys that file already sets (`sell_threshold`, `sell_fak_px`, `sell_scrap_rest_px`, `sell_late_window_s`, persist) stay until the operator edits them. The live file now sets `sell_dump_max_ttm_s` 240, `sell_scrap_max_ttm_s` 360, persist 3 / 2 / 90 and `sell_dump_also_kept` true; a key it omits stays at the code default. A leftover `sell_dump_if_sister_miss_s` or `sell_persist_skip_ttm_s` key is ignored because it is no longer in `DEFAULTS`. Do not edit the live file from git.

## Sequential bags and auto-redeem (on live, 3 Oct 2026)

Both are off in code and in the example. The live file turns both on
(`mint_sequential` true, `redeem_enabled` true, `redeem_startup_sweep`
false, 200 shares). How they work:

**`mint_sequential`** (default false) keeps one bag of capital in play
instead of two:

- The next window is minted only from `mint_seq_lead_s` (30s) before its
  start to `mint_seq_cutoff_s` (240s) after it. There is no 14-minute
  lookahead.
- It is minted only once no other bag is still live and uncashed. A bag
  stops blocking when its winner is sold (`sold_winner`, which the held
  dump also sets) or its window ends.
  - A winner cashed at 0.9995 before the end lets the next bag mint at
    start − 30s.
  - An unsold winner frees the gate at the end. Its cash comes back from
    redeem.
- If pUSD is short inside that range, it waits and retries every mint tick
  (`mint_seq_wait_cash`, throttled to one line per 30s). Past the cutoff
  the window is skipped once (`mint_seq_skip` with `last_wait` and
  `waited_s`). A blocked previous bag logs `mint_seq_wait_prev`.
- Minting is price neutral, so a mint at the open costs the same.
  Sell, scrap and dump key off `end_ts` and the books, so a bag minted at
  the open sells exactly like one minted early.
- `max_open_sets`, `enter_*_ttm_min` and the adjacent lookahead are ignored
  while sequential is on.

**`redeem_enabled`** (default false) runs a fourth thread, `mintbot-redeem`:

- **Jobs.** From `end_ts + redeem_min_after_end_s` (60s), each landed bag
  gets a job in `positions_mint.json` → `redeems`. Once per start, a
  Data API sweep adds any other `redeemable` non-neg-risk positions
  (`redeem_startup_sweep`, default true; **false live**, so only bags this
  process minted are redeemed).
- **Each tick** (`redeem_poll_s`, 15s):
  - Both legs flat on two reads → `nothing_held`. This covers bags that
    were fully sold or redeemed elsewhere.
  - Not resolved yet → wait. On-chain resolution lands about 1–1.5 min
    after the end.
  - Resolved but the held legs pay under `redeem_min_payout_usd` → no
    transaction (`redeem_no_winner`). This is the case for a kept loser
    half that lost.
  - Otherwise it submits `adapter.redeemPositions(pUSD, 0, conditionId,
    [1,2])` through the same relayer PROXY path and signer as the mint.
    `setApprovalForAll(adapter)` is prepended once if missing.
- **Retries.** Failures back off 60s, doubling to a 15-minute cap, up to
  `redeem_max_attempts` (6). After that it logs `redeem_gave_up` and sends
  an ntfy alert. A submit with no answer is re-checked after
  `redeem_tx_timeout_s` (300s).
- **Logs.** `redeem_wait_resolution`, `redeem_submitted`,
  `redeem_confirmed`, `redeem_submit_fail`, `redeem_nothing_held`,
  `redeem_sweep`.
- **State.** A redeemed bag becomes `completed` with `redeemed: true`.
- **Safety.** It never runs on the sell loop. Mint and redeem share one
  relayer submit lock (same nonce). Under `dry_run` it logs
  `redeem_dry_run` and sends nothing.
- **Cost.** Gas is about 365k–453k per redeem (estimated + 15%, capped at
  650k), paid by the Polymarket relayer. Each redeem uses one relayer
  transaction from the builder quota.

Live keys for the one $200 bag: `"shares": 200`, `"mint_sequential": true`,
`"redeem_enabled": true`, `"redeem_startup_sweep": false`. Lead 30 / cutoff
240 stay at the defaults. A bag that dumps with `sell_dump_also_kept` on is
flat on both legs, so its redeem job ends as `nothing_held`.

## Sister scrap bidder (wallet B, opt-in, off)

`scrapbidder.py` + `deploy/polyscrapbid.service` buy from the complement deposit wallet. **20 shares**, rest at **`bid_rest_px` 5¢**. This post-scrap buy also needs `scrap_hedge_enabled` (default **false**, #212). When it is on and A `sold_loser` on leg L, B FAK-buys 20 shares at the **live ask** whenever that ask is ≤ **7.5¢** (`bid_fak_max_notional / shares` = 1.50/20). If `shares * ask` is under **$1**, the FAK **limit** is raised to **5¢** (`1/shares`) so the order notional clears Polymarket's marketable-BUY floor; the fill is still the cheaper ask. FAK only when limit × shares is in **[$1.00, $1.50]**. If that take cannot fire, B rests a GTD/GTC BUY at 5¢ only when the rest is strictly below the ask (or the ask is missing). A rest at or above the ask is skipped (`cross_ask`). A's scrap rest and the 180s window do not block that leg. There is no price ladder. GTD is used when the expiration is at least ~180s ahead; otherwise the order is GTC and still cancelled by T−20s / window end. The winner leg A still holds stays blocked. Markets A never held are not bid (`bid_absent_enabled` defaults false).

**Dump hedge:** when A sets `sell_dump_leg` (normal held dump only), B FAK-buys **`dump_hedge_shares` (10)** of the **other** leg. Same notional band via `dump_hedge_fak_min_notional` / `dump_hedge_fak_max_notional` (default $1.00–$1.50 → 10¢–15¢ at 10 shares). Otherwise rest at `dump_hedge_rest_px` (10¢) when that rest does not cross the ask. Fills live in `dump_filled`, separate from the 20-share scrap clip. Raise the max notional if the other side is richer than 15¢.

**A→B top-up:** only with `topup_enabled` (default **false**, #212). When a hedge place is wanted and B's on-chain pUSD is under `topup_need_usd` ($1.50), or the place fails balance/allowance, scrapbidder spawns `sister_topup.py --once --live`. That script reads mintbot `.env` (not `.env.complement`) and submits one gasless PROXY batch: pUSD `transfer` of `topup_usd` ($5) from A's funder to deposit wallet `0x2b2D1dA1a49E8BF73EbBC3EAC35D79cc88cd4ad2`. It refuses the Magic proxy `0xCfF5…` and refuses to sign as B. One accepted transfer per broke episode (`positions_topup.json`, gitignored). The episode closes when B's balance is back above the need, or when a place-fail that happened while B already looked funded is followed by a successful place. Scrapbidder calls `update_balance_allowance` (collateral) before a live place so the credited pUSD is the CLOB balance. `--live` still no-ops while strategy `dry_run` is true or `topup_enabled` is false. Do not add `.env` to `polyscrapbid.service`.

Manual check (no orders): `.venv/bin/python sister_topup.py --once --reason manual`. A real submit also needs `--live` and `dry_run` false. There is no second systemd unit.

If that sold leg has no B bid or fill for ~10s while the window is open past cancel, log `scrapbid_miss` (throttled ~30s) and poll at ~1s until the order is up. Books are quoted only for open sister orders, the last 180s, a sold leg, or a dump hedge, and mint intents are read again after that quote so a scrap during the pass is not planned as `a_still_long`. Never mint. Never FAK-sell. `bid_enabled` false and `dry_run` true until the operator turns them on. Credentials stay in gitignored `.env.complement`. Do not start `polyscrapbid` unless the operator asks. This is not `complementbot`.

See `TECHNICAL_DESIGN.md` for the full guided tour.

## Chainlink TWAP tape (audit only)

`oracle_log_enabled` defaults **off**. A missing key in live `strategy_mint.json` stays off; do not edit that file for this tape. While a 15m mint intent is open (including the pre-open bag and ~2 minutes after the end), mintbot appends `logs/oracle_twap.jsonl`.

The live path is Polymarket RTDS topic `crypto_prices_twap_sixty` for `btc/usd` (Chainlink's 60s TWAP, no Data Streams credentials). The strike (`open_ref`) is the TWAP sample stamped exactly at the window start, and the close is the one at the window end; that is what Gamma publishes as `priceToBeat` / `finalPrice`. Each `oracle_open_ref` row says where it came from (`strike_source`: `rtds_twap_at_start`, `crypto_price_open` fallback, or `gamma_price_to_beat` correction) and how late it was captured (`capture_delay_s`). crypto-price (`variant=fifteen`) `openPrice` is only a fallback when no boundary sample arrived within 75s (`oracle_strike_late`); it is a different series and was up to ~$40 off. About 10 minutes after a window ends, one Gamma request per tick (retry every 2 minutes, stop after an hour) checks both values (`oracle_strike_check`, `oracle_close_check`) and writes a corrected row on a mismatch.

Stored samples are 15s through the middle of the window, 2s around the open and in the last 3 minutes, and 1s in the last 60s and just after the end. The recorder wakes every second while a bag is open so that tighter cadence is not stuck behind a cold sleep. A silent feed logs `oracle_feed_stall` once, then at most once a minute, then `oracle_feed_recovered`; after 45s without a sample the socket is closed and reconnected with backoff (`oracle_feed_watchdog`, `oracle_feed_reconnect`). In a bag's last 360s (#234) the limit is 5s, a dead socket redials within 2s, and the post-window Gamma audit waits. Samples carry `recv_ts`, the local arrival time. crypto-price and Gamma errors log once per window per kind; a 429 backs off (20/40/80/120s crypto-price, 120/240/480/600s Gamma). A window with no end-boundary sample by 5 minutes after the end logs `oracle_window_end_missed`. The tape rolls at 20 MB into `logs/archive/oracle_twap.jsonl.<UTC stamp>` and is gzipped in the background, like `mintbot.log` (#219). Nothing is pruned.

**The tape is audit-only.** `oracle_log_enabled` stays off. `sell_late_window_s` is 0, and `sell_oracle_edge_floor_usd`, `sell_oracle_edge_per_ttm`, and `sell_oracle_stale_s` are 0, so the late veto stays off and those zeros do not re-arm the old dollar or stale veto. The scrap oracle veto is disabled in the live sell path; the pure helper remains for tests. `sell_oracle_edge_persist_s` stays 3. Mint eligibility, winner cash-out, and held dump do not read the tape.

## Lockbot — deleted

Removed 2026-10-05 by Joel: `lockbot.py`, `lockbot_summary.py`,
`lockbot.example.json`, all `buy/lock_*.py`, `buy/relay_batch.py` (lockbot-only),
lockbot tests, and `deploy/polylockbot.service`. Copy-trading strategies
were already gone (#242). VM unit stopped/disabled and unit file removed.
Mintbot and scrapbidder untouched.

## Deploy boundary

Copy VM → GitHub for backup. Do not blindly merge GitHub onto the VM.
`polymintbot` is live for scrap; do not stop it or edit mint/scrap knobs
unless the operator asks. `polypathlog` is retired. Lockbot/`polylockbot`
is deleted. Creating a PR is not a merge and not a restart.

## Changelog

- **2026-10-05** — Lockbot deleted entirely (code + VM unit). Copy-trading strategies s1/s2/s3 already removed (#242). Mintbot left live for scrap.

- **2026-10-03 13:25 IST** — Docs aligned to the live VM.
  - 200 shares a side, with `mint_sequential` and `redeem_enabled` on (`redeem_startup_sweep` off).
  - `sell_persist_s` cut from 5 to 3 today; the last-minute window is 90s.
  - `sell_dump_also_kept` turned on at 10:31 IST (PR #232).
  - `sell_dump_tiers` (PR #231) and the post-dump kept stop (PR #228) are not live.
  - Replaced the stale 100-share, two-bag and 5s/60s persist text.
