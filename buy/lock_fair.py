"""Win probability for a Chainlink 60-second TWAP lock.

Settlement is the 60-second TWAP at the window end, compared with the
strike (the 60-second TWAP at the window start, Gamma ``priceToBeat``).
Inside the last minute the elapsed part of that average is known. The
unknown tail is a random walk in the live Chainlink price.

This mirrors ``q1e_lock_chainlink`` / ``q1_fairvalue`` without a Binance
basis: the path is already the Chainlink print, so the basis term is 0.
The variance of the remaining average is ``sigma^2 * tau^3 / 10800``
(the integral of a Brownian motion over ``tau`` seconds, divided by the
60-second window). At and before the last minute the variance falls
back to ``sigma^2 * (tau - 40)``, the same pre-lock scale as
``q1_fairvalue``.
"""

from __future__ import annotations

import math
from typing import Optional, Sequence


SETTLE_S = 60.0
# ``Var(∫_0^τ dW) / 60^2 = τ^3 / (3 * 3600)``.
VARIANCE_DENOM = 10800.0
SIGMA_FLOOR = 1e-9


def normal_cdf(z: float) -> float:
    """Standard normal CDF. ``Phi(0) = 0.5``."""
    if z != z:
        return 0.5
    if z > 12.0:
        return 1.0
    if z < -12.0:
        return 0.0
    return 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))


def taker_fee(price: float, rate: float = 0.07, exponent: float = 1.0) -> float:
    """Fee in probability units per share: ``rate * (p*(1-p))**exponent``.

    The backtest uses ``0.07 * p * (1-p)`` (exponent 1).
    """
    p = float(price)
    if not math.isfinite(p) or p <= 0.0 or p >= 1.0:
        return 0.0
    base = p * (1.0 - p)
    try:
        return max(0.0, float(rate) * (base ** float(exponent)))
    except (OverflowError, ValueError):
        return 0.0


def population_std(values: Sequence[float]) -> float:
    """NumPy's default standard deviation (divide by N, not N-1)."""
    n = len(values)
    if n < 2:
        return 0.0
    mean = sum(values) / n
    var = sum((float(v) - mean) ** 2 for v in values) / n
    return math.sqrt(var) if var > 0.0 else 0.0


def sigma_from_prices(
    prices_1s: Sequence[float],
    *,
    short_n: int = 300,
    long_n: int = 900,
    floor: float = SIGMA_FLOOR,
) -> tuple[float, int]:
    """``$/sqrt(second)`` from 1-second price levels.

    Returns ``(sigma, n_returns)``. Sigma is the max of the trailing
    short and long windows, the same max-of-5-min-and-15-min rule as
    the backtest, with a floor so a flat tape cannot divide by zero.
    """
    levels = [float(p) for p in prices_1s if p is not None and math.isfinite(float(p))]
    rets = [levels[i] - levels[i - 1] for i in range(1, len(levels))]
    if not rets:
        return float(floor), 0
    short = rets[-max(1, int(short_n)) :]
    long = rets[-max(1, int(long_n)) :]
    sigma = max(population_std(short), population_std(long), float(floor))
    return sigma, len(rets)


def expected_close(
    elapsed_prices: Sequence[float],
    tau_s: float,
    *,
    settle_s: float = SETTLE_S,
) -> tuple[float, float]:
    """``(expected_final, variance_scale)`` so variance is ``sigma^2 * scale``.

    ``elapsed_prices`` are 1-second Chainlink prints from the start of
    the settlement window through the current second, inclusive.

    When ``tau`` is inside the last minute, q1e fills the remaining
    ``tau - 1`` seconds with the last print and averages all
    ``settle_s`` slots. The scale is ``tau^3 / 10800``. The match with
    that path average is exact when ``len(elapsed_prices)`` equals the
    known-slot count ``settle_s - (tau - 1)``.

    When ``tau`` is still at or above the settlement window, the close
    has not started and the expectation is the live (last) price. The
    scale is ``max(tau - 40, 0)``.
    """
    prices = [float(p) for p in elapsed_prices]
    if not prices:
        raise ValueError("elapsed_prices is empty")
    tau = float(tau_s)
    window = float(settle_s)
    if not math.isfinite(tau) or not math.isfinite(window) or window <= 0:
        raise ValueError("bad tau or settle window")
    if tau >= window:
        return prices[-1], max(tau - 40.0, 0.0)
    fill = max(0.0, tau - 1.0)
    weight_known = window - fill
    known_avg = sum(prices) / len(prices)
    expected = (known_avg * weight_known + prices[-1] * fill) / window
    scale = (tau ** 3) / VARIANCE_DENOM
    return expected, scale


