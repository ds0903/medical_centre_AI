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

from config import CLINIC_API_URL, CLINIC_API_KEY, DEBUG, SKIP_POST, SKIP_ENABLED

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Внутрішні хелпери
# ---------------------------------------------------------------------------

def _load_test_data() -> dict:
    path = os.path.join(os.path.dirname(__file__), "..", "data", "test_visits.json")
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def _parse_caption(caption: str) -> tuple[str, str]:
    """CAPTION = 'Прізвище Ім'я По-батькові Спеціалізація' → (повне ПІБ, спеціалізація).
    Fallback: якщо < 4 слів — повертає (caption, caption).
    """
    parts = caption.strip().split(" ", 3)
    if len(parts) >= 4:
        return " ".join(parts[:3]), parts[3]
    return caption, caption


def _make_headers() -> dict:
    return {
        "Bearer": CLINIC_API_KEY,
        "Accept": "application/json",
        "Content-Type": "application/json",
    }


# ---------------------------------------------------------------------------
# Отримання даних
# ---------------------------------------------------------------------------

async def update_statuses(mode: int) -> None:
    """
    POST /bridge/call/updatestatuses — оновити статуси перед fetch.

    mode — той самий параметр Ранок/Вечір, що й shift у listextractor:
      0 — ранкова сесія, 1 — вечірня сесія.
    """
    if DEBUG:
        logger.debug(f"[DEBUG] update_statuses(mode={mode}): пропускаємо")
        return
    async with aiohttp.ClientSession() as http:
        async with http.post(
            f"{CLINIC_API_URL}/bridge/call/updatestatuses",
            headers=_make_headers(),
            json={"fields": [mode]},
        ) as resp:
            if resp.status >= 400:
                body = await resp.text()
                logger.warning(f"[API] updatestatuses(mode={mode}) HTTP {resp.status}: {body[:200]}")
            else:
                logger.info(f"[API] updatestatuses(mode={mode}) → ok")


async def get_upcoming_visits(shift: int) -> list[dict]:
    """
    Отримати список записів на дзвінки з API клініки.

    shift=0 — ранкова сесія: витягує слоти на сьогодні (друга половина дня)
    shift=1 — вечірня сесія: витягує слоти на завтра (перша половина дня)

    API: POST https://cabinet.mkom.ua/bridge/call/listextractor  → {"fields": [shift]}
         POST https://cabinet.mkom.ua/bridge/call/appointments   → список записів

    Returns: list of dicts з внутрішніми ключами:
        visit_id             : str  (= str(ID))
        patient_name         : str  (= PATIENT)
        patient_phone        : str  (= PATPHONE)
        doctor_name          : str  (= CAPTION повністю, для DB та sheets-пошуку)
        doctor_specialization: str  (= все після 3-го пробілу CAPTION)
        place_id             : str  (= str(IDPOINT))
        visit_datetime       : str  ISO 8601  (= START_E)
    """
    if DEBUG:
        data = _load_test_data()
        appointments = data.get("appointments", [])
        visits = [
            {
                "visit_id":              str(a["ID"]),
                "patient_name":          a["PATIENT"],
                "patient_phone":         a["PATPHONE"].replace("-", "").replace(" ", ""),
                "doctor_name":           a["CAPTION"],
                "doctor_specialization": a.get("SPECIALITY_AI") or _parse_caption(a["CAPTION"])[1],
                "place_id":              str(a["IDPOINT"]),
                "visit_datetime":        a["START_E"],
                "message":               a.get("MESSAGE") or "",
                "appointment":           a.get("APPOINTMENT") or "",
                "preparation":           a.get("PREPARATION") or "",
            }
            for a in appointments
        ]
        logger.debug(f"[DEBUG] Завантажено {len(visits)} записів з test_visits.json (shift={shift})")
        return visits

    async with aiohttp.ClientSession() as http:
        # Крок 1: сформувати список (shift=0 — сьогодні, shift=1 — завтра)
        async with http.post(
            f"{CLINIC_API_URL}/bridge/call/listextractor",
            headers=_make_headers(),
            json={"fields": [shift]},
        ) as resp:
            if resp.status >= 400:
                body = await resp.text()
                logger.warning(f"[API] listextractor HTTP {resp.status} (продовжуємо): {body[:200]}")
            else:
                extracted = await resp.json()
                processed = extracted.get('PROCESSED', '?') if isinstance(extracted, dict) else extracted
                logger.info(f"[API] listextractor(shift={shift}) → {processed}")

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
                    "visit_id":              str(a["ID"]),
                    "patient_name":          a["PATIENT"],
                    "patient_phone":         a["PATPHONE"].replace("-", "").replace(" ", ""),
                    "doctor_name":           a["CAPTION"],
                    "doctor_specialization": a.get("SPECIALITY_AI") or _parse_caption(a["CAPTION"])[1],
                    "place_id":              str(a["IDPOINT"]),
                    "visit_datetime":        a["START_E"],
                    "message":               a.get("MESSAGE") or "",
                    "appointment":           a.get("APPOINTMENT") or "",
                    "preparation":           a.get("PREPARATION") or "",
                }
                for a in appointments
            ]


