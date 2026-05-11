"""Retry AI enrichment for `Signal` rows where `ai_analysis IS NULL`.

When an OpenRouter call fails at signal-build time (insufficient credits,
timeout, schema mismatch, daily cap), `signal_builder.build_signal`
persists the Signal with NULL `ai_analysis` / `confidence` / `expires_at`
per Resolution R6. The next daily cycle won't retry that row — `build_signal`
short-circuits on `(fingerprint, signal_date)` conflict before calling AI.

Without a backfill, NULL-AI signals are stranded: not delivered (fan-out
filters NULL confidence), not scored (outcome eval requires confidence),
not auto-expired (reaper requires explicit expires_at). They sit in
PENDING forever, visible only via `/api/signals` with `DisplayStatus="pending"`.

This module fixes that. `backfill_null_ai(session, limit)` walks NULL-AI
signals oldest-first, reconstructs the original AI prompt context from
the persisted `trigger_data` (which already carries `regime_at_creation`
and `news_context` per Stage 7-P6), and re-calls `openrouter_client.analyze`.
On success the Signal's `ai_analysis`/`confidence`/`expires_at` columns
populate; the row becomes deliverable on the next fan-out tick.

Contract:
    - Idempotent (NULL-only filter — re-running is a no-op for rows
      already enriched).
    - Caller owns the commit (D14 — same contract as `run_daily_cycle`,
      `fan_out_signal`, etc).
    - `limit` is REQUIRED (default 10) so an operator click can't accidentally
      blow through OpenRouter quota on a backlog of hundreds of signals.
"""

from __future__ import annotations

from typing import Any, cast

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from etfpulse.adapters.openrouter import openrouter_client
from etfpulse.models import MarketRegime, Signal, SignalPosture
from etfpulse.pipeline.analysis import apply_analysis_to_signal
from etfpulse.pipeline.news_context import NewsContextItem
from etfpulse.pipeline.regime_monitor import RegimeClassification

log = structlog.get_logger()


async def backfill_null_ai(session: AsyncSession, *, limit: int = 10) -> dict[str, Any]:
    """Re-run OpenRouter enrichment for up to `limit` NULL-AI signals.

    Selection: `Signal.ai_analysis IS NULL` ordered by `created_at ASC` so
    the oldest stranded rows clear first (consistent with FIFO operator
    intuition). `limit` caps OpenRouter spend per invocation.

    Per-row failure (AI returns None — quota, network, schema mismatch) is
    counted in `failed` but does NOT abort the loop — same catch-and-continue
    contract as `run_daily_cycle`. The first 3 error reasons are sampled
    into `error_samples` so the operator UI can surface the actual cause
    without scraping server logs.
    """
    stmt = (
        select(Signal)
        .where(Signal.ai_analysis.is_(None))
        .order_by(Signal.created_at.asc())
        .limit(limit)
    )
    rows = (await session.execute(stmt)).scalars().all()

    summary: dict[str, Any] = {
        "scanned": len(rows),
        "updated": 0,
        "failed": 0,
        "error_samples": [],
    }

    for signal in rows:
        regime = _reconstruct_regime(signal.trigger_data or {})
        news_context = _reconstruct_news_context(signal.trigger_data or {})

        # Strip the two enrichment keys — `analyze()` takes them as
        # dedicated args; leaving them in `trigger_data` would cause the
        # prompt to surface them twice.
        original_trigger = {
            k: v
            for k, v in (signal.trigger_data or {}).items()
            if k not in ("regime_at_creation", "news_context")
        }

        try:
            analysis, reason = await openrouter_client.analyze_with_reason(
                signal_type=signal.signal_type,
                asset=signal.asset,
                trigger_data=original_trigger,
                regime=regime,
                news_context=news_context,
                current_price=signal.price_at_creation,
            )
        except Exception as exc:
            # `analyze_with_reason()` is documented to never raise (same R6
            # contract as `analyze()`), but a defensive catch keeps a
            # misbehaving adapter from killing the whole batch.
            summary["failed"] += 1
            _record_error(summary, signal.id, type(exc).__name__, str(exc))
            continue

        if analysis is None:
            summary["failed"] += 1
            # `reason` is the operator-facing string the adapter returns —
            # "request failed: HTTP 402: ...", "schema mismatch: ...", etc.
            # Surface it directly so the operator UI shows the actual cause
            # instead of "see server logs".
            _record_error(
                summary,
                signal.id,
                _classify_reason(reason),
                reason or "unknown reason",
            )
            continue

        apply_analysis_to_signal(signal, analysis)
        summary["updated"] += 1
        log.info(
            "ai_backfill_signal_updated",
            signal_id=signal.id,
            asset=signal.asset,
            signal_type=signal.signal_type,
            confidence=analysis.confidence,
        )

    await session.flush()
    log.info("ai_backfill_done", **{k: v for k, v in summary.items() if k != "error_samples"})
    return summary


