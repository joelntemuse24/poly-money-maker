"""Upload oldest tape days once the spool is over the size cap.

This is not called from the recorder write path. A separate low-priority
process runs it. A day is removed locally only after ``rclone check``
(or a size plus MD5 fallback) says the remote copy matches. A failed
copy or check leaves the files in place so the next run can retry.
"""

from __future__ import annotations

import hashlib
import os
import subprocess
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Callable, Optional

from recorder.disk import TAPE_MAX_BYTES, _remove_tree, tape_bytes, tape_days


BWLIMIT = "2M"
PROTECT_RECENT_S = 120.0


def resolve_tape_remote(repo: Path, environ: Optional[dict] = None) -> str:
    """``POLY_TAPE_REMOTE`` or the first line of ``deploy/tape_remote.path``."""
    env = os.environ if environ is None else environ
    value = str(env.get("POLY_TAPE_REMOTE") or "").strip()
    if value:
        return value
    pointer = repo / "deploy" / "tape_remote.path"
    try:
        text = pointer.read_text(encoding="utf-8")
    except OSError:
        return ""
    for line in text.splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            return line
    return ""


def remote_for_day(remote: str, day: str, kind: str = "") -> str:
    base = remote.rstrip("/") + "/" + day
    if kind:
        return base + "/" + kind
    return base


def copy_command(local: Path, remote: str, *, bwlimit: str = BWLIMIT) -> list[str]:
    return [
        "rclone",
        "copy",
        str(local),
        remote,
        "--bwlimit",
        bwlimit,
        "--transfers",
        "1",
        "--checkers",
        "2",
    ]


def check_command(local: Path, remote: str) -> list[str]:
    return ["rclone", "check", str(local), remote, "--one-way"]


def _run(runner: Callable, cmd: list[str]) -> tuple[int, str, str]:
    try:
        result = runner(cmd)
    except OSError as exc:
        return 1, "", str(exc)[:500]
    code = int(getattr(result, "returncode", 1))
    stdout = getattr(result, "stdout", b"") or b""
    stderr = getattr(result, "stderr", b"") or b""
    if isinstance(stdout, bytes):
        stdout = stdout.decode("utf-8", "replace")
    if isinstance(stderr, bytes):
        stderr = stderr.decode("utf-8", "replace")
    return code, stdout, stderr


def _hash_unsupported(text: str) -> bool:
    lowered = text.lower()
    return "hash" in lowered and ("not supported" in lowered or "unsupported" in lowered or "no hash" in lowered)


