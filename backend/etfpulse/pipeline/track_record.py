"""Outcome evaluator — turns aged Signals into SignalOutcome rows.

Stage 8-P2; v2 horizon-aware rubric shipped in PR B ([[#60]]).

Public surface:
    `evaluate_pending_outcomes(session) -> dict[str, int]`
        Drains every Signal whose stated validity window has ended
        (`Signal.expires_at <= now`) AND has no SignalOutcome row yet AND
        a non-NULL `price_at_creation`. For each, derives the per-signal
        `window_hours = (expires_at - created_at)`, fetches daily OHLC bars
        from the SAME source as the signal's price_at_creation (no fallback —
        avoids SoSoValue↔Binance micro-skew), computes hit/stop and the
        in-window price checkpoints (24h, 72h when inside the window) plus
        `price_at_validity_end` (close at t0 + window_hours), and inserts
        one SignalOutcome stamped with `scoring_version='v2'` + `window_hours`.
        Caller owns the transaction (D14/D18) — same contract as
        `pipeline.signal_builder.build_signal`.

Per-horizon behavior:
    - **scalp (window < 24h)**: skipped with `skipped_scalp_intraday_unsupported`
      counter. Daily klines have no bar inside a 6h window; honest scoring
      requires intraday kline support ([[#62]]). UI buckets scalp signals
      as "intraday data pending" until that lands.
    - **swing (24h ≤ window < 96h)**: full scoring. `price_after_24h` +
      `price_after_72h` populated (where `price_after_72h == price_at_validity_end`
      by construction).
    - **position (window ≥ 96h)**: full scoring. All three checkpoints
      populated — `price_after_24h`, `price_after_72h` (interim mid-trade
      reading), and `price_at_validity_end` (close at the stated end).

Why daily granularity for swing/position:
    The detectors run on daily ETF flow data; swing/position signals
    naturally fire at the close of a UTC trading day. Sub-day price action
    might be more accurate for hit/stop checks but daily highs/lows are
    good enough for a public track record and stay within the existing
    kline plumbing. Scalp signals do NOT have this property — see [[#62]].

Why no upper time cutoff:
    A signal that was never evaluated (e.g. eval ticks missed during an
    outage) gets picked up the next time this runs, then never re-evaluated
    (the SignalOutcome row makes the candidate query skip it). Bounded
    indirectly by kline history availability (Binance keeps years,
    SoSoValue ~3 months).

Legacy rows (pre-PR-B): carry NULL `scoring_version` / `window_hours` /
    `price_at_validity_end`. They were scored against a fixed 72h window
    regardless of the signal's stated `time_horizon`. The reader treats
    NULL `scoring_version` as legacy/'v1' for bucket filtering. Wiping +
    re-evaluating these rows under the v2 rubric is tracked in [[#61]].

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
from datetime import UTC, datetime
from decimal import Decimal
from typing import Literal, cast

import structlog
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from etfpulse.constants import MARKET_ASSET
from etfpulse.models import Signal, SignalDirection, SignalOutcome
from etfpulse.pipeline.prices import PriceBar, PriceSource, get_daily_klines_from_source

log = structlog.get_logger()


# PR B (issue #60 v2 rubric): the window is derived per-signal from
# `(Signal.expires_at - Signal.created_at)` so scalp/swing/position signals
# are scored against the window the AI committed to, not a fixed 72h ruler.
# `_window_hours_for(signal)` is the canonical reader; the candidate query
# uses `Signal.expires_at <= now` as the readiness gate.

# Minimum window where daily klines provide enough resolution. Below this,
# the daily bar containing t0 closes ~14h+ past the signal's validity end,
# so any "close at validity end" pick would lie. Scalp signals (6h validity)
# fall below this floor and are explicitly skipped (#62). When intraday
# kline support ships, lower this floor or remove the gate entirely.
_MIN_SCORABLE_WINDOW_HOURS = 24

# Active rubric version stamped on every new outcome row. Pre-PR-B rows
# carry NULL `scoring_version` — the reader treats NULL as v1 / legacy
# (scored against the fixed 72h window the constants used to encode).
_SCORING_VERSION = "v2"

# The kline fetch range. Padded on both ends so a daily bar that opens
# just after t0+window still falls in the response — picking the close
# at the window end needs the bar containing that timestamp, which can
# extend slightly past the horizon.
_KLINE_FETCH_PAD_HOURS = 12


def _window_hours_for(signal: Signal) -> int | None:
    """Derive the scoring window from `(expires_at - created_at)`.

    Reads the STORED fact (`expires_at`) rather than re-deriving from
    `ai_analysis["time_horizon"]` — keeps the evaluator robust against
    later changes to `_HORIZON_TO_DURATION` in pipeline/analysis.py.
    A retroactive map change must NOT silently re-score historical rows.

    Returns None when:
      - `expires_at` is missing (AI-failed signal — already filtered upstream)
      - the difference is <= 0 (clock skew, time-travel, future change to
        compute_expires_at producing pre-creation timestamps)
    The evaluator counts these as `skipped_invalid_window` rather than
    treating them as "score over 0h" (which would either crash or produce
    nonsense).
    """
    if signal.expires_at is None or signal.created_at is None:
        return None
    delta_seconds = (signal.expires_at - signal.created_at).total_seconds()
    if delta_seconds <= 0:
        return None
    return round(delta_seconds / 3600)


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
    """Computed result of scoring a signal against its native window. Frozen
    so callers can't accidentally mutate one and have it affect another row.

    `price_at_validity_end` is the close at `t0 + window_hours` — the
    canonical "outcome price" under PR B's v2 rubric. The legacy
    `price_after_24h` / `price_after_72h` columns are populated only when
    the respective checkpoint falls inside the signal's window:
        - swing (72h): both populated; price_after_72h == price_at_validity_end
        - position (168h): both populated; price_at_validity_end is the close at 7d
        - scalp (6h): not scored in PR B — see issue #62
    Outside-window checkpoints stay NULL so consumers can render "not
    applicable for this horizon" rather than a misleading price.
    """

    price_after_24h: Decimal | None
    price_after_72h: Decimal | None
    price_at_validity_end: Decimal | None
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
    window_hours: int,
    bars: list[PriceBar],
) -> _OutcomeMetrics | None:
    """Score a sorted list of daily bars against entry/stop/target over the
    signal's NATIVE window. PR B — `window_hours` is per-signal (derived
    from `expires_at - created_at`); pre-PR-B callers passed a constant 72.

    Pure function — no DB, no clock — so the test surface is just
    "given these bars, these levels, and this window, compute the metrics."

    `max_favorable` / `max_adverse` are unsigned fractions of entry
    (e.g. `0.032` = +3.2% favorable; `0.018` = +1.8% adverse). Storing
    as fractions keeps the dashboard renderer asset-agnostic — no
    division by entry needed at display time.

    Returns None when no bars fall inside [t0, t0+window_hours] — happens
    when kline history is too short (e.g. evaluating a 30-day-old signal
    on a source that only keeps a week) OR when the window is sub-daily
    and no daily bar opens inside it. Caller skips inserting an outcome
    row in that case rather than persisting a misleading all-NULL row.

    `price_after_24h` and `price_after_72h` are populated only when those
    checkpoints fall inside the signal's window — otherwise NULL. This
    keeps the legacy column semantics ("price at exactly +24h / +72h")
    intact while the new `price_at_validity_end` carries the horizon-aware
    fact.
    """
    horizon_end_ms = t0_ms + window_hours * 3600 * 1000
    twenty_four_ms = t0_ms + 24 * 3600 * 1000
    seventy_two_ms = t0_ms + 72 * 3600 * 1000

    in_window = [b for b in bars if t0_ms <= b.timestamp_ms <= horizon_end_ms]
    if not in_window:
        return None

    # Sort ascending by timestamp — defensive: SoSoValue's docstring notes
    # ordering "isn't contractually specified" so we can't rely on it.
    in_window.sort(key=lambda b: b.timestamp_ms)

    # `_pick_close_at` returns the close of the bar whose open is the
    # latest one ≤ the target timestamp, or None when no such bar exists.
    # Populate legacy checkpoints only when in-window — outside-window
    # values would be coincidental noise.
    price_after_24h = (
        _pick_close_at(in_window, twenty_four_ms) if twenty_four_ms <= horizon_end_ms else None
    )
    price_after_72h = (
        _pick_close_at(in_window, seventy_two_ms) if seventy_two_ms <= horizon_end_ms else None
    )
    price_at_validity_end = _pick_close_at(in_window, horizon_end_ms)

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
        price_at_validity_end=price_at_validity_end,
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


def compute_hit_rate_pct(hits: int, targeted: int) -> float | None:
    """Canonical hit-rate computation — single source of truth across the codebase.

    Returns the percent (0.0–100.0, rounded to 2dp) of `hits / targeted`, or
    `None` when `targeted == 0`. The None-vs-zero distinction is load-bearing:
    a cohort with no targeted signals has UNKNOWN hit rate, not 0%, and the
    public/Telegram surfaces render the None as "pending"/"not yet computable"
    rather than a misleading "0%".

    Callers that want an integer percent (Telegram bot, TrackRecordStat) wrap
    the result with `round()` at render time — precision is a presentation
    concern, not the helper's job.

    Inputs are non-negative ints by contract; `targeted < 0` or `hits > targeted`
    would indicate a caller bug and is NOT defensively rejected here (would
    mask the bug rather than fix it).
    """
    if targeted == 0:
        return None
    return round((hits / targeted) * 100, 2)


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
        pct = compute_hit_rate_pct(hits, targeted)
        return round(pct) if pct is not None else None

    def targeted_count(self, confidence_floor: int) -> int:
        """Cohort size — count of signals with confidence >= floor that had
        a target set. Useful for callers that want to render "N signals" too."""
        targeted, _ = self.by_floor.get(confidence_floor, (0, 0))
        return targeted


# Horizon labels for the bucketed track-record (PR B). Mirror the AI's
# `time_horizon` enum from `pipeline/analysis.py:_HORIZON_TO_DURATION`
# (scalp 6h / swing 72h / position 168h). Ranges are tolerant of small
# `round()` drift inside `_window_hours_for` — exact equality would brittle
# on a future 7d-vs-168h definition change.
HorizonLabel = Literal["scalp", "swing", "position", "legacy"]
# Canonical ordering of horizon buckets — also re-exported so the
# route/route-test/UI layers iterate without their own duplicate tuple
# (DRY: a future "intraday" bucket lands in one place).
HORIZON_LABELS: tuple[HorizonLabel, ...] = ("scalp", "swing", "position", "legacy")


def horizon_label_for(window_hours: int | None) -> HorizonLabel:
    """Bucket `window_hours` into one of {scalp, swing, position, legacy}.

    `None` → "legacy" (rows written before PR B v2 rubric; scored against
    the fixed 72h window).

    Ranges are tolerant rather than exact equality:
        - scalp: window < 24h (matches the AI's scalp slot at 6h)
        - swing: 24h <= window < 96h (matches the swing slot at 72h)
        - position: window >= 96h (matches the position slot at 168h)

    The 96h boundary is the geometric mean-ish midpoint between swing (72h)
    and position (168h). Future horizon additions land in the right bucket
    without code change as long as they don't straddle a boundary by
    rounding (12h scalp → still scalp; 100h position → still position).
    """
    if window_hours is None:
        return "legacy"
    if window_hours < 24:
        return "scalp"
    if window_hours < 96:
        return "swing"
    return "position"


@dataclass(frozen=True, slots=True)
class TrackRecordStatByHorizon:
    """PR B (#60) — cumulative-by-floor hit-rate, bucketed by horizon.

    Sibling of `TrackRecordStat`. Two parallel surfaces:
        - `TrackRecordStat` (existing): mixed-horizon cumulative — kept for
           legacy callers (Telegram /performance, internal cohort filtering
           after additional horizon-slicing in the caller).
        - `TrackRecordStatByHorizon` (new): per-(floor, horizon) cumulative
           — feeds the bucketed track-record API and the per-signal alert
           that now honors the signal's own horizon.

    `by_floor_and_horizon[(N, label)]` is `(targeted, hits)`. The dict
    always contains entries for every (1..10, label) combination, with
    `(0, 0)` for empty cohorts — callers don't need to defensively check
    `.get()` and the UI gets a stable shape across deploys.

    `hit_rate_pct(N, label)` mirrors the flat version's API for symmetry.
    """

    by_floor_and_horizon: dict[tuple[int, HorizonLabel], tuple[int, int]]

    def hit_rate_pct(self, confidence_floor: int, horizon: HorizonLabel) -> int | None:
        targeted, hits = self.by_floor_and_horizon.get((confidence_floor, horizon), (0, 0))
        pct = compute_hit_rate_pct(hits, targeted)
        return round(pct) if pct is not None else None

    def targeted_count(self, confidence_floor: int, horizon: HorizonLabel) -> int:
        targeted, _ = self.by_floor_and_horizon.get((confidence_floor, horizon), (0, 0))
        return targeted


async def get_stats_by_confidence_floor_and_horizon(
    session: AsyncSession,
) -> TrackRecordStatByHorizon:
    """Build the cumulative-by-(floor, horizon) snapshot in ONE GROUP BY.

    Same shape as `get_stats_by_confidence_floor` but groups by
    `window_hours` too. Legacy rows (NULL `window_hours`) bucket as
    "legacy" — explicit, queryable, doesn't dilute the per-horizon
    numbers with mis-scored history.

    Cumulates in Python (10 floors × 4 labels = 40 iterations) — same
    rationale as the flat version: trivial loop, SQL stays auditable.
    """
    stmt = (
        select(
            SignalOutcome.confidence,
            SignalOutcome.window_hours,
            func.count().filter(SignalOutcome.hit_target.is_not(None)).label("targeted"),
            func.count().filter(SignalOutcome.hit_target.is_(True)).label("hits"),
        )
        .select_from(SignalOutcome)
        .where(SignalOutcome.evaluated_at.is_not(None))
        .group_by(SignalOutcome.confidence, SignalOutcome.window_hours)
    )
    rows = (await session.execute(stmt)).all()

    # Stage 1 — flatten (confidence, window_hours) rows into
    # (confidence, label) pairs. Multiple distinct window_hours values can
    # share a label (e.g. 168 and 240 both → "position") so we accumulate.
    per_confidence_label: dict[tuple[int, HorizonLabel], tuple[int, int]] = {}
    for row in rows:
        label = horizon_label_for(row.window_hours)
        key = (row.confidence, label)
        prior_t, prior_h = per_confidence_label.get(key, (0, 0))
        per_confidence_label[key] = (prior_t + row.targeted, prior_h + row.hits)

    # Stage 2 — cumulate by floor (10 → 1) within each label independently.
    # Pre-seed every (floor, label) so consumers see (0, 0) for empty cohorts.
    by_floor_and_horizon: dict[tuple[int, HorizonLabel], tuple[int, int]] = {}
    for label in HORIZON_LABELS:
        cum_targeted = 0
        cum_hits = 0
        for c in range(10, 0, -1):
            t, h = per_confidence_label.get((c, label), (0, 0))
            cum_targeted += t
            cum_hits += h
            by_floor_and_horizon[(c, label)] = (cum_targeted, cum_hits)

    return TrackRecordStatByHorizon(by_floor_and_horizon=by_floor_and_horizon)


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


async def evaluate_pending_outcomes(
    session: AsyncSession, *, limit: int | None = None
) -> dict[str, int]:
    """Score aged signals that don't yet have an outcome.

    Returns a per-tick summary dict — same convention as
    `pipeline.delivery.send_pending_deliveries`. Useful for admin/log
    visibility: how many candidates, how many actually got an outcome
    inserted, how many were skipped and why.

    `limit` (None = unlimited) caps the batch size to bound upstream
    klines API spend per invocation. Important on fresh deploys with
    a large stranded backlog: a single tick processing 200+ candidates
    would fire 200+ klines fetches sequentially, risking SoSoValue's
    100 req/min cap. The scheduler wrapper passes
    `settings.outcome_eval_batch_limit`; the admin route exposes it
    as a query param. Steady-state ticks usually have ≤ a handful of
    candidates so the cap is rarely binding.

    `summary["remaining"]` reports candidates left over after the limit
    truncation — operators see "we processed 50 of 247, click again".
    `0` means the backlog is drained and the next scheduled tick has
    nothing to do.

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
        # PR B (#60) — per-signal window derived from expires_at. These
        # counters surface horizons we can't honestly score yet so an
        # operator can see "X scalps skipped, intraday support pending."
        "skipped_invalid_window": 0,
        "skipped_scalp_intraday_unsupported": 0,
        "errored": 0,
        # Eligible candidates left over after the limit truncation. 0 in the
        # unlimited / fully-drained case; > 0 means another tick (or another
        # button click) will pick up where this one left off.
        "remaining": 0,
    }

    now = datetime.now(UTC)

    # PR B (#60) — readiness gate is per-signal: a signal is a candidate
    # when its OWN stated validity window has ended (`expires_at <= now`).
    # Replaces the pre-PR-B fixed `created_at <= now - 72h` filter which
    # mis-aligned scalp (eligible too late) and position (eligible too
    # early) signals. `expires_at IS NOT NULL` subsumes the old
    # `confidence IS NOT NULL` proxy — AI-failed signals lack both.
    # LEFT JOIN filters out signals that already have an outcome row,
    # rather than a NOT IN subquery — cheaper plan + clearer.
    # PR F.3 — `asset != MARKET_ASSET` excludes regime_shift MARKET signals
    # from outcome scoring. They carry no asset to price against (no BTC/ETH
    # klines apply to "the market as a whole"), so any score would be
    # arbitrary. The `price_at_creation.is_not(None)` filter incidentally
    # excludes them today (no spot price fetched for MARKET), but making it
    # explicit keeps the intent decoupled from that coincidence.
    base_filters = (
        Signal.asset != MARKET_ASSET,
        Signal.price_at_creation.is_not(None),
        Signal.expires_at.is_not(None),
        Signal.expires_at <= now,
    )
    stmt = (
        select(Signal)
        .outerjoin(SignalOutcome, SignalOutcome.signal_id == Signal.id)
        .where(SignalOutcome.id.is_(None))
        .where(*base_filters)
        .order_by(Signal.created_at)
    )
    if limit is not None:
        stmt = stmt.limit(limit)
    result = await session.execute(stmt)
    signals = list(result.scalars().all())
    summary["candidates"] = len(signals)

    # Compute `remaining` only when a limit was applied AND the batch
    # filled it — that's the only case where there COULD be more. Avoids
    # an unnecessary COUNT roundtrip on the (common) unlimited path and
    # on the (common) under-limit-batch path.
    if limit is not None and len(signals) == limit:
        total_eligible = await session.scalar(
            select(func.count(Signal.id))
            .outerjoin(SignalOutcome, SignalOutcome.signal_id == Signal.id)
            .where(SignalOutcome.id.is_(None))
            .where(*base_filters)
        )
        summary["remaining"] = max(0, (total_eligible or 0) - len(signals))

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

    # PR B (#60) — derive the per-signal scoring window. None means either
    # NULL expires_at (filtered upstream, defensive recheck) or non-positive
    # delta (clock skew / future bug); treat both as invalid and count.
    window_hours = _window_hours_for(signal)
    if window_hours is None:
        summary["skipped_invalid_window"] += 1
        log.warning(
            "outcome_eval_skip_invalid_window",
            signal_id=signal.id,
            created_at=str(signal.created_at),
            expires_at=str(signal.expires_at),
        )
        return

    # PR B (#60 + #62) — scalp signals (6h validity) have no daily bar
    # inside their window. Skip explicitly with a dedicated counter rather
    # than letting `_compute_metrics` return None via the generic
    # "no bars in window" path — distinguishes "kline data missing" from
    # "window is sub-daily by design." Lift this gate when intraday kline
    # support lands (#62).
    if window_hours < _MIN_SCORABLE_WINDOW_HOURS:
        summary["skipped_scalp_intraday_unsupported"] += 1
        log.info(
            "outcome_eval_skip_scalp_intraday_unsupported",
            signal_id=signal.id,
            window_hours=window_hours,
            note="see issue #62 — daily klines insufficient for sub-day windows",
        )
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

    # Bind t0 + horizon ms ONCE per iteration; downstream uses both several
    # times. Horizon is now per-signal (#60 v2 rubric).
    t0_ms = int(signal.created_at.timestamp() * 1000)
    pad_ms = _KLINE_FETCH_PAD_HOURS * 3600 * 1000
    horizon_ms = window_hours * 3600 * 1000

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
        window_hours=window_hours,
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
        # PR B (#60) — v2 fields stamped on every new outcome. NULL on
        # pre-PR-B rows = legacy 72h scoring; the public reader uses
        # COALESCE(scoring_version, 'v1') semantics. `window_hours` is the
        # rubric's data fact; `scoring_version` is the rubric pin.
        price_at_validity_end=metrics.price_at_validity_end,
        window_hours=window_hours,
        scoring_version=_SCORING_VERSION,
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
