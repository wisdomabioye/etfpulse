"""PR I.3 — per-detector precision aggregator tests.

Four layers, ordered cheap → expensive:

1. Empty-state contracts (full grid present, regime_shift excluded).
2. Aggregation correctness (grouping, horizon bucketing, totals).
3. Filter behaviour (cohort version, lookback window, hit_target nullability).
4. Threshold behaviour + report-shape canary (min_samples gate, params echo,
   defensive dedupe on signal_type collisions).

The Wilson-CI math is pinned in `test_calibration.py`; here we verify
the aggregator wires it to the right cells.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from etfpulse.pipeline.per_detector import (
    DetectorRow,
    PerDetectorReport,
    compute_per_detector,
)
from tests._helpers.seed_outcomes import seed_signal_with_outcome

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _row_by_type(report: PerDetectorReport, signal_type: str) -> DetectorRow | None:
    """Lookup helper — keeps the assertion blocks readable."""
    for row in report.detectors:
        if row.signal_type == signal_type:
            return row
    return None


_DEFAULT_CALL_KWARGS = {
    "ai_prompt_version": "v3",
    "lookback_days": 90,
    "min_samples": 3,
}


# ---------------------------------------------------------------------------
# Layer 1 — empty-state contracts
# ---------------------------------------------------------------------------


class TestEmptyDB:
    async def test_returns_full_grid_with_zero_samples(self, db_session):
        report = await compute_per_detector(db_session, **_DEFAULT_CALL_KWARGS)

        # 4 registered non-excluded detectors must always appear.
        assert {r.signal_type for r in report.detectors} == {
            "flow_anomaly",
            "magnitude",
            "acceleration",
            "divergence",
        }
        for row in report.detectors:
            assert row.total.n_samples == 0
            assert row.total.hit_rate is None
            for horizon in ("scalp", "swing", "position", "legacy"):
                cell = row.horizons[horizon]
                assert cell.n_samples == 0
                assert cell.hit_rate is None
                assert cell.ci_low is None
                assert cell.ci_high is None

    async def test_regime_shift_never_appears(self, db_session):
        # PR I.3b will fold this in. Until then it's structurally excluded.
        report = await compute_per_detector(db_session, **_DEFAULT_CALL_KWARGS)
        assert all(r.signal_type != "regime_shift" for r in report.detectors)

    async def test_detector_order_matches_registry(self, db_session):
        # Order is ALL_DETECTORS precedence (excluding regime_shift), then
        # any legacy types alphabetically. With empty DB only the registered
        # 4 appear, in the same order as pipeline/detectors/__init__.py.
        report = await compute_per_detector(db_session, **_DEFAULT_CALL_KWARGS)
        types = [r.signal_type for r in report.detectors]
        assert types == ["flow_anomaly", "magnitude", "acceleration", "divergence"]


# ---------------------------------------------------------------------------
# Layer 2 — aggregation correctness
# ---------------------------------------------------------------------------


class TestAggregation:
    async def test_groups_by_signal_type(self, db_session):
        # 3 acceleration wins, 1 acceleration loss → 75% hit rate on
        # acceleration; magnitude untouched.
        for i in range(3):
            await seed_signal_with_outcome(
                db_session, key=f"acc-win-{i}", signal_type="acceleration", hit_target=True
            )
        await seed_signal_with_outcome(
            db_session, key="acc-loss", signal_type="acceleration", hit_target=False
        )

        report = await compute_per_detector(db_session, **_DEFAULT_CALL_KWARGS)
        acc = _row_by_type(report, "acceleration")
        assert acc is not None
        assert acc.total.n_samples == 4
        assert acc.total.wins == 3
        assert acc.total.losses == 1
        assert acc.total.hit_rate == 0.75
        assert acc.total.ci_low is not None and acc.total.ci_high is not None

        # Unrelated detector untouched.
        flow = _row_by_type(report, "flow_anomaly")
        assert flow is not None
        assert flow.total.n_samples == 0

    async def test_horizon_bucketing(self, db_session):
        # 2 magnitude swing wins + 1 magnitude position loss → split by
        # horizon, total aggregates. min_samples=1 here because the test's
        # point is bucketing correctness, not the threshold gate.
        for i in range(2):
            await seed_signal_with_outcome(
                db_session,
                key=f"mag-swing-{i}",
                signal_type="magnitude",
                hit_target=True,
                window_hours=72,
            )
        await seed_signal_with_outcome(
            db_session,
            key="mag-pos",
            signal_type="magnitude",
            hit_target=False,
            window_hours=168,
        )

        report = await compute_per_detector(
            db_session, ai_prompt_version="v3", lookback_days=90, min_samples=1
        )
        mag = _row_by_type(report, "magnitude")
        assert mag is not None
        assert mag.horizons["swing"].n_samples == 2
        assert mag.horizons["swing"].wins == 2
        assert mag.horizons["swing"].hit_rate == 1.0
        assert mag.horizons["position"].n_samples == 1
        assert mag.horizons["position"].wins == 0
        # Total aggregates across horizons.
        assert mag.total.n_samples == 3
        assert mag.total.wins == 2

    async def test_legacy_bucket_for_null_window_hours(self, db_session):
        # window_hours=None → "legacy" bucket (pre-PR-B v2 rubric).
        await seed_signal_with_outcome(
            db_session,
            key="div-legacy",
            signal_type="divergence",
            hit_target=True,
            window_hours=None,
        )
        report = await compute_per_detector(db_session, **_DEFAULT_CALL_KWARGS)
        div = _row_by_type(report, "divergence")
        assert div is not None
        assert div.horizons["legacy"].n_samples == 1
        # Below min_samples=3, so hit_rate stays null even though wins=1.
        assert div.horizons["legacy"].hit_rate is None

    async def test_legacy_signal_type_appended_alphabetically(self, db_session):
        # A signal_type that's NOT in ALL_DETECTORS (e.g. a removed detector)
        # should still surface in the report — sorted to the tail.
        await seed_signal_with_outcome(
            db_session,
            key="old1",
            signal_type="old_detector_b",
            hit_target=True,
        )
        await seed_signal_with_outcome(
            db_session,
            key="old2",
            signal_type="old_detector_a",
            hit_target=True,
        )
        report = await compute_per_detector(db_session, **_DEFAULT_CALL_KWARGS)
        types = [r.signal_type for r in report.detectors]
        # Registered detectors first, then legacy types alphabetically.
        assert types == [
            "flow_anomaly",
            "magnitude",
            "acceleration",
            "divergence",
            "old_detector_a",
            "old_detector_b",
        ]


# ---------------------------------------------------------------------------
# Layer 3 — filter behaviour
# ---------------------------------------------------------------------------


class TestFilters:
    async def test_ai_prompt_version_filter(self, db_session):
        # Seed v2 + v3 wins for acceleration. v3 query returns v3 only.
        await seed_signal_with_outcome(
            db_session,
            key="v2-acc",
            signal_type="acceleration",
            ai_prompt_version="v2",
            hit_target=True,
        )
        await seed_signal_with_outcome(
            db_session,
            key="v3-acc",
            signal_type="acceleration",
            ai_prompt_version="v3",
            hit_target=False,
        )

        v3_report = await compute_per_detector(
            db_session, ai_prompt_version="v3", lookback_days=90, min_samples=1
        )
        acc_v3 = _row_by_type(v3_report, "acceleration")
        assert acc_v3 is not None
        assert acc_v3.total.n_samples == 1
        assert acc_v3.total.wins == 0
        assert acc_v3.total.losses == 1

        v2_report = await compute_per_detector(
            db_session, ai_prompt_version="v2", lookback_days=90, min_samples=1
        )
        acc_v2 = _row_by_type(v2_report, "acceleration")
        assert acc_v2 is not None
        assert acc_v2.total.n_samples == 1
        assert acc_v2.total.wins == 1

    async def test_lookback_window_excludes_old_outcomes(self, db_session):
        # Outcome evaluated 100 days ago — should be excluded by a 90-day lookback.
        old = datetime.now(UTC) - timedelta(days=100)
        recent = datetime.now(UTC) - timedelta(days=5)
        await seed_signal_with_outcome(
            db_session,
            key="acc-old",
            signal_type="acceleration",
            hit_target=True,
            evaluated_at=old,
        )
        await seed_signal_with_outcome(
            db_session,
            key="acc-recent",
            signal_type="acceleration",
            hit_target=False,
            evaluated_at=recent,
        )

        report = await compute_per_detector(
            db_session, ai_prompt_version="v3", lookback_days=90, min_samples=1
        )
        acc = _row_by_type(report, "acceleration")
        assert acc is not None
        assert acc.total.n_samples == 1  # only the recent loss
        assert acc.total.wins == 0
        assert acc.total.losses == 1

    async def test_unevaluated_outcomes_excluded(self, db_session):
        # evaluated_at IS NULL → not counted (shared predicate with
        # calibration / track_record).
        await seed_signal_with_outcome(
            db_session,
            key="unevaluated",
            signal_type="acceleration",
            hit_target=True,
            evaluated_at=None,
        )
        report = await compute_per_detector(
            db_session, ai_prompt_version="v3", lookback_days=90, min_samples=1
        )
        acc = _row_by_type(report, "acceleration")
        assert acc is not None
        assert acc.total.n_samples == 0

    async def test_null_hit_target_excluded(self, db_session):
        # hit_target IS NULL → AI declined to set a target. Not in numerator
        # OR denominator — matches calibration's classification rule.
        await seed_signal_with_outcome(
            db_session,
            key="no-target",
            signal_type="acceleration",
            hit_target=None,
            target_price=None,
        )
        report = await compute_per_detector(
            db_session, ai_prompt_version="v3", lookback_days=90, min_samples=1
        )
        acc = _row_by_type(report, "acceleration")
        assert acc is not None
        assert acc.total.n_samples == 0


class TestMinSamplesGate:
    async def test_below_min_samples_null_hit_rate(self, db_session):
        # 2 wins for acceleration, min_samples=3 → hit_rate stays null
        # but wins/losses/n_samples populated so FE can show "n=2 (need 3)".
        for i in range(2):
            await seed_signal_with_outcome(
                db_session,
                key=f"acc-{i}",
                signal_type="acceleration",
                hit_target=True,
            )

        report = await compute_per_detector(
            db_session, ai_prompt_version="v3", lookback_days=90, min_samples=3
        )
        acc = _row_by_type(report, "acceleration")
        assert acc is not None
        assert acc.total.n_samples == 2
        assert acc.total.wins == 2
        assert acc.total.hit_rate is None
        assert acc.total.ci_low is None
        assert acc.total.ci_high is None

    async def test_at_min_samples_hit_rate_populated(self, db_session):
        for i in range(3):
            await seed_signal_with_outcome(
                db_session,
                key=f"acc-{i}",
                signal_type="acceleration",
                hit_target=(i == 0),
            )

        report = await compute_per_detector(
            db_session, ai_prompt_version="v3", lookback_days=90, min_samples=3
        )
        acc = _row_by_type(report, "acceleration")
        assert acc is not None
        assert acc.total.n_samples == 3
        assert acc.total.wins == 1
        assert acc.total.hit_rate == 1 / 3
        # Wilson CI bounds present when hit_rate is.
        assert acc.total.ci_low is not None
        assert acc.total.ci_high is not None
        assert 0.0 <= acc.total.ci_low <= acc.total.hit_rate <= acc.total.ci_high <= 1.0

    async def test_total_gates_independently_from_horizon_cells(self, db_session):
        # 6 swing wins for acceleration → swing cell hit_rate populated (n=6 >= 3),
        # legacy cell stays null (n=0). Total also populated (n=6).
        for i in range(6):
            await seed_signal_with_outcome(
                db_session,
                key=f"acc-swing-{i}",
                signal_type="acceleration",
                hit_target=(i < 4),  # 4 wins, 2 losses
                window_hours=72,
            )

        report = await compute_per_detector(
            db_session, ai_prompt_version="v3", lookback_days=90, min_samples=3
        )
        acc = _row_by_type(report, "acceleration")
        assert acc is not None
        assert acc.horizons["swing"].hit_rate is not None
        assert acc.horizons["swing"].n_samples == 6
        # All other horizons untouched.
        for horizon in ("scalp", "position", "legacy"):
            assert acc.horizons[horizon].n_samples == 0
            assert acc.horizons[horizon].hit_rate is None
        # Total mirrors swing (only horizon with data).
        assert acc.total.n_samples == 6
        assert acc.total.hit_rate is not None

    async def test_total_above_min_when_per_horizon_below(self, db_session):
        # Spread 5 outcomes across all 4 horizons (2/1/1/1). With min=3,
        # every per-horizon cell is below threshold, but total (n=5) is above.
        # This is the heart of Option C rendering.
        await seed_signal_with_outcome(
            db_session,
            key="acc-s1",
            signal_type="acceleration",
            hit_target=True,
            window_hours=72,
        )
        await seed_signal_with_outcome(
            db_session,
            key="acc-s2",
            signal_type="acceleration",
            hit_target=True,
            window_hours=72,
        )
        await seed_signal_with_outcome(
            db_session,
            key="acc-p1",
            signal_type="acceleration",
            hit_target=True,
            window_hours=168,
        )
        await seed_signal_with_outcome(
            db_session,
            key="acc-l1",
            signal_type="acceleration",
            hit_target=False,
            window_hours=None,
        )
        await seed_signal_with_outcome(
            db_session,
            key="acc-c1",
            signal_type="acceleration",
            hit_target=True,
            window_hours=6,  # scalp
        )

        report = await compute_per_detector(
            db_session, ai_prompt_version="v3", lookback_days=90, min_samples=3
        )
        acc = _row_by_type(report, "acceleration")
        assert acc is not None
        # Every per-horizon cell below min_samples=3.
        for horizon in ("scalp", "swing", "position", "legacy"):
            assert acc.horizons[horizon].hit_rate is None
            assert acc.horizons[horizon].n_samples in (1, 2)
        # Total IS above threshold.
        assert acc.total.n_samples == 5
        assert acc.total.wins == 4
        assert acc.total.losses == 1
        assert acc.total.hit_rate == 0.8


# ---------------------------------------------------------------------------
# Layer 4 — report-level shape
# ---------------------------------------------------------------------------


class TestReportShape:
    async def test_carries_params_back(self, db_session):
        report = await compute_per_detector(
            db_session,
            ai_prompt_version="v3",
            lookback_days=45,
            min_samples=5,
        )
        assert report.ai_prompt_version == "v3"
        assert report.lookback_days == 45
        assert report.min_samples == 5

    async def test_counts_outcome_regardless_of_seed_decimal_precision(self, db_session):
        # Defensive: target_price/price_at_signal are Decimal columns. The
        # aggregator MUST NOT depend on (or accidentally coerce) those —
        # GROUP BY is on signal_type + window_hours only. This regression
        # guard fires if a future schema change folds price columns into
        # the aggregation key by mistake.
        await seed_signal_with_outcome(
            db_session,
            key="precision",
            signal_type="acceleration",
            hit_target=True,
            target_price=Decimal("99999.99"),
            price_at_signal=Decimal("100000.00"),
        )
        report = await compute_per_detector(
            db_session, ai_prompt_version="v3", lookback_days=90, min_samples=1
        )
        acc = _row_by_type(report, "acceleration")
        assert acc is not None
        assert acc.total.wins == 1
        assert acc.total.n_samples == 1

    async def test_dedupes_registered_signal_types(self, monkeypatch):
        # `tests/test_detectors_framework.py` pins detector NAME uniqueness
        # but does NOT pin SIGNAL_TYPE uniqueness — the Protocol allows two
        # detectors to share a signal_type (e.g. an A/B test). Without
        # dedupe, the aggregator would emit duplicate rows for that type
        # and the FE's Map-keyed render would collide. This test simulates
        # the collision and confirms `_ordered_detector_list` first-wins
        # dedupe behaviour without needing a DB.
        from types import SimpleNamespace

        from etfpulse.pipeline import per_detector as per_detector_mod

        # Two stubs sharing signal_type="acceleration" — same shape as the
        # Detector Protocol exposes (name + signal_type attrs).
        fake_registry = [
            SimpleNamespace(name="acc_v1", signal_type="acceleration"),
            SimpleNamespace(name="acc_v2", signal_type="acceleration"),  # collision
            SimpleNamespace(name="mag_v1", signal_type="magnitude"),
        ]
        monkeypatch.setattr(per_detector_mod, "ALL_DETECTORS", fake_registry)

        ordered = per_detector_mod._ordered_detector_list(set())
        # First occurrence of "acceleration" kept; second dropped.
        assert ordered == ["acceleration", "magnitude"]
