"""Keep rotated mintbot logs for backtests.

The live file still rolls at ``maxBytes``. Each roll is a rename into
``logs/archive/`` (or next to the live log if that directory cannot be
created). Gzip runs on one background worker so the trading loop does
not wait. Archives are never pruned. ``logs/oracle_twap.jsonl`` is not
this handler's file; its writer calls ``roll_if_over`` before each append
and lands in the same ``logs/archive/`` directory.

Warnings go to stderr. Logging them would re-enter this handler's lock.
"""

from __future__ import annotations

import errno
import gzip
import os
import shutil
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Callable, Optional


_STAMP = "%Y%m%dT%H%M%SZ"
_executor: Optional[ThreadPoolExecutor] = None
_executor_lock = threading.Lock()


def warn_archive(message: str) -> None:
    """Stderr only. Never raises, never touches the logging locks."""
    try:
        sys.stderr.write(f"warning: mintbot log archive: {message}\n")
        sys.stderr.flush()
    except Exception:
        return


def archive_stamp(when: datetime) -> str:
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    else:
        when = when.astimezone(timezone.utc)
    return when.strftime(_STAMP)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _name_taken(path: Path) -> bool:
    gz = Path(str(path) + ".gz")
    tmp = Path(str(gz) + ".tmp")
    return path.exists() or gz.exists() or tmp.exists()


def unique_archive_path(directory: Path, log_name: str, when: datetime) -> Path:
    """``log_name.<UTC stamp>`` with ``.2``, ``.3``, … if that second is taken."""
    stem = f"{log_name}.{archive_stamp(when)}"
    candidate = directory / stem
    n = 2
    while _name_taken(candidate):
        candidate = directory / f"{stem}.{n}"
        n += 1
        if n > 10_000:
            raise OSError(f"no free archive name for {stem}")
    return candidate


def rename_no_clobber(src: str, dest: Path) -> None:
    """Move ``src`` to ``dest``. Fail if ``dest`` already exists."""
    if dest.exists():
        raise FileExistsError(dest)
    try:
        os.link(src, dest)
    except FileExistsError:
        raise
    except OSError as exc:
        if exc.errno == errno.EEXIST:
            raise FileExistsError(dest) from exc
        if exc.errno not in {errno.EXDEV, errno.EPERM, errno.ENOTSUP, errno.EOPNOTSUPP}:
            raise
        os.rename(src, dest)
        return
    try:
        os.unlink(src)
    except OSError:
        try:
            os.unlink(dest)
        except OSError:
            pass
        raise


def compress_archived_log(src: str | Path) -> None:
    """Gzip ``src`` to ``src.gz`` via a temp file. Never raises.

    The uncompressed source is removed only after the ``.gz`` is flushed
    and ``os.replace``d into place. A failure leaves ``src`` on disk.
    """
    src = Path(src)
    final = Path(str(src) + ".gz")
    tmp = Path(str(final) + ".tmp")
    try:
        if not src.is_file():
            return
        with src.open("rb") as raw_in, tmp.open("wb") as raw_out:
            with gzip.GzipFile(filename="", mode="wb", fileobj=raw_out, mtime=0) as gz:
                shutil.copyfileobj(raw_in, gz)
            raw_out.flush()
            os.fsync(raw_out.fileno())
        os.replace(tmp, final)
    except Exception as exc:
        try:
            if tmp.exists():
                tmp.unlink()
        except OSError:
            pass
        warn_archive(f"compression failed for {src}: {exc}")
        return
    try:
        os.remove(src)
    except OSError as exc:
        warn_archive(f"compressed {final} but could not remove {src}: {exc}")


def _archive_executor() -> ThreadPoolExecutor:
    global _executor
    with _executor_lock:
        if _executor is None:
            _executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="mintlog-gz")
        return _executor


def flush_archive_jobs(timeout: float = 30.0) -> None:
    """Wait until gzip jobs submitted so far have finished."""
    executor = _executor
    if executor is None:
        return
    _archive_executor().submit(lambda: None).result(timeout=timeout)


