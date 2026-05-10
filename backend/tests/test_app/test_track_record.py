"""GET /api/track-record — summary aggregate + paginated outcomes list.

Pattern: same dependency-override + AsyncClient/ASGITransport as test_signals
and test_dashboard. Seeds SignalOutcome rows via db_session, asserts on the
response shape + filter/pagination correctness.
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
from etfpulse.models import Signal, SignalOutcome
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


# ---------------------------------------------------------------------------
# Seed helpers
# ---------------------------------------------------------------------------


_NOW = datetime(2026, 5, 1, 12, 0, tzinfo=UTC)


async def _seed_signal_with_outcome(
    db_session,
    *,
    asset: str = "BTC",
    signal_type: str = "flow_anomaly",
    direction: str = "long",
    confidence: int = 7,
    hit_target: bool | None = None,
    hit_stop: bool | None = None,
    evaluated_at: datetime | None = None,
    key: str = "x",
    ai_prompt_version: str = "v3",
) -> SignalOutcome:
    """Seed one Signal + one SignalOutcome (the public surface for /track-record).

    Defaults produce the most common case — a long BTC signal where the
    eval already ran. Override per-test for the edge cases."""
    signal = Signal(
        signal_type=signal_type,
        asset=asset,
        trigger_data={},
        ai_analysis={"suggested_action": "consider long", "headline": "x"},
        confidence=confidence,
        status="alerted",
        price_at_creation=Decimal("84200"),
        price_source="binance",
        ai_prompt_version=ai_prompt_version,
        fingerprint=compute_fingerprint("track-record-test", key),
        signal_date=date(2026, 4, 25),
    )
    db_session.add(signal)
    await db_session.flush()

    outcome = SignalOutcome(
        signal_id=signal.id,
        asset=asset,
        signal_type=signal_type,
        direction=direction,
        confidence=confidence,
        entry_price=Decimal("84200"),
        stop_price=Decimal("82000"),
        target_price=Decimal("89500"),
        price_at_signal=Decimal("84200"),
        price_after_24h=Decimal("85100"),
        price_after_72h=Decimal("89600") if hit_target else Decimal("84500"),
        hit_target=hit_target,
        hit_stop=hit_stop,
        max_favorable=Decimal("0.064") if hit_target else Decimal("0.011"),
        max_adverse=Decimal("0.005"),
        evaluated_at=evaluated_at or _NOW,
    )
    db_session.add(outcome)
    await db_session.flush()
    return outcome


# ---------------------------------------------------------------------------
# Empty state
# ---------------------------------------------------------------------------


class TestEmptyTrackRecord:
    async def test_empty_db_returns_zero_summary_and_no_items(self, db_session, client):
        r = await client.get("/api/track-record")
        assert r.status_code == 200
        body = r.json()

        assert body["items"] == []
        assert body["total"] == 0
        assert body["next_cursor"] is None
        assert body["page"] is None  # no `?page=` supplied → cursor mode
        assert body["total_pages"] == 0

        s = body["summary"]
        assert s == {
            "total_evaluated": 0,
            "targets_hit": 0,
            "stops_hit": 0,
            "targeted_count": 0,
            "hit_rate_pct": None,
            "avg_confidence_hits": None,
            "avg_confidence_misses": None,
        }


# ---------------------------------------------------------------------------
# Summary stats
# ---------------------------------------------------------------------------


class TestSummary:
    async def test_counts_match_seeded_outcomes(self, db_session, client):
        await _seed_signal_with_outcome(db_session, hit_target=True, key="hit1")
        await _seed_signal_with_outcome(db_session, hit_target=True, key="hit2")
        await _seed_signal_with_outcome(db_session, hit_target=False, hit_stop=True, key="stop1")
        await _seed_signal_with_outcome(db_session, hit_target=False, hit_stop=False, key="neither")

        r = await client.get("/api/track-record")
        s = r.json()["summary"]

        assert s["total_evaluated"] == 4
        assert s["targets_hit"] == 2
        assert s["stops_hit"] == 1
        assert s["targeted_count"] == 4  # all four have a target set
        # 2/4 = 50%
        assert s["hit_rate_pct"] == 50.0

    async def test_hit_rate_excludes_signals_with_no_target(self, db_session, client):
        """Signals where hit_target IS NULL (AI declined to volunteer one)
        must NOT inflate the denominator. Verifies the `targeted_count`
        narrowing in the schema."""
        # 2 with targets, 1 hit; 2 without targets at all.
        await _seed_signal_with_outcome(db_session, hit_target=True, key="hit")
        await _seed_signal_with_outcome(db_session, hit_target=False, key="miss")
        await _seed_signal_with_outcome(db_session, hit_target=None, key="no-target-1")
        await _seed_signal_with_outcome(db_session, hit_target=None, key="no-target-2")

        s = (await client.get("/api/track-record")).json()["summary"]

        assert s["total_evaluated"] == 4
        assert s["targeted_count"] == 2
        # 1/2 = 50%, NOT 1/4 = 25%
        assert s["hit_rate_pct"] == 50.0

    async def test_avg_confidence_split_by_hit_status(self, db_session, client):
        await _seed_signal_with_outcome(db_session, hit_target=True, confidence=8, key="h1")
        await _seed_signal_with_outcome(db_session, hit_target=True, confidence=10, key="h2")
        await _seed_signal_with_outcome(db_session, hit_target=False, confidence=4, key="m1")
        await _seed_signal_with_outcome(db_session, hit_target=False, confidence=6, key="m2")

        s = (await client.get("/api/track-record")).json()["summary"]

        # AVG hits: (8+10)/2 = 9.0
        assert s["avg_confidence_hits"] == 9.0
        # AVG misses: (4+6)/2 = 5.0
        assert s["avg_confidence_misses"] == 5.0

    async def test_avg_confidence_none_when_bucket_empty(self, db_session, client):
        await _seed_signal_with_outcome(db_session, hit_target=True, confidence=8, key="h")
        # No misses at all.

        s = (await client.get("/api/track-record")).json()["summary"]
        assert s["avg_confidence_hits"] == 8.0
        assert s["avg_confidence_misses"] is None


# ---------------------------------------------------------------------------
# Filters
# ---------------------------------------------------------------------------


class TestFilters:
    async def test_asset_filter_narrows_summary_and_items(self, db_session, client):
        """Per the route's design — summary mirrors filters. A user filtering
        to BTC sees BTC-specific hit rate next to BTC-specific items."""
        await _seed_signal_with_outcome(db_session, asset="BTC", hit_target=True, key="btc1")
        await _seed_signal_with_outcome(db_session, asset="BTC", hit_target=False, key="btc2")
        await _seed_signal_with_outcome(db_session, asset="ETH", hit_target=True, key="eth1")
        await _seed_signal_with_outcome(db_session, asset="ETH", hit_target=True, key="eth2")

        r = await client.get("/api/track-record?asset=BTC")
        body = r.json()

        assert body["summary"]["total_evaluated"] == 2
        assert body["summary"]["targets_hit"] == 1
        assert body["summary"]["hit_rate_pct"] == 50.0
        assert len(body["items"]) == 2
        assert all(item["asset"] == "BTC" for item in body["items"])

    async def test_signal_type_filter(self, db_session, client):
        await _seed_signal_with_outcome(
            db_session, signal_type="flow_anomaly", hit_target=True, key="fa"
        )
        await _seed_signal_with_outcome(
            db_session, signal_type="magnitude", hit_target=False, key="mag"
        )

        r = await client.get("/api/track-record?signal_type=flow_anomaly")
        body = r.json()

        assert body["summary"]["total_evaluated"] == 1
        assert len(body["items"]) == 1
        assert body["items"][0]["signal_type"] == "flow_anomaly"

    async def test_confidence_min_filter(self, db_session, client):
        await _seed_signal_with_outcome(db_session, confidence=3, key="low")
        await _seed_signal_with_outcome(db_session, confidence=5, key="mid")
        await _seed_signal_with_outcome(db_session, confidence=8, key="high")

        body = (await client.get("/api/track-record?confidence_min=5")).json()
        assert body["summary"]["total_evaluated"] == 2
        assert {item["confidence"] for item in body["items"]} == {5, 8}

    async def test_invalid_confidence_min_rejected(self, db_session, client):
        # Field constraint ge=1 le=10 — 0 and 11 must 422.
        for bad in (0, 11, -1):
            r = await client.get(f"/api/track-record?confidence_min={bad}")
            assert r.status_code == 422

    async def test_ai_prompt_version_filter_narrows(self, db_session, client):
        """Issue #32 — cross-version filter so a prompt bump doesn't
        pollute the headline hit rate. v2 cohort and v3 cohort must
        each slice to their own outcomes only."""
        await _seed_signal_with_outcome(
            db_session, hit_target=True, key="v2-hit", ai_prompt_version="v2"
        )
        await _seed_signal_with_outcome(
            db_session, hit_target=False, key="v2-miss", ai_prompt_version="v2"
        )
        await _seed_signal_with_outcome(
            db_session, hit_target=True, key="v3-1", ai_prompt_version="v3"
        )

        body_v2 = (await client.get("/api/track-record?ai_prompt_version=v2")).json()
        assert body_v2["summary"]["total_evaluated"] == 2
        assert body_v2["summary"]["targets_hit"] == 1
        assert body_v2["summary"]["hit_rate_pct"] == 50.0
        assert len(body_v2["items"]) == 2

        body_v3 = (await client.get("/api/track-record?ai_prompt_version=v3")).json()
        assert body_v3["summary"]["total_evaluated"] == 1
        assert body_v3["summary"]["targets_hit"] == 1
        assert body_v3["summary"]["hit_rate_pct"] == 100.0

    async def test_ai_prompt_version_multi_digit_accepted(self, db_session, client):
        """Pattern `^v[0-9]+$` accepts multi-digit cohorts — v10, v12, v100.
        A prompt-version bump past v9 must not silently break the filter."""
        await _seed_signal_with_outcome(
            db_session, hit_target=True, key="v12-only", ai_prompt_version="v12"
        )
        await _seed_signal_with_outcome(
            db_session, hit_target=False, key="v3-other", ai_prompt_version="v3"
        )

        body = (await client.get("/api/track-record?ai_prompt_version=v12")).json()
        assert body["summary"]["total_evaluated"] == 1
        assert body["items"][0]["confidence"] == 7  # the v12 one

    async def test_ai_prompt_version_invalid_format_rejected(self, db_session, client):
        """Pattern `^v[0-9]+$` matches DB CHECK — `v3`, `v12` ok;
        `3`, `v`, `v3.0`, `vX` rejected with 422."""
        for bad in ("3", "v", "v3.0", "vX", "version3"):
            r = await client.get(f"/api/track-record?ai_prompt_version={bad}")
            assert r.status_code == 422, f"expected 422 for {bad!r}"

    async def test_ai_prompt_version_combines_with_other_filters(self, db_session, client):
        """Cross-version filter composes with asset/signal_type/confidence_min
        — verifies the JOIN-when-set logic doesn't shadow the other WHEREs."""
        await _seed_signal_with_outcome(
            db_session,
            asset="BTC",
            hit_target=True,
            key="match",
            ai_prompt_version="v3",
        )
        await _seed_signal_with_outcome(
            db_session,
            asset="ETH",
            hit_target=True,
            key="wrong-asset",
            ai_prompt_version="v3",
        )
        await _seed_signal_with_outcome(
            db_session,
            asset="BTC",
            hit_target=True,
            key="wrong-version",
            ai_prompt_version="v2",
        )

        body = (await client.get("/api/track-record?asset=BTC&ai_prompt_version=v3")).json()
        assert body["summary"]["total_evaluated"] == 1
        assert body["items"][0]["asset"] == "BTC"


