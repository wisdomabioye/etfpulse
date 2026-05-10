"""Backfill `Signal.price_at_creation` for rows that persisted with NULL.

Why this exists:
    - Stage 4a shipped without a spot-price source — every Signal created
      before Stage 8 wired `pipeline.prices` persists with NULL price.
    - Even after Stage 8, a transient provider outage can still produce
      NULL `price_at_creation`. This module is the second-chance path.

Idempotent: only touches rows with `price_at_creation IS NULL`. Re-runs
after quota / network recovery catch what earlier runs missed.

Date-matching policy: use the close of `signal_date` itself (not
`signal_date - 1`). By the time we backfill, that day's kline is
published — its close is the most faithful "reference price on this
signal's day". If `signal_date` is missing (listing gap, weekend), walk
back up to 5 days for the nearest previous bar.

D14 contract: `backfill_null_prices(session)` does NOT commit — caller
owns the transaction. Script wrapper (`scripts/backfill_signal_prices.py`)
opens a session, calls this, commits at the end. A mid-run failure rolls
back all in-progress updates; the next run picks them up because of the
NULL-only SELECT — costs a few re-fetched klines but keeps the function
trivially testable.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import structlog
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from etfpulse.models import Signal
from etfpulse.pipeline.prices import (
    Asset,
    PriceSource,
    get_daily_klines_with_source,
)

log = structlog.get_logger()

# How far back from `signal_date` to walk looking for a kline match.
# 5 covers a long weekend + a holiday Monday with room to spare.
_MAX_LOOKBACK_DAYS = 5


def date_to_ms(d: date) -> int:
    """UTC midnight of `d` as epoch milliseconds — the param format both
    SoSoValue klines and Binance klines accept.
    """
    return int(datetime(d.year, d.month, d.day, tzinfo=UTC).timestamp() * 1000)


def match_close(closes: dict[date, Decimal], signal_date: date) -> Decimal | None:
    """Find the closest-available close on or before `signal_date`.

    Direct hit is preferred; otherwise walk back up to `_MAX_LOOKBACK_DAYS`
    days for the nearest previous bar (handles holiday / weekend gaps).
    """
    for delta in range(_MAX_LOOKBACK_DAYS + 1):
        candidate = signal_date - timedelta(days=delta)
        if candidate in closes:
            return closes[candidate]
    return None


async def fetch_closes_by_date(
    asset: Asset,
    start: date,
    end: date,
) -> tuple[dict[date, Decimal], PriceSource] | None:
    """Return `({signal_date: close_price}, source)` or None if both providers fail.

    Delegates primary/fallback selection to `get_daily_klines_with_source`
    so this script and the daily cycle cannot drift on which provider
    runs first or how errors are classified.

    `start`/`end` inclusive; widen by ±1 day to absorb timezone edge
    cases (both providers bar-open at UTC midnight, but over-fetching is
    cheaper than missing boundary days).
    """
    start_ms = date_to_ms(start - timedelta(days=1))
    end_ms = date_to_ms(end + timedelta(days=1))
    span_days = (end - start).days + 3  # padding; 500 is the API cap anyway
    limit = min(max(span_days, 10), 500)

    result = await get_daily_klines_with_source(
        asset, start_time_ms=start_ms, end_time_ms=end_ms, limit=limit
    )
    if result is None:
        return None
    bars, source = result
    closes = {bar.bar_date: bar.close for bar in bars}
    return closes, source


def group_by_asset(signals: Sequence[Signal]) -> dict[Asset, list[Signal]]:
    """Bucket signals by their (narrow Literal) asset symbol.

    Rows with an asset string outside the supported universe are silently
    dropped — backfill is best-effort, and crashing on an unknown future
    asset would make a one-shot rescue script harder to operate. The
    skipped count surfaces in the caller's summary.
    """
    by_asset: dict[Asset, list[Signal]] = {"BTC": [], "ETH": []}
    for sig in signals:
        if sig.asset == "BTC":
            by_asset["BTC"].append(sig)
        elif sig.asset == "ETH":
            by_asset["ETH"].append(sig)
    return by_asset


async def backfill_null_prices(session: AsyncSession) -> dict[str, int]:
    """Populate `Signal.price_at_creation` + `price_source` for NULL rows.

    D14: does NOT commit; caller wraps the transaction. A mid-run failure
    therefore rolls back every in-progress UPDATE — but the NULL-only
    SELECT means re-runs are idempotent and pick up where we left off.

    Returns a summary dict suitable for logging and printing verbatim:
        {
            "candidates": N,                  # rows starting with NULL
            "updated": N,                     # rows we filled
            "skipped_no_bar": N,              # signal_date has no kline match
            "skipped_fetch_fail": N,          # both providers failed
            "skipped_unsupported_asset": N,   # asset not in {BTC, ETH}
        }

    Per-asset failure (kline fetch returns None) is non-fatal: the loop
    counts the skip and moves on. Same catch-and-continue spirit as
    `run_daily_cycle`'s D13 contract, scoped to the rescue path.
    """
    summary = {
        "candidates": 0,
        "updated": 0,
        "skipped_no_bar": 0,
        "skipped_fetch_fail": 0,
        "skipped_unsupported_asset": 0,
    }

    null_signals = (
        (await session.execute(select(Signal).where(Signal.price_at_creation.is_(None))))
        .scalars()
        .all()
    )
    summary["candidates"] = len(null_signals)
    if not null_signals:
        return summary

    by_asset = group_by_asset(null_signals)
    summary["skipped_unsupported_asset"] = len(null_signals) - sum(
        len(v) for v in by_asset.values()
    )

    for asset, sigs in by_asset.items():
        if not sigs:
            continue

        dates = [s.signal_date for s in sigs]
        result = await fetch_closes_by_date(asset, min(dates), max(dates))
        if result is None:
            log.warning(
                "backfill_fetch_failed_both_providers",
                asset=asset,
                signal_count=len(sigs),
            )
            summary["skipped_fetch_fail"] += len(sigs)
            continue

        closes, source = result
        log.info(
            "backfill_klines_fetched",
            asset=asset,
            bars=len(closes),
            source=source,
        )

        for sig in sigs:
            price = match_close(closes, sig.signal_date)
            if price is None:
                summary["skipped_no_bar"] += 1
                continue

            await session.execute(
                update(Signal)
                .where(Signal.id == sig.id)
                .values(price_at_creation=price, price_source=source)
            )
            summary["updated"] += 1

    log.info("backfill_summary", **summary)
    return summary
