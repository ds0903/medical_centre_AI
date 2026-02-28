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
        logger.info("=" * 55)
        logger.info("[DEBUG] Симуляція дзвінка")
        logger.info(f"  Пацієнт : {self.visit['patient_name']}")
        logger.info(f"  Лікар   : {self.visit['doctor_name']}")
        logger.info(f"  Час     : {self.visit['visit_datetime']}")
        logger.info("  Ти граєш роль пацієнта. Ctrl+C — завершити.")
        logger.info("=" * 55)

        output_stream = self._pa.open(
            format=FORMAT, channels=CHANNELS, rate=OUTPUT_RATE, output=True
        )
        start = time.time()

        try:
            async with self.client.aio.live.connect(
                model=GEMINI_MODEL,
                config=self._build_config(),
            ) as session:

                # AI починає розмову першим
                await session.send_client_content(
                    turns={"parts": [{"text": "Починай розмову — коротко привітайся та нагадай про візит (максимум 2-3 речення)"}]},
                    turn_complete=True,
                )

                # Отримуємо першу репліку AI
                async for resp in session.receive():
                    if resp.data:
                        output_stream.write(resp.data)
                    if resp.server_content and resp.server_content.turn_complete:
                        break

                # Відкриваємо мікрофон
                input_stream = self._pa.open(
                    format=FORMAT,
                    channels=CHANNELS,
                    rate=INPUT_RATE,
                    input=True,
                    frames_per_buffer=CHUNK,
                )

                loop = asyncio.get_event_loop()

                async def _send_mic() -> None:
                    while not self._end_event.is_set():
                        data = await loop.run_in_executor(
                            None,
                            lambda: input_stream.read(CHUNK, exception_on_overflow=False),
                        )
                        await session.send_realtime_input(
                            audio={"data": data, "mime_type": "audio/pcm"}
                        )

                async def _recv_and_handle() -> None:
                    async for resp in session.receive():
                        if resp.data:
                            output_stream.write(resp.data)

                        if resp.tool_call:
                            for fc in resp.tool_call.function_calls:
                                args = dict(fc.args) if fc.args else {}
                                result = await handle_tool_call(fc.name, args, self.visit)

                                if fc.name == "end_call":
                                    self.call_result = result.get("result", "no_result")
                                    logger.info(f"[AGENT] end_call → {self.call_result}")
                                    self._end_event.set()
                                    await session.send_tool_response(
                                        function_responses=[
                                            types.FunctionResponse(
                                                id=fc.id,
                                                name=fc.name,
                                                response=result,
                                            )
                                        ]
                                    )
                                    return

                                await session.send_tool_response(
                                    function_responses=[
                                        types.FunctionResponse(
                                            id=fc.id,
                                            name=fc.name,
                                            response=result,
                                        )
                                    ]
                                )

                send_task = asyncio.create_task(_send_mic())
                recv_task = asyncio.create_task(_recv_and_handle())

                try:
                    await asyncio.wait(
                        [send_task, recv_task],
                        return_when=asyncio.FIRST_COMPLETED,
                    )
                finally:
                    send_task.cancel()
                    recv_task.cancel()
                    await asyncio.gather(send_task, recv_task, return_exceptions=True)

                input_stream.stop_stream()
                input_stream.close()

        except KeyboardInterrupt:
            logger.info("[AGENT] Перервано користувачем")
        finally:
            output_stream.stop_stream()
            output_stream.close()

        self.call_duration = time.time() - start
        result = self.call_result or "no_result"
        logger.info(f"[AGENT] Завершено. Тривалість: {self.call_duration:.0f}с. Результат: {result}")
        return result

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

        warmup_session = getattr(self, "_warmup_session", None)
        warmup_cm = getattr(self, "_warmup_cm", None)
        greeting_buffer = getattr(self, "_greeting_buffer", [])

        async def _stream(session) -> None:
            """Двосторонній стрімінг аудіо між Binotel SIP та Gemini."""

            # Якщо greeting вже буферизований — відтворюємо одразу (нульова затримка)
            if greeting_buffer:
                greeting_duration = getattr(self, "_greeting_duration", 3.0)
                logger.info(
                    f"[AGENT] Буферизоване привітання: {len(greeting_buffer)} chunks"
                    f" ({greeting_duration:.1f}с)"
                )
                for chunk in greeting_buffer:
                    await binotel_call.send_audio(chunk)
                # Чекаємо поки pyVoIP відтворить привітання (trans() дренує зі швидкістю 8kHz).
                # Без цього _send_patient_audio стартує одразу, пацієнт говорить "алло"
                # ще під час привітання, і Gemini може неправильно відреагувати.
                logger.info(f"[AGENT] Чекаємо кінця привітання ({greeting_duration:.1f}с)...")
                await asyncio.sleep(greeting_duration)
            else:
                # Холодний старт: генеруємо привітання зараз
                await session.send_client_content(
                    turns={"parts": [{"text": "Починай розмову — коротко привітайся та нагадай про візит (максимум 2-3 речення)"}]},
                    turn_complete=True,
                )
                async for resp in session.receive():
                    if resp.data:
                        await binotel_call.send_audio(resp.data)
                    if resp.server_content and resp.server_content.turn_complete:
                        break

            async def _send_patient_audio() -> None:
                async for chunk in binotel_call.receive_audio():
                    if self._end_event.is_set():
                        break
                    await session.send_realtime_input(
                        audio={"data": chunk, "mime_type": "audio/pcm;rate=16000"}
                    )

            async def _recv_and_handle() -> None:
                _draining = False  # True після end_call — граємо прощання, не обриваємо
                async for resp in session.receive():
                    if resp.data:
                        await binotel_call.send_audio(resp.data)
                    if _draining:
                        # Чекаємо поки AI договорить прощання (turn_complete)
                        if resp.server_content and resp.server_content.turn_complete:
                            logger.info("[AGENT] AI завершив прощання — завершуємо")
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
                            result = await handle_tool_call(fc.name, args, self.visit)
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

            send_task = asyncio.create_task(_send_patient_audio())
            recv_task = asyncio.create_task(_recv_and_handle())
            try:
                await asyncio.wait([send_task, recv_task], return_when=asyncio.FIRST_COMPLETED)
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
                await asyncio.gather(send_task, recv_task, return_exceptions=True)

        try:
            if warmup_session is not None:
                # Pre-warmed шлях: сесія вже відкрита, закриваємо вручну після
                try:
                    await _stream(warmup_session)
                finally:
                    if warmup_cm is not None:
                        try:
                            await warmup_cm.__aexit__(None, None, None)
                        except Exception:
                            pass
            else:
                # Холодний шлях: звичайний async with
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
        self._warmup_cm = None
        self._warmup_session = None
        try:
            self._warmup_cm = self.client.aio.live.connect(
                model=GEMINI_MODEL,
                config=self._build_config(),
            )
            self._warmup_session = await self._warmup_cm.__aenter__()
            await self._warmup_session.send_client_content(
                turns={"parts": [{"text": "Починай розмову — коротко привітайся та нагадай про візит (максимум 2-3 речення)"}]},
                turn_complete=True,
            )
            async for resp in self._warmup_session.receive():
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
            if self._warmup_cm is not None:
                try:
                    await self._warmup_cm.__aexit__(None, None, None)
                except Exception:
                    pass
            self._warmup_session = None
            self._warmup_cm = None
            self._greeting_buffer = []

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
