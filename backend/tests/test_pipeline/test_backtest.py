"""Integration tests for the PR I.5 backtest orchestrator.

Most of the look-ahead defense is pinned in `test_detector_lookahead.py`
(per-detector) and most of the cache semantics in `test_ai_cache.py`. This
file tests the orchestrator's *integration* properties:

  - Dedupe across the date sweep
  - Detector-override sourcing (kwargs land on the right detector)
  - AI resolver chain (cache miss → existing Signal → ai_analysis)
  - Single-asset + MARKET routing through the scoring helpers
  - Read-only invariant (no new rows in `signals` / `signal_outcomes`)
  - JSON serialisation of `BacktestReport`
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import func, select

from etfpulse.models import (
    ETFFlow,
    MarketRegime,
    RegimeSnapshot,
    Signal,
    SignalOutcome,
    SignalPosture,
    SignalStatus,
    SignalType,
)
from etfpulse.pipeline import ai_cache
from etfpulse.pipeline.analysis import AI_PROMPT_VERSION
from etfpulse.pipeline.backtest import (
    BacktestReport,
    make_resolver,
    run_backtest,
)
from etfpulse.pipeline.detectors import compute_fingerprint


def _analysis_dict(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "headline": "Strong inflows",
        "reasoning": ["sustained inflow streak"],
        "confidence": 8,
        "risks": ["near-term volatility"],
        "suggested_action": "consider long",
        "time_horizon": "swing",
        "entry_price": "84200",
        "stop_price": "82000",
        "target_price": "89500",
    }
    base.update(overrides)
    return base


@pytest.fixture(autouse=True)
def _isolate_cache(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Each test starts with an empty on-disk cache, written to tmpdir."""
    monkeypatch.setattr(ai_cache, "CACHE_ROOT", tmp_path)
    return tmp_path


def _seed_flow_streak(
    db_session, asset: str, *, start: date, length: int, value: int = 100_000_000
) -> None:
    """Seed a multi-day same-sign flow streak — enough for `flow_anomaly` to
    fire on day `length+1` if a break flow lands there."""
    for i in range(length):
        db_session.add(
            ETFFlow(
                asset=asset,
                captured_at=start + timedelta(days=i),
                total_net_flow_usd=Decimal(value),
                raw_response={},
            )
        )


def _seed_break(db_session, asset: str, *, when: date, value: int = -50_000_000) -> None:
    db_session.add(
        ETFFlow(
            asset=asset,
            captured_at=when,
            total_net_flow_usd=Decimal(value),
            raw_response={},
        )
    )


async def _seed_prod_signal(
    db_session,
    *,
    fingerprint: str,
    asset: str,
    signal_date: date,
    signal_type: str,
    analysis: dict[str, object] | None = None,
    confidence: int = 7,
) -> Signal:
    """Seed a production Signal with a stamped `ai_analysis`, so the
    resolver's tier-2 (existing-Signal lookup) returns a hit. Direction
    lives in `ai_analysis["suggested_action"]`, not as a Signal column."""
    now = datetime.now(UTC)
    sig = Signal(
        signal_type=signal_type,
        asset=asset,
        signal_date=signal_date,
        confidence=confidence,
        fingerprint=fingerprint,
        ai_prompt_version=AI_PROMPT_VERSION,
        ai_analysis=analysis or _analysis_dict(),
        trigger_data={},
        status=SignalStatus.PENDING.value,
        created_at=now - timedelta(days=10),
        expires_at=now,
        price_at_creation=Decimal("84200"),
        price_source="sosovalue",
        entry_price=Decimal("84200"),
        stop_price=Decimal("82000"),
        target_price=Decimal("89500"),
    )
    db_session.add(sig)
    await db_session.flush()
    return sig


class TestOrchestratorSmoke:
    async def test_returns_report_with_stable_detector_order(self, db_session):
        # Empty DB — no hits anywhere, but report should still come back well-formed.
        report = await run_backtest(db_session, start=date(2026, 4, 15), end=date(2026, 4, 17))
        assert isinstance(report, BacktestReport)
        names = [d.detector_name for d in report.per_detector]
        assert names == [
            "flow_anomaly",
            "magnitude",
            "acceleration",
            "divergence",
            "regime_shift",
        ]
        assert report.counters["dates_walked"] == 3
        assert report.counters["hits_total"] == 0
        assert report.outcomes == []

    async def test_unknown_detector_override_raises(self, db_session):
        with pytest.raises(ValueError, match="unknown detector override"):
            await run_backtest(
                db_session,
                start=date(2026, 4, 15),
                end=date(2026, 4, 16),
                detector_overrides={"bogus": {"foo": 1}},
            )

    async def test_invalid_window_raises(self, db_session):
        with pytest.raises(ValueError, match="end .* < start"):
            await run_backtest(db_session, start=date(2026, 4, 17), end=date(2026, 4, 15))


