"""Compact JSONL tape of 5m book snapshots and order events; logging only.

One file per UTC day under ``logs/fivemin_books``. A failure is swallowed via
``_fail`` and never changes an order.
"""
from __future__ import annotations

import json
import math
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

RECORD_EVENTS = {
    "fivemin_signal", "fivemin_signal_blocked", "fivemin_momentum_block",
    "fivemin_momentum_pass_headroom", "fivemin_min_fill_skip", "fivemin_fill",
    "fivemin_signal_nofill", "fivemin_order_error", "fivemin_side98_fill",
}

_PRUNE_EVERY_S = 86400.0
_KEEP_DAYS = 14
_MIN_WRITE_S = 0.2
_FLUSH_S = 5.0
_EVENT_CAP = 50
_EVENT_KEYS = ("slug", "side", "avg", "price", "reason", "adverse_30s_usd", "headroom", "shares")


def _num(value):
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if math.isfinite(out) else None


def _level(level):
    if isinstance(level, dict):
        price, size = level.get("price"), level.get("size")
    elif isinstance(level, (list, tuple)):
        try:
            price, size = level[0], level[1]
        except IndexError:
            return None
    else:
        price, size = getattr(level, "price", None), getattr(level, "size", None)
    price, size = _num(price), _num(size)
    return None if price is None or size is None else [round(price, 4), round(size, 2)]


def _top5(levels, *, highest=False):
    out = [level for level in (_level(item) for item in (levels or ())) if level is not None]
    out.sort(key=lambda item: item[0], reverse=highest)
    return out[:5]


def _side(quotes):
    return {"b": _top5(quotes.get("bids"), highest=True), "a": _top5(quotes.get("asks"))}


def _freeze(book):
    return tuple((name, tuple(map(tuple, book[name]["b"])), tuple(map(tuple, book[name]["a"])))
                 for name in ("up", "dn"))


def _scalars(fields):
    return {key: value for key, value in fields.items()
            if isinstance(value, (str, int, float, bool))}


class BookTape:
    """Append-only book tape. Every public method swallows its own errors."""

    def __init__(self, root: Path, log=lambda *a, **k: None):
        self.root = Path(root)
        self.log = log
        self.enabled = True
        self._handle = None
        self._day = None
        self._failed = False
        self._markets: dict[str, dict] = {}
        self._sampled: set[str] = set()
        self._events: dict[str, list] = {}
        self._last_flush = 0.0
        self._last_prune = 0.0

    def _fail(self, exc):
        if self._failed:
            return
        self._failed = True
        try:
            self.log("fivemin_book_tape_error", error=str(exc)[:240])
        except Exception:
            pass

    def _close_file(self):
        handle, self._handle = self._handle, None
        self._day = None
        if handle is not None:
            try:
                handle.close()
            except Exception:
                pass

    def _file(self, now):
        stamp = _num(now)
        day = datetime.fromtimestamp(time.time() if stamp is None else stamp,
                                    timezone.utc).date().isoformat()
        if self._handle is not None and day == self._day:
            return self._handle
        self._close_file()
        self.root.mkdir(parents=True, exist_ok=True)
        self._handle = open(self.root / f"{day}.jsonl", "a", encoding="utf-8", buffering=65536)
        self._day = day
        return self._handle

    def _write(self, now, line):
        self._file(now).write(json.dumps(line, separators=(",", ":")) + "\n")

    def _flush(self):
        handle = self._handle
        if handle is not None:
            handle.flush()

    def sample(self, cfg, now, rows, quotes, grid, strategy):
        try:
            self.enabled = bool(cfg.get("fivemin_book_record", True))
            if not self.enabled:
                return
            from buy.fivemin import gate_ratio, twap60

            window = float(cfg.get("fivemin_book_record_window_s", 150))
            rows = list(rows or ())
            for row in rows:
                ttm = row["end"] - now
                if not 0 < ttm <= window:
                    continue
                cid = row["condition_id"]
                self._sampled.add(cid)
                sides = quotes.get(cid) or {}
                book = {"up": _side(sides.get("up") or {}), "dn": _side(sides.get("down") or {})}

                g = math.floor(now * 10) - 5
                spot = grid.price_at(g)
                p30 = grid.price_at(g - 300)
                p10 = grid.price_at(g - 100)
                tw = twap60(grid, g)
                offset = row.get("offset")
                proxy = tw + offset if tw is not None and offset is not None else None
                strike = row.get("strike")
                fav = "up" if (proxy is not None and strike is not None and proxy >= strike) else "down"
                ratio = (gate_ratio(proxy, strike, fav, ttm)
                         if proxy is not None and strike is not None else None)
                spot, p30, p10 = _num(spot), _num(p30), _num(p10)
                d30 = None if spot is None or p30 is None else spot - p30
                d10 = None if spot is None or p10 is None else spot - p10

                signature = (_freeze(book), spot)
                market = self._markets.setdefault(
                    cid, {"slug": row["slug"], "last_write": float("-inf"), "signature": None})
                market["slug"] = row["slug"]
                if signature == market["signature"] or now - market["last_write"] < _MIN_WRITE_S:
                    continue
                market["signature"] = signature
                market["last_write"] = now
                self._write(now, {
                    "t": "s", "ts": round(now, 3), "slug": row["slug"], "ttm": round(ttm, 3),
                    "strike": strike, "spot": spot, "proxy": proxy, "fav": fav,
                    "gate_ratio": ratio, "d30": d30, "d10": d10,
                    "up": book["up"], "dn": book["dn"],
                })

            by_cid = {row["condition_id"]: row for row in rows}
            for cid in list(self._sampled):
                row = by_cid.get(cid)
                if row is None or row["end"] - now <= 0:
                    self._end(cid, now)

            if now - self._last_flush >= _FLUSH_S:
                self._last_flush = now
                self._flush()
        except Exception as exc:
            self._fail(exc)

    def _end(self, cid, now):
        self._sampled.discard(cid)
        market = self._markets.pop(cid, None)
        if market is None:
            return
        slug = market.get("slug")
        events = self._events.pop(slug, []) if slug is not None else []
        self._write(now, {"t": "end", "ts": round(now, 3), "slug": slug, "events": events})
        self._flush()

    def event(self, name, fields):
        try:
            if not self.enabled:
                return
            fields = dict(fields or {})
            if "slug" not in fields:
                return
            slug = fields["slug"]
            ts = fields.get("ts") or time.time()
            if _num(ts) is None:
                ts = time.time()
            scalars = _scalars(fields)
            subset = {key: scalars[key] for key in _EVENT_KEYS if key in scalars}
            events = self._events.setdefault(slug, [])
            events.append({"ev": name, "ts": ts, **subset})
            del events[:-_EVENT_CAP]
            self._write(ts, {"t": "e", "ev": name, "ts": ts, **subset})
        except Exception as exc:
            self._fail(exc)

    def prune(self, now):
        try:
            if now - self._last_prune < _PRUNE_EVERY_S:
                return
            self._last_prune = now
            cutoff = datetime.fromtimestamp(now, timezone.utc).date() - timedelta(days=_KEEP_DAYS)
            for path in self.root.glob("*.jsonl"):
                try:
                    day = date.fromisoformat(path.stem)
                except ValueError:
                    continue
                if day < cutoff:
                    path.unlink()
        except Exception as exc:
            self._fail(exc)

    def close(self):
        try:
            self._flush()
        except Exception:
            pass
        self._close_file()
