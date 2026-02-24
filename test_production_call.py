"""
Тест production дзвінка через Binotel.

Запуск:
    python test_production_call.py

Що відбудеться:
  1. SIP клієнт (914) зареєструється в Binotel → стане Онлайн
  2. Binotel задзвонить на вказаний номер пацієнта
  3. Коли пацієнт відповість — підключиться AI "Оля" (Gemini Live)
  4. Розмова відбудеться як справжній дзвінок
"""

import asyncio
import logging
import os
import sys

# Примусово production режим для цього тесту
os.environ["ENV"] = "production"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger(__name__)

# Тестовий візит — змінюй phone на свій номер
TEST_VISIT = {
    "visit_id":       "test-001",
    "patient_name":   "Тестовий Пацієнт",
    "patient_phone":  "0687034410",        # ← твій номер
    "doctor_name":    "Лікар Тестовий",
    "doctor_id":      "doc-001",
    "visit_datetime": "2026-02-25T10:00:00",
}


async def main() -> None:
    from services import binotel
    from agents.block1.agent import Block1Agent

    logger.info("=== ТЕСТ PRODUCTION ДЗВІНКА ===")
    logger.info(f"Дзвінок на: {TEST_VISIT['patient_phone']}")

    # Крок 1: Запустити Binotel сервіси (SIP реєстрація + WebSocket)
    logger.info("Реєструємо SIP extension 914...")
    await binotel.startup()
    logger.info("SIP онлайн! Перевір в Binotel — 914 має бути Онлайн")

    try:
        # Крок 2: Ініціювати дзвінок
        logger.info(f"Дзвонимо на {TEST_VISIT['patient_phone']}...")
        call = await binotel.initiate_call(
            phone=TEST_VISIT["patient_phone"],
            patient_name=TEST_VISIT["patient_name"],
        )

        if call is None:
            logger.error("Пацієнт не відповів або технічна помилка")
            return

        # Крок 3: Запустити агента
        logger.info("Пацієнт відповів! Запускаємо AI агента...")
        agent = Block1Agent(TEST_VISIT)
        result = await agent.run(call)
        agent.cleanup()

        # Крок 4: Завершити дзвінок
        if call.is_active:
            await call.hangup()

        logger.info(f"=== РЕЗУЛЬТАТ: {result} ===")

    finally:
        await binotel.shutdown()
        logger.info("Binotel сервіси зупинено")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nЗупинено.")