def _classify_reason(reason: str | None) -> str:
    """Map the adapter's free-text reason into a short tag for the operator
    UI. `kind` shows up as the bold label next to the signal id; the full
    `detail` is the raw reason. Keeping the tag set small (≤6 values) makes
    the failure samples easy to skim — operator can tell "all 10 failed
    for the same reason" at a glance."""
    if reason is None:
        return "Unknown"
    r = reason.lower()
    if "402" in r or "credit" in r:
        return "InsufficientCredits"
    if "429" in r or "cap" in r or "rate limit" in r:
        return "RateLimited"
    if "invalid json" in r:
        return "InvalidJson"
    if "schema mismatch" in r:
        return "SchemaMismatch"
    if "missing" in r and "key" in r:
        return "ConfigMissing"
    if "fixture" in r:
        return "FixtureMissing"
    return "RequestFailed"


def _record_error(summary: dict[str, Any], signal_id: int, kind: str, detail: str) -> None:
    """Append up to the first 3 errors to `summary["error_samples"]`. Capping
    keeps the response payload bounded when a backlog of bad signals all
    fail for the same reason (e.g. account out of credits → all 10 hit 402)."""
    samples = summary["error_samples"]
    if len(samples) >= 3:
        return
    samples.append(
        {
            "signal_id": signal_id,
            "kind": kind,
            "detail": detail[:200],  # bound per-sample size too
        }
    )


def _reconstruct_regime(trigger_data: dict[str, Any]) -> RegimeClassification | None:
    """Rebuild a `RegimeClassification` from the JSONB blob persisted at
    signal-build time. Returns None when the key is absent (legacy signal
    pre-Stage-7-P6) OR when the stored shape is malformed — better to
    re-analyse without regime context than to crash the backfill."""
    raw = trigger_data.get("regime_at_creation")
    if not isinstance(raw, dict):
        return None
    try:
        return RegimeClassification(
            regime=MarketRegime(raw["regime"]),
            signal_posture=SignalPosture(raw["signal_posture"]),
            confidence=int(raw["confidence"]),
            reasoning={},  # not persisted in trigger_data; AI doesn't read it
            btc_dominance=None,  # not persisted in trigger_data
            macro_events_nearby=list(raw.get("macro_events_nearby") or []),
        )
    except (KeyError, ValueError, TypeError):
        return None


def _reconstruct_news_context(trigger_data: dict[str, Any]) -> list[NewsContextItem] | None:
    """Pull the persisted news_context list. Same defensive-tolerant pattern
    as regime — return None when malformed/absent so the AI re-runs without
    news rather than failing the backfill."""
    raw = trigger_data.get("news_context")
    if not isinstance(raw, list):
        return None
    # NewsContextItem is a TypedDict — no runtime validation, just cast.
    # The shape was written by `gather_news_context` originally; round-tripping
    # the same dict satisfies the type at runtime.
    return cast(list[NewsContextItem], raw)
