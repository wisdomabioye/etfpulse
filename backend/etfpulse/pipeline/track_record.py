"""Outcome evaluator — turns aged Signals into SignalOutcome rows.

Stage 8-P2.

Public surface:
    `evaluate_pending_outcomes(session) -> dict[str, int]`
        Drains every Signal that's at least `_EVAL_DELAY_HOURS` old AND has
        no SignalOutcome row yet AND has an actionable suggested_action AND
        a non-NULL `price_at_creation`. For each, fetches daily OHLC bars
        from the SAME source as the signal's price_at_creation (no fallback —
        avoids SoSoValue↔Binance micro-skew), computes hit/stop/24h/72h
        prices over the next 72h window, and inserts one SignalOutcome.
        Caller owns the transaction (D14/D18) — same contract as
        `pipeline.signal_builder.build_signal`.

Why daily granularity is enough:
    The detectors run on daily ETF flow data; signals naturally fire at the
    close of a UTC trading day. Sub-day price action might be more accurate
    for hit/stop checks, but daily highs/lows are good enough for a public
    track record and stay within the existing kline plumbing — no new
    intraday-price adapter needed (which would be Wave 2 scope creep).

Why no upper time cutoff:
    The Stage 8 design doc proposed a 24h-72h window for `Signal.created_at`,
    but that loses signals if any eval tick is missed. We instead require
    "at least 72h old" with no upper bound (capped indirectly by kline
    history availability — Binance keeps years, SoSoValue ~3 months). A
    30-day-old signal that was never evaluated will be picked up the next
    time this runs, then never re-evaluated (the SignalOutcome row makes
    the candidate query skip it).

Direction is derived from `signal.ai_analysis["suggested_action"]`:
    - "consider long"  → SignalDirection.LONG
    - "consider short" → SignalDirection.SHORT
    - "wait"           → returns None from `_direction_from_signal` →
                          loop skips with `skipped_no_direction`. (Note:
                          the SQL candidate query does NOT read JSONB, so
                          "wait" signals still appear in `candidates` —
                          drop happens inside the loop.)
    - anything else    → same path as "wait" — log + skip without erroring,
                          defensive against legacy / malformed JSONB.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Literal, cast

import structlog
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from etfpulse.models import Signal, SignalDirection, SignalOutcome
from etfpulse.pipeline.prices import PriceBar, PriceSource, get_daily_klines_from_source

log = structlog.get_logger()


# Wait at least 72h after the signal fires so we have enough kline coverage
# to compute hit/stop AND the 24h+72h close prices in one pass. Evaluating
# at <72h would leave `price_after_72h` and the running max/adverse fields
# permanently NULL, polluting the dashboard's hit-rate aggregate.
_EVAL_DELAY_HOURS = 72

# Window we score against — fixed regardless of when the eval runs. A
# signal that fires at t0 is scored over bars in [t0, t0 + 72h].
_HORIZON_HOURS = 72

# The kline range we ask the source for. Add headroom so a daily bar that
# OPENS just after t0+72h still falls in the response — picking the close
# at t0+72h needs the bar containing that timestamp, which can extend
# slightly past the horizon.
_KLINE_FETCH_PAD_HOURS = 12


# Map suggested_action strings → SignalDirection. Single source of truth so
# direction handling never drifts between this module and the formatter.
_DIRECTION_FROM_SUGGESTED: dict[str, SignalDirection] = {
    "consider long": SignalDirection.LONG,
    "consider short": SignalDirection.SHORT,
}


# Asset Literal mirrors `pipeline.prices.Asset` — narrowing helper below
# guarantees we only forward known assets to the kline fetcher.
_KnownAsset = Literal["BTC", "ETH"]
_KNOWN_ASSETS: frozenset[str] = frozenset({"BTC", "ETH"})


@dataclass(frozen=True, slots=True)
class _OutcomeMetrics:
    """Computed result of scoring a signal's 72h kline window. Frozen so
    callers can't accidentally mutate one and have it affect another row."""

    price_after_24h: Decimal | None
    price_after_72h: Decimal | None
    max_favorable: Decimal | None
    max_adverse: Decimal | None
    hit_target: bool | None
    hit_stop: bool | None


