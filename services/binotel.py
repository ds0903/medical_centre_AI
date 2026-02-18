"""
Сервіс інтеграції з Binotel API.

У DEBUG режимі — заглушка, реального дзвінка немає.
У PRODUCTION режимі — реальні запити до Binotel (TODO нижче).

========================================================================
TODO BINOTEL — ПОВНА ІНТЕГРАЦІЯ (заповнити після отримання документації)
========================================================================

ЩО ВІДОМО (з публічних джерел):
  - REST API base: https://api.binotel.com/api/4.0/
  - Auth: key + secret + MD5 підпис (params sorted JSON + secret → MD5)
  - Ініціація дзвінка: POST /calls/ext-to-phone.json
      params: ext_number (внутр. номер бота), phone_number (пацієнт),
              playbackWaiting (bool), limitCallTime (sec), trunkNumber
  - Завершення: POST /calls/hangup-call.json  params: generalCallID
  - Webhook події: receivedTheCall, answeredTheCall, hangupTheCall, apiCallCompleted
  - Webhook payload містить: generalCallID, internalNumber, externalNumber, billsec
  - SIP сервер: sip1.binotel.com або sip5.binotel.com, порт 5060, UDP/TCP
  - Кодек: G.729 (між Binotel і SIP клієнтом)

ЩО ТРЕБА УТОЧНИТИ У BINOTEL (support@binotel.ua):
  1. Точний формат MD5 підпису (порядок полів, кодування)
  2. Точний JSON формат запиту extToPhone та формат відповіді (де generalCallID?)
  3. Чи є sandbox середовище для тестування без реальних дзвінків?
  4. Як налаштувати webhook URL в особистому кабінеті
  5. Caller ID — який номер клініки підставляти у trunkNumber?
  6. Чи підтримує Binotel WebSocket аудіо стримінг? (не SIP, а HTTP/WS)
     Якщо НІ — потрібен SIP клієнт (pjsua2/Asterisk) для аудіо моста

АРХІТЕКТУРА АУДІО В PRODUCTION (коли уточнимо):
  Варіант А (SIP): extToPhone → Binotel дзвонить на наш SIP ext → pjsua2 відповідає
                   → SIP аудіо ↔ PCM конвертер ↔ Gemini Live API
  Варіант Б (якщо є WS аудіо): extToPhone → webhook answered → WS аудіо ↔ Gemini
========================================================================
"""

import logging
from typing import AsyncGenerator, Optional

from config import BINOTEL_API_KEY, BINOTEL_API_SECRET, BINOTEL_API_URL, DEBUG

logger = logging.getLogger(__name__)


