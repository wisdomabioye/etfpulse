"""AccelerationDetector — fires when recent 7-day cumulative flow differs
from the prior 7-day cumulative flow by ≥50% (in either direction).

Algorithm (pure, in `_detect_acceleration`):
    1. If fewer than `window * 2` rows → None (need two adjacent windows).
    2. Split the last `window * 2` rows into older half and recent half.
    3. Sum each half.
    4. If |prior_sum| < `min_prior_usd` (default $1M) → None. Small
       denominators make the percentage change numerically unstable and
       semantically noisy.
    5. change = (recent_sum - prior_sum) / prior_sum. If |change| < 0.5 → None.
    6. Direction = "long" if recent_sum > 0, else "short".

Fingerprint per spec #51: sha256(asset|"acceleration"|date|direction)[:32].
Same rationale as MagnitudeDetector — threshold is NOT in the fingerprint
so a backfill that tweaks the computed change doesn't double-fire the same
signal-date + direction.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from etfpulse.constants import SUPPORTED_ASSETS
from etfpulse.models import ETFFlow, SignalType
from etfpulse.pipeline.detectors.base import DetectorHit, compute_fingerprint


class AccelerationDetector:
    name = "acceleration"
    signal_type = SignalType.ACCELERATION.value

    def __init__(
        self,
        window: int = 7,
        change_threshold: float = 0.50,
        min_prior_usd: Decimal = Decimal("1000000"),
    ) -> None:
        self.window = window
        self.change_threshold = change_threshold
        self.min_prior_usd = min_prior_usd

    async def detect(self, session: AsyncSession) -> list[DetectorHit]:
        hits: list[DetectorHit] = []
        for asset in SUPPORTED_ASSETS:
            stmt = (
                select(ETFFlow.captured_at, ETFFlow.total_net_flow_usd)
                .where(ETFFlow.asset == asset)
                .order_by(ETFFlow.captured_at.desc())
                .limit(self.window * 2)
            )
            result = await session.execute(stmt)
            rows = [(row.captured_at, row.total_net_flow_usd) for row in reversed(result.all())]
            hit = self._detect_acceleration(asset, rows)
            if hit is not None:
                hits.append(hit)
        return hits

    def _detect_acceleration(
        self,
        asset: str,
        rows: list[tuple[date, Decimal]],
    ) -> DetectorHit | None:
        """Pure function — accepts (date, flow) tuples ascending by date."""
        need = self.window * 2
        if len(rows) < need:
            return None

        # Take the LAST `need` rows so the split is adjacent to today's data.
        window_rows = rows[-need:]
        prior = window_rows[: self.window]
        recent = window_rows[self.window :]

        prior_sum = sum((f for _d, f in prior), Decimal(0))
        recent_sum = sum((f for _d, f in recent), Decimal(0))

        if abs(prior_sum) < self.min_prior_usd:
            return None

        # Decimal / Decimal → Decimal, abs() preserves Decimal.
        change = (recent_sum - prior_sum) / prior_sum
        if abs(change) < Decimal(str(self.change_threshold)):
            return None

        # Signal direction follows the sign of the recent half; a large
        # positive recent_sum after a smaller prior_sum is a long acceleration.
        if recent_sum == 0:
            return None
        direction = "long" if recent_sum > 0 else "short"

        latest_date = recent[-1][0]

        return DetectorHit(
            signal_type=self.signal_type,
            asset=asset,
            signal_date=latest_date,
            trigger_data={
                "signal_date": latest_date.isoformat(),
                "prior_window_sum_usd": str(prior_sum),
                "recent_window_sum_usd": str(recent_sum),
                "change_ratio": str(change),
                "window_days": self.window,
                "direction": direction,
            },
            fingerprint=compute_fingerprint(
                asset,
                "acceleration",
                latest_date.isoformat(),
                direction,
            ),
        )