def _compute_metrics(
    *,
    direction: SignalDirection,
    entry: Decimal,
    stop: Decimal | None,
    target: Decimal | None,
    t0_ms: int,
    bars: list[PriceBar],
) -> _OutcomeMetrics | None:
    """Score a sorted list of daily bars against the entry/stop/target.

    Pure function — no DB, no clock — so the test surface is just
    "given these bars and these levels, compute the metrics."

    `max_favorable` / `max_adverse` are unsigned fractions of entry
    (e.g. `0.032` = +3.2% favorable; `0.018` = +1.8% adverse). Storing
    as fractions keeps the dashboard renderer asset-agnostic — no
    division by entry needed at display time.

    Returns None when no bars fall inside [t0, t0+72h] — happens when
    kline history is too short (e.g. evaluating a 30-day-old signal on
    a source that only keeps a week). Caller skips inserting an outcome
    row in that case rather than persisting a misleading all-NULL row
    that would silently pollute the hit-rate aggregate.
    """
    horizon_end_ms = t0_ms + _HORIZON_HOURS * 3600 * 1000
    twenty_four_ms = t0_ms + 24 * 3600 * 1000

    in_window = [b for b in bars if t0_ms <= b.timestamp_ms <= horizon_end_ms]
    if not in_window:
        return None

    # Sort ascending by timestamp — defensive: SoSoValue's docstring notes
    # ordering "isn't contractually specified" so we can't rely on it.
    in_window.sort(key=lambda b: b.timestamp_ms)

    # Pick the bar whose open is the latest one ≤ the target timestamp.
    # `_pick_close_at` returns None if no bar covers the timestamp at all.
    price_after_24h = _pick_close_at(in_window, twenty_four_ms)
    price_after_72h = _pick_close_at(in_window, horizon_end_ms)

    # Max favorable / adverse over the window. For a long: favorable is
    # the highest high above entry; adverse is the lowest low below entry.
    # Mirror for shorts. Both expressed as fractions of entry, clamped at
    # zero (a long that never traded above entry has favorable=0, not
    # negative).
    highest = max(b.high for b in in_window)
    lowest = min(b.low for b in in_window)

    if direction == SignalDirection.LONG:
        max_favorable = max((highest - entry) / entry, Decimal(0))
        max_adverse = max((entry - lowest) / entry, Decimal(0))
        hit_target = (target is not None) and (highest >= target)
        hit_stop = (stop is not None) and (lowest <= stop)
    else:  # SHORT
        max_favorable = max((entry - lowest) / entry, Decimal(0))
        max_adverse = max((highest - entry) / entry, Decimal(0))
        hit_target = (target is not None) and (lowest <= target)
        hit_stop = (stop is not None) and (highest >= stop)

    return _OutcomeMetrics(
        price_after_24h=price_after_24h,
        price_after_72h=price_after_72h,
        max_favorable=max_favorable,
        max_adverse=max_adverse,
        # Surface as None when the level wasn't set (e.g. AI declined to
        # volunteer a stop) — the dashboard uses null to mean "no reference
        # level", not "level not hit". Without this distinction, a "wait"-
        # adjacent partial signal would get a deceptive False.
        hit_target=hit_target if target is not None else None,
        hit_stop=hit_stop if stop is not None else None,
    )


def _pick_close_at(bars: list[PriceBar], at_ms: int) -> Decimal | None:
    """Return the close of the bar whose open is the latest one ≤ `at_ms`.

    Daily bars at SoSoValue/Binance open at 00:00 UTC. A signal firing at
    04:30 UTC on Day 0 wants:
        price_after_24h ≈ close of Day 0 (the bar containing t0+24h, which
                          opens at 00:00 of Day 1 and runs to 00:00 of Day 2)
        price_after_72h ≈ close of Day 2 (the bar containing t0+72h)
    "Bar with the latest open ≤ target" finds those naturally.

    Returns None when no bar precedes the target — happens when the
    requested timestamp is before any bar in the window (shouldn't
    normally trigger because bars are sorted and start ≤ t0)."""
    candidate: PriceBar | None = None
    for bar in bars:
        if bar.timestamp_ms > at_ms:
            break
        candidate = bar
    return candidate.close if candidate is not None else None


