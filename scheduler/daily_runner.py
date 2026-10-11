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
    ALLOWED_PHONES,
    CALL_TIMEOUT_SEC,
    DEBUG,
    DEDUP_PHONE_CALLS,
    MAX_CALL_ATTEMPTS,
    PAUSE_BETWEEN_CALLS_SEC,
    SCHEDULER_EVENING_CALL_END_HOUR,
    SCHEDULER_EVENING_CALL_HOUR,
    SCHEDULER_EVENING_CALL_MINUTE,
    SCHEDULER_EVENING_FETCH_HOUR,
    SCHEDULER_EVENING_FETCH_MINUTE,
    SCHEDULER_MORNING_CALL_END_HOUR,
    SCHEDULER_MORNING_CALL_HOUR,
    SCHEDULER_MORNING_CALL_MINUTE,
    SCHEDULER_MORNING_FETCH_HOUR,
    SCHEDULER_MORNING_FETCH_MINUTE,
    SCHEDULER_SAFETY_GUARDS,
    SKIP_SCHEDULER,
)
from db.database import get_session
from db.models import CallHistory, CallResult, CallStatus, CallTask
from services import call_log_sheets, clinic_api
from services.binotel import initiate_call

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Архівування — перенос call_tasks → call_history
# ---------------------------------------------------------------------------

def _task_to_history(task: CallTask) -> CallHistory:
    return CallHistory(
        id=task.id,
        visit_id=task.visit_id,
        patient_name=task.patient_name,
        patient_phone=task.patient_phone,
        doctor_name=task.doctor_name,
        doctor_specialization=task.doctor_specialization,
        place_id=task.place_id or "",
        visit_datetime=task.visit_datetime,
        message=task.message,
        appointment=task.appointment,
        preparation=task.preparation,
        call_session=task.call_session,
        status=task.status,
        result=task.result,
        attempts=task.attempts,
        call_duration=task.call_duration,
        created_at=task.created_at,
        completed_at=task.completed_at,
        error_message=task.error_message,
        operator_note=task.operator_note,
    )


async def _archive_call_tasks() -> None:
    """Перенести всі задачі з call_tasks в call_history і очистити чергу."""
    async with get_session() as session:
        result = await session.execute(select(CallTask))
        tasks = result.scalars().all()

        if not tasks:
            return

        for task in tasks:
            session.add(_task_to_history(task))
            await session.delete(task)

    logger.info(f"[ARCHIVE] Перенесено {len(tasks)} задач в call_history")


async def _archive_stale_tasks() -> None:
    """Архівує (переносить в call_history, НЕ видаляє) задачі створені НЕ сьогодні —
    захист на випадок якщо архівування минулого дня не відбулось (краш, пропущена вечірня сесія тощо).
    Викликається перед кожним fetch, щоб новий день завжди починався з чистої черги без втрати даних.
    """
    today_kyiv = datetime.now(ZoneInfo("Europe/Kyiv")).date()
    async with get_session() as session:
        result = await session.execute(select(CallTask))
        tasks = result.scalars().all()

        stale = [
            t for t in tasks
            if t.created_at.replace(tzinfo=timezone.utc).astimezone(ZoneInfo("Europe/Kyiv")).date() != today_kyiv
        ]
        if not stale:
            return

        for task in stale:
            session.add(_task_to_history(task))
            await session.delete(task)

    logger.info(f"[ARCHIVE] Архівовано {len(stale)} застарілих задач (не сьогоднішніх) перед fetch")


# ---------------------------------------------------------------------------
# КРОК 1 — Щодня о 8:00: отримати список візитів та зберегти задачі
# ---------------------------------------------------------------------------

async def fetch_and_schedule_calls(shift: int) -> None:
    """
    Отримує список пацієнтів через API та зберігає call_tasks у БД.

    shift=0 — ранкова сесія (пацієнти на сьогодні)
    shift=1 — вечірня сесія (пацієнти на завтра)
    """
    logger.info(f"[FETCH] Отримуємо записи (shift={shift})")

    try:
        await clinic_api.update_statuses(shift)
        visits = await clinic_api.get_upcoming_visits(shift)
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
                doctor_specialization=visit.get("doctor_specialization", ""),
                place_id=visit["place_id"],
                visit_datetime=datetime.fromisoformat(visit["visit_datetime"]),
                message=visit.get("message", ""),
                appointment=visit.get("appointment", ""),
                preparation=visit.get("preparation", ""),
                call_session="morning" if shift == 0 else "evening",
                status=CallStatus.pending,
                attempts=0,
            )
            session.add(task)
            created += 1

    logger.info(f"[FETCH] Створено нових задач: {created}")


