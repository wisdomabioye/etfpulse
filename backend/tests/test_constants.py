"""Constants integrity tests.

`SUPPORTED_ASSETS` is the asset universe ETFPulse processes. Several
type-level Literal mirrors (`pipeline.prices.Asset`,
`api.schemas.signals.AssetLiteral`, frontend `AssetSymbol`) must agree
with it by content. Python's `typing.Literal` and TypeScript's literal
unions can't derive their members from a tuple at the type level, so
the duplication is mechanical — these tests catch drift before runtime.
"""

from __future__ import annotations

import typing

from etfpulse.api.schemas.signals import AssetLiteral
from etfpulse.bot.constants import VALID_ASSETS
from etfpulse.constants import MARKET_ASSET, SUPPORTED_ASSETS
from etfpulse.pipeline.prices import Asset


def _literal_args(literal_type: object) -> tuple[str, ...]:
    """Extract the string members of a `Literal[...]` alias."""
    return tuple(str(a) for a in typing.get_args(literal_type))


class TestAssetAlignment:
    def test_bot_valid_assets_is_supported_assets(self):
        """`VALID_ASSETS` is intentionally a re-export — `is`, not just
        `==`, so the constant identity is preserved and a future
        `VALID_ASSETS = ("BTC", "ETH", "SOL")` redefinition would
        immediately stand out as not-the-shared-constant."""
        assert VALID_ASSETS is SUPPORTED_ASSETS

    def test_pipeline_asset_literal_matches(self):
        """`pipeline.prices.Asset = Literal["BTC", "ETH"]` MUST mirror
        SUPPORTED_ASSETS member-for-member. The Literal can't be derived
        programmatically, so this test is the runtime guard against the
        two diverging when a new asset lands."""
        assert _literal_args(Asset) == SUPPORTED_ASSETS

    def test_api_asset_literal_matches(self):
        """`api.schemas.signals.AssetLiteral` is the public API filter
        contract — exposed through frontend `AssetSymbol`. Intentionally
        broader than `SUPPORTED_ASSETS`: it accepts the `MARKET` sentinel
        (PR F.3 — regime_shift signals) as a filter value even though
        MARKET is not a tradeable/priceable asset. The pipeline-side
        `Asset` Literal stays narrow because pricing genuinely is only
        BTC/ETH."""
        assert _literal_args(AssetLiteral) == SUPPORTED_ASSETS + (MARKET_ASSET,)
