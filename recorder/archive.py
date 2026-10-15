"""Copy one UTC day's logs into a durable directory. Never deletes logs.

``data/tape/<day>/`` is moved into the durable tree after the copy is in
place (it is the live spool, not the archive). Mintbot logs, journal
export, shadow / price-tape files, and oracle tapes are extracted or
copied and left where they are.
"""

from __future__ import annotations

import gzip
import json
import os
import shutil
import subprocess
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Optional


SLOW_DAYS = 21


def parse_day(value: str) -> date:
    return datetime.strptime(value, "%Y-%m-%d").date()


def yesterday(now: Optional[float] = None) -> date:
    moment = datetime.now(timezone.utc) if now is None else datetime.fromtimestamp(float(now), timezone.utc)
    return (moment.date() - timedelta(days=1))


def stamp_day(name: str) -> Optional[date]:
    """UTC day encoded in ``name.YYYYmmddTHHMMSSZ``."""
    import re

    match = re.search(r"(20\d{6})T\d{6}Z", name)
    if not match:
        return None
    return datetime.strptime(match.group(1), "%Y%m%d").date()


def _mtime_day(path: Path, mtime: Optional[float] = None) -> Optional[date]:
    if mtime is None:
        try:
            mtime = path.stat().st_mtime
        except OSError:
            return None
    return datetime.fromtimestamp(float(mtime), timezone.utc).date()


def overlaps_day(path: Path, day: date, *, mtime: Optional[float] = None, slow_days: int = SLOW_DAYS) -> bool:
    """True when ``path`` might contain rows from ``day``.

    A rotated name is closed on its stamp day and may hold earlier rows,
    so stamps up to ``slow_days`` later are included and then filtered
    by timestamp. A live file is included while its mtime is on or after
    ``day`` and not more than ``slow_days`` later.
    """
    stamp = stamp_day(path.name)
    if stamp is not None:
        return day <= stamp <= day + timedelta(days=slow_days)
    modified = _mtime_day(path, mtime)
    if modified is None:
        return False
    return day <= modified <= day + timedelta(days=slow_days)


def bucket_name(path: Path) -> str:
    name = path.name
    if name.startswith("mintbot.log"):
        return "mintbot.jsonl.gz"
    if "oracle_twap" in name:
        return "oracle_twap.jsonl.gz"
    if "price_tape" in name:
        return "price_tape.jsonl.gz"
    if "shadow_early" in name:
        return "shadow_early_scrap.jsonl.gz"
    if name.startswith("books"):
        return "books.jsonl.gz"
    if name.startswith("lockbot"):
        return "lockbot.jsonl.gz"
    if name.startswith("pathlog.log"):
        return "pathlog.jsonl.gz"
    if name.startswith("scrapbid.log"):
        return "scrapbid.jsonl.gz"
    if name.startswith("recorder.log"):
        return "recorder.jsonl.gz"
    return name + ".gz" if not name.endswith(".gz") else name


def iter_sources(repo: Path) -> list[Path]:
    """Log files the daily job knows about. Missing paths are skipped."""
    names = [
        "mintbot.log",
        "pathlog.log",
        "scrapbid.log",
        "recorder.log",
        "logs/oracle_twap.jsonl",
        "logs/shadow_early_scrap.jsonl",
        "logs/price_tape.jsonl",
        "logs/books.jsonl",
        "logs/lockbot.jsonl",
    ]
    found: list[Path] = []
    for name in names:
        path = repo / name
        if path.is_file():
            found.append(path)
    for pattern in ("mintbot.log.[0-9]*", "pathlog.log.[0-9]*", "scrapbid.log.[0-9]*"):
        found.extend(path for path in repo.glob(pattern) if path.is_file())
    archive = repo / "logs" / "archive"
    if archive.is_dir():
        found.extend(path for path in sorted(archive.iterdir()) if path.is_file())
    return found


def line_ts(obj: dict) -> Optional[float]:
    for key in ("ts", "recv_ts", "exch_ts"):
        value = obj.get(key)
        try:
            parsed = float(value)
        except (TypeError, ValueError):
            continue
        if parsed == parsed and parsed > 0:
            return parsed
    return None


