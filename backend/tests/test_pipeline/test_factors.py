"""PR I.2 — cross-factor confirmation tests.

Three layers:

1. Pure scorers (`score_regime_factor`, `score_news_factor`,
   `direction_sign_from_action`) — no DB, no I/O. Most of the test
   surface lives here because the per-factor agreement logic is
   the load-bearing math.

2. Price scorer (`score_price_factor`) — stubs the klines fetcher so
   we can exercise the magnitude floor + direction-detection branches
   without touching the network.

3. Orchestrator (`compute_confirmation`) — verifies the score formula
   (sum of confirming factors), the wait/unknown short-circuit, and
   that all three factor votes land in the `votes` dict.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from etfpulse.models import MarketRegime
from etfpulse.pipeline.factors import (
    ConfirmationResult,
    compute_confirmation,
    direction_sign_from_action,
    score_news_factor,
    score_price_factor,
    score_regime_factor,
)
from etfpulse.pipeline.prices import PriceBar

# Helpers --------------------------------------------------------------------


_REF_TIME = datetime(2026, 4, 22, 4, 30, tzinfo=UTC)


def _bar(*, open_iso: str, close_price: str) -> PriceBar:
    """Tiny synthetic bar — only `close` is exercised by the price scorer."""
    ts = int(datetime.fromisoformat(open_iso).replace(tzinfo=UTC).timestamp() * 1000)
    return PriceBar(
        timestamp_ms=ts,
        open=Decimal(close_price),
        high=Decimal(close_price),
        low=Decimal(close_price),
        close=Decimal(close_price),
    )


def _make_klines_fetcher(bars: list[PriceBar] | None):
    """Build a klines_fetcher stub. Pass `None` to simulate fetch failure."""

    async def _fetch(asset, source, *, start_time_ms=None, end_time_ms=None, limit=100):
        return bars

    return _fetch


# ---------------------------------------------------------------------------
# direction_sign_from_action — pure
# ---------------------------------------------------------------------------


class TestDirectionSign:
    def test_consider_long_returns_plus_one(self):
        assert direction_sign_from_action("consider long") == 1

    def test_consider_short_returns_minus_one(self):
        assert direction_sign_from_action("consider short") == -1

    def test_wait_returns_zero(self):
        # 0 is the "skip scoring entirely" sentinel by upstream contract.
        assert direction_sign_from_action("wait") == 0

    def test_none_returns_zero(self):
        # Missing / null suggested_action also short-circuits.
        assert direction_sign_from_action(None) == 0

    def test_unknown_string_returns_zero(self):
        # Defensive: an unfamiliar action ("strong buy" etc.) shouldn't
        # crash — it falls through to the no-direction branch.
        assert direction_sign_from_action("strong buy") == 0


# ---------------------------------------------------------------------------
# score_regime_factor — pure
# ---------------------------------------------------------------------------


class TestRegimeFactor:
    def test_markup_votes_bullish(self):
        v = score_regime_factor(signal_type="flow_anomaly", regime=MarketRegime.MARKUP)
        assert v["vote"] == 1
        assert "markup" in v["reason"]

    def test_accumulation_votes_bullish(self):
        # ACCUMULATION shares the bullish bucket — that's the Wyckoff
        # phase preceding a true MARKUP, so flows tend the same direction.
        v = score_regime_factor(signal_type="flow_anomaly", regime=MarketRegime.ACCUMULATION)
        assert v["vote"] == 1

    def test_markdown_votes_bearish(self):
        v = score_regime_factor(signal_type="flow_anomaly", regime=MarketRegime.MARKDOWN)
        assert v["vote"] == -1
        assert "markdown" in v["reason"]

    def test_distribution_votes_bearish(self):
        v = score_regime_factor(signal_type="flow_anomaly", regime=MarketRegime.DISTRIBUTION)
        assert v["vote"] == -1

    def test_uncertain_votes_zero(self):
        # The regime classifier biases to UNCERTAIN when conviction is low;
        # zero vote keeps it from polluting the confirmation count.
        v = score_regime_factor(signal_type="flow_anomaly", regime=MarketRegime.UNCERTAIN)
        assert v["vote"] == 0
        assert "uncertain" in v["reason"]

    def test_missing_regime_votes_zero(self):
        # Cold-boot: classifier hasn't produced a snapshot yet. No signal,
        # not "disagreement" — same handling as UNCERTAIN.
        v = score_regime_factor(signal_type="flow_anomaly", regime=None)
        assert v["vote"] == 0
        assert "no regime snapshot" in v["reason"]

    def test_regime_shift_self_confirmation_excluded(self):
        # A regime_shift signal IS about a change in regime; counting
        # "regime agrees" would be circular. Score on the other 2 factors
        # only (max realistic = 1 in v1: price agrees + news=0).
        v = score_regime_factor(signal_type="regime_shift", regime=MarketRegime.MARKUP)
        assert v["vote"] == 0
        assert "self-confirmation" in v["reason"]


# ---------------------------------------------------------------------------
# score_news_factor — pure (always 0 in v1)
# ---------------------------------------------------------------------------


class TestNewsFactor:
    def test_v1_always_returns_zero(self):
        # News sentiment isn't computed yet — schema reserves the slot
        # so v2 lifts the ceiling without a migration.
        v = score_news_factor()
        assert v["vote"] == 0
        assert "v2" in v["reason"]


# ---------------------------------------------------------------------------
# score_price_factor — async, with stubbed klines fetcher
# ---------------------------------------------------------------------------


class TestPriceFactor:
    async def test_up_move_above_floor_votes_plus_one(self):
        # 1.5% up over 24h, floor is 1% → vote +1.
        bars = [
            _bar(open_iso="2026-04-21T00:00:00", close_price="100"),
            _bar(open_iso="2026-04-22T00:00:00", close_price="101.5"),
        ]
        v = await score_price_factor(
            asset="BTC",
            price_source="binance",
            reference_time=_REF_TIME,
            window_hours=24,
            min_pct=Decimal("0.01"),
            klines_fetcher=_make_klines_fetcher(bars),
        )
        assert v["vote"] == 1
        assert "up" in v["reason"]

    async def test_down_move_below_floor_votes_minus_one(self):
        # 2% down over 24h → vote -1.
        bars = [
            _bar(open_iso="2026-04-21T00:00:00", close_price="100"),
            _bar(open_iso="2026-04-22T00:00:00", close_price="98"),
        ]
        v = await score_price_factor(
            asset="BTC",
            price_source="binance",
            reference_time=_REF_TIME,
            window_hours=24,
            min_pct=Decimal("0.01"),
            klines_fetcher=_make_klines_fetcher(bars),
        )
        assert v["vote"] == -1
        assert "down" in v["reason"]

    async def test_sub_threshold_drift_votes_zero(self):
        # 0.3% move with 1% floor — too small to be a signal.
        bars = [
            _bar(open_iso="2026-04-21T00:00:00", close_price="100"),
            _bar(open_iso="2026-04-22T00:00:00", close_price="100.3"),
        ]
        v = await score_price_factor(
            asset="BTC",
            price_source="binance",
            reference_time=_REF_TIME,
            window_hours=24,
            min_pct=Decimal("0.01"),
            klines_fetcher=_make_klines_fetcher(bars),
        )
        assert v["vote"] == 0
        assert "flat" in v["reason"]

    async def test_no_klines_returned_votes_zero(self):
        # Provider outage — degrade gracefully, don't disagree.
        v = await score_price_factor(
            asset="BTC",
            price_source="binance",
            reference_time=_REF_TIME,
            window_hours=24,
            min_pct=Decimal("0.01"),
            klines_fetcher=_make_klines_fetcher(None),
        )
        assert v["vote"] == 0
        assert "unavailable" in v["reason"]

    async def test_empty_klines_list_votes_zero(self):
        # Same handling as `None` — both produce "no signal," not -1.
        v = await score_price_factor(
            asset="BTC",
            price_source="binance",
            reference_time=_REF_TIME,
            window_hours=24,
            min_pct=Decimal("0.01"),
            klines_fetcher=_make_klines_fetcher([]),
        )
        assert v["vote"] == 0

    async def test_bars_outside_window_return_zero(self):
        # All bars are way in the future (after `reference_time + padding`)
        # so neither the start nor end pick succeeds.
        far_future = [
            _bar(open_iso="2030-01-01T00:00:00", close_price="100"),
            _bar(open_iso="2030-01-02T00:00:00", close_price="105"),
        ]
        v = await score_price_factor(
            asset="BTC",
            price_source="binance",
            reference_time=_REF_TIME,
            window_hours=24,
            min_pct=Decimal("0.01"),
            klines_fetcher=_make_klines_fetcher(far_future),
        )
        assert v["vote"] == 0
        assert "no bar" in v["reason"]

    async def test_zero_start_price_degenerate_votes_zero(self):
        # Defensive: a future bad-data row with close=0 would NaN the
        # pct-change. Helper short-circuits to vote=0 rather than crash.
        bars = [
            _bar(open_iso="2026-04-21T00:00:00", close_price="0"),
            _bar(open_iso="2026-04-22T00:00:00", close_price="100"),
        ]
        v = await score_price_factor(
            asset="BTC",
            price_source="binance",
            reference_time=_REF_TIME,
            window_hours=24,
            min_pct=Decimal("0.01"),
            klines_fetcher=_make_klines_fetcher(bars),
        )
        assert v["vote"] == 0
        assert "degenerate" in v["reason"]

    async def test_handles_unsorted_input_bars(self):
        # Defensive — provider docs don't contractually guarantee bar
        # ordering. Helper sorts internally before walking.
        unsorted_bars = [
            _bar(open_iso="2026-04-22T00:00:00", close_price="102"),
            _bar(open_iso="2026-04-21T00:00:00", close_price="100"),
        ]
        v = await score_price_factor(
            asset="BTC",
            price_source="binance",
            reference_time=_REF_TIME,
            window_hours=24,
            min_pct=Decimal("0.01"),
            klines_fetcher=_make_klines_fetcher(unsorted_bars),
        )
        assert v["vote"] == 1


# ---------------------------------------------------------------------------
# compute_confirmation — orchestrator
# ---------------------------------------------------------------------------


class TestComputeConfirmation:
    async def test_returns_none_for_wait(self):
        # "wait" signals have no direction; orchestrator short-circuits
        # before touching factors. Caller persists NULL confirmation.
        result = await compute_confirmation(
            suggested_action="wait",
            asset="BTC",
            signal_type="flow_anomaly",
            price_source="binance",
            reference_time=_REF_TIME,
            regime=MarketRegime.MARKUP,
            window_hours=24,
            min_pct=Decimal("0.01"),
            klines_fetcher=_make_klines_fetcher([]),
        )
        assert result is None

    async def test_returns_none_for_none_action(self):
        # Defensive: AI-failed signals shouldn't reach this code path,
        # but the orchestrator handles None without crashing.
        result = await compute_confirmation(
            suggested_action=None,
            asset="BTC",
            signal_type="flow_anomaly",
            price_source="binance",
            reference_time=_REF_TIME,
            regime=MarketRegime.MARKUP,
            window_hours=24,
            min_pct=Decimal("0.01"),
            klines_fetcher=_make_klines_fetcher([]),
        )
        assert result is None

    async def test_long_all_factors_agree_max_realistic_v1_is_two(self):
        # Long signal + price up 2% + regime MARKUP. News=0 always in v1.
        # Score = 2 (price + regime), news contributes 0.
        bars = [
            _bar(open_iso="2026-04-21T00:00:00", close_price="100"),
            _bar(open_iso="2026-04-22T00:00:00", close_price="102"),
        ]
        result = await compute_confirmation(
            suggested_action="consider long",
            asset="BTC",
            signal_type="flow_anomaly",
            price_source="binance",
            reference_time=_REF_TIME,
            regime=MarketRegime.MARKUP,
            window_hours=24,
            min_pct=Decimal("0.01"),
            klines_fetcher=_make_klines_fetcher(bars),
        )
        assert isinstance(result, ConfirmationResult)
        assert result.score == 2  # price + regime; news=0
        assert result.votes["price"]["vote"] == 1
        assert result.votes["regime"]["vote"] == 1
        assert result.votes["news"]["vote"] == 0

    async def test_long_with_disagreeing_regime_scores_one(self):
        # Long + price up + regime MARKDOWN → only price confirms.
        # Disagreeing regime contributes 0 to the count (recorded in
        # votes for audit, doesn't push below zero).
        bars = [
            _bar(open_iso="2026-04-21T00:00:00", close_price="100"),
            _bar(open_iso="2026-04-22T00:00:00", close_price="102"),
        ]
        result = await compute_confirmation(
            suggested_action="consider long",
            asset="BTC",
            signal_type="flow_anomaly",
            price_source="binance",
            reference_time=_REF_TIME,
            regime=MarketRegime.MARKDOWN,
            window_hours=24,
            min_pct=Decimal("0.01"),
            klines_fetcher=_make_klines_fetcher(bars),
        )
        assert result is not None
        assert result.score == 1
        # Regime vote IS recorded (with -1), it just doesn't contribute
        # positively to the agreement count.
        assert result.votes["regime"]["vote"] == -1

    async def test_short_with_down_price_and_bear_regime_scores_two(self):
        # Symmetry check — short direction with confirming factors
        # produces the same score as long with confirming factors.
        bars = [
            _bar(open_iso="2026-04-21T00:00:00", close_price="100"),
            _bar(open_iso="2026-04-22T00:00:00", close_price="98"),
        ]
        result = await compute_confirmation(
            suggested_action="consider short",
            asset="BTC",
            signal_type="flow_anomaly",
            price_source="binance",
            reference_time=_REF_TIME,
            regime=MarketRegime.MARKDOWN,
            window_hours=24,
            min_pct=Decimal("0.01"),
            klines_fetcher=_make_klines_fetcher(bars),
        )
        assert result is not None
        assert result.score == 2

    async def test_all_factors_neutral_scores_zero(self):
        # Flat price + UNCERTAIN regime + news=0 → no confirmation at all.
        bars = [
            _bar(open_iso="2026-04-21T00:00:00", close_price="100"),
            _bar(open_iso="2026-04-22T00:00:00", close_price="100.05"),
        ]
        result = await compute_confirmation(
            suggested_action="consider long",
            asset="BTC",
            signal_type="flow_anomaly",
            price_source="binance",
            reference_time=_REF_TIME,
            regime=MarketRegime.UNCERTAIN,
            window_hours=24,
            min_pct=Decimal("0.01"),
            klines_fetcher=_make_klines_fetcher(bars),
        )
        assert result is not None
        assert result.score == 0

    async def test_regime_shift_signal_excluded_from_regime_factor(self):
        # Regime-shift signals have the regime factor self-confirmation
        # carve-out. With a perfect price confirmation, the max score
        # is 1 (price only). Regime vote is 0 (carve-out), news=0.
        bars = [
            _bar(open_iso="2026-04-21T00:00:00", close_price="100"),
            _bar(open_iso="2026-04-22T00:00:00", close_price="102"),
        ]
        result = await compute_confirmation(
            suggested_action="consider long",
            asset="BTC",
            signal_type="regime_shift",
            price_source="binance",
            reference_time=_REF_TIME,
            regime=MarketRegime.MARKUP,  # would normally agree
            window_hours=24,
            min_pct=Decimal("0.01"),
            klines_fetcher=_make_klines_fetcher(bars),
        )
        assert result is not None
        assert result.score == 1  # price only; regime carved out
        assert result.votes["regime"]["vote"] == 0

    async def test_votes_dict_has_all_three_factor_keys(self):
        # Shape contract: every result carries all three keys so the FE
        # can render the breakdown without per-key existence checks.
        bars = [
            _bar(open_iso="2026-04-21T00:00:00", close_price="100"),
            _bar(open_iso="2026-04-22T00:00:00", close_price="100"),
        ]
        result = await compute_confirmation(
            suggested_action="consider long",
            asset="BTC",
            signal_type="flow_anomaly",
            price_source="binance",
            reference_time=_REF_TIME,
            regime=MarketRegime.UNCERTAIN,
            window_hours=24,
            min_pct=Decimal("0.01"),
            klines_fetcher=_make_klines_fetcher(bars),
        )
        assert result is not None
        assert set(result.votes.keys()) == {"price", "regime", "news"}
        # Every vote has both a numeric vote and a string reason.
        for v in result.votes.values():
            assert isinstance(v["vote"], int)
            assert isinstance(v["reason"], str)
            assert v["reason"]  # non-empty

    @pytest.mark.parametrize("score_min,score_max", [(0, 3)])
    async def test_score_stays_within_db_check_bounds(self, score_min, score_max):
        # Score must fit `confirmation_score IS NULL OR BETWEEN 0 AND 3`
        # — pinned here so an off-by-one in the agreement formula would
        # fail the constraint downstream rather than produce silent rows.
        bars = [
            _bar(open_iso="2026-04-21T00:00:00", close_price="100"),
            _bar(open_iso="2026-04-22T00:00:00", close_price="102"),
        ]
        result = await compute_confirmation(
            suggested_action="consider long",
            asset="BTC",
            signal_type="flow_anomaly",
            price_source="binance",
            reference_time=_REF_TIME,
            regime=MarketRegime.MARKUP,
            window_hours=24,
            min_pct=Decimal("0.01"),
            klines_fetcher=_make_klines_fetcher(bars),
        )
        assert result is not None
        assert score_min <= result.score <= score_max
