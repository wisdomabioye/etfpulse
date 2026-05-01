"""DivergenceDetector — pure-helper tests + DB integration smoke tests.

The pure helpers (`_all_same_sign`, `_price_change_over_window`,
`_closest_close_on_or_before`) are tested directly with hand-built fixtures.
The async DB+kline path is exercised by one integration smoke test that uses
the existing fixture-mode SoSoValue/Binance adapters.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

from etfpulse.models import ETFFlow
from etfpulse.pipeline.detectors.divergence import (
    DivergenceDetector,
    _all_same_sign,
    _closest_close_on_or_before,
    _price_change_over_window,
)
from etfpulse.pipeline.prices import PriceBar


def _flow_seq(*flows: int | float, start: date = date(2026, 4, 18)) -> list[tuple[date, Decimal]]:
    return [(start + timedelta(days=i), Decimal(str(f))) for i, f in enumerate(flows)]


def _bar(d: date, close: int | float) -> PriceBar:
    """Build a PriceBar at UTC midnight of `d` with all OHLC = close.

    Tests only need `bar_date` + `close`; the rest are filler.
    """
    ts = int(datetime(d.year, d.month, d.day, tzinfo=UTC).timestamp() * 1000)
    c = Decimal(str(close))
    return PriceBar(timestamp_ms=ts, open=c, high=c, low=c, close=c)


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


class TestAllSameSign:
    def test_all_positive_returns_one(self):
        assert _all_same_sign(_flow_seq(100, 200, 50)) == 1

    def test_all_negative_returns_minus_one(self):
        assert _all_same_sign(_flow_seq(-100, -50, -300)) == -1

    def test_mixed_returns_none(self):
        assert _all_same_sign(_flow_seq(100, -50, 200)) is None

    def test_zero_in_window_breaks_same_sign(self):
        """A zero-flow day means we can't claim consistent direction —
        it's neither positive nor negative."""
        assert _all_same_sign(_flow_seq(100, 0, 200)) is None


class TestPriceChangeOverWindow:
    def test_simple_drop(self):
        flows = _flow_seq(100, 200, 300)  # 2026-04-18..20
        bars = [
            _bar(date(2026, 4, 18), 90_000),
            _bar(date(2026, 4, 19), 88_000),
            _bar(date(2026, 4, 20), 85_000),
        ]
        change = _price_change_over_window(bars, flows)
        assert change == Decimal("-5000")

    def test_simple_rise(self):
        flows = _flow_seq(-100, -200)
        bars = [
            _bar(date(2026, 4, 18), 80_000),
            _bar(date(2026, 4, 19), 84_000),
        ]
        assert _price_change_over_window(bars, flows) == Decimal("4000")

    def test_falls_back_to_previous_bar_on_gap(self):
        """If the exact window-end date is missing from klines, walk back."""
        flows = _flow_seq(100, 200, 300)  # window end = 2026-04-20
        bars = [
            _bar(date(2026, 4, 18), 90_000),
            _bar(date(2026, 4, 19), 88_000),
            # No bar for 2026-04-20 → fall back to 04-19's close.
        ]
        assert _price_change_over_window(bars, flows) == Decimal("-2000")

    def test_no_matching_bars_returns_none(self):
        flows = _flow_seq(100, 200, 300)
        bars = [_bar(date(2025, 1, 1), 50_000)]  # totally outside window
        assert _price_change_over_window(bars, flows) is None


class TestClosestCloseOnOrBefore:
    def test_direct_hit(self):
        closes = {date(2026, 4, 20): Decimal("100")}
        assert _closest_close_on_or_before(closes, date(2026, 4, 20)) == Decimal("100")

    def test_walks_back_within_5_days(self):
        closes = {date(2026, 4, 18): Decimal("90")}
        assert _closest_close_on_or_before(closes, date(2026, 4, 20)) == Decimal("90")

    def test_returns_none_beyond_5_days(self):
        closes = {date(2026, 4, 10): Decimal("50")}
        assert _closest_close_on_or_before(closes, date(2026, 4, 20)) is None


