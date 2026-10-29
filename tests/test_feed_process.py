"""feed_process: shared-ring trigger IPC, health parity, fallback and shutdown."""
from __future__ import annotations

import ast
import json
import os
import sys
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

from buy import feed_process as fp
from buy.binance_trades import PriceGrid
from buy.feed_process import (
    H_HEARTBEAT_NS, H_MAGIC, H_PARENT_NS, H_STOP, H_TICKS, H_TRIG_W, MAGIC,
    HEADER_SLOTS, PARENT_STALE_S, SOURCE_NAMES,
    FeedProcessClient, FeedWorker, Layout, SharedBlock, SharedPriceGrid, TickRingReader,
    TickRingWriter, detect_trigger, read_trigger, source_slot, trigger_to_jump_info,
    write_trigger,
)
from buy.fivemin import DEFAULTS as FM_DEFAULTS

ROOT = Path(__file__).resolve().parents[1]
MINT = ROOT / "mintbot.py"


def _defaults() -> dict:
    tree = ast.parse(MINT.read_text(), filename=str(MINT))
    for node in tree.body:
        if isinstance(node, ast.Assign) and getattr(node.targets[0], "id", None) == "DEFAULTS":
            return ast.literal_eval(node.value)
    raise AssertionError("DEFAULTS not found")


