"""
Журнал дзвінків Модуля 1 (нагадування про візит) → Google Sheets.

Пише в ту саму таблицю, що й Модуль 2.2 (B1_LOG_SHEETS_ID, за замовчуванням
B2_RESULT_SHEETS_ID), але в окремий аркуш B1_LOG_SHEET_NAME. Якщо аркуша ще немає —
створює його сам (із заголовками, жирним шрифтом, закріпленим рядком і форматом дат).

Один рядок = одна спроба дзвінка. Найновіші рядки зверху.
Запис іде у фоні й ніколи не кидає виняток у логіку дзвінків.

Використання:
    from services import call_log_sheets
    call_log_sheets.log_call(task, "confirmed", duration_sec, attempts=1)
"""

import asyncio
import logging
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from config import B1_LOG_SHEET_NAME, B1_LOG_SHEETS_ID, B2_CREDENTIALS_PATH, DEBUG

logger = logging.getLogger(__name__)

_KYIV = ZoneInfo("Europe/Kyiv")
_SHEETS_EPOCH = datetime(1899, 12, 30)
_WRITE_TIMEOUT_SEC = 30

HEADERS = [
    "Дата/час дзвінка",
    "ПІБ пацієнта",
    "Телефон",
    "Лікар",
    "Запис пацієнта",
    "Дія бота",
    "Тривалість (с)",
    "Спроба",
    "ID візиту",
]
_DATETIME_COLUMNS = (0, 4)   # колонки з датою/часом

_ACTION_LABELS = {
    "confirmed":    "Підтвердив запис",
    "cancelled":    "Скасував запис",
    "rescheduled":  "Перенесено запис",
    "transferred":  "Передано оператору",
    "no_answer":    "Не відповів",
    "wrong_number": "Не той номер / не записувався",
    "no_result":    "Без результату",
    "error":        "Помилка дзвінка",
}


def _serial(dt: datetime | None) -> float | str:
    """Дата/час → серійний номер Google Sheets (реальна дата, а не текст)."""
    if dt is None:
        return ""
    naive = dt.replace(tzinfo=None)
    return round((naive - _SHEETS_EPOCH).total_seconds() / 86400, 6)


def _build_row(
    *,
    patient_name: str,
    phone: str,
    doctor: str,
    visit_dt: datetime | None,
    result: str,
    duration: float | None,
    attempts: int,
    visit_id: str,
    finished_at: datetime,
) -> list:
    # Початок дзвінка = момент завершення мінус тривалість (окремо старт не зберігається).
    started_at = finished_at - timedelta(seconds=duration) if duration else finished_at
    return [
        _serial(started_at),
        patient_name,
        phone,
        doctor,
        _serial(visit_dt),
        _ACTION_LABELS.get(result, result),
        round(duration) if duration else "",
        attempts,
        visit_id,
    ]


