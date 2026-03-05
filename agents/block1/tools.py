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

from services import clinic_api

logger = logging.getLogger(__name__)

_MONTHS_UA = [
    "січня", "лютого", "березня", "квітня", "травня", "червня",
    "липня", "серпня", "вересня", "жовтня", "листопада", "грудня",
]


def _fmt_dt(iso: str) -> str:
    dt = datetime.fromisoformat(iso)
    return f"{dt.day} {_MONTHS_UA[dt.month - 1]} о {dt.strftime('%H:%M')}"


def _slot_half(hour: int) -> str:
    return "перша" if hour < 13 else "друга"


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
                    "Отримати вільні слоти для запису до лікаря. "
                    "Викликати ОДРАЗУ як пацієнт сказав що хоче перенести візит — "
                    "ще до того як питати пацієнта про дату. "
                    "Параметрів не потрібно — лікар відомий з контексту дзвінка."
                ),
                parameters=types.Schema(
                    type="OBJECT",
                    properties={},
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
        cached = call_cache.get("get_available_slots")
        if cached and time.monotonic() - cached["_ts"] < _SLOTS_CACHE_TTL:
            logger.warning(f"[TOOL] get_available_slots — кеш (TTL {_SLOTS_CACHE_TTL}с)")
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
        raw = await clinic_api.get_doctor_slots(visit_context["doctor_name"])
        if not raw:
            return {"success": False, "message": "Вільних слотів немає"}

        # Формуємо структуровану відповідь для Gemini
        slots = []
        seen_dates = []
        for item in raw:
            dt = datetime.fromisoformat(item["START_E"])
            date_str = f"{dt.day} {_MONTHS_UA[dt.month - 1]}"
            time_str = dt.strftime("%H:%M")
            half = _slot_half(dt.hour)
            slots.append({
                "slot_id": str(item["ID"]),
                "date": date_str,
                "time": time_str,
                "half": half,
            })
            if date_str not in seen_dates:
                seen_dates.append(date_str)

        logger.info(f"[TOOL] get_available_slots → {len(slots)} слотів, дати: {', '.join(seen_dates)}")
        for s in slots:
            logger.debug(f"[TOOL]   slot_id={s['slot_id']} | {s['date']} {s['time']} ({s['half']} половина)")
        result = {
            "success": True,
            "available_dates": ", ".join(seen_dates),
            "slots": slots,
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
