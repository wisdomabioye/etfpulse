"""Public regime API — latest market-regime classification.

Stage 7-P7. Reads the most recent `RegimeSnapshot` (one row, single
ORDER BY captured_at DESC LIMIT 1 — wrapped in `regime_monitor.get_latest_regime`
so this route and the `regime_shift` detector cannot drift on what
"latest" means).

503 on empty table: a fresh deploy has no snapshots until the first
daily cycle has run. We surface that as 503 (per the
register_exception_handlers opaque-error policy) rather than 404 — a
504 would suggest "your request timed out", and 200-with-null-fields
would let stale clients silently render an empty card.

No auth (Wave 1 scope per open_issues #43, same as /api/signals).
"""

from __future__ import annotations

import structlog
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from etfpulse.api.deps import get_db_session
from etfpulse.api.schemas.regime import RegimeResponse
from etfpulse.models import REGIME_MACRO_EVENTS_KEY
from etfpulse.pipeline.regime_monitor import get_latest_regime

log = structlog.get_logger()
router = APIRouter(prefix="/regime", tags=["regime"])


@router.get("", response_model=RegimeResponse)
async def get_regime(session: AsyncSession = Depends(get_db_session)) -> RegimeResponse:
    """Return the latest classification + structured reasoning."""
    snapshot = await get_latest_regime(session)
    if snapshot is None:
        # Cold-boot — no snapshots yet. Opaque 503 per the project's
        # exception-handler policy: tell the client "upstream isn't ready",
        # don't expose internals about which table is empty.
        log.info("regime_endpoint_no_snapshot")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="regime not yet classified",
        )

    if snapshot.regime is None or snapshot.signal_posture is None or snapshot.confidence is None:
        # Defensive — pre-Stage-7 snapshot rows have NULL regime/posture/
        # confidence. The response schema requires non-null values, so
        # fail closed rather than serializing whatever happens to be there
        # or falling back to a fake confidence value.
        log.warning("regime_endpoint_legacy_snapshot", id=snapshot.id)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="regime not yet classified",
        )

    # Macro-events JSONB is wrapped as `{REGIME_MACRO_EVENTS_KEY: [...]}`
    # on insert (see signal_builder). Unwrap defensively — the wrapper
    # could be None (no events nearby) or absent (legacy rows).
    raw_macro = snapshot.macro_events or {}
    nearby_unknown: object = raw_macro.get(REGIME_MACRO_EVENTS_KEY, [])
    if isinstance(nearby_unknown, list):
        # `str(item)` is intentional defense-in-depth, NOT silent type
        # coercion — today only strings go in (signal_builder writes
        # `list[str]`), so this is a no-op for normal data. If a future
        # writer ever leaks a non-string (int, dict), we'd rather render
        # `"42"` in the UI than 500 the response. The CHECK on the column
        # only enforces enum membership of `regime`/`signal_posture`,
        # not the shape of `macro_events` JSONB.
        macro_events_nearby = [str(item) for item in nearby_unknown]
    else:
        macro_events_nearby = []

    # Pydantic validates `regime`/`signal_posture` against the response
    # Literal types at construction. If the classifier ever stamps a
    # value outside the enum, the route 500s loudly here rather than
    # serializing garbage. This is the desired behavior — silent value
    # drift would corrupt the track-record query.
    return RegimeResponse(
        regime=snapshot.regime,  # type: ignore[arg-type]  # validated by Pydantic Literal
        signal_posture=snapshot.signal_posture,  # type: ignore[arg-type]  # validated by Pydantic Literal
        confidence=snapshot.confidence,
        reasoning=snapshot.reasoning or {},
        macro_events_nearby=macro_events_nearby,
        classified_at=snapshot.captured_at,
    )
