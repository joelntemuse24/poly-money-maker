"""WhatsApp alerts via CallMeBot when a loser scrap (or, opt-in, a held dump) fills.

Alerts only. Nothing here reads or writes intent state, and nothing here can
block the sell loop: ``send`` is a non-blocking put on a bounded queue, and
one daemon worker does the HTTP GET. Every entry point swallows its own
exceptions.

Secrets: ``CALLMEBOT_PHONE`` and ``CALLMEBOT_APIKEY`` come from the process
environment (systemd ``EnvironmentFile=.env``). The API key and the request
URL are never logged; error text is passed through ``redact`` first.
"""

from __future__ import annotations

import queue
import re
import threading
import time
from datetime import datetime, timezone
from typing import Any, Callable, Mapping, Optional
from urllib.parse import quote, quote_plus

CALLMEBOT_URL = "https://api.callmebot.com/whatsapp.php"
ENV_PHONE = "CALLMEBOT_PHONE"
ENV_APIKEY = "CALLMEBOT_APIKEY"
HTTP_TIMEOUT_S = 8.0
RETRY_DELAY_S = 5.0
QUEUE_MAX = 50
BAG_TZ = "Europe/Dublin"
# A window-end partial scrap is only announced this soon after the end, so a
# restart does not re-announce old bags still sitting in state.
WINDOW_END_NOTIFY_S = 60.0
MIN_SOLD = 0.01

LogFn = Callable[..., None]
HttpGet = Callable[..., Any]

_APIKEY_PARAM = re.compile(r"(apikey=)[^&\s'\"]+", re.IGNORECASE)
_URL = re.compile(r"https?://[^\s'\"<>)]+", re.IGNORECASE)


def redact(text: Any, *secrets: Optional[str]) -> str:
    """Strip secrets (raw and URL-encoded), any ``apikey=`` value, and URLs."""
    out = str(text if text is not None else "")
    for secret in secrets:
        if not secret:
            continue
        for form in {secret, quote(secret, safe=""), quote_plus(secret)}:
            if form:
                out = out.replace(form, "***")
    out = _URL.sub("<url>", out)
    return _APIKEY_PARAM.sub(r"\1***", out)


def _default_get(url: str, *, params: dict, timeout: float) -> Any:
    import requests

    return requests.get(url, params=params, timeout=timeout)


