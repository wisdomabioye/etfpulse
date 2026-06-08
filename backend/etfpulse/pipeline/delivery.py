"""Signal → SignalDelivery fan-out + send worker + message formatter.

Three functions:
    `fan_out_signal(session, signal_id) -> int`
        Matches a freshly-built Signal against active users+groups; inserts
        SignalDelivery rows. Idempotent via partial UNIQUE indexes.
    `send_pending_deliveries(session) -> dict[str, int]`
        Drains status=PENDING deliveries whose retry-backoff window has
        elapsed. For each: resolves target chat_id, increments attempt
        counter, calls telegram_client.send_message, flips status+error.
        Blocks / chat-not-found / migrated are TERMINAL on the first
        observation (the underlying condition won't change between
        retries). Transient errors (rate limit, 5xx, network) stay
        PENDING for retry — only flipping to FAILED once the row reaches
        `settings.delivery_max_attempts` (Branch 2).
    `format_signal_message(signal) -> str`
        Single HTML rendering for a Signal. Handles NULL ai_analysis with
        a trigger-data fallback; HTML-escapes all dynamic content; truncates
        to 4000 chars (Telegram's 4096 limit minus headroom).

Fan-out details below:

`fan_out_signal(session, signal_id)` matches a freshly-built Signal against
every active User (with Telegram channel) and TelegramGroup whose
preferences accept it, and inserts one SignalDelivery row per match. The
partial UNIQUE indexes on `signal_deliveries` (ux_delivery_user_signal and
ux_delivery_group_signal, installed by the initial migration) make
`ON CONFLICT DO NOTHING` a clean idempotency primitive — no application-
level "have we already delivered?" logic needed.

Matching semantics:
    - User: `is_active` AND NOT `pref_paused` AND at least one active
      Telegram NotificationChannel AND asset in pref_assets (or pref_assets
      is empty = "all assets") AND min_confidence ≤ signal.confidence.
    - TelegramGroup: same minus the channel join (groups deliver to chat_id
      directly, not via NotificationChannel).

Skip conditions (return 0, no status change):
    - Signal doesn't exist
    - Signal status is not PENDING (idempotent re-call, or already expired)
    - Signal.expires_at is in the past (reaper will flip status later)
    - Signal.confidence is NULL (AI failed at creation — issue for ops to
      notice, not for us to paper over with everyone-gets-everything)

On success, marks signal.status=ALERTED even when delivery_count=0 (the
work is done from the pipeline's POV). Caller owns the transaction (D18).
"""

from __future__ import annotations

import html
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, cast

import structlog
from cachetools import TTLCache
from sqlalchemy import func, or_, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession
from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from etfpulse.adapters.telegram import (
    TelegramBlockedError,
    TelegramChatMigratedError,
    TelegramChatNotFoundError,
    TelegramError,
    telegram_client,
)
from etfpulse.config import settings
from etfpulse.constants import MARKET_ASSET, SUPPORTED_ASSETS
from etfpulse.models import (
    ChannelType,
    DeliveryStatus,
    NotificationChannel,
    Signal,
    SignalDelivery,
    SignalStatus,
    TelegramGroup,
    User,
)
from etfpulse.pipeline.track_record import (
    HorizonLabel,
    TrackRecordStatByHorizon,
    get_stats_by_confidence_floor_and_horizon,
)

log = structlog.get_logger()


