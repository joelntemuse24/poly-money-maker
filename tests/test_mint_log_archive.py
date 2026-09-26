"""Mintbot log rotation keeps history in logs/archive (no bot import)."""

from __future__ import annotations

import ast
import gzip
import io
import logging
import os
import tempfile
import threading
import time
import unittest
import uuid
from contextlib import redirect_stderr
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path
from unittest.mock import patch

from buy.log_archive import (
    ArchiveRotatingFileHandler,
    compress_archived_log,
    flush_archive_jobs,
)


ROOT = Path(__file__).resolve().parents[1]
MINT = ROOT / "mintbot.py"
STAMP = datetime(2026, 9, 26, 4, 9, 12, tzinfo=timezone.utc)
STAMP_TEXT = "20260926T040912Z"


class MintLogArchiveTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.addCleanup(flush_archive_jobs)
        self.root = Path(self._tmp.name)
        self.log_path = self.root / "mintbot.log"
        self.archive = self.root / "logs" / "archive"

    def _handler(
        self,
        max_bytes: int = 80,
        *,
        clock=None,
        archive_dir: Path | None = None,
    ) -> tuple[ArchiveRotatingFileHandler, logging.Logger]:
        kwargs = {"maxBytes": max_bytes}
        if clock is not None:
            kwargs["clock"] = clock
        if archive_dir is not None:
            kwargs["archive_dir"] = archive_dir
        handler = ArchiveRotatingFileHandler(self.log_path, **kwargs)
        handler.setFormatter(logging.Formatter("%(message)s"))
        logger = logging.getLogger(f"mint-archive-{uuid.uuid4()}")
        logger.setLevel(logging.INFO)
        logger.propagate = False
        logger.handlers = [handler]
        self.addCleanup(handler.close)
        self.addCleanup(logger.handlers.clear)
        return handler, logger

    def _gz_files(self) -> list[Path]:
        if not self.archive.exists():
            return []
        return sorted(p for p in self.archive.iterdir() if p.name.endswith(".gz"))

    def test_rotation_writes_timestamped_gz_with_matching_bytes(self) -> None:
        _handler, logger = self._handler(max_bytes=40, clock=lambda: STAMP)
        logger.info("kept-history")
        logger.info("x" * 80)
        flush_archive_jobs()

        gz_files = self._gz_files()
        self.assertEqual(len(gz_files), 1)
        self.assertEqual(gz_files[0].name, f"mintbot.log.{STAMP_TEXT}.gz")
        self.assertEqual(gzip.decompress(gz_files[0].read_bytes()), b"kept-history\n")
        self.assertEqual(self.log_path.read_text(encoding="utf-8"), ("x" * 80) + "\n")
        self.assertFalse(any(p.suffix == ".tmp" or p.name.endswith(".tmp") for p in self.archive.iterdir()))
        self.assertFalse((self.root / "mintbot.log.1").exists())

    def test_rotation_returns_before_gzip_finishes(self) -> None:
        started = threading.Event()
        release = threading.Event()
        real = compress_archived_log

        def slow(src):
            started.set()
            self.assertTrue(release.wait(5))
            real(src)

        _handler, logger = self._handler(max_bytes=20, clock=lambda: STAMP)
        logger.info("async-body")
        with patch("buy.log_archive.compress_archived_log", side_effect=slow):
            t0 = time.monotonic()
            logger.info("z" * 40)
            elapsed = time.monotonic() - t0
        self.assertLess(elapsed, 0.5)
        self.assertTrue(started.wait(2))
        plain = self.archive / f"mintbot.log.{STAMP_TEXT}"
        self.assertEqual(plain.read_bytes(), b"async-body\n")
        release.set()
        flush_archive_jobs()
        self.assertEqual(gzip.decompress((self.archive / f"mintbot.log.{STAMP_TEXT}.gz").read_bytes()), b"async-body\n")

    def test_repeated_rotations_keep_every_archive(self) -> None:
        _handler, logger = self._handler(max_bytes=30, clock=lambda: STAMP)
        logger.info("seed")
        bodies = []
        for i in range(4):
            bodies.append(f"block-{i}-" + ("b" * 40))
            logger.info(bodies[-1])
        flush_archive_jobs()

        gz_files = self._gz_files()
        self.assertEqual(len(gz_files), 4)
        found = {gzip.decompress(path.read_bytes()) for path in gz_files}
        self.assertIn(b"seed\n", found)
        for body in bodies[:-1]:
            self.assertIn((body + "\n").encode(), found)
        names = {path.name for path in gz_files}
        self.assertIn(f"mintbot.log.{STAMP_TEXT}.gz", names)
        self.assertIn(f"mintbot.log.{STAMP_TEXT}.2.gz", names)
        self.assertIn(f"mintbot.log.{STAMP_TEXT}.3.gz", names)
        self.assertEqual(self.log_path.read_text(encoding="utf-8"), bodies[-1] + "\n")

    def test_same_second_suffix_does_not_clobber_existing_gz(self) -> None:
        self.archive.mkdir(parents=True)
        existing = self.archive / f"mintbot.log.{STAMP_TEXT}.gz"
        existing.write_bytes(b"previous-history")
        _handler, logger = self._handler(max_bytes=20, clock=lambda: STAMP)
        logger.info("fresh")
        logger.info("y" * 40)
        flush_archive_jobs()

        self.assertEqual(existing.read_bytes(), b"previous-history")
        rolled = self.archive / f"mintbot.log.{STAMP_TEXT}.2.gz"
        self.assertTrue(rolled.is_file())
        self.assertEqual(gzip.decompress(rolled.read_bytes()), b"fresh\n")

    def test_compression_failure_keeps_uncompressed_and_warns(self) -> None:
        _handler, logger = self._handler(max_bytes=20, clock=lambda: STAMP)
        logger.info("keep-plain")
        stderr = io.StringIO()
        with patch("buy.log_archive.gzip.GzipFile", side_effect=OSError("disk full")):
            with redirect_stderr(stderr):
                logger.info("z" * 40)
                flush_archive_jobs()
        logger.info("still-logging")
        flush_archive_jobs()

        warning = stderr.getvalue()
        self.assertIn("warning:", warning)
        self.assertIn("disk full", warning)
        plain = self.archive / f"mintbot.log.{STAMP_TEXT}"
        self.assertEqual(plain.read_text(encoding="utf-8"), "keep-plain\n")
        for gz_path in self._gz_files():
            self.assertNotEqual(gzip.decompress(gz_path.read_bytes()), b"keep-plain\n")
        self.assertIn("still-logging", self.log_path.read_text(encoding="utf-8"))

    def test_compress_failure_does_not_raise_or_drop_source(self) -> None:
        self.archive.mkdir(parents=True)
        src = self.archive / f"mintbot.log.{STAMP_TEXT}"
        src.write_bytes(b"payload\n")
        stderr = io.StringIO()
        with patch("buy.log_archive.gzip.GzipFile", side_effect=OSError("boom")):
            with redirect_stderr(stderr):
                compress_archived_log(src)
        self.assertTrue(src.is_file())
        self.assertEqual(src.read_bytes(), b"payload\n")
        self.assertFalse(Path(str(src) + ".gz").exists())
        self.assertIn("boom", stderr.getvalue())

    def test_source_removed_only_after_gz_replace(self) -> None:
        self.archive.mkdir(parents=True)
        src = self.archive / f"mintbot.log.{STAMP_TEXT}"
        src.write_bytes(b"payload\n")
        real_remove = os.remove
        seen: list[Path] = []

        def spy(path):
            target = Path(path)
            seen.append(target)
            if target == src:
                final = Path(str(src) + ".gz")
                self.assertTrue(final.is_file())
                self.assertGreater(final.stat().st_size, 0)
                self.assertFalse(Path(str(final) + ".tmp").exists())
            real_remove(path)

        with patch("buy.log_archive.os.remove", side_effect=spy):
            compress_archived_log(src)
        self.assertIn(src, seen)
        self.assertFalse(src.exists())
        self.assertEqual(gzip.decompress(Path(str(src) + ".gz").read_bytes()), b"payload\n")

    def test_maxbytes_trigger_matches_stock_handler(self) -> None:
        max_bytes = 2_000_000
        prefix = b"q" * (max_bytes - 50)
        self.log_path.write_bytes(prefix)
        stock_path = self.root / "stock.log"
        stock_path.write_bytes(prefix)

        archive_handler, archive_logger = self._handler(max_bytes=max_bytes)
        self.assertEqual(archive_handler.maxBytes, 2_000_000)
        self.assertIs(
            ArchiveRotatingFileHandler.shouldRollover,
            RotatingFileHandler.shouldRollover,
        )

        stock = RotatingFileHandler(stock_path, maxBytes=max_bytes, backupCount=3)
        stock.setFormatter(logging.Formatter("%(message)s"))
        stock_logger = logging.getLogger(f"mint-archive-stock-{uuid.uuid4()}")
        stock_logger.setLevel(logging.INFO)
        stock_logger.propagate = False
        stock_logger.handlers = [stock]
        self.addCleanup(stock.close)
        self.addCleanup(stock_logger.handlers.clear)

        archive_logger.info("short")
        stock_logger.info("short")
        flush_archive_jobs()
        self.assertEqual(self._gz_files(), [])
        self.assertFalse((self.root / "stock.log.1").exists())
        self.assertLess(self.log_path.stat().st_size, max_bytes)
        self.assertGreater(self.log_path.stat().st_size, max_bytes - 50)

        archive_logger.info("R" * 80)
        stock_logger.info("R" * 80)
        flush_archive_jobs()

        self.assertTrue((self.root / "stock.log.1").is_file())
        gz_files = self._gz_files()
        self.assertEqual(len(gz_files), 1)
        self.assertRegex(gz_files[0].name, r"^mintbot\.log\.\d{8}T\d{6}Z(?:\.\d+)?\.gz$")
        archived = gzip.decompress(gz_files[0].read_bytes())
        self.assertEqual(archived, stock_path.with_name("stock.log.1").read_bytes())
        self.assertLess(self.log_path.stat().st_size, max_bytes)
        self.assertTrue(self.log_path.read_text(encoding="utf-8").startswith("R" * 80))

    def test_uncreatable_archive_dir_renames_beside_the_log(self) -> None:
        blocked = self.root / "logs" / "archive"
        blocked.parent.mkdir(parents=True)
        blocked.write_text("not-a-directory", encoding="utf-8")
        oracle = self.root / "logs" / "oracle_twap.jsonl"
        oracle.write_text('{"event":"twap"}\n', encoding="utf-8")
        _handler, logger = self._handler(max_bytes=20, clock=lambda: STAMP, archive_dir=blocked)
        logger.info("beside")
        stderr = io.StringIO()
        with redirect_stderr(stderr):
            logger.info("w" * 40)
            flush_archive_jobs()

        sibling = self.root / f"mintbot.log.{STAMP_TEXT}"
        self.assertTrue(sibling.is_file())
        self.assertEqual(sibling.read_text(encoding="utf-8"), "beside\n")
        self.assertFalse(sibling.with_name(sibling.name + ".gz").exists())
        self.assertIn("warning:", stderr.getvalue())
        self.assertEqual(oracle.read_text(encoding="utf-8"), '{"event":"twap"}\n')
        self.assertEqual(blocked.read_text(encoding="utf-8"), "not-a-directory")

    def test_oracle_tape_is_not_rotated(self) -> None:
        oracle = self.root / "logs" / "oracle_twap.jsonl"
        oracle.parent.mkdir(parents=True)
        oracle.write_text("tape-line\n", encoding="utf-8")
        _handler, logger = self._handler(max_bytes=20, clock=lambda: STAMP)
        logger.info("mint-line")
        logger.info("k" * 40)
        flush_archive_jobs()
        self.assertEqual(oracle.read_text(encoding="utf-8"), "tape-line\n")
        self.assertEqual(self._gz_files()[0].parent, self.archive)

    def test_log_setup_uses_two_megabyte_archive_handler(self) -> None:
        source = MINT.read_text(encoding="utf-8")
        self.assertIn('if float(cfg["poll_s"]) < 2:', source)
        self.assertIn('raise ValueError("poll_s must be >= 2")', source)
        tree = ast.parse(source, filename=str(MINT))
        fn = next(
            node
            for node in tree.body
            if isinstance(node, ast.FunctionDef) and node.name == "log_setup"
        )
        segment = ast.get_source_segment(source, fn)
        self.assertIsNotNone(segment)
        assert segment is not None
        self.assertIn("maxBytes=2_000_000", segment)
        self.assertNotIn("backupCount", segment)
        self.assertIn("ArchiveRotatingFileHandler", segment)

        ns: dict = {"LOG_FILE": self.log_path, "sys": __import__("sys")}
        exec(compile(ast.Module(body=[fn], type_ignores=[]), str(MINT), "exec"), ns)
        logger = logging.getLogger("mintbot")
        previous = list(logger.handlers)
        logger.handlers.clear()
        try:
            ns["log_setup"]()
            handlers = [h for h in logger.handlers if isinstance(h, ArchiveRotatingFileHandler)]
            self.assertEqual(len(handlers), 1)
            self.assertEqual(handlers[0].maxBytes, 2_000_000)
            self.assertEqual(handlers[0].archive_dir, self.log_path.parent / "logs" / "archive")
        finally:
            for handler in list(logger.handlers):
                handler.close()
            logger.handlers[:] = previous


if __name__ == "__main__":
    unittest.main()
