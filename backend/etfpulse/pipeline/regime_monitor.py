"""Market-regime classifier — Wyckoff-inspired phases driven by ETF-flow trend
and news velocity, with a macro-event safety override.

Single concern: turn the last few days of ingested data into one
`RegimeClassification` value that downstream consumers (regime_shift detector,
/api/regime endpoint, AI prompt v2) can read without re-deriving.

Algorithm (Stage 07-P4 — corrects the off-by-one bug in the stage doc that
left `regime` undefined when no events were nearby):

    1. Compute a directional score from ETF flow 7d trend + news velocity.
    2. Assign `regime` UNCONDITIONALLY from the score thresholds. (Bug fix:
       the stage doc had this inside the `if no_macro_events_nearby` branch,
       so a macro-event window left `regime` undefined.)
    3. Start with the regime's default `signal_posture`.
    4. If macro events are nearby, OVERRIDE the posture to `cautious` —
       leave the regime unchanged. Macro events change how aggressively we
       fire signals; they don't redefine what phase the market is in.

`btc_dominance` would normally feed this scorer too, but it depends on the
SoSoValue `/currencies/sector-spotlight` endpoint that is still un-spikeable
(open issue #25). Until that lands, the classifier ships without dominance —
the `reasoning.dominance` line records the gap explicitly so it's visible
on the dashboard rather than hidden in code.

D14 contract: this module never commits. Callers (run_daily_cycle, /api/regime)
own the transaction boundary.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any

import structlog
from sqlalchemy import desc, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from etfpulse.adapters.sosovalue import (
    MacroEvent,
    SoSoValueError,
    sosovalue_client,
)
from etfpulse.models import ETFFlow, MarketRegime, NewsItem, RegimeSnapshot, SignalPosture

log = structlog.get_logger()

# --- Tunables --------------------------------------------------------------

# Mirrors the asset universe in `pipeline/signal_builder._ASSETS`; kept
# private here so `_compute_flow_score` can zero-fill missing assets in its
# reasoning dict (stable frontend shape per finding H).
_TRACKED_ASSETS: tuple[str, ...] = ("BTC", "ETH")

# Window over which we sum ETF net flows to derive directional bias. 7 days
# is short enough to be responsive, long enough to dampen single-day noise.
_FLOW_WINDOW_DAYS = 7

# News velocity = count of news items in the last N hours. High velocity is
# treated as a "noise" / "event-rich" signal, not directional.
_NEWS_WINDOW_HOURS = 24

# Macro events ±N days from today are considered "nearby" — the SoSoValue
# events endpoint returns the next ~10 events; we filter by date.
_MACRO_NEARBY_WINDOW_DAYS = 2

# Score thresholds. The score is a normalised int in roughly [-100, 100],
# computed as `flow_score + news_score`. Boundaries chosen to give a slight
# bias toward UNCERTAIN under low-conviction conditions.
_THRESHOLD_MARKUP = 40  # > MARKUP / >= ACCUMULATION
_THRESHOLD_ACCUMULATION = 10
_THRESHOLD_DISTRIBUTION = -10
_THRESHOLD_MARKDOWN = -40  # <= MARKDOWN / > DISTRIBUTION

# Default posture per regime. Macro events override CAUTIOUS regardless.
_DEFAULT_POSTURE: dict[MarketRegime, SignalPosture] = {
    MarketRegime.ACCUMULATION: SignalPosture.AGGRESSIVE,
    MarketRegime.MARKUP: SignalPosture.NORMAL,
    MarketRegime.DISTRIBUTION: SignalPosture.NORMAL,
    MarketRegime.MARKDOWN: SignalPosture.AGGRESSIVE,
    MarketRegime.UNCERTAIN: SignalPosture.CAUTIOUS,
}


@dataclass(frozen=True, slots=True)
class RegimeClassification:
    """One regime decision — the unit returned by `classify_regime`.

    `reasoning` is the structured score breakdown that backs the regime + posture
    decision. Persisted as JSONB on `regime_snapshots.reasoning` so the
    dashboard can render *why* without re-running the classifier. Schema is
    open; consumers must tolerate missing keys.

    `confidence` is 1-10, mirroring `Signal.confidence` semantics: 10 = strong
    evidence, 1 = barely above noise. Currently derived from |score| only;
    Phase 2 may incorporate detector agreement.
    """

    regime: MarketRegime
    signal_posture: SignalPosture
    confidence: int
    reasoning: dict[str, Any] = field(default_factory=dict)
    macro_events_nearby: list[str] = field(default_factory=list)


async def classify_regime(session: AsyncSession) -> RegimeClassification:
    """Run the classifier against current DB state + adapter-fetched macro events.

    Network-bound: makes one call to `get_macro_events()` (cached for 24h
    inside the adapter, so repeat invocations within a cycle are free).
    DB-bound: two short queries (flow trend, news velocity).

    Failure modes:
        - SoSoValue down → macro_events_nearby = [], reasoning notes the gap.
          The classifier still produces a regime; posture follows its default.
        - DB empty (no flows yet) → score = 0 → UNCERTAIN regime.
    """
    today = datetime.now(UTC).date()

    flow_score, flow_reasoning = await _compute_flow_score(session, today)
    news_score, news_reasoning = await _compute_news_score(session)
    macro_events_nearby, macro_reasoning = await _fetch_macro_events_nearby(today)

    score = flow_score + news_score
    regime = _regime_from_score(score)
    confidence = _confidence_from_score(score)

    posture = _DEFAULT_POSTURE[regime]
    if macro_events_nearby:
        posture = SignalPosture.CAUTIOUS

    reasoning: dict[str, Any] = {
        "score": score,
        "flow": flow_reasoning,
        "news": news_reasoning,
        "macro": macro_reasoning,
        # btc_dominance is unavailable until SoSoValue sector-spotlight
        # comes back online (open issue #25). Surfacing the gap here means
        # the dashboard shows it explicitly rather than silently dropping it.
        "dominance": {
            "available": False,
            "note": "sector-spotlight endpoint pending — see open issue #25",
        },
    }

    classification = RegimeClassification(
        regime=regime,
        signal_posture=posture,
        confidence=confidence,
        reasoning=reasoning,
        macro_events_nearby=macro_events_nearby,
    )
    log.info(
        "regime_classified",
        regime=regime.value,
        signal_posture=posture.value,
        confidence=confidence,
        score=score,
        macro_events_nearby=len(macro_events_nearby),
    )
    return classification


async def get_latest_regime(session: AsyncSession) -> RegimeSnapshot | None:
    """Most recent persisted `RegimeSnapshot` (or None if the table is empty).

    Shared by the regime_shift detector (which reads it to detect transitions)
    and `/api/regime` (which renders it). Pinning a single helper here avoids
    two callers diverging on what "latest" means.
    """
    stmt = select(RegimeSnapshot).order_by(desc(RegimeSnapshot.captured_at)).limit(1)
    result = await session.execute(stmt)
    return result.scalar_one_or_none()


# ---------------------------------------------------------------------------
# Scoring helpers (private — tests can still hit them directly)
# ---------------------------------------------------------------------------


async def _compute_flow_score(session: AsyncSession, today: date) -> tuple[int, dict[str, Any]]:
    """Score the 7-day net-flow trend across both tracked assets.

    Logic:
        - Sum of net_flow over the last `_FLOW_WINDOW_DAYS` days, per asset,
          INCLUSIVE of `today` (so an N=7 window covers exactly 7 distinct
          dates: `today` and the 6 days before it).
        - Combined sum is normalised to roughly [-50, 50] via a soft cap on
          the dollar magnitude. The cap is intentionally generous so an
          unusually large day doesn't peg the score.
        - Returns (score_int, reasoning_dict).

    `by_asset_usd` always contains an entry per tracked asset (zero-filled
    when there's no data) so frontend rendering sees a stable shape.
    """
    # Inclusive 7-day window: subtract (N-1) so `cutoff` is the earliest
    # included date. `>=` then gives exactly N distinct dates with `today`
    # at the top.
    cutoff = today - timedelta(days=_FLOW_WINDOW_DAYS - 1)
    stmt = (
        select(ETFFlow.asset, func.sum(ETFFlow.total_net_flow_usd))
        .where(ETFFlow.captured_at >= cutoff)
        .group_by(ETFFlow.asset)
    )
    result = await session.execute(stmt)
    sums: dict[str, Decimal] = {asset: total for asset, total in result.all()}

    combined = sum(sums.values(), Decimal(0))
    # Soft cap at ±$5B — anything beyond that pegs the score. Calibrated
    # against typical 7-day BTC+ETH ETF flow magnitudes observed in 2024-25.
    cap = Decimal("5_000_000_000")
    clamped = max(min(combined, cap), -cap)
    score = int((clamped / cap) * 50)

    by_asset_usd = {asset: str(sums.get(asset, Decimal(0))) for asset in _TRACKED_ASSETS}

    return score, {
        "window_days": _FLOW_WINDOW_DAYS,
        "by_asset_usd": by_asset_usd,
        "combined_usd": str(combined),
        "score": score,
    }


async def _compute_news_score(session: AsyncSession) -> tuple[int, dict[str, Any]]:
    """News velocity as a small NEGATIVE additive on the overall score.

    High velocity = lots of headlines = noise → pushes ambiguous regimes
    toward UNCERTAIN. Low/no news leaves the score unchanged. Velocity is
    NOT directional — we don't try to infer sentiment from headlines in
    Phase 1. Window is hour-precision (24h ending now), not date-precision,
    so this helper does not take a `today` parameter — that would imply
    date-aware filtering it does not perform.
    """
    cutoff = datetime.now(UTC) - timedelta(hours=_NEWS_WINDOW_HOURS)
    stmt = select(func.count(NewsItem.id)).where(NewsItem.captured_at >= cutoff)
    velocity = await session.scalar(stmt) or 0

    # Map velocity to a 0..15 negative-only pull. >100 articles in 24h
    # produces the full -15; below 10 produces 0.
    if velocity <= 10:
        score = 0
    elif velocity >= 100:
        score = -15
    else:
        score = -int(((velocity - 10) / 90) * 15)

    return score, {
        "window_hours": _NEWS_WINDOW_HOURS,
        "velocity_count": velocity,
        "score": score,
    }


async def _fetch_macro_events_nearby(
    today: date,
) -> tuple[list[str], dict[str, Any]]:
    """Pull upcoming macro events from SoSoValue and filter to the ±N-day window.

    Soft-failure: any `SoSoValueError` (quota, rate-limit, network) is logged
    and treated as "no events nearby" rather than aborting classification. The
    reasoning dict records the gap so the dashboard isn't deceived into
    thinking "no events" == "definitely safe".
    """
    try:
        events = await sosovalue_client.get_macro_events()
    except SoSoValueError as exc:
        log.warning(
            "regime_macro_fetch_failed",
            error_type=type(exc).__name__,
            error=str(exc),
        )
        return [], {
            "window_days": _MACRO_NEARBY_WINDOW_DAYS,
            "fetch_error": type(exc).__name__,
            "events_nearby": [],
        }

    nearby_labels = _filter_events_nearby(events, today)
    return nearby_labels, {
        "window_days": _MACRO_NEARBY_WINDOW_DAYS,
        "events_nearby": nearby_labels,
        "events_total": len(events),
    }


def _filter_events_nearby(events: list[MacroEvent], today: date) -> list[str]:
    """Return event labels falling within ±_MACRO_NEARBY_WINDOW_DAYS of today.

    Pure helper so tests can hit it without a network mock.
    """
    nearby: list[str] = []
    for event in events:
        delta = abs((event.date - today).days)
        if delta <= _MACRO_NEARBY_WINDOW_DAYS:
            nearby.extend(event.events)
    return nearby


def _regime_from_score(score: int) -> MarketRegime:
    """Map a [-100, 100] int score to a Wyckoff phase.

    Boundaries tuned so a borderline score lands in UNCERTAIN rather than
    flipping between adjacent regimes day-to-day; the regime_shift detector
    fires only on actual transitions, so chattering would create alert spam.
    """
    if score >= _THRESHOLD_MARKUP:
        return MarketRegime.MARKUP
    if score >= _THRESHOLD_ACCUMULATION:
        return MarketRegime.ACCUMULATION
    if score <= _THRESHOLD_MARKDOWN:
        return MarketRegime.MARKDOWN
    if score <= _THRESHOLD_DISTRIBUTION:
        return MarketRegime.DISTRIBUTION
    return MarketRegime.UNCERTAIN


def _confidence_from_score(score: int) -> int:
    """1-10 confidence derived from |score|. Linear ramp; clamped at ends."""
    magnitude = abs(score)
    # 0..50 → 1..10 (0 magnitude = 1, 50 = 10).
    confidence = 1 + int((min(magnitude, 50) / 50) * 9)
    return max(1, min(10, confidence))
