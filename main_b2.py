"""
Блок 2 — точка входу.

Запускає два незалежних модулі паралельно:
  Модуль 2.1: планувальник вихідних дзвінків щодо повторних візитів
  Модуль 2.2: listener на нові записи дзвінків з Binotel → аналіз

Використання:
  python main_b2.py

Порівняння з main.py (Блок 1):
  main.py    — нагадування про поточний запис (завтра/сьогодні)
  main_b2.py — нагадування про повторний запис (рекомендація лікаря)
             + аналіз якості дзвінків колл-центру
"""

import asyncio
import logging
import signal

from apscheduler.schedulers.asyncio import AsyncIOScheduler

from scheduler.repeat_runner import setup_scheduler as setup_b21_scheduler

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


async def start_call_analyzer_listener() -> None:
    """
    Модуль 2.2: слухає Binotel на нові завершені дзвінки → аналізує.
    TODO: реалізувати polling або webhook від Binotel.
    """
    logger.info("[B2] Модуль 2.2 (аналіз дзвінків) — TODO: реалізувати")
    # Заглушка — нескінченне очікування щоб не падав процес
    while True:
        await asyncio.sleep(3600)


async def main() -> None:
    logger.info("=" * 60)
    logger.info("Блок 2 стартує")
    logger.info("  Модуль 2.1 — нагадування про повторні візити")
    logger.info("  Модуль 2.2 — аналіз дзвінків колл-центру")
    logger.info("=" * 60)

    # APScheduler для Модуля 2.1
    scheduler = AsyncIOScheduler(timezone="Europe/Kiev")
    setup_b21_scheduler(scheduler)
    scheduler.start()

    # Graceful shutdown
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, lambda: asyncio.create_task(_shutdown(scheduler)))

    # Запускаємо listener Модуля 2.2 паралельно
    await start_call_analyzer_listener()


async def _shutdown(scheduler: AsyncIOScheduler) -> None:
    logger.info("[B2] Завершення роботи...")
    scheduler.shutdown(wait=False)
    tasks = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
    asyncio.get_event_loop().stop()


if __name__ == "__main__":
    asyncio.run(main())