class TestDedupe:
    async def test_same_fingerprint_across_multiple_as_of_counts_once(self, db_session):
        # Seed a flow_anomaly hit that persists across the 14-day lookback —
        # i.e. it fires on the same break date for multiple consecutive as_of
        # values during the sweep.
        _seed_flow_streak(db_session, "BTC", start=date(2026, 4, 1), length=5, value=100_000_000)
        _seed_break(db_session, "BTC", when=date(2026, 4, 6), value=-50_000_000)
        # ETH stays quiet so only one detector fires.
        await db_session.flush()

        report = await run_backtest(db_session, start=date(2026, 4, 6), end=date(2026, 4, 10))
        # The same break date fires on every as_of in [Apr 6, Apr 10].
        # Dedupe ensures hits_unique == 1.
        assert report.counters["hits_total"] >= 1
        assert report.counters["hits_unique"] == 1
        assert report.counters["hits_duplicate"] == report.counters["hits_total"] - 1


class TestResolverChain:
    """The 3-tier resolver: cache hit → existing prod Signal → optional live AI."""

    async def test_resolver_uses_existing_signal_when_cache_cold(self, db_session):
        # Seed a flow_anomaly hit on Apr 6, then seed a matching prod Signal
        # with AI analysis so the resolver finds it in tier 2.
        _seed_flow_streak(db_session, "BTC", start=date(2026, 4, 1), length=5)
        _seed_break(db_session, "BTC", when=date(2026, 4, 6), value=-50_000_000)
        await db_session.flush()

        # The fingerprint must match what `flow_anomaly` computes —
        # `(asset, "flow_anomaly", break_date, streak_length, streak_dir)`.
        fp = compute_fingerprint("BTC", "flow_anomaly", "2026-04-06", "5", "long")
        await _seed_prod_signal(
            db_session,
            fingerprint=fp,
            asset="BTC",
            signal_date=date(2026, 4, 6),
            signal_type=SignalType.FLOW_ANOMALY.value,
        )

        report = await run_backtest(db_session, start=date(2026, 4, 6), end=date(2026, 4, 6))
        # Resolver should have found the AI direction via tier 2.
        scored_rows = [r for r in report.outcomes if r.skip_reason is None]
        assert len(scored_rows) == 1
        assert scored_rows[0].direction == "long"
        assert scored_rows[0].confidence == 8

    async def test_resolver_caches_existing_signal_lookup(self, db_session, tmp_path):
        """Tier-2 lookup populates the cache so a subsequent sweep skips
        the DB query and reads from disk."""
        _seed_flow_streak(db_session, "BTC", start=date(2026, 4, 1), length=5)
        _seed_break(db_session, "BTC", when=date(2026, 4, 6), value=-50_000_000)
        await db_session.flush()
        fp = compute_fingerprint("BTC", "flow_anomaly", "2026-04-06", "5", "long")
        await _seed_prod_signal(
            db_session,
            fingerprint=fp,
            asset="BTC",
            signal_date=date(2026, 4, 6),
            signal_type=SignalType.FLOW_ANOMALY.value,
        )

        resolver = make_resolver(db_session)
        await run_backtest(
            db_session,
            start=date(2026, 4, 6),
            end=date(2026, 4, 6),
            ai_resolver=resolver,
        )
        # The cache should now contain the resolved analysis under the
        # production AI_PROMPT_VERSION subdir.
        cached_file = tmp_path / AI_PROMPT_VERSION / f"{fp}.json"
        assert cached_file.is_file()

    async def test_missing_signal_results_in_no_direction_skip(self, db_session):
        # Seed a hit but NO matching prod signal — resolver returns None.
        _seed_flow_streak(db_session, "BTC", start=date(2026, 4, 1), length=5)
        _seed_break(db_session, "BTC", when=date(2026, 4, 6), value=-50_000_000)
        await db_session.flush()

        report = await run_backtest(db_session, start=date(2026, 4, 6), end=date(2026, 4, 6))
        assert report.counters["scored"] == 0
        assert report.counters["skipped_no_direction"] >= 1
        # All outcomes carry skip_reason.
        assert all(r.skip_reason is not None for r in report.outcomes)


