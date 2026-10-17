"""Terminal state pruning keeps uncertain inventory and recoverable work."""

from __future__ import annotations

import gzip
import json
import os
import tempfile
import unittest
from copy import deepcopy
from datetime import date, datetime, timezone
from pathlib import Path

from buy.mint_loops import persist_digest
from buy.state_prune import prune_state
from recorder.archive import bucket_name, extract_logs, iter_sources


NOW = 100000.0


def terminal(**fields):
    return {"status": "completed", "end_ts": 1000, "updated_at": 2000,
            "sold_loser": True, "sold_winner": True, "redeemed": True, **fields}


class StatePruneTests(unittest.TestCase):
    def test_legacy_terminal_with_null_optional_stamps_is_pruned(self):
        for status in ("completed", "failed"):
            with self.subTest(status=status):
                intent = {"status": status, "end_ts": 1000, "updated_at": 2000,
                          "last_fail_ts": None, "created_at": None}
                original = deepcopy(intent)
                state = {"intents": {"old": intent}, "redeems": {}}
                rows = prune_state(state, NOW)
                self.assertEqual(state["intents"], {})
                self.assertEqual(rows, [{"ts": NOW, "event": "state_pruned",
                                        "condition_id": "old", "intent": original}])

    def test_old_uses_positive_end_update_and_job_update_only(self):
        for fields in ({"end_ts": None}, {"end_ts": "bad"}, {"updated_at": None},
                       {"updated_at": float("inf")}, {"updated_at": False},
                       {"last_fail_ts": NOW}, {"created_at": NOW}):
            with self.subTest(fields=fields):
                state = {"intents": {"old": terminal(**fields)}}
                self.assertEqual(len(prune_state(state, NOW)), 1)
        for fields in ({"end_ts": NOW}, {"updated_at": NOW},
                       {"end_ts": NOW - 21600, "updated_at": 1},
                       {"end_ts": None, "updated_at": None},
                       {"end_ts": -1, "updated_at": "bad"}):
            with self.subTest(fields=fields):
                self.assertEqual(prune_state({"intents": {"keep": terminal(**fields)}}, NOW), [])
        state = {"intents": {"keep": terminal()},
                 "redeems": {"keep": {"status": "done", "updated_at": NOW - 1}}}
        self.assertEqual(prune_state(state, NOW), [])
        state["redeems"]["keep"]["updated_at"] = None
        self.assertEqual(len(prune_state(state, NOW)), 1)
        self.assertEqual(prune_state({"intents": {"keep": {"status": "failed", "created_at": 1}}}, NOW), [])

    def test_only_terminal_statuses_are_pruned(self):
        intents = {status: terminal(status=status) for status in (
            "submitting", "pending", "executed", "mined", "confirmed_waiting_inventory",
            "confirmed", "unknown", "", "done")}
        intents.update(null=None, malformed=[1])
        state = {"intents": intents}
        before = deepcopy(state)
        self.assertEqual(prune_state(state, NOW), [])
        self.assertEqual(state, before)

    def test_open_work_and_gave_up_markers_are_kept(self):
        cases = [{"redeem_gave_up": True}, {"sell_exit_inflight": True},
                 {"sell_scrap_rest_id": "order"}, {"reclaim_hot": True},
                 {"reclaim_bought": True}, {"reclaim_buy_posted": True},
                 {"reclaim_stop_latched": True}, {"reclaim_filled": 1}]
        for key in ("some_inflight", "some_uncertain", "some_pending", "some_retrying"):
            cases.append({key: True})
        for value in ("STATE_NEW", "STATE_EXECUTED", "STATE_MINED"):
            cases.append({"status": "failed", "relayer_state": value})
        for fields in cases:
            with self.subTest(fields=fields):
                self.assertEqual(prune_state({"intents": {"keep": terminal(**fields)}}, NOW), [])

    def test_mined_completed_is_pruned_but_mined_failed_is_kept(self):
        state = {"intents": {
            "completed": terminal(status="completed", relayer_state="STATE_MINED"),
            "failed": terminal(status="failed", relayer_state="STATE_MINED"),
        }}
        rows = prune_state(state, NOW)
        self.assertEqual([row["condition_id"] for row in rows], ["completed"])
        self.assertEqual(set(state["intents"]), {"failed"})

    def test_inventory_and_stopped_reclaim_evidence_do_not_block_prune(self):
        state = {"intents": {"old": terminal(
            redeemed=False, sold_loser=False, sold_winner=False,
            observed_up=100, observed_dn=100, before_up=10, sell_scrap_keep=100,
            reclaim_stopped=True, reclaim_filled=50, reclaim_hedge_filled=20,
            redeem_status="legacy", reclaim_hedge_kept=True)}}
        self.assertEqual(len(prune_state(state, NOW)), 1)

    def test_redeem_jobs_preserved_and_only_open_jobs_block(self):
        for status in ("waiting", "submitting", "submitted", "retry", "unknown", None):
            with self.subTest(status=status):
                state = {"intents": {"keep": terminal()}, "redeems": {"keep": {"status": status}}}
                self.assertEqual(prune_state(state, NOW), [])
        for status in ("done", "no_winner", "gave_up"):
            with self.subTest(status=status):
                job = {"status": status, "updated_at": 3000}
                state = {"intents": {"old": terminal()}, "redeems": {"old": job}}
                self.assertEqual(len(prune_state(state, NOW)), 1)
                self.assertIs(state["redeems"]["old"], job)
        state = {"intents": {"keep": terminal(redeem_gave_up=True)},
                 "redeems": {"keep": {"status": "gave_up"}}}
        self.assertEqual(prune_state(state, NOW), [])

    def test_archive_rows_detached_and_other_state_unchanged(self):
        intent = terminal(nested={"values": [1]})
        state = {"intents": {"old": intent}, "other": {"keep": True}}
        before = persist_digest(state)
        rows = prune_state(state, NOW)
        intent["nested"]["values"].append(2)
        self.assertEqual(rows[0]["intent"]["nested"]["values"], [1])
        self.assertNotEqual(before, persist_digest(state))
        self.assertEqual(state["other"], {"keep": True})

    def test_invalid_inputs_and_configurable_retention(self):
        for state in (None, [], {}, {"intents": []}, {"intents": {"x": terminal()}, "redeems": []}):
            self.assertEqual(prune_state(state, NOW), [])
        for now, retention in ((float("nan"), 0), (NOW, -1), (NOW, float("inf")), (NOW, "bad")):
            self.assertEqual(prune_state({"intents": {"x": terminal()}}, now, retention), [])
        self.assertEqual(len(prune_state({"intents": {"x": terminal()}}, 2001, 0)), 1)


