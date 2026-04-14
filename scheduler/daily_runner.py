"""
Планувальник щоденних дзвінків — Блок 1.

DEBUG  : fetch + call запускаються одразу при старті через main.py
PRODUCTION : APScheduler — fetch о 08:00, calls о 10:00
"""

import asyncio
import logging
import uuid
from datetime import date, datetime, timedelta, timezone

from zoneinfo import ZoneInfo

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from sqlalchemy import select

from agents.block1.agent import Block1Agent
from config import (
    CALL_TIMEOUT_SEC,
    DAYS_BEFORE_VISIT,
    DEBUG,
    MAX_CALL_ATTEMPTS,
    PAUSE_BETWEEN_CALLS_SEC,
    SCHEDULER_CALL_END_HOUR,
    SCHEDULER_CALL_HOUR,
    SCHEDULER_FETCH_HOUR,
    SKIP_SCHEDULER,
)
from db.database import get_session
from db.models import CallHistory, CallResult, CallStatus, CallTask
from services import clinic_api
from services.binotel import initiate_call

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Архівування — перенос call_tasks → call_history
# ---------------------------------------------------------------------------

async def _archive_call_tasks() -> None:
    """Перенести всі задачі з call_tasks в call_history і очистити чергу."""
    async with get_session() as session:
        result = await session.execute(select(CallTask))
        tasks = result.scalars().all()

        if not tasks:
            return

        for task in tasks:
            session.add(CallHistory(
                id=task.id,
                visit_id=task.visit_id,
                patient_name=task.patient_name,
                patient_phone=task.patient_phone,
                doctor_name=task.doctor_name,
                doctor_id=task.doctor_id,
                visit_datetime=task.visit_datetime,
                message=task.message,
                status=task.status,
                result=task.result,
                attempts=task.attempts,
                call_duration=task.call_duration,
                created_at=task.created_at,
                completed_at=task.completed_at,
                error_message=task.error_message,
                operator_note=task.operator_note,
            ))
            await session.delete(task)

    logger.info(f"[ARCHIVE] Перенесено {len(tasks)} задач в call_history")


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
                place_id=visit["place_id"],
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
    """Обробляє всі pending задачі — робить дзвінки пацієнтам.
    Після основного циклу — один повторний прохід по тих хто не відповів.
    """
    logger.info("[CALLS] Починаємо обдзвін пацієнтів")

    # Cleanup після краша: задачі що залишились в in_progress з попереднього запуску.
    # attempts=0 → дзвінок не пішов (краш до generalCallID) → скидаємо в pending для retry.
    # attempts≥1 → дзвінок реально пішов (краш під час розмови) → failed/no_result,
    #              щоб не передзвонювати двічі.
    async with get_session() as session:
        result = await session.execute(
            select(CallTask).where(CallTask.status == CallStatus.in_progress)
        )
        stuck = result.scalars().all()
        for t in stuck:
            if t.attempts == 0:
                t.status = CallStatus.pending
                logger.warning(f"[CALLS] Краш до дзвінка → pending: {t.patient_name}")
            else:
                t.status = CallStatus.failed
                t.result = CallResult.no_result
                logger.warning(f"[CALLS] Краш під час дзвінка → failed: {t.patient_name}")

    _kyiv = ZoneInfo("Europe/Kyiv")

    passes = ["Основний обдзвін", "Повторний обдзвін (не відповіли)"]

    for label in passes:
        async with get_session() as session:
            result = await session.execute(
                select(CallTask).where(CallTask.status == CallStatus.pending)
                .order_by(CallTask.visit_datetime)
            )
            tasks = result.scalars().all()

        if not tasks:
            logger.info(f"[CALLS] {label}: немає задач — завершуємо")
            break

        logger.info(f"[CALLS] {label}: задач {len(tasks)}")
        stopped_by_time = False

        for i, task in enumerate(tasks):
            now_hour = datetime.now(_kyiv).hour
            if not SKIP_SCHEDULER and now_hour >= SCHEDULER_CALL_END_HOUR:
                logger.info(
                    f"[CALLS] Кінець робочого часу ({SCHEDULER_CALL_END_HOUR}:00). "
                    f"Залишилось: {len(tasks) - i}. Зупиняємось."
                )
                stopped_by_time = True
                break

            logger.info(f"[CALLS] [{i + 1}/{len(tasks)}] {task.patient_name}")
            await _process_single_call(task)

            if i < len(tasks) - 1:
                await asyncio.sleep(PAUSE_BETWEEN_CALLS_SEC)

        if stopped_by_time:
            break

    # Shutdown Binotel після всіх дзвінків
    if not DEBUG:
        from services import binotel as _binotel
        await _binotel.shutdown()

    # Архівуємо всі задачі (виконані + ті кому не встигли зателефонувати)
    await _archive_call_tasks()
    logger.info("[CALLS] Обдзвін завершено")


