"""
Керування запуском дзвінків з Google Sheets — аркуш «Запуск».

Структура аркуша:
  A: Модуль        («Модуль 1» — нагадування про візит, «Модуль 2» — повторні візити)
  B: Команда       («Старт» — дзвонимо; «Стоп» або порожньо — не дзвонимо)
  C: Статус бота   (пише сам бот: яку команду побачив і коли)
  D: Опис

Перед кожним дзвінком модуль читає свою команду. «Старт» → дзвінок іде далі. Усе інше →
бот чекає на місці і перечитує команду кожні RUN_CONTROL_POLL_SEC секунд. Дзвінок, що вже
триває, не переривається. Задачі лишаються pending — після «Старт» обдзвін продовжується
з того ж місця.

Якщо таблиця недоступна — діє остання відома команда; якщо її ще не було — дзвонимо
(збій Google не повинен зупиняти бот). Вимкнено в DEBUG і коли ID таблиці не задано.

Аркуш створюється сам при старті сервісу (ensure_ready) із «Старт» в обох рядках — щоб
перший запуск не зупинив дзвінки. Створити/перевірити вручну: python -m services.run_control

Використання:
    from services import run_control
    ok = await run_control.wait_for_start(run_control.MODULE_1, should_abort=lambda: False)
"""

import asyncio
import logging
import time
from datetime import datetime
from typing import Callable
from zoneinfo import ZoneInfo

from config import (
    B2_CREDENTIALS_PATH,
    DEBUG,
    RUN_CONTROL_POLL_SEC,
    RUN_CONTROL_SHEET_NAME,
    RUN_CONTROL_SHEETS_ID,
)

logger = logging.getLogger(__name__)

_KYIV = ZoneInfo("Europe/Kyiv")
_IO_TIMEOUT_SEC = 10

MODULE_1 = "Модуль 1"
MODULE_2 = "Модуль 2"

CMD_START = "Старт"
CMD_STOP = "Стоп"

HEADERS = ["Модуль", "Команда", "Статус бота", "Опис"]
_DEFAULT_ROWS = [
    [MODULE_1, CMD_START, "", "Нагадування про візит (Блок 1)"],
    [MODULE_2, CMD_START, "", "Повторні візити (Блок 2.1)"],
]
_START_WORDS = ("старт", "start")


def _norm(text: str) -> str:
    return text.strip().casefold()


def _is_start(command: str) -> bool:
    return _norm(command) in _START_WORDS


class _SheetClient:
    def __init__(self) -> None:
        self._service = None
        self._rows: dict[str, int] = {}   # нормалізована назва модуля → номер рядка в аркуші
        self.lock = asyncio.Lock()

    @property
    def _quoted(self) -> str:
        return "'" + RUN_CONTROL_SHEET_NAME.replace("'", "''") + "'"

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

    def ensure_sheet_sync(self) -> bool:
        """Створює аркуш із заголовками та рядками модулів, якщо його ще немає. True — створено."""
        svc = self._get_service()
        sid = RUN_CONTROL_SHEETS_ID
        meta = svc.spreadsheets().get(
            spreadsheetId=sid, fields="sheets.properties(sheetId,title)"
        ).execute()
        if any(s["properties"]["title"] == RUN_CONTROL_SHEET_NAME for s in meta.get("sheets", [])):
            return False

        resp = svc.spreadsheets().batchUpdate(
            spreadsheetId=sid,
            body={"requests": [{"addSheet": {"properties": {
                "title": RUN_CONTROL_SHEET_NAME,
                "gridProperties": {"frozenRowCount": 1},
            }}}]},
        ).execute()
        sheet_id = resp["replies"][0]["addSheet"]["properties"]["sheetId"]

        svc.spreadsheets().values().update(
            spreadsheetId=sid,
            range=f"{self._quoted}!A1",
            valueInputOption="RAW",
            body={"values": [HEADERS] + _DEFAULT_ROWS},
        ).execute()
        svc.spreadsheets().batchUpdate(
            spreadsheetId=sid,
            body={"requests": [
                {"repeatCell": {
                    "range": {"sheetId": sheet_id, "startRowIndex": 0, "endRowIndex": 1},
                    "cell": {"userEnteredFormat": {"textFormat": {"bold": True}}},
                    "fields": "userEnteredFormat.textFormat.bold",
                }},
                # Випадаючий список у колонці «Команда» (strict=False — не лякає, якщо впишуть інше)
                {"setDataValidation": {
                    "range": {"sheetId": sheet_id, "startRowIndex": 1,
                              "endRowIndex": 1 + len(_DEFAULT_ROWS),
                              "startColumnIndex": 1, "endColumnIndex": 2},
                    "rule": {
                        "condition": {"type": "ONE_OF_LIST", "values": [
                            {"userEnteredValue": CMD_START}, {"userEnteredValue": CMD_STOP},
                        ]},
                        "showCustomUi": True,
                        "strict": False,
                    },
                }},
            ]},
        ).execute()
        return True

    def read_commands_sync(self) -> dict[str, str]:
        """{нормалізована назва модуля: команда}. Модуля без рядка в результаті немає."""
        svc = self._get_service()
        values = svc.spreadsheets().values().get(
            spreadsheetId=RUN_CONTROL_SHEETS_ID,
            range=f"{self._quoted}!A2:B50",
        ).execute().get("values", [])
        commands: dict[str, str] = {}
        self._rows = {}
        for idx, row in enumerate(values):
            name = _norm(row[0]) if row else ""
            if not name:
                continue
            commands[name] = row[1] if len(row) > 1 else ""
            self._rows[name] = idx + 2   # values[0] = рядок 2 (рядок 1 — заголовки)
        return commands

    def write_status_sync(self, module: str, text: str) -> None:
        row = self._rows.get(_norm(module))
        if row is None:
            return
        self._get_service().spreadsheets().values().update(
            spreadsheetId=RUN_CONTROL_SHEETS_ID,
            range=f"{self._quoted}!C{row}",
            valueInputOption="RAW",
            body={"values": [[text]]},
        ).execute()


