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

from config import CLINIC_API_URL, CLINIC_API_KEY, DEBUG, SKIP_POST

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Внутрішні хелпери
# ---------------------------------------------------------------------------

def _load_test_data() -> dict:
    path = os.path.join(os.path.dirname(__file__), "..", "data", "test_visits.json")
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def _make_headers() -> dict:
    return {
        "Bearer": CLINIC_API_KEY,
        "Accept": "application/json",
        "Content-Type": "application/json",
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
                "patient_phone":  a["PATPHONE"].replace("-", "").replace(" ", ""),
                "doctor_name":    a["CAPTION"],
                "doctor_id":      str(a["IDPOINT"]),
                "visit_datetime": a["START_E"],
                "message":        a.get("MESSAGE") or "",
            }
            for a in appointments
        ]
        logger.debug(f"[DEBUG] Завантажено {len(visits)} записів з test_visits.json")
        return visits

    async with aiohttp.ClientSession() as http:
        # Крок 1: сформувати список на завтра
        async with http.post(
            f"{CLINIC_API_URL}/bridge/call/listextractor",
            headers=_make_headers(),
            json={"fields": []},
        ) as resp:
            if resp.status >= 400:
                body = await resp.text()
                logger.warning(f"[API] listextractor HTTP {resp.status} (продовжуємо): {body[:200]}")
            else:
                extracted = await resp.json()
                processed = extracted.get('PROCESSED', '?') if isinstance(extracted, dict) else extracted
                logger.info(f"[API] listextractor → {processed}")

        # Крок 2: отримати готовий список
        async with http.post(
            f"{CLINIC_API_URL}/bridge/call/appointments",
            headers=_make_headers(),
            json={"fields": []},
        ) as resp:
            resp.raise_for_status()
            appointments = await resp.json()
            if isinstance(appointments, dict):
                appointments = appointments.get("appointments", [])
            logger.info(f"[API] appointments → {len(appointments)} записів")
            return [
                {
                    "visit_id":       str(a["ID"]),
                    "patient_name":   a["PATIENT"],
                    "patient_phone":  a["PATPHONE"].replace("-", "").replace(" ", ""),
                    "doctor_name":    a["CAPTION"],
                    "doctor_id":      str(a["IDPOINT"]),
                    "visit_datetime": a["START_E"],
                    "message":        a.get("MESSAGE") or "",
                }
                for a in appointments
            ]


async def get_doctor_slots(doctor_name: str) -> list[dict]:
    """
    Отримати вільні слоти для запису до лікаря.

    API: POST {CLINIC_API_URL}/bridge/call/doctorslots
    Повертає: [{ID, START_E}, ...] — вже відфільтровані сервером (макс 5 робочих днів)
    """
    if DEBUG:
        base = datetime.now().replace(second=0, microsecond=0, minute=0)
        day1 = base + timedelta(days=1)
        day2 = base + timedelta(days=2)
        return [
            {"ID": 9000001, "START_E": day1.strftime("%Y-%m-%d 09:00:00")},
            {"ID": 9000002, "START_E": day1.strftime("%Y-%m-%d 10:30:00")},
            {"ID": 9000003, "START_E": day1.strftime("%Y-%m-%d 14:00:00")},
            {"ID": 9000004, "START_E": day2.strftime("%Y-%m-%d 09:30:00")},
            {"ID": 9000005, "START_E": day2.strftime("%Y-%m-%d 15:00:00")},
        ]

    async with aiohttp.ClientSession() as http:
        async with http.post(
            f"{CLINIC_API_URL}/bridge/call/doctorslots",
            headers=_make_headers(),
            json={"fields": [doctor_name.strip()]},
        ) as resp:
            resp.raise_for_status()
            raw_text = await resp.text()
            logger.info(f"[API] doctorslots HTTP {resp.status} → {raw_text[:300]}")
            data = await resp.json(content_type=None)
            slots = data if isinstance(data, list) else data.get("slots", data.get("SLOTS", data.get("data", [])))
            logger.info(f"[API] doctorslots → {len(slots)} слотів для '{doctor_name}'")
            return slots


