"""
Планувальник Модуля 2.1 — вихідні дзвінки щодо повторних візитів.

Алгоритм (з ТЗ розділ «Алгоритм дзвінків»):
  1. Визначити пацієнтів у яких строк повторного візиту = сьогодні + N днів (default: 2)
  2. Для кожного пацієнта:
     a. Перевірити чи є вже запис до цього лікаря в ±5 днів → якщо є: пропустити
     b. Перевірити наявність вільних слотів (30 днів вперед)
     c. Якщо слотів немає взагалі → не дзвонити
     d. Якщо слоти є → додати в чергу та виконати дзвінок
  3. Виконати дзвінки в робочі години (Пн-Пт 9-17, Сб 9-14, Нд 10-14)
  4. Повторні спроби: max B2_MAX_RETRY при недодзвоні

.env параметри:
  B2_CALL_DAYS_BEFORE=2  (за скільки днів до повторного візиту дзвонити)
  B2_MAX_RETRY=2
"""

import asyncio
import logging
import uuid
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from sqlalchemy import select

from db.database import get_session
from db.models import RepeatCallTask, RepeatCallHistory, RepeatCallStatus, RepeatCallResult

logger = logging.getLogger(__name__)

_KYIV = ZoneInfo("Europe/Kyiv")

# Робочі години для вихідних дзвінків (год_початку, хв, год_кінця, хв)
WORK_HOURS = {
    0: (9, 0, 17, 0),   # понеділок
    1: (9, 0, 17, 0),   # вівторок
    2: (9, 0, 17, 0),   # середа
    3: (9, 0, 17, 0),   # четвер
    4: (9, 0, 17, 0),   # п'ятниця
    5: (9, 0, 14, 0),   # субота
    6: (10, 0, 14, 0),  # неділя
}

_RESULT_MAP = {
    "booked":    RepeatCallResult.booked,
    "declined":  RepeatCallResult.declined,
    "self_book": RepeatCallResult.self_book,
    "no_answer": RepeatCallResult.no_answer,
    "no_result": RepeatCallResult.no_result,
}

# API статус для клініки: 1=Записано, 2=Відмовився, 3=Сам запишеться, 4=Не додзвонились
_STATUS_API_MAP = {
    "booked":    1,
    "declined":  2,
    "self_book": 3,
    "no_answer": 4,
    "no_result": 4,
}


def _is_work_time(dt: datetime | None = None) -> bool:
    """Чи зараз робочі години для вихідних дзвінків."""
    if dt is None:
        dt = datetime.now(_KYIV)
    elif dt.tzinfo is None:
        dt = dt.replace(tzinfo=_KYIV)
    hours = WORK_HOURS.get(dt.weekday())
    if not hours:
        return False
    sh, sm, eh, em = hours
    start = dt.replace(hour=sh, minute=sm, second=0, microsecond=0)
    end   = dt.replace(hour=eh, minute=em, second=0, microsecond=0)
    return start <= dt < end


# ---------------------------------------------------------------------------
# КРОК 1 — Фетч та формування черги
# ---------------------------------------------------------------------------

