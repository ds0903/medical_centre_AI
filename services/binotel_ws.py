"""
Binotel WebSocket клієнт.

URL: wss://ws.binotel.com:9002
Auth: {"task": "authLikeService", "key": "...", "secret": "..."}

Після підключення отримуємо реал-тайм події про дзвінки:
  answeredTheCall   — пацієнт підняв трубку
  hangupTheCall     — дзвінок завершено
  apiCallCompleted  — дзвінок повністю завершено (з деталями)
  receivedTheCall   — дзвінок ініційовано
"""

import asyncio
import json
import logging
from typing import Optional

import websockets
from websockets.exceptions import ConnectionClosed

from config import BINOTEL_API_KEY, BINOTEL_API_SECRET

logger = logging.getLogger(__name__)

BINOTEL_WS_URL = "wss://ws.binotel.com:9002"


class BinotelWebSocket:
    """
    WebSocket підписник на події дзвінків Binotel.

    Використання:
        ws = BinotelWebSocket()
        await ws.connect()
        answered = await ws.wait_for_answered("12345678", timeout=60)
        await ws.disconnect()
    """

    def __init__(self) -> None:
        self._ws: Optional[websockets.WebSocketClientProtocol] = None
        self._listener_task: Optional[asyncio.Task] = None
        # generalCallID → asyncio.Event
        self._answered_events: dict[str, asyncio.Event] = {}
        self._hangup_events: dict[str, asyncio.Event] = {}

    # ------------------------------------------------------------------
    # З'єднання
    # ------------------------------------------------------------------

    async def connect(self) -> None:
        """Підключитись до Binotel WebSocket та авторизуватись."""
        self._ws = await websockets.connect(
            BINOTEL_WS_URL,
            ping_interval=30,
            ping_timeout=10,
        )

        # Перше повідомлення — запит авторизації
        first = await asyncio.wait_for(self._ws.recv(), timeout=15)
        logger.debug(f"[WS] Binotel: {first}")

        if "authorise" in first.lower() or "authorize" in first.lower():
            await self._ws.send(json.dumps({
                "task": "authLikeService",
                "key": BINOTEL_API_KEY,
                "secret": BINOTEL_API_SECRET,
            }))
            auth_resp = await asyncio.wait_for(self._ws.recv(), timeout=10)
            logger.debug(f"[WS] Auth response: {auth_resp}")

        self._listener_task = asyncio.create_task(
            self._listen(), name="binotel_ws_listener"
        )
        logger.info("[WS] Binotel WebSocket підключено та авторизовано")

    async def disconnect(self) -> None:
        """Відключитись від WebSocket."""
        if self._listener_task and not self._listener_task.done():
            self._listener_task.cancel()
            await asyncio.gather(self._listener_task, return_exceptions=True)
        if self._ws:
            await self._ws.close()
        logger.info("[WS] Binotel WebSocket відключено")

    # ------------------------------------------------------------------
    # Фоновий слухач
    # ------------------------------------------------------------------

    async def _listen(self) -> None:
        """Нескінченний цикл прийому подій від Binotel."""
        try:
            async for raw in self._ws:
                try:
                    data = json.loads(raw)
                except json.JSONDecodeError:
                    logger.debug(f"[WS] Не JSON: {raw[:100]}")
                    continue

                self._handle_event(data)

        except ConnectionClosed as e:
            logger.warning(f"[WS] З'єднання закрито: {e}")
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.error(f"[WS] Помилка слухача: {e}", exc_info=True)

    def _handle_event(self, data: dict) -> None:
        """Обробити одну подію від Binotel."""
        # Binotel може використовувати "event" або "status" або "task"
        event = (
            data.get("event")
            or data.get("status")
            or data.get("task")
            or ""
        ).lower()

        call_id = str(data.get("generalCallID", ""))

        logger.debug(f"[WS] Подія: {event} | callID={call_id} | {data}")

        if not call_id:
            return

        if event == "answeredthecall":
            self._set_event(self._answered_events, call_id)

        elif event in ("hangupthecall", "apicallcompleted"):
            self._set_event(self._hangup_events, call_id)
            # Якщо ще чекали відповіді — знімаємо блокування
            self._set_event(self._answered_events, call_id)

    @staticmethod
    def _set_event(store: dict, call_id: str) -> None:
        if call_id in store:
            store[call_id].set()

    # ------------------------------------------------------------------
    # Очікування подій
    # ------------------------------------------------------------------

    async def wait_for_answered(
        self, general_call_id: str, timeout: int = 60
    ) -> bool:
        """
        Чекати поки пацієнт підніме трубку.

        Returns:
            True  — подія answeredTheCall отримана
            False — timeout або з'єднання втрачено
        """
        event = asyncio.Event()
        self._answered_events[general_call_id] = event
        try:
            await asyncio.wait_for(event.wait(), timeout=timeout)
            return True
        except asyncio.TimeoutError:
            return False
        finally:
            self._answered_events.pop(general_call_id, None)

    async def wait_for_hangup(
        self, general_call_id: str, timeout: int = 600
    ) -> None:
        """Чекати завершення дзвінка (для логування тривалості)."""
        event = asyncio.Event()
        self._hangup_events[general_call_id] = event
        try:
            await asyncio.wait_for(event.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            pass
        finally:
            self._hangup_events.pop(general_call_id, None)