async def _mark_in_progress(task: CallTask) -> None:
    """Позначити задачу як in_progress і збільшити лічильник спроб.
    Викликається ПІСЛЯ того як Binotel підтвердив дзвінок (generalCallID отримано).
    """
    async with get_session() as session:
        db_task = await session.get(CallTask, task.id)
        db_task.status = CallStatus.in_progress
        db_task.attempts += 1


async def _process_single_call(task: CallTask) -> None:
    """Виконати один дзвінок пацієнту (з таймаутом)."""

    visit_context = {
        "visit_id": task.visit_id,
        "patient_name": task.patient_name,
        "patient_phone": task.patient_phone,
        "doctor_name": task.doctor_name,
        "place_id": task.place_id,
        "visit_datetime": task.visit_datetime.isoformat(),
        "message": task.message or "",
    }

    try:
        await asyncio.wait_for(
            _execute_call(task, visit_context),
            timeout=CALL_TIMEOUT_SEC,
        )
    except asyncio.TimeoutError:
        logger.error(
            f"[CALLS] Таймаут дзвінка ({CALL_TIMEOUT_SEC}с): {task.patient_name}"
        )
        await _save_error(task, f"Таймаут дзвінка ({CALL_TIMEOUT_SEC}с)")
    except Exception as e:
        logger.error(f"[CALLS] Помилка при дзвінку {task.patient_name}: {e}", exc_info=True)
        await _save_error(task, str(e))


async def _execute_call(task: CallTask, visit_context: dict) -> None:
    """Внутрішня логіка одного дзвінка (обгорнута в таймаут _process_single_call)."""
    if DEBUG:
        await _mark_in_progress(task)
        agent = Block1Agent(visit_context)
        call_result = await agent.run()
        call_duration = agent.call_duration
        agent.cleanup()
    else:
        from services import binotel as _binotel
        # Повний рестарт перед кожним дзвінком — extension одразу онлайн, warmup завжди свіжий
        await _binotel.shutdown()
        await _binotel.startup()

        # Запускаємо warmup і initiate_call паралельно поки пацієнт чує гудки
        agent = Block1Agent(visit_context)
        call_task = asyncio.create_task(
            initiate_call(task.patient_phone, task.patient_name)
        )
        warmup_task = asyncio.create_task(agent.warmup())
        binotel_call = None

        try:
            binotel_call = await call_task
            # Binotel підтвердив дзвінок (generalCallID отримано) — тепер рахуємо спробу.
            # Якщо initiate_call кинув виняток (до generalCallID) — сюди не доходимо,
            # спроба НЕ рахується і задача залишається pending для retry.
            await _mark_in_progress(task)

            if binotel_call is None:
                warmup_task.cancel()
                try:
                    await warmup_task
                except (asyncio.CancelledError, Exception):
                    pass
                logger.warning(f"[CALLS] Не відповів: {task.patient_name}")
                await _handle_no_answer(task)
                return

            await warmup_task
            call_result = await agent.run(binotel_call)
            call_duration = agent.call_duration
            agent.cleanup()
            await asyncio.sleep(2.0)
        finally:
            # Cleanup subtasks при таймауті або помилці
            if not call_task.done():
                call_task.cancel()
            if not warmup_task.done():
                warmup_task.cancel()
            if binotel_call is not None:
                try:
                    await binotel_call.hangup()
                except (asyncio.CancelledError, Exception):
                    pass

    # no_answer (one-way audio / пацієнт мовчить) → scheduler зробить retry
    if call_result == "no_answer":
        await _handle_no_answer(task)
        return
    await _save_result(task, call_result, call_duration)


# ---------------------------------------------------------------------------
# Збереження результатів
# ---------------------------------------------------------------------------

_RESULT_MAP = {
    "confirmed":   CallResult.confirmed,
    "cancelled":   CallResult.cancelled,
    "rescheduled": CallResult.rescheduled,
    "no_result":   CallResult.no_result,
    "no_answer":   CallResult.no_answer,
    "transferred": CallResult.transferred,
}

# Маппінг результату дзвінка → STATUS для API клініки:
#   1 — Підтверджує, 2 — Відміняє, 3 — Не зрозумів, 4 — Не додзвонились
# "rescheduled" відсутній — reschedule_visit() вже передає слот до API,
# окремого update_event_status не потрібно.
_STATUS_API_MAP = {
    "confirmed": 1,
    "cancelled": 2,
    "no_result": 3,
    "no_answer": 4,
    "transferred": 5,
}


async def _save_result(task: CallTask, result_str: str, duration: float) -> None:
    async with get_session() as session:
        db_task = await session.get(CallTask, task.id)
        db_task.status = CallStatus.completed
        db_task.result = _RESULT_MAP.get(result_str, CallResult.no_answer)
        db_task.call_duration = duration
        db_task.completed_at = datetime.now(timezone.utc).replace(tzinfo=None)
    logger.info(f"[CALLS] Результат збережено: {result_str} ({duration:.0f}с)")

    api_status = _STATUS_API_MAP.get(result_str)
    if api_status is not None:
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
        db_task.result = CallResult.no_result
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
