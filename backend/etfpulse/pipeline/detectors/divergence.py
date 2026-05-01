"""DivergenceDetector — fires when ETF flows and spot price disagree on
direction over the same recent window.

Algorithm (per asset):
    1. Pull the last `lookback_days` ETFFlow rows. Require all `lookback_days`
       to be the SAME sign (all-positive or all-negative). Mixed-sign windows
       are not divergence — they're noise.
    2. Fetch a daily-kline window covering the same dates via
       `pipeline.prices.get_daily_klines_with_source` (P3 composer). If the
       composer returns None (both providers down), skip — we can't tell.
    3. Compute price change = close(latest) - close(earliest). If the price
       moved in the OPPOSITE direction from the flow sign, emit a hit.
    4. `divergence_type` is one of:
         "flow_pos_price_neg" — institutional buying despite price drop
                                (bullish flows, bearish price → potential bottom)
         "flow_neg_price_pos" — institutional selling despite price rise
                                (bearish flows, bullish price → potential top)

Idempotency (R2): fingerprint =
sha256(asset|"divergence"|signal_date|divergence_type)[:32]. The
`divergence_type` string carries the directional information; raw price/flow
magnitudes are intentionally NOT in the fingerprint so a small recomputation
on backfilled data doesn't double-fire on the same date.

Network-bound: this detector calls the price composer per asset. Failures
are non-fatal at the per-asset level (skip that asset, continue to the next).
The signal-builder cycle's outer try/except (D13) catches anything else.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from etfpulse.models import ETFFlow, SignalType
from etfpulse.pipeline.detectors.base import DetectorHit, compute_fingerprint
from etfpulse.pipeline.prices import (
    Asset,
    PriceBar,
    get_daily_klines_with_source,
)

_TRACKED_ASSETS: tuple[Asset, ...] = ("BTC", "ETH")


class DivergenceDetector:
    name = "divergence"
    signal_type = SignalType.DIVERGENCE.value

    def __init__(self, lookback_days: int = 3) -> None:
        # 3 days = task spec ("require all-same-sign" for last N=3). Longer
        # windows make divergence rarer; shorter ones make it noisier.
        self.lookback_days = lookback_days

    async def detect(self, session: AsyncSession) -> list[DetectorHit]:
        hits: list[DetectorHit] = []
        for asset in _TRACKED_ASSETS:
            hit = await self._detect_for_asset(session, asset)
            if hit is not None:
                hits.append(hit)
        return hits

    async def _detect_for_asset(self, session: AsyncSession, asset: Asset) -> DetectorHit | None:
        flow_rows = await self._load_flow_rows(session, asset)
        if len(flow_rows) < self.lookback_days:
            return None

        flow_sign = _all_same_sign(flow_rows)
        if flow_sign is None:
            return None  # mixed-sign window → not divergence

        bars = await self._load_price_bars(asset, flow_rows)
        if bars is None:
            return None  # both providers down — can't classify; backfill later

        price_change = _price_change_over_window(bars, flow_rows)
        if price_change is None:
            return None  # not enough kline coverage to compute a change

        # Divergence is sign-mismatch between flow and price.
        if flow_sign > 0 and price_change >= 0:
            return None
        if flow_sign < 0 and price_change <= 0:
            return None

        divergence_type = "flow_pos_price_neg" if flow_sign > 0 else "flow_neg_price_pos"
        latest_date, _ = flow_rows[-1]
        earliest_date, _ = flow_rows[0]

        return DetectorHit(
            signal_type=self.signal_type,
            asset=asset,
            signal_date=latest_date,
            trigger_data={
                "divergence_type": divergence_type,
                "lookback_days": self.lookback_days,
                "window_start": earliest_date.isoformat(),
                "window_end": latest_date.isoformat(),
                "flows_usd": [str(flow) for _d, flow in flow_rows],
                "price_change_usd": str(price_change),
                "price_at_window_start": str(bars[0].close),
                "price_at_window_end": str(bars[-1].close),
            },
            fingerprint=compute_fingerprint(
                asset,
                "divergence",
                latest_date.isoformat(),
                divergence_type,
            ),
        )

    async def _load_flow_rows(
        self, session: AsyncSession, asset: Asset
    ) -> list[tuple[date, Decimal]]:
        """Newest `lookback_days` rows ordered ASCENDING by date."""
        stmt = (
            select(ETFFlow.captured_at, ETFFlow.total_net_flow_usd)
            .where(ETFFlow.asset == asset)
            .order_by(ETFFlow.captured_at.desc())
            .limit(self.lookback_days)
        )
        result = await session.execute(stmt)
        return [(row.captured_at, row.total_net_flow_usd) for row in reversed(result.all())]

    async def _load_price_bars(
        self, asset: Asset, flow_rows: list[tuple[date, Decimal]]
    ) -> list[PriceBar] | None:
        """Fetch klines covering the flow window. Returns None on total provider failure.

        We pad the window by one day on each side to absorb timezone edge
        cases (a flow_row dated 2026-04-22 is a UTC aggregator day, but the
        kline series might bar-open at 23:00 the prior day on some providers).
        """
        start_date, _ = flow_rows[0]
        end_date, _ = flow_rows[-1]
        start_ms = _date_to_ms(start_date - timedelta(days=1))
        end_ms = _date_to_ms(end_date + timedelta(days=1))

        result = await get_daily_klines_with_source(
            asset, start_time_ms=start_ms, end_time_ms=end_ms, limit=10
        )
        if result is None:
            return None
        bars, _source = result
        if not bars:
            return None
        # Sort ascending so [0] = window start, [-1] = window end.
        return sorted(bars, key=lambda b: b.timestamp_ms)


def _all_same_sign(flow_rows: list[tuple[date, Decimal]]) -> int | None:
    """Returns +1 if all flows positive, -1 if all negative, None if mixed/zero."""
    signs = {1 if flow > 0 else -1 if flow < 0 else 0 for _d, flow in flow_rows}
    if signs == {1}:
        return 1
    if signs == {-1}:
        return -1
    return None


def _price_change_over_window(
    bars: list[PriceBar], flow_rows: list[tuple[date, Decimal]]
) -> Decimal | None:
    """Close(latest_flow_date) - close(earliest_flow_date), or None if missing.

    Walks the (date-ascending) bars to find the closes that match the flow
    window endpoints. Falls back to the closest-available bar if the exact
    date isn't in the kline series (provider gap).
    """
    earliest_date, _ = flow_rows[0]
    latest_date, _ = flow_rows[-1]
    by_date = {bar.bar_date: bar.close for bar in bars}

    start_close = _closest_close_on_or_before(by_date, earliest_date)
    end_close = _closest_close_on_or_before(by_date, latest_date)
    if start_close is None or end_close is None:
        return None
    return end_close - start_close


def _closest_close_on_or_before(closes: dict[date, Decimal], target: date) -> Decimal | None:
    """Walk back up to 5 days looking for the nearest available close."""
    for delta in range(6):
        candidate = target - timedelta(days=delta)
        if candidate in closes:
            return closes[candidate]
    return None


def _date_to_ms(d: date) -> int:
    """UTC midnight of `d` as epoch milliseconds — kline start/end_time format."""
    return int(datetime(d.year, d.month, d.day, tzinfo=UTC).timestamp() * 1000)
