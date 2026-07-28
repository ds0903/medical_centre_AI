"""
Модуль 2.2 — AI-аналіз дзвінків колл-центру.

Pipeline:
  1. Binotel REST API → завантажити аудіо запису дзвінка
  2. STT (Gemini Audio inline) → транскрипт з розміткою [ОПЕРАТОР] / [КЛІЄНТ]
  3. AI scoring → оцінка за критеріями + фрази-маркери (Gemini JSON output)
  4. Google Sheets → зберегти результат

Лінії для моніторингу (з ТЗ):
  901-906 — колл-центр
  907, 909 — МРТ кабінет
  908 — диспетчер НП
  911, 912, 913 — відділення на Гірничих інженерів
  914 — AI бот (наш)
  ВИНЯТОК: 910 — кабінет СК (не аналізуємо)
"""

import base64
import logging
from dataclasses import dataclass, field

from google import genai
from google.genai import types

from agents.block2.call_analyzer.config_reader import ConfigReader, Criterion, MarkerPhrase
from agents.block2.call_analyzer.scorer_prompt import (
    TRANSCRIPTION_PROMPT,
    build_scoring_prompt,
    parse_scoring_response,
)
from config import (
    GEMINI_FLASH_MODEL,
    VERTEX_PROJECT,
    VERTEX_LOCATION,
    B2_MONITORED_LINES,
    B2_EXCLUDED_LINES,
)

logger = logging.getLogger(__name__)

# Дефолтний набір ліній з ТЗ (якщо .env не переоприділяє)
_DEFAULT_MONITORED = {"901", "902", "903", "904", "905", "906", "907", "908", "909", "911", "912", "913", "914"}
_DEFAULT_EXCLUDED  = {"910"}

MONITORED_LINES: set[str] = B2_MONITORED_LINES if B2_MONITORED_LINES else _DEFAULT_MONITORED
EXCLUDED_LINES:  set[str] = B2_EXCLUDED_LINES  if B2_EXCLUDED_LINES  else _DEFAULT_EXCLUDED

# Аудіо формати які підтримує Gemini
_AUDIO_MIME_MAP = {
    b"\xff\xfb": "audio/mpeg",   # MP3
    b"\xff\xf3": "audio/mpeg",
    b"\xff\xf2": "audio/mpeg",
    b"ID3":      "audio/mpeg",
    b"RIFF":     "audio/wav",
    b"OggS":     "audio/ogg",
    b"fLaC":     "audio/flac",
}


def _detect_mime(data: bytes) -> str:
    for sig, mime in _AUDIO_MIME_MAP.items():
        if data[:len(sig)] == sig:
            return mime
    return "audio/mpeg"   # fallback


@dataclass
class CallAnalysisResult:
    """Результат аналізу одного дзвінка."""
    call_id:          str
    operator_line:    str
    operator_name:    str
    call_datetime:    str
    duration_sec:     int
    direction:        str                    # "inbound" / "outbound"
    transcript:       str                    # повний транскрипт
    ai_comment:       str                    # вільний коментар AI
    criteria_scores:  dict[str, bool]        # {назва критерію: True/False}
    marker_violations: list[str]             # фрази-маркери яких не було
    marker_used:      list[str] = field(default_factory=list)
    total_score:      float = 0.0
    audio_url:        str = ""


