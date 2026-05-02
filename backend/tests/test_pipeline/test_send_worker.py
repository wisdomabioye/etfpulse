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
from etfpulse.pipeline.track_record import TrackRecordStat

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

    # -----------------------------------------------------------------------
    # Stage 7-P9 additions — regime + news_context blocks. Both render only
    # when the matching trigger_data key is present, so older signals (built
    # before the v2 prompt) format identically to pre-Stage-7.
    # -----------------------------------------------------------------------

    def test_regime_block_renders_when_trigger_data_has_regime_at_creation(self):
        signal = _signal_with_ai(
            trigger_data={
                "streak_length": 4,
                "regime_at_creation": {
                    "regime": "distribution",
                    "signal_posture": "cautious",
                    "confidence": 7,
                    "macro_events_nearby": ["FOMC meeting"],
                },
            }
        )
        msg = format_signal_message(signal)

        assert "<b>Market regime:</b>" in msg
        assert "Regime:</i> distribution" in msg
        assert "Posture:</i> cautious" in msg
        assert "Conf:</i> 7/10" in msg
        assert "Macro nearby:" in msg
        assert "FOMC meeting" in msg

    def test_regime_block_omitted_when_absent(self):
        """Signals built before Stage 7-P6 have no `regime_at_creation` key
        — message must format without the regime block, no placeholder."""
        signal = _signal_with_ai(trigger_data={"streak_length": 4})
        msg = format_signal_message(signal)

        assert "<b>Market regime:</b>" not in msg
        assert "Regime:" not in msg

    def test_regime_block_tolerates_partial_blob(self):
        """A future writer that drops one of the keys must not 500 the send.
        Posture-only blob renders just the posture bit."""
        signal = _signal_with_ai(
            trigger_data={
                "regime_at_creation": {"signal_posture": "paused"},
            }
        )
        msg = format_signal_message(signal)

        assert "Posture:</i> paused" in msg
        assert "Regime:</i>" not in msg

    def test_regime_block_html_escaped(self):
        """JSONB blob is technically writeable; escape every dynamic value."""
        signal = _signal_with_ai(
            trigger_data={
                "regime_at_creation": {
                    "regime": "<script>x</script>",
                    "macro_events_nearby": ["FOMC & CPI"],
                },
            }
        )
        msg = format_signal_message(signal)

        assert "<script>x</script>" not in msg
        assert "&lt;script&gt;" in msg
        assert "FOMC &amp; CPI" in msg

    def test_news_block_renders_with_title_and_summary(self):
        signal = _signal_with_ai(
            trigger_data={
                "news_context": [
                    {
                        "title": "BTC breakout above 70k",
                        "summary": "Spot inflows accelerated this morning.",
                        "category": 1,
                        "published_iso": "2026-04-23T08:00:00Z",
                    }
                ],
            }
        )
        msg = format_signal_message(signal)

        assert "<b>News context:</b>" in msg
        assert "BTC breakout above 70k" in msg
        assert "Spot inflows accelerated" in msg

    def test_news_block_caps_to_three_items(self):
        """`gather_news_context` can return 10+ items on busy days; the
        formatter caps to 3 to stay well under Telegram's 4000-char limit."""
        signal = _signal_with_ai(
            trigger_data={
                "news_context": [
                    {
                        "title": f"Item {i}",
                        "summary": f"Summary {i}",
                        "category": 1,
                        "published_iso": "x",
                    }
                    for i in range(10)
                ],
            }
        )
        msg = format_signal_message(signal)

        assert "Item 0" in msg
        assert "Item 1" in msg
        assert "Item 2" in msg
        # Items 3+ must not appear — caller would need to follow the link
        # to /signals/:id for the full list.
        assert "Item 3" not in msg
        assert "Item 9" not in msg

    def test_news_block_trims_long_summary(self):
        """A 500-char summary must be trimmed to keep the message terse."""
        signal = _signal_with_ai(
            trigger_data={
                "news_context": [
                    {
                        "title": "Long",
                        "summary": "A" * 500,
                        "category": 1,
                        "published_iso": "x",
                    }
                ],
            }
        )
        msg = format_signal_message(signal)

        # Trimmed to 180-1 chars + ellipsis, so 500 A's must NOT all appear.
        assert "A" * 500 not in msg
        assert "…" in msg

    def test_news_block_omitted_when_absent(self):
        signal = _signal_with_ai(trigger_data={"streak_length": 4})
        msg = format_signal_message(signal)

        assert "News context:" not in msg

    def test_news_block_skips_items_with_no_title_or_summary(self):
        """Items where both title and summary are null/empty contribute
        nothing useful — they must be skipped, not rendered as bullets."""
        signal = _signal_with_ai(
            trigger_data={
                "news_context": [
                    {"title": None, "summary": None, "category": 1, "published_iso": "x"},
                    {
                        "title": "Real headline",
                        "summary": None,
                        "category": 1,
                        "published_iso": "x",
                    },
                ],
            }
        )
        msg = format_signal_message(signal)

        assert "Real headline" in msg
        # The empty item must not produce a stray "• " bullet.
        # (Counting the "Real headline" bullet — exactly one news bullet.)
        assert msg.count("• <b>Real headline</b>") == 1

    def test_no_ai_trigger_dump_filters_before_slicing(self):
        """The 6-key cap on the trigger-data dump must apply to *rendered*
        keys, not iterated ones. Regression: an earlier version sliced
        `[:6]` BEFORE the regime/news skip filter, so a trigger_data whose
        first 6 insertion-order keys included `regime_at_creation` and
        `news_context` would silently drop real detector keys past index 6."""
        # Insertion order: regime + news first (mimicking an old writer
        # that prepended them), then 6 detector keys. Without the fix, only
        # 4 detector keys would render. With the fix, all 6 render.
        signal = _signal_with_ai(
            ai_analysis=None,
            confidence=None,
            trigger_data={
                "regime_at_creation": {"regime": "markup"},
                "news_context": [
                    {"title": "x", "summary": "y", "category": 1, "published_iso": "z"}
                ],
                "detector_a": 1,
                "detector_b": 2,
                "detector_c": 3,
                "detector_d": 4,
                "detector_e": 5,
                "detector_f": 6,
            },
        )
        msg = format_signal_message(signal)

        for key in (
            "detector_a",
            "detector_b",
            "detector_c",
            "detector_d",
            "detector_e",
            "detector_f",
        ):
            assert key in msg, f"trigger-dump cap should not have dropped {key}"

    # -----------------------------------------------------------------------
    # Stage 8-P8 — action block (entry/stop/target) + track-record stat line
    # -----------------------------------------------------------------------

    def test_action_block_renders_when_signal_has_entry_stop_target(self):
        signal = _signal_with_ai(
            entry_price=Decimal("84200.00"),
            stop_price=Decimal("82000.00"),
            target_price=Decimal("89500.00"),
        )
        msg = format_signal_message(signal)
        assert "<b>Action levels:</b>" in msg
        assert "Entry:</i> $84,200.00" in msg
        assert "Stop:</i> $82,000.00" in msg
        assert "Target:</i> $89,500.00" in msg

    def test_action_block_includes_risk_reward_when_all_three_set(self):
        # |89500 - 84200| / |84200 - 82000| = 5300 / 2200 ≈ 2.4
        signal = _signal_with_ai(
            entry_price=Decimal("84200.00"),
            stop_price=Decimal("82000.00"),
            target_price=Decimal("89500.00"),
        )
        msg = format_signal_message(signal)
        assert "R:R</i> 1:2.4" in msg

    def test_action_block_omits_risk_reward_when_any_leg_missing(self):
        signal = _signal_with_ai(
            entry_price=Decimal("84200.00"),
            stop_price=None,
            target_price=Decimal("89500.00"),
        )
        msg = format_signal_message(signal)
        # Action block still shows the parts that ARE set, but no R:R.
        assert "<b>Action levels:</b>" in msg
        assert "Entry:</i>" in msg
        assert "Target:</i>" in msg
        assert "R:R" not in msg

    def test_action_block_omits_risk_reward_when_stop_equals_entry(self):
        """Risk distance == 0 would divide by zero — defensive guard mirrors
        frontend `SuggestedActionPanel.computeRiskReward`."""
        signal = _signal_with_ai(
            entry_price=Decimal("84200"),
            stop_price=Decimal("84200"),
            target_price=Decimal("89500"),
        )
        msg = format_signal_message(signal)
        assert "<b>Action levels:</b>" in msg
        assert "R:R" not in msg

    def test_action_block_omitted_when_no_levels_set(self):
        """Legacy v1/v2 signal with no AI-suggested prices — section absent."""
        signal = _signal_with_ai()  # no entry/stop/target overrides
        msg = format_signal_message(signal)
        assert "Action levels:" not in msg

    def test_track_record_stat_renders_when_stat_supplied_and_cohort_has_data(self):
        signal = _signal_with_ai()  # confidence=7
        stat = TrackRecordStat(
            by_floor={
                7: (15, 9),  # 9/15 → 60%
                **{floor: (0, 0) for floor in [1, 2, 3, 4, 5, 6, 8, 9, 10]},
            }
        )
        msg = format_signal_message(signal, track_record_stat=stat)
        assert "Our signals at confidence ≥7 hit target 60% of the time" in msg
        assert "(over 15 evaluated)" in msg

    def test_track_record_stat_omitted_when_stat_not_supplied(self):
        """Default kwarg — legacy callers and the pure-formatter test surface
        get the message without the stat line."""
        signal = _signal_with_ai()
        msg = format_signal_message(signal)
        assert "Our signals at confidence" not in msg

    def test_track_record_stat_omitted_when_signal_has_null_confidence(self):
        """No confidence → can't pick a cohort floor → skip the line cleanly
        rather than rendering 'confidence ≥None hit target X%'."""
        signal = _signal_with_ai(ai_analysis=None, confidence=None)
        stat = TrackRecordStat(by_floor={floor: (10, 6) for floor in range(1, 11)})
        msg = format_signal_message(signal, track_record_stat=stat)
        assert "Our signals at confidence" not in msg

    def test_track_record_stat_omitted_when_cohort_is_empty(self):
        """Fresh deploy: no signals scored at this floor yet → null hit_rate
        → line skipped (better than rendering '0% over 0 evaluated')."""
        signal = _signal_with_ai()  # confidence=7
        stat = TrackRecordStat(by_floor={floor: (0, 0) for floor in range(1, 11)})
        msg = format_signal_message(signal, track_record_stat=stat)
        assert "Our signals at confidence" not in msg

    def test_no_ai_still_renders_regime_and_news_blocks(self):
        """Regime + news context are captured at build-time independent of
        AI success. A no-AI signal must still carry these reads."""
        signal = _signal_with_ai(
            ai_analysis=None,
            confidence=None,
            trigger_data={
                "streak_length": 4,
                "regime_at_creation": {"regime": "markup", "signal_posture": "normal"},
                "news_context": [
                    {
                        "title": "Macro update",
                        "summary": "details",
                        "category": 1,
                        "published_iso": "x",
                    }
                ],
            },
        )
        msg = format_signal_message(signal)

        assert "AI analysis unavailable" in msg
        assert "Regime:</i> markup" in msg
        assert "Macro update" in msg
        # The trigger_data dump must NOT echo the regime/news keys (they're
        # already rendered as their own blocks).
        assert "regime_at_creation:" not in msg
        assert "news_context:" not in msg
        # Other trigger_data keys still appear in the dump.
        assert "streak_length" in msg


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


