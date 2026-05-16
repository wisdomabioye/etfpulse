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
    TelegramChatMigratedError,
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
from etfpulse.pipeline.track_record import TrackRecordStatByHorizon


def _stat_for_swing(by_floor: dict[int, tuple[int, int]]) -> TrackRecordStatByHorizon:
    """PR B (#60) — helper builds a TrackRecordStatByHorizon where the
    swing bucket carries `by_floor` and every other bucket is empty.
    `_signal_with_ai` fixtures use `time_horizon="swing"`, so the per-signal
    alert reads the swing bucket; mirroring that here keeps tests focused
    on the cohort math rather than the bucketing logic (covered separately
    in test_track_record)."""
    by_floor_and_horizon: dict = {}
    for floor in range(1, 11):
        for label in ("scalp", "swing", "position", "legacy"):
            by_floor_and_horizon[(floor, label)] = by_floor[floor] if label == "swing" else (0, 0)
    return TrackRecordStatByHorizon(by_floor_and_horizon=by_floor_and_horizon)


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
    def test_full_signal_renders_skim_only_shape(self):
        """PR H.2 — alert carries title, headline, decision/levels, footer.
        Reasoning, regime, news, and risks live on /signals/:id (linked via
        the inline keyboard) — duplicating them here was producing 30-line
        messages most users skipped past."""
        msg = format_signal_message(_signal_with_ai())

        assert "<b>BTC flow anomaly signal</b>" in msg
        assert "BTC inflows snap 4-day streak" in msg
        # Decision block carries direction · confidence · horizon on one line.
        assert "<b>Decision:</b>" in msg
        assert "consider short" in msg
        assert "Conf 7/10" in msg
        assert "swing" in msg
        assert "2026-04-23" in msg  # signal_date
        assert "2026-04-26 10:30 UTC" in msg  # expires_at

        # PR H.3 — Risks are back in the alert (capped to top 2).
        assert "<b>Risks:</b>" in msg
        assert "Macro headwind" in msg

        # Sections still owned exclusively by /signals/:id.
        assert "Volume spike" not in msg
        assert "Institutional rotation" not in msg
        assert "<b>Reasoning:</b>" not in msg
        assert "<b>Market regime:</b>" not in msg
        assert "<b>News context:</b>" not in msg

    def test_risks_capped_to_top_two_bullets(self):
        """PR H.3 — risks come from the AI in priority order; the alert
        renders the first 2 only. Anyone wanting the full list follows the
        inline keyboard to /signals/:id."""
        signal = _signal_with_ai(
            ai_analysis={
                "headline": "Risky breakout",
                "reasoning": [],
                "confidence": 7,
                "risks": ["FOMC tomorrow", "Thin Asia liquidity", "Regime uncertain"],
                "suggested_action": "consider long",
                "time_horizon": "swing",
            }
        )
        msg = format_signal_message(signal)
        assert "FOMC tomorrow" in msg
        assert "Thin Asia liquidity" in msg
        assert "Regime uncertain" not in msg

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
        # No AI + no confidence → no decision block at all.
        assert "<b>Decision:</b>" not in msg

    def test_html_special_chars_escaped(self):
        """LLM might return `<` / `>` / `&` in headline, suggested_action,
        or risks. Must escape to prevent HTML parse errors OR (worst case)
        injection. Reasoning is no longer rendered (PR H.2) so not covered."""
        signal = _signal_with_ai(
            ai_analysis={
                "headline": "BTC <script>alert(1)</script> breakout",
                "reasoning": [],
                "confidence": 7,
                "risks": ["Fake & risk"],
                "suggested_action": "consider long & hold",
                "time_horizon": "scalp",
            }
        )
        msg = format_signal_message(signal)

        # Dangerous raw tags must not appear.
        assert "<script>" not in msg
        # Escaped versions of the rendered fields ARE present.
        assert "&lt;script&gt;alert(1)&lt;/script&gt;" in msg
        assert "consider long &amp; hold" in msg
        assert "Fake &amp; risk" in msg

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
    # PR H.2 — regime + news blocks were removed from the alert body. They
    # remain visible on /signals/:id (linked via the inline keyboard). These
    # tests pin the absence so a future "render regime in the alert" change
    # has to deliberately update both the formatter and these assertions.
    # -----------------------------------------------------------------------

    def test_regime_at_creation_does_not_render_in_alert(self):
        """Even when `trigger_data.regime_at_creation` is fully populated,
        the alert body MUST NOT echo it. The web detail page surfaces this."""
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

        assert "<b>Market regime:</b>" not in msg
        assert "distribution" not in msg
        assert "FOMC meeting" not in msg

    def test_news_context_does_not_render_in_alert(self):
        """Even when `trigger_data.news_context` is fully populated, the
        alert body MUST NOT echo it. The web detail page surfaces this."""
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

        assert "<b>News context:</b>" not in msg
        assert "BTC breakout above 70k" not in msg
        assert "Spot inflows accelerated" not in msg

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
    # Decision block — direction + confidence + horizon on line 1, then
    # spot/entry/stop/target/R:R on line 2. Replaces the old "meta line +
    # separate Action levels block" — traders see actionable numbers first.
    # -----------------------------------------------------------------------

    def test_decision_block_renders_levels_when_signal_has_entry_stop_target(self):
        signal = _signal_with_ai(
            entry_price=Decimal("84200.00"),
            stop_price=Decimal("82000.00"),
            target_price=Decimal("89500.00"),
        )
        msg = format_signal_message(signal)
        assert "<b>Decision:</b>" in msg
        assert "Entry:</i> $84,200.00" in msg
        assert "Stop:</i> $82,000.00" in msg
        assert "Target:</i> $89,500.00" in msg

    def test_decision_block_includes_risk_reward_when_all_three_set(self):
        # |89500 - 84200| / |84200 - 82000| = 5300 / 2200 ≈ 2.4
        signal = _signal_with_ai(
            entry_price=Decimal("84200.00"),
            stop_price=Decimal("82000.00"),
            target_price=Decimal("89500.00"),
        )
        msg = format_signal_message(signal)
        assert "R:R</i> 1:2.4" in msg

    def test_decision_block_omits_risk_reward_when_any_leg_missing(self):
        signal = _signal_with_ai(
            entry_price=Decimal("84200.00"),
            stop_price=None,
            target_price=Decimal("89500.00"),
        )
        msg = format_signal_message(signal)
        # Decision block still shows the parts that ARE set, but no R:R.
        assert "<b>Decision:</b>" in msg
        assert "Entry:</i>" in msg
        assert "Target:</i>" in msg
        assert "R:R" not in msg

    def test_decision_block_omits_risk_reward_when_stop_equals_entry(self):
        """Risk distance == 0 would divide by zero — defensive guard mirrors
        frontend `SuggestedActionPanel.computeRiskReward`."""
        signal = _signal_with_ai(
            entry_price=Decimal("84200"),
            stop_price=Decimal("84200"),
            target_price=Decimal("89500"),
        )
        msg = format_signal_message(signal)
        assert "<b>Decision:</b>" in msg
        assert "R:R" not in msg

    def test_decision_block_renders_when_only_direction_and_confidence_set(self):
        """Legacy v1/v2 signal with no AI-suggested prices — decision line
        still renders (direction · confidence · horizon), prices line absent."""
        signal = _signal_with_ai()  # no entry/stop/target overrides
        msg = format_signal_message(signal)
        assert "<b>Decision:</b>" in msg
        assert "consider short" in msg
        assert "Conf 7/10" in msg
        # No price legs → no Entry/Stop/Target/Spot/R:R fragments.
        assert "Entry:" not in msg
        assert "Spot:" not in msg

    def test_decision_block_renders_spot_when_price_at_creation_set(self):
        """price_at_creation (live spot at signal-build time) anchors the
        decision line so traders see the market price the alert was issued
        against — independent of whether AI volunteered entry/stop/target."""
        signal = _signal_with_ai(price_at_creation=Decimal("82352.65"))
        msg = format_signal_message(signal)
        assert "<b>Decision:</b>" in msg
        assert "Spot:</i> $82,352.65" in msg

    def test_track_record_stat_renders_when_stat_supplied_and_cohort_has_data(self):
        signal = _signal_with_ai()  # confidence=7, time_horizon=swing
        stat = _stat_for_swing(
            {
                7: (15, 9),  # 9/15 → 60%
                **{floor: (0, 0) for floor in [1, 2, 3, 4, 5, 6, 8, 9, 10]},
            }
        )
        msg = format_signal_message(signal, track_record_stat=stat)
        # PR B (#60) — line is now prefixed by the signal's horizon
        # ("Our swing signals at confidence ≥7..."). Verifies the bucket
        # lookup ran against the right horizon.
        assert "Our swing signals at confidence ≥7 hit target 60% of the time" in msg
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
        stat = _stat_for_swing({floor: (10, 6) for floor in range(1, 11)})
        msg = format_signal_message(signal, track_record_stat=stat)
        assert "Our swing signals at confidence" not in msg

    def test_track_record_stat_omitted_when_cohort_is_empty(self):
        """Fresh deploy: no signals scored at this floor yet → null hit_rate
        → line skipped (better than rendering '0% over 0 evaluated')."""
        signal = _signal_with_ai()  # confidence=7, time_horizon=swing
        stat = _stat_for_swing({floor: (0, 0) for floor in range(1, 11)})
        msg = format_signal_message(signal, track_record_stat=stat)
        assert "Our swing signals at confidence" not in msg

    def test_no_ai_omits_regime_and_news_dump(self):
        """PR H.2 — no-AI fallback dumps trigger_data but skips the bulky
        regime/news JSONB blobs (they'd dwarf the actual detector signal).
        The web detail page renders the full objects for anyone who follows
        the inline keyboard link."""
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
        # Bulky JSONB blobs MUST NOT appear in the dump or anywhere else.
        assert "regime_at_creation:" not in msg
        assert "news_context:" not in msg
        assert "<b>Market regime:</b>" not in msg
        assert "<b>News context:</b>" not in msg
        assert "Macro update" not in msg
        # Real detector keys still appear in the dump.
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
        # Branch 2: default 0 + 1 = 1 (was 2 under the legacy off-by-one
        # where default=1 doubled with the on-attempt increment).
        assert delivery.attempt_count == 1
        # last_attempt_at MUST be set (even on success — historical record).
        assert delivery.last_attempt_at is not None

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

    async def test_chat_migrated_updates_group_chat_id(self, db_session, stub_send):
        """Basic→supergroup migration: self-heal `TelegramGroup.chat_id` so
        the NEXT signal lands on the migrated chat. THIS delivery is lost
        (stale by retry time), but the group stays active."""
        new_chat_id = -1003991800653
        stub_send.side_effect = TelegramChatMigratedError(
            f"Group migrated to supergroup. New chat id: {new_chat_id}",
            new_chat_id=new_chat_id,
        )
        delivery, _, group = await _seed_group_delivery(db_session, chat_id=-100500)

        summary = await send_pending_deliveries(db_session)

        assert summary["migrated"] == 1
        assert summary["failed"] == 0  # NOT the generic transient bucket
        await db_session.refresh(delivery)
        await db_session.refresh(group)
        assert delivery.status == DeliveryStatus.FAILED.value
        assert f"new chat_id={new_chat_id}" in delivery.error_message
        # The actual heal — future fan-outs use the new id.
        assert group.chat_id == new_chat_id
        # Group MUST stay reachable; it didn't disappear, it just moved.
        assert group.is_active is True

    async def test_chat_migrated_collision_soft_deletes_stale_row(self, db_session, stub_send):
        """Issue #77 — regression for the prod re-delivery loop.

        Setup: TWO TelegramGroup rows exist — `legacy` (pre-migration chat_id)
        and `survivor` (already registered at the post-migration chat_id, e.g.
        via `my_chat_member`). A pending delivery targets `legacy`; Telegram
        returns ChatMigrated pointing at `survivor`'s chat_id.

        The old code did `legacy.chat_id = exc.new_chat_id` unconditionally,
        violating `uq_telegram_groups_chat_id` on flush → wrapper rollback →
        every sibling DELIVERED state in the same tick reverted to PENDING →
        worker re-sent the same messages every 30s forever.

        New behavior: collision detected → `legacy.is_active=False`, sentinel
        error message names the survivor, NO IntegrityError, `survivor` is
        untouched (still active, still holds the migrated chat_id).
        """
        legacy_chat = -100500
        survivor_chat = -1003991800653
        stub_send.side_effect = TelegramChatMigratedError(
            f"Group migrated to supergroup. New chat id: {survivor_chat}",
            new_chat_id=survivor_chat,
        )
        delivery, _, legacy = await _seed_group_delivery(db_session, chat_id=legacy_chat)
        survivor = TelegramGroup(chat_id=survivor_chat, title="Migrated Supergroup")
        db_session.add(survivor)
        await db_session.flush()
        survivor_id = survivor.id

        # The actual contract: this call must NOT raise. The prior bug
        # surfaced as IntegrityError on flush inside the worker.
        summary = await send_pending_deliveries(db_session)

        assert summary["migrated"] == 1
        assert summary["failed"] == 0
        await db_session.refresh(delivery)
        await db_session.refresh(legacy)
        await db_session.refresh(survivor)

        # Stale row: deactivated, chat_id UNTOUCHED (would have collided).
        assert legacy.is_active is False
        assert legacy.chat_id == legacy_chat

        # Survivor: completely untouched.
        assert survivor.is_active is True
        assert survivor.chat_id == survivor_chat
        assert survivor.id == survivor_id

        # Delivery: terminal with a sentinel that names the surviving group
        # so an operator can match logs without parsing chat_ids.
        assert delivery.status == DeliveryStatus.FAILED.value
        assert delivery.error_message == (
            f"migrated: target already registered as group_id={survivor.id}"
        )

    async def test_chat_migrated_collision_does_not_rollback_siblings(self, db_session, stub_send):
        """The actual user-visible symptom from issue #77 — sibling deliveries
        in the same tick must commit independently of the collision.

        Before the fix: IntegrityError from the collision propagated out of
        the per-row try/except (it raised at session.flush()), the wrapper
        rolled back the WHOLE tick, and the already-sent DM messages stayed
        PENDING → re-sent every 30s.

        After the fix: collision is handled cleanly inline, the healthy DM
        delivery in the same tick lands as DELIVERED on the very first tick.
        """
        legacy_chat = -100501
        survivor_chat = -1003991800654
        legacy_delivery, _, legacy = await _seed_group_delivery(db_session, chat_id=legacy_chat)
        survivor = TelegramGroup(chat_id=survivor_chat, title="Migrated Supergroup")
        db_session.add(survivor)
        await db_session.flush()

        healthy_delivery, _, _ = await _seed_user_delivery(db_session, chat_id="700")

        sent_at = datetime(2026, 4, 23, 12, 0, tzinfo=UTC)

        def _route(chat_id, *args, **kwargs):
            if chat_id == legacy_chat:
                raise TelegramChatMigratedError(
                    f"Group migrated to supergroup. New chat id: {survivor_chat}",
                    new_chat_id=survivor_chat,
                )
            return SentMessage(message_id=99, chat_id=int(chat_id), sent_at=sent_at)

        stub_send.side_effect = _route

        summary = await send_pending_deliveries(db_session)
        # IMPORTANT — the wrapper isn't in the loop here, so we mimic the
        # real flush boundary by committing. Pre-fix this would IntegrityError.
        await db_session.commit()

        assert summary["migrated"] == 1
        assert summary["sent"] == 1

        await db_session.refresh(healthy_delivery)
        await db_session.refresh(legacy_delivery)
        await db_session.refresh(legacy)

        # The actual regression check — healthy delivery is DELIVERED, not
        # rolled back to PENDING by the sibling collision.
        assert healthy_delivery.status == DeliveryStatus.DELIVERED.value
        assert healthy_delivery.delivered_at is not None

        # And the collision row is properly terminal.
        assert legacy_delivery.status == DeliveryStatus.FAILED.value
        assert legacy.is_active is False

    async def test_chat_migrated_collision_multiple_stale_rows(self, db_session, stub_send):
        """Defensive — two stale TelegramGroup rows both migrate to the same
        survivor in one tick. Each must soft-delete cleanly without
        cross-row interference."""
        survivor_chat = -1003991800655
        survivor = TelegramGroup(chat_id=survivor_chat, title="Migrated Supergroup")
        db_session.add(survivor)
        await db_session.flush()

        stale_a_delivery, _, stale_a = await _seed_group_delivery(db_session, chat_id=-100600)
        stale_b_delivery, _, stale_b = await _seed_group_delivery(db_session, chat_id=-100601)

        stub_send.side_effect = TelegramChatMigratedError(
            f"Group migrated to supergroup. New chat id: {survivor_chat}",
            new_chat_id=survivor_chat,
        )

        summary = await send_pending_deliveries(db_session)
        await db_session.commit()

        assert summary["migrated"] == 2
        for delivery, stale in (
            (stale_a_delivery, stale_a),
            (stale_b_delivery, stale_b),
        ):
            await db_session.refresh(delivery)
            await db_session.refresh(stale)
            assert stale.is_active is False
            assert delivery.status == DeliveryStatus.FAILED.value
            assert delivery.error_message == (
                f"migrated: target already registered as group_id={survivor.id}"
            )

        await db_session.refresh(survivor)
        assert survivor.is_active is True
        assert survivor.chat_id == survivor_chat

    async def test_transient_error_stays_pending_for_retry(self, db_session, stub_send):
        """Branch 2: transient errors (rate limit / 5xx / network) keep
        the row PENDING until `delivery_max_attempts` is reached. Channel
        stays active either way. `last_attempt_at` advances so the next
        tick respects the backoff window."""
        stub_send.side_effect = TelegramError("rate limited")
        delivery, _, channel = await _seed_user_delivery(db_session, chat_id="300")

        summary = await send_pending_deliveries(db_session)

        # Single attempt at count=1 — below default cap=5 → retry path.
        assert summary["retrying"] == 1
        assert summary["failed"] == 0
        await db_session.refresh(delivery)
        await db_session.refresh(channel)
        assert delivery.status == DeliveryStatus.PENDING.value
        assert delivery.attempt_count == 1
        assert delivery.last_attempt_at is not None
        # Error message records the most recent failure cause — readable
        # even while the row is still PENDING.
        assert "rate limited" in delivery.error_message
        # IMPORTANT — channel still active so future signals try again.
        assert channel.is_active is True

    async def test_successful_retry_clears_stale_error_message(self, db_session, stub_send):
        """Branch 2: when a row finally succeeds after one or more transient
        retries, the DELIVERED row must NOT carry the error_message from
        the prior failed attempt — admin metrics / dashboards would
        otherwise read it as "delivered, but with this error" which is
        nonsensical. Pre-seed the row in the post-transient-failure state
        the worker would have left it in, then exercise a successful pickup."""
        delivery, _, _ = await _seed_user_delivery(db_session, chat_id="302")
        # Simulate state after a transient failure: attempted once, error
        # recorded, status still PENDING (waiting for retry).
        delivery.attempt_count = 1
        delivery.last_attempt_at = None  # eligible via the NULL branch
        delivery.error_message = "Too Many Requests: retry after 30"
        await db_session.flush()

        # Default stub_send returns SentMessage → success.
        summary = await send_pending_deliveries(db_session)

        assert summary["sent"] == 1
        await db_session.refresh(delivery)
        assert delivery.status == DeliveryStatus.DELIVERED.value
        # The stale "Too Many Requests" message MUST be gone.
        assert delivery.error_message is None

    async def test_transient_error_terminal_at_cap(self, db_session, stub_send, monkeypatch):
        """When `attempt_count` reaches `delivery_max_attempts`, a
        transient failure flips the row to FAILED with a descriptive
        error message. Channel still stays active — the failure is
        delivery-instance specific, not target-wide."""
        from etfpulse.config import settings

        # Squeeze cap to 2 so we can exercise it without seeding 4
        # mock retries by hand.
        monkeypatch.setattr(settings, "delivery_max_attempts", 2)

        stub_send.side_effect = TelegramError("rate limited again")
        delivery, _, channel = await _seed_user_delivery(db_session, chat_id="301")
        # Pre-seed the row at attempt_count = max - 1 so this single call
        # bumps it to max, hitting the cap branch.
        delivery.attempt_count = 1
        delivery.last_attempt_at = None  # still eligible via the NULL clause
        await db_session.flush()

        summary = await send_pending_deliveries(db_session)

        assert summary["failed"] == 1
        assert summary["retrying"] == 0
        await db_session.refresh(delivery)
        await db_session.refresh(channel)
        assert delivery.status == DeliveryStatus.FAILED.value
        assert "max_attempts=2 reached" in delivery.error_message
        # Channel stays active — Telegram rate-limit is not a "this
        # target is dead" signal.
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
            "migrated": 0,
            "retrying": 0,
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
            "failed": 0,
            "blocked": 1,
            "chat_not_found": 0,
            "migrated": 0,
            # Generic TelegramError at count=1 is below default cap=5 →
            # retrying bucket, NOT failed.
            "retrying": 1,
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


