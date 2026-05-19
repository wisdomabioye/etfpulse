"""Pin `User.wallet_address` constraints (PR C.6 step 2).

The wallet_address column is the single source of truth for "which EVM
wallet does this user sign SoDEX orders from." Three DB invariants make
that load-bearing:

1. NULL is allowed (Telegram-only / dashboard-only users are valid).
2. Lowercased 40-hex format only (`ck_users_wallet_address_format`).
3. Globally unique across non-NULL values (`ix_users_wallet_address`).

A future migration that drops any of these would silently break the
SoDEX flow — this file pins the runtime contract.
"""

from __future__ import annotations

import pytest
from sqlalchemy.exc import IntegrityError

from etfpulse.models import User


class TestWalletAddressFormat:
    """`ck_users_wallet_address_format` — `^0x[0-9a-f]{40}$` or NULL."""

    async def test_null_is_allowed(self, db_session):
        """Most existing users have NULL — must not be rejected."""
        user = User()
        db_session.add(user)
        await db_session.flush()
        assert user.wallet_address is None

    async def test_valid_lowercase_address_accepted(self, db_session):
        user = User(wallet_address="0x" + "ab" * 20)
        db_session.add(user)
        await db_session.flush()
        assert user.wallet_address == "0x" + "ab" * 20

    @pytest.mark.parametrize(
        "bad_address",
        [
            "0x" + "AB" * 20,  # uppercase rejected — write-side must normalise
            "0x" + "a" * 39,  # too short (column type permits 0-42 chars, CHECK enforces exact 42)
            "ab" * 20 + "ab",  # missing 0x prefix
            "0X" + "ab" * 20,  # uppercase prefix
            "0x" + "g" * 40,  # non-hex char
            "",  # empty string (NULL would pass, "" is not the same)
            # NOTE: a too-long string (e.g. `0x` + 41 hex) is rejected by the
            # VARCHAR(42) column type BEFORE the CHECK can run. Column-type
            # rejection is a valid safety net but distinct from CHECK
            # enforcement; we test the CHECK's format predicate here.
        ],
    )
    async def test_invalid_format_rejected(self, db_session, bad_address):
        user = User(wallet_address=bad_address)
        db_session.add(user)
        with pytest.raises(IntegrityError):
            await db_session.flush()
        await db_session.rollback()


class TestWalletAddressUniqueness:
    """`ix_users_wallet_address` is partial UNIQUE WHERE NOT NULL."""

    async def test_two_users_with_distinct_wallets(self, db_session):
        u1 = User(wallet_address="0x" + "11" * 20)
        u2 = User(wallet_address="0x" + "22" * 20)
        db_session.add_all([u1, u2])
        await db_session.flush()
        assert u1.id != u2.id

    async def test_duplicate_wallet_rejected(self, db_session):
        shared = "0x" + "33" * 20
        db_session.add(User(wallet_address=shared))
        await db_session.flush()
        db_session.add(User(wallet_address=shared))
        with pytest.raises(IntegrityError):
            await db_session.flush()
        await db_session.rollback()

    async def test_multiple_null_wallets_allowed(self, db_session):
        """Many users may have NULL — partial index excludes NULLs from the
        uniqueness check. Without this, every Telegram-only user past the
        first would fail to register."""
        users = [User() for _ in range(5)]
        db_session.add_all(users)
        await db_session.flush()
        assert all(u.id is not None for u in users)
