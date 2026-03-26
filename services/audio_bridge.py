"""
Конвертація аудіо між форматами SIP (pyVoIP) та Gemini Live API.

pyVoIP read/write формат: unsigned 8-bit PCM, 8 kHz, mono
  - parse_pcmu: audioop.ulaw2lin(..., 1) + bias(+128) → unsigned 8-bit
  - parse_pcma: audioop.alaw2lin(..., 1) + bias(+128) → unsigned 8-bit
  - encode_pcmu/pcma (patched): passthrough — байти вже готові

Gemini вхід:   signed 16-bit PCM, 16 kHz, mono
Gemini вихід:  signed 16-bit PCM, 24 kHz, mono

SIP → Gemini:
  unsigned-8bit → signed-16bit → resample_poly 8→16kHz → HP 80Hz → pre-emphasis → Noise gate → AGC → NR + comfort noise

Gemini → SIP:
  Butterworth LP (anti-alias) → decimate 24→8kHz → Smooth norm → G.711 A/µ-law

audioop-lts: pip install audioop-lts (сумісна заміна audioop для Python 3.13+)
scipy + numpy: pip install scipy numpy
"""

import audioop
import logging
import time

import noisereduce as nr
import numpy as np
import webrtcvad
from scipy.signal import butter, sosfilt, sosfilt_zi, resample_poly

_log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Gemini → SIP: LP фільтр (anti-aliasing перед децимацією)
# Butterworth 8-го порядку, зріз 3400 Hz — стандарт телефонії G.711.
# ---------------------------------------------------------------------------
_LP_SOS = butter(8, 3400 / (24000 / 2), btype="low", output="sos")

# ---------------------------------------------------------------------------
# SIP → Gemini: HP фільтр (DC offset + мережевий гул 50 Гц)
# Butterworth 4-го порядку, зріз 80 Hz @ 16kHz.
# Видаляє DC зсув та гармоніки мережі (50/100 Гц), не чіпає голос (>300 Гц).
# ---------------------------------------------------------------------------
_HP_SOS = butter(4, 80 / (16000 / 2), btype="high", output="sos")

# ---------------------------------------------------------------------------
# SIP → Gemini: Smooth AGC
#
# Підтягує тихих пацієнтів до цільового RMS.
# Під час тиші/шуму (rms < NOISE_FLOOR) — gain плавно повертається до 1.0.
# Alpha = 0.05 → ~20 chunks (~400 мс) для повної адаптації.
# ---------------------------------------------------------------------------
_NOISE_FLOOR_RMS = 100    # нижче цього порогу — тиша/шум, не підсилюємо
_TARGET_RMS_IN   = 4000   # цільовий RMS на вході (16-bit signed, max = 32768)
_MAX_GAIN_IN     = 5.0    # захист від надмірного підсилення шуму (знижено з 8.0)
_AGC_ALPHA       = 0.05   # швидкість адаптації (0 = ніколи, 1 = миттєво)
_NOISE_GATE_RMS  = 250    # soft noise gate: нижче цього — плавне згасання (не підсилюємо фон)
_COMFORT_NOISE_RMS = 40   # RMS comfort noise після NR (запобігає мертвій тиші)

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
_zi_hp_input: np.ndarray | None = None       # стан HP фільтра (SIP→Gemini)
_input_gain  = [1.0]   # поточний gain AGC (список для mutable closure)
_output_gain = [1.0]   # поточний gain нормалізації виходу
_pre_emphasis_prev = [0.0]  # pre-emphasis: попередній семпл x[n-1]
_last_out_sample = [0.0]    # G.711 fade-out: останній семпл виходу

