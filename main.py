"""
Точка входу.

DEBUG mode   : одразу запускає fetch → один тестовий дзвінок через мікрофон
PRODUCTION   : запускає APScheduler (fetch о 8:00, calls о 10:00)
               + ініціалізує Binotel SIP та WebSocket

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
    Debug: одразу запускає fetch + calls, потім планувальник для наступних циклів.
    Аудіо — мікрофон/динамік. Дані — з test_visits.json. Binotel не потрібен.
    """
    logger.info("=== ЗАПУСК У DEBUG РЕЖИМІ ===")

    await init_db()

    # Одразу запускаємо поточний цикл
    await fetch_and_schedule_calls()
    await process_pending_calls()

    # Планувальник для наступних щоденних запусків
    scheduler = create_scheduler()
    scheduler.start()

    logger.info("Планувальник запущено (DEBUG). Ctrl+C для зупинки.")
    try:
        while True:
            await asyncio.sleep(60)
    except (KeyboardInterrupt, SystemExit):
        scheduler.shutdown()
        await close_db()
        logger.info("Зупинено.")


async def run_production() -> None:
    """Production: планувальник працює безперервно + Binotel сервіси."""
    logger.info(f"=== ЗАПУСК У PRODUCTION РЕЖИМІ ===")

    await init_db()

    # Ініціалізуємо Binotel (SIP + WebSocket) до старту планувальника
    from services import binotel
    await binotel.startup()

    scheduler = create_scheduler()
    scheduler.start()

    logger.info("Планувальник запущено. Ctrl+C для зупинки.")
    try:
        while True:
            await asyncio.sleep(60)
    except (KeyboardInterrupt, SystemExit):
        scheduler.shutdown()
        await binotel.shutdown()
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
