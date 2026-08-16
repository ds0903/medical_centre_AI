import os
from dotenv import load_dotenv

load_dotenv()

# Режим роботи
ENV = os.getenv("ENV", "debug")
DEBUG = ENV == "debug"

# Google Cloud credentials (Vertex AI)
_creds = os.path.join(os.path.dirname(__file__), "credentials.json")
if os.path.exists(_creds):
    os.environ.setdefault("GOOGLE_APPLICATION_CREDENTIALS", _creds)

# Vertex AI
VERTEX_PROJECT  = os.getenv("VERTEX_PROJECT_ID", "")
VERTEX_LOCATION = os.getenv("VERTEX_LOCATION", "us-central1")

# Gemini (через Vertex AI)
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")  # не потрібен для Vertex, лишаємо для сумісності
GEMINI_MODEL        = os.getenv("GEMINI_MODEL", "gemini-live-2.5-flash-native-audio")
GEMINI_FLASH_MODEL  = os.getenv("GEMINI_FLASH_MODEL", "gemini-2.5-flash")

# PostgreSQL
DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "postgresql+asyncpg://postgres:postgres@localhost:5432/medical_centre"
)

# Binotel REST API
BINOTEL_API_KEY = os.getenv("BINOTEL_API_KEY", "")
BINOTEL_API_SECRET = os.getenv("BINOTEL_API_SECRET", "")
BINOTEL_API_BASE = "https://api.binotel.com/api/4.0"

# Binotel SIP (для production аудіо-мосту)
BINOTEL_INTERNAL_NUMBER = os.getenv("BINOTEL_INTERNAL_NUMBER", "900")   # внутрішній номер бота
BINOTEL_SIP_SERVER = os.getenv("BINOTEL_SIP_SERVER", "sip1.binotel.com")
BINOTEL_SIP_PORT = int(os.getenv("BINOTEL_SIP_PORT", "5075"))
BINOTEL_SIP_USERNAME = os.getenv("BINOTEL_SIP_USERNAME", "")             # SIP auth username (не = internal number!)
BINOTEL_SIP_PASSWORD = os.getenv("BINOTEL_SIP_PASSWORD", "")
BINOTEL_OPERATOR_NUMBER = os.getenv("BINOTEL_OPERATOR_NUMBER", "901")    # куди передавати оператору

# API клініки
CLINIC_API_URL = os.getenv("CLINIC_API_URL", "")
CLINIC_API_KEY = os.getenv("CLINIC_API_KEY", "")

# Планувальник
SKIP_FETCH = os.getenv("SKIP_FETCH", "false").lower() == "true"
SKIP_SCHEDULER = os.getenv("SKIP_SCHEDULER", "false").lower() == "true"
SKIP_POST = os.getenv("SKIP_POST", "false").lower() == "true"  # не надсилати POST-запити до API клініки (update_state, reschedule)

# Захист від помилкових дзвінків: вимикає (1) дзвінки в суботу/неділю
# і (2) дзвінки по задачах створених не сьогодні (залишки з попереднього дня).
# false — обидві перевірки вимкнені (для тестування / ручного режиму).
SCHEDULER_SAFETY_GUARDS = os.getenv("SCHEDULER_SAFETY_GUARDS", "true").lower() != "false"

# Планувальник — ранкова сесія (shift=0: дзвонимо сьогоднішнім пацієнтам)
SCHEDULER_MORNING_FETCH_HOUR   = int(os.getenv("SCHEDULER_MORNING_FETCH_HOUR",   "8"))
SCHEDULER_MORNING_FETCH_MINUTE = int(os.getenv("SCHEDULER_MORNING_FETCH_MINUTE", "0"))
SCHEDULER_MORNING_CALL_HOUR    = int(os.getenv("SCHEDULER_MORNING_CALL_HOUR",    "9"))
SCHEDULER_MORNING_CALL_MINUTE  = int(os.getenv("SCHEDULER_MORNING_CALL_MINUTE",  "0"))
SCHEDULER_MORNING_CALL_END_HOUR = int(os.getenv("SCHEDULER_MORNING_CALL_END_HOUR", "13"))

# Планувальник — вечірня сесія (shift=1: дзвонимо завтрашнім пацієнтам)
SCHEDULER_EVENING_FETCH_HOUR   = int(os.getenv("SCHEDULER_EVENING_FETCH_HOUR",   "13"))
SCHEDULER_EVENING_FETCH_MINUTE = int(os.getenv("SCHEDULER_EVENING_FETCH_MINUTE", "0"))
SCHEDULER_EVENING_CALL_HOUR    = int(os.getenv("SCHEDULER_EVENING_CALL_HOUR",    "14"))
SCHEDULER_EVENING_CALL_MINUTE  = int(os.getenv("SCHEDULER_EVENING_CALL_MINUTE",  "0"))
SCHEDULER_EVENING_CALL_END_HOUR = int(os.getenv("SCHEDULER_EVENING_CALL_END_HOUR", "18"))

# Дзвінки
MAX_CALL_ATTEMPTS = int(os.getenv("MAX_CALL_ATTEMPTS", "3"))
PAUSE_BETWEEN_CALLS_SEC = int(os.getenv("PAUSE_BETWEEN_CALLS_SEC", "30"))
CALL_TIMEOUT_SEC = int(os.getenv("CALL_TIMEOUT_SEC", "300"))   # таймаут одного дзвінка (5 хв)

# Whitelist номерів: якщо задано — дзвонимо тільки цим номерам (для тестування)
_raw = os.getenv("ALLOWED_PHONES", "")
ALLOWED_PHONES: list[str] = [p.strip() for p in _raw.split(",") if p.strip()]

