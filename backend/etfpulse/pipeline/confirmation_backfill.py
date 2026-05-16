"""One-shot backfill — populate Signal.confirmation_score for historical rows.

PR I.2. Mirrors the contract of `pipeline.price_backfill`:
    - Pure logic here, CLI wrapper in `scripts/backfill_confirmation.py`.
    - D14 — does NOT commit; caller owns the transaction.
    - Idempotent — only touches rows where `confirmation_score IS NULL` AND
      we can derive a direction (skipping "wait" / AI-failed rows whose
      score is permanently NULL by design).

Why this is needed: PR I.2 adds the column nullable, so existing signals
land with `confirmation_score=NULL`. Without backfill the FE would show
the "no score" placeholder for every historical signal — looks like the
feature broke. Running this once after migrate fills every eligible row
based on the historical regime + price window.

Historical regime: PR A.1 (issue #59) added `trigger_data.regime_at_creation`
so newer signals carry the regime they were built against. For older
signals without that block, fall back to the closest `RegimeSnapshot`
strictly before `signal.created_at` (None = no signal → regime factor
votes 0, which is the same result as UNCERTAIN at build time).

Historical price: Binance daily klines retain years; SoSoValue keeps a
few months. We pin to `signal.price_source` (falling back to "binance"
when NULL on pre-Stage-7 legacy rows) so the lookback uses the same
provider the signal originally saw — same rationale as `_evaluate_one`.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any, cast

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from etfpulse.config import settings
from etfpulse.models import MarketRegime, RegimeSnapshot, Signal
from etfpulse.pipeline.factors import compute_confirmation, direction_sign_from_action
from etfpulse.pipeline.prices import Asset, PriceSource

log = structlog.get_logger()


_SUPPORTED_ASSETS: frozenset[str] = frozenset({"BTC", "ETH"})
_VALID_PRICE_SOURCES: frozenset[str] = frozenset({"sosovalue", "binance"})


async def backfill_null_confirmation(
    session: AsyncSession,
    *,
    limit: int | None = None,
) -> dict[str, int]:
    """Score every eligible signal that landed with NULL `confirmation_score`.

    Returns a 5-key summary dict suitable for the CLI to print + ops
    logs. Eligible = AI succeeded (`ai_analysis IS NOT NULL`) AND no
    score yet (`confirmation_score IS NULL`). Within that set:

      - "wait" / unknown action → permanently NULL → counted as
        `skipped_no_direction`. Same handling as new-signal path.
      - asset not in {BTC, ETH} → counted as `skipped_unsupported_asset`
        (MARKET sentinels and any future cross-asset signals).
      - factor scoring failure → counted as `errored`; row stays NULL,
        operator can re-run (D13 catch-and-continue per signal).
      - scored successfully → counted as `updated`.

    `limit` caps the batch size to bound upstream klines fetches; absent
    = unlimited. Each scored signal triggers one klines fetch (Binance is
    rate-limit-friendly so 100s of rows per run is fine, but the cap
    exists for operator control).

    D14 — does NOT commit. Caller's session contextmanager owns it.
    """
    summary: dict[str, int] = {
        "candidates": 0,
        "updated": 0,
        "skipped_no_direction": 0,
        "skipped_unsupported_asset": 0,
        "errored": 0,
    }

    stmt = (
        select(Signal)
        .where(
            Signal.confirmation_score.is_(None),
            Signal.ai_analysis.is_not(None),
            Signal.confidence.is_not(None),
        )
        .order_by(Signal.created_at)
    )
    if limit is not None:
        stmt = stmt.limit(limit)
    signals = list((await session.execute(stmt)).scalars().all())
    summary["candidates"] = len(signals)

    for signal in signals:
        try:
            updated = await _score_one(session, signal, summary)
            if updated:
                summary["updated"] += 1
        except Exception as exc:  # noqa: BLE001 — per-signal D13 catch
            summary["errored"] += 1
            log.exception(
                "confirmation_backfill_signal_failed",
                signal_id=signal.id,
                error_type=type(exc).__name__,
                error=str(exc),
            )

    # Flush so the caller's `session.refresh(signal)` (in tests) and the
    # script's `commit()` (in production) both see the in-memory mutations.
    # Same pattern as `pipeline.track_record.evaluate_pending_outcomes`.
    # D14 — flush is NOT commit; the transaction boundary stays the caller's.
    await session.flush()
    log.info("confirmation_backfill_summary", **summary)
    return summary


async def _score_one(session: AsyncSession, signal: Signal, summary: dict[str, int]) -> bool:
    """Score one signal in place. Returns True iff the signal was updated.

    Mutates `summary` for skip categories so the caller's counter logic
    stays declarative.
    """
    # Direction guard — same contract as build_signal's hook.
    suggested = _suggested_action_from(signal.ai_analysis)
    if direction_sign_from_action(suggested) == 0:
        summary["skipped_no_direction"] += 1
        return False

    if signal.asset not in _SUPPORTED_ASSETS:
        summary["skipped_unsupported_asset"] += 1
        return False

    # `Signal.price_source` is nullable (pre-Stage-7 rows). Default to
    # "binance" because it's the fallback provider in `prices.py` and the
    # historical-kline-richest of the two. mypy can't narrow `str | None`
    # to `PriceSource` after the `in` check so the cast pins the literal.
    raw_source = signal.price_source or "binance"
    if raw_source not in _VALID_PRICE_SOURCES:
        # Defensive: a future writer that stamps an unknown provider tag.
        # Fall through to "binance" rather than crash.
        raw_source = "binance"
    price_source: PriceSource = cast(PriceSource, raw_source)

    regime = _historical_regime(signal)
    if regime is None:
        regime = await _closest_regime_snapshot(session, signal.created_at)

    asset: Asset = cast(Asset, signal.asset)
    result = await compute_confirmation(
        suggested_action=suggested,
        asset=asset,
        signal_type=signal.signal_type,
        price_source=price_source,
        reference_time=signal.created_at,
        regime=regime,
        window_hours=settings.factor_price_window_hours,
        min_pct=settings.factor_price_min_pct,
    )
    if result is None:
        # Reached only if direction_sign was >0 but compute_confirmation
        # short-circuited anyway. Theoretically unreachable given the guard
        # above, but defensive: count it as no-direction rather than crash.
        summary["skipped_no_direction"] += 1
        return False

    signal.confirmation_score = result.score
    signal.factor_votes = dict(result.votes)
    return True


def _suggested_action_from(ai_analysis: dict[str, Any] | None) -> str | None:
    """Pull `suggested_action` out of the JSONB blob with type safety.

    `ai_analysis` is typed `dict | None` on the column but JSONB
    technically allows non-dict values (lists, scalars) — defend
    against that without crashing the backfill loop.
    """
    if not isinstance(ai_analysis, dict):
        return None
    suggested = ai_analysis.get("suggested_action")
    return suggested if isinstance(suggested, str) else None


def _historical_regime(signal: Signal) -> MarketRegime | None:
    """Read the regime the signal was built against from its trigger_data.

    PR A.1 (issue #59 fix) embeds the regime at signal creation under
    `trigger_data.regime_at_creation.regime`. Older signals (pre-A.1)
    don't have this block — caller falls back to a RegimeSnapshot lookup.
    """
    if not isinstance(signal.trigger_data, dict):
        return None
    regime_block = signal.trigger_data.get("regime_at_creation")
    if not isinstance(regime_block, dict):
        return None
    raw = regime_block.get("regime")
    if not isinstance(raw, str):
        return None
    try:
        return MarketRegime(raw)
    except ValueError:
        # Unknown enum value — likely a future regime label this code
        # doesn't recognise. Treat as "no regime" rather than crash.
        return None


async def _closest_regime_snapshot(session: AsyncSession, before: datetime) -> MarketRegime | None:
    """Latest RegimeSnapshot strictly before `before`, or None if no such
    snapshot exists.

    Used for pre-A.1 signals that lack `trigger_data.regime_at_creation`.
    Returning None matches the build-time fallback (regime factor votes 0
    when no snapshot exists yet) so old signals score identically to how
    they would have at build time.
    """
    stmt = (
        select(RegimeSnapshot)
        .where(RegimeSnapshot.captured_at < before)
        .order_by(RegimeSnapshot.captured_at.desc())
        .limit(1)
    )
    row = (await session.execute(stmt)).scalar_one_or_none()
    if row is None or not row.regime:
        return None
    try:
        return MarketRegime(row.regime)
    except ValueError:
        return None


# Re-export Decimal so importers don't reach back into `decimal` directly
# — keeps the type used by `min_pct` documented in one place.
__all__ = ["Decimal", "backfill_null_confirmation"]
