"""
Binotel REST API клієнт.

Документація: http://developers.binotel.ua/#rest-api-calls
Base URL: https://api.binotel.com/api/4.0/
Auth: POST JSON з полями key + secret (без підпису)
"""

import asyncio
import logging

import aiohttp

from config import BINOTEL_API_KEY, BINOTEL_API_SECRET, BINOTEL_API_BASE

logger = logging.getLogger(__name__)


class BinotelRestClient:
    """Низькорівневий HTTP клієнт до Binotel REST API."""

    async def send_request(self, endpoint: str, params: dict, _retries: int = 3) -> dict:
        """
        Відправити POST запит до Binotel API з retry логікою.

        Args:
            endpoint: шлях без версії, напр. "calls/internal-number-to-external-number"
            params:   параметри запиту (без key/secret — вони додаються автоматично)
            _retries: кількість спроб (default 3)

        Returns:
            dict з відповіддю. Поле "status" == "success" або "error".

        Retry: мережеві помилки та 5xx з exponential backoff (1с, 2с).
        Не ретраїть 4xx (клієнтські помилки).
        """
        payload = {
            "key": BINOTEL_API_KEY,
            "secret": BINOTEL_API_SECRET,
            **params,
        }
        url = f"{BINOTEL_API_BASE}/{endpoint}.json"

        last_error: Exception | None = None
        for attempt in range(1, _retries + 1):
            try:
                async with aiohttp.ClientSession() as http:
                    async with http.post(
                        url,
                        json=payload,
                        headers={"Content-Type": "application/json"},
                        timeout=aiohttp.ClientTimeout(total=60),
                    ) as resp:
                        if resp.status >= 500 and attempt < _retries:
                            wait = 2 ** (attempt - 1)
                            logger.warning(
                                f"[BINOTEL REST] {endpoint} сервер {resp.status} "
                                f"(спроба {attempt}/{_retries}). Retry через {wait}с"
                            )
                            await asyncio.sleep(wait)
                            continue
                        resp.raise_for_status()
                        return await resp.json(content_type=None)
            except (
                aiohttp.ClientConnectionError,
                aiohttp.ServerDisconnectedError,
                asyncio.TimeoutError,
                OSError,
            ) as e:
                last_error = e
                if attempt < _retries:
                    wait = 2 ** (attempt - 1)
                    logger.warning(
                        f"[BINOTEL REST] {endpoint} мережева помилка "
                        f"(спроба {attempt}/{_retries}): {e}. Retry через {wait}с"
                    )
                    await asyncio.sleep(wait)
                else:
                    logger.error(
                        f"[BINOTEL REST] {endpoint} провалено після {_retries} спроб: {e}"
                    )
                    raise

        raise last_error  # type: ignore[misc]

    # ------------------------------------------------------------------
    # Статус extension
    # ------------------------------------------------------------------

    async def get_employees(self) -> list:
        """Отримати список співробітників і їх статус (online/offline)."""
        result = await self.send_request("settings/list-of-employees", {})
        if result.get("status") != "success":
            return []
        return list(result.get("listOfEmployees", {}).values())

    async def wait_for_extension_online(
        self, internal_number: str, timeout: int = 60
    ) -> bool:
        """
        Чекати поки SIP extension стане Online в Binotel.

        Returns:
            True  — extension онлайн
            False — timeout
        """
        deadline = asyncio.get_event_loop().time() + timeout
        while asyncio.get_event_loop().time() < deadline:
            employees = await self.get_employees()
            for emp in employees:
                endpoint = emp.get("endpointData", {})
                if not isinstance(endpoint, dict):
                    continue
                if str(endpoint.get("internalNumber")) == str(internal_number):
                    status = endpoint.get("status", {}).get("status", "offline")
                    logger.info(f"[BINOTEL REST] Extension {internal_number} статус: '{status}'")
                    if status in ("online", "inuse", "ringing"):
                        logger.info(f"[BINOTEL REST] Extension {internal_number} — Онлайн ✓")
                        return True
            logger.debug(f"[BINOTEL REST] Extension {internal_number} ще офлайн, чекаємо...")
            await asyncio.sleep(3)
        logger.warning(f"[BINOTEL REST] Extension {internal_number} не став онлайн за {timeout}с")
        return False

    # ------------------------------------------------------------------
    # Дзвінки
    # ------------------------------------------------------------------

    async def initiate_call(
        self,
        internal_number: str,
        external_number: str,
        limit_call_time: int = 300,
    ) -> str:
        """
        Ініціювати двосторонній дзвінок:
          Binotel → наш SIP extension (internal_number)
          Binotel → пацієнт (external_number)

        Returns:
            generalCallID — ідентифікатор дзвінка для подальших операцій.
        """
        result = await self.send_request(
            "calls/internal-number-to-external-number",
            {
                "internalNumber": internal_number,
                "externalNumber": external_number,
                "limitCallTime": str(limit_call_time),
            },
        )
        if result.get("status") != "success":
            raise RuntimeError(
                f"Binotel initiate_call error {result.get('code')}: {result.get('message')}"
            )
        call_id = result["generalCallID"]
        logger.info(f"[BINOTEL REST] Дзвінок ініційовано. generalCallID={call_id}")
        return str(call_id)

    async def hangup_call(self, general_call_id: str) -> bool:
        """Завершити дзвінок."""
        result = await self.send_request(
            "calls/hangup-call",
            {"generalCallID": general_call_id},
        )
        ok = result.get("status") == "success"
        if ok:
            logger.info(f"[BINOTEL REST] Дзвінок {general_call_id} завершено")
        elif result.get("code") == 104:
            # 104 = дзвінок вже закрився (пацієнт поклав трубку або SIP завершив раніше) — це норма
            logger.info(f"[BINOTEL REST] Дзвінок {general_call_id} вже завершено (SIP closed earlier)")
        else:
            logger.warning(
                f"[BINOTEL REST] Не вдалося завершити {general_call_id}: "
                f"{result.get('code')} {result.get('message')}"
            )
        return ok

    async def attended_transfer(
        self, general_call_id: str, destination_number: str
    ) -> bool:
        """
        Перевести дзвінок на іншого співробітника (attended transfer).

        Args:
            general_call_id:   ID активного дзвінка
            destination_number: внутрішній номер оператора
        """
        result = await self.send_request(
            "calls/attended-call-transfer",
            {
                "generalCallID": general_call_id,
                "externalNumber": destination_number,
            },
        )
        ok = result.get("status") == "success"
        if ok:
            logger.info(
                f"[BINOTEL REST] Дзвінок {general_call_id} передано → {destination_number}"
            )
        else:
            logger.warning(
                f"[BINOTEL REST] Передача не вдалася: {result.get('code')} {result.get('message')}"
            )
        return ok

    # ------------------------------------------------------------------
    # Статистика / статус дзвінка
    # ------------------------------------------------------------------

    async def get_call_details(self, general_call_id: str) -> dict:
        """
        Отримати деталі дзвінка.

        Returns:
            dict з полями disposition, billsec тощо.
            Повертає {} якщо дзвінок ще не з'явився в статистиці.

        Поле disposition:
            "ONLINE"    — дзвінок активний
            "ANSWERED"  — завершено успішно
            "NO ANSWER" — не відповів
            "BUSY"      — зайнято
            ""          — дзвінок ще не починався / в процесі набору
        """
        result = await self.send_request(
            "stats/call-details",
            {"generalCallID": general_call_id},
        )
        if result.get("status") != "success":
            return {}
        details = result.get("callDetails", {})
        return details.get(str(general_call_id), {})

    async def wait_for_answer(
        self, general_call_id: str, timeout: int = 60
    ) -> bool:
        """
        Polling stats/call-details поки пацієнт не відповів або не вичерпався timeout.

        Returns:
            True  — пацієнт відповів (disposition == "ONLINE" або "ANSWERED")
            False — не відповів / помилка / timeout
        """
        deadline = asyncio.get_event_loop().time() + timeout
        while asyncio.get_event_loop().time() < deadline:
            details = await self.get_call_details(general_call_id)
            disposition = details.get("disposition", "")

            if disposition in ("ONLINE", "ANSWERED"):
                return True
            if disposition in ("NO ANSWER", "BUSY", "FAILED", "CANCEL"):
                logger.info(
                    f"[BINOTEL REST] Пацієнт не відповів ({disposition}): {general_call_id}"
                )
                return False

            await asyncio.sleep(1)

        logger.warning(f"[BINOTEL REST] Timeout очікування відповіді: {general_call_id}")
        return False
