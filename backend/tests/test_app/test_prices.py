"""GET /api/prices/spot endpoint tests.

Covers:
  - 200 with BTC + ETH + source when both providers answer
  - TTL cache short-circuits the second call within the window
  - One asset fails → still 200 with that asset null; surviving asset's
    source reported
  - Both fail → 200 with all-null prices + null source (not 503; the UI
    hides the strip cleanly)
  - Cross-provider tick → source="mixed"
  - Concurrent cache miss is serialized — N parallel requests fire exactly
    one upstream fetch per asset, not N (stampede guard).
"""

from __future__ import annotations

import asyncio
from decimal import Decimal

import httpx
import pytest
from httpx import ASGITransport

from etfpulse.app import create_app


@pytest.fixture
async def client():
    app = create_app()
    transport = ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


@pytest.fixture(autouse=True)
def reset_spot_cache():
    """Clear the in-process TTL cache between tests so each case starts cold."""
    from etfpulse.api.routes.prices import _spot_cache

    _spot_cache.clear()
    yield
    _spot_cache.clear()


class TestSpotPrices:
    async def test_both_assets_succeed_single_source(self, client, monkeypatch):
        async def _fake(asset: str) -> tuple[Decimal, str]:
            return Decimal("82352.50") if asset == "BTC" else Decimal("3140.10"), "sosovalue"

        monkeypatch.setattr("etfpulse.api.routes.prices.get_spot_price_with_source", _fake)
        r = await client.get("/api/prices/spot")
        assert r.status_code == 200
        body = r.json()
        assert body["btc"] == 82352.50
        assert body["eth"] == 3140.10
        assert body["source"] == "sosovalue"
        assert "fetched_at" in body

    async def test_cache_short_circuits_second_call(self, client, monkeypatch):
        calls = {"n": 0}

        async def _fake(asset: str) -> tuple[Decimal, str]:
            calls["n"] += 1
            return Decimal("100"), "sosovalue"

        monkeypatch.setattr("etfpulse.api.routes.prices.get_spot_price_with_source", _fake)
        await client.get("/api/prices/spot")
        await client.get("/api/prices/spot")
        # First call resolves BTC + ETH (2 invocations). Second call hits
        # cache → no further invocations.
        assert calls["n"] == 2

    async def test_one_asset_fails_other_reported(self, client, monkeypatch):
        async def _fake(asset: str):
            if asset == "BTC":
                return Decimal("82000"), "sosovalue"
            return None

        monkeypatch.setattr("etfpulse.api.routes.prices.get_spot_price_with_source", _fake)
        r = await client.get("/api/prices/spot")
        assert r.status_code == 200
        body = r.json()
        assert body["btc"] == 82000.0
        assert body["eth"] is None
        assert body["source"] == "sosovalue"

    async def test_both_fail_returns_nulls(self, client, monkeypatch):
        async def _fake(asset: str):
            return None

        monkeypatch.setattr("etfpulse.api.routes.prices.get_spot_price_with_source", _fake)
        r = await client.get("/api/prices/spot")
        assert r.status_code == 200
        body = r.json()
        assert body["btc"] is None
        assert body["eth"] is None
        assert body["source"] is None

    async def test_mixed_sources_reported(self, client, monkeypatch):
        async def _fake(asset: str):
            if asset == "BTC":
                return Decimal("82000"), "sosovalue"
            return Decimal("3100"), "binance"

        monkeypatch.setattr("etfpulse.api.routes.prices.get_spot_price_with_source", _fake)
        r = await client.get("/api/prices/spot")
        assert r.status_code == 200
        assert r.json()["source"] == "mixed"

    async def test_concurrent_cache_miss_fires_one_upstream_fetch(self, client, monkeypatch):
        """Stampede guard: N concurrent requests on a cold cache must
        resolve to exactly one upstream fetch per asset, not N. Without the
        `_spot_fetch_lock` + double-checked re-read, parallel awaits would
        each see a miss and hammer SoSoValue."""
        calls = {"n": 0}
        # Slow fake so all N requests are guaranteed to be in-flight before
        # the first one finishes populating the cache.
        gate = asyncio.Event()

        async def _fake(asset: str):
            calls["n"] += 1
            await gate.wait()
            return Decimal("100"), "sosovalue"

        monkeypatch.setattr("etfpulse.api.routes.prices.get_spot_price_with_source", _fake)

        async def _hit():
            return await client.get("/api/prices/spot")

        # Launch 5 concurrent requests, release the gate, await all.
        tasks = [asyncio.create_task(_hit()) for _ in range(5)]
        # Yield once so all tasks reach the lock + fetch await before the gate trips.
        await asyncio.sleep(0)
        gate.set()
        results = await asyncio.gather(*tasks)

        assert all(r.status_code == 200 for r in results)
        # Exactly 2 invocations — one for BTC, one for ETH. The other 4
        # callers re-read the cache after acquiring the lock and skip.
        assert calls["n"] == 2
