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
import os
import tempfile
from typing import AsyncGenerator, Optional

try:
    import fcntl as _fcntl  # Linux (VPS). На Windows (локальна розробка) недоступний.
except ImportError:
    _fcntl = None

from config import (
    BINOTEL_INTERNAL_NUMBER,
    BINOTEL_OPERATOR_NUMBER,
    BINOTEL_SIP_PASSWORD,
    BINOTEL_SIP_PORT,
    BINOTEL_SIP_SERVER,
    BINOTEL_SIP_USERNAME,
    CONNECT_WATCHDOG_SEC,
    DEBUG,
)
from services import watchdog as _watchdog
from services.audio_bridge import apply_plc_decay, generate_fadeout_g711, gemini_output_to_sip, reset_state, sip_to_gemini_input
from services import opus_codec as _opus_codec
from services.binotel_rest import BinotelRestClient
from services.binotel_sip import BinotelSipAudio, BinotelSipClient
from services.binotel_ws import BinotelWebSocket

logger = logging.getLogger(__name__)

# Глобальні сервіси (ініціалізуються один раз у startup)
_rest = BinotelRestClient()
_ws: Optional[BinotelWebSocket] = None
_sip: Optional[BinotelSipClient] = None
_active_call: Optional["BinotelCall"] = None  # активний дзвінок (щоб shutdown не вбивав SIP під час прощання)

# Міжпроцесний замок на SIP. Модулі 1 і 2 — окремі systemd-сервіси, але обидва
# біндять локальний UDP-порт 5060 і реєструють одного SIP-extension. Одночасно
# працювати не можуть → без замка другий падає з "[Errno 98] Address already in use".
_SIP_LOCK_PATH = os.path.join(tempfile.gettempdir(), "medical_ai_sip.lock")
_SIP_LOCK_WAIT_SEC = 150  # має бути < CALL_TIMEOUT_SEC (startup() виконується всередині його таймауту)
_sip_lock_fd: Optional[int] = None


async def _acquire_sip_lock() -> None:
    global _sip_lock_fd
    if _fcntl is None or _sip_lock_fd is not None:
        return
    fd = os.open(_SIP_LOCK_PATH, os.O_CREAT | os.O_RDWR, 0o666)
    loop = asyncio.get_event_loop()
    deadline = loop.time() + _SIP_LOCK_WAIT_SEC
    warned = False
    while True:
        try:
            _fcntl.flock(fd, _fcntl.LOCK_EX | _fcntl.LOCK_NB)
            _sip_lock_fd = fd
            if warned:
                logger.info("[BINOTEL] SIP-замок отримано")
            return
        except OSError:
            if not warned:
                logger.warning("[BINOTEL] SIP зайнятий іншим модулем — чекаємо своєї черги...")
                warned = True
            if loop.time() >= deadline:
                os.close(fd)
                raise RuntimeError(
                    f"SIP зайнятий іншим модулем довше {_SIP_LOCK_WAIT_SEC}с — дзвінок не розпочато"
                )
            await asyncio.sleep(0.5)


def _release_sip_lock() -> bool:
    global _sip_lock_fd
    if _sip_lock_fd is None:
        return False
    try:
        _fcntl.flock(_sip_lock_fd, _fcntl.LOCK_UN)
        os.close(_sip_lock_fd)
    except OSError:
        pass
    _sip_lock_fd = None
    return True


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------


def is_ready() -> bool:
    """Перевірити чи SIP та WebSocket з'єднання живі."""
    if _sip is None or _ws is None:
        return False
    if _sip._phone is None:
        return False
    if _ws._ws is None or getattr(_ws._ws, 'close_code', None) is not None:
        return False
    return True


async def ensure_ready() -> None:
    """Перевірити з'єднання та перепідключитись якщо потрібно."""
    if DEBUG:
        return
    if is_ready():
        return
    logger.warning("[BINOTEL] З'єднання втрачено — перепідключення...")
    await shutdown()
    await startup()


