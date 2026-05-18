"""Detector registry — every detector ships in this package and is appended
to `ALL_DETECTORS` to be picked up by `signal_builder.run_daily_cycle`.

Anti-drift rules installed by this module:
    D9  — Detectors live ONLY in `etfpulse.pipeline.detectors.*` and implement
          the `Detector` Protocol below. No detector logic anywhere else.
    D10 — Adding a detector requires registering it in `ALL_DETECTORS`.
          `signal_builder` iterates this list — unregistered detectors never
          run. There is no auto-discovery; explicit > implicit.
    D11 — `ALL_DETECTORS` order determines precedence on fingerprint
          collisions: the unique index on `(fingerprint, signal_date)` makes
          the first insert win, so order detectors strongest-signal-first
          when multiple could plausibly fire on the same input.

A single detector failing (raising) MUST NOT abort the daily cycle — that is
`signal_builder`'s responsibility (try/except around each `detect()` call,
log + continue). The Protocol intentionally allows exceptions.
"""

from __future__ import annotations

from datetime import date
from typing import Protocol

from sqlalchemy.ext.asyncio import AsyncSession

from etfpulse.models import MarketRegime
from etfpulse.pipeline.detectors.base import DetectorHit, compute_fingerprint


class Detector(Protocol):
    """Stateless reader over the DB session. Side-effect free.

    Attributes:
        name: Stable, human-readable identifier used in logs and metrics.
              Must be unique across `ALL_DETECTORS`.
        signal_type: One of `models.signal.SignalType` values. Determines
                     which `Signal.signal_type` value the resulting rows get.

    The `as_of` parameter (PR I.5, rule D25) is the backtest seam. When set,
    detectors MUST ignore any source row dated strictly after `as_of` (UTC
    calendar day for date-typed columns, end-of-day UTC for datetime columns).
    Production callers (`signal_builder.run_daily_cycle`) pass `None`, which
    preserves pre-I.5 behaviour. Look-ahead defense is the detector's
    responsibility — the backtest orchestrator does NOT pre-filter the session.

    The `current_regime` parameter (PR I.4, rule D26) is the regime-
    conditional threshold seam. When set, detectors MAY scale their
    thresholds based on the current `MarketRegime` (today only
    `MagnitudeDetector` does — see `pipeline/regime_thresholds.py`). When
    `None`, detectors MUST use base thresholds. The orchestrator
    (`signal_builder.run_daily_cycle` / backtest) owns the regime fetch and
    threads it through — detectors MUST NOT query `RegimeSnapshot`
    themselves (single source of truth, no redundant queries).
    """

    name: str
    signal_type: str

    async def detect(
        self,
        session: AsyncSession,
        *,
        as_of: date | None = None,
        current_regime: MarketRegime | None = None,
    ) -> list[DetectorHit]: ...


ALL_DETECTORS: list[Detector] = []


# Concrete detectors registered explicitly here so the registry has a single,
# greppable composition point — same pattern as `ALL_ROUTERS` / `STARTUP_TASKS`.
# Detector modules MUST NOT self-register at import time; that would tie the
# registry to import-order surprises.
#
# Imports placed at module bottom so `ALL_DETECTORS` and `Detector` are defined
# before detector modules (which import them via the package) are loaded.
from etfpulse.config import settings  # noqa: E402
from etfpulse.pipeline.detectors.acceleration import AccelerationDetector  # noqa: E402
from etfpulse.pipeline.detectors.divergence import DivergenceDetector  # noqa: E402
from etfpulse.pipeline.detectors.flow_anomaly import FlowAnomalyDetector  # noqa: E402
from etfpulse.pipeline.detectors.magnitude import MagnitudeDetector  # noqa: E402
from etfpulse.pipeline.detectors.regime_shift import RegimeShiftDetector  # noqa: E402

# Detector thresholds (issue #33). Each detector's __init__ accepts
# overrides; here we wire env-driven defaults so ops can tune without
# code deploys. Tests that need tight values still construct detectors
# directly with explicit kwargs — those bypass settings entirely.
ALL_DETECTORS.extend(
    [
        FlowAnomalyDetector(
            lookback_days=settings.flow_anomaly_lookback_days,
            min_streak_length=settings.flow_anomaly_min_streak_length,
        ),
        MagnitudeDetector(
            lookback_days=settings.magnitude_lookback_days,
            percentile_threshold=settings.magnitude_percentile_threshold,
            min_history_days=settings.magnitude_min_history_days,
        ),
        AccelerationDetector(
            window=settings.acceleration_window,
            change_threshold=settings.acceleration_change_threshold,
            min_slope_old_usd=settings.acceleration_min_slope_old_usd,
        ),
        DivergenceDetector(
            lookback_days=settings.divergence_lookback_days,
            min_price_change_pct=settings.divergence_min_price_change_pct,
            min_flow_sum_usd=settings.divergence_min_flow_sum_usd,
        ),
        RegimeShiftDetector(),
    ]
)


__all__ = ["ALL_DETECTORS", "Detector", "DetectorHit", "compute_fingerprint"]