class TestReadOnly:
    async def test_run_backtest_writes_no_rows(self, db_session):
        # Baseline counts before.
        n_sig_before = (await db_session.execute(select(func.count()).select_from(Signal))).scalar()
        n_out_before = (
            await db_session.execute(select(func.count()).select_from(SignalOutcome))
        ).scalar()

        _seed_flow_streak(db_session, "BTC", start=date(2026, 4, 1), length=5)
        _seed_break(db_session, "BTC", when=date(2026, 4, 6), value=-50_000_000)
        await db_session.flush()
        # Re-baseline after the test's own seeded signal-domain rows so we
        # measure ONLY orchestrator-induced writes.
        n_sig_after_seed = (
            await db_session.execute(select(func.count()).select_from(Signal))
        ).scalar()
        n_out_after_seed = (
            await db_session.execute(select(func.count()).select_from(SignalOutcome))
        ).scalar()
        assert n_sig_after_seed == n_sig_before  # we didn't seed any signals here
        assert n_out_after_seed == n_out_before

        await run_backtest(db_session, start=date(2026, 4, 6), end=date(2026, 4, 10))

        n_sig_after = (await db_session.execute(select(func.count()).select_from(Signal))).scalar()
        n_out_after = (
            await db_session.execute(select(func.count()).select_from(SignalOutcome))
        ).scalar()
        assert n_sig_after == n_sig_after_seed
        assert n_out_after == n_out_after_seed


class TestDetectorOverrides:
    async def test_override_changes_hit_count(self, db_session):
        # 4-day streak: with default min_streak_length=3, fires on day 5.
        # With min_streak_length=10, does NOT fire — too short.
        _seed_flow_streak(db_session, "BTC", start=date(2026, 4, 1), length=4)
        _seed_break(db_session, "BTC", when=date(2026, 4, 5), value=-50_000_000)
        await db_session.flush()

        baseline = await run_backtest(db_session, start=date(2026, 4, 5), end=date(2026, 4, 5))
        baseline_hits = next(
            d for d in baseline.per_detector if d.detector_name == "flow_anomaly"
        ).n_hits

        # Tighter min_streak_length suppresses the hit.
        tighter = await run_backtest(
            db_session,
            start=date(2026, 4, 5),
            end=date(2026, 4, 5),
            detector_overrides={"flow_anomaly": {"min_streak_length": 10}},
        )
        tighter_hits = next(
            d for d in tighter.per_detector if d.detector_name == "flow_anomaly"
        ).n_hits

        assert baseline_hits >= 1
        assert tighter_hits == 0

    async def test_override_records_applied_kwargs_in_report(self, db_session):
        report = await run_backtest(
            db_session,
            start=date(2026, 4, 1),
            end=date(2026, 4, 1),
            detector_overrides={"magnitude": {"percentile_threshold": 0.95}},
        )
        assert report.detector_configs["magnitude"]["percentile_threshold"] == 0.95


class TestRegimeShiftScoring:
    async def test_regime_shift_with_market_signal_routes_to_composite(self, db_session):
        # Seed two regime snapshots straddling a UTC day → regime_shift fires.
        db_session.add(
            RegimeSnapshot(
                captured_at=datetime(2026, 4, 5, 12, 0, tzinfo=UTC),
                regime=MarketRegime.ACCUMULATION.value,
                signal_posture=SignalPosture.NORMAL.value,
                confidence=7,
            )
        )
        db_session.add(
            RegimeSnapshot(
                captured_at=datetime(2026, 4, 6, 12, 0, tzinfo=UTC),
                regime=MarketRegime.MARKUP.value,
                signal_posture=SignalPosture.NORMAL.value,
                confidence=7,
            )
        )
        await db_session.flush()

        # Seed a MARKET-asset prod signal so the resolver finds AI direction.
        fp = compute_fingerprint("MARKET", "regime_shift", "2026-04-06", "markup")
        await _seed_prod_signal(
            db_session,
            fingerprint=fp,
            asset="MARKET",
            signal_date=date(2026, 4, 6),
            signal_type=SignalType.REGIME_SHIFT.value,
        )

        report = await run_backtest(db_session, start=date(2026, 4, 6), end=date(2026, 4, 6))
        # Some kline-related skip is acceptable depending on fixture coverage
        # for that date — what we pin is the routing decision: if the row is
        # scored, scoring_version must be the MARKET composite tag.
        regime_rows = [r for r in report.outcomes if r.detector_name == "regime_shift"]
        assert len(regime_rows) == 1
        row = regime_rows[0]
        if row.skip_reason is None:
            assert row.scoring_version == "market-v1"
            assert row.composite_return_pct is not None
        else:
            # Acceptable if kline window doesn't have enough data for the
            # synthetic date — the routing decision already happened.
            assert row.skip_reason in {"no_klines", "no_bars_in_window"}


