"""Oldest tape days are uploaded and verified before a local delete."""

from __future__ import annotations

import hashlib
import tempfile
import unittest
from datetime import date
from pathlib import Path

from recorder.upload import resolve_tape_remote, upload_over_cap


class Result:
    def __init__(self, code: int, stdout: bytes = b"", stderr: bytes = b"") -> None:
        self.returncode = code
        self.stdout = stdout
        self.stderr = stderr


def _age(path: Path) -> None:
    import os

    os.utime(path, (1_000_000, 1_000_000))


class TapeUploadTests(unittest.TestCase):
    def _tree(self, root: Path) -> tuple[Path, Path]:
        old = root / "logs" / "durable" / "2026-10-01" / "tape"
        old.mkdir(parents=True)
        bag = old / "bag.jsonl.gz"
        bag.write_bytes(b"old-bytes")
        _age(bag)
        (root / "logs" / "durable" / "2026-10-01" / "mintbot.jsonl.gz").write_bytes(b"keep-log")
        newer = root / "data" / "tape" / "2026-10-02"
        newer.mkdir(parents=True)
        stay = newer / "stay.jsonl.gz"
        stay.write_bytes(b"newer")
        _age(stay)
        return old, newer

    def test_verified_upload_deletes_only_the_oldest_day(self):
        calls: list[list[str]] = []

        def runner(cmd: list[str]) -> Result:
            calls.append(cmd)
            return Result(0)

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            old, newer = self._tree(root)
            result = upload_over_cap(
                root,
                "gdrive:poly-tape",
                runner,
                tape_max=10,
                today=date(2026, 10, 15),
                now=2_000_000,
                protect_recent_s=120,
            )
            self.assertTrue(result["ok"])
            self.assertEqual([row["day"] for row in result["deleted"]], ["2026-10-01"])
            self.assertFalse(old.exists())
            self.assertEqual((root / "logs" / "durable" / "2026-10-01" / "mintbot.jsonl.gz").read_bytes(), b"keep-log")
            self.assertEqual((newer / "stay.jsonl.gz").read_bytes(), b"newer")
            self.assertEqual(calls[0][:4], ["rclone", "copy", str(old), "gdrive:poly-tape/2026-10-01"])
            self.assertIn("--bwlimit", calls[0])
            self.assertIn("2M", calls[0])
            self.assertEqual(calls[1][:4], ["rclone", "check", str(old), "gdrive:poly-tape/2026-10-01"])
            self.assertIn("--one-way", calls[1])
            self.assertNotIn("--size-only", calls[1])

    def test_failed_check_keeps_the_files_and_does_not_skip_ahead(self):
        calls: list[list[str]] = []

        def runner(cmd: list[str]) -> Result:
            calls.append(cmd)
            if cmd[1] == "check":
                return Result(1, b"", b"ERROR : sizes differ")
            return Result(0)

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            old, newer = self._tree(root)
            result = upload_over_cap(
                root,
                "gdrive:poly-tape",
                runner,
                tape_max=10,
                today=date(2026, 10, 15),
                now=2_000_000,
            )
            self.assertFalse(result["ok"])
            self.assertEqual(result["failed"], "2026-10-01")
            self.assertEqual(result["deleted"], [])
            self.assertEqual((old / "bag.jsonl.gz").read_bytes(), b"old-bytes")
            self.assertTrue((newer / "stay.jsonl.gz").is_file())
            self.assertEqual([cmd[1] for cmd in calls], ["copy", "check"])

    def test_md5_fallback_requires_size_and_hash(self):
        payload = b"old-bytes"
        digest = hashlib.md5(payload).hexdigest()

        def runner(cmd: list[str]) -> Result:
            if cmd[1] == "check":
                return Result(1, b"", b"hash type not supported")
            if cmd[1] == "md5sum":
                return Result(0, f"{digest}  bag.jsonl.gz\n".encode())
            if cmd[1] == "ls":
                return Result(0, f"{len(payload)} bag.jsonl.gz\n".encode())
            return Result(0)

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            old, _newer = self._tree(root)
            result = upload_over_cap(
                root,
                "gdrive:poly-tape",
                runner,
                tape_max=10,
                today=date(2026, 10, 15),
                now=2_000_000,
            )
            self.assertTrue(result["ok"])
            self.assertFalse(old.exists())

        def mismatch(cmd: list[str]) -> Result:
            if cmd[1] == "check":
                return Result(1, b"", b"hash type not supported")
            if cmd[1] == "md5sum":
                return Result(0, b"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa  bag.jsonl.gz\n")
            if cmd[1] == "ls":
                return Result(0, f"{len(payload)} bag.jsonl.gz\n".encode())
            return Result(0)

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            old, _newer = self._tree(root)
            result = upload_over_cap(
                root,
                "gdrive:poly-tape",
                mismatch,
                tape_max=10,
                today=date(2026, 10, 15),
                now=2_000_000,
            )
            self.assertFalse(result["ok"])
            self.assertIn("hash_mismatch", result["error"])
            self.assertEqual((old / "bag.jsonl.gz").read_bytes(), payload)

    def test_missing_remote_and_today_are_not_deleted(self):
        def runner(cmd: list[str]) -> Result:
            raise AssertionError(cmd)

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            old, _newer = self._tree(root)
            result = upload_over_cap(root, "", runner, tape_max=10, today=date(2026, 10, 15), now=2_000_000)
            self.assertEqual(result["error"], "no_remote")
            self.assertTrue((old / "bag.jsonl.gz").is_file())
            under = upload_over_cap(root, "", runner, tape_max=10**9, today=date(2026, 10, 15), now=2_000_000)
            self.assertTrue(under["ok"])
            self.assertEqual(under["deleted"], [])
            self.assertEqual(resolve_tape_remote(root, {}), "")
            (root / "deploy").mkdir()
            (root / "deploy" / "tape_remote.path").write_text("# c\ngdrive:poly-tape\n", encoding="utf-8")
            self.assertEqual(resolve_tape_remote(root, {}), "gdrive:poly-tape")
            self.assertEqual(resolve_tape_remote(root, {"POLY_TAPE_REMOTE": "gdrive:other"}), "gdrive:other")

        def boom(cmd: list[str]) -> Result:
            raise AssertionError(cmd)

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            today = root / "data" / "tape" / "2026-10-15"
            today.mkdir(parents=True)
            live = today / "live.jsonl.gz"
            live.write_bytes(b"x" * 50)
            _age(live)
            result = upload_over_cap(root, "gdrive:poly-tape", boom, tape_max=10, today=date(2026, 10, 15), now=2_000_000)
            self.assertEqual(result["error"], "no_deletable_day")
            self.assertEqual(live.read_bytes(), b"x" * 50)