def _direction_from_signal(signal: Signal) -> SignalDirection | None:
    """Pull the signal direction out of the AI analysis JSONB.

    Returns None when the JSONB lacks a usable suggested_action OR carries
    'wait' (filtered upstream but defended here too) OR has a string the
    map doesn't recognise (legacy / malformed). The caller skips the
    signal in all three cases."""
    if not signal.ai_analysis:
        return None
    suggested = signal.ai_analysis.get("suggested_action")
    if not isinstance(suggested, str):
        return None
    return _DIRECTION_FROM_SUGGESTED.get(suggested)


def _narrow_asset(asset: str) -> _KnownAsset | None:
    """The kline fetcher's signature wants Literal["BTC","ETH"]. This narrows
    a Signal.asset string at runtime; unknown assets get logged-and-skipped
    rather than crashing the eval loop."""
    if asset in _KNOWN_ASSETS:
        return cast(_KnownAsset, asset)
    return None


@dataclass(frozen=True, slots=True)
class TrackRecordStat:
    """Cumulative hit-rate stats keyed by confidence floor.

    Stage 8-P8 — feeds the Telegram formatter's "Our signals at confidence
    ≥N hit target Y% of the time" stat line. Computed by
    `get_stats_by_confidence_floor` and cached per-process at the
    delivery-worker layer (~10min TTL) so a tick rendering 100 messages
    pays for one DB roundtrip, not 100.

    `by_floor[N]` is `(targeted, hits)` — both COUNTs CUMULATIVE across
    all signals with confidence >= N (i.e. floor=7 sums confidence 7+8+9+10).
    Targeted is the denominator (signals where AI set a target);
    hits is the numerator (signals that hit it).

    `hit_rate_pct(N)` returns the integer percent for floor N, or None
    when no signal in that cohort had a target — same null-vs-zero
    convention as `/api/track-record.summary.hit_rate_pct`.
    """

    by_floor: dict[int, tuple[int, int]]

    def hit_rate_pct(self, confidence_floor: int) -> int | None:
        targeted, hits = self.by_floor.get(confidence_floor, (0, 0))
        if targeted == 0:
            return None
        return round((hits / targeted) * 100)

    def targeted_count(self, confidence_floor: int) -> int:
        """Cohort size — count of signals with confidence >= floor that had
        a target set. Useful for callers that want to render "N signals" too."""
        targeted, _ = self.by_floor.get(confidence_floor, (0, 0))
        return targeted


async def get_stats_by_confidence_floor(session: AsyncSession) -> TrackRecordStat:
    """Build the cumulative-by-floor snapshot in ONE GROUP BY query.

    Reads `signal_outcomes` (filtered to `evaluated_at IS NOT NULL` —
    same defensive filter as `/api/track-record` and `/api/dashboard/stats`).
    Cumulates in Python — Postgres window functions could do this in SQL
    but the Python loop is 10 iterations max (confidence is 1..10) and
    keeps the SQL trivially auditable.
    """
    stmt = (
        select(
            SignalOutcome.confidence,
            func.count().filter(SignalOutcome.hit_target.is_not(None)).label("targeted"),
            func.count().filter(SignalOutcome.hit_target.is_(True)).label("hits"),
        )
        .select_from(SignalOutcome)
        .where(SignalOutcome.evaluated_at.is_not(None))
        .group_by(SignalOutcome.confidence)
    )
    rows = (await session.execute(stmt)).all()
    raw: dict[int, tuple[int, int]] = {row.confidence: (row.targeted, row.hits) for row in rows}

    by_floor: dict[int, tuple[int, int]] = {}
    cum_targeted = 0
    cum_hits = 0
    # Walk 10 → 1 so each floor's tuple is the sum of itself + everything above.
    for c in range(10, 0, -1):
        if c in raw:
            t, h = raw[c]
            cum_targeted += t
            cum_hits += h
        by_floor[c] = (cum_targeted, cum_hits)

    return TrackRecordStat(by_floor=by_floor)


