"""PR I.1 — confidence calibration tests.

Three layers, ordered from cheap to expensive:

1. Pure helpers (`make_buckets`, `wilson_ci`, `classify_outcome_as_win`)
   — no DB. Tests dominate this file because the math correctness is
   pinned here; the integration layer just verifies the wiring.

2. Integration (`compute_calibration`) — runs against `db_session`.
   Seeds outcomes with known confidence/horizon/hit_target and asserts
   the orchestrator returns the expected reliability grid.

3. Cross-module DRY — pins that the route reuses
   `evaluated_outcomes_predicate()` (same definition as calibration).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from etfpulse.models import SignalOutcome
from etfpulse.pipeline.calibration import (
    CalibrationBucket,
    CalibrationReport,
    classify_outcome_as_win,
    compute_calibration,
    make_buckets,
    wilson_ci,
)
from etfpulse.pipeline.track_record import (
    HORIZON_LABELS,
    evaluated_outcomes_predicate,
)
from tests._helpers.seed_outcomes import seed_signal_with_outcome

# ---------------------------------------------------------------------------
# make_buckets — pure
# ---------------------------------------------------------------------------


class TestMakeBuckets:
    def test_default_size_two_returns_five_pairs(self):
        # The default — 5 buckets spanning confidence 1..10 in pairs.
        buckets = make_buckets()
        assert buckets == [(1, 2), (3, 4), (5, 6), (7, 8), (9, 10)]

    def test_size_one_returns_ten_singletons(self):
        # Per-confidence-level resolution; useful when N is large enough.
        buckets = make_buckets(bucket_size=1)
        assert buckets == [(i, i) for i in range(1, 11)]
        assert len(buckets) == 10

    def test_size_five_returns_two_halves(self):
        # Low-vs-high split — coarsest meaningful resolution above
        # "everything together".
        assert make_buckets(bucket_size=5) == [(1, 5), (6, 10)]

    def test_size_ten_returns_single_bucket(self):
        # Effectively "no bucketing" — collapses all confidences into one.
        # Still a legal config (lets the FE turn off the bucket axis).
        assert make_buckets(bucket_size=10) == [(1, 10)]

    def test_invalid_size_three_raises_value_error(self):
        # 3 doesn't divide 10 evenly. Strict to avoid partial trailing
        # buckets that would skew downstream rendering.
        with pytest.raises(ValueError, match="bucket_size must be one of"):
            make_buckets(bucket_size=3)

    def test_invalid_size_zero_raises(self):
        # Defensive against a config bug where bucket_size = 0 reaches
        # this helper.
        with pytest.raises(ValueError):
            make_buckets(bucket_size=0)

    def test_invalid_size_eleven_raises(self):
        with pytest.raises(ValueError):
            make_buckets(bucket_size=11)


# ---------------------------------------------------------------------------
# wilson_ci — pure
# ---------------------------------------------------------------------------


class TestWilsonCI:
    def test_zero_n_returns_none_pair(self):
        # Proportion undefined with zero trials. The None pair is the
        # contract the FE uses to render "—".
        assert wilson_ci(0, 0) == (None, None)

    def test_zero_wins_does_not_go_below_zero(self):
        # The Wilson formula can produce slightly-negative low bounds
        # near 0; we clamp. (0 wins / 10 trials) should report a low
        # bound of exactly 0.0, never -0.05.
        lo, hi = wilson_ci(0, 10)
        assert lo == 0.0
        # Upper bound is meaningfully > 0 (the famous "you haven't seen
        # any yet but it could still happen" property).
        assert hi is not None
        assert 0.20 < hi < 0.35

    def test_all_wins_does_not_exceed_one(self):
        # Mirror of test above — clamp to <=1.
        lo, hi = wilson_ci(10, 10)
        assert hi == 1.0
        assert lo is not None
        assert 0.65 < lo < 0.80

    def test_half_wins_is_centred(self):
        # 5/10 — CI should bracket 0.5 roughly symmetrically.
        lo, hi = wilson_ci(5, 10)
        assert lo is not None
        assert hi is not None
        # Width-around-0.5: lo + hi ≈ 1.0 by symmetry of Wilson at p=0.5.
        assert abs((lo + hi) - 1.0) < 0.01
        # Standard table value: Wilson(5,10) ≈ [0.237, 0.763].
        assert 0.22 < lo < 0.26
        assert 0.74 < hi < 0.78

    def test_known_value_eight_out_of_ten(self):
        # Standard table reference: Wilson(8, 10) at z=1.96 ≈ (0.490, 0.943).
        lo, hi = wilson_ci(8, 10)
        assert lo is not None and hi is not None
        assert abs(lo - 0.490) < 0.01
        assert abs(hi - 0.943) < 0.01

    def test_large_n_narrows_interval(self):
        # Same proportion (60%) at N=1000 should have much tighter
        # bounds than at N=10. Confirms the CI scales with sample size.
        lo_small, hi_small = wilson_ci(6, 10)
        lo_big, hi_big = wilson_ci(600, 1000)
        assert lo_small is not None and hi_small is not None
        assert lo_big is not None and hi_big is not None
        assert (hi_small - lo_small) > (hi_big - lo_big)
        # And the large-N CI hugs 0.6 closely.
        assert 0.55 < lo_big < 0.60
        assert 0.60 < hi_big < 0.65

    def test_custom_z_widens_interval(self):
        # 99% CI uses z=2.576 — must be wider than the default 1.96.
        lo_95, hi_95 = wilson_ci(5, 10)
        lo_99, hi_99 = wilson_ci(5, 10, z=2.576)
        assert lo_95 is not None and hi_95 is not None
        assert lo_99 is not None and hi_99 is not None
        assert lo_99 < lo_95
        assert hi_99 > hi_95


# ---------------------------------------------------------------------------
# classify_outcome_as_win — pure
# ---------------------------------------------------------------------------


class TestClassifyOutcomeAsWin:
    def test_returns_true_when_hit_target_is_true(self):
        out = SignalOutcome(
            signal_id=1,
            asset="BTC",
            signal_type="flow_anomaly",
            direction="long",
            confidence=8,
            price_at_signal=Decimal("84000"),
            hit_target=True,
        )
        assert classify_outcome_as_win(out) is True

    def test_returns_false_when_hit_target_is_false(self):
        out = SignalOutcome(
            signal_id=1,
            asset="BTC",
            signal_type="flow_anomaly",
            direction="long",
            confidence=8,
            price_at_signal=Decimal("84000"),
            hit_target=False,
        )
        assert classify_outcome_as_win(out) is False

    def test_returns_none_when_hit_target_is_none(self):
        # No-target signals are excluded from both numerator and
        # denominator — the None signal is what tells the aggregator to
        # skip the row entirely.
        out = SignalOutcome(
            signal_id=1,
            asset="BTC",
            signal_type="flow_anomaly",
            direction="long",
            confidence=8,
            price_at_signal=Decimal("84000"),
            hit_target=None,
        )
        assert classify_outcome_as_win(out) is None


# ---------------------------------------------------------------------------
# compute_calibration — integration against db_session
# ---------------------------------------------------------------------------


# `_seed` is the shared seed helper from `tests/_helpers/seed_outcomes.py`,
# aliased so the existing call-site names read naturally. Pre-PR-I.1 this
# file had its own near-identical inline helper — see the shared module
# for the rationale.
_seed = seed_signal_with_outcome


def _find_bucket(
    report: CalibrationReport, *, floor: int, ceiling: int, horizon: str
) -> CalibrationBucket:
    """Tiny lookup helper — every test reads back specific cells."""
    for b in report.buckets:
        if b.bucket_floor == floor and b.bucket_ceiling == ceiling and b.horizon == horizon:
            return b
    raise AssertionError(f"Bucket ({floor},{ceiling},{horizon}) not in report")


class TestComputeCalibrationEmpty:
    async def test_empty_db_returns_full_grid_of_zero_buckets(self, db_session):
        # Cold-start case. The full grid is always materialised so the FE
        # has a stable shape — every (bucket × horizon) cell present, all
        # n=0, hit_rate=None.
        report = await compute_calibration(
            db_session,
            ai_prompt_version="v3",
            lookback_days=90,
            min_samples=20,
            bucket_size=2,
        )
        # 5 confidence buckets × 4 horizons = 20 cells.
        assert len(report.buckets) == 5 * len(HORIZON_LABELS)
        for b in report.buckets:
            assert b.n_samples == 0
            assert b.wins == 0
            assert b.losses == 0
            assert b.hit_rate is None
            assert b.ci_low is None
            assert b.ci_high is None
        # Echoed config so the FE can render the section header.
        assert report.ai_prompt_version == "v3"
        assert report.lookback_days == 90
        assert report.min_samples == 20
        assert report.bucket_size == 2


class TestComputeCalibrationBucketing:
    async def test_aggregates_within_bucket(self, db_session):
        # Five hits, two losses, all at confidence 7 or 8 → both fall in
        # the (7, 8) bucket. Total n=7, wins=5. min_samples=1 to surface
        # the rate even at small N.
        for i in range(3):
            await _seed(db_session, confidence=7, hit_target=True, key=f"c7-h-{i}")
        for i in range(2):
            await _seed(db_session, confidence=8, hit_target=True, key=f"c8-h-{i}")
        for i in range(2):
            await _seed(db_session, confidence=7, hit_target=False, key=f"c7-l-{i}")

        report = await compute_calibration(
            db_session,
            ai_prompt_version="v3",
            lookback_days=90,
            min_samples=1,
            bucket_size=2,
        )
        cell = _find_bucket(report, floor=7, ceiling=8, horizon="swing")
        assert cell.wins == 5
        assert cell.losses == 2
        assert cell.n_samples == 7
        assert cell.hit_rate is not None
        assert abs(cell.hit_rate - 5 / 7) < 1e-9
        # Wilson CI present and reasonable for 5/7.
        assert cell.ci_low is not None and cell.ci_high is not None
        assert 0.0 < cell.ci_low < cell.hit_rate < cell.ci_high < 1.0

    async def test_separates_buckets(self, db_session):
        # 3 hits at conf 8 (bucket 7-8) and 3 hits at conf 3 (bucket 3-4)
        # — must not pool.
        for i in range(3):
            await _seed(db_session, confidence=8, hit_target=True, key=f"hi-{i}")
        for i in range(3):
            await _seed(db_session, confidence=3, hit_target=True, key=f"lo-{i}")

        report = await compute_calibration(
            db_session,
            ai_prompt_version="v3",
            lookback_days=90,
            min_samples=1,
            bucket_size=2,
        )
        hi = _find_bucket(report, floor=7, ceiling=8, horizon="swing")
        lo = _find_bucket(report, floor=3, ceiling=4, horizon="swing")
        assert hi.n_samples == 3
        assert lo.n_samples == 3
        # Buckets that got nothing stay at n=0 (stable shape).
        empty = _find_bucket(report, floor=5, ceiling=6, horizon="swing")
        assert empty.n_samples == 0

    async def test_separates_horizons(self, db_session):
        # Same confidence (8), different windows: 72h → swing, 168h →
        # position. Should land in different cells of the same bucket row.
        await _seed(db_session, confidence=8, hit_target=True, key="s", window_hours=72)
        await _seed(db_session, confidence=8, hit_target=True, key="p", window_hours=168)

        report = await compute_calibration(
            db_session,
            ai_prompt_version="v3",
            lookback_days=90,
            min_samples=1,
            bucket_size=2,
        )
        swing = _find_bucket(report, floor=7, ceiling=8, horizon="swing")
        position = _find_bucket(report, floor=7, ceiling=8, horizon="position")
        assert swing.n_samples == 1
        assert position.n_samples == 1

    async def test_legacy_window_bucketed_as_legacy(self, db_session):
        # NULL window_hours = pre-PR-B row. Goes to the "legacy" horizon
        # bucket — surfaced separately so the chart doesn't dilute v2
        # cohorts with legacy 72h-rubric data.
        await _seed(
            db_session,
            confidence=8,
            hit_target=True,
            key="legacy",
            window_hours=None,
        )

        report = await compute_calibration(
            db_session,
            ai_prompt_version="v3",
            lookback_days=90,
            min_samples=1,
            bucket_size=2,
        )
        legacy = _find_bucket(report, floor=7, ceiling=8, horizon="legacy")
        assert legacy.n_samples == 1
        assert legacy.wins == 1


class TestComputeCalibrationFilters:
    async def test_filters_by_ai_prompt_version(self, db_session):
        # v3 and v2 cohorts should be visible only when queried with
        # their own version — calibration is grouped per prompt version
        # by design (AI behaviour drift between versions).
        await _seed(
            db_session,
            confidence=8,
            hit_target=True,
            key="v3",
            ai_prompt_version="v3",
        )
        await _seed(
            db_session,
            confidence=8,
            hit_target=True,
            key="v2",
            ai_prompt_version="v2",
        )

        v3_report = await compute_calibration(
            db_session,
            ai_prompt_version="v3",
            lookback_days=90,
            min_samples=1,
            bucket_size=2,
        )
        v2_report = await compute_calibration(
            db_session,
            ai_prompt_version="v2",
            lookback_days=90,
            min_samples=1,
            bucket_size=2,
        )
        assert _find_bucket(v3_report, floor=7, ceiling=8, horizon="swing").n_samples == 1
        assert _find_bucket(v2_report, floor=7, ceiling=8, horizon="swing").n_samples == 1

    async def test_excludes_outcomes_with_null_hit_target(self, db_session):
        # No-target outcomes are excluded from both wins and losses —
        # same convention as the public hit-rate. If we had counted them
        # as losses, the calibration would be misleadingly pessimistic.
        await _seed(db_session, confidence=8, hit_target=True, key="t")
        await _seed(db_session, confidence=8, hit_target=None, key="n1")
        await _seed(db_session, confidence=8, hit_target=None, key="n2")

        report = await compute_calibration(
            db_session,
            ai_prompt_version="v3",
            lookback_days=90,
            min_samples=1,
            bucket_size=2,
        )
        cell = _find_bucket(report, floor=7, ceiling=8, horizon="swing")
        # 1 hit / 1 sample — None-target rows excluded.
        assert cell.wins == 1
        assert cell.losses == 0
        assert cell.n_samples == 1

    async def test_excludes_unevaluated_outcomes(self, db_session):
        # `evaluated_at IS NULL` rows shouldn't appear. Uses the shared
        # `evaluated_outcomes_predicate()` — also pinned in
        # `test_evaluated_outcomes_predicate_shared_with_route` below.
        await _seed(
            db_session,
            confidence=8,
            hit_target=True,
            key="unev",
            evaluated_at=None,
        )

        report = await compute_calibration(
            db_session,
            ai_prompt_version="v3",
            lookback_days=90,
            min_samples=1,
            bucket_size=2,
        )
        assert _find_bucket(report, floor=7, ceiling=8, horizon="swing").n_samples == 0

    async def test_respects_lookback_window(self, db_session):
        # Old outcome (180d ago) excluded by lookback_days=90.
        old_evaluated = datetime.now(UTC) - timedelta(days=180)
        await _seed(
            db_session,
            confidence=8,
            hit_target=True,
            key="old",
            evaluated_at=old_evaluated,
        )
        # Fresh outcome (within window).
        await _seed(db_session, confidence=8, hit_target=True, key="new")

        report = await compute_calibration(
            db_session,
            ai_prompt_version="v3",
            lookback_days=90,
            min_samples=1,
            bucket_size=2,
        )
        cell = _find_bucket(report, floor=7, ceiling=8, horizon="swing")
        # Only the fresh outcome counted.
        assert cell.n_samples == 1


class TestComputeCalibrationMinSamples:
    async def test_under_min_samples_reports_null_hit_rate(self, db_session):
        # 5 outcomes, all hits. min_samples=10 → bucket reports counts
        # but hit_rate=None because the CI would be too wide to trust.
        for i in range(5):
            await _seed(db_session, confidence=8, hit_target=True, key=f"u-{i}")

        report = await compute_calibration(
            db_session,
            ai_prompt_version="v3",
            lookback_days=90,
            min_samples=10,
            bucket_size=2,
        )
        cell = _find_bucket(report, floor=7, ceiling=8, horizon="swing")
        # Counts are still reported (UI may show "5 samples · pending").
        assert cell.n_samples == 5
        assert cell.wins == 5
        # Rate hidden until N reaches the floor.
        assert cell.hit_rate is None
        assert cell.ci_low is None
        assert cell.ci_high is None

    async def test_at_min_samples_reports_hit_rate(self, db_session):
        # Exactly at the threshold — surface the rate.
        for i in range(3):
            await _seed(db_session, confidence=8, hit_target=True, key=f"a-{i}")

        report = await compute_calibration(
            db_session,
            ai_prompt_version="v3",
            lookback_days=90,
            min_samples=3,
            bucket_size=2,
        )
        cell = _find_bucket(report, floor=7, ceiling=8, horizon="swing")
        assert cell.n_samples == 3
        assert cell.hit_rate == 1.0
        assert cell.ci_low is not None and cell.ci_high is not None


class TestComputeCalibrationNoCommit:
    async def test_no_commit_in_orchestrator(self, db_session):
        # D14 compliance — the orchestrator must NOT call session.commit().
        # The per-test transaction fixture rolls back at the end; if the
        # orchestrator had committed mid-flight, the next test's
        # `_seed` would observe its rows. We assert by inspecting
        # whether `db_session.in_transaction()` is still true after the
        # call (it is, because we never committed).
        await _seed(db_session, confidence=8, hit_target=True, key="commit-check")
        await compute_calibration(
            db_session,
            ai_prompt_version="v3",
            lookback_days=90,
            min_samples=1,
            bucket_size=2,
        )
        # The conftest opens a transaction per test; if it's still open,
        # the orchestrator didn't commit. Asserts the contract explicitly.
        assert db_session.in_transaction() is True


# ---------------------------------------------------------------------------
# DRY pin — both calibration and the route use the SHARED predicate
# ---------------------------------------------------------------------------


class TestSharedEvaluatedOutcomesPredicate:
    """`evaluated_outcomes_predicate()` is the single source of truth for
    'this outcome counts toward public stats.' Calibration uses it (verified
    by `test_excludes_unevaluated_outcomes` above); the route uses it
    (verified by existing test_track_record.py tests); per-floor stats
    use it (verified by existing test_track_record.py tests).

    These tests pin the contract directly: the predicate produces the SQL
    `evaluated_at IS NOT NULL` shape and is importable from both call
    sites without re-defining it.
    """

    def test_predicate_compiles_to_is_not_null_sql(self):
        # Compile the expression to literal SQL and assert it matches.
        # `compile()` with a literal-bind dialect would be cleaner but
        # we just check the str() form here — sufficient to catch drift.
        expr = evaluated_outcomes_predicate()
        compiled = str(expr).lower()
        assert "evaluated_at" in compiled
        assert "is not null" in compiled

    def test_predicate_importable_from_pipeline_track_record(self):
        # Pin the import path. If anyone moves it, this test surfaces
        # the rename and forces callers to follow.
        from etfpulse.pipeline.track_record import (
            evaluated_outcomes_predicate as imported,
        )

        assert imported is evaluated_outcomes_predicate

    def test_predicate_emits_same_sql_each_call(self):
        # Two invocations of the helper must compile to identical SQL —
        # the contract we actually care about. Identity-based assertions
        # ("must be a new object each call") would constrain the helper's
        # implementation without protecting any caller-visible behaviour;
        # a future refactor to a module-level constant should not break
        # this test.
        a = str(evaluated_outcomes_predicate())
        b = str(evaluated_outcomes_predicate())
        assert a == b
        assert "evaluated_at" in a.lower()
        assert "is not null" in a.lower()