async def fetch_and_queue_repeat_calls(days_before: int = 2) -> int:
    """
    Отримати пацієнтів з /followup_appts і додати в repeat_call_tasks.

    API повертає пацієнтів з ONDATE від завтра до +4 днів.
    days_before зберігається для сумісності але не передається в API.

    Алгоритм:
      1. GET /followup_appts → список пацієнтів
      2. Для кожного:
         a. Якщо задача вже є в БД (GLOBALKEY) — пропустити
         b. Перевірити наявність вільних слотів (doctorslots2)
         c. Якщо є слоти → додати в repeat_call_tasks

    Returns: кількість доданих задач
    """
    from services import clinic_api as _capi

    logger.info("[B2 FETCH] Отримуємо пацієнтів з /followup_appts")

    try:
        patients = await _capi.get_repeat_visit_reminders()
    except Exception as e:
        logger.error(f"[B2 FETCH] Помилка отримання списку: {e}")
        return 0

    logger.info(f"[B2 FETCH] Знайдено {len(patients)} пацієнтів від API")
    created = 0

    async with get_session() as session:
        for p in patients:
            source_visit_id = str(p.get("source_visit_id", ""))
            if not source_visit_id:
                continue

            # Пропускаємо якщо задача вже є
            existing = await session.scalar(
                select(RepeatCallTask).where(RepeatCallTask.source_visit_id == source_visit_id)
            )
            if existing:
                logger.debug(f"[B2 FETCH] Задача вже є: {source_visit_id}")
                continue

            # Перевірка наявності вільних слотів через doctorslots2
            doctor_name = str(p.get("doctor_name", ""))
            idpoint     = int(p.get("idpoint", 0))
            try:
                slots = await _capi.get_doctor_slots_b2(doctor_name, idpoint)
                avail_slots = [
                    s for s in (slots or [])
                    if datetime.fromisoformat(s["START_E"]).weekday() < 5
                ]
            except Exception as _e:
                logger.warning(f"[B2 FETCH] get_doctor_slots_b2 помилка (пропускаємо): {_e}")
                avail_slots = []

            if not avail_slots:
                logger.info(f"[B2 FETCH] Немає вільних слотів → не дзвонимо: {p.get('patient_name')}")
                continue

            # Парсимо дати
            prev_date = None
            prev_date_raw = p.get("previous_visit_date", "")
            if prev_date_raw:
                try:
                    prev_date = datetime.fromisoformat(str(prev_date_raw))
                except Exception:
                    pass

            repeat_date = None
            repeat_date_raw = p.get("repeat_visit_date", "")
            if repeat_date_raw:
                try:
                    repeat_date = datetime.fromisoformat(str(repeat_date_raw))
                except Exception:
                    pass

            patient_id = str(p.get("patient_id", ""))
            doctor_id  = str(p.get("doctor_id", ""))

            task = RepeatCallTask(
                id=uuid.uuid4(),
                source_visit_id=source_visit_id,
                patient_name=p.get("patient_name", ""),
                patient_phone=p.get("patient_phone", ""),
                patient_id=patient_id or None,
                previous_visit_date=prev_date,
                previous_doctor=p.get("previous_doctor", ""),
                doctor_id=doctor_id or None,
                doctor_name=doctor_name,
                doctor_specialization=p.get("doctor_specialization", ""),
                doctor_recommendation=p.get("doctor_recommendation", ""),
                repeat_visit_date=repeat_date,
                idpoint=idpoint or None,
                status=RepeatCallStatus.pending,
                attempts=0,
            )
            session.add(task)
            created += 1
            logger.info(f"[B2 FETCH] Додано задачу: {p.get('patient_name')} ({doctor_name}, IDPOINT={idpoint})")

    logger.info(f"[B2 FETCH] Створено нових задач: {created}")
    return created


# ---------------------------------------------------------------------------
# КРОК 2 — Виконання дзвінків
# ---------------------------------------------------------------------------

