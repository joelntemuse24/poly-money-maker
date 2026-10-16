"""Daily archive copies a day and does not delete source logs."""

from __future__ import annotations

import ast
import gzip
import json
import tempfile
import unittest
from datetime import date, datetime, timezone
from pathlib import Path

from recorder.archive import (
    archive_day,
    bucket_name,
    in_day,
    journal_command,
    move_tape_day,
    overlaps_day,
    resolve_remote,
)
from recorder.disk import enforce_tape_cap
from recorder.tape import TapeWriter


ROOT = Path(__file__).resolve().parents[1]


def _touch(path: Path, text: str, when: float) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    ts = when
    import os

    os.utime(path, (ts, ts))


class OverlapTests(unittest.TestCase):
    def test_stamp_window_and_mtime(self):
        day = date(2026, 10, 14)
        stamped = Path("mintbot.log.20261014T120001Z.gz")
        self.assertTrue(overlaps_day(stamped, day, mtime=0))
        later = Path("oracle_twap.jsonl.20261020T000000Z.gz")
        self.assertTrue(overlaps_day(later, day, mtime=0))
        too_late = Path("oracle_twap.jsonl.20261201T000000Z.gz")
        self.assertFalse(overlaps_day(too_late, day, mtime=0))
        live = Path("mintbot.log")
        oct_14 = datetime(2026, 10, 14, 12, tzinfo=timezone.utc).timestamp()
        oct_15 = datetime(2026, 10, 15, 1, tzinfo=timezone.utc).timestamp()
        self.assertTrue(overlaps_day(live, day, mtime=oct_15))
        self.assertFalse(overlaps_day(live, day, mtime=datetime(2026, 10, 1, tzinfo=timezone.utc).timestamp()))
        self.assertTrue(in_day(oct_14, day))
        self.assertFalse(in_day(oct_15, day))

    def test_buckets(self):
        self.assertEqual(bucket_name(Path("logs/archive/mintbot.log.20261014T120001Z.gz")), "mintbot.jsonl.gz")
        self.assertEqual(bucket_name(Path("logs/price_tape.jsonl")), "price_tape.jsonl.gz")
        self.assertEqual(bucket_name(Path("logs/shadow_early_scrap.jsonl")), "shadow_early_scrap.jsonl.gz")


