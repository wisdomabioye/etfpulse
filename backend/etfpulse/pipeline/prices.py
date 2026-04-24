"""Price composition — primary (SoSoValue) with Binance fallback.

Single concern of this module: hide the choice of price source from
signal_builder and the backfill script. Callers ask for a price; the
composer returns `(price, source)` or `None` if both providers fail.

Why the tuple includes the source: `Signal.price_at_creation` is consumed
by Stage 08 outcome evaluation, which looks up +24h / +72h prices for
return computation. Mixing sources between creation and evaluation
introduces ~10-20bp of spurious P&L drift (SoSoValue aggregator price vs
Binance USDT pair). The source string is stuffed into `Signal.trigger_data`
so Stage 08 can pin outcome-eval to the same provider — avoiding
apples-to-oranges comparisons in the public track record.

Both providers raise their own error hierarchies; we catch the narrowest
"expected-to-fail" exception per provider (SoSoValueError, BinanceError)
and log-then-try-next rather than propagating. Anything broader (e.g.
KeyError from an unexpected response shape) surfaces as an unhandled
exception so we notice real bugs rather than silently swallowing them.

Issue #34.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Literal

import structlog

from etfpulse.adapters.binance import BinanceError, binance_client
from etfpulse.adapters.sosovalue import SoSoValueError, sosovalue_client

log = structlog.get_logger()


PriceSource = Literal["sosovalue", "binance"]

Asset = Literal["BTC", "ETH"]


async def get_spot_price_with_source(asset: Asset) -> tuple[Decimal, PriceSource] | None:
    """Fetch a live spot price for `asset`, tagging which provider it came from.

    Order of attempts:
        1. SoSoValue `/currencies/{id}/market-snapshot` — primary because it
           is the same data source that fed the detectors that produced this
           signal; stays internally consistent.
        2. Binance `/api/v3/ticker/price` — fallback that has survived every
           SoSoValue quota/rate-limit incident to date (issue #34 rationale).

    Returns None only if BOTH providers fail. Callers treat `None` as a
    recoverable gap: persist `Signal.price_at_creation = NULL` and move on;
    the backfill script can attempt again later once providers recover.
    """
    try:
        price = await sosovalue_client.get_spot_price(asset)
        return price, "sosovalue"
    except SoSoValueError as exc:
        log.warning(
            "spot_price_primary_failed",
            asset=asset,
            error_type=type(exc).__name__,
            error=str(exc),
        )

    try:
        price = await binance_client.get_spot_price(asset)
        return price, "binance"
    except BinanceError as exc:
        log.warning(
            "spot_price_fallback_failed",
            asset=asset,
            error_type=type(exc).__name__,
            error=str(exc),
        )

    log.error("spot_price_both_sources_failed", asset=asset)
    return None
