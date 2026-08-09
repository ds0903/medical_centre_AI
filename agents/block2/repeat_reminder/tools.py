"""
Function calling tools для Модуля 2.1 — нагадування про повторні візити.

Відмінності від block1/tools.py:
  get_available_slots  — слоти за doctor_id (не за visit_id)
  book_new_visit       — створює НОВИЙ запис (clinic_api.create_new_visit)
  decline_with_reason  — фіксує відмову з однієї з 26 категорій
  mark_self_book       — пацієнт записується самостійно
  end_call             — завершити дзвінок
"""

import logging
import time
from datetime import datetime
from typing import Any

from google.genai import types

from agents.block2.repeat_reminder.prompts import DECLINE_CATEGORIES

logger = logging.getLogger(__name__)

# Українські місяці та дні тижня (аналогічно block1/tools.py)
_MONTHS_UA = [
    "січня", "лютого", "березня", "квітня", "травня", "червня",
    "липня", "серпня", "вересня", "жовтня", "листопада", "грудня",
]
_WEEKDAYS_UA = [
    "понеділок", "вівторок", "середа", "четвер",
    "п'ятниця", "субота", "неділя",
]

_ORDINALS_UA = [
    "", "першого", "другого", "третього", "четвертого", "п'ятого",
    "шостого", "сьомого", "восьмого", "дев'ятого", "десятого",
    "одинадцятого", "дванадцятого", "тринадцятого", "чотирнадцятого",
    "п'ятнадцятого", "шістнадцятого", "сімнадцятого", "вісімнадцятого",
    "дев'ятнадцятого", "двадцятого", "двадцять першого", "двадцять другого",
    "двадцять третього", "двадцять четвертого", "двадцять п'ятого",
    "двадцять шостого", "двадцять сьомого", "двадцять восьмого",
    "двадцять дев'ятого", "тридцятого", "тридцять першого",
]


def _ua_ordinal(day: int) -> str:
    if 1 <= day <= 31:
        return _ORDINALS_UA[day]
    return str(day)


_HOURS_UA = [
    "опівночі", "першій", "другій", "третій", "четвертій", "п'ятій",
    "шостій", "сьомій", "восьмій", "дев'ятій", "десятій", "одинадцятій",
    "дванадцятій", "тринадцятій", "чотирнадцятій", "п'ятнадцятій",
    "шістнадцятій", "сімнадцятій", "вісімнадцятій", "дев'ятнадцятій",
    "двадцятій", "двадцять першій", "двадцять другій", "двадцять третій",
]
_MINUTES_UA = {
    0: "", 5: "п'ять", 10: "десять", 15: "п'ятнадцять",
    20: "двадцять", 25: "двадцять п'ять", 30: "тридцять",
    35: "тридцять п'ять", 40: "сорок", 45: "сорок п'ять",
    50: "п'ятдесят", 55: "п'ятдесят п'ять",
}


def _format_hour_ua(hour: int, minute: int) -> str:
    h = _HOURS_UA[hour] if 0 <= hour < 24 else str(hour)
    m = _MINUTES_UA.get(minute, str(minute))
    return f"{h} {m}".strip() if m else h


def _natural_date_list(date_summaries: list[dict]) -> str:
    if not date_summaries:
        return "вільних дат немає"
    parts = [f"{d['date']} ({d['weekday']})" for d in date_summaries]
    if len(parts) == 1:
        return parts[0]
    return ", ".join(parts[:-1]) + " або " + parts[-1]


