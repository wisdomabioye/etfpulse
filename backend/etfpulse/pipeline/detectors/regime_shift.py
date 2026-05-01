"""RegimeShiftDetector — fires when today's regime classification differs
from the most-recent prior `RegimeSnapshot`.

Reads, doesn't classify: the orchestrator (`signal_builder.run_daily_cycle`)
runs `regime_monitor.classify_regime` and persists a fresh snapshot BEFORE
detectors run. By the time this detector executes, `regime_snapshots` already
holds today's row plus the previous one. The detector compares the two and
emits hits on any transition.

Per-asset rows: a regime shift is a market-wide event, not asset-specific.
But rather than introduce a `MARKET` asset sentinel that would ripple through
fan-out, fingerprints, and UI, we emit ONE hit per tracked asset with
identical content. Symmetry with every other detector — no sentinel
plumbing needed downstream.

Idempotency (R2): fingerprint =
sha256(asset|"regime_shift"|signal_date|new_regime)[:32]. Per-asset, per-date,
per-target-regime. Re-running the same cycle is a no-op via the
(fingerprint, signal_date) unique index.

Edge cases:
    - First-ever cycle (only one snapshot in DB) → no prior to compare → no hit.
    - Cycle with same regime as prior → no transition → no hit.
    - Three or more snapshots in a single day (e.g. backfill) → only the two
      most recent are compared. The detector is correct even if multiple
      classifications run within one signal_date.
"""

from __future__ import annotations

from datetime import UTC, date

from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from etfpulse.models import RegimeSnapshot, SignalType
from etfpulse.pipeline.detectors.base import DetectorHit, compute_fingerprint
from etfpulse.pipeline.prices import Asset

_TRACKED_ASSETS: tuple[Asset, ...] = ("BTC", "ETH")


class RegimeShiftDetector:
    name = "regime_shift"
    signal_type = SignalType.REGIME_SHIFT.value

    async def detect(self, session: AsyncSession) -> list[DetectorHit]:
        latest, previous = await self._load_two_latest(session)
        if latest is None or previous is None:
            return []  # not enough history to detect a transition
        if latest.regime is None or previous.regime is None:
            return []  # legacy snapshot rows pre-classifier — skip
        if latest.regime == previous.regime:
            return []  # no transition

        # The signal_date is today's calendar UTC date; the latest snapshot's
        # captured_at is the canonical timestamp for that.
        signal_date = latest.captured_at.astimezone(UTC).date()

        return [self._build_hit(latest, previous, asset, signal_date) for asset in _TRACKED_ASSETS]

    async def _load_two_latest(
        self, session: AsyncSession
    ) -> tuple[RegimeSnapshot | None, RegimeSnapshot | None]:
        """`(latest, previous)` or `(latest_or_None, None)` if <2 rows exist.

        The detector only emits hits when both are present, so the asymmetric
        return saves the caller from indexing into a possibly-empty list.
        """
        stmt = select(RegimeSnapshot).order_by(desc(RegimeSnapshot.captured_at)).limit(2)
        rows = (await session.execute(stmt)).scalars().all()
        latest = rows[0] if len(rows) >= 1 else None
        previous = rows[1] if len(rows) >= 2 else None
        return latest, previous

    @staticmethod
    def _build_hit(
        latest: RegimeSnapshot,
        previous: RegimeSnapshot,
        asset: Asset,
        signal_date: date,
    ) -> DetectorHit:
        # `regime` non-None is enforced by the caller's guard. `signal_posture`
        # is always populated alongside `regime` by `run_daily_cycle`, so
        # `latest.signal_posture`/`previous.signal_posture` are non-None in
        # practice — but the model column is nullable, so JSONB will accept
        # null if a future writer ever skips it. Trigger_data consumers must
        # tolerate null postures.
        new_regime = latest.regime
        old_regime = previous.regime
        assert new_regime is not None and old_regime is not None  # noqa: S101

        return DetectorHit(
            signal_type=SignalType.REGIME_SHIFT.value,
            asset=asset,
            signal_date=signal_date,
            trigger_data={
                "previous_regime": old_regime,
                "new_regime": new_regime,
                "previous_posture": previous.signal_posture,
                "new_posture": latest.signal_posture,
                "confidence": latest.confidence,
                "transitioned_at": latest.captured_at.astimezone(UTC).isoformat(),
            },
            fingerprint=compute_fingerprint(
                asset,
                "regime_shift",
                signal_date.isoformat(),
                new_regime,
            ),
        )
