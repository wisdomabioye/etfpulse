"""One-shot backfill — populate Signal.price_at_creation for historical NULLs.

Context: Stage 4a shipped without a spot-price source, so every Signal
created before 2026-04-24 persists with NULL price. Stage 08 track record
needs entry prices to compute +24h/+72h returns; NULL = unevaluable.

This script is IDEMPOTENT — it only touches rows where `price_at_creation`
is still NULL, so reruns are safe and will re-attempt previously-failed
fetches. Run again after quota reset / network recovery to catch what the
first run missed.

Strategy:
  1. Find all signals with NULL price_at_creation, grouped by asset.
  2. For each asset, determine the min/max signal_date range.
  3. Fetch daily klines for that window from SoSoValue (primary) or
     Binance (fallback). Both return 1d bars keyed by UTC open_time.
  4. Match each signal's signal_date to the bar whose open_date equals
     signal_date, use that bar's close price.
  5. Stamp `trigger_data["price_source"]` on the row so Stage 08 outcome
     evaluation queries the same provider when looking up +24h/+72h prices.

Run from backend/:

    uv run python scripts/backfill_signal_prices.py
    # or
    .venv/bin/python scripts/backfill_signal_prices.py

Date-matching decision: we use the signal's signal_date close (not
signal_date - 1). Rationale: signal_date is in the past by the time we're
backfilling, so that day's kline is already published. The close of
signal_date is the most faithful "what was the reference price on this
signal's day" reading. If the kline for signal_date is missing (listing
gap, holiday weirdness), we fall through to the previous available bar.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Literal

import structlog
from sqlalchemy import select, update

from etfpulse.adapters.binance import BinanceError, binance_client
from etfpulse.adapters.sosovalue import SoSoValueError, sosovalue_client
from etfpulse.db import async_session
from etfpulse.models import Signal

log = structlog.get_logger()

Asset = Literal["BTC", "ETH"]


def _date_to_ms(d: date) -> int:
    """UTC midnight of `d` as epoch milliseconds — the param format both
    SoSoValue klines and Binance klines accept."""
    return int(datetime(d.year, d.month, d.day, tzinfo=UTC).timestamp() * 1000)


async def _fetch_closes_by_date(
    asset: Asset, start: date, end: date
) -> tuple[dict[date, Decimal], str] | None:
    """Return `({signal_date: close_price}, source)` or None if both providers fail.

    `start` and `end` are inclusive; we widen by ±1 day when calling the API
    to absorb timezone edge cases (SoSoValue and Binance both bar-open at
    UTC midnight but we'd rather over-fetch than miss boundary days).
    """
    # Widen the window so boundary signals never fall outside the fetched
    # bars. +1 day on `end` also matters if some detectors publish after the
    # UTC rollover.
    start_ms = _date_to_ms(start - timedelta(days=1))
    end_ms = _date_to_ms(end + timedelta(days=1))
    span_days = (end - start).days + 3  # extra padding; API cap is 500 anyway
    limit = min(max(span_days, 10), 500)

    # Primary: SoSoValue.
    try:
        soso_klines = await sosovalue_client.get_daily_klines(
            asset, start_time_ms=start_ms, end_time_ms=end_ms, limit=limit
        )
        closes = {k.bar_date: k.close for k in soso_klines}
        if closes:
            return closes, "sosovalue"
        log.warning("backfill_sosovalue_empty", asset=asset)
    except SoSoValueError as exc:
        log.warning(
            "backfill_sosovalue_failed",
            asset=asset,
            error_type=type(exc).__name__,
            error=str(exc),
        )

    # Fallback: Binance.
    try:
        binance_klines = await binance_client.get_daily_klines(
            asset, start_time_ms=start_ms, end_time_ms=end_ms, limit=limit
        )
        closes = {k.bar_date: k.close for k in binance_klines}
        if closes:
            return closes, "binance"
        log.warning("backfill_binance_empty", asset=asset)
    except BinanceError as exc:
        log.warning(
            "backfill_binance_failed",
            asset=asset,
            error_type=type(exc).__name__,
            error=str(exc),
        )

    log.error("backfill_both_sources_failed", asset=asset)
    return None


def _match_close(closes: dict[date, Decimal], signal_date: date) -> Decimal | None:
    """Find the closest-available close on or before `signal_date`.

    Direct hit is preferred; otherwise walk back up to 5 days looking for
    the nearest previous bar (handles holiday gaps in some providers).
    """
    for delta in range(6):
        candidate = signal_date - timedelta(days=delta)
        if candidate in closes:
            return closes[candidate]
    return None


async def main() -> None:
    async with async_session() as session:
        null_signals = (
            await session.execute(
                select(Signal).where(Signal.price_at_creation.is_(None))
            )
        ).scalars().all()

    if not null_signals:
        print("No signals with NULL price_at_creation. Nothing to do.")
        return

    # Group by asset
    by_asset: dict[Asset, list[Signal]] = {"BTC": [], "ETH": []}
    for sig in null_signals:
        if sig.asset in ("BTC", "ETH"):
            by_asset[sig.asset].append(sig)

    updated = 0
    skipped_no_bar = 0
    skipped_fetch_fail = 0

    for asset, sigs in by_asset.items():
        if not sigs:
            continue

        dates = [s.signal_date for s in sigs]
        start = min(dates)
        end = max(dates)

        print(f"[{asset}] {len(sigs)} NULL signals from {start} to {end}")
        result = await _fetch_closes_by_date(asset, start, end)
        if result is None:
            print(f"[{asset}] both providers failed; skipping {len(sigs)} signals")
            skipped_fetch_fail += len(sigs)
            continue

        closes, source = result
        print(f"[{asset}] got {len(closes)} daily bars from {source}")

        for sig in sigs:
            price = _match_close(closes, sig.signal_date)
            if price is None:
                print(f"  #{sig.id} {sig.signal_date}: no bar match, leaving NULL")
                skipped_no_bar += 1
                continue

            # Re-open a session per update so transaction scope is tight.
            async with async_session() as s:
                # Merge: update price + stuff source into trigger_data.
                existing = (
                    await s.execute(select(Signal).where(Signal.id == sig.id))
                ).scalar_one()
                new_trigger_data = dict(existing.trigger_data)
                new_trigger_data["price_source"] = source
                await s.execute(
                    update(Signal)
                    .where(Signal.id == sig.id)
                    .values(price_at_creation=price, trigger_data=new_trigger_data)
                )
                await s.commit()
            updated += 1

    print(f"\nUpdated: {updated}")
    print(f"Skipped (no matching bar): {skipped_no_bar}")
    print(f"Skipped (both providers failed): {skipped_fetch_fail}")


if __name__ == "__main__":
    asyncio.run(main())
