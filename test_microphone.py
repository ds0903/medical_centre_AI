import asyncio
import pyaudio
import time
from google import genai

API_KEY = "AIzaSyB8SoZnjdGRaSy-sCndqZHh1Cmqdl_Jm1A"

# Аудіо налаштування
FORMAT = pyaudio.paInt16
CHANNELS = 1
INPUT_RATE = 16000   # мікрофон: 16kHz
OUTPUT_RATE = 24000  # Gemini вихід: 24kHz
CHUNK = 512

# Ціни Live API (за 1M токенів)
PRICE_INPUT = 3.00    # $ аудіо вхід
PRICE_OUTPUT = 12.00  # $ аудіо вихід
TOKENS_PER_SEC = 25   # аудіо токенів на секунду
USD_TO_UAH = 43.00


async def microphone_chat():
    client = genai.Client(api_key=API_KEY)

    config = {
        "response_modalities": ["AUDIO"],
        "speech_config": {
            "voice_config": {
                "prebuilt_voice_config": {
                    "voice_name": "Puck"
                }
            }
        },
        "system_instruction": """
        Ти менеджер медичного центру.
        Говориш ТІЛЬКИ українською.
        Зараз телефонуєш пацієнту нагадати про візит завтра о 14:00 до кардіолога.
        """
    }

    pa = pyaudio.PyAudio()

    # Потік для відтворення голосу AI
    output_stream = pa.open(
        format=FORMAT,
        channels=CHANNELS,
        rate=OUTPUT_RATE,
        output=True
    )

    input_seconds = 0.0
    output_bytes = 0
    start_time = time.time()

    try:
        async with client.aio.live.connect(
            model="gemini-2.5-flash-native-audio-preview-12-2025",
            config=config
        ) as session:
            print("Pidklyucheno do Gemini Live API!")
            print("AI pochne rozmovu...\n")

            # AI починає розмову
            await session.send_client_content(
                turns={"parts": [{"text": "Привітайся та нагадай про візит"}]},
                turn_complete=True
            )

            # Отримуємо та програємо початкову відповідь
            async for response in session.receive():
                if response.text:
                    print(f"AI: {response.text}")
                if response.data:
                    output_stream.write(response.data)
                    output_bytes += len(response.data)
                if response.server_content and response.server_content.turn_complete:
                    break

            # Відкриваємо мікрофон
            input_stream = pa.open(
                format=FORMAT,
                channels=CHANNELS,
                rate=INPUT_RATE,
                input=True,
                frames_per_buffer=CHUNK
            )

            print("\nGOVORY ZARAZ (Ctrl+C shchob zupynyty)...\n")

            loop = asyncio.get_event_loop()

            async def send_mic():
                """Відправляє аудіо з мікрофона в Gemini"""
                nonlocal input_seconds
                while True:
                    data = await loop.run_in_executor(
                        None,
                        lambda: input_stream.read(CHUNK, exception_on_overflow=False)
                    )
                    input_seconds += CHUNK / INPUT_RATE
                    await session.send_realtime_input(
                        audio={"data": data, "mime_type": "audio/pcm"}
                    )

            async def recv_audio():
                """Отримує та програє аудіо від Gemini"""
                nonlocal output_bytes
                async for response in session.receive():
                    if response.text:
                        print(f"AI: {response.text}")
                    if response.data:
                        output_stream.write(response.data)
                        output_bytes += len(response.data)

            send_task = asyncio.create_task(send_mic())
            recv_task = asyncio.create_task(recv_audio())

            try:
                await asyncio.gather(send_task, recv_task)
            except (KeyboardInterrupt, asyncio.CancelledError):
                send_task.cancel()
                recv_task.cancel()

            input_stream.stop_stream()
            input_stream.close()

    except KeyboardInterrupt:
        pass

    finally:
        output_stream.stop_stream()
        output_stream.close()
        pa.terminate()

        # Статистика
        duration = time.time() - start_time
        output_seconds = output_bytes / (OUTPUT_RATE * 2) if output_bytes > 0 else 0

        in_tokens = input_seconds * TOKENS_PER_SEC
        out_tokens = output_seconds * TOKENS_PER_SEC
        total_tokens = in_tokens + out_tokens

        in_cost = (in_tokens / 1_000_000) * PRICE_INPUT
        out_cost = (out_tokens / 1_000_000) * PRICE_OUTPUT
        total_usd = in_cost + out_cost
        total_uah = total_usd * USD_TO_UAH

        print("\n" + "=" * 50)
        print("        STATYSTYKA ROZMOVY")
        print("=" * 50)
        print(f"  Tryvalist:     {duration:.0f} sek")
        print(f"  Audio vkhid:   {input_seconds:.1f} sek ({in_tokens:.0f} tokeniv)")
        print(f"  Audio vykhid:  {output_seconds:.1f} sek ({out_tokens:.0f} tokeniv)")
        print(f"  Vsogo tokeniv: {total_tokens:.0f}")
        print(f"  Vartist:       ${total_usd:.6f} USD")
        print(f"  Vartist:       {total_uah:.4f} grn")
        print("=" * 50)


if __name__ == "__main__":
    try:
        asyncio.run(microphone_chat())
    except KeyboardInterrupt:
        print("\nZupyneno.")
