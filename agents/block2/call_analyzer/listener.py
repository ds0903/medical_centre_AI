"""
Модуль 2.2 — Binotel polling listener.

Кожні B2_POLL_INTERVAL_MIN хвилин:
  1. Запитує Binotel stats/calls-for-period за останній інтервал
  2. Фільтрує по MONITORED_LINES / EXCLUDED_LINES
  3. Перевіряє DB AnalyzedCall (дедуплікація)
  4. Запускає CallAnalyzer.process_call() для кожного нового дзвінка
  5. Зберігає AnalyzedCall у DB (щоб не аналізувати повторно)
"""

import asyncio
import logging
from datetime import datetime, timedelta, timezone

from db.database import get_session as async_session
from db.models import AnalyzedCall
from config import B2_POLL_INTERVAL_MIN

logger = logging.getLogger(__name__)


class CallAnalyzerListener:
    """
    Polling listener — опитує Binotel на нові завершені дзвінки і передає на аналіз.
    """

    def __init__(self, analyzer, binotel_rest=None):
        """
        Args:
            analyzer: екземпляр CallAnalyzer
            binotel_rest: BinotelRestClient (якщо None — створюється всередині)
        """
        self.analyzer = analyzer
        self._binotel = binotel_rest
        self._poll_interval = B2_POLL_INTERVAL_MIN * 60   # секунди
        self._running = False
        self._last_poll_dt: datetime | None = None

    # ------------------------------------------------------------------
    # Запуск / зупинка
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Запустити polling loop (блокуючий, до виклику stop())."""
        self._running = True
        logger.info(
            f"[LISTENER] Старт. Інтервал polling: {B2_POLL_INTERVAL_MIN} хв. "
            f"Моніторовані лінії: {self.analyzer.MONITORED_LINES if hasattr(self.analyzer, 'MONITORED_LINES') else 'з analyzer'}"
        )
        while self._running:
            try:
                await self._poll_once()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"[LISTENER] Помилка polling: {e}", exc_info=True)

            await asyncio.sleep(self._poll_interval)

    def stop(self) -> None:
        self._running = False

    # ------------------------------------------------------------------
    # Одна ітерація
    # ------------------------------------------------------------------

    async def _poll_once(self) -> None:
        now = datetime.now(timezone.utc)
        if self._last_poll_dt is None:
            # Перший запит: за останній інтервал
            since = now - timedelta(minutes=B2_POLL_INTERVAL_MIN)
        else:
            since = self._last_poll_dt

        self._last_poll_dt = now

        date_from = since.strftime("%Y-%m-%d %H:%M:%S")
        date_to   = now.strftime("%Y-%m-%d %H:%M:%S")

        logger.info(f"[LISTENER] Polling Binotel: {date_from} → {date_to}")

        binotel = self._get_binotel()
        try:
            calls = await binotel.get_calls_for_period(date_from, date_to)
        except Exception as e:
            logger.error(f"[LISTENER] get_calls_for_period помилка: {e!r}")
            return

        if not calls:
            logger.debug("[LISTENER] Нових дзвінків немає")
            return

        # Фільтр: тільки дзвінки де recordingStatus == 'uploaded' (запис є на сервері)
        calls_with_rec = [
            c for c in calls
            if c.get("recordingStatus") == "uploaded"
        ]
        logger.info(f"[LISTENER] Знайдено {len(calls)} дзвінків, з записом: {len(calls_with_rec)}")

        # Дедуплікація по DB
        new_calls = await self._filter_not_analyzed(calls_with_rec)
        logger.info(f"[LISTENER] Нових (не аналізованих): {len(new_calls)}")

        # Аналіз
        for call_meta in new_calls:
            await self._process_one(call_meta)

    async def _filter_not_analyzed(self, calls: list[dict]) -> list[dict]:
        """Повертає тільки ті дзвінки яких немає в AnalyzedCall."""
        if not calls:
            return []

        call_ids = [
            str(c.get("callId", c.get("generalCallID", "")))
            for c in calls
        ]

        from sqlalchemy import select
        async with async_session() as session:
            result = await session.execute(
                select(AnalyzedCall.call_id).where(AnalyzedCall.call_id.in_(call_ids))
            )
            already_done = {row[0] for row in result.fetchall()}

        return [
            c for c in calls
            if str(c.get("callId", c.get("generalCallID", ""))) not in already_done
        ]

    async def _process_one(self, call_meta: dict) -> None:
        call_id      = str(call_meta.get("generalCallID", call_meta.get("callId", "")))
        binotel_call_id = str(call_meta.get("callID", call_id))   # callID != generalCallID для деяких дзвінків
        line         = str(call_meta.get("internalNumber", call_meta.get("operatorLine", "")))

        # Отримуємо URL запису через stats/call-record (параметр generalCallID)
        if not call_meta.get("recordingUrl") and not call_meta.get("url"):
            rec_url = await self._get_binotel().get_call_record_url(call_id)
            if rec_url:
                call_meta["recordingUrl"] = rec_url
                logger.info(f"[LISTENER] recordingUrl для {call_id}: {rec_url[:80]}")

        try:
            result = await self.analyzer.process_call(call_meta)
            ok = 1
            err_msg = None
            sheets_row = None

            if result is not None:
                sheets_row = None   # sheets_row поки не повертається з append_call_result
                logger.info(f"[LISTENER] ✓ {call_id} (лінія {line}) score={result.total_score:.2f}")
            else:
                # None = лінія не моніторується або немає запису (не помилка)
                logger.debug(f"[LISTENER] {call_id} — пропущено (не моніторується або без запису)")
                return   # Не зберігаємо в DB — не витрачаємо рядки

        except Exception as e:
            logger.error(f"[LISTENER] Аналіз {call_id} провалився: {e!r}", exc_info=True)
            ok = 0
            err_msg = str(e)[:500]
            sheets_row = None

        # Зберегти в DB щоб не аналізувати повторно
        await self._save_analyzed(call_id, line, call_meta, ok, err_msg, sheets_row)

    async def _save_analyzed(
        self,
        call_id: str,
        line: str,
        call_meta: dict,
        ok: int,
        error_message: str | None,
        sheets_row: int | None,
    ) -> None:
        call_dt = call_meta.get("dateTime", call_meta.get("callDate", ""))
        async with async_session() as session:
            record = AnalyzedCall(
                call_id       = call_id,
                operator_line = line,
                call_datetime = str(call_dt),
                result_ok     = ok,
                sheets_row    = sheets_row,
                error_message = error_message,
            )
            session.add(record)
            await session.commit()

    def _get_binotel(self):
        if self._binotel is None:
            from services.binotel_rest import BinotelRestClient
            self._binotel = BinotelRestClient()
        return self._binotel