class ArchiveRunTests(unittest.TestCase):
    def test_extracts_yesterdays_lines_and_keeps_the_source(self):
        day = date(2026, 10, 14)
        in_ts = datetime(2026, 10, 14, 18, tzinfo=timezone.utc).timestamp()
        out_ts = datetime(2026, 10, 15, 0, 10, tzinfo=timezone.utc).timestamp()
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            live = repo / "mintbot.log"
            _touch(
                live,
                "\n".join(
                    [
                        json.dumps({"ts": in_ts, "event": "keep"}),
                        json.dumps({"ts": out_ts, "event": "today"}),
                    ]
                )
                + "\n",
                out_ts,
            )
            tape = repo / "data" / "tape" / "2026-10-14" / "btc-updown-15m-1.jsonl.gz"
            tape.parent.mkdir(parents=True)
            tape.write_bytes(b"gzip-bytes")
            other = repo / "data" / "tape" / "2026-10-15" / "stay.jsonl.gz"
            other.parent.mkdir(parents=True)
            other.write_bytes(b"still-spool")

            class Result:
                returncode = 0
                stdout = json.dumps({"MESSAGE": "mint", "__REALTIME_TIMESTAMP": "1"}).encode()
                stderr = b""

            manifest = archive_day(
                repo,
                day,
                repo / "logs" / "durable",
                journal_runner=lambda _cmd: Result(),
                remote="",
            )
            extracted = gzip.open(repo / "logs" / "durable" / "2026-10-14" / "mintbot.jsonl.gz", "rt").read()
            self.assertIn("keep", extracted)
            self.assertNotIn("today", extracted)
            self.assertEqual(live.read_text(encoding="utf-8").count("keep"), 1)
            self.assertFalse(tape.exists())
            moved = repo / "logs" / "durable" / "2026-10-14" / "tape" / "btc-updown-15m-1.jsonl.gz"
            self.assertEqual(moved.read_bytes(), b"gzip-bytes")
            self.assertEqual(other.read_bytes(), b"still-spool")
            self.assertEqual(manifest["deleted_logs"], [])
            self.assertTrue(manifest["journal"]["ok"])
            self.assertTrue((repo / "logs" / "durable" / "2026-10-14" / "journal-polymintbot.jsonl.gz").is_file())

    def test_journal_failure_is_recorded_and_logs_stay(self):
        day = date(2026, 10, 14)
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            live = repo / "mintbot.log"
            _touch(live, json.dumps({"ts": 1, "event": "x"}) + "\n", 1)

            class Result:
                returncode = 1
                stdout = b""
                stderr = b"permission denied"

            manifest = archive_day(repo, day, repo / "logs" / "durable", journal_runner=lambda _cmd: Result())
            self.assertFalse(manifest["journal"]["ok"])
            self.assertIn("permission", manifest["journal"]["error"])
            self.assertTrue(live.exists())
            self.assertEqual(manifest["deleted_logs"], [])

    def test_remote_is_optional_and_does_not_remove_the_local_copy(self):
        day = date(2026, 10, 14)
        calls = []

        class Result:
            returncode = 0
            stdout = b""
            stderr = b""

        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            (repo / "deploy").mkdir()
            (repo / "deploy" / "archive_remote.path").write_text("# comment\ngdrive:poly-logs\n", encoding="utf-8")
            self.assertEqual(resolve_remote(repo, {}), "gdrive:poly-logs")
            self.assertEqual(resolve_remote(repo, {"POLY_ARCHIVE_REMOTE": "s3:bucket/poly"}), "s3:bucket/poly")
            archive_day(
                repo,
                day,
                repo / "logs" / "durable",
                journal_runner=lambda _cmd: Result(),
                remote="gdrive:poly-logs",
                remote_runner=lambda cmd: calls.append(cmd) or Result(),
            )
            self.assertTrue((repo / "logs" / "durable" / "2026-10-14" / "MANIFEST.json").is_file())
            self.assertEqual(calls[0][0], "rclone")
            self.assertIn("gdrive:poly-logs/2026-10-14", calls[0])

    def test_same_size_different_bytes_keeps_the_source(self):
        day = date(2026, 10, 14)
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            source = repo / "data" / "tape" / "2026-10-14" / "bag.jsonl.gz"
            source.parent.mkdir(parents=True)
            source.write_bytes(b"AAAAAAAA")
            dest = repo / "logs" / "durable" / "2026-10-14"
            landing = dest / "tape" / "bag.jsonl.gz"
            landing.parent.mkdir(parents=True)
            landing.write_bytes(b"BBBBBBBB")
            notes = move_tape_day(repo, day, dest)
            self.assertTrue(source.is_file())
            self.assertEqual(source.read_bytes(), b"AAAAAAAA")
            self.assertEqual(landing.read_bytes(), b"BBBBBBBB")
            self.assertEqual(notes[0]["error"], "hash_mismatch")
            landing.write_bytes(b"AAAAAAAA")
            notes = move_tape_day(repo, day, dest)
            self.assertFalse(source.exists())
            self.assertTrue(notes[0]["already"])
            self.assertEqual(landing.read_bytes(), b"AAAAAAAA")

    def test_journal_command_is_the_utc_day(self):
        cmd = journal_command("polymintbot", date(2026, 10, 14))
        self.assertIn("polymintbot", cmd)
        self.assertIn("2026-10-14 00:00:00 UTC", cmd)
        self.assertIn("2026-10-15 00:00:00 UTC", cmd)


