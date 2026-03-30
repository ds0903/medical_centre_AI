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


_UA_ORDINALS = {
    1: "перше", 2: "друге", 3: "третє", 4: "четверте", 5: "п'яте",
    6: "шосте", 7: "сьоме", 8: "восьме", 9: "дев'яте", 10: "десяте",
    11: "одинадцяте", 12: "дванадцяте", 13: "тринадцяте", 14: "чотирнадцяте",
    15: "п'ятнадцяте", 16: "шістнадцяте", 17: "сімнадцяте", 18: "вісімнадцяте",
    19: "дев'ятнадцяте", 20: "двадцяте",
    21: "двадцять перше", 22: "двадцять друге", 23: "двадцять третє",
    24: "двадцять четверте", 25: "двадцять п'яте", 26: "двадцять шосте",
    27: "двадцять сьоме", 28: "двадцять восьме", 29: "двадцять дев'яте",
    30: "тридцяте", 31: "тридцять перше",
}
# Зворотній словник: "дванадцяте" → 12 (для числового fallback у матчингу)
_NUM_FROM_ORDINAL: dict[str, int] = {v: k for k, v in _UA_ORDINALS.items()}


def _ua_ordinal(day: int) -> str:
    """Повний порядковий числівник середнього роду: 14 → 'чотирнадцяте'."""
    return _UA_ORDINALS.get(day, f"{day}-те")