class StateArchiveTests(unittest.TestCase):
    def test_discovers_live_and_rotated_archives_and_combines_daily_bucket(self):
        day = date(2026, 10, 14)
        ts = datetime(2026, 10, 14, 12, tzinfo=timezone.utc).timestamp()
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            live = repo / "logs" / "state_archive.jsonl"
            rotation = repo / "logs" / "archive" / "state_archive.jsonl.20261015T000000Z.gz"
            rotation.parent.mkdir(parents=True)
            live_rows = [{"ts": ts, "event": "state_pruned", "condition_id": "live"},
                         {"ts": ts + 86400, "event": "state_pruned", "condition_id": "tomorrow"}]
            live.write_text("".join(json.dumps(row) + "\n" for row in live_rows), encoding="utf-8")
            os.utime(live, (ts, ts))
            rotated_row = {"ts": ts, "event": "state_pruned", "condition_id": "rotated"}
            with gzip.open(rotation, "wt", encoding="utf-8") as handle:
                handle.write(json.dumps(rotated_row) + "\n")
            self.assertEqual(set(iter_sources(repo)), {live, rotation})
            for path in (live, rotation):
                self.assertEqual(bucket_name(path), "state_archive.jsonl.gz")
            notes = extract_logs(repo, day, repo / "out")
            with gzip.open(repo / "out" / "state_archive.jsonl.gz", "rt", encoding="utf-8") as handle:
                rows = [json.loads(line) for line in handle]
            self.assertEqual(rows, [live_rows[0], rotated_row])
            self.assertEqual(notes[0]["lines"], 2)
            self.assertTrue(live.is_file())
            self.assertTrue(rotation.is_file())


if __name__ == "__main__":
    unittest.main()
