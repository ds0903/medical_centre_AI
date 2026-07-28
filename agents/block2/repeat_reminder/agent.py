"""
Модуль 2.1 — AI-нагадування про повторні візити.

Архітектура ідентична Block1Agent:
  - Binotel SIP + Gemini Realtime API
  - Відмінність: контекст дзвінка = попередній візит + рекомендація лікаря
  - Нові статуси: Записаний / Відмовився / Самостійний запис / Не додзвонились
  - Класифікація причини відмови (26 категорій)

TODO: реалізувати після Block1Agent як зразок.
"""

import logging

logger = logging.getLogger(__name__)


class RepeatReminderAgent:
    """Голосовий агент для нагадування про повторний візит (Модуль 2.1)."""

    def __init__(self, visit_context: dict):
        """
        Args:
            visit_context: дані про первинний візит та рекомендацію лікаря:
                - patient_name, patient_phone
                - previous_visit_date, previous_doctor
                - doctor_recommendation  (напр. "через 3 тижні")
                - repeat_visit_date      (абсолютна дата, розрахована планувальником)
                - visit_id               (ID запису в системі клініки)
        """
        self.visit_context = visit_context

    async def run(self) -> dict:
        """
        Провести дзвінок.

        Returns:
            {
                "status": "booked" | "declined" | "self_book" | "no_answer",
                "decline_reason_category": str | None,  # одна з 26 категорій
                "new_slot_id": str | None,              # якщо записали
                "duration_sec": int,
            }
        """
        raise NotImplementedError("TODO: реалізувати за зразком agents/block1/agent.py")