TOOLS = [
    types.Tool(
        function_declarations=[

            types.FunctionDeclaration(
                name="get_available_slots",
                description=(
                    "Отримати вільні слоти для повторного запису до лікаря. "
                    "БЕЗ аргументів: повертає до 5 конкретних варіантів дата+час одразу. "
                    "З date: повертає часи на конкретно обраний день (якщо пацієнт вже назвав дату)."
                ),
                parameters=types.Schema(
                    type="OBJECT",
                    properties={
                        "date": types.Schema(
                            type="STRING",
                            description="Дата у форматі 'DD ММММ' — тільки якщо пацієнт сам назвав конкретну дату",
                        ),
                    },
                ),
            ),

            types.FunctionDeclaration(
                name="book_new_visit",
                description=(
                    "Створити НОВИЙ запис пацієнта на повторний прийом. "
                    "Викликати тільки після явного підтвердження пацієнта."
                ),
                parameters=types.Schema(
                    type="OBJECT",
                    properties={
                        "slot_id": types.Schema(
                            type="STRING",
                            description="ID вільного слоту (з результату get_available_slots)",
                        ),
                    },
                    required=["slot_id"],
                ),
            ),

            types.FunctionDeclaration(
                name="decline_with_reason",
                description=(
                    "Пацієнт відмовився від запису. "
                    "Зафіксувати відмову з категорією причини. "
                    "Викликати ОДИН РАЗ після того як пацієнт пояснив причину або відмовився пояснювати."
                ),
                parameters=types.Schema(
                    type="OBJECT",
                    properties={
                        "category": types.Schema(
                            type="STRING",
                            description=(
                                f"Категорія причини відмови. Одна з: "
                                f"{', '.join(repr(c) for c in DECLINE_CATEGORIES)}"
                            ),
                        ),
                        "raw_reason": types.Schema(
                            type="STRING",
                            description="Дослівна відповідь пацієнта (для логів)",
                        ),
                    },
                    required=["category"],
                ),
            ),

            types.FunctionDeclaration(
                name="mark_self_book",
                description=(
                    "Пацієнт планує записатися самостійно або передзвонити в клініку. "
                    "Завершити дзвінок без повторних спроб від боту."
                ),
                parameters=types.Schema(type="OBJECT", properties={}),
            ),

            types.FunctionDeclaration(
                name="end_call",
                description="Завершити дзвінок з вказаним результатом.",
                parameters=types.Schema(
                    type="OBJECT",
                    properties={
                        "result": types.Schema(
                            type="STRING",
                            enum=["booked", "declined", "self_book", "no_answer", "no_result"],
                            description="Підсумковий результат дзвінка",
                        ),
                    },
                    required=["result"],
                ),
            ),

        ]
    )
]


