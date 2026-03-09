"""
Binotel сервіс — єдина точка входу для production режиму.

Оркеструє:
  - REST API   (binotel_rest.py)   — ініціація, завершення, передача дзвінка
  - WebSocket  (binotel_ws.py)     — реал-тайм події (пацієнт відповів)
  - SIP клієнт (binotel_sip.py)    — аудіо-міст через pyVoIP

Публічний інтерфейс (використовується в scheduler та agent):
  await startup()                          — викликати один раз при старті production
  await shutdown()                         — викликати при зупинці
  call = await initiate_call(phone, name)  — почати дзвінок
  await call.send_audio(pcm_24khz)         — відправити аудіо пацієнту
  async for chunk in call.receive_audio()  — отримати аудіо від пацієнта
  await call.hangup()                      — завершити дзвінок
  await call.transfer_to_operator(reason)  — передати оператору
"""

import asyncio
import logging
from typing import AsyncGenerator, Optional

from config import (
    BINOTEL_INTERNAL_NUMBER,
    BINOTEL_OPERATOR_NUMBER,
    BINOTEL_SIP_PASSWORD,
    BINOTEL_SIP_PORT,
    BINOTEL_SIP_SERVER,
    BINOTEL_SIP_USERNAME,
    DEBUG,
)
from services.audio_bridge import gemini_output_to_sip, reset_state, sip_to_gemini_input
from services import opus_codec as _opus_codec
from services.binotel_rest import BinotelRestClient
from services.binotel_sip import BinotelSipAudio, BinotelSipClient
from services.binotel_ws import BinotelWebSocket

logger = logging.getLogger(__name__)

# Глобальні сервіси (ініціалізуються один раз у startup)
_rest = BinotelRestClient()
_ws: Optional[BinotelWebSocket] = None
_sip: Optional[BinotelSipClient] = None


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------


async def startup() -> None:
    """
    Ініціалізувати Binotel сервіси.
    Викликати один раз при старті production режиму (перед планувальником).
    """
    global _ws, _sip

    if DEBUG:
        return  # В debug режимі Binotel не потрібен

    # SIP клієнт — реєструємось як extension
    _sip = BinotelSipClient(
        server=BINOTEL_SIP_SERVER,
        port=BINOTEL_SIP_PORT,
        username=BINOTEL_SIP_USERNAME,
        password=BINOTEL_SIP_PASSWORD,
    )
    _sip.start()

    # WebSocket — підписуємось на події
    _ws = BinotelWebSocket()
    await _ws.connect()

    logger.info("[BINOTEL] Сервіси запущено (SIP + WebSocket)")


async def shutdown() -> None:
    """Зупинити Binotel сервіси."""
    if _ws:
        await _ws.disconnect()
    if _sip:
        _sip.stop()
    logger.info("[BINOTEL] Сервіси зупинено")


# ---------------------------------------------------------------------------
# BinotelCall — активний дзвінок
# ---------------------------------------------------------------------------


