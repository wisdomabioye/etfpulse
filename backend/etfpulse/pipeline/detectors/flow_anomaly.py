"""FlowAnomalyDetector — fires when an N-day same-direction net-flow streak
breaks on the most recent settlement day.

Algorithm (in `_detect_streak_break`, pure & DB-free):
    1. Drop zero-flow days (per task spec — they're skipped, not breaking).
    2. Take the latest non-zero day as the candidate break.
    3. Walk backwards counting consecutive opposite-direction days.
    4. If the back-streak is >= `min_streak_length`, emit a hit dated to the
       break day.

Only the *most recent* row in the lookback window can produce a hit. A 14-day
window may contain older breaks, but those are stale — the daily run for that
date already fired (or, for missed days, the catch-up logic in #45 will
re-run them as if it were that day). This keeps a single daily run from
re-firing week-old events.

Idempotency (Resolution R2): fingerprint =
sha256(asset|"flow_anomaly"|break_date|streak_len|streak_dir)[:32]. The
(fingerprint, signal_date) unique index on `signals` makes a re-run silently
no-op rather than double-fire.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from etfpulse.constants import SUPPORTED_ASSETS
from etfpulse.models import ETFFlow, MarketRegime, SignalType
from etfpulse.pipeline.detectors.base import DetectorHit, compute_fingerprint


class FlowAnomalyDetector:
    name = "flow_anomaly"
    signal_type = SignalType.FLOW_ANOMALY.value

    def __init__(self, lookback_days: int = 14, min_streak_length: int = 3) -> None:
        self.lookback_days = lookback_days
        self.min_streak_length = min_streak_length

    async def detect(
        self,
        session: AsyncSession,
        *,
        as_of: date | None = None,
        current_regime: MarketRegime | None = None,  # noqa: ARG002 — accepted for Protocol parity; flow_anomaly has no regime-conditional knobs (PR I.4)
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
            # `desc().limit()` gives newest-first; reverse for chronological scan.
            rows = [(row.captured_at, row.total_net_flow_usd) for row in reversed(result.all())]
            hit = self._detect_streak_break(asset, rows)
            if hit is not None:
                hits.append(hit)
        return hits

    def _detect_streak_break(
        self,
        asset: str,
        rows: list[tuple[date, Decimal]],
    ) -> DetectorHit | None:
        """Pure function — accepts (date, flow_usd) tuples ascending by date."""
        nonzero = [r for r in rows if r[1] != 0]
        # Need at least min_streak_length prior days + 1 break day.
        if len(nonzero) < self.min_streak_length + 1:
            return None

        break_date, break_flow = nonzero[-1]
        break_dir = "long" if break_flow > 0 else "short"
        # The streak that just broke was the OPPOSITE direction.
        streak_dir = "short" if break_dir == "long" else "long"

        streak_length = 0
        streak_flows: list[Decimal] = []
        for _d, f in reversed(nonzero[:-1]):
            d_dir = "long" if f > 0 else "short"
            if d_dir == streak_dir:
                streak_length += 1
                streak_flows.append(f)
            else:
                break

        if streak_length < self.min_streak_length:
            return None

        return DetectorHit(
            signal_type=self.signal_type,
            asset=asset,
            signal_date=break_date,
            trigger_data={
                "break_date": break_date.isoformat(),
                # Decimal → str: JSONB through asyncpg is unreliable about
                # Decimal serialisation; strings round-trip cleanly.
                "break_flow_usd": str(break_flow),
                "streak_length": streak_length,
                "streak_direction": streak_dir,
                # Reverse so the list is chronological (oldest streak day first).
                "streak_flows_usd": [str(f) for f in reversed(streak_flows)],
            },
            fingerprint=compute_fingerprint(
                asset,
                "flow_anomaly",
                break_date.isoformat(),
                str(streak_length),
                streak_dir,
            ),
        )