# ---------------------------------------------------------------------------
# AEC — Acoustic Echo Cancellation
#
# Відомо що відіграли через динамік (reference). Ехо = затримана копія.
# Block-NLMS адаптивно оцінює і віднімає ехо з мікрофонного сигналу.
#
# Reference tap : gemini_output_to_sip() → 8kHz PCM → _aec_ref_buf
# AEC apply     : sip_to_gemini_input()  → 8kHz mic → subtract echo
#
# Параметри (телефонія 8kHz):
#   512 тап @ 8kHz = 64мс room IR — покриває speakerphone reverb
#   μ = 0.02 — стабільна конвергенція за ~2с, не розходиться
#   Затримка 0–400мс — SIP RTT + акустичний шлях
#   Double-talk guard: mic >> reference → не оновлюємо ваги фільтра
# ---------------------------------------------------------------------------
_AEC_FILTER_LEN   = 512    # тапів @ 8kHz = 64мс
_AEC_MU           = 0.02   # NLMS крок адаптації
_AEC_EPS          = 1e-6   # регуляризація
_AEC_MAX_DELAY    = 3200   # макс. затримка: 400мс @ 8kHz
_AEC_REF_CAPACITY = 16000  # кільцевий буфер: 2с @ 8kHz
_AEC_TAIL_SEC     = 1.5    # AEC активний Xс після закінчення мови AI
_AEC_DT_RATIO     = 2.5    # double-talk: mic_rms / ref_rms > X → пауза адаптації
_AEC_REEST_INT    = 24000  # переоцінка затримки кожні 3с (семпли @ 8kHz)
_AEC_FIRST_EST    = 8000   # перша оцінка після 1с накопичення reference

_aec_ref_buf    = np.zeros(_AEC_REF_CAPACITY, dtype=np.float64)
_aec_ref_write  = [0]     # позиція запису (modulo _AEC_REF_CAPACITY)
_aec_ref_total  = [0]     # загальна кількість записаних семплів
_aec_weights    = np.zeros(_AEC_FILTER_LEN, dtype=np.float64)
_aec_delay      = [320]   # затримка ехо в семплах (початк. 40мс @ 8kHz)
_aec_reest_ctr  = [0]     # лічильник до наступної переоцінки
_aec_first_done = [False] # True після першої оцінки затримки
_aec_last_ref_t = [0.0]   # monotonic time останнього reference push

# ---------------------------------------------------------------------------
# Noise reduction: профіль шуму лінії
#
# Перші _NR_PROFILE_CHUNKS чанків (~640мс) — збираємо тільки тихі фрейми
# (VAD-перевірка). Якщо пацієнт відповів одразу — його голос не потрапить
# до профілю і NR не буде гасити мову.
# Після збору — застосовуємо stationary noise reduction до кожного чанку.
# ---------------------------------------------------------------------------
_NR_PROFILE_CHUNKS = 40      # вікно збору: ~640мс (час-лічильник, не кількість тихих)
_NR_PROP_DECREASE  = 0.35    # наскільки придушувати шум (0.0–1.0); знижено з 0.75 → менше спотворення мови

_nr_chunk_count: int = 0
_nr_profile_buf: list[np.ndarray] = []
_nr_profile: np.ndarray | None = None  # конкатенований noise profile
_nr_collect_enabled: bool = False      # True після greeting → збирати шумовий профіль

# Adaptive NR refresh: оновлення профілю під час тривалої тиші
_NR_REFRESH_SILENCE = 60      # ~1с тиші перед початком оновлення профілю
_NR_REFRESH_CHUNKS  = 20      # тихих чанків для нового профілю
_nr_silence_count: int = 0
_nr_refresh_buf: list[np.ndarray] = []

# VAD для фільтрації профілю: агресивність 2 (достатньо для відокремлення голосу від шуму)
_profile_vad = webrtcvad.Vad(2)

# Кодек: встановлюється з binotel_sip.py після SIP-узгодження
_use_alaw:  bool = False
_use_opus:  bool = False   # True → Gemini output кодується через opus_codec, не G.711


_sip_input_log_done = False  # лог лише один раз про режим вхідного аудіо


def _aec_push_ref(samples: np.ndarray) -> None:
    """Записати reference семпли (те що відіграли AI) у кільцевий буфер."""
    n = len(samples)
    if n == 0:
        return
    pos = _aec_ref_write[0]
    space = _AEC_REF_CAPACITY - pos
    if n <= space:
        _aec_ref_buf[pos:pos + n] = samples
    else:
        _aec_ref_buf[pos:] = samples[:space]
        _aec_ref_buf[:n - space] = samples[space:]
    _aec_ref_write[0] = (pos + n) % _AEC_REF_CAPACITY
    _aec_ref_total[0] += n
    _aec_last_ref_t[0] = time.monotonic()