def in_day(ts: float, day: date) -> bool:
    start = datetime(day.year, day.month, day.day, tzinfo=timezone.utc).timestamp()
    return start <= float(ts) < start + 86400.0


def _open_text(path: Path):
    if path.name.endswith(".gz"):
        return gzip.open(path, "rt", encoding="utf-8", errors="replace")
    return path.open("rt", encoding="utf-8", errors="replace")


@dataclass
class _Sink:
    path: Path
    tmp: Path
    handle: object
    gz: gzip.GzipFile
    lines: int = 0

    def write(self, line: str) -> None:
        text = line if line.endswith("\n") else line + "\n"
        self.gz.write(text.encode("utf-8"))
        self.lines += 1

    def finish(self) -> Optional[Path]:
        self.gz.close()
        self.handle.close()
        if self.lines <= 0:
            try:
                self.tmp.unlink()
            except OSError:
                pass
            return None
        os.replace(self.tmp, self.path)
        return self.path


def _sink(dest: Path, name: str) -> _Sink:
    path = dest / name
    tmp = dest / (name + ".tmp")
    handle = open(tmp, "wb")
    gz = gzip.GzipFile(fileobj=handle, mode="wb", compresslevel=6, mtime=0)
    return _Sink(path, tmp, handle, gz)


def extract_logs(repo: Path, day: date, dest: Path) -> list[dict]:
    """Filter known logs into ``dest``. Sources are not removed."""
    dest.mkdir(parents=True, exist_ok=True)
    sinks: dict[str, _Sink] = {}
    notes: list[dict] = []
    for path in iter_sources(repo):
        if not overlaps_day(path, day):
            continue
        modified = _mtime_day(path)
        stamp = stamp_day(path.name)
        whole_file = stamp == day or modified == day
        name = bucket_name(path)
        try:
            with _open_text(path) as handle:
                for line in handle:
                    if not line.strip():
                        continue
                    include = False
                    try:
                        obj = json.loads(line)
                    except json.JSONDecodeError:
                        obj = None
                    if isinstance(obj, dict):
                        ts = line_ts(obj)
                        if ts is not None:
                            include = in_day(ts, day)
                        elif whole_file:
                            include = True
                    elif whole_file:
                        include = True
                    if not include:
                        continue
                    sink = sinks.get(name)
                    if sink is None:
                        sink = _sink(dest, name)
                        sinks[name] = sink
                    sink.write(line)
        except OSError as exc:
            notes.append({"path": str(path), "error": str(exc)[:200]})
    for name, sink in sinks.items():
        final = sink.finish()
        notes.append({"bucket": name, "lines": sink.lines, "path": None if final is None else str(final)})
    return notes


def move_tape_day(repo: Path, day: date, dest: Path) -> list[dict]:
    """Move ``data/tape/<day>`` into ``dest/tape``. Leave other days put."""
    source = repo / "data" / "tape" / day.isoformat()
    if not source.is_dir():
        return []
    target = dest / "tape"
    target.mkdir(parents=True, exist_ok=True)
    notes = []
    for path in sorted(source.iterdir()):
        if not path.is_file():
            continue
        landing = target / path.name
        if landing.exists() and landing.stat().st_size == path.stat().st_size:
            path.unlink()
            notes.append({"moved": str(landing), "already": True})
            continue
        if landing.exists():
            landing = target / f"{path.name}.dup"
        shutil.copy2(path, landing)
        if landing.is_file() and landing.stat().st_size == path.stat().st_size:
            path.unlink()
            notes.append({"moved": str(landing), "bytes": landing.stat().st_size})
        else:
            notes.append({"path": str(path), "error": "copy_size_mismatch"})
    try:
        if source.is_dir() and not any(source.iterdir()):
            source.rmdir()
    except OSError:
        pass
    return notes


def journal_command(unit: str, day: date) -> list[str]:
    nxt = day + timedelta(days=1)
    return [
        "journalctl",
        "-u",
        unit,
        "--since",
        f"{day.isoformat()} 00:00:00 UTC",
        "--until",
        f"{nxt.isoformat()} 00:00:00 UTC",
        "-o",
        "json",
        "--no-pager",
    ]


