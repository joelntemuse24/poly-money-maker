"""Gas limit for a mint PROXY relay batch.

The relayer client signs ``gasLimit`` into the proxy struct. RelayHub
forwards that stipend to the proxy factory (``call.gas(gasLimit)``).
``py_builder_relayer_client`` uses 500_000 when the field is omitted, and
its ``gas.py`` comment says that fallback must fit a relay-hub budget of
about 650_000 total. High-iteration CTF splits need more than 500_000, so
mint estimates the factory call and clamps the signed limit to that budget.

Estimation is mint-submit only. This module does not open a socket.
"""

from __future__ import annotations

from dataclasses import dataclass

# py_builder_relayer_client.gas: "Must fit within the relay hub's gas budget
# (~650k total)." The signed gasLimit is the stipend forwarded to the factory.
RELAY_HUB_GAS_MAX = 650_000
DEFAULT_MINT_GAS_MARGIN = 0.15
DEFAULT_MINT_GAS_FALLBACK = 650_000
DEFAULT_MINT_GAS_CAP = 650_000


@dataclass(frozen=True)
class MintGasPlan:
    gas_limit: int
    estimate: int | None
    margin: float
    cap: int
    hub_max: int
    clamped: bool
    source: str

    def relay_arg(self) -> str:
        """Value for ``ProxyTransactionArgs.gas_limit`` (decimal string)."""
        return str(self.gas_limit)

    def as_log(self) -> dict:
        return {
            "gas_limit": self.gas_limit,
            "gas_estimate": self.estimate,
            "gas_margin": self.margin,
            "gas_cap": self.cap,
            "gas_clamped": self.clamped,
            "gas_source": self.source,
            "gas_hub_max": self.hub_max,
        }


def _ceil_with_margin(estimate: int, margin: float) -> int:
    bps = int(round(float(margin) * 10_000))
    if bps < 0:
        bps = 0
    scaled = estimate * (10_000 + bps)
    return (scaled + 9_999) // 10_000


def plan_mint_gas(
    estimate: int | None,
    *,
    margin: float = DEFAULT_MINT_GAS_MARGIN,
    fallback: int = DEFAULT_MINT_GAS_FALLBACK,
    cap: int = DEFAULT_MINT_GAS_CAP,
) -> MintGasPlan:
    """Pick the signed gas limit from an estimate, or the fallback.

    ``cap`` is clamped to ``RELAY_HUB_GAS_MAX``. A result above the
    effective cap is reduced and marked ``clamped``.
    """
    effective_cap = min(int(cap), RELAY_HUB_GAS_MAX)
    if effective_cap < 1:
        effective_cap = 1
    margin_f = float(margin)
    if estimate is None or int(estimate) <= 0:
        requested = int(fallback)
        limit = min(requested, effective_cap)
        return MintGasPlan(
            gas_limit=limit,
            estimate=None,
            margin=margin_f,
            cap=effective_cap,
            hub_max=RELAY_HUB_GAS_MAX,
            clamped=limit < requested,
            source="fallback",
        )
    requested = _ceil_with_margin(int(estimate), margin_f)
    limit = min(requested, effective_cap)
    return MintGasPlan(
        gas_limit=limit,
        estimate=int(estimate),
        margin=margin_f,
        cap=effective_cap,
        hub_max=RELAY_HUB_GAS_MAX,
        clamped=limit < requested,
        source="estimate",
    )


def _parse_gas_quantity(result: object) -> int:
    if isinstance(result, bool) or result is None:
        raise ValueError("invalid gas quantity")
    if isinstance(result, int):
        value = result
    elif isinstance(result, str) and result.startswith(("0x", "0X")):
        value = int(result, 16)
    else:
        raise ValueError("invalid gas quantity")
    if value <= 0:
        raise ValueError("invalid gas quantity")
    return value


def estimate_proxy_call_gas(rpc_call, from_address: str, to: str, data: str) -> int | None:
    """``eth_estimateGas`` of signer → proxy factory. None on any failure.

    The params omit ``gas`` so the node is not pinned to the 500k default.
    ``rpc_call`` is ``(method, params) -> result`` and may raise.
    """
    try:
        result = rpc_call(
            "eth_estimateGas",
            [{"from": from_address, "to": to, "data": data}],
        )
        return _parse_gas_quantity(result)
    except Exception:
        return None


def choose_mint_relay_gas(
    rpc_call,
    *,
    from_address: str,
    to: str,
    data: str,
    margin: float = DEFAULT_MINT_GAS_MARGIN,
    fallback: int = DEFAULT_MINT_GAS_FALLBACK,
    cap: int = DEFAULT_MINT_GAS_CAP,
) -> MintGasPlan:
    """Estimate the encoded batch, then apply margin, fallback, and cap."""
    if rpc_call is None:
        estimate = None
    else:
        estimate = estimate_proxy_call_gas(rpc_call, from_address, to, data)
    return plan_mint_gas(estimate, margin=margin, fallback=fallback, cap=cap)


def validate_mint_gas(cfg: dict) -> None:
    """Reject bad mint gas knobs. Keys absent from a live file are allowed."""
    if "mint_gas_margin" in cfg and cfg["mint_gas_margin"] is not None:
        raw = cfg["mint_gas_margin"]
        if isinstance(raw, bool) or isinstance(raw, str):
            raise ValueError("mint_gas_margin must be between 0 and 1")
        try:
            margin = float(raw)
        except (TypeError, ValueError) as exc:
            raise ValueError("mint_gas_margin must be between 0 and 1") from exc
        if margin < 0 or margin > 1 or margin != margin:
            raise ValueError("mint_gas_margin must be between 0 and 1")
    for key in ("mint_gas_fallback", "mint_gas_cap"):
        if key not in cfg or cfg[key] is None:
            continue
        raw = cfg[key]
        if isinstance(raw, bool) or isinstance(raw, str):
            raise ValueError(f"{key} must be a positive integer")
        try:
            number = float(raw)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{key} must be a positive integer") from exc
        units = int(round(number))
        if units < 1 or abs(units - number) > 1e-6:
            raise ValueError(f"{key} must be a positive integer")


def mint_gas_settings(cfg: dict) -> tuple[float, int, int]:
    """Margin, fallback, and cap from a strategy dict, with code defaults."""
    margin_raw = cfg.get("mint_gas_margin", DEFAULT_MINT_GAS_MARGIN)
    fallback_raw = cfg.get("mint_gas_fallback", DEFAULT_MINT_GAS_FALLBACK)
    cap_raw = cfg.get("mint_gas_cap", DEFAULT_MINT_GAS_CAP)
    margin = float(DEFAULT_MINT_GAS_MARGIN if margin_raw is None else margin_raw)
    fallback = int(DEFAULT_MINT_GAS_FALLBACK if fallback_raw is None else fallback_raw)
    cap = int(DEFAULT_MINT_GAS_CAP if cap_raw is None else cap_raw)
    return margin, fallback, cap
