import logging
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

    # Оновити callresult enum — прибрати rescheduled, transferred, error
    await conn.execute(text("""
        DO $$
        BEGIN
            IF EXISTS (
                SELECT 1 FROM pg_enum e
                JOIN pg_type t ON e.enumtypid = t.oid
                WHERE t.typname = 'callresult'
                AND e.enumlabel IN ('rescheduled', 'transferred', 'error')
            ) THEN
                ALTER TABLE call_tasks ALTER COLUMN result TYPE text USING result::text;
                ALTER TABLE call_history ALTER COLUMN result TYPE text USING result::text;
                DROP TYPE callresult;
                UPDATE call_tasks SET result = 'no_result'
                    WHERE result IN ('rescheduled', 'transferred', 'error');
                UPDATE call_history SET result = 'no_result'
                    WHERE result IN ('rescheduled', 'transferred', 'error');
                CREATE TYPE callresult AS ENUM ('confirmed', 'cancelled', 'no_result', 'no_answer');
                ALTER TABLE call_tasks ALTER COLUMN result TYPE callresult USING result::callresult;
                ALTER TABLE call_history ALTER COLUMN result TYPE callresult USING result::callresult;
            END IF;
        END $$;
    """))


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