def export_journal(day: date, dest: Path, *, unit: str = "polymintbot", runner: Optional[Callable] = None) -> dict:
    """Gzip one day's journal. A permission error is reported, not raised."""
    cmd = journal_command(unit, day)
    dest.mkdir(parents=True, exist_ok=True)
    out_path = dest / f"journal-{unit}.jsonl.gz"
    if runner is not None:
        result = runner(cmd)
        code = int(getattr(result, "returncode", 1))
        stdout = getattr(result, "stdout", b"") or b""
        stderr = getattr(result, "stderr", b"") or b""
        if isinstance(stdout, str):
            stdout = stdout.encode("utf-8")
        if isinstance(stderr, str):
            stderr = stderr.encode("utf-8")
        if code != 0:
            return {"ok": False, "error": stderr.decode("utf-8", "replace")[:500]}
        if not stdout.strip():
            return {"ok": True, "empty": True, "bytes": 0}
        tmp = dest / (out_path.name + ".tmp")
        with open(tmp, "wb") as raw:
            with gzip.GzipFile(fileobj=raw, mode="wb", compresslevel=6, mtime=0) as gz:
                gz.write(stdout)
        os.replace(tmp, out_path)
        return {"ok": True, "path": str(out_path), "bytes": out_path.stat().st_size}
    try:
        proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
    except OSError as exc:
        return {"ok": False, "error": str(exc)[:500]}
    if proc.returncode != 0:
        return {"ok": False, "error": proc.stderr.decode("utf-8", "replace")[:500]}
    if not proc.stdout.strip():
        return {"ok": True, "empty": True, "bytes": 0}
    tmp = dest / (out_path.name + ".tmp")
    with open(tmp, "wb") as raw:
        with gzip.GzipFile(fileobj=raw, mode="wb", compresslevel=6, mtime=0) as gz:
            gz.write(proc.stdout)
    os.replace(tmp, out_path)
    return {"ok": True, "path": str(out_path), "bytes": out_path.stat().st_size}


def resolve_remote(repo: Path, environ: Optional[dict] = None) -> str:
    env = os.environ if environ is None else environ
    value = str(env.get("POLY_ARCHIVE_REMOTE") or "").strip()
    if value:
        return value
    pointer = repo / "deploy" / "archive_remote.path"
    try:
        text = pointer.read_text(encoding="utf-8")
    except OSError:
        return ""
    for line in text.splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            return line
    return ""


def push_remote(local_dir: Path, remote: str, day: date, runner: Callable) -> dict:
    target = remote.rstrip("/") + "/" + day.isoformat()
    cmd = ["rclone", "copy", str(local_dir), target, "--transfers", "2", "--checkers", "2"]
    result = runner(cmd)
    code = int(getattr(result, "returncode", 1))
    stderr = getattr(result, "stderr", b"") or b""
    if isinstance(stderr, bytes):
        stderr = stderr.decode("utf-8", "replace")
    return {"ok": code == 0, "target": target, "error": "" if code == 0 else str(stderr)[:500]}


def archive_day(
    repo: Path,
    day: date,
    dest_root: Path,
    *,
    unit: str = "polymintbot",
    journal_runner: Optional[Callable] = None,
    remote: str = "",
    remote_runner: Optional[Callable] = None,
) -> dict:
    """Build ``dest_root/<day>/``. Returns a manifest. Does not delete logs."""
    dest = dest_root / day.isoformat()
    dest.mkdir(parents=True, exist_ok=True)
    logs = extract_logs(repo, day, dest)
    tape = move_tape_day(repo, day, dest)
    journal = export_journal(day, dest, unit=unit, runner=journal_runner)
    remote_note = None
    if remote:
        if remote_runner is None:
            remote_note = push_remote(dest, remote, day, _subprocess_runner)
        else:
            remote_note = push_remote(dest, remote, day, remote_runner)
    manifest = {
        "day": day.isoformat(),
        "dest": str(dest),
        "logs": logs,
        "tape": tape,
        "journal": journal,
        "remote": remote_note,
        "deleted_logs": [],
    }
    (dest / "MANIFEST.json").write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return manifest


def _subprocess_runner(cmd: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
