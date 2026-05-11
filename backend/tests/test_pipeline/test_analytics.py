"""Tests for `pipeline.analytics` — pure helpers + integration against db_session.

Layered like `test_track_record.py`:
    1. Pure-function tests first (no DB, cheap, exhaustive on edges).
    2. Integration tests against `db_session` (one fixture seed per case
       so failures point at one dimension at a time).
    3. Cache behavior — module-level TTLCache is shared across tests, so
       we autouse a fixture that clears it pre/post each test.
"""

from __future__ import annotations

import math
from datetime import UTC, date, datetime
from decimal import Decimal

import pytest

from etfpulse.models import Signal, SignalOutcome
from etfpulse.pipeline.analytics import (
    BreakdownRow,
    _breakdown_cache,
    _bucket_index,
    _build_histogram,
    _build_row,
    get_cached_track_record_breakdown,
    get_track_record_breakdown,
)
from etfpulse.pipeline.detectors import compute_fingerprint

_NOW = datetime(2026, 5, 1, 12, 0, tzinfo=UTC)


@pytest.fixture(autouse=True)
def _reset_breakdown_cache():
    """Module-level cache leaks between tests if we don't clear it.
    Same pattern as `tests/test_pipeline/test_send_worker.py:reset_track_record_cache`."""
    _breakdown_cache.clear()
    yield
    _breakdown_cache.clear()


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


class TestBucketIndex:
    """Edges are LEFT-CLOSED, RIGHT-OPEN. `_HISTOGRAM_EDGES` =
    [0, 0.005, 0.01, 0.02, 0.05, 0.10, inf]. Six buckets indexed 0..5."""

    @pytest.mark.parametrize(
        "value, expected",
        [
            (0.0, 0),  # left edge of first bucket
            (0.004, 0),  # inside first
            (0.005, 1),  # right-open boundary belongs to next bucket
            (0.0099, 1),
            (0.01, 2),
            (0.019, 2),
            (0.02, 3),
            (0.05, 4),
            (0.099, 4),
            (0.10, 5),  # last finite edge → enters open-ended bucket
            (1.0, 5),  # large value still lands in last bucket
            (math.inf, 5),  # safety
        ],
    )
    def test_bucket_index_boundaries(self, value, expected):
        assert _bucket_index(value) == expected


class TestBuildHistogram:
    def test_empty_input_returns_full_zero_buckets(self):
        """Cold-boot — chart must render an empty axis, not a missing section."""
        result = _build_histogram([])
        assert len(result) == 6
        assert all(b.count == 0 for b in result)
        # Labels and bounds still present so the FE has a stable axis.
        assert result[0].label == "<0.5%"
        assert result[-1].label == "≥10%"
        assert result[-1].upper is None  # open-ended last bucket

    def test_bucket_assignment_across_full_range(self):
        # One value per bucket — verifies the label-to-bucket mapping.
        values = [
            Decimal("0.001"),  # <0.5%
            Decimal("0.007"),  # 0.5–1%
            Decimal("0.015"),  # 1–2%
            Decimal("0.030"),  # 2–5%
            Decimal("0.080"),  # 5–10%
            Decimal("0.200"),  # ≥10%
        ]
        result = _build_histogram(values)
        assert [b.count for b in result] == [1, 1, 1, 1, 1, 1]

    def test_multiple_values_same_bucket(self):
        # All in the 1–2% bucket.
        values = [Decimal("0.011"), Decimal("0.015"), Decimal("0.019")]
        result = _build_histogram(values)
        counts_by_label = {b.label: b.count for b in result}
        assert counts_by_label["1–2%"] == 3
        # Other buckets stay zero — important for chart readability.
        assert counts_by_label["<0.5%"] == 0
        assert counts_by_label["≥10%"] == 0


class TestBuildRow:
    """Centralised hit-rate-via-helper invocation — verify the delegation
    is intact so a regression in `compute_hit_rate_pct` doesn't quietly
    skew the breakdown numbers."""

    def test_zero_targeted_returns_none_hit_rate(self):
        row = _build_row("foo", total=5, targeted=0, hits=0)
        assert row.hit_rate_pct is None

    def test_perfect_hit_rate(self):
        row = _build_row("foo", total=5, targeted=5, hits=5)
        assert row.hit_rate_pct == 100.0

    def test_fractional_rounds_to_two_decimals(self):
        # 5/6 → 83.33%
        row = _build_row("foo", total=6, targeted=6, hits=5)
        assert row.hit_rate_pct == 83.33

    def test_passes_through_counts_unchanged(self):
        row = _build_row("foo", total=10, targeted=8, hits=3)
        assert row.total == 10
        assert row.targeted == 8
        assert row.hits == 3
        assert isinstance(row, BreakdownRow)