def _aec_get_ref(delay: int, length: int) -> np.ndarray:
    """Читати 'length' семплів reference, що закінчуються за 'delay' семплів до write pos."""
    end = (_aec_ref_write[0] - delay) % _AEC_REF_CAPACITY
    start = (end - length) % _AEC_REF_CAPACITY
    if start < end:
        return _aec_ref_buf[start:end].copy()
    return np.concatenate([_aec_ref_buf[start:], _aec_ref_buf[:end]])


def _aec_estimate_delay(mic: np.ndarray) -> int:
    """Cross-correlation між mic і reference → оцінка затримки ехо."""
    n = len(mic)
    available = _aec_ref_total[0] - _AEC_FILTER_LEN
    search = min(_AEC_MAX_DELAY, max(0, available - n))
    if search < 64:
        return _aec_delay[0]
    try:
        ref_long = _aec_get_ref(0, search + n)
        corr = np.abs(np.correlate(mic, ref_long, 'valid'))
        new_d = int(np.argmax(corr))
        new_d = max(0, min(new_d, _AEC_MAX_DELAY))
        # Exponential smoothing — захист від одноразових викидів
        smoothed = int(round(0.75 * _aec_delay[0] + 0.25 * new_d))
        _log.debug(f"[AEC] Delay {_aec_delay[0]}→{smoothed} сем ({smoothed * 125 // 1000}мс)")
        return smoothed
    except Exception:
        return _aec_delay[0]


def _aec_apply(mic: np.ndarray) -> np.ndarray:
    """
    Block-NLMS Acoustic Echo Cancellation @ 8kHz. Повністю векторизований.

    Алгоритм:
      1. Echo estimate = convolve(reference[delay:], weights)
      2. Error = mic - echo_estimate
      3. Double-talk check (mic >> reference → пауза адаптації)
      4. Block NLMS update: weights += (μ/N) * correlate(ref, e)[::-1] / norm

    Математика:
      Convolution (valid):  y[i] = Σ_k w[k] * ref[i+M-1-k]  (FIR filter)
      Correlation gradient: grad[m] = Σ_i e[i] * ref[i+M-1-m] = corr(ref,e,'valid')[::-1][m]

    Returns: float64 array — мікрофонний сигнал без ехо AI.
    """
    global _aec_weights
    n = len(mic)
    if n == 0:
        return mic

    # Переоцінка затримки
    _aec_reest_ctr[0] += n
    _threshold = _AEC_FIRST_EST if not _aec_first_done[0] else _AEC_REEST_INT
    if _aec_reest_ctr[0] >= _threshold:
        _aec_reest_ctr[0] = 0
        _aec_first_done[0] = True
        _aec_delay[0] = _aec_estimate_delay(mic)

    delay = _aec_delay[0]
    M = _AEC_FILTER_LEN
    needed = M + n - 1  # для 'valid' convolution

    if _aec_ref_total[0] < needed + delay:
        return mic  # замало reference — bypass без обробки

    ref = _aec_get_ref(delay, needed)  # M+n-1 семплів

    # Echo estimate (повністю векторизований, використовує numpy BLAS)
    # np.convolve(ref, w, 'valid')[i] = Σ_k ref[i+k] * w[M-1-k] = Σ_k w[k] * ref[i+M-1-k] = y[i] ✓
    echo_est = np.convolve(ref, _aec_weights, mode='valid')  # length = n
    e = mic - echo_est

    # Double-talk detection: якщо пацієнт говорить гучніше ніж ехо → не псуємо ваги
    ref_rms = float(np.sqrt(np.mean(ref[M - 1:] ** 2) + 1e-9))
    mic_rms = float(np.sqrt(np.mean(mic ** 2) + 1e-9))
    double_talk = (mic_rms > _AEC_DT_RATIO * ref_rms) or (ref_rms < 50.0)

    if not double_talk:
        # Block NLMS weight update (vectorized)
        # grad[m] = correlate(ref, e, 'valid')[M-1-m] → reversed output
        grad = np.correlate(ref, e, mode='valid')[::-1]  # length = M ✓
        norm = np.dot(ref, ref) + _AEC_EPS * len(ref)
        _aec_weights = _aec_weights + (_AEC_MU / n) * (grad / norm)

    return e


