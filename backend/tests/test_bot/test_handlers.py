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
    "etfpulse.bot.handlers.membership",
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


def _dm_update(
    chat_id: int = 42, username: str = "alice", language_code: str | None = None
) -> MagicMock:
    update = MagicMock()
    update.effective_chat.id = chat_id
    update.effective_chat.type = Chat.PRIVATE
    update.effective_chat.title = None
    update.effective_user.id = chat_id
    update.effective_user.username = username
    # Pin explicitly so MagicMock's default-truthy attr doesn't leak
    # through `resolve_lang` (i18n) as a non-string sentinel.
    update.effective_user.language_code = language_code
    update.effective_message.reply_html = AsyncMock()
    return update


def _group_update(
    chat_id: int = -100, title: str = "Test Group", language_code: str | None = None
) -> MagicMock:
    update = MagicMock()
    update.effective_chat.id = chat_id
    update.effective_chat.type = Chat.GROUP
    update.effective_chat.title = title
    update.effective_user.id = 999
    update.effective_user.username = "admin"
    update.effective_user.language_code = language_code
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

    async def test_dm_welcome_embeds_command_list(self, db_session, patch_session):
        """Regression guard. `welcome.dm` interpolates `{command_list}` so
        `/start` and `/help` derive from the same registry. If someone
        deletes the placeholder by accident, `str.format(**kwargs)` would
        silently drop the kwarg and `/start` would lose its bullets —
        none of the existing welcome tests would notice. Pin every
        advertised command to appear in the rendered welcome."""
        update = _dm_update(chat_id=900)
        await cmd_start(update, _ctx())

        reply_text = update.effective_message.reply_html.await_args.args[0]
        for cmd in ["/start", "/prefs", "/subscribe", "/unsubscribe", "/performance", "/help"]:
            assert cmd in reply_text, f"DM welcome must embed {cmd} from the command list"

    async def test_group_welcome_embeds_command_list(self, db_session, patch_session):
        """Same guard for the group welcome path (both /start in a group
        AND the my_chat_member auto-welcome reuse `welcome.group`)."""
        update = _group_update(chat_id=-100900, title="Beta")
        await cmd_start(update, _ctx())

        reply_text = update.effective_message.reply_html.await_args.args[0]
        for cmd in ["/start", "/prefs", "/subscribe", "/unsubscribe", "/performance", "/help"]:
            assert cmd in reply_text, f"group welcome must embed {cmd}"

    async def test_dm_welcome_translated_when_language_code_set(self, db_session, patch_session):
        """Issue #37 — /start respects `effective_user.language_code`.
        A Spanish-locale client should see the es welcome string."""
        update = _dm_update(chat_id=150, username="maria", language_code="es-MX")
        await cmd_start(update, _ctx())

        reply_text = update.effective_message.reply_html.await_args.args[0]
        # Spanish-specific token from the welcome.dm translation.
        assert "Bienvenido a ETFPulse" in reply_text
        # And NOT the English version.
        assert "Welcome to ETFPulse" not in reply_text

    async def test_dm_welcome_unknown_language_falls_back_to_english(
        self, db_session, patch_session
    ):
        update = _dm_update(chat_id=151, language_code="ja")
        await cmd_start(update, _ctx())
        reply_text = update.effective_message.reply_html.await_args.args[0]
        assert "Welcome to ETFPulse" in reply_text


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


# ---- /prefs in groups — admin gate (issue #39) ----------------------------


def _group_ctx(args: list[str], *, member_status: str | None) -> MagicMock:
    """Build a ContextTypes-like mock whose `bot.get_chat_member` resolves
    to a ChatMember with the given status. `member_status=None` makes the
    API call raise — exercises the fail-closed branch."""
    from telegram.error import TelegramError

    ctx = MagicMock()
    ctx.args = args
    if member_status is None:
        ctx.bot.get_chat_member = AsyncMock(side_effect=TelegramError("api down"))
    else:
        member = MagicMock()
        member.status = member_status
        ctx.bot.get_chat_member = AsyncMock(return_value=member)
    return ctx


