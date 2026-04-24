"""Price composer tests — SoSoValue primary + Binance fallback (issue #34)."""

from __future__ import annotations

from decimal import Decimal

import pytest

from etfpulse.adapters.binance import BinanceError
from etfpulse.adapters.sosovalue import SoSoValueError, SoSoValueRateLimitError
from etfpulse.pipeline.prices import get_spot_price_with_source


class TestHappyPath:
    async def test_sosovalue_success_returns_tuple_with_source(self):
        """When SoSoValue works, we never hit Binance — source tag proves it."""
        result = await get_spot_price_with_source("BTC")
        assert result is not None
        price, source = result
        assert source == "sosovalue"
        assert price == Decimal("84120.50")


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
        assert price == Decimal("84120.50000000")

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
