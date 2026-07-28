"""
Модуль 2.2 — AI-аналіз дзвінків колл-центру.

Pipeline:
  1. Binotel REST API → завантажити аудіо запису дзвінка
  2. STT (Gemini Audio / Google STT) → транскрипт з розміткою мовців
  3. AI scoring → оцінка за критеріями + фрази-маркери
  4. Google Sheets → зберегти результат
  5. (Looker Studio оновлюється автоматично з Sheets)

Лінії для моніторингу (з ТЗ):
  901-906 — колл-центр
  907, 909 — МРТ кабінет
  908 — диспетчер НП
  911, 912, 913 — відділення на Гірничих інженерів
  914 — AI бот (наш)
  ВИНЯТОК: 910 — кабінет СК (не аналізуємо)
"""

import logging
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

MONITORED_LINES = {"901", "902", "903", "904", "905", "906", "907", "908", "909", "911", "912", "913", "914"}
EXCLUDED_LINES = {"910"}


@dataclass
class CallAnalysisResult:
    """Результат аналізу одного дзвінка."""
    call_id: str
    operator_line: str
    operator_name: str
    call_datetime: str
    duration_sec: int
    direction: str                         # "inbound" / "outbound"
    transcript: str                        # повний транскрипт
    ai_comment: str                        # вільний коментар AI
    criteria_scores: dict[str, bool]       # {назва критерію: True/False}
    marker_violations: list[str]           # фрази-маркери яких не було
    total_score: float                     # 0.0–1.0
    audio_url: str = ""


class CallAnalyzer:
    """
    Обробляє завершений дзвінок:
    завантажує аудіо → транскрибує → оцінює → зберігає в Sheets.
    """

    def __init__(self, sheets_writer, config_reader):
        """
        Args:
            sheets_writer: екземпляр SheetsWriter (services/sheets_writer.py)
            config_reader: читає критерії та фрази-маркери з конфіг аркуша Sheets
        """
        self.sheets = sheets_writer
        self.config = config_reader

    async def process_call(self, call_meta: dict) -> CallAnalysisResult | None:
        """
        Обробити один дзвінок.

        Args:
            call_meta: метадані з Binotel webhook/polling:
                {callId, operatorLine, operatorName, dateTime, duration, direction, recordingUrl}

        Returns:
            CallAnalysisResult або None якщо лінія не моніторується.
        """
        line = str(call_meta.get("operatorLine", ""))
        if line in EXCLUDED_LINES or line not in MONITORED_LINES:
            logger.debug(f"[ANALYZER] Лінія {line} — пропускаємо")
            return None

        logger.info(f"[ANALYZER] Обробка дзвінка {call_meta.get('callId')} лінія {line}")

        # Крок 1: завантажити аудіо
        audio_bytes = await self._download_audio(call_meta["recordingUrl"])

        # Крок 2: транскрипція
        transcript = await self._transcribe(audio_bytes)

        # Крок 3: AI-аналіз
        result = await self._score(call_meta, transcript)

        # Крок 4: зберегти в Sheets
        await self.sheets.append_call_result(result)

        return result

    async def _download_audio(self, url: str) -> bytes:
        """Завантажити аудіо з Binotel."""
        # TODO: binotel_rest.get_recording(url)
        raise NotImplementedError

    async def _transcribe(self, audio_bytes: bytes) -> str:
        """
        STT: аудіо → транскрипт з розміткою [OPERATOR] / [CLIENT].
        Варіант: Gemini File API (завантажити аудіо → prompt → отримати транскрипт).
        """
        # TODO: Gemini Files API або Google STT
        raise NotImplementedError

    async def _score(self, call_meta: dict, transcript: str) -> CallAnalysisResult:
        """
        AI-оцінка транскрипту:
          - завантажити актуальні критерії з конфіг аркуша
          - надіслати транскрипт + критерії в Gemini
          - розпарсити structured output
        """
        # TODO: завантажити критерії з self.config, передати в Gemini structured output
        raise NotImplementedError
