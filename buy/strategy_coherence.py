"""Fail-closed hourly strategy coherence (pure; no bot import).

Soft-edge ``max_usd`` is the *entry* Binance-vs-PTB favor edge, the same
units as ``min_underlying_edge_usd``. A max at or above the buy floor
makes every intentional fill a dump candidate — that combo must not load.
"""

from __future__ import annotations

from typing import Any, Mapping

EPS = 1e-12


def _f(cfg: Mapping[str, Any], key: str, default: float = 0.0) -> float:
    raw = cfg[key] if key in cfg and cfg[key] is not None else default
    try:
        value = float(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{key} must be finite") from exc
    if value != value or value in (float("inf"), float("-inf")):
        raise ValueError(f"{key} must be finite")
    return value


def _b(cfg: Mapping[str, Any], key: str, default: bool = False) -> bool:
    if key not in cfg or cfg[key] is None:
        return default
    return bool(cfg[key])


def soft_edge_has_price_edge(
    entry_price: float,
    exit_bid: float,
    epsilon: float = EPS,
) -> bool:
    """True when a sell at ``exit_bid`` is still above the entry ask.

    Used at decision time and to document 99¢ a22 fills vs a ~95¢ exit:
    ``entry >= exit_bid - epsilon`` has no price edge.
    """
    try:
        entry = float(entry_price)
        bid = float(exit_bid)
        eps = float(epsilon)
    except (TypeError, ValueError):
        return False
    if entry != entry or bid != bid:
        return False
    return entry < bid - eps


def validate_hedge_dump_ladder(cfg: Mapping[str, Any]) -> None:
    """Keep ``dump < qualify <= recovery_cancel <= 1`` (0 dump = disabled)."""
    dump = _f(cfg, "hedge_toxic_bid_max", 0.0)
    qualify = _f(cfg, "hedge_threshold", 0.50)
    recovery = _f(cfg, "hedge_recovery_cancel", 0.53)
    if dump > 0 and not (dump < qualify <= recovery <= 1):
        raise ValueError(
            "hedge_recovery_cancel must satisfy dump < qualify <= recovery_cancel <= 1"
        )
    if dump <= 0 and not (qualify <= recovery <= 1):
        raise ValueError(
            "hedge_recovery_cancel must satisfy qualify <= recovery_cancel <= 1"
        )


def validate_soft_edge_coherence(cfg: Mapping[str, Any]) -> None:
    max_usd = _f(cfg, "soft_edge_exit_max_usd", 0.0)
    bid = _f(cfg, "soft_edge_exit_bid", 0.0)
    persist = _f(cfg, "soft_edge_exit_persist_s", 0.0)
    if max_usd < 0:
        raise ValueError("soft_edge_exit_max_usd must be >= 0")
    if not (0 <= bid <= 1):
        raise ValueError("soft_edge_exit_bid must satisfy 0 <= bid <= 1")
    if persist < 0:
        raise ValueError("soft_edge_exit_persist_s must be >= 0")
    if not _b(cfg, "soft_edge_exit_enabled", False):
        return

    if _b(cfg, "underlying_gate_enabled", False):
        floor = _f(cfg, "min_underlying_edge_usd", 0.0)
        if not (max_usd < floor - EPS):
            raise ValueError(
                "soft_edge_exit_max_usd must be strictly below "
                "min_underlying_edge_usd when soft_edge_exit_enabled "
                "and underlying_gate_enabled (else every intentional fill "
                "is a soft-edge dump candidate)"
            )

    a22_w = _f(cfg, "a22_window_min", 0.0)
    b15_w = _f(cfg, "b15_window_min", 0.0)
    c5_w = _f(cfg, "c5_window_min", 0.0)
    if a22_w > 0:
        a22_min = _f(cfg, "a22_min_price", 0.0)
        if not (bid > a22_min + EPS):
            raise ValueError(
                "soft_edge_exit_bid must be > a22_min_price when A22 is on "
                "(cheapest a22 fill would have no price edge)"
            )
    if b15_w > 0:
        b15_max = _f(cfg, "buy_max_price", 0.0)
        if not (bid > b15_max + EPS):
            raise ValueError(
                "soft_edge_exit_bid must be > buy_max_price when B15 is on "
                "(a b15 fill at the cap would have no price edge)"
            )
    if c5_w > 0:
        c5_min = _f(cfg, "c5_min_price", 0.0)
        if not (bid > c5_min + EPS):
            raise ValueError(
                "soft_edge_exit_bid must be > c5_min_price when C5 is on "
                "(cheapest c5 fill would have no price edge)"
            )


def validate_hourly_strategy_coherence(cfg: Mapping[str, Any]) -> None:
    """Raise ValueError on knob combos that must not load."""
    validate_hedge_dump_ladder(cfg)
    validate_soft_edge_coherence(cfg)


def validate_15m_strategy_coherence(cfg: Mapping[str, Any]) -> None:
    """Fail-closed 15m probe knobs (single sleeve; no hourly a22/b15 rewrite).

    Soft-edge max must sit strictly below the Chainlink last-vs-PTB buy floor
    when both gates are on. Early-hot is meaningless on a 15m clock.
    ``market_spend_cap`` 0 means “use buy_max_spend only”.
    """
    validate_soft_edge_coherence(cfg)
    if "hedge_recovery_cancel" in cfg:
        validate_hedge_dump_ladder(cfg)
    if _b(cfg, "early_hot_defer_enabled", False):
        raise ValueError(
            "early_hot_defer_enabled must be false on 15m "
            "(hourly early-hot is meaningless on a 15m clock)"
        )
    cap = _f(cfg, "market_spend_cap", 0.0)
    if cap < 0:
        raise ValueError("market_spend_cap must be >= 0 (0 = disabled)")
    budget = _f(cfg, "buy_budget", 0.0)
    if cap > 0 and cap + EPS < budget:
        raise ValueError(
            "market_spend_cap must be >= buy_budget when market_spend_cap > 0"
        )
    window_min = _f(cfg, "buy_window_min", 0.0)
    if window_min <= 0 or window_min > 15 + EPS:
        raise ValueError("buy_window_min must satisfy 0 < minutes <= 15")