async def fan_out_signal(session: AsyncSession, signal_id: int) -> int:
    """Insert one SignalDelivery per matching user/group. Returns new row count.

    Does NOT commit — caller (scheduler wrapper or test harness) owns the
    transaction boundary, same contract as `run_daily_cycle` (D18).
    """
    signal = await session.get(Signal, signal_id)
    if signal is None:
        log.warning("fan_out_signal_missing", signal_id=signal_id)
        return 0

    if signal.status != SignalStatus.PENDING.value:
        # Idempotent re-call. A signal that's already ALERTED was fanned out
        # previously; re-fanning would dupe via the partial unique indexes
        # anyway, but short-circuiting saves the query work.
        log.info(
            "fan_out_signal_skip_not_pending",
            signal_id=signal_id,
            status=signal.status,
        )
        return 0

    if signal.expires_at is not None and signal.expires_at < datetime.now(UTC):
        log.info(
            "fan_out_signal_skip_expired",
            signal_id=signal_id,
            expires_at=str(signal.expires_at),
        )
        return 0

    if signal.confidence is None:
        # AI didn't run. We don't send everyone every AI-failed signal — that
        # would punish free users with low-quality alerts. Signal stays
        # PENDING; ops sees the log if this happens often.
        log.warning("fan_out_signal_skip_null_confidence", signal_id=signal_id)
        return 0

    # PR I.2 — confirmation gate. Filter when the score is BELOW the
    # threshold, but pass NULL through. NULL means "scoring didn't apply"
    # (wait signal — no direction to confirm) and we keep the existing
    # delivery behaviour for those rather than silently dropping them.
    # AI-failed signals were already cut above via the confidence check.
    if (
        signal.confirmation_score is not None
        and signal.confirmation_score < settings.delivery_min_confirmation
    ):
        log.info(
            "fan_out_signal_skip_low_confirmation",
            signal_id=signal_id,
            asset=signal.asset,
            signal_type=signal.signal_type,
            confirmation_score=signal.confirmation_score,
            threshold=settings.delivery_min_confirmation,
        )
        return 0

    user_rows = await _match_users(session, signal)
    group_ids = await _match_groups(session, signal)

    # Two separate INSERTs: user deliveries and group deliveries have
    # different NOT NULL columns (user_id+channel_id vs group_id), and
    # SQLAlchemy's bulk values() requires homogeneous key sets per statement.
    # Heterogeneous bulk would trip "explicitly rendered as bound parameter"
    # on whichever column is NULL in one row and NOT NULL in another.
    inserted_count = 0

    if user_rows:
        user_payload: list[dict[str, Any]] = [
            {"signal_id": signal_id, "user_id": user_id, "channel_id": channel_id}
            for user_id, channel_id in user_rows
        ]
        stmt = (
            insert(SignalDelivery)
            .values(user_payload)
            .on_conflict_do_nothing()
            .returning(SignalDelivery.id)
        )
        inserted_count += len((await session.execute(stmt)).scalars().all())

    if group_ids:
        group_payload: list[dict[str, Any]] = [
            {"signal_id": signal_id, "group_id": group_id} for group_id in group_ids
        ]
        stmt = (
            insert(SignalDelivery)
            .values(group_payload)
            .on_conflict_do_nothing()
            .returning(SignalDelivery.id)
        )
        inserted_count += len((await session.execute(stmt)).scalars().all())

    # Mark alerted even when inserted=0 (edge case 14 — "work is done").
    # `flush` persists the change so any immediate `session.refresh(signal)`
    # (e.g. in tests) sees the new status. Caller still owns the transaction
    # boundary (D18) — we're only pushing to the DB buffer, not committing.
    signal.status = SignalStatus.ALERTED.value
    await session.flush()

    # Branch 5 — distinct observability event for the "fanned out to nobody"
    # case. A signal flips to ALERTED whether or not any recipients matched
    # (edge case 14), and `fan_out_signal_done` is emitted for both cases.
    # Greping logs for "fan_out_signal_no_recipients" gives operators a
    # single signal-path string to alert on (e.g., admin Run Cycle producing
    # ALERTED signals that nobody actually receives). Same payload shape so
    # downstream log pipelines parse both events uniformly.
    if inserted_count == 0:
        log.info(
            "fan_out_signal_no_recipients",
            signal_id=signal_id,
            asset=signal.asset,
            signal_type=signal.signal_type,
            confidence=signal.confidence,
            candidates_user=len(user_rows),
            candidates_group=len(group_ids),
        )

    log.info(
        "fan_out_signal_done",
        signal_id=signal_id,
        asset=signal.asset,
        signal_type=signal.signal_type,
        confidence=signal.confidence,
        candidates_user=len(user_rows),
        candidates_group=len(group_ids),
        inserted=inserted_count,
    )
    return inserted_count


async def _match_users(session: AsyncSession, signal: Signal) -> list[tuple[int, int]]:
    """Users with an active Telegram channel whose prefs accept this signal.

    Returns (user_id, channel_id) pairs — both needed for SignalDelivery.

    **Drift hazard.** The same filter rules are mirrored in Python in
    `etfpulse.api.routes.admin._trace_user` (Branch 5 observability —
    the `/api/admin/signals/{id}/delivery-trace` endpoint reports which
    rule excluded a recipient). The SQL here is the source of truth;
    the trace helper exists to explain it. Adding/changing a filter
    here MUST be reflected there or operators get misleading exclude
    reasons. A consistency-contract test in
    `tests/test_app/test_admin.py::TestDeliveryTraceConsistency` pins
    the two implementations together.
    """
    assert signal.confidence is not None  # caller guarantees

    stmt = (
        select(User.id, NotificationChannel.id.label("channel_id"))
        .join(NotificationChannel, NotificationChannel.user_id == User.id)
        .where(
            User.is_active.is_(True),
            User.pref_paused.is_(False),
            NotificationChannel.is_active.is_(True),
            NotificationChannel.channel_type == ChannelType.TELEGRAM.value,
            *_asset_pref_filter(User.pref_assets, signal.asset),
            User.pref_min_confidence <= signal.confidence,
        )
    )
    result = await session.execute(stmt)
    return [(row.id, row.channel_id) for row in result.all()]


