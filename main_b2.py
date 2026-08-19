"""
Модуль 2.1 — точка входу (нагадування про повторні візити).

Запускає планувальник вихідних дзвінків щодо повторних прийомів.
Незалежний від main.py (Блок 1) та main_b3.py (Модуль 2.2).

Запуск:
    python main_b2.py
"""

import asyncio
import logging
import signal
import sys

from apscheduler.schedulers.asyncio import AsyncIOScheduler

from config import SKIP_SCHEDULER
from db.database import close_db, init_db
from scheduler.repeat_runner import setup_scheduler as setup_b21_scheduler

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger(__name__)


async def _shutdown(scheduler: AsyncIOScheduler) -> None:
    logger.info("[B2] Завершення роботи...")
    scheduler.shutdown(wait=False)
    await close_db()
    tasks = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
    for t in tasks:
        t.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
    asyncio.get_event_loop().stop()


async def main() -> None:
    logger.info("=" * 60)
    logger.info("Модуль 2.1 — нагадування про повторні візити")
    logger.info("=" * 60)

    await init_db()

    scheduler = AsyncIOScheduler(timezone="Europe/Kyiv")
    setup_b21_scheduler(scheduler)
    scheduler.start()

    if SKIP_SCHEDULER:
        from scheduler.repeat_runner import run_repeat_calls
        logger.info("[B2] SKIP_SCHEDULER=true — запускаємо одразу без чекання cron")
        asyncio.create_task(run_repeat_calls())

    loop = asyncio.get_running_loop()
    try:
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, lambda: asyncio.create_task(_shutdown(scheduler)))
    except NotImplementedError:
        pass  # Windows

    logger.info("[B2] Планувальник запущено. Ctrl+C для зупинки.")
    try:
        while True:
            await asyncio.sleep(60)
    except asyncio.CancelledError:
        pass


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nЗупинено.")
    except Exception as e:
        logger.error(f"[B2] Критична помилка: {e!r}", exc_info=True)
        sys.exit(1)
