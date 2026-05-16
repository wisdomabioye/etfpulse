"""Cross-factor confirmation — does an orthogonal signal agree with the AI?

PR I.2 (predictive-robustness pass). The detector hit is the TRIGGER; this
module asks whether each of THREE orthogonal factor families agrees with
the AI's directional claim:

  - Price (recent move in the predicted direction over a fixed window)
  - Regime (current bull/bear regime aligns with the trade direction)
  - News (sentiment vote — reserved for v2; always returns 0 in v1)

Score = COUNT of factors that confirm. Range 0..3 (v1 max realistic = 2
because news always votes 0). The DB CHECK allows 0..3 so v2 can lift the
news ceiling without a migration.

Why news returns 0 in v1: we don't have a sentiment classifier yet
(NewsItem has no `sentiment` column — verified). Shipping a stub keeps
the (3-factor, room-for-4) schema stable and the FE wired so v2 is a pure
internal change.

Why these three factors and not "detector agreement"? Five detectors
mostly share the same flow series, so co-firing isn't independent
evidence. Price/regime/news are ORTHOGONAL families. The original I.2
plan called this out explicitly.

Direction comes from AI's `suggested_action`:
  - "consider long"  → +1 (look for confirming up-moves)
  - "consider short" → -1
  - "wait"           → 0 → no direction → caller leaves confirmation NULL

D14 contract: the orchestrator is read-only and never commits.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Literal, TypedDict, cast

from etfpulse.models import MarketRegime
from etfpulse.pipeline.prices import (
    Asset,
    PriceBar,
    PriceSource,
    get_daily_klines_from_source,
)

# Direction sign used for the agreement check. +1 = long, -1 = short.
# 0 ("wait") short-circuits before any factor scoring runs.
DirectionSign = Literal[-1, 0, 1]
FactorName = Literal["price", "regime", "news"]


class FactorVote(TypedDict):
    """One factor's verdict.

    `vote` is unsigned-by-direction: +1 means "this factor points UP", -1
    means "DOWN", 0 means "no signal" (either insufficient data or
    deliberately neutral in v1, as with news). The orchestrator multiplies
    by direction-sign to compute agreement.

    `reason` is a short string the FE renders on the signal-detail page so
    users see WHY a factor voted the way it did.
    """

    vote: int  # -1 | 0 | 1
    reason: str


@dataclass(frozen=True, slots=True)
class ConfirmationResult:
    """Outcome of one factor-scoring pass.

    `score` is the integer count of confirming factors. NULL is never
    represented here — the caller (`pipeline.signal_builder.build_signal`)
    is responsible for deciding when to skip scoring entirely (AI failed,
    `suggested_action == "wait"`).

    `votes` keeps each factor's vote + reason in a serialisable shape;
    it's persisted on `Signal.factor_votes` JSONB for audit + FE
    "breakdown" rendering.
    """

    score: int  # 0..3
    votes: dict[FactorName, FactorVote]


# ---------------------------------------------------------------------------
# Pure scorers — single source of truth for the per-factor verdict
# ---------------------------------------------------------------------------


def score_regime_factor(
    *,
    signal_type: str,
    regime: MarketRegime | None,
) -> FactorVote:
    """Does the current market regime point in the same direction the
    trade does?

    Mapping (orthodox Wyckoff phases):
      - MARKUP / ACCUMULATION       → +1 (bullish regime)
      - MARKDOWN / DISTRIBUTION     → -1 (bearish regime)
      - UNCERTAIN                   →  0 (no signal)
      - None (no snapshot yet)      →  0 (no signal)

    **Carve-out**: when `signal_type == "regime_shift"`, returns 0 because
    the signal itself IS about a regime change — counting "regime agrees"
    would be self-confirmation. Score it on 2 factors (price + news=0)
    only; max realistic = 1 in v1.
    """
    if signal_type == "regime_shift":
        return FactorVote(
            vote=0,
            reason="self-confirmation excluded (signal is about regime change)",
        )
    if regime is None:
        return FactorVote(vote=0, reason="no regime snapshot available")
    if regime in (MarketRegime.MARKUP, MarketRegime.ACCUMULATION):
        return FactorVote(vote=1, reason=f"regime is {regime.value} (bullish)")
    if regime in (MarketRegime.MARKDOWN, MarketRegime.DISTRIBUTION):
        return FactorVote(vote=-1, reason=f"regime is {regime.value} (bearish)")
    # UNCERTAIN (or any future neutral phase) leaves the vote at 0.
    return FactorVote(vote=0, reason=f"regime is {regime.value} (neutral)")


def score_news_factor() -> FactorVote:
    """v1 stub — news sentiment isn't computed yet.

    Defined as a function (not a constant) so v2 can swap in real logic
    without touching call sites. The denominator in the FE renders as 3
    so the v2 upgrade doesn't shift the ceiling.
    """
    return FactorVote(
        vote=0,
        reason="news sentiment not yet evaluated (reserved for v2)",
    )


async def score_price_factor(
    *,
    asset: Asset,
    price_source: PriceSource,
    reference_time: datetime,
    window_hours: int,
    min_pct: Decimal,
    klines_fetcher=None,
) -> FactorVote:
    """Did the price move in the trade's direction over the lookback window?

    Strategy:
      - Fetch daily klines ending at `reference_time`, padded enough to
        contain the `(reference_time - window_hours)` and `reference_time`
        boundary bars.
      - Compare `close` at the window-end bar vs `close` at the window-
        start bar.
      - If |pct_change| < `min_pct`: vote=0 ("flat — no signal").
      - Else vote=+1 for an up-move, -1 for a down-move.

    Why daily klines: same data source the outcome evaluator uses
    (`pipeline.track_record._evaluate_one` → `get_daily_klines_from_source`),
    so price provenance is consistent across the pipeline. Sub-day
    resolution would need intraday klines (issue #62 — not landed).

    Pinned to the SAME `price_source` the signal's `price_at_creation`
    came from to avoid SoSoValue↔Binance micro-skew between the entry
    price and the confirmation read. Same rationale as
    `_evaluate_one`'s `cast(PriceSource, signal.price_source)` block.

    `klines_fetcher` injection: production calls
    `prices.get_daily_klines_from_source`; tests pass a stub. Same
    seam the outcome evaluator's tests use.

    `min_pct` rejects micro-drift: a 0.1% move over 24h is noise, not
    confirmation. Default lives in `settings.factor_price_min_pct`.

    Returns vote=0 with `reason` set when the fetch fails or no bar
    falls in the window — the factor is "no signal", not "disagreement".
    """
    # Late-binding for the fetcher: production callers omit the kwarg, so we
    # resolve the module-level reference at call time. Two reasons:
    #   1. Test monkeypatches of `etfpulse.pipeline.factors.get_daily_klines_from_source`
    #      take effect (a captured-at-def-time default would freeze the
    #      original ref before the patch fires).
    #   2. Pure unit tests still pass an explicit `klines_fetcher=...` to
    #      avoid module-level patching for one-off canned data.
    fetcher = klines_fetcher if klines_fetcher is not None else get_daily_klines_from_source
    window_ms = window_hours * 3600 * 1000
    end_ms = int(reference_time.timestamp() * 1000)
    start_ms = end_ms - window_ms
    # Pad both ends by 24h so the bars covering the boundaries are
    # included. `_pick_close_at` style selection picks the right bar from
    # the padded set. Pad is FIXED at 24h (one daily-kline bar) regardless
    # of `window_hours` — these are daily klines, so 24h is exactly the
    # minimum gap that still guarantees a bar exists on either side of a
    # weekend or holiday. Scaling pad with `window_hours` would over-fetch
    # without changing the result. Revisit if `window_hours` becomes
    # sub-daily (issue #62 — intraday klines).
    pad_ms = 24 * 3600 * 1000
    bars = await fetcher(
        asset,
        price_source,
        start_time_ms=start_ms - pad_ms,
        end_time_ms=end_ms + pad_ms,
    )
    if not bars:
        return FactorVote(vote=0, reason="price klines unavailable")

    # Walk to find the latest bar opening ≤ start_ms, and the latest
    # bar opening ≤ end_ms. Sorted ascending defensively (provider
    # docs don't contractually guarantee order).
    sorted_bars: list[PriceBar] = sorted(bars, key=lambda b: b.timestamp_ms)
    start_bar = _last_bar_le(sorted_bars, start_ms)
    end_bar = _last_bar_le(sorted_bars, end_ms)
    if start_bar is None or end_bar is None:
        return FactorVote(vote=0, reason="no bar in price-window")
    if start_bar.close <= 0:
        # Defensive: zero or negative close would NaN the pct-change. In
        # practice closes are always positive; this branch catches a
        # future bad-data day rather than crashing the cycle.
        return FactorVote(vote=0, reason="degenerate start price")

    pct_change = (end_bar.close - start_bar.close) / start_bar.close
    abs_pct = abs(pct_change)
    if abs_pct < min_pct:
        return FactorVote(
            vote=0,
            reason=(f"price flat over {window_hours}h: {pct_change:+.2%} < {min_pct:.2%} floor"),
        )
    direction_word = "up" if pct_change > 0 else "down"
    return FactorVote(
        vote=1 if pct_change > 0 else -1,
        reason=f"price moved {direction_word} {pct_change:+.2%} over {window_hours}h",
    )


def _last_bar_le(bars: list[PriceBar], at_ms: int) -> PriceBar | None:
    """Latest bar whose `timestamp_ms <= at_ms`, or None if none qualify.

    `bars` MUST be sorted ascending; the linear walk relies on that.
    Identical pattern to `pipeline.track_record._pick_close_at` — kept
    here as a private helper so the price factor doesn't have to import
    a private symbol from another module.
    """
    candidate: PriceBar | None = None
    for bar in bars:
        if bar.timestamp_ms > at_ms:
            break
        candidate = bar
    return candidate


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------


# Mapping from suggested_action → directional sign. Single source of truth so
# the confirmation orchestrator and any future "direction inference" caller
# stay in lockstep. Mirrors `pipeline.track_record._DIRECTION_FROM_SUGGESTED`
# in semantics but is kept local (different concern: directional sign for
# factor scoring vs `SignalDirection` enum for outcome rows).
_DIRECTION_SIGN: dict[str, DirectionSign] = {
    "consider long": 1,
    "consider short": -1,
}


def direction_sign_from_action(suggested_action: str | None) -> DirectionSign:
    """Convert AI's `suggested_action` to ±1 (or 0 for wait / unknown).

    Returns 0 — not None — so callers can multiply by `vote` to compute
    agreement without a special-case branch. A 0 result means "skip
    scoring entirely" by upstream contract.
    """
    if not suggested_action:
        return 0
    return _DIRECTION_SIGN.get(suggested_action, 0)


async def compute_confirmation(
    *,
    suggested_action: str | None,
    asset: Asset,
    signal_type: str,
    price_source: PriceSource,
    reference_time: datetime,
    regime: MarketRegime | None,
    window_hours: int,
    min_pct: Decimal,
    klines_fetcher=None,
) -> ConfirmationResult | None:
    """Score one signal across all three factors.

    Returns None when there's no direction to confirm (`suggested_action`
    is "wait" / unknown / missing). Caller leaves `Signal.confirmation_score`
    and `Signal.factor_votes` NULL in that case.

    Otherwise returns a `ConfirmationResult` with `score` ∈ [0..3] and
    the per-factor breakdown dict ready to persist on `factor_votes`.

    Score formula: `sum(1 for vote in votes if vote.vote * direction > 0)`.
    Disagreeing factors contribute 0 (their vote is recorded for audit but
    doesn't push the score below zero). News always votes 0 in v1.

    `reference_time` is the moment the score is anchored to — for
    new signals this is `Signal.created_at`; for historical backfill it's
    the (historical) creation time so we look at the price window the
    signal would have seen, not the window today.
    """
    direction = direction_sign_from_action(suggested_action)
    if direction == 0:
        return None

    price_vote = await score_price_factor(
        asset=asset,
        price_source=price_source,
        reference_time=reference_time,
        window_hours=window_hours,
        min_pct=min_pct,
        klines_fetcher=klines_fetcher,
    )
    regime_vote = score_regime_factor(signal_type=signal_type, regime=regime)
    news_vote = score_news_factor()

    votes: dict[FactorName, FactorVote] = {
        "price": price_vote,
        "regime": regime_vote,
        "news": news_vote,
    }
    # Agreement check: sign of `vote` matches sign of `direction`. Cast
    # via the helper so mypy keeps the `DirectionSign` Literal narrow.
    direction_int = cast(int, direction)
    score = sum(1 for v in votes.values() if v["vote"] * direction_int > 0)
    return ConfirmationResult(score=score, votes=votes)


# ---------------------------------------------------------------------------
# Public exports
# ---------------------------------------------------------------------------


__all__ = [
    "ConfirmationResult",
    "FactorName",
    "FactorVote",
    "compute_confirmation",
    "direction_sign_from_action",
    "score_news_factor",
    "score_price_factor",
    "score_regime_factor",
]
