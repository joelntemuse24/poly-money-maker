"""Small helpers shared by the recorder modules."""

from __future__ import annotations

import datetime as _dt
import os
import re

# Sentinel so a parsed JSON null can still be stored under "raw".
NO_RAW = object()

_DAY_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def make_record(src, tw, tm, ts_exch=None, raw=NO_RAW, raw_s=None):
    """Build one output record with receipt timestamps captured at recv()."""
    rec = {
        "src": src,
        "ts_recv_wall": tw,
        "ts_recv_mono": tm,
        "ts_exch": ts_exch,
    }
    if raw is not NO_RAW:
        rec["raw"] = raw
    elif raw_s is not None:
        rec["raw_s"] = raw_s
    return rec


def as_ms(value):
    """Convert an exchange millisecond timestamp to int, or return None."""
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        try:
            return int(float(value))
        except (TypeError, ValueError):
            return None


def ts_to_ms(value):
    """Normalise a seconds or milliseconds timestamp to milliseconds."""
    ms = as_ms(value)
    if ms is None:
        return None
    return ms if ms > 10**12 else ms * 1000


def iso_to_ms(value):
    """Convert an ISO-8601 exchange timestamp to milliseconds."""
    if not value:
        return None
    try:
        text = value.replace("Z", "+00:00")
        return int(_dt.datetime.fromisoformat(text).timestamp() * 1000)
    except (TypeError, ValueError):
        return None


def utc_day_hour():
    """Return the current UTC day and hour tokens used in file paths."""
    now = _dt.datetime.now(_dt.timezone.utc)
    return now.strftime("%Y-%m-%d"), now.strftime("%Y%m%dT%H")


def utc_day():
    return _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%d")


def is_day_dir(name):
    return bool(_DAY_RE.match(name))


def tree_bytes(root):
    """Total size of regular files below root."""
    total = 0
    for dirpath, _dirnames, filenames in os.walk(root):
        for name in filenames:
            try:
                total += os.path.getsize(os.path.join(dirpath, name))
            except OSError:
                continue
    return total
