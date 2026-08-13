#!/usr/bin/env python3
"""Tiny always-on-top Polymarket glance (Pomodoro-timer shaped, not a bot).

Public Data API only — no private key, no orders. Drag it to a screen corner.

    python widget/polydesk.py
    python widget/polydesk.py --address 0xYourProxy
    python widget/polydesk.py --once

Reads FUNDER_ADDRESS from the environment or repo .env. Position *value* is
mark-to-market of holdings, not idle CLOB cash.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, List, Optional

import requests

REPO = Path(__file__).resolve().parents[1]
DATA_API = "https://data-api.polymarket.com"
ADDRESS_RE = re.compile(r"^0x[a-fA-F0-9]{40}$")
SAVED_ADDRESS = Path(__file__).resolve().parent / ".address"
MIN_SIZE = 0.01
POLL_S = 6.0
UA = "poly-money-maker-polydesk/1.0"


@dataclass(frozen=True)
class Snapshot:
    ok: bool
    value: Optional[float]
    holding: bool
    count: int
    labels: tuple[str, ...]
    error: str = ""
    ts: float = 0.0


def looks_like_address(value: str) -> bool:
    return bool(ADDRESS_RE.match((value or "").strip()))


def load_saved_address() -> str:
    try:
        text = SAVED_ADDRESS.read_text(encoding="utf-8").strip()
    except OSError:
        return ""
    return text if looks_like_address(text) else ""


def save_address(address: str) -> None:
    if not looks_like_address(address):
        return
    SAVED_ADDRESS.write_text(address.strip() + "\n", encoding="utf-8")


def address_from_env() -> str:
    for key in ("FUNDER_ADDRESS", "POLY_ADDRESS", "POLYMARKET_ADDRESS"):
        raw = os.getenv(key, "").strip()
        if looks_like_address(raw):
            return raw
    return ""


def load_dotenv_address() -> str:
    env_path = REPO / ".env"
    if not env_path.is_file():
        return ""
    try:
        from dotenv import load_dotenv

        load_dotenv(env_path)
    except Exception:
        pass
    return address_from_env()


def resolve_address(cli: str = "") -> str:
    for candidate in (cli, address_from_env(), load_dotenv_address(), load_saved_address()):
        if looks_like_address(candidate):
            return candidate.strip()
    return ""


def parse_value(payload: Any) -> Optional[float]:
    if isinstance(payload, list) and payload:
        payload = payload[0]
    if not isinstance(payload, dict):
        return None
    try:
        value = float(payload.get("value"))
    except (TypeError, ValueError):
        return None
    if value != value:  # NaN
        return None
    return value


def _finite(value: Any) -> Optional[float]:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    if parsed != parsed:
        return None
    return parsed


def summarize_positions(rows: Iterable[Any], min_size: float = MIN_SIZE) -> tuple[bool, int, tuple[str, ...]]:
    labels: List[str] = []
    seen = set()
    count = 0
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        size = _finite(row.get("size"))
        if size is None or size < min_size:
            continue
        count += 1
        cid = str(row.get("conditionId") or row.get("asset") or "")
        if cid in seen:
            continue
        seen.add(cid)
        title = str(row.get("title") or row.get("slug") or "").strip()
        outcome = str(row.get("outcome") or "").strip()
        if title and outcome:
            labels.append(f"{outcome} · {title}")
        elif title:
            labels.append(title)
        elif outcome:
            labels.append(outcome)
    return count > 0, count, tuple(labels[:4])


def fetch_snapshot(address: str, session: Optional[requests.Session] = None) -> Snapshot:
    now = time.time()
    if not looks_like_address(address):
        return Snapshot(False, None, False, 0, (), "set a 0x proxy address", now)
    client = session or requests.Session()
    headers = {"User-Agent": UA}
    try:
        value_resp = client.get(
            f"{DATA_API}/value",
            params={"user": address},
            timeout=8,
            headers=headers,
        )
        value_resp.raise_for_status()
        value = parse_value(value_resp.json())
        pos_resp = client.get(
            f"{DATA_API}/positions",
            params={
                "user": address,
                "sizeThreshold": MIN_SIZE,
                "limit": 50,
                "offset": 0,
                "sortBy": "CURRENT",
                "sortDirection": "DESC",
            },
            timeout=8,
            headers=headers,
        )
        pos_resp.raise_for_status()
        rows = pos_resp.json()
        if not isinstance(rows, list):
            raise ValueError("positions was not a list")
        holding, count, labels = summarize_positions(rows)
        return Snapshot(True, value, holding, count, labels, "", now)
    except Exception as exc:
        return Snapshot(False, None, False, 0, (), str(exc)[:160], now)


def format_usd(value: Optional[float]) -> str:
    if value is None:
        return "—"
    if abs(value) >= 1000:
        return f"${value:,.0f}"
    return f"${value:,.2f}"


def format_age(ts: float, now: Optional[float] = None) -> str:
    if ts <= 0:
        return ""
    age = max(0, int((now or time.time()) - ts))
    if age < 5:
        return "live"
    if age < 60:
        return f"{age}s ago"
    return f"{age // 60}m ago"


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Always-on-top Polymarket balance glance")
    parser.add_argument("--address", default="", help="proxy / FUNDER_ADDRESS (0x…)")
    parser.add_argument("--once", action="store_true", help="print one snapshot and exit (no window)")
    parser.add_argument("--poll", type=float, default=POLL_S, help="refresh seconds (default 6)")
    return parser


def _print_once(snap: Snapshot) -> int:
    status = "HOLDING" if snap.holding else "FLAT"
    if not snap.ok:
        print(f"error: {snap.error}", file=sys.stderr)
        return 1
    extra = f"  {snap.count} pos" if snap.holding else ""
    print(f"{format_usd(snap.value)}  {status}{extra}")
    for label in snap.labels:
        print(f"  {label}")
    return 0


def run_window(address: str, poll_s: float) -> int:
    try:
        import tkinter as tk
    except ImportError:
        print(
            "tkinter is missing. On Debian/Ubuntu: sudo apt install python3-tk\n"
            "Or run with --once, or open widget/index.html in a small browser window.",
            file=sys.stderr,
        )
        return 2

    current = {"address": address, "snap": Snapshot(False, None, False, 0, (), "starting…", time.time())}
    lock = threading.Lock()
    stop = threading.Event()
    session = requests.Session()

    def poller() -> None:
        while not stop.is_set():
            with lock:
                addr = current["address"]
            snap = fetch_snapshot(addr, session)
            with lock:
                current["snap"] = snap
            stop.wait(max(1.5, poll_s))

    threading.Thread(target=poller, daemon=True).start()

    root = tk.Tk()
    root.title("Polydesk")
    root.geometry("228x268+24+48")
    root.resizable(False, False)
    root.configure(bg="#241f1a")
    try:
        root.attributes("-topmost", True)
    except tk.TclError:
        pass
    try:
        root.overrideredirect(True)
    except tk.TclError:
        pass

    drag = {"x": 0, "y": 0}

    def start_drag(event: tk.Event) -> None:
        drag["x"] = event.x_root - root.winfo_x()
        drag["y"] = event.y_root - root.winfo_y()

    def on_drag(event: tk.Event) -> None:
        root.geometry(f"+{event.x_root - drag['x']}+{event.y_root - drag['y']}")

    canvas = tk.Canvas(root, width=228, height=268, bg="#241f1a", highlightthickness=0)
    canvas.pack(fill="both", expand=True)
    canvas.bind("<ButtonPress-1>", start_drag)
    canvas.bind("<B1-Motion>", on_drag)

    def quit_app(_event: Optional[tk.Event] = None) -> None:
        stop.set()
        root.destroy()

    root.bind("<Escape>", quit_app)

    def prompt_address() -> None:
        dialog = tk.Toplevel(root)
        dialog.title("Proxy address")
        dialog.attributes("-topmost", True)
        dialog.configure(bg="#241f1a")
        tk.Label(
            dialog,
            text="Polymarket proxy (FUNDER_ADDRESS)",
            fg="#f3eee6",
            bg="#241f1a",
        ).pack(padx=12, pady=(12, 4))
        entry = tk.Entry(dialog, width=44)
        entry.pack(padx=12, pady=4)
        with lock:
            entry.insert(0, current["address"])
        entry.focus_set()

        def save() -> None:
            raw = entry.get().strip()
            if not looks_like_address(raw):
                return
            save_address(raw)
            with lock:
                current["address"] = raw
            dialog.destroy()

        tk.Button(dialog, text="Save", command=save).pack(pady=(4, 12))
        entry.bind("<Return>", lambda _e: save())

    def popup(event: tk.Event) -> None:
        menu = tk.Menu(root, tearoff=0)
        menu.add_command(label="Set address…", command=prompt_address)
        menu.add_command(label="Quit", command=quit_app)
        menu.tk_popup(event.x_root, event.y_root)

    canvas.bind("<Button-3>", popup)
    canvas.bind("<Button-2>", popup)
    canvas.bind("<Double-Button-1>", lambda _e: prompt_address())

    def paint() -> None:
        with lock:
            snap = current["snap"]
            addr = current["address"]
        canvas.delete("all")
        holding = snap.ok and snap.holding
        plate = "#f6e4d8" if holding else "#efe8de"
        rim = "#c4452d" if holding else "#d5ccc0"
        canvas.create_oval(34, 28, 194, 188, fill=plate, outline=rim, width=4)
        canvas.create_text(
            114,
            92,
            text=format_usd(snap.value) if snap.ok else "—",
            font=("Helvetica", 26, "bold"),
            fill="#2b241c",
        )
        caption = "HOLDING" if holding else ("FLAT" if snap.ok else "OFFLINE")
        cap_color = "#c4452d" if holding else "#6f675e"
        canvas.create_text(114, 128, text=caption, font=("Helvetica", 11, "bold"), fill=cap_color)
        if snap.ok and holding:
            detail = f"{snap.count} position" + ("s" if snap.count != 1 else "")
        elif snap.ok:
            detail = "no open size"
        else:
            detail = (snap.error or "waiting")[:28]
        canvas.create_text(114, 148, text=detail, font=("Helvetica", 9), fill="#7a736a")
        age = format_age(snap.ts) if snap.ok else ""
        tail = addr[0:6] + "…" + addr[-4:] if looks_like_address(addr) else "double-click to set address"
        canvas.create_text(114, 214, text="POLYDESK", font=("Helvetica", 8, "bold"), fill="#8a8178")
        canvas.create_text(
            114,
            232,
            text=" · ".join(p for p in (age, tail) if p),
            font=("Helvetica", 8),
            fill="#6a635c",
        )
        canvas.create_text(114, 252, text="esc quit  ·  drag anywhere", font=("Helvetica", 7), fill="#4e4842")
        root.after(400, paint)

    if not looks_like_address(address):
        root.after(200, prompt_address)
    paint()
    try:
        root.mainloop()
    finally:
        stop.set()
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    args = _build_parser().parse_args(argv)
    address = resolve_address(args.address)
    if args.once:
        return _print_once(fetch_snapshot(address))
    if looks_like_address(address):
        save_address(address)
    return run_window(address, args.poll)


if __name__ == "__main__":
    raise SystemExit(main())