class TestPrefsGroupAdminGate:
    async def test_group_admin_can_change(self, db_session, patch_session):
        """Admin (creator/administrator) status → mutation goes through."""
        from telegram import ChatMember

        from etfpulse.models import TelegramGroup

        upd = _group_update(chat_id=-200100, title="AdminTest")
        await cmd_prefs(
            upd,
            _group_ctx(["confidence", "8"], member_status=ChatMember.ADMINISTRATOR),
        )

        group = (
            await db_session.execute(select(TelegramGroup).where(TelegramGroup.chat_id == -200100))
        ).scalar_one()
        assert group.pref_min_confidence == 8
        # No denial message — last reply should be the success confirmation.
        reply_text = upd.effective_message.reply_html.await_args.args[0]
        assert "Updated" in reply_text

    async def test_group_owner_can_change(self, db_session, patch_session):
        """Creator (= telegram.ChatMember.OWNER) is also admin."""
        from telegram import ChatMember

        from etfpulse.models import TelegramGroup

        upd = _group_update(chat_id=-200200)
        await cmd_prefs(
            upd,
            _group_ctx(["assets", "BTC"], member_status=ChatMember.OWNER),
        )

        group = (
            await db_session.execute(select(TelegramGroup).where(TelegramGroup.chat_id == -200200))
        ).scalar_one()
        assert group.pref_assets == ["BTC"]

    async def test_group_member_cannot_change(self, db_session, patch_session):
        """Regular member → denial message + prefs untouched + NO group row
        created (the deny-before-DB check prevents side effects)."""
        from telegram import ChatMember

        from etfpulse.models import TelegramGroup

        upd = _group_update(chat_id=-200300)
        await cmd_prefs(
            upd,
            _group_ctx(["confidence", "8"], member_status=ChatMember.MEMBER),
        )

        reply_text = upd.effective_message.reply_html.await_args.args[0]
        assert "Only group admins" in reply_text

        # No TelegramGroup row was created — gate ran before get_or_create.
        n = (
            await db_session.execute(select(TelegramGroup).where(TelegramGroup.chat_id == -200300))
        ).scalar_one_or_none()
        assert n is None

    async def test_group_restricted_cannot_change(self, db_session, patch_session):
        """RESTRICTED members aren't admins — same denial path as regular MEMBER."""
        from telegram import ChatMember

        upd = _group_update(chat_id=-200400)
        await cmd_prefs(
            upd,
            _group_ctx(["assets", "ETH"], member_status=ChatMember.RESTRICTED),
        )
        reply_text = upd.effective_message.reply_html.await_args.args[0]
        assert "Only group admins" in reply_text

    async def test_group_view_allowed_for_anyone(self, db_session, patch_session):
        """No-args /prefs in a group is a READ — anyone can run it. The admin
        check short-circuits because args is empty; no get_chat_member call."""
        from telegram import ChatMember

        upd = _group_update(chat_id=-200500)
        ctx = _group_ctx([], member_status=ChatMember.MEMBER)
        await cmd_prefs(upd, ctx)

        reply_text = upd.effective_message.reply_html.await_args.args[0]
        assert "Current preferences" in reply_text
        # get_chat_member shouldn't even have been called for a read.
        ctx.bot.get_chat_member.assert_not_called()

    async def test_admin_api_failure_fails_closed(self, db_session, patch_session):
        """If get_chat_member raises (network blip, bot kicked mid-call),
        treat as non-admin. Safer than letting the mutation through on
        an unverifiable identity."""
        from etfpulse.models import TelegramGroup

        upd = _group_update(chat_id=-200600)
        await cmd_prefs(
            upd,
            _group_ctx(["confidence", "9"], member_status=None),
        )
        reply_text = upd.effective_message.reply_html.await_args.args[0]
        assert "Only group admins" in reply_text
        # Confirm no row was created either.
        n = (
            await db_session.execute(select(TelegramGroup).where(TelegramGroup.chat_id == -200600))
        ).scalar_one_or_none()
        assert n is None


# ---- /help + /track-record (no DB) ---------------------------------------


