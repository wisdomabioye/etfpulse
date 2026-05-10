"""Tests for pipeline.price_backfill — covers the helpers + the
end-to-end orchestrator with `get_daily_klines_with_source` stubbed.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal

import pytest

from etfpulse.models import Signal, SignalStatus
from etfpulse.pipeline.detectors.base import compute_fingerprint
from etfpulse.pipeline.price_backfill import (
    backfill_null_prices,
    date_to_ms,
    fetch_closes_by_date,
    group_by_asset,
    match_close,
)

# ---------------------------------------------------------------------------
# Pure helpers — no DB, no network
# ---------------------------------------------------------------------------


class TestDateToMs:
    def test_returns_utc_midnight_ms(self):
        # 2025-01-01 UTC midnight = 1735689600000 ms (verified via
        # `datetime(2025,1,1,tzinfo=UTC).timestamp()*1000`).
        assert date_to_ms(date(2025, 1, 1)) == 1735689600000

    def test_is_timezone_pinned(self):
        """Same calendar date → same ms regardless of host TZ. Pinned via
        UTC tzinfo on the datetime, not naive. Cross-check against an
        independent UTC computation rather than a hardcoded magic number."""
        d = date(2026, 5, 7)
        expected = int(datetime(d.year, d.month, d.day, tzinfo=UTC).timestamp() * 1000)
        assert date_to_ms(d) == expected


class TestMatchClose:
    def test_direct_hit(self):
        closes = {date(2026, 5, 7): Decimal("100"), date(2026, 5, 8): Decimal("110")}
        assert match_close(closes, date(2026, 5, 8)) == Decimal("110")

    def test_walks_back_one_day_for_weekend_gap(self):
        """Crypto trades 24/7, but providers occasionally miss a bar.
        Stepping back one day must find the prior close."""
        closes = {date(2026, 5, 6): Decimal("100")}
        assert match_close(closes, date(2026, 5, 7)) == Decimal("100")

    def test_walks_back_up_to_five_days(self):
        closes = {date(2026, 5, 2): Decimal("99")}
        assert match_close(closes, date(2026, 5, 7)) == Decimal("99")

    def test_returns_none_if_gap_exceeds_window(self):
        """Beyond _MAX_LOOKBACK_DAYS, no match — caller must keep NULL."""
        closes = {date(2026, 5, 1): Decimal("99")}
        assert match_close(closes, date(2026, 5, 7)) is None

    def test_returns_none_on_empty_closes(self):
        assert match_close({}, date(2026, 5, 7)) is None

    def test_does_not_walk_forward(self):
        """We never pick a future bar — `signal_date` defines the reference
        moment; future closes are by definition not "the price on this day"."""
        closes = {date(2026, 5, 8): Decimal("110")}
        assert match_close(closes, date(2026, 5, 7)) is None


class TestGroupByAsset:
    def _mk(self, asset: str) -> Signal:
        return Signal(
            signal_type="flow_anomaly",
            asset=asset,
            trigger_data={},
            status=SignalStatus.PENDING.value,
            fingerprint=compute_fingerprint(asset, "group-test", asset),
            signal_date=date(2026, 5, 7),
        )

    def test_buckets_btc_and_eth(self):
        sigs = [self._mk("BTC"), self._mk("BTC"), self._mk("ETH")]
        grouped = group_by_asset(sigs)
        assert len(grouped["BTC"]) == 2
        assert len(grouped["ETH"]) == 1

    def test_silently_drops_unsupported_assets(self):
        """Future asset like 'SOL' shouldn't crash a rescue script. The
        caller's summary surfaces the count via subtraction (input vs
        sum-of-buckets)."""
        sigs = [self._mk("BTC"), self._mk("SOL"), self._mk("ETH")]
        grouped = group_by_asset(sigs)
        assert sum(len(v) for v in grouped.values()) == 2

    def test_returns_both_keys_even_on_empty_input(self):
        """Stable shape — caller can iterate `grouped.items()` without
        worrying about KeyError."""
        grouped = group_by_asset([])
        assert set(grouped.keys()) == {"BTC", "ETH"}


# ---------------------------------------------------------------------------
# fetch_closes_by_date — stub the underlying provider call
# ---------------------------------------------------------------------------


class _FakeBar:
    def __init__(self, bar_date: date, close: Decimal):
        self.bar_date = bar_date
        self.close = close


class TestFetchClosesByDate:
    async def test_translates_bars_to_close_dict(self, monkeypatch):
        bars = [
            _FakeBar(date(2026, 5, 6), Decimal("100")),
            _FakeBar(date(2026, 5, 7), Decimal("110")),
        ]

        async def _stub(asset, start_time_ms=None, end_time_ms=None, limit=100):
            return bars, "sosovalue"

        monkeypatch.setattr("etfpulse.pipeline.price_backfill.get_daily_klines_with_source", _stub)

        result = await fetch_closes_by_date("BTC", date(2026, 5, 6), date(2026, 5, 7))
        assert result is not None
        closes, source = result
        assert source == "sosovalue"
        assert closes == {date(2026, 5, 6): Decimal("100"), date(2026, 5, 7): Decimal("110")}

    async def test_returns_none_when_both_providers_fail(self, monkeypatch):
        async def _stub(asset, start_time_ms=None, end_time_ms=None, limit=100):
            return None

        monkeypatch.setattr("etfpulse.pipeline.price_backfill.get_daily_klines_with_source", _stub)
        assert await fetch_closes_by_date("BTC", date(2026, 5, 6), date(2026, 5, 7)) is None

    async def test_pads_window_by_one_day(self, monkeypatch):
        """Verify ±1 day padding is applied — boundary signal_dates with
        slight provider TZ differences still get covered."""
        captured = {}

        async def _stub(asset, start_time_ms=None, end_time_ms=None, limit=100):
            captured["start_ms"] = start_time_ms
            captured["end_ms"] = end_time_ms
            captured["limit"] = limit
            return [], "sosovalue"

        monkeypatch.setattr("etfpulse.pipeline.price_backfill.get_daily_klines_with_source", _stub)
        await fetch_closes_by_date("BTC", date(2026, 5, 7), date(2026, 5, 7))

        assert captured["start_ms"] == date_to_ms(date(2026, 5, 6))
        assert captured["end_ms"] == date_to_ms(date(2026, 5, 8))


# ---------------------------------------------------------------------------
# Orchestrator — runs against the real test DB; provider stubbed.
# ---------------------------------------------------------------------------


@pytest.fixture
def stub_klines_btc_eth(monkeypatch):
    """Return canned closes around a known date, distinct per asset so we
    can verify rows are bucketed correctly."""
    btc_close = Decimal("84000")
    eth_close = Decimal("2480")

    async def _stub(asset, start_time_ms=None, end_time_ms=None, limit=100):
        bars = [
            _FakeBar(date(2025, 4, 22), Decimal("0")),  # placeholder day-before
            _FakeBar(date(2025, 4, 23), btc_close if asset == "BTC" else eth_close),
        ]
        return bars, "sosovalue"

    monkeypatch.setattr("etfpulse.pipeline.price_backfill.get_daily_klines_with_source", _stub)


async def _make_signal(db_session, *, asset: str, fp_seed: str, signal_date: date) -> Signal:
    sig = Signal(
        signal_type="flow_anomaly",
        asset=asset,
        trigger_data={},
        status=SignalStatus.PENDING.value,
        fingerprint=compute_fingerprint(asset, "backfill-test", fp_seed),
        signal_date=signal_date,
    )
    db_session.add(sig)
    await db_session.flush()
    return sig


class TestBackfillNullPrices:
    async def test_no_op_when_table_empty(self, db_session):
        summary = await backfill_null_prices(db_session)
        assert summary == {
            "candidates": 0,
            "updated": 0,
            "skipped_no_bar": 0,
            "skipped_fetch_fail": 0,
            "skipped_unsupported_asset": 0,
        }

    async def test_fills_btc_and_eth(self, db_session, stub_klines_btc_eth):
        await _make_signal(db_session, asset="BTC", fp_seed="b1", signal_date=date(2025, 4, 23))
        await _make_signal(db_session, asset="ETH", fp_seed="e1", signal_date=date(2025, 4, 23))

        summary = await backfill_null_prices(db_session)
        assert summary["candidates"] == 2
        assert summary["updated"] == 2
        assert summary["skipped_no_bar"] == 0
        assert summary["skipped_fetch_fail"] == 0

        from sqlalchemy import select

        rows = (await db_session.execute(select(Signal).order_by(Signal.id))).scalars().all()
        prices = {r.asset: r.price_at_creation for r in rows}
        assert prices["BTC"] == Decimal("84000")
        assert prices["ETH"] == Decimal("2480")
        assert all(r.price_source == "sosovalue" for r in rows)

    async def test_idempotent_on_already_filled_rows(self, db_session, stub_klines_btc_eth):
        """Already-filled rows are excluded by the NULL-only SELECT."""
        sig = await _make_signal(
            db_session, asset="BTC", fp_seed="filled", signal_date=date(2025, 4, 23)
        )
        sig.price_at_creation = Decimal("99999")  # pre-filled — must NOT be touched
        sig.price_source = "binance"
        await db_session.flush()

        summary = await backfill_null_prices(db_session)
        assert summary["candidates"] == 0
        assert summary["updated"] == 0

        await db_session.refresh(sig)
        assert sig.price_at_creation == Decimal("99999")
        assert sig.price_source == "binance"

    async def test_skipped_no_bar_when_signal_date_too_old(self, db_session, stub_klines_btc_eth):
        """Signal_date much earlier than any returned bar → no match → leaves NULL."""
        sig = await _make_signal(
            db_session, asset="BTC", fp_seed="ancient", signal_date=date(2024, 1, 1)
        )

        summary = await backfill_null_prices(db_session)
        assert summary["candidates"] == 1
        assert summary["updated"] == 0
        assert summary["skipped_no_bar"] == 1

        await db_session.refresh(sig)
        assert sig.price_at_creation is None

    async def test_skipped_fetch_fail_when_both_providers_down(self, db_session, monkeypatch):
        """`get_daily_klines_with_source` returning None → all signals
        for that asset are counted as fetch-fail."""

        async def _none(asset, start_time_ms=None, end_time_ms=None, limit=100):
            return None

        monkeypatch.setattr("etfpulse.pipeline.price_backfill.get_daily_klines_with_source", _none)

        await _make_signal(db_session, asset="BTC", fp_seed="b1", signal_date=date(2025, 4, 23))
        await _make_signal(db_session, asset="BTC", fp_seed="b2", signal_date=date(2025, 4, 24))

        summary = await backfill_null_prices(db_session)
        assert summary["candidates"] == 2
        assert summary["updated"] == 0
        assert summary["skipped_fetch_fail"] == 2

    async def test_unsupported_asset_counted_separately(self, db_session, stub_klines_btc_eth):
        """A signal with an asset string outside {BTC, ETH} is dropped from
        the per-asset buckets and surfaced as skipped_unsupported_asset.
        Pre-Stage-12 detector universe is BTC+ETH only, but defensive code
        keeps the rescue script useful as the universe widens (#97)."""
        await _make_signal(db_session, asset="SOL", fp_seed="sol", signal_date=date(2025, 4, 23))
        await _make_signal(db_session, asset="BTC", fp_seed="btc", signal_date=date(2025, 4, 23))

        summary = await backfill_null_prices(db_session)
        assert summary["candidates"] == 2
        assert summary["updated"] == 1
        assert summary["skipped_unsupported_asset"] == 1
