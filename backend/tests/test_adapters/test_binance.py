"""Binance adapter tests.

Fixture-mode only — every test runs offline against JSON fixtures that
mirror the verified live response shapes (captured 2026-04-24 from
`data-api.binance.vision`).
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from etfpulse.adapters.binance import BinanceClient, BinanceError, BinanceKline


class TestSpotPrice:
    async def test_btc_spot_from_fixture(self):
        """ticker/price fixture parses cleanly and round-trips as Decimal."""
        client = BinanceClient()
        price = await client.get_spot_price("BTC")
        assert isinstance(price, Decimal)
        assert price == Decimal("84120.50000000")

    async def test_eth_spot_from_fixture(self):
        client = BinanceClient()
        price = await client.get_spot_price("ETH")
        assert price == Decimal("2480.50000000")

    async def test_spot_cache_hits_on_second_call(self):
        """Second call within the TTL window must not re-read the fixture."""
        client = BinanceClient()
        first = await client.get_spot_price("BTC")
        second = await client.get_spot_price("BTC")
        assert first is second  # same Decimal instance from cache

    async def test_missing_fixture_raises(self, monkeypatch):
        """If the ticker fixture vanishes, the adapter raises BinanceError
        rather than silently returning None. Guards against fixture deletion
        masking real-API outages in tests."""
        client = BinanceClient()

        def _missing(name: str):
            return None

        monkeypatch.setattr(BinanceClient, "_load_fixture", staticmethod(_missing))
        with pytest.raises(BinanceError):
            await client.get_spot_price("BTC")


class TestKlines:
    async def test_btc_klines_parse_12_element_arrays(self):
        """Wire shape is arrays, not objects — `from_raw` handles the
        positional unpacking."""
        client = BinanceClient()
        bars = await client.get_daily_klines("BTC", limit=3)
        assert len(bars) == 3
        assert all(isinstance(b, BinanceKline) for b in bars)

    async def test_kline_close_is_decimal_and_matches_fixture(self):
        """Close price is at index 4 of the raw array, returned as Decimal."""
        client = BinanceClient()
        bars = await client.get_daily_klines("BTC", limit=3)
        # Fixture's last bar close is 84120.50000000
        assert bars[-1].close == Decimal("84120.50000000")

    async def test_kline_bar_date_is_utc(self):
        """`bar_date` converts ms-epoch open_time to a UTC calendar date."""
        client = BinanceClient()
        bars = await client.get_daily_klines("ETH", limit=3)
        # Fixture first bar open_time = 1745193600000 ms = 2025-04-21 UTC
        assert str(bars[0].bar_date) == "2025-04-21"

    async def test_klines_cache_hits_on_second_call(self):
        client = BinanceClient()
        first = await client.get_daily_klines("BTC", limit=3)
        second = await client.get_daily_klines("BTC", limit=3)
        assert first is second
