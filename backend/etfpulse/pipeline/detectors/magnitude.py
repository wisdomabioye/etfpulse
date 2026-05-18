"""MagnitudeDetector — fires when today's |net flow| is an outlier vs the
last 90 days.

Algorithm (pure, in `_detect_magnitude`):
    1. If fewer than `min_history_days` rows in the window → None (too little
       history for a stable percentile).
    2. Compute the 80th percentile of |flow| over the window.
    3. If the most-recent day's |flow| is STRICTLY greater than p80, emit a
       hit; otherwise None.
    4. Direction = "long" if the most-recent flow is positive, else "short".
       (The magnitude comparison uses |flow|; direction reports the sign.)
    5. Zero on the most-recent day → None (no direction to report).

Fingerprint per spec #51: sha256(asset|"magnitude"|date|direction)[:32].
Crucially the bucket/threshold is NOT in the fingerprint — backfilling
earlier data that slightly shifts p80 still produces the same fingerprint,
so a re-run doesn't double-fire on the same-date same-direction event.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from etfpulse.constants import SUPPORTED_ASSETS
from etfpulse.models import ETFFlow, SignalType
from etfpulse.pipeline.detectors.base import DetectorHit, compute_fingerprint


def _percentile(values: list[Decimal], p: float) -> Decimal:
    """Nearest-rank percentile. `p` in [0, 1]. `values` need not be sorted."""
    sorted_vals = sorted(values)
    idx = int(p * (len(sorted_vals) - 1))
    return sorted_vals[idx]


class MagnitudeDetector:
    name = "magnitude"
    signal_type = SignalType.MAGNITUDE.value

    def __init__(
        self,
        lookback_days: int = 90,
        percentile_threshold: float = 0.80,
        min_history_days: int = 30,
    ) -> None:
        self.lookback_days = lookback_days
        self.percentile_threshold = percentile_threshold
        self.min_history_days = min_history_days

    async def detect(
        self, session: AsyncSession, *, as_of: date | None = None
    ) -> list[DetectorHit]:
        hits: list[DetectorHit] = []
        for asset in SUPPORTED_ASSETS:
            stmt = select(ETFFlow.captured_at, ETFFlow.total_net_flow_usd).where(
                ETFFlow.asset == asset
            )
            if as_of is not None:
                stmt = stmt.where(ETFFlow.captured_at <= as_of)
            stmt = stmt.order_by(ETFFlow.captured_at.desc()).limit(self.lookback_days)
            result = await session.execute(stmt)
            rows = [(row.captured_at, row.total_net_flow_usd) for row in reversed(result.all())]
            hit = self._detect_magnitude(asset, rows)
            if hit is not None:
                hits.append(hit)
        return hits

    def _detect_magnitude(
        self,
        asset: str,
        rows: list[tuple[date, Decimal]],
    ) -> DetectorHit | None:
        """Pure function — accepts (date, flow) tuples ascending by date."""
        if len(rows) < self.min_history_days:
            return None

        latest_date, latest_flow = rows[-1]
        if latest_flow == 0:
            return None

        abs_flows = [abs(f) for _d, f in rows]
        threshold = _percentile(abs_flows, self.percentile_threshold)

        if abs(latest_flow) <= threshold:
            return None

        direction = "long" if latest_flow > 0 else "short"

        return DetectorHit(
            signal_type=self.signal_type,
            asset=asset,
            signal_date=latest_date,
            trigger_data={
                "signal_date": latest_date.isoformat(),
                "latest_flow_usd": str(latest_flow),
                "abs_flow_usd": str(abs(latest_flow)),
                "threshold_usd": str(threshold),
                "percentile": self.percentile_threshold,
                "lookback_days": self.lookback_days,
                "sample_size": len(rows),
                "direction": direction,
            },
            fingerprint=compute_fingerprint(
                asset,
                "magnitude",
                latest_date.isoformat(),
                direction,
            ),
        )
