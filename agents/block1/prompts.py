"""
Завантаження та рендеринг системного промпту для Блоку 1.
Промпт зберігається в prompts.yaml у корені проекту.
"""

import logging
import os
import re
from datetime import datetime
from zoneinfo import ZoneInfo

import yaml

from services.sheets_loader import clean_appointment_name

_TIME_DOT_RE = re.compile(r"\b(\d{1,2})\.(\d{2})\b")


def _normalize_time_dots(text: str) -> str:
    """'8.45 - 9.00' → '8:45 - 9:00' — крапка в часі плутає модель (вимовляє 'крапка')."""
    if not text:
        return text
    return _TIME_DOT_RE.sub(r"\1:\2", text)

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


_HOURS_DATIVE_UA = [
    "дванадцятій", "першій", "другій", "третій", "четвертій", "п'ятій",
    "шостій", "сьомій", "восьмій", "дев'ятій", "десятій", "одинадцятій",
]  # індекс = hour % 12

_MIN_ONES_UA = ["", "одна", "дві", "три", "чотири", "п'ять", "шість", "сім", "вісім", "дев'ять"]
_MIN_TEENS_UA = [
    "десять", "одинадцять", "дванадцять", "тринадцять", "чотирнадцять", "п'ятнадцять",
    "шістнадцять", "сімнадцять", "вісімнадцять", "дев'ятнадцять",
]
_MIN_TENS_UA = ["", "", "двадцять", "тридцять", "сорок", "п'ятдесят"]


def _spoken_minutes(minute: int) -> str:
    """Хвилини словами: 0→'', 5→'п'ять', 20→'двадцять', 45→'сорок п'ять'."""
    if minute == 0:
        return ""
    if minute < 10:
        return _MIN_ONES_UA[minute]
    if minute < 20:
        return _MIN_TEENS_UA[minute - 10]
    tens, ones = divmod(minute, 10)
    word = _MIN_TENS_UA[tens]
    return f"{word} {_MIN_ONES_UA[ones]}" if ones else word


def _spoken_time(hour: int, minute: int) -> str:
    """Готова вимовлена фраза часу: (13, 40) → 'о першій сорок'. Рахує Python — LLM не вигадує."""
    hour_word = _HOURS_DATIVE_UA[hour % 12]
    prefix = "об" if hour_word.startswith(("о", "а")) else "о"
    minutes_word = _spoken_minutes(minute)
    return f"{prefix} {hour_word} {minutes_word}".strip()


def _format_visit_date(iso_datetime: str) -> str:
    """Перетворює ISO дату на читабельний рядок з готовою вимовленою фразою часу:
    '19 лютого о першій сорок' — час вже словами, LLM більше НЕ конвертує цифри сама."""
    dt = datetime.fromisoformat(iso_datetime)
    month = _MONTHS_UA[dt.month - 1]
    return f"{dt.day} {month} {_spoken_time(dt.hour, dt.minute)}"


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


def _visit_label(visit: dict) -> str:
    """Пріоритет назви для озвучення: аналог з довідника → поле 'Процедура' → спеціалізація."""
    from services.sheets_loader import find_by_appointment
    appointment = visit.get("appointment", "")
    result = find_by_appointment(appointment) if appointment else None
    analog_name = result["analog_name"] if result else ""
    # Якщо аналогу нема — озвучуємо сире поле "Процедура", але БЕЗ касового коду (лідируючі цифри з API).
    appointment_clean = clean_appointment_name(appointment) if appointment else appointment
    return analog_name or appointment_clean or visit.get("doctor_specialization", visit.get("doctor_name", ""))


def _format_reminder_list(visit: dict) -> str:
    """Готовий текст для multi-visit промпту — перелік всіх записів."""
    linked = visit.get("linked_visits", [])
    items = [f"[до/на] {_visit_label(visit)} — {_format_visit_date(visit['visit_datetime'])}"]
    for v in linked:
        items.append(f"[до/на] {_visit_label(v)} — {_format_visit_date(v['visit_datetime'])}")
    count = len(items)
    dt = datetime.fromisoformat(visit['visit_datetime'])
    date_str = f"{dt.day} {_MONTHS_UA[dt.month - 1]}"
    # ⚠ НЕ "в цей день" — пацієнт не чує яка саме дата, питає "на завтра?".
    # Дата мусить бути сказана прямо тут, а не лише всередині кожного пункту списку.
    lines = [f"У вас {count} {'запис' if count == 1 else 'записи' if count < 5 else 'записів'} {date_str}:"]
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
        lines.append(f"      • {_visit_label(v)} — {_format_visit_date(v['visit_datetime'])}")
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
    row_num = result["row_num"] if result else None

    # Якщо немає ні назви ні підготовки — нічого не знайшли
    if not analog_name and not appointment:
        logger.info(f"[PROMPT] Інфо про послугу НЕ знайдено для '{appointment}'")
        return "", "", ""

    lines = []
    if analog_name:
        lines.append(f"• Зрозуміла назва: {analog_name}")
    preparation_text = preparation if preparation else "Спеціальна підготовка не потрібна."
    lines.append(f"• Підготовка: {preparation_text}")

    label = analog_name or clean_appointment_name(appointment) or "прийом"
    full_block = (
        f"\n\nІНФО ПРО ПРОЦЕДУРУ «{label}» (вже знайдено — ⚠ ЗАБОРОНЕНО викликати get_preparation_info, відповідай звідси):\n"
        + "\n".join(lines)
    )
    brief = preparation[:200] if preparation else ""
    row_info = f" рядок={row_num}" if row_num else ""
    logger.info(f"[PROMPT] Інфо знайдено{row_info}: аналог='{analog_name}', підготовка={bool(preparation)}")
    return full_block, brief, analog_name


def _build_confirmation_farewell(brief: str, clinic_address: str, multi: bool = False) -> str:
    """Pre-built прощальний текст при підтвердженні."""
    record_word = "Ваші записи підтверджено" if multi else "Ваш запис підтверджено"
    if clinic_address:
        return f"Чудово! {record_word}. Чекаємо вас за адресою: {clinic_address}. На все добре!"
    return f"Чудово! {record_word}. Чекаємо вас! На все добре!"


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
        appointment=clean_appointment_name(visit.get("appointment", "")),
        visit_date=_format_visit_date(visit["visit_datetime"]),
        visit_id=visit["visit_id"],
        message=_normalize_time_dots(visit.get("message", "")),
        greeting=_get_greeting(),
        today_date=_get_today_date(),
        clinic_address=clinic_address,
        linked_visits_block="",
        confirmation_farewell=confirmation_farewell,
    )

    if full_block:
        prompt += full_block
    return prompt
