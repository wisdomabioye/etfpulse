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
    """PR F.2 — helper returns `(price_change, start_close, end_close)`.
    Returning all three values from one bar-walk lets the detector compute
    `price_change_pct = abs(change) / start_close` AND populate the
    trigger_data endpoint snapshots without re-reading `bars[0]` /
    `bars[-1]` (which are PADDING bars; issue #37)."""

    def test_simple_drop(self):
        flows = _flow_seq(100, 200, 300)  # 2026-04-18..20
        bars = [
            _bar(date(2026, 4, 18), 90_000),
            _bar(date(2026, 4, 19), 88_000),
            _bar(date(2026, 4, 20), 85_000),
        ]
        result = _price_change_over_window(bars, flows)
        assert result == (Decimal("-5000"), Decimal("90000"), Decimal("85000"))

    def test_simple_rise(self):
        flows = _flow_seq(-100, -200)
        bars = [
            _bar(date(2026, 4, 18), 80_000),
            _bar(date(2026, 4, 19), 84_000),
        ]
        assert _price_change_over_window(bars, flows) == (
            Decimal("4000"),
            Decimal("80000"),
            Decimal("84000"),
        )

    def test_falls_back_to_previous_bar_on_gap(self):
        """If the exact window-end date is missing from klines, walk back."""
        flows = _flow_seq(100, 200, 300)  # window end = 2026-04-20
        bars = [
            _bar(date(2026, 4, 18), 90_000),
            _bar(date(2026, 4, 19), 88_000),
            # No bar for 2026-04-20 → fall back to 04-19's close.
        ]
        assert _price_change_over_window(bars, flows) == (
            Decimal("-2000"),
            Decimal("90000"),
            Decimal("88000"),
        )

    def test_ignores_padding_bars(self):
        """Issue #37 regression. `_load_price_bars` pads the window by one
        day on each side; the helper MUST resolve closes by flow-window
        date, not by bar index. We construct bars where the padding-day
        closes differ sharply from the in-window closes and assert the
        helper returns the in-window values."""
        flows = _flow_seq(100, 200, 300)  # 2026-04-18..20
        bars = [
            # Pre-window padding bar — wildly different close so a buggy
            # `bars[0]` reader would obviously fail.
            _bar(date(2026, 4, 17), 50_000),
            _bar(date(2026, 4, 18), 90_000),
            _bar(date(2026, 4, 19), 88_000),
            _bar(date(2026, 4, 20), 85_000),
            # Post-window padding bar.
            _bar(date(2026, 4, 21), 130_000),
        ]
        assert _price_change_over_window(bars, flows) == (
            Decimal("-5000"),
            Decimal("90000"),
            Decimal("85000"),
        )

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


# ---------------------------------------------------------------------------
# Magnitude floors (PR F.2)
# ---------------------------------------------------------------------------


def _seed_flows(db_session, flows: list[Decimal], start: date = date(2026, 4, 18)):
    """Insert positive-flow days for BTC starting at `start`."""
    for i, flow in enumerate(flows):
        db_session.add(
            ETFFlow(
                asset="BTC",
                captured_at=start + timedelta(days=i),
                total_net_flow_usd=flow,
            )
        )


def _patch_klines(monkeypatch, *, start: date, start_close: int, end_close: int):
    """Two-bar series at `start` and `start+2` so `_price_change_over_window`
    can match both endpoints of a 3-day flow window."""

    async def _stub(asset, start_time_ms=None, end_time_ms=None, limit=100):
        return (
            [
                _bar(start - timedelta(days=1), start_close),
                _bar(start, start_close),
                _bar(start + timedelta(days=2), end_close),
            ],
            "sosovalue",
        )

    monkeypatch.setattr(
        "etfpulse.pipeline.detectors.divergence.get_daily_klines_with_source",
        _stub,
    )


