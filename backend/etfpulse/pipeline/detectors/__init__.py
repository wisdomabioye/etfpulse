"""Detector registry — every detector ships in this package and is appended
to `ALL_DETECTORS` to be picked up by `signal_builder.run_daily_cycle`.

Anti-drift rules installed by this module:
    D9  — Detectors live ONLY in `etfpulse.pipeline.detectors.*` and implement
          the `Detector` Protocol below. No detector logic anywhere else.
    D10 — Adding a detector requires registering it in `ALL_DETECTORS`.
          `signal_builder` iterates this list — unregistered detectors never
          run. There is no auto-discovery; explicit > implicit.
    D11 — `ALL_DETECTORS` order determines precedence on fingerprint
          collisions: the unique index on `(fingerprint, signal_date)` makes
          the first insert win, so order detectors strongest-signal-first
          when multiple could plausibly fire on the same input.

A single detector failing (raising) MUST NOT abort the daily cycle — that is
`signal_builder`'s responsibility (try/except around each `detect()` call,
log + continue). The Protocol intentionally allows exceptions.
"""

from __future__ import annotations

from typing import Protocol

from sqlalchemy.ext.asyncio import AsyncSession

from etfpulse.pipeline.detectors.base import DetectorHit, compute_fingerprint


class Detector(Protocol):
    """Stateless reader over the DB session. Side-effect free.

    Attributes:
        name: Stable, human-readable identifier used in logs and metrics.
              Must be unique across `ALL_DETECTORS`.
        signal_type: One of `models.signal.SignalType` values. Determines
                     which `Signal.signal_type` value the resulting rows get.
    """

    name: str
    signal_type: str

    async def detect(self, session: AsyncSession) -> list[DetectorHit]: ...


ALL_DETECTORS: list[Detector] = []


__all__ = ["ALL_DETECTORS", "Detector", "DetectorHit", "compute_fingerprint"]
