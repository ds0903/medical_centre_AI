"""
Google Sheets writer для Модуля 2.2.

Записує результати аналізу дзвінків в Google Sheets.
Аутентифікація через service account (credentials.json — вже є в репо).
"""

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from agents.block2.call_analyzer.analyzer import CallAnalysisResult

logger = logging.getLogger(__name__)

# Колонки результатів (порядок = порядок в таблиці)
RESULT_COLUMNS = [
    "Дата та час",
    "ID дзвінка",
    "Лінія",
    "Ім'я оператора",
    "Тривалість (с)",
    "Напрям",           # вхідний / вихідний
    "Коментар AI",
    "Загальна оцінка",
    "Посилання на аудіо",
    # далі динамічно: по одній колонці на кожен критерій + маркер
]


class SheetsWriter:
    """Записує та оновлює рядки в Google Sheets."""

    def __init__(self, spreadsheet_id: str, result_sheet_name: str = "Результати"):
        self.spreadsheet_id = spreadsheet_id
        self.sheet_name = result_sheet_name
        self._client = None   # google.oauth2 / googleapiclient ініціалізуються в connect()

    async def connect(self, credentials_path: str = "credentials.json") -> None:
        """Ініціалізувати Google Sheets API клієнт."""
        # TODO: google-auth + googleapiclient.discovery.build("sheets", "v4", ...)
        raise NotImplementedError

    async def append_call_result(self, result: "CallAnalysisResult") -> None:
        """Додати рядок з результатом аналізу дзвінка."""
        # TODO: зібрати row зі значень result + criteria_scores + marker_violations
        # → sheets.spreadsheets().values().append(...)
        logger.info(f"[SHEETS] Записуємо результат дзвінка {result.call_id}")
        raise NotImplementedError

    async def ensure_headers(self, criteria_names: list[str], marker_names: list[str]) -> None:
        """
        Переконатись що заголовки таблиці відповідають поточним критеріям.
        Викликати при старті або після оновлення конфігурації.
        """
        # TODO: порівняти поточний рядок 1 з очікуваними заголовками → оновити якщо різняться
        raise NotImplementedError
