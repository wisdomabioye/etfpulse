"""`resolve_symbol_id` — cache lookup against `sodex_symbols`."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from etfpulse.models import SodexSymbol, Venue
from etfpulse.pipeline.execution.symbols import (
    SymbolNotResolved,
    resolve_symbol_id,
)


def _make_symbol(
    *,
    venue: str,
    name: str,
    asset: str,
    symbol_id: int,
    refreshed_at: datetime | None = None,
) -> SodexSymbol:
    return SodexSymbol(
        venue=venue,
        symbol_id=symbol_id,
        name=name,
        asset=asset,
        raw={"name": name, "id": symbol_id},
        refreshed_at=refreshed_at or datetime.now(UTC),
    )


class TestResolveHappyPath:
    async def test_resolves_btc_spot(self, db_session):
        db_session.add(
            _make_symbol(venue=Venue.SODEX_SPOT.value, name="vBTC_vUSDC", asset="BTC", symbol_id=1)
        )
        await db_session.flush()

        sid = await resolve_symbol_id(db_session, Venue.SODEX_SPOT.value, "BTC")
        assert sid == 1

    async def test_resolves_eth_perps_separate_from_spot(self, db_session):
        """Same asset on different venues — disambiguated by `venue` filter."""
        db_session.add(
            _make_symbol(venue=Venue.SODEX_SPOT.value, name="vETH_vUSDC", asset="ETH", symbol_id=2)
        )
        db_session.add(
            _make_symbol(
                venue=Venue.SODEX_PERPS.value, name="vETH_vUSDC", asset="ETH", symbol_id=42
            )
        )
        await db_session.flush()

        assert await resolve_symbol_id(db_session, Venue.SODEX_SPOT.value, "ETH") == 2
        assert await resolve_symbol_id(db_session, Venue.SODEX_PERPS.value, "ETH") == 42


class TestResolveMisses:
    async def test_missing_raises_symbol_not_resolved(self, db_session):
        # Cache is empty (clean DB).
        with pytest.raises(SymbolNotResolved) as exc_info:
            await resolve_symbol_id(db_session, Venue.SODEX_SPOT.value, "BTC")
        # Carries the requested (venue, asset) for operator-actionable logs.
        assert exc_info.value.venue == Venue.SODEX_SPOT.value
        assert exc_info.value.asset == "BTC"

    async def test_wrong_venue_raises(self, db_session):
        """BTC on spot exists; asking for BTC on perps misses."""
        db_session.add(
            _make_symbol(venue=Venue.SODEX_SPOT.value, name="vBTC_vUSDC", asset="BTC", symbol_id=1)
        )
        await db_session.flush()

        with pytest.raises(SymbolNotResolved):
            await resolve_symbol_id(db_session, Venue.SODEX_PERPS.value, "BTC")

    async def test_wrong_asset_raises(self, db_session):
        db_session.add(
            _make_symbol(venue=Venue.SODEX_SPOT.value, name="vBTC_vUSDC", asset="BTC", symbol_id=1)
        )
        await db_session.flush()

        with pytest.raises(SymbolNotResolved):
            await resolve_symbol_id(db_session, Venue.SODEX_SPOT.value, "SOL")


class TestMultiQuoteDisambiguation:
    """V1 limitation: multi-quote ties resolve by `refreshed_at DESC`. The
    behaviour is documented (module docstring) and tested here so a
    future change to disambiguate by `quote` doesn't silently break."""

    async def test_freshest_match_wins(self, db_session):
        now = datetime.now(UTC)
        # Older entry: BTC/USDT
        db_session.add(
            _make_symbol(
                venue=Venue.SODEX_SPOT.value,
                name="vBTC_vUSDT",
                asset="BTC",
                symbol_id=99,
                refreshed_at=now - timedelta(hours=1),
            )
        )
        # Fresher entry: BTC/USDC
        db_session.add(
            _make_symbol(
                venue=Venue.SODEX_SPOT.value,
                name="vBTC_vUSDC",
                asset="BTC",
                symbol_id=1,
                refreshed_at=now,
            )
        )
        await db_session.flush()

        sid = await resolve_symbol_id(db_session, Venue.SODEX_SPOT.value, "BTC")
        # Freshest wins (vBTC_vUSDC, id=1).
        assert sid == 1
