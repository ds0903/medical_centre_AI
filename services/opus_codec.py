"""
Opus encode/decode для RTP (raw Opus frames, без Ogg-контейнера).

Вхід енкодера:  PCM signed 16-bit, 24 kHz (Gemini вихід)
Вихід енкодера: raw Opus frame bytes → RTP payload

Вхід декодера:  raw Opus frame bytes (від Binotel RTP)
Вихід декодера: PCM unsigned 8-bit, 16 kHz → сумісний з pyVoIP pmin buffer.
  libopus декодує напряму в 16kHz (нативна підтримка) — без проміжного ресемплу.
  16kHz = 0-8kHz частоти (вдвічі краще ніж старий 8kHz шлях, де було лише 0-4kHz).

pyogg вже містить скомпільований libopus для Windows/Linux/macOS.
"""

import ctypes
import logging

import numpy as np
from pyogg.opus import libopus
from scipy.signal import firwin, lfilter, lfilter_zi

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Opus параметри
# ---------------------------------------------------------------------------
_OPUS_APPLICATION_VOIP  = 2048   # оптимізовано для голосових дзвінків
_OPUS_SAMPLE_RATE       = 48000  # частота енкодера (RTP стандарт)
_OPUS_DECODE_RATE       = 16000  # частота декодера: напряму 16kHz без ресемплу
_OPUS_CHANNELS          = 1      # моно
_OPUS_FRAME_DURATION_MS = 20     # 20мс фрейми
_OPUS_FRAME_SAMPLES     = _OPUS_SAMPLE_RATE * _OPUS_FRAME_DURATION_MS // 1000  # 960 @ 48kHz (encode)
_OPUS_DECODE_SAMPLES    = _OPUS_DECODE_RATE * _OPUS_FRAME_DURATION_MS // 1000  # 320 @ 16kHz (decode)
_OPUS_MAX_PACKET        = 4000   # макс розмір Opus пакету (байт)
_OPUS_BITRATE           = 48000  # 48 kbps — чіткіший голос, HYBRID mode (до 12kHz)
_OPUS_COMPLEXITY        = 10     # максимальна якість кодування (0–10)
_OPUS_BANDWIDTH_WIDEBAND = 1103  # SILK mode, 0–8kHz — оптимально для телефонного голосу

# ---------------------------------------------------------------------------
# Stateful upsample 24kHz → 48kHz
#
# Вставляємо нулі між семплами (zero-insertion) + stateful FIR LP 11kHz.
# Windowed-sinc FIR (firwin) має ЛІНІЙНУ фазу → константна затримка → без фазових
# спотворень на транзієнтах (приголосні, сибілянти). IIR Butterworth має нелінійну
# фазу → "розмазує" сибілянти → шипіння. lfilter з zi зберігає стан між chunks
# → без edge-артефактів на межах чанків.
# ---------------------------------------------------------------------------
_UP_FIR = firwin(64, 7500 / 24000)             # 64-tap LP @ 7.5kHz — вирівняно під Opus WIDEBAND (8kHz)
_UP_FIR_ZI_TEMPLATE = lfilter_zi(_UP_FIR, [1.0])  # шаблон початкового стану

# ---------------------------------------------------------------------------
# Стан кодека
# ---------------------------------------------------------------------------
_encoder = None
_decoder = None
_upsample_zi: np.ndarray | None = None  # стан LP фільтра між chunks

# Буфер для накопичення 24kHz чанків до повного Opus фрейму
# Потрібно 960 семплів @ 48kHz = 480 семплів @ 24kHz = 960 байт (s16)
_encode_buf = bytearray()
_ENCODE_BUF_THRESHOLD = _OPUS_FRAME_SAMPLES * 2 // 2  # 960 байт @ 24kHz (ресемплюємо вгору)


def _get_encoder():
    global _encoder
    if _encoder is None:
        err = ctypes.c_int(0)
        _encoder = libopus.opus_encoder_create(
            _OPUS_SAMPLE_RATE, _OPUS_CHANNELS, _OPUS_APPLICATION_VOIP, ctypes.byref(err)
        )
        if err.value != 0:
            raise RuntimeError(f"opus_encoder_create failed: {err.value}")
        libopus.opus_encoder_ctl(_encoder, 4002, _OPUS_BITRATE)       # OPUS_SET_BITRATE
        libopus.opus_encoder_ctl(_encoder, 4010, _OPUS_COMPLEXITY)    # OPUS_SET_COMPLEXITY
        libopus.opus_encoder_ctl(_encoder, 4012, 0)                   # OPUS_SET_INBAND_FEC = off (звільняємо бітрейт)
        libopus.opus_encoder_ctl(_encoder, 4014, 0)                   # OPUS_SET_PACKET_LOSS_PERC = 0%
        libopus.opus_encoder_ctl(_encoder, 4004, _OPUS_BANDWIDTH_WIDEBAND)  # MAX_BANDWIDTH = WIDEBAND (SILK 8kHz)
        logger.info(
            f"[OPUS] Encoder ініціалізовано ({_OPUS_BITRATE // 1000}kbps, "
            f"complexity={_OPUS_COMPLEXITY}, SILK 8kHz, FEC=off)"
        )
    return _encoder