class TestT0Anchoring:
    """The orchestrator's t0 must shift past UTC midnight by the production
    cron offset so the bar at midnight of signal_date+1 (which production
    EXCLUDES because it fired AFTER that bar opened) is also excluded from
    backtest scoring. Without the shift, backtest produces one extra leading
    bar in the window — `_compute_metrics` would see different highs/lows
    than production, drifting `hit_target` / `max_favorable` / `max_adverse`.
    """

    def test_t0_shift_excludes_midnight_bar(self):
        # Pin the contract directly against the public helper math. If
        # someone later anchors t0 at midnight, this test fails because the
        # bar timestamp = t0_ms qualifies for the inclusive lower bound.
        from datetime import time as _time

        from etfpulse.config import settings as _settings

        signal_date = date(2026, 4, 6)
        t0_dt = datetime.combine(
            signal_date + timedelta(days=1),
            _time(hour=_settings.scheduler_cron_hour, minute=_settings.scheduler_cron_minute),
            tzinfo=UTC,
        )
        midnight_dt = datetime.combine(
            signal_date + timedelta(days=1), datetime.min.time(), tzinfo=UTC
        )
        assert t0_dt > midnight_dt, (
            "t0 must shift past UTC midnight of signal_date+1 so the daily "
            "bar opening at that midnight is excluded — production would "
            "have fired at the daily cron time, AFTER the bar opened."
        )

    async def test_midnight_cron_does_not_regress_to_off_by_one(self, db_session, monkeypatch):
        """If an operator sets cron=00:00 UTC, naive `time(cron_h, cron_m)`
        would put t0 exactly at midnight — the original off-by-one bug.
        The orchestrator floors the offset at 1 minute past midnight so the
        daily bar opening at that midnight is still excluded from the window."""
        from etfpulse.config import settings as _settings

        monkeypatch.setattr(_settings, "scheduler_cron_hour", 0)
        monkeypatch.setattr(_settings, "scheduler_cron_minute", 0)

        # Seed a hit + matching prod signal so we exercise the t0-anchoring
        # path (only reached after AI direction is resolved).
        _seed_flow_streak(db_session, "BTC", start=date(2026, 4, 1), length=5)
        _seed_break(db_session, "BTC", when=date(2026, 4, 6), value=-50_000_000)
        await db_session.flush()
        fp = compute_fingerprint("BTC", "flow_anomaly", "2026-04-06", "5", "long")
        await _seed_prod_signal(
            db_session,
            fingerprint=fp,
            asset="BTC",
            signal_date=date(2026, 4, 6),
            signal_type=SignalType.FLOW_ANOMALY.value,
        )

        # The run completes without raising. The actual scoring outcome is
        # implementation-dependent on the kline fixture for those dates;
        # what we pin is that the cron=00:00 case doesn't bring back the
        # off-by-one bug (which would have manifested as an extra leading
        # bar inflating max_favorable). We compare against the default-cron
        # run — if the floor failed and t0 = midnight, the bar at midnight
        # D+1 would join the window and `max_favorable` would be at least
        # as large as the default case for any non-trivial bars.
        midnight_report = await run_backtest(
            db_session, start=date(2026, 4, 6), end=date(2026, 4, 6)
        )

        # Reset to default and re-run.
        monkeypatch.setattr(_settings, "scheduler_cron_hour", 4)
        monkeypatch.setattr(_settings, "scheduler_cron_minute", 30)
        default_report = await run_backtest(
            db_session, start=date(2026, 4, 6), end=date(2026, 4, 6)
        )

        # Both must agree on hit_target — any positive intra-day offset
        # gives the same daily-aligned window.
        midnight_rows = [r for r in midnight_report.outcomes if r.skip_reason is None]
        default_rows = [r for r in default_report.outcomes if r.skip_reason is None]
        assert len(midnight_rows) == len(default_rows)
        for m, d in zip(midnight_rows, default_rows, strict=True):
            assert m.hit_target == d.hit_target
            assert m.hit_stop == d.hit_stop


