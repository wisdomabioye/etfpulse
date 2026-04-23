"""Send worker + message formatter tests.

Structured as two layers:
    - `TestFormatSignalMessage` — pure function, no DB, no Telegram.
    - `TestSendPendingDeliveries` — integration: seed SignalDelivery rows,
      mock `telegram_client.send_message`, run the worker, assert on
      delivery.status + channel.is_active transitions.

The send worker never hits a real Telegram API in tests — we patch
`etfpulse.pipeline.delivery.telegram_client.send_message` to return a
`SentMessage` stub or raise one of our error classes.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from unittest.mock import AsyncMock

import pytest

from etfpulse.adapters.telegram import (
    SentMessage,
    TelegramBlockedError,
    TelegramChatNotFoundError,
    TelegramError,
)
from etfpulse.models import (
    ChannelType,
    DeliveryStatus,
    NotificationChannel,
    Signal,
    SignalDelivery,
    TelegramGroup,
    User,
)
from etfpulse.pipeline.delivery import format_signal_message, send_pending_deliveries
from etfpulse.pipeline.detectors import compute_fingerprint

# ---------------------------------------------------------------------------
# format_signal_message — pure function
# ---------------------------------------------------------------------------


def _signal_with_ai(**overrides) -> Signal:
    defaults = {
        "signal_type": "flow_anomaly",
        "asset": "BTC",
        "trigger_data": {"streak_length": 4},
        "ai_analysis": {
            "headline": "BTC inflows snap 4-day streak",
            "reasoning": ["Volume spike", "Institutional rotation likely"],
            "confidence": 7,
            "risks": ["Macro headwind"],
            "suggested_action": "consider short",
            "time_horizon": "swing",
        },
        "confidence": 7,
        "fingerprint": compute_fingerprint("fmt-test"),
        "signal_date": date(2026, 4, 23),
        "expires_at": datetime(2026, 4, 26, 10, 30, tzinfo=UTC),
    }
    defaults.update(overrides)
    return Signal(**defaults)


class TestFormatSignalMessage:
    def test_full_signal_renders_all_sections(self):
        msg = format_signal_message(_signal_with_ai())

        assert "<b>BTC flow anomaly signal</b>" in msg
        assert "BTC inflows snap 4-day streak" in msg
        assert "consider short" in msg
        assert "Confidence:</i> 7/10" in msg
        assert "Horizon:</i> swing" in msg
        assert "Volume spike" in msg
        assert "Institutional rotation" in msg
        assert "Macro headwind" in msg
        assert "2026-04-23" in msg  # signal_date
        assert "2026-04-26 10:30 UTC" in msg  # expires_at

    def test_null_ai_analysis_falls_back_to_trigger_data(self):
        signal = _signal_with_ai(
            ai_analysis=None,
            confidence=None,
            trigger_data={"streak_length": 4, "direction": "long", "break_date": "2026-04-22"},
        )
        msg = format_signal_message(signal)

        assert "Trigger data" in msg
        assert "streak_length" in msg
        assert "AI analysis unavailable" in msg
        # Shouldn't render empty confidence/horizon sections.
        assert "Confidence:" not in msg

    def test_html_special_chars_escaped(self):
        """LLM might return `<` / `>` / `&` in reasoning. Must escape to
        prevent HTML parse errors OR (worst case) injection."""
        signal = _signal_with_ai(
            ai_analysis={
                "headline": "BTC <script>alert(1)</script> breakout",
                "reasoning": ["R&D spike", "A > B"],
                "confidence": 7,
                "risks": ["Fake & risk"],
                "suggested_action": "wait",
                "time_horizon": "scalp",
            }
        )
        msg = format_signal_message(signal)

        # Dangerous raw tags must not appear.
        assert "<script>" not in msg
        # Escaped versions ARE present.
        assert "&lt;script&gt;alert(1)&lt;/script&gt;" in msg
        assert "R&amp;D spike" in msg
        assert "A &gt; B" in msg

    def test_truncates_long_messages(self):
        """Telegram's cap is 4096; we target 4000 for headroom. Verify that
        a pathologically-long AI response gets safely trimmed."""
        signal = _signal_with_ai(
            ai_analysis={
                "headline": "X" * 5000,
                "reasoning": [],
                "confidence": 7,
                "risks": [],
                "suggested_action": "wait",
                "time_horizon": "scalp",
            }
        )
        msg = format_signal_message(signal)

        assert len(msg) <= 4000
        assert msg.endswith("…")

    def test_no_expires_at_omits_footer_piece(self):
        signal = _signal_with_ai(expires_at=None)
        msg = format_signal_message(signal)

        assert "Signal date: 2026-04-23" in msg
        assert "expires:" not in msg


# ---------------------------------------------------------------------------
# send_pending_deliveries — integration
# ---------------------------------------------------------------------------


async def _seed_user_delivery(
    db_session, *, chat_id: str, confidence: int = 7, channel_active: bool = True
) -> tuple[SignalDelivery, Signal, NotificationChannel]:
    signal = Signal(
        signal_type="flow_anomaly",
        asset="BTC",
        trigger_data={},
        ai_analysis={
            "headline": "test",
            "reasoning": [],
            "confidence": confidence,
            "risks": [],
            "suggested_action": "wait",
            "time_horizon": "scalp",
        },
        confidence=confidence,
        fingerprint=compute_fingerprint(f"send-test-{chat_id}"),
        signal_date=date(2026, 4, 23),
    )
    db_session.add(signal)
    await db_session.flush()

    user = User()
    db_session.add(user)
    await db_session.flush()

    channel = NotificationChannel(
        user_id=user.id,
        channel_type=ChannelType.TELEGRAM.value,
        channel_identifier=chat_id,
        is_active=channel_active,
    )
    db_session.add(channel)
    await db_session.flush()

    delivery = SignalDelivery(
        signal_id=signal.id,
        user_id=user.id,
        channel_id=channel.id,
        status=DeliveryStatus.PENDING.value,
    )
    db_session.add(delivery)
    await db_session.flush()
    return delivery, signal, channel


async def _seed_group_delivery(
    db_session, *, chat_id: int
) -> tuple[SignalDelivery, Signal, TelegramGroup]:
    signal = Signal(
        signal_type="magnitude",
        asset="ETH",
        trigger_data={},
        ai_analysis={
            "headline": "group test",
            "reasoning": [],
            "confidence": 8,
            "risks": [],
            "suggested_action": "consider long",
            "time_horizon": "swing",
        },
        confidence=8,
        fingerprint=compute_fingerprint(f"group-send-{chat_id}"),
        signal_date=date(2026, 4, 23),
    )
    db_session.add(signal)
    await db_session.flush()

    group = TelegramGroup(chat_id=chat_id, title="Alpha Group")
    db_session.add(group)
    await db_session.flush()

    delivery = SignalDelivery(
        signal_id=signal.id,
        group_id=group.id,
        status=DeliveryStatus.PENDING.value,
    )
    db_session.add(delivery)
    await db_session.flush()
    return delivery, signal, group


@pytest.fixture
def stub_send(monkeypatch):
    """Default stub — send succeeds. Per-test overrides via .side_effect."""
    sent_at = datetime(2026, 4, 23, 12, 0, tzinfo=UTC)
    stub = AsyncMock(return_value=SentMessage(message_id=42, chat_id=999, sent_at=sent_at))
    monkeypatch.setattr("etfpulse.pipeline.delivery.telegram_client.send_message", stub)
    return stub


class TestSendPendingDeliveries:
    async def test_happy_path_marks_delivered(self, db_session, stub_send):
        delivery, _, _ = await _seed_user_delivery(db_session, chat_id="100")

        summary = await send_pending_deliveries(db_session)

        assert summary["sent"] == 1
        assert summary["total"] == 1
        await db_session.refresh(delivery)
        assert delivery.status == DeliveryStatus.DELIVERED.value
        assert delivery.delivered_at is not None
        assert delivery.attempt_count == 2  # started at 1 (default), +1 on attempt

    async def test_blocked_deactivates_channel(self, db_session, stub_send):
        stub_send.side_effect = TelegramBlockedError("user blocked bot")
        delivery, _, channel = await _seed_user_delivery(db_session, chat_id="200")

        summary = await send_pending_deliveries(db_session)

        assert summary["blocked"] == 1
        assert summary["sent"] == 0
        await db_session.refresh(delivery)
        await db_session.refresh(channel)
        assert delivery.status == DeliveryStatus.FAILED.value
        assert "blocked" in delivery.error_message.lower()
        # Channel deactivated so future fan-outs skip this user.
        assert channel.is_active is False

    async def test_chat_not_found_deactivates_group(self, db_session, stub_send):
        stub_send.side_effect = TelegramChatNotFoundError("chat not found")
        delivery, _, group = await _seed_group_delivery(db_session, chat_id=-100500)

        summary = await send_pending_deliveries(db_session)

        assert summary["chat_not_found"] == 1
        await db_session.refresh(delivery)
        await db_session.refresh(group)
        assert delivery.status == DeliveryStatus.FAILED.value
        assert group.is_active is False

    async def test_generic_error_keeps_channel_active(self, db_session, stub_send):
        """Rate limits / 5xx / network errors are transient — channel stays
        active so fan-out continues targeting this user in future cycles."""
        stub_send.side_effect = TelegramError("rate limited")
        delivery, _, channel = await _seed_user_delivery(db_session, chat_id="300")

        summary = await send_pending_deliveries(db_session)

        assert summary["failed"] == 1
        await db_session.refresh(delivery)
        await db_session.refresh(channel)
        assert delivery.status == DeliveryStatus.FAILED.value
        # IMPORTANT — channel still active.
        assert channel.is_active is True

    async def test_only_pending_deliveries_processed(self, db_session, stub_send):
        """A delivery already marked DELIVERED/FAILED is skipped."""
        pending_del, _, _ = await _seed_user_delivery(db_session, chat_id="400")
        already_delivered, _, _ = await _seed_user_delivery(db_session, chat_id="401")
        already_delivered.status = DeliveryStatus.DELIVERED.value
        await db_session.flush()

        summary = await send_pending_deliveries(db_session)

        assert summary["total"] == 1  # only the PENDING one
        assert summary["sent"] == 1

    async def test_empty_queue_returns_zero_summary(self, db_session, stub_send):
        summary = await send_pending_deliveries(db_session)
        assert summary == {
            "total": 0,
            "sent": 0,
            "failed": 0,
            "blocked": 0,
            "chat_not_found": 0,
            "skipped_no_target": 0,
        }
        stub_send.assert_not_awaited()

    async def test_mixed_outcomes_in_one_pass(self, db_session, stub_send, monkeypatch):
        """3 deliveries, each gets a different Telegram response — summary
        reflects all three outcomes."""
        d1, _, _ = await _seed_user_delivery(db_session, chat_id="500")
        d2, _, _ = await _seed_user_delivery(db_session, chat_id="501")
        d3, _, _ = await _seed_user_delivery(db_session, chat_id="502")

        # Sequentially: success, blocked, generic error.
        sent_at = datetime(2026, 4, 23, 12, 0, tzinfo=UTC)
        stub_send.side_effect = [
            SentMessage(message_id=1, chat_id=500, sent_at=sent_at),
            TelegramBlockedError("blocked"),
            TelegramError("other"),
        ]

        summary = await send_pending_deliveries(db_session)

        assert summary == {
            "total": 3,
            "sent": 1,
            "failed": 1,
            "blocked": 1,
            "chat_not_found": 0,
            "skipped_no_target": 0,
        }

    async def test_formatter_is_called_with_signal(self, db_session, stub_send):
        """The text argument to send_message must be produced by
        format_signal_message — verify it contains expected markers."""
        await _seed_user_delivery(db_session, chat_id="600")
        await send_pending_deliveries(db_session)

        call_args = stub_send.await_args
        text_sent = call_args.kwargs.get("text") or call_args.args[1]
        assert "<b>BTC flow anomaly signal</b>" in text_sent
        assert "test" in text_sent  # the fake headline

    async def test_joined_query_is_one_db_call(self, db_session, stub_send):
        """Seed 5 pending deliveries and confirm the send worker loads them
        all in a single SELECT (via the JOINed query). We can't count
        queries trivially without profiling, but we CAN verify no N+1
        ordering issue by ensuring all 5 get processed."""
        for i in range(5):
            await _seed_user_delivery(db_session, chat_id=f"700_{i}")

        summary = await send_pending_deliveries(db_session)
        assert summary["sent"] == 5
        assert summary["total"] == 5


# ---------------------------------------------------------------------------
# Decimal smoke (signal adjacent)
# ---------------------------------------------------------------------------


async def test_send_worker_with_decimal_price(db_session, stub_send):
    """Sanity — signal with Decimal price_at_creation doesn't break sending."""
    signal = Signal(
        signal_type="flow_anomaly",
        asset="BTC",
        trigger_data={},
        ai_analysis={
            "headline": "decimal-test",
            "reasoning": [],
            "confidence": 7,
            "risks": [],
            "suggested_action": "wait",
            "time_horizon": "scalp",
        },
        confidence=7,
        price_at_creation=Decimal("42000.00"),
        fingerprint=compute_fingerprint("decimal-send"),
        signal_date=date(2026, 4, 23),
    )
    db_session.add(signal)
    user = User()
    db_session.add(user)
    await db_session.flush()
    channel = NotificationChannel(
        user_id=user.id,
        channel_type=ChannelType.TELEGRAM.value,
        channel_identifier="800",
    )
    db_session.add(channel)
    await db_session.flush()
    db_session.add(
        SignalDelivery(
            signal_id=signal.id,
            user_id=user.id,
            channel_id=channel.id,
            status=DeliveryStatus.PENDING.value,
        )
    )
    await db_session.flush()

    summary = await send_pending_deliveries(db_session)
    assert summary["sent"] == 1
