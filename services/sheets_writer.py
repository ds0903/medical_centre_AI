"""
Модуль 2.2 — Google Sheets writer.

Записує результати аналізу дзвінків у таблицю результатів.

Структура аркуша «Результати»:
  Рядок 1: заголовки (генеруються автоматично)
  Рядок 2+: результати дзвінків

Фіксовані колонки (A–K):
  A: ID дзвінка     — Binotel generalCallID
  B: Дата/час       — дата та час дзвінка
  C: Лінія          — внутрішній номер оператора
  D: Номер телефону — номер клієнта
  E: Оператор       — ім'я оператора
  F: Напрям         — вхідний / вихідний
  G: Тривалість (с) — тривалість розмови
  H: Оцінка (%)     — загальна оцінка у відсотках
  I: AI коментар    — короткий коментар AI
  J: Маркери (є)    — використані фрази-маркери
  K: Маркери (немає)— відсутні фрази-маркери

Динамічні колонки (L+):
  Один стовпець на кожен критерій (з аркуша «Конфігурація»)
  Значення: «✓» (виконано) або «✗» (не виконано)

Якщо структура _FIXED_HEADERS змінюється (додається нова колонка) —
ensure_headers() сама вставляє фізичну колонку в Sheets (insertDimension),
що зсуває ВСІ існуючі рядки — старі дані не з'їжджають під новий заголовок.
"""

import asyncio
import logging

logger = logging.getLogger(__name__)

_FIXED_HEADERS = [
    "ID дзвінка",
    "Дата/час",
    "Лінія",
    "Номер телефону",
    "Оператор",
    "Напрям",
    "Тривалість (с)",
    "Оцінка (%)",
    "AI коментар",
    "Маркери (є)",
    "Маркери (немає)",
]