class ArchiveRotatingFileHandler(RotatingFileHandler):
    """Roll at ``maxBytes`` into a timestamped archive. Never delete history.

    ``shouldRollover`` is the stock size check. ``backupCount`` is forced
    to 0 so the parent numbered-backup deletion path is not used; this
    class replaces ``doRollover`` and does not prune ``logs/archive/``.
    """

    def __init__(
        self,
        filename,
        maxBytes: int = 2_000_000,
        archive_dir: str | Path | None = None,
        clock: Callable[[], datetime] | None = None,
        encoding: str | None = None,
        delay: bool = False,
    ) -> None:
        super().__init__(
            filename,
            maxBytes=maxBytes,
            backupCount=0,
            encoding=encoding,
            delay=delay,
        )
        if archive_dir is None:
            archive_dir = Path(self.baseFilename).parent / "logs" / "archive"
        self.archive_dir = Path(archive_dir)
        self._clock = clock or _utc_now

    def doRollover(self) -> None:
        if self.stream:
            self.stream.close()
            self.stream = None
        try:
            self._archive_current()
        except Exception as exc:
            warn_archive(f"rotation failed: {exc}")
        try:
            if not self.delay:
                self.stream = self._open()
        except Exception as exc:
            warn_archive(f"reopen after rotation failed: {exc}")

    def _archive_current(self) -> None:
        archive_log_file(self.baseFilename, self.archive_dir, self._clock())


def archive_log_file(src: str | Path, archive_dir: str | Path, when: datetime) -> Optional[Path]:
    """Rename ``src`` to ``archive_dir/<name>.<UTC stamp>`` and gzip it later.

    If ``archive_dir`` cannot be created the file is renamed next to
    ``src`` and left uncompressed. Returns the renamed path, or ``None``
    when there was nothing to move or no free name.
    """
    src = str(src)
    if not os.path.isfile(src):
        return None
    directory, gzip_later = _destination_dir(Path(archive_dir), Path(src).parent)
    log_name = os.path.basename(src)
    dest: Path | None = None
    for _ in range(100):
        dest = unique_archive_path(directory, log_name, when)
        try:
            rename_no_clobber(src, dest)
        except FileExistsError:
            continue
        else:
            break
    else:
        warn_archive(f"rotation could not find a free name for {src}")
        return None
    if gzip_later and dest is not None:
        _schedule_gzip(dest)
    return dest


def _destination_dir(archive_dir: Path, fallback: Path) -> tuple[Path, bool]:
    try:
        archive_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        warn_archive(
            f"cannot create archive directory {archive_dir} ({exc}); "
            "renamed uncompressed next to the live log"
        )
        return fallback, False
    return archive_dir, True


def _schedule_gzip(dest: Path) -> None:
    try:
        _archive_executor().submit(compress_archived_log, dest)
    except Exception as exc:
        warn_archive(f"could not schedule compression of {dest}: {exc}")


def roll_if_over(
    path: str | Path,
    incoming_bytes: int,
    max_bytes: int,
    *,
    archive_dir: str | Path | None = None,
    clock: Callable[[], datetime] | None = None,
) -> Optional[Path]:
    """Archive ``path`` first if appending ``incoming_bytes`` would pass ``max_bytes``.

    Same trigger as ``RotatingFileHandler``: a non-empty file whose size
    plus the next write reaches the cap. ``max_bytes <= 0`` never rolls.
    Never raises; a failed roll leaves the file in place to keep appending.
    """
    if max_bytes <= 0:
        return None
    target = Path(path)
    try:
        size = target.stat().st_size
    except OSError:
        return None
    if size <= 0 or size + int(incoming_bytes) < max_bytes:
        return None
    if archive_dir is None:
        archive_dir = target.parent / "archive"
    try:
        return archive_log_file(target, archive_dir, (clock or _utc_now)())
    except Exception as exc:
        warn_archive(f"rotation failed for {target}: {exc}")
        return None