async def _match_groups(session: AsyncSession, signal: Signal) -> list[int]:
    """TelegramGroups whose prefs accept this signal.

    Drift hazard: same as `_match_users` above — mirrored in
    `etfpulse.api.routes.admin._trace_group`. Keep them in sync.
    """
    assert signal.confidence is not None

    stmt = select(TelegramGroup.id).where(
        TelegramGroup.is_active.is_(True),
        TelegramGroup.pref_paused.is_(False),
        *_asset_pref_filter(TelegramGroup.pref_assets, signal.asset),
        TelegramGroup.pref_min_confidence <= signal.confidence,
    )
    result = await session.execute(stmt)
    return list(result.scalars().all())


def _asset_pref_filter(pref_column: Any, signal_asset: str) -> tuple[Any, ...]:
    """Build the WHERE clause for the `pref_assets` filter.

    Two modes (PR F.3):
      * Single-asset signal → empty `pref_assets` means "all assets",
        non-empty must contain the signal's asset.
      * MARKET signal → bypass the `pref_assets` filter entirely. Regime
        shifts (the only MARKET signal type today) are market-wide events
        that reach every subscriber regardless of their asset preferences.
        Users who don't want them can use `pref_paused` or set a confidence
        floor above the regime classifier's typical output.

    Returns a tuple of SQLAlchemy WHERE-clause fragments (empty tuple for
    MARKET signals — splatted into the call site's `.where(...)` arg list).
    Keeping the rule in one helper means `_match_users` and `_match_groups`
    can't drift, and the admin trace mirror (`api/routes/admin._asset_matches`)
    has a single Python-side rule to mirror.
    """
    if signal_asset == MARKET_ASSET:
        return ()
    return (
        or_(
            func.cardinality(pref_column) == 0,
            pref_column.contains([signal_asset]),
        ),
    )


# ---------------------------------------------------------------------------
# Message formatter — single source of truth for Signal → HTML text (D20).
# Handles NULL ai_analysis with a trigger-data fallback, HTML-escapes every
# dynamic field to prevent injection from LLM output or user-configured
# trigger data, and truncates to Telegram's safe body size.
#
# PR H.2 — trimmed to a skim-only format. Reasoning, regime, news, and risks
# all live on /signals/:id (linked via `build_signal_keyboard`). The alert
# itself carries title, headline, decision/levels, optional track-record
# stat, and footer. This is intentional divergence from /signals/:id —
# Telegram is for skim + decide + click; the web page is for the full read.
# ---------------------------------------------------------------------------

_TELEGRAM_TEXT_CAP = 4000  # Telegram's hard limit is 4096; headroom for safety.

# Risks render in the alert (PR H.3) — capped to the first 2 bullets. Risks
# are the most actionable line in the message ("FOMC tomorrow" tells you
# when NOT to act); reasoning and news context are explanatory and live on
# the web page. Two is the cap because alerts are skim-only — anyone who
# needs the full risk list follows the inline keyboard to /signals/:id.
_RISKS_MAX_BULLETS = 2

# Trigger-data dump (no-AI branch) caps and the keys we deliberately
# exclude — regime + news blobs are bulky JSONB structures that would
# dwarf the actual detector signal in the dump. Skip them; the full
# objects are visible on /signals/:id for anyone who clicks through.
_TRIGGER_DUMP_LIMIT = 6
_TRIGGER_DUMP_SKIP_KEYS = frozenset({"regime_at_creation", "news_context"})

# Stage 8-P8 — track-record snapshot cache. Refreshed by the send worker
# via `_get_track_record_stat` which falls through to a DB query on miss.
# 10-min TTL keeps the stat fresh enough for "we hit 64% of the time"
# language (the underlying number changes once per outcome-eval cycle, ~1h)
# without re-querying on every signal in a tick. Single-key cache: there's
# only one global track record at a time.
_TRACK_RECORD_CACHE_TTL_SEC = 600
_TRACK_RECORD_CACHE_KEY = "current"
_track_record_cache: TTLCache[str, TrackRecordStatByHorizon] = TTLCache(
    maxsize=1, ttl=_TRACK_RECORD_CACHE_TTL_SEC
)


