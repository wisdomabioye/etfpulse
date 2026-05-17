"""Shared statistical helpers for read-only aggregations.

Lives separately from the aggregators (`calibration.py`, `per_detector.py`)
so that any pair of them can reuse the same primitives without creating a
sibling-import coupling. The dependency direction stays linear:

    stats        ←  calibration
    stats        ←  per_detector
    track_record ←  calibration
    track_record ←  per_detector

No project imports inside this module — keeps the math primitive testable
in isolation and the import graph cheap for callers (the OpenRouter
adapter pulls `pipeline.analysis` transitively, and via that nothing
heavy should land in the hot AI-call path).
"""

from __future__ import annotations

import math

# Wilson 95% CI critical value (two-sided). Textbook 1.96 — NOT the more
# precise 1.959963984540054. The textbook value makes `wilson_ci(n, n)`
# saturate to exactly 1.0 (and `wilson_ci(0, n)` to exactly 0.0) by
# numerical fluke of the formula, and downstream tests + UI rendering
# count on those exact endpoints. Bumping to higher precision would shift
# the upper bound by ~1e-6, which is invisible to users but breaks the
# `assert hi == 1.0` tests. Mathematical constant, not a tunable.
_Z_95 = 1.96


def wilson_ci(wins: int, n: int, *, z: float = _Z_95) -> tuple[float | None, float | None]:
    """Wilson score interval for a binomial proportion.

    Args:
        wins: successes (>= 0).
        n: trials (>= 0).
        z: critical value; default 1.96 ≈ 95% two-sided.

    Returns:
        `(ci_low, ci_high)` clamped to [0, 1]. Returns `(None, None)` when
        `n == 0` — the proportion is undefined with no trials.

    The Wilson interval is the standard "small-N safe" CI. Unlike the
    normal-approximation interval, it doesn't collapse to [1.0, 1.0] when
    wins == n (saturation) or [0.0, 0.0] when wins == 0. Hand-rolled here
    (scipy isn't a dependency — checked pyproject.toml). Tested against
    table-of-known-values.

    Edge cases:
        wins=0, n=10  → (0.0, ~0.31)        — non-trivial upper bound
        wins=10, n=10 → (~0.72, 1.0)        — non-trivial lower bound
        wins=5, n=10  → (~0.24, ~0.76)      — centred around 0.5
    """
    if n <= 0:
        return (None, None)
    p = wins / n
    z2_over_n = (z * z) / n
    denom = 1 + z2_over_n
    center = (p + z2_over_n / 2) / denom
    margin = (z * math.sqrt((p * (1 - p) + z2_over_n / 4) / n)) / denom
    lo = max(0.0, center - margin)
    hi = min(1.0, center + margin)
    return (lo, hi)


__all__ = ["wilson_ci"]