class RotationConfigTests(unittest.TestCase):
    def test_journald_does_not_delete_by_age(self):
        text = (ROOT / "deploy" / "journald-size.conf").read_text(encoding="utf-8")
        active = [line.split("#", 1)[0].strip() for line in text.splitlines()]
        self.assertNotIn("MaxRetentionSec=7day", active)
        self.assertFalse(any(line.startswith("MaxRetentionSec=") for line in active))
        self.assertTrue(any(line.startswith("SystemMaxUse=400M") for line in active))
        self.assertTrue(any(line.startswith("SystemKeepFree=2G") for line in active))

    def test_recorder_unit_is_idle_and_capped(self):
        text = (ROOT / "deploy" / "polyrecorder.service").read_text(encoding="utf-8")
        self.assertIn("Nice=19", text)
        self.assertIn("IOSchedulingClass=idle", text)
        self.assertIn("MemoryMax=150M", text)
        self.assertIn("CPUQuota=25%", text)
        self.assertIn("recorder.py", text)
        self.assertNotIn("EnvironmentFile=", text)
        self.assertNotIn("mintbot.py", text)
        timer = (ROOT / "deploy" / "polyarchive.timer").read_text(encoding="utf-8")
        self.assertIn("OnCalendar=*-*-* 00:20:00 UTC", timer)
        self.assertIn("Persistent=true", timer)
        unit = (ROOT / "deploy" / "polyarchive.service").read_text(encoding="utf-8")
        self.assertIn("Type=oneshot", unit)
        self.assertIn("Nice=19", unit)
        self.assertIn("tools/archive_logs.py", unit)
        upload = (ROOT / "deploy" / "polyupload.service").read_text(encoding="utf-8")
        self.assertIn("Nice=19", upload)
        self.assertIn("IOSchedulingClass=idle", upload)
        self.assertIn("CPUQuota=10%", upload)
        self.assertIn("MemoryMax=100M", upload)
        self.assertIn("tools/upload_tape.py", upload)
        self.assertNotIn("recorder.py", upload)
        timer = (ROOT / "deploy" / "polyupload.timer").read_text(encoding="utf-8")
        self.assertIn("OnCalendar=*:0/15", timer)
        source = (ROOT / "recorder" / "upload.py").read_text(encoding="utf-8")
        self.assertIn('"--bwlimit"', source)
        self.assertIn('"2M"', source)
        run_source = (ROOT / "recorder" / "run.py").read_text(encoding="utf-8")
        self.assertIn('REPO_ROOT / "logs" / "durable"', run_source)
        self.assertNotIn("tape_dir.parent", run_source)
        self.assertNotIn("rclone", run_source)
        self.assertIn("recorder_disk_emergency", run_source)

    def test_pathlog_and_scrapbidder_keep_a_small_rotation(self):
        expected = {"pathlog.py": "backupCount=3", "scrapbidder.py": "backupCount=2"}
        for name, count in expected.items():
            source = (ROOT / name).read_text(encoding="utf-8")
            tree = ast.parse(source)
            segment = ""
            for node in tree.body:
                if isinstance(node, ast.FunctionDef) and node.name in {"log_setup", "_setup_log"}:
                    segment = ast.get_source_segment(source, node) or ""
            self.assertIn("RotatingFileHandler", segment, name)
            self.assertNotIn("ArchiveRotatingFileHandler", segment, name)
            self.assertIn(count, segment, name)