class _CallLogWriter:
    def __init__(self) -> None:
        self._service = None
        self._sheet_id: int | None = None
        self._ready = False
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------------ sync

    @property
    def _quoted(self) -> str:
        return "'" + B1_LOG_SHEET_NAME.replace("'", "''") + "'"

    def _get_service(self):
        if self._service is None:
            from google.oauth2 import service_account
            from googleapiclient.discovery import build
            creds = service_account.Credentials.from_service_account_file(
                B2_CREDENTIALS_PATH,
                scopes=["https://www.googleapis.com/auth/spreadsheets"],
            )
            self._service = build("sheets", "v4", credentials=creds, cache_discovery=False)
        return self._service

    def _ensure_sheet_sync(self) -> None:
        svc = self._get_service()
        sid = B1_LOG_SHEETS_ID
        meta = svc.spreadsheets().get(
            spreadsheetId=sid, fields="sheets.properties(sheetId,title)"
        ).execute()
        sheet_id = next(
            (s["properties"]["sheetId"] for s in meta.get("sheets", [])
             if s["properties"]["title"] == B1_LOG_SHEET_NAME),
            None,
        )
        needs_format = False
        if sheet_id is None:
            resp = svc.spreadsheets().batchUpdate(
                spreadsheetId=sid,
                body={"requests": [{"addSheet": {"properties": {
                    "title": B1_LOG_SHEET_NAME,
                    "gridProperties": {"frozenRowCount": 1},
                }}}]},
            ).execute()
            sheet_id = resp["replies"][0]["addSheet"]["properties"]["sheetId"]
            needs_format = True
            logger.info(f"[CALL LOG] Створено аркуш «{B1_LOG_SHEET_NAME}»")

        current = svc.spreadsheets().values().get(
            spreadsheetId=sid, range=f"{self._quoted}!1:1"
        ).execute().get("values", [[]])
        current = current[0] if current else []
        if current != HEADERS:
            svc.spreadsheets().values().update(
                spreadsheetId=sid,
                range=f"{self._quoted}!A1",
                valueInputOption="RAW",
                body={"values": [HEADERS]},
            ).execute()
            needs_format = True

        if needs_format:
            requests = [{
                "repeatCell": {
                    "range": {"sheetId": sheet_id, "startRowIndex": 0, "endRowIndex": 1},
                    "cell": {"userEnteredFormat": {"textFormat": {"bold": True}}},
                    "fields": "userEnteredFormat.textFormat.bold",
                }
            }]
            for col in _DATETIME_COLUMNS:
                requests.append({
                    "repeatCell": {
                        "range": {"sheetId": sheet_id, "startRowIndex": 1,
                                  "startColumnIndex": col, "endColumnIndex": col + 1},
                        "cell": {"userEnteredFormat": {"numberFormat": {
                            "type": "DATE_TIME", "pattern": "yyyy-mm-dd hh:mm:ss"}}},
                        "fields": "userEnteredFormat.numberFormat",
                    }
                })
            svc.spreadsheets().batchUpdate(
                spreadsheetId=sid, body={"requests": requests}
            ).execute()

        self._sheet_id = sheet_id
        self._ready = True

    def _write_sync(self, row: list) -> None:
        if not self._ready:
            self._ensure_sheet_sync()
        svc = self._get_service()
        sid = B1_LOG_SHEETS_ID
        try:
            # Новий рядок одразу під заголовком — найсвіжіші дзвінки зверху.
            svc.spreadsheets().batchUpdate(
                spreadsheetId=sid,
                body={"requests": [{"insertDimension": {
                    "range": {"sheetId": self._sheet_id, "dimension": "ROWS",
                              "startIndex": 1, "endIndex": 2},
                    "inheritFromBefore": False,
                }}]},
            ).execute()
            svc.spreadsheets().values().update(
                spreadsheetId=sid,
                range=f"{self._quoted}!A2",
                valueInputOption="RAW",
                body={"values": [row]},
            ).execute()
        except Exception:
            self._ready = False   # наступного разу перевіримо аркуш заново (могли видалити)
            raise

    # ----------------------------------------------------------------- async

    async def write(self, row: list) -> None:
        async with self._lock:
            await asyncio.wait_for(asyncio.to_thread(self._write_sync, row), timeout=_WRITE_TIMEOUT_SEC)


_writer = _CallLogWriter()
_pending: set[asyncio.Task] = set()


def is_enabled() -> bool:
    return bool(B1_LOG_SHEETS_ID) and not DEBUG


async def _safe_write(row: list) -> None:
    try:
        await _writer.write(row)
        logger.info(f"[CALL LOG] Записано в «{B1_LOG_SHEET_NAME}»: {row[1]} → {row[5]}")
    except Exception as e:
        logger.warning(f"[CALL LOG] Не вдалось записати в Google Sheets: {e!r}")


def log_call(
    task,
    result: str,
    duration: float | None = None,
    attempts: int = 0,
) -> None:
    """
    Додати рядок у журнал (fire-and-forget). Ніколи не кидає виняток.

    result: confirmed | cancelled | rescheduled | transferred | no_answer |
            wrong_number | no_result | error
    """
    if not is_enabled():
        return
    try:
        row = _build_row(
            patient_name=task.patient_name or "",
            phone=task.patient_phone or "",
            doctor=task.doctor_name or "",
            visit_dt=task.visit_datetime,
            result=result,
            duration=duration,
            attempts=attempts,
            visit_id=str(task.visit_id),
            finished_at=datetime.now(_KYIV),
        )
        t = asyncio.get_running_loop().create_task(_safe_write(row), name="b1_call_log")
        _pending.add(t)
        t.add_done_callback(_pending.discard)
    except Exception as e:
        logger.warning(f"[CALL LOG] Не вдалось сформувати рядок: {e!r}")
