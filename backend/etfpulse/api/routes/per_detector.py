"""Public per-detector precision API.

PR I.3. On-read aggregation — no daily job, no stored snapshot table.
The route caches the computed grid per (ai_prompt_version, lookback_days,
min_samples) for `settings.calibration_cache_ttl_seconds` (reused — same
on-read pattern as calibration, same FE surface). DB cost is paid at most
once per TTL window per worker regardless of FE traffic.

Cache miss path is serialised with an `asyncio.Lock` — same double-checked
locking pattern as `routes/calibration.py` and `routes/prices.py`. A burst
of concurrent first-paint requests fires exactly one aggregation.

Empty cohort behaviour: returns 200 with the full grid (every registered
detector row present, every horizon cell present) — every cell carries
`n_samples=0` and `hit_rate=null`. 404 would conflate "endpoint missing"
with "no data yet"; the FE distinguishes those.
"""

from __future__ import annotations

import asyncio

import structlog
from cachetools import TTLCache
from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from etfpulse.api.deps import get_db_session
from etfpulse.api.schemas.per_detector import PerDetectorResponse
from etfpulse.config import settings
from etfpulse.pipeline.analysis import AI_PROMPT_VERSION
from etfpulse.pipeline.per_detector import compute_per_detector

log = structlog.get_logger()
router = APIRouter(prefix="/track-record/per-detector", tags=["track-record"])


# Cache key: every aggregation input that could change the result.
# `min_samples` is baked in even though it currently comes from a single
# settings value — keeps the cache safe against a future per-request
# override (operator tooling sweeping the threshold).
_PerDetectorCacheKey = tuple[str, int, int]

# `maxsize=32` matches calibration's headroom — one entry per
# (active_prompt_version × lookback_days × min_samples) tuple. Steady
# state has one canonical tuple; the headroom accommodates ad-hoc
# query-string overrides without thrashing the LRU.
_per_detector_cache: TTLCache[_PerDetectorCacheKey, PerDetectorResponse] = TTLCache(
    maxsize=32, ttl=settings.calibration_cache_ttl_seconds
)
_per_detector_lock = asyncio.Lock()


@router.get("", response_model=PerDetectorResponse)
async def get_per_detector(
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
            "`settings.calibration_lookback_days` — same setting as "
            "calibration; one knob per concept)."
        ),
    ),
    session: AsyncSession = Depends(get_db_session),
) -> PerDetectorResponse:
    """Per-detector precision grid (detector × horizon + across-horizons total).

    Returns the full grid so the FE renders stable rows regardless of
    cohort density. regime_shift is excluded from the response by design —
    PR I.3b (composite scoring for MARKET signals) will fold it in.
    """
    effective_version = ai_prompt_version or AI_PROMPT_VERSION
    effective_lookback = (
        lookback_days if lookback_days is not None else settings.calibration_lookback_days
    )
    effective_min_samples = settings.per_detector_min_samples

    key: _PerDetectorCacheKey = (
        effective_version,
        effective_lookback,
        effective_min_samples,
    )
    cached = _per_detector_cache.get(key)
    if cached is not None:
        return cached

    async with _per_detector_lock:
        # Double-checked locking — a prior holder may have populated the
        # cache while we waited.
        cached = _per_detector_cache.get(key)
        if cached is not None:
            return cached

        report = await compute_per_detector(
            session,
            ai_prompt_version=effective_version,
            lookback_days=effective_lookback,
            min_samples=effective_min_samples,
        )

        # `model_validate` recurses into the nested dataclass + dict-of-
        # dataclass fields via `from_attributes=True`. No hand-rolled
        # field-by-field copy = no drift when a new field lands on the
        # internal dataclass.
        response = PerDetectorResponse.model_validate(report)
        _per_detector_cache[key] = response
        return response