async def process_repeat_calls(max_retry: int = 2) -> None:
    """Обробити всі pending задачі з repeat_call_tasks."""
    from config import DEBUG, CALL_TIMEOUT_SEC, PAUSE_BETWEEN_CALLS_SEC

    logger.info("[B2 CALLS] Починаємо обдзвін щодо повторних візитів")

    # Cleanup: скидаємо in_progress від попереднього краша
    async with get_session() as session:
        result = await session.execute(
            select(RepeatCallTask).where(RepeatCallTask.status == RepeatCallStatus.in_progress)
        )
        stuck = result.scalars().all()
        for t in stuck:
            if t.attempts == 0:
                t.status = RepeatCallStatus.pending
                logger.warning(f"[B2 CALLS] Краш до дзвінка → pending: {t.patient_name}")
            else:
                t.status = RepeatCallStatus.failed
                t.result = RepeatCallResult.no_result
                logger.warning(f"[B2 CALLS] Краш під час дзвінка → failed: {t.patient_name}")

    async with get_session() as session:
        result = await session.execute(
            select(RepeatCallTask).where(RepeatCallTask.status == RepeatCallStatus.pending)
            .order_by(RepeatCallTask.created_at)
        )
        tasks = result.scalars().all()

    from config import ALLOWED_PHONES
    if ALLOWED_PHONES:
        tasks = [t for t in tasks if t.patient_phone in ALLOWED_PHONES]
        logger.info(f"[B2 CALLS] Після whitelist-фільтру (ALLOWED_PHONES) залишилось {len(tasks)} задач")

    if not tasks:
        logger.info("[B2 CALLS] Немає pending задач")
        return

    logger.info(f"[B2 CALLS] Задач для обдзвону: {len(tasks)}")

    from config import SKIP_SCHEDULER as _skip_sched
    for i, task in enumerate(tasks):
        if not _skip_sched and not _is_work_time():
            logger.info(f"[B2 CALLS] Вийшли за межі робочого часу — зупиняємось ({len(tasks) - i} залишилось)")
            break

        logger.info(f"[B2 CALLS] [{i+1}/{len(tasks)}] {task.patient_name}")
        try:
            await asyncio.wait_for(
                _execute_repeat_call(task, max_retry),
                timeout=CALL_TIMEOUT_SEC,
            )
        except asyncio.TimeoutError:
            logger.error(f"[B2 CALLS] Таймаут дзвінка ({CALL_TIMEOUT_SEC}с): {task.patient_name}")
            await _save_repeat_error(task, f"Таймаут дзвінка ({CALL_TIMEOUT_SEC}с)")
        except Exception as e:
            logger.error(f"[B2 CALLS] Помилка дзвінка {task.patient_name}: {e}", exc_info=True)
            await _save_repeat_error(task, str(e))

        if i < len(tasks) - 1:
            from config import PAUSE_BETWEEN_CALLS_SEC as _pause
            await asyncio.sleep(_pause)

    logger.info("[B2 CALLS] Обдзвін завершено")


async def _execute_repeat_call(task: RepeatCallTask, max_retry: int) -> None:
    """Виконати один дзвінок щодо повторного візиту."""
    from config import DEBUG

    visit_context = {
        "source_visit_id":     task.source_visit_id,
        "patient_name":        task.patient_name,
        "patient_phone":       task.patient_phone,
        "patient_id":          task.patient_id or "",
        "previous_visit_date": task.previous_visit_date.isoformat() if task.previous_visit_date else "",
        "previous_doctor":     task.previous_doctor or "",
        "doctor_id":           task.doctor_id or "",
        "doctor_name":         task.doctor_name,
        "doctor_specialization": task.doctor_specialization or "",
        "doctor_recommendation": task.doctor_recommendation or "",
        "repeat_visit_date":   task.repeat_visit_date.isoformat() if task.repeat_visit_date else "",
        "idpoint":             task.idpoint or 0,
    }

    # Позначаємо як in_progress перед ініціацією дзвінка
    async with get_session() as session:
        db_task = await session.get(RepeatCallTask, task.id)
        db_task.status = RepeatCallStatus.in_progress
        db_task.attempts += 1

    from agents.block2.repeat_reminder.agent import RepeatReminderAgent

    if DEBUG:
        agent = RepeatReminderAgent(visit_context)
        call_result = await agent.run()
        call_duration = agent.call_duration
        decline_category = getattr(agent, "_decline_category", "")
        new_visit_id = getattr(agent, "_new_visit_id", "")
        agent.cleanup()
    else:
        from services import binotel as _binotel
        await _binotel.shutdown()
        await _binotel.startup()

        agent = RepeatReminderAgent(visit_context)
        call_task    = asyncio.create_task(_binotel.initiate_call(task.patient_phone, task.patient_name))
        warmup_task  = asyncio.create_task(agent.warmup())
        binotel_call = None

        try:
            binotel_call = await call_task
            if binotel_call is None:
                warmup_task.cancel()
                try:
                    await warmup_task
                except (asyncio.CancelledError, Exception):
                    pass
                logger.warning(f"[B2 CALLS] Не відповів: {task.patient_name}")
                await _handle_no_answer(task, max_retry)
                return

            await warmup_task
            call_result = await agent.run(binotel_call)
            call_duration = agent.call_duration
            decline_category = getattr(agent, "_decline_category", "")
            new_visit_id = getattr(agent, "_new_visit_id", "")
            agent.cleanup()
            await asyncio.sleep(2.0)
        finally:
            if not call_task.done():
                call_task.cancel()
            if not warmup_task.done():
                warmup_task.cancel()
            if binotel_call is not None:
                try:
                    await binotel_call.hangup()
                except (asyncio.CancelledError, Exception):
                    pass

    await _save_repeat_result(task, call_result, call_duration, decline_category, new_visit_id)


