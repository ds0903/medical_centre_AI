"""
Агент Блоку 1: Нагадування про візити.

DEBUG mode   : мікрофон → Gemini Live → динамік  (ти = пацієнт)
PRODUCTION   : Binotel SIP аудіо ↔ Gemini Live
"""

import asyncio
import logging
import math
import re
import struct
import time

try:
    import webrtcvad as _webrtcvad
    _VAD = _webrtcvad.Vad(2)   # aggressiveness 0-3: 2 = стандарт для телефонії
    _VAD_AVAILABLE = True
except ImportError:
    _VAD = None
    _VAD_AVAILABLE = False

try:
    import pyaudio
    _PYAUDIO_AVAILABLE = True
except ImportError:
    pyaudio = None
    _PYAUDIO_AVAILABLE = False

from google import genai
from google.genai import types

from agents.block1.prompts import build_system_prompt
from agents.block1.tools import TOOLS, handle_tool_call
from config import GEMINI_MODEL, DEBUG, VERTEX_PROJECT, VERTEX_LOCATION, SILENCE_TIMEOUT_SEC, SILENCE_START_DELAY_SEC, WATCHDOG_TIMEOUT_SEC, INTERRUPTED_PAUSE_SEC, GEMINI_LOGS, LOG_FILE

logger = logging.getLogger(__name__)

# Аудіо константи
FORMAT = pyaudio.paInt16 if _PYAUDIO_AVAILABLE else 8  # paInt16 = 8
CHANNELS = 1
INPUT_RATE = 16000   # мікрофон / SIP → Gemini
OUTPUT_RATE = 24000  # Gemini → динамік / SIP
CHUNK = 512

# Silence detection
VAD_THRESHOLD = 300      # RMS поріг для 16-bit PCM (fallback якщо webrtcvad недоступний)
MAX_SILENCE_COUNT = 3    # після 3 разів — вішаємо трубку
_VAD_FRAME_BYTES = 320   # 10мс @ 16kHz 16-bit mono = 160 семплів × 2 байти


def _calc_echo_gate(audio_bytes: int) -> float:
    """Адаптивний ехо-гейт: коротша пауза для коротких фраз AI, довша для довгих.

    audio_bytes — скільки байтів аудіо AI вже відіграв у цьому турі (24kHz 16-bit).
    Результат: 0.3с (фраза <0.5с) ... 1.0с (фраза >2.5с).
    """
    audio_sec = audio_bytes / 48000  # 24kHz 16-bit mono = 48000 bytes/sec
    return min(0.3 + min(audio_sec, 2.5) * 0.28, INTERRUPTED_PAUSE_SEC)


def _is_voice_rms(pcm: bytes) -> bool:
    """Fallback RMS VAD."""
    n = len(pcm) // 2
    if n == 0:
        return False
    samples = struct.unpack(f"<{n}h", pcm[:n * 2])
    rms = math.sqrt(sum(s * s for s in samples) / n)
    return rms > VAD_THRESHOLD


class _DebugCall:
    """
    Mock BinotelCall для debug режиму.
    Замість SIP — локальний мікрофон і динамік.
    Має той самий інтерфейс що BinotelCall → run_production() працює без змін.
    """

    def __init__(self, pa) -> None:
        self.is_active = True
        self._out = pa.open(format=FORMAT, channels=CHANNELS, rate=OUTPUT_RATE, output=True)
        self._inp = pa.open(
            format=FORMAT, channels=CHANNELS, rate=INPUT_RATE,
            input=True, frames_per_buffer=CHUNK,
        )

    async def send_audio(self, pcm: bytes) -> None:
        if self.is_active:
            loop = asyncio.get_event_loop()
            await loop.run_in_executor(None, self._out.write, pcm)

    async def receive_audio(self):
        loop = asyncio.get_event_loop()
        while self.is_active:
            data = await loop.run_in_executor(
                None, lambda: self._inp.read(CHUNK, exception_on_overflow=False)
            )
            yield data

    def flush_audio(self) -> None:
        pass  # debug: PyAudio output має власний буфер, flush не потрібен

    async def hangup(self) -> None:
        self.is_active = False
        logger.info("[DEBUG] Дзвінок завершено")

    async def transfer_to_operator(self, reason: str = "") -> None:
        self.is_active = False
        logger.info(f"[DEBUG] Передача оператору: {reason}")

    def close(self) -> None:
        self._out.stop_stream()
        self._out.close()
        self._inp.stop_stream()
        self._inp.close()