async def _get_track_record_stat(session: AsyncSession) -> TrackRecordStatByHorizon:
    """Memoised per-process snapshot of the bucketed cohort stats. Caller —
    `send_pending_deliveries` — invokes this ONCE per tick, then passes the
    same `TrackRecordStatByHorizon` to every `format_signal_message` call
    so a 100-message tick still pays one DB query for the cohort stats.

    PR B (#60) — switched from `TrackRecordStat` (mixed-horizon) to
    `TrackRecordStatByHorizon` (per-bucket). The per-signal alert now
    slices the cohort stat by the signal's own horizon — a scalp signal's
    "Our signals at confidence ≥7 hit target X%" line uses scalp-bucket
    numbers, not a swing+position+legacy mix. Honest framing of the proof
    point that the signal recipient is being shown.

    Cache miss: one GROUP BY query against `signal_outcomes`. Cache hit:
    no DB roundtrip at all. Tests can clear the cache via `_track_record_cache.clear()`.
    """
    cached = _track_record_cache.get(_TRACK_RECORD_CACHE_KEY)
    if cached is not None:
        return cached
    fresh = await get_stats_by_confidence_floor_and_horizon(session)
    _track_record_cache[_TRACK_RECORD_CACHE_KEY] = fresh
    return fresh


def _format_decision_block(signal: Signal, analysis: dict[str, Any]) -> str | None:
    """Render the trader-facing top-of-message decision summary.

    Format (compact, mobile-skimmable):
        <b>Decision:</b> consider long · Conf 8/10 · swing
        <i>Spot:</i> $82,352.65 · <i>Entry:</i> $82,400 · <i>Stop:</i> $80,800
        · <i>Target:</i> $85,800 · <i>R:R</i> 1:2.1

    Section order putting direction + levels FIRST (was: headline → meta →
    action levels separately, then reasoning). Traders skimming on mobile
    get the actionable numbers in the first two lines; the AI reasoning,
    regime, news, and risks follow as context for the decision.

    Returns None only when there is genuinely nothing actionable — no
    suggested_action, no confidence, no price levels. Otherwise renders
    whatever subset is available so a partial signal still leads with
    its strongest fact.
    """
    suggested = html.escape(str(analysis.get("suggested_action", "")))
    horizon = html.escape(str(analysis.get("time_horizon", "")))
    confidence = signal.confidence or 0
    entry = signal.entry_price
    stop = signal.stop_price
    target = signal.target_price
    spot = signal.price_at_creation

    has_decision = bool(suggested or confidence or horizon)
    has_levels = entry is not None or stop is not None or target is not None or spot is not None
    if not has_decision and not has_levels:
        return None

    lines: list[str] = []

    # Line 1 — direction · confidence · horizon. Each piece is optional.
    if has_decision:
        decision_bits: list[str] = []
        if suggested:
            decision_bits.append(suggested)
        if confidence:
            decision_bits.append(f"Conf {confidence}/10")
        if horizon:
            decision_bits.append(horizon)
        lines.append("<b>Decision:</b> " + " · ".join(decision_bits))

    # Line 2 — spot anchor + entry/stop/target + R:R. Skipped wholesale
    # when there are no prices at all (legacy signals predating P1 + P34
    # OR signal where both providers failed and AI declined levels).
    if has_levels:
        level_bits: list[str] = []
        if spot is not None:
            level_bits.append(f"<i>Spot:</i> {_format_usd(spot)}")
        if entry is not None:
            level_bits.append(f"<i>Entry:</i> {_format_usd(entry)}")
        if stop is not None:
            level_bits.append(f"<i>Stop:</i> {_format_usd(stop)}")
        if target is not None:
            level_bits.append(f"<i>Target:</i> {_format_usd(target)}")

        # R:R only when all three legs are set + risk distance > 0. Same
        # guard as the frontend `SuggestedActionPanel.computeRiskReward`.
        if entry is not None and stop is not None and target is not None:
            risk = abs(entry - stop)
            reward = abs(target - entry)
            if risk > 0:
                rr = round((float(reward) / float(risk)) * 10) / 10
                level_bits.append(f"<i>R:R</i> 1:{rr}")

        if level_bits:
            lines.append(" · ".join(level_bits))

    return "\n" + "\n".join(lines)


