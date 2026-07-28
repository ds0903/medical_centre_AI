"""
Планувальник Модуля 2.1 — вихідні дзвінки щодо повторних візитів.

Алгоритм (з ТЗ розділ «Алгоритм дзвінків»):
  1. Визначити пацієнтів у яких строк повторного візиту = сьогодні + N днів (default: 2)
  2. Для кожного пацієнта:
     a. Перевірити чи є вже запис до цього лікаря в ±5 днів → якщо є: пропустити
     b. Перевірити наявність вільних слотів (2 дні навколо дати + 30 днів)
     c. Якщо слотів немає взагалі → не дзвонити
     d. Якщо слоти є → додати в чергу дзвінків
  3. Виконати дзвінки в робочі години:
     Будні: 9:00–17:00 | Субота: 9:00–14:00 | Неділя: 10:00–14:00
  4. Повторні спроби: 2 спроби при недодзвоні

Робочий діапазон дзвінків налаштовується через .env:
  B2_CALL_DAYS_BEFORE=2   (за скільки днів до повторного візиту дзвонити)
  B2_MAX_RETRY=2
"""

import asyncio
import logging
from datetime import datetime, timedelta

from apscheduler.schedulers.asyncio import AsyncIOScheduler

logger = logging.getLogger(__name__)

# Робочі години (год, хв) — з ТЗ
WORK_HOURS = {
    0: (9, 0, 17, 0),   # пн
    1: (9, 0, 17, 0),   # вт
    2: (9, 0, 17, 0),   # ср
    3: (9, 0, 17, 0),   # чт
    4: (9, 0, 17, 0),   # пт
    5: (9, 0, 14, 0),   # сб
    6: (10, 0, 14, 0),  # нд
}


def _is_work_time(dt: datetime) -> bool:
    """Чи зараз робочі години для вихідних дзвінків."""
    hours = WORK_HOURS.get(dt.weekday())
    if not hours:
        return False
    start_h, start_m, end_h, end_m = hours
    start = dt.replace(hour=start_h, minute=start_m, second=0, microsecond=0)
    end   = dt.replace(hour=end_h,   minute=end_m,   second=0, microsecond=0)
    return start <= dt < end


async def fetch_repeat_visit_patients(days_before: int = 2) -> list[dict]:
    """
    Отримати пацієнтів яким потрібно зателефонувати сьогодні.

    Returns:
        Список dict з полями:
          patient_name, patient_phone, visit_id,
          previous_visit_date, previous_doctor,
          doctor_recommendation, repeat_visit_date
    """
    # TODO: clinic_api.get_repeat_visit_reminders(target_date=date.today() + timedelta(days=days_before))
    raise NotImplementedError


async def run_repeat_calls() -> None:
    """Основний job: визначити і виконати дзвінки щодо повторних візитів."""
    from config import B2_CALL_DAYS_BEFORE  # type: ignore[import-not-found]

    now = datetime.now()
    if not _is_work_time(now):
        logger.info("[B2 RUNNER] Поза робочими годинами — пропускаємо")
        return

    patients = await fetch_repeat_visit_patients(days_before=B2_CALL_DAYS_BEFORE)
    logger.info(f"[B2 RUNNER] Пацієнтів для обзвону: {len(patients)}")

    for patient in patients:
        # TODO: перевірити існуючий запис (±5 днів), перевірити слоти, ініціювати дзвінок
        logger.info(f"[B2 RUNNER] TODO: дзвінок → {patient.get('patient_name')}")


def setup_scheduler(scheduler: AsyncIOScheduler) -> None:
    """
    Додати job до APScheduler.
    Запускається щогодини в робочі дні — сам перевіряє _is_work_time.
    """
    scheduler.add_job(
        run_repeat_calls,
        trigger="cron",
        hour="9-17",
        minute="0",
        day_of_week="mon-sun",
        id="b2_repeat_calls",
        replace_existing=True,
    )
    logger.info("[B2 RUNNER] Планувальник повторних дзвінків зареєстровано")