class BinotelCall:
    """Представляє активний дзвінок через Binotel."""

    def __init__(self, call_id: str, phone: str):
        self.call_id = call_id
        self.phone = phone
        self.is_active = True

    async def hangup(self) -> None:
        """
        Завершити дзвінок.

        TODO: POST {BINOTEL_API_URL}/calls/{call_id}/hangup  (або інший endpoint)
        TODO: Уточнити endpoint та параметри завершення дзвінка
        """
        self.is_active = False
        if DEBUG:
            logger.debug(f"[DEBUG] Binotel: hangup call_id={self.call_id}")
            return

        # TODO: реальний hangup -----------------------------------------------
        # import aiohttp
        # async with aiohttp.ClientSession() as http:
        #     async with http.post(
        #         f"{BINOTEL_API_URL}/calls/{self.call_id}/hangup",
        #         headers={"Authorization": f"Bearer {BINOTEL_API_KEY}"},
        #     ) as resp:
        #         resp.raise_for_status()
        # ----------------------------------------------------------------------
        raise NotImplementedError("TODO: реалізувати hangup")

    async def send_audio(self, pcm_data: bytes) -> None:
        """
        Відправити аудіо (24kHz PCM) пацієнту через Binotel.
        Конвертація PCM → G.711 виконується в audio_bridge.py.

        TODO: Уточнити метод передачі аудіо (WebSocket stream? RTP?)
        TODO: Уточнити кодек який очікує Binotel (G.711 μ-law / A-law / інший?)
        TODO: Уточнити розмір chunks для стримінгу
        """
        if DEBUG:
            return  # В debug аудіо грає через локальний динамік

        # TODO: реальна відправка аудіо ----------------------------------------
        # from services.audio_bridge import gemini_to_binotel
        # g711_data = gemini_to_binotel(pcm_data)
        # await self._ws.send_bytes(g711_data)   # TODO: тип з'єднання
        # -----------------------------------------------------------------------
        raise NotImplementedError("TODO: реалізувати send_audio")

    async def receive_audio(self) -> AsyncGenerator[bytes, None]:
        """
        Отримати аудіо від пацієнта у форматі PCM 16kHz (для Gemini).
        Конвертація G.711 → PCM виконується в audio_bridge.py.

        TODO: Уточнити метод отримання аудіо потоку від Binotel
        TODO: Уточнити формат вхідного аудіо від Binotel
        """
        if DEBUG:
            return  # В debug аудіо береться з мікрофона

        # TODO: реальне отримання аудіо ----------------------------------------
        # async for chunk in self._ws:
        #     from services.audio_bridge import binotel_to_gemini
        #     yield binotel_to_gemini(chunk.data)
        # -----------------------------------------------------------------------
        raise NotImplementedError("TODO: реалізувати receive_audio")
        # Потрібен yield щоб Python розпізнав функцію як generator
        yield b""


async def initiate_call(phone: str, patient_name: str) -> Optional[BinotelCall]:
    """
    Ініціювати вихідний дзвінок через Binotel.

    Returns:
        BinotelCall якщо пацієнт відповів
        None якщо не відповів / зайнято / недоступний

    TODO: POST {BINOTEL_API_URL}/calls/initiate (або інший endpoint)
          body: {"phone": phone, "caller_id": "CLINIC_NUMBER", "webhook_url": "..."}
    TODO: Уточнити caller_id — який номер клініки підставляти
    TODO: Уточнити як отримати call_id після ініціації
    TODO: Уточнити як дізнатися що пацієнт ВІДПОВІВ:
          - webhook від Binotel на наш сервер?
          - polling статусу дзвінка?
          - event stream?
    TODO: Реалізувати timeout — скільки чекати відповіді (30 сек?)
    TODO: Реалізувати retry логіку (MAX_CALL_ATTEMPTS з config.py)
    """
    if DEBUG:
        logger.info(f"[DEBUG] Binotel: симулюємо дзвінок → {phone} ({patient_name})")
        logger.info(f"[DEBUG] В debug режимі дзвінок відбувається через мікрофон")
        return BinotelCall(call_id="debug-call-001", phone=phone)

    # TODO: реальний запит ---------------------------------------------------
    # import aiohttp
    # async with aiohttp.ClientSession() as http:
    #     async with http.post(
    #         f"{BINOTEL_API_URL}/calls/initiate",
    #         headers={
    #             "Authorization": f"Bearer {BINOTEL_API_KEY}",
    #             "Content-Type": "application/json",
    #         },
    #         json={
    #             "phone": phone,
    #             "caller_id": "CLINIC_PHONE_NUMBER",  # TODO: взяти з env
    #             # "webhook_url": "https://our-server/binotel/webhook",  # TODO
    #         },
    #     ) as resp:
    #         resp.raise_for_status()
    #         data = await resp.json()
    #         call_id = data["call_id"]   # TODO: уточнити поле
    #
    # # TODO: чекати на відповідь пацієнта (webhook або polling)
    # # answered = await _wait_for_answer(call_id, timeout=30)
    # # if not answered:
    # #     return None
    #
    # return BinotelCall(call_id=call_id, phone=phone)
    # -------------------------------------------------------------------------
    raise NotImplementedError("TODO: реалізувати initiate_call")