async def startup() -> None:
    """
    Ініціалізувати Binotel сервіси.
    Ідемпотентно: якщо вже запущені і здорові — пропускає.
    """
    global _ws, _sip

    if DEBUG:
        return

    # Вже запущено і здорово? Пропускаємо.
    if is_ready():
        logger.debug("[BINOTEL] Сервіси вже запущені")
        return

    # Очистити залишки якщо є
    await shutdown()

    # Один SIP на весь VPS: чекаємо поки інший модуль завершить свій дзвінок
    await _acquire_sip_lock()
    try:
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
    except BaseException:
        # Не тримаємо замок якщо підняти сервіси не вдалось
        _release_sip_lock()
        raise

    logger.info("[BINOTEL] Сервіси запущено (SIP + WebSocket)")


async def shutdown() -> None:
    """Зупинити Binotel сервіси (безпечний для повторного виклику)."""
    global _ws, _sip
    # Якщо є активний дзвінок — чекаємо завершення щоб прощання AI дійшло до пацієнта.
    # Це запобігає race condition коли block1/block2 паралельно викликають shutdown()
    # поки інший runner ще в drain-фазі (flush + 5с sleep після end_call).
    _ac = _active_call
    if _ac is not None and _ac.is_active:
        logger.warning("[BINOTEL] Shutdown: активний дзвінок — чекаємо завершення (до 30с)...")
        _loop = asyncio.get_event_loop()
        _deadline = _loop.time() + 30.0
        while _ac.is_active and _loop.time() < _deadline:
            await asyncio.sleep(0.3)
        if _ac.is_active:
            logger.warning("[BINOTEL] Shutdown: таймаут — примусово зупиняємо")
    if _ws:
        try:
            await _ws.disconnect()
        except Exception:
            pass
        _ws = None
    if _sip:
        sip_ref = _sip
        _sip = None
        try:
            await asyncio.wait_for(
                asyncio.get_event_loop().run_in_executor(None, sip_ref.stop),
                timeout=10,
            )
        except asyncio.TimeoutError:
            logger.warning("[BINOTEL] SIP stop timeout (10с) — продовжуємо без очікування")
        except Exception:
            pass
    logger.info("[BINOTEL] Сервіси зупинено")
    if _release_sip_lock():
        # Пауза > інтервалу опитування замка (0.5с): дає іншому модулю шанс забрати чергу,
        # інакше цей процес одразу перехопить замок у наступному startup().
        await asyncio.sleep(1.5)


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
        _last_good: bytes = b""  # PLC: останній хороший chunk (після конвертації)
        _plc_count = 0           # PLC: поточна кількість повторів
        _PLC_MAX = 3             # PLC: максимум повторів перед тишею
        _PLC_DECAY = 0.7         # PLC: згасання амплітуди за кожен повтор
        while self.is_active and self._sip.is_active:
            pcm_8khz = await loop.run_in_executor(None, self._sip.read)
            if pcm_8khz:
                _gap_streak = 0
                _good_streak += 1
                _plc_count = 0
                # Стабільна мережа → поступово зменшуємо буфер
                if _good_streak >= 50 and _jitter_target > 2:
                    _jitter_target -= 1
                    _good_streak = 0
                converted = sip_to_gemini_input(pcm_8khz)
                _last_good = converted
                _jbuf.append(converted)
                if len(_jbuf) >= _jitter_target:
                    yield _jbuf.pop(0)
            else:
                _good_streak = 0
                _gap_streak += 1
                # Нестабільна мережа → збільшуємо буфер (max 4)
                if _gap_streak >= 3 and _jitter_target < 4:
                    _jitter_target += 1
                    _gap_streak = 0
                # PLC: повторюємо останній chunk зі згасанням замість тиші
                if _last_good and _plc_count < _PLC_MAX:
                    _plc_count += 1
                    _jbuf.append(apply_plc_decay(_last_good, _PLC_DECAY ** _plc_count))
                # Зливаємо буфер
                if _jbuf:
                    yield _jbuf.pop(0)
                else:
                    await asyncio.sleep(0.01)

    # ------------------------------------------------------------------
    # Управління дзвінком
    # ------------------------------------------------------------------

    def flush_audio(self) -> None:
        """Очистити аудіо буфер з fade-out (запобігає кліку при barge-in)."""
        self._sip.flush_audio()

    def flush_encode_buffer(self) -> None:
        """Дозакодувати і відправити залишок PCM-буфера (останній фрейм туру).

        Викликається при turn_complete — гарантує що останні ~200-400мс
        кожної репліки AI дійдуть до пацієнта.
        """
        if not self.is_active:
            return
        from services.audio_bridge import _use_opus
        if not _use_opus:
            return
        frames = _opus_codec.flush()
        for frame in frames:
            for rtp in getattr(getattr(self._sip, "_call", None), "RTPClients", []):
                from services.binotel_sip import _ensure_opus_queue
                try:
                    _ensure_opus_queue(rtp).put_nowait(frame)
                except Exception:
                    pass

    async def hangup(self) -> None:
        """Завершити дзвінок (SIP + REST якщо є call_id)."""
        global _active_call
        if not self.is_active:
            return
        self.is_active = False
        _active_call = None
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
        global _active_call
        if not self.is_active:
            return
        self.is_active = False
        _active_call = None
        reset_state()

        logger.info(f"[BINOTEL] Передача оператору ({BINOTEL_OPERATOR_NUMBER}): {reason}")
        if self.call_id:
            await _rest.attended_transfer(self.call_id, BINOTEL_OPERATOR_NUMBER)
            # Чекаємо 2с щоб Binotel встиг обробити transfer, потім закриваємо SIP.
            # Якщо оператор підняв — Binotel вже взяв міст, наш hangup лише прибирає бота.
            # Якщо оператор не підняв — SIP закривається чисто, без лімбо для пацієнта.
            await asyncio.sleep(2)
            self._sip.hangup()
        else:
            self._sip.hangup()