# ---------------------------------------------------------------------------
# Зміна статусів
# ---------------------------------------------------------------------------

async def confirm_visit(visit_id: str) -> bool:
    """POST /bridge/call/updatestate → статус 1 (Підтверджує)."""
    if DEBUG:
        logger.debug(f"[DEBUG] confirm_visit(visit_id={visit_id})")
        return True
    if SKIP_POST:
        logger.info(f"[API] confirm_visit (SKIP_POST): visit_id={visit_id}, status=1")
        return True
    async with aiohttp.ClientSession() as http:
        async with http.post(
            f"{CLINIC_API_URL}/bridge/call/updatestate",
            headers=_make_headers(),
            json={"fields": [int(visit_id), 1]},
        ) as resp:
            if resp.status >= 400:
                body = await resp.text()
                logger.error(f"[API] confirm_visit HTTP {resp.status} body: {body}")
                resp.raise_for_status()
            logger.info(f"[API] confirm_visit → visit_id={visit_id}, status=1")
            return True


async def reschedule_visit(visit_id: str, slot_id: str) -> bool:
    """POST /bridge/call/slotmover → перенести візит на новий слот, потім updatestate статус 4."""
    if SKIP_POST:
        logger.info(f"[API] reschedule_visit (SKIP_POST): visit_id={visit_id}, slot_id={slot_id}")
        return True
    async with aiohttp.ClientSession() as http:
        async with http.post(
            f"{CLINIC_API_URL}/bridge/call/slotmover",
            headers=_make_headers(),
            json={"fields": [int(visit_id), int(slot_id)]},
        ) as resp:
            resp.raise_for_status()
            logger.info(f"[API] reschedule_visit → visit_id={visit_id}, slot_id={slot_id}")

        async with http.post(
            f"{CLINIC_API_URL}/bridge/call/updatestate",
            headers=_make_headers(),
            json={"fields": [int(visit_id), 4]},
        ) as resp:
            resp.raise_for_status()
            logger.info(f"[API] reschedule_visit updatestate → visit_id={visit_id}, status=4 (Перенесено)")

        return True


async def update_state(visit_id: str, state: str) -> None:
    """
    Оновити статус події через POST /bridge/call/updatestate.
    SKIP_POST=true → тільки лог, без реального запиту.
    """
    if SKIP_POST:
        logger.info(f"[API] update_state (SKIP_POST): visit_id={visit_id}, state={state}")
        return

    # TODO: реальний запит до API
    logger.info(f"[API] update_state: visit_id={visit_id}, state={state} (stub)")


async def cancel_visit(visit_id: str, reason: Optional[str] = None) -> bool:
    """POST /bridge/call/updatestate → статус 2 (Відміняє)."""
    if DEBUG:
        logger.debug(f"[DEBUG] cancel_visit(visit_id={visit_id}, reason={reason})")
        return True
    if SKIP_POST:
        logger.info(f"[API] cancel_visit (SKIP_POST): visit_id={visit_id}, status=2")
        return True
    async with aiohttp.ClientSession() as http:
        async with http.post(
            f"{CLINIC_API_URL}/bridge/call/updatestate",
            headers=_make_headers(),
            json={"fields": [int(visit_id), 2]},
        ) as resp:
            resp.raise_for_status()
            logger.info(f"[API] cancel_visit → visit_id={visit_id}, status=2")
            return True


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
    _STATUS_LABELS = {1: "Підтверджує", 2: "Відміняє", 3: "Не зрозумів", 4: "Не додзвонились", 5: "Перенаправлено"}
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

    if SKIP_POST:
        logger.info(f"[API] update_event_status (SKIP_POST): ID={event_id}, STATUS={status} ({label})")
        return

    async with aiohttp.ClientSession() as http:
        async with http.post(
            f"{CLINIC_API_URL}/bridge/call/updatestate",
            headers=_make_headers(),
            json={"fields": [int(event_id), status]},
        ) as resp:
            resp.raise_for_status()
            logger.info(f"[API] update_event_status → ID={event_id}, STATUS={status} ({label})")
