"""Signal builder — orchestrates one daily cycle.

Two functions:
    `build_signal(session, hit, price_at_creation=, price_source=)` —
        converts a `DetectorHit` into a persisted `Signal`. Idempotent via
        `(fingerprint, signal_date)` upsert. Optionally enriches with AI
        analysis (Resolution R6 — never blocks). Persists the spot price
        from the price composer when provided.
    `run_daily_cycle(session)` — full orchestration: ingest flows →
        ingest news (INSTITUTION + ANNOUNCEMENT) → fetch spot prices →
        classify + persist regime snapshot → run all detectors →
        build_signal per hit. Returns a summary dict.

Anti-drift rules installed by this stage:
    D12 — `build_signal` calls AI OPTIONALLY: upsert first (so re-runs are
          idempotent and don't waste OpenRouter calls), then call AI only on
          newly-inserted rows. AI failure is non-fatal — Signal persists with
          NULL ai_analysis / confidence / expires_at.
    D13 — `run_daily_cycle` wraps every detector call in try/except. One bad
          detector cannot kill the cycle — log + continue. Same pattern
          extends to spot-price fetches (issue #34): a failed fetch persists
          NULL `price_at_creation`, and the backfill script picks it up.
    D14 — `run_daily_cycle` does NOT commit. Caller (the scheduler in #45,
          or the admin route in #47) owns the transaction boundary, same
          contract as `pipeline/ingestor.py`.

Issue #34 (resolved 2026-04-24): spot price is fetched once per asset at
the start of every cycle via `pipeline.prices.get_spot_price_with_source`,
which tries SoSoValue primary then Binance fallback. The source tag is
persisted on `Signal.price_source` (a dedicated String column from the
Stage 07 migration) so Stage 08 outcome evaluation can pin the +24h /
+72h lookup to the same provider.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, Literal

import structlog
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from etfpulse.adapters.openrouter import openrouter_client
from etfpulse.adapters.sosovalue import SoSoValueError
from etfpulse.models import (
    REGIME_MACRO_EVENTS_KEY,
    NewsCategory,
    RegimeSnapshot,
    Signal,
)
from etfpulse.pipeline.analysis import AI_PROMPT_VERSION, compute_expires_at
from etfpulse.pipeline.detectors import ALL_DETECTORS, DetectorHit
from etfpulse.pipeline.ingestor import ingest_etf_flows, ingest_news
from etfpulse.pipeline.news_context import NewsContextItem, gather_news_context
from etfpulse.pipeline.prices import PriceSource, get_spot_price_with_source
from etfpulse.pipeline.regime_monitor import RegimeClassification, classify_regime

# Categories ingested every cycle. INSTITUTION feeds AI explanations of
# institutional flow context; ANNOUNCEMENT picks up regulator/issuer news that
# moves macro posture. Other categories (KOL, crypto-stock) are too noisy to
# justify the quota cost in Phase 1.
_NEWS_CATEGORIES: tuple[NewsCategory, ...] = (
    NewsCategory.INSTITUTION,
    NewsCategory.ANNOUNCEMENT,
)

log = structlog.get_logger()

_ASSETS: tuple[Literal["BTC", "ETH"], ...] = ("BTC", "ETH")


async def build_signal(
    session: AsyncSession,
    hit: DetectorHit,
    price_at_creation: Decimal | None = None,
    price_source: PriceSource | None = None,
    regime: RegimeClassification | None = None,
    news_context: list[NewsContextItem] | None = None,
) -> Signal | None:
    """Upsert a Signal from a detector hit; enrich with AI analysis if available.

    Returns the persisted Signal on a NEW insert, or None if a row with the
    same (fingerprint, signal_date) already existed (idempotent skip).

    AI enrichment is best-effort (R6 / D12): if `openrouter_client.analyze`
    returns None, the Signal stays in the DB with NULL ai_analysis. The
    caller should not retry the AI call here — that's the next daily cycle's
    problem (and a future enricher job's, if we add one).

    `price_at_creation` + `price_source` (issue #34): the caller is expected
    to fetch once per asset per cycle via `pipeline.prices` and pass the
    matching price in here. If `price_at_creation` is None (both providers
    failed), the Signal persists with NULL price — the backfill script
    picks it up later. The source string, when present, is persisted on the
    dedicated `Signal.price_source` column so Stage 08 can match the
    provider on outcome evaluation.

    `regime` + `news_context` (Stage 7-P6, closes #32): when supplied, both
    are (a) embedded in `trigger_data` for audit + future re-analysis, and
    (b) forwarded to `openrouter_client.analyze` as additional prompt
    context. `Signal.ai_prompt_version` is stamped from the analysis
    module's `AI_PROMPT_VERSION` so the track-record query can compare
    apples-to-apples across prompt revisions.
    """
    # Augment trigger_data with regime + news (defensive copy — DetectorHit
    # is frozen and we don't want to surprise other callers by mutating it).
    persisted_trigger_data: dict[str, Any] = dict(hit.trigger_data)
    if regime is not None:
        persisted_trigger_data["regime_at_creation"] = {
            "regime": regime.regime.value,
            "signal_posture": regime.signal_posture.value,
            "confidence": regime.confidence,
            "macro_events_nearby": list(regime.macro_events_nearby),
        }
    if news_context:
        persisted_trigger_data["news_context"] = news_context

    stmt = (
        insert(Signal)
        .values(
            signal_type=hit.signal_type,
            asset=hit.asset,
            trigger_data=persisted_trigger_data,
            fingerprint=hit.fingerprint,
            signal_date=hit.signal_date,
            price_at_creation=price_at_creation,
            price_source=price_source,
            ai_prompt_version=AI_PROMPT_VERSION,
            # ai_analysis, confidence, expires_at NULL at insert; updated
            # below if AI enrichment succeeds.
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

    # Pass the ORIGINAL hit.trigger_data to the AI prompt — not the augmented
    # `persisted_trigger_data`. Regime + news are surfaced via dedicated
    # prompt sections; duplicating them inside the trigger-data block would
    # encourage the model to double-cite.
    analysis = await openrouter_client.analyze(
        signal_type=hit.signal_type,
        asset=hit.asset,
        trigger_data=hit.trigger_data,
        regime=regime,
        news_context=news_context,
        # Stage 8-P1 — anchor the v3 entry/stop/target rules on the real
        # spot price we already fetched for `Signal.price_at_creation`.
        # When None (both providers failed), the system prompt instructs
        # the model to return null entry/stop/target rather than guess.
        current_price=price_at_creation,
    )
    if analysis is not None:
        # `mode="json"` so the JSONB column gets a JSON-friendly dict
        # (Decimal → str), while the column-level fields below get the
        # native Decimal directly off the schema. Same value flowing into
        # two destinations — JSONB for audit, columns for queryable
        # canonical projection. Mirrors the `confidence` pattern.
        signal.ai_analysis = analysis.model_dump(mode="json")
        signal.confidence = analysis.confidence
        signal.expires_at = compute_expires_at(analysis.time_horizon)
        # Stage 8-P1 — mirror the AI-suggested levels onto dedicated columns
        # for the outcome evaluator + indexable queries. Each is None when
        # the AI suggested "wait" OR declined to volunteer a level (the
        # validator drops non-positive + wait-mode prices).
        signal.entry_price = analysis.entry_price
        signal.stop_price = analysis.stop_price
        signal.target_price = analysis.target_price

    log.info(
        "signal_built",
        id=signal.id,
        signal_type=signal.signal_type,
        asset=signal.asset,
        ai_present=analysis is not None,
        ai_prompt_version=signal.ai_prompt_version,
    )
    return signal


async def run_daily_cycle(session: AsyncSession) -> dict[str, Any]:
    """Full daily orchestration. Returns a summary suitable for admin/logging.

    Steps:
        1. Ingest ETF flows for each asset. Any `SoSoValueError` (quota, rate
           limit, network, 5xx) is caught and recorded — the cycle continues
           with whatever data is already in the DB. Resolution R10.
        2. Ingest news for each category in `_NEWS_CATEGORIES`. Same D13
           catch-and-continue contract.
        3. Fetch spot prices once per asset (issue #34).
        4. Classify the current market regime, persist a `RegimeSnapshot`.
           The snapshot must land BEFORE detectors run so `RegimeShiftDetector`
           can compare it against the prior snapshot.
        5. Iterate `ALL_DETECTORS`. Each detector internally handles its own
           assets. Per D13, every `detect()` call is wrapped in try/except
           so one buggy detector cannot kill the cycle.
        6. For each hit, call `build_signal`. New rows are counted as
           `signals_new`; idempotent skips as `signals_duplicate`.

    The session is NOT committed (D14) — caller owns the transaction.
    """
    summary: dict[str, Any] = {
        "ingested": {},
        "ingest_errors": [],
        "news_ingested": {},
        "news_errors": [],
        "prices": {},
        "price_errors": [],
        "regime": None,
        "regime_error": None,
        "detectors_run": 0,
        "detector_errors": [],
        "signals_new": 0,
        "signals_duplicate": 0,
        "ai_succeeded": 0,
        "ai_failed": 0,
    }

    # --- ETF flow ingestion -------------------------------------------------
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

    # --- News ingestion (D13: per-category catch-and-continue) -------------
    # News context feeds the AI prompt in Phase 2 and the regime classifier's
    # velocity score today. A failed category does not abort the cycle —
    # missing news results in a slightly less informed regime and a less
    # contextual AI prompt, both of which degrade gracefully.
    for category in _NEWS_CATEGORIES:
        try:
            count = await ingest_news(session, category=category)
            summary["news_ingested"][category.name] = count
        except SoSoValueError as exc:
            log.warning(
                "daily_cycle_news_ingest_failed",
                category=int(category),
                error_type=type(exc).__name__,
                error=str(exc),
            )
            summary["news_errors"].append((category.name, type(exc).__name__))

    # --- Spot prices (issue #34) --------------------------------------------
    # One fetch per asset at the top of the cycle. Both failures are
    # non-fatal: missing prices result in NULL price_at_creation, which the
    # backfill script can reconcile later from kline history. We don't
    # want a spot-price outage to silently degrade the whole pipeline.
    prices: dict[str, tuple[Decimal, PriceSource]] = {}
    for asset in _ASSETS:
        result = await get_spot_price_with_source(asset)
        if result is not None:
            prices[asset] = result
            summary["prices"][asset] = {
                "source": result[1],
                "price": str(result[0]),
            }
        else:
            summary["price_errors"].append(asset)

    # --- Regime classification + snapshot persistence -----------------------
    # MUST run before detectors so RegimeShiftDetector can compare today's
    # snapshot against the previous one. Failure is non-fatal: detectors that
    # don't depend on the regime still run; RegimeShiftDetector silently
    # skips when no snapshot exists.
    classification: RegimeClassification | None = None
    try:
        classification = await classify_regime(session)
        snapshot = RegimeSnapshot(
            captured_at=datetime.now(UTC),
            regime=classification.regime.value,
            signal_posture=classification.signal_posture.value,
            confidence=classification.confidence,
            reasoning=classification.reasoning,
            macro_events=(
                {REGIME_MACRO_EVENTS_KEY: classification.macro_events_nearby}
                if classification.macro_events_nearby
                else None
            ),
        )
        session.add(snapshot)
        await session.flush()  # assign id; commit stays the caller's job (D14)
        summary["regime"] = {
            "regime": classification.regime.value,
            "signal_posture": classification.signal_posture.value,
            "confidence": classification.confidence,
            "macro_events_nearby": list(classification.macro_events_nearby),
        }
    except Exception as exc:
        log.error(
            "daily_cycle_regime_failed",
            error_type=type(exc).__name__,
            error=str(exc),
            exc_info=exc,
        )
        summary["regime_error"] = type(exc).__name__
        classification = None  # downstream uses None-fallback

    # --- Detection + signal build ------------------------------------------
    # Per-asset news context cache so we don't re-query news_items inside
    # `build_signal` for every hit on the same asset within one cycle.
    # Cache key is `asset` only — safe because every call here uses the
    # default `hours`/`max_items`. If a future caller in this loop ever
    # passes non-default kwargs to `gather_news_context`, widen the key
    # to `(asset, hours, max_items)` first or it will return stale dicts.
    news_cache: dict[str, list[NewsContextItem]] = {}

    async def _news_for(asset: str) -> list[NewsContextItem]:
        if asset not in news_cache:
            news_cache[asset] = await gather_news_context(session, asset)
        return news_cache[asset]

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
            price_tuple = prices.get(hit.asset)
            price = price_tuple[0] if price_tuple else None
            source = price_tuple[1] if price_tuple else None
            news_for_hit = await _news_for(hit.asset)
            signal = await build_signal(
                session,
                hit,
                price_at_creation=price,
                price_source=source,
                regime=classification,
                news_context=news_for_hit,
            )
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
