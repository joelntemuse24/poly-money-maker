"""Pure 5m probe gates (no bot import, no I/O).

Used by tests and ``check_5m_probe_now.py``. ``buybot5m.py`` keeps its own
hot-reload loop; these helpers document the probe arithmetic so a later
~$40 cap is a JSON change, not a rewrite.

Window is last ``buy_start_s`` seconds (probe: 90), not 15m's
``buy_window_min`` minutes. Band/spend/arm helpers are shared with 15m.
"""

from __future__ import annotations

from typing import Tuple

from buy.probe_15m import (  # noqa: F401 — re-export for 5m callers
    EPS,
    ask_in_band,
    live_posting_armed,
    probe_spend_usd,
    shares_rail_needed,
    should_evaluate_entries,
)


def in_buy_window_s(seconds_left: float, window_s: float) -> bool:
    """True when TTM is inside the last ``window_s`` seconds (exclusive of expiry)."""
    try:
        left = float(seconds_left)
        window = float(window_s)
    except (TypeError, ValueError):
        return False
    if left != left or window != window:
        return False
    return 0 < left <= window + EPS


def probe_live_flip_note() -> Tuple[str, str]:
    """Exact later-live edit. Probe ships with both safeties on."""
    return (
        'Set `"dry_run": false` and restart polybuybot5m (dry_run is startup-locked).',
        'Then the one hot-reload knob is `"entry_enabled": true` (Joel only).',
    )
