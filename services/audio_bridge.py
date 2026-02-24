"""
Конвертація аудіо між форматами SIP (pyVoIP) та Gemini Live API.

pyVoIP read/write формат: unsigned 8-bit PCM, 8 kHz, mono
  - parse_pcmu: audioop.ulaw2lin(..., 1) + bias(+128) → unsigned 8-bit
  - encode_pcmu: bias(-128) + audioop.lin2ulaw(..., 1) → expects unsigned 8-bit

Gemini вхід:   signed 16-bit PCM, 16 kHz, mono
Gemini вихід:  signed 16-bit PCM, 24 kHz, mono

Ланцюжок перетворень:
  SIP → Gemini: unsigned-8bit → signed-8bit → signed-16bit → resample 8→16kHz
  Gemini → SIP: resample 24→8kHz → signed-16bit → signed-8bit → unsigned-8bit

audioop-lts: pip install audioop-lts (сумісна заміна audioop для Python 3.13+)
"""

import audioop

# Стан ресемплера (audioop.ratecv — stateful)
_state_sip_to_gemini: tuple | None = None
_state_gemini_to_sip: tuple | None = None


def sip_to_gemini_input(pcm_8khz_u8: bytes) -> bytes:
    """
    PCM unsigned 8-bit 8 kHz (pyVoIP) → PCM signed 16-bit 16 kHz (Gemini вхід).

    pyVoIP decode pipeline: µ-law → 8-bit signed → bias +128 → unsigned 8-bit.
    Ми повертаємо це назад до signed 16-bit і upsampling до 16kHz для Gemini.
    """
    global _state_sip_to_gemini
    # Крок 1: unsigned 8-bit → signed 8-bit (прибираємо bias +128)
    signed_8bit = audioop.bias(pcm_8khz_u8, 1, -128)
    # Крок 2: signed 8-bit → signed 16-bit
    pcm_16bit = audioop.lin2lin(signed_8bit, 1, 2)
    # Крок 3: resample 8kHz → 16kHz (stateful для якіснішого ресемплінгу)
    result, _state_sip_to_gemini = audioop.ratecv(
        pcm_16bit, 2, 1, 8000, 16000, _state_sip_to_gemini
    )
    return result


def gemini_output_to_sip(pcm_24khz: bytes) -> bytes:
    """
    PCM signed 16-bit 24 kHz (Gemini вихід) → PCM unsigned 8-bit 8 kHz (pyVoIP).

    pyVoIP encode pipeline: unsigned 8-bit → bias -128 → signed 8-bit → µ-law.
    Ми конвертуємо Gemini 24kHz/16-bit до unsigned 8-bit/8kHz що очікує pyVoIP.
    """
    global _state_gemini_to_sip
    # Крок 1: resample 24kHz → 8kHz (залишаємо 16-bit для точності)
    resampled, _state_gemini_to_sip = audioop.ratecv(
        pcm_24khz, 2, 1, 24000, 8000, _state_gemini_to_sip
    )
    # Крок 2: signed 16-bit → signed 8-bit
    pcm_8bit_signed = audioop.lin2lin(resampled, 2, 1)
    # Крок 3: signed 8-bit → unsigned 8-bit (додаємо bias +128 як очікує pyVoIP)
    return audioop.bias(pcm_8bit_signed, 1, 128)


def reset_state() -> None:
    """Скинути стан ресемплерів між дзвінками."""
    global _state_sip_to_gemini, _state_gemini_to_sip
    _state_sip_to_gemini = None
    _state_gemini_to_sip = None