# ---------------------------------------------------------------------------
# DB integration smoke test
# ---------------------------------------------------------------------------


class TestDetectIntegration:
    async def test_returns_empty_when_too_few_rows(self, db_session):
        """No flow data → detector silently returns []."""
        detector = DivergenceDetector(lookback_days=3)
        hits = await detector.detect(db_session)
        assert hits == []

    async def test_emits_hit_on_flow_pos_price_neg(self, db_session, monkeypatch):
        """Insert 3 positive-flow days, monkeypatch the kline composer to return
        a downward-trending price series → expect a `flow_pos_price_neg` hit."""
        # Seed BTC with 3 positive-flow days.
        for i, flow in enumerate(
            [Decimal("100_000_000"), Decimal("200_000_000"), Decimal("150_000_000")]
        ):
            db_session.add(
                ETFFlow(
                    asset="BTC",
                    captured_at=date(2026, 4, 18) + timedelta(days=i),
                    total_net_flow_usd=flow,
                )
            )
        await db_session.flush()

        async def _stub_klines(asset, start_time_ms=None, end_time_ms=None, limit=100):
            return (
                [
                    _bar(date(2026, 4, 17), 90_000),  # padding day
                    _bar(date(2026, 4, 18), 88_000),
                    _bar(date(2026, 4, 19), 86_000),
                    _bar(date(2026, 4, 20), 80_000),
                    _bar(date(2026, 4, 21), 79_000),  # padding day
                ],
                "sosovalue",
            )

        monkeypatch.setattr(
            "etfpulse.pipeline.detectors.divergence.get_daily_klines_with_source",
            _stub_klines,
        )

        detector = DivergenceDetector(lookback_days=3)
        hits = await detector.detect(db_session)

        # ETH has no flows → no ETH hit; only BTC.
        btc_hits = [h for h in hits if h.asset == "BTC"]
        assert len(btc_hits) == 1
        hit = btc_hits[0]
        assert hit.signal_type == "divergence"
        assert hit.signal_date == date(2026, 4, 20)
        assert hit.trigger_data["divergence_type"] == "flow_pos_price_neg"
        assert hit.trigger_data["lookback_days"] == 3
        assert len(hit.fingerprint) == 32

    async def test_no_hit_when_flow_and_price_agree(self, db_session, monkeypatch):
        """All positive flows + rising price → not divergence, no hit."""
        for i, flow in enumerate([Decimal("100"), Decimal("200"), Decimal("150")]):
            db_session.add(
                ETFFlow(
                    asset="BTC",
                    captured_at=date(2026, 4, 18) + timedelta(days=i),
                    total_net_flow_usd=flow,
                )
            )
        await db_session.flush()

        async def _stub(asset, start_time_ms=None, end_time_ms=None, limit=100):
            return (
                [
                    _bar(date(2026, 4, 18), 80_000),
                    _bar(date(2026, 4, 20), 90_000),
                ],
                "sosovalue",
            )

        monkeypatch.setattr(
            "etfpulse.pipeline.detectors.divergence.get_daily_klines_with_source",
            _stub,
        )

        hits = await DivergenceDetector(lookback_days=3).detect(db_session)
        assert [h for h in hits if h.asset == "BTC"] == []

    async def test_no_hit_when_klines_unavailable(self, db_session, monkeypatch):
        """Both providers down → composer returns None → detector skips silently."""
        for i, flow in enumerate([Decimal("100"), Decimal("200"), Decimal("150")]):
            db_session.add(
                ETFFlow(
                    asset="BTC",
                    captured_at=date(2026, 4, 18) + timedelta(days=i),
                    total_net_flow_usd=flow,
                )
            )
        await db_session.flush()

        async def _none(asset, start_time_ms=None, end_time_ms=None, limit=100):
            return None

        monkeypatch.setattr(
            "etfpulse.pipeline.detectors.divergence.get_daily_klines_with_source",
            _none,
        )

        hits = await DivergenceDetector(lookback_days=3).detect(db_session)
        assert hits == []
