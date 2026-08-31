# Last-120 5m — first full overlay tape (27–31 Aug 2026)

Research note. Operator shipped **B+C** (persist 1s, dump 40¢, flatten
walks avg <75¢) on the VM **31 Aug ~22:25Z** and **`$10`**
**31 Aug ~22:58Z**. Next is the last-30s hedge ladder (TTM>30 stays
40/50/52/53; TTM≤30 is dump 40 / persist 58/60 / recovery 62). Do **not** re-paste `$10`. Do
**not** paste last-45 + $25. Do not size up from **$2.50**. Pastes
are in `CURRENT.md`.

Live (VM 31 Aug ~15:49Z): last **120s**, winning ask **75–90¢**, one
**$2.50** FAK @ **90¢**, `late_90` / early / ≥95 **off**,
`min_underlying_edge_usd` **$0**. Hedge: persist **5s @ 50/52**, sell
live bid **<53¢**, dump **≤32¢** ignore-oracle, persist-sell still
needs TWAP against/flat. Overlay pasted **27 Aug ~17:26Z**.

The bot executed that table. Wallet **~$120.66 → $129.73** in ~94h ≈
**+$0.10–$0.12/h**. Recap-sum of 93 Dublin hours **+$11.48** (47 green /
43 red / 3 flat). Break-even WR ≈ **80%**; live ≈ **82–83%**. Knife
edge. **Do not size up on this fill quality.**

Cloud cannot see the VM journal, pathlog ticks, or
`exports/trades_last120.csv`. Numbers below are the operator brief
plus WhatsApp hourly recaps (Drive) and public Coinbase 1m around
cluster hours. Unresolved bags are not losses.

---

## 1. Where the dollars went

A **loss** is a recap-named sell-or-held-to-zero on a resolved market.
**62 named + 6 unnamed recap counts ≈ 68** (brief: **69** recap
losses / 411 fills). Dollar drag assumes **$2.50** spend and
`shares = 2.50 / fill` when a sell price is known.

| Bucket | n (named) | Est. drag | Avg | Notes |
|---|---:|---:|---:|---|
| **Held-to-zero** | **30** | **$75.00** | −$2.50 | Main leak. Book never sold. |
| **Dump ≤32¢** | **24** | **$49.54** | −$2.06 | Median recover ~1–20¢. Delayed total loss. |
| Persist 32–53 | 5 | $5.68 | −$1.14 | The only “real” hedges. |
| Scratch ≥53 | 3 | $1.78 | −$0.59 | Includes 60¢ / 63¢ / 75¢. |
| Unnamed recap losses | 6 | ~$15 if h2z | — | Early 28th hours + 30th h23. |
| **Named total** | **62** | **$132** | −$2.13 | |
| Walk fill &lt;75¢ (subset of above) | 24 | $51.25 | −$2.14 | Loss-only; winners not in this table. |
| In-band 75–80 | 13 | $28.92 | −$2.22 | |
| In-band 80–90 | 25 | $51.83 | −$2.07 | Same dollars as walks. |
| Dublin day 08–18 | 25 | $50.18 | — | Hour P&amp;L still **+$25.05**. |
| Evening 19–23 | 15 | $32.93 | — | Hour P&amp;L **−$10.45**. |
| Night 00–07 | 22 | $48.89 | — | Hour P&amp;L **−$3.12**. |

**Wallet sells (brief, full overlay):** 52 sells / 411 fills = **12.7%**.
Dump ≤32 **33**, persist 32–53 **11**, scratch ≥53 **8**. Sell cash vs
spend on those 52 ≈ **−$87**. **8 sold-then-won**. Held-to-zero ≈
69 − 44 true-loser-sells ≈ **25** (named catalog has **30** h2z; the
gap is recap vs CSV join).

**UI CSV with Redeems (29 Aug 03:01Z–31 Aug 15:21Z, 256 fills):**
WR **82%**. Winner median **+$0.48**. Sell-only 33, median **−$2.10**.
Walk subset 32 fills **−$6.61** (18 redeemed / 12 sold). Band 75–90:
207 fills **+$24.50**.

**Why +$0.48 / −$2.10:** a median **84¢** winner is ~3 shares × $1 =
+$0.48. A dump that recovers **~$0.19–$0.40** on a $2.50 bag is
**−$2.10**. Four winners pay one loser. Live is one extra winner per
twenty fills.

### Q1 answers

