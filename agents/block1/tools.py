"""
Function calling tools для Gemini Live API — Блок 1: Нагадування про візити.

Gemini викликає функції під час розмови.
Ми виконуємо їх і повертаємо результат назад в сесію.
"""

import logging
import time
from datetime import datetime
from typing import Any

from google.genai import types

from config import GEMINI_LOGS, LOG_FILE
from services import clinic_api

logger = logging.getLogger(__name__)

_MONTHS_UA = [
    "січня", "лютого", "березня", "квітня", "травня", "червня",
    "липня", "серпня", "вересня", "жовтня", "листопада", "грудня",
]


def _fmt_dt(iso: str) -> str:
    dt = datetime.fromisoformat(iso)
    return f"{dt.day} {_MONTHS_UA[dt.month - 1]} о {dt.strftime('%H:%M')}"


def _slot_period(hour: int) -> str:
    """Розбиваємо день на 3 частини: ранок / обід / вечір."""
    if hour < 12:
        return "ранок"
    elif hour < 17:
        return "обід"
    else:
        return "вечір"


def _ordinal_suffix(day: int) -> str:
    """Закінчення порядкового числівника: 1→ше, 2→ге, 3→тє, решта→те."""
    if day == 1:
        return "ше"
    if day == 2:
        return "ге"
    if day == 3:
        return "тє"
    return "те"


def _natural_date_list(date_summaries: list[dict]) -> str:
    """
    Будує природній перелік дат для вимови AI — всі з закінченням '-те'.

    Один місяць:  "12-те, 13-те, 14-те, 15-те, 16-те або 17-те жовтня"
    Різні місяці: "12-те жовтня або 5-те листопада"
    """
    by_month: dict[str, list[int]] = {}
    order: list[str] = []
    for d in date_summaries:
        parts = d["date"].split()
        day = int(parts[0])
        month = parts[1] if len(parts) > 1 else ""
        if month not in by_month:
            by_month[month] = []
            order.append(month)
        by_month[month].append(day)

    segments = []
    for month in order:
        days = by_month[month]
        items = [f"{d}-{_ordinal_suffix(d)}" for d in days]
        if len(items) == 1:
            segments.append(f"{items[0]} {month}")
        else:
            segments.append(f"{', '.join(items[:-1])} або {items[-1]} {month}")
    return ", ".join(segments)


# Мінімальна кількість слотів у частині дня для показу діапазону замість переліку
_PERIOD_THRESHOLD = 6


# ---------------------------------------------------------------------------
# Визначення інструментів для Gemini
# ---------------------------------------------------------------------------

