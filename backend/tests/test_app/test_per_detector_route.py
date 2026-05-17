"""Route-level tests for `GET /api/track-record/per-detector` (PR I.3).

Aggregator correctness is pinned in `tests/test_pipeline/test_per_detector.py`.
Here we focus on:
  - URL routing + JSON contract (key set, regime_shift excluded)
  - default vs override of `ai_prompt_version` + `lookback_days`
  - 200 + full grid on cold DB (NOT 404)
  - 422 on invalid params
  - cache stampede guard (asyncio.Lock fires aggregation once)
"""

from __future__ import annotations

import asyncio

import httpx
import pytest

from etfpulse.api.deps import get_db_session
from etfpulse.api.routes import per_detector as per_detector_route
from etfpulse.app import create_app
from etfpulse.pipeline.analysis import AI_PROMPT_VERSION
from tests._helpers.seed_outcomes import seed_signal_with_outcome


@pytest.fixture(autouse=True)
def _clear_per_detector_cache():
    """Each test starts with an empty cache — prior writes can't leak."""
    per_detector_route._per_detector_cache.clear()
    yield
    per_detector_route._per_detector_cache.clear()


@pytest.fixture
async def client(db_session):
    """Route client that shares the test's per-test transaction session.

    Mirrors the calibration / track-record route fixtures: overrides
    `get_db_session` so the route reads the rows the test seeded without
    anyone needing to `commit()`.
    """
    app = create_app()

    async def _override():
        yield db_session

    app.dependency_overrides[get_db_session] = _override
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


_seed_outcome = seed_signal_with_outcome


class TestPerDetectorHappyPath:
    async def test_returns_200_with_full_grid_on_cold_db(self, client):
        r = await client.get("/api/track-record/per-detector")
        assert r.status_code == 200
        body = r.json()
        assert body["ai_prompt_version"] == AI_PROMPT_VERSION
        # 4 registered non-excluded detectors must always appear, in
        # ALL_DETECTORS order.
        types = [d["signal_type"] for d in body["detectors"]]
        assert types == ["flow_anomaly", "magnitude", "acceleration", "divergence"]
        # Every cell n=0, hit_rate null on cold start.
        for detector in body["detectors"]:
            assert detector["total"]["n_samples"] == 0
            assert detector["total"]["hit_rate"] is None
            for horizon in ("scalp", "swing", "position", "legacy"):
                cell = detector["horizons"][horizon]
                assert cell["n_samples"] == 0
                assert cell["hit_rate"] is None

    async def test_returns_populated_grid_after_seeding(self, db_session, client):
        for i in range(3):
            await _seed_outcome(
                db_session,
                signal_type="acceleration",
                hit_target=True,
                key=f"acc-{i}",
            )

        r = await client.get("/api/track-record/per-detector")
        assert r.status_code == 200
        body = r.json()
        acc = next(d for d in body["detectors"] if d["signal_type"] == "acceleration")
        assert acc["total"]["n_samples"] == 3
        assert acc["total"]["wins"] == 3
        # min_samples default is 3 → at threshold → hit_rate populated.
        assert acc["total"]["hit_rate"] == 1.0
        assert acc["total"]["ci_low"] is not None
        assert acc["total"]["ci_high"] is not None

    async def test_lookback_days_query_param_overrides_default(self, client):
        r = await client.get("/api/track-record/per-detector?lookback_days=7")
        assert r.status_code == 200
        body = r.json()
        assert body["lookback_days"] == 7

    async def test_ai_prompt_version_filters_cohort(self, db_session, client):
        # Different cohorts must not bleed into each other.
        for i in range(3):
            await _seed_outcome(
                db_session,
                signal_type="acceleration",
                hit_target=True,
                key=f"v2-{i}",
                ai_prompt_version="v2",
            )
        for i in range(3):
            await _seed_outcome(
                db_session,
                signal_type="acceleration",
                hit_target=False,
                key=f"v3-{i}",
                ai_prompt_version="v3",
            )

        v2_body = (await client.get("/api/track-record/per-detector?ai_prompt_version=v2")).json()
        v3_body = (await client.get("/api/track-record/per-detector?ai_prompt_version=v3")).json()

        v2_acc = next(d for d in v2_body["detectors"] if d["signal_type"] == "acceleration")
        v3_acc = next(d for d in v3_body["detectors"] if d["signal_type"] == "acceleration")
        assert v2_acc["total"]["wins"] == 3
        assert v2_acc["total"]["losses"] == 0
        assert v3_acc["total"]["wins"] == 0
        assert v3_acc["total"]["losses"] == 3