# ---------------------------------------------------------------------------
# Integration seeding
# ---------------------------------------------------------------------------


async def _seed(
    db_session,
    *,
    asset: str = "BTC",
    signal_type: str = "flow_anomaly",
    direction: str = "long",
    confidence: int = 7,
    hit_target: bool | None = True,
    max_favorable: Decimal | None = Decimal("0.025"),
    max_adverse: Decimal | None = Decimal("0.008"),
    evaluated_at: datetime | None = _NOW,
    key: str = "x",
) -> SignalOutcome:
    """One Signal + matching SignalOutcome. Defaults produce a "long BTC
    flow_anomaly at confidence 7 that hit target with 2.5% MFE / 0.8% MAE"
    — the most-common-case row. Override per-test for the specific
    dimension under exam."""
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
        fingerprint=compute_fingerprint("analytics-test", key),
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
        target_price=Decimal("89500"),
        price_at_signal=Decimal("84200"),
        hit_target=hit_target,
        max_favorable=max_favorable,
        max_adverse=max_adverse,
        evaluated_at=evaluated_at,
    )
    db_session.add(outcome)
    await db_session.flush()
    return outcome


# ---------------------------------------------------------------------------
# get_track_record_breakdown — DB-integration
# ---------------------------------------------------------------------------


class TestGetTrackRecordBreakdown:
    async def test_empty_db_returns_zero_with_full_confidence_buckets(self, db_session):
        b = await get_track_record_breakdown(db_session)
        assert b.total_outcomes == 0
        assert b.by_detector == []
        assert b.by_asset == []
        assert b.by_direction == []
        # Confidence buckets MUST be backfilled — the "9–10 is empty" finding
        # depends on the bucket being visible to the reader.
        assert len(b.by_confidence_bucket) == 4
        assert [r.label for r in b.by_confidence_bucket] == [
            "1–3 (low)",
            "4–6 (mid)",
            "7–8 (high)",
            "9–10 (very high)",
        ]
        assert all(r.total == 0 for r in b.by_confidence_bucket)
        # Histograms still rendered with their 6 buckets, all count=0.
        assert len(b.mfe_histogram) == 6
        assert len(b.mae_histogram) == 6
        assert all(bucket.count == 0 for bucket in b.mfe_histogram)

    async def test_filter_excludes_unevaluated_rows(self, db_session):
        # Two outcomes: one evaluated, one pending (evaluated_at=NULL).
        await _seed(db_session, key="a")
        await _seed(db_session, key="b", evaluated_at=None)
        b = await get_track_record_breakdown(db_session)
        assert b.total_outcomes == 1

    async def test_by_detector_groups_correctly(self, db_session):
        await _seed(db_session, signal_type="flow_anomaly", key="a", hit_target=True)
        await _seed(db_session, signal_type="flow_anomaly", key="b", hit_target=False)
        await _seed(db_session, signal_type="magnitude", key="c", hit_target=True)
        b = await get_track_record_breakdown(db_session)
        by_label = {r.label: r for r in b.by_detector}
        assert by_label["flow_anomaly"].total == 2
        assert by_label["flow_anomaly"].hits == 1
        assert by_label["flow_anomaly"].hit_rate_pct == 50.0
        assert by_label["magnitude"].total == 1
        assert by_label["magnitude"].hit_rate_pct == 100.0

    async def test_by_asset_groups_correctly(self, db_session):
        await _seed(db_session, asset="BTC", key="a", hit_target=True)
        await _seed(db_session, asset="BTC", key="b", hit_target=True)
        await _seed(db_session, asset="ETH", key="c", hit_target=False)
        b = await get_track_record_breakdown(db_session)
        by_label = {r.label: r for r in b.by_asset}
        assert by_label["BTC"].hit_rate_pct == 100.0
        assert by_label["ETH"].hit_rate_pct == 0.0
        assert by_label["ETH"].total == 1
        assert by_label["ETH"].hits == 0

    async def test_by_direction_groups_correctly(self, db_session):
        await _seed(db_session, direction="long", key="a", hit_target=True)
        await _seed(db_session, direction="short", key="b", hit_target=False)
        await _seed(db_session, direction="short", key="c", hit_target=True)
        b = await get_track_record_breakdown(db_session)
        by_label = {r.label: r for r in b.by_direction}
        assert by_label["long"].hit_rate_pct == 100.0
        assert by_label["short"].hit_rate_pct == 50.0

    async def test_by_confidence_bucket_partitions_correctly(self, db_session):
        # One outcome per bucket — verifies the SQL CASE partitions match
        # the documented bucket edges (1–3 / 4–6 / 7–8 / 9–10).
        await _seed(db_session, confidence=2, key="low", hit_target=True)
        await _seed(db_session, confidence=5, key="mid", hit_target=False)
        await _seed(db_session, confidence=8, key="high", hit_target=True)
        await _seed(db_session, confidence=10, key="vh", hit_target=True)
        b = await get_track_record_breakdown(db_session)
        by_label = {r.label: r for r in b.by_confidence_bucket}
        assert by_label["1–3 (low)"].total == 1
        assert by_label["4–6 (mid)"].total == 1
        assert by_label["7–8 (high)"].total == 1
        assert by_label["9–10 (very high)"].total == 1

    async def test_confidence_bucket_boundary_three_goes_low(self, db_session):
        # Pin the boundary explicitly: confidence=3 → "low", confidence=4 → "mid".
        await _seed(db_session, confidence=3, key="three", hit_target=True)
        await _seed(db_session, confidence=4, key="four", hit_target=True)
        b = await get_track_record_breakdown(db_session)
        by_label = {r.label: r for r in b.by_confidence_bucket}
        assert by_label["1–3 (low)"].total == 1
        assert by_label["4–6 (mid)"].total == 1

    async def test_mfe_histogram_buckets_seeded_values(self, db_session):
        # One row per bucket so the spread is unambiguous.
        await _seed(db_session, max_favorable=Decimal("0.001"), key="a")  # <0.5%
        await _seed(db_session, max_favorable=Decimal("0.007"), key="b")  # 0.5–1%
        await _seed(db_session, max_favorable=Decimal("0.080"), key="c")  # 5–10%
        b = await get_track_record_breakdown(db_session)
        by_label = {bucket.label: bucket.count for bucket in b.mfe_histogram}
        assert by_label["<0.5%"] == 1
        assert by_label["0.5–1%"] == 1
        assert by_label["5–10%"] == 1
        assert by_label["1–2%"] == 0

    async def test_null_max_favorable_excluded_from_histogram(self, db_session):
        # max_favorable NULL (price fetch failed) — row should still count
        # in categorical breakdowns but skip the MFE histogram.
        await _seed(db_session, max_favorable=None, max_adverse=Decimal("0.005"), key="a")
        b = await get_track_record_breakdown(db_session)
        assert b.total_outcomes == 1
        assert sum(bucket.count for bucket in b.mfe_histogram) == 0
        assert sum(bucket.count for bucket in b.mae_histogram) == 1


