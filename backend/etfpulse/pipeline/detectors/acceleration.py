"""AccelerationDetector — fires when the rate of change of cumulative ETF
flows is itself changing (true second derivative).

PR F.1 — redesigned per locked decision #1 in open_issues.md §76. The pre-F.1
detector was a first-derivative ratio: it compared two adjacent 7-day window
sums and fired on a ≥50% change. That's "the recent week's flows are 50%
bigger than the prior week's" — which fires on any strong week, including
steady linear growth. Linear growth is not acceleration; it's a steady
positive slope. The new detector measures slope-of-slope (whether the trend
itself is steepening or flattening).

Algorithm (pure, in `_detect_acceleration`):
    1. If fewer than `window * 3` rows → None (need three adjacent windows
       to compute a second derivative).
    2. Split the last `window * 3` rows into oldest, mid, recent halves.
    3. Sum each: `oldest_sum`, `mid_sum`, `recent_sum`.
    4. Compute slopes (first derivatives in USD):
         slope_old = mid_sum - oldest_sum
         slope_new = recent_sum - mid_sum
       And the second derivative:
         second_derivative = slope_new - slope_old
                           = recent_sum - 2*mid_sum + oldest_sum
    5. If |slope_old| < `min_prior_usd` → None. The "before" slope must
       have been real before we can talk about it changing. (Semantic note:
       `min_prior_usd` was the floor on prior-window SUM under the pre-F.1
       algorithm; PR F.1 reuses the same env var to floor |slope_old|.
       Operators rarely tune this so renaming was deferred — see task #38.)
    6. change_ratio = second_derivative / slope_old. If |change_ratio| <
       `change_threshold` (default 1.00 = 100%) → None. Second derivatives
       are more volatile than first derivatives so the threshold is higher
       than the pre-F.1 default of 0.50.
    7. Direction = "long" if `second_derivative > 0` (trend becoming more
       positive / less negative — bullish acceleration), else "short"
       (trend becoming more negative / less positive — bearish). The
       threshold check above guarantees `second_derivative != 0` (since
       `change_threshold > 0` is enforced by config), so the direction
       branch is total.

What this catches vs. rejects:
    - Steady $100M/week → second_derivative=0 → NO fire. (Pre-F.1 fired
      on every "the recent week is 50% above the prior week" case, even
      when growth was linear.)
    - $100M → $150M → $300M → strong upward acceleration → fires long.
    - $300M → $200M → $50M → strong downward deceleration → fires short.
    - Tiny baseline ($100 → $200 → $5M) → blocked by min_prior_usd on
      |slope_old|; near-zero denominators don't fire spurious signals.

Fingerprint per spec #51: sha256(asset|"acceleration"|date|direction)[:32].
Same rationale as Magnitude/Divergence — threshold + magnitude are NOT in
the fingerprint so a backfill that tweaks the computed change doesn't
double-fire the same signal-date + direction.
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
        change_threshold: float = 1.00,
        min_prior_usd: Decimal = Decimal("1000000"),
    ) -> None:
        # PR F.1 — defaults: 7-day windows × 3 = 21d history needed; 100%
        # threshold (raised from 50% under the pre-F.1 first-derivative
        # algorithm — second derivatives are more volatile and need a higher
        # bar). `min_prior_usd` now floors |slope_old| instead of |prior_sum|.
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
                .limit(self.window * 3)
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
        need = self.window * 3
        if len(rows) < need:
            return None

        # Take the LAST `need` rows so the three windows are adjacent to today's data.
        window_rows = rows[-need:]
        oldest = window_rows[: self.window]
        mid = window_rows[self.window : self.window * 2]
        recent = window_rows[self.window * 2 :]

        oldest_sum = sum((f for _d, f in oldest), Decimal(0))
        mid_sum = sum((f for _d, f in mid), Decimal(0))
        recent_sum = sum((f for _d, f in recent), Decimal(0))

        # First derivatives (week-over-week change in USD).
        slope_old = mid_sum - oldest_sum
        slope_new = recent_sum - mid_sum

        # Floor on |slope_old| — the "before" slope must be real before we
        # can meaningfully say it's changed. Also avoids huge ratios from
        # near-zero denominators (same numeric-stability rationale as the
        # pre-F.1 prior-sum floor).
        if abs(slope_old) < self.min_prior_usd:
            return None

        # Second derivative (rate of rate of change in USD).
        second_derivative = slope_new - slope_old

        # Ratio threshold — second_derivative relative to the prior slope.
        # Decimal / Decimal preserves precision; Decimal(str(float)) avoids
        # the binary-float artifacts of Decimal(float).
        change_ratio = second_derivative / slope_old
        if abs(change_ratio) < Decimal(str(self.change_threshold)):
            return None

        # Threshold check + `change_threshold > 0` constraint together
        # guarantee `second_derivative != 0` by the time we reach here.
        direction = "long" if second_derivative > 0 else "short"

        latest_date = recent[-1][0]

        return DetectorHit(
            signal_type=self.signal_type,
            asset=asset,
            signal_date=latest_date,
            trigger_data={
                "signal_date": latest_date.isoformat(),
                # Three-window decomposition — operator-visible so the AI
                # prompt + UI can render why this fired.
                "oldest_window_sum_usd": str(oldest_sum),
                "mid_window_sum_usd": str(mid_sum),
                "recent_window_sum_usd": str(recent_sum),
                "slope_old_usd": str(slope_old),
                "slope_new_usd": str(slope_new),
                "second_derivative_usd": str(second_derivative),
                # `change_ratio` key preserved from the pre-F.1 schema —
                # `frontend/src/components/signals/TriggerDataTable.tsx`
                # special-cases this key to render as a percent. Same
                # semantic meaning (second_derivative / slope_old vs the old
                # (recent - prior) / prior) — both are "how big is the
                # change relative to the baseline." Avoids breaking the FE
                # percent-rendering for this detector.
                "change_ratio": str(change_ratio),
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