# ---------------------------------------------------------------------------
# Ініціація дзвінка
# ---------------------------------------------------------------------------


async def initiate_call(phone: str, patient_name: str, _retry: int = 0) -> Optional[BinotelCall]:
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

    # Скидаємо прапори перед новим дзвінком — кожен дзвінок визначає кодек заново через SDP.
    # _encode_pcmu_log_done: якщо не скинути → set_codec_alaw() пропускається для дзвінка 2+
    # → _use_alaw=False після reset_state() → AI голос відправляється µ-law замість A-law
    # → Binotel декодує неправильно → спотворений звук → ехо від початку 2-го дзвінка.
    from services import binotel_sip as _bsip
    _bsip._next_call_use_opus = False
    _bsip._encode_pcmu_log_done = False
    _bsip._encode_opus_log_done = False

    logger.info(f"[BINOTEL] Дзвінок → {patient_name} ({phone})")

    # Сторож: до отримання generalCallID усе має вкластись у CONNECT_WATCHDOG_SEC.
    # Якщо завис (event loop чи Binotel) — дамп стеків + перезапуск процесу.
    # Після generalCallID дзвінок уже пішов — далі його страхує звичайний CALL_TIMEOUT_SEC.
    _watchdog.arm("connect (до generalCallID)", CONNECT_WATCHDOG_SEC)

    # Крок 1: Чекаємо поки наш SIP extension стане Online в Binotel
    logger.info(f"[BINOTEL] Чекаємо статус Онлайн для extension {BINOTEL_INTERNAL_NUMBER}...")
    online = await _rest.wait_for_extension_online(BINOTEL_INTERNAL_NUMBER, timeout=15)
    if not online:
        # SIP реєстрація "впала" з боку Binotel — перепідключаємо і чекаємо знову.
        logger.warning(
            f"[BINOTEL] Extension {BINOTEL_INTERNAL_NUMBER} офлайн — перепідключаємо SIP..."
        )
        # startup() може чекати SIP-замок до 150с — на цей час сторож вимкнено
        _watchdog.disarm()
        await shutdown()
        await startup()
        _watchdog.arm("connect (до generalCallID)", CONNECT_WATCHDOG_SEC)
        online = await _rest.wait_for_extension_online(BINOTEL_INTERNAL_NUMBER, timeout=10)
        if not online:
            _watchdog.disarm()
            logger.error(
                f"[BINOTEL] Extension {BINOTEL_INTERNAL_NUMBER} все ще офлайн після перепідключення — "
                f"пропускаємо дзвінок, спробуємо пізніше."
            )
            return None
        logger.info(f"[BINOTEL] Extension {BINOTEL_INTERNAL_NUMBER} знову онлайн ✓")

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
            _watchdog.disarm()
            sip_wait_task.cancel()
            _sip._expecting_call = False
            logger.error(f"[BINOTEL] Помилка ініціації дзвінка: {e}")
            return None
    except Exception as e:
        _watchdog.disarm()
        sip_wait_task.cancel()
        _sip._expecting_call = False
        logger.error(f"[BINOTEL] Помилка ініціації дзвінка: {e}")
        return None

    # REST відповів (generalCallID отримано або error 150) — фаза підключення пройдена.
    _watchdog.disarm()

    # Крок 4: Чекаємо INVITE та відповідаємо на нього.
    # Якщо SIP впав (fatalCallback → _phone=None → is_ready()=False) під час очікування
    # — не чекаємо 50с, скасовуємо одразу і повертаємо None щоб caller міг перезапустити.
    async def _sip_dead_monitor():
        while True:
            await asyncio.sleep(0.5)
            if not is_ready():
                return  # SIP впав

    _sip_monitor = asyncio.create_task(_sip_dead_monitor())
    done, _ = await asyncio.wait(
        {sip_wait_task, _sip_monitor},
        return_when=asyncio.FIRST_COMPLETED,
    )
    _sip_monitor.cancel()
    if sip_wait_task not in done:
        # SIP monitor спрацював раніше — SIP збій
        sip_wait_task.cancel()
        try:
            await sip_wait_task
        except (asyncio.CancelledError, Exception):
            pass
        logger.warning("[BINOTEL] SIP збій під час очікування INVITE — скасовуємо дзвінок")
        if general_call_id:
            try:
                await _rest.hangup_call(general_call_id)
            except Exception:
                pass
        return None
    sip_audio = sip_wait_task.result()
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
        # Error 150 шлях: немає generalCallID → не можемо відстежити відповідь
        # через WebSocket або REST. sip_audio.is_active ЗАВЖДИ True після нашого Answer
        # (це наш SIP бік) — тому ≠ "пацієнт відповів".
        # Чекаємо фактичні RTP аудіо-пакети від пацієнта: якщо він підняв трубку —
        # Binotel починає форвардити його аудіо і read() повертає не порожній буфер.
        # Якщо пацієнт не відповів — read() завжди порожній до таймауту.
        _AUDIO_TIMEOUT = 10.0    # 10с: якщо за цей час нема RTP → retry (не 45с)
        _FRAMES_NEEDED = 5       # мінімум 5 послідовних ненульових фреймів (~250мс)
        logger.info(f"[BINOTEL] Error 150: чекаємо RTP аудіо від пацієнта (до {_AUDIO_TIMEOUT:.0f}с)...")
        _consecutive = 0
        _loop = asyncio.get_running_loop()
        _deadline = _loop.time() + _AUDIO_TIMEOUT
        while _loop.time() < _deadline:
            if not sip_audio.is_active:
                break
            await asyncio.sleep(0.05)
            try:
                pcm = await asyncio.wait_for(
                    _loop.run_in_executor(None, sip_audio.read),
                    timeout=1.0,
                )
            except asyncio.TimeoutError:
                pcm = b""
            if pcm and len(pcm) >= 16:
                _consecutive += 1
                if _consecutive >= _FRAMES_NEEDED:
                    break
            else:
                _consecutive = 0  # скидаємо при прогалині
        answered = sip_audio.is_active and _consecutive >= _FRAMES_NEEDED
        if not answered:
            # Error 150 + таймаут: вина технічна (не пацієнт) → retry 1 раз.
            # Hangup поточний SIP щоб не лишати відкрите з'єднання.
            sip_audio.hangup()
            if _retry == 0:
                logger.warning("[BINOTEL] Error 150: RTP не прийшов → retry дзвінка (спроба 2)...")
                await asyncio.sleep(3)  # коротка пауза перед повтором
                return await initiate_call(phone, patient_name, _retry=1)
            else:
                logger.warning("[BINOTEL] Error 150: retry також не вдався → call_failed")
                return None

    if not answered:
        logger.warning(f"[BINOTEL] Пацієнт не відповів: {patient_name} ({phone})")
        sip_audio.hangup()
        if general_call_id:
            await _rest.hangup_call(general_call_id)
        return None

    logger.info(f"[BINOTEL] Пацієнт відповів. Починаємо розмову: {patient_name}")
    global _active_call
    _active_call = BinotelCall(general_call_id, sip_audio)
    return _active_call
