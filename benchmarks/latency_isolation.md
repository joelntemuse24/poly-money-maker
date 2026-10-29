# Latency isolation: measured results

All numbers below were produced **on this cloud VM (4 vCPU, Linux 6.12, CPython
3.12.3)**, not on the Zurich 2-vCPU live host. They are relative comparisons
between configurations measured back-to-back on the same machine, which is what
the flag decisions need; they are not absolute predictions of live latency.

Every benchmark is reproducible and committed:

| tool | measures |
| --- | --- |
| `tools/bench_gc_pause.py` | collector pause distribution (`gc.callbacks`) at two heap sizes |
| `tools/bench_thread_switch.py` | one-way GIL handoff latency + 2-thread throughput per switch interval |
| `tools/bench_tick_lock.py` | how long `Runner._tick_lock` is held, per scenario and flag |
| `tools/bench_feed_process.py` | trigger -> "would send the POST", in-process vs child process |

Reproduce:

```bash
.venv/bin/python tools/bench_gc_pause.py     --ticks 300 --heap 400000
.venv/bin/python tools/bench_thread_switch.py --spins 300
.venv/bin/python tools/bench_tick_lock.py    --ticks 100 --repeats 5
.venv/bin/python tools/bench_feed_process.py --triggers 100 [--load] [--switch S]
```

The 5m sniper code path (`Strategy._sniper_check` -> `_sniper_execute` ->
`io.buy`) is the same in every row. Nothing here touches a real websocket or the
CLOB: the network, the presign cache and `fast_post_order` are identical in both
modes and are excluded on purpose, so the tables isolate the thing each flag
changes.

---

## 1. `gc_freeze_hotpath`

Two heap sizes, because `gc.freeze()` moves the *existing* object graph out of
the collector's tracking set, so its effect scales with how much the process
already holds. `--heap 400000` stands in for the live bot's startup objects.

Ticks are a repeated 5m cycle (entry, dump, journal lines), 300 cycles, 594
orders per run. Pauses are the collector's own duration inside `gc.callbacks`.