def _format_track_record_stat_line(
    signal: Signal, stat: TrackRecordStatByHorizon | None
) -> str | None:
    """Render the killer "Our {horizon} signals at confidence ≥N hit target
    Y% of the time (over M signals)" stat line, or None when not applicable.

    PR B (#60) — the cohort is now sliced by the signal's OWN horizon, not
    averaged across all horizons. A scalp recipient sees scalp-bucket
    numbers; a swing recipient sees swing-bucket numbers. The pre-PR-B
    line was misleading by construction — mixing windows in the denominator
    of "X% of signals at confidence ≥N hit their target."

    Horizon is read directly from `signal.ai_analysis["time_horizon"]`
    (set by `apply_analysis_to_signal` alongside `expires_at`). The AI
    prompt's `time_horizon` Literal {scalp, swing, position} maps 1:1
    to the HorizonLabel bucket — no derivation needed when both ends use
    the same enum strings. Going through `window_hours = expires_at -
    created_at` would be equivalent for live signals but adds a
    `created_at` dependency that pure-formatter tests don't always supply.

    Skips when:
      - `stat` not provided (legacy callers / pure-formatter tests)
      - `signal.confidence` is NULL (AI failed at build time — no cohort
        applies because the signal isn't in the cohort denominator)
      - `time_horizon` missing/unrecognised (defensive — should never fire
        in production since the AI schema constrains it)
      - the signal's horizon bucket has zero targeted signals (e.g. a
        scalp signal at any deploy where scalp scoring is gated on #62
        → empty scalp bucket → no proof point yet → suppress cleanly)

    The stat is intentionally the LAST piece of the message body (just
    before the footer) so the alert ends on the proof point — same
    placement as the Stage 8 design doc spec.
    """
    if stat is None or signal.confidence is None:
        return None
    horizon_raw = (signal.ai_analysis or {}).get("time_horizon")
    if horizon_raw not in ("scalp", "swing", "position"):
        return None
    horizon = cast(HorizonLabel, horizon_raw)
    confidence = signal.confidence
    pct = stat.hit_rate_pct(confidence, horizon)
    if pct is None:
        return None
    cohort = stat.targeted_count(confidence, horizon)
    return (
        f"\n<i>Our {horizon} signals at confidence ≥{confidence} hit target "
        f"{pct}% of the time (over {cohort} evaluated).</i>"
    )


def _format_usd(d: Decimal) -> str:
    """Compact USD formatter for prices in the action block. Renders with
    thousands separators via `f"{n:,}"` once cast to float. Same shape as
    the frontend's `lib/format.formatUsdPrice`.

    Caller MUST None-check upstream — this function expects a real Decimal.
    Per-call float() is safe here because we render as visible text only;
    no arithmetic happens after this point. Two-decimal cap matches the
    frontend formatter exactly so a Telegram alert and the web page show
    the same dollar value to the cent."""
    return f"${float(d):,.2f}"


# SIG2X — assets we surface an Execute button for. Derived from the
# single-source-of-truth `SUPPORTED_ASSETS` (BTC, ETH today) so that
# adding a new asset to the universe automatically widens the gate.
# MARKET (regime_shift) is intentionally NOT in `SUPPORTED_ASSETS`
# so it's excluded for free — regime claims are not single-asset
# trade calls.
_EXECUTE_TRADEABLE_ASSETS = frozenset(SUPPORTED_ASSETS)

# SIG2X — directions that map to a concrete order. `wait` hides the
# button. Mirrors `ACTIONABLE_DIRECTIONS` in `src/lib/signalExecute.ts`
# on the FE. If either side adds a new direction (e.g. `'hold'`), BOTH
# must update — there's no compile-time linkage.
_EXECUTE_ACTIONABLE_DIRECTIONS = frozenset({"consider long", "consider short"})


def _signal_is_executable(signal: Signal) -> bool:
    """Should the Telegram alert keyboard include a one-tap "⚡ Execute"
    button for this signal? Mirrors `isExecutableSignal` in
    `frontend/src/lib/signalExecute.ts`. Drift between the two is a
    real product bug — keep both rule sets aligned."""
    if signal.asset not in _EXECUTE_TRADEABLE_ASSETS:
        return False
    analysis = signal.ai_analysis
    if not analysis:
        return False
    return analysis.get("suggested_action") in _EXECUTE_ACTIONABLE_DIRECTIONS


def build_signal_keyboard(signal: Signal) -> InlineKeyboardMarkup | None:
    """Inline keyboard attached to a signal alert (issue #38).

    "📊 View on web" deep-links to `/signals/:id`. For actionable
    signals (tradeable asset + concrete direction — see
    `_signal_is_executable`), a "⚡ Execute" button is added on the
    same row deep-linking to `/execute?signal_id={id}`. In the
    Telegram-WebApp context the Execute link triggers initData-based
    auto-auth + form prefill — one tap from alert to signing prompt.

    Returns None when `frontend_url` is unset → adapter sends without
    any reply_markup.

    Lives in this module (not `bot/keyboards.py`) so `pipeline/`
    doesn't have to import from `bot/` — delivery already owns the
    rest of the outbound message rendering, and this keyboard is
    part of that.
    """
    base = settings.frontend_url.rstrip("/")
    if not base:
        return None
    view_btn = InlineKeyboardButton("📊 View on web", url=f"{base}/signals/{signal.id}")
    if _signal_is_executable(signal):
        execute_btn = InlineKeyboardButton(
            "⚡ Execute", url=f"{base}/execute?signal_id={signal.id}"
        )
        return InlineKeyboardMarkup([[view_btn, execute_btn]])
    return InlineKeyboardMarkup([[view_btn]])