async def handle_tool_call(
    function_name: str,
    args: dict,
    visit_context: dict,
    call_cache: dict | None = None,
) -> dict[str, Any]:
    """Виконати tool call від Gemini."""
    if call_cache is None:
        call_cache = {}

    logger.info(f"[TOOL 2.1] {function_name}({args})")

    # ── get_available_slots ─────────────────────────────────────────────────
    if function_name == "get_available_slots":
        date_arg: str = args.get("date", "")

        raw: list[dict] | None = call_cache.get("__raw_slots")
        if raw is None:
            try:
                from services import clinic_api as _capi
                doctor_name = visit_context.get("doctor_name", "")
                idpoint     = int(visit_context.get("idpoint", 0))
                raw = await _capi.get_doctor_slots_b2(doctor_name, idpoint)
                call_cache["__raw_slots"] = raw if raw is not None else []
            except Exception as e:
                logger.error(f"[TOOL 2.1] get_doctor_slots_b2 помилка: {e!r}")
                raw = []

        # Фільтр вихідних
        filtered: list[dict] = []
        for item in (raw or []):
            try:
                dt = datetime.fromisoformat(item["START_E"])
                if dt.weekday() >= 5:
                    continue
                filtered.append(item)
            except Exception:
                pass

        if not filtered:
            result = {
                "success": False,
                "available_dates": [],
                "message": (
                    "Вільних слотів наразі немає. "
                    "Запропонуй пацієнту самостійно зателефонувати в клініку або виклич mark_self_book."
                ),
            }
            call_cache["get_available_slots"] = {"result": result, "_ts": time.monotonic()}
            return result

        # Без дати → повернути конкретні слоти (дата + час разом, без двох кроків)
        if not date_arg:
            slots_list = []
            for item in filtered[:5]:
                dt = datetime.fromisoformat(item["START_E"])
                date_str = f"{_ua_ordinal(dt.day)} {_MONTHS_UA[dt.month - 1]}"
                hour_str = _format_hour_ua(dt.hour, dt.minute)
                slots_list.append({
                    "slot_id": str(item["ID"]),
                    "date": date_str,
                    "weekday": _WEEKDAYS_UA[dt.weekday()],
                    "time": f"{dt.hour}:{dt.minute:02d}",
                    "label": f"{date_str} о {hour_str}",
                })

            if not slots_list:
                result = {
                    "success": False,
                    "available_dates": [],
                    "message": (
                        "Вільних слотів наразі немає. "
                        "Запропонуй пацієнту самостійно зателефонувати в клініку або виклич mark_self_book."
                    ),
                }
                call_cache["get_available_slots"] = {"result": result, "_ts": time.monotonic()}
                return result

            labels = ", ".join(s["label"] for s in slots_list)
            result = {
                "success": True,
                "message": (
                    f"Скажи пацієнту ДОСЛІВНО: «Є вільний час: {labels}. Який вам зручний?» "
                    "Коли пацієнт обере конкретний час — одразу виклич book_new_visit(slot_id='...') "
                    "з відповідним slot_id зі списку нижче. НЕ перепитуй зайвого."
                ),
                "slots": slots_list,
            }
            call_cache["get_available_slots"] = {"result": result, "_ts": time.monotonic()}
            return result

        # З датою → повернути слоти на конкретний день
        matched_slots: list[dict] = []
        for item in filtered:
            dt = datetime.fromisoformat(item["START_E"])
            date_str = f"{_ua_ordinal(dt.day)} {_MONTHS_UA[dt.month - 1]}"
            # Порівнюємо гнучко: "четвертого квітня", "4 квітня" тощо
            if (date_arg.lower() in date_str.lower()
                    or date_str.lower() in date_arg.lower()
                    or str(dt.day) in date_arg):
                matched_slots.append(item)

        if not matched_slots:
            result = {
                "success": False,
                "message": f"На обрану дату «{date_arg}» вільного часу немає. Запропонуй іншу дату з переліку.",
            }
            return result

        time_items = []
        for item in matched_slots:
            dt = datetime.fromisoformat(item["START_E"])
            time_items.append({
                "slot_id": str(item["ID"]),
                "time": f"{dt.hour}:{dt.minute:02d}",
                "datetime_iso": item["START_E"],
            })

        times_text = ", ".join(t["time"] for t in time_items)
        result = {
            "success": True,
            "date": date_arg,
            "message": (
                f"На {date_arg} є такі години: {times_text}. "
                "Запитай пацієнта: «Який час вам зручніший?» "
                "Коли обере — переконайся що підтверджує, потім виклич book_new_visit(slot_id='...')."
            ),
            "slots": time_items,
        }
        call_cache[f"slots_{date_arg}"] = result
        return result

    # ── book_new_visit ──────────────────────────────────────────────────────
    if function_name == "book_new_visit":
        slot_id = str(args.get("slot_id", ""))
        if not slot_id:
            return {"success": False, "error": "slot_id не передано"}

        # Захист від дублікатів
        if call_cache.get("book_new_visit", {}).get("success"):
            logger.warning("[TOOL 2.1] book_new_visit: вже виконано — повертаємо кеш")
            return call_cache["book_new_visit"]

        try:
            from services import clinic_api as _capi
            patient_name = visit_context.get("patient_name", "")
            patphone     = visit_context.get("patient_phone", "")
            new_visit_id = await _capi.save_b2_booking(slot_id, patient_name, patphone)

            if not new_visit_id:
                result = {
                    "success": False,
                    "error": (
                        "Не вдалося створити запис. Скажи пацієнту що виникла технічна проблема "
                        "і запропонуй зателефонувати в клініку самостійно."
                    ),
                }
                return result

            # Знаходимо datetime для підтвердження
            raw = call_cache.get("__raw_slots", [])
            visit_dt_str = ""
            for item in raw:
                if str(item["ID"]) == slot_id:
                    try:
                        dt = datetime.fromisoformat(item["START_E"])
                        date_str = f"{_ua_ordinal(dt.day)} {_MONTHS_UA[dt.month - 1]}"
                        visit_dt_str = f"{date_str} о {dt.hour}:{dt.minute:02d}"
                    except Exception:
                        pass
                    break

            result = {
                "success": True,
                "new_visit_id": new_visit_id,
                "slot_id": slot_id,
                "visit_datetime": visit_dt_str,
                "message": (
                    f"Запис підтверджено! "
                    + (f"Вас записано {visit_dt_str}. " if visit_dt_str else "")
                    + "Скажи пацієнту про підтвердження і попрощайся. "
                    + "Потім виклич end_call(result='booked')."
                ),
            }
            call_cache["book_new_visit"] = result
            logger.info(f"[TOOL 2.1] book_new_visit → new_visit_id={new_visit_id}")
            return result

        except Exception as e:
            logger.error(f"[TOOL 2.1] book_new_visit помилка: {e!r}")
            result = {
                "success": False,
                "error": (
                    "Технічна помилка при створенні запису. "
                    "Скажи пацієнту що виникла тимчасова помилка і запропонуй зателефонувати в клініку."
                ),
            }
            return result

    # ── decline_with_reason ─────────────────────────────────────────────────
    if function_name == "decline_with_reason":
        category = args.get("category", "Не вказали причину")
        if category not in DECLINE_CATEGORIES:
            category = "Не вказали причину"
        raw_reason = args.get("raw_reason", "")
        result = {"success": True, "category": category, "raw_reason": raw_reason}
        call_cache["decline"] = result
        logger.info(f"[TOOL 2.1] decline_with_reason: category='{category}'")
        return result

    # ── mark_self_book ──────────────────────────────────────────────────────
    if function_name == "mark_self_book":
        result = {"success": True}
        call_cache["self_book"] = result
        logger.info("[TOOL 2.1] mark_self_book")
        return result

    # ── end_call ────────────────────────────────────────────────────────────
    if function_name == "end_call":
        call_result = args.get("result", "no_result")
        result = {"success": True, "result": call_result}
        call_cache["end_call"] = result
        logger.info(f"[TOOL 2.1] end_call → {call_result}")
        return result

    logger.error(f"[TOOL 2.1] Невідома функція: {function_name}")
    return {"error": f"Невідома функція: {function_name}"}
