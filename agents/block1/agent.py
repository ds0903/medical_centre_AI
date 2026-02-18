"""
Агент Блоку 1: Нагадування про візити.

DEBUG mode   : мікрофон → Gemini Live → динамік  (ти = пацієнт)
PRODUCTION   : Binotel аудіо ↔ Gemini Live        (TODO після документації Binotel)
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
INPUT_RATE = 16000   # мікрофон / Binotel → Gemini
OUTPUT_RATE = 24000  # Gemini → динамік / Binotel
CHUNK = 512


class Block1Agent:
    """
    Голосовий агент для нагадування пацієнту про візит.

    Використовує Gemini Live API з native audio — без STT/TTS посередників.
    Function calling дозволяє агенту підтверджувати/переносити/скасовувати візити
    прямо під час розмови.
    """

    def __init__(self, visit: dict) -> None:
        """
        Args:
            visit: dict з ключами:
                visit_id, patient_name, patient_phone,
                doctor_name, doctor_id, visit_datetime
        """
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
                    turns={"parts": [{"text": "Починай розмову — привітайся та нагадай про візит"}]},
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
                        # Аудіо відповідь
                        if resp.data:
                            output_stream.write(resp.data)

                        # Tool call від Gemini
                        if resp.tool_call:
                            for fc in resp.tool_call.function_calls:
                                args = dict(fc.args) if fc.args else {}
                                result = await handle_tool_call(fc.name, args, self.visit)

                                # end_call — завершуємо сесію
                                if fc.name == "end_call":
                                    self.call_result = result.get("result", "no_result")
                                    logger.info(f"[AGENT] end_call → {self.call_result}")
                                    self._end_event.set()

                                    # Повертаємо результат в Gemini перед виходом
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

                                # Інші tool calls — повертаємо результат
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
    # PRODUCTION режим: Binotel ↔ Gemini
    # ------------------------------------------------------------------

    async def run_production(self, binotel_call) -> str:
        """
        Production режим: аудіо міст між Binotel та Gemini Live.

        TODO: Реалізувати після отримання документації Binotel API.
        TODO: Уточнити як читати/писати аудіо потік Binotel (WebSocket? callback?)
        TODO: Замінити локальні send_mic / recv_audio на binotel_call.send_audio / receive_audio
        TODO: Реалізувати transfer_to_operator через Binotel SIP transfer
        """
        raise NotImplementedError("TODO: реалізувати production режим з Binotel")

    # ------------------------------------------------------------------
    # Публічний інтерфейс
    # ------------------------------------------------------------------

    async def run(self, binotel_call=None) -> str:
        """
        Запустити агента.

        Args:
            binotel_call: BinotelCall об'єкт (production) або None (debug)

        Returns:
            Результат розмови: confirmed / rescheduled / cancelled / no_result
        """
        if DEBUG:
            return await self.run_debug()
        return await self.run_production(binotel_call)

    def cleanup(self) -> None:
        """Звільнити PyAudio ресурси."""
        self._pa.terminate()
