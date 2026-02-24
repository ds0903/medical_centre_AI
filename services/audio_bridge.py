"""
Конвертація аудіо між форматами SIP (pyVoIP) та Gemini Live API.

pyVoIP read/write формат: unsigned 8-bit PCM, 8 kHz, mono
  - parse_pcmu: audioop.ulaw2lin(..., 1) + bias(+128) → unsigned 8-bit
  - parse_pcma: audioop.alaw2lin(..., 1) + bias(+128) → unsigned 8-bit
  - encode_pcmu/pcma (patched): passthrough — байти вже готові

Gemini вхід:   signed 16-bit PCM, 16 kHz, mono
Gemini вихід:  signed 16-bit PCM, 24 kHz, mono

Ланцюжок перетворень:
  SIP → Gemini: unsigned-8bit → signed-8bit → signed-16bit → ratecv 8→16kHz
  Gemini → SIP: Butterworth LP (anti-alias) → decimate 24→8kHz → G.711 A/µ-law

audioop-lts: pip install audioop-lts (сумісна заміна audioop для Python 3.13+)
scipy + numpy: pip install scipy numpy (покращений ресемплер для Gemini→SIP)
"""

import audioop

import numpy as np
from scipy.signal import butter, sosfilt, sosfilt_zi

# ---------------------------------------------------------------------------
# LP фільтр для downsampling 24kHz → 8kHz
# Butterworth 8-го порядку, зріз на 3400 Hz (стандарт телефонії G.711).
# Придушує частоти вище 4 kHz перед децимацією щоб уникнути aliasing.
# ---------------------------------------------------------------------------
_LP_SOS = butter(8, 3400 / (24000 / 2), btype="low", output="sos")

# Стан фільтра між chunk-ами (stateful — безперервний сигнал без стрибків на межах)
_zi_gemini_to_sip: np.ndarray | None = None

# Стан ресемплера audioop для SIP→Gemini напрямку
_state_sip_to_gemini: tuple | None = None

# Прапор кодеку: встановлюється з binotel_sip.py після SIP-узгодження.
# False = PCMU (µ-law), True = PCMA (A-law).
_use_alaw: bool = False


def sip_to_gemini_input(pcm_8khz_u8: bytes) -> bytes:
    """
    PCM unsigned 8-bit 8 kHz (pyVoIP) → PCM signed 16-bit 16 kHz (Gemini вхід).

    pyVoIP decode pipeline: G.711 → 8-bit signed → bias +128 → unsigned 8-bit.
    Ми повертаємо це назад до signed 16-bit і upsampling до 16kHz для Gemini.
    Напрямок менш критичний для якості (8kHz input → не можна додати нових частот).
    """
    global _state_sip_to_gemini
    # Крок 1: unsigned 8-bit → signed 8-bit (прибираємо bias +128)
    signed_8bit = audioop.bias(pcm_8khz_u8, 1, -128)
    # Крок 2: signed 8-bit → signed 16-bit
    pcm_16bit = audioop.lin2lin(signed_8bit, 1, 2)
    # Крок 3: resample 8kHz → 16kHz (stateful)
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

    Шлях (з anti-aliasing фільтром):
      int16 array → Butterworth LP 3.4kHz → decimate [::3] → G.711 encoding → RTP
    """
    global _zi_gemini_to_sip

    # bytes → float64 numpy array (signed 16-bit little-endian)
    arr = np.frombuffer(pcm_24khz, dtype="<i2").astype(np.float64)

    if len(arr) == 0:
        return b""

    # Ініціалізуємо стан фільтра на першому chunk (steady-state init — без startup transient)
    if _zi_gemini_to_sip is None:
        _zi_gemini_to_sip = sosfilt_zi(_LP_SOS) * arr[0]

    # Anti-aliasing LP фільтр (stateful — безперервний через chunk-и)
    filtered, _zi_gemini_to_sip = sosfilt(_LP_SOS, arr, zi=_zi_gemini_to_sip)

    # Децимація 24kHz → 8kHz (беремо кожен 3-й семпл)
    decimated = filtered[::3]

    # float64 → int16 (clip щоб уникнути overflow)
    pcm_8khz = np.clip(decimated, -32768, 32767).astype("<i2").tobytes()

    # G.711 encoding: A-law або µ-law залежно від кодеку
    if _use_alaw:
        return audioop.lin2alaw(pcm_8khz, 2)
    return audioop.lin2ulaw(pcm_8khz, 2)


def set_codec_alaw(is_alaw: bool) -> None:
    """Встановити кодек: True = PCMA (A-law), False = PCMU (µ-law)."""
    global _use_alaw
    _use_alaw = is_alaw


def reset_state() -> None:
    """Скинути стан ресемплерів та фільтра між дзвінками."""
    global _state_sip_to_gemini, _zi_gemini_to_sip
    _state_sip_to_gemini = None
    _zi_gemini_to_sip = None
