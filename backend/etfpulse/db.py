from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from etfpulse.config import settings

engine = create_async_engine(
    settings.database_url,
    echo=(not settings.is_production),
    pool_size=10,
    max_overflow=20,
)

async_session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


async def get_session():
    """FastAPI dependency for DB sessions."""
    async with async_session() as session:
        yield session
