"""
Function calling tools для Модуля 2.1.

Аналог agents/block1/tools.py з відмінностями:
  - book_new_visit()      замість reschedule_visit() (створення НОВОГО запису)
  - decline_with_reason() фіксує відмову з категорією (26 категорій)
  - confirm_self_book()   пацієнт записується самостійно
"""

import logging
from typing import Any

from google.genai import types

from agents.block2.repeat_reminder.prompts import DECLINE_CATEGORIES

logger = logging.getLogger(__name__)


TOOLS = [
    types.Tool(
        function_declarations=[

            types.FunctionDeclaration(
                name="get_available_slots",
                description=(
                    "Отримати вільні слоти для повторного запису до лікаря. "
                    "БЕЗ date: повертає список доступних дат. "
                    "З date: повертає конкретний час на обрану дату."
                ),
                parameters=types.Schema(
                    type="OBJECT",
                    properties={
                        "date": types.Schema(type="STRING", description="Дата (необов'язково)"),
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
                        "slot_id": types.Schema(type="STRING", description="ID вільного часу"),
                    },
                    required=["slot_id"],
                ),
            ),

            types.FunctionDeclaration(
                name="decline_with_reason",
                description=(
                    "Пацієнт відмовився від запису. "
                    "Зафіксувати відмову з категорією причини. "
                    "Викликати однократно після того як пацієнт дав відповідь (або промовчав)."
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
                    "Пацієнт планує записатися самостійно або передзвонити. "
                    "Завершити дзвінок без повторних спроб."
                ),
                parameters=types.Schema(type="OBJECT", properties={}),
            ),

            types.FunctionDeclaration(
                name="end_call",
                description="Завершити дзвінок.",
                parameters=types.Schema(
                    type="OBJECT",
                    properties={
                        "result": types.Schema(
                            type="STRING",
                            enum=["booked", "declined", "self_book", "no_answer", "no_result"],
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

    if function_name == "get_available_slots":
        # TODO: реалізувати аналогічно block1/tools.py
        raise NotImplementedError

    if function_name == "book_new_visit":
        # TODO: виклик clinic_api.create_visit(visit_context["doctor_id"], args["slot_id"])
        raise NotImplementedError

    if function_name == "decline_with_reason":
        category = args.get("category", "Не вказали причину")
        if category not in DECLINE_CATEGORIES:
            category = "Не вказали причину"
        result = {"success": True, "category": category}
        call_cache["decline"] = result
        return result

    if function_name == "mark_self_book":
        result = {"success": True}
        call_cache["self_book"] = result
        return result

    if function_name == "end_call":
        result = {"success": True, "result": args.get("result", "no_result")}
        call_cache["end_call"] = result
        return result

    logger.error(f"[TOOL 2.1] Невідома функція: {function_name}")
    return {"error": f"Невідома функція: {function_name}"}