async def check_call_enabled(visit_id: str) -> bool:
    """POST /bridge/call/ai_checkstate → ENABLED: 0=не дзвонити, 1=дзвонити.
    На помилці — fallback True (краще зателефонувати ніж пропустити).
    SKIP_ENABLED=true → перевірка пропускається, завжди True.
    """
    if DEBUG:
        return True
    if SKIP_ENABLED:
        logger.info(f"[API] ai_checkstate (SKIP_ENABLED): visit={visit_id} → перевірку пропущено")
        return True
    async with aiohttp.ClientSession() as http:
        try:
            async with http.post(
                f"{CLINIC_API_URL}/bridge/call/ai_checkstate",
                headers=_make_headers(),
                json={"fields": [int(visit_id)]},
            ) as resp:
                if resp.status >= 400:
                    body = await resp.text()
                    logger.warning(f"[API] ai_checkstate HTTP {resp.status} → fallback=True: {body[:200]}")
                    return True
                data = await resp.json()
                logger.info(f"[API] ai_checkstate raw → {data}")
                if isinstance(data, list):
                    item = data[0] if data else {}
                    enabled = int(item.get("ENABLED", 1) if isinstance(item, dict) else item)
                else:
                    enabled = int(data.get("ENABLED", 1))
                logger.info(f"[API] ai_checkstate visit={visit_id} → ENABLED={enabled}")
                return enabled == 1
        except Exception as e:
            logger.warning(f"[API] ai_checkstate помилка → fallback=True: {e}")
            return True


