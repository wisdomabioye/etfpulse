"""Public calibration API — empirical reliability curve.

PR I.1. On-read aggregation: no daily job, no stored snapshot table. The
route caches the computed grid per (ai_prompt_version, lookback_days,
bucket_size, min_samples) for `settings.calibration_cache_ttl_seconds`
(default 300s) so DB cost is paid at most once per TTL window per worker
regardless of FE traffic.

Cache miss path is serialised with an `asyncio.Lock` — same double-checked
locking pattern as `routes/prices.py`'s spot-price stampede guard. A burst
of concurrent first-paint requests fires exactly one aggregation, not N.

Empty cohort behaviour: returns 200 with the full bucket grid (every cell
present), each cell carrying `n_samples=0` and `hit_rate=null`. 404 would
conflate "endpoint missing" with "no data yet" — the FE specifically wants
to distinguish those.
"""

from __future__ import annotations

import asyncio

import structlog
from cachetools import TTLCache
from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from etfpulse.api.deps import get_db_session
from etfpulse.api.schemas.calibration import CalibrationBucketOut, CalibrationResponse
from etfpulse.config import settings
from etfpulse.pipeline.analysis import AI_PROMPT_VERSION
from etfpulse.pipeline.calibration import compute_calibration

log = structlog.get_logger()
router = APIRouter(prefix="/track-record/calibration", tags=["track-record"])


# Cache key includes every aggregation input that could change the result:
# (version, lookback, bucket_size, min_samples). bucket_size + min_samples
# come from settings today, but baking them into the key future-proofs the
# cache against per-request overrides we might add later (e.g. operator
# tooling that sweeps min_samples to find the inflection point).
_CalibrationCacheKey = tuple[str, int, int, int]

# `maxsize=32` is comfortably above what we expect: one entry per
# (active_prompt_version × lookback_days) combo, and lookback_days has
# one canonical value (`settings.calibration_lookback_days`) at any time.
# We size for headroom on the case where an operator query-string overrides
# lookback_days repeatedly.
_calibration_cache: TTLCache[_CalibrationCacheKey, CalibrationResponse] = TTLCache(
    maxsize=32, ttl=settings.calibration_cache_ttl_seconds
)
_calibration_lock = asyncio.Lock()


@router.get("", response_model=CalibrationResponse)
async def get_calibration(
    ai_prompt_version: str | None = Query(
        default=None,
        pattern=r"^v[0-9]+$",
        description=(
            "Restrict to signals built with this prompt version. "
            "Defaults to the active `AI_PROMPT_VERSION` so the FE always "
            "shows the current cohort without coordinating with the backend."
        ),
    ),
    lookback_days: int | None = Query(
        default=None,
        ge=1,
        le=730,
        description=(
            "Override the rolling window (default: "
            "`settings.calibration_lookback_days`). Bounded at 2 years — "
            "older outcomes are dominated by stale prompt cohorts."
        ),
    ),
    session: AsyncSession = Depends(get_db_session),
) -> CalibrationResponse:
    """Reliability curve per (confidence bucket × horizon).

    Returns the full grid (every bucket × horizon combination) so the FE
    can render fixed-position tiles with no layout shift between empty and
    populated states. Empty / under-min-samples cells carry `hit_rate=null`.
    """
    effective_version = ai_prompt_version or AI_PROMPT_VERSION
    effective_lookback = (
        lookback_days if lookback_days is not None else settings.calibration_lookback_days
    )

    key: _CalibrationCacheKey = (
        effective_version,
        effective_lookback,
        settings.calibration_bucket_size,
        settings.calibration_min_samples_per_bucket,
    )
    cached = _calibration_cache.get(key)
    if cached is not None:
        return cached

    async with _calibration_lock:
        # Double-checked locking — a prior holder may have populated the
        # cache while we waited. Same pattern as routes/prices.py:get_spot_prices.
        cached = _calibration_cache.get(key)
        if cached is not None:
            return cached

        report = await compute_calibration(
            session,
            ai_prompt_version=effective_version,
            lookback_days=effective_lookback,
            min_samples=settings.calibration_min_samples_per_bucket,
            bucket_size=settings.calibration_bucket_size,
        )

        # `model_validate` reads attributes off the internal dataclass
        # directly — see `CalibrationBucketOut.model_config.from_attributes`.
        # Avoids a hand-rolled field-by-field copy that would silently
        # drift if a new field is added to the dataclass.
        response = CalibrationResponse(
            ai_prompt_version=report.ai_prompt_version,
            lookback_days=report.lookback_days,
            min_samples=report.min_samples,
            bucket_size=report.bucket_size,
            buckets=[CalibrationBucketOut.model_validate(b) for b in report.buckets],
        )
        _calibration_cache[key] = response
        return response
