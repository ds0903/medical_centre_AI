"""
Клієнт API медичної клініки.

У DEBUG режимі всі методи повертають дані з data/test_visits.json або тестові заглушки.
У PRODUCTION режимі — реальні HTTP-запити до API клініки (TODO).

TODO (глобально): Уточнити у замовника:
  - Базовий URL та версію API
  - Метод автентифікації (Bearer token / API key / Basic auth)
  - Формат дат (ISO 8601 чи інший?)
  - Чи є rate limiting? Які коди помилок?
"""

import json
import logging
import os
from datetime import date, datetime, timedelta
from typing import Optional

import aiohttp

from config import CLINIC_API_URL, CLINIC_API_KEY, DEBUG

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Внутрішні хелпери
# ---------------------------------------------------------------------------

def _load_test_data() -> dict:
    path = os.path.join(os.path.dirname(__file__), "..", "data", "test_visits.json")
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def _make_headers() -> dict:
    # TODO: Уточнити формат заголовку авторизації у замовника
    return {
        "Authorization": f"Bearer {CLINIC_API_KEY}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }


# ---------------------------------------------------------------------------
# Отримання даних
# ---------------------------------------------------------------------------

async def get_upcoming_visits(target_date: date) -> list[dict]:
    """
    Отримати список запланованих візитів на вказану дату.

    Returns: list of dicts з ключами:
        visit_id      : str
        patient_name  : str
        patient_phone : str
        doctor_name   : str
        doctor_id     : str
        visit_datetime: str  ISO 8601

    ---
    TODO: GET {CLINIC_API_URL}/visits
          params: date=YYYY-MM-DD, status=scheduled
    TODO: Уточнити назву параметра дати (date / appointment_date / from?)
    TODO: Уточнити чи фільтрує по статусу автоматично або потрібен параметр
    TODO: Додати обробку пагінації якщо відповідь містить page/total
    TODO: Уточнити структуру відповіді (data[]? items[]? appointments[]?)
    """
    if DEBUG:
        data = _load_test_data()
        visits = data.get("visits", [])
        logger.debug(f"[DEBUG] Завантажено {len(visits)} візитів з test_visits.json")
        return visits

    # TODO: реальний запит ---------------------------------------------------
    # async with aiohttp.ClientSession() as http:
    #     async with http.get(
    #         f"{CLINIC_API_URL}/visits",
    #         headers=_make_headers(),
    #         params={"date": target_date.isoformat(), "status": "scheduled"},
    #     ) as resp:
    #         resp.raise_for_status()
    #         body = await resp.json()
    #         return body["data"]   # TODO: уточнити ключ
    # -------------------------------------------------------------------------
    raise NotImplementedError("TODO: реалізувати get_upcoming_visits")


async def get_available_slots(
    doctor_id: str,
    preference: Optional[str] = None,
    limit: int = 3,
) -> list[dict]:
    """
    Отримати вільні слоти для запису до лікаря.

    Args:
        doctor_id  : ID лікаря з visit_context
        preference : Текстове побажання пацієнта ("після 18:00", "вихідні" тощо)
        limit      : Максимальна кількість варіантів для відповіді (3 — оптимально)

    Returns: list of dicts з ключами:
        slot_id    : str
        datetime   : str  ISO 8601
        doctor_name: str

    ---
    TODO: GET {CLINIC_API_URL}/doctors/{doctor_id}/slots
          params: preference=..., limit=...
    TODO: Уточнити чи API розуміє preference як текст або очікує structured (date_from, time_from)?
    TODO: Уточнити чи слоти прив'язані до конкретного кабінету/ресурсу
    TODO: Уточнити формат slot_id для подальшого PATCH
    """
    if DEBUG:
        base = datetime.now().replace(second=0, microsecond=0)
        # Підбираємо перший робочий день після поточного
        base = base.replace(hour=15, minute=30) + timedelta(days=1)
        slots = [
            {
                "slot_id": "slot_001",
                "datetime": base.isoformat(),
                "doctor_name": "Лікар (тест)",
            },
            {
                "slot_id": "slot_002",
                "datetime": (base + timedelta(minutes=30)).isoformat(),
                "doctor_name": "Лікар (тест)",
            },
            {
                "slot_id": "slot_003",
                "datetime": (base + timedelta(days=1, hours=-1)).isoformat(),
                "doctor_name": "Лікар (тест)",
            },
        ]
        logger.debug(f"[DEBUG] Повертаємо {len(slots)} тестових слотів")
        return slots[:limit]

    # TODO: реальний запит ---------------------------------------------------
    # params = {"limit": limit}
    # if preference:
    #     params["preference"] = preference   # TODO: уточнити назву параметра
    #
    # async with aiohttp.ClientSession() as http:
    #     async with http.get(
    #         f"{CLINIC_API_URL}/doctors/{doctor_id}/slots",
    #         headers=_make_headers(),
    #         params=params,
    #     ) as resp:
    #         resp.raise_for_status()
    #         body = await resp.json()
    #         return body["slots"][:limit]   # TODO: уточнити ключ
    # -------------------------------------------------------------------------
    raise NotImplementedError("TODO: реалізувати get_available_slots")


# ---------------------------------------------------------------------------
# Зміна статусів
# ---------------------------------------------------------------------------

async def confirm_visit(visit_id: str) -> bool:
    """
    Підтвердити візит пацієнта.

    ---
    TODO: PATCH {CLINIC_API_URL}/visits/{visit_id}
          body: {"status": "confirmed"}
    TODO: Уточнити точну назву статусу (confirmed / approve / active?)
    TODO: Уточнити чи повертає 200 з оновленим об'єктом або просто 204
    """
    if DEBUG:
        logger.debug(f"[DEBUG] confirm_visit(visit_id={visit_id})")
        return True

    # TODO: реальний запит ---------------------------------------------------
    # async with aiohttp.ClientSession() as http:
    #     async with http.patch(
    #         f"{CLINIC_API_URL}/visits/{visit_id}",
    #         headers=_make_headers(),
    #         json={"status": "confirmed"},
    #     ) as resp:
    #         resp.raise_for_status()
    #         return True
    # -------------------------------------------------------------------------
    raise NotImplementedError("TODO: реалізувати confirm_visit")


async def reschedule_visit(visit_id: str, slot_id: str) -> bool:
    """
    Перенести візит на новий слот.

    ---
    TODO: PATCH {CLINIC_API_URL}/visits/{visit_id}
          body: {"slot_id": slot_id, "status": "rescheduled"}
    TODO: Уточнити чи існує окремий endpoint для переносу (POST /reschedule?)
    TODO: Уточнити чи автоматично звільняється старий слот або потрібен окремий DELETE
    TODO: Уточнити чи потрібен новий datetime замість slot_id
    """
    if DEBUG:
        logger.debug(f"[DEBUG] reschedule_visit(visit_id={visit_id}, slot_id={slot_id})")
        return True

    # TODO: реальний запит ---------------------------------------------------
    # async with aiohttp.ClientSession() as http:
    #     async with http.patch(
    #         f"{CLINIC_API_URL}/visits/{visit_id}",
    #         headers=_make_headers(),
    #         json={"slot_id": slot_id, "status": "rescheduled"},
    #     ) as resp:
    #         resp.raise_for_status()
    #         return True
    # -------------------------------------------------------------------------
    raise NotImplementedError("TODO: реалізувати reschedule_visit")


async def cancel_visit(visit_id: str, reason: Optional[str] = None) -> bool:
    """
    Скасувати візит пацієнта.

    ---
    TODO: PATCH {CLINIC_API_URL}/visits/{visit_id}
          body: {"status": "cancelled", "cancel_reason": reason}
    TODO: Уточнити назву поля причини (reason / cancel_reason / note?)
    TODO: Уточнити чи причина є обов'язковою
    TODO: Уточнити чи фіксується хто скасував (бот / пацієнт / адмін)
    """
    if DEBUG:
        logger.debug(f"[DEBUG] cancel_visit(visit_id={visit_id}, reason={reason})")
        return True

    # TODO: реальний запит ---------------------------------------------------
    # payload = {"status": "cancelled"}
    # if reason:
    #     payload["cancel_reason"] = reason   # TODO: уточнити назву поля
    #
    # async with aiohttp.ClientSession() as http:
    #     async with http.patch(
    #         f"{CLINIC_API_URL}/visits/{visit_id}",
    #         headers=_make_headers(),
    #         json=payload,
    #     ) as resp:
    #         resp.raise_for_status()
    #         return True
    # -------------------------------------------------------------------------
    raise NotImplementedError("TODO: реалізувати cancel_visit")