class CallAnalyzer:
    """
    Обробляє завершений дзвінок:
    завантажує аудіо → транскрибує → оцінює → зберігає в Sheets.
    """

    def __init__(self, sheets_writer, config_reader: ConfigReader):
        """
        Args:
            sheets_writer: екземпляр SheetsWriter (services/sheets_writer.py)
            config_reader: екземпляр ConfigReader (читає критерії з Sheets)
        """
        self.sheets = sheets_writer
        self.config = config_reader
        self._gemini = genai.Client(
            vertexai=True,
            project=VERTEX_PROJECT,
            location=VERTEX_LOCATION,
        )

    def _is_monitored(self, line: str) -> bool:
        line = str(line).strip()
        return line not in EXCLUDED_LINES and line in MONITORED_LINES

    # ------------------------------------------------------------------
    # Публічний API
    # ------------------------------------------------------------------

    async def process_call(self, call_meta: dict) -> CallAnalysisResult | None:
        """
        Обробити один дзвінок.

        Args:
            call_meta: метадані з Binotel polling:
                {callId, internalNumber, operatorName, dateTime,
                 billsec, direction, recordingUrl, ...}

        Returns:
            CallAnalysisResult або None якщо лінія не моніторується або запис відсутній.
        """
        call_id  = str(call_meta.get("callId", call_meta.get("generalCallID", "")))
        line     = str(call_meta.get("internalNumber", call_meta.get("operatorLine", "")))
        rec_url  = call_meta.get("recordingUrl", call_meta.get("recording", ""))

        if not self._is_monitored(line):
            logger.debug(f"[ANALYZER] Лінія {line} — пропускаємо")
            return None

        if not rec_url:
            logger.info(f"[ANALYZER] Дзвінок {call_id} — немає запису, пропускаємо")
            return None

        logger.info(f"[ANALYZER] Обробка дзвінка {call_id} (лінія {line})")

        try:
            audio_bytes = await self._download_audio(rec_url)
        except Exception as e:
            logger.error(f"[ANALYZER] Завантаження аудіо {call_id} провалилось: {e!r}")
            raise

        try:
            transcript = await self._transcribe(audio_bytes)
        except Exception as e:
            logger.error(f"[ANALYZER] Транскрипція {call_id} провалилась: {e!r}")
            raise

        try:
            result = await self._score(call_meta, transcript, rec_url)
        except Exception as e:
            logger.error(f"[ANALYZER] Оцінка {call_id} провалилась: {e!r}")
            raise

        try:
            await self.sheets.append_call_result(result)
        except Exception as e:
            logger.error(f"[ANALYZER] Запис в Sheets провалився: {e!r}")
            # Не падаємо — результат вже порахований, просто не записаний

        logger.info(
            f"[ANALYZER] {call_id} оброблено: score={result.total_score:.2f} "
            f"({sum(result.criteria_scores.values())}/{len(result.criteria_scores)})"
        )
        return result

    # ------------------------------------------------------------------
    # Кроки pipeline
    # ------------------------------------------------------------------

    async def _download_audio(self, url: str) -> bytes:
        """Завантажити аудіо запис з Binotel."""
        from services.binotel_rest import BinotelRestClient
        client = BinotelRestClient()
        return await client.get_recording(url)

    async def _transcribe(self, audio_bytes: bytes) -> str:
        """
        STT через Gemini: аудіо → транскрипт з розміткою [ОПЕРАТОР] / [КЛІЄНТ].
        Передає аудіо inline (base64). Підтримує файли до ~20MB.
        """
        mime_type = _detect_mime(audio_bytes)
        b64_audio = base64.b64encode(audio_bytes).decode("utf-8")

        response = await self._gemini.aio.models.generate_content(
            model=GEMINI_FLASH_MODEL,
            contents=[
                types.Content(parts=[
                    types.Part(
                        inline_data=types.Blob(mime_type=mime_type, data=b64_audio)
                    ),
                    types.Part(text=TRANSCRIPTION_PROMPT),
                ])
            ],
            config=types.GenerateContentConfig(
                temperature=0.0,   # детерміністична транскрипція
                max_output_tokens=8192,
            ),
        )

        transcript = (response.text or "").strip()
        logger.info(f"[ANALYZER] Транскрипт: {len(transcript)} символів")
        return transcript

    async def _score(
        self, call_meta: dict, transcript: str, rec_url: str
    ) -> CallAnalysisResult:
        """
        AI-оцінка транскрипту за критеріями з Google Sheets.
        Повертає CallAnalysisResult.
        """
        # Завантажуємо актуальні критерії (з кешем 10хв)
        criteria = await self.config.get_criteria()
        markers  = await self.config.get_markers()

        if not criteria:
            logger.warning("[ANALYZER] Немає активних критеріїв — пустий результат")
            criteria = []

        prompt = build_scoring_prompt(transcript, criteria, markers, call_meta)

        response = await self._gemini.aio.models.generate_content(
            model=GEMINI_FLASH_MODEL,
            contents=[types.Content(parts=[types.Part(text=prompt)])],
            config=types.GenerateContentConfig(
                temperature=0.0,
                max_output_tokens=4096,
                response_mime_type="application/json",
            ),
        )

        raw_json = (response.text or "").strip()
        scored   = parse_scoring_response(raw_json, criteria)

        # Розрахунок оцінки з урахуванням ваги
        criteria_scores: dict[str, bool] = scored.get("criteria_scores", {})
        if criteria:
            total_weight = sum(c.weight for c in criteria if c.name in criteria_scores)
            passed_weight = sum(
                c.weight for c in criteria
                if c.name in criteria_scores and criteria_scores[c.name]
            )
            total_score = (passed_weight / total_weight) if total_weight > 0 else 0.0
        else:
            total_score = 0.0

        # Визначення напрямку
        raw_dir = call_meta.get("direction", call_meta.get("callType", "inbound"))
        direction = "inbound" if str(raw_dir).lower() in ("inbound", "incoming", "1", "вхідний") else "outbound"

        return CallAnalysisResult(
            call_id          = str(call_meta.get("callId", call_meta.get("generalCallID", ""))),
            operator_line    = str(call_meta.get("internalNumber", call_meta.get("operatorLine", ""))),
            operator_name    = call_meta.get("operatorName", ""),
            call_datetime    = call_meta.get("dateTime", call_meta.get("callDate", "")),
            duration_sec     = int(call_meta.get("billsec", call_meta.get("duration", 0))),
            direction        = direction,
            transcript       = transcript,
            ai_comment       = scored.get("ai_comment", ""),
            criteria_scores  = criteria_scores,
            marker_violations= scored.get("marker_violations", []),
            marker_used      = scored.get("marker_used", []),
            total_score      = round(total_score, 3),
            audio_url        = rec_url,
        )
