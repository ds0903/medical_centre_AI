"""
Читає критерії оцінки та фрази-маркери з конфігураційних аркушів Google Sheets.

Замовник сам оновлює критерії — AI зчитує перед кожним аналізом (з кешем).

Структура аркуша «Конфігурація» (критерії):
  Колонка A: Блок          (напр. «Привітання», «Виявлення потреб»)
  Колонка B: Назва         (назва критерію)
  Колонка C: Індикатор     (опис для AI що шукати в транскрипті)
  Колонка D: Активний      («Так» / «Ні»)
  Колонка E: Вага          (число 0.0–1.0, необов'язково; default=1.0)

Структура аркуша «Маркери» (фрази-маркери):
  Колонка A: Фраза         (ключова фраза/шаблон)
  Колонка B: Умова         (коли має бути вжита)
  Колонка C: Активний      («Так» / «Ні»)
"""

import asyncio
import logging
import time
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

_CACHE_TTL_SEC = 600   # перезавантажувати конфіг не частіше ніж раз на 10 хвилин


@dataclass
class Criterion:
    block: str
    name: str
    indicator: str   # опис для AI що шукати в транскрипті
    active: bool
    weight: float = 1.0


@dataclass
class MarkerPhrase:
    phrase: str
    condition: str   # коли має бути вжита
    active: bool


class ConfigReader:
    """
    Зчитує та кешує конфігурацію критеріїв і маркерів з Google Sheets.

    Використовує google-api-python-client (sync) через asyncio.to_thread.
    """

    def __init__(
        self,
        spreadsheet_id: str,
        credentials_path: str = "credentials.json",
        criteria_sheet: str = "Конфігурація",
        markers_sheet: str = "Маркери",
    ):
        self.spreadsheet_id = spreadsheet_id
        self.credentials_path = credentials_path
        self.criteria_sheet = criteria_sheet
        self.markers_sheet = markers_sheet

        self._criteria_cache: list[Criterion] = []
        self._markers_cache: list[MarkerPhrase] = []
        self._last_load: float = 0.0
        self._service = None  # googleapiclient sheets service (lazy init)

    # ------------------------------------------------------------------
    # Публічні методи
    # ------------------------------------------------------------------

    async def get_criteria(self, force_reload: bool = False) -> list[Criterion]:
        """Повернути активні критерії оцінки."""
        await self._ensure_loaded(force_reload)
        return [c for c in self._criteria_cache if c.active]

    async def get_markers(self, force_reload: bool = False) -> list[MarkerPhrase]:
        """Повернути активні фрази-маркери."""
        await self._ensure_loaded(force_reload)
        return [m for m in self._markers_cache if m.active]

    async def get_all_criteria(self) -> list[Criterion]:
        """Всі критерії (включаючи неактивні) — для заголовків Sheets."""
        await self._ensure_loaded()
        return list(self._criteria_cache)

    # ------------------------------------------------------------------
    # Внутрішні методи
    # ------------------------------------------------------------------

    async def _ensure_loaded(self, force: bool = False) -> None:
        now = time.monotonic()
        if force or not self._criteria_cache or now - self._last_load > _CACHE_TTL_SEC:
            await self._reload()

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
            scopes=["https://www.googleapis.com/auth/spreadsheets.readonly"],
        )
        self._service = build("sheets", "v4", credentials=creds)
        return self._service

    def _read_sheet_sync(self, sheet_name: str) -> list[list[str]]:
        """Синхронне читання аркуша (запускається в executor)."""
        service = self._get_service()
        result = (
            service.spreadsheets()
            .values()
            .get(
                spreadsheetId=self.spreadsheet_id,
                range=f"{sheet_name}!A2:F",  # пропускаємо рядок заголовків
            )
            .execute()
        )
        return result.get("values", [])

    async def _reload(self) -> None:
        logger.info(f"[CONFIG READER] Завантаження конфігурації з Sheets ({self.spreadsheet_id})")
        try:
            # Критерії
            criteria_rows = await asyncio.to_thread(self._read_sheet_sync, self.criteria_sheet)
            self._criteria_cache = []
            for row in criteria_rows:
                if not row or not any(row):
                    continue
                block     = (row[0] if len(row) > 0 else "").strip()
                name      = (row[1] if len(row) > 1 else "").strip()
                indicator = (row[2] if len(row) > 2 else "").strip()
                active_s  = (row[3] if len(row) > 3 else "Так").strip().lower()
                weight_s  = (row[4] if len(row) > 4 else "1.0").strip()
                if not name:
                    continue
                active = active_s in ("так", "yes", "true", "1", "+")
                try:
                    weight = float(weight_s) if weight_s else 1.0
                except ValueError:
                    weight = 1.0
                self._criteria_cache.append(Criterion(
                    block=block, name=name, indicator=indicator,
                    active=active, weight=weight,
                ))

            # Маркери
            try:
                markers_rows = await asyncio.to_thread(self._read_sheet_sync, self.markers_sheet)
                self._markers_cache = []
                for row in markers_rows:
                    if not row or not any(row):
                        continue
                    phrase    = (row[0] if len(row) > 0 else "").strip()
                    condition = (row[1] if len(row) > 1 else "").strip()
                    active_s  = (row[2] if len(row) > 2 else "Так").strip().lower()
                    if not phrase:
                        continue
                    active = active_s in ("так", "yes", "true", "1", "+")
                    self._markers_cache.append(MarkerPhrase(
                        phrase=phrase, condition=condition, active=active
                    ))
            except Exception as _me:
                logger.warning(f"[CONFIG READER] Аркуш маркерів не знайдено або помилка: {_me}")
                self._markers_cache = []

            self._last_load = time.monotonic()
            logger.info(
                f"[CONFIG READER] Завантажено {len(self._criteria_cache)} критеріїв "
                f"(активних: {sum(1 for c in self._criteria_cache if c.active)}), "
                f"{len(self._markers_cache)} маркерів"
            )

        except Exception as e:
            logger.error(f"[CONFIG READER] Помилка завантаження конфігурації: {e}", exc_info=True)
            if not self._criteria_cache:
                raise
