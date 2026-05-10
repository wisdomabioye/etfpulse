"""Price composer tests — SoSoValue primary + Binance fallback (issue #34)."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from etfpulse.adapters.binance import BinanceError, BinanceKline
from etfpulse.adapters.sosovalue import (
    KlinePoint,
    SoSoValueError,
    SoSoValueRateLimitError,
)
from etfpulse.pipeline.prices import (
    PriceBar,
    get_daily_klines_with_source,
    get_spot_price_with_source,
)


class TestHappyPath:
    async def test_sosovalue_success_returns_tuple_with_source(self):
        """When SoSoValue works, we never hit Binance — source tag proves it."""
        result = await get_spot_price_with_source("BTC")
        assert result is not None
        price, source = result
        assert source == "sosovalue"
        assert price == Decimal("82352.65")


class TestFallback:
    async def test_falls_back_to_binance_on_rate_limit(self, monkeypatch):
        """Rate-limit (a subclass of SoSoValueError) must trigger the fallback —
        not propagate as an unhandled exception."""
        from etfpulse.adapters.sosovalue import sosovalue_client

        async def _rate_limited(asset):
            raise SoSoValueRateLimitError("per-minute limit tripped")

        monkeypatch.setattr(sosovalue_client, "get_spot_price", _rate_limited)
        result = await get_spot_price_with_source("BTC")
        assert result is not None
        price, source = result
        assert source == "binance"
        assert price == Decimal("82352.65000000")

    async def test_falls_back_on_generic_sosovalue_error(self, monkeypatch):
        """Any SoSoValueError (base class) triggers fallback — quota, network, 5xx."""
        from etfpulse.adapters.sosovalue import sosovalue_client

        async def _broken(asset):
            raise SoSoValueError("synthetic failure")

        monkeypatch.setattr(sosovalue_client, "get_spot_price", _broken)
        result = await get_spot_price_with_source("ETH")
        assert result is not None
        assert result[1] == "binance"


class TestTotalFailure:
    async def test_both_failing_returns_none(self, monkeypatch):
        """When both providers fail, return None (not raise). Callers persist
        NULL price and let the backfill script retry later."""
        from etfpulse.adapters.binance import binance_client
        from etfpulse.adapters.sosovalue import sosovalue_client

        async def _soso_broken(asset):
            raise SoSoValueError("primary down")

        async def _binance_broken(asset):
            raise BinanceError("fallback down")

        monkeypatch.setattr(sosovalue_client, "get_spot_price", _soso_broken)
        monkeypatch.setattr(binance_client, "get_spot_price", _binance_broken)

        result = await get_spot_price_with_source("BTC")
        assert result is None

    async def test_unexpected_exception_not_swallowed(self, monkeypatch):
        """Only the *expected* adapter errors are caught — unexpected exceptions
        (KeyError, TypeError, etc.) must propagate so real bugs aren't masked."""
        from etfpulse.adapters.sosovalue import sosovalue_client

        async def _unexpected(asset):
            raise KeyError("shouldnt-happen field missing")

        monkeypatch.setattr(sosovalue_client, "get_spot_price", _unexpected)
        with pytest.raises(KeyError):
            await get_spot_price_with_source("BTC")


# ---------------------------------------------------------------------------
# PriceBar converters
# ---------------------------------------------------------------------------


class TestPriceBarConverters:
    """Both factories must produce the same data given equivalent inputs.

    The factories' job is shape adaptation — the OHLC values themselves come
    from each provider directly. If `from_sosovalue` and `from_binance`
    return different `PriceBar`s for the same numbers, downstream code (Stage
    8 outcome evaluation) sees provider-dependent behavior, defeating the
    point of having one neutral type.
    """

    def test_from_sosovalue_preserves_all_fields(self):
        kp = KlinePoint(
            timestamp=1745366400000,  # 2025-04-23 UTC midnight
            open=Decimal("83000"),
            high=Decimal("85000"),
            low=Decimal("82500"),
            close=Decimal("84120.50"),
            volume=Decimal("123.45"),
        )
        bar = PriceBar.from_sosovalue(kp)
        assert bar.timestamp_ms == 1745366400000
        assert bar.open == Decimal("83000")
        assert bar.high == Decimal("85000")
        assert bar.low == Decimal("82500")
        assert bar.close == Decimal("84120.50")
        assert bar.bar_date == date(2025, 4, 23)

    def test_from_binance_preserves_all_fields(self):
        bk = BinanceKline.from_raw(
            [
                1745366400000,  # open_time — 2025-04-23 UTC midnight
                "83000",
                "85000",
                "82500",
                "84120.50000000",
                "123.45",
                1745452799999,  # close_time
                "0",  # quote_asset_volume (ignored)
                0,  # trades (ignored)
                "0",  # taker_buy_base (ignored)
                "0",  # taker_buy_quote (ignored)
                "0",  # ignore field
            ]
        )
        bar = PriceBar.from_binance(bk)
        assert bar.timestamp_ms == 1745366400000
        assert bar.open == Decimal("83000")
        assert bar.high == Decimal("85000")
        assert bar.low == Decimal("82500")
        assert bar.close == Decimal("84120.50000000")
        # bar_date pins to open_time — must match SoSoValue semantics so a
        # signal looked up by date matches regardless of provider.
        assert bar.bar_date == date(2025, 4, 23)

    def test_both_factories_produce_equivalent_bar_date(self):
        """The whole point of `bar_date` being source-neutral: same wall-clock
        day must produce the same `bar_date` regardless of provider."""
        ts = 1745366400000
        kp = KlinePoint(
            timestamp=ts,
            open=Decimal("1"),
            high=Decimal("1"),
            low=Decimal("1"),
            close=Decimal("1"),
        )
        bk = BinanceKline.from_raw(
            [ts, "1", "1", "1", "1", "0", ts + 86_399_999, "0", 0, "0", "0", "0"]
        )
        assert PriceBar.from_sosovalue(kp).bar_date == PriceBar.from_binance(bk).bar_date


