"""
Планувальник щоденних дзвінків — Блок 1.

DEBUG  : fetch + call запускаються одразу при старті через main.py
PRODUCTION : APScheduler — fetch о 08:00, calls о 10:00
"""

import asyncio
import logging
import uuid
from datetime import date, datetime, timedelta

from zoneinfo import ZoneInfo

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from sqlalchemy import select

from agents.block1.agent import Block1Agent
from config import (
    DAYS_BEFORE_VISIT,
    DEBUG,
    MAX_CALL_ATTEMPTS,
    PAUSE_BETWEEN_CALLS_SEC,
    SCHEDULER_CALL_END_HOUR,
    SCHEDULER_CALL_HOUR,
    SCHEDULER_FETCH_HOUR,
)
from db.database import get_session
from db.models import CallResult, CallStatus, CallTask
from services import clinic_api
from services.binotel import initiate_call

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# КРОК 1 — Щодня о 8:00: отримати список візитів та зберегти задачі
# ---------------------------------------------------------------------------

async def fetch_and_schedule_calls() -> None:
    """
    Отримує візити на завтра (або через DAYS_BEFORE_VISIT днів)
    та зберігає call_tasks у БД для кожного нового візиту.
    """
    target_date = date.today() + timedelta(days=DAYS_BEFORE_VISIT)
    logger.info(f"[FETCH] Отримуємо візити на {target_date}")

    try:
        visits = await clinic_api.get_upcoming_visits(target_date)
    except Exception as e:
        logger.error(f"[FETCH] Помилка отримання візитів: {e}")
        return

    logger.info(f"[FETCH] Знайдено {len(visits)} візитів")
    created = 0

    async with get_session() as session:
        for visit in visits:
            # Пропускаємо якщо задача вже існує
            existing = await session.scalar(
                select(CallTask).where(CallTask.visit_id == visit["visit_id"])
            )
            if existing:
                logger.debug(f"[FETCH] Задача вже є: {visit['visit_id']}")
                continue

            task = CallTask(
                id=uuid.uuid4(),
                visit_id=visit["visit_id"],
                patient_name=visit["patient_name"],
                patient_phone=visit["patient_phone"],
                doctor_name=visit["doctor_name"],
                doctor_id=visit["doctor_id"],
                visit_datetime=datetime.fromisoformat(visit["visit_datetime"]),
                message=visit.get("message", ""),
                status=CallStatus.pending,
                attempts=0,
            )
            session.add(task)
            created += 1

    logger.info(f"[FETCH] Створено нових задач: {created}")


# ---------------------------------------------------------------------------
# КРОК 2 — Щодня о 10:00: обдзвонити всіх пацієнтів
# ---------------------------------------------------------------------------

async def process_pending_calls() -> None:
    """Обробляє всі pending задачі — робить дзвінки пацієнтам."""
    logger.info("[CALLS] Починаємо обдзвін пацієнтів")

    async with get_session() as session:
        result = await session.execute(
            select(CallTask).where(CallTask.status == CallStatus.pending)
        )
        tasks = result.scalars().all()

    logger.info(f"[CALLS] Задач в черзі: {len(tasks)}")

    _kyiv = ZoneInfo("Europe/Kyiv")

    for i, task in enumerate(tasks):
        # Перевіряємо кінець робочого часу перед кожним дзвінком
        now_hour = datetime.now(_kyiv).hour
        if now_hour >= SCHEDULER_CALL_END_HOUR:
            logger.info(
                f"[CALLS] Кінець робочого часу ({SCHEDULER_CALL_END_HOUR}:00). "
                f"Залишилось необдзвонених: {len(tasks) - i}. Зупиняємось."
            )
            break

        logger.info(f"[CALLS] [{i + 1}/{len(tasks)}] {task.patient_name}")
        await _process_single_call(task)

        # Пауза між дзвінками щоб не спамити
        if i < len(tasks) - 1:
            await asyncio.sleep(PAUSE_BETWEEN_CALLS_SEC)

    logger.info("[CALLS] Обдзвін завершено")


