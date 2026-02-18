"""
Конвертація аудіо між форматами Binotel та Gemini.

Binotel (production) : G.711 μ-law, 8 kHz, mono
Gemini вхід          : PCM 16-bit,  16 kHz, mono
Gemini вихід         : PCM 16-bit,  24 kHz, mono

TODO: Уточнити у Binotel точний кодек (μ-law чи A-law?)
TODO: Протестувати якість аудіо після resample на реальному дзвінку
"""

import audioop


def binotel_to_gemini(g711_data: bytes) -> bytes:
    """G.711 μ-law 8 kHz → PCM 16-bit 16 kHz (для відправки в Gemini)."""
    # TODO: якщо Binotel використовує A-law — замінити ulaw2lin на alaw2lin
    pcm_8k = audioop.ulaw2lin(g711_data, 2)
    pcm_16k, _ = audioop.ratecv(pcm_8k, 2, 1, 8000, 16000, None)
    return pcm_16k


def gemini_to_binotel(pcm_24k: bytes) -> bytes:
    """PCM 16-bit 24 kHz → G.711 μ-law 8 kHz (для відправки пацієнту через Binotel)."""
    pcm_8k, _ = audioop.ratecv(pcm_24k, 2, 1, 24000, 8000, None)
    # TODO: якщо Binotel використовує A-law — замінити lin2ulaw на lin2alaw
    return audioop.lin2ulaw(pcm_8k, 2)
