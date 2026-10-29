"""State persistence runs off-thread; durable boundaries remain explicit."""
from __future__ import annotations

import ast
import json
import logging
import os
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from buy.mint_loops import persist_digest
from buy.state_writer import StateWriter, atomic_write, snapshot_state
from test_mint_cpu import _dump_cfg, _dump_harness, _fill_fak, _held_after_scrap
from test_mint_sequential import MintHarness, S, _cfg, _market

ROOT = Path(__file__).resolve().parents[1]


def functions(*names, namespace=None, **extra):
    tree = ast.parse((ROOT / "mintbot.py").read_text())
    nodes = [
        node for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name in names
    ]
    if {node.name for node in nodes} != set(names):
        raise AssertionError(f"missing functions: {set(names) - {node.name for node in nodes}}")
    ns = namespace if namespace is not None else {}
    ns.update(extra)
    module = ast.Module(body=nodes, type_ignores=[])
    exec(compile(module, "mintbot.py", "exec"), ns)
    return ns


class WriterTests(unittest.TestCase):
    def test_coalesces_first_and_latest_and_flush_stop(self):
        with tempfile.TemporaryDirectory() as tmp:
            entered = threading.Event()
            release = threading.Event()
            seen = []

            def write(path, value):
                seen.append(value["n"])
                if value["n"] == 0:
                    entered.set()
                    if not release.wait(3):
                        raise TimeoutError("test did not release first write")
                atomic_write(path, value)

            writer = StateWriter(Path(tmp) / "state.json", write=write)
            try:
                writer.submit({"n": 0})
                self.assertTrue(entered.wait(3))
                for n in range(1, 100):
                    seq = writer.submit({"n": n})
                self.assertFalse(writer.wait(seq, 0.01))
                release.set()
                self.assertTrue(writer.flush(3))
            finally:
                release.set()
                self.assertTrue(writer.stop(3))

            self.assertEqual(seen, [0, 99])
            self.assertEqual(json.loads(writer.path.read_text()), {"n": 99})

    def test_failed_replace_retains_old_file_and_next_submit_retries(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.json"
            atomic_write(path, {"n": 0})
            logs = []
            writer = StateWriter(path, log=lambda *a, **k: logs.append((a, k)))
            try:
                with patch("buy.state_writer.os.replace", side_effect=OSError("disk failure")):
                    seq = writer.submit({"n": 1})
                    self.assertFalse(writer.wait(seq, 3))
                    self.assertEqual(json.loads(path.read_text()), {"n": 0})
                    self.assertIsInstance(writer.last_error, OSError)
                self.assertTrue(writer.wait(writer.submit({"n": 3}), 3))
            finally:
                self.assertTrue(writer.stop(3))

            self.assertEqual(len(logs), 1)
            self.assertEqual(json.loads(path.read_text()), {"n": 3})

    def test_wait_finishes_after_directory_fsync_and_callback(self):
        with tempfile.TemporaryDirectory() as tmp:
            entered = threading.Event()
            release = threading.Event()
            callbacks = []
            fsync_threads = []
            real_fsync = os.fsync

            def fsync(fd):
                fsync_threads.append(threading.get_ident())
                if len(fsync_threads) == 2:
                    entered.set()
                    if not release.wait(3):
                        raise TimeoutError("test did not release directory fsync")
                real_fsync(fd)

            with patch("buy.state_writer.os.fsync", side_effect=fsync):
                writer = StateWriter(
                    Path(tmp) / "state.json",
                    on_saved=lambda *args: callbacks.append(args),
                )
                try:
                    seq = writer.submit({"n": 1})
                    self.assertTrue(entered.wait(3))
                    self.assertFalse(writer.wait(seq, 0.01))
                    release.set()
                    self.assertTrue(writer.wait(seq, 3))
                    self.assertEqual(callbacks[0][0], seq)
                finally:
                    release.set()
                    self.assertTrue(writer.stop(3))

            self.assertEqual(len(fsync_threads), 2)
            self.assertTrue(all(t != threading.get_ident() for t in fsync_threads))

    def test_archive_written_before_pruned_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.json"
            archive = Path(tmp) / "logs/state_archive.jsonl"
            row = {
                "ts": 100,
                "event": "state_pruned",
                "condition_id": "old",
                "intent": {"status": "failed"},
            }

            def write(path, snapshot):
                self.assertEqual(json.loads(archive.read_text()), row)
                atomic_write(path, snapshot)

            writer = StateWriter(path, archive_path=archive, write=write)
            try:
                seq = writer.submit({"intents": {}}, archive_rows=[row])
                self.assertTrue(writer.wait(seq, 3))
            finally:
                self.assertTrue(writer.stop(3))

    def test_large_archive_batch_has_two_fsyncs_and_one_state_serialization(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.json"
            archive = Path(tmp) / "logs/state_archive.jsonl"
            snapshot = {"intents": {"live": {"status": "confirmed"}}}
            rows = [{"ts": 100, "event": "state_pruned", "condition_id": str(i),
                     "intent": {"status": "completed", "last_fail_ts": None}} for i in range(2400)]
            expected_rows = list(rows)
            events = []
            fsync_count = []
            real_fsync, real_dumps = os.fsync, json.dumps
            serialized_states = []

            def fsync(fd):
                fsync_count.append(fd)
                real_fsync(fd)

            def dumps(value, **kwargs):
                if "intents" in value:
                    serialized_states.append(value)
                return real_dumps(value, **kwargs)

            writer = StateWriter(path, archive_path=archive,
                                 log=lambda event, **kw: events.append((event, kw)))
            try:
                with patch("buy.state_writer.os.fsync", side_effect=fsync), \
                     patch("buy.state_writer.json.dumps", side_effect=dumps):
                    seq = writer.submit(snapshot, archive_rows=rows)
                    self.assertTrue(writer.wait(seq, 5))
                self.assertEqual(len(fsync_count), 4)  # Archive + directory, state + directory.
                self.assertEqual(serialized_states, [snapshot])
                self.assertEqual([json.loads(line) for line in archive.read_text().splitlines()], expected_rows)
                summary = next(kw for event, kw in events if event == "state_pruned_summary")
                self.assertEqual(summary["bytes_after"], path.stat().st_size)
                self.assertEqual(summary["bytes_before"], path.stat().st_size + archive.stat().st_size)
                self.assertEqual(summary["removed"], 2400)
            finally:
                self.assertTrue(writer.stop(3))

    def test_late_submits_after_stop_are_noops_and_log_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            events = []
            path = Path(tmp) / "state.json"
            writer = StateWriter(path, log=lambda event, **kw: events.append(event))
            seq = writer.submit({"n": 1})
            self.assertTrue(writer.stop(3))
            self.assertTrue(writer.wait(seq, 0))
            self.assertEqual(writer.submit({"n": 2}), 0)
            self.assertEqual(writer.submit({"n": 3}), 0)
            self.assertEqual(json.loads(path.read_text()), {"n": 1})
            self.assertEqual(events, ["state_save_after_stop"])

    def test_late_commit_returns_false_and_leaves_digest_dirty(self):
        lock = threading.RLock()
        ns = functions("commit_state", "persist_dirty", STATE_LOCK=lock,
                       _saved_persist_form=None, persist_digest=persist_digest,
                       _STATE_WRITER=SimpleNamespace(submit=lambda snapshot: 0))
        state = {"intents": {"late": {"status": "confirmed"}}}
        self.assertFalse(ns["commit_state"](state, durable=True))
        self.assertTrue(ns["persist_dirty"](state))

    def test_structural_copy_and_hot_commit_no_json_or_disk(self):
        state = {
            "intents": {"cid": {"status": "confirmed", "nested": [1, {"n": 2}]}},
            "redeems": {},
        }
        snapshots = []
        lock = threading.RLock()
        test = self

        class Writer:
            def submit(self, snapshot):
                test.assertTrue(lock._is_owned())
                snapshots.append(snapshot)
                return len(snapshots)

            def wait(self, seq):
                test.assertFalse(lock._is_owned(), "wait held STATE_LOCK")
                test.assertEqual(seq, len(snapshots))
                return True

        ns = functions(
            "commit_state", "remember_persisted_state", "persist_dirty", "_wait_state_save",
            _STATE_WRITER=Writer(),
            STATE_LOCK=lock,
            _saved_persist_form=None,
            persist_digest=persist_digest,
            atomic_save=Mock(side_effect=AssertionError("disk I/O")),
        )
        with patch("json.dumps", side_effect=AssertionError("serialization")):
            with lock:
                seq = ns["commit_state"](state, dirty=True, durable=True)
            ns["_wait_state_save"](seq)
            self.assertFalse(ns["persist_dirty"](state))
            state["intents"]["cid"]["nested"][1]["n"] = 3
            self.assertTrue(ns["persist_dirty"](state))
            self.assertEqual(snapshots[0]["intents"]["cid"]["nested"][1]["n"], 2)
        ns["atomic_save"].assert_not_called()

    def test_manage_sells_queues_commit_without_serializing(self):
        ns, events, fak_calls, clock = _dump_harness(_fill_fak)
        intent = _held_after_scrap(
            clock["now"] + 200.0,
            nested=[{"n": 1}],
        )
        state = {"intents": {"cid": intent}}
        cfg = _dump_cfg(sell_enabled=True, post_dump_log_enabled=False)
        snapshots = []

        def submit(snapshot):
            self.assertTrue(ns["STATE_LOCK"]._is_owned())
            snapshots.append(snapshot)
            return len(snapshots)

        writer = Mock()
        writer.submit.side_effect = submit
        ns["_STATE_WRITER"] = writer
        ns["atomic_save"] = Mock(side_effect=AssertionError("sell tick performed disk I/O"))
        ns["remember_persisted_state"](state)

        with patch("json.dumps", side_effect=AssertionError("sell tick serialized state")) as dumps:
            ns["manage_sells"](cfg, state, object())
            self.assertEqual(intent["sell_dump_armed_at"], clock["now"])
            self.assertEqual(len(snapshots), 1)
            self.assertEqual(fak_calls, [])
            first = snapshots[0]
            digest = ns["_saved_persist_form"]
            self.assertEqual(digest, persist_digest(first))
            self.assertFalse(ns["persist_dirty"](state))
            self.assertIsNot(first["intents"]["cid"], intent)

            intent["nested"][0]["n"] = 2
            self.assertEqual(first["intents"]["cid"]["nested"][0]["n"], 1)
            self.assertEqual(ns["_saved_persist_form"], digest)
            self.assertTrue(ns["persist_dirty"](state))

            clock["now"] += 2.0
            ns["manage_sells"](cfg, state, object())
            self.assertEqual(len(fak_calls), 1)
            self.assertTrue(intent["sold_dump"])
            self.assertEqual(len(snapshots), 2)
            self.assertTrue(snapshots[-1]["intents"]["cid"]["sold_dump"])
            self.assertEqual(snapshots[-1]["intents"]["cid"]["nested"][0]["n"], 2)
            self.assertNotEqual(ns["_saved_persist_form"], digest)
            self.assertEqual(ns["_saved_persist_form"], persist_digest(snapshots[-1]))
            self.assertFalse(ns["persist_dirty"](state))
            self.assertFalse(ns["commit_state"](state))
            dumps.assert_not_called()

        self.assertIn("sell_dump_done", [row["event"] for row in events])
        self.assertFalse(first["intents"]["cid"].get("sold_dump", False))
        ns["atomic_save"].assert_not_called()
        writer.wait.assert_not_called()

    def test_mint_claim_is_durable_before_relayer_submit(self):
        market = _market(S, 2)
        harness = MintHarness(_cfg(), {"intents": {}}, [market], balance=1_000.0)
        ns = harness.ns
        snapshots = []
        saved = []
        order = []
        test = self

        class Writer:
            def submit(self, snapshot):
                test.assertTrue(ns["STATE_LOCK"]._is_owned())
                snapshots.append(snapshot)
                seq = len(snapshots)
                order.append(("submit", seq))
                return seq

            def wait(self, seq):
                test.assertFalse(ns["STATE_LOCK"]._is_owned(), "wait held STATE_LOCK")
                saved.append(snapshots[seq - 1])
                order.append(("saved", seq))
                return True

        functions(
            "commit_state", "persist_dirty", "remember_persisted_state", "_wait_state_save",
            namespace=ns,
            _STATE_WRITER=Writer(),
            _saved_persist_form=None,
            persist_digest=persist_digest,
            atomic_save=Mock(side_effect=AssertionError("synchronous disk I/O")),
        )
        ns["_prune_state_tick"] = lambda *_a, **_k: None
        ns["remember_persisted_state"](harness.state)
        real_submit = harness._submit

        def submit_relayer(*args, **kwargs):
            self.assertEqual(len(saved), 1)
            self.assertEqual(order, [("submit", 1), ("saved", 1)])
            claim = saved[0]["intents"][market.condition_id]
            self.assertEqual(claim["status"], "submitting")
            self.assertIsNone(claim["transaction_id"])
            self.assertIsNot(claim, harness.state["intents"][market.condition_id])
            order.append(("relayer", None))
            return real_submit(*args, **kwargs)

        ns["submit_mint_batch"] = submit_relayer
        self.assertEqual(harness.tick(S), "submitted")
        self.assertEqual(
            order,
            [("submit", 1), ("saved", 1), ("relayer", None), ("submit", 2), ("saved", 2)],
        )
        self.assertEqual(len(harness.submits), 1)
        self.assertEqual(saved[0]["intents"][market.condition_id]["status"], "submitting")
        self.assertEqual(saved[1]["intents"][market.condition_id]["status"], "pending")
        self.assertEqual(saved[1]["intents"][market.condition_id]["transaction_id"], "tx-1")
        ns["atomic_save"].assert_not_called()

    def test_redeem_submit_waits_for_queued_claim_outside_state_lock(self):
        for saved in (True, False):
            with self.subTest(saved=saved):
                lock = threading.RLock()
                order = []

                class Writer:
                    def flush(self):
                        if lock._is_owned():
                            raise AssertionError("redeem flush held STATE_LOCK")
                        order.append("saved")
                        return saved

                ns = functions(
                    "build_redeem_desk", _STATE_WRITER=Writer(), STATE_LOCK=lock,
                    RedeemIO=lambda **fields: SimpleNamespace(**fields),
                    RedeemDesk=lambda io, **kwargs: io,
                    commit_state=lambda *_a, **_k: None,
                    log_event=lambda *_a, **_k: None,
                    notify=lambda *_a, **_k: None,
                    submit_redeem=lambda *_a: order.append("relayer") or ("tx", None, {}),
                )
                io = ns["build_redeem_desk"]({"cfg": {}}, object(), object(), "funder")
                result = io.submit("cid", True)
                self.assertEqual(order, ["saved", "relayer"] if saved else ["saved"])
                self.assertEqual(result[0], "tx" if saved else None)

    def test_save_error_invalidates_digest(self):
        ns = functions(
            "_state_save_error",
            STATE_LOCK=threading.RLock(),
            _saved_persist_form=(1,),
        )
        ns["_state_save_error"](1, OSError())
        self.assertIsNone(ns["_saved_persist_form"])

    def test_background_failure_invalidates_digest_and_next_commit_retries(self):
        state = {"intents": {"cid": {"status": "confirmed", "shares": 50.0}}}
        ns = functions(
            "commit_state", "persist_dirty", "_state_save_error",
            STATE_LOCK=threading.RLock(),
            _saved_persist_form=None,
            persist_digest=persist_digest,
            atomic_save=Mock(side_effect=AssertionError("synchronous disk I/O")),
        )
        attempts = []
        worker_threads = []

        def write(_path, snapshot):
            attempts.append(snapshot)
            worker_threads.append(threading.get_ident())
            if len(attempts) == 1:
                raise OSError("background write failed")

        with tempfile.TemporaryDirectory() as tmp:
            writer = StateWriter(
                Path(tmp) / "state.json", write=write, on_error=ns["_state_save_error"],
            )
            ns["_STATE_WRITER"] = writer
            try:
                self.assertTrue(ns["commit_state"](state))
                self.assertFalse(writer.flush(3))
                self.assertIsInstance(writer.last_error, OSError)
                self.assertIsNone(ns["_saved_persist_form"])
                self.assertTrue(ns["persist_dirty"](state))

                self.assertTrue(ns["commit_state"](state))
                self.assertTrue(writer.flush(3))
                self.assertIsNone(writer.last_error)
                self.assertFalse(ns["persist_dirty"](state))
                self.assertFalse(ns["commit_state"](state))
            finally:
                self.assertTrue(writer.stop(3))

        self.assertEqual(attempts, [state, state])
        self.assertTrue(all(t != threading.get_ident() for t in worker_threads))
        ns["atomic_save"].assert_not_called()

    def test_snapshot_scalars_shared_and_containers_detached(self):
        source = {"a": [{"b": "text"}]}
        copy = snapshot_state(source)
        self.assertIsNot(copy["a"], source["a"])
        self.assertIs(copy["a"][0]["b"], source["a"][0]["b"])

    def test_logging_producer_returns_while_stream_sink_is_blocked(self):
        entered = threading.Event()
        release = threading.Event()
        returned = threading.Event()
        writes = []
        sink_threads = []

        def write(text):
            writes.append(text)
            sink_threads.append(threading.get_ident())
            entered.set()
            if not release.wait(3):
                raise TimeoutError("test did not release logging sink")

        stream = SimpleNamespace(write=write, flush=lambda: None)
        logger = logging.getLogger("mintbot")
        old_handlers = logger.handlers[:]
        old_level = logger.level
        old_propagate = logger.propagate
        old_disabled = logger.disabled
        producer = None
        listener = None
        queue_handlers = []

        with tempfile.TemporaryDirectory() as tmp:
            log_file = Path(tmp) / "mintbot.log"
            import os as _os
            _saved_env = dict(_os.environ)
            import mintbot as _mb
            _os.environ.clear()
            _os.environ.update(_saved_env)

            ns = functions(
                "log_setup", LOG_FILE=log_file, sys=SimpleNamespace(stdout=stream),
                atexit=SimpleNamespace(register=lambda *a, **k: None),
                _DeferredJsonFormatter=_mb._DeferredJsonFormatter,
                _DeferredQueueHandler=_mb._DeferredQueueHandler,
                _stop_log_listener=lambda: None,
            )
            try:
                logger.disabled = False
                ns["log_setup"]()
                listener = ns["_LOG_LISTENER"]
                queue_handlers = logger.handlers[:]

                def produce():
                    logger.info("first event")
                    logger.info("second event")
                    returned.set()

                producer = threading.Thread(target=produce)
                producer.start()
                self.assertTrue(entered.wait(3))
                self.assertTrue(returned.wait(1), "logging producer waited for the sink")
                self.assertFalse(release.is_set())
            finally:
                release.set()
                if producer is not None:
                    producer.join(3)
                if listener is not None:
                    listener.stop()
                    for handler in listener.handlers:
                        handler.close()
                for handler in queue_handlers:
                    handler.close()
                logger.handlers = old_handlers
                logger.setLevel(old_level)
                logger.propagate = old_propagate
                logger.disabled = old_disabled

            self.assertFalse(producer.is_alive())
            self.assertEqual(writes, ["first event\n", "second event\n"])
            self.assertEqual(log_file.read_text().splitlines(), ["first event", "second event"])
            self.assertTrue(all(t != producer.ident for t in sink_threads))