def fair_up(
    *,
    strike: float,
    elapsed_prices: Sequence[float],
    tau_s: float,
    sigma: float,
    noise_frac: float = 0.00002,
    settle_s: float = SETTLE_S,
) -> dict:
    """``P(Up)`` for a strike and a live path.

    Up wins when the final TWAP is greater than or equal to the strike.
    ``noise_frac`` is q1e's 0.2 bp Chainlink noise (``0.00002 * strike``).
    """
    k = float(strike)
    if not math.isfinite(k) or k <= 0:
        raise ValueError("strike must be positive")
    expected, scale = expected_close(elapsed_prices, tau_s, settle_s=settle_s)
    sig = max(float(sigma), 0.0)
    variance = (sig ** 2) * scale
    noise = (float(noise_frac) * k) ** 2
    sd = math.sqrt(max(variance + noise, 0.0))
    if sd <= 0.0:
        z = 0.0 if expected == k else (12.0 if expected > k else -12.0)
    else:
        z = (expected - k) / sd
    p_up = normal_cdf(z)
    return {
        "p_up": p_up,
        "p_down": 1.0 - p_up,
        "expected": expected,
        "variance": variance,
        "sigma": sig,
        "z": z,
        "tau_s": float(tau_s),
    }


def side_z(
    *,
    strike: float,
    expected: float,
    sigma: float,
    tau_s: float,
    noise_frac: float = 0.00002,
) -> dict:
    """Favourite side, ``z_side`` and ``q`` for the NIULAI4 rule.

    The projected close is the caller's (the same elapsed-TWAP expectation
    the bot already uses). Inside the last minute the study's variance is
    ``sigma^2 * max(tau, 0.5)^3 / 10800``, plus ``(noise_frac * strike)^2``.
    ``z`` is for Up. The traded side is the sign of ``expected - strike``
    (a tie stays Up, which is how settlement treats a tie). ``z_side`` is
    that side's z, so it is ``>= 0`` whenever a side is chosen.
    """
    k = float(strike)
    if not math.isfinite(k) or k <= 0:
        raise ValueError("strike must be positive")
    exp = float(expected)
    tau = float(tau_s)
    if not math.isfinite(exp) or not math.isfinite(tau):
        raise ValueError("expected and tau must be finite")
    scale = (max(tau, 0.5) ** 3) / VARIANCE_DENOM
    sig = max(float(sigma), 0.0)
    variance = (sig ** 2) * scale
    noise = (float(noise_frac) * k) ** 2
    sd = math.sqrt(max(variance + noise, 0.0))
    if sd <= 0.0:
        z = 0.0 if exp == k else (12.0 if exp > k else -12.0)
    else:
        z = (exp - k) / sd
    side = "up" if exp >= k else "down"
    z_side = z if side == "up" else -z
    return {
        "side": side,
        "z": z,
        "z_side": z_side,
        "q": normal_cdf(z_side),
        "q_up": normal_cdf(z),
        "sd": sd,
        "variance": variance,
        "tau_s": tau,
    }


def sigma_1s(prices_1s: Sequence[float], *, floor_frac: float = 1e-6) -> tuple[float, int]:
    """``$/second`` from 1-second levels. NumPy's N-divisor, study floor ``1e-6 * px``."""
    levels = [float(p) for p in prices_1s if p is not None and math.isfinite(float(p))]
    rets = [levels[i] - levels[i - 1] for i in range(1, len(levels))]
    last = abs(levels[-1]) if levels else 1.0
    floor = max(float(floor_frac) * last, SIGMA_FLOOR)
    if len(rets) < 2:
        return floor, len(rets)
    return max(population_std(rets), floor), len(rets)


def signed_move(now_px: float, then_px: float, sigma: float) -> Optional[float]:
    """Binance change over the lookback, in units of ``sigma`` (1-second)."""
    sig = float(sigma)
    if not math.isfinite(sig) or sig <= 0:
        return None
    now = float(now_px)
    then = float(then_px)
    if not math.isfinite(now) or not math.isfinite(then):
        return None
    return (now - then) / sig


def resample_1s(samples: Sequence[tuple[float, float]]) -> list[float]:
    """Last price in each whole second, from ``(obs_ts, price)`` pairs."""
    rows = [(float(ts), float(px)) for ts, px in samples if math.isfinite(float(ts)) and math.isfinite(float(px))]
    if len(rows) < 2:
        return [rows[0][1]] if rows else []
    rows.sort()
    start = math.floor(rows[0][0])
    end = math.floor(rows[-1][0])
    if end - start > 100_000:
        start = end - 100_000
    out: list[float] = []
    idx = 0
    last: Optional[float] = None
    n = len(rows)
    for sec in range(int(start), int(end) + 1):
        limit = sec + 1.0 - 1e-9
        while idx < n and rows[idx][0] <= limit:
            last = rows[idx][1]
            idx += 1
        if last is not None:
            out.append(last)
    return out