async def get_recent_outcomes(session: AsyncSession, *, limit: int = 5) -> list[SignalOutcome]:
    """Most recent N evaluated outcomes, newest-first by `evaluated_at DESC`.

    Stage 8-P9 — feeds the Telegram `/track-record` command's "Last N"
    section. Same `evaluated_at IS NOT NULL` filter as the rest of the
    track-record surface (`/api/track-record`, `/api/dashboard/stats`)
    so an unevaluated row leaked by a future writer never reaches the bot.

    Returns empty list when no outcomes exist yet (cold-boot before any
    signal ages past 72h) — caller renders a "no outcomes yet" caption.
    """
    stmt = (
        select(SignalOutcome)
        .where(SignalOutcome.evaluated_at.is_not(None))
        .order_by(SignalOutcome.evaluated_at.desc(), SignalOutcome.id.desc())
        .limit(limit)
    )
    result = await session.execute(stmt)
    return list(result.scalars().all())


async def evaluate_pending_outcomes(session: AsyncSession) -> dict[str, int]:
    """Score every aged signal that doesn't yet have an outcome.

    Returns a per-tick summary dict — same convention as
    `pipeline.delivery.send_pending_deliveries`. Useful for admin/log
    visibility: how many candidates, how many actually got an outcome
    inserted, how many were skipped and why.

    Does NOT commit (D14/D18). Caller's session wrapper owns the
    transaction boundary — the scheduler job in P3 wraps a fresh
    `async_session()` exactly like `_run_cycle_with_session`.
    """
    summary = {
        "candidates": 0,
        "evaluated": 0,
        "skipped_no_direction": 0,
        "skipped_unknown_asset": 0,
        "skipped_no_klines": 0,
        "skipped_no_bars_in_window": 0,
        "errored": 0,
    }

    cutoff = datetime.now(UTC) - timedelta(hours=_EVAL_DELAY_HOURS)

    # LEFT JOIN to filter out signals that already have an outcome row,
    # rather than a NOT IN subquery — cheaper plan + clearer.
    stmt = (
        select(Signal)
        .outerjoin(SignalOutcome, SignalOutcome.signal_id == Signal.id)
        .where(SignalOutcome.id.is_(None))
        .where(Signal.created_at <= cutoff)
        .where(Signal.price_at_creation.is_not(None))
        .where(Signal.confidence.is_not(None))
        .order_by(Signal.created_at)
    )
    result = await session.execute(stmt)
    signals = list(result.scalars().all())
    summary["candidates"] = len(signals)

    for signal in signals:
        # Per-signal try/except mirrors `run_daily_cycle`'s D13 contract: a
        # single corrupt signal must NOT abort the entire eval cycle and
        # lose every prior signal's in-flight outcome row. Anything raised
        # here gets logged + counted, the loop moves on. Caller commits
        # whatever DID succeed.
        try:
            await _evaluate_one(session, signal, summary)
        except Exception as exc:  # noqa: BLE001 — D13 catch-and-continue
            summary["errored"] += 1
            log.exception(
                "outcome_eval_signal_failed",
                signal_id=signal.id,
                error_type=type(exc).__name__,
                error=str(exc),
            )

    await session.flush()
    log.info("outcome_eval_summary", **summary)
    return summary


