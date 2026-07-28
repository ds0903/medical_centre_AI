"""
Читає критерії оцінки та фрази-маркери з конфігураційного аркуша Google Sheets.

Замовник сам оновлює критерії — AI зчитує перед кожним аналізом.
Структура конфіг аркуша (з ТЗ розділ 11.3):
  Колонки: Блок | Назва критерію | Індикатор для AI | Активний (Так/Ні)
"""

import logging
from dataclasses import dataclass

logger = logging.getLogger(__name__)


@dataclass
class Criterion:
    block: str
    name: str
    indicator: str    # опис для AI що шукати в транскрипті
    active: bool
    weight: float = 1.0


@dataclass
class MarkerPhrase:
    phrase: str
    condition: str    # коли має бути вжита
    active: bool


class ConfigReader:
    """Зчитує та кешує конфігурацію критеріїв з Google Sheets."""

    def __init__(self, sheets_client, spreadsheet_id: str, config_sheet_name: str = "Конфігурація"):
        self.sheets = sheets_client
        self.spreadsheet_id = spreadsheet_id
        self.sheet_name = config_sheet_name
        self._criteria_cache: list[Criterion] | None = None
        self._markers_cache: list[MarkerPhrase] | None = None

    async def get_criteria(self, force_reload: bool = False) -> list[Criterion]:
        """Повернути активні критерії оцінки."""
        if self._criteria_cache is None or force_reload:
            await self._reload()
        return [c for c in (self._criteria_cache or []) if c.active]

    async def get_markers(self, force_reload: bool = False) -> list[MarkerPhrase]:
        """Повернути активні фрази-маркери."""
        if self._markers_cache is None or force_reload:
            await self._reload()
        return [m for m in (self._markers_cache or []) if m.active]

    async def _reload(self) -> None:
        """Завантажити конфігурацію з Sheets."""
        # TODO: Google Sheets API → зчитати рядки → розпарсити в Criterion / MarkerPhrase
        logger.info("[CONFIG] Завантаження конфігурації критеріїв з Sheets...")
        raise NotImplementedError