# ---------------------------------------------------------------------------
# get_daily_klines_with_source
# ---------------------------------------------------------------------------


class TestKlinesComposer:
    async def test_sosovalue_success_returns_bars_tagged_sosovalue(self):
        """Fixture-backed: SoSoValue happy path. Fixture has 90 bars (limit is
        ignored under fixture mode — it only shapes the upstream HTTP call)."""
        result = await get_daily_klines_with_source("BTC", limit=3)
        assert result is not None
        bars, source = result
        assert source == "sosovalue"
        assert len(bars) == 90
        # Last bar's close matches the fixture (anchored value used elsewhere).
        assert bars[-1].close == Decimal("82220.14")

    async def test_falls_back_to_binance_on_error(self, monkeypatch):
        """SoSoValueError → composer must try Binance, return source='binance'."""
        from etfpulse.adapters.sosovalue import sosovalue_client

        async def _broken(asset, start_time_ms=None, end_time_ms=None, limit=100):
            raise SoSoValueError("simulated outage")

        monkeypatch.setattr(sosovalue_client, "get_daily_klines", _broken)
        result = await get_daily_klines_with_source("BTC", limit=3)
        assert result is not None
        bars, source = result
        assert source == "binance"
        assert len(bars) > 0

    async def test_falls_back_when_primary_returns_empty(self, monkeypatch):
        """Successful empty response from primary still triggers fallback —
        an empty list is no more useful than a failure to the caller."""
        from etfpulse.adapters.sosovalue import sosovalue_client

        async def _empty(asset, start_time_ms=None, end_time_ms=None, limit=100):
            return []

        monkeypatch.setattr(sosovalue_client, "get_daily_klines", _empty)
        result = await get_daily_klines_with_source("BTC", limit=3)
        assert result is not None
        _, source = result
        assert source == "binance"

    async def test_both_failing_returns_none(self, monkeypatch):
        from etfpulse.adapters.binance import binance_client
        from etfpulse.adapters.sosovalue import sosovalue_client

        async def _soso_broken(asset, start_time_ms=None, end_time_ms=None, limit=100):
            raise SoSoValueError("primary down")

        async def _binance_broken(asset, start_time_ms=None, end_time_ms=None, limit=100):
            raise BinanceError("fallback down")

        monkeypatch.setattr(sosovalue_client, "get_daily_klines", _soso_broken)
        monkeypatch.setattr(binance_client, "get_daily_klines", _binance_broken)

        result = await get_daily_klines_with_source("BTC", limit=3)
        assert result is None

    async def test_rate_limit_subclass_triggers_fallback(self, monkeypatch):
        """Rate-limit is a SoSoValueError subclass; must follow the same
        catch-and-fallback path as the base error."""
        from etfpulse.adapters.sosovalue import sosovalue_client

        async def _rate_limited(asset, start_time_ms=None, end_time_ms=None, limit=100):
            raise SoSoValueRateLimitError("per-minute limit tripped")

        monkeypatch.setattr(sosovalue_client, "get_daily_klines", _rate_limited)
        result = await get_daily_klines_with_source("BTC", limit=3)
        assert result is not None
        assert result[1] == "binance"

    async def test_unexpected_exception_not_swallowed(self, monkeypatch):
        """Same contract as the spot-price composer — only the documented
        adapter errors are caught."""
        from etfpulse.adapters.sosovalue import sosovalue_client

        async def _unexpected(asset, start_time_ms=None, end_time_ms=None, limit=100):
            raise KeyError("schema drift")

        monkeypatch.setattr(sosovalue_client, "get_daily_klines", _unexpected)
        with pytest.raises(KeyError):
            await get_daily_klines_with_source("BTC", limit=3)
