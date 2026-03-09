"""
Агент Блоку 1: Нагадування про візити.

DEBUG mode   : мікрофон → Gemini Live → динамік  (ти = пацієнт)
PRODUCTION   : Binotel SIP аудіо ↔ Gemini Live
"""

import asyncio
import logging
import math
import struct
import time

try:
    import webrtcvad as _webrtcvad
    _VAD = _webrtcvad.Vad(2)   # aggressiveness 0-3: 2 = збалансований
    _VAD_AVAILABLE = True
except ImportError:
    _VAD = None
    _VAD_AVAILABLE = False

import pyaudio
from google import genai
from google.genai import types

from agents.block1.prompts import build_system_prompt
from agents.block1.tools import TOOLS, handle_tool_call
from config import GEMINI_MODEL, DEBUG, VERTEX_PROJECT, VERTEX_LOCATION, SILENCE_TIMEOUT_SEC, SILENCE_START_DELAY_SEC, WATCHDOG_TIMEOUT_SEC, INTERRUPTED_PAUSE_SEC, GEMINI_LOGS

logger = logging.getLogger(__name__)

# Аудіо константи
FORMAT = pyaudio.paInt16
CHANNELS = 1
INPUT_RATE = 16000   # мікрофон / SIP → Gemini
OUTPUT_RATE = 24000  # Gemini → динамік / SIP
CHUNK = 512

