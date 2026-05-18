"""Regime-conditional detector thresholds (PR I.4).

Pure helpers that compute the EFFECTIVE threshold a detector should use given
the current `MarketRegime`. Detectors call these from inside their `detect()`
methods so the threshold-application logic stays in one greppable place.

Design contract:
  * **Defaults to 1.0** — all multipliers read from `settings.regime_mult_*`
    fields, which all default to 1.0. With defaults, every helper returns
    `(base, False)` — i.e. base threshold unchanged, no clamp fired.
  * **UNCERTAIN + None are pass-through.** When `current_regime is None`
    (no snapshot yet, or threading explicitly disabled) OR
    `MarketRegime.UNCERTAIN`, the helper returns the base threshold. No
    env var exists for UNCERTAIN by design — it's the explicit "we don't
    know" case, and applying a multiplier to "we don't know" would
    overcommit.
  * **Clamp + warn on out-of-range.** `_percentile` in
    `pipeline/detectors/magnitude.py` requires `p ∈ (0, 1)`. A mult that
    would produce out-of-range effective threshold is clamped to
    `(_PCT_MIN, _PCT_MAX)` and a warning is logged so the operator sees the
    misconfiguration. The clamped boolean is surfaced to the caller for
    test-pin / log-once semantics.
  * **No DB, no clock, no I/O.** Pure functions; just (base, regime) → (eff,
    was_clamped). Tests pin behaviour with hand-built inputs.

Rule D26 (added by this PR): regime is THREADED from `signal_builder` /
`backtest.run_backtest` into each `detector.detect()` via the Protocol
kwarg `current_regime`. Detectors MUST NOT query `RegimeSnapshot` themselves
— that would add 5 redundant queries per cycle and put the regime-state
mirror in five places. The orchestrator owns the regime fetch.
"""

from __future__ import annotations

from decimal import Decimal

import structlog

from etfpulse.config import settings
from etfpulse.models import MarketRegime

log = structlog.get_logger()


# Valid range for `_percentile`'s `p` argument — exclusive on both ends.
# `_percentile` indexes `int(p * (len - 1))` which yields 0 at p=0 (degenerate
# min-value lookup) and `len-1` at p=1 (degenerate max-value lookup). Either
# would technically work but produce nonsensical "magnitude" semantics — the
# detector should always be picking from somewhere inside the distribution,
# not endpoints. Clamping at 0.01 / 0.99 leaves room for legitimate tuning.
_PCT_MIN = Decimal("0.01")
_PCT_MAX = Decimal("0.99")


def _magnitude_pctile_multiplier_for(regime: MarketRegime | None) -> Decimal:
    """Map a regime to its configured magnitude-percentile multiplier.

    Returns 1.0 for None / UNCERTAIN (no env var exists for those — they
    mean "use base"). The match-statement here is the only place that
    enumerates the supported regimes; adding a regime to `MarketRegime`
    without extending this match raises by falling through to the
    explicit default-1.0 branch, so a future regime addition fails
    SAFE (no behaviour change) rather than crashing or quietly
    miscounting.
    """
    if regime is None:
        return Decimal("1.0")
    match regime:
        case MarketRegime.MARKUP:
            return settings.regime_mult_magnitude_pctile_markup
        case MarketRegime.MARKDOWN:
            return settings.regime_mult_magnitude_pctile_markdown
        case MarketRegime.ACCUMULATION:
            return settings.regime_mult_magnitude_pctile_accumulation
        case MarketRegime.DISTRIBUTION:
            return settings.regime_mult_magnitude_pctile_distribution
        case MarketRegime.UNCERTAIN:
            return Decimal("1.0")
        case _:
            # Unknown regime — pass-through default. Logged so an operator
            # can see they've added a regime without wiring it up here.
            log.warning(
                "regime_thresholds_unknown_regime",
                regime=str(regime),
                detector="magnitude",
            )
            return Decimal("1.0")


def apply_magnitude_pctile_multiplier(
    base: float, regime: MarketRegime | None
) -> tuple[float, bool]:
    """Compute the effective `percentile_threshold` for `MagnitudeDetector`
    given the current regime. Returns `(effective, was_clamped)`.

    `base` is a float because `MagnitudeDetector.percentile_threshold` is
    typed `float` (compared against `_percentile`'s float input downstream).
    We do the multiplication in `Decimal` to avoid float drift on the
    settings side (multipliers are stored as `Decimal`), then convert back
    at the boundary.

    `was_clamped` is True when the raw `base × mult` fell outside `(_PCT_MIN,
    _PCT_MAX)` and was clamped. Surfacing this lets the caller log once-per-
    cycle (rather than once-per-row) so an operator sees the misconfiguration
    without log spam.
    """
    mult = _magnitude_pctile_multiplier_for(regime)
    if mult == Decimal("1.0"):
        # Fast path — no allocation, no clamp check, identical to pre-I.4.
        return base, False

    raw = Decimal(str(base)) * mult
    clamped = False
    if raw < _PCT_MIN:
        effective = _PCT_MIN
        clamped = True
    elif raw > _PCT_MAX:
        effective = _PCT_MAX
        clamped = True
    else:
        effective = raw

    if clamped:
        log.warning(
            "regime_threshold_clamped",
            detector="magnitude",
            param="percentile_threshold",
            base=str(base),
            regime=str(regime),
            multiplier=str(mult),
            raw=str(raw),
            effective=str(effective),
        )

    return float(effective), clamped


__all__ = ["apply_magnitude_pctile_multiplier"]