@pytest.fixture(autouse=True)
def reset_track_record_cache():
    """Stage 8-P8 — `delivery._track_record_cache` is module-level state.
    Clear between tests so a stale cohort snapshot from one test can't
    leak into another's expectations."""
    from etfpulse.pipeline.delivery import _track_record_cache

    _track_record_cache.clear()
    yield
    _track_record_cache.clear()


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


# ---------------------------------------------------------------------------
# Stage 8-P8 — track-record stat is pre-fetched once per send tick
# ---------------------------------------------------------------------------


class TestTrackRecordStatPrefetch:
    async def test_send_worker_calls_get_stats_exactly_once_per_tick(
        self, db_session, stub_send, monkeypatch
    ):
        """A 3-message tick should issue ONE get_stats_by_confidence_floor
        call, not three. Pins the cache + per-tick prefetch contract."""
        # Seed 3 deliveries.
        for i in range(3):
            await _seed_user_delivery(db_session, chat_id=f"500{i}", confidence=7)

        call_count = {"n": 0}
        original = TrackRecordStat(by_floor={floor: (10, 7) for floor in range(1, 11)})

        async def _stub(session):
            call_count["n"] += 1
            return original

        monkeypatch.setattr("etfpulse.pipeline.delivery.get_stats_by_confidence_floor", _stub)

        await send_pending_deliveries(db_session)
        assert call_count["n"] == 1, "stat fetcher must be called once per tick, not per signal"

    async def test_cached_stat_skips_db_on_second_tick(self, db_session, stub_send, monkeypatch):
        """Two consecutive ticks within the TTL share one fetch — the second
        tick reads the cached snapshot."""
        await _seed_user_delivery(db_session, chat_id="601", confidence=7)
        await db_session.flush()

        call_count = {"n": 0}

        async def _stub(session):
            call_count["n"] += 1
            return TrackRecordStat(by_floor={floor: (5, 3) for floor in range(1, 11)})

        monkeypatch.setattr("etfpulse.pipeline.delivery.get_stats_by_confidence_floor", _stub)

        await send_pending_deliveries(db_session)
        # Seed a second delivery so the second tick has work to do.
        # Distinct chat_id → distinct fingerprint per the helper's contract.
        await _seed_user_delivery(db_session, chat_id="602", confidence=7)
        await send_pending_deliveries(db_session)

        assert call_count["n"] == 1, "cache must absorb the second tick's stat lookup"