class TestStaticHandlers:
    async def test_help_lists_advertised_commands(self):
        """Renders every advertised command from `bot/commands.py:COMMAND_SPECS`.

        Pin the advertised set explicitly so a removal from the registry that
        nobody intended (e.g., an accidental delete during refactor) trips
        this test. Aliases like `/track_record` MUST NOT appear — they're
        unadvertised on purpose.
        """
        update = _dm_update()
        await cmd_help(update, _ctx())

        reply_text = update.effective_message.reply_html.await_args.args[0]
        for cmd in [
            "/start",
            "/prefs",
            "/subscribe",
            "/unsubscribe",
            "/performance",
            "/help",
        ]:
            assert cmd in reply_text, f"/help must advertise {cmd}"
        # Hyphen form is unreachable in Telegram bot commands — must NEVER
        # appear in user-facing copy. Pin to catch the bug we just fixed.
        assert "/track-record" not in reply_text
        # `/track_record` is an unadvertised alias — keep it out of /help.
        assert "/track_record" not in reply_text

    async def test_help_renders_in_spanish(self):
        update = _dm_update(language_code="es-MX")
        await cmd_help(update, _ctx())

        reply_text = update.effective_message.reply_html.await_args.args[0]
        # Spanish header from the `help.header` i18n key.
        assert "Comandos de ETFPulse" in reply_text
        # Spanish command description from `cmd.performance.desc.es`.
        assert "historial de rendimiento" in reply_text
        # Confirms English is suppressed, not just appended.
        assert "ETFPulse commands" not in reply_text


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

    async def test_footer_uses_frontend_url_not_telegram_public_url(
        self, db_session, patch_session, monkeypatch
    ):
        """The 'Full record:' link must point to the FRONTEND SPA, not the
        bot's webhook host. They are typically separate domains (Vercel +
        Coolify); pre-fix the footer pointed at the backend, where the
        /track-record route doesn't exist (404).

        Seed one outcome so the render path passes the cold-boot guard and
        actually reaches the footer.
        """
        from datetime import UTC, date, datetime
        from decimal import Decimal

        from etfpulse.config import settings
        from etfpulse.models import Signal, SignalOutcome
        from etfpulse.pipeline.detectors import compute_fingerprint

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
            fingerprint=compute_fingerprint("footer-url-test"),
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
                price_after_72h=Decimal("89600"),
                hit_target=True,
                hit_stop=False,
                evaluated_at=datetime.now(UTC),
            )
        )
        await db_session.flush()

        # Distinct domains so we can prove the bot picks the right one.
        monkeypatch.setattr(settings, "frontend_url", "https://app.example.com")
        monkeypatch.setattr(settings, "telegram_public_url", "https://api.example.com")

        update = _dm_update()
        await cmd_track_record(update, _ctx())

        reply_text = update.effective_message.reply_html.await_args.args[0]
        assert "Full record:" in reply_text
        assert "https://app.example.com/track-record" in reply_text
        # The backend host MUST NOT appear in the user-facing track-record
        # link — that was the bug.
        assert "api.example.com" not in reply_text

    async def test_footer_skipped_when_frontend_url_empty(
        self, db_session, patch_session, monkeypatch
    ):
        """Dev / mis-configured prod: empty `frontend_url` must skip the
        footer cleanly rather than rendering `https:///track-record`."""
        from datetime import UTC, date, datetime
        from decimal import Decimal

        from etfpulse.config import settings
        from etfpulse.models import Signal, SignalOutcome
        from etfpulse.pipeline.detectors import compute_fingerprint

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
            fingerprint=compute_fingerprint("footer-empty-url-test"),
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
                price_after_72h=Decimal("89600"),
                hit_target=True,
                hit_stop=False,
                evaluated_at=datetime.now(UTC),
            )
        )
        await db_session.flush()

        monkeypatch.setattr(settings, "frontend_url", "")

        update = _dm_update()
        await cmd_track_record(update, _ctx())

        reply_text = update.effective_message.reply_html.await_args.args[0]
        assert "Full record:" not in reply_text
        # Defensive: no orphan-protocol artefacts from a previous bug.
        assert "https:///" not in reply_text

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


# ---- my_chat_member (issue #35) -------------------------------------------


def _membership_update(
    *,
    chat_id: int = -100600,
    chat_type: str = Chat.SUPERGROUP,
    title: str = "Alpha Traders",
    old_status: str,
    new_status: str,
    language_code: str | None = None,
) -> MagicMock:
    """Build a fake my_chat_member Update.

    `effective_chat` reads from update.my_chat_member.chat for membership
    updates; we mirror them so the handler picks up the same chat both ways."""
    update = MagicMock()
    update.effective_chat.id = chat_id
    update.effective_chat.type = chat_type
    update.effective_chat.title = title
    update.effective_user = MagicMock()
    update.effective_user.language_code = language_code
    update.effective_message.reply_html = AsyncMock()

    event = MagicMock()
    event.chat.id = chat_id
    event.chat.type = chat_type
    event.chat.title = title
    event.old_chat_member.status = old_status
    event.new_chat_member.status = new_status
    update.my_chat_member = event
    return update


