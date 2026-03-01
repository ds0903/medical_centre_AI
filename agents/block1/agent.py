"""
Агент Блоку 1: Нагадування про візити.

DEBUG mode   : мікрофон → Gemini Live → динамік  (ти = пацієнт)
PRODUCTION   : Binotel SIP аудіо ↔ Gemini Live
"""

import asyncio
import logging
import time

import pyaudio
from google import genai
from google.genai import types

from agents.block1.prompts import build_system_prompt
from agents.block1.tools import TOOLS, handle_tool_call
from config import GEMINI_API_KEY, GEMINI_MODEL, DEBUG

logger = logging.getLogger(__name__)

# Аудіо константи
FORMAT = pyaudio.paInt16
CHANNELS = 1
INPUT_RATE = 16000   # мікрофон / SIP → Gemini
OUTPUT_RATE = 24000  # Gemini → динамік / SIP
CHUNK = 512


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
        self.client = genai.Client(api_key=GEMINI_API_KEY)
        self.call_result: str | None = None
        self.call_duration: float = 0.0
        self._pa = pyaudio.PyAudio()
        self._end_event = asyncio.Event()

    # ------------------------------------------------------------------
    # Конфіг сесії
    # ------------------------------------------------------------------

    def _build_config(self) -> types.LiveConnectConfig:
        return types.LiveConnectConfig(
            response_modalities=["AUDIO"],
            speech_config=types.SpeechConfig(
                voice_config=types.VoiceConfig(
                    prebuilt_voice_config=types.PrebuiltVoiceConfig(
                        voice_name="Puck"
                    )
                )
            ),
            system_instruction=types.Content(
                parts=[types.Part(text=build_system_prompt(self.visit))]
            ),
            tools=TOOLS,
            # Вимикаємо "thinking" — для real-time голосу паузи на "обдумування"
            # неприйнятні: пацієнт чує мовчання, потім весь текст відразу (розмазаний звук).
            thinking_config=types.ThinkingConfig(thinking_budget=0),
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

            # Прапор: greeting ще грає — не відправляємо аудіо Gemini в SIP
            _greeting_done = asyncio.Event()

            async def _send_patient_audio() -> None:
                async for chunk in binotel_call.receive_audio():
                    if self._end_event.is_set():
                        break
                    await session.send_realtime_input(
                        audio={"data": chunk, "mime_type": "audio/pcm;rate=16000"}
                    )

            async def _recv_and_handle() -> None:
                _draining = False  # True після end_call — граємо прощання, не обриваємо
                try:
                    # session.receive() — пер-тернова: виходить після кожного turn_complete.
                    # while True перезапускає для наступного turn.
                    while True:
                        turn_had_content = False
                        async for resp in session.receive():
                            turn_had_content = True
                            # Якщо пацієнт перебив AI — зупиняємо аудіо, слухаємо пацієнта
                            if resp.server_content and resp.server_content.interrupted:
                                logger.info("[AGENT] AI перервано — пацієнт говорить")
                                continue
                            # Поки greeting ще грає — читаємо сесію (щоб буфер не переповнився)
                            # але НЕ відправляємо аудіо Gemini в SIP (щоб не перекривати greeting)
                            if resp.data and _greeting_done.is_set():
                                await binotel_call.send_audio(resp.data)
                            if _draining:
                                # Чекаємо поки AI договорить прощання (turn_complete)
                                if resp.server_content and resp.server_content.turn_complete:
                                    logger.info("[AGENT] AI завершив прощання — чекаємо 3с...")
                                    await asyncio.sleep(3.0)
                                    return
                                continue
                            if resp.tool_call:
                                for fc in resp.tool_call.function_calls:
                                    args = dict(fc.args) if fc.args else {}
                                    if fc.name == "transfer_to_operator":
                                        reason = args.get("reason", "")
                                        logger.info(f"[AGENT] transfer_to_operator: {reason}")
                                        await binotel_call.transfer_to_operator(reason)
                                        self.call_result = "transferred"
                                        self._end_event.set()
                                        await session.send_tool_response(function_responses=[
                                            types.FunctionResponse(id=fc.id, name=fc.name,
                                                response={"success": True, "message": "Передаємо на оператора"})
                                        ])
                                        return
                                    try:
                                        result = await handle_tool_call(fc.name, args, self.visit)
                                    except Exception as tool_err:
                                        logger.error(f"[AGENT] Tool {fc.name} помилка: {tool_err!r}")
                                        result = {"success": False, "error": str(tool_err), "result": "no_result"}
                                    if fc.name == "end_call":
                                        self.call_result = result.get("result", "no_result")
                                        logger.info(f"[AGENT] end_call → {self.call_result}")
                                        self._end_event.set()
                                        await session.send_tool_response(function_responses=[
                                            types.FunctionResponse(id=fc.id, name=fc.name, response=result)
                                        ])
                                        _draining = True  # Не return — слухаємо прощання AI
                                        break
                                    await session.send_tool_response(function_responses=[
                                        types.FunctionResponse(id=fc.id, name=fc.name, response=result)
                                    ])
                        # session.receive() вийшов (turn_complete або сесія закрита)
                        if not turn_had_content:
                            logger.warning("[AGENT] recv_task: session.receive() без контенту — сесія закрита")
                            return
                        logger.debug("[AGENT] recv_task: turn завершено, перезапускаємо session.receive()")
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    logger.error(f"[AGENT] recv_task впав: {e!r}", exc_info=True)
                    raise

            # Запускаємо ОБИДВА task-и одразу — щоб session.receive() читався
            # і буфер Gemini не переповнився під час відтворення привітання
            send_task = asyncio.create_task(_send_patient_audio(), name="send_patient_audio")
            recv_task = asyncio.create_task(_recv_and_handle(), name="recv_and_handle")

            if greeting_buffer:
                # Warmup шлях: відтворюємо буферизоване привітання пацієнту.
                # Сесія Gemini свіжа — запускається паралельно поки грає буфер (8с).
                for chunk in greeting_buffer:
                    await binotel_call.send_audio(chunk)
                if greeting_duration > 0:
                    await asyncio.sleep(greeting_duration)
            else:
                # Холодний шлях: Gemini генерує привітання в реальному часі
                await session.send_client_content(
                    turns={"parts": [{"text": "Починай розмову — коротко привітайся та нагадай про візит (максимум 2-3 речення)"}]},
                    turn_complete=True,
                )

            # Тепер дозволяємо recv_and_handle відправляти аудіо Gemini → SIP
            _greeting_done.set()
            logger.info("[AGENT] Greeting завершено — починаємо двосторонній стрімінг")

            try:
                done, pending = await asyncio.wait(
                    [send_task, recv_task], return_when=asyncio.FIRST_COMPLETED
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
                send_task.cancel()
                recv_task.cancel()
                results = await asyncio.gather(send_task, recv_task, return_exceptions=True)
                for name, r in zip(["send_task", "recv_task"], results):
                    if isinstance(r, Exception):
                        logger.error(f"[AGENT] {name} exception (finally): {r!r}")

        try:
            # Завжди відкриваємо свіжу Gemini-сесію для реального дзвінка.
            # Warmup-сесія вже закрита після буферизації — вона не може бути
            # переданою через idle-timeout Gemini між warmup і відповіддю пацієнта.
            async with self.client.aio.live.connect(
                model=GEMINI_MODEL,
                config=self._build_config(),
            ) as session:
                await _stream(session)

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
        except Exception as e:
            logger.warning(f"[AGENT] Warmup помилка: {e} — запустимо звичайний шлях")
            self._greeting_buffer = []
        finally:
            # Завжди закриваємо warmup-сесію — аудіо вже буферизовано.
            # Реальна Gemini-сесія відкриється коли пацієнт підніме трубку.
            if _warmup_cm is not None:
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
