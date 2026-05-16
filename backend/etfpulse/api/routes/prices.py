"""Public spot-price API — live BTC + ETH prices for the top-nav strip.

One endpoint, both assets, 30s in-process TTL cache. Reuses
`pipeline.prices.get_spot_price_with_source` so the same primary/fallback
chain (SoSoValue → Binance) that anchors `Signal.price_at_creation` also
drives this surface. Public (no auth) — same Phase-1 posture as
`/api/signals` and `/api/regime`.

Cache rationale: the top-nav fetches once per page mount + every 60s
thereafter. With multiple tabs open and a few visitors, a naive
implementation would exceed SoSoValue's 100 req/min quickly. A 30s TTL
covers a moderate visitor burst while still updating twice a minute.

Concurrency: the miss path is serialized with an `asyncio.Lock` so a
burst of N concurrent first-paint requests fires exactly one upstream
fetch, not 2N. Without the lock, ASGI workers servicing parallel requests
would each see a miss on a cold/expired cache and stampede SoSoValue.

`source` reports the provider that answered for at least one asset this
tick. If BTC came from SoSoValue and ETH fell over to Binance, we report
"mixed" so the UI can surface the partial-outage state without lying about
provenance. The common-case single-source response is what the UI optimises
for; "mixed" is rare-but-not-impossible.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import structlog
from cachetools import TTLCache
from fastapi import APIRouter

from etfpulse.api.schemas.prices import SpotPrices
from etfpulse.pipeline.prices import get_spot_price_with_source

log = structlog.get_logger()
router = APIRouter(prefix="/prices", tags=["prices"])


_SPOT_CACHE_TTL_SEC = 30
_SPOT_CACHE_KEY = "current"
# Single-key cache — there's exactly one "current spot for both assets" at
# any moment. cachetools.TTLCache handles expiry transparently on get/set.
_spot_cache: TTLCache[str, SpotPrices] = TTLCache(maxsize=1, ttl=_SPOT_CACHE_TTL_SEC)

# Stampede guard for the miss path. After the lock is acquired, re-check
# the cache before fetching — the previous holder may have populated it
# while we were waiting.
_spot_fetch_lock = asyncio.Lock()


@router.get("/spot", response_model=SpotPrices)
async def get_spot_prices() -> SpotPrices:
    """Return BTC + ETH spot prices in a single response.

    Cache hit: zero upstream calls. Cache miss: at most one call per asset
    per provider in the primary/fallback chain, regardless of concurrent
    request count (asyncio.Lock + double-check). Both providers failing
    for BOTH assets returns nulls + null source (still 200 — the frontend
    hides the strip cleanly rather than treating it as fatal).
    """
    cached = _spot_cache.get(_SPOT_CACHE_KEY)
    if cached is not None:
        return cached

    async with _spot_fetch_lock:
        # Double-checked locking — a prior holder of the lock may have
        # populated the cache while we waited. Skip the fetch in that case.
        cached = _spot_cache.get(_SPOT_CACHE_KEY)
        if cached is not None:
            return cached

        # Parallel fetch — both assets share the same primary/fallback chain
        # and run independently. Sequential await would double the cold-cache
        # latency for no benefit.
        btc_result, eth_result = await asyncio.gather(
            get_spot_price_with_source("BTC"),
            get_spot_price_with_source("ETH"),
        )

        sources = {r[1] for r in (btc_result, eth_result) if r is not None}
        if len(sources) == 1:
            source: str | None = sources.pop()
        elif len(sources) > 1:
            # Cross-provider tick — UI surfaces this as a hint that one
            # chain is degraded. Rare in practice; explicit is better than
            # averaging.
            source = "mixed"
        else:
            source = None

        fresh = SpotPrices(
            btc=float(btc_result[0]) if btc_result is not None else None,
            eth=float(eth_result[0]) if eth_result is not None else None,
            source=source,
            fetched_at=datetime.now(UTC),
        )
        _spot_cache[_SPOT_CACHE_KEY] = fresh
        return fresh