1. **Fraction of named loss dollars:** (a) walks &lt;75 **39%** ($51 /
   $132); (b) 75–80 **22%**, 80–90 **39%**; (c) held-to-zero **57%**
   ($75) — this is the pile; (d) empty-FAK-then-sold is inside the 52
   wallet sells / 91 attempts (39 attempts sold nothing); (e) 8
   sold-then-won (not in the loss-dollar table; they stole a
   would-be +$0.48); (f) evening+night hour P&amp;L **−$13.57** vs day
   **+$25.05**. Night+evening *named* drag is $82 vs day’s $50, but
   day still holds the two worst clock hours.

2. **No single entry filter removes a lot of loss $ and little winner
   $.** Walks look bad *as a loss catalog* ($51) but the UI walk
   *subset including winners* is only **−$6.61 / 32**. Scaled to 73
   overlay walks that is about **+$0.16/h** if refused — useful, not
   the business. Skipping 75–80 without winner-split is overfitting.
   Time-of-day 08–18 Dublin would have kept **+$25** of the **+$11**
   recap-sum, but the two worst hours (**28th h15 −$7.10**, **30th
   h16 −$6.61**) are *day* hours. Do not add a vol/momentum buy skip
   (already rejected). `$25` edge is a scored counterfactual only:
   pathlog+Binance join (PR #132) said last-45+$25 was −EV on books.

3. **48 `saw_in_band` misses:** brief’s 1m CLOB history is mostly
   **99/1 wicks** (`up_max=0.135 dn_max=0.995`). A few look restable
   (dn 0.820 / 0.765, up 0.865 / 0.825). Treat as wick noise unless
   the VM journal shows `buy_skip_rest_confirm` / `ambiguous` on
   those slugs. Not an entry bug to “fix” from cloud.

4. **Cluster hours are BTC-move cascades, not coin-flips.** Coinbase
   1m (public; Binance 451 from this host):

   | Hour (Dublin) | Named losses | BTC (UTC window) |
   |---|---|---|
   | 28th h15 (−$7.10) | 88¢ Down h2z, 59¢ Up h2z, 87¢ Down dump 41¢ | 14:00Z **−$542**, 5m range **~$980** |
   | 30th h16 (−$6.61) | four consecutive clocks 16:38–16:53 | 15:35–16:55Z grind **+$400**, 16:10Z **+$292** spike |
   | 28th h19 (−$5.10) | 37¢ Down h2z, 87¢ Up dump 1¢ | slow grind **−$341** |
   | 29th h01 (−$4.17) | 77/61 h2z + 80 dump 23 | night cascade, same side |

   One impulse prints two or three 5m clocks in a row. A −$7 hour
   wipes a clean redeem day.

---

## 2. Hedge autopsy — gate vs empty FAK vs slow persist

| Signal | Count | Read |
|---|---:|---|
| `hedge_skip_recovery` | 14,805 | Bid ≥53¢. **Working.** Not selling 55–90. |
| `hedge_skip_oracle_still_winning` | 2,793 | Persist blocked while TWAP still agrees. |
| `hedge_attempt` | 91 | Qualify fired, POST attempted. |
| `hedge_fail` | **91** | Confirm path logs fail every time. |
| `hedge_fill` | **0** | Tape lie. |
| `hedge_uncertain_resolved` | **52** | Matches 52 CSV sells. |
| `hedge_ghost_unconfirmed` | 50 | Same class. |
| `sell_attempt_rejected` | 293 | ~3.2 rejects / attempt (retries *are* firing). |
| Wallet sells | 52 | 33 dump / 11 persist / 8 scratch. |
| Attempts that sold nothing | **39 / 91** | Held-to-zero or recovered. |
| Named held-to-zero | **30** | Many never printed 50/52 or ≤32. |

**Q2.1 Trigger reconstruction (named sells + brief 52):** dump ≤32 is
the economic mode. Persist 32–53 recovered real money (avg −$1.14 vs
−$2.06). Scratches exist (8 CSV / 3 named) and include the 75¢ print
on 30th 16:53 (`sell_gt_72`). Median recover **~$0.19–$0.40** → about
**16¢ on the dollar**.

**Q2.2 Held-to-zero split (cannot journal-grep from cloud):** 30 named
h2z vs 91 attempts vs 52 sells. Roughly **half of true losers never
got a persist/dump qualify** (book stayed 55–80 or gapped 80→0
without resting at 50/32). The other miss is **attempt then empty
FAK** (39/91). Oracle/GUI veto until 0 is the 2,793
`oracle_still_winning` + 52 `no_consensus` events — we cannot split
those by later-lost vs later-won without the VM tape. Recovery+oracle
**is** holding winners (14.8k recovery skips). The miss is **loser
exit quality**.

**Q2.3 Unmatched sell retry already exists.** `sell_market_with_retry`
re-quotes unmatched 400s; dump / `persist_done` uses
`max_retries=12`. `hedge_should_keep_retrying` / `hedge_fail_is_terminal`
keep a live dump from idling. This is **not** the 21 Aug hourly hole
(undercut 2¢ / no sell retry). **0 `hedge_fill` is confirm/logging:**
POST returns empty → `hedge_fail` → later inspect logs
`hedge_uncertain_resolved` and the wallet is already flat. This PR
emits `hedge_fill` on that inspect path (`via=uncertain_resolved`)
so the next 48h tape matches the wallet. **No P&amp;L change.**

**Q2.4 / Q2.5 Counterfactual loser exits (named 62 + 6 unnamed as h2z):**

| If every named loser sold at | Remaining drag | Save vs tape | +$/h on 93h | Winner-dump risk |
|---|---:|---:|---:|---|
| **50¢** | $28 + $7.5 unnamed | **~$112** | **+$1.20** | Upper bound. Pathlog paper 75–90/120 printed **0** 50/52 persists. Live extra clocks *do* print 50 — 8 STW already. |
| **32¢** | $73 + $10 unnamed | **~$63** | **+$0.68** | We already *try* to dump at 32; fills print 1–20¢. Saving this means hitting the 32¢ bid, not waiting for 1¢. |
| First bid ≤50 after persist | between 32 and 50 | — | — | Live fade already does this **after** persist completes. The hole is persist never completing. |

Cloud could not run `--hedge-sweep` (no ticks here). Paper
`--compare --paper` on 3158 markets: **0 hedges** on restable 75–90.
Persist 0/1/2s vs 5s on *losers only* is the right VM next step;
faster persist will also dump some of the 8 STW class.

**Q2.6 Oracle / GUI skips on losers vs winners:** 2,793
`oracle_still_winning` is the intended false-hedge brake (hourly
lesson). It blocks persist-sell while BTC still agrees — that is
exactly when a 5m book can look 50/52 for one second on a winner.
Dump ≤32 already bypasses oracle. The cost is losers that fade
60→20 while TWAP has not crossed, then dump at 1¢ or h2z. **Do not
turn oracle off** without a loser/winner split on the VM journal.

Operator hypothesis **holds:** better loser exits (not more entries,
not size) is the path from +$0.10/h toward +$0.20–$0.40/h at $2.50.

---

## 3. Ranked changes (B+C now shipping; D is still no)

| Rank | Change | Tape $ | Winner-dump risk | Ship? |
|---|---|---|---|---|
| **B** | **Loser exits:** persist **1s** @ 50/52 and dump while bid is still **40¢** (hit the 40¢ bid, don’t wait for 1¢). | Illustrative: if true losers recovered **$1.00–$1.25** instead of **$0–$0.40**, loser drag shrinks ~$50–$110 on this tape → **+$0.20–$0.40/h at $2.50**. | 8 STW already. Faster persist adds more. | **Shipped this PR (5m-only).** Code defaults + example JSON hedge knobs + live paste in `CURRENT.md`. |
| **C** | **Flatten walks** (`avg &lt; 75¢`) at live bid while bid **&lt;75¢**. Must run *before* recovery 53 or a 70¢ walk HOLDs. | UI walk subset **−$6.61 / 32**; ~**+$0.16/h** if the 73 overlay walks match. | Low (walk WR ~56% vs 82%). | **Shipped this PR** with B. `hedge_flatten_walks` + `toxic_force_exit_below=0.75`. |
| **A** | **Tape: `hedge_fill` on `uncertain_resolved`.** Sell unmatched retry is already in 5m. | **$0.** Makes the next 48h measurable. | None. | **Already in this PR, 5m-only.** |
| **D** | Tighter entry so take-rate falls toward paper (36% of clocks vs paper ~6–14%). | Extra clocks are walks + loose in-band. Live WR **82%** vs paper **96%**. Tightening toward restable 75–90 cuts losers *and* a lot of +$0.48 winners. | Low dump risk, high opportunity cost. | **Not this PR.** Do not re-open last-45+$25. |

`two_slice_missing` is **not a leak** (early slice is off).

**Walks vs toxic:** 73 `buy_fill_below_band`. This PR raises
`toxic_force_exit_below` to **75¢** and flattens those bags at the live
bid while bid **<75¢**. Pre-change, junk walks (`avg &lt; 65¢`) armed
`toxic_fill` but dump waited for **bid ≤32**, which is why walk bags
showed up as held-to-zero or 1–20¢ dumps.

**Time of day:** Dublin day **+$0.61/h** vs evening **−$0.52** vs night
**−$0.10**. Session filter is illustrative only. Clusters sit inside
day hours.

**Scaling:** linear map of **+$0.40/h at $2.50** → **~$1/h** is **2.5×**
(~$6.25) and **~$2/h** is **5×** (~$12.50). Walks and empty FAKs get
worse at size. **Do not scale until (B) is evidenced on a live-shaped
tape.**

Do not start hourly/15m as the fix. 5m time-scale is not the problem;
exit quality on 5m losers is.

---

## 4. Measure on the next 48h (clock started 31 Aug ~22:25Z)

B+C is live. Operator pasted and restarted `polybuybot5m` **31 Aug
~22:25Z**. Printed: persist 1.0 dump 0.4 min 0.4 toxic 0.75 flatten
True start 120 edge 0.0 dry_run False entry True. Services: inactive /
active / inactive. On the next 48h tape:

- `hedge_fill` should rise toward wallet sells; `hedge_fail` then
  `uncertain_resolved` should no longer be the only sell story.
- Sell px median (want **≫ 19¢**; **40–50¢** is the B lever; flatten
  walks should print **closer to fill** than 1–20¢).
- Walk rate (73/411 = **17.8%**). Held-to-zero count should fall.
- `hedge_attempt` `reason=flatten_walk` vs `bid_le_dump` vs `persist_live_bid`.
- Sold-then-won count vs the tape’s **8** (winner-dump risk).
- Dublin $/h and drawdown (trough was **−$14** from ~$121).
- `cycle_error` stays **0**. `invalid amounts` stays **0**.

If the operator later pastes a persist/dump knob, compare those same
lines to this overlay — not to pathlog’s 96% WR.

---

## 5. Explicit non-recommendations

- **Do not** size toward $1–2/h on +$9 in four days.
- **Do not** paste last-45 + $25 (empty / −EV).
- **Do not** add a vol/momentum buy skip.
- **Do not** add a profit-take sell.
- **Do not** start `polybuybot` / `polybuybothourly` / `polymintbot`.
- Example JSON `strategy_buy5m.example.json` still describes last-45 +
  $25 for `--sweep` **entry**. Hedge knobs in that file now match B+C
  (persist 1s / dump 40 / flatten). Live JSON paste is in `CURRENT.md`.

---

## 6. VM ~1s tape (31 Aug ~19:53Z) — join was empty for the wrong reason

Operator coverage:

| | |
|---|---|
| 5m JSONL on disk | **3137** files, **1.92 MB** |
| Oldest / newest | 17 Aug 19:40Z → 31 Aug 19:45Z |
| Overlay clocks (27 Aug 17:26Z → 31 Aug ~14Z) | **726** vs ~**1106** expected |
| Named-loss JSONL copied | **31** present / **25** missing |
| `loss_ticks.tgz` | **7.4K** — too small for 31 full 5m 1Hz books |
| `journal_fills` / autopsies | **0** (parser bug, not an empty tape) |

**Prune already ate the left third of the overlay** (~380 clocks, including
25 of 56 named-loss slugs). Export remaining overlay ticks before the
next 400 MB / 14d cut. Mean bytes/file across all 3137 is ~**600 B**.
That is header + a handful of ticks, not 300 × 1s lines. The autopsy
prints `mean_ticks` / `p50` / `median_dt` before anyone trusts a 5s
persist run on this tape.

**Why the persist CF printed zeros:** `buy_fill` journal lines have
`token_id`, `avg_price`, `filled` — **no `slug`**. `buy_success` has
`condition_id`. Research JSONL (`underlying_research_buy5m.jsonl`) has
`slug` / `start_ts` and **`logged_at`** (unix), not `ts`. A snippet that
required `event==buy_fill` **and** `slug` joined nothing. Tick coverage
and the 31-file copy were fine.

`check_last120_tick_autopsy.py` joins through pathlog `open` headers
(`up`/`dn` token → slug, `cid` → slug, `start_ts` → `btc-updown-5m-{start}`)
and walks live dump/persist/fade (`evaluate_held_bag`, dump **32¢** any
bag, persist **50/52**, recovery **53¢**, fade on). Pathlog has no
last-trade and no Chainlink — oracle is **not** replayed. GUI proxy is
tight mid; **`min_bid_edge` 5¢ makes a 51¢ vs 49¢ book `ambiguous`**, so
the report scores persist with GUI on and book-only. That is a live
gate, not a paper invention: persist 50/52 coin-flips fail consensus
until the other mid is ~**56¢+**, which is why named persist 32–53 was
only **5** and dump-at-32 / held-to-zero dominate.

On the VM (does **not** checkout the live tree). ``cd`` into the repo
so ``buy/`` imports; ``--repo`` is also inserted on ``sys.path`` when
the file lives in ``/tmp``:

```bash
cd ~/poly-money-maker
git fetch origin cursor/last120-loss-catalog-f488
git show origin/cursor/last120-loss-catalog-f488:check_last120_tick_autopsy.py \
    > /tmp/check_last120_tick_autopsy.py
python3 /tmp/check_last120_tick_autopsy.py --repo "$PWD" \
    --out /tmp/last120-research --since 2026-08-27T17:26:00 \
    | tee /tmp/last120-research/autopsy.txt
```

If an older `/tmp` copy raises `No module named 'buy'`:

```bash
cd ~/poly-money-maker
PYTHONPATH="$PWD" python3 /tmp/check_last120_tick_autopsy.py --repo "$PWD" \
    --out /tmp/last120-research --since 2026-08-27T17:26:00 \
    | tee /tmp/last120-research/autopsy.txt
```

**Binance 1s (VM `reversal_1s.txt`, T-90, overlay):** 1139 scored clocks —
every 5m window, **not** only 75–90 books. Flip **15.4%**. |$20–40| is
**12.4%** flips (the 27 Aug 25% figure is stale). Gate |dist|≥$25 keeps
60% of *all* clocks at 6.5% flip / paper +$171. That keep% is an upper
bound on fill loss: live already takes 36% of clocks (75–90). Against-mom
skip is only +$36 paper on this all-window set. **Do not paste `$25` or
a vol skip from this table.** Score edge on joined 75–90 fills after the
autopsy.

Paste `autopsy.txt`. Do **not** restart 5m. Do **not** paste live JSON.

### Autopsy paste (31 Aug ~20:23Z) — first full report

This is the first `autopsy.txt`. Earlier VM pastes were tick coverage,
empty join, `reversal_1s.txt`, and `No module named 'buy'`.

| | |
|---|---|
| Join | **436** unique fills (research). **283** have a tick file. **153** pruned. |
| Journal | `buy_fill` **353**, `buy_success`/`buy_ghost_fill` **436**, `buy_attempt` **721**. Keys match the join fix (`token_id`, no `slug`). |
| Tick density | all_5m **mean 1.39** ticks (`p50=1`, `p90=2`). Overlay **mean 1.0**. Named-loss **1.0**. **Not a 1s book.** |
| Named-loss | 31 present / 25 missing. `printed_<=50=2` and those were already `<=32`. `book_run>=1s=0`. |
| Persist 0/1/2/5 | **Identical** on losers: 4 dumps @ ~4.5¢, 21 redeem_loss, 0 persist. One extra persist at persist_s=0 is an unresolved fill, not a loser save. |
| Overlay first-touch | 15 / 770 clocks still had a 75–90 print on that **single** tick; 14 redeem_win, 0 losers. Restable subset, not a path. |

**Persist 0s vs 5s is not identified on this tape.** The 4 dumps are
markets where the only recorded tick already was a 4–20¢ bid. 21/25
resolved losers never printed 50 on that tick (`min_bid=None` — the
line is the entry/ghost 90¢ book, or the walk is `ts > fill` so the
lone tick does not count). Recap catalog still stands: held-to-zero
and dump-at-1–20¢. Ghost rows used ask **90¢** as avg (FAK cap, not
fill VWAP); the checker now prefers research `buy_fill` price.

**Why one tick:** `pathlog.py` Gamma-resolved *every* unresolved JSONL
every poll (~3000 files). Gamma never marks most old 5m books 0.99, so
the queue never drains and one cycle lasts minutes. Sampling then hits
each 5m market ~once. Fix: resolve only books that closed in the last
**6h**, **8** Gamma lookups per poll, 60s cooldown on a miss. After
that lands, `sudo systemctl restart polypathlog` (recorder only, no
orders). Do **not** restart `polybuybot5m`. Confirm with
`mean_ticks` on a new 5m file (`wc -l` should be hundreds, not 2).

---

## 7. VM reversal paste (31 Aug ~20:53Z) — not a fill winner-split

Operator ran:

```bash
.venv/bin/python check_reversal_features.py --csv exports/trades.csv --restart-utc 2026-08-27T17:26:00
.venv/bin/python check_participation.py --hours 96 --csv exports/trades.csv
```

**SESSION TAPE n=0** is a join miss, not zero fills. The checker required
`series_of(marketName)=="5m"` via a `Month D, H:MM AM-H:MM AM` title.
`check_fetch_trades` writes `slug=btc-updown-5m-{start}` and often a
generic title (`BTC Up or Down 5m`). Participation still loaded **1905**
buys (no title regex). Historical tables still ran because they do not
use the CSV.

**Those tables are every 5m clock, not the 411 last-120 buys.**
`--hours` defaulted to **48** (576 windows / 566 scored), not the ~94h
overlay. Flip = Binance side of PTB at **T-90s** disagrees with close.
Paper fill is implied from `|dist|` (session-calibrated ~85¢), not live
VWAP. Hedge is not replayed except as a $0 / $1 salvage column.

| Read this | Not this |
|---|---|
| `live_late_7590` 380 hits, **17.1% flip / 82.9% WR**, mean_fill 0.87, **+$0.50/h** with $1 salvage / **−$0.85/h** with $0 | `last45_e20` +$1.98/h — same last-45+$20 paper that already went empty/−EV live |
| GATE `|dist|≥25` keep **60% of ALL clocks** (incl. 50/50). skip% is an **upper bound**; live take is **36% of 75–90** | “skip 40% of fills and keep +$85” |
| MID $10–40 against-mom **17.6%** vs with **14.5%** | a momentum skip |
| vol30 flip **~14–16%** flat | a vol skip |
| Script `RECOMMENDATION` last-45+$25 / persist 5 / dump 32 | a live paste |

`live_late_7590` matches the knife-edge tape (WR ~82–83%, flip ~17%).
That is last-120 75–90 on implied `|dist|` fills, not proof that $25
or last-45 helps. **Do not paste last-45 + $25. Do not size up.**

Participation **96h** (27 Aug 20:53Z → 31 Aug 20:53Z; overlay started
17:26Z so the first ~3.5h are outside this cut):

| | |
|---|---|
| 5m clocks | **1151** (96h × 12) |
| “bought” | **585 / 1151 = 50.8%** — sources **`bot` 536 + `bot+pnl` 49**. **No `csv`.** |
| CSV | 1905 rows loaded, **0 matched** to 5m questions (same title/slug miss as SESSION TAPE) |
| above_ceiling_only | **409** — ask stayed ≥90¢ (the 99/1 pile) |
| never_reached_trigger | **77** — never printed 75¢ |
| saw_in_band | **50** — 1m CLOB wick; samples are mostly 99/1 (`up_max=0.135 dn_max=0.995`) |
| gapped_through_band | **30** |
| named skips on misses | ambiguous 21, incomplete_book 19, side 3, rest_confirm 2, consensus 1, edge 1 |

**585 is not 411 wallet fills.** `load_bot_buys` counts `buy_attempt` +
`buy_fill` + `buy_ghost_fill`. Empty FAKs inflate “bought.” Wallet take
on the overlay was **411/1129 = 36%**. The 409 above-ceiling misses are
the restable-subset story: paper 96% WR only sees books that actually
printed 75–90.

A few `saw_in_band` look restable (dn 0.820 / 0.765, up 0.865). Treat as
1m wick unless the journal shows `buy_skip_rest_confirm` on that slug.
Do **not** reopen last-45 + $25 from this table.

Join + fill×TTM split + slug match + a join diagnostic are in this PR.
Re-run after `git pull`. Historical `--hours 96` if the overlay span is
wanted. Participation: `--bot 5m` (15m/hourly are stopped).

---

## 8. VM SESSION TAPE n=437 (31 Aug ~21:30Z) — −$1139 is not live P&L

Operator re-fetched `exports/trades.csv` (stale file ended 1787357611,
before the 27 Aug 17:26Z restart) then:

```bash
.venv/bin/python check_fetch_trades.py --out exports/trades.csv
.venv/bin/python check_reversal_features.py --csv exports/trades.csv \
  --restart-utc 2026-08-27T17:26:00 --hours 96 --out /tmp/reversal_join.txt
```

Fetch: **4922** rows, newest **2026-08-31 20:39:00Z**, **491**
post-restart. Titles in this pull were range-shaped
(`August 27, 1:30PM-1:35PM ET`), so join worked without the slug
fallback. `git pull` still aborted on local `check_path_backtest.py`.
`--skip-miss-history` is on origin, not the VM checkout.

**Do not treat `session_pnl=−1139.58` / mean −$2.61 as live P&L.**
`GET /trades` is CLOB Buy/Sell only. `session_pnl` only credited
`action == "Redeem"`. 437 markets: **redeem=0**, **hedge=54**,
**other=383**. Every unresolved winner looked like `open` / exit $0 /
pnl = −spent (~−$2.70). Smoking gun: SESSION $5 buckets **50–55** and
**60–65** show WR **100%** and negative `paper_pnl`. Live overlay from
the first tape is still ~**+$9** / **+$0.10–$0.12/h**.

What is usable from this paste (Binance at fill, not wallet cash):

| |dist| at fill | n | flip | WR |
|---|---|---|---|
| 0–5 | 53 | 47.2% | 52.8% |
| 5–10 | 65 | 33.8% | 66.2% |
| 10–15 | 57 | 19.3% | 80.7% |
| 15–20 | 60 | 16.7% | 83.3% |
| 20–25 | 37 | 13.5% | 86.5% |
| 25–30 | 41 | 19.5% | 80.5% |
| 30–35 | 24 | 8.3% | 91.7% |

Mean fill **0.801**, span **99.17h**, **4.1** fills/h. At 80¢ the
no-hedge max flip is **20%**. 0–10 is not eatable; 10–20 is knife-edge;
20+ mostly is. GATE `ev_nohedge` (flip × fill, not the fake wallet
column): all **−0.09**; ≥10 **+0.13**; ≥20 **+0.18**; ≥30 **+0.25**.

**0–10 is the entry leak.** 118 fills, **47 flips** (~40% flip). That
is about **half** the scored session flips (47/83 in the printed
buckets). Redeem-only at mean fill 80¢ is about **−$0.85**/fill in
0–5 and **−$0.43** in 5–10 ≈ **−$73** on the pile. last-45+$25 kept
**6/405** and is still no. last-120+`$10` keeps the window and drops
only this pile. B+C still handles 10+ losers that fade.

SESSION replay (keep if TTM ≤ window and |dist| ≥ edge — poisoned by
−$2.70 opens until paper-credit):

| name | keep | keep/h | note |
|---|---|---|---|
| all_fills | 405 | 4.1 | almost all last-120 |
| last45_e0 | 27 | 0.3 | ~6.7% of fills in last 45s |
| last45_e25 | 6 | 0.1 | empty |

Historical COMBOS `last45_e20` +$1.86/h is implied-|dist| paper on
**all 5m clocks**, not this tape. VM script on `e962a75` still printed
last-45+$25 as RECOMMENDATION. **Do not paste it. Do not size up.**

Hedges with BTC features: **n=48**, mean_|dist| **20.7**, against
**44%**. Some recovered (+3.11, +2.86); most still lost. Participation
**585/1151 “bought”** remains bot-log (attempt-inflated); wallet take
on the overlay was **411/1129 = 36%**. 15m/hourly 0 is correct.

Paper-credit landed on `main` as #135. Operator ran the re-run below
~22:08Z after stash + pull to `7cb783c`. Results in §9.

```bash
cd ~/poly-money-maker
git stash push -m "vm local check_path_backtest" -- check_path_backtest.py
git pull
.venv/bin/python check_reversal_features.py --csv exports/trades.csv \
  --restart-utc 2026-08-27T17:26:00 --hours 96 --out /tmp/reversal_join.txt
```

---

## 9. Paper-credit tape (31 Aug ~22:08Z) — −$163 is still not live P&L

Same CSV as §8 (through 20:39Z). VM pulled `7cb783c` after stashing
local `check_path_backtest.py`. Binance 1s: **345996** bars
`[1787867518..1788213508]`. awk printed SESSION TAPE twice because
the script writes stdout **and** `--out`.

```
n=437 redeem=0 paper_win=288 paper_loss=71 hedge=54 other=24
session_pnl=-163.16 mean=-0.37
span 99.17h  mean fill 0.801  4.1 fills/h
```

Paper-credit **worked**. Winners print **+$0.30 to +$1.21** (walks up
to **+$5.74**), not −$2.70. `redeem` is still 0 (`/trades` has no
Redeem). Real Redeem rows still take precedence when present.

**Do not treat −$163 / −$0.37/bag as live P&L.** Mix:

- 24 `open` before Binance 1s (1787851800–1787867400 plus a few later
  holes) still −spend ≈ **−$65**
- GATE featured 403 (`|dist|` present): `paper_pnl_kept=−85.12` /
  **−$0.86/h**
- Binance close vs PTB can disagree with Polymarket resolution

Live overlay recap from the first tape is still ~**+$9** /
**+$0.10–$0.12/h**. Do not replace that with this banner.

### Fill × TTM (win = paper_win)

This is the winner-split asked for. 288 / (288+71) = **80.2%** paper WR
excluding hedges (matches live ~80–82%). Including hedges:
288 / 413 = **69.7%**.

| fill ¢ | n | WR | mean_pnl | hedges |
|---|---:|---:|---:|---:|
| 0–0.7 | 47 | 34.0% | −0.68 | 15 |
| 0.7–0.75 | 36 | 61.1% | −0.32 | 5 |
| 0.75–0.8 | 58 | 60.3% | −0.36 | 9 |
| 0.8–0.85 | 148 | 70.3% | −0.37 | 9 |
| 0.85–0.9 | 119 | 74.8% | −0.29 | 13 |
| 0.9–1.01 | 29 | 75.9% | −0.36 | 3 |

| TTM s | n | WR | mean_pnl | hedges |
|---|---:|---:|---:|---:|
| 0–30 | 4 | 25.0% | −2.03 | 0 |
| 30–60 | 56 | 53.6% | −0.83 | 4 |
| 60–90 | 148 | 70.9% | −0.18 | 14 |
| 90–120 | 229 | 66.4% | −0.36 | 36 |

**&lt;70¢ walks are the bad pile (34% WR).** 75–90 is 60–76% WR and
slightly negative mean_pnl (hedges + paper_loss in-band). Last-45-ish
(TTM 30–60) is worse than 60–120. n=4 in 0–30: ignore.

### |dist| at fill (paper_pnl now paper-credit)

Flip rates match §8. Dollars are no longer −$2.70-poisoned.

| |dist| | n | flip | WR | paper_pnl |
|---|---:|---:|---:|---:|
| 0–5 | 53 | 47.2% | 52.8% | −38.13 |
| 5–10 | 65 | 33.8% | 66.2% | −16.70 |
| 10–15 | 57 | 19.3% | 80.7% | −7.63 |
| 15–20 | 59 | 16.9% | 83.1% | −19.64 |
| 20–25 | 37 | 13.5% | 86.5% | +2.01 |
| 25–30 | 40 | 17.5% | 82.5% | −7.78 |
| 30–35 | 24 | 8.3% | 91.7% | −0.48 |

0–10 is not eatable at an 80¢ fill (no-hedge cap **20%**). 10–20 is
knife-edge. 20+ mostly is on flip, but **paper P&amp;L stays flat /
negative until ≥$30** and fill rate collapses.

### GATE keep |dist|≥X (featured 403)

| min | keep | keep/h | flip_kept | wr_kept | paper_pnl_kept |
|---|---:|---:|---:|---:|---:|
| 0 | 403 | 4.1 | 22.6% | 77.4% | −85.12 |
| 10 | 285 | 2.9 | 15.4% | 84.6% | −30.29 |
| 20 | 169 | 1.7 | 13.6% | 86.4% | −3.03 |
| 25 | 132 | 1.3 | 13.6% | 86.4% | −5.03 |
| 30 | 92 | 0.9 | 12.0% | 88.0% | +2.75 |
| 40 | 55 | 0.6 | 12.7% | 87.3% | +9.23 |

**Do not add GATE `|dist|≥25` / last-45+$25 from this table.** ≥$25
cuts the tape to 1.3 fills/h and is still −$5 paper. last-120+`$10`
is the 0–10 cut (separate from this ≥25 row). Historical COMBOS
`last45_e20` +$1.85/h is implied-|dist| paper on **all 5m clocks**,
not these fills.

### SESSION replay (un-poisoned for paper_win / paper_loss)

Keep actual wallet/paper P&amp;L if that fill’s TTM ≤ window and
|BTC−PTB| ≥ edge. Not a last-45 simulator — early fills skip.

| name | keep | skip | keep/h | pnl_kept |
|---|---:|---:|---:|---:|
| all_fills | 403 | 0 | 4.1 | −85.12 |
| last45_e25 | 6 | 397 | 0.1 | −3.46 |
| last45_e20 | 7 | 396 | 0.1 | −6.16 |
| last45_e0 | 27 | 376 | 0.3 | −23.73 |
| late120_e25 | 132 | 271 | 1.3 | −5.03 |
| late120_e0 | 403 | 0 | 4.1 | −85.12 |

**Confirms last-45+$25 empty / −EV on this tape. Do not paste.**

### Hedges (pre-B+C)

Tape **54** sells / **47** with BTC: mean_|dist| **20.6**, against
**45%**, mean_mom30 **−5.6**. Recovery still poor (many −$1.5 to
−$2.8; a few +3.11 / +2.86). This tape is dump **32** / persist **5s**.
B+C (persist 1s / dump 40 / flatten &lt;75) is not in these rows
(tape is pre-paste). Operator pasted B+C **31 Aug ~22:25Z**.

### What this does / does not change

Stay **last-120 / 75–90 / $2.50**. **`$10` is live** (31 Aug ~22:58Z).
Next is the last-30s hedge ladder after merge + 5m restart. Early /
≥95 / late_90 **off**. **Do not size up.** **Do not add a vol or
against-momentum skip.** Measure B+C + `$10` + ladder on the next
live recap, not this paper banner. Optional later: exclude `open` from banner `session_pnl` so
the 24 coverage-gap bags do not drag −$65; `/activity?type=REDEEM` if
we want real Redeem rows. VM stash `vm local check_path_backtest` is
still on the box.
