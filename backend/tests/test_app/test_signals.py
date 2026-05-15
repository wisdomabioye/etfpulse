"""GET /api/signals (list + detail) tests.

Pattern: override `get_db_session` dependency to yield `db_session`, so the
route reads from the same transaction the test seeds wrote to. Test rollback
cleans everything up.
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
    ChannelType,
    NotificationChannel,
    Signal,
    SignalDelivery,
    SignalOutcome,
    SignalStatus,
    User,
)
from etfpulse.pipeline.detectors import compute_fingerprint


@pytest.fixture
async def client(db_session):
    """Async httpx client with get_db_session overridden to yield the test's
    db_session. Uses AsyncClient + ASGITransport (NOT TestClient) to avoid
    the cross-loop bug — TestClient spawns its own anyio portal with a
    separate event loop, which clashes with asyncpg connections attached
    to the test's loop. ASGITransport calls the app directly on the test's
    loop."""
    app = create_app()

    async def _override() -> AsyncIterator:
        yield db_session

    app.dependency_overrides[get_db_session] = _override

    # Lifespan runs once here; our scheduler/bot autouse fixtures keep
    # both startup tasks as no-ops during tests.
    transport = ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c
    app.dependency_overrides.clear()


_AI_ANALYSIS = {
    "headline": "test headline",
    "reasoning": ["r1"],
    "confidence": 7,
    "risks": ["k1"],
    "suggested_action": "consider short",
    "time_horizon": "swing",
}


async def _seed_signal(
    db_session,
    *,
    asset: str = "BTC",
    signal_type: str = "flow_anomaly",
    confidence: int | None = 7,
    status: str = SignalStatus.ALERTED.value,
    expires_at: datetime | None = None,
    created_at: datetime | None = None,
    headline: str | None = "test headline",
    ai_analysis: dict | None = None,
) -> Signal:
    if created_at is None:
        created_at = datetime.now(UTC)
    if ai_analysis is None and headline is not None:
        ai_analysis = {**_AI_ANALYSIS, "headline": headline, "confidence": confidence or 7}

    signal = Signal(
        signal_type=signal_type,
        asset=asset,
        trigger_data={},
        ai_analysis=ai_analysis,
        confidence=confidence,
        status=status,
        fingerprint=compute_fingerprint(asset, signal_type, str(created_at.timestamp())),
        signal_date=date(2026, 4, 22),
        expires_at=expires_at,
    )
    db_session.add(signal)
    await db_session.flush()
    # Pin created_at after flush so tests can order deterministically
    # (server_default=now() fires at INSERT; we override here).
    signal.created_at = created_at
    await db_session.flush()
    return signal


async def _seed_delivery(db_session, signal_id: int, chat_id: str = "100") -> None:
    user = User()
    db_session.add(user)
    await db_session.flush()
    channel = NotificationChannel(
        user_id=user.id,
        channel_type=ChannelType.TELEGRAM.value,
        channel_identifier=chat_id,
    )
    db_session.add(channel)
    await db_session.flush()
    db_session.add(SignalDelivery(signal_id=signal_id, user_id=user.id, channel_id=channel.id))
    await db_session.flush()


# ---- List — basics --------------------------------------------------------


class TestListBasics:
    async def test_empty_db_returns_empty_page(self, db_session, client):
        r = await client.get("/api/signals")
        assert r.status_code == 200
        body = r.json()
        assert body["items"] == []
        assert body["next_cursor"] is None
        # `total`/`total_pages` are populated in both modes; `page` is None
        # in cursor mode (this request omits `?page=`) — only set when the
        # client actually requested a page-numbered slice.
        assert body["total"] == 0
        assert body["total_pages"] == 0
        assert body["page"] is None

    async def test_returns_signals_desc_by_created_at(self, db_session, client):
        now = datetime.now(UTC)
        await _seed_signal(db_session, created_at=now - timedelta(hours=2), headline="oldest")
        await _seed_signal(db_session, created_at=now - timedelta(hours=1), headline="mid")
        await _seed_signal(db_session, created_at=now, headline="newest")

        r = await client.get("/api/signals")
        assert r.status_code == 200
        headlines = [item["headline"] for item in r.json()["items"]]
        assert headlines == ["newest", "mid", "oldest"]

    async def test_dto_shape_matches_schema(self, db_session, client):
        await _seed_signal(db_session, confidence=7)
        r = await client.get("/api/signals")
        item = r.json()["items"][0]
        # Key set matches SignalListItem contract.
        assert set(item.keys()) == {
            "id",
            "asset",
            "signal_type",
            "status",
            "confidence",
            "headline",
            "suggested_action",
            "time_horizon",
            "signal_date",
            "created_at",
            "expires_at",
            "alerted_to",
        }


# ---- List — filters -------------------------------------------------------


class TestListFilters:
    async def test_filter_by_asset(self, db_session, client):
        await _seed_signal(db_session, asset="BTC", headline="btc")
        await _seed_signal(db_session, asset="ETH", headline="eth")

        r = await client.get("/api/signals?asset=BTC")
        assert r.status_code == 200
        items = r.json()["items"]
        assert len(items) == 1
        assert items[0]["asset"] == "BTC"

    async def test_filter_by_asset_market(self, db_session, client):
        # PR F.3.FE — MARKET is the cross-asset sentinel for regime_shift
        # signals; the asset filter must accept it (was 422 pre-widen).
        await _seed_signal(db_session, asset="BTC", headline="btc")
        await _seed_signal(
            db_session, asset="MARKET", signal_type="regime_shift", headline="market"
        )

        r = await client.get("/api/signals?asset=MARKET")
        assert r.status_code == 200
        items = r.json()["items"]
        assert len(items) == 1
        assert items[0]["asset"] == "MARKET"

    async def test_filter_by_signal_type(self, db_session, client):
        await _seed_signal(db_session, signal_type="flow_anomaly", headline="fa")
        await _seed_signal(db_session, signal_type="magnitude", headline="m")

        r = await client.get("/api/signals?signal_type=magnitude")
        items = r.json()["items"]
        assert len(items) == 1
        assert items[0]["signal_type"] == "magnitude"

    async def test_filter_by_confidence_min(self, db_session, client):
        await _seed_signal(db_session, confidence=3, headline="low")
        await _seed_signal(db_session, confidence=7, headline="mid")
        await _seed_signal(db_session, confidence=9, headline="high")

        r = await client.get("/api/signals?confidence_min=7")
        headlines = sorted(item["headline"] for item in r.json()["items"])
        assert headlines == ["high", "mid"]

    async def test_excludes_expired_by_default(self, db_session, client):
        now = datetime.now(UTC)
        await _seed_signal(db_session, expires_at=now - timedelta(hours=1), headline="expired")
        await _seed_signal(db_session, expires_at=now + timedelta(hours=1), headline="future")
        await _seed_signal(db_session, expires_at=None, headline="no_expiry")

        r = await client.get("/api/signals")
        headlines = {item["headline"] for item in r.json()["items"]}
        assert headlines == {"future", "no_expiry"}  # "expired" excluded

    async def test_include_expired_true(self, db_session, client):
        now = datetime.now(UTC)
        await _seed_signal(db_session, expires_at=now - timedelta(hours=1), headline="expired")
        await _seed_signal(db_session, expires_at=now + timedelta(hours=1), headline="future")

        r = await client.get("/api/signals?include_expired=true")
        headlines = {item["headline"] for item in r.json()["items"]}
        assert headlines == {"expired", "future"}

    async def test_invalid_asset_returns_422(self, db_session, client):
        r = await client.get("/api/signals?asset=DOGE")
        assert r.status_code == 422

    async def test_invalid_signal_type_returns_422(self, db_session, client):
        r = await client.get("/api/signals?signal_type=nonsense")
        assert r.status_code == 422

    async def test_confidence_out_of_range_returns_422(self, db_session, client):
        r = await client.get("/api/signals?confidence_min=11")
        assert r.status_code == 422


# ---- List — cursor pagination ---------------------------------------------


class TestCursorPagination:
    async def test_paginates_across_pages(self, db_session, client):
        """Seed 5 signals, request limit=2, walk the cursor chain."""
        now = datetime.now(UTC)
        for i in range(5):
            await _seed_signal(
                db_session,
                created_at=now - timedelta(hours=i),
                headline=f"signal-{i}",
            )

        # Page 1: 2 newest
        r1 = await client.get("/api/signals?limit=2")
        body1 = r1.json()
        assert [i["headline"] for i in body1["items"]] == ["signal-0", "signal-1"]
        assert body1["next_cursor"] is not None

        # Page 2
        r2 = await client.get(f"/api/signals?limit=2&cursor={body1['next_cursor']}")
        body2 = r2.json()
        assert [i["headline"] for i in body2["items"]] == ["signal-2", "signal-3"]
        assert body2["next_cursor"] is not None

        # Page 3: last one
        r3 = await client.get(f"/api/signals?limit=2&cursor={body2['next_cursor']}")
        body3 = r3.json()
        assert [i["headline"] for i in body3["items"]] == ["signal-4"]
        assert body3["next_cursor"] is None  # no more pages


class TestPagePagination:
    """Page-mode pagination — `?page=N` returns offset-based slices plus
    `total / page / total_pages`. Cursor logic is bypassed when page is
    supplied."""

    async def test_first_page_returns_total_and_pages(self, db_session, client):
        now = datetime.now(UTC)
        for i in range(5):
            await _seed_signal(
                db_session,
                created_at=now - timedelta(hours=i),
                headline=f"signal-{i}",
            )

        r = await client.get("/api/signals?page=1&limit=2")
        body = r.json()
        assert r.status_code == 200
        assert [i["headline"] for i in body["items"]] == ["signal-0", "signal-1"]
        assert body["total"] == 5
        assert body["page"] == 1
        assert body["total_pages"] == 3  # ceil(5/2)

    async def test_middle_page_offsets_correctly(self, db_session, client):
        now = datetime.now(UTC)
        for i in range(5):
            await _seed_signal(
                db_session,
                created_at=now - timedelta(hours=i),
                headline=f"signal-{i}",
            )

        r = await client.get("/api/signals?page=2&limit=2")
        body = r.json()
        assert [i["headline"] for i in body["items"]] == ["signal-2", "signal-3"]
        assert body["page"] == 2
        assert body["total_pages"] == 3

    async def test_last_page_partial(self, db_session, client):
        now = datetime.now(UTC)
        for i in range(5):
            await _seed_signal(
                db_session,
                created_at=now - timedelta(hours=i),
                headline=f"signal-{i}",
            )

        r = await client.get("/api/signals?page=3&limit=2")
        body = r.json()
        assert [i["headline"] for i in body["items"]] == ["signal-4"]
        assert body["next_cursor"] is None  # no more

    async def test_total_respects_filters(self, db_session, client):
        """`total` must reflect the filtered count, not all signals."""
        now = datetime.now(UTC)
        # Distinct created_at per row — fingerprint hashes (asset, type,
        # timestamp_str), so same-timestamp seed rows collide.
        for i in range(3):
            await _seed_signal(
                db_session,
                created_at=now - timedelta(minutes=i),
                asset="BTC",
                headline=f"btc-{i}",
            )
        for i in range(2):
            await _seed_signal(
                db_session,
                created_at=now - timedelta(minutes=10 + i),
                asset="ETH",
                headline=f"eth-{i}",
            )

        r = await client.get("/api/signals?page=1&limit=10&asset=BTC")
        body = r.json()
        assert body["total"] == 3
        assert body["total_pages"] == 1

    async def test_page_zero_is_rejected(self, db_session, client):
        """`page` is 1-indexed; `?page=0` must 422 not return last page."""
        r = await client.get("/api/signals?page=0")
        assert r.status_code == 422

    async def test_malformed_cursor_returns_422(self, db_session, client):
        r = await client.get("/api/signals?cursor=not-valid")
        assert r.status_code == 422


# ---- List — derived fields ------------------------------------------------


class TestListDerivedFields:
    async def test_evaluated_status_when_outcome_exists(self, db_session, client):
        signal = await _seed_signal(db_session, headline="with-outcome")
        db_session.add(
            SignalOutcome(
                signal_id=signal.id,
                asset="BTC",
                signal_type="flow_anomaly",
                direction="long",
                confidence=7,
                price_at_signal=Decimal("42000"),
            )
        )
        await db_session.flush()

        r = await client.get("/api/signals")
        items = r.json()["items"]
        assert items[0]["status"] == "evaluated"

    async def test_alerted_to_counts_deliveries(self, db_session, client):
        signal = await _seed_signal(db_session)
        await _seed_delivery(db_session, signal.id, chat_id="100")
        await _seed_delivery(db_session, signal.id, chat_id="101")

        r = await client.get("/api/signals")
        items = r.json()["items"]
        assert items[0]["alerted_to"] == 2

    async def test_null_ai_analysis_yields_null_headline(self, db_session, client):
        await _seed_signal(db_session, ai_analysis=None, headline=None, confidence=None)
        r = await client.get("/api/signals")
        item = r.json()["items"][0]
        assert item["headline"] is None
        assert item["suggested_action"] is None


# ---- Detail ---------------------------------------------------------------


class TestDetail:
    async def test_returns_detail(self, db_session, client):
        signal = await _seed_signal(db_session, headline="detail-test")

        r = await client.get(f"/api/signals/{signal.id}")
        assert r.status_code == 200
        body = r.json()
        assert body["id"] == signal.id
        assert body["ai_analysis"]["headline"] == "detail-test"
        assert body["ai_analysis"]["reasoning"] == ["r1"]
        assert body["outcome"] is None
        # Frontend will truncate; backend returns full 32 chars.
        assert len(body["fingerprint"]) == 32

    async def test_unknown_id_returns_404(self, db_session, client):
        r = await client.get("/api/signals/999999")
        assert r.status_code == 404

    async def test_outcome_populated_when_row_exists(self, db_session, client):
        signal = await _seed_signal(db_session)
        db_session.add(
            SignalOutcome(
                signal_id=signal.id,
                asset="BTC",
                signal_type="flow_anomaly",
                direction="long",
                confidence=7,
                price_at_signal=Decimal("42000"),
                hit_target=True,
                evaluated_at=datetime.now(UTC),
            )
        )
        await db_session.flush()

        r = await client.get(f"/api/signals/{signal.id}")
        body = r.json()
        assert body["outcome"] is not None
        assert body["outcome"]["hit_target"] is True
        assert body["status"] == "evaluated"

    async def test_null_ai_analysis_serializes(self, db_session, client):
        signal = await _seed_signal(db_session, ai_analysis=None, headline=None)
        r = await client.get(f"/api/signals/{signal.id}")
        assert r.status_code == 200
        assert r.json()["ai_analysis"] is None

    async def test_price_at_creation_and_source_surfaced(self, db_session, client):
        """`price_at_creation` + `price_source` are populated by signal_builder
        from the live-spot composer (`get_spot_price_with_source`). The detail
        route surfaces them so the frontend can show "Spot at signal: $X (via
        sosovalue)" without waiting the 24-72h until an outcome row exists.
        Decimal → float on the wire to match the rest of the price fields."""
        signal = await _seed_signal(db_session)
        signal.price_at_creation = Decimal("82352.65")
        signal.price_source = "sosovalue"
        await db_session.flush()

        r = await client.get(f"/api/signals/{signal.id}")
        body = r.json()
        assert body["price_at_creation"] == 82352.65
        assert body["price_source"] == "sosovalue"

    async def test_price_at_creation_null_when_unset(self, db_session, client):
        """Both providers may have failed at build time → fields persist NULL.
        Endpoint serializes as JSON null (not absent), so the frontend type
        union with `null` is correct."""
        signal = await _seed_signal(db_session)
        r = await client.get(f"/api/signals/{signal.id}")
        body = r.json()
        assert body["price_at_creation"] is None
        assert body["price_source"] is None
