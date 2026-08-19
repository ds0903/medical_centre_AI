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
        # call_tasks
        "ALTER TABLE call_tasks ADD COLUMN IF NOT EXISTS message VARCHAR",
        "ALTER TABLE call_tasks ADD COLUMN IF NOT EXISTS appointment VARCHAR",
        "ALTER TABLE call_tasks ADD COLUMN IF NOT EXISTS preparation VARCHAR",
        "ALTER TABLE call_tasks ADD COLUMN IF NOT EXISTS call_session VARCHAR",
        "ALTER TABLE call_tasks ADD COLUMN IF NOT EXISTS error_message VARCHAR",
        "ALTER TABLE call_tasks ADD COLUMN IF NOT EXISTS operator_note VARCHAR",
        "ALTER TABLE call_tasks ADD COLUMN IF NOT EXISTS commentary VARCHAR(512)",
        "ALTER TABLE call_tasks ADD COLUMN IF NOT EXISTS doctor_specialization VARCHAR",
        "ALTER TABLE call_tasks ADD COLUMN IF NOT EXISTS updated_at TIMESTAMP",
        # call_history
        "ALTER TABLE call_history ADD COLUMN IF NOT EXISTS message VARCHAR",
        "ALTER TABLE call_history ADD COLUMN IF NOT EXISTS appointment VARCHAR",
        "ALTER TABLE call_history ADD COLUMN IF NOT EXISTS preparation VARCHAR",
        "ALTER TABLE call_history ADD COLUMN IF NOT EXISTS call_session VARCHAR",
        "ALTER TABLE call_history ADD COLUMN IF NOT EXISTS error_message VARCHAR",
        "ALTER TABLE call_history ADD COLUMN IF NOT EXISTS operator_note VARCHAR",
        "ALTER TABLE call_history ADD COLUMN IF NOT EXISTS commentary VARCHAR(512)",
        "ALTER TABLE call_history ADD COLUMN IF NOT EXISTS doctor_specialization VARCHAR",
        # repeat_call_tasks
        "ALTER TABLE repeat_call_tasks ADD COLUMN IF NOT EXISTS patient_id VARCHAR",
        "ALTER TABLE repeat_call_tasks ADD COLUMN IF NOT EXISTS previous_doctor VARCHAR",
        "ALTER TABLE repeat_call_tasks ADD COLUMN IF NOT EXISTS doctor_id VARCHAR",
        "ALTER TABLE repeat_call_tasks ADD COLUMN IF NOT EXISTS doctor_specialization VARCHAR",
        "ALTER TABLE repeat_call_tasks ADD COLUMN IF NOT EXISTS doctor_recommendation VARCHAR",
        "ALTER TABLE repeat_call_tasks ADD COLUMN IF NOT EXISTS repeat_visit_date TIMESTAMP",
        "ALTER TABLE repeat_call_tasks ADD COLUMN IF NOT EXISTS decline_category VARCHAR",
        "ALTER TABLE repeat_call_tasks ADD COLUMN IF NOT EXISTS new_visit_id VARCHAR",
        "ALTER TABLE repeat_call_tasks ADD COLUMN IF NOT EXISTS error_message VARCHAR",
        "ALTER TABLE repeat_call_tasks ADD COLUMN IF NOT EXISTS idpoint INTEGER",
        "ALTER TABLE repeat_call_tasks ADD COLUMN IF NOT EXISTS commentary VARCHAR(512)",
        # repeat_call_history
        "ALTER TABLE repeat_call_history ADD COLUMN IF NOT EXISTS patient_id VARCHAR",
        "ALTER TABLE repeat_call_history ADD COLUMN IF NOT EXISTS previous_doctor VARCHAR",
        "ALTER TABLE repeat_call_history ADD COLUMN IF NOT EXISTS doctor_id VARCHAR",
        "ALTER TABLE repeat_call_history ADD COLUMN IF NOT EXISTS doctor_specialization VARCHAR",
        "ALTER TABLE repeat_call_history ADD COLUMN IF NOT EXISTS doctor_recommendation VARCHAR",
        "ALTER TABLE repeat_call_history ADD COLUMN IF NOT EXISTS repeat_visit_date TIMESTAMP",
        "ALTER TABLE repeat_call_history ADD COLUMN IF NOT EXISTS decline_category VARCHAR",
        "ALTER TABLE repeat_call_history ADD COLUMN IF NOT EXISTS new_visit_id VARCHAR",
        "ALTER TABLE repeat_call_history ADD COLUMN IF NOT EXISTS error_message VARCHAR",
        "ALTER TABLE repeat_call_history ADD COLUMN IF NOT EXISTS idpoint INTEGER",
        "ALTER TABLE repeat_call_history ADD COLUMN IF NOT EXISTS commentary VARCHAR(512)",
        # analyzed_calls
        "ALTER TABLE analyzed_calls ADD COLUMN IF NOT EXISTS sheets_row INTEGER",
        "ALTER TABLE analyzed_calls ADD COLUMN IF NOT EXISTS error_message VARCHAR",
    ]
    for sql in migrations:
        await conn.execute(text(sql))

    # Додати rescheduled до callresult enum якщо відсутній
    await conn.execute(text(
        "ALTER TYPE callresult ADD VALUE IF NOT EXISTS 'rescheduled'"
    ))
    await conn.execute(text(
        "ALTER TYPE callresult ADD VALUE IF NOT EXISTS 'transferred'"
    ))

    # Перейменувати doctor_id → place_id (IDPOINT — це точка клініки, не лікар)
    for table in ("call_tasks", "call_history"):
        exists = await conn.execute(text(
            f"SELECT 1 FROM pg_attribute "
            f"WHERE attrelid = '{table}'::regclass AND attname = 'doctor_id' AND NOT attisdropped"
        ))
        if exists.scalar():
            await conn.execute(text(
                f"ALTER TABLE {table} RENAME COLUMN doctor_id TO place_id"
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