async def _handle_no_answer(task: RepeatCallTask, max_retry: int) -> None:
    async with get_session() as session:
        db_task = await session.get(RepeatCallTask, task.id)
        if db_task.attempts >= max_retry:
            db_task.status = RepeatCallStatus.failed
            db_task.result = RepeatCallResult.no_answer
            db_task.completed_at = datetime.now(timezone.utc).replace(tzinfo=None)
            db_task.commentary = "Пацієнт не підняв трубку (вичерпано спроби)."
            logger.warning(f"[B2 CALLS] Вичерпано спроби: {task.patient_name}")
            await _send_api_status(task, "no_answer", "Пацієнт не підняв трубку (вичерпано спроби).")
        else:
            db_task.status = RepeatCallStatus.pending
            logger.info(f"[B2 CALLS] Retry {db_task.attempts}/{max_retry}: {task.patient_name}")


async def _save_repeat_result(
    task: RepeatCallTask,
    result_str: str,
    duration: float,
    decline_category: str = "",
    new_visit_id: str = "",
) -> None:
    commentary = _build_commentary(result_str, decline_category, new_visit_id)
    async with get_session() as session:
        db_task = await session.get(RepeatCallTask, task.id)
        db_task.status        = RepeatCallStatus.completed
        db_task.result        = _RESULT_MAP.get(result_str, RepeatCallResult.no_result)
        db_task.call_duration = duration
        db_task.completed_at  = datetime.now(timezone.utc).replace(tzinfo=None)
        db_task.commentary    = commentary[:512] if commentary else None
        if decline_category:
            db_task.decline_category = decline_category
        if new_visit_id:
            db_task.new_visit_id = new_visit_id
    logger.info(f"[B2 CALLS] Результат: {result_str} ({duration:.0f}с)")
    await _send_api_status(task, result_str, commentary)


async def _save_repeat_error(task: RepeatCallTask, error_msg: str) -> None:
    async with get_session() as session:
        db_task = await session.get(RepeatCallTask, task.id)
        db_task.status        = RepeatCallStatus.failed
        db_task.result        = RepeatCallResult.no_result
        db_task.error_message = error_msg[:500]


async def _send_api_status(task: RepeatCallTask, result_str: str, commentary: str) -> None:
    """Відправити результат дзвінка в API клініки.

    STATUS=1 (booked) — не викликаємо: booking вже виконано в tools.book_new_visit
                        через ai_save_event під час розмови.
    STATUS≠1 — викликаємо ai_savestate з GLOBALKEY + статус.
    """
    from config import SKIP_POST
    if SKIP_POST:
        logger.info(f"[B2 CALLS] SKIP_POST=true — не надсилаємо статус в API ({result_str})")
        return

    api_status = _STATUS_API_MAP.get(result_str)
    if api_status is None:
        return

    if api_status == 1:
        # Booking вже зареєстровано через ai_save_event в book_new_visit
        logger.info(f"[B2 CALLS] STATUS=1 (booked) — ai_save_event вже викликано під час розмови")
        return

    try:
        from services import clinic_api as _capi
        await _capi.update_repeat_call_status(task.source_visit_id, api_status, commentary)
    except Exception as e:
        logger.warning(f"[B2 CALLS] update_repeat_call_status помилка: {e}")


