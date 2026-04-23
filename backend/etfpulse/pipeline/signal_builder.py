"""Signal builder — orchestrates one daily cycle.

Two functions:
    `build_signal(session, hit)` — converts a `DetectorHit` into a persisted
        `Signal`. Idempotent via `(fingerprint, signal_date)` upsert.
        Optionally enriches with AI analysis (Resolution R6 — never blocks).
    `run_daily_cycle(session)` — full orchestration: ingest → detect →
        build_signal per hit. Returns a summary dict for admin/logging.

Anti-drift rules installed by this stage:
    D12 — `build_signal` calls AI OPTIONALLY: upsert first (so re-runs are
          idempotent and don't waste OpenRouter calls), then call AI only on
          newly-inserted rows. AI failure is non-fatal — Signal persists with
          NULL ai_analysis / confidence / expires_at.
    D13 — `run_daily_cycle` wraps every detector call in try/except. One bad
          detector cannot kill the cycle — log + continue.
    D14 — `run_daily_cycle` does NOT commit. Caller (the scheduler in #45,
          or the admin route in #47) owns the transaction boundary, same
          contract as `pipeline/ingestor.py`.

Open gaps acknowledged here:
    - `Signal.price_at_creation` is NULL (issue #34) — SoSoValue spot/klines
      adapter not yet wired. Wave 1 alerting doesn't need it; Wave 2
      SignalOutcome does.
"""

from __future__ import annotations

from typing import Any, Literal

import structlog
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from etfpulse.adapters.openrouter import openrouter_client
from etfpulse.adapters.sosovalue import SoSoValueError
from etfpulse.models import Signal
from etfpulse.pipeline.analysis import compute_expires_at
from etfpulse.pipeline.detectors import ALL_DETECTORS, DetectorHit
from etfpulse.pipeline.ingestor import ingest_etf_flows

log = structlog.get_logger()

_ASSETS: tuple[Literal["BTC", "ETH"], ...] = ("BTC", "ETH")


async def build_signal(session: AsyncSession, hit: DetectorHit) -> Signal | None:
    """Upsert a Signal from a detector hit; enrich with AI analysis if available.

    Returns the persisted Signal on a NEW insert, or None if a row with the
    same (fingerprint, signal_date) already existed (idempotent skip).

    AI enrichment is best-effort (R6 / D12): if `openrouter_client.analyze`
    returns None, the Signal stays in the DB with NULL ai_analysis. The
    caller should not retry the AI call here — that's the next daily cycle's
    problem (and a future enricher job's, if we add one).
    """
    stmt = (
        insert(Signal)
        .values(
            signal_type=hit.signal_type,
            asset=hit.asset,
            trigger_data=hit.trigger_data,
            fingerprint=hit.fingerprint,
            signal_date=hit.signal_date,
            # price_at_creation, ai_analysis, confidence, expires_at all NULL
            # at insert. Updated below if AI succeeds. price stays NULL until
            # issue #34 lands.
        )
        .on_conflict_do_nothing(index_elements=["fingerprint", "signal_date"])
        .returning(Signal.id)
    )
    result = await session.execute(stmt)
    new_id = result.scalar_one_or_none()
    if new_id is None:
        log.debug(
            "signal_duplicate",
            signal_type=hit.signal_type,
            asset=hit.asset,
            signal_date=str(hit.signal_date),
            fingerprint=hit.fingerprint,
        )
        return None

    # Re-fetch as ORM object so the caller (and tests) get full row state.
    signal = await session.get(Signal, new_id)
    if signal is None:
        # Defensive — a row we just inserted disappearing means concurrent
        # delete, which would be its own bug worth surfacing.
        log.error("signal_inserted_then_missing", id=new_id)
        return None

    analysis = await openrouter_client.analyze(
        signal_type=hit.signal_type,
        asset=hit.asset,
        trigger_data=hit.trigger_data,
    )
    if analysis is not None:
        signal.ai_analysis = analysis.model_dump()
        signal.confidence = analysis.confidence
        signal.expires_at = compute_expires_at(analysis.time_horizon)

    log.info(
        "signal_built",
        id=signal.id,
        signal_type=signal.signal_type,
        asset=signal.asset,
        ai_present=analysis is not None,
    )
    return signal


async def run_daily_cycle(session: AsyncSession) -> dict[str, Any]:
    """Full daily orchestration. Returns a summary suitable for admin/logging.

    Steps:
        1. Ingest ETF flows for each asset. Any `SoSoValueError` (quota, rate
           limit, network, 5xx) is caught and recorded — the cycle continues
           with whatever data is already in the DB. Resolution R10.
        2. Iterate `ALL_DETECTORS`. Each detector internally handles its own
           assets. Per D13, every `detect()` call is wrapped in try/except
           so one buggy detector cannot kill the cycle.
        3. For each hit, call `build_signal`. New rows are counted as
           `signals_new`; idempotent skips as `signals_duplicate`.

    The session is NOT committed (D14) — caller owns the transaction.
    """
    summary: dict[str, Any] = {
        "ingested": {},
        "ingest_errors": [],
        "detectors_run": 0,
        "detector_errors": [],
        "signals_new": 0,
        "signals_duplicate": 0,
        "ai_succeeded": 0,
        "ai_failed": 0,
    }

    # --- Ingestion ----------------------------------------------------------
    for asset in _ASSETS:
        try:
            count = await ingest_etf_flows(session, asset)
            summary["ingested"][asset] = count
        except SoSoValueError as exc:
            log.warning(
                "daily_cycle_ingest_failed",
                asset=asset,
                error_type=type(exc).__name__,
                error=str(exc),
            )
            summary["ingest_errors"].append((asset, type(exc).__name__))

    # --- Detection + signal build ------------------------------------------
    for detector in ALL_DETECTORS:
        summary["detectors_run"] += 1
        try:
            hits = await detector.detect(session)
        except Exception as exc:
            # D13: catch any exception, log, continue. We do NOT re-raise
            # because the cycle's job is to do as much work as possible.
            log.error(
                "daily_cycle_detector_failed",
                detector=detector.name,
                error_type=type(exc).__name__,
                error=str(exc),
                exc_info=exc,
            )
            summary["detector_errors"].append((detector.name, type(exc).__name__))
            continue

        for hit in hits:
            signal = await build_signal(session, hit)
            if signal is None:
                summary["signals_duplicate"] += 1
            else:
                summary["signals_new"] += 1
                if signal.ai_analysis is not None:
                    summary["ai_succeeded"] += 1
                else:
                    summary["ai_failed"] += 1

    log.info("daily_cycle_complete", **summary)
    return summary