def sip_to_gemini_input(pcm: bytes) -> bytes:
    """
    Аудіо від пацієнта → PCM signed 16-bit 16 kHz (Gemini вхід).

    Opus mode: вхід s16 16kHz (з _opus_recv_queue, bypass pmin) → AGC → NR.
      Без u8↔s16 конвертації — пряма передача без деградації.
    G.711 mode: вхід u8 8kHz (з pmin) → bias+lin2lin → resample 8→16kHz → AGC → NR.
    """
    global _sip_input_log_done, _zi_hp_input

    if _use_opus:
        # Opus: вхід вже s16 16kHz — пропускаємо bias/lin2lin/resample
        if not _sip_input_log_done:
            _sip_input_log_done = True
            import logging as _logging
            _logging.getLogger(__name__).info(
                "[BRIDGE] SIP→Gemini: s16 16kHz Opus direct (без u8 деградації, спектр 0-8kHz)"
            )
        arr = np.frombuffer(pcm, dtype="<i2").astype(np.float64)
        # AEC @ 8kHz: Opus mic 16kHz → 8kHz → AEC → 16kHz
        if time.monotonic() - _aec_last_ref_t[0] < _AEC_TAIL_SEC and _aec_ref_total[0] > _AEC_FILTER_LEN:
            arr_8k = arr[::2]  # 16→8kHz (голос < 4kHz, аліасинг не критичний)
            arr_8k = _aec_apply(arr_8k)
            arr = resample_poly(arr_8k, 2, 1)  # 8→16kHz
        resampled = arr
    else:
        # G.711: u8 8kHz → s16 → resample 8→16kHz
        if not _sip_input_log_done:
            _sip_input_log_done = True
            import logging as _logging
            _logging.getLogger(__name__).info(
                "[BRIDGE] SIP→Gemini: 8kHz G.711 → resample→16kHz (спектр 0-4kHz)"
            )
        s8  = audioop.bias(pcm, 1, -128)
        s16 = audioop.lin2lin(s8, 1, 2)
        arr = np.frombuffer(s16, dtype="<i2").astype(np.float64)
        # AEC @ 8kHz: застосовуємо до сирого 8kHz сигналу перед ресемплом
        if time.monotonic() - _aec_last_ref_t[0] < _AEC_TAIL_SEC and _aec_ref_total[0] > _AEC_FILTER_LEN:
            arr = _aec_apply(arr)
        resampled = resample_poly(arr, up=2, down=1)

    # Крок 2.5: HP фільтр — DC offset + гул мережі 50 Гц
    if len(resampled) > 0:
        if _zi_hp_input is None:
            _zi_hp_input = sosfilt_zi(_HP_SOS) * resampled[0]
        resampled, _zi_hp_input = sosfilt(_HP_SOS, resampled, zi=_zi_hp_input)

    # Крок 2.7: Pre-emphasis ВИМКНЕНО — Gemini натренований на природну мову,
    # pre-emphasis спотворює спектр і погіршує розпізнавання (напр. "бажаю" → "вашає").
    # if len(resampled) > 0:
    #     pre = np.empty_like(resampled)
    #     pre[0] = resampled[0] - 0.97 * _pre_emphasis_prev[0]
    #     pre[1:] = resampled[1:] - 0.97 * resampled[:-1]
    #     _pre_emphasis_prev[0] = resampled[-1]
    #     resampled = pre

    # Крок 3: Noise gate + Smooth AGC
    rms = float(np.sqrt(np.mean(resampled ** 2))) if len(resampled) else 0.0
    # Noise gate: плавне згасання низькорівневого сигналу (фон/шум лінії).
    # Квадратична рампа: RMS 100 → gate 0.16, RMS 200 → gate 0.64, RMS 250 → gate 1.0.
    if 0 < rms < _NOISE_GATE_RMS:
        gate = (rms / _NOISE_GATE_RMS) ** 2
        resampled = resampled * gate
        rms *= gate
    if rms > _NOISE_FLOOR_RMS:
        # Сигнал — адаптуємо gain до цільового RMS
        target = min(_TARGET_RMS_IN / rms, _MAX_GAIN_IN)
        _input_gain[0] = (1.0 - _AGC_ALPHA) * _input_gain[0] + _AGC_ALPHA * target
    else:
        # Тиша/шум — плавно повертаємо gain до нейтрального
        _input_gain[0] = (1.0 - _AGC_ALPHA) * _input_gain[0] + _AGC_ALPHA * 1.0

    result = resampled * _input_gain[0]

    # Noise reduction: stationary spectral gating по профілю шуму лінії
    global _nr_chunk_count, _nr_profile, _nr_profile_buf, _nr_silence_count, _nr_refresh_buf
    if not _nr_collect_enabled:
        # Profiling ще не увімкнено (greeting не завершено) — пропускаємо NR
        return np.clip(result, -32768, 32767).astype("<i2").tobytes()
    if _nr_chunk_count < _NR_PROFILE_CHUNKS:
        _nr_chunk_count += 1

        # VAD-фільтр: додаємо до профілю тільки тихі фрейми.
        # Якщо пацієнт відповів одразу — його голос не псує профіль.
        # webrtcvad потребує рівно 20мс @ 16kHz = 640 байт s16.
        pcm_s16 = np.clip(result, -32768, 32767).astype("<i2").tobytes()
        if len(pcm_s16) == 640:
            try:
                if not _profile_vad.is_speech(pcm_s16, 16000):
                    _nr_profile_buf.append(result.copy())
            except Exception:
                _nr_profile_buf.append(result.copy())  # fallback: додаємо як є
        else:
            _nr_profile_buf.append(result.copy())  # нестандартний розмір: додаємо без VAD

        if _nr_chunk_count == _NR_PROFILE_CHUNKS:
            if _nr_profile_buf:
                _nr_profile = np.concatenate(_nr_profile_buf)
                _log.debug(
                    f"[BRIDGE] NR profile: {len(_nr_profile_buf)}/{_NR_PROFILE_CHUNKS} тихих фреймів"
                )
            else:
                _log.warning("[BRIDGE] NR profile: 0 тихих фреймів — NR вимкнено для цього дзвінка")
            _nr_profile_buf.clear()
    elif _nr_profile is not None:
        # Adaptive refresh: оновлюємо NR профіль під час тривалої тиші
        _refresh_pcm = np.clip(result, -32768, 32767).astype("<i2").tobytes()
        _is_silent = False
        if len(_refresh_pcm) == 640:
            try:
                _is_silent = not _profile_vad.is_speech(_refresh_pcm, 16000)
            except Exception:
                pass
        if _is_silent:
            _nr_silence_count += 1
            if _nr_silence_count >= _NR_REFRESH_SILENCE:
                _nr_refresh_buf.append(result.copy())
                if len(_nr_refresh_buf) >= _NR_REFRESH_CHUNKS:
                    _nr_profile = np.concatenate(_nr_refresh_buf)
                    _nr_refresh_buf.clear()
                    _nr_silence_count = 0
                    _log.debug("[BRIDGE] NR profile оновлено (adaptive refresh)")
        else:
            _nr_silence_count = 0
            _nr_refresh_buf.clear()
        try:
            result = nr.reduce_noise(
                y=result.astype(np.float32),
                sr=16000,
                y_noise=_nr_profile.astype(np.float32),
                stationary=True,
                prop_decrease=_NR_PROP_DECREASE,
            )
        except Exception:
            pass  # fallback: без шумопоглинання

    # Comfort noise: тихий шум після NR щоб лінія не була мертвою.
    # RMS 40 повністю маскується голосом (RMS 3000+) але заповнює тишу.
    if _nr_profile is not None:
        result = np.asarray(result, dtype=np.float64)
        result += np.random.randn(len(result)) * _COMFORT_NOISE_RMS

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

    _last_out_sample[0] = float(decimated[-1]) if len(decimated) else 0.0
    ref_clip = np.clip(decimated, -32768, 32767)
    _aec_push_ref(ref_clip)  # AEC: тапаємо reference (те що відіграємо пацієнту)
    pcm_8khz = ref_clip.astype("<i2").tobytes()

    # G.711 encoding
    if _use_alaw:
        return audioop.lin2alaw(pcm_8khz, 2)
    return audioop.lin2ulaw(pcm_8khz, 2)


