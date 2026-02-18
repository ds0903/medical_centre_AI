import logging
from contextlib import asynccontextmanager

from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine, async_sessionmaker

from config import DATABASE_URL
from db.models import Base

logger = logging.getLogger(__name__)

engine = create_async_engine(
    DATABASE_URL,
    echo=False,
    pool_size=10,
    max_overflow=20,
)

AsyncSessionLocal = async_sessionmaker(
    engine,
    class_=AsyncSession,
    expire_on_commit=False,
)


async def init_db() -> None:
    """Створити всі таблиці якщо не існують."""
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    logger.info("БД ініціалізовано")


async def close_db() -> None:
    """Закрити з'єднання з БД."""
    await engine.dispose()
    logger.info("З'єднання з БД закрито")


@asynccontextmanager
async def get_session():
    """Context manager для сесії БД."""
    async with AsyncSessionLocal() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
