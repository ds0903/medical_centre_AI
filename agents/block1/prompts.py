"""
Завантаження та рендеринг системного промпту для Блоку 1.
Промпт зберігається в prompts.yaml у корені проекту.
"""

import os
from datetime import datetime

import yaml

_PROMPTS_PATH = os.path.join(os.path.dirname(__file__), "..", "..", "prompts.yaml")

_MONTHS_UA = [
    "січня", "лютого", "березня", "квітня", "травня", "червня",
    "липня", "серпня", "вересня", "жовтня", "листопада", "грудня",
]


def _format_visit_date(iso_datetime: str) -> str:
    """Перетворює ISO дату на читабельний рядок: '19 лютого о 14:00'."""
    dt = datetime.fromisoformat(iso_datetime)
    month = _MONTHS_UA[dt.month - 1]
    return f"{dt.day} {month} о {dt.strftime('%H:%M')}"


def build_system_prompt(visit: dict) -> str:
    """
    Завантажує промпт з prompts.yaml та підставляє дані конкретного візиту.

    Args:
        visit: dict з ключами visit_id, patient_name, doctor_name,
               doctor_id, visit_datetime
    """
    with open(_PROMPTS_PATH, "r", encoding="utf-8") as f:
        prompts = yaml.safe_load(f)

    template: str = prompts["block1_reminder"]["system"]

    return template.format(
        patient_name=visit["patient_name"],
        doctor_name=visit["doctor_name"],
        visit_date=_format_visit_date(visit["visit_datetime"]),
        visit_id=visit["visit_id"],
        doctor_id=visit["doctor_id"],
        message=visit.get("message", ""),
    )
