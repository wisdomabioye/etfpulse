"""Smoke tests for the full command handlers.

Handlers open their own `async_session()` via `etfpulse.db`. We patch that
import in each handler module to yield `db_session` wrapped in a SAVEPOINT
(`begin_nested`), so the handler's `session.commit()` commits to the
savepoint while the outer db_session transaction stays intact and rolls
back at test teardown. Standard SQLAlchemy test-isolation pattern — no real
data leaks to the test DB.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy import select
from telegram import Chat

from etfpulse.bot.handlers.help import cmd_help
from etfpulse.bot.handlers.prefs import cmd_prefs
from etfpulse.bot.handlers.start import cmd_start
from etfpulse.bot.handlers.subscribe import cmd_subscribe, cmd_unsubscribe
from etfpulse.bot.handlers.track_record import cmd_performance, cmd_track_record
from etfpulse.models import ChannelType, NotificationChannel, User

# The handler modules that import `async_session` directly.
_SESSION_CONSUMERS = (
    "etfpulse.bot.handlers.start",
    "etfpulse.bot.handlers.subscribe",
    "etfpulse.bot.handlers.prefs",
    "etfpulse.bot.handlers.track_record",
)


@pytest.fixture
def patch_session(monkeypatch, db_session):
    """Replace `async_session()` in each handler module with a context manager
    that yields db_session via SAVEPOINT. Handler commits hit the savepoint;
    outer db_session rollback undoes everything at test exit."""

    @asynccontextmanager
    async def _yielder():
        async with db_session.begin_nested():
            yield db_session

    for mod in _SESSION_CONSUMERS:
        monkeypatch.setattr(f"{mod}.async_session", _yielder)


def _dm_update(chat_id: int = 42, username: str = "alice") -> MagicMock:
    update = MagicMock()
    update.effective_chat.id = chat_id
    update.effective_chat.type = Chat.PRIVATE
    update.effective_chat.title = None
    update.effective_user.id = chat_id
    update.effective_user.username = username
    update.effective_message.reply_html = AsyncMock()
    return update


def _group_update(chat_id: int = -100, title: str = "Test Group") -> MagicMock:
    update = MagicMock()
    update.effective_chat.id = chat_id
    update.effective_chat.type = Chat.GROUP
    update.effective_chat.title = title
    update.effective_user.id = 999
    update.effective_user.username = "admin"
    update.effective_message.reply_html = AsyncMock()
    return update


def _ctx(args: list[str] | None = None) -> MagicMock:
    ctx = MagicMock()
    ctx.args = args or []
    return ctx


# ---- /start ----------------------------------------------------------------


class TestCmdStart:
    async def test_dm_creates_user_and_channel(self, db_session, patch_session):
        update = _dm_update(chat_id=100, username="alice")
        await cmd_start(update, _ctx())

        # Verify DB state (inside the SAVEPOINT — visible to this session).
        channel = (
            await db_session.execute(
                select(NotificationChannel).where(NotificationChannel.channel_identifier == "100")
            )
        ).scalar_one()
        assert channel.username == "alice"
        assert channel.channel_type == ChannelType.TELEGRAM.value

        # Reply fired with the DM welcome text.
        update.effective_message.reply_html.assert_awaited_once()
        reply_text = update.effective_message.reply_html.await_args.args[0]
        assert "Welcome to ETFPulse" in reply_text

    async def test_start_reactivates_unsubscribed_user(self, db_session, patch_session):
        """If a user previously /unsubscribed (is_active=false), /start flips
        them back on. Less surprising than demanding /subscribe."""
        await cmd_start(_dm_update(chat_id=200), _ctx())
        await cmd_unsubscribe(_dm_update(chat_id=200), _ctx())
        # After unsubscribe, is_active should be false.

        # Now /start again — should re-activate.
        await cmd_start(_dm_update(chat_id=200), _ctx())

        channel = (
            await db_session.execute(
                select(NotificationChannel).where(NotificationChannel.channel_identifier == "200")
            )
        ).scalar_one()
        user = await db_session.get(User, channel.user_id)
        assert user is not None
        assert user.is_active is True

    async def test_group_creates_telegram_group(self, db_session, patch_session):
        from etfpulse.models import TelegramGroup

        await cmd_start(_group_update(chat_id=-100500, title="Alpha"), _ctx())

        group = (
            await db_session.execute(select(TelegramGroup).where(TelegramGroup.chat_id == -100500))
        ).scalar_one()
        assert group.title == "Alpha"


# ---- /unsubscribe + /subscribe --------------------------------------------


class TestSubscribeUnsubscribe:
    async def test_unsubscribe_preserves_prefs(self, db_session, patch_session):
        """/unsubscribe is a soft-delete: is_active=false, but pref_assets etc.
        stay intact so /subscribe resumes where they left off."""
        await cmd_start(_dm_update(chat_id=300), _ctx())
        await cmd_prefs(_dm_update(chat_id=300), _ctx(["assets", "BTC"]))

        await cmd_unsubscribe(_dm_update(chat_id=300), _ctx())

        channel = (
            await db_session.execute(
                select(NotificationChannel).where(NotificationChannel.channel_identifier == "300")
            )
        ).scalar_one()
        user = await db_session.get(User, channel.user_id)
        assert user is not None
        assert user.is_active is False
        # Preferences preserved.
        assert user.pref_assets == ["BTC"]

    async def test_subscribe_after_unsubscribe_restores_active(self, db_session, patch_session):
        await cmd_start(_dm_update(chat_id=400), _ctx())
        await cmd_unsubscribe(_dm_update(chat_id=400), _ctx())
        await cmd_subscribe(_dm_update(chat_id=400), _ctx())

        channel = (
            await db_session.execute(
                select(NotificationChannel).where(NotificationChannel.channel_identifier == "400")
            )
        ).scalar_one()
        user = await db_session.get(User, channel.user_id)
        assert user is not None
        assert user.is_active is True


# ---- /prefs ---------------------------------------------------------------


class TestCmdPrefs:
    async def test_no_args_shows_current(self, db_session, patch_session):
        update = _dm_update(chat_id=500)
        await cmd_prefs(update, _ctx())

        reply_text = update.effective_message.reply_html.await_args.args[0]
        assert "Current preferences" in reply_text
        assert "BTC" in reply_text  # default assets include BTC

    async def test_assets_updates_pref_assets(self, db_session, patch_session):
        update = _dm_update(chat_id=600)
        await cmd_prefs(update, _ctx(["assets", "btc"]))

        channel = (
            await db_session.execute(
                select(NotificationChannel).where(NotificationChannel.channel_identifier == "600")
            )
        ).scalar_one()
        user = await db_session.get(User, channel.user_id)
        assert user is not None
        assert user.pref_assets == ["BTC"]

    async def test_confidence_updates_min(self, db_session, patch_session):
        update = _dm_update(chat_id=700)
        await cmd_prefs(update, _ctx(["confidence", "9"]))

        channel = (
            await db_session.execute(
                select(NotificationChannel).where(NotificationChannel.channel_identifier == "700")
            )
        ).scalar_one()
        user = await db_session.get(User, channel.user_id)
        assert user is not None
        assert user.pref_min_confidence == 9

    async def test_invalid_asset_replies_with_error(self, db_session, patch_session):
        """Reject unknown assets with a user-facing message, don't silently fail."""
        update = _dm_update(chat_id=800)
        await cmd_prefs(update, _ctx(["assets", "SOL"]))

        reply_text = update.effective_message.reply_html.await_args.args[0]
        assert "invalid asset" in reply_text.lower()

        # pref_assets was NOT modified.
        channel = (
            await db_session.execute(
                select(NotificationChannel).where(NotificationChannel.channel_identifier == "800")
            )
        ).scalar_one()
        user = await db_session.get(User, channel.user_id)
        assert user is not None
        # Still on defaults — env default is "BTC,ETH".
        assert "BTC" in user.pref_assets

    async def test_invalid_confidence_replies_with_error(self, db_session, patch_session):
        update = _dm_update(chat_id=900)
        await cmd_prefs(update, _ctx(["confidence", "99"]))

        reply_text = update.effective_message.reply_html.await_args.args[0]
        assert "must be 1-10" in reply_text

    async def test_unknown_subcommand_shows_usage(self, db_session, patch_session):
        update = _dm_update(chat_id=1000)
        await cmd_prefs(update, _ctx(["foo"]))

        reply_text = update.effective_message.reply_html.await_args.args[0]
        assert "Usage" in reply_text


