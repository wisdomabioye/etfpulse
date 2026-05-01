"""GET /api/dashboard/stats aggregation tests.

Pattern: same dependency-override + AsyncClient/ASGITransport as test_signals.
Seeds signals via db_session, asserts on the aggregated response.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, date, datetime, timedelta

import httpx
import pytest
from httpx import ASGITransport

from etfpulse.api.deps import get_db_session
from etfpulse.app import create_app
from etfpulse.models import (
    MarketRegime,
    RegimeSnapshot,
    Signal,
    SignalPosture,
)
from etfpulse.pipeline.detectors import compute_fingerprint


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


async def _seed_signal(
    db_session,
    *,
    confidence: int | None = 7,
    created_at: datetime | None = None,
    key: str = "x",
) -> Signal:
    created_at = created_at or datetime.now(UTC)
    signal = Signal(
        signal_type="flow_anomaly",
        asset="BTC",
        trigger_data={},
        confidence=confidence,
        status="alerted",
        fingerprint=compute_fingerprint("stats-test", key, str(created_at.timestamp())),
        signal_date=date(2026, 4, 22),
    )
    db_session.add(signal)
    await db_session.flush()
    # Override server_default created_at for deterministic tests.
    signal.created_at = created_at
    await db_session.flush()
    return signal


class TestDashboardStats:
    async def test_empty_db_returns_zero_state(self, db_session, client):
        """Clean install: no signals → all fields at their empty-state defaults."""
        r = await client.get("/api/dashboard/stats")
        assert r.status_code == 200
        body = r.json()
        assert body == {
            "total_signals": 0,
            "signals_today": 0,
            "avg_confidence": None,
            "last_signal_at": None,
            # Stage 7-P7: regime fields null until the first cycle runs.
            "current_regime": None,
            "signal_posture": None,
        }

    async def test_total_signals_counts_all(self, db_session, client):
        for i in range(3):
            await _seed_signal(db_session, key=f"t{i}")

        r = await client.get("/api/dashboard/stats")
        assert r.json()["total_signals"] == 3

    async def test_signals_today_respects_utc_midnight(self, db_session, client):
        """Seed a signal from yesterday UTC and a signal from 00:00 UTC today.
        Only today's should count toward signals_today."""
        now = datetime.now(UTC)
        today_midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)

        # Yesterday (23:59 yesterday UTC) — NOT counted.
        await _seed_signal(
            db_session,
            created_at=today_midnight - timedelta(minutes=1),
            key="yesterday",
        )
        # Exactly 00:00:00 UTC today — counted (>= today_midnight).
        await _seed_signal(db_session, created_at=today_midnight, key="midnight")
        # Just after — counted.
        await _seed_signal(
            db_session,
            created_at=today_midnight + timedelta(hours=3),
            key="morning",
        )

        r = await client.get("/api/dashboard/stats")
        body = r.json()
        assert body["total_signals"] == 3
        assert body["signals_today"] == 2

    async def test_avg_confidence_ignores_null(self, db_session, client):
        """Signals with confidence=None (AI-failed) must NOT drag the average."""
        await _seed_signal(db_session, confidence=None, key="null1")
        await _seed_signal(db_session, confidence=None, key="null2")
        await _seed_signal(db_session, confidence=6, key="c6")
        await _seed_signal(db_session, confidence=8, key="c8")

        r = await client.get("/api/dashboard/stats")
        body = r.json()
        assert body["total_signals"] == 4
        # Average over the two non-null: (6 + 8) / 2 = 7.0
        assert body["avg_confidence"] == 7.0

    async def test_avg_confidence_none_when_all_null(self, db_session, client):
        """AVG over all-NULL column → NULL → serialized as None."""
        await _seed_signal(db_session, confidence=None, key="n1")
        await _seed_signal(db_session, confidence=None, key="n2")

        r = await client.get("/api/dashboard/stats")
        body = r.json()
        assert body["total_signals"] == 2
        assert body["avg_confidence"] is None

    async def test_avg_confidence_returned_as_float(self, db_session, client):
        """Postgres AVG returns Decimal; we cast to float for JSON. Verify
        this explicitly — a Decimal in the response would break JS clients."""
        await _seed_signal(db_session, confidence=7, key="f1")

        r = await client.get("/api/dashboard/stats")
        body = r.json()
        assert isinstance(body["avg_confidence"], float)
        assert body["avg_confidence"] == 7.0

    async def test_last_signal_at_matches_max_created_at(self, db_session, client):
        now = datetime.now(UTC).replace(microsecond=0)
        await _seed_signal(db_session, created_at=now - timedelta(hours=3), key="old")
        await _seed_signal(db_session, created_at=now - timedelta(hours=1), key="mid")
        await _seed_signal(db_session, created_at=now, key="newest")

        r = await client.get("/api/dashboard/stats")
        body = r.json()
        # ISO parse and compare — allow for minor serialization tolerance.
        assert body["last_signal_at"] is not None
        parsed = datetime.fromisoformat(body["last_signal_at"])
        assert parsed == now

    async def test_returns_all_documented_fields(self, db_session, client):
        """Locks the response contract — frontend depends on exactly these keys."""
        r = await client.get("/api/dashboard/stats")
        body = r.json()
        assert set(body.keys()) == {
            "total_signals",
            "signals_today",
            "avg_confidence",
            "last_signal_at",
            "current_regime",
            "signal_posture",
        }

    async def test_current_regime_populated_when_snapshot_exists(self, db_session, client):
        """When a `regime_snapshots` row exists, the home stats surface it
        without a second roundtrip to /api/regime."""
        db_session.add(
            RegimeSnapshot(
                captured_at=datetime.now(UTC),
                regime=MarketRegime.MARKUP.value,
                signal_posture=SignalPosture.NORMAL.value,
                confidence=8,
            )
        )
        await db_session.flush()

        r = await client.get("/api/dashboard/stats")
        body = r.json()
        assert body["current_regime"] == "markup"
        assert body["signal_posture"] == "normal"

    async def test_current_regime_uses_latest_snapshot(self, db_session, client):
        """Multiple snapshots → newest wins (matches /api/regime semantics
        + uses the same `get_latest_regime` helper)."""
        now = datetime.now(UTC)
        db_session.add(
            RegimeSnapshot(
                captured_at=now - timedelta(days=1),
                regime=MarketRegime.ACCUMULATION.value,
                signal_posture=SignalPosture.AGGRESSIVE.value,
                confidence=5,
            )
        )
        db_session.add(
            RegimeSnapshot(
                captured_at=now,
                regime=MarketRegime.DISTRIBUTION.value,
                signal_posture=SignalPosture.CAUTIOUS.value,
                confidence=6,
            )
        )
        await db_session.flush()

        r = await client.get("/api/dashboard/stats")
        body = r.json()
        assert body["current_regime"] == "distribution"
        assert body["signal_posture"] == "cautious"

    async def test_current_regime_null_for_legacy_snapshot(self, db_session, client):
        """Pre-Stage-7 snapshot (regime IS NULL) → fields surface as null,
        not the raw column value."""
        # Legacy row with no regime/posture columns populated.
        db_session.add(RegimeSnapshot(captured_at=datetime.now(UTC)))
        await db_session.flush()

        r = await client.get("/api/dashboard/stats")
        body = r.json()
        assert body["current_regime"] is None
        assert body["signal_posture"] is None