TOOLS = [
    types.Tool(
        function_declarations=[

            types.FunctionDeclaration(
                name="confirm_visit",
                description=(
                    "Підтвердити візит пацієнта. "
                    "Викликати коли пацієнт чітко підтвердив що прийде."
                ),
                parameters=types.Schema(
                    type="OBJECT",
                    properties={
                        "visit_id": types.Schema(
                            type="STRING",
                            description="ID візиту для підтвердження",
                        ),
                    },
                    required=["visit_id"],
                ),
            ),

            types.FunctionDeclaration(
                name="get_available_slots",
                description=(
                    "Отримати вільні дати або слоти для запису до лікаря. "
                    "БЕЗ параметра date: повертає список доступних ДАТ (без конкретних часів). "
                    "Викликати ОДРАЗУ як пацієнт сказав що хоче перенести. "
                    "З параметром date: повертає конкретні СЛОТИ на обрану дату. "
                    "Викликати ПІСЛЯ того як пацієнт обрав дату. "
                    "НЕ викликати повторно якщо слоти для цієї дати вже отримані — "
                    "пацієнт вже бачив список, одразу виклич reschedule_visit з потрібним slot_id."
                ),
                parameters=types.Schema(
                    type="OBJECT",
                    properties={
                        "date": types.Schema(
                            type="STRING",
                            description=(
                                "Дата для отримання слотів (напр. '12 жовтня'). "
                                "Якщо не вказана — повертає тільки список доступних дат."
                            ),
                        ),
                    },
                ),
            ),

            types.FunctionDeclaration(
                name="reschedule_visit",
                description=(
                    "Перенести візит на обраний пацієнтом слот. "
                    "Викликати лише після того як пацієнт підтвердив конкретний час."
                ),
                parameters=types.Schema(
                    type="OBJECT",
                    properties={
                        "visit_id": types.Schema(
                            type="STRING",
                            description="ID поточного візиту",
                        ),
                        "slot_id": types.Schema(
                            type="STRING",
                            description="ID слоту з результату get_available_slots",
                        ),
                    },
                    required=["visit_id", "slot_id"],
                ),
            ),

            types.FunctionDeclaration(
                name="cancel_visit",
                description=(
                    "Скасувати візит пацієнта. "
                    "Викликати ЛИШЕ якщо пацієнт чітко сказав 'скасуйте', 'видаліть', 'анулюйте' запис."
                ),
                parameters=types.Schema(
                    type="OBJECT",
                    properties={
                        "visit_id": types.Schema(
                            type="STRING",
                            description="ID візиту для скасування",
                        ),
                        "reason": types.Schema(
                            type="STRING",
                            description="Причина скасування якщо пацієнт вказав",
                        ),
                    },
                    required=["visit_id"],
                ),
            ),

types.FunctionDeclaration(
                name="end_call",
                description=(
                    "Завершити розмову. "
                    "ОБОВ'ЯЗКОВО викликати після прощання з пацієнтом з відповідним результатом."
                ),
                parameters=types.Schema(
                    type="OBJECT",
                    properties={
                        "result": types.Schema(
                            type="STRING",
                            description="Підсумок розмови",
                            enum=["confirmed", "cancelled", "no_result", "no_answer"],
                        ),
                    },
                    required=["result"],
                ),
            ),

        ]
    )
]


# ---------------------------------------------------------------------------
# Обробка викликів
# ---------------------------------------------------------------------------

# Функції які можна викликати лише 1 раз за дзвінок
_ONE_TIME_TOOLS = {"confirm_visit", "cancel_visit", "reschedule_visit", "end_call"}
# TTL кешу для get_available_slots (секунди)
_SLOTS_CACHE_TTL = 60


