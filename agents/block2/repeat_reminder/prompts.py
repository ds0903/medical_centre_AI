"""
Промпти для Модуля 2.1 — нагадування про повторні візити.

Структура ідентична agents/block1/prompts.py:
YAML зберігається поруч з цим файлом (prompts_b2.yaml).
"""

import logging
import os
from datetime import datetime
from zoneinfo import ZoneInfo

import yaml

logger = logging.getLogger(__name__)

_YAML_PATH = os.path.join(os.path.dirname(__file__), "prompts_b2.yaml")

_MONTHS_UA = [
    "січня", "лютого", "березня", "квітня", "травня", "червня",
    "липня", "серпня", "вересня", "жовтня", "листопада", "грудня",
]

_WEEKDAYS_UA = [
    "понеділок", "вівторок", "середа", "четвер",
    "п'ятниця", "субота", "неділя",
]

# Категорії причин відмови (26 шт. з ТЗ)
DECLINE_CATEGORIES = [
    "Не вказали причину",
    "Не встигають доїхати",
    "Змінилися плани",
    "Викликали на роботу",
    "Їде у відрядження",
    "Одужання",
    "Хотіли швидше",
    "Були в іншого фахівця",
    "Дорого",
    "Захворіла дитина",
    "Вже не треба",
    "Потрапили в лікарню",
    "Уже були на прийомі",
    "Не готові всі відповіді аналізів",
    "Записані в інший день",
    "Захворіли",
    "Поїхали",
    "За сімейними обставинами",
    "Нема з ким залишити дитину",
    "Погодні умови",
    "Викликали додому лікаря",
    "За станом здоров'я (гін.)",
    "Знайшли територіально ближче",
    "Транспортні проблеми",
    "Смерть пацієнта",
    "Повітряна тривога",
]


def _get_today_date() -> str:
    now = datetime.now(ZoneInfo("Europe/Kyiv"))
    weekday = _WEEKDAYS_UA[now.weekday()]
    month = _MONTHS_UA[now.month - 1]
    return f"{weekday}, {now.day} {month} {now.year} р."


def _get_greeting() -> str:
    hour = datetime.now(ZoneInfo("Europe/Kyiv")).hour
    if hour < 12:
        return "Доброго ранку"
    if hour < 18:
        return "Доброго дня"
    return "Доброго вечора"


def _format_date(iso_str: str) -> str:
    """ISO дата → читабельний рядок: '14 квітня 2026 р.'"""
    try:
        dt = datetime.fromisoformat(iso_str)
        return f"{dt.day} {_MONTHS_UA[dt.month - 1]} {dt.year} р."
    except Exception:
        return iso_str


def build_system_prompt(visit_context: dict) -> str:
    """
    Будує системний промпт для агента Модуля 2.1.

    Args:
        visit_context: словник з ключами:
            patient_name, patient_phone, patient_id,
            previous_visit_date, previous_doctor,
            doctor_id, doctor_name, doctor_specialization,
            doctor_recommendation, repeat_visit_date,
            source_visit_id
    """
    try:
        with open(_YAML_PATH, encoding="utf-8") as f:
            config = yaml.safe_load(f)
        template: str = config["block2_repeat"]["system"]
    except FileNotFoundError:
        logger.error(f"[PROMPT 2.1] {_YAML_PATH} не знайдено!")
        template = _FALLBACK_TEMPLATE

    prev_date = visit_context.get("previous_visit_date", "")
    if prev_date:
        prev_date = _format_date(str(prev_date))

    repeat_date = visit_context.get("repeat_visit_date", "")
    if repeat_date:
        repeat_date = _format_date(str(repeat_date))

    decline_list = "\n".join(f"{i+1}. {c}" for i, c in enumerate(DECLINE_CATEGORIES))

    return template.format(
        today_date=_get_today_date(),
        greeting=_get_greeting(),
        patient_name=visit_context.get("patient_name", ""),
        patient_phone=visit_context.get("patient_phone", ""),
        previous_visit_date=prev_date,
        previous_doctor=visit_context.get("previous_doctor", ""),
        doctor_recommendation=visit_context.get("doctor_recommendation", ""),
        repeat_visit_date=repeat_date,
        decline_categories=decline_list,
    )


_FALLBACK_TEMPLATE = """\
Ти — Оля, голосовий асистент медичного центру «Медіком» (Кривий Ріг).
Сьогодні: {today_date}. {greeting}.

Телефонуєш пацієнту {patient_name} щодо повторного прийому.
Попередній прийом: {previous_visit_date} у лікаря {previous_doctor}.
Рекомендація лікаря: {doctor_recommendation}.
Орієнтовна дата повторного прийому: {repeat_visit_date}.

Мета: записати пацієнта на повторний прийом або зафіксувати відмову з причиною.
Доступні функції: get_available_slots, book_new_visit, decline_with_reason, mark_self_book, end_call.
Категорії відмови:
{decline_categories}
"""