def set_codec_alaw(is_alaw: bool) -> None:
    """Встановити кодек: True = PCMA (A-law), False = PCMU (µ-law)."""
    global _use_alaw
    _use_alaw = is_alaw


def set_codec_opus(enabled: bool) -> None:
    """Увімкнути/вимкнути Opus режим (output через opus_codec замість G.711)."""
    global _use_opus
    _use_opus = enabled
    import logging
    logging.getLogger(__name__).info(f"[BRIDGE] Codec → {'Opus' if enabled else 'G.711'}")


def generate_fadeout_g711(fade_ms: int = 10) -> bytes:
    """G.711 fade-out від останнього семпла до нуля (запобігає кліку при barge-in)."""
    samples = int(8000 * fade_ms / 1000)  # 8kHz
    if abs(_last_out_sample[0]) < 10:
        return b""  # вже біля нуля — клік не буде
    ramp = np.linspace(_last_out_sample[0], 0.0, samples)
    pcm = np.clip(ramp, -32768, 32767).astype("<i2").tobytes()
    _last_out_sample[0] = 0.0
    if _use_alaw:
        return audioop.lin2alaw(pcm, 2)
    return audioop.lin2ulaw(pcm, 2)


def apply_plc_decay(chunk: bytes, decay: float) -> bytes:
    """Повторити PCM chunk зі зменшеною амплітудою (PLC при втраті пакетів)."""
    arr = np.frombuffer(chunk, dtype="<i2").astype(np.float64) * decay
    return np.clip(arr, -32768, 32767).astype("<i2").tobytes()


