"""
Відстеження стану щоденних сесій — fetch і дзвінки.
Файл: state/YYYY-MM-DD.json (один файл на день, Київський час).
Використовується тільки коли SKIP_SCHEDULER=false.
"""

import json
import logging
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)

_STATE_DIR = Path("state")
_KYIV = ZoneInfo("Europe/Kyiv")

_KEYS = (
    "morning_fetch_done",
    "morning_calls_done",
    "evening_fetch_done",
    "evening_calls_done",
)


def _today() -> str:
    return datetime.now(_KYIV).strftime("%Y-%m-%d")


def _state_file() -> Path:
    _STATE_DIR.mkdir(exist_ok=True)
    return _STATE_DIR / f"{_today()}.json"


def _load() -> dict:
    f = _state_file()
    if f.exists():
        try:
            return json.loads(f.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {k: False for k in _KEYS}


def _save(state: dict) -> None:
    _state_file().write_text(json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8")


def is_done(key: str) -> bool:
    return _load().get(key, False)


def mark_done(key: str) -> None:
    state = _load()
    state[key] = True
    _save(state)
    logger.info(f"[STATE] {key} = true")


def get_state() -> dict:
    return _load()