class WhatsAppNotifier:
    """Bounded queue + one worker thread. ``send`` never blocks."""

    def __init__(
        self,
        phone: Optional[str],
        apikey: Optional[str],
        *,
        log: Optional[LogFn] = None,
        http_get: Optional[HttpGet] = None,
        timeout: float = HTTP_TIMEOUT_S,
        retry_delay_s: float = RETRY_DELAY_S,
        maxsize: int = QUEUE_MAX,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.phone = str(phone or "").strip()
        self._apikey = str(apikey or "").strip()
        self._log = log
        self._get = http_get or _default_get
        self.timeout = float(timeout)
        self.retry_delay_s = float(retry_delay_s)
        self._sleep = sleep
        self._queue: queue.Queue = queue.Queue(maxsize=max(1, int(maxsize)))
        self._put_lock = threading.Lock()
        self._worker: Optional[threading.Thread] = None
        self._worker_lock = threading.Lock()

    @classmethod
    def from_env(cls, env: Mapping[str, str], **kwargs: Any) -> "WhatsAppNotifier":
        return cls(env.get(ENV_PHONE), env.get(ENV_APIKEY), **kwargs)

    @property
    def enabled(self) -> bool:
        return bool(self.phone and self._apikey)

    def missing_env(self) -> list[str]:
        missing = []
        if not self.phone:
            missing.append(ENV_PHONE)
        if not self._apikey:
            missing.append(ENV_APIKEY)
        return missing

    def phone_hint(self) -> str:
        return f"...{self.phone[-4:]}" if len(self.phone) >= 4 else ""

    def redact(self, text: Any) -> str:
        return redact(text, self._apikey, self.phone)

    def send(self, text: str, *, kind: str, meta: Optional[dict] = None) -> bool:
        """Queue one message. Full queue drops the oldest. Never raises."""
        try:
            if not self.enabled:
                return False
            item = {"text": str(text), "kind": str(kind), "meta": dict(meta or {})}
            with self._put_lock:
                try:
                    self._queue.put_nowait(item)
                except queue.Full:
                    try:
                        dropped = self._queue.get_nowait()
                        self._queue.task_done()
                    except queue.Empty:
                        dropped = None
                    self._queue.put_nowait(item)
                    self._emit(
                        "notify_dropped",
                        kind=(dropped or {}).get("kind"),
                        reason="queue_full",
                        maxsize=self._queue.maxsize,
                        **(dropped or {}).get("meta", {}),
                    )
            self._ensure_worker()
            return True
        except Exception:
            return False

    def flush(self, timeout: float = 5.0) -> bool:
        """Wait for the queue to drain (tests / shutdown). True if drained."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self._queue.unfinished_tasks == 0:
                return True
            time.sleep(0.01)
        return self._queue.unfinished_tasks == 0

    def _ensure_worker(self) -> None:
        with self._worker_lock:
            if self._worker is not None and self._worker.is_alive():
                return
            self._worker = threading.Thread(
                target=self._run, name="mintbot-whatsapp", daemon=True
            )
            self._worker.start()

    def _run(self) -> None:
        while True:
            item = self._queue.get()
            try:
                self._deliver(item)
            except Exception:
                pass
            finally:
                self._queue.task_done()

    def _deliver(self, item: dict) -> None:
        kind = item.get("kind")
        meta = item.get("meta") or {}
        params = {"phone": self.phone, "text": item.get("text", ""), "apikey": self._apikey}
        status: Optional[int] = None
        error = ""
        for attempt in (1, 2):
            status, error, retryable = self._attempt(params)
            if status is not None and 200 <= status < 300 and not error:
                self._emit("notify_sent", kind=kind, status=status, attempts=attempt, **meta)
                return
            if attempt == 1 and retryable:
                self._sleep(self.retry_delay_s)
                continue
            break
        self._emit(
            "notify_failed",
            kind=kind,
            status=status,
            error=self.redact(error)[:200],
            attempts=attempt,
            **meta,
        )

    def _attempt(self, params: dict) -> tuple[Optional[int], str, bool]:
        """``(status, error, retryable)``. Retry only on 429, 5xx or no reply."""
        try:
            response = self._get(CALLMEBOT_URL, params=params, timeout=self.timeout)
        except Exception as exc:
            return None, f"{type(exc).__name__}: {exc}", True
        try:
            status = int(getattr(response, "status_code", 0) or 0)
        except (TypeError, ValueError):
            status = 0
        body = str(getattr(response, "text", "") or "")[:300]
        if status == 429 or status >= 500:
            return status, body or f"HTTP {status}", True
        if not 200 <= status < 300:
            return status, body or f"HTTP {status}", False
        # A bad key comes back as 203 "APIKey is invalid", not a 4xx.
        if status == 203 or "invalid" in body.lower():
            return status, body or f"HTTP {status}", False
        return status, "", False

    def _emit(self, event: str, **fields: Any) -> None:
        if self._log is None:
            return
        try:
            self._log(event, **fields)
        except Exception:
            return


def _num(value: Any) -> Optional[float]:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    if out != out or out in (float("inf"), float("-inf")):
        return None
    return out


def _shares_txt(value: float) -> str:
    return f"{value:.0f}" if abs(value - round(value)) < 0.05 else f"{value:.1f}"


def _px_txt(value: float) -> str:
    text = f"{value:.3f}".rstrip("0")
    head, _, frac = text.partition(".")
    return f"{head}.{frac.ljust(2, '0')}"


def _left_txt(seconds: Optional[float]) -> str:
    if seconds is None:
        return "time left ?"
    if seconds <= 0:
        return "window ended"
    total = int(seconds)
    minutes, secs = divmod(total, 60)
    return f"{minutes}m{secs:02d}s left" if minutes else f"{secs}s left"


def bag_start_label(start_ts: Any, tz_name: str = BAG_TZ) -> str:
    """``HH:MM`` of the window start in Europe/Dublin (UTC if tzdata is missing)."""
    ts = _num(start_ts)
    if ts is None:
        return "?"
    try:
        from zoneinfo import ZoneInfo

        return datetime.fromtimestamp(ts, ZoneInfo(tz_name)).strftime("%H:%M")
    except Exception:
        return datetime.fromtimestamp(ts, timezone.utc).strftime("%H:%M UTC")


def _start_ts(intent: Mapping[str, Any]) -> Optional[float]:
    start = _num(intent.get("start_ts"))
    if start is not None:
        return start
    end = _num(intent.get("end_ts"))
    return end - 900.0 if end is not None else None


def _leg_name(leg: Any) -> str:
    return {"up": "UP", "dn": "DN"}.get(str(leg or ""), str(leg or "?").upper())


def _other(leg: Any) -> Optional[str]:
    return {"up": "dn", "dn": "up"}.get(str(leg or ""))


def _bid_part(leg: Optional[str], bids: Optional[Mapping[str, Any]]) -> Optional[str]:
    if leg is None or not bids:
        return None
    bid = _num(bids.get(leg))
    if bid is None or bid <= 0:
        return None
    return f"{_leg_name(leg)} bid {_px_txt(bid)}"


def scrap_message(
    intent: Mapping[str, Any],
    *,
    now: float,
    leg: Any = None,
    bids: Optional[Mapping[str, Any]] = None,
    partial: bool = False,
) -> Optional[str]:
    """``Mintbot scrap: 11:30 bag | sold 62 DN @ 0.02 ($1.24) | 3m12s left | kept 63 | UP bid 0.98``.

    None when nothing was sold. Price is the recorded average fill
    (``sell_fill_px``), else the last posted limit."""
    sold = _num(intent.get("sell_filled")) or 0.0
    if sold < MIN_SOLD:
        return None
    leg = leg or intent.get("sold_leg") or intent.get("sell_loser_leg")
    px = _num(intent.get("sell_fill_px"))
    if px is None:
        px = _num(intent.get("sell_limit"))
    parts = [f"{bag_start_label(_start_ts(intent))} bag"]
    if px is not None:
        parts.append(f"sold {_shares_txt(sold)} {_leg_name(leg)} @ {_px_txt(px)} (${sold * px:.2f})")
    else:
        parts.append(f"sold {_shares_txt(sold)} {_leg_name(leg)}")
    end = _num(intent.get("end_ts"))
    parts.append(_left_txt(None if end is None else end - now))
    keep = _num(intent.get("sell_scrap_keep"))
    if keep is not None and keep >= MIN_SOLD:
        parts.append(f"kept {_shares_txt(keep)}")
    bid = _bid_part(_other(leg), bids)
    if bid:
        parts.append(bid)
    title = "Mintbot scrap (partial, window ended)" if partial else "Mintbot scrap"
    return f"{title}: " + " | ".join(parts)


def dump_message(
    intent: Mapping[str, Any],
    *,
    now: float,
    leg: Any = None,
    bids: Optional[Mapping[str, Any]] = None,
) -> Optional[str]:
    """``Mintbot DUMP: 11:30 bag | sold 100 UP @ 0.31 ($31.00) | 3m12s left | DN bid 0.66``."""
    sold = _num(intent.get("sell_dump_filled")) or 0.0
    if sold < MIN_SOLD:
        return None
    leg = leg or intent.get("sell_dump_leg")
    px = _num(intent.get("sell_dump_fill_px"))
    if px is None:
        px = _num(intent.get("sell_dump_limit"))
    parts = [f"{bag_start_label(_start_ts(intent))} bag"]
    if px is not None:
        parts.append(f"sold {_shares_txt(sold)} {_leg_name(leg)} @ {_px_txt(px)} (${sold * px:.2f})")
    else:
        parts.append(f"sold {_shares_txt(sold)} {_leg_name(leg)}")
    end = _num(intent.get("end_ts"))
    parts.append(_left_txt(None if end is None else end - now))
    bid = _bid_part(_other(leg), bids)
    if bid:
        parts.append(bid)
    return "Mintbot DUMP: " + " | ".join(parts)


def _truthy(value: Any, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, str):
        text = value.strip().lower()
        if text in {"1", "true", "yes", "on"}:
            return True
        if text in {"0", "false", "no", "off", ""}:
            return False
        return default
    return bool(value)


class BagAlerts:
    """Glue between the sell loop and the notifier. Every method is safe to
    call from the trading loop: cheap, non-blocking and exception-free."""

    def __init__(self, notifier: WhatsAppNotifier, *, log: Optional[LogFn] = None) -> None:
        self.notifier = notifier
        self._log = log
        self.scrap = False
        self.dump = False
        self.dry_run = True
        self._bids: dict[str, dict] = {}
        self._sent: set[tuple[str, str]] = set()

    def configure(self, cfg: Mapping[str, Any]) -> None:
        """Re-read knobs each sell tick so strategy hot-reload applies."""
        try:
            on = self.notifier.enabled
            self.scrap = on and _truthy(cfg.get("notify_scrap_whatsapp"), True)
            self.dump = on and _truthy(cfg.get("notify_dump_whatsapp"), False)
            self.dry_run = _truthy(cfg.get("dry_run"), True)
        except Exception:
            self.scrap = self.dump = False

    def startup(self, cfg: Mapping[str, Any]) -> None:
        """Exactly one startup line; never includes the key or full phone."""
        try:
            self.configure(cfg)
            if not self.notifier.enabled:
                self._emit(
                    "notify_whatsapp_off",
                    reason="missing_env",
                    missing=self.notifier.missing_env(),
                )
                return
            self._emit(
                "notify_whatsapp_on",
                phone=self.notifier.phone_hint(),
                scrap=self.scrap,
                dump=self.dump,
                dry_run=self.dry_run,
            )
        except Exception:
            return

    def note_bids(self, cid: str, up_bid: Any, dn_bid: Any) -> None:
        try:
            self._bids[str(cid)] = {"up": up_bid, "dn": dn_bid}
            if len(self._bids) > 200:
                for key in list(self._bids)[:100]:
                    self._bids.pop(key, None)
        except Exception:
            return

    def scrap_filled(
        self,
        intent: Mapping[str, Any],
        cid: str,
        *,
        now: float,
        leg: Any = None,
        window_end: bool = False,
    ) -> bool:
        try:
            if not self.scrap or self.dry_run or intent.get("sell_dry"):
                return False
            key = ("scrap", str(cid))
            if key in self._sent:
                return False
            if window_end:
                end = _num(intent.get("end_ts"))
                if intent.get("sold_loser") or end is None or not 0 <= now - end <= WINDOW_END_NOTIFY_S:
                    return False
            text = scrap_message(
                intent, now=now, leg=leg, bids=self._bids.get(str(cid)), partial=window_end,
            )
            if text is None:
                return False
            self._sent.add(key)
            return self.notifier.send(
                text, kind="scrap", meta={"condition_id": str(cid), "slug": intent.get("slug")},
            )
        except Exception:
            return False

    def dump_filled(
        self, intent: Mapping[str, Any], cid: str, *, now: float, leg: Any = None
    ) -> bool:
        try:
            if not self.dump or self.dry_run or intent.get("sell_dump_dry"):
                return False
            key = ("dump", str(cid))
            if key in self._sent:
                return False
            text = dump_message(intent, now=now, leg=leg, bids=self._bids.get(str(cid)))
            if text is None:
                return False
            self._sent.add(key)
            return self.notifier.send(
                text, kind="dump", meta={"condition_id": str(cid), "slug": intent.get("slug")},
            )
        except Exception:
            return False

    def _emit(self, event: str, **fields: Any) -> None:
        if self._log is None:
            return
        try:
            self._log(event, **fields)
        except Exception:
            return