def format_signal_message(
    signal: Signal, *, track_record_stat: TrackRecordStatByHorizon | None = None
) -> str:
    """Render a Signal as an HTML message (parse_mode=HTML compatible).

    Every dynamic field goes through `html.escape()` so LLM output containing
    `<` / `>` / `&` can't break Telegram's HTML parser or (worst case) inject
    tags. Falls back to a terse trigger-data summary if `ai_analysis` is NULL.

    PR H.2 — trimmed to a skim-only format. The web detail page (linked via
    `build_signal_keyboard`) carries reasoning, regime, and news context;
    the Telegram alert was duplicating all of them, producing 30-line
    messages most users skipped past. Section order:
        1. Title line — "<asset> <signal_type> signal"
        2. Headline (when AI succeeded)
        3. Decision block — direction · confidence · horizon, then prices
           (spot at signal · entry · stop · target · R:R). One compact
           two-line block carrying everything a skimming trader needs.
        4. Risks — top 2 bullets only (PR H.3). Risks tell you when NOT to
           act and are the most actionable text in the message.
        5. Track-record stat line (when track_record_stat supplied AND the
           cohort at this confidence floor has data)
        6. Footer — signal date · expires
    Sections 3, 4, and 5 are conditional — older / partial signals render
    without them, no rule branch needed at the call site.

    `track_record_stat` is opt-in. The pure-formatter test surface omits
    it (legacy + simpler tests). The send worker pre-fetches it once per
    tick and threads it through.
    """
    asset = html.escape(signal.asset)
    signal_type = html.escape(signal.signal_type.replace("_", " "))
    # Bind once — `signal.trigger_data` is JSONB-nullable and several
    # downstream callers want a dict to .get() against.
    trigger_data: dict[str, Any] = signal.trigger_data or {}

    parts: list[str] = [f"<b>{asset} {signal_type} signal</b>"]

    analysis = signal.ai_analysis
    if analysis:
        headline = html.escape(str(analysis.get("headline", "")))

        parts.append(f"\n<b>{headline}</b>")

        decision_block = _format_decision_block(signal, analysis)
        if decision_block:
            parts.append(decision_block)

        # Risks — capped to the top 2 bullets. The AI prompt returns risks
        # in priority order, so the first N is a clean cap rather than
        # dropping the "most important" ambiguity. Full list lives on the
        # web detail page for anyone who needs it.
        risks = analysis.get("risks") or []
        if risks:
            visible_risks = risks[:_RISKS_MAX_BULLETS]
            bullets = "\n".join(f"• {html.escape(str(r))}" for r in visible_risks)
            parts.append(f"\n<b>Risks:</b>\n{bullets}")
    else:
        # No AI — surface what we have from trigger_data so the signal is
        # still actionable rather than a bare headline. Regime + news blobs
        # are filtered out of the dump (they're bulky JSONB; visible on
        # /signals/:id for anyone who follows the link).
        parts.append("\n<b>Trigger data:</b>")
        if trigger_data:
            # Filter BEFORE slicing so the cap applies to rendered keys, not
            # iterated keys — otherwise a trigger_data with regime/news in
            # the first 6 positions would silently drop real detector keys
            # past index 6.
            visible = [(k, v) for k, v in trigger_data.items() if k not in _TRIGGER_DUMP_SKIP_KEYS]
            for k, v in visible[:_TRIGGER_DUMP_LIMIT]:
                parts.append(f"• <i>{html.escape(str(k))}:</i> {html.escape(str(v))}")

        parts.append("\n<i>AI analysis unavailable — raw detector output only.</i>")

    # Track-record stat — last body element, just before the footer. Renders
    # only when the caller supplied a stat AND this signal's confidence floor
    # has cohort data (helper handles all the skip cases).
    stat_line = _format_track_record_stat_line(signal, track_record_stat)
    if stat_line:
        parts.append(stat_line)

    footer_bits: list[str] = [f"Signal date: {signal.signal_date.isoformat()}"]
    if signal.expires_at:
        footer_bits.append(f"expires: {signal.expires_at.strftime('%Y-%m-%d %H:%M UTC')}")
    parts.append(f"\n<i>{' · '.join(footer_bits)}</i>")

    message = "\n".join(parts)

    if len(message) > _TELEGRAM_TEXT_CAP:
        # Trim with an ellipsis — Telegram 400s on >4096 char messages, so
        # being conservative is cheaper than being wrong.
        message = message[: _TELEGRAM_TEXT_CAP - 1].rstrip() + "…"
    return message


