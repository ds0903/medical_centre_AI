"""
Ініціалізація бази даних — створює БД і всі таблиці.

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


async def main() -> None:
    from db.database import init_db, close_db
    from db.models import CallTask

    logger.info("Підключаємось до PostgreSQL...")
    await init_db()

    # Показуємо що створилось
    from db.database import engine
    from sqlalchemy import inspect, text
    async with engine.connect() as conn:
        tables = await conn.run_sync(lambda c: inspect(c).get_table_names())
        logger.info(f"Таблиці в БД: {tables}")

    await close_db()
    logger.info("Готово.")


if __name__ == "__main__":
    asyncio.run(main())