# ---------------------------------------------------------------------------
# Pagination
# ---------------------------------------------------------------------------


class TestPagination:
    async def test_cursor_mode_default_returns_next_cursor_when_more(self, db_session, client):
        # Seed 5 outcomes with strictly increasing evaluated_at so the
        # ORDER BY DESC sort is deterministic.
        for i in range(5):
            await _seed_signal_with_outcome(
                db_session,
                hit_target=True,
                evaluated_at=_NOW - timedelta(hours=i),
                key=f"e{i}",
            )

        r = await client.get("/api/track-record?limit=2")
        body = r.json()
        assert len(body["items"]) == 2
        assert body["next_cursor"] is not None
        assert body["page"] is None
        assert body["total"] == 5
        assert body["total_pages"] == 3

    async def test_cursor_mode_no_next_when_all_fit(self, db_session, client):
        await _seed_signal_with_outcome(db_session, hit_target=True, key="only")

        body = (await client.get("/api/track-record?limit=20")).json()
        assert len(body["items"]) == 1
        assert body["next_cursor"] is None

    async def test_page_mode_offset_and_total_pages(self, db_session, client):
        for i in range(7):
            await _seed_signal_with_outcome(
                db_session,
                hit_target=True,
                evaluated_at=_NOW - timedelta(hours=i),
                key=f"p{i}",
            )

        r = await client.get("/api/track-record?page=2&limit=3")
        body = r.json()

        assert body["page"] == 2
        assert body["total"] == 7
        # ceil(7/3) = 3
        assert body["total_pages"] == 3
        assert len(body["items"]) == 3

    async def test_page_mode_last_page_partial(self, db_session, client):
        for i in range(7):
            await _seed_signal_with_outcome(
                db_session,
                hit_target=True,
                evaluated_at=_NOW - timedelta(hours=i),
                key=f"p{i}",
            )

        # Page 3 of 3 has 1 row (7 - 2*3 = 1).
        body = (await client.get("/api/track-record?page=3&limit=3")).json()
        assert len(body["items"]) == 1

    async def test_invalid_cursor_returns_422(self, db_session, client):
        r = await client.get("/api/track-record?cursor=garbage")
        assert r.status_code == 422

    async def test_cursor_walk_returns_each_row_exactly_once(self, db_session, client):
        # Walk through the cursor-paginated list and verify every seeded
        # row appears exactly once with no gaps or dupes.
        for i in range(5):
            await _seed_signal_with_outcome(
                db_session,
                hit_target=True,
                evaluated_at=_NOW - timedelta(hours=i),
                key=f"walk{i}",
            )

        seen_ids: list[int] = []
        cursor: str | None = None
        for _ in range(10):  # safety bound
            url = "/api/track-record?limit=2"
            if cursor:
                url += f"&cursor={cursor}"
            body = (await client.get(url)).json()
            seen_ids.extend(item["id"] for item in body["items"])
            cursor = body["next_cursor"]
            if cursor is None:
                break
        else:
            pytest.fail("cursor walk did not terminate within 10 pages")

        assert len(seen_ids) == 5
        assert len(set(seen_ids)) == 5  # no duplicates


