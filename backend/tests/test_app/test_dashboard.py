"""GET /api/dashboard/stats aggregation tests.

Pattern: same dependency-override + AsyncClient/ASGITransport as test_signals.
Seeds signals via db_session, asserts on the aggregated response.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import httpx
import pytest
from httpx import ASGITransport

from etfpulse.api.deps import get_db_session
from etfpulse.app import create_app
from etfpulse.models import (
    MarketRegime,
    RegimeSnapshot,
    Signal,
    SignalOutcome,
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
            # PR B (#60) — `hit_rate_global` is the v2 name; `hit_rate_72h`
            # stays for one deprecation cycle and carries the same value.
            # Both null + 0 evaluated until signals age past their window.
            "hit_rate_global": None,
            "hit_rate_72h": None,
            "evaluated_count": 0,
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
            # PR B (#60) — `hit_rate_global` is v2; `hit_rate_72h` is the
            # deprecated parallel kept for one release cycle.
            "hit_rate_global",
            "hit_rate_72h",
            "evaluated_count",
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


# ---------------------------------------------------------------------------
# Stage 8-P5 — hit_rate_72h + evaluated_count (closes #44)
# ---------------------------------------------------------------------------


async def _seed_outcome(
    db_session,
    *,
    confidence: int = 7,
    hit_target: bool | None = True,
    key: str = "x",
) -> SignalOutcome:
    """Seed a Signal + matching SignalOutcome — what `/api/dashboard/stats`
    aggregates over for hit_rate_72h. Mirrors the helper in test_track_record."""
    signal = Signal(
        signal_type="flow_anomaly",
        asset="BTC",
        trigger_data={},
        ai_analysis={"suggested_action": "consider long", "headline": "x"},
        confidence=confidence,
        status="alerted",
        price_at_creation=Decimal("84200"),
        price_source="binance",
        ai_prompt_version="v3",
        fingerprint=compute_fingerprint("dashboard-hit-rate", key),
        signal_date=date(2026, 4, 25),
    )
    db_session.add(signal)
    await db_session.flush()

    outcome = SignalOutcome(
        signal_id=signal.id,
        asset="BTC",
        signal_type="flow_anomaly",
        direction="long",
        confidence=confidence,
        entry_price=Decimal("84200"),
        target_price=Decimal("89500"),
        price_at_signal=Decimal("84200"),
        hit_target=hit_target,
        evaluated_at=datetime.now(UTC),
    )
    db_session.add(outcome)
    await db_session.flush()
    return outcome


class TestHitRate72h:
    async def test_hit_rate_computed_as_percent(self, db_session, client):
        """3 hits + 1 miss out of 4 with-target outcomes = 75%. PERCENT
        unit (0..100), not fraction (0..1) — same as track-record API so
        the FE never converts."""
        for i in range(3):
            await _seed_outcome(db_session, hit_target=True, key=f"hit{i}")
        await _seed_outcome(db_session, hit_target=False, key="miss")

        body = (await client.get("/api/dashboard/stats")).json()
        assert body["hit_rate_72h"] == 75.0
        assert body["evaluated_count"] == 4

    async def test_hit_rate_excludes_no_target_signals_from_denominator(self, db_session, client):
        """Same rationale as `/api/track-record` — signals where AI declined
        a target (hit_target IS NULL) shouldn't dilute the rate."""
        await _seed_outcome(db_session, hit_target=True, key="hit")
        await _seed_outcome(db_session, hit_target=None, key="no-target-1")
        await _seed_outcome(db_session, hit_target=None, key="no-target-2")

        body = (await client.get("/api/dashboard/stats")).json()
        # 1 hit / 1 with-target = 100%, NOT 1/3 = 33%
        assert body["hit_rate_72h"] == 100.0
        # All three outcome rows count toward `evaluated_count` though.
        assert body["evaluated_count"] == 3

    async def test_hit_rate_none_when_no_targets_set(self, db_session, client):
        """All outcomes have NULL hit_target → no signal had a target →
        hit_rate is undefined → null. Better than rendering '0%' on the
        home tile for an empty cohort."""
        await _seed_outcome(db_session, hit_target=None, key="n1")
        await _seed_outcome(db_session, hit_target=None, key="n2")

        body = (await client.get("/api/dashboard/stats")).json()
        assert body["hit_rate_72h"] is None
        assert body["evaluated_count"] == 2

    async def test_hit_rate_returned_as_float(self, db_session, client):
        """Pin the wire type — frontend expects `number | null`. A string
        would silently break the panel's `Math.round` call."""
        await _seed_outcome(db_session, hit_target=True, key="t")

        body = (await client.get("/api/dashboard/stats")).json()
        assert isinstance(body["hit_rate_72h"], float)

    async def test_outcome_with_null_evaluated_at_excluded(self, db_session, client):
        """Defensive — `evaluated_at IS NOT NULL` in the WHERE matches
        the track-record endpoint's filter, so a future writer leaking a
        NULL evaluated_at row doesn't pollute the home tile."""
        # Manually insert a Signal + outcome with evaluated_at=None.
        signal = Signal(
            signal_type="flow_anomaly",
            asset="BTC",
            trigger_data={},
            ai_analysis={"suggested_action": "consider long", "headline": "x"},
            confidence=7,
            status="alerted",
            price_at_creation=Decimal("84200"),
            price_source="binance",
            ai_prompt_version="v3",
            fingerprint=compute_fingerprint("dashboard-hit-rate", "no-eval"),
            signal_date=date(2026, 4, 25),
        )
        db_session.add(signal)
        await db_session.flush()
        db_session.add(
            SignalOutcome(
                signal_id=signal.id,
                asset="BTC",
                signal_type="flow_anomaly",
                direction="long",
                confidence=7,
                target_price=Decimal("89500"),
                price_at_signal=Decimal("84200"),
                hit_target=True,
                evaluated_at=None,  # explicitly unevaluated
            )
        )
        await db_session.flush()

        body = (await client.get("/api/dashboard/stats")).json()
        assert body["evaluated_count"] == 0
        assert body["hit_rate_72h"] is None
