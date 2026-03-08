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
VAD_THRESHOLD = 300      # RMS поріг для 16-bit PCM (підбирати під лінію SIP)
MAX_SILENCE_COUNT = 3    # після 3 разів — вішаємо трубку


def _is_voice(pcm: bytes) -> bool:
    """Проста VAD: True якщо RMS енергія чанку перевищує поріг."""
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
            # Прапор знімається коли пацієнт заговорив, tool call, або система інжектує текст.
            _suppress_next_audio = [False]

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
                async for chunk in binotel_call.receive_audio():
                    if self._end_event.is_set():
                        break
                    if not _greeting_done.is_set():
                        continue
                    # Ехо-гейт: після interrupted ігноруємо аудіо поки не розсіється відлуння
                    if time.time() < _interrupted_pause_until[0]:
                        continue
                    if _is_voice(chunk):
                        _patient_spoke.set()
                        _activity[0] = time.time()
                        _suppress_next_audio[0] = False
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
                        _ai_buf.clear()
                        async for resp in session.receive():
                            turn_had_content = True
                            # Якщо пацієнт перебив AI — зупиняємо аудіо, слухаємо пацієнта
                            if resp.server_content and resp.server_content.interrupted:
                                logger.info("[AGENT] AI перервано — пацієнт говорить")
                                _interrupted_pause_until[0] = time.time() + INTERRUPTED_PAUSE_SEC
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
                                        logger.info(f"[AI] {''.join(_ai_buf)}")
                                    if not _draining:
                                        break  # явний вихід — suppress піднімається ДО наступного туру
                            if resp.data and _greeting_done.is_set():
                                if not _suppress_next_audio[0]:
                                    await binotel_call.send_audio(resp.data)
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
                                # Чекаємо поки AI договорить фразу (turn_complete)
                                if resp.server_content and resp.server_content.turn_complete:
                                    if _pending_transfer is not None:
                                        logger.info(f"[AGENT] AI завершив фразу — передаємо оператору")
                                        await binotel_call.transfer_to_operator(_pending_transfer)
                                    else:
                                        logger.info("[AGENT] AI завершив прощання — чекаємо 2с...")
                                        await asyncio.sleep(2.0)
                                    return
                                continue
                            if resp.tool_call:
                                # Знімаємо suppress ТІЛЬКИ для patient_said — це реальна мова пацієнта.
                                # get_available_slots та інші — AI-ініційовані, suppress не знімаємо.
                                if any(fc.name == "patient_said" for fc in resp.tool_call.function_calls):
                                    _suppress_next_audio[0] = False
                                _interrupted_pause_until[0] = time.time() + 10.0
                                _patient_spoke.set()
                                _activity[0] = time.time()
                                _schedule_silence_timer(audio_delay=0.0)
                                # Збираємо всі відповіді в батч — надсилаємо одним send_tool_response,
                                # щоб Gemini не генерував проміжні turn-и між tool calls.
                                _batch: list[types.FunctionResponse] = []
                                _after_batch = None  # ("end_call"|"transfer", ...) — виконати після відправки
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
                                    try:
                                        result = await handle_tool_call(fc.name, args, self.visit, _call_cache)
                                    except Exception as tool_err:
                                        logger.error(f"[AGENT] Tool {fc.name} помилка: {tool_err!r}")
                                        result = {"success": True}
                                    _batch.append(types.FunctionResponse(id=fc.id, name=fc.name, response=result))
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
                            # Ехо-гейт: після завершення мови AI блокуємо SIP-ехо власного голосу.
                            _interrupted_pause_until[0] = time.time() + INTERRUPTED_PAUSE_SEC
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