# ---- /help + /track-record (no DB) ---------------------------------------


class TestStaticHandlers:
    async def test_help_lists_commands(self):
        update = _dm_update()
        await cmd_help(update, _ctx())

        reply_text = update.effective_message.reply_html.await_args.args[0]
        for cmd in [
            "/start",
            "/prefs",
            "/subscribe",
            "/unsubscribe",
            "/help",
            "/track-record",
            "/performance",
        ]:
            assert cmd in reply_text


# ---- /track-record + /performance ----------------------------------------


class TestCmdTrackRecord:
    async def test_empty_db_renders_no_outcomes_caption(self, db_session, patch_session):
        """Cold-boot — no SignalOutcome rows. Render the consistent
        "no outcomes evaluated yet" copy that matches the web HeroHitRatePanel
        + TrackRecord page empty states."""
        update = _dm_update()
        await cmd_track_record(update, _ctx())

        reply_text = update.effective_message.reply_html.await_args.args[0]
        assert "Signal track record" in reply_text
        assert "No outcomes evaluated yet" in reply_text
        assert "72h after a signal fires" in reply_text

    async def test_renders_summary_and_recent_with_seeded_outcomes(self, db_session, patch_session):
        """Seed 3 outcomes (2 target-hit, 1 stop-hit); assert hit rate + recent
        list render correctly with both ✅ and ❌ icons."""
        from datetime import UTC, date, datetime
        from decimal import Decimal

        from etfpulse.models import Signal, SignalOutcome
        from etfpulse.pipeline.detectors import compute_fingerprint

        # `outcomes_spec` covers the two non-pending verdicts the recent
        # list can render — `(hit_target, hit_stop)` per signal. Two
        # target-hits + one stop-hit exercises both ✅ and ❌ icons.
        outcomes_spec = [(True, False), (True, False), (False, True)]
        for i, (hit_target, hit_stop) in enumerate(outcomes_spec):
            signal = Signal(
                signal_type="flow_anomaly",
                asset="BTC",
                trigger_data={},
                ai_analysis={"suggested_action": "consider long", "headline": "x"},
                confidence=8,
                status="alerted",
                price_at_creation=Decimal("84200"),
                price_source="binance",
                ai_prompt_version="v3",
                fingerprint=compute_fingerprint("track-rec-bot", str(i)),
                signal_date=date(2026, 4, 25),
                entry_price=Decimal("84200"),
                stop_price=Decimal("82000"),
                target_price=Decimal("89500"),
            )
            db_session.add(signal)
            await db_session.flush()
            db_session.add(
                SignalOutcome(
                    signal_id=signal.id,
                    asset="BTC",
                    signal_type="flow_anomaly",
                    direction="long",
                    confidence=8,
                    entry_price=Decimal("84200"),
                    stop_price=Decimal("82000"),
                    target_price=Decimal("89500"),
                    price_at_signal=Decimal("84200"),
                    price_after_72h=Decimal("89600") if hit_target else Decimal("81900"),
                    hit_target=hit_target,
                    hit_stop=hit_stop,
                    evaluated_at=datetime.now(UTC),
                )
            )
        await db_session.flush()

        update = _dm_update()
        await cmd_track_record(update, _ctx())

        reply_text = update.effective_message.reply_html.await_args.args[0]
        # Summary block.
        assert "Total evaluated:</b> 3" in reply_text
        # 2/3 → 67%
        assert "Targets hit:</b> 2 (67%)" in reply_text
        # Recent list.
        assert "Last 3:" in reply_text
        # Each outcome rendered with the correct icon.
        assert "✅" in reply_text
        assert "❌" in reply_text

    async def test_recent_capped_at_five(self, db_session, patch_session):
        """Even with 8 evaluated outcomes, the bot lists the most recent 5."""
        from datetime import UTC, date, datetime, timedelta
        from decimal import Decimal

        from etfpulse.models import Signal, SignalOutcome
        from etfpulse.pipeline.detectors import compute_fingerprint

        now = datetime.now(UTC)
        for i in range(8):
            signal = Signal(
                signal_type="flow_anomaly",
                asset="BTC",
                trigger_data={},
                ai_analysis={"suggested_action": "consider long", "headline": "x"},
                confidence=7,
                status="alerted",
                price_at_creation=Decimal("84200"),
                price_source="binance",
                ai_prompt_version="v3",
                fingerprint=compute_fingerprint("track-rec-bot-cap", str(i)),
                signal_date=date(2026, 4, 25),
                target_price=Decimal("89500"),
            )
            db_session.add(signal)
            await db_session.flush()
            db_session.add(
                SignalOutcome(
                    signal_id=signal.id,
                    asset="BTC",
                    signal_type="flow_anomaly",
                    direction="long",
                    confidence=7,
                    target_price=Decimal("89500"),
                    price_at_signal=Decimal("84200"),
                    hit_target=True,
                    hit_stop=False,
                    evaluated_at=now - timedelta(hours=i),
                )
            )
        await db_session.flush()

        update = _dm_update()
        await cmd_track_record(update, _ctx())

        reply_text = update.effective_message.reply_html.await_args.args[0]
        assert "Last 5:" in reply_text
        # Total evaluated still reflects all 8.
        assert "Total evaluated:</b> 8" in reply_text

    async def test_performance_alias_dispatches_to_same_handler(self):
        """`/performance` is bound to the same callable — the alias and the
        primary command produce identical output. Pin this so a future
        rename doesn't silently desync them."""
        # Same function object — alias is `cmd_performance = cmd_track_record`.
        assert cmd_performance is cmd_track_record

    async def test_no_target_signals_render_with_pending_icon(self, db_session, patch_session):
        """Outcomes where AI didn't set a target render with ⏳ — distinct
        from "neither hit" (which has a target but didn't reach it)."""
        from datetime import UTC, date, datetime
        from decimal import Decimal

        from etfpulse.models import Signal, SignalOutcome
        from etfpulse.pipeline.detectors import compute_fingerprint

        signal = Signal(
            signal_type="flow_anomaly",
            asset="ETH",
            trigger_data={},
            ai_analysis={"suggested_action": "consider long", "headline": "x"},
            confidence=4,
            status="alerted",
            price_at_creation=Decimal("2480"),
            price_source="binance",
            ai_prompt_version="v3",
            fingerprint=compute_fingerprint("no-target-bot"),
            signal_date=date(2026, 4, 25),
        )
        db_session.add(signal)
        await db_session.flush()
        db_session.add(
            SignalOutcome(
                signal_id=signal.id,
                asset="ETH",
                signal_type="flow_anomaly",
                direction="long",
                confidence=4,
                target_price=None,  # AI declined a target
                price_at_signal=Decimal("2480"),
                hit_target=None,
                hit_stop=False,
                evaluated_at=datetime.now(UTC),
            )
        )
        await db_session.flush()

        update = _dm_update()
        await cmd_track_record(update, _ctx())

        reply_text = update.effective_message.reply_html.await_args.args[0]
        # No-target signals appear in the recent list with the ⏳ icon
        # rather than the ✅/❌ verdicts.
        assert "⏳" in reply_text
        assert "no target set" in reply_text
        # Path 3 of `_format_track_record_message` — outcomes exist but
        # none had a target → list-only with the "not yet computable" caption.
        # Beats the cold-boot copy when we DO have data; beats rendering "0%".
        assert "Hit rate not yet computable" in reply_text
        # Sanity — the cold-boot caption MUST NOT fire here.
        assert "No outcomes evaluated yet" not in reply_text