def _natural_date_list(date_summaries: list[dict]) -> str:
    """
    Будує природній перелік дат для вимови AI — дати вже в ordinal форматі.

    Один місяць:  "дванадцяте, тринадцяте або чотирнадцяте жовтня"
    Різні місяці: "дванадцяте жовтня або п'яте листопада"
    """
    by_month: dict[str, list[str]] = {}
    order: list[str] = []
    for d in date_summaries:
        parts = d["date"].split()
        day_word = parts[0]   # вже ordinal: "дванадцяте"
        month = parts[1] if len(parts) > 1 else ""
        if month not in by_month:
            by_month[month] = []
            order.append(month)
        by_month[month].append(day_word)

    segments = []
    for month in order:
        items = by_month[month]
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
                    "Отримати доступні дати або вільний час для запису до лікаря. "
                    "БЕЗ параметра date: повертає список доступних ДАТ (без конкретних часів). "
                    "Викликати ОДРАЗУ як пацієнт сказав що хоче перенести. "
                    "З параметром date: повертає конкретний вільний час на обрану дату. "
                    "Викликати ПІСЛЯ того як пацієнт обрав дату. "
                    "НЕ викликати повторно якщо вільний час для цієї дати вже отримано — "
                    "пацієнт вже бачив список, одразу виклич reschedule_visit з потрібним slot_id."
                ),
                parameters=types.Schema(
                    type="OBJECT",
                    properties={
                        "date": types.Schema(
                            type="STRING",
                            description=(
                                "Дата для отримання доступного часу (напр. '12 жовтня'). "
                                "Якщо не вказана — повертає тільки список доступних дат."
                            ),
                        ),
                    },
                ),
            ),

            types.FunctionDeclaration(
                name="reschedule_visit",
                description=(
                    "Перенести візит на обраний пацієнтом час. "
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
                            description="ID вільного часу з результату get_available_slots",
                        ),
                    },
                    required=["visit_id", "slot_id"],
                ),
            ),

            types.FunctionDeclaration(
                name="cancel_visit",
                description=(
                    "Скасувати візит пацієнта. "
                    "Викликати ЛИШЕ якщо пацієнт явно відмовився від переносу або чітко попросив скасувати."
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
                    "ОБОВ'ЯЗКОВО викликати після прощання. "
                    "confirmed — пацієнт підтвердив запис; "
                    "rescheduled — запис перенесено; "
                    "cancelled — запис скасовано; "
                    "no_result — пацієнт не визначився або попросив передзвонити пізніше; "
                    "no_answer — пацієнт не відповів на дзвінок."
                ),
                parameters=types.Schema(
                    type="OBJECT",
                    properties={
                        "result": types.Schema(
                            type="STRING",
                            description="Підсумок розмови",
                            enum=["confirmed", "rescheduled", "cancelled", "no_result", "no_answer"],
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
                        "СТОП. Доступний час вже було показано пацієнту. "
                        "Пацієнт щойно назвав конкретний час — знайди потрібний slot_id "
                        "у списку slots[] і ОДРАЗУ виклич reschedule_visit(visit_id=..., slot_id=...). "
                        "НЕ перераховуй доступний час знову. НЕ питай ще раз який час."
                    ),
                }
                return result
            return cached["result"]

    logger.info(f"[TOOL] {function_name}({args})")

    if function_name == "confirm_visit":
        success = await clinic_api.confirm_visit(args["visit_id"])
        result = {
            "success": success,
            "message": (
                "Візит підтверджено. Тепер ОДРАЗУ виклич end_call(result='confirmed'). НЕ питай нічого більше."
                if success else
                "Помилка підтвердження. Скажи пацієнту і заверши дзвінок."
            ),
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
            return {"success": False, "message": "Вільного часу немає"}

        # Групуємо слоти по датах
        dates_map: dict[str, list[dict]] = {}
        for item in raw:
            dt = datetime.fromisoformat(item["START_E"])
            date_str = f"{_ua_ordinal(dt.day)} {_MONTHS_UA[dt.month - 1]}"
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

            # Захист: якщо початковий перелік дат вже надавався — перевіряємо одразу
            # чи запитана дата є в ньому. Без цього get_available_slots(date=16) шукає
            # в raw_slots і знаходить реальні записи (16-те реально є в БД клініки,
            # але поза першими 3 датами які ми показали пацієнту) → AI суперечить сама
            # собі (спочатку "є тільки 12, 13, 14", потім "на 16 є слоти вранці та в обід").
            _initial_call = call_cache.get("get_available_slots")
            if _initial_call:
                _initial_dates = [d["date"] for d in _initial_call["result"].get("available_dates", [])]
                _req_l = requested_date.lower().strip()
                _found = False
                for _id in _initial_dates:
                    if _req_l in _id.lower() or _id.lower() in _req_l:
                        _found = True
                        break
                if not _found:
                    # Числовий fallback: "16" або "16 жовтня" → шістнадцяте
                    try:
                        _req_day = int(_req_l.split()[0])
                        for _id in _initial_dates:
                            if _NUM_FROM_ORDINAL.get(_id.split()[0]) == _req_day:
                                _found = True
                                break
                    except (ValueError, IndexError):
                        pass
                if not _found:
                    _dates_str = ", ".join(_initial_dates) if _initial_dates else "відсутні"
                    logger.info(
                        f"[TOOL] get_available_slots(date={requested_date}) → поза переліком → відхилено"
                    )
                    return {
                        "success": False,
                        "message": (
                            f"На жаль, на {requested_date} вільних місць немає — "
                            f"ця дата поза нашим переліком. "
                            f"Є тільки: {_dates_str}. "
                            f"Скажи пацієнту це і запитай яку з цих дат обрати."
                        ),
                    }

            # Нечіткий пошук — "дванадцяте жовтня" або "12 жовтня" або просто "12"
            matched_slots = None
            _req = requested_date.lower().strip()
            for d, slots in dates_map.items():
                if _req in d.lower() or d.lower() in _req:
                    matched_slots = slots
                    break
            if not matched_slots:
                # Числовий fallback: AI передала "12" або "12 жовтня" → шукаємо ordinal ключ
                try:
                    _req_day = int(_req.split()[0])
                    for d, slots in dates_map.items():
                        if _NUM_FROM_ORDINAL.get(d.split()[0]) == _req_day:
                            matched_slots = slots
                            break
                except (ValueError, IndexError):
                    pass
            if not matched_slots:
                return {
                    "success": False,
                    "message": f"На {requested_date} вільного часу немає. "
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

            # Pre-обчислюємо як ТОЧНО озвучити кожну частину дня.
            # ≥ THRESHOLD слотів → діапазон ("з 8:30 по 11:45")
            # < THRESHOLD слотів → перелік ("8:30, 9:00, 9:30")
            # AI отримує готовий текст — не думає сама, просто зачитує.
            period_say: dict[str, str] = {}
            for p in active_periods:
                pslots = periods_map[p]
                if len(pslots) >= _PERIOD_THRESHOLD:
                    period_say[p] = f"з {pslots[0]['time']} по {pslots[-1]['time']}"
                else:
                    period_say[p] = ", ".join(s["time"] for s in pslots)

            if len(active_periods) == 1:
                # Єдина частина дня — одразу кажемо часи без питання про частину
                only = active_periods[0]
                period_prompt = (
                    f"Скажи пацієнту: 'є час {period_say[only]}' і запитай який зручніше. "
                    f"Тільки числа HH:MM, без словесного формату ('о другій' тощо)."
                )
            else:
                # Кілька частин — спочатку питаємо яка частина, потім кажемо готове формулювання
                guide = "; ".join(f"{p} → {period_say[p]}" for p in active_periods)
                period_prompt = (
                    f"Запитай пацієнта яка частина дня зручніше ({' / '.join(active_periods)}). "
                    f"Коли пацієнт обере — скажи ТІЛЬКИ: {guide}. "
                    f"Тільки числа HH:MM, без словесного формату."
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
            "message": (
                "Візит перенесено. Тепер ОДРАЗУ виклич end_call(result='rescheduled'). НЕ питай нічого більше."
                if success else
                "Помилка переносу. Скажи пацієнту і заверши дзвінок."
            ),
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
            "message": (
                "Візит скасовано. Тепер ОДРАЗУ виклич end_call(result='cancelled'). НЕ питай нічого більше."
                if success else
                "Помилка скасування. Скажи пацієнту і заверши дзвінок."
            ),
        }
        call_cache["cancel_visit"] = result
        return result

    if function_name == "end_call":
        result = {"success": True, "result": args.get("result", "no_result")}
        call_cache["end_call"] = result
        return result

    logger.error(f"[TOOL] Невідома функція: {function_name}")
    return {"error": f"Невідома функція: {function_name}"}
