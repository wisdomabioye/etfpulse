"""Price composition — primary (SoSoValue) with Binance fallback.

Single concern of this module: hide the choice of price source from
signal_builder and the backfill script. Callers ask for a price (spot or
historical bars); the composer returns `(value, source)` or `None` if both
providers fail.

Why the tuple includes the source: `Signal.price_at_creation` is consumed
by Stage 08 outcome evaluation, which looks up +24h / +72h prices for
return computation. Mixing sources between creation and evaluation
introduces ~10-20bp of spurious P&L drift (SoSoValue aggregator price vs
Binance USDT pair). The source string is persisted on `Signal.price_source`
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

from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Literal

import structlog

from etfpulse.adapters.binance import BinanceError, BinanceKline, binance_client
from etfpulse.adapters.sosovalue import (
    KlinePoint,
    SoSoValueError,
    sosovalue_client,
)

log = structlog.get_logger()


PriceSource = Literal["sosovalue", "binance"]

Asset = Literal["BTC", "ETH"]


@dataclass(frozen=True, slots=True)
class PriceBar:
    """Source-neutral OHLC daily bar.

    Both adapters' shapes (`sosovalue.KlinePoint` and `binance.BinanceKline`)
    feed into this via the `from_*` factories so callers handle one type
    regardless of which provider answered. Construct via the factories,
    not the dataclass directly — the factories own the shape adaptation.
    """

    timestamp_ms: int
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal

    @property
    def bar_date(self) -> date:
        """UTC calendar date of the bar open. Matches `Signal.signal_date`."""
        return datetime.fromtimestamp(self.timestamp_ms / 1000, tz=UTC).date()

    @classmethod
    def from_sosovalue(cls, kp: KlinePoint) -> PriceBar:
        """Adapt a SoSoValue `KlinePoint` (timestamp/open/high/low/close)."""
        return cls(
            timestamp_ms=kp.timestamp,
            open=kp.open,
            high=kp.high,
            low=kp.low,
            close=kp.close,
        )

    @classmethod
    def from_binance(cls, bk: BinanceKline) -> PriceBar:
        """Adapt a Binance `BinanceKline` (open_time/open/high/low/close).

        We deliberately pin the bar's identity on `open_time` rather than
        `close_time` so `bar_date` matches SoSoValue's bar_date semantics.
        """
        return cls(
            timestamp_ms=bk.open_time,
            open=bk.open,
            high=bk.high,
            low=bk.low,
            close=bk.close,
        )


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


async def get_daily_klines_with_source(
    asset: Asset,
    start_time_ms: int | None = None,
    end_time_ms: int | None = None,
    limit: int = 100,
) -> tuple[list[PriceBar], PriceSource] | None:
    """Fetch daily OHLC bars for `asset`, tagging which provider answered.

    Mirrors `get_spot_price_with_source` exactly: same primary/fallback
    order, same per-provider exception family, same `None` contract on
    total failure. Each source's native kline DTO is adapted to `PriceBar`
    via the `from_*` factories so callers handle one shape regardless.

    Empty result from primary triggers fallback. A successful HTTP call
    returning `[]` is not an error, but it also gives the caller no usable
    bars — better to try the other provider before giving up.
    """
    try:
        soso = await sosovalue_client.get_daily_klines(
            asset,
            start_time_ms=start_time_ms,
            end_time_ms=end_time_ms,
            limit=limit,
        )
        if soso:
            return [PriceBar.from_sosovalue(k) for k in soso], "sosovalue"
        log.warning("klines_primary_empty", asset=asset)
    except SoSoValueError as exc:
        log.warning(
            "klines_primary_failed",
            asset=asset,
            error_type=type(exc).__name__,
            error=str(exc),
        )

    try:
        binance = await binance_client.get_daily_klines(
            asset,
            start_time_ms=start_time_ms,
            end_time_ms=end_time_ms,
            limit=limit,
        )
        if binance:
            return [PriceBar.from_binance(k) for k in binance], "binance"
        log.warning("klines_fallback_empty", asset=asset)
    except BinanceError as exc:
        log.warning(
            "klines_fallback_failed",
            asset=asset,
            error_type=type(exc).__name__,
            error=str(exc),
        )

    log.error("klines_both_sources_failed", asset=asset)
    return None


async def get_daily_klines_from_source(
    asset: Asset,
    source: PriceSource,
    *,
    start_time_ms: int | None = None,
    end_time_ms: int | None = None,
    limit: int = 100,
) -> list[PriceBar] | None:
    """Fetch daily OHLC bars from EXACTLY ONE source — no fallback.

    Stage 8-P2 outcome evaluator pins kline fetches to the same source
    that produced `Signal.price_at_creation` (recorded in `Signal.price_source`)
    so 24h/72h returns aren't compared against an apples-to-oranges price
    feed. Falling back to the other provider would re-introduce the
    micro-skew this pinning is designed to avoid.

    Returns None on:
        - the requested source raising its own error family
        (SoSoValueError / BinanceError) — caller treats as a transient
        skip and tries again next eval tick.
        - empty result from the source — same treatment as failure (no
        fallback). The signal is left unevaluated; future eval ticks
        retry once kline history catches up.

    Anything else (KeyError, network library bugs, etc.) propagates per
    the same convention as `get_daily_klines_with_source`.
    """
    if source == "sosovalue":
        try:
            soso = await sosovalue_client.get_daily_klines(
                asset,
                start_time_ms=start_time_ms,
                end_time_ms=end_time_ms,
                limit=limit,
            )
        except SoSoValueError as exc:
            log.warning(
                "klines_pinned_sosovalue_failed",
                asset=asset,
                error_type=type(exc).__name__,
                error=str(exc),
            )
            return None
        if not soso:
            log.warning("klines_pinned_sosovalue_empty", asset=asset)
            return None
        return [PriceBar.from_sosovalue(k) for k in soso]

    # source == "binance" — exhaustive over the PriceSource Literal.
    try:
        binance = await binance_client.get_daily_klines(
            asset,
            start_time_ms=start_time_ms,
            end_time_ms=end_time_ms,
            limit=limit,
        )
    except BinanceError as exc:
        log.warning(
            "klines_pinned_binance_failed",
            asset=asset,
            error_type=type(exc).__name__,
            error=str(exc),
        )
        return None
    if not binance:
        log.warning("klines_pinned_binance_empty", asset=asset)
        return None
    return [PriceBar.from_binance(k) for k in binance]
