"""Outcome evaluator — `_compute_metrics` (pure) + `evaluate_pending_outcomes`
(integration against db_session).

The pure-function tests live first and dominate the file — that's where the
hit/stop / max-favorable/adverse logic lives, and they're cheap to enumerate
without a DB. The integration tests just verify the orchestrator skips +
inserts correctly; the metric correctness is already pinned upstream.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import select

from etfpulse.models import Signal, SignalDirection, SignalOutcome
from etfpulse.pipeline.detectors import compute_fingerprint
from etfpulse.pipeline.prices import PriceBar
from etfpulse.pipeline.track_record import (
    _compute_metrics,
    compute_hit_rate_pct,
    evaluate_pending_outcomes,
    get_stats_by_confidence_floor,
)

# ---------------------------------------------------------------------------
# compute_hit_rate_pct — canonical helper, single source of truth for the
# `(hits / targeted) * 100` math used by API routes, bot handler, and
# TrackRecordStat. Pinned here so a regression in any of those callsites
# fails this test rather than a downstream integration test where the
# error message is "frontend renders 0% instead of None".
# ---------------------------------------------------------------------------


class TestComputeHitRatePct:
    def test_empty_cohort_returns_none(self):
        # The None-vs-zero distinction is load-bearing — see helper docstring.
        assert compute_hit_rate_pct(0, 0) is None

    def test_zero_hits_returns_zero_not_none(self):
        # Zero hits with a non-empty cohort IS zero percent (not None).
        assert compute_hit_rate_pct(0, 5) == 0.0

    def test_perfect_hit_rate(self):
        assert compute_hit_rate_pct(7, 7) == 100.0

    def test_rounds_to_two_decimal_places(self):
        # 5/6 = 0.83333... → 83.33%
        assert compute_hit_rate_pct(5, 6) == 83.33

    def test_returns_float_type(self):
        # Float (not int) is the canonical type — callers wanting an int
        # round at render time. Pinning here so the contract doesn't drift.
        result = compute_hit_rate_pct(3, 10)
        assert isinstance(result, float)
        assert result == 30.0


# ---------------------------------------------------------------------------
# Pure-function helpers
# ---------------------------------------------------------------------------


def _bar(*, open_iso: str, o: str, h: str, low: str, c: str) -> PriceBar:
    """Build a synthetic daily bar from human-readable inputs.

    `low` (not `l`) avoids the ambiguous-l ruff lint. Decimals are passed as
    strings so callers don't accidentally introduce float artifacts (e.g.
    `0.1 + 0.2`)."""
    ts = int(datetime.fromisoformat(open_iso).replace(tzinfo=UTC).timestamp() * 1000)
    return PriceBar(
        timestamp_ms=ts,
        open=Decimal(o),
        high=Decimal(h),
        low=Decimal(low),
        close=Decimal(c),
    )


# t0 fixed for the pure tests so the bar timestamps below stay readable.
# Signal fires at 04:30 UTC on 2026-04-22 → bar covering t0+24h is the
# 2026-04-23 daily bar; bar covering t0+72h is the 2026-04-25 daily bar.
_T0 = datetime(2026, 4, 22, 4, 30, tzinfo=UTC)
_T0_MS = int(_T0.timestamp() * 1000)


def _four_day_bars(prices: list[tuple[str, str, str, str]]) -> list[PriceBar]:
    """Helper for the common case: bars for 2026-04-22 / 04-23 / 04-24 /
    04-25 (the four daily bars that the [t0, t0+72h] window can touch —
    Day 22 is pre-signal, Days 23-25 are in-window).

    Each `prices` item is `(open, high, low, close)` strings. The function
    stamps them onto the four consecutive UTC daily opens. Length must be 4."""
    assert len(prices) == 4
    return [
        _bar(
            open_iso=f"2026-04-{day:02d}T00:00:00",
            o=o,
            h=h,
            low=low,
            c=c,
        )
        for day, (o, h, low, c) in zip([22, 23, 24, 25], prices, strict=True)
    ]


# ---------------------------------------------------------------------------
# _compute_metrics — pure
# ---------------------------------------------------------------------------


class TestComputeMetricsLong:
    def test_long_hits_target_high_above_target(self):
        # Day 23 high pokes above target — should mark hit_target=True.
        bars = _four_day_bars(
            [
                ("84000", "84500", "83800", "84200"),  # Day 22
                ("84200", "85100", "84000", "85000"),  # Day 23 — high 85100 > target 85000
                ("85000", "85200", "84800", "85100"),  # Day 24
                ("85100", "85400", "85000", "85300"),  # Day 25
            ]
        )
        m = _compute_metrics(
            direction=SignalDirection.LONG,
            entry=Decimal("84200"),
            stop=Decimal("83000"),
            target=Decimal("85000"),
            t0_ms=_T0_MS,
            window_hours=72,
            bars=bars,
        )
        assert m is not None
        assert m.hit_target is True
        assert m.hit_stop is False

    def test_long_hits_stop_low_below_stop(self):
        bars = _four_day_bars(
            [
                ("84000", "84200", "83100", "83500"),  # low 83100 ≤ stop 83200
                ("83500", "83800", "83000", "83600"),
                ("83600", "84000", "83500", "83900"),
                ("83900", "84100", "83700", "84000"),
            ]
        )
        m = _compute_metrics(
            direction=SignalDirection.LONG,
            entry=Decimal("84000"),
            stop=Decimal("83200"),
            target=Decimal("85000"),
            t0_ms=_T0_MS,
            window_hours=72,
            bars=bars,
        )
        assert m is not None
        assert m.hit_target is False
        assert m.hit_stop is True

    def test_long_neither_hits_drift(self):
        bars = _four_day_bars(
            [
                ("84200", "84300", "84100", "84250"),
                ("84250", "84400", "84150", "84300"),
                ("84300", "84500", "84200", "84400"),
                ("84400", "84600", "84300", "84450"),
            ]
        )
        m = _compute_metrics(
            direction=SignalDirection.LONG,
            entry=Decimal("84200"),
            stop=Decimal("83000"),
            target=Decimal("86000"),
            t0_ms=_T0_MS,
            window_hours=72,
            bars=bars,
        )
        assert m is not None
        assert m.hit_target is False
        assert m.hit_stop is False

    def test_long_max_favorable_and_adverse_are_unsigned_fractions(self):
        # Day 22 bar opens at 00:00 — BEFORE t0 (04:30 Day 22) — so its
        # high/low don't count. Only Day 23/24/25 are in_window.
        # In-window highest: 85000 (Day 24); lowest: 83800 (Day 23).
        bars = _four_day_bars(
            [
                # Day 22 — high/low here are pre-signal, must NOT influence
                # max_favorable. Putting an extreme high to prove the filter.
                ("84200", "99999", "10000", "84300"),
                ("84300", "84600", "83800", "84400"),  # Day 23
                ("84400", "85000", "84200", "84800"),  # Day 24 — high 85000
                ("84800", "84900", "84500", "84700"),  # Day 25
            ]
        )
        m = _compute_metrics(
            direction=SignalDirection.LONG,
            entry=Decimal("84200"),
            stop=None,
            target=None,
            t0_ms=_T0_MS,
            window_hours=72,
            bars=bars,
        )
        assert m is not None
        # (85000 - 84200) / 84200
        assert m.max_favorable is not None
        assert abs(m.max_favorable - Decimal("800") / Decimal("84200")) < Decimal("1e-12")
        # (84200 - 83800) / 84200
        assert m.max_adverse is not None
        assert abs(m.max_adverse - Decimal("400") / Decimal("84200")) < Decimal("1e-12")

    def test_long_max_favorable_clamped_to_zero_when_only_drawdown(self):
        # Price never trades above entry — favorable should be 0, not negative.
        bars = _four_day_bars(
            [
                ("84200", "84200", "83500", "83800"),
                ("83800", "84000", "83400", "83700"),
                ("83700", "83900", "83300", "83600"),
                ("83600", "83800", "83200", "83500"),
            ]
        )
        m = _compute_metrics(
            direction=SignalDirection.LONG,
            entry=Decimal("84200"),
            stop=None,
            target=None,
            t0_ms=_T0_MS,
            window_hours=72,
            bars=bars,
        )
        assert m is not None
        assert m.max_favorable == Decimal(0)
        # adverse: (84200 - 83200) / 84200
        assert m.max_adverse is not None
        assert m.max_adverse > Decimal(0)


class TestComputeMetricsShort:
    def test_short_hits_target_low_below_target(self):
        # Day 23 low pokes below target — short profits.
        bars = _four_day_bars(
            [
                ("84200", "84300", "83900", "84000"),
                ("84000", "84100", "82900", "83100"),  # low 82900 ≤ target 83000
                ("83100", "83400", "83000", "83200"),
                ("83200", "83500", "83100", "83300"),
            ]
        )
        m = _compute_metrics(
            direction=SignalDirection.SHORT,
            entry=Decimal("84200"),
            stop=Decimal("85500"),
            target=Decimal("83000"),
            t0_ms=_T0_MS,
            window_hours=72,
            bars=bars,
        )
        assert m is not None
        assert m.hit_target is True
        assert m.hit_stop is False

    def test_short_hits_stop_high_above_stop(self):
        bars = _four_day_bars(
            [
                ("84200", "85700", "84000", "85500"),  # high 85700 ≥ stop 85500
                ("85500", "85900", "85000", "85800"),
                ("85800", "86000", "85700", "85900"),
                ("85900", "86100", "85800", "86000"),
            ]
        )
        m = _compute_metrics(
            direction=SignalDirection.SHORT,
            entry=Decimal("84200"),
            stop=Decimal("85500"),
            target=Decimal("83000"),
            t0_ms=_T0_MS,
            window_hours=72,
            bars=bars,
        )
        assert m is not None
        assert m.hit_target is False
        assert m.hit_stop is True


class TestComputeMetricsLevels:
    def test_no_target_hit_target_is_none_not_false(self):
        """When the AI declined to set a target, hit_target is unknowable —
        return None, not False. False would skew the dashboard hit-rate."""
        bars = _four_day_bars(
            [("84200", "84500", "83900", "84300")] * 4,
        )
        m = _compute_metrics(
            direction=SignalDirection.LONG,
            entry=Decimal("84200"),
            stop=Decimal("83000"),
            target=None,
            t0_ms=_T0_MS,
            window_hours=72,
            bars=bars,
        )
        assert m is not None
        assert m.hit_target is None
        assert m.hit_stop is False

    def test_no_stop_hit_stop_is_none_not_false(self):
        bars = _four_day_bars(
            [("84200", "84500", "83900", "84300")] * 4,
        )
        m = _compute_metrics(
            direction=SignalDirection.LONG,
            entry=Decimal("84200"),
            stop=None,
            target=Decimal("85000"),
            t0_ms=_T0_MS,
            window_hours=72,
            bars=bars,
        )
        assert m is not None
        assert m.hit_target is False
        assert m.hit_stop is None


class TestComputeMetricsPriceClose:
    def test_price_after_24h_picks_bar_containing_t0_plus_24h(self):
        # t0 = 04:30 Day 22 → t0+24h = 04:30 Day 23 → bar covering Day 23
        # opens at 00:00 Day 23 → close should be Day 23's close.
        bars = _four_day_bars(
            [
                ("84200", "84500", "83900", "84100"),  # Day 22 close 84100
                ("84100", "84800", "83800", "84600"),  # Day 23 close 84600 ← want
                ("84600", "85000", "84300", "84800"),  # Day 24 close 84800
                ("84800", "85200", "84500", "85000"),  # Day 25 close 85000
            ]
        )
        m = _compute_metrics(
            direction=SignalDirection.LONG,
            entry=Decimal("84200"),
            stop=None,
            target=None,
            t0_ms=_T0_MS,
            window_hours=72,
            bars=bars,
        )
        assert m is not None
        assert m.price_after_24h == Decimal("84600")
        # t0+72h = 04:30 Day 25 → bar covering Day 25.
        assert m.price_after_72h == Decimal("85000")


class TestComputeMetricsEmpty:
    def test_no_bars_in_window_returns_none(self):
        """All bars before t0 — none in [t0, t0+72h]. Caller treats this
        as "no data" and skips the outcome row entirely."""
        # All bars are from BEFORE 2026-04-22 — none in window.
        bars = [
            _bar(open_iso="2026-04-15T00:00:00", o="80000", h="80500", low="79500", c="80200"),
            _bar(open_iso="2026-04-16T00:00:00", o="80200", h="80800", low="79800", c="80500"),
        ]
        m = _compute_metrics(
            direction=SignalDirection.LONG,
            entry=Decimal("84200"),
            stop=Decimal("83000"),
            target=Decimal("85000"),
            t0_ms=_T0_MS,
            window_hours=72,
            bars=bars,
        )
        assert m is None


# ---------------------------------------------------------------------------
# evaluate_pending_outcomes — integration against db_session
# ---------------------------------------------------------------------------


def _make_signal(
    *,
    created_at: datetime,
    expires_at: datetime | None = None,
    horizon_hours: int = 72,
    suggested_action: str = "consider long",
    confidence: int | None = 7,
    price_at_creation: Decimal | None = Decimal("84200"),
    price_source: str | None = "binance",
    entry_price: Decimal | None = Decimal("84200"),
    stop_price: Decimal | None = Decimal("83000"),
    target_price: Decimal | None = Decimal("85000"),
    fingerprint_extra: str = "x",
    asset: str = "BTC",
    signal_type: str = "flow_anomaly",
) -> Signal:
    """Build an in-memory Signal for evaluator tests.

    PR B (#60) — the evaluator candidate filter is now
    `expires_at <= now AND expires_at IS NOT NULL`. The helper sets
    `expires_at = created_at + horizon_hours` by default (72h swing) so
    existing tests that pre-date the v2 rubric keep working without
    per-test edits. Horizon-specific tests pass `horizon_hours=168` for
    position or override `expires_at` directly.

    `created_at` is set on the Signal object too — Signal's column has
    `server_default=func.now()`, but assigning explicitly sends our value
    on INSERT instead of letting the server clock fire."""
    signal = Signal(
        signal_type=signal_type,
        asset=asset,
        trigger_data={"streak_days": 4},
        ai_analysis={"suggested_action": suggested_action, "headline": "x"}
        if suggested_action
        else None,
        confidence=confidence,
        status="alerted",
        price_at_creation=price_at_creation,
        price_source=price_source,
        ai_prompt_version="v3",
        fingerprint=compute_fingerprint("track-record-test", fingerprint_extra),
        signal_date=created_at.date(),
        entry_price=entry_price,
        stop_price=stop_price,
        target_price=target_price,
    )
    signal.created_at = created_at
    signal.expires_at = (
        expires_at if expires_at is not None else created_at + timedelta(hours=horizon_hours)
    )
    return signal


@pytest.fixture
def stub_klines(monkeypatch):
    """Replace the live kline fetcher with a callable that records its call
    args and returns a hand-built bar list. Tests can override the returned
    bars per-test by calling `set_bars(...)`."""
    state: dict = {"calls": [], "bars": [], "raise_none": False}

    async def _fake(asset, source, *, start_time_ms=None, end_time_ms=None, limit=100):
        state["calls"].append(
            {"asset": asset, "source": source, "start_ms": start_time_ms, "end_ms": end_time_ms}
        )
        if state["raise_none"]:
            return None
        return list(state["bars"])

    monkeypatch.setattr("etfpulse.pipeline.track_record.get_daily_klines_from_source", _fake)
    return state


class TestEvaluatePendingOutcomes:
    async def test_inserts_outcome_for_aged_signal(self, db_session, stub_klines):
        # Signal from 80h ago — passes the 72h cutoff.
        t0 = datetime.now(UTC) - timedelta(hours=80)
        signal = _make_signal(created_at=t0, fingerprint_extra="aged")
        signal.created_at = t0  # override server_default
        db_session.add(signal)
        await db_session.flush()

        # Bars covering the 72h post-signal window — all show price drift up,
        # high pokes through target.
        t0_ms = int(t0.timestamp() * 1000)
        stub_klines["bars"] = [
            PriceBar(
                timestamp_ms=t0_ms + i * 24 * 3600 * 1000,
                open=Decimal("84200") + Decimal(i * 100),
                high=Decimal("85100") + Decimal(i * 100),
                low=Decimal("83800") + Decimal(i * 100),
                close=Decimal("84500") + Decimal(i * 100),
            )
            for i in range(3)
        ]

        summary = await evaluate_pending_outcomes(db_session)
        assert summary["evaluated"] == 1
        assert summary["candidates"] == 1

        # One outcome row exists.
        outcomes = (await db_session.execute(select(SignalOutcome))).scalars().all()
        assert len(outcomes) == 1
        outcome = outcomes[0]
        assert outcome.signal_id == signal.id
        assert outcome.direction == "long"
        assert outcome.hit_target is True  # high 85100 ≥ target 85000
        assert outcome.hit_stop is False
        assert outcome.evaluated_at is not None

        # Klines fetched from the source pinned on the signal.
        assert stub_klines["calls"][0]["source"] == "binance"
        assert stub_klines["calls"][0]["asset"] == "BTC"

    async def test_skips_signal_under_72h_old(self, db_session, stub_klines):
        # 60h old — still in the no-eval window.
        signal = _make_signal(
            created_at=datetime.now(UTC) - timedelta(hours=60),
            fingerprint_extra="too-recent",
        )
        signal.created_at = datetime.now(UTC) - timedelta(hours=60)
        db_session.add(signal)
        await db_session.flush()

        summary = await evaluate_pending_outcomes(db_session)
        assert summary["candidates"] == 0
        assert summary["evaluated"] == 0
        assert (await db_session.execute(select(SignalOutcome))).scalars().first() is None

    async def test_skips_wait_signal(self, db_session, stub_klines):
        signal = _make_signal(
            created_at=datetime.now(UTC) - timedelta(hours=80),
            suggested_action="wait",
            entry_price=None,
            stop_price=None,
            target_price=None,
            fingerprint_extra="wait",
        )
        signal.created_at = datetime.now(UTC) - timedelta(hours=80)
        db_session.add(signal)
        await db_session.flush()

        summary = await evaluate_pending_outcomes(db_session)
        # The signal IS a candidate (passes the SQL filter) but the
        # direction-from-suggested-action map drops it inside the loop.
        assert summary["candidates"] == 1
        assert summary["evaluated"] == 0
        assert summary["skipped_no_direction"] == 1

    async def test_skips_signal_with_null_price_at_creation(self, db_session, stub_klines):
        signal = _make_signal(
            created_at=datetime.now(UTC) - timedelta(hours=80),
            price_at_creation=None,
            fingerprint_extra="no-price",
        )
        signal.created_at = datetime.now(UTC) - timedelta(hours=80)
        db_session.add(signal)
        await db_session.flush()

        summary = await evaluate_pending_outcomes(db_session)
        assert summary["candidates"] == 0  # filtered at the SQL level

    async def test_skips_market_signal_even_with_price(self, db_session, stub_klines):
        """PR F.3 — MARKET regime_shift signals must be filtered at the SQL
        level (`Signal.asset != MARKET_ASSET` in `base_filters`).

        Defensive: even if a future change populates `price_at_creation`
        for MARKET signals (e.g. weighted BTC+ETH average), they should
        still be skipped because there's no single asset price series to
        score against. The explicit asset filter decouples the skip
        behavior from the price-null coincidence today.
        """
        signal = _make_signal(
            created_at=datetime.now(UTC) - timedelta(hours=80),
            asset="MARKET",
            signal_type="regime_shift",
            # Force a non-null price so this test pins the asset filter
            # specifically, not the price-null filter.
            price_at_creation=Decimal("84200"),
            fingerprint_extra="market-skip",
        )
        signal.created_at = datetime.now(UTC) - timedelta(hours=80)
        db_session.add(signal)
        await db_session.flush()

        summary = await evaluate_pending_outcomes(db_session)
        assert summary["candidates"] == 0
        assert summary["evaluated"] == 0
        # No klines fetched — the SQL filter rejected the signal before
        # the per-signal loop ran.
        assert stub_klines["calls"] == []

    async def test_idempotent_skips_signal_with_existing_outcome(self, db_session, stub_klines):
        t0 = datetime.now(UTC) - timedelta(hours=80)
        signal = _make_signal(created_at=t0, fingerprint_extra="dupe")
        signal.created_at = t0
        db_session.add(signal)
        await db_session.flush()

        # Pre-insert an outcome row so the LEFT-JOIN-IS-NULL filter excludes it.
        db_session.add(
            SignalOutcome(
                signal_id=signal.id,
                asset="BTC",
                signal_type="flow_anomaly",
                direction="long",
                confidence=7,
                price_at_signal=Decimal("84200"),
                evaluated_at=datetime.now(UTC),
            )
        )
        await db_session.flush()

        summary = await evaluate_pending_outcomes(db_session)
        assert summary["candidates"] == 0
        assert summary["evaluated"] == 0
        # No new row.
        rows = (await db_session.execute(select(SignalOutcome))).scalars().all()
        assert len(rows) == 1

    async def test_kline_fetch_failure_logs_and_skips(self, db_session, stub_klines):
        t0 = datetime.now(UTC) - timedelta(hours=80)
        signal = _make_signal(created_at=t0, fingerprint_extra="kline-fail")
        signal.created_at = t0
        db_session.add(signal)
        await db_session.flush()

        stub_klines["raise_none"] = True
        summary = await evaluate_pending_outcomes(db_session)
        assert summary["candidates"] == 1
        assert summary["evaluated"] == 0
        assert summary["skipped_no_klines"] == 1
        assert (await db_session.execute(select(SignalOutcome))).scalars().first() is None

    async def test_no_bars_in_window_skips_without_inserting(self, db_session, stub_klines):
        t0 = datetime.now(UTC) - timedelta(hours=80)
        signal = _make_signal(created_at=t0, fingerprint_extra="empty-window")
        signal.created_at = t0
        db_session.add(signal)
        await db_session.flush()

        # Bars exist but are from a year ago — none in our [t0, t0+72h] window.
        old_ts = int((t0 - timedelta(days=365)).timestamp() * 1000)
        stub_klines["bars"] = [
            PriceBar(
                timestamp_ms=old_ts,
                open=Decimal("50000"),
                high=Decimal("50100"),
                low=Decimal("49900"),
                close=Decimal("50050"),
            )
        ]

        summary = await evaluate_pending_outcomes(db_session)
        assert summary["candidates"] == 1
        assert summary["evaluated"] == 0
        assert summary["skipped_no_bars_in_window"] == 1
        # CRITICAL — no all-NULL outcome row inserted, would pollute hit-rate.
        assert (await db_session.execute(select(SignalOutcome))).scalars().first() is None

    async def test_null_price_source_defaults_to_sosovalue(self, db_session, stub_klines):
        """Legacy pre-Stage-7 signals have NULL price_source. The evaluator
        still scores them, defaulting to the live composer's primary."""
        t0 = datetime.now(UTC) - timedelta(hours=80)
        signal = _make_signal(
            created_at=t0,
            price_source=None,  # legacy
            fingerprint_extra="legacy-source",
        )
        signal.created_at = t0
        db_session.add(signal)
        await db_session.flush()

        t0_ms = int(t0.timestamp() * 1000)
        stub_klines["bars"] = [
            PriceBar(
                timestamp_ms=t0_ms + 24 * 3600 * 1000,
                open=Decimal("84200"),
                high=Decimal("84500"),
                low=Decimal("83900"),
                close=Decimal("84300"),
            )
        ]

        summary = await evaluate_pending_outcomes(db_session)
        assert summary["evaluated"] == 1
        assert stub_klines["calls"][0]["source"] == "sosovalue"

    async def test_skips_unknown_asset(self, db_session, stub_klines):
        """Defensive — if a future signal type emits asset="SOL" before the
        kline fetcher learns it, log + skip rather than crash the loop."""
        t0 = datetime.now(UTC) - timedelta(hours=80)
        signal = _make_signal(created_at=t0, fingerprint_extra="sol")
        signal.asset = "SOL"
        signal.created_at = t0
        db_session.add(signal)
        await db_session.flush()

        summary = await evaluate_pending_outcomes(db_session)
        assert summary["candidates"] == 1
        assert summary["evaluated"] == 0
        assert summary["skipped_unknown_asset"] == 1

    async def test_one_bad_signal_does_not_kill_the_cycle(
        self, db_session, stub_klines, monkeypatch
    ):
        """D13 — per-signal try/except in the eval loop. A corrupt signal
        whose kline-fetch (or any other downstream call) raises must NOT
        prevent the OTHER candidates from being scored + persisted.
        Regression: an earlier draft had a single top-level try block that
        let one bad signal abort every prior in-flight outcome."""
        t0 = datetime.now(UTC) - timedelta(hours=80)
        bad = _make_signal(created_at=t0, fingerprint_extra="bad")
        bad.created_at = t0
        good = _make_signal(created_at=t0, fingerprint_extra="good")
        good.created_at = t0
        db_session.add_all([bad, good])
        await db_session.flush()

        # Build a fetcher that raises on the FIRST signal (whichever id
        # comes first in created_at order — both share `t0`, so the lower
        # id sorts first by tiebreak). Subsequent calls return real bars.
        call_n = {"i": 0}
        t0_ms = int(t0.timestamp() * 1000)

        async def _flaky(asset, source, *, start_time_ms=None, end_time_ms=None, limit=100):
            call_n["i"] += 1
            if call_n["i"] == 1:
                raise RuntimeError("simulated transient failure on first signal")
            return [
                PriceBar(
                    timestamp_ms=t0_ms + 24 * 3600 * 1000,
                    open=Decimal("84200"),
                    high=Decimal("84500"),
                    low=Decimal("83900"),
                    close=Decimal("84300"),
                )
            ]

        monkeypatch.setattr("etfpulse.pipeline.track_record.get_daily_klines_from_source", _flaky)

        summary = await evaluate_pending_outcomes(db_session)
        assert summary["candidates"] == 2
        assert summary["evaluated"] == 1  # the good one still got through
        assert summary["errored"] == 1
        # One outcome row exists — for the good signal.
        outcomes = (await db_session.execute(select(SignalOutcome))).scalars().all()
        assert len(outcomes) == 1


# ---------------------------------------------------------------------------
# get_stats_by_confidence_floor + TrackRecordStat (Stage 8-P8)
# ---------------------------------------------------------------------------


async def _seed_outcome_only(
    db_session,
    *,
    confidence: int,
    hit_target: bool | None,
    key: str,
    evaluated_at: datetime | None = None,
) -> None:
    """Tiny helper for the per-floor stats tests — seeds Signal + Outcome
    in one call. Distinct from `_make_signal` (used by the eval-loop tests)
    because here we never call the eval loop; we just need the rows present."""
    signal = _make_signal(created_at=datetime.now(UTC) - timedelta(hours=80), fingerprint_extra=key)
    db_session.add(signal)
    await db_session.flush()
    db_session.add(
        SignalOutcome(
            signal_id=signal.id,
            asset="BTC",
            signal_type="flow_anomaly",
            direction="long",
            confidence=confidence,
            target_price=Decimal("89500"),
            price_at_signal=Decimal("84200"),
            hit_target=hit_target,
            evaluated_at=evaluated_at or datetime.now(UTC),
        )
    )
    await db_session.flush()


class TestGetStatsByConfidenceFloor:
    async def test_empty_db_returns_all_zero_floors(self, db_session):
        stat = await get_stats_by_confidence_floor(db_session)
        # All 10 floors present, all (0, 0).
        for floor in range(1, 11):
            assert stat.by_floor[floor] == (0, 0)
            assert stat.hit_rate_pct(floor) is None

    async def test_cumulative_across_floors(self, db_session):
        """3 hits at conf 8, 1 miss at conf 7, 2 hits at conf 9, 1 miss at conf 5.
        At floor 7+: targeted=7, hits=5 → 71% (rounded). Floor 9+: targeted=2, hits=2 → 100%.
        Floor 5+: targeted=7, hits=5 → 71%."""
        await _seed_outcome_only(db_session, confidence=8, hit_target=True, key="a")
        await _seed_outcome_only(db_session, confidence=8, hit_target=True, key="b")
        await _seed_outcome_only(db_session, confidence=8, hit_target=True, key="c")
        await _seed_outcome_only(db_session, confidence=7, hit_target=False, key="d")
        await _seed_outcome_only(db_session, confidence=9, hit_target=True, key="e")
        await _seed_outcome_only(db_session, confidence=9, hit_target=True, key="f")
        await _seed_outcome_only(db_session, confidence=5, hit_target=False, key="g")

        stat = await get_stats_by_confidence_floor(db_session)
        # Floor 9 → only conf 9: 2 hits / 2 targeted = 100%
        assert stat.by_floor[9] == (2, 2)
        assert stat.hit_rate_pct(9) == 100
        # Floor 8 → conf 8+9: 5 hits / 5 targeted = 100%
        assert stat.by_floor[8] == (5, 5)
        assert stat.hit_rate_pct(8) == 100
        # Floor 7 → +1 miss at 7: 5 hits / 6 targeted ≈ 83%
        assert stat.by_floor[7] == (6, 5)
        assert stat.hit_rate_pct(7) == 83
        # Floor 5 → +1 miss at 5: 5 hits / 7 targeted ≈ 71%
        assert stat.by_floor[5] == (7, 5)
        assert stat.hit_rate_pct(5) == 71
        # Floor 1 → same as floor 5 (nothing at confidences 2-4 here)
        assert stat.by_floor[1] == (7, 5)

    async def test_excludes_no_target_signals_from_targeted_count(self, db_session):
        """A signal with hit_target IS NULL (AI declined) doesn't count
        toward the targeted denominator — same rationale as `/api/track-record`
        and `/api/dashboard/stats`."""
        await _seed_outcome_only(db_session, confidence=8, hit_target=True, key="t")
        await _seed_outcome_only(db_session, confidence=8, hit_target=None, key="n1")
        await _seed_outcome_only(db_session, confidence=8, hit_target=None, key="n2")

        stat = await get_stats_by_confidence_floor(db_session)
        # 1 hit / 1 targeted = 100% (the two None-target signals don't count)
        assert stat.by_floor[8] == (1, 1)
        assert stat.hit_rate_pct(8) == 100
        # `targeted_count` mirrors `by_floor[N][0]`.
        assert stat.targeted_count(8) == 1

    async def test_excludes_unevaluated_outcomes(self, db_session):
        """Defensive — `evaluated_at IS NOT NULL` filter parity with
        the dashboard + track-record endpoints."""
        signal = _make_signal(
            created_at=datetime.now(UTC) - timedelta(hours=80), fingerprint_extra="unev"
        )
        db_session.add(signal)
        await db_session.flush()
        db_session.add(
            SignalOutcome(
                signal_id=signal.id,
                asset="BTC",
                signal_type="flow_anomaly",
                direction="long",
                confidence=8,
                target_price=Decimal("89500"),
                price_at_signal=Decimal("84200"),
                hit_target=True,
                evaluated_at=None,  # explicitly unevaluated
            )
        )
        await db_session.flush()

        stat = await get_stats_by_confidence_floor(db_session)
        assert stat.by_floor[8] == (0, 0)
        assert stat.hit_rate_pct(8) is None

    async def test_hit_rate_pct_rounds_to_nearest_integer(self, db_session):
        """3/7 = 42.857% → rounds to 43, not 42 or 42.86."""
        for i in range(3):
            await _seed_outcome_only(db_session, confidence=7, hit_target=True, key=f"h{i}")
        for i in range(4):
            await _seed_outcome_only(db_session, confidence=7, hit_target=False, key=f"m{i}")

        stat = await get_stats_by_confidence_floor(db_session)
        assert stat.hit_rate_pct(7) == 43


# ---------------------------------------------------------------------------
# Limit + remaining — protects upstream klines APIs from fresh-deploy backlogs
# ---------------------------------------------------------------------------


class TestEvaluatePendingOutcomesLimit:
    """`limit` caps batch size to bound upstream klines fetch volume.
    `remaining` reports the leftover so the caller (admin button or operator
    log) knows whether to click again."""

    async def test_limit_caps_candidates_processed(self, db_session, stub_klines):
        """5 aged eligible signals + limit=2 → only 2 in candidates AND
        only 2 klines fetches happen (proved via stub call count)."""
        t0 = datetime.now(UTC) - timedelta(hours=80)
        for i in range(5):
            sig = _make_signal(created_at=t0, fingerprint_extra=f"limit-{i}")
            sig.created_at = t0 - timedelta(seconds=i)  # deterministic ordering
            db_session.add(sig)
        await db_session.flush()

        t0_ms = int(t0.timestamp() * 1000)
        stub_klines["bars"] = [
            PriceBar(
                timestamp_ms=t0_ms + i * 24 * 3600 * 1000,
                open=Decimal("84200"),
                high=Decimal("84300"),
                low=Decimal("84100"),
                close=Decimal("84200"),
            )
            for i in range(3)
        ]

        summary = await evaluate_pending_outcomes(db_session, limit=2)
        assert summary["candidates"] == 2
        assert summary["evaluated"] == 2
        # Only 2 klines calls — proves the LIMIT is applied at the SQL level,
        # not after a 5-row materialization.
        assert len(stub_klines["calls"]) == 2

    async def test_remaining_reports_leftover_when_limit_filled(self, db_session, stub_klines):
        """Limit=2 with 5 eligible → remaining=3. Operator can re-click
        until remaining=0 to drain the backlog."""
        t0 = datetime.now(UTC) - timedelta(hours=80)
        for i in range(5):
            sig = _make_signal(created_at=t0, fingerprint_extra=f"rem-{i}")
            sig.created_at = t0 - timedelta(seconds=i)
            db_session.add(sig)
        await db_session.flush()

        t0_ms = int(t0.timestamp() * 1000)
        stub_klines["bars"] = [
            PriceBar(
                timestamp_ms=t0_ms,
                open=Decimal("84200"),
                high=Decimal("84300"),
                low=Decimal("84100"),
                close=Decimal("84200"),
            )
        ]

        summary = await evaluate_pending_outcomes(db_session, limit=2)
        assert summary["candidates"] == 2
        assert summary["remaining"] == 3

    async def test_remaining_is_zero_when_limit_not_filled(self, db_session, stub_klines):
        """3 eligible + limit=10 → remaining=0 (no need for another click).
        Avoids the COUNT roundtrip on the common under-limit path."""
        t0 = datetime.now(UTC) - timedelta(hours=80)
        for i in range(3):
            sig = _make_signal(created_at=t0, fingerprint_extra=f"under-{i}")
            sig.created_at = t0 - timedelta(seconds=i)
            db_session.add(sig)
        await db_session.flush()

        stub_klines["bars"] = [
            PriceBar(
                timestamp_ms=int(t0.timestamp() * 1000),
                open=Decimal("84200"),
                high=Decimal("84300"),
                low=Decimal("84100"),
                close=Decimal("84200"),
            )
        ]

        summary = await evaluate_pending_outcomes(db_session, limit=10)
        assert summary["candidates"] == 3
        assert summary["remaining"] == 0

    async def test_remaining_zero_in_unlimited_mode(self, db_session, stub_klines):
        """`limit=None` (scheduler default in current call site) → remaining
        always 0. The unlimited path is fully drained by definition; we
        skip the COUNT roundtrip entirely."""
        t0 = datetime.now(UTC) - timedelta(hours=80)
        for i in range(3):
            sig = _make_signal(created_at=t0, fingerprint_extra=f"unlim-{i}")
            sig.created_at = t0 - timedelta(seconds=i)
            db_session.add(sig)
        await db_session.flush()

        stub_klines["bars"] = [
            PriceBar(
                timestamp_ms=int(t0.timestamp() * 1000),
                open=Decimal("84200"),
                high=Decimal("84300"),
                low=Decimal("84100"),
                close=Decimal("84200"),
            )
        ]

        summary = await evaluate_pending_outcomes(db_session, limit=None)
        assert summary["candidates"] == 3
        assert summary["remaining"] == 0

    async def test_oldest_signals_processed_first(self, db_session, stub_klines):
        """FIFO drain — oldest signals score first when the limit truncates.
        Same operator intuition as the AI-retry backfill."""
        now = datetime.now(UTC)
        old = _make_signal(created_at=now - timedelta(hours=200), fingerprint_extra="OLD")
        old.created_at = now - timedelta(hours=200)
        new = _make_signal(created_at=now - timedelta(hours=80), fingerprint_extra="NEW")
        new.created_at = now - timedelta(hours=80)
        db_session.add_all([new, old])  # insert order ≠ created_at order
        await db_session.flush()

        stub_klines["bars"] = [
            PriceBar(
                timestamp_ms=int((now - timedelta(hours=200)).timestamp() * 1000),
                open=Decimal("84200"),
                high=Decimal("84300"),
                low=Decimal("84100"),
                close=Decimal("84200"),
            )
        ]

        summary = await evaluate_pending_outcomes(db_session, limit=1)
        assert summary["evaluated"] == 1
        # Only one outcome row inserted — for the OLDER signal.
        outcomes = (await db_session.execute(select(SignalOutcome))).scalars().all()
        assert len(outcomes) == 1
        assert outcomes[0].signal_id == old.id


# ---------------------------------------------------------------------------
# PR B (issue #60) — per-horizon scoring
# ---------------------------------------------------------------------------


class TestComputeMetricsHorizonAware:
    """PR B replaced the fixed 72h window with `window_hours` per-signal.
    These tests pin the new contract: legacy 24h/72h checkpoints are NULL
    when outside the window, and `price_at_validity_end` is the canonical
    'outcome close' for every horizon."""

    def test_swing_72h_populates_legacy_and_validity_end_same(self):
        """Swing's validity end (72h) == legacy 72h checkpoint. Both columns
        populate identically so existing dashboard/Telegram consumers reading
        `price_after_72h` keep working unchanged for swing signals."""
        bars = _four_day_bars(
            [
                ("84000", "84200", "83800", "84100"),
                ("84100", "84500", "84000", "84300"),
                ("84300", "84600", "84200", "84400"),
                ("84400", "84800", "84300", "84600"),
            ]
        )
        m = _compute_metrics(
            direction=SignalDirection.LONG,
            entry=Decimal("84000"),
            stop=Decimal("83000"),
            target=Decimal("90000"),
            t0_ms=_T0_MS,
            window_hours=72,
            bars=bars,
        )
        assert m is not None
        # validity_end is the close of the bar containing t0+72h, same as
        # the legacy 72h checkpoint by construction. Asserting equality
        # protects against drift in `_pick_close_at`'s targeting.
        assert m.price_at_validity_end == m.price_after_72h
        assert m.price_after_24h is not None  # 24h is inside the 72h window

    def test_position_168h_populates_all_three_checkpoints(self):
        """A 168h-window signal records 24h, 72h, AND validity_end (168h).
        The legacy fields aren't NULL — they're meaningful interim checkpoints
        for a multi-day trade, just not the final outcome price."""
        # Eight daily bars covering Day 22..29 (168h = 7 days after t0).
        # _T0_MS is the open of Day 22 + 4h.
        bars = [
            PriceBar(
                timestamp_ms=_T0_MS + (i - 1) * 24 * 3600 * 1000,
                open=Decimal("84000") + Decimal(i * 50),
                high=Decimal("84500") + Decimal(i * 50),
                low=Decimal("83800") + Decimal(i * 50),
                close=Decimal("84200") + Decimal(i * 50),
            )
            for i in range(8)  # 0..7 days
        ]
        m = _compute_metrics(
            direction=SignalDirection.LONG,
            entry=Decimal("84200"),
            stop=Decimal("83000"),
            target=Decimal("90000"),
            t0_ms=_T0_MS,
            window_hours=168,
            bars=bars,
        )
        assert m is not None
        # All three are populated for position (168h covers both interim
        # checkpoints + the end).
        assert m.price_after_24h is not None
        assert m.price_after_72h is not None
        assert m.price_at_validity_end is not None
        # validity_end (close at t0+168h) is the last bar's close — higher
        # than the 72h checkpoint (price drift up over the window).
        assert m.price_at_validity_end > m.price_after_72h


class TestEvaluatePendingOutcomesV2:
    """The v2 evaluator skips scalp signals (#62), rejects invalid windows,
    and stamps `scoring_version='v2'` + `window_hours` + `price_at_validity_end`
    on every new outcome row."""

    async def test_skips_scalp_signal_with_dedicated_counter(self, db_session, stub_klines):
        """A scalp signal (6h validity) is bucketed in `skipped_scalp_intraday_unsupported`,
        NOT in `skipped_no_bars_in_window`. The distinct counter is what makes
        '#62 is the blocker' visible to operators rather than 'kline data is missing'."""
        t0 = datetime.now(UTC) - timedelta(hours=10)
        scalp = _make_signal(
            created_at=t0,
            horizon_hours=6,  # scalp
            fingerprint_extra="scalp",
        )
        db_session.add(scalp)
        await db_session.flush()

        summary = await evaluate_pending_outcomes(db_session)
        assert summary["candidates"] == 1
        assert summary["skipped_scalp_intraday_unsupported"] == 1
        assert summary["evaluated"] == 0
        assert summary["skipped_no_bars_in_window"] == 0  # not the generic path
        # No outcome row inserted.
        assert (await db_session.execute(select(SignalOutcome))).scalars().first() is None

    async def test_rejects_invalid_window_when_expires_at_equals_created_at(
        self, db_session, stub_klines
    ):
        """Defensive: a signal with non-positive (`expires_at <= created_at`)
        window — produced by clock skew or a buggy future `compute_expires_at`
        — is skipped with the dedicated counter, NOT silently scored."""
        t0 = datetime.now(UTC) - timedelta(hours=80)
        bad = _make_signal(
            created_at=t0,
            expires_at=t0,  # zero-length window
            fingerprint_extra="zero-window",
        )
        # Note: candidate gate is `expires_at <= now` — t0 (80h ago) ≤ now.
        # The invalid-window guard fires INSIDE _evaluate_one.
        db_session.add(bad)
        await db_session.flush()

        summary = await evaluate_pending_outcomes(db_session)
        assert summary["candidates"] == 1
        assert summary["skipped_invalid_window"] == 1
        assert summary["evaluated"] == 0

    async def test_v2_stamps_window_hours_and_scoring_version_on_outcome(
        self, db_session, stub_klines
    ):
        """Every new outcome row carries `scoring_version='v2'` and the
        derived `window_hours`. Legacy NULL semantics are preserved for old
        rows but new writes are always tagged."""
        t0 = datetime.now(UTC) - timedelta(hours=80)
        signal = _make_signal(created_at=t0, fingerprint_extra="v2-stamp")
        db_session.add(signal)
        await db_session.flush()

        t0_ms = int(t0.timestamp() * 1000)
        stub_klines["bars"] = [
            PriceBar(
                timestamp_ms=t0_ms + i * 24 * 3600 * 1000,
                open=Decimal("84200"),
                high=Decimal("84300"),
                low=Decimal("84100"),
                close=Decimal("84250"),
            )
            for i in range(3)
        ]

        summary = await evaluate_pending_outcomes(db_session)
        assert summary["evaluated"] == 1

        outcome = (await db_session.execute(select(SignalOutcome))).scalar_one()
        assert outcome.scoring_version == "v2"
        assert outcome.window_hours == 72  # swing default in _make_signal
        # price_at_validity_end is populated (= price_after_72h for swing).
        assert outcome.price_at_validity_end is not None
        assert outcome.price_at_validity_end == outcome.price_after_72h
