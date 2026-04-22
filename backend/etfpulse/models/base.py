from sqlalchemy import MetaData
from sqlalchemy.orm import DeclarativeBase

# Naming convention auto-generates names for FK / unique / PK / indexed
# constraints we never name explicitly. CheckConstraint is intentionally absent:
# SQLAlchemy applies the convention pattern *on top of* any explicit `name=`,
# producing doubled names like `ck_signals_ck_signals_confidence_range`. By
# omitting `ck`, we get "what you write is what's in the DB" semantics for
# CHECK constraints — every CheckConstraint MUST pass an explicit `name=`.
_NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=_NAMING_CONVENTION)