class TestMagnitudeFloors:
    """PR F.2 — directional sign-mismatch alone isn't enough. Both the price
    swing and the institutional flow volume must be economically meaningful
    for divergence to fire. Without these floors the detector fires on tiny
    drifts across small flow weeks, dominating the signal cohort with noise
    (see #76)."""

    async def test_no_hit_when_price_change_below_floor(self, db_session, monkeypatch):
        """Flow + price directionally disagree, but the price moved only 0.5%
        over the window — below the 2% default floor. Skip."""
        start = date(2026, 4, 18)
        # $450M total flow — clears the flow floor.
        _seed_flows(
            db_session,
            [Decimal("150_000_000"), Decimal("200_000_000"), Decimal("100_000_000")],
            start=start,
        )
        await db_session.flush()

        # Price drops 0.5% (80_000 → 79_600). Sign mismatches positive flow.
        _patch_klines(monkeypatch, start=start, start_close=80_000, end_close=79_600)

        hits = await DivergenceDetector(lookback_days=3).detect(db_session)
        assert [h for h in hits if h.asset == "BTC"] == []

    async def test_no_hit_when_flow_sum_below_floor(self, db_session, monkeypatch):
        """Flow + price directionally disagree, price moved 10% (well past
        the price floor), but total flow was only $100M — below the $300M
        default floor. Skip."""
        start = date(2026, 4, 18)
        # $100M total — below the $300M default floor.
        _seed_flows(
            db_session,
            [Decimal("30_000_000"), Decimal("40_000_000"), Decimal("30_000_000")],
            start=start,
        )
        await db_session.flush()

        _patch_klines(monkeypatch, start=start, start_close=80_000, end_close=72_000)

        hits = await DivergenceDetector(lookback_days=3).detect(db_session)
        assert [h for h in hits if h.asset == "BTC"] == []

    async def test_hits_when_both_magnitudes_clear_floors(self, db_session, monkeypatch):
        """Sign mismatch + 5% price drop + $400M flow → both floors cleared,
        fires. `trigger_data` must carry the new magnitude fields so the AI
        prompt + UI can render them."""
        start = date(2026, 4, 18)
        _seed_flows(
            db_session,
            [Decimal("100_000_000"), Decimal("200_000_000"), Decimal("100_000_000")],
            start=start,
        )
        await db_session.flush()

        _patch_klines(monkeypatch, start=start, start_close=80_000, end_close=76_000)

        hits = await DivergenceDetector(lookback_days=3).detect(db_session)
        btc = [h for h in hits if h.asset == "BTC"]
        assert len(btc) == 1
        hit = btc[0]
        assert hit.trigger_data["divergence_type"] == "flow_pos_price_neg"
        # New magnitude fields are populated.
        assert Decimal(hit.trigger_data["flow_sum_usd"]) == Decimal("400000000")
        # 4000 / 80000 = 0.05 → 5%.
        assert Decimal(hit.trigger_data["price_change_pct"]) == Decimal("0.05")

    async def test_trigger_data_endpoints_ignore_padding_bars(self, db_session, monkeypatch):
        """Issue #37 regression. `_load_price_bars` pads the kline window
        by one day on each side; `trigger_data["price_at_window_start"]` and
        `price_at_window_end` must be the closes on the actual flow-window
        endpoints, NOT the padding bars. This test sets distinct padding
        closes so a buggy `bars[0].close` / `bars[-1].close` reader would
        immediately fail.

        Invariant check: `price_change_pct` from `trigger_data` MUST equal
        `(end - start) / start` computed from the same `trigger_data`
        fields. Pre-fix the algebra was broken because pct used the right
        start close but the snapshot field used the padding-bar one.
        """
        start = date(2026, 4, 18)
        _seed_flows(
            db_session,
            [Decimal("100_000_000"), Decimal("200_000_000"), Decimal("100_000_000")],
            start=start,
        )
        await db_session.flush()

        async def _stub(asset, start_time_ms=None, end_time_ms=None, limit=100):
            return (
                [
                    # Pre-window padding: wildly different close.
                    _bar(start - timedelta(days=1), 50_000),
                    _bar(start, 80_000),
                    _bar(start + timedelta(days=2), 76_000),
                    # Post-window padding: also wildly different.
                    _bar(start + timedelta(days=3), 130_000),
                ],
                "sosovalue",
            )

        monkeypatch.setattr(
            "etfpulse.pipeline.detectors.divergence.get_daily_klines_with_source",
            _stub,
        )

        hits = await DivergenceDetector(lookback_days=3).detect(db_session)
        btc = [h for h in hits if h.asset == "BTC"]
        assert len(btc) == 1
        td = btc[0].trigger_data
        # Endpoints are the IN-WINDOW closes, not the padding bars.
        assert Decimal(td["price_at_window_start"]) == Decimal("80000")
        assert Decimal(td["price_at_window_end"]) == Decimal("76000")
        # Algebra invariant: pct from trigger_data is internally consistent.
        start_close = Decimal(td["price_at_window_start"])
        end_close = Decimal(td["price_at_window_end"])
        expected_pct = abs(end_close - start_close) / start_close
        assert Decimal(td["price_change_pct"]) == expected_pct

    async def test_constructor_overrides_bypass_settings(self, db_session, monkeypatch):
        """Constructor kwargs override the global defaults — used by tighter
        configs (operator wants 5% / $1B floor) or looser ones (test fixtures
        with small synthetic numbers)."""
        start = date(2026, 4, 18)
        # $10 total. Would be filtered by ANY non-zero flow floor.
        _seed_flows(db_session, [Decimal("3"), Decimal("4"), Decimal("3")], start=start)
        await db_session.flush()

        _patch_klines(monkeypatch, start=start, start_close=80_000, end_close=64_000)

        # Lower both floors enough that this synthetic case fires.
        detector = DivergenceDetector(
            lookback_days=3,
            min_price_change_pct=0.10,
            min_flow_sum_usd=Decimal("1"),
        )
        hits = await detector.detect(db_session)
        assert len([h for h in hits if h.asset == "BTC"]) == 1

    async def test_exactly_at_price_floor_fires(self, db_session, monkeypatch):
        """Boundary case — `price_change_pct >= min_price_change_pct` is
        inclusive. Exactly 2% on the 2% floor fires (the gate is `<`, not
        `<=`). Pins the inequality direction."""
        start = date(2026, 4, 18)
        _seed_flows(
            db_session,
            [Decimal("100_000_000"), Decimal("200_000_000"), Decimal("100_000_000")],
            start=start,
        )
        await db_session.flush()

        # Exactly 2% drop: 80_000 → 78_400.
        _patch_klines(monkeypatch, start=start, start_close=80_000, end_close=78_400)

        hits = await DivergenceDetector(lookback_days=3).detect(db_session)
        assert len([h for h in hits if h.asset == "BTC"]) == 1

    async def test_exactly_at_flow_floor_fires(self, db_session, monkeypatch):
        """Boundary — `abs(flow_sum) >= min_flow_sum_usd` is inclusive."""
        start = date(2026, 4, 18)
        # Sum = $300M = default floor exactly.
        _seed_flows(
            db_session,
            [Decimal("100_000_000"), Decimal("100_000_000"), Decimal("100_000_000")],
            start=start,
        )
        await db_session.flush()

        _patch_klines(monkeypatch, start=start, start_close=80_000, end_close=72_000)

        hits = await DivergenceDetector(lookback_days=3).detect(db_session)
        assert len([h for h in hits if h.asset == "BTC"]) == 1

    async def test_zero_start_close_returns_no_hit(self, db_session, monkeypatch):
        """Defensive — a provider returning a zero close at the window start
        would zero-divide on `abs(price_change) / price_at_start`. The
        detector must skip rather than crash.

        Setup nuance: the zero-divide guard runs ONLY after the directional
        check passes. With positive flows + positive price_change the
        directional check exits first and the guard never executes — the
        test would pass for the wrong reason. So we use NEGATIVE flows
        (institutional selling) paired with a rising price (start_close=0
        → end_close=70_000), which IS divergence directionally and reaches
        the magnitude block where the guard sits.
        """
        start = date(2026, 4, 18)
        _seed_flows(
            db_session,
            # All-negative flows — divergence with a rising price would
            # normally fire if magnitudes cleared.
            [Decimal("-100_000_000"), Decimal("-200_000_000"), Decimal("-100_000_000")],
            start=start,
        )
        await db_session.flush()

        async def _stub(asset, start_time_ms=None, end_time_ms=None, limit=100):
            return (
                [
                    _bar(start - timedelta(days=1), 0),  # zero pad
                    _bar(start, 0),  # zero start_close — poison
                    _bar(start + timedelta(days=2), 70_000),
                ],
                "sosovalue",
            )

        monkeypatch.setattr(
            "etfpulse.pipeline.detectors.divergence.get_daily_klines_with_source",
            _stub,
        )

        # Must not raise (pre-fix: assertion error or ZeroDivisionError on
        # the pct calc; with the new graceful skip: returns no hit).
        hits = await DivergenceDetector(lookback_days=3).detect(db_session)
        assert [h for h in hits if h.asset == "BTC"] == []

    async def test_negative_flow_negative_price_path_also_gated(self, db_session, monkeypatch):
        """The magnitude check must apply symmetrically to the
        flow_neg_price_pos path (institutional selling despite rising price).
        Pre-PR-F.2 a 0.5% price rise on $50M outflows would have fired."""
        start = date(2026, 4, 18)
        # All-negative flows, $60M total → below $300M floor.
        _seed_flows(
            db_session,
            [Decimal("-20_000_000"), Decimal("-20_000_000"), Decimal("-20_000_000")],
            start=start,
        )
        await db_session.flush()

        # Price RISES (sign mismatch with negative flows), but only 0.5%.
        _patch_klines(monkeypatch, start=start, start_close=80_000, end_close=80_400)

        hits = await DivergenceDetector(lookback_days=3).detect(db_session)
        assert [h for h in hits if h.asset == "BTC"] == []
