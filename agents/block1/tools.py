"""
Function calling tools для Gemini Live API — Блок 1: Нагадування про візити.

Gemini викликає функції під час розмови.
Ми виконуємо їх і повертаємо результат назад в сесію.
"""

import logging
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
                    "Викликати коли пацієнт хоче перенести візит на інший час."
                ),
                parameters=types.Schema(
                    type="OBJECT",
                    properties={
                        "doctor_id": types.Schema(
                            type="STRING",
                            description="ID лікаря",
                        ),
                        "preference": types.Schema(
                            type="STRING",
                            description=(
                                "Побажання пацієнта щодо часу. "
                                "Приклади: 'після 18:00', 'вихідні', 'п'ятниця вдень', 'наступний тиждень'"
                            ),
                        ),
                    },
                    required=["doctor_id"],
                ),
            ),

            types.FunctionDeclaration(
                name="reschedule_visit",
                description=(
                    "Перенести візит на обраний пацієнтом новий час. "
                    "Викликати лише після того як пацієнт вибрав конкретний слот."
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
                            description="ID слоту отриманого з get_available_slots",
                        ),
                    },
                    required=["visit_id", "slot_id"],
                ),
            ),

            types.FunctionDeclaration(
                name="cancel_visit",
                description=(
                    "Скасувати візит пацієнта. "
                    "Викликати лише якщо пацієнт чітко сказав що хоче скасувати."
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
                name="transfer_to_operator",
                description=(
                    "Передати дзвінок живому оператору. "
                    "Викликати якщо: пацієнт просить оператора, екстрена ситуація, "
                    "бот не може впоратися із запитом."
                ),
                parameters=types.Schema(
                    type="OBJECT",
                    properties={
                        "reason": types.Schema(
                            type="STRING",
                            description="Причина передачі оператору",
                        ),
                    },
                    required=["reason"],
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
                            enum=["confirmed", "rescheduled", "cancelled", "no_result"],
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

async def handle_tool_call(
    function_name: str,
    args: dict,
    visit_context: dict,
) -> dict[str, Any]:
    """
    Виконати tool call від Gemini та повернути результат.

    Returns:
        dict який Gemini отримає як відповідь функції
    """
    logger.info(f"[TOOL] {function_name}({args})")

    if function_name == "confirm_visit":
        success = await clinic_api.confirm_visit(args["visit_id"])
        return {
            "success": success,
            "message": "Візит підтверджено" if success else "Помилка підтвердження, спробуйте пізніше",
        }

    if function_name == "get_available_slots":
        slots = await clinic_api.get_available_slots(
            doctor_id=args["doctor_id"],
            preference=args.get("preference"),
        )
        # Форматуємо дати для природного озвучення
        formatted = [
            {
                "slot_id": s["slot_id"],
                "display": _fmt_dt(s["datetime"]),
                "datetime": s["datetime"],
            }
            for s in slots
        ]
        return {"slots": formatted, "count": len(formatted)}

    if function_name == "reschedule_visit":
        success = await clinic_api.reschedule_visit(
            visit_id=args["visit_id"],
            slot_id=args["slot_id"],
        )
        return {
            "success": success,
            "message": "Візит перенесено" if success else "Помилка переносу, спробуйте пізніше",
        }

    if function_name == "cancel_visit":
        success = await clinic_api.cancel_visit(
            visit_id=args["visit_id"],
            reason=args.get("reason"),
        )
        return {
            "success": success,
            "message": "Візит скасовано" if success else "Помилка скасування, спробуйте пізніше",
        }

    if function_name == "transfer_to_operator":
        # TODO: реалізувати передачу через Binotel після отримання документації
        logger.warning(f"[TRANSFER] Передача оператору: {args.get('reason')}")
        return {"success": True, "message": "Передаємо на оператора"}

    if function_name == "end_call":
        return {"success": True, "result": args.get("result", "no_result")}

    logger.error(f"[TOOL] Невідома функція: {function_name}")
    return {"error": f"Невідома функція: {function_name}"}
