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
    overlaps_day,
    resolve_remote,
)


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
        self.assertIn("Nice=10", text)
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

    def test_pathlog_and_scrapbidder_archive_instead_of_deleting(self):
        for name in ("pathlog.py", "scrapbidder.py"):
            tree = ast.parse((ROOT / name).read_text(encoding="utf-8"))
            segment = ""
            for node in tree.body:
                if isinstance(node, ast.FunctionDef) and node.name in {"log_setup", "_setup_log"}:
                    segment = ast.get_source_segment((ROOT / name).read_text(encoding="utf-8"), node) or ""
            self.assertIn("ArchiveRotatingFileHandler", segment, name)
            self.assertNotIn("backupCount", segment, name)