def _build_commentary(result: str, decline_category: str, new_visit_id: str) -> str:
    if result == "booked":
        return (f"Записано на повторний прийом. ID: {new_visit_id}." if new_visit_id
                else "Записано на повторний прийом.")
    if result == "declined":
        return (f"Відмовився. Причина: {decline_category}." if decline_category
                else "Відмовився від запису.")
    if result == "self_book":
        return "Пацієнт запишеться самостійно."
    if result == "no_answer":
        return "Пацієнт не підняв трубку."
    return "Незрозумілий результат або помилка."


# ---------------------------------------------------------------------------
# Архівування
# ---------------------------------------------------------------------------

async def archive_repeat_tasks() -> None:
    """Перенести виконані задачі в repeat_call_history."""
    async with get_session() as session:
        result = await session.execute(
            select(RepeatCallTask).where(
                RepeatCallTask.status.in_([
                    RepeatCallStatus.completed,
                    RepeatCallStatus.failed,
                    RepeatCallStatus.skipped,
                ])
            )
        )
        tasks = result.scalars().all()
        if not tasks:
            return
        for t in tasks:
            from db.models import RepeatCallHistory
            hist = RepeatCallHistory(
                id=t.id,
                source_visit_id=t.source_visit_id,
                patient_name=t.patient_name,
                patient_phone=t.patient_phone,
                patient_id=t.patient_id,
                previous_visit_date=t.previous_visit_date,
                previous_doctor=t.previous_doctor,
                doctor_id=t.doctor_id,
                doctor_name=t.doctor_name,
                doctor_specialization=t.doctor_specialization,
                doctor_recommendation=t.doctor_recommendation,
                repeat_visit_date=t.repeat_visit_date,
                status=t.status,
                result=t.result,
                decline_category=t.decline_category,
                new_visit_id=t.new_visit_id,
                attempts=t.attempts,
                call_duration=t.call_duration,
                created_at=t.created_at,
                completed_at=t.completed_at,
                error_message=t.error_message,
                commentary=t.commentary,
            )
            session.add(hist)
            await session.delete(t)
    logger.info(f"[B2 ARCHIVE] Архівовано {len(tasks)} задач")


# ---------------------------------------------------------------------------
# Основний job (APScheduler)
# ---------------------------------------------------------------------------

async def run_repeat_calls() -> None:
    """Основний щогодинний job."""
    from config import B2_CALL_DAYS_BEFORE, B2_MAX_RETRY, SKIP_SCHEDULER, SKIP_FETCH  # type: ignore[import-not-found]

    now = datetime.now(_KYIV)
    if not SKIP_SCHEDULER and not _is_work_time(now):
        logger.info(f"[B2 RUNNER] Поза робочим часом ({now.strftime('%H:%M')}) — пропускаємо")
        return

    logger.info(f"[B2 RUNNER] Запуск о {now.strftime('%H:%M')} (Пн-Нд)")

    # Фетч: формуємо чергу якщо ще не сформовано
    if SKIP_FETCH:
        logger.info("[B2 RUNNER] SKIP_FETCH=true — пропускаємо fetch, беремо existing задачі з БД")
    else:
        await fetch_and_queue_repeat_calls(days_before=B2_CALL_DAYS_BEFORE)

    # Дзвінки
    await process_repeat_calls(max_retry=B2_MAX_RETRY)

    # Архівування: після закінчення робочого часу або о 20:00
    if now.hour >= 19:
        await archive_repeat_tasks()


def setup_scheduler(scheduler: AsyncIOScheduler) -> None:
    """Зареєструвати job щогодини в робочі та вихідні дні (сам перевіряє _is_work_time)."""
    _tz = ZoneInfo("Europe/Kyiv")
    scheduler.add_job(
        run_repeat_calls,
        CronTrigger(hour="9-18", minute="0", day_of_week="mon-sun", timezone=_tz),
        id="b2_repeat_calls",
        name="Модуль 2.1: нагадування про повторні візити",
        replace_existing=True,
    )
    logger.info("[B2 RUNNER] Планувальник зареєстровано (щогодини 9-18, всі дні)")
