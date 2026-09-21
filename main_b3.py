"""
Модуль 2.2 — точка входу (AI-аналіз дзвінків колл-центру).

Запускає незалежно від main.py та main_b2.py:
  - CallAnalyzerListener: polling Binotel кожні N хв → аналіз → Google Sheets
  - Email scheduler: тижневий звіт (пн 09:00) + місячний (1-го 09:00)

Запуск:
    python main_b3.py
"""

import asyncio
import logging
import signal
import sys
from datetime import datetime

from apscheduler.schedulers.asyncio import AsyncIOScheduler

from config import (
    B2_CONFIG_SHEET_NAME,
    B2_CONFIG_SHEETS_ID,
    B2_CREDENTIALS_PATH,
    B2_MARKERS_SHEET_NAME,
    B2_RESULT_SHEET_NAME,
    B2_RESULT_SHEETS_ID,
)
from db.database import close_db, init_db

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger(__name__)


# ------------------------------------------------------------------
# Listener (основний нескінченний цикл)
# ------------------------------------------------------------------

async def _run_listener() -> None:
    from agents.block2.call_analyzer.analyzer import CallAnalyzer
    from agents.block2.call_analyzer.config_reader import ConfigReader
    from agents.block2.call_analyzer.listener import CallAnalyzerListener
    from services.sheets_writer import SheetsWriter

    if not B2_RESULT_SHEETS_ID:
        logger.warning(
            "[B3] B2_RESULT_SHEETS_ID не задано — listener вимкнено. "
            "Встановіть B2_RESULT_SHEETS_ID у .env"
        )
        while True:
            await asyncio.sleep(3600)

    config_reader = ConfigReader(
        spreadsheet_id   = B2_CONFIG_SHEETS_ID or B2_RESULT_SHEETS_ID,
        credentials_path = B2_CREDENTIALS_PATH,
        criteria_sheet   = B2_CONFIG_SHEET_NAME,
        markers_sheet    = B2_MARKERS_SHEET_NAME,
    )
    sheets_writer = SheetsWriter(
        spreadsheet_id   = B2_RESULT_SHEETS_ID,
        sheet_name       = B2_RESULT_SHEET_NAME,
        credentials_path = B2_CREDENTIALS_PATH,
    )

    try:
        criteria = await config_reader.get_all_criteria()
        await sheets_writer.ensure_headers([c.name for c in criteria])
        logger.info(f"[B3] Sheets заголовки готові ({len(criteria)} критеріїв)")
    except Exception as e:
        logger.warning(f"[B3] Не вдалось ініціалізувати заголовки Sheets: {e!r} — продовжуємо")

    analyzer = CallAnalyzer(sheets_writer=sheets_writer, config_reader=config_reader)
    listener = CallAnalyzerListener(analyzer=analyzer)

    logger.info("[B3] Listener запущено")
    await listener.start()


# ------------------------------------------------------------------
# Email scheduler
# ------------------------------------------------------------------

def _setup_email_scheduler(scheduler: AsyncIOScheduler) -> None:
    from services.email_reporter import send_weekly_report, send_monthly_report

    async def _weekly():
        logger.info("[B3] Email: тижневий звіт")
        await send_weekly_report()

    async def _monthly():
        logger.info("[B3] Email: місячний звіт")
        await send_monthly_report()

    scheduler.add_job(_weekly,  "cron", day_of_week="mon", hour=9, minute=0, id="weekly_report")
    scheduler.add_job(_monthly, "cron", day=1,             hour=9, minute=0, id="monthly_report")
    logger.info("[B3] Email scheduler: тижневий (пн 09:00), місячний (1-го 09:00)")


# ------------------------------------------------------------------
# Graceful shutdown
# ------------------------------------------------------------------

async def _shutdown(scheduler: AsyncIOScheduler) -> None:
    logger.info("[B3] Завершення роботи...")
    scheduler.shutdown(wait=False)
    await close_db()
    tasks = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
    for t in tasks:
        t.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
    asyncio.get_event_loop().stop()


# ------------------------------------------------------------------
# Main
# ------------------------------------------------------------------

async def main() -> None:
    logger.info("=" * 60)
    logger.info("Модуль 2.2 (B3) — AI-аналіз дзвінків колл-центру")
    logger.info("=" * 60)

    await init_db()

    scheduler = AsyncIOScheduler(timezone="Europe/Kyiv")
    _setup_email_scheduler(scheduler)
    scheduler.start()

    loop = asyncio.get_running_loop()
    try:
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, lambda: asyncio.create_task(_shutdown(scheduler)))
    except NotImplementedError:
        pass  # Windows не підтримує add_signal_handler

    listener_task = asyncio.create_task(_run_listener())
    logger.info("[B3] Готово. Ctrl+C для зупинки.")

    try:
        await listener_task
    except asyncio.CancelledError:
        pass
    except Exception as e:
        logger.error(f"[B3] Критична помилка listener: {e!r}", exc_info=True)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nЗупинено.")
    except Exception as e:
        logger.error(f"[B3] Критична помилка: {e!r}", exc_info=True)
        sys.exit(1)