async def _evaluate_one(session: AsyncSession, signal: Signal, summary: dict[str, int]) -> None:
    """Score one candidate signal. Mutates `summary` in place + adds an
    outcome row to `session` on success. Raises on unexpected failures —
    the caller's per-signal try/except absorbs them so the loop continues."""
    direction = _direction_from_signal(signal)
    if direction is None:
        summary["skipped_no_direction"] += 1
        log.info(
            "outcome_eval_skip_no_direction",
            signal_id=signal.id,
            suggested=(signal.ai_analysis or {}).get("suggested_action"),
        )
        return

    asset = _narrow_asset(signal.asset)
    if asset is None:
        summary["skipped_unknown_asset"] += 1
        log.warning("outcome_eval_skip_unknown_asset", signal_id=signal.id, asset=signal.asset)
        return

    # `Signal.price_source` is nullable. When NULL (legacy pre-Stage-7
    # signals), default to "sosovalue" — same primary the live composer
    # would have tried at signal creation. `cast` here because mypy
    # can't narrow the `in (literals,)` runtime check into the Literal
    # alias; the runtime check guarantees the cast is safe.
    if signal.price_source in ("sosovalue", "binance"):
        source = cast(PriceSource, signal.price_source)
    else:
        source = "sosovalue"
        log.info(
            "outcome_eval_default_price_source",
            signal_id=signal.id,
            stored_source=signal.price_source,
        )

    # Bind the t0 epoch ms ONCE per iteration; downstream uses it three
    # times (start_ms, end_ms, _compute_metrics) and recomputing each
    # time would be smelly + risk drift if the conversion ever changes.
    t0_ms = int(signal.created_at.timestamp() * 1000)
    pad_ms = _KLINE_FETCH_PAD_HOURS * 3600 * 1000
    horizon_ms = _HORIZON_HOURS * 3600 * 1000

    # Pad both ends so daily bars whose open straddles the boundary
    # are still included — `_pick_close_at` does the precise selection.
    bars = await get_daily_klines_from_source(
        asset, source, start_time_ms=t0_ms - pad_ms, end_time_ms=t0_ms + horizon_ms + pad_ms
    )
    if bars is None:
        summary["skipped_no_klines"] += 1
        return

    # `Signal.price_at_creation` is the canonical entry baseline —
    # `_compute_metrics` needs a single Decimal here. We've already
    # filtered out NULLs in the candidate query, but mypy can't see
    # that on a column declared `Decimal | None`.
    price_at_signal = signal.price_at_creation
    assert price_at_signal is not None  # noqa: S101 — guarded by .where above

    # Entry for hit/stop computation — prefer the AI-suggested entry
    # (Stage 8-P1), fall back to price_at_creation when AI declined to
    # set one. Same fallback as the formatter would do at display
    # time, so the track record agrees with the rendered alert. Use
    # explicit `is not None` (not `or`) — `Decimal(0)` is falsy and
    # would otherwise silently trigger the fallback if a future writer
    # ever inserts a zero entry_price.
    entry_for_metrics = signal.entry_price if signal.entry_price is not None else price_at_signal

    metrics = _compute_metrics(
        direction=direction,
        entry=entry_for_metrics,
        stop=signal.stop_price,
        target=signal.target_price,
        t0_ms=t0_ms,
        bars=bars,
    )
    if metrics is None:
        summary["skipped_no_bars_in_window"] += 1
        log.warning(
            "outcome_eval_skip_no_bars_in_window",
            signal_id=signal.id,
            bars_returned=len(bars),
        )
        return

    outcome = SignalOutcome(
        signal_id=signal.id,
        # Denormalized from Signal — see SignalOutcome model docstring.
        asset=signal.asset,
        signal_type=signal.signal_type,
        direction=direction.value,
        confidence=signal.confidence,  # filtered NOT NULL above
        entry_price=signal.entry_price,
        stop_price=signal.stop_price,
        target_price=signal.target_price,
        price_at_signal=price_at_signal,
        price_after_24h=metrics.price_after_24h,
        price_after_72h=metrics.price_after_72h,
        hit_target=metrics.hit_target,
        hit_stop=metrics.hit_stop,
        max_favorable=metrics.max_favorable,
        max_adverse=metrics.max_adverse,
        evaluated_at=datetime.now(UTC),
    )
    session.add(outcome)
    summary["evaluated"] += 1
    log.info(
        "outcome_eval_inserted",
        signal_id=signal.id,
        asset=signal.asset,
        direction=direction.value,
        hit_target=metrics.hit_target,
        hit_stop=metrics.hit_stop,
    )
