"""Route-level tests for `GET /api/track-record/calibration`.

The orchestrator's correctness is pinned in `test_pipeline/test_calibration.py`.
Here we focus on:
  - URL routing + response shape
  - default vs override of `ai_prompt_version` + `lookback_days`
  - 200 + empty buckets on cold DB (not 404)
  - 422 on a bogus `ai_prompt_version` query param
  - cache miss serialisation (asyncio.Lock prevents stampede)
"""

from __future__ import annotations

import asyncio

import httpx
import pytest

from etfpulse.api.deps import get_db_session
from etfpulse.api.routes import calibration as calibration_route
from etfpulse.app import create_app
from etfpulse.pipeline.analysis import AI_PROMPT_VERSION
from tests._helpers.seed_outcomes import seed_signal_with_outcome


@pytest.fixture(autouse=True)
def _clear_calibration_cache():
    """Each test starts with an empty cache so prior writes don't leak."""
    calibration_route._calibration_cache.clear()
    yield
    calibration_route._calibration_cache.clear()


@pytest.fixture
async def client(db_session):
    """Route client that shares the test's per-test transaction session.

    Mirrors the pattern in `test_app/test_track_record.py` — overrides
    the `get_db_session` dependency so the route reads the SAME rows the
    test seeded, without anyone needing to `commit()`. Required because
    the per-test transaction rolls back at teardown; a real `commit()`
    is a no-op against the wrapping connection-level transaction.
    """
    app = create_app()

    async def _override():
        yield db_session

    app.dependency_overrides[get_db_session] = _override
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


# Route-level alias on the shared seed helper. Pre-PR-I.1 cleanup this
# file had its own near-identical inline copy; collapsed so calibration /
# track-record / future backtest tests share one definition of "evaluated
# outcome." The `client` fixture overrides `get_db_session` to the test's
# `db_session`, so flushes are visible to the route without a commit.
_seed_outcome = seed_signal_with_outcome


class TestCalibrationRouteHappyPath:
    async def test_returns_200_with_empty_grid_on_cold_db(self, client):
        # Cold start: no outcomes. Returns 200 (not 404) with the full
        # bucket grid materialised — every cell n=0, hit_rate=null.
        r = await client.get("/api/track-record/calibration")
        assert r.status_code == 200
        body = r.json()
        assert body["ai_prompt_version"] == AI_PROMPT_VERSION
        # 5 confidence buckets × 4 horizons = 20 cells.
        assert len(body["buckets"]) == 20
        for bucket in body["buckets"]:
            assert bucket["n_samples"] == 0
            assert bucket["hit_rate"] is None

    async def test_returns_populated_grid_after_seeding(self, db_session, client):
        # Seed 3 wins at confidence 8 + swing — expect that cell populated,
        # all other cells still zero.
        for i in range(3):
            await _seed_outcome(db_session, confidence=8, hit_target=True, key=f"r-{i}")

        # Use a min_samples=1 override via the route: actually we don't
        # expose min_samples as a query param (it's a config knob), so
        # we monkey-patch the settings for this test.
        # — Approach: trust that the default min_samples (20) is too high
        # for 3 outcomes, so the route should report counts but null rate.
        # That's the contract; verify it.
        r = await client.get("/api/track-record/calibration")
        assert r.status_code == 200
        body = r.json()
        # Find the (7-8) × swing cell.
        target = next(
            b
            for b in body["buckets"]
            if b["bucket_floor"] == 7 and b["bucket_ceiling"] == 8 and b["horizon"] == "swing"
        )
        assert target["n_samples"] == 3
        assert target["wins"] == 3
        # Below the default min_samples (20) — rate hidden.
        assert target["hit_rate"] is None

    async def test_lookback_days_query_param_overrides_default(self, client):
        # `lookback_days=7` is a valid override; the route reads it and
        # passes it through. Cold DB so the response just echoes the
        # input.
        r = await client.get("/api/track-record/calibration?lookback_days=7")
        assert r.status_code == 200
        body = r.json()
        assert body["lookback_days"] == 7

    async def test_ai_prompt_version_query_param_filters_cohort(self, db_session, client):
        # Seed two cohorts: v2 + v3 (current). Asking for v2 should
        # return that cohort; asking for v3 should return its own.
        await _seed_outcome(
            db_session, confidence=8, hit_target=True, key="v2", ai_prompt_version="v2"
        )
        await _seed_outcome(
            db_session, confidence=8, hit_target=True, key="v3", ai_prompt_version="v3"
        )

        # Min_samples default (20) hides the rate, but n_samples is exposed
        # — that's enough to verify the filter works.
        v2_r = await client.get("/api/track-record/calibration?ai_prompt_version=v2")
        v3_r = await client.get("/api/track-record/calibration?ai_prompt_version=v3")
        assert v2_r.status_code == 200
        assert v3_r.status_code == 200

        def n_at_78_swing(body):
            return next(
                b["n_samples"]
                for b in body["buckets"]
                if b["bucket_floor"] == 7 and b["bucket_ceiling"] == 8 and b["horizon"] == "swing"
            )

        assert n_at_78_swing(v2_r.json()) == 1
        assert n_at_78_swing(v3_r.json()) == 1
        # The v2 response carries v2 as its echoed version.
        assert v2_r.json()["ai_prompt_version"] == "v2"
        assert v3_r.json()["ai_prompt_version"] == "v3"


class TestCalibrationRouteValidation:
    async def test_invalid_ai_prompt_version_returns_422(self, client):
        # Doesn't match `^v[0-9]+$`. FastAPI/pydantic should 422.
        r = await client.get("/api/track-record/calibration?ai_prompt_version=garbage")
        assert r.status_code == 422

    async def test_lookback_days_zero_returns_422(self, client):
        # ge=1 on the query param.
        r = await client.get("/api/track-record/calibration?lookback_days=0")
        assert r.status_code == 422

    async def test_lookback_days_too_large_returns_422(self, client):
        # le=730 on the query param.
        r = await client.get("/api/track-record/calibration?lookback_days=999")
        assert r.status_code == 422


class TestCalibrationRouteCache:
    async def test_concurrent_cache_miss_serialises_aggregation(
        self, db_session, client, monkeypatch
    ):
        """Stampede guard — N concurrent first-paint requests should
        fire AT MOST ONE aggregation against the DB, not N. Same
        contract as `routes/prices.py`'s spot-price cache.
        """
        await _seed_outcome(db_session, confidence=8, hit_target=True, key="stampede")

        call_count = {"n": 0}
        real_compute = calibration_route.compute_calibration

        async def counting_compute(*args, **kwargs):
            call_count["n"] += 1
            # Small await yields control so other concurrent requests
            # have a chance to enter the lock-wait state.
            await asyncio.sleep(0.01)
            return await real_compute(*args, **kwargs)

        monkeypatch.setattr(calibration_route, "compute_calibration", counting_compute)

        # Fire 5 concurrent requests.
        responses = await asyncio.gather(
            *[client.get("/api/track-record/calibration") for _ in range(5)]
        )
        for r in responses:
            assert r.status_code == 200
        # Exactly one of the 5 actually computed — the rest served from
        # the cache populated under the lock.
        assert call_count["n"] == 1