`--heap 0` (tiny heap, only the harness's own objects):

| mode | collections | gen0 | gen1 | gen2 | p50 µs | p90 µs | p99 µs | max µs |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| off (today) | 28 | 25 | 3 | 0 | 26.2 | 135.0 | 515.1 | 573.4 |
| `gc_freeze_hotpath` | 1 | 1 | 0 | 0 | 25.2 | 25.2 | 25.2 | 25.2 |
| freeze + thresholds + boundary | 1 | 0 | 0 | 1 | 928.9 | 928.9 | 928.9 | 928.9 |

`--heap 400000` (realistic long-lived heap):

| mode | collections | gen0 | gen1 | gen2 | p50 µs | p90 µs | p99 µs | max µs |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| off (today) | 1168 | 1064 | 97 | **7** | 14.0 | 25.8 | 262.0 | **40 027** |
| `gc_freeze_hotpath` | 1 | 1 | 0 | 0 | 27.9 | 27.9 | 27.9 | 27.9 |
| freeze + thresholds + boundary | 1 | 0 | 0 | 1 | 1 085.9 | 1 085.9 | 1 085.9 | 1 085.9 |

Three re-runs of the same configuration gave max pause 40.0 / 40.7 / 43.1 ms for
`off` and 28.4 / 25.8 / 26.0 µs for `gc_freeze_hotpath`; the effect is stable.

**Reading it:**

- Without the flag, this process took **7 gen2 collections in the run, the worst
  one a 40 ms stop-the-world pause**. A 40 ms pause in the window between
  `recv()` and the FAK POST is a missed jump - that is the risk being bought
  down, and it is much larger than the ~1 ms recv->decision the path currently
  costs.
- `gc_freeze_hotpath` alone reduces collector activity to a single gen0 pass and
  caps the worst pause at **~28 µs**, a ~1400x reduction in the tail. p50 is
  unchanged (14 -> 28 µs, i.e. tens of microseconds), which is the honest trade:
  the same total collection work, just no long generations to walk.
- The threshold knobs (`gc_hotpath_gen0_threshold` / `_gen2_threshold`) plus
  `gc_hotpath_boundary_collect` **do not help and can hurt**: with the heap
  frozen there is almost nothing left to collect automatically, so all the
  knobs buy is one deliberate 1.09 ms collection per run. 1.09 ms at a safe point
  is cheaper than a 40 ms pause, but it is 39x the 28 µs of plain freeze, and it
  only happens every 5 minutes anyway. **Recommendation: turn on
  `gc_freeze_hotpath` alone; leave the threshold and boundary knobs at 0/false.**

Honest caveat: this harness allocates a synthetic heap. The live bot's real
object count and allocation rate are unknown to me, so the absolute pause values
will differ. What transfers is the shape: the flag removes automatic gen2 passes
and bounds the tail, and `gc_hotpath_stats` will print the live distribution
every 5 minutes so the owner can verify it on the VM.

---

## 2. `thread_switch_interval_s`

One-way handoff: a pure-Python publisher thread stamps the clock and sets an
event, then keeps running; a blocked "feed" thread wakes and measures the delta.
This is exactly the live shape (feed thread blocked in `recv()`, other threads
holding the GIL). 300 handoffs x 3 repeats; p50/p90/p99 are the median of the
repeats.

| interval | p50 µs | p90 µs | p99 µs | max µs | 2-thread iters/s | vs default |
| --- | --- | --- | --- | --- | --- | --- |
| 5 ms (CPython default) | 283.9 | 419.7 | 481.7 | 539.1 | 112 363 869 | 0.0 % |
| 5 ms (explicit) | 292.7 | 424.3 | 502.0 | 546.7 | 112 925 920 | +0.5 % |
| 1 ms | 68.3 | 95.6 | 110.0 | 121.1 | 112 782 744 | +0.4 % |
| 0.5 ms | 30.5 | 78.1 | 104.9 | 115.4 | 110 743 740 | -1.4 % |

**Reading it:**

- A thread that is handed work while another thread holds the GIL waits on
  average about **half a switch interval** before it even starts. p50 drops
  283.9 -> 68.3 -> 30.5 µs as the interval goes 5 -> 1 -> 0.5 ms. That is real
  and it is on the sniper path: the feed thread's `recv()` return, and the
  parent-side wakeups in item 4, both pay it.
- The p99 gain is smaller (481.7 -> 110.0 -> 104.9 µs): 0.5 ms is not twice as
  good as 1 ms at the tail, because the remaining tail is scheduler noise, not
  the interval.
- **CPU cost is not measurable here** (-1.4 % to +0.5 %, inside run-to-run
  noise) for two CPU-bound threads. The classic "shorter interval = more context
  switches = less throughput" effect is not visible at 8 feeds + a poll loop on
  2 vCPU in this benchmark. I would not claim there is *zero* cost on the VM;
  I only claim I could not measure one. The flag is trivially reversible.
- **Recommendation: 0.001.** It captures most of the p50 win (283.9 -> 68.3 µs,
  -76 %) at half the switching rate of 0.0005, and its p99 (110.0 µs) is the
  same as 0.0005's (104.9 µs). 0.0005 is defensible if the VM shows no CPU
  regression, but 1 ms is the better default to flip first.

---

## 3. `tick_lock_short`

`Runner._tick_lock` is what the feed thread waits for at `on_binance_tick`
(50 ms timeout). Hold durations are measured by wrapping the RLock itself.

Two scenarios: `cycle` = repeated entry/dump/journal cycles (the routine load);
`sniper` = the event-driven trigger firing an order.

| scenario | mode | holds | p50 µs | p90 µs | p99 µs | max µs | total held | in journal sink |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| cycle | default | 1400 | 12.8 | 86.8 | 195.5 | 704.1 | 46.83 ms | 3.52 ms |
| cycle | `tick_lock_short` | 1400 | 13.6 | 87.7 | **168.1** | **271.2** | 43.52 ms | 3.29 ms |
| sniper | default | 40 | 8.5 | 19.2 | 24.7 | 26.1 | 0.39 ms | 0.35 ms |
| sniper | `tick_lock_short` | 40 | 8.7 | 20.4 | 23.3 | 24.3 | 0.40 ms | 0.35 ms |

**Reading it, honestly:**

- p50 and p90 are **unchanged**. The lock is held for roughly the same time
  either way; what the flag changes is *what is inside that time*.
- The journal sink (json encoding + the write, i.e. `log_event`) accounts for
  **3.29-3.52 ms of the ~44 ms total hold** in the cycle scenario. With the flag
  on that work happens after the lock is released. The visible effect is on the
  tail: p99 195.5 -> 168.1 µs (`-14 %`) and max 704.1 -> 271.2 µs (`-61 %`).
- The `sniper` scenario barely moves, because that path already released the
  lock before `_sniper_execute` POSTs. The flag does not lengthen the sniper's
  critical section either (p50 8.5 -> 8.7 µs, within noise).
- What this is *not*: a p50 improvement. If the trigger arrives while the tick
  thread is in the middle of `Strategy.tick`, it still waits. The gain is that
  a tick which emits several journal lines no longer holds the lock for the
  whole encode-and-write, so the **worst** waits shrink by roughly 2-4x.
- **Recommendation: turn it on.** It is behaviour-neutral (see the replay tests
  below), costs nothing measurable, and removes the largest single contributor
  to long lock holds.

Behaviour parity is covered by `tests/test_tick_lock_short.py`: the same
scripted tick stream is replayed in both modes and the **decisions, order calls,
state snapshots and journal order are asserted identical**, including one
feed-triggered sniper order:

```
ReplayEquivalenceTest::test_decisions_state_and_journal_are_identical
ReplayEquivalenceTest::test_saves_keep_state_at_mutation_time
SniperPathEquivalenceTest::test_feed_triggered_buy_and_latency_line_match
SniperPathEquivalenceTest::test_state_transition_marks_the_sniper_attempt_once
```

`save()` is deliberately **not** deferred: a state snapshot must be taken at the
moment of the mutation, or a crash between the tick and the flush could lose an
attempt marker. Only `log` and `alert` move.

---

## 4. `feed_process`

Trigger -> "the instant our code would start the POST". `io.buy` is stubbed to
record `perf_counter_ns`, so the number excludes the network (identical in both
modes) and isolates the detection path.

The `feed_process` row includes the benchmark's own shared-ring write, the
trigger write and the `eventfd` write, i.e. what the child does after accepting a
tick. It does **not** include a websocket `recv()`.

100 triggers, no other load:

| mode | fired | p50 µs | p90 µs | p99 µs | max µs |
| --- | --- | --- | --- | --- | --- |
| in-process | 100 | 30.0 | 33.0 | 56.5 | 120.4 |
| `feed_process` | 100 | **36.4** | 44.8 | **227.5** | **330.4** |

With a CPU-busy main process and a live-cost journal (the real `log_event`
encoding):

| mode | switch | fired | p50 µs | p90 µs | p99 µs | max µs |
| --- | --- | --- | --- | --- | --- | --- |
| in-process | 5 ms | 100 | **31.7** | 42.0 | 86.5 | 145.7 |
| `feed_process` | 5 ms | 100 | 5 534.8 | 16 193.3 | 32 534.2 | 32 764.8 |
| in-process | 0.5 ms | 100 | **32.7** | 601.1 | 608.6 | 619.0 |
| `feed_process` | 0.5 ms | 100 | 648.8 | 1 399.4 | 1 693.6 | 1 697.6 |

**Reading it plainly: the separate process is slower, and by a lot under load.**

- Unloaded it costs a flat **+6.4 µs at p50** and roughly 4x the p99. That is
  the shared-memory writes (two rings: exchange time and receive time), the
  trigger record and the `eventfd` round trip. There is no scenario in which a
  local function call loses to an IPC hop.
- Under load it is much worse: **p50 31.7 µs in-process vs 5 534.8 µs split at
  the default 5 ms switch interval**. The reason is exact and worth writing down:
  the `eventfd` wakes the parent's reader thread, but that thread cannot run
  until it is *given the GIL*, which with a CPU-busy main process is bounded by
  the switch interval. 5.4 ms is one switch interval. This was verified in
  isolation: a blocking `eventfd` read delivered in 5 111 µs at a 5 ms interval
  and 565 µs at 0.5 ms.
- Shortening the interval to 0.5 ms does not rescue it (648.8 µs vs 32.7 µs).

So the mechanism I expected to help - getting the detector away from the main
process's GIL - is exactly what is defeated by GIL handoff at the receiving end.
The child detects the jump promptly; the parent cannot act on it promptly. A
future attempt would need the *sender* to be the process that does the fast work
(or a non-Python reader thread via `asyncio`/`selectors` on a C-level wakeup),
not just the detector.

**Recommendation: leave `feed_process` off.** The implementation is complete -
health parity, reconnect parity, the same per-feed recv stamps and
sniper_order_latency fields, clean shutdown, fallback if the child dies - and
`tests/test_feed_process.py` covers all of it, but it is slower in every
configuration measured here and its tail is materially worse. The flag defaults
to false, and this is why.

### What *is* implemented for `feed_process` (parity checklist)

| requirement | where |
| --- | --- |
| all feed websockets in the child | `FeedWorker.run` builds one `BinanceTrades` with every extra URL |
| jump detector in the child | `detect_trigger` (same `best_jump_in_window` scan), `FeedWorker._maybe_trigger` |
| lowest-latency IPC | shared-memory tick ring + trigger slots + blocking `eventfd` wakeup |
| same per-feed recv timestamps | the ring carries both axes (exchange time and local receive time); `recv_wall_ms` travels in the trigger record, so `sniper_order_latency.trigger_recv_ts`, `recv_to_decision_ms` and `feed_recv` stay comparable with the in-process path |
| `sniper_order_latency` fields | `trigger_to_jump_info` reproduces the in-process dict; asserted equal in `TriggerRecordTest` |
| health parity | 1 s heartbeat in the shared block, `feed_process_status_s` poll, `feed_process_fallback` on death or a stale beat |
| reconnect parity | same `BinanceTrades` class and backoff ran in the child |
| clean shutdown | `H_STOP` flag + `eventfd` poke + `wait`/`terminate`/`kill` escalation; asserted no orphan pid and no leaked segment |
| fallback to in-process | `Runner._feedproc_fallback` restarts in-process feeds and restores the local grid |

The child is a **plain subprocess** (`python -m buy.feed_worker`), not
`multiprocessing`: at start-up the bot has ~20 threads and no `mintbot` module
state is safe to fork. The config (no secrets) goes over stdin; the segment name
and `eventfd` number go on argv.

---

## Recommendation summary

| flag | value | why |
| --- | --- | --- |
| `gc_freeze_hotpath` | **true** | removes 40 ms gen2 stop-the-world pauses; tail 40 ms -> 28 µs |
| `gc_hotpath_gen0_threshold` | **0** (leave) | no benefit once frozen; measured worse (1.09 ms deliberate collection) |
| `gc_hotpath_gen2_threshold` | **0** (leave) | same |
| `gc_hotpath_boundary_collect` | **false** | same; the 1 ms safe-point collection is 39x the plain-freeze pause |
| `thread_switch_interval_s` | **0.001** | p50 GIL handoff 283.9 -> 68.3 µs; no measurable CPU cost |
| `tick_lock_short` | **true** | lock-hold p99 -14 %, max -61 %; behaviour identical under replay |
| `feed_process` | **false** | slower in both modes (+4.8 µs p50 idle, 5.4 ms p50 under load); see above |

Order to flip, safest first: `gc_freeze_hotpath`, then `thread_switch_interval_s`,
then `tick_lock_short`. Each is independent and each can be reverted alone.

### Deploy notes

- **New dependencies: none.** `buy/hotpath.py` and `buy/feed_process.py` use only
  the standard library (`gc`, `sys`, `threading`, `ctypes`, `multiprocessing.shared_memory`,
  `os.eventfd`, `subprocess`). No change to `requirements.txt`.
- **Where the flags go:** the live, gitignored `strategy_mint.json`, e.g.

  ```json
  "gc_freeze_hotpath": true,
  "thread_switch_interval_s": 0.001,
  "tick_lock_short": true,
  "feed_process": false
  ```

  Keys absent from the live file take the code default (all off), so deploying
  the code changes nothing until these lines are added. `mintbot.DEFAULTS` now
  carries `gc_hotpath_gen0_threshold`, `gc_hotpath_gen2_threshold`,
  `gc_hotpath_boundary_collect`, `gc_hotpath_boundary_offset_s`,
  `thread_switch_interval_s`, `tick_lock_short`, `feed_process`,
  `feed_process_trigger_ttl_ms`, `feed_process_status_s`, and
  `validate_strategy` accepts/sanity-checks each.
- **Restart required:** `thread_switch_interval_s` and `gc_freeze_hotpath` are
  applied at start-up / warmup, so the unit must be restarted (which the operator
  does deliberately; the deploy workflow does not). `tick_lock_short` and
  `feed_process` are read per tick / at start respectively.
- **What to look for after the restart:**
  - `gc_hotpath_armed` once, with `gc_frozen` > 0 - if it is 0 the freeze did
    nothing and the flag is not helping.
  - `gc_hotpath_stats` every 5 minutes: `gc_pause_max_us` should be in the tens
    of µs and `gc_gen2` should stay 0 after the first minutes.
  - `thread_switch_interval` once, with `after_s` 0.001.
  - In `sniper_order_latency`, `recv_to_decision_ms` and `decision_to_send_ms`
    should not regress; that is the end-to-end check that none of this hurt.
  - `feed_process_*` events should be absent while the flag is false.
- **Rollback:** set the flag back and restart. No state, order or journal format
  changed.

### Not measured / cannot be measured here

- Live end-to-end `recv -> POST` on the Zurich VM: no access, and the network
  path is excluded from every table deliberately.
- The real GC object count and allocation rate of the live bot; the `heap`
  sweep brackets it (0 and 400k containers), and `gc_hotpath_stats` will report
  the real distribution on the VM.
- True CPU cost of a shorter switch interval on 2 vCPU under the live 8-feed +
  2-loop load. The 4-vCPU benchmark showed none; 2 vCPU is the risk case.
- `feed_process` with real websockets in the child: the child's `recv()` cost is
  excluded; only the IPC path is measured. Even so, the IPC path alone is
  already slower, so the conclusion does not depend on it.