class TestRetryBackoffFilter:
    """Branch 2 — the WHERE clause that defers re-picking a row until its
    backoff window elapses. The math is `base * 2^(attempt_count - 1)`."""

    async def test_pending_row_not_in_backoff_window_is_skipped(
        self, db_session, stub_send, monkeypatch
    ):
        """A row that JUST failed transiently (last_attempt_at = now,
        attempt_count = 1) must NOT be re-picked by the next tick — the
        30s default backoff hasn't elapsed."""
        from etfpulse.config import settings

        monkeypatch.setattr(settings, "delivery_retry_base_seconds", 30)

        delivery, _, _ = await _seed_user_delivery(db_session, chat_id="800")
        # Simulate "tick 1 just ran transient-failed this row".
        delivery.attempt_count = 1
        delivery.last_attempt_at = datetime.now(UTC)
        await db_session.flush()

        summary = await send_pending_deliveries(db_session)

        # Worker query filters this row out — it's still in backoff.
        assert summary["total"] == 0
        stub_send.assert_not_awaited()
        # Row state untouched.
        await db_session.refresh(delivery)
        assert delivery.status == DeliveryStatus.PENDING.value
        assert delivery.attempt_count == 1

    async def test_pending_row_past_backoff_is_picked_up(self, db_session, stub_send, monkeypatch):
        """When the elapsed time since last_attempt_at exceeds the backoff
        window, the row IS re-picked. Simulate by setting last_attempt_at
        far in the past + a tiny base so the math definitely elapses."""
        from datetime import timedelta

        from etfpulse.config import settings

        monkeypatch.setattr(settings, "delivery_retry_base_seconds", 30)

        delivery, _, _ = await _seed_user_delivery(db_session, chat_id="801")
        # attempt_count=1 → backoff = 30 * 2^0 = 30s. Set last_attempt_at
        # 5 min ago — well past the window.
        delivery.attempt_count = 1
        delivery.last_attempt_at = datetime.now(UTC) - timedelta(seconds=300)
        await db_session.flush()

        summary = await send_pending_deliveries(db_session)

        # This time the row IS picked up.
        assert summary["total"] == 1
        assert summary["sent"] == 1
        await db_session.refresh(delivery)
        # attempt_count incremented to 2 on this attempt.
        assert delivery.attempt_count == 2
        assert delivery.status == DeliveryStatus.DELIVERED.value

    async def test_backoff_doubles_per_attempt(self, db_session, stub_send, monkeypatch):
        """`attempt_count=3` means we already attempted 3 times. Backoff
        for the NEXT pickup = `base * 2^(3-1) = 4 * base`. A row with
        last_attempt_at younger than 4 * base must NOT be re-picked."""
        from datetime import timedelta

        from etfpulse.config import settings

        # base=10s → backoffs: 10, 20, 40, 80s for attempts 1,2,3,4.
        monkeypatch.setattr(settings, "delivery_retry_base_seconds", 10)

        delivery, _, _ = await _seed_user_delivery(db_session, chat_id="802")
        delivery.attempt_count = 3
        # 30s ago — past attempt-1's 10s but BEFORE attempt-3's 40s window.
        delivery.last_attempt_at = datetime.now(UTC) - timedelta(seconds=30)
        await db_session.flush()

        summary = await send_pending_deliveries(db_session)

        # Filtered out — backoff for attempt 3 is 40s, only 30s elapsed.
        assert summary["total"] == 0

        # Now push it further into the past so the 40s window IS exceeded.
        delivery.last_attempt_at = datetime.now(UTC) - timedelta(seconds=50)
        await db_session.flush()
        # Fresh send fixture call expected — re-seed stub return.
        stub_send.reset_mock()

        summary = await send_pending_deliveries(db_session)
        assert summary["total"] == 1
        assert summary["sent"] == 1

    async def test_null_last_attempt_at_always_picked_up(self, db_session, stub_send):
        """Fresh row from fan-out has last_attempt_at IS NULL — backoff
        math doesn't apply. Must be picked up on the first tick."""
        delivery, _, _ = await _seed_user_delivery(db_session, chat_id="803")
        assert delivery.last_attempt_at is None
        assert delivery.attempt_count == 0  # new default

        summary = await send_pending_deliveries(db_session)

        assert summary["total"] == 1
        assert summary["sent"] == 1
        await db_session.refresh(delivery)
        assert delivery.attempt_count == 1
        assert delivery.last_attempt_at is not None


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
        """A 3-message tick should issue ONE
        get_stats_by_confidence_floor_and_horizon call, not three. Pins
        the cache + per-tick prefetch contract."""
        # Seed 3 deliveries.
        for i in range(3):
            await _seed_user_delivery(db_session, chat_id=f"500{i}", confidence=7)

        call_count = {"n": 0}
        original = _stat_for_swing({floor: (10, 7) for floor in range(1, 11)})

        async def _stub(session):
            call_count["n"] += 1
            return original

        monkeypatch.setattr(
            "etfpulse.pipeline.delivery.get_stats_by_confidence_floor_and_horizon", _stub
        )

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
            return _stat_for_swing({floor: (5, 3) for floor in range(1, 11)})

        monkeypatch.setattr(
            "etfpulse.pipeline.delivery.get_stats_by_confidence_floor_and_horizon", _stub
        )

        await send_pending_deliveries(db_session)
        # Seed a second delivery so the second tick has work to do.
        # Distinct chat_id → distinct fingerprint per the helper's contract.
        await _seed_user_delivery(db_session, chat_id="602", confidence=7)
        await send_pending_deliveries(db_session)

        assert call_count["n"] == 1, "cache must absorb the second tick's stat lookup"


