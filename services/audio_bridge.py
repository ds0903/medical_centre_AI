"""
Конвертація аудіо між форматами SIP (pyVoIP) та Gemini Live API.

pyVoIP повертає/приймає: PCM 16-bit, 8 kHz, mono
Gemini вхід:             PCM 16-bit, 16 kHz, mono
Gemini вихід:            PCM 16-bit, 24 kHz, mono

Тобто нам потрібен лише ресемплінг — G.711 декодує сама бібліотека pyVoIP.

audioop-lts: pip install audioop-lts (сумісна заміна audioop для Python 3.13+)
"""

import audioop

# Стан ресемплера (audioop.ratecv — stateful)
_state_sip_to_gemini: tuple | None = None
_state_gemini_to_sip: tuple | None = None


def sip_to_gemini_input(pcm_8khz: bytes) -> bytes:
    """
    PCM 16-bit 8 kHz (pyVoIP) → PCM 16-bit 16 kHz (Gemini вхід).
    Stateful: зберігає стан між chunk-ами для якіснішого ресемплінгу.
    """
    global _state_sip_to_gemini
    result, _state_sip_to_gemini = audioop.ratecv(
        pcm_8khz, 2, 1, 8000, 16000, _state_sip_to_gemini
    )
    return result


def gemini_output_to_sip(pcm_24khz: bytes) -> bytes:
    """
    PCM 16-bit 24 kHz (Gemini вихід) → PCM 16-bit 8 kHz (pyVoIP).
    Stateful: зберігає стан між chunk-ами.
    """
    global _state_gemini_to_sip
    result, _state_gemini_to_sip = audioop.ratecv(
        pcm_24khz, 2, 1, 24000, 8000, _state_gemini_to_sip
    )
    return result


def reset_state() -> None:
    """Скинути стан ресемплерів між дзвінками."""
    global _state_sip_to_gemini, _state_gemini_to_sip
    _state_sip_to_gemini = None
    _state_gemini_to_sip = None