class LayoutTest(unittest.TestCase):
    def test_regions_do_not_overlap(self):
        layout = Layout(sources=3, capacity=8, triggers=4)
        # Every named feed gets its own slot, whatever ``sources`` asks for.
        self.assertEqual(layout.sources, len(SOURCE_NAMES))
        self.assertGreaterEqual(layout.trigger_offset, layout.header_bytes)
        self.assertGreaterEqual(layout.tick_offset, layout.trigger_offset + layout.trigger_bytes)
        # Two axes (exchange time, receive time) per source.
        expected = layout.tick_offset + 2 * layout.sources * 2 * layout.capacity * 8
        self.assertEqual(layout.total_bytes, expected)
        # The two axes never alias, and both fit in the block.
        last = layout.sources - 1
        for axis in (0, 1):
            for position in (0, layout.capacity - 1):
                for index in (layout.ts_index(last, position, axis),
                              layout.px_index(last, position, axis)):
                    self.assertLess(index, layout.total_bytes // 8)

    def test_source_slot_is_stable_and_falls_back_to_other(self):
        self.assertEqual(source_slot("primary"), 0)
        self.assertEqual(source_slot("bybit"), SOURCE_NAMES.index("bybit"))
        self.assertEqual(source_slot("a-feed-that-does-not-exist"), len(SOURCE_NAMES) - 1)
        self.assertGreaterEqual(len(SOURCE_NAMES), 8)


class RingTest(unittest.TestCase):
    def setUp(self):
        self.layout = Layout(sources=3, capacity=16, triggers=4)
        self.block = SharedBlock(self.layout.total_bytes, create=True)
        self.writer = TickRingWriter(self.block, self.layout)
        self.reader = TickRingReader(self.block, self.layout)

    def tearDown(self):
        self.block.close()
        self.block.unlink()

    def test_empty_ring(self):
        self.assertEqual(self.reader.count("primary"), 0)
        self.assertIsNone(self.reader.latest("primary"))
        self.assertIsNone(self.reader.window_minmax("primary", 0.0, 1e9))

    def test_append_and_latest(self):
        for n in range(5):
            self.writer.append("primary", 1000.0 + n, 70_000.0 + n)
        self.assertEqual(self.reader.count("primary"), 5)
        self.assertEqual(self.reader.latest("primary"), (1004.0, 70_004.0))

    def test_sources_are_isolated(self):
        self.writer.append("primary", 1000.0, 70_000.0)
        self.writer.append("bybit", 1001.0, 60_000.0)
        self.assertEqual(self.reader.latest("primary"), (1000.0, 70_000.0))
        self.assertEqual(self.reader.latest("bybit"), (1001.0, 60_000.0))
        self.assertIsNone(self.reader.latest("kraken"))

    def test_ring_evicts_oldest_like_a_deque(self):
        for n in range(40):
            self.writer.append("primary", 1000.0 + n, 70_000.0 + n)
        self.assertEqual(self.reader.count("primary"), 40)
        # Only the last 16 survive the scan window.
        bounds = self.reader.window_minmax("primary", 0.0, 1e9)
        self.assertEqual(bounds, (70_000.0 + 24, 70_000.0 + 39))

    def test_window_minmax_matches_a_plain_scan(self):
        rows = []
        for n in range(30):
            ts, px = 1000.0 + n, 70_000.0 + (n % 7) * 3.0
            self.writer.append("primary", ts, px)
            rows.append((ts, px))
        # Only the newest ``capacity`` ticks survive, as with deque(maxlen).
        live = rows[-self.layout.capacity:]
        lower, upper = 1005.5, 1020.2
        expected = [px for ts, px in live if lower <= ts <= upper]
        self.assertEqual(self.reader.window_minmax("primary", lower, upper),
                         (min(expected), max(expected)))
        # ``between`` returns newest-first, which is what ``price_at`` walks.
        self.assertEqual(self.reader.between("primary", lower, upper),
                         list(reversed([r for r in live if lower <= r[0] <= upper])))


class TriggerRecordTest(unittest.TestCase):
    def setUp(self):
        self.layout = Layout(sources=3, capacity=8, triggers=4)
        self.block = SharedBlock(self.layout.total_bytes, create=True)

    def tearDown(self):
        self.block.close()
        self.block.unlink()

    def test_round_trip_preserves_every_field(self):
        write_trigger(self.block, self.layout, exchange_ms=1_700_000_000_500.0,
                      recv_wall_ms=1_700_000_000_512.5, spot=70_035.5, raw_spot=70_030.0,
                      low=70_000.0, high=70_040.0, up_jump=35.5, down_jump=0.0,
                      source="bybit")
        record = read_trigger(self.block, self.layout, 0)
        self.assertEqual(record["seq"], 0)
        self.assertAlmostEqual(record["ts_ms"], 1_700_000_000_500.0)
        self.assertAlmostEqual(record["recv_wall_ms"], 1_700_000_000_512.5)
        self.assertAlmostEqual(record["spot"], 70_035.5)
        self.assertAlmostEqual(record["raw_spot"], 70_030.0)
        self.assertAlmostEqual(record["min"], 70_000.0)
        self.assertAlmostEqual(record["max"], 70_040.0)
        self.assertAlmostEqual(record["up_jump"], 35.5)
        self.assertEqual(record["down_jump"], 0.0)
        self.assertEqual(record["source"], "bybit")

    def test_write_counter_advances_and_slots_wrap(self):
        for n in range(6):
            write_trigger(self.block, self.layout, exchange_ms=float(n), recv_wall_ms=float(n),
                          spot=1.0, raw_spot=1.0, low=0.0, high=1.0, up_jump=1.0,
                          down_jump=0.0, source="primary")
        self.assertEqual(int(self.block.i64[self.layout.header_index(H_TRIG_W)]), 6)
        self.assertEqual(read_trigger(self.block, self.layout, 4)["ts_ms"], 4.0)
        self.assertEqual(read_trigger(self.block, self.layout, 5)["ts_ms"], 5.0)

    def test_unwritten_slot_reads_as_none(self):
        self.assertIsNone(read_trigger(self.block, self.layout, 3))

    def test_trigger_to_jump_info_matches_the_in_process_shape(self):
        grid = PriceGrid()
        grid.add(999_600.0, 70_000.0, source="primary", recv_ms=999_600.0,
                 recv_wall_ms=999_600.0)
        grid.add(1_000_000.0, 70_030.0, source="primary", recv_ms=1_000_000.0,
                 recv_wall_ms=1_000_500.0)
        reference = grid.best_jump_in_window(500.0)
        write_trigger(self.block, self.layout, exchange_ms=reference["ts_ms"],
                      recv_wall_ms=reference["recv_wall_ms"], spot=reference["spot"],
                      raw_spot=reference["raw_spot"], low=reference["min"],
                      high=reference["max"], up_jump=reference["up_jump"],
                      down_jump=reference["down_jump"], source=reference["source"])
        info = trigger_to_jump_info(read_trigger(self.block, self.layout, 0))
        for key in ("spot", "raw_spot", "min", "max", "up_jump", "down_jump"):
            self.assertAlmostEqual(float(info[key]), float(reference[key]), msg=key)
        self.assertEqual(info["source"], reference["source"])
        # The per-feed receive stamp is what the latency journal reads.
        self.assertAlmostEqual(info["recv_wall_ms"], 1_000_500.0)
        self.assertAlmostEqual(info["ts_ms"], 1_000_000.0)


class SharedGridTest(unittest.TestCase):
    """The parent's read-only grid must answer like the in-process one."""

    def setUp(self):
        self.layout = Layout(sources=3, capacity=4096, triggers=4)
        self.block = SharedBlock(self.layout.total_bytes, create=True)
        self.writer = TickRingWriter(self.block, self.layout)
        self.local = PriceGrid()
        self.remote = SharedPriceGrid(self.block, self.layout)

    def tearDown(self):
        self.block.close()
        self.block.unlink()

    def _feed(self, rows):
        for exchange_ms, px in rows:
            self.local.add(exchange_ms, px, source="primary", recv_ms=exchange_ms,
                           recv_wall_ms=exchange_ms)
            self.writer.append("primary", exchange_ms, px)

    def test_twap60_price_at_and_latest_match_the_local_grid(self):
        rows = [(1_700_000_000_000.0 + n * 100.0, 70_000.0 + (n % 11)) for n in range(2000)]
        self._feed(rows)
        for g in (1_700_000_000_000 // 100, 1_700_000_000_000 // 100 + 900):
            self.assertAlmostEqual(self.remote.twap60(g), self.local.twap60(g), places=6)
        self.assertAlmostEqual(self.remote.price_at(1_700_000_015), self.local.price_at(1_700_000_015))
        self.assertAlmostEqual(self.remote.latest_exchange_ms(), self.local.latest_exchange_ms())

    def test_window_extrema_match_the_local_grid(self):
        rows = [(1_700_000_000_000.0 + n * 7.0, 70_000.0 + (n % 13) - 6) for n in range(400)]
        self._feed(rows)
        self.assertEqual(self.remote.window_extrema(500.0), self.local.window_extrema(500.0))

    def test_jump_is_the_child_verdict_not_a_rescan(self):
        self._feed([(1_700_000_000_000.0, 70_000.0)])
        self.assertIsNone(self.remote.best_jump_in_window(500.0))
        self.assertTrue(self.remote.trigger_only)
        write_trigger(self.block, self.layout, exchange_ms=1_700_000_000_100.0,
                      recv_wall_ms=1_700_000_000_150.0, spot=70_030.0, raw_spot=70_030.0,
                      low=70_000.0, high=70_030.0, up_jump=30.0, down_jump=0.0, source="primary")
        self.remote.set_trigger(read_trigger(self.block, self.layout, 0))
        info = self.remote.best_jump_in_window(500.0)
        self.assertAlmostEqual(info["up_jump"], 30.0)
        self.assertEqual(self.remote.jump_in_window(500.0), info)

    def test_feed_recv_snapshot_carries_the_child_recv_stamp(self):
        write_trigger(self.block, self.layout, exchange_ms=1_700_000_000_100.0,
                      recv_wall_ms=1_700_000_000_187.0, spot=70_030.0, raw_spot=70_030.0,
                      low=70_000.0, high=70_030.0, up_jump=30.0, down_jump=0.0, source="bybit")
        self.remote.set_trigger(read_trigger(self.block, self.layout, 0))
        snap = self.remote.feed_recv_snapshot(500.0)
        self.assertEqual(snap["first_feed"], "bybit")
        self.assertAlmostEqual(snap["feed_recv"]["bybit"], 1_700_000_000_187.0)


class DetectTriggerTest(unittest.TestCase):
    def _grid(self, jump_usd):
        grid = PriceGrid()
        grid.add(1_000_000.0, 70_000.0, source="primary", recv_ms=1_000_000.0,
                 recv_wall_ms=1_000_000.0)
        grid.add(1_000_100.0, 70_000.0 + jump_usd, source="primary", recv_ms=1_000_100.0,
                 recv_wall_ms=1_000_100.0)
        return grid

    def test_threshold_is_the_same_as_the_in_process_path(self):
        self.assertIsNone(detect_trigger(self._grid(24.0), threshold_usd=25.0 - 1e-9,
                                        window_ms=500))
        info = detect_trigger(self._grid(25.0), threshold_usd=25.0 - 1e-9, window_ms=500)
        self.assertAlmostEqual(info["up_jump"], 25.0)

    def test_down_jump_also_triggers(self):
        grid = PriceGrid()
        grid.add(1_000_000.0, 70_030.0, source="primary", recv_ms=1_000_000.0,
                 recv_wall_ms=1_000_000.0)
        grid.add(1_000_100.0, 70_000.0, source="primary", recv_ms=1_000_100.0,
                 recv_wall_ms=1_000_100.0)
        info = detect_trigger(grid, threshold_usd=25.0, window_ms=500)
        self.assertAlmostEqual(info["down_jump"], 30.0)


class FakeChildTest(unittest.TestCase):
    """Drive a real fork: child detects, parent wakes, grid reads through the ring."""

    CFG = {"feed_process_status_s": 0.2, "sniper_jump_usd": 25.0, "sniper_window_ms": 500,
           "sniper_extra_ws_enabled": False}

    def test_child_publishes_a_trigger_and_the_parent_sees_it(self):
        got: list[dict] = []
        logs: list[tuple[str, dict]] = []
        cfg = dict(self.CFG, _feed_process_fake_feed=True,
                   _feed_process_fake_ticks=[[1_000_000.0, 70_000.0, 0.0],
                                             [1_000_050.0, 70_000.0, 0.01],
                                             [1_000_100.0, 70_035.0, 0.01]])
        client = FeedProcessClient(cfg, on_trigger=got.append,
                                   log=lambda event, **kw: logs.append((event, kw)))
        try:
            self.assertTrue(client.start())
            deadline = time.time() + 5.0
            while not got and time.time() < deadline:
                time.sleep(0.01)
            self.assertTrue(got, "child must publish one trigger")
            trigger = got[0]
            self.assertAlmostEqual(trigger["up_jump"], 35.0)
            self.assertEqual(trigger["source"], "primary")
            self.assertTrue(trigger["recv_wall_ms"] > 0)
            self.assertGreaterEqual(client.grid.latest_exchange_ms(), 1_000_050.0)
            self.assertEqual([e for e, _ in logs][:1], ["feed_process_started"])
            self.assertFalse(client.fell_back)
        finally:
            client.stop()
        self.assertFalse(client.alive())

    def test_stop_is_clean_and_terminates_the_child(self):
        cfg = dict(self.CFG, _feed_process_fake_feed=True, _feed_process_fake_ticks=[])
        client = FeedProcessClient(cfg, on_trigger=lambda t: None, log=lambda *a, **k: None)
        self.assertTrue(client.start())
        pid = client.process.pid
        self.assertTrue(client.alive())
        client.stop()
        self.assertFalse(client.alive())
        self.assertIsNone(client.block)
        with self.assertRaises(ProcessLookupError):
            os.kill(pid, 0)

    def test_shm_handle_is_unlinked_so_segments_do_not_leak(self):
        cfg = dict(self.CFG, _feed_process_fake_feed=True, _feed_process_fake_ticks=[])
        client = FeedProcessClient(cfg, on_trigger=lambda t: None, log=lambda *a, **k: None)
        self.assertTrue(client.start())
        name = client.block.name
        client.stop()
        seg = Path(f"/dev/shm/{name}")
        self.assertFalse(seg.exists(), f"shared segment {name} leaked")


class HealthTest(unittest.TestCase):
    def _client(self, **cfg):
        client = FeedProcessClient(dict({"feed_process_status_s": 0.1}, **cfg),
                                   on_trigger=lambda t: None, log=lambda *a, **k: None)
        client.block = SharedBlock(Layout().total_bytes, create=True)
        client.layout = client.layout or Layout()
        client.process = SimpleNamespace(poll=lambda: None, pid=1, returncode=None)
        return client

    def _teardown(self, client):
        if client.block is not None:
            client.block.close()
            client.block.unlink()

    def test_healthy_child_reports_no_failure(self):
        client = self._client()
        try:
            client.block.i64[client.layout.header_index(H_HEARTBEAT_NS)] = int(
                client.now() * 1e9)
            self.assertIsNone(client._health_failure())
        finally:
            self._teardown(client)

    def test_dead_child_reports_the_exitcode(self):
        client = self._client()
        try:
            client.process = SimpleNamespace(poll=lambda: -9, pid=1, returncode=-9)
            self.assertEqual(client._health_failure(), "exitcode:-9")
        finally:
            self._teardown(client)

    def test_stale_heartbeat_is_detected(self):
        client = self._client(feed_process_status_s=0.1)
        try:
            client.block.i64[client.layout.header_index(H_HEARTBEAT_NS)] = int(
                (client.now() - 60.0) * 1e9)
            self.assertTrue(str(client._health_failure()).startswith("heartbeat_stale"))
        finally:
            self._teardown(client)

    def test_heartbeat_within_the_window_is_not_stale(self):
        client = self._client(feed_process_status_s=1.0)
        try:
            client.block.i64[client.layout.header_index(H_HEARTBEAT_NS)] = int(
                (client.now() - 1.0) * 1e9)
            self.assertIsNone(client._health_failure())
        finally:
            self._teardown(client)

    def test_monitor_calls_fallback_exactly_once(self):
        client = self._client(feed_process_status_s=0.05)
        fell: list[str] = []
        client.on_fallback = fell.append
        try:
            client.process = SimpleNamespace(poll=lambda: 1, pid=1, returncode=1)
            client._monitor_loop()
            self.assertEqual(fell, ["exitcode:1"])
            self.assertTrue(client.fell_back)
        finally:
            self._teardown(client)


class WorkerTest(unittest.TestCase):
    def setUp(self):
        self.layout = Layout(sources=3, capacity=64, triggers=4)
        self.block = SharedBlock(self.layout.total_bytes, create=True)

    def tearDown(self):
        self.block.close()
        self.block.unlink()

    def _worker(self, cfg=None):
        worker = FeedWorker(dict({"sniper_jump_usd": 25.0, "sniper_window_ms": 500,
                                  "sniper_extra_ws_enabled": False}, **(cfg or {})),
                            self.block, self.layout, None)
        worker.grid = PriceGrid()
        worker.install_mirror()
        return worker

    def test_mirrors_every_tick_into_the_ring_and_publishes_a_trigger(self):
        worker = self._worker()
        # Feed threads call grid.add (the worker wraps it to mirror into the ring);
        # _on_tick only runs the detector, so it must not duplicate the tick.
        for ms, px, recv in ((1_000_000.0, 70_000.0, 1_000_002.0),
                             (1_000_050.0, 70_000.0, 1_000_052.0)):
            worker.grid.add(ms, px, source="primary", recv_ms=ms, recv_wall_ms=recv)
            worker._on_tick(ms, px)
        self.assertEqual(int(self.block.i64[self.layout.header_index(H_TRIG_W)]), 0)
        worker.grid.add(1_000_100.0, 70_030.0, source="primary", recv_ms=1_000_100.0,
                        recv_wall_ms=1_000_120.0)
        worker._on_tick(1_000_100.0, 70_030.0)
        self.assertEqual(int(self.block.i64[self.layout.header_index(H_TRIG_W)]), 1)
        self.assertEqual(TickRingReader(self.block, self.layout, axis=0).count("primary"), 3)
        self.assertEqual(TickRingReader(self.block, self.layout, axis=1).count("primary"), 3)
        record = read_trigger(self.block, self.layout, 0)
        self.assertEqual(record["spot"], 70_030.0)
        # The recv stamp comes from the receive axis, not the exchange timestamp.
        self.assertAlmostEqual(record["recv_wall_ms"], 1_000_120.0)
        self.assertAlmostEqual(record["ts_ms"], 1_000_100.0)

    def test_sub_threshold_ticks_never_wake_the_parent(self):
        worker = self._worker()
        worker.grid.add(1_000_000.0, 70_000.0, source="primary", recv_ms=1_000_000.0,
                        recv_wall_ms=1_000_000.0)
        worker._on_tick(1_000_000.0, 70_000.0)
        for step in range(1, 20):
            ms, px = 1_000_000.0 + step * 50.0, 70_000.0 + step
            worker.grid.add(ms, px, source="primary", recv_ms=ms, recv_wall_ms=ms)
            worker._on_tick(ms, px)
        self.assertEqual(int(self.block.i64[self.layout.header_index(H_TRIG_W)]), 0)

    def test_stop_flag_silences_the_detector(self):
        worker = self._worker()
        worker.stop.set()
        worker.grid.add(1_000_000.0, 70_000.0, source="primary", recv_ms=1_000_000.0,
                        recv_wall_ms=1_000_000.0)
        worker._on_tick(1_000_000.0, 70_000.0)
        worker.grid.add(1_000_100.0, 70_030.0, source="primary", recv_ms=1_000_100.0,
                        recv_wall_ms=1_000_100.0)
        worker._on_tick(1_000_100.0, 70_030.0)
        self.assertEqual(int(self.block.i64[self.layout.header_index(H_TRIG_W)]), 0)

    def test_heartbeat_is_written_on_the_shared_block(self):
        worker = self._worker()
        worker._last_heartbeat = 0.0
        worker._heartbeat()
        self.assertGreater(int(self.block.i64[self.layout.header_index(H_HEARTBEAT_NS)]), 0)

    def test_magic_and_version_are_stamped_for_the_reader(self):
        self.assertEqual(MAGIC & 0xFFFFFFFF, 0x46656431)
        self.assertEqual(fp.VERSION, 1)


class SniperLatencyParityTest(unittest.TestCase):
    """A child-fed trigger must produce a sane ``sniper_order_latency`` line.

    Regression: the first version mirrored the primary tick twice and stamped
    ``recv_ms`` with exchange time, which made ``recv_to_decision_ms`` read as
    ~10^11 ms. The line must be usable for comparison with the in-process path.
    """

    def test_recv_to_decision_is_in_milliseconds_not_epoch_garbage(self):
        import os

        from test_tick_lock_short import Harness, _book, cfg as harness_cfg

        from buy.feed_process import FeedProcessClient, TickRingWriter, write_trigger

        harness = Harness(short=True, sniper=True)
        cfg = harness_cfg(feed_process=True, sniper_5m_enabled=True, fivemin_gate_k=0.0,
                          sniper_extra_ws_enabled=False)
        harness.runner.cfg = lambda: cfg
        harness.reset_market(time.time(), ttm_s=60)
        client = FeedProcessClient(dict(cfg, _feed_process_fake_feed=True),
                                   on_trigger=harness.runner.on_feed_trigger,
                                   log=lambda *a, **k: None)
        try:
            self.assertTrue(client.start())
            harness.runner.local_grid, harness.runner.grid = harness.runner.grid, client.grid
            harness.quotes = {"up": _book(0.92, 0.94, 20.0), "down": _book(0.06, 0.08, 20.0)}
            harness.clock[0] = time.time()
            now_ms = harness.clock[0] * 1000.0
            pre, jump, recv = now_ms - 400.0, now_ms, now_ms + 0.8
            writer_ex, writer_recv = (TickRingWriter(client.block, client.layout, axis=0),
                                      TickRingWriter(client.block, client.layout, axis=1))
            writer_ex.append("primary", pre, 70_000.0)
            writer_recv.append("primary", pre + 1.0, 70_000.0)
            writer_ex.append("primary", jump, 70_035.0)
            writer_recv.append("primary", recv, 70_035.0)
            write_trigger(client.block, client.layout, exchange_ms=jump, recv_wall_ms=recv,
                          spot=70_035.0, raw_spot=70_035.0, low=70_000.0, high=70_035.0,
                          up_jump=35.0, down_jump=0.0, source="primary")
            os.write(client.wake_fd, b"\x01" * 8)
            deadline = time.time() + 5.0
            while not harness.buys and time.time() < deadline:
                time.sleep(0.01)
            self.assertTrue(harness.buys, "child-fed trigger must fire the sniper")
            line = next(k for e, k in harness.logs if e == "sniper_order_latency")
            self.assertEqual(line["feed"], "primary")
            # Same stamp travels as trigger_recv_ts, so the journal stays comparable.
            self.assertAlmostEqual(line["trigger_recv_ts"] * 1000.0, recv, places=3)
            self.assertIsNotNone(line["recv_to_decision_ms"])
            self.assertLess(abs(line["recv_to_decision_ms"]), 5_000.0,
                            "recv_to_decision_ms must be a real delta, not an epoch value")
        finally:
            client.stop()

    def test_child_grid_mirrors_each_tick_once(self):
        """Two axes, one write per tick: no duplicated primary tick."""

        from buy.feed_process import H_TICKS

        layout = Layout(sources=3, capacity=64, triggers=4)
        block = SharedBlock(layout.total_bytes, create=True)
        try:
            worker = FeedWorker({"sniper_jump_usd": 1e9, "sniper_extra_ws_enabled": False},
                                block, layout, None)
            worker.grid = PriceGrid()
            worker.install_mirror()
            for n in range(5):
                ms = 1_000_000.0 + n
                worker.grid.add(ms, 70_000.0 + n, source="primary", recv_ms=ms,
                                recv_wall_ms=ms + 0.5)
                worker._on_tick(ms, 70_000.0 + n)
            self.assertEqual(TickRingReader(block, layout, axis=0).count("primary"), 5)
            self.assertEqual(TickRingReader(block, layout, axis=1).count("primary"), 5)
            self.assertEqual(int(block.i64[layout.header_index(H_TRIG_W)]), 0)
            # The recv axis carries the local stamp, the exchange axis the venue one.
            self.assertEqual(TickRingReader(block, layout, axis=0).latest("primary"),
                             (1_000_004.0, 70_004.0))
            self.assertEqual(TickRingReader(block, layout, axis=1).latest("primary"),
                             (1_000_004.5, 70_004.0))
        finally:
            block.close()
            block.unlink()


class FallbackWiringTest(unittest.TestCase):
    """Runner keeps in-process feeds unless the flag is on, and falls back cleanly."""

    def _runner(self, cfg):
        from test_tick_lock_short import Harness

        harness = Harness(short=True)
        harness.runner.cfg = lambda: dict(cfg)
        return harness

    def test_flag_off_never_starts_a_child_and_starts_in_process_feeds(self):
        started = []
        cfg = {"feed_process": False}
        harness = self._runner(cfg)
        harness.runner.binance = SimpleNamespace(start=lambda: started.append("in"),
                                                 stop=lambda: None)
        harness.runner.rtds = SimpleNamespace(start=lambda: None, stop=lambda: None,
                                              drain=lambda: [], reconnect=lambda r: None)
        harness.runner.books = SimpleNamespace(start=lambda: None, stop=lambda: None)
        self.assertFalse(harness.runner._start_feed_process())
        harness.runner.start()
        harness.runner._io_stop.set()
        self.assertEqual(started, ["in"])
        self.assertIsNone(harness.runner.feed_process)

    def test_fallback_restarts_in_process_feeds_and_restores_the_local_grid(self):
        started, stopped = [], []
        harness = self._runner({"feed_process": True})
        harness.runner.binance = SimpleNamespace(start=lambda: started.append("in"),
                                                 stop=lambda: None)
        local = harness.runner.grid
        child_grid = SimpleNamespace(trigger_only=True)
        harness.runner.feed_process = SimpleNamespace(
            stop=lambda: stopped.append("child"), grid=child_grid)
        harness.runner.local_grid = local
        harness.runner.grid = child_grid
        harness.runner._feedproc_fallback("exitcode:1")
        self.assertEqual(started, ["in"])
        self.assertEqual(stopped, ["child"])
        self.assertIs(harness.runner.grid, local)
        self.assertIsNone(harness.runner.feed_process)
        events = [e for e, _ in harness.logs]
        self.assertIn("feed_process_fallback_ready", events)

    def test_stop_tears_down_the_child(self):
        stopped = []
        harness = self._runner({"feed_process": True})
        harness.runner.binance = SimpleNamespace(start=lambda: None, stop=lambda: None)
        harness.runner.books = SimpleNamespace(start=lambda: None, stop=lambda: None)
        harness.runner.rtds = SimpleNamespace(start=lambda: None, stop=lambda: None,
                                              drain=lambda: [], reconnect=lambda r: None)
        harness.runner.feed_process = SimpleNamespace(stop=lambda: stopped.append(1), grid=None)
        harness.runner.stop()
        self.assertEqual(stopped, [1])
        self.assertIsNone(harness.runner.feed_process)

    def test_periodic_tick_never_rescans_a_trigger_only_grid(self):
        harness = self._runner({"feed_process": True, "sniper_5m_enabled": True})
        grid = harness.runner.grid
        calls = []
        grid.best_jump_in_window = lambda *a, **k: calls.append(1) or None
        grid.trigger_only = True
        row = harness.runner.strategy.state["markets"]["cid"]
        row["strike"] = 70_000.0
        harness.runner.current = row
        harness.runner.tick()
        self.assertEqual(calls, [])


class OrphanWatchdogTest(unittest.TestCase):
    """A child must never outlive its parent."""

    def setUp(self):
        self.layout = Layout(sources=3, capacity=8, triggers=4)
        self.block = SharedBlock(self.layout.total_bytes, create=True)
        self.clock = [1000.0]
        self.worker = FeedWorker({"sniper_extra_ws_enabled": False}, self.block,
                                 self.layout, None, now=lambda: self.clock[0])

    def tearDown(self):
        self.block.close()
        self.block.unlink()

    def _beat(self, at):
        self.block.i64[self.layout.header_index(H_PARENT_NS)] = int(at * 1e9)

    def test_parent_slot_does_not_collide_with_tick_counters(self):
        highest_counter = H_TICKS + 2 * len(SOURCE_NAMES) - 1
        self.assertGreater(H_PARENT_NS, highest_counter)
        self.assertLess(H_PARENT_NS, HEADER_SLOTS)

    def test_no_parent_stamp_yet_is_not_treated_as_gone(self):
        self.assertFalse(self.worker.parent_gone())

    def test_child_survives_while_the_parent_beats(self):
        for step in range(5):
            self.clock[0] += 5.0
            self._beat(self.clock[0])
            self.assertFalse(self.worker.parent_gone(), f"step {step}")

    def test_child_exits_after_a_silent_parent(self):
        self._beat(self.clock[0])
        self.assertFalse(self.worker.parent_gone())
        # The parent stops bumping.
        self.clock[0] += PARENT_STALE_S + 1.0
        self.assertTrue(self.worker.parent_gone())

    def test_a_late_beat_clears_the_stale_timer(self):
        self._beat(self.clock[0])
        self.assertFalse(self.worker.parent_gone())
        self.clock[0] += PARENT_STALE_S * 0.9
        self.assertFalse(self.worker.parent_gone())
        self._beat(self.clock[0])          # parent came back
        self.assertFalse(self.worker.parent_gone())
        self.clock[0] += PARENT_STALE_S * 0.5
        self.assertFalse(self.worker.parent_gone())


class FlagTest(unittest.TestCase):
    def test_defaults_off_everywhere(self):
        defaults = _defaults()
        self.assertIs(defaults["feed_process"], False)
        self.assertIs(FM_DEFAULTS["feed_process"], False)
        self.assertEqual(defaults["feed_process_trigger_ttl_ms"], 250)
        self.assertGreater(defaults["feed_process_status_s"], 0)
        example = json.loads((ROOT / "strategy_mint.example.json").read_text())
        self.assertIs(example["feed_process"], False)

    def test_no_network_work_at_import(self):
        source = (ROOT / "buy" / "feed_process.py").read_text()
        tree = ast.parse(source)
        for node in tree.body:
            if isinstance(node, (ast.ImportFrom, ast.Import)):
                names = [a.name for a in node.names]
                self.assertNotIn("websocket", names)
                self.assertNotIn("requests", names)
        # BinanceTrades/PriceGrid are imported inside run(), not at module scope.
        self.assertIn("from buy.binance_trades import BinanceTrades, PriceGrid",
                      source)


if __name__ == "__main__":
    unittest.main()