# Silence detection
VAD_THRESHOLD = 300      # RMS поріг для 16-bit PCM (fallback якщо webrtcvad недоступний)
MAX_SILENCE_COUNT = 3    # після 3 разів — вішаємо трубку
_VAD_FRAME_BYTES = 320   # 10мс @ 16kHz 16-bit mono = 160 семплів × 2 байти


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

    def __init__(self, pa: pyaudio.PyAudio) -> None:
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
        self._pa = pyaudio.PyAudio()
        self._end_event = asyncio.Event()

    # ------------------------------------------------------------------
    # Конфіг сесії
    # ------------------------------------------------------------------

    def _build_config(self) -> types.LiveConnectConfig:
        prompt = build_system_prompt(self.visit)
        return types.LiveConnectConfig(
            response_modalities=["AUDIO"],
            speech_config=types.SpeechConfig(
                voice_config=types.VoiceConfig(
                    prebuilt_voice_config=types.PrebuiltVoiceConfig(
                        voice_name="Leda"
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
            # VAD на стороні Gemini: повільніше реагує на кінець мови → менше обрізань пацієнта.
            # end_of_speech_sensitivity=LOW → довша пауза перш ніж Gemini вважає що пацієнт замовк.
            realtime_input_config=types.RealtimeInputConfig(
                automatic_activity_detection=types.AutomaticActivityDetection(
                    start_of_speech_sensitivity=types.StartSensitivity.START_SENSITIVITY_HIGH,
                    end_of_speech_sensitivity=types.EndSensitivity.END_SENSITIVITY_LOW,
                    silence_duration_ms=800,
                )
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

        start = time.time()

        greeting_buffer = getattr(self, "_greeting_buffer", [])
        greeting_duration = getattr(self, "_greeting_duration", 0.0)

        async def _stream(session) -> None:
            """Двосторонній стрімінг аудіо між Binotel SIP та Gemini."""

            _greeting_done = asyncio.Event()
            _patient_spoke = asyncio.Event()   # скидається на початку кожного silence-чеку
            _silence_count = 0
            _silence_task: asyncio.Task | None = None   # _check_silence task
            _timer_task: asyncio.Task | None = None     # _start_silence_timer task
            _call_cache: dict = {}  # захист від дублікатів tool calls
            _activity = [time.time()]  # [0] = час останньої активності (для watchdog)
            _interrupted_pause_until = [0.0]  # до цього часу ігноруємо аудіо пацієнта (ехо-гейт)
            # Фільтр дублікатних турів: Gemini Live іноді генерує 2 тури на одну подію.
            # Після кожного AI туру піднімаємо прапор — наступний тур не відтворюємо в SIP.
            # Прапор знімається ТІЛЬКИ коли patient_said приносить НОВИЙ текст пацієнта.
            # Gemini викликає patient_said перед кожним туром → дедуплікація по тексту запобігає
            # тому що ghost тури скидають suppress повторним викликом того самого тексту.
            _suppress_next_audio = [False]
            _last_patient_said_text = [""]  # текст останнього НОВОГО patient_said

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
                    if _silence_count >= MAX_SILENCE_COUNT:
                        logger.info("[AGENT] Тиша 3 рази — завершуємо дзвінок")
                        self.call_result = "no_answer"
                        self._end_event.set()
                        _suppress_next_audio[0] = False
                        await session.send_client_content(
                            turns={"parts": [{"text": (
                                "[ІНСТРУКЦІЯ ДЛЯ АГЕНТА, НЕ СЛОВА ПАЦІЄНТА]: "
                                "Пацієнт не відповідає вже третій раз. "
                                "Скажи коротке ввічливе прощання і одразу виклич end_call(result='no_answer')."
                            )}]},
                            turn_complete=True,
                        )
                    elif _silence_count == 1:
                        _suppress_next_audio[0] = False
                        await session.send_client_content(
                            turns={"parts": [{"text": (
                                "[ІНСТРУКЦІЯ ДЛЯ АГЕНТА, НЕ СЛОВА ПАЦІЄНТА]: "
                                "Пацієнт мовчить. Запитай тепло і невимушено: 'Алло, чуєте мене?'"
                            )}]},
                            turn_complete=True,
                        )
                    else:
                        _suppress_next_audio[0] = False
                        await session.send_client_content(
                            turns={"parts": [{"text": (
                                "[ІНСТРУКЦІЯ ДЛЯ АГЕНТА, НЕ СЛОВА ПАЦІЄНТА]: "
                                "Пацієнт знову мовчить. Скажи: 'Я все ще чекаю вашої відповіді.'"
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

            async def _watchdog() -> None:
                """Фоновий watchdog: якщо жодної активності за WATCHDOG_TIMEOUT_SEC — re-inject або hangup."""
                _injected = 0
                while not self._end_event.is_set():
                    await asyncio.sleep(5)
                    if self._end_event.is_set():
                        break
                    idle = time.time() - _activity[0]
                    if idle < WATCHDOG_TIMEOUT_SEC:
                        continue
                    _activity[0] = time.time()  # скидаємо щоб не спрацював двічі підряд
                    _injected += 1
                    if _injected >= 2:
                        logger.warning(f"[AGENT] Watchdog: {idle:.0f}с бездіяльності — завершуємо дзвінок")
                        self.call_result = "no_answer"
                        self._end_event.set()
                        return
                    logger.warning(f"[AGENT] Watchdog: {idle:.0f}с бездіяльності — re-inject prompt")
                    try:
                        _suppress_next_audio[0] = False
                        await session.send_client_content(
                            turns={"parts": [{"text": "[ІНСТРУКЦІЯ ДЛЯ АГЕНТА, НЕ СЛОВА ПАЦІЄНТА]: Пацієнт мовчить. Скажи лише: 'Алло, ви мене чуєте?' — і мовчки чекай відповіді. Не роби жодних дій до відповіді пацієнта."}]},
                            turn_complete=True,
                        )
                    except Exception as e:
                        logger.error(f"[AGENT] Watchdog помилка send_client_content: {e!r}")
                        self._end_event.set()
                        return

            async def _send_patient_audio() -> None:
                _vad_buf = bytearray()  # буфер webrtcvad (потребує фіксовані 10мс = 320 байт)
                async for chunk in binotel_call.receive_audio():
                    if self._end_event.is_set():
                        break
                    if not _greeting_done.is_set():
                        continue
                    # Ехо-гейт: після interrupted ігноруємо аудіо поки не розсіється відлуння
                    if time.time() < _interrupted_pause_until[0]:
                        continue
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
                    if _has_voice:
                        _patient_spoke.set()
                        _activity[0] = time.time()
                        # НЕ скидаємо _suppress_next_audio тут — тільки patient_said tool call скидає.
                        # Інакше VAD скидає suppress раніше ніж приходить patient_said,
                        # і ghost Turn 2 вважається "реальним" туром.
                    await session.send_realtime_input(
                        audio={"data": chunk, "mime_type": "audio/pcm;rate=16000"}
                    )

            async def _recv_and_handle() -> None:
                nonlocal _silence_count
                _draining = False       # True — чекаємо поки AI договорить перед завершенням
                _pending_transfer = None  # str reason → transfer після фрази AI; None → end_call
                _ai_buf: list[str] = []        # накопичуємо транскрипцію AI за тур
                try:
                    # session.receive() — пер-тернова: виходить після кожного turn_complete.
                    # while True перезапускає для наступного turn.
                    while True:
                        turn_had_content = False
                        turn_had_audio = False  # True якщо в цьому турі AI говорила
                        turn_audio_bytes = 0    # байти аудіо в цьому турі → для розрахунку затримки
                        _had_patient_said = False  # True якщо в поточному inner-loop був patient_said
                        _ai_buf.clear()
                        async for resp in session.receive():
                            turn_had_content = True
                            # Якщо пацієнт перебив AI — зупиняємо аудіо, слухаємо пацієнта
                            if resp.server_content and resp.server_content.interrupted:
                                logger.info("[AGENT] AI перервано — пацієнт говорить")
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
                                if sc.turn_complete:
                                    if _ai_buf and GEMINI_LOGS:
                                        _is_ghost_log = _suppress_next_audio[0] and not _had_patient_said
                                        _tag = "[AI GHOST]" if _is_ghost_log else "[AI]"
                                        logger.info(f"{_tag} {''.join(_ai_buf)}")
                                    _ai_buf.clear()
                                    if _draining:
                                        # Чекаємо поки AI договорить перед завершенням/передачею
                                        if turn_had_audio:
                                            if _pending_transfer is not None:
                                                logger.info("[AGENT] AI завершив фразу — передаємо оператору")
                                                await binotel_call.transfer_to_operator(_pending_transfer)
                                            else:
                                                logger.info("[AGENT] AI завершив прощання — чекаємо 2с...")
                                                await asyncio.sleep(2.0)
                                            return
                                        continue
                                    # Реальний тур: suppress вимкнений АБО в цьому inner-loop був patient_said.
                                    # Ghost/дублікатний тур: suppress=True і patient_said не було.
                                    if (not _suppress_next_audio[0]) or _had_patient_said:
                                        break
                                    # Ghost тур — НЕ виходимо з inner loop, чекаємо реальної відповіді пацієнта.
                                    logger.debug("[AGENT] Ghost тур — продовжуємо читати сесію")
                                    if turn_had_audio and _greeting_done.is_set():
                                        _interrupted_pause_until[0] = time.time() + INTERRUPTED_PAUSE_SEC
                                        _schedule_silence_timer(audio_delay=0.0)
                                        _suppress_next_audio[0] = True
                                    turn_had_audio = False
                                    turn_audio_bytes = 0
                                    _had_patient_said = False
                            if resp.data and _greeting_done.is_set():
                                if not _suppress_next_audio[0]:
                                    await binotel_call.send_audio(resp.data)
                                    # Гейт ТІЛЬКИ коли аудіо реально грає через SIP.
                                    # Оновлюємо щоразу — gate "ковзає" разом з мовою AI.
                                    # Ghost тури не грають → луни немає → гейт не потрібен.
                                    _interrupted_pause_until[0] = time.time() + INTERRUPTED_PAUSE_SEC
                                turn_had_audio = True
                                turn_audio_bytes += len(resp.data)
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
                                # Знімаємо suppress ТІЛЬКИ якщо patient_said приносить НОВИЙ текст.
                                # Gemini викликає patient_said перед кожним туром який генерує —
                                # ghost тури передають той самий текст → дедупліація захищає suppress.
                                for fc in resp.tool_call.function_calls:
                                    if fc.name == "patient_said":
                                        _ps_text = (dict(fc.args) if fc.args else {}).get("text", "")
                                        _is_injection = "[ІНСТРУКЦІЯ" in _ps_text or "Пацієнт мовчить" in _ps_text
                                        _is_new = _ps_text and _ps_text != _last_patient_said_text[0]
                                        if _is_new and not _is_injection:
                                            _suppress_next_audio[0] = False
                                            _had_patient_said = True
                                            _last_patient_said_text[0] = _ps_text
                                        break
                                _interrupted_pause_until[0] = time.time() + 10.0
                                _patient_spoke.set()
                                _activity[0] = time.time()
                                _schedule_silence_timer(audio_delay=0.0)
                                # Збираємо всі відповіді в батч — надсилаємо одним send_tool_response,
                                # щоб Gemini не генерував проміжні turn-и між tool calls.
                                _batch: list[types.FunctionResponse] = []
                                _after_batch = None  # ("end_call"|"transfer", ...) — виконати після відправки
                                _slots_fetched = False  # True якщо get_available_slots успішно виконано в цьому батчі
                                for fc in resp.tool_call.function_calls:
                                    args = dict(fc.args) if fc.args else {}
                                    if fc.name == "transfer_to_operator":
                                        reason = args.get("reason", "")
                                        logger.info(f"[AGENT] transfer_to_operator: {reason}")
                                        self.call_result = "transferred"
                                        self._end_event.set()
                                        _batch.append(types.FunctionResponse(
                                            id=fc.id, name=fc.name,
                                            response={"success": True, "message": "Передаємо на оператора"},
                                        ))
                                        _after_batch = ("transfer", reason)
                                        break
                                    # end_call в одному батчі з get_available_slots = галюцинація:
                                    # Gemini не бачив слоти, але вже вирішив завершити дзвінок.
                                    # Ігноруємо — надсилаємо відповідь Gemini, але дзвінок не закриваємо.
                                    if fc.name == "end_call" and _slots_fetched:
                                        logger.warning("[AGENT] end_call проігноровано — викликано в одному батчі з get_available_slots (галюцинація)")
                                        _batch.append(types.FunctionResponse(
                                            id=fc.id, name=fc.name, response={"success": True}
                                        ))
                                        continue
                                    try:
                                        result = await handle_tool_call(fc.name, args, self.visit, _call_cache)
                                    except Exception as tool_err:
                                        logger.error(f"[AGENT] Tool {fc.name} помилка: {tool_err!r}")
                                        result = {"success": True}
                                    _batch.append(types.FunctionResponse(id=fc.id, name=fc.name, response=result))
                                    if fc.name == "get_available_slots" and result.get("success"):
                                        _slots_fetched = True
                                    if fc.name == "end_call":
                                        self.call_result = result.get("result", "no_result")
                                        logger.info(f"[AGENT] end_call → {self.call_result}")
                                        self._end_event.set()
                                        _after_batch = ("end_call",)
                                        break
                                if _batch:
                                    await session.send_tool_response(function_responses=_batch)
                                if _after_batch:
                                    if _after_batch[0] == "transfer":
                                        _pending_transfer = _after_batch[1]
                                    _draining = True
                        # session.receive() вийшов (turn_complete або сесія закрита)
                        if not turn_had_content:
                            logger.warning("[AGENT] recv_task: session.receive() без контенту — сесія закрита")
                            return
                        logger.debug("[AGENT] recv_task: turn завершено, перезапускаємо session.receive()")
                        # Запускаємо таймер тиші тільки якщо AI реально говорила в цьому турі.
                        # Якщо тур був лише tool_call (без аудіо) — таймер не запускаємо,
                        # щоб він не тік поки AI формує та озвучує відповідь у наступному турі.
                        if not _draining and _greeting_done.is_set() and turn_had_audio:
                            # Ехо-гейт: AI вже замовкла — скорочуємо гейт до 300мс (луна розсіялась).
                            # Під час мови AI гейт вже тримався per-chunk (INTERRUPTED_PAUSE_SEC).
                            _interrupted_pause_until[0] = time.time() + 0.3
                            _schedule_silence_timer(audio_delay=0.0)
                            # Піднімаємо фільтр: якщо Gemini згенерує ще один тур без відповіді
                            # пацієнта — це дублікат, не відтворювати в SIP.
                            _suppress_next_audio[0] = True
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    logger.error(f"[AGENT] recv_task впав: {e!r}", exc_info=True)
                    raise

            send_task = asyncio.create_task(_send_patient_audio(), name="send_patient_audio")
            recv_task = asyncio.create_task(_recv_and_handle(), name="recv_and_handle")
            watchdog_task = asyncio.create_task(_watchdog(), name="watchdog")

            if greeting_buffer:
                # Warmup шлях: та сама сесія вже привіталась — граємо буфер і чекаємо
                for chunk in greeting_buffer:
                    await binotel_call.send_audio(chunk)
                if greeting_duration > 0:
                    await asyncio.sleep(greeting_duration)
                _greeting_done.set()
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
        _warmup_cm = None
        try:
            _warmup_cm = self.client.aio.live.connect(
                model=GEMINI_MODEL,
                config=self._build_config(),
            )
            warmup_session = await _warmup_cm.__aenter__()
            await warmup_session.send_client_content(
                turns={"parts": [{"text": "Починай розмову — коротко привітайся та нагадай про візит (максимум 2-3 речення)"}]},
                turn_complete=True,
            )
            async for resp in warmup_session.receive():
                if resp.data:
                    self._greeting_buffer.append(resp.data)
                    # 24kHz 16-bit = 48000 bytes/sec → тривалість цього chunk
                    self._greeting_duration += len(resp.data) / 48000
                if resp.server_content and resp.server_content.turn_complete:
                    break
            logger.info(
                f"[AGENT] Warmup готовий: {len(self._greeting_buffer)} chunks буферизовано"
                f" ({self._greeting_duration:.1f}с)"
            )
            # Зберігаємо сесію живою — run_production() реюзає її (Варіант А)
            self._warmup_cm = _warmup_cm
            self._warmup_session = warmup_session
            _warmup_cm = None  # не закривати в finally
        except Exception as e:
            logger.warning(f"[AGENT] Warmup помилка: {e} — запустимо звичайний шлях")
            self._greeting_buffer = []
            self._warmup_session = None
            self._warmup_cm = None
        finally:
            if _warmup_cm is not None:  # закриваємо тільки якщо не збережено
                try:
                    await _warmup_cm.__aexit__(None, None, None)
                except Exception:
                    pass

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
        self._pa.terminate()
