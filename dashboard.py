#!/usr/bin/env python3
"""
Live dashboard viewer for the BTC Exit Bot.
Run: python3 dashboard.py

Reads .dashboard_status.json (written by bot.py each tick) and
tails bot.log for recent events. Renders a fixed-screen Rich Live display.
Press Ctrl+C to exit (does NOT affect the running bot).
"""
import json
import os
import time
from datetime import datetime

from rich import box
from rich.align import Align
from rich.console import Console
from rich.layout import Layout
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

_BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STATUS_FILE = os.path.join(_BASE_DIR, ".dashboard_status.json")
LOG_FILE = os.path.join(_BASE_DIR, "bot.log")
STRATEGY_FILE = os.path.join(_BASE_DIR, "strategy.json")

# Defaults — kept in sync with bot.py _STRATEGY_DEFAULTS
_DEFAULTS = {
    "sell_threshold": 0.08,
    "hedge_enabled": False,
    "hedge_threshold": 0.50,
    "sell_window_min": 0.5,
}

def _load_thresholds():
    """Read thresholds from strategy.json, falling back to defaults."""
    cfg = dict(_DEFAULTS)
    try:
        if os.path.exists(STRATEGY_FILE):
            with open(STRATEGY_FILE, "r") as f:
                overrides = json.load(f)
            for k in cfg:
                if k in overrides:
                    if isinstance(cfg[k], bool):
                        value = overrides[k]
                        cfg[k] = (
                            value if isinstance(value, bool)
                            else str(value).lower() in ("1", "true", "yes")
                        )
                    else:
                        cfg[k] = float(overrides[k])
    except Exception:
        pass
    return cfg

_strat = _load_thresholds()
SELL_THRESHOLD = _strat["sell_threshold"]
HEDGE_ENABLED = _strat["hedge_enabled"]
HEDGE_THRESHOLD = _strat["hedge_threshold"]
SELL_WINDOW_MIN = _strat["sell_window_min"]

console = Console()


def read_status():
    try:
        with open(STATUS_FILE, "r") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def tail_log(n=10):
    """Read the last n lines of bot.log for recent events."""
    try:
        with open(LOG_FILE, "rb") as f:
            f.seek(0, 2)
            fsize = f.tell()
            read_size = min(fsize, 8192)
            f.seek(max(0, fsize - read_size))
            lines = f.read().decode("utf-8", errors="replace").splitlines()
        return lines[-n:]
    except FileNotFoundError:
        return []


def format_event(line):
    """Parse a JSON log line into a colored display string."""
    try:
        e = json.loads(line)
    except (json.JSONDecodeError, TypeError):
        return None

    ts = e.get("ts", "")[:19].split("T")[-1] if "T" in e.get("ts", "") else e.get("ts", "")[:8]
    event = e.get("event", "")

    if event == "sell_fill":
        leg = e.get("leg", "?").upper()
        sold = e.get("sold", 0)
        px = e.get("price", 0)
        reason = e.get("trigger_reason", "unknown")
        seconds_left = e.get("seconds_left")
        ttm = f" · {seconds_left:.1f}s left" if isinstance(seconds_left, (int, float)) else ""
        return (
            f"[dim]{ts}[/]  [bold bright_yellow]SELL[/] {leg} "
            f"[bold]{sold:.2f}[/] @ {px:.3f} [dim]({reason}{ttm})[/]"
        )
    elif event == "hedge_fill":
        leg = e.get("leg", "?").upper()
        sold = e.get("sold", 0)
        px = e.get("price", 0)
        return f"[dim]{ts}[/]  [bold bright_red]HEDGE[/] {leg} [bold]{sold:.2f}[/] @ {px:.3f}"
    elif event == "sell_ghost_fill" or event == "hedge_ghost_fill":
        leg = e.get("leg", "?").upper()
        sold = e.get("sold", 0)
        return f"[dim]{ts}[/]  [bold yellow]GHOST[/] {leg} [bold]{sold:.2f}[/] confirmed"
    elif event == "sell_attempt":
        leg = e.get("leg", "?").upper()
        reason = e.get("trigger_reason", "unknown")
        return f"[dim]{ts}[/]  [dim]ATTEMPT[/] {leg} sell [dim]({reason})[/]"
    elif event == "hedge_attempt":
        leg = e.get("leg", "?").upper()
        return f"[dim]{ts}[/]  [dim yellow]HEDGE ATTEMPT[/] {leg}"
    elif event == "redeem_submit":
        return f"[dim]{ts}[/]  [bright_magenta]REDEEM[/] submitted"
    elif event == "cycle_error":
        tb = e.get("traceback", "")
        last_line = tb.strip().splitlines()[-1] if tb.strip() else "unknown"
        return f"[dim]{ts}[/]  [bold red]ERROR[/] {last_line[:60]}"
    elif event == "gc":
        return f"[dim]{ts}[/]  [dim]GC[/] cleaned stale positions"
    elif event == "shutdown":
        return f"[dim]{ts}[/]  [bright_green]SHUTDOWN[/] clean exit"
    else:
        return f"[dim]{ts}[/]  [dim]{event}[/]"


def px_style(px, sz):
    """Color a price value by danger level."""
    if sz < 0.01:
        return "[dim]-[/]"
    if px is None:
        return "[dim]?[/]"
    if px <= SELL_THRESHOLD:
        return f"[bold red]{px:.2f}[/]"
    elif HEDGE_ENABLED and px <= HEDGE_THRESHOLD:
        return f"[yellow]{px:.2f}[/]"
    else:
        return f"[bright_green]{px:.2f}[/]"