# ---------------------------------------------------------------------------
# build_signal_keyboard (issue #38)
# ---------------------------------------------------------------------------


class TestBuildSignalKeyboard:
    def test_none_when_frontend_url_unset(self, monkeypatch):
        """Empty `frontend_url` → no button. Caller passes None to the
        adapter, which sends the message without any reply_markup."""
        from etfpulse.config import settings
        from etfpulse.pipeline.delivery import build_signal_keyboard

        monkeypatch.setattr(settings, "frontend_url", "")
        signal = _signal_with_ai()
        signal.id = 42
        assert build_signal_keyboard(signal) is None

    def test_renders_deep_link_when_configured(self, monkeypatch):
        """Configured `frontend_url` → InlineKeyboardMarkup with one button
        pointing at `/signals/<id>` on the configured origin."""
        from etfpulse.config import settings
        from etfpulse.pipeline.delivery import build_signal_keyboard

        monkeypatch.setattr(settings, "frontend_url", "https://etfpulse.example.com")
        signal = _signal_with_ai()
        signal.id = 42
        kb = build_signal_keyboard(signal)
        assert kb is not None
        button = kb.inline_keyboard[0][0]
        assert button.url == "https://etfpulse.example.com/signals/42"
        assert "View on web" in button.text

    def test_trailing_slash_is_stripped(self, monkeypatch):
        """A trailing slash in `frontend_url` shouldn't produce `//signals/`."""
        from etfpulse.config import settings
        from etfpulse.pipeline.delivery import build_signal_keyboard

        monkeypatch.setattr(settings, "frontend_url", "https://etfpulse.example.com/")
        signal = _signal_with_ai()
        signal.id = 7
        kb = build_signal_keyboard(signal)
        assert kb is not None
        assert kb.inline_keyboard[0][0].url == "https://etfpulse.example.com/signals/7"
