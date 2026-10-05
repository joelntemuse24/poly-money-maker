"""Shared websocket-client options for lockbot feeds.

``skip_utf8_validation`` skips websocket-client's pure-Python UTF-8 walk.
That walk held the GIL on the book, wallet, and Binance threads. ``wsaccel``
is used by websocket-client automatically when it is installed; lockbot does
not import it and does not require it.
"""

from __future__ import annotations


def connect_kwargs() -> dict:
    """Keyword arguments for ``create_connection`` and ``run_forever``."""
    return {"skip_utf8_validation": True}


def heartbeat_kind(message: object) -> str:
    """``PING`` or ``PONG`` for an application-level heartbeat, else empty."""
    if isinstance(message, str):
        text = message.strip()
    elif isinstance(message, (bytes, bytearray)):
        text = bytes(message).strip().decode("ascii", "ignore")
    else:
        return ""
    upper = text.upper()
    if upper in {"PING", "PONG"}:
        return upper
    return ""


def wsaccel_available() -> bool:
    try:
        import wsaccel  # noqa: F401
    except ImportError:
        return False
    return True