class BinotelCall:
    """
    Представляє активний дзвінок з пацієнтом.

    Надає async інтерфейс аудіо для Block1Agent.run_production().
    """

    def __init__(self, general_call_id: Optional[str], sip_audio: BinotelSipAudio) -> None:
        self.call_id = general_call_id  # None якщо REST API повернув error 150
        self._sip = sip_audio
        self.is_active = True

    # ------------------------------------------------------------------
    # Аудіо
    # ------------------------------------------------------------------

    async def send_audio(self, pcm_24khz: bytes) -> None:
        """
        Відправити аудіо Gemini пацієнту.
        Opus mode: encode + кладемо фрейми в RTPClient._opus_send_queue.
        G.711 mode: конвертуємо PCM 24kHz → G.711 8kHz і пишемо в SIP RTP.
        """
        if not self.is_active:
            return
        from services.audio_bridge import _use_opus
        if _use_opus:
            frames = _opus_codec.encode(pcm_24khz)
            for frame in frames:
                for rtp in getattr(getattr(self._sip, "_call", None), "RTPClients", []):
                    from services.binotel_sip import _ensure_opus_queue
                    try:
                        _ensure_opus_queue(rtp).put_nowait(frame)
                    except Exception:
                        pass
        else:
            pcm_g711 = gemini_output_to_sip(pcm_24khz)
            loop = asyncio.get_event_loop()
            await loop.run_in_executor(None, self._sip.write, pcm_g711)

    async def receive_audio(self) -> AsyncGenerator[bytes, None]:
        """
        Отримати аудіо від пацієнта для Gemini.
        Yields PCM 16kHz 16-bit chunks.

        Адаптивний jitter buffer (2–4 пакети):
          - Стабільна мережа: target=2 (~40мс затримки)
          - Нестабільна (gaps): target росте до 4 (~80мс) → менше stutter
          - Після 50 стабільних пакетів підряд — target знижується назад
        """
        loop = asyncio.get_event_loop()
        _jbuf: list[bytes] = []
        _jitter_target = 2   # поточний розмір буфера (адаптивний 2..4)
        _gap_streak = 0      # скільки пустих reads підряд
        _good_streak = 0     # скільки пакетів з даними підряд без gaps
        while self.is_active and self._sip.is_active:
            pcm_8khz = await loop.run_in_executor(None, self._sip.read)
            if pcm_8khz:
                _gap_streak = 0
                _good_streak += 1
                # Стабільна мережа → поступово зменшуємо буфер
                if _good_streak >= 50 and _jitter_target > 2:
                    _jitter_target -= 1
                    _good_streak = 0
                _jbuf.append(sip_to_gemini_input(pcm_8khz))
                if len(_jbuf) >= _jitter_target:
                    yield _jbuf.pop(0)
            else:
                _good_streak = 0
                _gap_streak += 1
                # Нестабільна мережа → збільшуємо буфер (max 4)
                if _gap_streak >= 3 and _jitter_target < 4:
                    _jitter_target += 1
                    _gap_streak = 0
                # Зливаємо буфер щоб не накопичувати
                if _jbuf:
                    yield _jbuf.pop(0)
                else:
                    await asyncio.sleep(0.01)

    # ------------------------------------------------------------------
    # Управління дзвінком
    # ------------------------------------------------------------------

    def flush_audio(self) -> None:
        """Очистити Opus чергу виводу — AI замовкає миттєво при перериванні."""
        self._sip.flush_audio()

    async def hangup(self) -> None:
        """Завершити дзвінок (SIP + REST якщо є call_id)."""
        if not self.is_active:
            return
        self.is_active = False
        reset_state()
        _opus_codec.reset()

        self._sip.hangup()
        if self.call_id:
            await _rest.hangup_call(self.call_id)
        logger.info(f"[BINOTEL] Дзвінок {self.call_id or 'SIP-only'} завершено")

    async def transfer_to_operator(self, reason: str = "") -> None:
        """
        Передати дзвінок живому оператору.

        Args:
            reason: причина передачі (для логування)
        """
        if not self.is_active:
            return
        self.is_active = False
        reset_state()

        logger.info(f"[BINOTEL] Передача оператору ({BINOTEL_OPERATOR_NUMBER}): {reason}")
        if self.call_id:
            await _rest.attended_transfer(self.call_id, BINOTEL_OPERATOR_NUMBER)
        self._sip.hangup()


# ---------------------------------------------------------------------------
# Ініціація дзвінка
# ---------------------------------------------------------------------------


