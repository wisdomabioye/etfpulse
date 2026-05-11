"""HTTP-layer tests for `GET /api/analytics/breakdown`.

The heavy lifting (per-dimension SQL, bucket math) is pinned upstream in
`tests/test_pipeline/test_analytics.py`. These tests only need to verify:

    1. The route mounts and returns 200 on cold-boot (empty state UX).
    2. The Pydantic projection preserves shape + values when seeded.
    3. `extra="forbid"` is honored (no spurious fields creep into the
       response).

Anything more would duplicate the pipeline tests one layer up.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, date, datetime
from decimal import Decimal

import httpx
import pytest
from httpx import ASGITransport

from etfpulse.api.deps import get_db_session
from etfpulse.app import create_app
from etfpulse.models import Signal, SignalOutcome
from etfpulse.pipeline.analytics import _breakdown_cache
from etfpulse.pipeline.detectors import compute_fingerprint

_NOW = datetime(2026, 5, 1, 12, 0, tzinfo=UTC)


@pytest.fixture(autouse=True)
def _reset_cache():
    """Module-level cache must be cleared per-test or seeded rows are
    invisible behind a stale snapshot."""
    _breakdown_cache.clear()
    yield
    _breakdown_cache.clear()


@pytest.fixture
async def client(db_session):
    app = create_app()

    async def _override() -> AsyncIterator:
        yield db_session

    app.dependency_overrides[get_db_session] = _override
    transport = ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c
    app.dependency_overrides.clear()


async def _seed_outcome(
    db_session,
    *,
    asset: str = "BTC",
    signal_type: str = "flow_anomaly",
    direction: str = "long",
    confidence: int = 7,
    hit_target: bool | None = True,
    max_favorable: Decimal | None = Decimal("0.025"),
    max_adverse: Decimal | None = Decimal("0.008"),
    key: str = "x",
) -> None:
    signal = Signal(
        signal_type=signal_type,
        asset=asset,
        trigger_data={},
        ai_analysis={"suggested_action": "consider long", "headline": "x"},
        confidence=confidence,
        status="alerted",
        price_at_creation=Decimal("84200"),
        price_source="binance",
        ai_prompt_version="v3",
        fingerprint=compute_fingerprint("analytics-route-test", key),
        signal_date=date(2026, 4, 25),
    )
    db_session.add(signal)
    await db_session.flush()
    db_session.add(
        SignalOutcome(
            signal_id=signal.id,
            asset=asset,
            signal_type=signal_type,
            direction=direction,
            confidence=confidence,
            target_price=Decimal("89500"),
            price_at_signal=Decimal("84200"),
            hit_target=hit_target,
            max_favorable=max_favorable,
            max_adverse=max_adverse,
            evaluated_at=_NOW,
        )
    )
    await db_session.flush()


class TestEmptyState:
    async def test_cold_boot_returns_200_with_zero_counts(self, client):
        """Visiting /analytics before any signal evaluated must not 503 —
        empty state is a legitimate first-run experience."""
        r = await client.get("/api/analytics/breakdown")
        assert r.status_code == 200
        body = r.json()
        assert body["total_outcomes"] == 0
        assert body["by_detector"] == []
        assert body["by_asset"] == []
        assert body["by_direction"] == []
        # Confidence buckets are backfilled even when empty.
        assert len(body["by_confidence_bucket"]) == 4
        # Histograms always render their 6 buckets so the chart axis is stable.
        assert len(body["mfe_histogram"]) == 6
        assert len(body["mae_histogram"]) == 6


class TestSeededResponse:
    async def test_full_breakdown_shape(self, db_session, client):
        await _seed_outcome(db_session, key="a", hit_target=True)
        await _seed_outcome(db_session, key="b", hit_target=False)
        await _seed_outcome(
            db_session,
            key="c",
            asset="ETH",
            direction="short",
            confidence=9,
            hit_target=True,
            max_favorable=Decimal("0.080"),
        )

        r = await client.get("/api/analytics/breakdown")
        assert r.status_code == 200
        body = r.json()

        assert body["total_outcomes"] == 3

        # by_detector — all 3 share signal_type, so one row, hit_rate 2/3.
        det = {row["label"]: row for row in body["by_detector"]}
        assert det["flow_anomaly"]["total"] == 3
        assert det["flow_anomaly"]["hits"] == 2
        assert det["flow_anomaly"]["hit_rate_pct"] == 66.67

        # by_asset — BTC (2 rows, 1 hit) vs ETH (1 row, 1 hit).
        asset = {row["label"]: row for row in body["by_asset"]}
        assert asset["BTC"]["hit_rate_pct"] == 50.0
        assert asset["ETH"]["hit_rate_pct"] == 100.0

        # by_direction — long (2 rows, 1 hit) vs short (1 row, 1 hit).
        direction = {row["label"]: row for row in body["by_direction"]}
        assert direction["long"]["hit_rate_pct"] == 50.0
        assert direction["short"]["hit_rate_pct"] == 100.0

        # MFE histogram — one row at 0.080 lands in 5–10% bucket.
        mfe = {b["label"]: b["count"] for b in body["mfe_histogram"]}
        assert mfe["5–10%"] == 1

    async def test_response_has_no_extra_fields(self, db_session, client):
        """`extra="forbid"` guards against silent schema drift —
        confirm the response has only the documented fields."""
        await _seed_outcome(db_session, key="a")
        r = await client.get("/api/analytics/breakdown")
        body = r.json()
        assert set(body.keys()) == {
            "total_outcomes",
            "by_detector",
            "by_asset",
            "by_confidence_bucket",
            "by_direction",
            "mfe_histogram",
            "mae_histogram",
        }
        # Sample one row's keys too — DTO shape must hold all the way down.
        row = body["by_detector"][0]
        assert set(row.keys()) == {"label", "total", "targeted", "hits", "hit_rate_pct"}
        bucket = body["mfe_histogram"][0]
        assert set(bucket.keys()) == {"label", "lower", "upper", "count"}
