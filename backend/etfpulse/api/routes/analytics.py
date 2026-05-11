"""Public analytics API — diagnostic breakdown over `signal_outcomes`.

Stage 8-P10. Backs the public `/analytics` page (`pages/Analytics.tsx`),
which shows per-detector / per-asset / per-confidence / per-direction hit
rates + MFE/MAE distribution histograms — the "is this signal system
actually calibrated?" view that complements the simpler `/track-record`
list.

No auth — same Phase 1 policy as `/api/track-record` and `/api/signals`
(open_issues #43). The information is meta-aggregate (no per-user data,
no signal IDs), so there's nothing here that needs gating.

Empty-DB / cold-boot returns 200 with `total_outcomes=0` and every
breakdown an empty list. The FE handles that as the empty state, NOT
an error — readers visiting before the first signals have evaluated
shouldn't see a 503.

Caching: handled inside `pipeline.analytics.get_cached_track_record_breakdown`
(5-min in-process TTLCache). No HTTP-layer cache headers — the FE
TanStack hook owns request-side caching.
"""

from __future__ import annotations

import structlog
from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from etfpulse.api.deps import get_db_session
from etfpulse.api.schemas.analytics import TrackRecordBreakdownOut
from etfpulse.pipeline.analytics import get_cached_track_record_breakdown

log = structlog.get_logger()
router = APIRouter(prefix="/analytics", tags=["analytics"])


@router.get("/breakdown", response_model=TrackRecordBreakdownOut)
async def get_breakdown(
    session: AsyncSession = Depends(get_db_session),
) -> TrackRecordBreakdownOut:
    """Return the diagnostic breakdown — 4 categorical slices + 2 histograms."""
    breakdown = await get_cached_track_record_breakdown(session)
    return TrackRecordBreakdownOut.from_breakdown(breakdown)