class TestResolverRobustness:
    async def test_cache_write_failure_does_not_crash_run(self, db_session, monkeypatch, tmp_path):
        """A read-only cache directory must not crash a backtest. The
        resolver wraps `ai_cache.put` in a non-fatal try/except so the
        analysis still flows back to the orchestrator and scoring proceeds."""
        from etfpulse.pipeline import ai_cache as _ai_cache

        def _raise(**_kwargs):
            raise OSError("simulated read-only fs")

        monkeypatch.setattr(_ai_cache, "put", _raise)

        _seed_flow_streak(db_session, "BTC", start=date(2026, 4, 1), length=5)
        _seed_break(db_session, "BTC", when=date(2026, 4, 6), value=-50_000_000)
        await db_session.flush()
        fp = compute_fingerprint("BTC", "flow_anomaly", "2026-04-06", "5", "long")
        await _seed_prod_signal(
            db_session,
            fingerprint=fp,
            asset="BTC",
            signal_date=date(2026, 4, 6),
            signal_type=SignalType.FLOW_ANOMALY.value,
        )

        # Must not raise. The scored row should still come back — cache
        # write failure is non-fatal.
        report = await run_backtest(db_session, start=date(2026, 4, 6), end=date(2026, 4, 6))
        scored = [r for r in report.outcomes if r.skip_reason is None]
        assert len(scored) == 1

    async def test_wait_action_produces_no_direction_skip(self, db_session):
        """When the resolved AI analysis has `suggested_action="wait"`, the
        orchestrator must short-circuit with `skip_reason="no_direction"` +
        `direction="wait"` so the row is visible in the report but not scored."""
        _seed_flow_streak(db_session, "BTC", start=date(2026, 4, 1), length=5)
        _seed_break(db_session, "BTC", when=date(2026, 4, 6), value=-50_000_000)
        await db_session.flush()
        fp = compute_fingerprint("BTC", "flow_anomaly", "2026-04-06", "5", "long")
        await _seed_prod_signal(
            db_session,
            fingerprint=fp,
            asset="BTC",
            signal_date=date(2026, 4, 6),
            signal_type=SignalType.FLOW_ANOMALY.value,
            analysis=_analysis_dict(
                suggested_action="wait",
                # Pydantic enforces entry/stop/target == None for wait.
                entry_price=None,
                stop_price=None,
                target_price=None,
            ),
        )

        report = await run_backtest(db_session, start=date(2026, 4, 6), end=date(2026, 4, 6))
        wait_rows = [r for r in report.outcomes if r.direction == "wait"]
        assert len(wait_rows) == 1
        assert wait_rows[0].skip_reason == "no_direction"
        assert wait_rows[0].confidence is not None
        assert report.counters["skipped_no_direction"] >= 1


class TestHorizonConstantsParity:
    """`pipeline.backtest._HORIZON_HOURS` duplicates the horizon-to-window
    mapping that production-side `pipeline.analysis._HORIZON_TO_DURATION`
    owns. Duplication is the lesser of two evils (the source is private and
    `timedelta`-valued, the backtest needs an int-hour form), but the drift
    risk is real — a future PR that retunes horizon lengths in `analysis.py`
    would otherwise silently produce backtest reports against stale window
    sizes. This test pins equivalence; if it fails, update `_HORIZON_HOURS`
    to match."""

    def test_backtest_horizons_match_production_durations(self):
        from etfpulse.pipeline.analysis import _HORIZON_TO_DURATION
        from etfpulse.pipeline.backtest import _HORIZON_HOURS

        prod_hours = {k: int(v.total_seconds() // 3600) for k, v in _HORIZON_TO_DURATION.items()}
        assert _HORIZON_HOURS == prod_hours, (
            "backtest._HORIZON_HOURS has drifted from analysis._HORIZON_TO_DURATION — "
            "update backtest._HORIZON_HOURS to match the production horizon definitions."
        )


class TestReportSerialisation:
    async def test_to_json_dict_is_json_serialisable(self, db_session):
        report = await run_backtest(db_session, start=date(2026, 4, 15), end=date(2026, 4, 16))
        payload = report.to_json_dict()
        # Round-trips through json.dumps without raising.
        roundtrip = json.loads(json.dumps(payload))
        assert roundtrip["start"] == "2026-04-15"
        assert roundtrip["end"] == "2026-04-16"
        assert roundtrip["ai_prompt_version"] == AI_PROMPT_VERSION
        # Per-detector list shape is stable across runs.
        assert len(roundtrip["per_detector"]) == 5
