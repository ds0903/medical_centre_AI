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

from config import (
    DEBUG,
    ENV,
    SCHEDULER_MORNING_FETCH_HOUR,
    SCHEDULER_MORNING_CALL_HOUR,
    SCHEDULER_MORNING_CALL_END_HOUR,
    SCHEDULER_EVENING_FETCH_HOUR,
    SCHEDULER_EVENING_CALL_HOUR,
    SCHEDULER_EVENING_CALL_END_HOUR,
    SKIP_FETCH,
    SKIP_SCHEDULER,
)
from services.sheets_loader import load_services_catalog
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
    await load_services_catalog()

    # В debug завжди запускаємо ранкову сесію одразу (shift=0, архів не потрібен)
    logger.info("[MAIN] DEBUG — запускаємо ранкову сесію одразу")
    await fetch_and_schedule_calls(shift=0)
    await process_pending_calls(call_end_hour=SCHEDULER_MORNING_CALL_END_HOUR, archive=False)

    # Планувальник для наступних щоденних запусків
    scheduler = create_scheduler()
    scheduler.start()

    logger.info("Планувальник запущено (DEBUG). Ctrl+C для зупинки.")
    try:
        while True:
            await asyncio.sleep(60)
    except (KeyboardInterrupt, SystemExit):
        scheduler.shutdown(wait=False)
        await close_db()
        logger.info("Зупинено.")


async def run_production() -> None:
    """Production: планувальник працює безперервно. Binotel стартує тільки під час дзвінків."""
    logger.info(f"=== ЗАПУСК У PRODUCTION РЕЖИМІ ===")

    await init_db()
    await load_services_catalog()

    if SKIP_FETCH:
        # Пропускаємо fetch — одразу дзвонимо по тому що є в БД (ручний режим)
        logger.info("[MAIN] SKIP_FETCH=true — пропускаємо fetch, йдемо одразу дзвонити")
        now = datetime.now(ZoneInfo("Europe/Kyiv"))
        if now.hour >= SCHEDULER_EVENING_CALL_HOUR:
            await process_pending_calls(call_end_hour=SCHEDULER_EVENING_CALL_END_HOUR, archive=True)
        else:
            await process_pending_calls(call_end_hour=SCHEDULER_MORNING_CALL_END_HOUR, archive=False)
    elif SKIP_SCHEDULER:
        # Графік вимкнено — одразу fetch + дзвінки без state
        now = datetime.now(ZoneInfo("Europe/Kyiv"))
        if now.hour >= SCHEDULER_EVENING_FETCH_HOUR:
            logger.info(f"[MAIN] Час {now.strftime('%H:%M')} ≥ {SCHEDULER_EVENING_FETCH_HOUR}:00 — вечірній fetch")
            await fetch_and_schedule_calls(shift=1)
        elif now.hour >= SCHEDULER_MORNING_FETCH_HOUR:
            logger.info(f"[MAIN] Час {now.strftime('%H:%M')} ≥ {SCHEDULER_MORNING_FETCH_HOUR}:00 — ранковий fetch")
            await fetch_and_schedule_calls(shift=0)
    else:
        # Графік увімкнено — відновлення після рестарту по стану БД
        from scheduler.daily_runner import _has_pending_tasks, _is_workday
        now = datetime.now(ZoneInfo("Europe/Kyiv"))
        hour = now.hour
        logger.info(f"[MAIN] Відновлення після рестарту — час {now.strftime('%H:%M')}")

        if not _is_workday():
            logger.info("[MAIN] Вихідний день — fetch/дзвінки при відновленні пропускаємо")
        elif hour >= SCHEDULER_EVENING_FETCH_HOUR:
            # Вечірня зона
            if await _has_pending_tasks("evening"):
                if SCHEDULER_EVENING_CALL_HOUR <= hour < SCHEDULER_EVENING_CALL_END_HOUR:
                    logger.info("[MAIN] Є pending evening задачі — відновлюємо вечірній обдзвін")
                    await process_pending_calls(call_end_hour=SCHEDULER_EVENING_CALL_END_HOUR, archive=True)
            else:
                logger.info(f"[MAIN] Час {now.strftime('%H:%M')} — вечірній fetch (немає evening задач)")
                await fetch_and_schedule_calls(shift=1)
                if SCHEDULER_EVENING_CALL_HOUR <= hour < SCHEDULER_EVENING_CALL_END_HOUR:
                    if await _has_pending_tasks("evening"):
                        await process_pending_calls(call_end_hour=SCHEDULER_EVENING_CALL_END_HOUR, archive=True)
        elif hour >= SCHEDULER_MORNING_FETCH_HOUR:
            # Ранкова зона
            if await _has_pending_tasks("morning"):
                if SCHEDULER_MORNING_CALL_HOUR <= hour < SCHEDULER_MORNING_CALL_END_HOUR:
                    logger.info("[MAIN] Є pending morning задачі — відновлюємо ранковий обдзвін")
                    await process_pending_calls(call_end_hour=SCHEDULER_MORNING_CALL_END_HOUR, archive=False)
            else:
                logger.info(f"[MAIN] Час {now.strftime('%H:%M')} — ранковий fetch (немає morning задач)")
                await fetch_and_schedule_calls(shift=0)
                if SCHEDULER_MORNING_CALL_HOUR <= hour < SCHEDULER_MORNING_CALL_END_HOUR:
                    if await _has_pending_tasks("morning"):
                        await process_pending_calls(call_end_hour=SCHEDULER_MORNING_CALL_END_HOUR, archive=False)

            # Після ранкових — вечірній fetch+дзвінки (тільки якщо ранковий час уже минув)
            if hour >= SCHEDULER_MORNING_CALL_HOUR:
                logger.info("[MAIN] Ранкові завершено — вечірній fetch+дзвінки одразу")
                if not await _has_pending_tasks("evening"):
                    await fetch_and_schedule_calls(shift=1)
                if await _has_pending_tasks("evening"):
                    await process_pending_calls(call_end_hour=SCHEDULER_EVENING_CALL_END_HOUR, archive=True)

    scheduler = create_scheduler()
    scheduler.start()

    logger.info("Планувальник запущено. Ctrl+C для зупинки.")
    try:
        while True:
            await asyncio.sleep(60)
    except (KeyboardInterrupt, SystemExit):
        scheduler.shutdown(wait=False)
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
