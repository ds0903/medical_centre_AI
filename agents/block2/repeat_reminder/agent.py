"""
Модуль 2.1 — AI-нагадування про повторні візити.

Архітектура ідентична Block1Agent (agents/block1/agent.py):
  Gemini Live API (native audio) + Binotel SIP аудіо-міст.

Відмінності:
  - Контекст: попередній візит + рекомендація лікаря
  - Інструменти: book_new_visit, decline_with_reason, mark_self_book
  - Результати: booked | declined | self_book | no_answer | no_result
  - Немає transfer_to_operator
"""

import asyncio
import logging
import math
import struct
import time

try:
    import webrtcvad as _webrtcvad
    _VAD = _webrtcvad.Vad(2)
    _VAD_AVAILABLE = True
except ImportError:
    _VAD = None
    _VAD_AVAILABLE = False

try:
    import pyaudio
    _PYAUDIO_AVAILABLE = True
except ImportError:
    pyaudio = None
    _PYAUDIO_AVAILABLE = False

from google import genai
from google.genai import types
from google.genai.errors import APIError
from websockets.exceptions import ConnectionClosedError

from agents.block2.repeat_reminder.prompts import build_system_prompt
from agents.block2.repeat_reminder.tools import TOOLS, handle_tool_call
from config import (
    GEMINI_MODEL, DEBUG, VERTEX_PROJECT, VERTEX_LOCATION,
    SILENCE_TIMEOUT_SEC, SILENCE_START_DELAY_SEC, WATCHDOG_TIMEOUT_SEC,
    INTERRUPTED_PAUSE_SEC, GEMINI_LOGS, LOG_FILE, CALL_TIMEOUT_SEC,
    GEMINI_API_KEY,
)

logger = logging.getLogger(__name__)

FORMAT   = pyaudio.paInt16 if _PYAUDIO_AVAILABLE else 8
CHANNELS = 1
INPUT_RATE  = 16000
OUTPUT_RATE = 24000
CHUNK = 512

VAD_THRESHOLD   = 300
MAX_SILENCE_COUNT = 3
_VAD_FRAME_BYTES  = 320


def _calc_echo_gate(audio_bytes: int) -> float:
    audio_sec = audio_bytes / 48000
    return min(0.3 + min(audio_sec, 2.5) * 0.28, INTERRUPTED_PAUSE_SEC)


def _is_voice_rms(pcm: bytes) -> bool:
    n = len(pcm) // 2
    if n == 0:
        return False
    samples = struct.unpack(f"<{n}h", pcm[:n * 2])
    rms = math.sqrt(sum(s * s for s in samples) / n)
    return rms > VAD_THRESHOLD


def _build_slots_hint(raw_slots: list | None) -> tuple[str | None, dict | None]:
    """Форматує доступні слоти у підказку для Gemini + pre-built cache entry."""
    if raw_slots is None:
        return None, None
    if not raw_slots:
        hint = (
            "[SYSTEM_SLOTS] Вільних слотів наразі немає.\n"
            "Якщо пацієнт погоджується записатись — повідом що наразі вільного часу немає "
            "і запропонуй зателефонувати в клініку або записатись самостійно через mark_self_book."
        )
        no_slots_result = {
            "success": False,
            "available_dates": [],
            "message": (
                "Вільних слотів немає. "
                "Запропонуй пацієнту самостійно зателефонувати в клініку або виклич mark_self_book."
            ),
        }
        return hint, {"result": no_slots_result, "_ts": time.monotonic()}

    try:
        from agents.block2.repeat_reminder.tools import (
            _ua_ordinal, _MONTHS_UA, _WEEKDAYS_UA, _format_hour_ua,
        )
        from datetime import datetime as _dt

        from collections import OrderedDict as _OD
        grouped: _OD = _OD()
        slots_list = []
        count = 0
        for item in raw_slots:
            if count >= 5:
                break
            d = _dt.fromisoformat(item["START_E"])
            if d.weekday() >= 5:
                continue
            date_str = f"{_ua_ordinal(d.day)} {_MONTHS_UA[d.month - 1]}"
            hour_str = _format_hour_ua(d.hour, d.minute)
            grouped.setdefault(date_str, []).append({"hour_ua": hour_str, "slot_id": str(item["ID"])})
            slots_list.append({"slot_id": str(item["ID"]), "date": date_str, "time": f"{d.hour}:{d.minute:02d}"})
            count += 1

        if not slots_list:
            return None, None

        date_phrases = []
        for date_str, date_slots in grouped.items():
            times_ua = " і о ".join(s["hour_ua"] for s in date_slots)
            date_phrases.append(f"{date_str} о {times_ua}")
        labels = ", ".join(date_phrases)

        hint = (
            f"[SYSTEM_CACHE] get_available_slots() = {labels}\n"
            "Це технічний кеш — НЕ слова пацієнта. МОВЧИ. Чекай першої репліки пацієнта."
        )
        cache_result = {
            "success": True,
            "message": (
                f"Скажи пацієнту ДОСЛІВНО: «Є вільний час: {labels}. Який вам зручний?» "
                "Коли пацієнт обере — виклич book_new_visit(slot_id='...') з відповідним slot_id."
            ),
            "slots": slots_list,
        }
        return hint, {"result": cache_result, "_ts": time.monotonic()}
    except Exception as _e:
        logger.debug(f"[AGENT] _build_slots_hint failed: {_e!r}")
        return None, None


class _DebugCall:
    """Mock BinotelCall для debug режиму (мікрофон/динамік)."""

    def __init__(self, pa) -> None:
        self.is_active = True
        self._out = pa.open(format=FORMAT, channels=CHANNELS, rate=OUTPUT_RATE, output=True)
        self._inp = pa.open(
            format=FORMAT, channels=CHANNELS, rate=INPUT_RATE,
            input=True, frames_per_buffer=CHUNK,
        )

    async def send_audio(self, pcm: bytes) -> None:
        if self.is_active:
            loop = asyncio.get_event_loop()
            await loop.run_in_executor(None, self._out.write, pcm)

    async def receive_audio(self):
        loop = asyncio.get_event_loop()
        while self.is_active:
            data = await loop.run_in_executor(
                None, lambda: self._inp.read(CHUNK, exception_on_overflow=False)
            )
            yield data

    def flush_audio(self) -> None:
        pass

    def flush_encode_buffer(self) -> None:
        pass

    async def hangup(self) -> None:
        self.is_active = False

    def close(self) -> None:
        self._out.stop_stream()
        self._out.close()
        self._inp.stop_stream()
        self._inp.close()