# ---------------------------------------------------------------------------
# Item shape
# ---------------------------------------------------------------------------


class TestItemShape:
    async def test_item_carries_denormalized_signal_fields(self, db_session, client):
        """Each item is renderable on its own — no JOIN needed by the FE."""
        await _seed_signal_with_outcome(
            db_session, asset="ETH", signal_type="magnitude", hit_target=True, key="shape"
        )

        item = (await client.get("/api/track-record")).json()["items"][0]
        assert set(item.keys()) >= {
            "id",
            "signal_id",
            "asset",
            "signal_type",
            "direction",
            "confidence",
            "entry_price",
            "stop_price",
            "target_price",
            "price_at_signal",
            "price_after_24h",
            "price_after_72h",
            "hit_target",
            "hit_stop",
            "max_favorable",
            "max_adverse",
            "evaluated_at",
        }
        assert item["asset"] == "ETH"
        assert item["signal_type"] == "magnitude"

    async def test_decimal_prices_serialize_as_floats(self, db_session, client):
        await _seed_signal_with_outcome(db_session, hit_target=True, key="num")
        item = (await client.get("/api/track-record")).json()["items"][0]
        # Postgres NUMERIC → Python Decimal → Pydantic float | None → JSON number.
        # Frontend type matches `number | null`; a string would break that contract.
        assert isinstance(item["entry_price"], float)
        assert isinstance(item["price_at_signal"], float)


# ---------------------------------------------------------------------------
# Sort + ordering
# ---------------------------------------------------------------------------


class TestSortOrder:
    async def test_results_ordered_by_evaluated_at_desc(self, db_session, client):
        """Newest-first ordering pinned — the track record is a chronological
        transparency document; the frontend depends on this implicit sort."""
        for i in range(3):
            await _seed_signal_with_outcome(
                db_session,
                hit_target=True,
                evaluated_at=_NOW - timedelta(hours=i),
                key=f"sort{i}",
            )

        items = (await client.get("/api/track-record")).json()["items"]
        timestamps = [datetime.fromisoformat(item["evaluated_at"]) for item in items]
        assert timestamps == sorted(timestamps, reverse=True)
