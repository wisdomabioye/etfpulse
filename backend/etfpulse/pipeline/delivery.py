"""Signal → SignalDelivery fan-out + send worker + message formatter.

Three functions:
    `fan_out_signal(session, signal_id) -> int`
        Matches a freshly-built Signal against active users+groups; inserts
        SignalDelivery rows. Idempotent via partial UNIQUE indexes.
    `send_pending_deliveries(session) -> dict[str, int]`
        Drains status=PENDING deliveries: resolves target chat_id, formats
        message, calls telegram_client.send_message, flips status+error.
        Blocks/chat-not-found deactivate the target (channel/group); generic
        errors just mark the delivery failed. No retry (D4).
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
from typing import Any

import structlog
from sqlalchemy import func, or_, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from etfpulse.adapters.telegram import (
    TelegramBlockedError,
    TelegramChatNotFoundError,
    TelegramError,
    telegram_client,
)
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
            # Empty pref_assets = "all assets" (edge case 15). Non-empty →
            # must contain the signal's asset.
            or_(
                func.cardinality(User.pref_assets) == 0,
                User.pref_assets.contains([signal.asset]),
            ),
            User.pref_min_confidence <= signal.confidence,
        )
    )
    result = await session.execute(stmt)
    return [(row.id, row.channel_id) for row in result.all()]


async def _match_groups(session: AsyncSession, signal: Signal) -> list[int]:
    """TelegramGroups whose prefs accept this signal."""
    assert signal.confidence is not None

    stmt = select(TelegramGroup.id).where(
        TelegramGroup.is_active.is_(True),
        TelegramGroup.pref_paused.is_(False),
        or_(
            func.cardinality(TelegramGroup.pref_assets) == 0,
            TelegramGroup.pref_assets.contains([signal.asset]),
        ),
        TelegramGroup.pref_min_confidence <= signal.confidence,
    )
    result = await session.execute(stmt)
    return list(result.scalars().all())


# ---------------------------------------------------------------------------
# Message formatter — single source of truth for Signal → HTML text (D20).
# Handles NULL ai_analysis with a trigger-data fallback, HTML-escapes every
# dynamic field to prevent injection from LLM output or user-configured
# trigger data, and truncates to Telegram's safe body size.
# ---------------------------------------------------------------------------

_TELEGRAM_TEXT_CAP = 4000  # Telegram's hard limit is 4096; headroom for safety.


def format_signal_message(signal: Signal) -> str:
    """Render a Signal as an HTML message (parse_mode=HTML compatible).

    Every dynamic field goes through `html.escape()` so LLM output containing
    `<` / `>` / `&` can't break Telegram's HTML parser or (worst case) inject
    tags. Falls back to a trigger-data summary if `ai_analysis` is NULL.
    """
    asset = html.escape(signal.asset)
    signal_type = html.escape(signal.signal_type.replace("_", " "))

    parts: list[str] = [f"<b>{asset} {signal_type} signal</b>"]

    analysis = signal.ai_analysis
    if analysis:
        headline = html.escape(str(analysis.get("headline", "")))
        suggested = html.escape(str(analysis.get("suggested_action", "")))
        horizon = html.escape(str(analysis.get("time_horizon", "")))
        confidence = signal.confidence or 0

        parts.append(f"\n<b>{headline}</b>")
        meta_bits: list[str] = []
        if suggested:
            meta_bits.append(f"<i>Suggested action:</i> {suggested}")
        if confidence:
            meta_bits.append(f"<i>Confidence:</i> {confidence}/10")
        if horizon:
            meta_bits.append(f"<i>Horizon:</i> {horizon}")
        if meta_bits:
            parts.append("\n" + " · ".join(meta_bits))

        reasoning = analysis.get("reasoning") or []
        if reasoning:
            bullets = "\n".join(f"• {html.escape(str(r))}" for r in reasoning)
            parts.append(f"\n<b>Reasoning:</b>\n{bullets}")

        risks = analysis.get("risks") or []
        if risks:
            bullets = "\n".join(f"• {html.escape(str(r))}" for r in risks)
            parts.append(f"\n<b>Risks:</b>\n{bullets}")
    else:
        # No AI — surface what we have from trigger_data so the signal is
        # still actionable rather than a bare headline.
        parts.append("\n<b>Trigger data:</b>")
        if signal.trigger_data:
            for k, v in list(signal.trigger_data.items())[:6]:
                parts.append(f"• <i>{html.escape(str(k))}:</i> {html.escape(str(v))}")
        parts.append("\n<i>AI analysis unavailable — raw detector output only.</i>")

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
        - success               → status=DELIVERED, delivered_at=now
        - TelegramBlockedError  → status=FAILED, deactivate channel/group
        - TelegramChatNotFoundError → status=FAILED, deactivate channel/group
        - TelegramError (other) → status=FAILED, channel/group STAYS active
          (transient — rate limit, 5xx, network — keep retryable)

    No retry (D4 — signals are time-sensitive). Does NOT commit — caller
    owns the transaction boundary (D18).
    """
    summary = {
        "total": 0,
        "sent": 0,
        "failed": 0,
        "blocked": 0,
        "chat_not_found": 0,
        "skipped_no_target": 0,
    }

    # Single query: delivery + signal + (channel OR group target). LEFT JOINs
    # on channel and group because each delivery has exactly one of them.
    stmt = (
        select(SignalDelivery, Signal, NotificationChannel, TelegramGroup)
        .join(Signal, Signal.id == SignalDelivery.signal_id)
        .outerjoin(NotificationChannel, NotificationChannel.id == SignalDelivery.channel_id)
        .outerjoin(TelegramGroup, TelegramGroup.id == SignalDelivery.group_id)
        .where(SignalDelivery.status == DeliveryStatus.PENDING.value)
    )
    result = await session.execute(stmt)

    for delivery, signal, channel, group in result.all():
        summary["total"] += 1

        chat_id: int | str | None = None
        if channel is not None:
            chat_id = channel.channel_identifier
        elif group is not None:
            chat_id = group.chat_id

        if chat_id is None:
            # FK target deleted or never linked — treat as skipped, not
            # failed, so it doesn't pollute failure metrics.
            delivery.status = DeliveryStatus.SKIPPED.value
            delivery.error_message = "delivery target missing (channel/group not linked)"
            summary["skipped_no_target"] += 1
            continue

        message = format_signal_message(signal)

        try:
            sent = await telegram_client.send_message(chat_id, message, parse_mode="HTML")
            delivery.status = DeliveryStatus.DELIVERED.value
            delivery.delivered_at = sent.sent_at
            summary["sent"] += 1
        except TelegramBlockedError as exc:
            # User blocked bot OR bot kicked from group. Stop retrying to
            # this target entirely by deactivating the channel/group.
            delivery.status = DeliveryStatus.FAILED.value
            delivery.error_message = f"blocked: {str(exc)[:480]}"
            if channel is not None:
                channel.is_active = False
            if group is not None:
                group.is_active = False
            summary["blocked"] += 1
        except TelegramChatNotFoundError as exc:
            # Chat doesn't exist (deleted, never existed). Same remediation.
            delivery.status = DeliveryStatus.FAILED.value
            delivery.error_message = f"chat not found: {str(exc)[:480]}"
            if channel is not None:
                channel.is_active = False
            if group is not None:
                group.is_active = False
            summary["chat_not_found"] += 1
        except TelegramError as exc:
            # Transient (rate limit, 5xx, network). Keep the channel/group
            # active — next worker tick may succeed. But this signal is
            # stale by then; we accept the miss rather than retry inline.
            delivery.status = DeliveryStatus.FAILED.value
            delivery.error_message = str(exc)[:500]
            summary["failed"] += 1

        delivery.attempt_count = (delivery.attempt_count or 0) + 1

    await session.flush()

    log.info("send_pending_deliveries_done", **summary)
    return summary
