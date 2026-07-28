"""
Ініціалізація бази даних — створює таблиці і додає відсутні колонки.

Запуск:
    python init_db.py
"""

import asyncio
import logging
import sys

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger(__name__)

# Колонки які мають існувати: {таблиця: [(колонка, DDL-тип)]}
REQUIRED_COLUMNS = {
    "call_tasks": [
        ("doctor_specialization", "VARCHAR"),
        ("message", "VARCHAR"),
        ("error_message", "VARCHAR"),
        ("operator_note", "VARCHAR"),
    ],
    "call_history": [
        ("doctor_specialization", "VARCHAR"),
        ("message", "VARCHAR"),
        ("error_message", "VARCHAR"),
        ("operator_note", "VARCHAR"),
        ("place_id", "VARCHAR"),
    ],
}


async def ensure_columns(conn) -> None:
    from sqlalchemy import text, inspect

    existing: dict[str, set] = {}
    for table in REQUIRED_COLUMNS:
        cols = await conn.run_sync(
            lambda c, t=table: {col["name"] for col in inspect(c).get_columns(t)}
            if t in inspect(c).get_table_names()
            else set()
        )
        existing[table] = cols

    for table, columns in REQUIRED_COLUMNS.items():
        if table not in existing:
            logger.warning(f"Таблиця '{table}' не існує — пропускаємо колонки")
            continue
        for col_name, col_type in columns:
            if col_name not in existing[table]:
                sql = f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {col_name} {col_type}"
                await conn.execute(text(sql))
                logger.info(f"[MIGRATION] {table}.{col_name} ({col_type}) — додано")
            else:
                logger.info(f"[OK] {table}.{col_name} — існує")


async def main() -> None:
    from db.database import init_db, close_db, engine

    logger.info("Підключаємось до PostgreSQL...")
    await init_db()

    async with engine.begin() as conn:
        # Показуємо таблиці
        from sqlalchemy import inspect, text
        tables = await conn.run_sync(lambda c: inspect(c).get_table_names())
        logger.info(f"Таблиці в БД: {tables}")

        # Enum значення
        await conn.execute(text("ALTER TYPE callstatus ADD VALUE IF NOT EXISTS 'skipped'"))
        logger.info("[MIGRATION] callstatus enum: 'skipped' — OK")

        # Перевіряємо і додаємо відсутні колонки
        await ensure_columns(conn)

    await close_db()
    logger.info("Готово.")


if __name__ == "__main__":
    asyncio.run(main())
