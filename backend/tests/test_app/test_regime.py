"""GET /api/regime endpoint tests.

Covers:
  - 200 with the latest snapshot's fields when a row exists
  - 503 when `regime_snapshots` is empty (cold-boot)
  - 503 when the latest row is a legacy pre-Stage-7 snapshot with NULL
    regime/posture/confidence (defensive — schema requires non-null)
  - `macro_events_nearby` is unwrapped from the JSONB wrapper key
  - Response contract — exactly the documented field set
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from httpx import ASGITransport

from etfpulse.api.deps import get_db_session
from etfpulse.app import create_app
from etfpulse.models import (
    REGIME_MACRO_EVENTS_KEY,
    MarketRegime,
    RegimeSnapshot,
    SignalPosture,
)


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


class TestGetRegime:
    async def test_empty_table_returns_503(self, db_session, client):
        r = await client.get("/api/regime")
        assert r.status_code == 503
        assert r.json() == {"detail": "regime not yet classified"}

    async def test_returns_latest_snapshot(self, db_session, client):
        captured = datetime.now(UTC)
        db_session.add(
            RegimeSnapshot(
                captured_at=captured,
                regime=MarketRegime.MARKUP.value,
                signal_posture=SignalPosture.NORMAL.value,
                confidence=8,
                reasoning={"score": 50, "flow": {"score": 40}},
            )
        )
        await db_session.flush()

        r = await client.get("/api/regime")
        assert r.status_code == 200
        body = r.json()
        assert body["regime"] == "markup"
        assert body["signal_posture"] == "normal"
        assert body["confidence"] == 8
        assert body["reasoning"] == {"score": 50, "flow": {"score": 40}}
        assert body["macro_events_nearby"] == []
        # ISO round-trip — captured_at should be UTC and parse back equal.
        assert datetime.fromisoformat(body["classified_at"]) == captured

    async def test_picks_newest_when_multiple(self, db_session, client):
        """Mirrors /api/dashboard/stats and the regime_shift detector — both
        use `get_latest_regime`, all three must agree on what 'latest' is."""
        now = datetime.now(UTC)
        db_session.add(
            RegimeSnapshot(
                captured_at=now - timedelta(days=1),
                regime=MarketRegime.ACCUMULATION.value,
                signal_posture=SignalPosture.AGGRESSIVE.value,
                confidence=4,
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

        r = await client.get("/api/regime")
        body = r.json()
        assert body["regime"] == "distribution"
        assert body["signal_posture"] == "cautious"
        assert body["confidence"] == 6

    async def test_legacy_null_snapshot_returns_503(self, db_session, client):
        """Defensive: a pre-Stage-7 snapshot exists with NULL regime/posture/
        confidence. Endpoint must 503 rather than serialize partial data."""
        db_session.add(RegimeSnapshot(captured_at=datetime.now(UTC)))
        await db_session.flush()

        r = await client.get("/api/regime")
        assert r.status_code == 503

    async def test_macro_events_unwrapped_from_jsonb_wrapper(self, db_session, client):
        """`regime_snapshots.macro_events` is stored as
        `{REGIME_MACRO_EVENTS_KEY: [...]}` (the column type is `dict | None`,
        not `list | None`). The endpoint must unwrap and return a flat list."""
        db_session.add(
            RegimeSnapshot(
                captured_at=datetime.now(UTC),
                regime=MarketRegime.MARKUP.value,
                signal_posture=SignalPosture.CAUTIOUS.value,
                confidence=7,
                macro_events={REGIME_MACRO_EVENTS_KEY: ["FOMC", "CPI"]},
            )
        )
        await db_session.flush()

        r = await client.get("/api/regime")
        body = r.json()
        assert body["macro_events_nearby"] == ["FOMC", "CPI"]

    async def test_macro_events_absent_yields_empty_list(self, db_session, client):
        """`macro_events` column is None on the snapshot → response shows []."""
        db_session.add(
            RegimeSnapshot(
                captured_at=datetime.now(UTC),
                regime=MarketRegime.UNCERTAIN.value,
                signal_posture=SignalPosture.CAUTIOUS.value,
                confidence=2,
                macro_events=None,
            )
        )
        await db_session.flush()

        r = await client.get("/api/regime")
        assert r.json()["macro_events_nearby"] == []

    async def test_response_contract_locks_fields(self, db_session, client):
        """Frontend (#104) depends on exactly this key set. Locking prevents
        accidental schema drift."""
        db_session.add(
            RegimeSnapshot(
                captured_at=datetime.now(UTC),
                regime=MarketRegime.MARKUP.value,
                signal_posture=SignalPosture.NORMAL.value,
                confidence=5,
            )
        )
        await db_session.flush()

        r = await client.get("/api/regime")
        body = r.json()
        assert set(body.keys()) == {
            "regime",
            "signal_posture",
            "confidence",
            "reasoning",
            "macro_events_nearby",
            "classified_at",
        }