class TestMyChatMember:
    async def test_bot_added_creates_group(self, db_session, patch_session):
        from telegram import ChatMember

        from etfpulse.bot.handlers.membership import handle_my_chat_member
        from etfpulse.models import TelegramGroup

        upd = _membership_update(
            chat_id=-100700,
            title="Alpha Traders",
            old_status=ChatMember.LEFT,
            new_status=ChatMember.MEMBER,
        )
        await handle_my_chat_member(upd, _ctx())

        group = (
            await db_session.execute(select(TelegramGroup).where(TelegramGroup.chat_id == -100700))
        ).scalar_one()
        assert group.title == "Alpha Traders"
        assert group.is_active is True
        upd.effective_message.reply_html.assert_awaited_once()
        body = upd.effective_message.reply_html.await_args.args[0]
        assert "ETFPulse is now monitoring this group" in body

    async def test_bot_removed_soft_deletes_group(self, db_session, patch_session):
        from telegram import ChatMember

        from etfpulse.bot.handlers.membership import handle_my_chat_member
        from etfpulse.models import TelegramGroup

        # First add → registers.
        await handle_my_chat_member(
            _membership_update(
                chat_id=-100800,
                old_status=ChatMember.LEFT,
                new_status=ChatMember.MEMBER,
            ),
            _ctx(),
        )
        # Then remove.
        await handle_my_chat_member(
            _membership_update(
                chat_id=-100800,
                old_status=ChatMember.MEMBER,
                new_status=ChatMember.LEFT,
            ),
            _ctx(),
        )

        group = (
            await db_session.execute(select(TelegramGroup).where(TelegramGroup.chat_id == -100800))
        ).scalar_one()
        assert group.is_active is False

    async def test_readd_reactivates_existing_group(self, db_session, patch_session):
        """Same chat removed then re-added — preserves prefs, flips is_active back on."""
        from telegram import ChatMember

        from etfpulse.bot.handlers.membership import handle_my_chat_member
        from etfpulse.models import TelegramGroup

        await handle_my_chat_member(
            _membership_update(
                chat_id=-100900,
                title="Original",
                old_status=ChatMember.LEFT,
                new_status=ChatMember.MEMBER,
            ),
            _ctx(),
        )
        # Mutate prefs on this group so we can verify they survive the
        # remove → re-add cycle (soft-delete must preserve user config).
        seeded = (
            await db_session.execute(select(TelegramGroup).where(TelegramGroup.chat_id == -100900))
        ).scalar_one()
        seeded.pref_min_confidence = 9
        seeded.pref_assets = ["BTC"]
        await db_session.flush()

        await handle_my_chat_member(
            _membership_update(
                chat_id=-100900,
                old_status=ChatMember.MEMBER,
                new_status=ChatMember.BANNED,
            ),
            _ctx(),
        )
        # Re-add with a new title.
        await handle_my_chat_member(
            _membership_update(
                chat_id=-100900,
                title="Renamed",
                old_status=ChatMember.BANNED,
                new_status=ChatMember.MEMBER,
            ),
            _ctx(),
        )

        group = (
            await db_session.execute(select(TelegramGroup).where(TelegramGroup.chat_id == -100900))
        ).scalar_one()
        assert group.is_active is True
        assert group.title == "Renamed"  # title refreshed on re-add
        # Prefs from before removal must still be intact — that's the
        # whole point of soft-delete vs hard-delete on bot removal.
        assert group.pref_min_confidence == 9
        assert group.pref_assets == ["BTC"]

    async def test_promotion_is_noop(self, db_session, patch_session):
        """member → administrator should not create a new row or touch state."""
        from telegram import ChatMember

        from etfpulse.bot.handlers.membership import handle_my_chat_member
        from etfpulse.models import TelegramGroup

        # Seed via initial add.
        await handle_my_chat_member(
            _membership_update(
                chat_id=-101000,
                old_status=ChatMember.LEFT,
                new_status=ChatMember.MEMBER,
            ),
            _ctx(),
        )
        before = (
            await db_session.execute(select(TelegramGroup).where(TelegramGroup.chat_id == -101000))
        ).scalar_one()
        before_updated_at = before.updated_at

        # Promotion.
        upd = _membership_update(
            chat_id=-101000,
            old_status=ChatMember.MEMBER,
            new_status=ChatMember.ADMINISTRATOR,
        )
        await handle_my_chat_member(upd, _ctx())

        # No reply, no DB change.
        upd.effective_message.reply_html.assert_not_called()
        await db_session.refresh(before)
        assert before.updated_at == before_updated_at

    async def test_dm_ignored(self, db_session, patch_session):
        """my_chat_member fires for DMs too (e.g. the user blocks the bot).
        Handler should no-op — DM state lives on User.is_active via /start."""
        from telegram import ChatMember

        from etfpulse.bot.handlers.membership import handle_my_chat_member
        from etfpulse.models import TelegramGroup

        upd = _membership_update(
            chat_id=42,
            chat_type=Chat.PRIVATE,
            title=None,
            old_status=ChatMember.MEMBER,
            new_status=ChatMember.BANNED,
        )
        await handle_my_chat_member(upd, _ctx())

        # No row created.
        n = (
            await db_session.execute(select(TelegramGroup).where(TelegramGroup.chat_id == 42))
        ).scalar_one_or_none()
        assert n is None
        upd.effective_message.reply_html.assert_not_called()

    async def test_remove_unknown_group_is_noop(self, db_session, patch_session):
        """Bot removed from a group we never registered (pre-handler legacy).
        Should log + return cleanly, not error."""
        from telegram import ChatMember

        from etfpulse.bot.handlers.membership import handle_my_chat_member

        upd = _membership_update(
            chat_id=-999999,
            old_status=ChatMember.MEMBER,
            new_status=ChatMember.LEFT,
        )
        await handle_my_chat_member(upd, _ctx())
        # Just shouldn't raise.

    # NOTE on concurrency: `_on_added` catches IntegrityError on the
    # chat_id UNIQUE constraint and re-resolves, mirroring the same
    # pattern in `_common.py:_resolve_or_create_group`. Neither path is
    # unit-tested for the IntegrityError branch because the SAVEPOINT
    # fixture (`patch_session`) can't compose with a `commit + rollback`
    # cycle inside the handler — the rollback unwinds the outer test
    # transaction. The code is short, parallel to the well-worn
    # `_resolve_or_create_group` shape, and the same race in production
    # exercises it.

    async def test_added_welcome_translated(self, db_session, patch_session):
        """Issue #37 — membership welcome reuses the `welcome.group` key
        from i18n so /start and my_chat_member registration speak the
        same language. Verifies the DRY-fix landed and works end-to-end."""
        from telegram import ChatMember

        from etfpulse.bot.handlers.membership import handle_my_chat_member

        upd = _membership_update(
            chat_id=-101300,
            title="Grupo Cripto",
            old_status=ChatMember.LEFT,
            new_status=ChatMember.MEMBER,
            language_code="es",
        )
        await handle_my_chat_member(upd, _ctx())

        body = upd.effective_message.reply_html.await_args.args[0]
        # Spanish-specific token from welcome.group.
        assert "ahora monitoriza este grupo" in body

    async def test_welcome_send_failure_does_not_undo_registration(self, db_session, patch_session):
        """If reply_html raises (no send permission), the group is still
        registered — commit happens before the reply."""
        from telegram import ChatMember
        from telegram.error import TelegramError

        from etfpulse.bot.handlers.membership import handle_my_chat_member
        from etfpulse.models import TelegramGroup

        upd = _membership_update(
            chat_id=-101100,
            old_status=ChatMember.LEFT,
            new_status=ChatMember.MEMBER,
        )
        upd.effective_message.reply_html = AsyncMock(side_effect=TelegramError("no permission"))

        await handle_my_chat_member(upd, _ctx())

        group = (
            await db_session.execute(select(TelegramGroup).where(TelegramGroup.chat_id == -101100))
        ).scalar_one()
        assert group.is_active is True