async def get_doctor_slots(slot_id: str) -> list[dict]:
    """
    Отримати вільні слоти для запису до лікаря.

    API: POST {CLINIC_API_URL}/bridge/call/doctorslots
    Параметр: slot_id — ID поточного слота запису пацієнта (visit_id).
    API знаходить наступний вільний слот того ж лікаря і повертає вільні слоти на 3 дні вперед.
    Повертає: [{ID, START_E}, ...]
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
            json={"fields": [str(slot_id)]},
        ) as resp:
            resp.raise_for_status()
            raw_text = await resp.text()
            logger.info(f"[API] doctorslots HTTP {resp.status} → {raw_text[:300]}")
            data = await resp.json(content_type=None)
            slots = data if isinstance(data, list) else data.get("slots", data.get("SLOTS", data.get("data", [])))
            logger.info(f"[API] doctorslots → {len(slots)} слотів для slot_id={slot_id}")
            return slots


# ---------------------------------------------------------------------------
# Зміна статусів
# ---------------------------------------------------------------------------

async def confirm_visit(visit_id: str) -> bool:
    """POST /bridge/call/updatestate → статус 1 (Підтверджує)."""
    commentary = "Пацієнт підтвердив запис."
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
            json={"fields": [int(visit_id), 1, commentary]},
        ) as resp:
            if resp.status >= 400:
                body = await resp.text()
                logger.error(f"[API] confirm_visit HTTP {resp.status} body: {body}")
                resp.raise_for_status()
            logger.info(f"[API] confirm_visit → visit_id={visit_id}, status=1 | {commentary}")
            return True


async def reschedule_visit(visit_id: str, slot_id: str) -> bool:
    """POST /bridge/call/slotmover → перенести візит на новий слот (статус 4 встановлює сам slotmover)."""
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
    commentary = "Пацієнт скасував запис."
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
            json={"fields": [int(visit_id), 2, commentary]},
        ) as resp:
            resp.raise_for_status()
            logger.info(f"[API] cancel_visit → visit_id={visit_id}, status=2 | {commentary}")
            return True


# ---------------------------------------------------------------------------
# Відправка результату дзвінка
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Блок 2 — Модуль 2.1: повторні візити
# ---------------------------------------------------------------------------

def _format_ondate(ondate) -> str:
    """
    ONDATE для doctorslots2 у тому вигляді, в якому його віддав /followup_appts
    ("YYYY-MM-DD HH:MM:SS"). У БД дата лежить як DateTime, а в visit_context — як
    isoformat з "T", тому повертаємо оригінальний формат.
    """
    if not ondate:
        return ""
    if isinstance(ondate, (datetime, date)):
        if isinstance(ondate, datetime):
            return ondate.strftime("%Y-%m-%d %H:%M:%S")
        return ondate.strftime("%Y-%m-%d")
    return str(ondate).strip().replace("T", " ", 1)


async def get_doctor_slots_b2(doctor_name: str, idpoint: int, ondate=None) -> list[dict]:
    """
    Отримати вільні слоти лікаря для повторного запису (Модуль 2.1).

    API: POST /bridge/call/doctorslots2
    Параметри: ["DOCTORNAME", IDPOINT, ONDATE]
      ONDATE — значення з попереднього виклику /followup_appts (repeat_visit_date).
    Повертає: [{ID, START_E}, ...]
    """
    if DEBUG:
        base = datetime.now().replace(second=0, microsecond=0, minute=0)
        slots = []
        for day_offset in range(1, 8):
            day = base + timedelta(days=day_offset)
            slots.extend([
                {"ID": 9100000 + day_offset * 10 + 1, "START_E": day.strftime("%Y-%m-%d 09:00:00")},
                {"ID": 9100000 + day_offset * 10 + 2, "START_E": day.strftime("%Y-%m-%d 11:00:00")},
                {"ID": 9100000 + day_offset * 10 + 3, "START_E": day.strftime("%Y-%m-%d 14:30:00")},
            ])
        return slots[:15]

    fields: list = [doctor_name, idpoint]
    ondate_str = _format_ondate(ondate)
    if ondate_str:
        fields.append(ondate_str)
    else:
        logger.warning(
            f"[API] doctorslots2: ONDATE порожній для {doctor_name} IDPOINT={idpoint} — "
            f"викликаємо лише з 2 параметрами"
        )

    async with aiohttp.ClientSession() as http:
        async with http.post(
            f"{CLINIC_API_URL}/bridge/call/doctorslots2",
            headers=_make_headers(),
            json={"fields": fields},
        ) as resp:
            resp.raise_for_status()
            data = await resp.json(content_type=None)
            slots = data if isinstance(data, list) else data.get("slots", data.get("SLOTS", []))
            logger.info(
                f"[API] doctorslots2 → {len(slots)} слотів для {doctor_name} "
                f"IDPOINT={idpoint} ONDATE={ondate_str or '-'}"
            )
            return slots


async def get_doctor_slots_by_id(doctor_id: str) -> list[dict]:
    """Backward-compat alias (Блок 1). Для Модуля 2.1 використовуй get_doctor_slots_b2."""
    return await get_doctor_slots_b2(doctor_id, 0)


async def get_repeat_visit_reminders() -> list[dict]:
    """
    Отримати список пацієнтів для повторного дзвінка.

    API: POST /bridge/call/followup_appts (без параметрів)
    Повертає пацієнтів з ONDATE від завтра до +4 днів.

    Returns: list of dicts з внутрішніми ключами:
        source_visit_id      : str  (= ID — числовий ID запису, передається в ai_savestate)
        patient_name         : str  (= PATIENT)
        patient_phone        : str  (= PATPHONE, нормалізований)
        patient_id           : str  (= GLOBALID)
        doctor_id            : str  (= str(PERFORMER_ID))
        doctor_name          : str  (= DOCTORNAME)
        doctor_specialization: str  (= SPECIALITY_AI)
        repeat_visit_date    : str  (= ONDATE ISO)
        idpoint              : int  (= IDPOINT)
    """
    if DEBUG:
        _path = os.path.join(os.path.dirname(__file__), "..", "data", "test_repeat_visits.json")
        try:
            with open(_path, "r", encoding="utf-8") as f:
                raw = json.load(f)
        except FileNotFoundError:
            logger.warning(f"[DEBUG] {_path} не знайдено — повертаємо порожній список")
            return []
        return [_map_followup_record(r) for r in raw]

    async with aiohttp.ClientSession() as http:
        async with http.post(
            f"{CLINIC_API_URL}/bridge/call/followup_appts",
            headers=_make_headers(),
            json={"fields": []},
        ) as resp:
            resp.raise_for_status()
            data = await resp.json(content_type=None)
            items = data if isinstance(data, list) else data.get("data", [])
            logger.info(f"[API] followup_appts → {len(items)} пацієнтів")
            return [_map_followup_record(r) for r in items]


def _map_followup_record(r: dict) -> dict:
    """Маппінг полів /followup_appts → внутрішній формат."""
    raw_phone = str(r.get("PATPHONE", "")).replace("-", "").replace(" ", "").replace("+", "")
    if raw_phone.startswith("380"):
        phone = "+" + raw_phone
    elif raw_phone.startswith("0") and len(raw_phone) == 10:
        phone = "+38" + raw_phone
    else:
        phone = raw_phone
    return {
        "source_visit_id":      str(r.get("ID", "")),
        "patient_name":         str(r.get("PATIENT", "")),
        "patient_phone":        phone,
        "patient_id":           str(r.get("GLOBALID", "")),
        "doctor_id":            str(r.get("PERFORMER_ID", "")),
        "doctor_name":          str(r.get("DOCTORNAME", "")),
        "doctor_specialization": str(r.get("SPECIALITY_AI", "")),
        "repeat_visit_date":    str(r.get("ONDATE", "")),
        "idpoint":              int(r.get("IDPOINT", 0)),
        "previous_visit_date":  "",
        "previous_doctor":      "",
        "doctor_recommendation": "",
    }


async def check_existing_appointment(
    patient_id: str,
    doctor_id: str,
    target_date: date,
    delta_days: int = 5,
) -> bool:
    """
    Перевірити чи є вже існуючий запис до цього лікаря в ±N днів від цільової дати.

    Повертає True якщо запис вже є → дзвонити не потрібно.

    TODO: уточнити endpoint у замовника.
    """
    if DEBUG:
        return False  # у debug режимі завжди дзвонимо

    date_from = (datetime.combine(target_date, datetime.min.time()) - timedelta(days=delta_days)).date()
    date_to   = (datetime.combine(target_date, datetime.min.time()) + timedelta(days=delta_days)).date()

    async with aiohttp.ClientSession() as http:
        try:
            async with http.post(
                f"{CLINIC_API_URL}/bridge/call/check_appointment",
                headers=_make_headers(),
                json={"fields": [str(patient_id), str(doctor_id), date_from.isoformat(), date_to.isoformat()]},
            ) as resp:
                if resp.status >= 400:
                    logger.warning(f"[API] check_appointment HTTP {resp.status} → fallback=False")
                    return False
                data = await resp.json(content_type=None)
                exists = bool(data.get("exists", False) if isinstance(data, dict) else data)
                logger.info(f"[API] check_appointment patient={patient_id} doctor={doctor_id} → exists={exists}")
                return exists
        except Exception as e:
            logger.warning(f"[API] check_appointment помилка → fallback=False: {e}")
            return False


async def save_b2_booking(slot_id: str, patient_name: str, patphone: str) -> Optional[str]:
    """
    Зафіксувати бронювання слоту (STATUS=1, booked).

    API: POST /bridge/call/ai_save_event
    Поля: [ID слоту (int), STATUS=1, PATIENT, PATPHONE]
    Повертає slot_id як підтвердження (нового visit_id API не повертає).
    """
    if DEBUG:
        logger.info(f"[DEBUG] save_b2_booking slot_id={slot_id} patient={patient_name}")
        return f"DEBUG_SLOT_{slot_id}"

    if SKIP_POST:
        logger.info(f"[API] save_b2_booking (SKIP_POST): slot_id={slot_id} patient={patient_name}")
        return f"SKIP_SLOT_{slot_id}"

    async with aiohttp.ClientSession() as http:
        async with http.post(
            f"{CLINIC_API_URL}/bridge/call/ai_save_event",
            headers=_make_headers(),
            json={"fields": [int(slot_id), 1, patient_name, patphone]},
        ) as resp:
            resp.raise_for_status()
            logger.info(f"[API] ai_save_event → slot_id={slot_id} patient={patient_name}")
            return slot_id


async def update_repeat_call_status(source_visit_id: str, status: int, commentary: str = "") -> None:
    """
    Зафіксувати результат дзвінка Модуля 2.1 для статусів ≠ 1 (booked).

    STATUS=1 (booked) → обробляється через save_b2_booking() під час дзвінка.
    STATUS≠1 → ai_savestate: [source_visit_id (ID з API клініки), status_code]

    status: 2=Відмовився, 3=Сам запишеться, 4=Не додзвонились/Помилка
    """
    commentary = (commentary or "")[:512]
    _STATUS_LABELS = {2: "Відмовився", 3: "Сам запишеться", 4: "Не додзвонились", 5: "Без результату"}
    label = _STATUS_LABELS.get(status, str(status))

    if DEBUG:
        logger.info(f"[DEBUG] update_repeat_call_status source_visit_id={source_visit_id} STATUS={status} ({label})")
        return

    if SKIP_POST:
        logger.info(f"[API] update_repeat_call_status (SKIP_POST): source_visit_id={source_visit_id} STATUS={status} ({label})")
        return

    async with aiohttp.ClientSession() as http:
        async with http.post(
            f"{CLINIC_API_URL}/bridge/call/ai_savestate",
            headers=_make_headers(),
            json={"fields": [str(source_visit_id), status]},
        ) as resp:
            resp.raise_for_status()
            logger.info(f"[API] ai_savestate → source_visit_id={source_visit_id} STATUS={status} ({label})")


async def update_event_status(event_id: str, status: int, commentary: str = "") -> None:
    """
    Відправити результат дзвінка назад до API клініки.

    Args:
        event_id:    ID події (= ID з appointments API)
        status:      1=Підтверджує, 2=Відміняє, 3=Не зрозумів, 4=Не додзвонились
        commentary:  AI-коментар до дзвінка (max 512 символів)

    DEBUG: оновлює STATUS прямо в data/test_visits.json
    """
    _STATUS_LABELS = {1: "Підтверджує", 2: "Відміняє", 3: "Не зрозумів", 4: "Не додзвонились", 5: "Перенаправлено"}
    label = _STATUS_LABELS.get(status, str(status))
    commentary = (commentary or "")[:512]

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
            logger.info(f"[DEBUG] update_event_status → ID={event_id}, STATUS={status} ({label}) | {commentary}")
        else:
            logger.warning(f"[DEBUG] update_event_status → ID={event_id} не знайдено в JSON")
        return

    if SKIP_POST:
        logger.info(f"[API] update_event_status (SKIP_POST): ID={event_id}, STATUS={status} ({label}) | {commentary}")
        return

    async with aiohttp.ClientSession() as http:
        async with http.post(
            f"{CLINIC_API_URL}/bridge/call/updatestate",
            headers=_make_headers(),
            json={"fields": [int(event_id), status, commentary]},
        ) as resp:
            resp.raise_for_status()
            logger.info(f"[API] update_event_status → ID={event_id}, STATUS={status} ({label}) | {commentary}")
