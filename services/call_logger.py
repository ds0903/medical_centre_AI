"""
Логування дзвінків у файл (LOG_FILE=true в .env).

Формат: JSON Lines — один рядок = один дзвінок.
Файл: logs/calls_YYYY-MM-DD.jsonl

Структура запису:
  ts       — час початку дзвінка
  patient  — ім'я пацієнта
  doctor   — лікар
  visit    — дата/час запису
  duration — тривалість дзвінка (секунди)
  result   — confirmed / rescheduled / cancelled / no_answer / no_result / transferred
  transcript — список реплік: r="p" (пацієнт) або r="a" (AI)
"""

import json
import logging
from datetime import datetime
from pathlib import Path

_log = logging.getLogger(__name__)

# Акумулятор поточного дзвінка (один дзвінок за раз — система не паралельна)
_current: dict | None = None


def start_call(visit: dict) -> None:
    """Почати запис нового дзвінка."""
    global _current
    _current = {
        "ts":         datetime.now().isoformat(timespec="seconds"),
        "patient":    visit.get("patient_name", ""),
        "doctor":     visit.get("doctor_name", ""),
        "visit":      visit.get("visit_datetime", ""),
        "phone":      visit.get("phone", ""),
        "duration":   0,
        "result":     "no_result",
        "transcript": [],
    }


def log_patient(text: str) -> None:
    """Додати репліку пацієнта до транскрипту."""
    if _current is None:
        return
    _current["transcript"].append({"r": "p", "t": text})


def log_ai(text: str) -> None:
    """Додати репліку AI до транскрипту."""
    if _current is None:
        return
    _current["transcript"].append({"r": "a", "t": text})


def end_call(result: str, duration: float) -> None:
    """Завершити запис і дописати рядок у файл."""
    global _current
    if _current is None:
        return

    _current["result"] = result
    _current["duration"] = round(duration)

    log_dir = Path("logs")
    log_dir.mkdir(exist_ok=True)
    log_file = log_dir / f"calls_{datetime.now().strftime('%Y-%m-%d')}.jsonl"

    try:
        with open(log_file, "a", encoding="utf-8") as f:
            f.write(json.dumps(_current, ensure_ascii=False) + "\n")
        _log.debug(f"[CALL LOG] Збережено → {log_file.name}")
    except Exception as e:
        _log.warning(f"[CALL LOG] Помилка запису: {e}")

    _current = None
