"""
Завантаження та рендеринг системного промпту для Блоку 1.
Промпт зберігається в prompts.yaml у корені проекту.
"""

import logging
import os
from datetime import datetime
from zoneinfo import ZoneInfo

import yaml

logger = logging.getLogger(__name__)

_procedure_aliases_cache: str = ""


async def load_procedure_aliases() -> None:
    """Завантажує довідник синонімів з Google Sheets і кешує в пам'яті."""
    global _procedure_aliases_cache
    from services.sheets_loader import fetch_procedure_aliases
    _procedure_aliases_cache = await fetch_procedure_aliases()

_PROMPTS_PATH = os.path.join(os.path.dirname(__file__), "..", "..", "prompts.yaml")

_MONTHS_UA = [
    "січня", "лютого", "березня", "квітня", "травня", "червня",
    "липня", "серпня", "вересня", "жовтня", "листопада", "грудня",
]

_WEEKDAYS_UA = [
    "понеділок", "вівторок", "середа", "четвер",
    "п'ятниця", "субота", "неділя",
]


def _get_today_date() -> str:
    """Повертає рядок: 'понеділок, 30 березня 2026 р.' (київський час)."""
    now = datetime.now(ZoneInfo("Europe/Kyiv"))
    weekday = _WEEKDAYS_UA[now.weekday()]
    month = _MONTHS_UA[now.month - 1]
    return f"{weekday}, {now.day} {month} {now.year} р."


def _format_visit_date(iso_datetime: str) -> str:
    """Перетворює ISO дату на читабельний рядок: '19 лютого о 14:00'."""
    dt = datetime.fromisoformat(iso_datetime)
    month = _MONTHS_UA[dt.month - 1]
    return f"{dt.day} {month} о {dt.hour}:{dt.minute:02d}"


def _get_greeting() -> str:
    hour = datetime.now(ZoneInfo("Europe/Kyiv")).hour
    if hour < 12:
        return "Доброго ранку"
    if hour < 18:
        return "Доброго дня"
    return "Доброго вечора"


_CLINIC_ADDRESSES: dict[str, str] = {
    "1": "Сонячний мікрорайон, 59, Кривий Ріг",
    "3": "вулиця Пушкіна, 13-О, Кривий Ріг",
}


def build_system_prompt(visit: dict) -> str:
    """
    Завантажує промпт з prompts.yaml та підставляє дані конкретного візиту.

    Args:
        visit: dict з ключами visit_id, patient_name, doctor_name,
               place_id, visit_datetime
    """
    with open(_PROMPTS_PATH, "r", encoding="utf-8") as f:
        prompts = yaml.safe_load(f)

    template: str = prompts["block1_reminder"]["system"]

    place_id = str(visit.get("place_id", ""))
    clinic_address = _CLINIC_ADDRESSES.get(place_id, "")

    prompt = template.format(
        patient_name=visit["patient_name"],
        patient_phone=visit.get("patient_phone", ""),
        doctor_name=visit["doctor_name"],
        visit_date=_format_visit_date(visit["visit_datetime"]),
        visit_id=visit["visit_id"],
        message=visit.get("message", ""),
        greeting=_get_greeting(),
        today_date=_get_today_date(),
        clinic_address=clinic_address,
    )

    if _procedure_aliases_cache:
        prompt += f"\n\n{_procedure_aliases_cache}"

    return prompt