# Дедуплікація телефонів: true (default) — 1 унікальний телефон = 1 дзвінок за сесію
# false — дзвоним по кожному visit_id окремо (для тестування)
DEDUP_PHONE_CALLS: bool = os.getenv("DEDUP_PHONE_CALLS", "true").lower() != "false"

# Silence detection
SILENCE_TIMEOUT_SEC = float(os.getenv("SILENCE_TIMEOUT_SEC", os.getenv("SILENCE_TIMEOUT", "5.0")))   # секунд тиші → питаємо
SILENCE_START_DELAY_SEC = float(os.getenv("SILENCE_START_DELAY_SEC", "1.0"))  # затримка після audio_delay (SIP jitter буфер)

# Watchdog: максимальна бездіяльність (ні аудіо, ні tool calls, ні VAD) перед re-inject prompt
WATCHDOG_TIMEOUT_SEC = float(os.getenv("WATCHDOG_TIMEOUT_SEC", "20.0"))

# Echo gate: скільки секунд НЕ передавати аудіо пацієнта в Gemini після interrupted події
# (щоб відлуння від SIP-лінії не викликало хибний повторний interrupted)
INTERRUPTED_PAUSE_SEC = float(os.getenv("INTERRUPTED_PAUSE_SEC", "1.0"))

# Логування розмови: [ПАЦІЄНТ] і [AI] в лог (true = показувати)
GEMINI_LOGS = os.getenv("GEMINI_LOGS", "false").lower() == "true"

# Transfer to operator: якщо false — transfer не відбувається (для тестування)
TRANSFER_ENABLED = os.getenv("TRANSFER_ENABLED", "false").lower() == "true"

# Логування дзвінків у файл logs/calls_YYYY-MM-DD.jsonl
LOG_FILE = os.getenv("LOG_FILE", "false").lower() == "true"

# Google Sheets — довідник синонімів процедур (Аркуш 1)
SHEETS_ID  = os.getenv("SHEETS_ID", "")
SHEETS_GID = os.getenv("SHEETS_GID", "0")

# Google Sheets — прайс + підготовка до процедур (Аркуш 2, той самий файл)
SERVICES_GID = os.getenv("SERVICES_GID", "")

# ---------------------------------------------------------------------------
# Блок 2 — Модуль 2.1: нагадування про повторні візити
# ---------------------------------------------------------------------------

# За скільки днів до цільової дати повторного візиту дзвонити (default: 2)
B2_CALL_DAYS_BEFORE = int(os.getenv("B2_CALL_DAYS_BEFORE", "2"))

# Максимальна кількість спроб при недодзвоні (default: 2)
B2_MAX_RETRY = int(os.getenv("B2_MAX_RETRY", "2"))

# ---------------------------------------------------------------------------
# Блок 2 — Модуль 2.2: аналіз дзвінків колл-центру
# ---------------------------------------------------------------------------

# Google Sheets: ID таблиці результатів (обов'язково)
B2_RESULT_SHEETS_ID   = os.getenv("B2_RESULT_SHEETS_ID", "")

# Google Sheets: назви аркушів (можна в одній таблиці або різних)
B2_RESULT_SHEET_NAME  = os.getenv("B2_RESULT_SHEET_NAME",  "Результати")
B2_CONFIG_SHEET_NAME  = os.getenv("B2_CONFIG_SHEET_NAME",  "Конфігурація")
B2_MARKERS_SHEET_NAME = os.getenv("B2_MARKERS_SHEET_NAME", "Маркери")

# Google Sheets: ID таблиці конфігурації (якщо інша — інакше використовується B2_RESULT_SHEETS_ID)
B2_CONFIG_SHEETS_ID   = os.getenv("B2_CONFIG_SHEETS_ID", "")

# Шлях до service account credentials (default: credentials.json у корені)
B2_CREDENTIALS_PATH   = os.getenv("B2_CREDENTIALS_PATH", "credentials.json")

# Як часто опитувати Binotel на нові завершені дзвінки (хвилини, default: 30)
B2_POLL_INTERVAL_MIN  = int(os.getenv("B2_POLL_INTERVAL_MIN", "30"))

# Лінії для моніторингу (розділені комою; якщо порожньо — використовується дефолтний набір з ТЗ)
_b2_lines_raw = os.getenv("B2_MONITORED_LINES", "")
B2_MONITORED_LINES: set[str] = (
    {l.strip() for l in _b2_lines_raw.split(",") if l.strip()}
    if _b2_lines_raw else set()  # порожній → використовується хардкод в analyzer.py
)

# Виключені лінії (не аналізуємо)
_b2_excl_raw = os.getenv("B2_EXCLUDED_LINES", "910")
B2_EXCLUDED_LINES: set[str] = {l.strip() for l in _b2_excl_raw.split(",") if l.strip()}

# ---------------------------------------------------------------------------
# Модуль 2.1 — Email звітність (щотижнева + щомісячна)
# ---------------------------------------------------------------------------
SMTP_HOST        = os.getenv("SMTP_HOST", "")
SMTP_PORT        = int(os.getenv("SMTP_PORT", "587"))
SMTP_USER        = os.getenv("SMTP_USER", "")
SMTP_PASSWORD    = os.getenv("SMTP_PASSWORD", "")
SMTP_FROM        = os.getenv("SMTP_FROM", "")
B2_REPORT_EMAILS: list[str] = [
    e.strip() for e in os.getenv("B2_REPORT_EMAILS", "").split(",") if e.strip()
]
