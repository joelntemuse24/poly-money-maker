# Last-120 5m — first full overlay tape (27–31 Aug 2026)

Research note. **Not a live deploy.** Do not paste a new
`strategy_buy5m.json` overlay and do not restart unless the operator
asks after they buy a recommendation. Do not size up from **$2.50**.

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

## 3. Ranked changes (do not ship a live paste)

| Rank | Change | Tape $ | Winner-dump risk | Ship? |
|---|---|---|---|---|
| **B** | **Loser exits:** persist 1–2s and/or dump while bid is still 40–50 (hit the 32¢ bid, don’t wait for 1¢). | Illustrative: if true losers recovered **$1.00–$1.25** instead of **$0–$0.40**, loser drag shrinks ~$50–$110 on this tape → **+$0.20–$0.40/h at $2.50**. | 8 STW already. Faster persist adds more. Score on VM `--hedge-sweep` + the 411-fill journal before any JSON. | **Recommend research on the VM, then one knob if the sweep agrees.** Not a paste in this PR. |
| **C** | **Refuse or flatten walks** (`avg &lt; 75¢`). `toxic_fill` already arms; dump still waits for bid ≤32. | UI walk subset **−$6.61 / 32**; ~**+$0.16/h** if the 73 overlay walks match. | Low (walk WR ~56% vs 82%). | Second. Flatten (sell immediately) is closer to B than “skip the fill.” |
| **A** | **Tape: `hedge_fill` on `uncertain_resolved`.** Sell unmatched retry is already in 5m. | **$0.** Makes the next 48h measurable. | None. | **This PR, 5m-only.** |
| **D** | Tighter entry so take-rate falls toward paper (36% of clocks vs paper ~6–14%). | Extra clocks are walks + loose in-band. Live WR **82%** vs paper **96%**. Tightening toward restable 75–90 cuts losers *and* a lot of +$0.48 winners. | Low dump risk, high opportunity cost. | Not first. Do not re-open last-45+$25. |

`two_slice_missing` is **not a leak** (early slice is off).

**Walks vs toxic:** 73 `buy_fill_below_band`. Junk walks (`avg &lt; 65¢`)
arm `toxic_fill`; dump is still **bid ≤32**. That is why walk bags
show up as held-to-zero or 1–20¢ dumps, not as an immediate flatten.

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

## 4. Measure on the next 48h (if a knob is pasted later)

After a 5m restart that includes the tape fix (no strategy change
required for A):

- `hedge_fill` should rise toward wallet sells; `hedge_fail` then
  `uncertain_resolved` should no longer be the only sell story.
- Sell px median (want **≫ 19¢**; 40–50¢ is the +EV lever).
- Walk rate (73/411 = **17.8%**). Held-to-zero count.
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
  $25 for `--sweep`. Live JSON is the overlay in `CURRENT.md`.

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