class TestPerDetectorValidation:
    async def test_invalid_ai_prompt_version_returns_422(self, client):
        r = await client.get("/api/track-record/per-detector?ai_prompt_version=garbage")
        assert r.status_code == 422

    async def test_lookback_days_zero_returns_422(self, client):
        r = await client.get("/api/track-record/per-detector?lookback_days=0")
        assert r.status_code == 422

    async def test_lookback_days_too_large_returns_422(self, client):
        r = await client.get("/api/track-record/per-detector?lookback_days=999")
        assert r.status_code == 422


class TestPerDetectorShape:
    """Schema key-set assertions — the canary for silent shape drift.

    PR I.2 had a real bug where a new column landed on the response without
    a test catching it; pinning the key set here means future shape changes
    must be explicit.
    """

    async def test_top_level_response_keys(self, client):
        body = (await client.get("/api/track-record/per-detector")).json()
        assert set(body.keys()) == {
            "ai_prompt_version",
            "lookback_days",
            "min_samples",
            "detectors",
        }

    async def test_detector_row_keys(self, client):
        body = (await client.get("/api/track-record/per-detector")).json()
        for detector in body["detectors"]:
            assert set(detector.keys()) == {"signal_type", "horizons", "total"}
            assert set(detector["horizons"].keys()) == {
                "scalp",
                "swing",
                "position",
                "legacy",
            }

    async def test_cell_keys(self, client):
        body = (await client.get("/api/track-record/per-detector")).json()
        expected_cell_keys = {
            "n_samples",
            "wins",
            "losses",
            "hit_rate",
            "ci_low",
            "ci_high",
        }
        for detector in body["detectors"]:
            assert set(detector["total"].keys()) == expected_cell_keys
            for cell in detector["horizons"].values():
                assert set(cell.keys()) == expected_cell_keys

    async def test_regime_shift_never_in_response(self, db_session, client):
        # Even if regime_shift signals were scored (they shouldn't be), the
        # route's exclusion set keeps them out of the response until I.3b.
        await _seed_outcome(
            db_session,
            signal_type="regime_shift",
            hit_target=True,
            key="shouldnt-appear",
        )
        body = (await client.get("/api/track-record/per-detector")).json()
        assert all(d["signal_type"] != "regime_shift" for d in body["detectors"])


class TestPerDetectorCache:
    async def test_concurrent_cache_miss_serialises_aggregation(
        self, db_session, client, monkeypatch
    ):
        """N concurrent first-paint requests fire AT MOST ONE aggregation,
        not N. Same stampede guard as `routes/calibration.py` and `routes/prices.py`."""
        await _seed_outcome(db_session, signal_type="acceleration", hit_target=True, key="stampede")

        call_count = {"n": 0}
        real_compute = per_detector_route.compute_per_detector

        async def counting_compute(*args, **kwargs):
            call_count["n"] += 1
            await asyncio.sleep(0.01)
            return await real_compute(*args, **kwargs)

        monkeypatch.setattr(per_detector_route, "compute_per_detector", counting_compute)

        responses = await asyncio.gather(
            *[client.get("/api/track-record/per-detector") for _ in range(5)]
        )
        for r in responses:
            assert r.status_code == 200
        assert call_count["n"] == 1

    async def test_cache_keyed_by_params(self, db_session, client, monkeypatch):
        """Different (version, lookback) tuples MUST trigger separate computes."""
        await _seed_outcome(db_session, signal_type="acceleration", hit_target=True, key="key1")

        call_count = {"n": 0}
        real_compute = per_detector_route.compute_per_detector

        async def counting_compute(*args, **kwargs):
            call_count["n"] += 1
            return await real_compute(*args, **kwargs)

        monkeypatch.setattr(per_detector_route, "compute_per_detector", counting_compute)

        # Same params twice → 1 compute.
        await client.get("/api/track-record/per-detector?lookback_days=30")
        await client.get("/api/track-record/per-detector?lookback_days=30")
        assert call_count["n"] == 1

        # Different lookback → 2nd compute fires.
        await client.get("/api/track-record/per-detector?lookback_days=60")
        assert call_count["n"] == 2