# ---------------------------------------------------------------------------
# КРОК 2 — Щодня о 10:00: обдзвонити всіх пацієнтів
# ---------------------------------------------------------------------------

async def process_pending_calls(call_end_hour: int, archive: bool = False) -> None:
    """Обробляє всі pending задачі — робить дзвінки пацієнтам.
    Після основного циклу — один повторний прохід по тих хто не відповів.

    call_end_hour — година до якої дозволено запускати нові дзвінки
    archive       — чи архівувати задачі після завершення (тільки після вечірньої сесії)
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
            from sqlalchemy import case as sa_case
            _session_order = sa_case(
                (CallTask.call_session == "morning", 0),
                else_=1,
            )
            result = await session.execute(
                select(CallTask).where(CallTask.status == CallStatus.pending)
                .order_by(_session_order, CallTask.doctor_name, CallTask.visit_datetime)
            )
            tasks = result.scalars().all()

        if not tasks:
            logger.info(f"[CALLS] {label}: немає задач — завершуємо")
            break

        # Захист: дзвонимо тільки по задачах створених СЬОГОДНІ — щоб не подзвонити
        # випадково по залишках з учора якщо щось не очистилось перед fetch.
        if SCHEDULER_SAFETY_GUARDS:
            today_kyiv = datetime.now(_kyiv).date()
            before = len(tasks)
            tasks = [
                t for t in tasks
                if t.created_at.replace(tzinfo=timezone.utc).astimezone(_kyiv).date() == today_kyiv
            ]
            skipped_old = before - len(tasks)
            if skipped_old:
                logger.warning(
                    f"[CALLS] {label}: пропущено {skipped_old} задач створених НЕ сьогодні (захист SCHEDULER_SAFETY_GUARDS)"
                )
            if not tasks:
                logger.info(f"[CALLS] {label}: після фільтру 'тільки сьогодні' немає задач — завершуємо")
                break

        if ALLOWED_PHONES:
            tasks = [t for t in tasks if t.patient_phone in ALLOWED_PHONES]
            logger.info(f"[CALLS] {label}: після whitelist-фільтру залишилось {len(tasks)} задач")
        else:
            logger.info(f"[CALLS] {label}: задач {len(tasks)}")

        # Дедуплікація по телефону: 1 унікальний номер = 1 дзвінок за сесію
        linked_map: dict = {}  # primary task.id → [linked tasks]
        if DEDUP_PHONE_CALLS:
            seen: dict = {}
            deduped: list = []
            for task in tasks:
                phone = task.patient_phone
                if phone not in seen:
                    seen[phone] = task
                    deduped.append(task)
                    linked_map[task.id] = []
                else:
                    linked_map[seen[phone].id].append(task)
            skipped = len(tasks) - len(deduped)
            if skipped:
                logger.info(f"[CALLS] {label}: дедуп телефонів — прибрано {skipped} дублів")
            for primary_task in deduped:
                total = 1 + len(linked_map[primary_task.id])
                if total > 1:
                    specs = [primary_task.doctor_specialization or primary_task.doctor_name] + [
                        lt.doctor_specialization or lt.doctor_name for lt in linked_map[primary_task.id]
                    ]
                    logger.info(
                        f"[CALLS] Номер {primary_task.patient_phone}: {total} записів → {specs}"
                    )
            tasks = deduped

        stopped_by_time = False

        for i, task in enumerate(tasks):
            now_hour = datetime.now(_kyiv).hour
            if not SKIP_SCHEDULER and now_hour >= call_end_hour:
                logger.info(
                    f"[CALLS] Кінець робочого часу ({call_end_hour}:00). "
                    f"Залишилось: {len(tasks) - i}. Зупиняємось."
                )
                stopped_by_time = True
                break

            linked = linked_map.get(task.id, [])
            if linked:
                names = ", ".join(t.patient_name for t in linked)
                logger.info(f"[CALLS] [{i + 1}/{len(tasks)}] {task.patient_name} (+linked: {names})")
            else:
                logger.info(f"[CALLS] [{i + 1}/{len(tasks)}] {task.patient_name}")
            await _process_single_call(task, linked_tasks=linked)

            if i < len(tasks) - 1:
                await asyncio.sleep(PAUSE_BETWEEN_CALLS_SEC)

        if stopped_by_time:
            break

    # Shutdown Binotel після всіх дзвінків
    if not DEBUG:
        from services import binotel as _binotel
        await _binotel.shutdown()

    # Архівуємо тільки після вечірньої сесії — кінець робочого дня
    if archive:
        await _archive_call_tasks()
        logger.info("[CALLS] Обдзвін завершено (з архівуванням)")
    else:
        logger.info("[CALLS] Обдзвін завершено")


async def _mark_in_progress(task: CallTask) -> None:
    """Позначити задачу як in_progress і збільшити лічильник спроб.
    Викликається ПІСЛЯ того як Binotel підтвердив дзвінок (generalCallID отримано).
    """
    async with get_session() as session:
        db_task = await session.get(CallTask, task.id)
        db_task.status = CallStatus.in_progress
        db_task.attempts += 1


async def _process_single_call(task: CallTask, linked_tasks: list | None = None) -> None:
    """Виконати один дзвінок пацієнту (з таймаутом)."""

    # Перевіряємо чи слот ще актуальний перед дзвінком
    enabled = await clinic_api.check_call_enabled(task.visit_id)
    if not enabled:
        await _skip_by_checkstate(task)
        for lt in (linked_tasks or []):
            await _skip_by_checkstate(lt)
        return

    from services.clinic_api import _parse_caption
    doctor_specialization = task.doctor_specialization or _parse_caption(task.doctor_name)[1]

    linked_visit_list = []
    if linked_tasks:
        for lt in linked_tasks:
            lt_spec = lt.doctor_specialization or _parse_caption(lt.doctor_name)[1]
            linked_visit_list.append({
                "visit_id": lt.visit_id,
                "doctor_name": lt.doctor_name,
                "doctor_specialization": lt_spec,
                "visit_datetime": lt.visit_datetime.isoformat(),
                "appointment": lt.appointment or "",
                "preparation": lt.preparation or "",
            })
        logger.info(f"[CALLS] linked_visits для промпту: {[v['doctor_specialization'] for v in linked_visit_list]}")

    visit_context = {
        "visit_id": task.visit_id,
        "patient_name": task.patient_name,
        "patient_phone": task.patient_phone,
        "doctor_name": task.doctor_name,
        "doctor_specialization": doctor_specialization,
        "place_id": task.place_id,
        "visit_datetime": task.visit_datetime.isoformat(),
        "message": task.message or "",
        "appointment": task.appointment or "",
        "preparation": task.preparation or "",
        "linked_visits": linked_visit_list,
    }

    linked = linked_tasks or []
    try:
        await asyncio.wait_for(
            _execute_call(task, visit_context, linked),
            timeout=CALL_TIMEOUT_SEC,
        )
    except asyncio.TimeoutError:
        logger.error(
            f"[CALLS] Таймаут дзвінка ({CALL_TIMEOUT_SEC}с): {task.patient_name}"
        )
        await _save_error(task, f"Таймаут дзвінка ({CALL_TIMEOUT_SEC}с)")
        for lt in linked:
            await _skip_linked(lt)
    except Exception as e:
        logger.error(f"[CALLS] Помилка при дзвінку {task.patient_name}: {e}", exc_info=True)
        await _save_error(task, str(e))
        for lt in linked:
            await _skip_linked(lt)


async def _execute_call(task: CallTask, visit_context: dict, linked_tasks: list | None = None) -> None:
    """Внутрішня логіка одного дзвінка (обгорнута в таймаут _process_single_call)."""
    linked = linked_tasks or []
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
                will_retry = await _handle_no_answer(task)
                if not will_retry:
                    for lt in linked:
                        await _skip_linked(lt)
                # else: linked залишаються pending — retry-прохід згрупує їх знову через dedup
                return

            # Чекаємо warmup максимум 2с після того як пацієнт відповів.
            # Якщо TTS ще генерується — скасовуємо, cold path (Gemini привітається сама).
            if not warmup_task.done():
                try:
                    await asyncio.wait_for(asyncio.shield(warmup_task), timeout=2.0)
                except asyncio.TimeoutError:
                    logger.warning("[CALLS] Warmup не встиг за 2с після відповіді — cold path")
                    warmup_task.cancel()
                    try:
                        await warmup_task
                    except (asyncio.CancelledError, Exception):
                        pass
            else:
                await warmup_task
            call_result = await agent.run(binotel_call)
            call_duration = agent.call_duration
            call_commentary = agent.call_commentary
            agent.cleanup()
            await asyncio.sleep(2.0)
        finally:
            # Cleanup subtasks при таймауті або помилці
            if not call_task.done():
                call_task.cancel()
            if not warmup_task.done():
                warmup_task.cancel()
            if binotel_call is not None and call_result != "transferred":
                try:
                    await binotel_call.hangup()
                except (asyncio.CancelledError, Exception):
                    pass

    await _save_result(task, call_result, call_duration, call_commentary)

    for lt in linked:
        if call_result == "confirmed":
            await _confirm_linked_db(lt)
        elif call_result == "transferred":
            await _transfer_linked_db(lt)
        else:
            await _skip_linked(lt)


# ---------------------------------------------------------------------------
# Збереження результатів
# ---------------------------------------------------------------------------

_RESULT_MAP = {
    "confirmed":    CallResult.confirmed,
    "cancelled":    CallResult.cancelled,
    "rescheduled":  CallResult.rescheduled,
    "no_result":    CallResult.no_result,
    "no_answer":    CallResult.no_answer,
    "transferred":  CallResult.transferred,
    "wrong_number": CallResult.wrong_number,
}

# Маппінг результату дзвінка → STATUS для API клініки:
#   1 — Підтверджує, 2 — Відміняє, 3 — Не зрозумів, 4 — Не додзвонились
# "rescheduled" відсутній — reschedule_visit() вже передає слот до API,
# окремого update_event_status не потрібно.
_STATUS_API_MAP = {
    "confirmed":    1,
    "cancelled":    2,
    "no_result":    3,
    "no_answer":    4,
    "transferred":  5,
    "wrong_number": 6,
}


async def _save_result(task: CallTask, result_str: str, duration: float, commentary: str = "") -> None:
    async with get_session() as session:
        db_task = await session.get(CallTask, task.id)
        db_task.status = CallStatus.completed
        db_task.result = _RESULT_MAP.get(result_str, CallResult.no_answer)
        db_task.call_duration = duration
        db_task.completed_at = datetime.now(timezone.utc).replace(tzinfo=None)
        db_task.commentary = commentary[:512] if commentary else None
        attempts = db_task.attempts
    logger.info(f"[CALLS] Результат збережено: {result_str} ({duration:.0f}с)")

    api_status = _STATUS_API_MAP.get(result_str)
    if api_status is not None:
        await clinic_api.update_event_status(task.visit_id, api_status, commentary)

    # Журнал у Google Sheets — в кінці, щоб збій таблиці не затримав оновлення статусу в клініці
    call_log_sheets.log_call(task, result_str, duration, attempts=attempts)


async def _handle_no_answer(task: CallTask) -> bool:
    """Повертає True якщо буде retry, False якщо спроби вичерпано."""
    async with get_session() as session:
        db_task = await session.get(CallTask, task.id)
        attempts = db_task.attempts
        if attempts >= MAX_CALL_ATTEMPTS:
            db_task.status = CallStatus.failed
            db_task.result = CallResult.no_answer
            db_task.commentary = "Пацієнт не підняв трубку."
            db_task.completed_at = datetime.now(timezone.utc).replace(tzinfo=None)
            logger.warning(f"[CALLS] Вичерпано спроби для {task.patient_name}")
            await clinic_api.update_event_status(task.visit_id, 4, "Пацієнт не підняв трубку.")
            will_retry = False
        else:
            db_task.status = CallStatus.pending  # Спробуємо ще раз наступного разу
            logger.info(f"[CALLS] Спроба {attempts}/{MAX_CALL_ATTEMPTS} для {task.patient_name}")
            will_retry = True

    call_log_sheets.log_call(task, "no_answer", None, attempts=attempts)
    return will_retry


async def _save_error(task: CallTask, error_msg: str) -> None:
    async with get_session() as session:
        db_task = await session.get(CallTask, task.id)
        db_task.status = CallStatus.failed
        db_task.result = CallResult.no_result
        db_task.error_message = error_msg[:500]  # Обрізаємо щоб не переповнити поле
        attempts = db_task.attempts

    call_log_sheets.log_call(task, "error", None, attempts=attempts)


async def _skip_by_checkstate(task: CallTask) -> None:
    """Пропустити задачу бо ai_checkstate повернув ENABLED=0."""
    logger.info(f"[CALLS] ENABLED=0 — пропускаємо: {task.patient_name} visit={task.visit_id}")
    async with get_session() as session:
        db_task = await session.get(CallTask, task.id)
        db_task.status = CallStatus.skipped
        db_task.attempts = MAX_CALL_ATTEMPTS
        db_task.operator_note = "пропущено: ENABLED=0 (ai_checkstate)"
        db_task.completed_at = datetime.now(timezone.utc).replace(tzinfo=None)


async def _skip_linked(task: CallTask) -> None:
    """Пропускаємо linked task — пацієнт має кілька записів, дзвінок вже відбувся по primary."""
    logger.info(f"[CALLS] Пропускаємо linked: {task.patient_name} visit={task.visit_id}")
    async with get_session() as session:
        db_task = await session.get(CallTask, task.id)
        db_task.status = CallStatus.completed
        db_task.result = CallResult.no_result
        db_task.operator_note = "пропущено: дублікат номера телефону"
        db_task.completed_at = datetime.now(timezone.utc).replace(tzinfo=None)


async def _confirm_linked_db(task: CallTask) -> None:
    """Позначити linked task як confirmed у БД (без API-запиту — API викликається тільки по primary)."""
    logger.info(f"[CALLS] Linked confirmed: {task.patient_name} visit={task.visit_id}")
    async with get_session() as session:
        db_task = await session.get(CallTask, task.id)
        db_task.status = CallStatus.completed
        db_task.result = CallResult.confirmed
        db_task.operator_note = "підтверджено разом з основним записом"
        db_task.completed_at = datetime.now(timezone.utc).replace(tzinfo=None)


async def _transfer_linked_db(task: CallTask) -> None:
    """Позначити linked task як transferred у БД (без API-запиту)."""
    logger.info(f"[CALLS] Linked transferred: {task.patient_name} visit={task.visit_id}")
    async with get_session() as session:
        db_task = await session.get(CallTask, task.id)
        db_task.status = CallStatus.completed
        db_task.result = CallResult.transferred
        db_task.operator_note = "передано оператору разом з основним записом"
        db_task.completed_at = datetime.now(timezone.utc).replace(tzinfo=None)


# ---------------------------------------------------------------------------
# Планувальник (production)
# ---------------------------------------------------------------------------

async def _cleanup_logs() -> None:
    from pathlib import Path
    log_dir = Path("logs")
    if not log_dir.exists():
        return
    deleted = 0
    for f in log_dir.glob("calls_*.jsonl"):
        try:
            f.unlink()
            deleted += 1
        except Exception as e:
            logger.warning(f"[CLEANUP] Не вдалось видалити {f.name}: {e}")
    logger.info(f"[CLEANUP] Видалено {deleted} лог-файлів дзвінків")


def _is_workday() -> bool:
    """Пн-Пт = робочі дні. Сб-Нд = вихідні. Вимикається через SCHEDULER_SAFETY_GUARDS=false."""
    if not SCHEDULER_SAFETY_GUARDS:
        return True
    return datetime.now(ZoneInfo("Europe/Kyiv")).weekday() < 5  # 0=Пн, 5=Сб, 6=Нд


async def _has_pending_tasks(call_session: str) -> bool:
    """Перевіряє чи є в БД pending задачі для вказаної сесії."""
    async with get_session() as session:
        count = await session.scalar(
            select(CallTask).where(
                CallTask.status == CallStatus.pending,
                CallTask.call_session == call_session,
            ).limit(1)
        )
    return count is not None


async def _fetch_morning() -> None:
    if not _is_workday():
        logger.info("[SCHEDULER] Вихідний день — fetch пропускаємо")
        return
    # Архівуємо залишки з попереднього дня (не видаляємо — переносимо в історію) перед новим fetch
    if SCHEDULER_SAFETY_GUARDS:
        await _archive_stale_tasks()
    await fetch_and_schedule_calls(shift=0)

async def _fetch_evening() -> None:
    if not _is_workday():
        logger.info("[SCHEDULER] Вихідний день — fetch пропускаємо")
        return
    # Архівуємо залишки НЕ сьогоднішні (сьогоднішні ранкові задачі лишаються — їх ще не архівовано)
    if SCHEDULER_SAFETY_GUARDS:
        await _archive_stale_tasks()
    # Fetch сам пропустить дублікати — повторний виклик безпечний
    await fetch_and_schedule_calls(shift=1)

async def _process_morning_calls() -> None:
    from datetime import datetime
    from zoneinfo import ZoneInfo

    if not _is_workday():
        logger.info("[SCHEDULER] Вихідний день — ранковий обдзвін пропускаємо")
        return

    if await _has_pending_tasks("morning"):
        await process_pending_calls(call_end_hour=SCHEDULER_MORNING_CALL_END_HOUR, archive=False)
    else:
        logger.info("[SCHEDULER] morning_calls: немає pending задач — переходимо до вечірньої сесії")

    # Після ранкових — одразу вечірній fetch+дзвінки (без перевірки часу)
    now = datetime.now(ZoneInfo("Europe/Kyiv"))
    logger.info(f"[SCHEDULER] Ранкові завершено о {now.strftime('%H:%M')} — стартуємо вечірню сесію одразу")
    if not await _has_pending_tasks("evening"):
        await _fetch_evening()
    await _process_evening_calls()

async def _process_evening_calls() -> None:
    if not _is_workday():
        logger.info("[SCHEDULER] Вихідний день — вечірній обдзвін пропускаємо")
        return
    if not await _has_pending_tasks("evening"):
        logger.info("[SCHEDULER] evening_calls: немає pending задач — пропускаємо")
        return
    await process_pending_calls(call_end_hour=SCHEDULER_EVENING_CALL_END_HOUR, archive=True)


def create_scheduler() -> AsyncIOScheduler:
    _tz = ZoneInfo("Europe/Kyiv")
    scheduler = AsyncIOScheduler(timezone=_tz)

    scheduler.add_job(
        _fetch_morning,
        CronTrigger(hour=SCHEDULER_MORNING_FETCH_HOUR, minute=SCHEDULER_MORNING_FETCH_MINUTE, timezone=_tz),
        id="fetch_morning",
        name="Ранковий fetch (shift=0, сьогодні)",
        replace_existing=True,
    )
    scheduler.add_job(
        _process_morning_calls,
        CronTrigger(hour=SCHEDULER_MORNING_CALL_HOUR, minute=SCHEDULER_MORNING_CALL_MINUTE, timezone=_tz),
        id="calls_morning",
        name=f"Ранковий обдзвін ({SCHEDULER_MORNING_CALL_HOUR}:{SCHEDULER_MORNING_CALL_MINUTE:02d}-{SCHEDULER_MORNING_CALL_END_HOUR}:00)",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )
    scheduler.add_job(
        _fetch_evening,
        CronTrigger(hour=SCHEDULER_EVENING_FETCH_HOUR, minute=SCHEDULER_EVENING_FETCH_MINUTE, timezone=_tz),
        id="fetch_evening",
        name="Вечірній fetch (shift=1, завтра)",
        replace_existing=True,
    )
    scheduler.add_job(
        _process_evening_calls,
        CronTrigger(hour=SCHEDULER_EVENING_CALL_HOUR, minute=SCHEDULER_EVENING_CALL_MINUTE, timezone=_tz),
        id="calls_evening",
        name=f"Вечірній обдзвін ({SCHEDULER_EVENING_CALL_HOUR}:{SCHEDULER_EVENING_CALL_MINUTE:02d}-{SCHEDULER_EVENING_CALL_END_HOUR}:00)",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )
    scheduler.add_job(
        _cleanup_logs,
        CronTrigger(hour=0, minute=0, timezone=_tz),
        id="cleanup_logs",
        name="Очищення лог-файлів дзвінків (00:00)",
        replace_existing=True,
    )

    logger.info(
        f"[SCHEDULER] Ранок: fetch о {SCHEDULER_MORNING_FETCH_HOUR}:{SCHEDULER_MORNING_FETCH_MINUTE:02d}, "
        f"дзвінки о {SCHEDULER_MORNING_CALL_HOUR}:{SCHEDULER_MORNING_CALL_MINUTE:02d}–{SCHEDULER_MORNING_CALL_END_HOUR}:00 | "
        f"Вечір: fetch о {SCHEDULER_EVENING_FETCH_HOUR}:{SCHEDULER_EVENING_FETCH_MINUTE:02d}, "
        f"дзвінки о {SCHEDULER_EVENING_CALL_HOUR}:{SCHEDULER_EVENING_CALL_MINUTE:02d}–{SCHEDULER_EVENING_CALL_END_HOUR}:00 (Europe/Kyiv)"
    )
    return scheduler