# ---------------------------------------------------------------------------
# Cache behavior
# ---------------------------------------------------------------------------


class TestCachedBreakdown:
    async def test_cache_miss_then_hit_returns_same_instance(self, db_session):
        # First call: cache miss → fetches.
        first = await get_cached_track_record_breakdown(db_session)
        # Second call: cache hit → returns the SAME object reference.
        second = await get_cached_track_record_breakdown(db_session)
        assert first is second

    async def test_cache_clear_forces_refetch(self, db_session):
        first = await get_cached_track_record_breakdown(db_session)
        _breakdown_cache.clear()
        # After clear, the next call rebuilds — different instance.
        second = await get_cached_track_record_breakdown(db_session)
        assert first is not second

    async def test_cache_reflects_data_at_first_call_time(self, db_session):
        # Cache freezes the snapshot. Adding a row after a cached call
        # does NOT show up until clear() — important so consumers know
        # they're reading a 5-min-stale snapshot.
        await get_cached_track_record_breakdown(db_session)  # primes empty
        await _seed(db_session, key="late")
        stale = await get_cached_track_record_breakdown(db_session)
        assert stale.total_outcomes == 0
        _breakdown_cache.clear()
        fresh = await get_cached_track_record_breakdown(db_session)
        assert fresh.total_outcomes == 1
