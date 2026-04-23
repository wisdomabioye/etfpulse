"""Tests for `bot/handlers/_common.py` — the core logic that handlers wrap.

80% of the handler package's behaviour is here; handler-level tests in
test_handlers.py are mostly thin smoke coverage over these primitives.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from sqlalchemy import select
from telegram import Chat

from etfpulse.bot.handlers._common import (
    get_or_create_target,
    parse_asset_list,
    parse_confidence,
)
from etfpulse.config import settings
from etfpulse.models import ChannelType, NotificationChannel, TelegramGroup, User


def _dm_update(chat_id: int = 42, username: str = "alice") -> MagicMock:
    """Mock Update for a direct message from `@username` with `chat.id=chat_id`."""
    update = MagicMock()
    update.effective_chat.id = chat_id
    update.effective_chat.type = Chat.PRIVATE
    update.effective_chat.title = None
    update.effective_user.id = chat_id  # for DMs, user_id == chat_id
    update.effective_user.username = username
    return update


def _group_update(chat_id: int = -100, title: str = "Test Group") -> MagicMock:
    """Mock Update for a group message."""
    update = MagicMock()
    update.effective_chat.id = chat_id
    update.effective_chat.type = Chat.GROUP
    update.effective_chat.title = title
    update.effective_user.id = 999
    update.effective_user.username = "admin"
    return update


# ---- get_or_create_target — DM flow ---------------------------------------


class TestGetOrCreateTargetDM:
    async def test_creates_user_and_channel(self, db_session):
        target = await get_or_create_target(db_session, _dm_update(chat_id=42, username="alice"))
        await db_session.flush()

        assert target.kind == "user"
        assert isinstance(target.obj, User)

        # Both rows landed in the DB.
        channel = (
            await db_session.execute(
                select(NotificationChannel).where(
                    NotificationChannel.channel_type == ChannelType.TELEGRAM.value,
                    NotificationChannel.channel_identifier == "42",
                )
            )
        ).scalar_one()
        assert channel.user_id == target.obj.id
        assert channel.username == "alice"

    async def test_applies_env_defaults(self, db_session, monkeypatch):
        """New User on /start inherits settings.delivery_default_*, not the
        model-level defaults. This is why #52 added those fields."""
        monkeypatch.setattr(settings, "delivery_default_min_confidence", 8)
        monkeypatch.setattr(settings, "delivery_default_assets", "BTC")

        target = await get_or_create_target(db_session, _dm_update(chat_id=77))
        await db_session.flush()

        assert target.obj.pref_min_confidence == 8
        assert target.obj.pref_assets == ["BTC"]

    async def test_idempotent_returns_same_user(self, db_session):
        """Second call with the same chat_id returns the EXISTING User row —
        no IntegrityError, no duplicate."""
        first = await get_or_create_target(db_session, _dm_update(chat_id=42))
        await db_session.flush()
        second = await get_or_create_target(db_session, _dm_update(chat_id=42))
        await db_session.flush()

        assert first.obj.id == second.obj.id

        # Only one channel row exists.
        count = len(
            (
                await db_session.execute(
                    select(NotificationChannel).where(
                        NotificationChannel.channel_identifier == "42"
                    )
                )
            )
            .scalars()
            .all()
        )
        assert count == 1


# ---- get_or_create_target — group flow ------------------------------------


class TestGetOrCreateTargetGroup:
    async def test_creates_telegram_group(self, db_session):
        target = await get_or_create_target(
            db_session, _group_update(chat_id=-100200, title="Alpha Signals")
        )
        await db_session.flush()

        assert target.kind == "group"
        assert isinstance(target.obj, TelegramGroup)
        assert target.obj.chat_id == -100200
        assert target.obj.title == "Alpha Signals"

    async def test_applies_env_defaults(self, db_session, monkeypatch):
        monkeypatch.setattr(settings, "delivery_default_min_confidence", 7)
        monkeypatch.setattr(settings, "delivery_default_assets", "ETH")

        target = await get_or_create_target(db_session, _group_update(chat_id=-100300))
        await db_session.flush()

        assert target.obj.pref_min_confidence == 7
        assert target.obj.pref_assets == ["ETH"]

    async def test_idempotent_returns_same_group(self, db_session):
        first = await get_or_create_target(db_session, _group_update(chat_id=-100400))
        await db_session.flush()
        second = await get_or_create_target(db_session, _group_update(chat_id=-100400))
        await db_session.flush()

        assert first.obj.id == second.obj.id


# ---- parse_asset_list -----------------------------------------------------


class TestParseAssetList:
    def test_single_asset(self):
        assert parse_asset_list("BTC") == ["BTC"]

    def test_multiple_assets(self):
        assert parse_asset_list("BTC,ETH") == ["BTC", "ETH"]

    def test_lowercase_input_uppercased(self):
        assert parse_asset_list("btc,eth") == ["BTC", "ETH"]

    def test_whitespace_stripped(self):
        assert parse_asset_list(" BTC , ETH ") == ["BTC", "ETH"]

    def test_dedup_preserves_order(self):
        assert parse_asset_list("BTC,ETH,BTC") == ["BTC", "ETH"]

    def test_invalid_asset_raises(self):
        with pytest.raises(ValueError, match="invalid asset"):
            parse_asset_list("BTC,SOL")

    def test_all_invalid_raises(self):
        with pytest.raises(ValueError, match="invalid asset"):
            parse_asset_list("SOL,XRP")

    def test_empty_raises(self):
        with pytest.raises(ValueError, match="no assets"):
            parse_asset_list("")

    def test_whitespace_only_raises(self):
        with pytest.raises(ValueError, match="no assets"):
            parse_asset_list("  ,  ,")


# ---- parse_confidence -----------------------------------------------------


class TestParseConfidence:
    @pytest.mark.parametrize("raw,expected", [("1", 1), ("7", 7), ("10", 10)])
    def test_valid(self, raw, expected):
        assert parse_confidence(raw) == expected

    @pytest.mark.parametrize("raw", ["0", "11", "-1", "100"])
    def test_out_of_range_raises(self, raw):
        with pytest.raises(ValueError, match="must be 1-10"):
            parse_confidence(raw)

    @pytest.mark.parametrize("raw", ["abc", "7.5", ""])
    def test_not_integer_raises(self, raw):
        with pytest.raises(ValueError, match="must be an integer"):
            parse_confidence(raw)