def build_dashboard(status, events):
    """Build the complete dashboard layout."""
    if status is None:
        return Panel(
            Align.center("[bold yellow]Waiting for bot data...[/]\n[dim]Is the bot running? Check: sudo systemctl status polybot[/]"),
            border_style="yellow",
            box=box.HEAVY,
        )

    cycle = status.get("cycle", 0)
    nav = status.get("nav", 0.0)
    positions = status.get("positions", [])
    last_ts = status.get("ts", 0)
    age = time.time() - last_ts

    now_str = datetime.now().strftime("%H:%M:%S")

    # Stale detection
    if age > 15:
        status_icon = "[bold red]STALE[/]"
    else:
        status_icon = "[bold bright_green]LIVE[/]"

    # Categorize positions
    active = [p for p in positions if p["ttm_min"] > 0 and not p["redeemable"]]
    redeemable = [p for p in positions if p["redeemable"]]

    # P&L summary
    pnl = status.get("pnl", {})
    total_pnl = pnl.get("total_pnl", 0.0)
    total_trades = pnl.get("total_trades", 0)
    wins = pnl.get("wins", 0)
    losses = pnl.get("losses", 0)
    pnl_color = "bright_green" if total_pnl >= 0 else "bright_red"

    # Header
    header_text = (
        f"  {status_icon}  [dim]|[/]  "
        f"[bold bright_green]BTC EXIT BOT[/]  [dim]|[/]  "
        f"[bright_white]{now_str}[/]  [dim]|[/]  "
        f"[bright_yellow]NAV[/] [bold]${nav:>7.2f}[/]  [dim]|[/]  "
        f"[bright_cyan]TICK[/] #{cycle:04d}  [dim]|[/]  "
        f"[bright_green]{len(active)} active[/]  "
        f"[bright_magenta]{len(redeemable)} redeem[/]  [dim]|[/]  "
        f"[{pnl_color}]P&L[/] [bold {pnl_color}]${total_pnl:>+.2f}[/]  "
        f"[dim]({wins}W/{losses}L)[/]"
    )
    header = Panel(header_text, border_style="bright_green", box=box.HEAVY, padding=(0, 0))

    # Positions table
    table = Table(box=box.SIMPLE_HEAVY, border_style="bright_blue", expand=True, show_edge=False)
    table.add_column("MARKET", style="white", max_width=38, no_wrap=True)
    table.add_column("TTM", justify="right", width=7)
    table.add_column("UP qty", justify="right", width=8)
    table.add_column("DN qty", justify="right", width=8)
    table.add_column("STATE", justify="center", width=10)

    display_sets = active + redeemable

    if not display_sets:
        table.add_row("[dim]no positions[/]", "", "", "", "[dim]IDLE[/]")

    for p in display_sets:
        mins = p["ttm_min"]
        up_sz = p["up_size"]
        dn_sz = p["dn_size"]

        if p["redeemable"]:
            state = "[bright_magenta]REDEEM[/]"
            ttm_str = "[dim]done[/]"
        elif mins <= SELL_WINDOW_MIN:
            state = "[bold red]EXIT WINDOW[/]"
            ttm_sec = p.get("ttm_sec")
            if ttm_sec is not None:
                ttm_str = f"[bold red]{ttm_sec:.0f}s[/]"
            else:
                ttm_str = f"[bold red]{mins:.0f}m[/]"
        else:
            state = "[bright_green]WATCHING[/]"
            if mins < 1:
                ttm_str = f"[green]{mins*60:.0f}s[/]"
            else:
                ttm_str = f"[green]{mins:.0f}m[/]"

        table.add_row(
            p["question"][:38],
            ttm_str,
            f"{up_sz:.1f}" if up_sz > 0.01 else "[dim]-[/]",
            f"{dn_sz:.1f}" if dn_sz > 0.01 else "[dim]-[/]",
            state,
        )

    pos_panel = Panel(table, title="[bold bright_cyan]POSITIONS[/]", border_style="cyan", box=box.ROUNDED)

    # Events panel
    if events:
        event_lines = "\n".join(events[-10:])
    else:
        event_lines = "[dim]no events yet[/]"
    event_panel = Panel(event_lines, title="[bold bright_yellow]RECENT EVENTS[/]", border_style="yellow", box=box.ROUNDED)

    # Footer
    footer = Align.center(
        Text.from_markup(f"[dim]Ctrl+C to exit viewer (bot keeps running)  |  data age: {age:.0f}s[/]")
    )

    # Compose
    layout = Layout()
    layout.split_column(
        Layout(header, name="header", size=3),
        Layout(pos_panel, name="positions", ratio=2),
        Layout(event_panel, name="events", ratio=2),
        Layout(footer, name="footer", size=1),
    )
    return layout


def main():
    console.print("[bold bright_green]Starting dashboard viewer...[/] [dim](bot continues running independently)[/]")
    time.sleep(0.5)

    with Live(console=console, refresh_per_second=2, screen=True) as live_display:
        while True:
            try:
                status = read_status()
                log_lines = tail_log(15)
                events = []
                for line in log_lines:
                    formatted = format_event(line)
                    if formatted:
                        events.append(formatted)

                dashboard = build_dashboard(status, events)
                live_display.update(dashboard)
                time.sleep(0.5)
            except KeyboardInterrupt:
                break
            except Exception:
                time.sleep(1)

    console.print("[bold bright_green]Dashboard closed.[/] [dim]Bot is still running.[/]")


if __name__ == "__main__":
    main()