async def handle_tool_call(
    function_name: str,
    args: dict,
    visit_context: dict,
    call_cache: dict | None = None,
) -> dict[str, Any]:
    """
    Виконати tool call від Gemini та повернути результат.
    call_cache — словник що живе протягом одного дзвінка, захищає від дублів.

    Returns:
        dict який Gemini отримає як відповідь функції
    """
    if call_cache is None:
        call_cache = {}

    # Захист від дублікатів
    if function_name in _ONE_TIME_TOOLS and function_name in call_cache:
        logger.warning(f"[TOOL] {function_name} вже викликався — повертаємо кешований результат")
        return call_cache[function_name]

    if function_name == "get_available_slots":
        requested_date = args.get("date", "").strip()
        cache_key = f"get_available_slots:{requested_date}" if requested_date else "get_available_slots"
        cached = call_cache.get(cache_key)
        if cached and time.monotonic() - cached["_ts"] < _SLOTS_CACHE_TTL:
            logger.warning(f"[TOOL] get_available_slots — кеш '{cache_key}' (TTL {_SLOTS_CACHE_TTL}с)")
            if requested_date:
                cache_age = time.monotonic() - cached["_ts"]
                if cache_age < 3.0:
                    # Подвійний виклик у тому ж батчі (Gemini artifact, < 3с від першого виклику).
                    # СТОП тут хибний: пацієнт ще не бачив слоти і не називав конкретний час.
                    # Повертаємо звичайний результат щоб ghost-тур не казав "яку годину?"
                    # замість правильного переліку слотів.
                    logger.debug(f"[TOOL] get_available_slots — той самий батч ({cache_age:.2f}с) → без СТОП")
                    return cached["result"]
                # Повторний виклик з окремого туру — пацієнт вже бачив ці слоти
                # і щойно назвав конкретний час. Підказуємо Gemini одразу reschedule.
                result = {
                    **cached["result"],
                    "message": (
                        "СТОП. Ці слоти вже були показані пацієнту. "
                        "Пацієнт щойно назвав конкретний час — знайди потрібний slot_id "
                        "у списку slots[] і ОДРАЗУ виклич reschedule_visit(visit_id=..., slot_id=...). "
                        "НЕ перераховуй слоти знову. НЕ питай ще раз який час."
                    ),
                }
                return result
            return cached["result"]

    logger.info(f"[TOOL] {function_name}({args})")

    if function_name == "confirm_visit":
        success = await clinic_api.confirm_visit(args["visit_id"])
        result = {
            "success": success,
            "message": "Візит підтверджено" if success else "Помилка підтвердження, спробуйте пізніше",
        }
        call_cache["confirm_visit"] = result
        return result

    if function_name == "get_available_slots":
        # Використовуємо prefetch-кеш якщо він вже є (заповнений під час привітання).
        # Другий виклик (з датою) також бере з кешу — без повторного HTTP запиту.
        raw = call_cache.get("__raw_slots")
        if raw is None:
            raw = await clinic_api.get_doctor_slots(visit_context["doctor_name"])
            if raw:
                call_cache["__raw_slots"] = raw
        if not raw:
            return {"success": False, "message": "Вільних слотів немає"}

        # Групуємо слоти по датах
        dates_map: dict[str, list[dict]] = {}
        for item in raw:
            dt = datetime.fromisoformat(item["START_E"])
            date_str = f"{dt.day} {_MONTHS_UA[dt.month - 1]}"
            time_str = f"{dt.hour}:{dt.minute:02d}"  # без ведучого нуля: 8:30, 9:00
            period = _slot_period(dt.hour)
            slot = {
                "slot_id": str(item["ID"]),
                "date": date_str,
                "time": time_str,
                "period": period,
            }
            dates_map.setdefault(date_str, []).append(slot)

        requested_date = args.get("date", "").strip()

        if requested_date:
            # КРОК 2: пацієнт обрав дату → повертаємо слоти тільки за цю дату
            # Шукаємо дату (нечіткий пошук — "15" матчить "15 жовтня")
            matched_slots = None
            for d, slots in dates_map.items():
                if requested_date in d or d in requested_date:
                    matched_slots = slots
                    break
            if not matched_slots:
                return {
                    "success": False,
                    "message": f"На {requested_date} вільних слотів немає. "
                               f"Доступні дати: {', '.join(dates_map.keys())}",
                }
            # Групуємо слоти по частинах дня: ранок / обід / вечір
            period_order = ["ранок", "обід", "вечір"]
            periods_map: dict[str, list[dict]] = {p: [] for p in period_order}
            for s in matched_slots:
                periods_map[s["period"]].append(s)

            # Будуємо текстовий опис частин дня
            # Якщо в частині ≥ THRESHOLD слотів — показуємо кількість і діапазон (не перелічуємо)
            # Якщо < THRESHOLD — перелічуємо конкретні часи
            period_parts = []
            for pname in period_order:
                pslots = periods_map[pname]
                if not pslots:
                    continue
                if len(pslots) >= _PERIOD_THRESHOLD:
                    period_parts.append(
                        f"{pname}: {len(pslots)} вікон ({pslots[0]['time']}–{pslots[-1]['time']})"
                    )
                else:
                    times = ", ".join(s["time"] for s in pslots)
                    period_parts.append(f"{pname}: {times}")

            periods_text = "; ".join(period_parts)
            active_periods = [p for p in period_order if periods_map[p]]
            # Якщо є великі частини (≥ THRESHOLD) — пропонуємо пацієнту спочатку вибрати частину дня
            large_periods = [p for p in active_periods if len(periods_map[p]) >= _PERIOD_THRESHOLD]
            if large_periods and len(active_periods) > 1:
                period_prompt = (
                    f"Запитай пацієнта яка частина дня зручніше ({' / '.join(active_periods)}), "
                    f"потім назви конкретні часи тільки для обраної частини у форматі HH:MM (наприклад: 14:00, 14:20). "
                    f"НЕ перелічуй всі часи одразу. НЕ вживай словесний формат ('о другій', 'о пів на третю') — тільки числа."
                )
            else:
                period_prompt = (
                    f"Перерахуй пацієнту доступні часи у форматі HH:MM (наприклад: 14:00, 14:20) і запитай який зручніше. "
                    f"НЕ кажи часовий діапазон ('з X до Y'). "
                    f"НЕ вживай словесний формат ('о другій', 'о пів на третю') — тільки числа HH:MM."
                )

            logger.info(f"[TOOL] get_available_slots(date={requested_date}) → {len(matched_slots)} слотів")
            result = {
                "success": True,
                "message": (
                    f"На {requested_date} є {len(matched_slots)} вільних вікон: {periods_text}. "
                    f"Якщо пацієнт хоче час якого немає в списку — скажи що такого немає і назви найближчий. "
                    f"{period_prompt}"
                ),
                "date": requested_date,
                "slots": matched_slots,
                "total_slots": len(matched_slots),
            }
            # Кешуємо з ключем що включає дату
            call_cache[f"get_available_slots:{requested_date}"] = {"result": result, "_ts": time.monotonic()}
            return result

        # КРОК 1: без дати → повертаємо тільки список дат (БЕЗ часових діапазонів!)
        # НЕ передаємо time_range — AI округлює діапазони і вводить пацієнта в оману
        # (каже "з 9 до 13" а реально 9:20–12:30, або "на 9" а є тільки 9:20).
        # Точні часи покажемо в Кроці 2 коли пацієнт обере дату.
        date_summaries = []
        for d, slots in list(dates_map.items())[:3]:
            date_summaries.append({
                "date": d,
                "count": len(slots),
            })
        dates_list = ", ".join(d["date"] for d in date_summaries)
        logger.info(
            f"[TOOL] get_available_slots → {len(raw)} слотів на {len(date_summaries)} дат: {dates_list}"
        )
        natural = _natural_date_list(date_summaries)
        result = {
            "success": True,
            "message": (
                f"Скажи пацієнту ДОСЛІВНО цей перелік дат (не змінюй порядок і закінчення): «{natural}». "
                f"НЕ ЗГАДУЙ конкретні часи зараз — тільки дати. "
                f"Після переліку запитай на яку дату зручніше. "
                f"Коли пацієнт обере дату — виклич get_available_slots(date=...) щоб отримати конкретні часи."
            ),
            "available_dates": date_summaries,
            "total_dates": len(date_summaries),
        }
        call_cache["get_available_slots"] = {"result": result, "_ts": time.monotonic()}
        return result

    if function_name == "reschedule_visit":
        success = await clinic_api.reschedule_visit(
            visit_id=args["visit_id"],
            slot_id=args["slot_id"],
        )
        result = {
            "success": success,
            "message": "Візит перенесено" if success else "Помилка переносу, спробуйте пізніше",
        }
        call_cache["reschedule_visit"] = result
        return result

    if function_name == "cancel_visit":
        success = await clinic_api.cancel_visit(
            visit_id=args["visit_id"],
            reason=args.get("reason"),
        )
        result = {
            "success": success,
            "message": "Візит скасовано" if success else "Помилка скасування, спробуйте пізніше",
        }
        call_cache["cancel_visit"] = result
        return result

    if function_name == "end_call":
        result = {"success": True, "result": args.get("result", "no_result")}
        call_cache["end_call"] = result
        return result

    logger.error(f"[TOOL] Невідома функція: {function_name}")
    return {"error": f"Невідома функція: {function_name}"}