class SheetsWriter:
    """
    Записує результати аналізу дзвінків у Google Sheets.

    Аркуш «Результати»:
      - Рядок 1: заголовки (фіксовані + динамічні критерії)
      - Рядок 2+: дані

    Використовує google-api-python-client (sync) через asyncio.to_thread.
    """

    def __init__(
        self,
        spreadsheet_id: str,
        sheet_name: str = "Результати",
        credentials_path: str = "credentials.json",
    ):
        self.spreadsheet_id = spreadsheet_id
        self.sheet_name = sheet_name
        self.credentials_path = credentials_path
        self._service = None
        self._criteria_order: list[str] = []   # порядок критеріїв у заголовку
        self._headers_confirmed = False          # вже перевірили/оновили заголовки

    # ------------------------------------------------------------------
    # Публічний API
    # ------------------------------------------------------------------

    async def ensure_headers(self, criteria_names: list[str]) -> None:
        """
        Перевірити і за потреби оновити рядок заголовків (рядок 1).

        Безпечно для повторного виклику: якщо заголовки вже відповідають
        списку criteria_names — нічого не змінює.
        """
        self._criteria_order = list(criteria_names)
        expected = _FIXED_HEADERS + list(criteria_names)
        await asyncio.to_thread(self._ensure_headers_sync, expected)
        self._headers_confirmed = True

    async def append_call_result(self, result) -> int:
        """
        Додати рядок з результатом аналізу.

        Args:
            result: CallAnalysisResult (agents/block2/call_analyzer/analyzer.py)

        Returns:
            Номер рядка куди записано (1-indexed).
        """
        if not self._headers_confirmed:
            criteria_names = list(result.criteria_scores.keys())
            await self.ensure_headers(criteria_names)

        row = self._build_row(result)
        row_number = await asyncio.to_thread(self._append_row_sync, row)
        logger.info(f"[SHEETS] Дзвінок {result.call_id} → рядок {row_number}")
        return row_number

    # ------------------------------------------------------------------
    # Внутрішня побудова рядка
    # ------------------------------------------------------------------

    def _build_row(self, result) -> list:
        direction_ua = "вхідний" if result.direction == "inbound" else "вихідний"
        score_pct = f"{result.total_score * 100:.1f}%"
        markers_ok  = ", ".join(result.marker_used)        or "—"
        markers_bad = ", ".join(result.marker_violations)  or "—"

        fixed = [
            result.call_id,
            result.call_datetime,
            result.operator_line,
            result.client_phone,
            result.operator_name,
            direction_ua,
            result.duration_sec,
            score_pct,
            result.ai_comment,
            markers_ok,
            markers_bad,
        ]

        criteria_cells = [
            "✓" if result.criteria_scores.get(name, False) else "✗"
            for name in self._criteria_order
        ]

        return fixed + criteria_cells

    # ------------------------------------------------------------------
    # Sync-операції (запускаються в asyncio.to_thread)
    # ------------------------------------------------------------------

    def _get_service(self):
        if self._service is not None:
            return self._service
        try:
            from google.oauth2 import service_account
            from googleapiclient.discovery import build
        except ImportError:
            raise RuntimeError(
                "google-api-python-client не встановлено. "
                "Виконайте: pip install google-api-python-client google-auth"
            )
        creds = service_account.Credentials.from_service_account_file(
            self.credentials_path,
            scopes=["https://www.googleapis.com/auth/spreadsheets"],
        )
        self._service = build("sheets", "v4", credentials=creds, cache_discovery=False)
        return self._service

    def _get_sheet_id_sync(self) -> int | None:
        service = self._get_service()
        meta = service.spreadsheets().get(
            spreadsheetId=self.spreadsheet_id,
            fields="sheets.properties",
        ).execute()
        for sheet in meta.get("sheets", []):
            props = sheet.get("properties", {})
            if props.get("title") == self.sheet_name:
                return props.get("sheetId")
        return None

    def _ensure_headers_sync(self, expected: list[str]) -> None:
        service = self._get_service()
        result = (
            service.spreadsheets()
            .values()
            .get(
                spreadsheetId=self.spreadsheet_id,
                range=f"{self.sheet_name}!1:1",
            )
            .execute()
        )
        current = result.get("values", [[]])[0] if result.get("values") else []

        if current == expected:
            logger.debug("[SHEETS] Заголовки актуальні")
            return

        # Якщо current — підпослідовність expected (тільки додано нові колонки,
        # нічого не перейменовано/не видалено) — вставляємо фізичні колонки,
        # щоб зсунути ВСІ існуючі рядки і не поламати старі дані.
        insert_positions = self._diff_insertions(current, expected)
        if insert_positions is not None and insert_positions:
            sheet_id = self._get_sheet_id_sync()
            if sheet_id is not None:
                requests = [
                    {
                        "insertDimension": {
                            "range": {
                                "sheetId": sheet_id,
                                "dimension": "COLUMNS",
                                "startIndex": idx,
                                "endIndex": idx + 1,
                            },
                            "inheritFromBefore": False,
                        }
                    }
                    for idx in insert_positions
                ]
                service.spreadsheets().batchUpdate(
                    spreadsheetId=self.spreadsheet_id,
                    body={"requests": requests},
                ).execute()
                logger.info(
                    f"[SHEETS] Вставлено {len(insert_positions)} нових колонок "
                    f"(старі рядки зсунуто, дані збережено)"
                )

        service.spreadsheets().values().update(
            spreadsheetId=self.spreadsheet_id,
            range=f"{self.sheet_name}!A1",
            valueInputOption="RAW",
            body={"values": [expected]},
        ).execute()
        logger.info(f"[SHEETS] Заголовки оновлено ({len(expected)} колонок)")

    @staticmethod
    def _diff_insertions(current: list[str], expected: list[str]) -> list[int] | None:
        """
        Якщо current — підпослідовність expected (тільки вставки, без
        перейменувань/видалень/реордеру) — повертає індекси (0-based, у
        порядку зліва направо) куди треба вставити нові колонки.
        Інакше — None (falls back до простого overwrite заголовків).

        ВАЖЛИВО: індекси рахуються послідовно — insertDimension фізично
        зсуває існуючі колонки, тож вставка по одному індексу за раз
        зліва направо коректно відкриває місце для кожної нової колонки.
        """
        insertions: list[int] = []
        ci = 0
        for ei, name in enumerate(expected):
            if ci < len(current) and current[ci] == name:
                ci += 1
            else:
                insertions.append(ei)
        if ci != len(current):
            # current має щось, чого немає (в тому ж порядку) в expected —
            # перейменування/видалення/реордер — небезпечно автоматично рухати.
            return None
        return insertions

    def _append_row_sync(self, row: list) -> int:
        service = self._get_service()
        resp = (
            service.spreadsheets()
            .values()
            .append(
                spreadsheetId=self.spreadsheet_id,
                range=f"{self.sheet_name}!A:A",
                valueInputOption="USER_ENTERED",
                insertDataOption="INSERT_ROWS",
                body={"values": [row]},
            )
            .execute()
        )
        updated_range = resp.get("updates", {}).get("updatedRange", "")
        try:
            row_part = updated_range.split("!")[1]
            row_num = int("".join(c for c in row_part if c.isdigit())[:6])
        except Exception:
            row_num = -1
        return row_num