def _md5(path: Path) -> str:
    digest = hashlib.md5()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(64 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _parse_md5sum(text: str) -> dict[str, str]:
    found: dict[str, str] = {}
    for line in text.splitlines():
        parts = line.strip().split(None, 1)
        if len(parts) == 2 and len(parts[0]) == 32:
            found[parts[1].strip()] = parts[0].lower()
    return found


def _parse_ls(text: str) -> dict[str, int]:
    found: dict[str, int] = {}
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        size, _, name = stripped.partition(" ")
        try:
            found[name.strip()] = int(size)
        except ValueError:
            continue
    return found


def _local_files(local: Path) -> list[Path]:
    if not local.is_dir():
        return []
    return sorted(path for path in local.rglob("*") if path.is_file())


def verify_tree(local: Path, remote: str, runner: Callable) -> dict:
    """Size and hash via ``rclone check``, or MD5 plus ``rclone ls`` sizes."""
    code, out, err = _run(runner, check_command(local, remote))
    if code == 0:
        return {"ok": True, "method": "rclone_check"}
    detail = (err or out)[:500]
    if not _hash_unsupported(err + "\n" + out):
        return {"ok": False, "method": "rclone_check", "error": detail or "check_failed"}
    md5_code, md5_out, md5_err = _run(runner, ["rclone", "md5sum", remote])
    if md5_code != 0:
        return {"ok": False, "method": "md5", "error": (md5_err or md5_out)[:500]}
    ls_code, ls_out, ls_err = _run(runner, ["rclone", "ls", remote])
    if ls_code != 0:
        return {"ok": False, "method": "md5", "error": (ls_err or ls_out)[:500]}
    hashes = _parse_md5sum(md5_out)
    sizes = _parse_ls(ls_out)
    for path in _local_files(local):
        rel = path.relative_to(local).as_posix()
        digest = _md5(path)
        try:
            size = path.stat().st_size
        except OSError as exc:
            return {"ok": False, "method": "md5", "error": str(exc)[:200]}
        if hashes.get(rel) != digest or sizes.get(rel) != size:
            return {"ok": False, "method": "md5", "error": "hash_mismatch " + rel}
    return {"ok": True, "method": "md5"}


def _sources(repo: Path, day: str) -> list[tuple[str, Path]]:
    found: list[tuple[str, Path]] = []
    spool = repo / "data" / "tape" / day
    durable = repo / "logs" / "durable" / day / "tape"
    if _local_files(spool):
        found.append(("spool", spool))
    if _local_files(durable):
        found.append(("durable", durable))
    return found


def _recent(path: Path, now: float, window: float) -> bool:
    if window <= 0:
        return False
    for file_path in _local_files(path):
        try:
            if now - file_path.stat().st_mtime < window:
                return True
        except OSError:
            return True
    return False


def _skip_reason(repo: Path, day: str, today: date, now: float, protect_recent_s: float) -> str:
    if day == today.isoformat():
        return "today"
    for _kind, path in _sources(repo, day):
        if _recent(path, now, protect_recent_s):
            return "recent"
    return ""


def _delete_day(repo: Path, day: str) -> None:
    _remove_tree(repo / "data" / "tape" / day)
    _remove_tree(repo / "logs" / "durable" / day / "tape")


def upload_day(repo: Path, day: str, remote: str, runner: Callable, *, bwlimit: str = BWLIMIT) -> dict:
    """Copy one day and verify it. Does not delete local files."""
    sources = _sources(repo, day)
    if not sources:
        return {"ok": True, "day": day, "empty": True, "verified": []}
    both = len(sources) > 1
    verified = []
    for kind, path in sources:
        target = remote_for_day(remote, day, kind if both else "")
        code, _out, err = _run(runner, copy_command(path, target, bwlimit=bwlimit))
        if code != 0:
            return {"ok": False, "day": day, "error": (err or "copy_failed")[:500], "verified": verified}
        check = verify_tree(path, target, runner)
        if not check.get("ok"):
            return {"ok": False, "day": day, "error": check.get("error") or "verify_failed", "verified": verified}
        verified.append({"kind": kind, "remote": target, "method": check.get("method")})
    return {"ok": True, "day": day, "verified": verified}


def upload_over_cap(
    repo: Path,
    remote: str,
    runner: Callable,
    *,
    tape_max: int = TAPE_MAX_BYTES,
    today: Optional[date] = None,
    now: Optional[float] = None,
    protect_recent_s: float = PROTECT_RECENT_S,
    bwlimit: str = BWLIMIT,
) -> dict:
    """Upload oldest days until tape bytes are at or under ``tape_max``.

    Stops on the first copy or verify failure and leaves that day on disk.
    """
    day = today or datetime.now(timezone.utc).date()
    moment = time_now() if now is None else float(now)
    spool = repo / "data" / "tape"
    durable = repo / "logs" / "durable"
    deleted: list[dict] = []
    used = tape_bytes(spool, durable)
    if used <= int(tape_max):
        return {"ok": True, "deleted": deleted, "tape_bytes": used}
    if not str(remote or "").strip():
        return {"ok": False, "error": "no_remote", "deleted": deleted, "tape_bytes": used}
    while tape_bytes(spool, durable) > int(tape_max):
        chosen = ""
        for name in tape_days(spool, durable):
            if not _sources(repo, name):
                continue
            if _skip_reason(repo, name, day, moment, protect_recent_s):
                continue
            chosen = name
            break
        if not chosen:
            return {
                "ok": False,
                "error": "no_deletable_day",
                "deleted": deleted,
                "tape_bytes": tape_bytes(spool, durable),
            }
        uploaded = upload_day(repo, chosen, remote.strip(), runner, bwlimit=bwlimit)
        if not uploaded.get("ok"):
            return {
                "ok": False,
                "error": uploaded.get("error") or "upload_failed",
                "failed": chosen,
                "deleted": deleted,
                "tape_bytes": tape_bytes(spool, durable),
            }
        if uploaded.get("empty"):
            return {
                "ok": False,
                "error": "empty_day",
                "failed": chosen,
                "deleted": deleted,
                "tape_bytes": tape_bytes(spool, durable),
            }
        try:
            _delete_day(repo, chosen)
        except OSError as exc:
            return {
                "ok": False,
                "error": str(exc)[:500],
                "failed": chosen,
                "deleted": deleted,
                "tape_bytes": tape_bytes(spool, durable),
            }
        if _sources(repo, chosen):
            return {
                "ok": False,
                "error": "delete_failed",
                "failed": chosen,
                "deleted": deleted,
                "tape_bytes": tape_bytes(spool, durable),
            }
        deleted.append({"day": chosen, "verified": uploaded.get("verified")})
    return {"ok": True, "deleted": deleted, "tape_bytes": tape_bytes(spool, durable)}


def time_now() -> float:
    return datetime.now(timezone.utc).timestamp()


def subprocess_runner(cmd: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
