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
    items = [f"[до/на] {doctor_spec} — {_format_visit_date(visit['visit_datetime'])}"]
    for v in linked:
        items.append(f"[до/на] {v['doctor_specialization']} — {_format_visit_date(v['visit_datetime'])}")
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


def _fetch_service_info(visit: dict) -> tuple[str, str, str]:
    """
    Шукає інформацію про послугу по полю APPOINTMENT.
    Повертає (full_block, brief, analog_name):
      full_block  — блок для промпту (підготовка + інструкція)
      brief       — 1 речення для прощального тексту (порожній якщо немає)
      analog_name — зрозуміла назва процедури для вживання у розмові
    """
    from services.sheets_loader import find_by_appointment
    appointment = visit.get("appointment", "")
    preparation_from_api = visit.get("preparation", "")

    result = find_by_appointment(appointment) if appointment else None
    analog_name = result["analog_name"] if result else ""
    preparation = result["preparation"] if result else preparation_from_api

    # Якщо немає ні назви ні підготовки — нічого не знайшли
    if not analog_name and not appointment:
        logger.info(f"[PROMPT] Інфо про послугу НЕ знайдено для '{appointment}'")
        return "", "", ""

    lines = []
    if analog_name:
        lines.append(f"• Зрозуміла назва: {analog_name}")
    preparation_text = preparation if preparation else "Спеціальна підготовка не потрібна."
    lines.append(f"• Підготовка: {preparation_text}")

    label = analog_name or appointment or "прийом"
    full_block = (
        f"\n\nІНФО ПРО ПРОЦЕДУРУ «{label}» (вже знайдено — ⚠ ЗАБОРОНЕНО викликати get_preparation_info, відповідай звідси):\n"
        + "\n".join(lines)
    )
    brief = preparation[:200] if preparation else ""
    logger.info(f"[PROMPT] Інфо знайдено: аналог='{analog_name}', підготовка={bool(preparation)}")
    return full_block, brief, analog_name


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

    primary_full_block, brief, _ = _fetch_service_info(visit)
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

    if primary_full_block:
        prompt += primary_full_block

    for lv in visit.get("linked_visits", []):
        lv_full_block, _, _ = _fetch_service_info(lv)
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

    full_block, brief, analog_name = _fetch_service_info(visit)
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
        appointment=visit.get("appointment", ""),
        visit_date=_format_visit_date(visit["visit_datetime"]),
        visit_id=visit["visit_id"],
        message=visit.get("message", ""),
        greeting=_get_greeting(),
        today_date=_get_today_date(),
        clinic_address=clinic_address,
        linked_visits_block="",
        confirmation_farewell=confirmation_farewell,
    )

    if full_block:
        prompt += full_block
    return prompt
