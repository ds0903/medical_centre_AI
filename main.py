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
from datetime import datetime
from zoneinfo import ZoneInfo

from config import DEBUG, ENV, SCHEDULER_FETCH_HOUR, SKIP_FETCH
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

    # Якщо вже минув час fetch — запускаємо одразу
    now = datetime.now(ZoneInfo("Europe/Kyiv"))
    if now.hour >= SCHEDULER_FETCH_HOUR:
        logger.info(f"[MAIN] Час {now.strftime('%H:%M')} ≥ {SCHEDULER_FETCH_HOUR}:00 — запускаємо fetch одразу")
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
    """Production: планувальник працює безперервно. Binotel стартує тільки під час дзвінків."""
    logger.info(f"=== ЗАПУСК У PRODUCTION РЕЖИМІ ===")

    await init_db()

    if SKIP_FETCH:
        # Пропускаємо fetch — одразу дзвонимо по тому що є в БД
        logger.info("[MAIN] SKIP_FETCH=true — пропускаємо fetch, йдемо одразу дзвонити")
        await process_pending_calls()
    else:
        # Якщо вже минув час fetch — запускаємо одразу, інакше чекаємо розкладу
        now = datetime.now(ZoneInfo("Europe/Kyiv"))
        if now.hour >= SCHEDULER_FETCH_HOUR:
            logger.info(f"[MAIN] Час {now.strftime('%H:%M')} ≥ {SCHEDULER_FETCH_HOUR}:00 — запускаємо fetch одразу")
            await fetch_and_schedule_calls()

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
    except Exception as e:
        logger.error(f"[MAIN] Критична помилка — завершуємо: {e}", exc_info=True)
        # Намагаємось завершити активний дзвінок якщо він ще йде
        try:
            from services import binotel as _binotel
            asyncio.run(_binotel.shutdown())
        except Exception:
            pass
        sys.exit(1)
