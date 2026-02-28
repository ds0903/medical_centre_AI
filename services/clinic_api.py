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
    Отримати список записів на дзвінки з API клініки.

    API: GET https://cabinet.mkom.ua/bridge/call/appointments
    Повертає поля: ID, START_E, FINISH_E, CAPTION, LOCATION, MESSAGE,
                   PATIENT, PATPHONE, EVENTSTATE, IDPOINT, NOTE, ONLINE_EVENT

    Returns: list of dicts з внутрішніми ключами:
        visit_id      : str  (= str(ID))
        patient_name  : str  (= PATIENT)
        patient_phone : str  (= PATPHONE)
        doctor_name   : str  (= CAPTION)
        doctor_id     : str  (= str(IDPOINT))
        visit_datetime: str  ISO 8601  (= START_E)
    """
    if DEBUG:
        data = _load_test_data()
        appointments = data.get("appointments", [])
        visits = [
            {
                "visit_id":       str(a["ID"]),
                "patient_name":   a["PATIENT"],
                "patient_phone":  a["PATPHONE"],
                "doctor_name":    a["CAPTION"],
                "doctor_id":      str(a["IDPOINT"]),
                "visit_datetime": a["START_E"],
            }
            for a in appointments
        ]
        logger.debug(f"[DEBUG] Завантажено {len(visits)} записів з test_visits.json")
        return visits

    # TODO: реальний запит ---------------------------------------------------
    # async with aiohttp.ClientSession() as http:
    #     async with http.get(
    #         f"{CLINIC_API_URL}/bridge/call/appointments",
    #         headers=_make_headers(),
    #     ) as resp:
    #         resp.raise_for_status()
    #         appointments = await resp.json()
    #         return [
    #             {
    #                 "visit_id":       str(a["ID"]),
    #                 "patient_name":   a["PATIENT"],
    #                 "patient_phone":  a["PATPHONE"],
    #                 "doctor_name":    a["CAPTION"],
    #                 "doctor_id":      str(a["IDPOINT"]),
    #                 "visit_datetime": a["START_E"],
    #             }
    #             for a in appointments
    #         ]
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


# ---------------------------------------------------------------------------
# Відправка результату дзвінка
# ---------------------------------------------------------------------------

async def update_event_status(event_id: str, status: int) -> None:
    """
    Відправити результат дзвінка назад до API клініки.

    Args:
        event_id: ID події (= ID з appointments API)
        status:
            1 — Підтверджує
            2 — Відміняє
            3 — Не зрозумів / передано оператору
            4 — Не додзвонились

    DEBUG: оновлює STATUS прямо в data/test_visits.json

    ---
    TODO: уточнити endpoint та метод (POST / PATCH / PUT)
    TODO: уточнити формат авторизації (заголовок)
    """
    _STATUS_LABELS = {1: "Підтверджує", 2: "Відміняє", 3: "Не зрозумів", 4: "Не додзвонились"}
    label = _STATUS_LABELS.get(status, str(status))

    if DEBUG:
        path = os.path.join(os.path.dirname(__file__), "..", "data", "test_visits.json")
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)

        updated = False
        for appointment in data.get("appointments", []):
            if str(appointment["ID"]) == str(event_id):
                appointment["STATUS"] = status
                updated = True
                break

        if updated:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            logger.info(f"[DEBUG] update_event_status → ID={event_id}, STATUS={status} ({label})")
        else:
            logger.warning(f"[DEBUG] update_event_status → ID={event_id} не знайдено в JSON")
        return

    # TODO: реальний запит ---------------------------------------------------
    # async with aiohttp.ClientSession() as http:
    #     async with http.post(
    #         f"{CLINIC_API_URL}/bridge/call/appointments/status",
    #         headers=_make_headers(),
    #         json={"ID": int(event_id), "STATUS": status},
    #     ) as resp:
    #         resp.raise_for_status()
    # -------------------------------------------------------------------------
    raise NotImplementedError("TODO: реалізувати update_event_status")