class Block1Agent:
    """
    Голосовий агент для нагадування пацієнту про візит.

    Використовує Gemini Live API з native audio — без STT/TTS посередників.
    Function calling дозволяє агенту підтверджувати/переносити/скасовувати візити
    прямо під час розмови.
    """

    def __init__(self, visit: dict) -> None:
        self.visit = visit
        self.client = genai.Client(
            vertexai=True,
            project=VERTEX_PROJECT,
            location=VERTEX_LOCATION,
        )
        self.call_result: str | None = None
        self.call_duration: float = 0.0
        self._pa = pyaudio.PyAudio() if _PYAUDIO_AVAILABLE else None
        self._end_event = asyncio.Event()
        self._monitor_task: asyncio.Task | None = None
        self._resumption_handle: str | None = None
        self._session_reconnected: bool = False

    # ------------------------------------------------------------------
    # Конфіг сесії
    # ------------------------------------------------------------------

    def _build_config(self, resume_handle: str | None = None) -> types.LiveConnectConfig:
        prompt = build_system_prompt(self.visit)
        return types.LiveConnectConfig(
            response_modalities=["AUDIO"],
            speech_config=types.SpeechConfig(
                voice_config=types.VoiceConfig(
                    prebuilt_voice_config=types.PrebuiltVoiceConfig(
                        voice_name="Zephyr"
                    )
                ),
                language_code="uk-UA",
            ),
            system_instruction=types.Content(
                parts=[types.Part(text=prompt)]
            ),
            tools=TOOLS,
            # Вимикаємо "thinking" — для real-time голосу паузи на "обдумування"
            # неприйнятні: пацієнт чує мовчання, потім весь текст відразу (розмазаний звук).
            thinking_config=types.ThinkingConfig(thinking_budget=0),
            output_audio_transcription=types.AudioTranscriptionConfig() if GEMINI_LOGS else None,
            input_audio_transcription=types.AudioTranscriptionConfig(),
            # VAD на стороні Gemini: повільніше реагує на кінець мови → менше обрізань пацієнта.
            # end_of_speech_sensitivity=LOW → довша пауза перш ніж Gemini вважає що пацієнт замовк.
            realtime_input_config=types.RealtimeInputConfig(
                automatic_activity_detection=types.AutomaticActivityDetection(
                    start_of_speech_sensitivity=types.StartSensitivity.START_SENSITIVITY_HIGH,
                    end_of_speech_sensitivity=types.EndSensitivity.END_SENSITIVITY_LOW,
                    silence_duration_ms=1000,
                )
            ),
            # Session resumption: дозволяє відновити сесію після розриву за токеном (handle).
            # Замінює silence keepalive — більше не треба надсилати тишу щоб утримати сесію.
            # resume_handle=None → нова сесія; resume_handle=<token> → відновлення контексту.
            session_resumption=types.SessionResumptionConfig(
                handle=resume_handle,
                transparent=True,
            ),
        )

    # ------------------------------------------------------------------
    # DEBUG режим: локальний мікрофон ↔ динамік
    # ------------------------------------------------------------------

    async def run_debug(self) -> str:
        logger.info("[DEBUG] Симуляція дзвінка — мікрофон/динамік")
        call = _DebugCall(self._pa)
        try:
            return await self.run_production(call)
        except KeyboardInterrupt:
            logger.info("[AGENT] Перервано користувачем")
            return self.call_result or "no_result"
        finally:
            call.close()

    # ------------------------------------------------------------------
    # PRODUCTION режим: Binotel SIP ↔ Gemini Live
    # ------------------------------------------------------------------

    async def run_production(self, binotel_call) -> str:
        """
        Production режим: аудіо-міст між Binotel SIP та Gemini Live.

        Args:
            binotel_call: BinotelCall об'єкт з методами send_audio / receive_audio / hangup
        """
        logger.info("=" * 55)
        logger.info("[PRODUCTION] Дзвінок через Binotel")
        logger.info(f"  Пацієнт : {self.visit['patient_name']}")
        logger.info(f"  Лікар   : {self.visit['doctor_name']}")
        logger.info(f"  Час     : {self.visit['visit_datetime']}")
        logger.info("=" * 55)
        if LOG_FILE:
            from services import call_logger
            call_logger.start_call(self.visit)

        start = time.time()

        greeting_buffer = getattr(self, "_greeting_buffer", [])
        greeting_duration = getattr(self, "_greeting_duration", 0.0)
        _stale_warmup = [False]  # True коли сесія протухла але буфер ще валідний

        async def _stream(session) -> None:
            """Двосторонній стрімінг аудіо між Binotel SIP та Gemini."""

            _greeting_done = asyncio.Event()
            _patient_spoke = asyncio.Event()   # скидається на початку кожного silence-чеку
            _silence_count = 0
            _silence_task: asyncio.Task | None = None   # _check_silence task
            _timer_task: asyncio.Task | None = None     # _start_silence_timer task
            _call_cache: dict = {}  # захист від дублікатів tool calls
            # Seed від warmup prefetch (завантажено під час гудків → завжди готово)
            if getattr(self, "_prefetched_raw_slots", None):
                _call_cache["__raw_slots"] = self._prefetched_raw_slots
                logger.debug(f"[AGENT] Prefetch seed: {len(self._prefetched_raw_slots)} слотів → _call_cache")
            _activity = [time.time()]  # [0] = час останньої активності (для watchdog)
            _interrupted_pause_until = [0.0]  # до цього часу ігноруємо аудіо пацієнта (ехо-гейт)
            # Фільтр дублікатних турів: Gemini Live іноді генерує 2 тури на одну подію.
            # Після кожного AI туру піднімаємо прапор — наступний тур не відтворюємо в SIP.
            # Прапор знімається ТІЛЬКИ коли input_transcription підтверджує НОВИЙ текст пацієнта.
            # Дедуплікація по тексту запобігає тому що ghost тури скидають suppress
            # повторним input_transcription від того ж мовлення.
            _suppress_next_audio = [False]
            _last_patient_said_text = [""]  # текст останньої НОВОЇ транскрипції пацієнта
            _patient_ever_spoke = [False]   # True після першого VAD → one-way audio detection
            _patient_said_count = [0]       # кількість НОВИХ input_transcription від пацієнта (захист від передчасних tool calls)
            # True якщо VAD зафіксував реальний голос пацієнта і input_transcription ще не
            # скористався цим сигналом. (Резервний захист — input_transcription сам по собі
            # надійний, але _real_vad_pending додає ще один рівень безпеки.)
            _real_vad_pending = [False]
            # Стан підтвердження переносу:
            # 0 = reschedule ще не викликався
            # 1 = заблоковано, AI запитав "Все вірно?" — чекаємо відповідь пацієнта
            # 2 = пацієнт відповів після блокування → дозволяємо reschedule
            _reschedule_state = [0]
            # True одразу після блокування reschedule_visit → наступний тур (питання
            # підтвердження) повинен пробитись крізь suppress. Одноразовий прапор.
            _reschedule_just_blocked = [False]
            # True якщо get_available_slots(date=...) вже виконано в цьому дзвінку.
            # Потрібно для розрізнення "пацієнт підтвердив після вибору часу" від
            # "Gemini намагається reschedule без вибору дати і часу".
            _date_slots_fetched = [False]
            # True → наступний ghost тур має грати як реальний (після get_available_slots без дати).
            # Встановлюється в post-loop Turn 1, скидається коли Turn 2 проривається.
            _allow_next_turn = [False]
            # Час завершення останнього AI туру з реальним аудіо (для double-turn guard).
            _last_ai_turn_end = [0.0]

            async def _check_silence() -> None:
                """Чекає SILENCE_TIMEOUT секунд. Якщо пацієнт мовчить — питаємо або вішаємо."""
                nonlocal _silence_count
                _patient_spoke.clear()
                try:
                    await asyncio.wait_for(_patient_spoke.wait(), timeout=SILENCE_TIMEOUT_SEC)
                    # Пацієнт заговорив — скидаємо лічильник
                    _silence_count = 0
                except asyncio.TimeoutError:
                    if self._end_event.is_set():
                        return
                    _silence_count += 1
                    logger.info(f"[AGENT] Тиша {_silence_count}/{MAX_SILENCE_COUNT}")
                    # One-way audio: після 2 раундів тиші без жодного голосу від пацієнта —
                    # SIP з'єднання ймовірно одностороннє. Завершуємо → scheduler зробить retry.
                    if _silence_count >= 2 and not _patient_ever_spoke[0]:
                        logger.warning("[AGENT] One-way audio: жодного голосу від пацієнта — завершуємо")
                        self.call_result = "no_answer"
                        self._end_event.set()
                        return
                    if _silence_count >= MAX_SILENCE_COUNT:
                        logger.info("[AGENT] Тиша 3 рази — завершуємо дзвінок")
                        self.call_result = "no_answer"
                        self._end_event.set()
                        _suppress_next_audio[0] = False
                        await session.send_client_content(
                            turns={"parts": [{"text": (
                                "Пацієнт не відповідає. Скажи коротке прощання і виклич end_call(result='no_answer')."
                            )}]},
                            turn_complete=True,
                        )
                    elif _silence_count == 1:
                        _suppress_next_audio[0] = False
                        await session.send_client_content(
                            turns={"parts": [{"text": (
                                "Скажи пацієнту: «Повторіть будь ласка, я не почула вас.» — і мовчки чекай."
                            )}]},
                            turn_complete=True,
                        )
                    else:
                        _suppress_next_audio[0] = False
                        await session.send_client_content(
                            turns={"parts": [{"text": (
                                "Скажи пацієнту: «Я все ще чекаю вашої відповіді.» — і мовчки чекай."
                            )}]},
                            turn_complete=True,
                        )

            async def _start_silence_timer(audio_delay: float = 0.0) -> None:
                nonlocal _silence_task
                if _silence_task and not _silence_task.done():
                    _silence_task.cancel()
                if self._end_event.is_set():
                    return
                # Чекаємо поки аудіо відіграє через SIP + даємо пацієнту зреагувати
                await asyncio.sleep(audio_delay + SILENCE_START_DELAY_SEC)
                if self._end_event.is_set():
                    return
                _silence_task = asyncio.create_task(
                    _check_silence(), name="silence_monitor"
                )

            def _schedule_silence_timer(audio_delay: float = 0.0) -> None:
                """Скасовує попередній _start_silence_timer і запускає новий."""
                nonlocal _timer_task
                if _timer_task and not _timer_task.done():
                    _timer_task.cancel()
                if not self._end_event.is_set():
                    _timer_task = asyncio.create_task(
                        _start_silence_timer(audio_delay), name="silence_timer_start"
                    )

            _watchdog_injected = [0]  # винесено в _stream scope щоб скидати при продовженні розмови

            async def _watchdog() -> None:
                """Фоновий watchdog: якщо жодної активності за WATCHDOG_TIMEOUT_SEC — re-inject або hangup."""
                while not self._end_event.is_set():
                    await asyncio.sleep(5)
                    if self._end_event.is_set():
                        break
                    if not _greeting_done.is_set():
                        continue  # не рахуємо бездіяльність поки грає привітання
                    idle = time.time() - _activity[0]
                    if idle < WATCHDOG_TIMEOUT_SEC:
                        continue
                    _activity[0] = time.time()  # скидаємо щоб не спрацював двічі підряд
                    _watchdog_injected[0] += 1
                    if _watchdog_injected[0] >= 2:
                        logger.warning(f"[AGENT] Watchdog: {idle:.0f}с бездіяльності — завершуємо дзвінок")
                        self.call_result = "no_answer"
                        self._end_event.set()
                        return
                    logger.warning(f"[AGENT] Watchdog: {idle:.0f}с бездіяльності — re-inject prompt")
                    try:
                        _suppress_next_audio[0] = False
                        await session.send_client_content(
                            turns={"parts": [{"text": "Скажи пацієнту: «Повторіть будь ласка, я не почула вас.» — і мовчки чекай."}]},
                            turn_complete=True,
                        )
                    except Exception as e:
                        logger.error(f"[AGENT] Watchdog помилка send_client_content: {e!r}")
                        self._end_event.set()
                        return

            async def _send_patient_audio() -> None:
                _vad_buf = bytearray()  # буфер webrtcvad (потребує фіксовані 10мс = 320 байт)
                # Adaptive SNR: відсікає фоновий шум який webrtcvad помилково класифікує як голос
                _noise_rms = [0.0]     # поточний рівень фонового шуму (адаптивний)
                _NOISE_ALPHA = 0.05    # швидкість адаптації (~20 chunks для стабілізації)
                _SNR_RATIO = 2.0       # мін. SNR: голос повинен бути в 2.0x гучніше фону
                # Pre-roll буфер: зберігаємо останні ~300мс аудіо під час echo gate.
                # Коли gate спливає — відправляємо буфер першим, щоб Gemini почула
                # перші склади слова пацієнта що прозвучали під час gate.
                import collections
                _preroll_buf: collections.deque = collections.deque(maxlen=20)  # ~320мс @ 16ms/chunk
                _was_in_gate = [False]
                async for chunk in binotel_call.receive_audio():
                    if self._end_event.is_set():
                        break
                    if not _greeting_done.is_set():
                        continue
                    # Ехо-гейт: після interrupted ігноруємо аудіо поки не розсіється відлуння
                    if time.time() < _interrupted_pause_until[0]:
                        _preroll_buf.append(chunk)  # зберігаємо поки в gate
                        _was_in_gate[0] = True
                        continue
                    # Gate щойно закінчився — відправляємо накопичений pre-roll
                    if _was_in_gate[0]:
                        _was_in_gate[0] = False
                        for _pre_chunk in _preroll_buf:
                            await session.send_realtime_input(
                                audio={"data": _pre_chunk, "mime_type": "audio/pcm;rate=16000"}
                            )
                        _preroll_buf.clear()
                    # VAD: визначаємо чи є голос у chunk
                    if _VAD_AVAILABLE:
                        _vad_buf.extend(chunk)
                        _has_voice = False
                        while len(_vad_buf) >= _VAD_FRAME_BYTES:
                            frame = bytes(_vad_buf[:_VAD_FRAME_BYTES])
                            del _vad_buf[:_VAD_FRAME_BYTES]
                            try:
                                if _VAD.is_speech(frame, 16000):
                                    _has_voice = True
                            except Exception:
                                pass
                    else:
                        _has_voice = _is_voice_rms(chunk)
                    # Adaptive SNR check: навіть якщо VAD каже "голос", перевіряємо
                    # чи RMS достатньо вищий за фоновий шум. В шумному оточенні
                    # (вулиця, кафе) це запобігає хибним спрацюванням.
                    _n_s = len(chunk) // 2
                    if _n_s > 0:
                        _samps = struct.unpack(f"<{_n_s}h", chunk[:_n_s * 2])
                        _chunk_rms = math.sqrt(sum(s * s for s in _samps) / _n_s)
                    else:
                        _chunk_rms = 0.0
                    if not _has_voice:
                        # Тиша — плавно оновлюємо noise floor
                        _noise_rms[0] += _NOISE_ALPHA * (_chunk_rms - _noise_rms[0])
                    elif _noise_rms[0] > 50 and _chunk_rms < _noise_rms[0] * _SNR_RATIO:
                        # VAD каже "голос" але SNR занизький → фоновий шум, не мова
                        _has_voice = False
                        _noise_rms[0] += _NOISE_ALPHA * (_chunk_rms - _noise_rms[0])
                    if _has_voice:
                        _patient_spoke.set()
                        _patient_ever_spoke[0] = True
                        _activity[0] = time.time()
                        _real_vad_pending[0] = True  # реальний голос → input_transcription може споживати
                        # НЕ скидаємо _suppress_next_audio тут — тільки input_transcription скидає.
                        # Інакше VAD скидає suppress раніше ніж приходить транскрипція,
                        # і ghost Turn 2 вважається "реальним" туром.
                    await session.send_realtime_input(
                        audio={"data": chunk, "mime_type": "audio/pcm;rate=16000"}
                    )

            async def _recv_and_handle() -> None:
                nonlocal _silence_count
                _draining = False       # True — чекаємо поки AI договорить перед завершенням
                _ai_buf: list[str] = []        # накопичуємо транскрипцію AI за тур
                try:
                    # session.receive() — пер-тернова: виходить після кожного turn_complete.
                    # while True перезапускає для наступного turn.
                    while True:
                        turn_had_content = False
                        turn_had_audio = False       # True якщо в цьому турі AI говорила
                        turn_audio_bytes = 0         # байти аудіо в цьому турі → для розрахунку затримки
                        _turn_audio_played = False   # True якщо аудіо реально відправлено пацієнту (не dropped)
                        _had_patient_said = False    # True якщо в поточному inner-loop був input_transcription
                        # True якщо в цьому турі є get_available_slots(no date) з успішним результатом.
                        # Turn 1 з цим tool call часто галюцинує ("вільних слотів немає").
                        # Глушимо Turn 1 аудіо і дозволяємо Turn 2 (ghost) грати — він правильний.
                        _suppress_slots_turn = False
                        _ai_text_lower = ""   # текст AI цього туру (для перевірки ack vs full)
                        _ai_buf.clear()
                        async for resp in session.receive():
                            turn_had_content = True
                            # Якщо пацієнт перебив AI — зупиняємо аудіо, слухаємо пацієнта
                            if resp.server_content and resp.server_content.interrupted:
                                logger.info("[AGENT] AI перервано — пацієнт говорить")
                                # Flush Opus черги: trans() ще тримав буферизовані фрейми →
                                # без flush AI говорить ~0.5-1с після того як Gemini вже зупинився.
                                binotel_call.flush_audio()
                                # НЕ ставимо гейт — пацієнт ЗАРАЗ говорить, не можна блокувати його аудіо.
                                # Луна від обрізаного слова AI мінімальна і Gemini її сам ігнорує.
                                _patient_spoke.set()
                                if _timer_task and not _timer_task.done():
                                    _timer_task.cancel()
                                continue
                            if resp.server_content:
                                sc = resp.server_content
                                if sc.output_transcription and sc.output_transcription.text:
                                    _ai_buf.append(sc.output_transcription.text)
                                # input_transcription замінює patient_said tool:
                                # Gemini надсилає транскрипцію вхідного аудіо без tool call →
                                # ніякої відповіді не потрібно → ніяких ghost-турів.
                                # Guard 1: поки greeting не зіграло — будь-яка транскрипція фантомна
                                # (warmup leakage / ехо), ігноруємо щоб не тригерити tool calls.
                                # Guard 2: під час echo gate — аудіо до Gemini не йде, тож
                                # будь-яка транскрипція це артефакт; ігноруємо.
                                if (sc.input_transcription and sc.input_transcription.text
                                        and _greeting_done.is_set()
                                        and time.time() >= _interrupted_pause_until[0]):
                                    _it_text = sc.input_transcription.text.strip()
                                    if _it_text and _it_text != _last_patient_said_text[0]:
                                        _last_patient_said_text[0] = _it_text
                                        _suppress_next_audio[0] = False
                                        _had_patient_said = True
                                        _patient_said_count[0] += 1
                                        _real_vad_pending[0] = False
                                        _allow_next_turn[0] = False
                                        _activity[0] = time.time()
                                        _silence_count = 0           # розмова продовжується — скидаємо лічильники
                                        _watchdog_injected[0] = 0   # watchdog теж скидаємо — розмова жива
                                        # _reschedule_state: перехід 1→2 не тут.
                                        # AI сама вирішує чи підтвердив пацієнт і викликає reschedule_visit вдруге.
                                        logger.info(f"[ПАЦІЄНТ] {_it_text}")
                                        if LOG_FILE:
                                            from services import call_logger
                                            call_logger.log_patient(_it_text)
                                if sc.turn_complete:
                                    if _ai_buf:
                                        # Ghost = аудіо так і не дійшло до пацієнта в цьому турі
                                        _is_ghost = not _turn_audio_played
                                        if GEMINI_LOGS:
                                            _tag = "[AI GHOST]" if _is_ghost else "[AI]"
                                            logger.info(f"{_tag} {''.join(_ai_buf)}")
                                        if LOG_FILE and not _is_ghost:
                                            from services import call_logger
                                            call_logger.log_ai("".join(_ai_buf))
                                        # Behavior 1: AI сама запитала підтвердження переносу
                                        # (без попереднього виклику reschedule_visit) → state 0→1
                                        # → щоб наступний input_transcription "так" advance 1→2
                                        if not _is_ghost:
                                            _ai_text = "".join(_ai_buf)
                                            _ai_text_lower = _ai_text.lower()
                                            if any(w in _ai_text_lower for w in ("вірно", "правильно")):
                                                # state=1 ТІЛЬКИ якщо AI підтверджує конкретний СЛОТ (дата + час).
                                                # "На 16 жовтня, правильно?" — лише дата → НЕ міняємо state.
                                                # "Переносимо на 16 жовтня о третій. Все вірно?" → є час → state=1.
                                                # "Переносимо на 17 жовтня о 10:40. Все вірно?" → HH:MM → state=1.
                                                # Маркери конкретного слоту: HH:MM АБО слово "переносим".
                                                # "переносим" покриває "переносимо", "переносити" і варіанти з
                                                # текстовим часом ("о третій", "о пів на четверту" тощо).
                                                _slot_confirmed = (
                                                    re.search(r'\d{1,2}:\d{2}', _ai_text)
                                                    or 'переносим' in _ai_text_lower
                                                )
                                                if _slot_confirmed:
                                                    _reschedule_state[0] = 1
                                                    logger.debug("[AGENT] AI запитала підтвердження слоту → reschedule_state=1")
                                    _ai_buf.clear()
                                    if _draining:
                                        # Чекаємо поки AI договорить перед завершенням
                                        if turn_had_audio:
                                            logger.info("[AGENT] AI завершив прощання — чекаємо 1с...")
                                            await asyncio.sleep(1.0)
                                        else:
                                            # end_call прийшов без аудіо в цьому турі — прощання
                                            # вже прозвучало в попередньому турі (напр. AI сказав
                                            # "До побачення!" до cancel_visit, потім end_call окремо).
                                            # Виходимо одразу — інакше recv_task чекає Turn C якого немає → 15с.
                                            logger.info("[AGENT] end_call без аудіо — прощання вже прозвучало")
                                        return
                                    # Double-turn guard: Gemini іноді генерує 2 тури підряд (~1-2с) без відповіді
                                    # пацієнта. VAD між турами ловить фоновий шум → _real_vad_pending=True →
                                    # другий тур обходить suppress. Якщо від кінця попереднього AI туру пройшло
                                    # менше 2.5с і input_transcription не підтвердив реального голосу — це шум.
                                    _MIN_INTER_TURN_GAP = 2.5
                                    if _real_vad_pending[0] and not _had_patient_said:
                                        if time.time() - _last_ai_turn_end[0] < _MIN_INTER_TURN_GAP:
                                            _real_vad_pending[0] = False
                                            logger.debug("[AGENT] Double-turn guard: _real_vad_pending скинуто (шум між турами)")
                                    # Реальний тур: suppress вимкнений АБО в цьому inner-loop був input_transcription.
                                    # Ghost/дублікатний тур: suppress=True і транскрипції не було.
                                    # _allow_next_turn: Turn 1 мав get_available_slots → дозволяємо Turn 2 гратися.
                                    if (not _suppress_next_audio[0]) or _had_patient_said or _allow_next_turn[0] or _real_vad_pending[0]:
                                        # Скидаємо _allow_next_turn якщо ghost з датами щойно зіграв
                                        # (allow_next_turn=True і пацієнт не говорив у цьому турі).
                                        # Якщо вийшли через _had_patient_said — allow_next_turn вже
                                        # скинуто в input_transcription handler.
                                        if _allow_next_turn[0] and not _had_patient_said:
                                            _allow_next_turn[0] = False  # ghost bypass використано (ghost з датами зіграв)
                                        if _real_vad_pending[0] and not _had_patient_said:
                                            # VAD підтвердив реальний голос але input_transcription не надійшов
                                            # (коротке слово або перерване розпізнавання).
                                            # Знімаємо suppress щоб AI відповіла — інакше deadlock.
                                            _suppress_next_audio[0] = False
                                            _real_vad_pending[0] = False
                                        break
                                    # Ghost тур — НЕ виходимо з inner loop, чекаємо реальної відповіді пацієнта.
                                    logger.debug("[AGENT] Ghost тур — продовжуємо читати сесію")
                                    # НЕ встановлюємо echo gate: ghost аудіо не відтворювалось →
                                    # луни немає → блокувати пацієнта не треба.
                                    # Без цього виправлення: turn_had_audio=True (бо Gemini надіслав аудіо)
                                    # → gate блокував _send_patient_audio → Gemini не чув пацієнта
                                    # → генерував ще ghost → repeat ("по 3 рази повторювати" баг).
                                    if turn_had_audio and _greeting_done.is_set():
                                        _schedule_silence_timer(audio_delay=0.0)
                                    turn_had_audio = False
                                    turn_audio_bytes = 0
                                    _turn_audio_played = False  # скидаємо для наступного ghost-туру
                                    _had_patient_said = False
                            if resp.data and _greeting_done.is_set():
                                turn_audio_bytes += len(resp.data)
                                if _suppress_slots_turn and not _allow_next_turn[0]:
                                    pass  # Turn 1 після get_available_slots(no date) → завжди drop, Turn 2 грає замість
                                elif not _suppress_next_audio[0] or _allow_next_turn[0]:
                                    # Грає коли: suppress вимкнений АБО _allow_next_turn bypass (ghost з датами).
                                    await binotel_call.send_audio(resp.data)
                                    _turn_audio_played = True   # аудіо реально дійшло до пацієнта
                                    # Гейт "ковзає" разом з мовою AI під час стріму.
                                    # Ghost тури не грають → луни немає → гейт не потрібен.
                                    # Точний forward-dating виставляється на turn_complete (нижче).
                                    _interrupted_pause_until[0] = time.time() + _calc_echo_gate(turn_audio_bytes)
                                turn_had_audio = True
                                _activity[0] = time.time()
                                # AI говорить — скасовуємо таймер щоб він не тік під час мови AI
                                _patient_spoke.set()
                                if _timer_task and not _timer_task.done():
                                    _timer_task.cancel()
                                if _silence_task and not _silence_task.done():
                                    _silence_task.cancel()
                            if _draining:
                                continue  # пропускаємо tool_call поки AI договорить
                            if resp.tool_call:
                                _patient_spoke.set()
                                _activity[0] = time.time()
                                _schedule_silence_timer(audio_delay=0.0)
                                # Ghost tool call: (suppress=True АБО allow_next_turn=True) і input_transcription
                                # не приніс нового тексту. allow_next_turn=True = ми в "ghost з датами" вікні —
                                # будь-який батч без нового мовлення є ghost (напр. cancel після дублікатного "Ні").
                                # Надсилаємо порожні відповіді щоб сесія не зависла,
                                # але пропускаємо реальну обробку — розриваємо ланцюжок ghost-ів.
                                # ВАЖЛИВО: НЕ ставимо _interrupted_pause_until для ghost-батчів —
                                # ghost аудіо не грає, луни немає, але gate блокує пацієнта.
                                # Тільки end_call обходить ghost-перевірку — щоб дзвінок завершувався
                                # навіть якщо Gemini викликає end_call в окремому ghost-турі після прощання.
                                # reschedule/confirm/cancel НЕ обходять: ghost-тури не мають підтвердження
                                # від пацієнта — виконувати їх без підтвердження небезпечно.
                                _has_critical = any(
                                    fc.name == "end_call"
                                    for fc in resp.tool_call.function_calls
                                )
                                if (_suppress_next_audio[0] or _allow_next_turn[0]) and not _had_patient_said and not _has_critical:
                                    logger.debug("[AGENT] Ghost tool batch — пропускаємо")
                                    await session.send_tool_response(function_responses=[
                                        types.FunctionResponse(
                                            id=fc.id, name=fc.name, response={"success": True}
                                        )
                                        for fc in resp.tool_call.function_calls
                                    ])
                                    continue
                                # Збираємо всі відповіді в батч — надсилаємо одним send_tool_response,
                                # щоб Gemini не генерував проміжні turn-и між tool calls.
                                _batch: list[types.FunctionResponse] = []
                                _after_batch = None  # "end_call" — виконати після відправки
                                _slots_fetched = False  # True якщо get_available_slots успішно виконано в цьому батчі
                                for fc in resp.tool_call.function_calls:
                                    args = dict(fc.args) if fc.args else {}
                                    # Скидаємо suppress якщо input_transcription вже підтвердив
                                    # реальне мовлення пацієнта в поточному циклі.
                                    if _had_patient_said:
                                        _suppress_next_audio[0] = False
                                    # Захист від передчасних дій: confirm/cancel/reschedule заборонено
                                    # поки привітання не зіграло І поки пацієнт не сказав хоч щось.
                                    # Без цього: SIP-шум на старті → garbage transcription → AI відразу
                                    # скасовує запис ще до початку діалогу.
                                    _is_destructive = fc.name in ("confirm_visit", "cancel_visit", "reschedule_visit")
                                    if _is_destructive and (not _greeting_done.is_set() or _patient_said_count[0] == 0):
                                        logger.warning(f"[AGENT] {fc.name} заблоковано — привітання ще не завершено або пацієнт ще не говорив")
                                        _batch.append(types.FunctionResponse(
                                            id=fc.id, name=fc.name,
                                            response={
                                                "success": False,
                                                "message": "Спочатку поговори з пацієнтом — дочекайся його відповіді на запитання.",
                                            },
                                        ))
                                        continue
                                    # end_call(no_result) після першого ж повідомлення пацієнта — занадто рано.
                                    # Пацієнт міг задати питання (куди їхати, що взяти) — треба відповісти.
                                    # Блокуємо no_result якщо менше 2 реальних input_transcription.
                                    _end_args = dict(fc.args) if fc.args else {}
                                    if fc.name == "end_call" and _end_args.get("result") == "no_result" and _patient_said_count[0] < 2:
                                        logger.warning("[AGENT] end_call(no_result) заблоковано — менше 2 реплік від пацієнта")
                                        _batch.append(types.FunctionResponse(
                                            id=fc.id, name=fc.name,
                                            response={
                                                "success": False,
                                                "message": "Зарано завершувати. Пацієнт тільки-но почав говорити — дай відповідь на його питання і продовжуй розмову.",
                                            },
                                        ))
                                        continue
                                    # reschedule_visit — блокуємо тільки першу спробу (state=0).
                                    # AI отримує інструкцію спитати підтвердження → state→1.
                                    # Якщо AI вирішила що пацієнт підтвердив — сама знову викличе reschedule_visit.
                                    # Другий виклик (state=1) вже пропускаємо — AI сама вирішує.
                                    if fc.name == "reschedule_visit" and _reschedule_state[0] < 1:
                                        logger.warning("[AGENT] reschedule_visit заблоковано — потрібне підтвердження від пацієнта")
                                        _reschedule_state[0] = 1
                                        _reschedule_just_blocked[0] = True
                                        # Gemini вже встигла згенерувати "Чудово! Перенесено" аудіо
                                        # поки обробляли tool call → flush SIP буфер і придушуємо
                                        _suppress_next_audio[0] = True
                                        binotel_call.flush_audio()
                                        logger.info("[AGENT] reschedule block: flush аудіо + suppress щоб не грало 'Перенесено'")
                                        _batch.append(types.FunctionResponse(
                                            id=fc.id, name=fc.name,
                                            response={
                                                "success": False,
                                                "message": (
                                                    "Перед переносом скажи пацієнту дату і час: "
                                                    "'Отже, переносимо на [дата] о [час]. Все вірно?' "
                                                    "і мовчки ЧЕКАЙ відповідь. Тільки після 'так' — виклич reschedule_visit."
                                                ),
                                            },
                                        ))
                                        continue
                                    # end_call поки чекаємо підтвердження переносу:
                                    # AI запитав "Все вірно?" — не завершувати поки пацієнт не відповість.
                                    # Виняток: cancel_visit вже виконано → reschedule flow не актуальний → дозволяємо.
                                    if fc.name == "end_call" and _reschedule_state[0] == 1:
                                        if _call_cache.get("cancel_visit", {}).get("success"):
                                            logger.debug("[AGENT] end_call дозволено — cancel_visit вже виконано (reschedule скасовано)")
                                        else:
                                            logger.warning("[AGENT] end_call заблоковано — чекаємо підтвердження переносу")
                                            _batch.append(types.FunctionResponse(
                                                id=fc.id, name=fc.name,
                                                response={
                                                    "success": False,
                                                    "message": "НЕ завершуй дзвінок. Ти запитав пацієнта про підтвердження переносу — мовчки чекай відповідь.",
                                                },
                                            ))
                                            continue
                                    # end_call разом з get_available_slots (одному батчі або в наступному) = галюцинація:
                                    # Gemini отримала слоти але ще не сказала їх пацієнту, вже хоче завершити.
                                    # _slots_fetched: захист в межах поточного батчу
                                    # _date_slots_fetched[0]: захист між батчами (персистентна між resp)
                                    if fc.name == "end_call" and (_slots_fetched or _date_slots_fetched[0]):
                                        logger.warning("[AGENT] end_call проігноровано — викликано в одному батчі з get_available_slots (галюцинація)")
                                        _batch.append(types.FunctionResponse(
                                            id=fc.id, name=fc.name,
                                            response={
                                                "success": False,
                                                "message": "НЕ завершуй дзвінок. Ти щойно отримала слоти — продовжуй розмову: назви доступні часи пацієнту.",
                                            },
                                        ))
                                        continue
                                    try:
                                        result = await handle_tool_call(fc.name, args, self.visit, _call_cache)
                                    except Exception as tool_err:
                                        logger.error(f"[AGENT] Tool {fc.name} помилка: {tool_err!r}")
                                        result = {
                                            "success": False,
                                            "error": (
                                                "Технічна помилка при виконанні операції. "
                                                "Скажи пацієнту що сталась тимчасова технічна проблема "
                                                "і запропонуй зателефонувати в клініку для вирішення питання."
                                            ),
                                        }
                                    _batch.append(types.FunctionResponse(id=fc.id, name=fc.name, response=result))
                                    # reschedule_visit успішно виконано → state=2 щоб end_call
                                    # в тому ж батчі не блокувався (перевірка state==1 на рядку ~677).
                                    if fc.name == "reschedule_visit" and result.get("success"):
                                        _reschedule_state[0] = 2
                                    if fc.name == "get_available_slots" and result.get("success"):
                                        _slots_fetched = True
                                        # Якщо повернуто конкретні слоти для дати — позначаємо:
                                        # пацієнт вже пройшов вибір дати і часу
                                        if result.get("date"):
                                            _date_slots_fetched[0] = True
                                        else:
                                            # get_available_slots без дати (Step 1: список дат).
                                            # Turn 1 часто галюцинує "вільних слотів немає" до того
                                            # як Gemini інтегрує результат → глушимо Turn 1,
                                            # Turn 2 (ghost) з правильними датами зіграє замість нього.
                                            _suppress_slots_turn = True
                                            # Race condition fix: Gemini надсилає аудіо Turn 1
                                            # ДО того як tool result приходить → частковий список
                                            # вже починає грати. Обрізаємо черги + скидаємо флаг
                                            # щоб Turn 1 трактувався як ghost, Turn 2 — як реальний.
                                            binotel_call.flush_audio()
                                            _turn_audio_played = False
                                            logger.debug("[AGENT] get_available_slots(no date) → flush Turn 1 audio, suppress")
                                    if fc.name == "end_call":
                                        self.call_result = result.get("result", "no_result")
                                        # Якщо одна з ключових дій вже виконана — гарантуємо
                                        # правильний результат (захист від Gemini confusion
                                        # після тиші/перепитування "Алло?").
                                        if _call_cache.get("reschedule_visit", {}).get("success") and self.call_result != "rescheduled":
                                            logger.warning(f"[AGENT] end_call({self.call_result}) але reschedule вже виконано → rescheduled")
                                            self.call_result = "rescheduled"
                                        elif _call_cache.get("confirm_visit", {}).get("success") and self.call_result not in ("confirmed", "rescheduled"):
                                            logger.warning(f"[AGENT] end_call({self.call_result}) але confirm вже виконано → confirmed")
                                            self.call_result = "confirmed"
                                        elif _call_cache.get("cancel_visit", {}).get("success") and self.call_result != "cancelled":
                                            logger.warning(f"[AGENT] end_call({self.call_result}) але cancel вже виконано → cancelled")
                                            self.call_result = "cancelled"
                                        logger.info(f"[AGENT] end_call → {self.call_result}")
                                        self._end_event.set()
                                        _after_batch = ("end_call",)
                                        break
                                if _batch:
                                    await session.send_tool_response(function_responses=_batch)
                                if _after_batch:
                                    _draining = True
                        # session.receive() вийшов (turn_complete або сесія закрита)
                        if not turn_had_content:
                            logger.warning("[AGENT] recv_task: session.receive() без контенту — сесія закрита")
                            return
                        logger.debug("[AGENT] recv_task: turn завершено, перезапускаємо session.receive()")
                        # Запускаємо таймер тиші тільки якщо AI реально говорила в цьому турі.
                        # Якщо тур був лише tool_call (без аудіо) — таймер не запускаємо,
                        # щоб він не тік поки AI формує та озвучує відповідь у наступному турі.
                        # _suppress_slots_turn = Turn 1 мав get_available_slots(no date).
                        # Gemini ЗАВЖДИ галюцинує в Turn 1 (неповні дати, неправильний порядок).
                        # Turn 2 має правильні дати → глушимо Turn 1 і дозволяємо Turn 2 зіграти.
                        # Стосується будь-якого Turn 1 — незалежно від того чи говорив пацієнт.
                        if not _draining and _greeting_done.is_set() and _suppress_slots_turn:
                            _allow_next_turn[0] = True   # Turn 2 (правильні дати) проривається крізь suppress
                            logger.debug("[AGENT] get_available_slots(no date) → suppress Turn 1, дозволяємо Turn 2")
                        # reschedule_visit щойно заблоковано → AI зараз генерує питання підтвердження.
                        # Це питання мусить пробитись крізь suppress (інакше стає ghost → пацієнт не чує).
                        if not _draining and _greeting_done.is_set() and _reschedule_just_blocked[0]:
                            _reschedule_just_blocked[0] = False
                            _allow_next_turn[0] = True
                            logger.debug("[AGENT] reschedule blocked → allow next turn (питання підтвердження)")
                        if not _draining and _greeting_done.is_set() and turn_had_audio:
                            # Ехо-гейт після turn_complete: 1.0с буфер після останнього чанку.
                            # Під час стріму gate вже "ковзав" з кожним resp.data (0.3-1.0с).
                            # +1.0с від turn_complete → сумарно 1.3-2.0с захисту від ехо.
                            # Довший gate (forward-dating) вбиває barge-in → не використовуємо.
                            # Рідкісне ехо з гучномовника після gate → Gemini AEC + input_transcription
                            # guard (нижче) запобігають false confirm/cancel.
                            _interrupted_pause_until[0] = max(
                                _interrupted_pause_until[0],
                                time.time() + 1.0,
                            )
                            _schedule_silence_timer(audio_delay=0.0)
                            if _date_slots_fetched[0]:
                                _date_slots_fetched[0] = False
                                # Розрізняємо два сценарії після get_available_slots(date=...):
                                # Case A — ack-тур: Turn1="зараз перевірю" (без ранок/обід/вечір)
                                #   → Turn2 = реальні слоти, дозволяємо його через _allow_next_turn.
                                # Case B — повна відповідь: Turn1 вже містить ранок/обід/вечір
                                #   → подальший autonomous Turn2 = самоговоріння, suppress блокує.
                                _turn1_has_periods = any(
                                    w in _ai_text_lower for w in ('ранок', 'обід', 'вечір')
                                )
                                if not _turn1_has_periods:
                                    _allow_next_turn[0] = True  # ack-тур → Turn2 (реальні слоти) зіграє
                            # ЗАВЖДИ suppress — захист від самоговоріння (autonomous turns).
                            _last_ai_turn_end[0] = time.time()  # double-turn guard
                            _suppress_next_audio[0] = True
                            # Watchdog: відлік починається від кінця ВІДТВОРЕННЯ, не від генерації.
                            # Gemini надсилає аудіо за ~0-2с до того як пацієнт його дочув.
                            # Forward-date на тривалість аудіо → пацієнт отримує повний WATCHDOG_TIMEOUT_SEC.
                            _activity[0] = time.time() + (turn_audio_bytes / 48000)
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    logger.error(f"[AGENT] recv_task впав: {e!r}", exc_info=True)
                    raise

            send_task = asyncio.create_task(_send_patient_audio(), name="send_patient_audio")
            recv_task = asyncio.create_task(_recv_and_handle(), name="recv_and_handle")
            watchdog_task = asyncio.create_task(_watchdog(), name="watchdog")

            # Fallback prefetch для cold path (warmup не запускався → кеш порожній).
            # Warmup path: кеш вже заповнений із warmup() → задача не потрібна.
            if "__raw_slots" not in _call_cache:
                async def _prefetch_slots() -> None:
                    try:
                        from services import clinic_api as _capi
                        _raw = await _capi.get_doctor_slots(visit_context["doctor_name"])
                        if _raw:
                            _call_cache["__raw_slots"] = _raw
                            logger.debug(f"[AGENT] Cold prefetch: {len(_raw)} слотів кешовано")
                    except Exception as _e:
                        logger.debug(f"[AGENT] Cold prefetch failed (non-critical): {_e!r}")
                asyncio.create_task(_prefetch_slots(), name="prefetch_slots")

            if greeting_buffer:
                # Warmup шлях: граємо буфер з реал-тайм рейтом.
                # Без rate-limit всі chunks летять в Opus чергу миттєво →
                # черга (maxsize=300) переповнюється → кінець привітання дропається.
                # 24kHz 16-bit = 48000 bytes/sec → тривалість chunk = len/48000 с.
                _play_start = time.monotonic()
                _played_dur = 0.0
                for chunk in greeting_buffer:
                    await binotel_call.send_audio(chunk)
                    _played_dur += len(chunk) / 48000
                    _slack = (_play_start + _played_dur) - time.monotonic()
                    if _slack > 0.01:
                        await asyncio.sleep(_slack)
                # ── Suppress + echo gate після buffer playback ──────────────────────────
                # ПРОБЛЕМА: buffer грає напряму через binotel_call.send_audio(), обходячи
                # recv_task. recv_task ніколи не "бачив" цього аудіо-туру → ніколи не
                # виставляв _suppress_next_audio після turn_complete → Gemini генерує
                # автономний continuation turn (друге привітання) і він ГРАЄ, бо suppress=False.
                #
                # FIX 1 — suppress: завжди виставляємо після buffer, незалежно від primer.
                # Діє так само як recv_task виставляє після будь-якого реального AI-туру.
                _suppress_next_audio[0] = True
                _last_ai_turn_end[0] = time.time()   # double-turn guard baseline
                #
                # FIX 2 — echo gate: greeting щойно відіграло через SIP-динамік пацієнта.
                # Ехо повертається в SIP-мікрофон і потрапляє в send_task → Gemini чує
                # "своє" привітання як "аудіо пацієнта" → VAD спрацьовує → Gemini генерує
                # відповідь на ехо → ще один небажаний тур.
                # Gate = 1.5с (SIP jitter ~50мс + room echo decay ~800мс + запас 650мс).
                # Greeting грає в real-time (sleep у циклі) → по виходу залишок відтворення ≈ 0 →
                # потрібна тільки затримка затухання ехо кімнати.
                _interrupted_pause_until[0] = time.time() + 1.5
                # ────────────────────────────────────────────────────────────────────────
                # Primer — тільки для stale сесії (відновленої через handle):
                # нова сесія не має history привітання → треба повідомити Gemini.
                # turn_complete=False ЗАБОРОНЕНО: Gemini галюцинує в незавершеному стані.
                _needs_primer = _stale_warmup[0]
                if _needs_primer:
                    await session.send_client_content(
                        turns={"parts": [{"text": "Ти вже привіталась і нагадала пацієнту про його візит. Не повторюй привітання. Слухай що скаже пацієнт."}]},
                        turn_complete=True,
                    )
                    logger.info("[AGENT] Stale warmup: primer надіслано в відновлену сесію")
                else:
                    logger.info("[AGENT] Fresh warmup: suppress + echo gate встановлено")
                _greeting_done.set()
                from services.audio_bridge import enable_noise_profiling
                enable_noise_profiling()
                _activity[0] = time.time()  # watchdog рахує тільки після привітання
                logger.info("[AGENT] Стрімінг розпочато")
                # turn_complete вже відбувся під час warmup — запускаємо таймер одразу
                _schedule_silence_timer()
            else:
                # Cold шлях: тригеримо привітання в реальному часі
                await session.send_client_content(
                    turns={"parts": [{"text": "Починай розмову — коротко привітайся та нагадай про візит (максимум 2-3 речення)"}]},
                    turn_complete=True,
                )
                _greeting_done.set()
                from services.audio_bridge import enable_noise_profiling
                enable_noise_profiling()
                _activity[0] = time.time()  # watchdog рахує тільки після привітання
                logger.info("[AGENT] Стрімінг розпочато")
                # Таймер стартує після turn_complete в _recv_and_handle

            try:
                done, pending = await asyncio.wait(
                    [send_task, recv_task, watchdog_task], return_when=asyncio.FIRST_COMPLETED
                )
                for t in done:
                    if t.cancelled():
                        logger.info(f"[AGENT] '{t.get_name()}' скасовано")
                    elif t.exception():
                        logger.error(f"[AGENT] '{t.get_name()}' впав з помилкою: {t.exception()!r}",
                                     exc_info=t.exception())
                    else:
                        logger.info(f"[AGENT] '{t.get_name()}' завершено нормально")

                # Якщо end_call спрацював — recv_task ще грає прощання AI.
                # Чекаємо поки AI договорить (max 15с) перш ніж обривати дзвінок.
                if self._end_event.is_set() and not recv_task.done():
                    try:
                        await asyncio.wait_for(asyncio.shield(recv_task), timeout=15.0)
                    except asyncio.TimeoutError:
                        logger.warning("[AGENT] Timeout очікування прощання AI (15с)")
            finally:
                if _timer_task and not _timer_task.done():
                    _timer_task.cancel()
                if _silence_task and not _silence_task.done():
                    _silence_task.cancel()
                watchdog_task.cancel()
                send_task.cancel()
                recv_task.cancel()
                results = await asyncio.gather(send_task, recv_task, watchdog_task, return_exceptions=True)
                for name, r in zip(["send_task", "recv_task", "watchdog_task"], results):
                    if isinstance(r, Exception) and not isinstance(r, asyncio.CancelledError):
                        logger.error(f"[AGENT] {name} exception (finally): {r!r}")

        warmup_session = getattr(self, '_warmup_session', None)
        warmup_cm = getattr(self, '_warmup_cm', None)
        self._warmup_session = None
        self._warmup_cm = None

        # Зупиняємо monitor — пацієнт відповів, сесія зараз буде використана або закрита
        if self._monitor_task and not self._monitor_task.done():
            self._monitor_task.cancel()
        self._monitor_task = None

        # Session resumption: якщо monitor перепідключився через GoAway або idle close —
        # self._warmup_session вже вказує на свіжу сесію з відновленим контекстом.
        # Primer потрібен лише якщо відбувалось відновлення (сесія не та що генерувала привітання).
        if getattr(self, '_session_reconnected', False) and warmup_session and greeting_buffer:
            _stale_warmup[0] = True
            logger.info("[AGENT] Session була відновлена через handle → primer буде надіслано")

        try:
            if warmup_session and warmup_cm:
                # Варіант А: реюзаємо warmup сесію — вона вже привіталась, cold start не потрібен
                try:
                    await _stream(warmup_session)
                finally:
                    try:
                        await asyncio.wait_for(
                            warmup_cm.__aexit__(None, None, None), timeout=3.0
                        )
                    except (asyncio.TimeoutError, Exception):
                        pass
            else:
                # Cold шлях: warmup не вдався або не запускався
                _cm = self.client.aio.live.connect(
                    model=GEMINI_MODEL,
                    config=self._build_config(),
                )
                session = await _cm.__aenter__()
                try:
                    await _stream(session)
                finally:
                    try:
                        await asyncio.wait_for(
                            _cm.__aexit__(None, None, None), timeout=3.0
                        )
                    except (asyncio.TimeoutError, Exception):
                        pass

        except Exception as e:
            logger.error(f"[AGENT] Помилка в production режимі: {e}", exc_info=True)

        self.call_duration = time.time() - start
        result = self.call_result or "no_result"
        logger.info(f"[AGENT] Завершено. Тривалість: {self.call_duration:.0f}с. Результат: {result}")
        if LOG_FILE:
            from services import call_logger
            call_logger.end_call(result, self.call_duration)
        return result

    # ------------------------------------------------------------------
    # Warmup: підігрів Gemini поки пацієнт ще чує гудки
    # ------------------------------------------------------------------

    async def warmup(self) -> None:
        """
        Підключитись до Gemini і згенерувати перше привітання заздалегідь.

        Викликається ПАРАЛЕЛЬНО з binotel.initiate_call() поки пацієнт
        чує гудки (10-30 секунд). Буферизує greeting chunks у self._greeting_buffer.
        Коли пацієнт відповість — run_production() одразу відіграє готовий буфер.
        """
        self._greeting_buffer: list[bytes] = []
        self._greeting_duration: float = 0.0  # тривалість привітання в секундах
        self._prefetched_raw_slots: list | None = None
        self._resumption_handle = None
        self._session_reconnected = False
        _warmup_cm = None

        # Prefetch слотів паралельно з генерацією привітання.
        # Warmup стартує під час гудків (10-30с) — це максимально можливо рано.
        # Результат зберігається в self._prefetched_raw_slots і переноситься в
        # _call_cache на початку _stream() → перший get_available_slots завжди миттєвий.
        async def _warmup_prefetch() -> None:
            try:
                from services import clinic_api as _capi
                _raw = await _capi.get_doctor_slots(self.visit.get("doctor_name", ""))
                if _raw:
                    self._prefetched_raw_slots = _raw
                    logger.debug(f"[AGENT] Warmup prefetch: {len(_raw)} слотів кешовано")
            except Exception as _e:
                logger.debug(f"[AGENT] Warmup prefetch failed (non-critical): {_e!r}")

        prefetch_task = asyncio.create_task(_warmup_prefetch(), name="warmup_prefetch_slots")

        def _start_monitor(initial_session, initial_cm) -> asyncio.Task:
            """
            Запускає фоновий монітор warmup-сесії.

            Замінює silence keepalive: більше не надсилаємо аудіо-тишу щоб уникнути
            ghost-ів та передчасних end_call від Gemini.

            Логіка:
            - Читає повідомлення від Gemini (session_resumption_update, go_away).
            - session_resumption_update → зберігає новий handle.
            - go_away (Gemini закривається через ~60с) → відновлює сесію через handle.
            - Помилка з'єднання → відновлює сесію через handle (якщо є).
            - Без handle → warmup_session = None (cold path у run_production).
            """
            async def _monitor_loop() -> None:
                current_session = initial_session
                current_cm = initial_cm

                while True:
                    session_dead = False
                    try:
                        while True:
                            try:
                                msg = await asyncio.wait_for(
                                    current_session._receive(), timeout=30.0
                                )
                            except asyncio.TimeoutError:
                                # Тиша від сервера — нормально між турами. Продовжуємо.
                                continue
                            if (msg.session_resumption_update
                                    and msg.session_resumption_update.new_handle):
                                self._resumption_handle = msg.session_resumption_update.new_handle
                                logger.debug("[AGENT] Monitor: resumption handle оновлено")
                            if msg.go_away:
                                logger.warning(
                                    f"[AGENT] Monitor: GoAway ({msg.go_away.time_left})"
                                    " — відновлюємо сесію..."
                                )
                                session_dead = True
                                break
                    except asyncio.CancelledError:
                        raise
                    except Exception as _e:
                        _name = type(_e).__name__
                        # ConnectionClosedOK → штатне закриття після дзвінка, не логуємо як помилку
                        if "ConnectionClosedOK" not in _name and getattr(_e, "code", None) != 1000:
                            logger.info(f"[AGENT] Monitor: сесія закрита ({_name}) — відновлення...")
                        session_dead = True

                    if not session_dead:
                        break  # CancelledError або штатне завершення без GoAway

                    if not self._resumption_handle:
                        logger.warning("[AGENT] Monitor: немає handle → cold path")
                        self._warmup_session = None
                        self._warmup_cm = None
                        break

                    # Відновлюємо сесію через handle — контекст (привітання) збережено
                    try:
                        try:
                            await asyncio.wait_for(
                                current_cm.__aexit__(None, None, None), timeout=1.0
                            )
                        except Exception:
                            pass

                        new_cm = self.client.aio.live.connect(
                            model=GEMINI_MODEL,
                            config=self._build_config(
                                resume_handle=self._resumption_handle
                            ),
                        )
                        new_session = await new_cm.__aenter__()
                        current_session = new_session
                        current_cm = new_cm
                        self._warmup_session = new_session
                        self._warmup_cm = new_cm
                        self._warmup_time = time.time()
                        self._session_reconnected = True
                        logger.info("[AGENT] Monitor: warmup сесію відновлено через handle")
                        # Продовжуємо зовнішній while True з новою сесією
                    except asyncio.CancelledError:
                        raise
                    except Exception as _reconnect_err:
                        logger.error(
                            f"[AGENT] Monitor: відновлення провалилось: {_reconnect_err!r}"
                            " → cold path"
                        )
                        self._warmup_session = None
                        self._warmup_cm = None
                        break

            return asyncio.create_task(_monitor_loop(), name="warmup_monitor")

        async def _generate_greeting(session) -> None:
            """Генерує привітання і буферизує аудіо. Захоплює handle якщо прийшов під час туру."""
            await session.send_client_content(
                turns={"parts": [{"text": "Починай розмову — коротко привітайся та нагадай про візит (максимум 2-3 речення)"}]},
                turn_complete=True,
            )
            async for resp in session.receive():
                if resp.data:
                    self._greeting_buffer.append(resp.data)
                    self._greeting_duration += len(resp.data) / 48000
                if (resp.session_resumption_update
                        and resp.session_resumption_update.new_handle):
                    self._resumption_handle = resp.session_resumption_update.new_handle
                if resp.server_content and resp.server_content.turn_complete:
                    break

        try:
            _warmup_cm = self.client.aio.live.connect(
                model=GEMINI_MODEL,
                config=self._build_config(),
            )
            warmup_session = await _warmup_cm.__aenter__()
            await _generate_greeting(warmup_session)

            # Чекаємо завершення prefetch якщо ще не закінчився (greeting зазвичай довше)
            if not prefetch_task.done():
                try:
                    await asyncio.wait_for(asyncio.shield(prefetch_task), timeout=2.0)
                except asyncio.TimeoutError:
                    logger.debug("[AGENT] Warmup prefetch ще не завершився — _stream() підхопить")

            logger.info(
                f"[AGENT] Warmup готовий: {len(self._greeting_buffer)} chunks буферизовано"
                f" ({self._greeting_duration:.1f}с)"
                + (f", {len(self._prefetched_raw_slots)} слотів prefetch" if self._prefetched_raw_slots else "")
            )
            self._warmup_cm = _warmup_cm
            self._warmup_session = warmup_session
            self._warmup_time = time.time()
            _warmup_cm = None  # не закривати в finally

            self._monitor_task = _start_monitor(warmup_session, self._warmup_cm)
        except Exception as e:
            logger.warning(f"[AGENT] Warmup помилка: {e} — retry через 1с...")
            self._greeting_buffer = []
            self._warmup_session = None
            self._warmup_cm = None
            if _warmup_cm is not None:
                try:
                    await _warmup_cm.__aexit__(None, None, None)
                except Exception:
                    pass
                _warmup_cm = None
            # Retry — у нас є ще ~10-20с поки пацієнт чує гудки
            await asyncio.sleep(1)
            try:
                _warmup_cm = self.client.aio.live.connect(
                    model=GEMINI_MODEL,
                    config=self._build_config(),
                )
                warmup_session = await _warmup_cm.__aenter__()
                await _generate_greeting(warmup_session)
                self._warmup_cm = _warmup_cm
                self._warmup_session = warmup_session
                self._warmup_time = time.time()
                _warmup_cm = None
                logger.info(f"[AGENT] Warmup retry успішний: {len(self._greeting_buffer)} chunks ({self._greeting_duration:.1f}с)")
                self._monitor_task = _start_monitor(warmup_session, self._warmup_cm)
            except Exception as e2:
                logger.warning(f"[AGENT] Warmup retry також провалився: {e2} — cold path")
                self._greeting_buffer = []
                self._warmup_session = None
                self._warmup_cm = None
        finally:
            if _warmup_cm is not None:  # закриваємо тільки якщо не збережено
                try:
                    await _warmup_cm.__aexit__(None, None, None)
                except Exception:
                    pass
            if not prefetch_task.done():
                prefetch_task.cancel()

    # ------------------------------------------------------------------
    # Публічний інтерфейс
    # ------------------------------------------------------------------

    async def run(self, binotel_call=None) -> str:
        """
        Запустити агента.

        Args:
            binotel_call: BinotelCall об'єкт (production) або None (debug)

        Returns:
            Результат розмови: confirmed / rescheduled / cancelled / no_result / transferred
        """
        if DEBUG:
            return await self.run_debug()
        return await self.run_production(binotel_call)

    def cleanup(self) -> None:
        """Звільнити PyAudio ресурси."""
        if self._pa is not None:
            self._pa.terminate()