async def _process_single_call(task: CallTask) -> None:
    """Виконати один дзвінок пацієнту."""

    # Позначаємо як "в процесі"
    async with get_session() as session:
        db_task = await session.get(CallTask, task.id)
        db_task.status = CallStatus.in_progress
        db_task.attempts += 1

    visit_context = {
        "visit_id": task.visit_id,
        "patient_name": task.patient_name,
        "patient_phone": task.patient_phone,
        "doctor_name": task.doctor_name,
        "doctor_id": task.doctor_id,
        "visit_datetime": task.visit_datetime.isoformat(),
        "message": task.message or "",
    }

    try:
        if DEBUG:
            agent = Block1Agent(visit_context)
            call_result = await agent.run()
            call_duration = agent.call_duration
            agent.cleanup()
        else:
            # Ініціюємо дзвінок через Binotel
            binotel_call = await initiate_call(task.patient_phone, task.patient_name)

            if binotel_call is None:
                logger.warning(f"[CALLS] Не відповів: {task.patient_name}")
                await _handle_no_answer(task)
                return

            agent = Block1Agent(visit_context)
            call_result = await agent.run(binotel_call)
            call_duration = agent.call_duration
            agent.cleanup()
            await binotel_call.hangup()

        await _save_result(task, call_result, call_duration)

    except Exception as e:
        logger.error(f"[CALLS] Помилка при дзвінку {task.patient_name}: {e}", exc_info=True)
        await _save_error(task, str(e))


# ---------------------------------------------------------------------------
# Збереження результатів
# ---------------------------------------------------------------------------

_RESULT_MAP = {
    "confirmed": CallResult.confirmed,
    "rescheduled": CallResult.rescheduled,
    "cancelled": CallResult.cancelled,
    "no_result": CallResult.no_answer,
    "transferred": CallResult.transferred,
}

# Маппінг результату дзвінка → STATUS для API клініки:
#   1 — Підтверджує, 2 — Відміняє, 3 — Не зрозумів, 4 — Не додзвонились
_STATUS_API_MAP = {
    "confirmed": 1,
    "rescheduled": 1,
    "cancelled": 2,
    "no_result": 3,
    "transferred": 3,
}


async def _save_result(task: CallTask, result_str: str, duration: float) -> None:
    async with get_session() as session:
        db_task = await session.get(CallTask, task.id)
        db_task.status = CallStatus.completed
        db_task.result = _RESULT_MAP.get(result_str, CallResult.no_answer)
        db_task.call_duration = duration
        db_task.completed_at = datetime.utcnow()
    logger.info(f"[CALLS] Результат збережено: {result_str} ({duration:.0f}с)")

    api_status = _STATUS_API_MAP.get(result_str, 3)
    await clinic_api.update_event_status(task.visit_id, api_status)


async def _handle_no_answer(task: CallTask) -> None:
    async with get_session() as session:
        db_task = await session.get(CallTask, task.id)
        if db_task.attempts >= MAX_CALL_ATTEMPTS:
            db_task.status = CallStatus.failed
            db_task.result = CallResult.no_answer
            logger.warning(f"[CALLS] Вичерпано спроби для {task.patient_name}")
            await clinic_api.update_event_status(task.visit_id, 4)
        else:
            db_task.status = CallStatus.pending  # Спробуємо ще раз наступного разу
            logger.info(f"[CALLS] Спроба {db_task.attempts}/{MAX_CALL_ATTEMPTS} для {task.patient_name}")


async def _save_error(task: CallTask, error_msg: str) -> None:
    async with get_session() as session:
        db_task = await session.get(CallTask, task.id)
        db_task.status = CallStatus.failed
        db_task.result = CallResult.error
        db_task.error_message = error_msg[:500]  # Обрізаємо щоб не переповнити поле


# ---------------------------------------------------------------------------
# Планувальник (production)
# ---------------------------------------------------------------------------

def create_scheduler() -> AsyncIOScheduler:
    scheduler = AsyncIOScheduler(timezone="Europe/Kyiv")

    scheduler.add_job(
        fetch_and_schedule_calls,
        CronTrigger(hour=SCHEDULER_FETCH_HOUR, minute=0),
        id="fetch_visits",
        name="Отримати візити та створити задачі",
        replace_existing=True,
    )

    scheduler.add_job(
        process_pending_calls,
        CronTrigger(hour=SCHEDULER_CALL_HOUR, minute=0),
        id="process_calls",
        name="Обдзвонити пацієнтів",
        replace_existing=True,
    )

    logger.info(
        f"[SCHEDULER] Задачі: fetch о {SCHEDULER_FETCH_HOUR}:00, "
        f"calls о {SCHEDULER_CALL_HOUR}:00 (Europe/Kyiv)"
    )
    return scheduler
