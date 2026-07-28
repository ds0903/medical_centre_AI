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
from config import (
    B2_RESULT_SHEETS_ID,
    B2_RESULT_SHEET_NAME,
    B2_CONFIG_SHEETS_ID,
    B2_CONFIG_SHEET_NAME,
    B2_MARKERS_SHEET_NAME,
    B2_CREDENTIALS_PATH,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


async def start_call_analyzer_listener() -> None:
    """Модуль 2.2: polling Binotel → аналіз → Google Sheets."""
    from agents.block2.call_analyzer.config_reader import ConfigReader
    from agents.block2.call_analyzer.analyzer import CallAnalyzer
    from agents.block2.call_analyzer.listener import CallAnalyzerListener
    from services.sheets_writer import SheetsWriter

    if not B2_RESULT_SHEETS_ID:
        logger.warning(
            "[B2.2] B2_RESULT_SHEETS_ID не задано — Модуль 2.2 вимкнено. "
            "Встановіть B2_RESULT_SHEETS_ID у .env для активації."
        )
        while True:
            await asyncio.sleep(3600)

    config_sheets_id = B2_CONFIG_SHEETS_ID or B2_RESULT_SHEETS_ID
    logger.info(f"[B2.2] Config Sheets: {config_sheets_id}")
    logger.info(f"[B2.2] Result Sheets: {B2_RESULT_SHEETS_ID}")

    config_reader = ConfigReader(
        spreadsheet_id   = config_sheets_id,
        credentials_path = B2_CREDENTIALS_PATH,
        criteria_sheet   = B2_CONFIG_SHEET_NAME,
        markers_sheet    = B2_MARKERS_SHEET_NAME,
    )
    sheets_writer = SheetsWriter(
        spreadsheet_id   = B2_RESULT_SHEETS_ID,
        sheet_name       = B2_RESULT_SHEET_NAME,
        credentials_path = B2_CREDENTIALS_PATH,
    )

    # Ініціалізація заголовків Sheets (завантажуємо критерії)
    try:
        criteria = await config_reader.get_all_criteria()
        criteria_names = [c.name for c in criteria]
        await sheets_writer.ensure_headers(criteria_names)
        logger.info(f"[B2.2] Sheets заголовки готові ({len(criteria_names)} критеріїв)")
    except Exception as e:
        logger.warning(f"[B2.2] Не вдалось ініціалізувати заголовки: {e!r} (продовжуємо)")

    analyzer = CallAnalyzer(sheets_writer=sheets_writer, config_reader=config_reader)
    listener = CallAnalyzerListener(analyzer=analyzer)

    logger.info("[B2.2] Listener запущено")
    await listener.start()


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
    _listener_task: asyncio.Task | None = None

    def _handle_shutdown():
        asyncio.create_task(_shutdown(scheduler, _listener_task))

    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, _handle_shutdown)

    # Запускаємо listener Модуля 2.2 паралельно
    _listener_task = asyncio.create_task(start_call_analyzer_listener())
    await _listener_task


async def _shutdown(scheduler: AsyncIOScheduler, listener_task=None) -> None:
    logger.info("[B2] Завершення роботи...")
    scheduler.shutdown(wait=False)
    if listener_task and not listener_task.done():
        listener_task.cancel()
    tasks = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
    asyncio.get_event_loop().stop()


if __name__ == "__main__":
    asyncio.run(main())