async def initiate_call(phone: str, patient_name: str) -> Optional[BinotelCall]:
    """
    Ініціювати вихідний дзвінок пацієнту через Binotel.

    В DEBUG режимі повертає None — агент сам використовує мікрофон.

    Returns:
        BinotelCall якщо пацієнт відповів
        None якщо не відповів або технічна помилка
    """
    if DEBUG:
        logger.info(f"[DEBUG] Binotel: симулюємо дзвінок → {phone} ({patient_name})")
        logger.info("[DEBUG] Аудіо через мікрофон (run_debug)")
        return None

    if not _sip or not _ws:
        raise RuntimeError(
            "Binotel сервіси не ініціалізовані. Викличте binotel.startup() спочатку."
        )

    logger.info(f"[BINOTEL] Дзвінок → {patient_name} ({phone})")

    # Крок 1: Чекаємо поки наш SIP extension стане Online в Binotel
    logger.info(f"[BINOTEL] Чекаємо статус Онлайн для extension {BINOTEL_INTERNAL_NUMBER}...")
    online = await _rest.wait_for_extension_online(BINOTEL_INTERNAL_NUMBER, timeout=15)
    if not online:
        logger.warning(
            f"[BINOTEL] Extension {BINOTEL_INTERNAL_NUMBER} не став онлайн. "
            f"Спробуємо дзвонити все одно..."
        )

    # Пауза 4с — дати час залишковим дзвінкам від попередніх сесій прийти і відхилитись
    logger.info("[BINOTEL] Очищення залишкових дзвінків...")
    await asyncio.sleep(4)

    # Крок 2: Запускаємо INVITE listener ПАРАЛЕЛЬНО з REST API викликом.
    # Binotel надсилає INVITE поки обробляє REST API запит.
    # Ми маємо відповісти на INVITE ЗРАЗУ — тоді Binotel з'єднує обидві сторони.
    _sip._expecting_call = True
    sip_wait_task = asyncio.create_task(_sip.wait_for_call(timeout=50))

    # Крок 3: Ініціюємо дзвінок через REST API
    general_call_id: Optional[str] = None
    try:
        general_call_id = await _rest.initiate_call(
            internal_number=BINOTEL_INTERNAL_NUMBER,
            external_number=phone,
        )
    except RuntimeError as e:
        if "150" in str(e):
            # Error 150 = Binotel перевіряє qualify (=0) і повертає pre-check failure,
            # але INVITE вже надіслано або надсилається. Продовжуємо чекати INVITE.
            logger.warning(
                f"[BINOTEL] Error 150 (qualify=0): Binotel вже надіслав INVITE, "
                f"чекаємо підтвердження SIP з'єднання..."
            )
        else:
            sip_wait_task.cancel()
            _sip._expecting_call = False
            logger.error(f"[BINOTEL] Помилка ініціації дзвінка: {e}")
            return None
    except Exception as e:
        sip_wait_task.cancel()
        _sip._expecting_call = False
        logger.error(f"[BINOTEL] Помилка ініціації дзвінка: {e}")
        return None

    # Крок 4: Чекаємо INVITE та відповідаємо на нього
    sip_audio = await sip_wait_task
    if sip_audio is None:
        logger.warning(f"[BINOTEL] SIP extension не отримав дзвінок від Binotel")
        if general_call_id:
            await _rest.hangup_call(general_call_id)
        return None

    if general_call_id is None:
        logger.warning(
            "[BINOTEL] Немає generalCallID (error 150 від REST API). "
            "Дзвінок встановлено через SIP, REST API керування недоступне."
        )

    # Крок 5: Чекаємо поки пацієнт відповість (WebSocket подія + polling)
    if general_call_id:
        # Нормальний шлях: є generalCallID, чекаємо через WebSocket + polling
        tasks_to_await = []
        if _ws:
            tasks_to_await.append(
                asyncio.create_task(_ws.wait_for_answered(general_call_id, timeout=60))
            )
        tasks_to_await.append(
            asyncio.create_task(_rest.wait_for_answer(general_call_id, timeout=60))
        )

        done, pending = await asyncio.wait(
            tasks_to_await, return_when=asyncio.FIRST_COMPLETED
        )
        for t in pending:
            t.cancel()
        answered = any(t.result() for t in done if not t.exception())
    else:
        # Error 150 шлях: немає generalCallID.
        # Якщо пацієнт дзвонить і audio активний — вважаємо що відповів.
        # Чекаємо 30с на аудіо від пацієнта.
        logger.info("[BINOTEL] Чекаємо audio підтвердження (немає generalCallID)...")
        await asyncio.sleep(3)
        answered = sip_audio.is_active

    if not answered:
        logger.warning(f"[BINOTEL] Пацієнт не відповів: {patient_name} ({phone})")
        sip_audio.hangup()
        if general_call_id:
            await _rest.hangup_call(general_call_id)
        return None

    logger.info(f"[BINOTEL] Пацієнт відповів. Починаємо розмову: {patient_name}")
    return BinotelCall(general_call_id, sip_audio)