class TapeCapTests(unittest.TestCase):
    def test_size_cap_does_not_delete_unuploaded_tape(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            spool = root / "data" / "tape"
            durable = root / "logs" / "durable"
            old_tape = durable / "2026-10-01" / "tape"
            old_tape.mkdir(parents=True)
            (old_tape / "a.jsonl.gz").write_bytes(b"x" * 100)
            (durable / "2026-10-01" / "mintbot.jsonl.gz").write_bytes(b"keep-log")
            spool_day = spool / "2026-10-02"
            spool_day.mkdir(parents=True)
            (spool_day / "b.jsonl.gz").write_bytes(b"y" * 100)
            writer = TapeWriter(spool)
            result = enforce_tape_cap(
                spool,
                durable,
                free_min=0,
                tape_max=40,
                free_bytes_fn=lambda _path: 10**12,
                writer=writer,
            )
            self.assertEqual(result["deleted"], [])
            self.assertTrue(result["over_tape_cap"])
            self.assertFalse(result["low_disk"])
            self.assertTrue(writer.writes_enabled)
            self.assertEqual((old_tape / "a.jsonl.gz").read_bytes(), b"x" * 100)
            self.assertEqual((spool_day / "b.jsonl.gz").read_bytes(), b"y" * 100)
            self.assertEqual((durable / "2026-10-01" / "mintbot.jsonl.gz").read_bytes(), b"keep-log")

    def test_low_disk_deletes_oldest_durable_then_spool_and_alerts(self):
        from recorder.disk import disk_alerts

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            spool = root / "data" / "tape"
            durable = root / "logs" / "durable"
            old_tape = durable / "2026-10-01" / "tape"
            old_tape.mkdir(parents=True)
            (old_tape / "a.jsonl.gz").write_bytes(b"x" * 100)
            (durable / "2026-10-01" / "mintbot.jsonl.gz").write_bytes(b"keep-log")
            (durable / "2026-10-01" / "journal-polymintbot.jsonl.gz").write_bytes(b"keep-journal")
            spool_day = spool / "2026-10-02"
            spool_day.mkdir(parents=True)
            (spool_day / "b.jsonl.gz").write_bytes(b"y" * 100)
            open_day = spool / "2026-10-15"
            open_day.mkdir(parents=True)
            (open_day / "c.jsonl.gz").write_bytes(b"z" * 50)
            archive = root / "logs" / "archive"
            archive.mkdir(parents=True)
            (archive / "mintbot.log.1.gz").write_bytes(b"archive")

            def free(_path: Path) -> int:
                if old_tape.exists():
                    return 0
                if spool_day.exists():
                    return 0
                return 10**12

            writer = TapeWriter(spool)
            result = enforce_tape_cap(
                spool,
                durable,
                free_min=100,
                tape_max=10**12,
                protected_days={"2026-10-15"},
                free_bytes_fn=free,
                writer=writer,
            )
            self.assertEqual(
                [(row["day"], row["where"], row["why"]) for row in result["deleted"]],
                [("2026-10-01", "durable", "low_disk"), ("2026-10-02", "spool", "low_disk")],
            )
            self.assertFalse(old_tape.exists())
            self.assertFalse(spool_day.exists())
            self.assertEqual((durable / "2026-10-01" / "mintbot.jsonl.gz").read_bytes(), b"keep-log")
            self.assertEqual((durable / "2026-10-01" / "journal-polymintbot.jsonl.gz").read_bytes(), b"keep-journal")
            self.assertEqual((open_day / "c.jsonl.gz").read_bytes(), b"z" * 50)
            self.assertEqual((archive / "mintbot.log.1.gz").read_bytes(), b"archive")
            self.assertTrue(writer.writes_enabled)
            alert = disk_alerts(result)[0]
            self.assertEqual(alert["event"], "recorder_disk_emergency")
            self.assertEqual(alert["level"], "error")
            self.assertIn("without a verified upload", alert["message"])

    def test_open_day_survives_a_full_disk_and_writes_can_resume(self):
        start = datetime(2026, 10, 15, 12, tzinfo=timezone.utc).timestamp()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            spool = root / "data" / "tape"
            durable = root / "logs" / "durable"
            writer = TapeWriter(spool, flush_s=30.0)
            writer.write(
                {
                    "event_type": "bag",
                    "recv_ts": start,
                    "slug": "btc-updown-15m-open",
                    "start_ts": start,
                    "leg": "",
                }
            )
            writer.flush(force=True)
            self.assertEqual(writer.open_days(), {"2026-10-15"})
            stopped = enforce_tape_cap(
                spool,
                durable,
                free_min=100,
                tape_max=10**12,
                free_bytes_fn=lambda _path: 0,
                writer=writer,
            )
            self.assertEqual(stopped["deleted"], [])
            self.assertFalse(writer.writes_enabled)
            self.assertTrue(next(spool.rglob("*.jsonl.gz")).is_file())
            resumed = enforce_tape_cap(
                spool,
                durable,
                free_min=100,
                tape_max=10**12,
                free_bytes_fn=lambda _path: 10**12,
                writer=writer,
            )
            self.assertTrue(resumed["writes_enabled"])
            self.assertTrue(writer.writes_enabled)
            writer.close()

    def test_age_does_not_delete_durable_tape_in_the_recorder(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            spool = root / "data" / "tape"
            durable = root / "logs" / "durable"
            old = durable / "2026-10-01" / "tape"
            old.mkdir(parents=True)
            (old / "old.jsonl.gz").write_bytes(b"old")
            (durable / "2026-10-01" / "mintbot.jsonl.gz").write_bytes(b"keep-log")
            boundary = durable / "2026-10-08" / "tape"
            boundary.mkdir(parents=True)
            (boundary / "edge.jsonl.gz").write_bytes(b"edge")
            result = enforce_tape_cap(
                spool,
                durable,
                free_min=0,
                tape_max=10**12,
                free_bytes_fn=lambda _path: 10**12,
            )
            self.assertEqual(result["deleted"], [])
            self.assertEqual((old / "old.jsonl.gz").read_bytes(), b"old")
            self.assertEqual((boundary / "edge.jsonl.gz").read_bytes(), b"edge")
            self.assertEqual((durable / "2026-10-01" / "mintbot.jsonl.gz").read_bytes(), b"keep-log")
            self.assertTrue(result["writes_enabled"])
