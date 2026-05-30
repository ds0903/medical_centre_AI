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
_PROMPTS_MULTI_PATH = os.path.join(os.path.dirname(__file__), "..", "..", "prompts_multi.yaml")

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


def _format_visit_time(iso_datetime: str) -> str:
    dt = datetime.fromisoformat(iso_datetime)
    return f"{dt.hour}:{dt.minute:02d}"


def _format_reminder_list(visit: dict) -> str:
    """Готовий текст для multi-visit промпту — перелік всіх записів."""
    doctor_spec = visit.get("doctor_specialization", visit["doctor_name"])
    linked = visit.get("linked_visits", [])
    items = [f"[до/на] {doctor_spec} о {_format_visit_time(visit['visit_datetime'])}"]
    for v in linked:
        items.append(f"[до/на] {v['doctor_specialization']} о {_format_visit_time(v['visit_datetime'])}")
    count = len(items)
    lines = [f"У вас {count} {'запис' if count == 1 else 'записи' if count < 5 else 'записів'} в цей день:"]
    for item in items:
        lines.append(f" — {item}")
    lines.append("Ви плануєте прийти на всі?")
    return "\n    ".join(lines)


def _format_linked_visits(linked: list) -> str:
    if not linked:
        return ""
    # First line has no leading spaces — template `    {linked_visits_block}` adds 4.
    # Subsequent lines need their own 4-space indent since they're new lines after substitution.
    lines = ["ДОДАТКОВІ ЗАПИСИ ЦЬОГО ПАЦІЄНТА (той самий день):"]
    for v in linked:
        lines.append(f"      • {v['doctor_specialization']} — {_format_visit_date(v['visit_datetime'])}")
    lines.append("    ⚠ СЦЕНАРІЙ ДЛЯ КІЛЬКОХ ЗАПИСІВ — застосовуй обов'язково (дивись нижче).")
    return "\n".join(lines)


def _fetch_preparation(doctor_name: str) -> tuple[str, str]:
    """
    Шукає підготовку в каталозі.
    Повертає (full_block, brief) де:
      full_block — для вбудовування в промпт (відповіді на питання пацієнта)
      brief      — 1 речення для прощального тексту (порожній якщо не знайдено)
    """
    from services.sheets_loader import search_service
    matches = search_service(doctor_name)
    if not matches:
        logger.info(f"[PROMPT] Підготовка НЕ знайдена для '{doctor_name}'")
        return "", ""

    lines = []
    brief = ""
    for m in matches:
        line = f"• {m['name']}"
        if m["price"]:
            line += f" — {m['price']} грн"
        if m["preparation"]:
            line += f"\n  Підготовка: {m['preparation']}"
            if not brief:
                brief = m["preparation"]
        lines.append(line)

    full_block = (
        f"\n\nПІДГОТОВКА ДО ПРИЙОМУ «{doctor_name}» (вже знайдено — ⚠ ЗАБОРОНЕНО викликати get_preparation_info, відповідай звідси):\n"
        + "\n".join(lines)
    )
    logger.info(f"[PROMPT] Підготовка знайдена для '{doctor_name}': {len(matches)} позицій")
    return full_block, brief


def _build_confirmation_farewell(brief: str, clinic_address: str, multi: bool = False) -> str:
    """Pre-built прощальний текст при підтвердженні."""
    record_word = "Ваші записи підтверджено" if multi else "Ваш запис підтверджено"
    prep_part = f" До речі, щодо підготовки: {brief}." if brief else ""
    if clinic_address:
        return f"Чудово! {record_word}.{prep_part} Чекаємо вас за адресою: {clinic_address}. На все добре!"
    return f"Чудово! {record_word}.{prep_part} Чекаємо вас! На все добре!"


def _build_multi_prompt(visit: dict, clinic_address: str) -> str:
    """Спрощений промпт для дзвінка коли пацієнт має 2+ записів."""
    with open(_PROMPTS_MULTI_PATH, "r", encoding="utf-8") as f:
        prompts = yaml.safe_load(f)
    template: str = prompts["block1_reminder_multi"]["system"]

    # Prefetch підготовки для primary лікаря (brief для farewell)
    primary_full_block, brief = _fetch_preparation(visit["doctor_name"])
    visit["_has_preparation"] = bool(brief)
    confirmation_farewell = _build_confirmation_farewell(brief, clinic_address, multi=True)

    prompt = template.format(
        patient_name=visit["patient_name"],
        patient_phone=visit.get("patient_phone", ""),
        visit_id=visit["visit_id"],
        clinic_address=clinic_address,
        greeting=_get_greeting(),
        today_date=_get_today_date(),
        reminder_list=_format_reminder_list(visit),
        confirmation_farewell=confirmation_farewell,
    )

    # Вставляємо підготовку primary лікаря
    if primary_full_block:
        prompt += primary_full_block

    # Вставляємо підготовку linked лікарів (якщо є)
    for lv in visit.get("linked_visits", []):
        lv_name = lv.get("doctor_name", lv.get("doctor_specialization", ""))
        if lv_name:
            lv_full_block, _ = _fetch_preparation(lv_name)
            if lv_full_block:
                prompt += lv_full_block

    return prompt


def build_system_prompt(visit: dict) -> str:
    """
    Завантажує промпт та підставляє дані візиту.
    Якщо linked_visits не порожній — використовує спрощений multi-visit промпт.
    """
    place_id = str(visit.get("place_id", ""))
    clinic_address = _CLINIC_ADDRESSES.get(place_id, "")
    linked = visit.get("linked_visits", [])

    if linked:
        return _build_multi_prompt(visit, clinic_address)

    full_block, brief = _fetch_preparation(visit["doctor_name"])
    visit["_has_preparation"] = bool(brief)
    confirmation_farewell = _build_confirmation_farewell(brief, clinic_address, multi=False)

    with open(_PROMPTS_PATH, "r", encoding="utf-8") as f:
        prompts = yaml.safe_load(f)

    template: str = prompts["block1_reminder"]["system"]

    prompt = template.format(
        patient_name=visit["patient_name"],
        patient_phone=visit.get("patient_phone", ""),
        doctor_name=visit["doctor_name"],
        doctor_specialization=visit.get("doctor_specialization", visit["doctor_name"]),
        visit_date=_format_visit_date(visit["visit_datetime"]),
        visit_id=visit["visit_id"],
        message=visit.get("message", ""),
        greeting=_get_greeting(),
        today_date=_get_today_date(),
        clinic_address=clinic_address,
        linked_visits_block="",
        confirmation_farewell=confirmation_farewell,
    )

    if _procedure_aliases_cache:
        prompt += f"\n\n{_procedure_aliases_cache}"

    if full_block:
        prompt += full_block
    return prompt
