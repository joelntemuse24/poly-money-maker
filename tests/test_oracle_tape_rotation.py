"""logs/oracle_twap.jsonl rolls into logs/archive like mintbot.log (#219)."""

from __future__ import annotations

import ast
import gzip
import io
import json
import re
import tempfile
import unittest
from contextlib import redirect_stderr
from datetime import datetime, timezone
from pathlib import Path

from buy.log_archive import flush_archive_jobs, roll_if_over
from buy.oracle_log import TAPE_MAX_BYTES, OracleLogService, append_jsonl


ROOT = Path(__file__).resolve().parents[1]
MINT = ROOT / "mintbot.py"
STAMP = datetime(2026, 9, 30, 8, 0, 0, tzinfo=timezone.utc)
GZ_NAME = re.compile(r"^oracle_twap\.jsonl\.\d{8}T\d{6}Z(?:\.\d+)?\.gz$")


class OracleTapeRotationTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.addCleanup(flush_archive_jobs)
        self.logs = Path(self._tmp.name) / "logs"
        self.tape = self.logs / "oracle_twap.jsonl"
        self.archive = self.logs / "archive"

    def _gz(self) -> list[Path]:
        if not self.archive.exists():
            return []
        return sorted(p for p in self.archive.iterdir() if p.name.endswith(".gz"))

    def test_under_the_cap_does_not_roll(self):
        self.logs.mkdir(parents=True)
        self.tape.write_bytes(b"x" * 10)
        self.assertIsNone(roll_if_over(self.tape, 5, 100, clock=lambda: STAMP))
        self.assertIsNone(roll_if_over(self.tape, 5, 0, clock=lambda: STAMP))
        self.assertEqual(self.tape.read_bytes(), b"x" * 10)
        self.assertFalse(self.archive.exists())

    def test_missing_or_empty_tape_does_not_roll(self):
        self.assertIsNone(roll_if_over(self.tape, 500, 100))
        self.logs.mkdir(parents=True)
        self.tape.write_bytes(b"")
        self.assertIsNone(roll_if_over(self.tape, 500, 100))
        self.assertFalse(self.archive.exists())

    def test_over_the_cap_moves_to_archive_and_gzips(self):
        self.logs.mkdir(parents=True)
        self.tape.write_bytes(b"old-rows\n")
        dest = roll_if_over(self.tape, 95, 100, clock=lambda: STAMP)
        flush_archive_jobs()
        self.assertEqual(dest, self.archive / "oracle_twap.jsonl.20260930T080000Z")
        self.assertFalse(self.tape.exists())
        gz = self._gz()
        self.assertEqual([p.name for p in gz], ["oracle_twap.jsonl.20260930T080000Z.gz"])
        self.assertEqual(gzip.decompress(gz[0].read_bytes()), b"old-rows\n")

    def test_append_rolls_and_keeps_every_row(self):
        rows = [{"event": "oracle_twap", "i": i, "pad": "p" * 40} for i in range(12)]
        for row in rows:
            append_jsonl(self.tape, row, max_bytes=200)
        flush_archive_jobs()
        gz = self._gz()
        self.assertGreater(len(gz), 1)
        for path in gz:
            self.assertRegex(path.name, GZ_NAME)
        self.assertLess(self.tape.stat().st_size, 200)
        seen = []
        for path in sorted(gz, key=lambda p: p.stat().st_mtime_ns):
            seen.extend(gzip.decompress(path.read_bytes()).decode().splitlines())
        seen.extend(self.tape.read_text(encoding="utf-8").splitlines())
        self.assertEqual(sorted(json.loads(line)["i"] for line in seen), list(range(12)))
        self.assertEqual(len(seen), 12)

    def test_zero_cap_is_the_old_unbounded_append(self):
        for i in range(5):
            append_jsonl(self.tape, {"i": i, "pad": "p" * 80})
        self.assertFalse(self.archive.exists())
        self.assertEqual(len(self.tape.read_text(encoding="utf-8").splitlines()), 5)

    def test_uncreatable_archive_dir_renames_beside_and_keeps_appending(self):
        self.logs.mkdir(parents=True)
        self.archive.write_text("not-a-directory", encoding="utf-8")
        self.tape.write_bytes(b"first\n")
        stderr = io.StringIO()
        with redirect_stderr(stderr):
            append_jsonl(self.tape, {"i": 1, "pad": "p" * 80}, max_bytes=50)
            flush_archive_jobs()
        beside = [p for p in self.logs.iterdir() if p.name.startswith("oracle_twap.jsonl.")]
        self.assertEqual(len(beside), 1)
        self.assertEqual(beside[0].read_bytes(), b"first\n")
        self.assertIn("warning:", stderr.getvalue())
        self.assertEqual(json.loads(self.tape.read_text(encoding="utf-8"))["i"], 1)
        self.assertEqual(self.archive.read_text(encoding="utf-8"), "not-a-directory")

    def test_service_rolls_at_its_cap(self):
        self.assertEqual(TAPE_MAX_BYTES, 20_000_000)
        service = OracleLogService(self.tape, feed=object(), fetch_price=lambda _s: None, max_bytes=150)
        self.assertEqual(service.max_bytes, 150)
        for i in range(6):
            service._append({"event": "oracle_twap", "i": i, "pad": "p" * 40})
        flush_archive_jobs()
        self.assertGreater(len(self._gz()), 0)
        default = OracleLogService(self.tape, feed=object(), fetch_price=lambda _s: None)
        self.assertEqual(default.max_bytes, TAPE_MAX_BYTES)

    def test_mintbot_tape_uses_the_default_cap_and_same_archive_dir(self):
        source = MINT.read_text(encoding="utf-8")
        self.assertIn("OracleLogService(ORACLE_LOG_FILE)", source)
        tree = ast.parse(source)
        paths = {
            node.targets[0].id: ast.get_source_segment(source, node.value)
            for node in tree.body
            if isinstance(node, ast.Assign)
            and isinstance(node.targets[0], ast.Name)
            and node.targets[0].id in {"ORACLE_LOG_FILE", "LOG_FILE"}
        }
        self.assertEqual(paths["ORACLE_LOG_FILE"], 'REPO / "logs" / "oracle_twap.jsonl"')
        self.assertEqual(paths["LOG_FILE"], 'REPO / "mintbot.log"')


if __name__ == "__main__":
    unittest.main()
