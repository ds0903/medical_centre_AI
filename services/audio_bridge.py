"""
Конвертація аудіо між форматами SIP (pyVoIP) та Gemini Live API.

pyVoIP read/write формат: unsigned 8-bit PCM, 8 kHz, mono
  - parse_pcmu: audioop.ulaw2lin(..., 1) + bias(+128) → unsigned 8-bit
  - parse_pcma: audioop.alaw2lin(..., 1) + bias(+128) → unsigned 8-bit
  - encode_pcmu/pcma (patched): passthrough — байти вже готові

Gemini вхід:   signed 16-bit PCM, 16 kHz, mono
Gemini вихід:  signed 16-bit PCM, 24 kHz, mono

SIP → Gemini:
  unsigned-8bit → signed-16bit → resample_poly 8→16kHz → Smooth AGC

Gemini → SIP:
  Butterworth LP (anti-alias) → decimate 24→8kHz → Smooth norm → G.711 A/µ-law

audioop-lts: pip install audioop-lts (сумісна заміна audioop для Python 3.13+)
scipy + numpy: pip install scipy numpy
"""

import audioop

import numpy as np
from scipy.signal import butter, sosfilt, sosfilt_zi, resample_poly

# ---------------------------------------------------------------------------
# Gemini → SIP: LP фільтр (anti-aliasing перед децимацією)
# Butterworth 8-го порядку, зріз 3400 Hz — стандарт телефонії G.711.
# ---------------------------------------------------------------------------
_LP_SOS = butter(8, 3400 / (24000 / 2), btype="low", output="sos")

# ---------------------------------------------------------------------------
# SIP → Gemini: Smooth AGC
#
# Підтягує тихих пацієнтів до цільового RMS.
# Під час тиші/шуму (rms < NOISE_FLOOR) — gain плавно повертається до 1.0.
# Alpha = 0.05 → ~20 chunks (~400 мс) для повної адаптації.
# ---------------------------------------------------------------------------
_NOISE_FLOOR_RMS = 150    # нижче цього порогу — тиша/шум, не підсилюємо
_TARGET_RMS_IN   = 3000   # цільовий RMS на вході (16-bit signed, max = 32768)
_MAX_GAIN_IN     = 8.0    # захист від надмірного підсилення шуму
_AGC_ALPHA       = 0.05   # швидкість адаптації (0 = ніколи, 1 = миттєво)

# ---------------------------------------------------------------------------
# Gemini → SIP: Smooth нормалізація виходу
#
# Стабілізує гучність AI-голосу між дзвінками/репліками.
# Alpha = 0.1 → ~10 chunks (~200 мс) для адаптації.
# ---------------------------------------------------------------------------
_TARGET_RMS_OUT  = 5000   # цільовий RMS виходу
_MAX_GAIN_OUT    = 4.0    # обмеження підсилення
_OUT_ALPHA       = 0.1    # швидкість адаптації виходу

# ---------------------------------------------------------------------------
# Стан між chunk-ами (stateful потокова обробка)
# ---------------------------------------------------------------------------
_zi_gemini_to_sip: np.ndarray | None = None  # стан LP фільтра
_input_gain  = [1.0]   # поточний gain AGC (список для mutable closure)
_output_gain = [1.0]   # поточний gain нормалізації виходу

# Кодек: встановлюється з binotel_sip.py після SIP-узгодження
_use_alaw: bool = False


def sip_to_gemini_input(pcm_8khz_u8: bytes) -> bytes:
    """
    PCM unsigned 8-bit 8 kHz (pyVoIP) → PCM signed 16-bit 16 kHz (Gemini вхід).

    Pipeline:
      1. unsigned-8bit → signed-16bit
      2. resample_poly 8→16kHz (поліфазна фільтрація — краще ніж audioop.ratecv)
      3. Smooth AGC (плавно підтягує тихих пацієнтів, ігнорує тишу)
    """
    # Крок 1: unsigned-8bit → signed-8bit (прибираємо bias +128) → signed-16bit
    s8  = audioop.bias(pcm_8khz_u8, 1, -128)
    s16 = audioop.lin2lin(s8, 1, 2)

    # Крок 2: resample_poly 8→16kHz (поліфазний FIR ресемплер)
    arr      = np.frombuffer(s16, dtype="<i2").astype(np.float64)
    resampled = resample_poly(arr, up=2, down=1)

    # Крок 3: Smooth AGC
    rms = float(np.sqrt(np.mean(resampled ** 2))) if len(resampled) else 0.0
    if rms > _NOISE_FLOOR_RMS:
        # Сигнал — адаптуємо gain до цільового RMS
        target = min(_TARGET_RMS_IN / rms, _MAX_GAIN_IN)
        _input_gain[0] = (1.0 - _AGC_ALPHA) * _input_gain[0] + _AGC_ALPHA * target
    else:
        # Тиша/шум — плавно повертаємо gain до нейтрального
        _input_gain[0] = (1.0 - _AGC_ALPHA) * _input_gain[0] + _AGC_ALPHA * 1.0

    result = resampled * _input_gain[0]
    return np.clip(result, -32768, 32767).astype("<i2").tobytes()


def gemini_output_to_sip(pcm_24khz: bytes) -> bytes:
    """
    PCM signed 16-bit 24 kHz (Gemini вихід) → G.711 8 kHz (RTP payload).

    Кодек визначається під час SIP-узгодження (binotel_sip.py):
      PCMU (µ-law): стандарт США/Японія
      PCMA (A-law): стандарт Європа/Україна

    Pipeline:
      1. Butterworth LP 3.4kHz (anti-aliasing перед децимацією)
      2. Децимація [::3]: 24kHz → 8kHz
      3. Smooth нормалізація гучності (стабільний AI-голос)
      4. G.711 encoding → RTP
    """
    global _zi_gemini_to_sip

    arr = np.frombuffer(pcm_24khz, dtype="<i2").astype(np.float64)
    if len(arr) == 0:
        return b""

    # Ініціалізуємо стан фільтра (steady-state — без startup transient)
    if _zi_gemini_to_sip is None:
        _zi_gemini_to_sip = sosfilt_zi(_LP_SOS) * arr[0]

    # Anti-aliasing LP фільтр (stateful)
    filtered, _zi_gemini_to_sip = sosfilt(_LP_SOS, arr, zi=_zi_gemini_to_sip)

    # Децимація 24kHz → 8kHz
    decimated = filtered[::3]

    # Smooth нормалізація: стабілізуємо гучність AI-голосу
    rms = float(np.sqrt(np.mean(decimated ** 2))) if len(decimated) else 0.0
    if rms > 0:
        target = min(_TARGET_RMS_OUT / rms, _MAX_GAIN_OUT)
        _output_gain[0] = (1.0 - _OUT_ALPHA) * _output_gain[0] + _OUT_ALPHA * target
        decimated = decimated * _output_gain[0]

    pcm_8khz = np.clip(decimated, -32768, 32767).astype("<i2").tobytes()

    # G.711 encoding
    if _use_alaw:
        return audioop.lin2alaw(pcm_8khz, 2)
    return audioop.lin2ulaw(pcm_8khz, 2)


def set_codec_alaw(is_alaw: bool) -> None:
    """Встановити кодек: True = PCMA (A-law), False = PCMU (µ-law)."""
    global _use_alaw
    _use_alaw = is_alaw


def reset_state() -> None:
    """Скинути стан ресемплерів та фільтрів між дзвінками."""
    global _zi_gemini_to_sip
    _zi_gemini_to_sip = None
    _input_gain[0]  = 1.0
    _output_gain[0] = 1.0