# ---------------------------------------------------------------------------
# Send worker — drains pending deliveries through Telegram.
# ---------------------------------------------------------------------------


async def send_pending_deliveries(session: AsyncSession) -> dict[str, int]:
    """Process every PENDING SignalDelivery: send via Telegram, update status.

    Single JOINed query loads delivery + signal + channel + group at once
    (avoiding N+1). Per delivery:
        - success                     → status=DELIVERED, delivered_at=now
        - TelegramBlockedError        → status=FAILED, deactivate channel/group
        - TelegramChatNotFoundError   → status=FAILED, deactivate channel/group
        - TelegramChatMigratedError   → status=FAILED. If no other group
          row holds `exc.new_chat_id`, update `group.chat_id` and keep
          active so future signals land on the migrated supergroup. If a
          collision exists (e.g. `my_chat_member` already registered the
          supergroup), soft-delete this row instead; the survivor row
          becomes the live target. See issue #77.
        - TelegramError (other)       → if attempt_count < max_attempts:
          status STAYS PENDING and the row is re-picked next tick once the
          backoff window elapses. At the cap, status=FAILED. Channel/group
          stays active either way (transient errors don't justify
          deactivation).

    Retry filter: a PENDING row is eligible only when `last_attempt_at IS
    NULL` (never attempted) OR the elapsed time since `last_attempt_at`
    exceeds `delivery_retry_base_seconds * 2^(attempt_count - 1)`. Both
    fields are updated BEFORE the send call so a mid-attempt crash still
    advances the retry clock (one wasted attempt budget is better than
    an infinite retry loop on a poison row).

    Does NOT commit — caller owns the transaction boundary (D18).
    """
    summary = {
        "total": 0,
        "sent": 0,
        # Terminal-failure buckets.
        "failed": 0,
        "blocked": 0,
        "chat_not_found": 0,
        "migrated": 0,
        # Transient failure that stayed PENDING for the next retry tick.
        "retrying": 0,
        "skipped_no_target": 0,
    }

    # Stage 8-P8 — pre-fetch the track-record stat ONCE per tick. The cache
    # short-circuits the DB query within the 10-min TTL; on miss the
    # snapshot lands in the cache for the next tick. Threaded into every
    # `format_signal_message` call below so a 100-message tick still costs
    # ≤1 DB query for the cohort stats.
    track_stat = await _get_track_record_stat(session)

    # Backoff predicate: row is retryable when never-attempted OR enough
    # time has elapsed since the last attempt. `func.power(2, n-1)` matches
    # the canonical exponential schedule. `extract(epoch, …)` returns the
    # interval as seconds (double precision) so the inequality is straight
    # numeric. Uses server-side `now()` for time consistency across the
    # batch — Python clock skew between worker boot and query is irrelevant
    # but using one clock keeps reasoning simple.
    base_seconds = settings.delivery_retry_base_seconds
    backoff_elapsed = func.extract(
        "epoch", func.now() - SignalDelivery.last_attempt_at
    ) >= base_seconds * func.power(2, SignalDelivery.attempt_count - 1)

    # Single query: delivery + signal + (channel OR group target). LEFT JOINs
    # on channel and group because each delivery has exactly one of them.
    # Backoff filter pushes the retry-readiness decision into the database
    # so a backlog of 500 not-yet-due retries doesn't load 500 rows per tick.
    stmt = (
        select(SignalDelivery, Signal, NotificationChannel, TelegramGroup)
        .join(Signal, Signal.id == SignalDelivery.signal_id)
        .outerjoin(NotificationChannel, NotificationChannel.id == SignalDelivery.channel_id)
        .outerjoin(TelegramGroup, TelegramGroup.id == SignalDelivery.group_id)
        .where(
            SignalDelivery.status == DeliveryStatus.PENDING.value,
            or_(SignalDelivery.last_attempt_at.is_(None), backoff_elapsed),
        )
    )
    result = await session.execute(stmt)

    max_attempts = settings.delivery_max_attempts
    now = datetime.now(UTC)

    for delivery, signal, channel, group in result.all():
        summary["total"] += 1

        chat_id: int | str | None = None
        if channel is not None:
            chat_id = channel.channel_identifier
        elif group is not None:
            chat_id = group.chat_id

        if chat_id is None:
            # FK target deleted or never linked — treat as skipped, not
            # failed, so it doesn't pollute failure metrics. No attempt
            # was made; don't advance the retry clock.
            delivery.status = DeliveryStatus.SKIPPED.value
            delivery.error_message = "delivery target missing (channel/group not linked)"
            summary["skipped_no_target"] += 1
            continue

        # Advance the retry clock BEFORE the send. If we crash mid-send,
        # next tick sees an updated `last_attempt_at` and waits out the
        # backoff rather than hammering the same row immediately. Cost:
        # one wasted attempt budget on a rollback (we'd want at-most-once
        # delivery, which Telegram can't promise anyway).
        delivery.attempt_count = (delivery.attempt_count or 0) + 1
        delivery.last_attempt_at = now

        message = format_signal_message(signal, track_record_stat=track_stat)
        # Inline "View on web" keyboard (issue #38). Returns None when
        # `frontend_url` is unset → adapter sends without reply_markup.
        keyboard = build_signal_keyboard(signal)

        try:
            sent = await telegram_client.send_message(
                chat_id, message, parse_mode="HTML", reply_markup=keyboard
            )
            delivery.status = DeliveryStatus.DELIVERED.value
            delivery.delivered_at = sent.sent_at
            # Clear the stale error from any prior transient attempt — a
            # DELIVERED row with a non-NULL error_message would mislead
            # readers (admin metrics, dashboards) into thinking the send
            # failed even though it ultimately succeeded.
            delivery.error_message = None
            summary["sent"] += 1
        except TelegramBlockedError as exc:
            # User blocked bot OR bot kicked from group. Stop retrying to
            # this target entirely by deactivating the channel/group.
            # Terminal on first observation — repeated retries can't fix it.
            delivery.status = DeliveryStatus.FAILED.value
            delivery.error_message = f"blocked: {str(exc)[:480]}"
            if channel is not None:
                channel.is_active = False
            if group is not None:
                group.is_active = False
            summary["blocked"] += 1
        except TelegramChatNotFoundError as exc:
            # Chat doesn't exist (deleted, never existed). Terminal — same
            # remediation as Blocked.
            delivery.status = DeliveryStatus.FAILED.value
            delivery.error_message = f"chat not found: {str(exc)[:480]}"
            if channel is not None:
                channel.is_active = False
            if group is not None:
                group.is_active = False
            summary["chat_not_found"] += 1
        except TelegramChatMigratedError as exc:
            # Basic group was converted to a supergroup — chat_id changed.
            # Two-branch self-heal (issue #77):
            #   * NO collision  → update this row's chat_id; group stays active.
            #   * COLLISION     → another TelegramGroup already holds the new
            #     chat_id (typically because `my_chat_member` already
            #     registered the post-migration supergroup as a new row). Doing
            #     the naive UPDATE here would violate `uq_telegram_groups_chat_id`
            #     on flush, the whole tick rolls back, sibling DELIVERED rows
            #     get reverted to PENDING, and the worker re-sends them every
            #     30s forever. Soft-delete the stale row instead — `_match_groups`
            #     filters `is_active=True` so future fan-outs route to the
            #     survivor naturally.
            # Channels (DMs) cannot migrate — `if group` covers the
            # impossible-but-cheap case.
            if group is not None:
                existing_id = (
                    await session.execute(
                        select(TelegramGroup.id).where(
                            TelegramGroup.chat_id == exc.new_chat_id,
                            TelegramGroup.id != group.id,
                        )
                    )
                ).scalar_one_or_none()
                if existing_id is not None:
                    group.is_active = False
                    delivery.error_message = (
                        f"migrated: target already registered as group_id={existing_id}"
                    )
                else:
                    group.chat_id = exc.new_chat_id
                    delivery.error_message = f"migrated: new chat_id={exc.new_chat_id}"
            else:
                # DM path — preserve historical message shape.
                delivery.error_message = f"migrated: new chat_id={exc.new_chat_id}"
            delivery.status = DeliveryStatus.FAILED.value
            summary["migrated"] += 1
        except TelegramError as exc:
            # Transient (rate limit, 5xx, network). Retry until the cap.
            # At the cap, flip to FAILED so the row gets a terminal status
            # (and the reaper / dashboards see the same string for all
            # max-attempts failures). Channel/group stays active either
            # way — a future signal to the same target may succeed.
            if delivery.attempt_count >= max_attempts:
                delivery.status = DeliveryStatus.FAILED.value
                delivery.error_message = f"max_attempts={max_attempts} reached: {str(exc)[:460]}"
                summary["failed"] += 1
            else:
                # status stays PENDING; last_attempt_at + attempt_count
                # already set above. Record the most recent error so a
                # row in PENDING with an error_message tells operators
                # exactly why it's still pending.
                delivery.error_message = str(exc)[:500]
                summary["retrying"] += 1

    await session.flush()

    log.info("send_pending_deliveries_done", **summary)
    return summary
