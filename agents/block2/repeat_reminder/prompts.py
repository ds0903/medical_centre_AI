"""
Промпти для Модуля 2.1.

Завантажує системний промпт з prompts_b2.yaml (аналог prompts.yaml для Block1).
Відмінність від Block1: промпт включає контекст ПОПЕРЕДНЬОГО візиту та рекомендацію лікаря.
"""

from pathlib import Path
import yaml
import logging
from datetime import datetime

logger = logging.getLogger(__name__)

_YAML_PATH = Path(__file__).parent.parent.parent.parent / "prompts_b2.yaml"

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


def build_system_prompt(visit_context: dict) -> str:
    """
    Будує системний промпт для агента 2.1.

    Args:
        visit_context: словник з полями пацієнта та попереднього візиту.
    """
    try:
        with open(_YAML_PATH, encoding="utf-8") as f:
            config = yaml.safe_load(f)
        template = config["block2_repeat"]["system"]
    except FileNotFoundError:
        logger.warning("[PROMPT 2.1] prompts_b2.yaml не знайдено — використовуємо заглушку")
        template = _FALLBACK_TEMPLATE

    return template.format(
        today_date=datetime.now().strftime("%d.%m.%Y"),
        patient_name=visit_context.get("patient_name", ""),
        patient_phone=visit_context.get("patient_phone", ""),
        previous_visit_date=visit_context.get("previous_visit_date", ""),
        previous_doctor=visit_context.get("previous_doctor", ""),
        doctor_recommendation=visit_context.get("doctor_recommendation", ""),
        repeat_visit_date=visit_context.get("repeat_visit_date", ""),
        decline_categories="\n".join(f"{i+1}. {c}" for i, c in enumerate(DECLINE_CATEGORIES)),
    )


_FALLBACK_TEMPLATE = """
Ти — Марія, голосовий помічник медичного центру Медіком Кривбас.
Сьогодні: {today_date}

Телефонуєш пацієнту {patient_name} щодо повторного візиту.
Попередній прийом: {previous_visit_date} у лікаря {previous_doctor}.
Рекомендація лікаря: {doctor_recommendation}.
Рекомендована дата повторного прийому: {repeat_visit_date}.

TODO: додати повний промпт в prompts_b2.yaml
"""
