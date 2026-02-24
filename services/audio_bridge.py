"""
Конвертація аудіо між форматами SIP (pyVoIP) та Gemini Live API.

pyVoIP read/write формат: unsigned 8-bit PCM, 8 kHz, mono
  - parse_pcmu: audioop.ulaw2lin(..., 1) + bias(+128) → unsigned 8-bit
  - parse_pcma: audioop.alaw2lin(..., 1) + bias(+128) → unsigned 8-bit
  - encode_pcmu/pcma (patched): passthrough — байти вже готові

Gemini вхід:   signed 16-bit PCM, 16 kHz, mono
Gemini вихід:  signed 16-bit PCM, 24 kHz, mono

Ланцюжок перетворень:
  SIP → Gemini: unsigned-8bit → signed-8bit → signed-16bit → resample 8→16kHz
  Gemini → SIP (PCMU): resample 24→8kHz → lin2ulaw(width=2) → µ-law RTP
  Gemini → SIP (PCMA): resample 24→8kHz → lin2alaw(width=2) → A-law RTP

audioop-lts: pip install audioop-lts (сумісна заміна audioop для Python 3.13+)
"""

import audioop

# Стан ресемплера (audioop.ratecv — stateful)
_state_sip_to_gemini: tuple | None = None
_state_gemini_to_sip: tuple | None = None

# Прапор кодеку: встановлюється з binotel_sip.py після SIP-узгодження.
# False = PCMU (µ-law), True = PCMA (A-law).
_use_alaw: bool = False


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
    PCM signed 16-bit 24 kHz (Gemini вихід) → G.711 8 kHz (RTP payload).

    Кодек визначається автоматично під час SIP-узгодження (binotel_sip.py):
      PCMU (µ-law): lin2ulaw(width=2)  — стандарт США/Японія
      PCMA (A-law):  lin2alaw(width=2)  — стандарт Європа/Україна

    pyVoIP's encode_pcmu/pcma патчований на passthrough → байти йдуть напряму в RTP.

    Шлях (1× кодування, повна 16-bit точність):
      ratecv(24→8kHz) → lin2ulaw або lin2alaw(width=2) → RTP
    """
    global _state_gemini_to_sip
    # Ресемплюємо 24kHz → 8kHz, зберігаємо 16-bit
    resampled, _state_gemini_to_sip = audioop.ratecv(
        pcm_24khz, 2, 1, 24000, 8000, _state_gemini_to_sip
    )
    # Кодуємо відповідно до узгодженого кодеку
    if _use_alaw:
        return audioop.lin2alaw(resampled, 2)
    return audioop.lin2ulaw(resampled, 2)


def set_codec_alaw(is_alaw: bool) -> None:
    """Встановити кодек: True = PCMA (A-law), False = PCMU (µ-law)."""
    global _use_alaw
    _use_alaw = is_alaw


def reset_state() -> None:
    """Скинути стан ресемплерів між дзвінками."""
    global _state_sip_to_gemini, _state_gemini_to_sip
    _state_sip_to_gemini = None
    _state_gemini_to_sip = None
