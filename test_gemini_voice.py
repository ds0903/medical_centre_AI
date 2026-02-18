import asyncio
import pyaudio
from google import genai

API_KEY = "AIzaSyB8SoZnjdGRaSy-sCndqZHh1Cmqdl_Jm1A"

# Аудіо вихід: 24kHz 16-bit mono (формат Gemini)
FORMAT = pyaudio.paInt16
CHANNELS = 1
OUTPUT_RATE = 24000


async def test_voice():
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
        Ти менеджер медичного центру в Кривому Розі.
        Говориш ТІЛЬКИ українською мовою.
        Будь ввічливим та професійним.
        Представся і запитай як можеш допомогти.
        """
    }

    # Відкриваємо динамік для відтворення голосу
    pa = pyaudio.PyAudio()
    speaker = pa.open(format=FORMAT, channels=CHANNELS, rate=OUTPUT_RATE, output=True)

    async with client.aio.live.connect(
        model="gemini-2.5-flash-native-audio-preview-12-2025",
        config=config
    ) as session:

        # --- Перша репліка ---
        print("Dzvonymo patsiyentu...")
        await session.send_client_content(
            turns={"parts": [{"text": "Почни розмову, привітайся"}]},
            turn_complete=True
        )

        async for response in session.receive():
            if response.text:
                print(f"AI (tekst): {response.text}")
            if response.data:
                # Програємо аудіо ОДРАЗУ через динамік
                speaker.write(response.data)
            if response.server_content and response.server_content.turn_complete:
                print("Vidpovid zavershena\n")
                break

        # --- Друга репліка ---
        print("Ty kazhesh: 'Khochu zapysatysya do kardioloha'")
        await session.send_client_content(
            turns={"parts": [{"text": "Хочу записатися до кардіолога"}]},
            turn_complete=True
        )

        async for response in session.receive():
            if response.text:
                print(f"AI: {response.text}")
            if response.data:
                speaker.write(response.data)
            if response.server_content and response.server_content.turn_complete:
                print("Vidpovid zavershena")
                break

    speaker.stop_stream()
    speaker.close()
    pa.terminate()
    print("\nGotovo!")


asyncio.run(test_voice())