def _get_decoder():
    global _decoder
    if _decoder is None:
        err = ctypes.c_int(0)
        _decoder = libopus.opus_decoder_create(
            _OPUS_DECODE_RATE, _OPUS_CHANNELS, ctypes.byref(err)
        )
        if err.value != 0:
            raise RuntimeError(f"opus_decoder_create failed: {err.value}")
        logger.info(f"[OPUS] Decoder ініціалізовано ({_OPUS_DECODE_RATE // 1000}kHz output — нативний libopus)")
    return _decoder


def encode(pcm_24khz_s16: bytes) -> list[bytes]:
    """
    PCM signed 16-bit 24kHz → список raw Opus frames.

    Накопичує вхід у буфері, повертає готові фрейми коли є 960 семплів @ 48kHz.
    Повертає порожній список якщо даних ще недостатньо.
    """
    global _encode_buf, _upsample_zi
    _encode_buf.extend(pcm_24khz_s16)

    enc = _get_encoder()
    frames = []

    # 480 семплів @ 24kHz = 960 байт = один 20мс фрейм при ресемплінгу до 48kHz
    while len(_encode_buf) >= 960:
        chunk_s16 = bytes(_encode_buf[:960])
        del _encode_buf[:960]

        # Stateful 2x upsample 24kHz → 48kHz:
        # Zero-insertion + stateful FIR LP (лінійна фаза — без шипіння на сибілянтах).
        arr = np.frombuffer(chunk_s16, dtype="<i2").astype(np.float64)
        interleaved = np.zeros(len(arr) * 2, dtype=np.float64)
        interleaved[::2] = arr  # оригінальні семпли на парних позиціях
        if _upsample_zi is None:
            _upsample_zi = _UP_FIR_ZI_TEMPLATE * interleaved[0]
        filtered, _upsample_zi = lfilter(_UP_FIR, [1.0], interleaved, zi=_upsample_zi)
        pcm_48k = np.clip(filtered * 1.0, -32768, 32767).astype("<i2")  # *1.5 компенсує вставку нулів (знижено гучність)

        # Zero-copy передача в libopus через ctypes pointer (без tolist())
        pcm_arr = np.ascontiguousarray(pcm_48k, dtype=np.int16)
        pcm_ptr = pcm_arr.ctypes.data_as(ctypes.POINTER(ctypes.c_int16))
        out_buf = (ctypes.c_ubyte * _OPUS_MAX_PACKET)()
        n = libopus.opus_encode(enc, pcm_ptr, _OPUS_FRAME_SAMPLES, out_buf, _OPUS_MAX_PACKET)

        if n > 0:
            frames.append(bytes(out_buf[:n]))

    return frames


def decode(opus_bytes: bytes) -> bytes:
    """
    Raw Opus frame bytes → PCM signed 16-bit 16kHz.

    Пряма конвертація: libopus → s16 16kHz, без проміжної u8 деградації.
    Bypass pmin буфер — дані йдуть через _opus_recv_queue напряму в Gemini.
    Повертає 640 байт (320 семплів × 2 байти @ 16kHz @ 20ms).
    """
    dec = _get_decoder()

    inp = (ctypes.c_ubyte * len(opus_bytes))(*opus_bytes)
    pcm_ct = (ctypes.c_int16 * _OPUS_DECODE_SAMPLES)()
    n = libopus.opus_decode(dec, inp, len(opus_bytes), pcm_ct, _OPUS_DECODE_SAMPLES, 0)

    if n <= 0:
        return b"\x00" * (_OPUS_DECODE_SAMPLES * 2)  # тиша (s16 neutral = 0)

    return np.ctypeslib.as_array(pcm_ct[:n]).astype("<i2").tobytes()


def clear_encode_buffer() -> None:
    """Очистити буфер енкодера (при barge-in, щоб старе аудіо не потрапило в наступну репліку)."""
    global _encode_buf
    _encode_buf.clear()


def reset() -> None:
    """Скинути стан encoder/decoder та буфер між дзвінками."""
    global _encoder, _decoder, _encode_buf, _upsample_zi
    _upsample_zi = None
    if _encoder is not None:
        libopus.opus_encoder_destroy(_encoder)
        _encoder = None
    if _decoder is not None:
        libopus.opus_decoder_destroy(_decoder)
        _decoder = None
    _encode_buf.clear()
    logger.info("[OPUS] Стан скинуто")
