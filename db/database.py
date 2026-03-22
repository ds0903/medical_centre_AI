import asyncio
import logging
import sys
from contextlib import asynccontextmanager

import asyncpg
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine, async_sessionmaker

from config import DATABASE_URL
from db.models import Base

logger = logging.getLogger(__name__)

engine = create_async_engine(
    DATABASE_URL,
    echo=False,
    pool_size=10,
    max_overflow=20,
    pool_pre_ping=True,   # перевіряє з'єднання перед використанням → стійко до idle drops
    pool_recycle=1800,    # перевикористовує з'єднання не довше 30хв → запобігає "connection is closed"
)

AsyncSessionLocal = async_sessionmaker(
    engine,
    class_=AsyncSession,
    expire_on_commit=False,
)


async def _ensure_database_exists() -> None:
    """Створити базу даних якщо не існує."""
    url = make_url(DATABASE_URL)
    db_name = url.database

    conn = await asyncpg.connect(
        user=url.username,
        password=url.password,
        host=url.host or "localhost",
        port=url.port or 5432,
        database="postgres",
    )
    try:
        exists = await conn.fetchval(
            "SELECT 1 FROM pg_database WHERE datname = $1", db_name
        )
        if not exists:
            await conn.execute(f'CREATE DATABASE "{db_name}"')
            logger.info(f"База даних '{db_name}' створена")
        else:
            logger.debug(f"База даних '{db_name}' вже існує")
    finally:
        await conn.close()


async def init_db() -> None:
    """Створити БД і всі таблиці якщо не існують. Додати відсутні колонки."""
    await _ensure_database_exists()
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        await _migrate(conn)
    logger.info("БД ініціалізовано")


async def _migrate(conn) -> None:
    """Додати відсутні колонки та оновити enum типи."""
    migrations = [
        "ALTER TABLE call_tasks ADD COLUMN IF NOT EXISTS message VARCHAR",
        "ALTER TABLE call_tasks ADD COLUMN IF NOT EXISTS error_message VARCHAR",
        "ALTER TABLE call_tasks ADD COLUMN IF NOT EXISTS operator_note VARCHAR",
    ]
    for sql in migrations:
        await conn.execute(text(sql))

    # Додати rescheduled до callresult enum якщо відсутній
    await conn.execute(text(
        "ALTER TYPE callresult ADD VALUE IF NOT EXISTS 'rescheduled'"
    ))


async def close_db() -> None:
    """Закрити з'єднання з БД."""
    await engine.dispose()
    logger.info("З'єднання з БД закрито")


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        handlers=[logging.StreamHandler(sys.stdout)],
    )

    async def _run() -> None:
        await init_db()
        await close_db()

    asyncio.run(_run())


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
