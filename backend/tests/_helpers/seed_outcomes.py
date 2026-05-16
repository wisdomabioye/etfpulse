"""Shared signal+outcome seeding for tests that exercise public-stats reads.

Single source of truth for the `(Signal, SignalOutcome)` pair commonly
needed by track-record, calibration, dashboard hero, per-detector
breakdown, and (future) backtest tests. Three near-identical helpers
existed pre-PR I.1 — consolidated here per CLAUDE.md DRY rules.

The helper produces a Signal already deemed "evaluated" by the public
stats surfaces (matching `evaluated_outcomes_predicate()` in
`pipeline.track_record`). Tests that need a different lifecycle stage
(unevaluated, AI-failed, expired-but-unscored) override the relevant
keyword arg.

Contract:
  - `flush`-only; no `commit`. The caller's per-test transaction owns
    the boundary. Route-level tests that need the route's separate
    session to see the rows must use `app.dependency_overrides` on
    `get_db_session` (see `tests/test_app/test_track_record.py` pattern).
  - Returns the constructed `Signal` so callers can read `.id` for
    follow-up writes (e.g. seed a second outcome on the same signal).

Default values match the historical `tests/test_pipeline/test_track_record.py`
defaults (target=89500, price=84200) so existing stats-aggregation tests
migrate without behavioural drift.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from sqlalchemy.ext.asyncio import AsyncSession

from etfpulse.models import Signal, SignalOutcome
from etfpulse.pipeline.detectors import compute_fingerprint

# Sentinel for `evaluated_at` so the helper can distinguish "caller did
# not pass" (→ default to `now`) from "caller explicitly passed `None`"
# (→ persist NULL so the row tests the unevaluated filter).
_DEFAULT_EVALUATED_AT: object = object()


async def seed_signal_with_outcome(
    db_session: AsyncSession,
    *,
    key: str,
    confidence: int = 7,
    hit_target: bool | None = True,
    hit_stop: bool | None = None,
    direction: str = "long",
    asset: str = "BTC",
    signal_type: str = "flow_anomaly",
    ai_prompt_version: str = "v3",
    window_hours: int | None = 72,
    evaluated_at: datetime | None | object = _DEFAULT_EVALUATED_AT,
    target_price: Decimal | None = Decimal("89500"),
    price_at_signal: Decimal = Decimal("84200"),
    suggested_action: str = "consider long",
) -> Signal:
    """Seed one (Signal, SignalOutcome) pair, FLUSH (no commit).

    `evaluated_at` defaults to `now` (the typical "ready for stats"
    state). Pass `None` explicitly to seed an unevaluated outcome —
    used by tests that exercise the `evaluated_outcomes_predicate()`
    filter. Pass a specific `datetime` to test lookback-window filters.

    `window_hours=None` produces a `legacy` bucket row (NULL window +
    NULL scoring_version, the pre-v2 shape).
    """
    if evaluated_at is _DEFAULT_EVALUATED_AT:
        effective_eval_at: datetime | None = datetime.now(UTC)
    else:
        # Runtime type guarantees datetime|None here; mypy can't narrow
        # an `object` sentinel via `is`.
        effective_eval_at = evaluated_at  # type: ignore[assignment]

    t0 = datetime.now(UTC) - timedelta(hours=80)
    signal = Signal(
        signal_type=signal_type,
        asset=asset,
        trigger_data={"streak_days": 4},
        ai_analysis={"suggested_action": suggested_action, "headline": "x"},
        confidence=confidence,
        status="alerted",
        price_at_creation=price_at_signal,
        price_source="binance",
        ai_prompt_version=ai_prompt_version,
        fingerprint=compute_fingerprint("seed-helper", key),
        signal_date=t0.date(),
    )
    signal.created_at = t0
    db_session.add(signal)
    await db_session.flush()

    db_session.add(
        SignalOutcome(
            signal_id=signal.id,
            asset=asset,
            signal_type=signal_type,
            direction=direction,
            confidence=confidence,
            target_price=target_price,
            price_at_signal=price_at_signal,
            window_hours=window_hours,
            scoring_version="v2" if window_hours is not None else None,
            hit_target=hit_target,
            hit_stop=hit_stop,
            evaluated_at=effective_eval_at,
        )
    )
    await db_session.flush()
    return signal