class RepeatReminderAgent:
    """
    Голосовий агент Модуля 2.1 — нагадування про повторний візит.

    Gemini Live API (native audio) + Binotel SIP аудіо-міст.
    """

    def __init__(self, visit_context: dict) -> None:
        self.visit = visit_context
        self.client = genai.Client(
            vertexai=True,
            project=VERTEX_PROJECT,
            location=VERTEX_LOCATION,
        )
        self.call_result: str | None = None
        self.call_duration: float = 0.0
        self.call_commentary: str = ""
        self._pa = pyaudio.PyAudio() if _PYAUDIO_AVAILABLE else None
        self._end_event = asyncio.Event()
        self._monitor_task: asyncio.Task | None = None
        self._resumption_handle: str | None = None
        self._session_reconnected: bool = False

    # ------------------------------------------------------------------
    # Конфіг Gemini сесії
    # ------------------------------------------------------------------

    def _build_config(self, resume_handle: str | None = None) -> types.LiveConnectConfig:
        if not hasattr(self, "_cached_prompt"):
            self._cached_prompt = build_system_prompt(self.visit)
        prompt = self._cached_prompt
        return types.LiveConnectConfig(
            response_modalities=["AUDIO"],
            speech_config=types.SpeechConfig(
                voice_config=types.VoiceConfig(
                    prebuilt_voice_config=types.PrebuiltVoiceConfig(voice_name="Zephyr")
                ),
                language_code="uk-UA",
            ),
            system_instruction=types.Content(parts=[types.Part(text=prompt)]),
            tools=TOOLS,
            thinking_config=types.ThinkingConfig(thinking_budget=0),
            output_audio_transcription=types.AudioTranscriptionConfig() if GEMINI_LOGS else None,
            input_audio_transcription=types.AudioTranscriptionConfig(),
            realtime_input_config=types.RealtimeInputConfig(
                automatic_activity_detection=types.AutomaticActivityDetection(
                    start_of_speech_sensitivity=types.StartSensitivity.START_SENSITIVITY_LOW,
                    end_of_speech_sensitivity=types.EndSensitivity.END_SENSITIVITY_HIGH,
                    silence_duration_ms=600,
                )
            ),
            session_resumption=types.SessionResumptionConfig(
                handle=resume_handle,
                transparent=True,
            ),
        )

    # ------------------------------------------------------------------
    # DEBUG режим
    # ------------------------------------------------------------------

    async def run_debug(self) -> str:
        logger.info("[DEBUG 2.1] Симуляція дзвінка — мікрофон/динамік")
        call = _DebugCall(self._pa)
        try:
            return await self.run_production(call)
        except KeyboardInterrupt:
            return self.call_result or "no_result"
        finally:
            call.close()

    # ------------------------------------------------------------------
    # PRODUCTION режим: Binotel SIP ↔ Gemini Live
    # ------------------------------------------------------------------

    async def run_production(self, binotel_call) -> str:
        logger.info("=" * 55)
        logger.info("[AGENT] Повторний візит — дзвінок через Binotel")
        logger.info(f"  Пацієнт    : {self.visit.get('patient_name')}")
        logger.info(f"  Рекомендація: {self.visit.get('doctor_recommendation')}")
        logger.info(f"  Лікар      : {self.visit.get('doctor_name')}")
        logger.info("=" * 55)

        start = time.time()
        greeting_buffer = getattr(self, "_greeting_buffer", [])
        greeting_duration = getattr(self, "_greeting_duration", 0.0)
        _stale_warmup = [False]

        async def _stream(session) -> None:
            _greeting_done = asyncio.Event()
            _patient_spoke = asyncio.Event()
            _silence_count = 0
            _silence_task: asyncio.Task | None = None
            _timer_task: asyncio.Task | None = None
            _call_cache: dict = {}

            _raw_prefetch = getattr(self, "_prefetched_raw_slots", None)
            if _raw_prefetch is not None:
                _call_cache["__raw_slots"] = _raw_prefetch

            _activity = [time.time()]
            _interrupted_pause_until = [0.0]
            _suppress_next_audio = [False]
            _suppress_set_at = [0.0]
            _ai_turn_active = [False]
            _ai_turn_started_at = [0.0]
            _last_patient_said_text = [""]
            _last_ai_said = [""]
            _last_patient_said_time = [0.0]
            _patient_ever_spoke = [False]
            _patient_said_count = [0]
            _real_vad_pending = [False]
            _date_slots_fetched = [False]
            _allow_next_turn = [False]
            _last_ai_turn_end = [0.0]
            _suppress_timeout_fired = [False]

            _MAX_RECONNECTS = 3
            _live_session = [session]
            _live_cm = [None]
            _reconnecting = [False]
            _reconnected_event = asyncio.Event()
            _reconnect_count = [0]
            _reconnect_lock = asyncio.Lock()

            async def _do_reconnect() -> bool:
                async with _reconnect_lock:
                    if _reconnect_count[0] >= _MAX_RECONNECTS:
                        logger.error("[AGENT] Reconnect: вичерпано спроби")
                        return False
                    _reconnect_count[0] += 1
                    logger.warning(f"[AGENT] Reconnect #{_reconnect_count[0]}/{_MAX_RECONNECTS}...")
                    _reconnecting[0] = True
                    _reconnected_event.clear()
                    try:
                        new_cm = self.client.aio.live.connect(
                            model=GEMINI_MODEL,
                            config=self._build_config(resume_handle=self._resumption_handle),
                        )
                        new_session = await new_cm.__aenter__()
                        if _live_cm[0] is not None:
                            try:
                                await asyncio.wait_for(_live_cm[0].__aexit__(None, None, None), timeout=3.0)
                            except Exception:
                                pass
                        _live_session[0] = new_session
                        _live_cm[0] = new_cm
                        logger.info(f"[AGENT] Reconnect #{_reconnect_count[0]} успішний")
                        return True
                    except Exception as _re:
                        logger.error(f"[AGENT] Reconnect провалився: {_re!r}")
                        return False
                    finally:
                        _reconnecting[0] = False
                        _reconnected_event.set()

            async def _check_silence() -> None:
                nonlocal _silence_count
                _patient_spoke.clear()
                try:
                    await asyncio.wait_for(_patient_spoke.wait(), timeout=SILENCE_TIMEOUT_SEC)
                    _silence_count = 0
                except asyncio.TimeoutError:
                    if self._end_event.is_set():
                        return
                    _silence_count += 1
                    logger.info(f"[AGENT] Тиша {_silence_count}/{MAX_SILENCE_COUNT}")
                    if _silence_count >= 2 and not _patient_ever_spoke[0]:
                        logger.warning("[AGENT] One-way audio — завершуємо")
                        self.call_result = "no_result"
                        self._end_event.set()
                        return
                    if _silence_count >= MAX_SILENCE_COUNT:
                        self.call_result = "no_result"
                        self._end_event.set()
                        _suppress_next_audio[0] = False
                        await _live_session[0].send_client_content(
                            turns={"parts": [{"text": "Пацієнт не відповідає. Скажи коротке прощання і виклич end_call(result='no_answer')."}]},
                            turn_complete=True,
                        )
                    elif _silence_count == 1:
                        _suppress_next_audio[0] = False
                        await _live_session[0].send_client_content(
                            turns={"parts": [{"text": "Скажи пацієнту: «Повторіть будь ласка, я не почула вас.» — і мовчки чекай."}]},
                            turn_complete=True,
                        )
                    else:
                        _suppress_next_audio[0] = False
                        await _live_session[0].send_client_content(
                            turns={"parts": [{"text": "Скажи пацієнту: «Я все ще чекаю вашої відповіді.» — і мовчки чекай."}]},
                            turn_complete=True,
                        )

            async def _start_silence_timer(audio_delay: float = 0.0) -> None:
                nonlocal _silence_task
                if _silence_task and not _silence_task.done():
                    _silence_task.cancel()
                if self._end_event.is_set():
                    return
                if _patient_said_count[0] == 0:
                    return
                await asyncio.sleep(audio_delay + SILENCE_START_DELAY_SEC)
                if self._end_event.is_set():
                    return
                _silence_task = asyncio.create_task(_check_silence(), name="silence_monitor")

            def _schedule_silence_timer(audio_delay: float = 0.0) -> None:
                nonlocal _timer_task
                if _timer_task and not _timer_task.done():
                    _timer_task.cancel()
                if not self._end_event.is_set():
                    _timer_task = asyncio.create_task(
                        _start_silence_timer(audio_delay), name="silence_timer_start"
                    )

            _watchdog_injected = [0]

            async def _watchdog() -> None:
                while not self._end_event.is_set():
                    await asyncio.sleep(5)
                    if self._end_event.is_set():
                        break
                    if not _greeting_done.is_set():
                        continue
                    idle = time.time() - _activity[0]
                    if idle < WATCHDOG_TIMEOUT_SEC:
                        continue
                    if _ai_turn_active[0]:
                        if time.time() - _ai_turn_started_at[0] < 3.0:
                            continue
                    _activity[0] = time.time()
                    _watchdog_injected[0] += 1
                    if _watchdog_injected[0] >= 3:
                        logger.warning(f"[AGENT] Watchdog: {idle:.0f}с — завершуємо")
                        self.call_result = "no_result"
                        self._end_event.set()
                        return
                    logger.warning(f"[AGENT] Watchdog: {idle:.0f}с — re-inject")
                    try:
                        _suppress_next_audio[0] = False
                        await _live_session[0].send_client_content(
                            turns={"parts": [{"text": "Скажи пацієнту: «Повторіть будь ласка, я не почула вас.» — і мовчки чекай."}]},
                            turn_complete=True,
                        )
                    except Exception as e:
                        logger.error(f"[AGENT] Watchdog помилка: {e!r}")
                        self._end_event.set()
                        return

            async def _timeout_warning() -> None:
                warn_at = start + CALL_TIMEOUT_SEC - 60
                wait_sec = warn_at - time.time()
                if wait_sec > 0:
                    await asyncio.sleep(wait_sec)
                if self._end_event.is_set():
                    return
                try:
                    _suppress_next_audio[0] = False
                    await _live_session[0].send_client_content(
                        turns={"parts": [{"text": (
                            "УВАГА: до завершення дзвінка залишилась 1 хвилина. "
                            "Скажи пацієнту природно: «Вибачте, нам залишилась хвилинка — "
                            "ви погоджуєтесь записатись чи перетелефонуєте самі?» Чекай відповіді."
                        )}]},
                        turn_complete=True,
                    )
                except Exception as e:
                    logger.error(f"[AGENT] Timeout warning помилка: {e!r}")
                    return
                await asyncio.sleep(60)
                if self._end_event.is_set():
                    return
                logger.warning("[AGENT] Timeout warning: 60с минуло → no_result")
                self.call_result = "no_result"
                self._end_event.set()

            async def _sip_monitor() -> None:
                await _greeting_done.wait()
                while not self._end_event.is_set():
                    await asyncio.sleep(1)
                    if not binotel_call.is_active:
                        logger.warning("[AGENT] SIP скинуто — завершуємо")
                        if self.call_result in (None, "no_result"):
                            if _call_cache.get("book_new_visit", {}).get("success"):
                                self.call_result = "booked"
                                logger.warning("[AGENT] SIP drop: book_new_visit вже виконано → booked")
                            elif _call_cache.get("decline", {}).get("success"):
                                self.call_result = "declined"
                                logger.warning("[AGENT] SIP drop: decline_with_reason вже виконано → declined")
                            elif _call_cache.get("self_book", {}).get("success"):
                                self.call_result = "self_book"
                                logger.warning("[AGENT] SIP drop: mark_self_book вже виконано → self_book")
                            elif _last_patient_said_text[0]:
                                classified = await self._classify_last_phrase(_last_patient_said_text[0])
                                logger.info(f"[AGENT] SIP drop classify: '{_last_patient_said_text[0]}' → {classified}")
                                self.call_result = classified
                            else:
                                self.call_result = "no_result"
                        self._end_event.set()
                        return

            async def _send_patient_audio() -> None:
                _vad_buf = bytearray()
                _noise_rms = [0.0]
                _NOISE_ALPHA = 0.05
                _SNR_RATIO = 2.0
                _snr_filtered_chunks = [0]
                import collections
                _preroll_buf: collections.deque = collections.deque(maxlen=20)
                _was_in_gate = [False]
                _preroll_has_vad = [False]
                _last_speech_ts = [0.0]
                _eot_sent = [False]
                _EARLY_EOT_SEC = 0.50

                async for chunk in binotel_call.receive_audio():
                    if self._end_event.is_set():
                        break
                    if not _greeting_done.is_set():
                        continue
                    if time.time() < _interrupted_pause_until[0]:
                        _preroll_buf.append(chunk)
                        _was_in_gate[0] = True
                        if _is_voice_rms(chunk):
                            _preroll_has_vad[0] = True
                        continue
                    if _was_in_gate[0]:
                        _was_in_gate[0] = False
                        if _preroll_has_vad[0]:
                            for _pre_chunk in list(_preroll_buf):
                                if self._end_event.is_set() or _reconnecting[0]:
                                    break
                                try:
                                    await _live_session[0].send_realtime_input(
                                        audio={"data": _pre_chunk, "mime_type": "audio/pcm;rate=16000"}
                                    )
                                except (ConnectionClosedError, APIError):
                                    break
                        _preroll_buf.clear()
                        _preroll_has_vad[0] = False

                    if _VAD_AVAILABLE:
                        _vad_buf.extend(chunk)
                        _has_voice = False
                        while len(_vad_buf) >= _VAD_FRAME_BYTES:
                            frame = bytes(_vad_buf[:_VAD_FRAME_BYTES])
                            del _vad_buf[:_VAD_FRAME_BYTES]
                            try:
                                if _VAD.is_speech(frame, 16000):
                                    _has_voice = True
                            except Exception:
                                pass
                    else:
                        _has_voice = _is_voice_rms(chunk)

                    _n_s = len(chunk) // 2
                    _chunk_rms = 0.0
                    if _n_s > 0:
                        _samps = struct.unpack(f"<{_n_s}h", chunk[:_n_s * 2])
                        _chunk_rms = math.sqrt(sum(s * s for s in _samps) / _n_s)

                    if not _has_voice:
                        _noise_rms[0] += _NOISE_ALPHA * (_chunk_rms - _noise_rms[0])
                    elif _noise_rms[0] > 50 and _chunk_rms < _noise_rms[0] * _SNR_RATIO:
                        _has_voice = False
                        _noise_rms[0] += _NOISE_ALPHA * (_chunk_rms - _noise_rms[0])
                        _snr_filtered_chunks[0] += 1

                    if _has_voice:
                        _snr_filtered_chunks[0] = 0
                        _patient_spoke.set()
                        _patient_ever_spoke[0] = True
                        _activity[0] = time.time()
                        _real_vad_pending[0] = True
                        _time_since_ai = time.time() - _last_ai_turn_end[0]
                        if (_suppress_next_audio[0]
                                and _suppress_set_at[0] > 0
                                and _chunk_rms > VAD_THRESHOLD * 1.5
                                and time.time() - _suppress_set_at[0] > 2.5
                                and _time_since_ai > 1.5):
                            _suppress_next_audio[0] = False
                            _suppress_set_at[0] = 0.0
                            _suppress_timeout_fired[0] = True
                            logger.info("[AGENT] Suppress timeout: знято примусово")
                        _last_speech_ts[0] = time.time()
                        _eot_sent[0] = False
                    elif (not _eot_sent[0]
                          and _last_speech_ts[0] > 0
                          and not _reconnecting[0]
                          and time.time() >= _interrupted_pause_until[0]
                          and time.time() - _last_speech_ts[0] >= _EARLY_EOT_SEC):
                        _eot_sent[0] = True
                        try:
                            await _live_session[0].send_realtime_input(
                                activity_end=types.ActivityEnd()
                            )
                        except Exception:
                            pass

                    if _reconnecting[0]:
                        continue
                    try:
                        await _live_session[0].send_realtime_input(
                            audio={"data": chunk, "mime_type": "audio/pcm;rate=16000"}
                        )
                    except (ConnectionClosedError, APIError):
                        if self._end_event.is_set():
                            break
                        logger.warning("[AGENT] send_task: з'єднання перервано — чекаємо reconnect")
                        try:
                            await asyncio.wait_for(_reconnected_event.wait(), timeout=15.0)
                        except asyncio.TimeoutError:
                            logger.error("[AGENT] send_task: timeout reconnect → завершуємо")
                            break

            async def _recv_and_handle() -> None:
                nonlocal _silence_count
                _draining = False
                _ai_buf: list[str] = []

                while True:
                    try:
                        turn_had_content    = False
                        turn_had_audio      = False
                        _turn_was_interrupted = False
                        turn_audio_bytes    = 0
                        _turn_audio_played  = False
                        _had_patient_said   = False
                        _ai_text_lower = ""
                        _ai_buf.clear()
                        _ai_turn_active[0] = True
                        _ai_turn_started_at[0] = time.time()

                        async for resp in _live_session[0].receive():
                            if self._end_event.is_set() and not _draining:
                                break
                            turn_had_content = True

                            # ── Session resumption handle ──────────────────────────
                            if (resp.session_resumption_update
                                    and resp.session_resumption_update.new_handle):
                                self._resumption_handle = resp.session_resumption_update.new_handle

                            # ── Interrupted (перша перевірка — щоб continue пропустив все нижче) ──
                            if resp.server_content and resp.server_content.interrupted:
                                logger.info("[AGENT] AI перервано — пацієнт говорить")
                                binotel_call.flush_audio()
                                _interrupted_pause_until[0] = 0.0
                                _turn_was_interrupted = True
                                _patient_spoke.set()
                                if _timer_task and not _timer_task.done():
                                    _timer_task.cancel()
                                continue

                            # ── Аудіо AI → пацієнт ────────────────────────────────
                            if resp.data:
                                if not _greeting_done.is_set():
                                    continue  # greeting buffer handled in warmup
                                turn_had_audio = True
                                turn_audio_bytes += len(resp.data)
                                if _suppress_next_audio[0] and not _allow_next_turn[0]:
                                    continue  # suppress ghost turns
                                _allow_next_turn[0] = False
                                _turn_audio_played = True
                                gate = _calc_echo_gate(turn_audio_bytes)
                                _interrupted_pause_until[0] = max(
                                    _interrupted_pause_until[0],
                                    time.time() + gate,
                                )
                                await binotel_call.send_audio(resp.data)
                                _activity[0] = time.time()
                                _patient_spoke.set()
                                if _timer_task and not _timer_task.done():
                                    _timer_task.cancel()
                                if _silence_task and not _silence_task.done():
                                    _silence_task.cancel()

                            # ── Транскрипція AI ────────────────────────────────────
                            if resp.server_content:
                                sc = resp.server_content
                                if sc.output_transcription and sc.output_transcription.text:
                                    _ai_buf.append(sc.output_transcription.text)
                            if _ai_buf:
                                joined = " ".join(_ai_buf)
                                _ai_text_lower = joined.lower()
                                if not _suppress_next_audio[0] or _allow_next_turn[0]:
                                    _last_ai_said[0] = joined

                            # ── Транскрипція пацієнта ──────────────────────────────
                            if resp.server_content:
                                sc = resp.server_content
                                if (sc.input_transcription and sc.input_transcription.text
                                        and _greeting_done.is_set()
                                        and time.time() >= _interrupted_pause_until[0]):
                                    txt = sc.input_transcription.text.strip()
                                    if txt:
                                        now_t = time.time()
                                        _is_dup = (txt == _last_patient_said_text[0]
                                                   and now_t - _last_patient_said_time[0] < 3.0)
                                        if not _is_dup:
                                            _last_patient_said_text[0] = txt
                                            _last_patient_said_time[0] = now_t
                                            _suppress_next_audio[0] = False
                                            _suppress_set_at[0] = 0.0
                                            _had_patient_said = True
                                            _patient_said_count[0] += 1
                                            _real_vad_pending[0] = False
                                            _allow_next_turn[0] = False
                                            _activity[0] = now_t
                                            _silence_count = 0
                                            _watchdog_injected[0] = 0
                                            logger.info(f"[TRANSCRIPT] Пацієнт: «{txt}»")

                            # ── Tool calls ─────────────────────────────────────────
                            if (resp.tool_call
                                    and resp.tool_call.function_calls
                                    and _greeting_done.is_set()):
                                _batch = []
                                _after_batch = None
                                # Ghost-тур: suppress активний і пацієнт ще не говорив у цьому турі.
                                # end_call обходить перевірку — дзвінок повинен завершитись навіть у ghost-турі.
                                _has_critical = any(
                                    fc.name == "end_call"
                                    for fc in resp.tool_call.function_calls
                                )
                                if (_suppress_next_audio[0] or _allow_next_turn[0]) and not _had_patient_said and not _has_critical:
                                    logger.debug("[AGENT] Ghost tool batch — пропускаємо")
                                    await _live_session[0].send_tool_response(function_responses=[
                                        types.FunctionResponse(id=fc.id, name=fc.name, response={"success": True})
                                        for fc in resp.tool_call.function_calls
                                    ])
                                    continue
                                for fc in resp.tool_call.function_calls:
                                    args = {k: v for k, v in (fc.args or {}).items()}
                                    if _had_patient_said:
                                        _suppress_next_audio[0] = False
                                    # Захист: book_new_visit/decline заборонено поки пацієнт не заговорив
                                    _is_destructive = fc.name in ("book_new_visit", "decline_with_reason")
                                    if _is_destructive and (not _greeting_done.is_set() or _patient_said_count[0] == 0):
                                        logger.warning(f"[AGENT] {fc.name} заблоковано — пацієнт ще не говорив")
                                        _batch.append(types.FunctionResponse(
                                            id=fc.id, name=fc.name,
                                            response={"success": False, "message": "Спочатку поговори з пацієнтом — дочекайся його відповіді."},
                                        ))
                                        continue
                                    # end_call(no_result) до першої репліки — занадто рано
                                    _end_args = dict(fc.args) if fc.args else {}
                                    if fc.name == "end_call" and _end_args.get("result") == "no_result" and _patient_said_count[0] < 1:
                                        logger.warning("[AGENT] end_call(no_result) заблоковано — пацієнт ще нічого не сказав")
                                        _batch.append(types.FunctionResponse(
                                            id=fc.id, name=fc.name,
                                            response={"success": False, "message": "Пацієнт ще нічого не сказав — привітайся і дочекайся відповіді."},
                                        ))
                                        continue
                                    try:
                                        result = await handle_tool_call(
                                            fc.name, args, self.visit, _call_cache
                                        )
                                    except Exception as _te:
                                        logger.error(f"[AGENT] tool {fc.name} помилка: {_te!r}")
                                        result = {
                                            "success": False,
                                            "error": "Технічна помилка. Скажи пацієнту що виникла проблема і запропонуй зателефонувати в клініку.",
                                        }
                                    _batch.append(types.FunctionResponse(
                                        id=fc.id, name=fc.name, response=result
                                    ))

                                    if fc.name == "get_available_slots":
                                        # В Module 2 не пригнічуємо Turn 1 — бот одразу озвучує дати
                                        if args.get("date"):
                                            _date_slots_fetched[0] = True

                                    if fc.name == "book_new_visit" and result.get("success"):
                                        self.call_result = "booked"
                                        logger.info("[AGENT] book_new_visit → booked")

                                    if fc.name == "end_call":
                                        raw_result = result.get("result", "no_result")
                                        # Захист: якщо ключова дія вже виконана — гарантуємо правильний result
                                        if _call_cache.get("book_new_visit", {}).get("success") and raw_result != "booked":
                                            logger.warning(f"[AGENT] end_call({raw_result}) але book_new_visit виконано → booked")
                                            raw_result = "booked"
                                        elif _call_cache.get("decline", {}).get("success") and raw_result not in ("declined", "booked"):
                                            logger.warning(f"[AGENT] end_call({raw_result}) але decline виконано → declined")
                                            raw_result = "declined"
                                        elif _call_cache.get("self_book", {}).get("success") and raw_result not in ("self_book", "booked", "declined"):
                                            logger.warning(f"[AGENT] end_call({raw_result}) але self_book виконано → self_book")
                                            raw_result = "self_book"
                                        self.call_result = raw_result
                                        logger.info(f"[AGENT] end_call → {self.call_result}")
                                        self._end_event.set()
                                        _after_batch = ("end_call",)
                                        break

                                if _batch:
                                    await _live_session[0].send_tool_response(function_responses=_batch)
                                if _after_batch:
                                    _draining = True

                        # turn завершено
                        if not turn_had_content:
                            if not self._end_event.is_set() and not _draining:
                                logger.warning("[AGENT] recv_task: сесія закрита (no content) — reconnect...")
                                if await _do_reconnect():
                                    continue
                            logger.warning("[AGENT] recv_task: вихід (no content)")
                            if _draining:
                                binotel_call.flush_encode_buffer()
                                await asyncio.sleep(2.0)
                            return

                        if _ai_buf:
                            logger.info(f"[AI] Оля: «{' '.join(_ai_buf)}»")
                        logger.debug("[AGENT] recv_task: turn завершено")

                        # Double-turn guard: Gemini іноді генерує 2 тури підряд (~1-2с) без відповіді
                        # пацієнта. VAD між турами ловить фоновий шум → _real_vad_pending=True →
                        # другий тур обходить suppress. Якщо від кінця попереднього AI туру пройшло
                        # менше 2.5с і input_transcription не підтвердив реального голосу — це шум.
                        _MIN_INTER_TURN_GAP = 2.5
                        if _real_vad_pending[0] and not _had_patient_said:
                            if time.time() - _last_ai_turn_end[0] < _MIN_INTER_TURN_GAP:
                                _real_vad_pending[0] = False
                                logger.debug("[AGENT] Double-turn guard: _real_vad_pending скинуто (шум між турами)")

                        if _draining:
                            if turn_had_audio:
                                _farewell_wait = 5.0 if self.call_result == "booked" else 1.0
                                logger.info("[AGENT] AI завершив прощання — flush + %.0fс", _farewell_wait)
                                binotel_call.flush_encode_buffer()
                                await asyncio.sleep(_farewell_wait)
                            else:
                                _no_audio_wait = 5.0 if self.call_result == "booked" else 2.0
                                logger.info("[AGENT] end_call без аудіо — flush + %.0fс Opus drain", _no_audio_wait)
                                binotel_call.flush_encode_buffer()
                                await asyncio.sleep(_no_audio_wait)
                            return

                        _ai_turn_active[0] = False
                        _last_ai_turn_end[0] = time.time()

                        if _greeting_done.is_set() and turn_had_audio:
                            binotel_call.flush_encode_buffer()
                            _interrupted_pause_until[0] = max(_interrupted_pause_until[0], time.time() + 0.0)
                            _audio_queue_sec = turn_audio_bytes / 48000.0
                            _schedule_silence_timer(audio_delay=_audio_queue_sec)

                            if _date_slots_fetched[0]:
                                _date_slots_fetched[0] = False
                                _turn1_is_full = any(
                                    w in _ai_text_lower for w in (
                                        "ранок", "вранці", "обід", "вечір", "ввечері", "вечором",
                                        "підходить", "зручніш", "правильно", "вірно",
                                    )
                                )
                                if not _turn1_is_full:
                                    _allow_next_turn[0] = True

                            if not _turn_was_interrupted:
                                if _suppress_timeout_fired[0]:
                                    _suppress_timeout_fired[0] = False
                                else:
                                    _suppress_next_audio[0] = True
                                    _suppress_set_at[0] = time.time()
                            if _turn_audio_played:
                                _activity[0] = time.time() + (turn_audio_bytes / 48000)

                    except asyncio.CancelledError:
                        raise
                    except Exception as e:
                        if isinstance(e, (ConnectionClosedError, APIError, ConnectionResetError)):
                            if not self._end_event.is_set() and not _draining:
                                logger.warning(f"[AGENT] recv_task: з'єднання перервано — reconnect...")
                                if await _do_reconnect():
                                    continue
                        logger.error(f"[AGENT] recv_task впав: {e!r}", exc_info=True)
                        raise

            send_task            = asyncio.create_task(_send_patient_audio(), name="b21_send_audio")
            recv_task            = asyncio.create_task(_recv_and_handle(),    name="b21_recv_handle")
            watchdog_task        = asyncio.create_task(_watchdog(),           name="b21_watchdog")
            timeout_warning_task = asyncio.create_task(_timeout_warning(),    name="b21_timeout_warn")
            sip_monitor_task     = asyncio.create_task(_sip_monitor(),        name="b21_sip_monitor")

            # Fallback prefetch якщо warmup не запускався
            if "__raw_slots" not in _call_cache:
                async def _prefetch_slots() -> None:
                    try:
                        from services import clinic_api as _capi
                        doctor_name = self.visit.get("doctor_name", "")
                        idpoint     = int(self.visit.get("idpoint", 0))
                        _raw = await _capi.get_doctor_slots_b2(doctor_name, idpoint)
                        _call_cache["__raw_slots"] = _raw if _raw is not None else []
                        logger.debug(f"[AGENT] Cold prefetch: {len(_call_cache['__raw_slots'])} слотів")
                    except Exception as _e:
                        logger.debug(f"[AGENT] Cold prefetch failed: {_e!r}")
                asyncio.create_task(_prefetch_slots(), name="b21_prefetch")

            if greeting_buffer:
                _play_start = time.monotonic()
                _played_dur = 0.0
                for chunk in greeting_buffer:
                    await binotel_call.send_audio(chunk)
                    _played_dur += len(chunk) / 48000
                    _slack = (_play_start + _played_dur) - time.monotonic()
                    if _slack > 0.01:
                        await asyncio.sleep(_slack)
                _suppress_next_audio[0] = True
                _last_ai_turn_end[0] = time.time()
                # Greeting грає в real-time (sleep у циклі) → по виходу залишок ≈ 0.
                # Потрібна тільки затримка затухання ехо кімнати (~800мс + запас).
                # НЕ додавати _played_dur — аудіо вже відіграло!
                _interrupted_pause_until[0] = time.time() + 0.0

                _needs_primer = _stale_warmup[0]
                if _needs_primer:
                    await _live_session[0].send_client_content(
                        turns={"parts": [{"text": "Ти вже привіталась і нагадала про повторний візит. Не повторюй привітання. Чекай відповіді пацієнта."}]},
                        turn_complete=True,
                    )
                _greeting_done.set()

                _, _sh_cache_w = _build_slots_hint(_call_cache.get("__raw_slots"))
                if _sh_cache_w and "get_available_slots" not in _call_cache:
                    _call_cache["get_available_slots"] = _sh_cache_w
                    logger.info("[AGENT] Warmup: кеш слотів pre-populated")

                from services.audio_bridge import enable_noise_profiling
                enable_noise_profiling()
                _activity[0] = time.time()
                logger.info("[AGENT] Стрімінг розпочато (warmup path)")
                _schedule_silence_timer()
            else:
                await _live_session[0].send_client_content(
                    turns={"parts": [{"text": "Починай розмову — коротко привітайся та нагадай про рекомендацію лікаря щодо повторного прийому (максимум 2-3 речення)"}]},
                    turn_complete=True,
                )
                _greeting_done.set()
                from services.audio_bridge import enable_noise_profiling
                enable_noise_profiling()
                _activity[0] = time.time()
                logger.info("[AGENT] Стрімінг розпочато (cold path)")

                async def _cold_populate_cache() -> None:
                    raw = _call_cache.get("__raw_slots")
                    if raw is None:
                        try:
                            from services import clinic_api as _capi2
                            doctor_name = self.visit.get("doctor_name", "")
                            idpoint     = int(self.visit.get("idpoint", 0))
                            raw = await asyncio.wait_for(
                                _capi2.get_doctor_slots_b2(doctor_name, idpoint), timeout=2.0
                            )
                            _call_cache["__raw_slots"] = raw if raw is not None else []
                        except Exception:
                            pass
                    _, _sh = _build_slots_hint(raw)
                    if _sh and "get_available_slots" not in _call_cache:
                        _call_cache["get_available_slots"] = _sh
                        logger.info("[AGENT] Cold: кеш слотів pre-populated")
                asyncio.create_task(_cold_populate_cache(), name="b21_cold_cache")

            try:
                done, pending = await asyncio.wait(
                    [send_task, recv_task, watchdog_task, timeout_warning_task, sip_monitor_task],
                    return_when=asyncio.FIRST_COMPLETED,
                )
                for t in done:
                    if t.cancelled():
                        logger.info(f"[AGENT] '{t.get_name()}' скасовано")
                    elif t.exception():
                        logger.error(f"[AGENT] '{t.get_name()}' впав: {t.exception()!r}")
                    else:
                        logger.info(f"[AGENT] '{t.get_name()}' завершено")

                if self._end_event.is_set() and not recv_task.done():
                    try:
                        await asyncio.wait_for(asyncio.shield(recv_task), timeout=15.0)
                    except asyncio.TimeoutError:
                        logger.warning("[AGENT] Timeout очікування прощання AI (15с)")
            finally:
                if _timer_task and not _timer_task.done():
                    _timer_task.cancel()
                if _silence_task and not _silence_task.done():
                    _silence_task.cancel()
                watchdog_task.cancel()
                send_task.cancel()
                recv_task.cancel()
                timeout_warning_task.cancel()
                sip_monitor_task.cancel()
                await asyncio.gather(
                    send_task, recv_task, watchdog_task, timeout_warning_task, sip_monitor_task,
                    return_exceptions=True,
                )
                if _live_cm[0] is not None:
                    try:
                        await asyncio.wait_for(_live_cm[0].__aexit__(None, None, None), timeout=3.0)
                    except Exception:
                        pass
                # Страховка: якщо SIP впав до end_call — відновлюємо результат з кешу
                if self.call_result in (None, "no_result"):
                    if _call_cache.get("book_new_visit", {}).get("success"):
                        self.call_result = "booked"
                        logger.warning("[AGENT] finally: book_new_visit → booked")
                    elif _call_cache.get("decline", {}).get("success"):
                        self.call_result = "declined"
                        logger.warning("[AGENT] finally: decline → declined")
                    elif _call_cache.get("self_book", {}).get("success"):
                        self.call_result = "self_book"
                        logger.warning("[AGENT] finally: self_book → self_book")

                self._last_patient_text = _last_patient_said_text[0]
                self._last_ai_text = _last_ai_said[0]
                self._decline_category = _call_cache.get("decline", {}).get("category", "")
                self._new_visit_id = _call_cache.get("book_new_visit", {}).get("new_visit_id", "")

        # Warmup session management (ідентично Block1Agent)
        warmup_session = getattr(self, "_warmup_session", None)
        warmup_cm      = getattr(self, "_warmup_cm", None)
        self._warmup_session = None
        self._warmup_cm = None

        if self._monitor_task and not self._monitor_task.done():
            self._monitor_task.cancel()
        self._monitor_task = None

        if getattr(self, "_session_reconnected", False) and warmup_session and greeting_buffer:
            _stale_warmup[0] = True
            logger.info("[AGENT] Session відновлена через handle → primer буде надіслано")

        try:
            if warmup_session and warmup_cm:
                try:
                    await _stream(warmup_session)
                finally:
                    try:
                        await asyncio.wait_for(warmup_cm.__aexit__(None, None, None), timeout=3.0)
                    except Exception:
                        pass
            else:
                _cm = self.client.aio.live.connect(
                    model=GEMINI_MODEL, config=self._build_config()
                )
                session = await _cm.__aenter__()
                try:
                    await _stream(session)
                finally:
                    try:
                        await asyncio.wait_for(_cm.__aexit__(None, None, None), timeout=3.0)
                    except Exception:
                        pass
        except Exception as e:
            logger.error(f"[AGENT] Помилка в production: {e}", exc_info=True)

        self.call_duration = time.time() - start
        result = self.call_result or "no_result"
        self.call_commentary = self._build_commentary(result)
        logger.info(
            f"[AGENT] Завершено. Тривалість: {self.call_duration:.0f}с. "
            f"Результат: {result} | {self.call_commentary}"
        )
        return result

    # ------------------------------------------------------------------
    # Warmup: підігрів Gemini поки пацієнт чує гудки
    # ------------------------------------------------------------------

    async def warmup(self) -> None:
        """Підключитись до Gemini і згенерувати привітання заздалегідь."""
        self._greeting_buffer: list[bytes] = []
        self._greeting_duration: float = 0.0
        self._prefetched_raw_slots: list | None = None
        self._resumption_handle = None
        self._session_reconnected = False
        _warmup_cm = None

        async def _warmup_prefetch() -> None:
            try:
                from services import clinic_api as _capi
                doctor_name = self.visit.get("doctor_name", "")
                idpoint     = int(self.visit.get("idpoint", 0))
                _raw = await _capi.get_doctor_slots_b2(doctor_name, idpoint)
                self._prefetched_raw_slots = _raw if _raw is not None else []
                logger.debug(f"[AGENT] Warmup prefetch: {len(self._prefetched_raw_slots)} слотів")
            except Exception as _e:
                logger.debug(f"[AGENT] Warmup prefetch failed: {_e!r}")

        prefetch_task = asyncio.create_task(_warmup_prefetch(), name="b21_warmup_prefetch")

        def _start_monitor(initial_session, initial_cm) -> asyncio.Task:
            async def _monitor_loop() -> None:
                current_session = initial_session
                current_cm = initial_cm
                while True:
                    session_dead = False
                    try:
                        while True:
                            try:
                                msg = await asyncio.wait_for(current_session._receive(), timeout=30.0)
                            except asyncio.TimeoutError:
                                continue
                            if (msg.session_resumption_update
                                    and msg.session_resumption_update.new_handle):
                                self._resumption_handle = msg.session_resumption_update.new_handle
                            if msg.go_away:
                                logger.warning(f"[AGENT] Monitor: GoAway — відновлення...")
                                session_dead = True
                                break
                    except asyncio.CancelledError:
                        raise
                    except Exception as _e:
                        _name = type(_e).__name__
                        if "ConnectionClosedOK" not in _name and getattr(_e, "code", None) != 1000:
                            logger.info(f"[AGENT] Monitor: сесія закрита ({_name}) — відновлення...")
                        session_dead = True

                    if not session_dead:
                        break
                    if not self._resumption_handle:
                        logger.warning("[AGENT] Monitor: немає handle → cold path")
                        self._warmup_session = None
                        self._warmup_cm = None
                        break
                    try:
                        try:
                            await asyncio.wait_for(current_cm.__aexit__(None, None, None), timeout=1.0)
                        except Exception:
                            pass
                        new_cm = self.client.aio.live.connect(
                            model=GEMINI_MODEL,
                            config=self._build_config(resume_handle=self._resumption_handle),
                        )
                        new_session = await new_cm.__aenter__()
                        current_session = new_session
                        current_cm = new_cm
                        self._warmup_session = new_session
                        self._warmup_cm = new_cm
                        self._warmup_time = time.time()
                        self._session_reconnected = True
                        logger.info("[AGENT] Monitor: warmup сесію відновлено")
                    except asyncio.CancelledError:
                        raise
                    except Exception as _re:
                        logger.error(f"[AGENT] Monitor: відновлення провалилось: {_re!r}")
                        self._warmup_session = None
                        self._warmup_cm = None
                        break
            return asyncio.create_task(_monitor_loop(), name="b21_warmup_monitor")

        async def _generate_greeting(session) -> None:
            await session.send_client_content(
                turns={"parts": [{"text": (
                    "Починай розмову — коротко привітайся та нагадай чому дзвониш (максимум 2 речення)"
                )}]},
                turn_complete=True,
            )
            async for resp in session.receive():
                if resp.data:
                    self._greeting_buffer.append(resp.data)
                    self._greeting_duration += len(resp.data) / 48000
                if (resp.session_resumption_update
                        and resp.session_resumption_update.new_handle):
                    self._resumption_handle = resp.session_resumption_update.new_handle
                if resp.server_content and resp.server_content.turn_complete:
                    break

        try:
            _warmup_cm = self.client.aio.live.connect(
                model=GEMINI_MODEL, config=self._build_config()
            )
            warmup_session = await _warmup_cm.__aenter__()
            await _generate_greeting(warmup_session)

            if not prefetch_task.done():
                try:
                    await asyncio.wait_for(asyncio.shield(prefetch_task), timeout=2.0)
                except asyncio.TimeoutError:
                    pass

            logger.info(
                f"[AGENT] Warmup готовий: {len(self._greeting_buffer)} chunks"
                f" ({self._greeting_duration:.1f}с)"
                + (f", {len(self._prefetched_raw_slots)} слотів" if self._prefetched_raw_slots else "")
            )
            self._warmup_cm = _warmup_cm
            self._warmup_session = warmup_session
            self._warmup_time = time.time()
            _warmup_cm = None
            self._monitor_task = _start_monitor(warmup_session, self._warmup_cm)
        except Exception as e:
            logger.warning(f"[AGENT] Warmup помилка: {e} — retry...")
            self._greeting_buffer = []
            self._warmup_session = None
            self._warmup_cm = None
            if _warmup_cm is not None:
                try:
                    await _warmup_cm.__aexit__(None, None, None)
                except Exception:
                    pass
                _warmup_cm = None
            await asyncio.sleep(1)
            try:
                _warmup_cm = self.client.aio.live.connect(
                    model=GEMINI_MODEL, config=self._build_config()
                )
                warmup_session = await _warmup_cm.__aenter__()
                await _generate_greeting(warmup_session)
                self._warmup_cm = _warmup_cm
                self._warmup_session = warmup_session
                self._warmup_time = time.time()
                _warmup_cm = None
                logger.info(f"[AGENT] Warmup retry успішний: {len(self._greeting_buffer)} chunks")
                self._monitor_task = _start_monitor(warmup_session, self._warmup_cm)
            except Exception as e2:
                logger.warning(f"[AGENT] Warmup retry провалився: {e2} — cold path")
                self._greeting_buffer = []
                self._warmup_session = None
                self._warmup_cm = None
        finally:
            if _warmup_cm is not None:
                try:
                    await _warmup_cm.__aexit__(None, None, None)
                except Exception:
                    pass
            if not prefetch_task.done():
                prefetch_task.cancel()

    # ------------------------------------------------------------------
    # Класифікація останньої фрази (SIP drop fallback)
    # ------------------------------------------------------------------

    def _get_classify_client(self):
        if not hasattr(self, "_classify_client_cache"):
            from google import genai as _genai
            self._classify_client_cache = _genai.Client(api_key=GEMINI_API_KEY)
        return self._classify_client_cache

    _CLASSIFY_MODEL = "gemini-2.0-flash"

    async def _classify_last_phrase(self, phrase: str) -> str:
        """Gemini text API: аналізує останню фразу пацієнта при SIP drop → declined/no_result."""
        if not phrase or len(phrase.strip()) < 2:
            return "no_result"
        prompt = (
            f"Пацієнту зателефонували запропонувати записатись на повторний прийом до лікаря.\n"
            f"Пацієнт сказав: \"{phrase}\"\n\n"
            f"Визнач намір. Відповідь ТІЛЬКИ одне слово:\n"
            f"declined — відмовляється (ні/не треба/не хочу/не буду/не можу)\n"
            f"no_result — незрозуміло або не по темі"
        )
        try:
            resp = await self._get_classify_client().aio.models.generate_content(
                model=self._CLASSIFY_MODEL,
                contents=prompt,
            )
            text = (resp.text or "").strip().lower()
            if "declined" in text:
                return "declined"
            return "no_result"
        except Exception as _e:
            logger.warning(f"[AGENT] classify_phrase помилка: {_e!r}")
            return "no_result"

    # ------------------------------------------------------------------
    # Коментар до дзвінка
    # ------------------------------------------------------------------

    def _build_commentary(self, result: str) -> str:
        last_said = getattr(self, "_last_patient_text", "")
        decline_cat = getattr(self, "_decline_category", "")

        if result == "booked":
            new_id = getattr(self, "_new_visit_id", "")
            return (f"Записано на повторний прийом. ID нового запису: {new_id}." if new_id
                    else "Записано на повторний прийом.")[:512]

        if result == "declined":
            base = "Відмовився від запису."
            return (f"{base} Причина: {decline_cat}." if decline_cat else base)[:512]

        if result == "self_book":
            return "Пацієнт запишеться самостійно."

        _TEMPLATES = {
            "no_answer": "Пацієнт не підняв трубку.",
            "no_result":  "Незрозумілий результат або технічна помилка.",
        }
        return _TEMPLATES.get(result, "")[:512]

    # ------------------------------------------------------------------
    # Публічний інтерфейс
    # ------------------------------------------------------------------

    async def run(self, binotel_call=None) -> str:
        """
        Запустити агента.

        Args:
            binotel_call: BinotelCall (production) або None (debug — мікрофон/динамік)

        Returns:
            booked | declined | self_book | no_answer | no_result
        """
        if DEBUG:
            return await self.run_debug()
        return await self.run_production(binotel_call)

    def cleanup(self) -> None:
        """Звільнити PyAudio ресурси."""
        if self._pa:
            try:
                self._pa.terminate()
            except Exception:
                pass
