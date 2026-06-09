"""Tests for `pipeline.symbols_refresh`."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from etfpulse.adapters.sodex.perps_client import SodexPerpsClient
from etfpulse.adapters.sodex.responses import PerpsSymbol, SpotSymbol
from etfpulse.adapters.sodex.spot_client import SodexSpotClient
from etfpulse.models import SodexSymbol, Venue
from etfpulse.pipeline.symbols_refresh import (
    extract_asset_from_symbol_name,
    refresh_sodex_symbols,
)

# ---------------------------------------------------------------------------
# Asset extraction
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "name,expected",
    [
        ("vBTC_vUSDC", "BTC"),
        ("vETH_vUSDC", "ETH"),
        ("vSOL_vUSDT", "SOL"),
        ("VBTC_vUSDC", "BTC"),  # uppercase leading V
        ("vbtc_vusdc", "btc"),  # lowercase-only — preserved after strip
        ("BTC_USDC", "BTC"),  # no leading v
        ("vvBTC_vUSDC", "BTC"),  # double-v stripped
        # Perps `<BASE>-<QUOTE>` naming — the quote suffix MUST be stripped
        # so the asset is the canonical base the oracle/scoring key on.
        ("BTC-USD", "BTC"),
        ("ETH-USD", "ETH"),
        ("SOL-USD", "SOL"),
        ("XAUT-USD", "XAUT"),  # multi-char base, no leading v
        ("NATGAS-USD", "NATGAS"),
    ],
)
def test_extract_asset_from_symbol_name(name, expected):
    assert extract_asset_from_symbol_name(name) == expected


def test_extract_asset_perps_dash_only_quote_raises():
    # `-USD` has no base segment before the dash → malformed → raise.
    with pytest.raises(ValueError):
        extract_asset_from_symbol_name("-USD")


def test_extract_asset_empty_raises():
    with pytest.raises(ValueError):
        extract_asset_from_symbol_name("")


# ---------------------------------------------------------------------------
# Refresh — full flow with mock clients
# ---------------------------------------------------------------------------


def _make_spot_symbol(*, id_: int, name: str) -> SpotSymbol:
    """Build a SpotSymbol via `model_construct` (bypass validation).
    SpotSymbol has 20+ required fields per the wire shape; refresh
    logic only reads `id` + `name`, so leaving the rest unset is
    fine for these tests. `model_construct` is Pydantic v2's
    documented escape hatch for this purpose."""
    return SpotSymbol.model_construct(id=id_, name=name)


def _make_perps_symbol(*, id_: int, name: str) -> PerpsSymbol:
    return PerpsSymbol.model_construct(id=id_, name=name)


def _make_clients(*, spot_symbols, perps_symbols):
    spot = AsyncMock(spec=SodexSpotClient)
    spot.get_symbols = AsyncMock(return_value=spot_symbols)
    perps = AsyncMock(spec=SodexPerpsClient)
    perps.get_symbols = AsyncMock(return_value=perps_symbols)
    return spot, perps


async def test_first_refresh_inserts_rows(db_session):
    spot, perps = _make_clients(
        spot_symbols=[
            _make_spot_symbol(id_=1, name="vBTC_vUSDC"),
            _make_spot_symbol(id_=2, name="vETH_vUSDC"),
        ],
        perps_symbols=[
            _make_perps_symbol(id_=3, name="vBTC_vUSDC"),
        ],
    )

    summary = await refresh_sodex_symbols(db_session, spot_client=spot, perps_client=perps)
    assert summary["spot_inserted"] == 2
    assert summary["perps_inserted"] == 1
    assert summary["errors"] == 0

    rows = (await db_session.execute(select(SodexSymbol))).scalars().all()
    by_key = {(r.venue, r.name): r for r in rows}
    assert by_key[(Venue.SODEX_SPOT.value, "vBTC_vUSDC")].symbol_id == 1
    assert by_key[(Venue.SODEX_SPOT.value, "vBTC_vUSDC")].asset == "BTC"
    assert by_key[(Venue.SODEX_PERPS.value, "vBTC_vUSDC")].symbol_id == 3


async def test_second_refresh_updates_existing(db_session):
    """Same (venue, name) on re-refresh: updates symbol_id (rare but
    possible if the venue re-keys) + refreshed_at."""
    spot, perps = _make_clients(
        spot_symbols=[_make_spot_symbol(id_=1, name="vBTC_vUSDC")],
        perps_symbols=[],
    )
    summary = await refresh_sodex_symbols(db_session, spot_client=spot, perps_client=perps)
    assert summary["spot_inserted"] == 1

    # Re-refresh with same name but different ID.
    spot2, perps2 = _make_clients(
        spot_symbols=[_make_spot_symbol(id_=999, name="vBTC_vUSDC")],
        perps_symbols=[],
    )
    summary2 = await refresh_sodex_symbols(db_session, spot_client=spot2, perps_client=perps2)
    assert summary2["spot_inserted"] == 0
    assert summary2["spot_updated"] == 1

    row = (
        await db_session.execute(select(SodexSymbol).where(SodexSymbol.name == "vBTC_vUSDC"))
    ).scalar_one()
    assert row.symbol_id == 999


async def test_spot_fetch_failure_does_not_block_perps(db_session):
    """One-venue HTTP failure → that venue contributes nothing; the
    other still refreshes."""
    spot = AsyncMock(spec=SodexSpotClient)
    spot.get_symbols = AsyncMock(side_effect=RuntimeError("spot down"))
    perps = AsyncMock(spec=SodexPerpsClient)
    perps.get_symbols = AsyncMock(return_value=[_make_perps_symbol(id_=42, name="vETH_vUSDC")])

    summary = await refresh_sodex_symbols(db_session, spot_client=spot, perps_client=perps)
    assert summary["spot_inserted"] == 0
    assert summary["perps_inserted"] == 1
    assert summary["errors"] == 1