def enable_noise_profiling() -> None:
    """Увімкнути збір шумового профілю (викликати після greeting)."""
    global _nr_collect_enabled
    _nr_collect_enabled = True
    _log.debug("[BRIDGE] Noise profiling enabled")


def reset_state() -> None:
    """Скинути стан ресемплерів, фільтрів та noise profile між дзвінками."""
    global _zi_gemini_to_sip, _zi_hp_input, _nr_chunk_count, _nr_profile, _nr_profile_buf, _use_opus, _use_alaw, _sip_input_log_done, _nr_collect_enabled, _nr_silence_count, _nr_refresh_buf, _aec_ref_write, _aec_ref_total, _aec_weights, _aec_delay, _aec_reest_ctr, _aec_first_done, _aec_last_ref_t
    _zi_gemini_to_sip = None
    _zi_hp_input = None
    _input_gain[0]  = 1.0
    _output_gain[0] = 1.0
    _pre_emphasis_prev[0] = 0.0
    _last_out_sample[0] = 0.0
    _nr_chunk_count = 0
    _nr_profile_buf.clear()
    _nr_profile = None
    _nr_collect_enabled = False
    _nr_silence_count = 0
    _nr_refresh_buf.clear()
    _use_opus  = False
    _use_alaw  = False
    _sip_input_log_done = False
    _aec_ref_buf[:] = 0.0
    _aec_ref_write[0] = 0
    _aec_ref_total[0] = 0
    _aec_weights[:] = 0.0
    _aec_delay[0] = 320
    _aec_reest_ctr[0] = 0
    _aec_first_done[0] = False
    _aec_last_ref_t[0] = 0.0
