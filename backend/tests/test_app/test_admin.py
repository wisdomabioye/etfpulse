"""Admin trigger route — auth gate + successful cycle invocation.

We mock `_run_cycle_with_session` to a predictable return value rather than
running a full cycle through SoSoValue/OpenRouter on every test.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, date, datetime, timedelta

import httpx
import pytest
from fastapi.testclient import TestClient
from httpx import ASGITransport

from etfpulse.api.deps import get_db_session
from etfpulse.app import create_app
from etfpulse.config import settings
from etfpulse.models import (
    ChannelType,
    DeliveryStatus,
    NotificationChannel,
    Signal,
    SignalDelivery,
    SignalStatus,
    User,
)
from etfpulse.pipeline.detectors.base import compute_fingerprint
from etfpulse.pipeline.reapers import DELIVERY_REAPER_ERROR

_SAMPLE_SUMMARY = {
    "ingested": {"BTC": 2, "ETH": 1},
    "ingest_errors": [],
    "detectors_run": 1,
    "detector_errors": [],
    "signals_new": 1,
    "signals_duplicate": 0,
    "ai_succeeded": 1,
    "ai_failed": 0,
}


@pytest.fixture
def stub_cycle(monkeypatch):
    """Replace the cycle wrapper with a no-op that returns a fixed summary."""
    calls: list[None] = []

    async def _stub() -> dict:
        calls.append(None)
        return _SAMPLE_SUMMARY

    monkeypatch.setattr("etfpulse.api.routes.admin._run_cycle_with_session", _stub)
    return calls


@pytest.fixture
def stub_cycle_failing(monkeypatch):
    """Replace the cycle wrapper to simulate a rollback (returns None)."""

    async def _stub() -> None:
        return None

    monkeypatch.setattr("etfpulse.api.routes.admin._run_cycle_with_session", _stub)


# ---- Auth gate ------------------------------------------------------------


def test_without_key_returns_503_when_admin_disabled(monkeypatch, stub_cycle):
    """ADMIN_API_KEY unset → 503 (admin surface disabled). require_admin_key
    returns 503 BEFORE checking the header value, so even an absent header
    should see this."""
    monkeypatch.setattr(settings, "admin_api_key", "")

    with TestClient(create_app()) as client:
        r = client.post("/api/admin/signals/trigger")

    assert r.status_code == 503
    assert stub_cycle == [], "cycle must not run when admin is disabled"


def test_wrong_key_returns_401(monkeypatch, stub_cycle):
    """Correct env but mismatching header → 401."""
    monkeypatch.setattr(settings, "admin_api_key", "secret-key")

    with TestClient(create_app()) as client:
        r = client.post(
            "/api/admin/signals/trigger",
            headers={"X-Admin-Key": "wrong-key"},
        )

    assert r.status_code == 401
    assert stub_cycle == [], "cycle must not run when key is wrong"


def test_missing_header_with_key_set_returns_401(monkeypatch, stub_cycle):
    """ADMIN_API_KEY set but header absent → 401."""
    monkeypatch.setattr(settings, "admin_api_key", "secret-key")

    with TestClient(create_app()) as client:
        r = client.post("/api/admin/signals/trigger")

    assert r.status_code == 401
    assert stub_cycle == []


# ---- Happy path -----------------------------------------------------------


def test_correct_key_returns_200_and_summary(monkeypatch, stub_cycle):
    monkeypatch.setattr(settings, "admin_api_key", "secret-key")

    with TestClient(create_app()) as client:
        r = client.post(
            "/api/admin/signals/trigger",
            headers={"X-Admin-Key": "secret-key"},
        )

    assert r.status_code == 200
    body = r.json()
    # Contract with #50 post-deploy checks — these keys must be present.
    expected_keys = {
        "ingested",
        "ingest_errors",
        "detectors_run",
        "detector_errors",
        "signals_new",
        "signals_duplicate",
        "ai_succeeded",
        "ai_failed",
    }
    assert set(body.keys()) == expected_keys
    assert body["signals_new"] == 1
    assert len(stub_cycle) == 1


# ---- Cycle failure path ---------------------------------------------------


def test_cycle_rollback_returns_503(monkeypatch, stub_cycle_failing):
    """`_run_cycle_with_session` returns None on rollback → admin gets 503,
    not a confusing 200 with an empty body."""
    monkeypatch.setattr(settings, "admin_api_key", "secret-key")

    with TestClient(create_app()) as client:
        r = client.post(
            "/api/admin/signals/trigger",
            headers={"X-Admin-Key": "secret-key"},
        )

    assert r.status_code == 503
    assert r.json()["detail"] == "cycle failed — see server logs"


# ---------------------------------------------------------------------------
# GET /api/admin/metrics — operator dashboard (task #15)
# ---------------------------------------------------------------------------


@pytest.fixture
async def metrics_client(db_session, monkeypatch):
    """Async client with db_session override + a fixed admin key."""
    monkeypatch.setattr(settings, "admin_api_key", "secret-key")
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
    fp_seed: str,
    status: str = SignalStatus.PENDING.value,
    confidence: int | None = 7,
    expires_at: datetime | None = None,
) -> Signal:
    signal = Signal(
        signal_type="flow_anomaly",
        asset="BTC",
        trigger_data={},
        confidence=confidence,
        status=status,
        expires_at=expires_at,
        fingerprint=compute_fingerprint("BTC", "metrics-test", fp_seed),
        signal_date=date(2026, 4, 23),
    )
    db_session.add(signal)
    await db_session.flush()
    return signal


async def _seed_delivery(
    db_session,
    signal_id: int,
    *,
    status: str = DeliveryStatus.PENDING.value,
    created_at: datetime | None = None,
    error_message: str | None = None,
) -> SignalDelivery:
    user = User(pref_assets=["BTC"], pref_min_confidence=5)
    db_session.add(user)
    await db_session.flush()
    channel = NotificationChannel(
        user_id=user.id,
        channel_type=ChannelType.TELEGRAM.value,
        channel_identifier=f"chat-{user.id}",
    )
    db_session.add(channel)
    await db_session.flush()
    delivery = SignalDelivery(
        signal_id=signal_id,
        user_id=user.id,
        channel_id=channel.id,
        status=status,
        error_message=error_message,
    )
    db_session.add(delivery)
    await db_session.flush()
    if created_at is not None:
        delivery.created_at = created_at
        await db_session.flush()
    return delivery


class TestAdminMetricsAuth:
    async def test_disabled_when_admin_key_empty(self, db_session, monkeypatch):
        monkeypatch.setattr(settings, "admin_api_key", "")
        app = create_app()

        async def _override() -> AsyncIterator:
            yield db_session

        app.dependency_overrides[get_db_session] = _override
        transport = ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
            r = await c.get("/api/admin/metrics")
        app.dependency_overrides.clear()
        assert r.status_code == 503

    async def test_wrong_key_returns_401(self, metrics_client):
        r = await metrics_client.get("/api/admin/metrics", headers={"X-Admin-Key": "wrong"})
        assert r.status_code == 401


class TestAdminMetricsShape:
    async def test_empty_db_returns_all_zeros(self, metrics_client):
        r = await metrics_client.get("/api/admin/metrics", headers={"X-Admin-Key": "secret-key"})
        assert r.status_code == 200
        body = r.json()
        assert body["signal_status_counts"] == {"pending": 0, "alerted": 0, "expired": 0}
        assert body["delivery_status_counts"] == {
            "pending": 0,
            "delivered": 0,
            "failed": 0,
            "skipped": 0,
        }
        assert body["signals_overdue_unreaped"] == 0
        assert body["signals_null_confidence"] == 0
        assert body["deliveries_stuck_pending"] == 0
        assert body["deliveries_reaper_failures"] == 0

    async def test_signal_status_grouped_correctly(self, db_session, metrics_client):
        await _seed_signal(db_session, fp_seed="p1", status=SignalStatus.PENDING.value)
        await _seed_signal(db_session, fp_seed="p2", status=SignalStatus.PENDING.value)
        await _seed_signal(db_session, fp_seed="a1", status=SignalStatus.ALERTED.value)
        await _seed_signal(db_session, fp_seed="e1", status=SignalStatus.EXPIRED.value)

        r = await metrics_client.get("/api/admin/metrics", headers={"X-Admin-Key": "secret-key"})
        assert r.status_code == 200
        assert r.json()["signal_status_counts"] == {
            "pending": 2,
            "alerted": 1,
            "expired": 1,
        }

    async def test_overdue_unreaped_counts_only_past_unexpired(self, db_session, metrics_client):
        # Past expires_at + still pending → counted
        await _seed_signal(
            db_session,
            fp_seed="overdue1",
            status=SignalStatus.PENDING.value,
            expires_at=datetime.now(UTC) - timedelta(hours=1),
        )
        # Past expires_at but already EXPIRED → not counted (reaper already ran)
        await _seed_signal(
            db_session,
            fp_seed="already-expired",
            status=SignalStatus.EXPIRED.value,
            expires_at=datetime.now(UTC) - timedelta(hours=1),
        )
        # Future expires_at → not counted
        await _seed_signal(
            db_session,
            fp_seed="future",
            status=SignalStatus.PENDING.value,
            expires_at=datetime.now(UTC) + timedelta(hours=1),
        )
        # NULL expires_at → not counted (AI-failed, indeterminate horizon)
        await _seed_signal(
            db_session,
            fp_seed="null-exp",
            status=SignalStatus.PENDING.value,
            confidence=None,
            expires_at=None,
        )

        r = await metrics_client.get("/api/admin/metrics", headers={"X-Admin-Key": "secret-key"})
        assert r.json()["signals_overdue_unreaped"] == 1

    async def test_null_confidence_count(self, db_session, metrics_client):
        await _seed_signal(db_session, fp_seed="nc1", confidence=None)
        await _seed_signal(db_session, fp_seed="nc2", confidence=None)
        await _seed_signal(db_session, fp_seed="ok", confidence=7)

        r = await metrics_client.get("/api/admin/metrics", headers={"X-Admin-Key": "secret-key"})
        assert r.json()["signals_null_confidence"] == 2

    async def test_stuck_pending_uses_threshold(self, db_session, metrics_client):
        sig = await _seed_signal(db_session, fp_seed="d-stuck")
        cutoff = settings.delivery_pending_max_age_seconds
        # Older than threshold AND pending → counted
        await _seed_delivery(
            db_session,
            sig.id,
            status=DeliveryStatus.PENDING.value,
            created_at=datetime.now(UTC) - timedelta(seconds=cutoff + 60),
        )
        # Pending but fresh → not counted
        await _seed_delivery(
            db_session,
            sig.id,
            status=DeliveryStatus.PENDING.value,
            created_at=datetime.now(UTC),
        )
        # Old but already FAILED → not counted (terminal state)
        await _seed_delivery(
            db_session,
            sig.id,
            status=DeliveryStatus.FAILED.value,
            created_at=datetime.now(UTC) - timedelta(seconds=cutoff + 60),
        )

        r = await metrics_client.get("/api/admin/metrics", headers={"X-Admin-Key": "secret-key"})
        assert r.json()["deliveries_stuck_pending"] == 1

    async def test_reaper_failures_match_sentinel(self, db_session, metrics_client):
        sig = await _seed_signal(db_session, fp_seed="d-reaper")
        # Reaper sentinel → counted
        await _seed_delivery(
            db_session,
            sig.id,
            status=DeliveryStatus.FAILED.value,
            error_message=DELIVERY_REAPER_ERROR,
        )
        await _seed_delivery(
            db_session,
            sig.id,
            status=DeliveryStatus.FAILED.value,
            error_message=DELIVERY_REAPER_ERROR,
        )
        # Different failure cause → not counted
        await _seed_delivery(
            db_session,
            sig.id,
            status=DeliveryStatus.FAILED.value,
            error_message="bot blocked by user",
        )

        r = await metrics_client.get("/api/admin/metrics", headers={"X-Admin-Key": "secret-key"})
        assert r.json()["deliveries_reaper_failures"] == 2

    async def test_scheduler_jobs_null_when_scheduler_off(self, db_session, metrics_client):
        """Default test mode disables the scheduler (autouse fixture) →
        `app.state.scheduler` never gets set → field is null."""
        r = await metrics_client.get("/api/admin/metrics", headers={"X-Admin-Key": "secret-key"})
        assert r.json()["scheduler_jobs"] is None

    async def test_scheduler_jobs_lists_registered_jobs(self, db_session, monkeypatch):
        """With a scheduler attached, every registered job surfaces with
        id / next_run_at / trigger / pending. Uses a stub object so we
        don't have to boot APScheduler in tests."""
        monkeypatch.setattr(settings, "admin_api_key", "secret-key")

        class _StubJob:
            def __init__(self, jid: str, next_run, trigger: str, pending: bool):
                self.id = jid
                self.next_run_time = next_run
                self.trigger = trigger
                self.pending = pending

        class _StubScheduler:
            def __init__(self, jobs):
                self._jobs = jobs

            def get_jobs(self):
                return self._jobs

        fixed_next = datetime(2026, 5, 11, 4, 30, tzinfo=UTC)
        stub = _StubScheduler(
            [
                _StubJob("daily_cycle", fixed_next, "cron[hour='4', minute='30']", False),
                _StubJob("signal_expiry_reaper", None, "interval[0:15:00]", True),
            ]
        )

        app = create_app()
        app.state.scheduler = stub

        async def _override() -> AsyncIterator:
            yield db_session

        app.dependency_overrides[get_db_session] = _override
        transport = ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
            r = await c.get("/api/admin/metrics", headers={"X-Admin-Key": "secret-key"})
        app.dependency_overrides.clear()

        assert r.status_code == 200
        jobs = r.json()["scheduler_jobs"]
        assert isinstance(jobs, list)
        assert len(jobs) == 2
        by_id = {j["id"]: j for j in jobs}
        assert by_id["daily_cycle"]["next_run_at"] == "2026-05-11T04:30:00Z"
        assert by_id["daily_cycle"]["pending"] is False
        assert by_id["daily_cycle"]["trigger"] == "cron[hour='4', minute='30']"
        assert by_id["signal_expiry_reaper"]["next_run_at"] is None
        assert by_id["signal_expiry_reaper"]["pending"] is True

    async def test_delivery_status_grouped(self, db_session, metrics_client):
        sig = await _seed_signal(db_session, fp_seed="grouped")
        await _seed_delivery(db_session, sig.id, status=DeliveryStatus.PENDING.value)
        await _seed_delivery(db_session, sig.id, status=DeliveryStatus.DELIVERED.value)
        await _seed_delivery(db_session, sig.id, status=DeliveryStatus.DELIVERED.value)
        await _seed_delivery(db_session, sig.id, status=DeliveryStatus.FAILED.value)
        await _seed_delivery(db_session, sig.id, status=DeliveryStatus.SKIPPED.value)

        r = await metrics_client.get("/api/admin/metrics", headers={"X-Admin-Key": "secret-key"})
        assert r.json()["delivery_status_counts"] == {
            "pending": 1,
            "delivered": 2,
            "failed": 1,
            "skipped": 1,
        }
