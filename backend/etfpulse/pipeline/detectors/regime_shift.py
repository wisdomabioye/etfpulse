"""RegimeShiftDetector — fires on a UTC-day regime transition.

Reads, doesn't classify: the orchestrator (`signal_builder.run_daily_cycle`)
runs `regime_monitor.classify_regime` and persists a fresh snapshot BEFORE
detectors run. By the time this detector executes, `regime_snapshots` already
holds today's row plus the previous one. The detector compares the two and
emits a hit on a transition.

PR F.3 — re-designed per the locked decision in open_issues.md §76 (issue #46
closure):

  * **Single MARKET hit** per transition. A regime change is a market-wide
    event; emitting per-asset hits (BTC + ETH) artificially inflated the
    cohort and put the same content into two separate signals. The new
    contract: one `DetectorHit` with `asset=MARKET_ASSET`. Downstream
    (`delivery.py:_match_users` / `_match_groups`, `track_record.py`, and
    `api/routes/admin.py:_asset_matches`) treats MARKET signals as
    bypassing the per-asset `pref_assets` filter (regime shifts reach
    everyone subject to confidence floor and pause state) and as
    not-scorable (no asset to price against).

  * **UTC-day gate** — fires only when `previous.captured_at` and
    `latest.captured_at` are on different UTC calendar dates. Intra-day
    re-classification under the Branch 3 intra-day cycle previously
    produced flicker signals when the score oscillated across a regime
    boundary within one day. The day-gate makes regime_shift a once-per-
    UTC-day event: same-day transitions are absorbed and only the
    cross-midnight transition surfaces.

Idempotency (R2): fingerprint =
sha256(MARKET|"regime_shift"|signal_date|new_regime)[:32]. Per-date,
per-target-regime. The (fingerprint, signal_date) unique index on `signals`
makes downstream re-runs no-ops.

Edge cases:
    - First-ever cycle (only one snapshot in DB) → no prior to compare → no hit.
    - Cycle with same regime as prior → no transition → no hit.
    - Same-UTC-day transition (e.g. intra-day flip) → gated → no hit.
    - Three or more snapshots in a single day → only the two most recent are
      compared. If those two are same-day, no hit; if they straddle midnight,
      one hit fires.
    - Legacy snapshot with NULL regime → skipped (pre-classifier shape).
"""

from __future__ import annotations

from datetime import UTC, date

from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from etfpulse.constants import MARKET_ASSET
from etfpulse.models import RegimeSnapshot, SignalType
from etfpulse.pipeline.detectors.base import DetectorHit, compute_fingerprint


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

        latest_date = latest.captured_at.astimezone(UTC).date()
        previous_date = previous.captured_at.astimezone(UTC).date()
        if previous_date >= latest_date:
            # UTC-day gate: same-day flip (e.g. intra-day re-classification).
            # Don't fire — wait for the next UTC day to confirm the regime
            # change persists, then emit a single signal at the boundary.
            return []

        return [self._build_hit(latest, previous, latest_date)]

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
            asset=MARKET_ASSET,
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
                MARKET_ASSET,
                "regime_shift",
                signal_date.isoformat(),
                new_regime,
            ),
        )
