"""
Точка входу.

DEBUG mode   : одразу запускає fetch → один тестовий дзвінок через мікрофон
PRODUCTION   : запускає APScheduler (fetch о 8:00, calls о 10:00)

Запуск:
    python main.py
"""

import asyncio
import logging
import sys

from config import DEBUG, ENV
from db.database import close_db, init_db
from scheduler.daily_runner import (
    create_scheduler,
    fetch_and_schedule_calls,
    process_pending_calls,
)

logging.basicConfig(
    level=logging.DEBUG if DEBUG else logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger(__name__)


async def run_debug() -> None:
    """
    Debug: симулюємо один цикл (fetch → дзвінок через мікрофон).
    Всі API клініки — з test_visits.json.
    Ти граєш роль пацієнта через мікрофон.
    """
    logger.info(f"=== ЗАПУСК У DEBUG РЕЖИМІ ===")

    await init_db()
    await fetch_and_schedule_calls()
    await process_pending_calls()
    await close_db()


async def run_production() -> None:
    """Production: планувальник працює безперервно."""
    logger.info(f"=== ЗАПУСК У PRODUCTION РЕЖИМІ ===")

    await init_db()

    scheduler = create_scheduler()
    scheduler.start()

    logger.info("Планувальник запущено. Ctrl+C для зупинки.")
    try:
        while True:
            await asyncio.sleep(60)
    except (KeyboardInterrupt, SystemExit):
        scheduler.shutdown()
        await close_db()
        logger.info("Зупинено.")


async def main() -> None:
    if DEBUG:
        await run_debug()
    else:
        await run_production()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nЗупинено.")