_client = _SheetClient()
_last_state: dict[str, bool] = {}     # модуль → остання відома команда (True = «Старт»)
_last_status: dict[str, str] = {}     # модуль → що востаннє записали в «Статус бота»


def is_enabled() -> bool:
    return bool(RUN_CONTROL_SHEETS_ID) and not DEBUG


async def ensure_ready() -> None:
    """
    Викликається при старті сервісу: створює аркуш «Запуск», якщо його немає.
    Ніколи не кидає виняток — збій Sheets не повинен зупиняти бота.
    """
    if not is_enabled():
        return
    try:
        async with _client.lock:
            created = await asyncio.wait_for(
                asyncio.to_thread(_client.ensure_sheet_sync), timeout=_IO_TIMEOUT_SEC * 3
            )
        if created:
            logger.info(f"[RUN CONTROL] Створено аркуш «{RUN_CONTROL_SHEET_NAME}» (обидва модулі: «{CMD_START}»)")
        else:
            logger.info(f"[RUN CONTROL] Аркуш «{RUN_CONTROL_SHEET_NAME}» готовий")
    except Exception as e:
        logger.warning(f"[RUN CONTROL] Не вдалось перевірити/створити аркуш «{RUN_CONTROL_SHEET_NAME}»: {e!r}")


async def _publish_status(module: str, running: bool) -> None:
    """Записує в «Статус бота», яку команду бот побачив. Тільки при зміні; помилки ігноруємо."""
    when = datetime.now(_KYIV).strftime("%d.%m %H:%M")
    text = f"Працює (команду прийнято {when})" if running else f"Зупинено, чекаю «{CMD_START}» (з {when})"
    # Порівнюємо лише стан, а не час — інакше писали б у таблицю щоразу
    if _last_status.get(module, "").split(" (")[0] == text.split(" (")[0]:
        return
    try:
        async with _client.lock:
            await asyncio.wait_for(
                asyncio.to_thread(_client.write_status_sync, module, text), timeout=_IO_TIMEOUT_SEC
            )
        _last_status[module] = text
    except Exception as e:
        logger.debug(f"[RUN CONTROL] Не вдалось записати статус {module}: {e!r}")


async def is_running(module: str) -> bool:
    """True — команда «Старт», можна дзвонити. Одне читання аркуша."""
    if not is_enabled():
        return True
    try:
        async with _client.lock:
            commands = await asyncio.wait_for(
                asyncio.to_thread(_client.read_commands_sync), timeout=_IO_TIMEOUT_SEC
            )
    except Exception as e:
        known = _last_state.get(module)
        if known is None:
            logger.warning(
                f"[RUN CONTROL] {module}: не вдалось прочитати аркуш «{RUN_CONTROL_SHEET_NAME}» ({e!r}), "
                f"команди ще не було — дзвонимо"
            )
            return True
        logger.warning(
            f"[RUN CONTROL] {module}: не вдалось прочитати аркуш «{RUN_CONTROL_SHEET_NAME}» ({e!r}) — "
            f"діє остання відома команда: {'Старт' if known else 'Стоп'}"
        )
        return known

    command = commands.get(_norm(module))
    running = _is_start(command or "")
    if _last_state.get(module) != running:
        shown = "рядка модуля немає в аркуші" if command is None else (f"«{command}»" if command.strip() else "порожньо")
        logger.info(
            f"[RUN CONTROL] {module}: команда {shown} → "
            f"{'дзвонимо' if running else 'дзвінки зупинено'}"
        )
    _last_state[module] = running
    await _publish_status(module, running)
    return running


async def _release_sip() -> None:
    """
    Пауза не має тримати SIP-замок: між дзвінками він утримується до наступного shutdown(),
    і інший модуль за 150с отримав би «SIP зайнятий» та теж не зміг би дзвонити.
    """
    if DEBUG:
        return
    try:
        from services import binotel
        await binotel.shutdown()
    except Exception as e:
        logger.warning(f"[RUN CONTROL] Не вдалось звільнити SIP на паузі: {e!r}")


async def wait_for_start(module: str, should_abort: Callable[[], bool]) -> bool:
    """
    Чекає команди «Старт» для модуля.

    True  — можна дзвонити (команда «Старт» або перевірка вимкнена).
    False — очікування перервано: should_abort() став True (кінець робочого часу).
    """
    if await is_running(module):
        return True

    logger.warning(f"[RUN CONTROL] {module}: дзвінки на паузі — чекаємо команди «{CMD_START}»")
    await _release_sip()
    paused_at = time.monotonic()
    while True:
        if should_abort():
            logger.info(f"[RUN CONTROL] {module}: кінець робочого часу під час паузи")
            return False
        await asyncio.sleep(RUN_CONTROL_POLL_SEC)
        if should_abort():
            logger.info(f"[RUN CONTROL] {module}: кінець робочого часу під час паузи")
            return False
        if await is_running(module):
            logger.info(
                f"[RUN CONTROL] {module}: команда «{CMD_START}» — продовжуємо "
                f"(пауза тривала {(time.monotonic() - paused_at) / 60:.1f} хв)"
            )
            return True


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    if not RUN_CONTROL_SHEETS_ID:
        raise SystemExit("RUN_CONTROL_SHEETS_ID / B1_LOG_SHEETS_ID / B2_RESULT_SHEETS_ID не задано")
    if DEBUG:
        raise SystemExit("ENV=debug — керування вимкнено; запустіть із ENV=production")
    asyncio.run(ensure_ready())
