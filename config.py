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
GEMINI_MODEL   = os.getenv("GEMINI_MODEL", "gemini-live-2.5-flash-native-audio")

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

SCHEDULER_FETCH_HOUR = int(os.getenv("SCHEDULER_FETCH_HOUR", "8"))
SCHEDULER_CALL_HOUR = int(os.getenv("SCHEDULER_CALL_HOUR", "10"))
SCHEDULER_CALL_END_HOUR = int(os.getenv("SCHEDULER_CALL_END_HOUR", "21"))
DAYS_BEFORE_VISIT = int(os.getenv("DAYS_BEFORE_VISIT", "1"))

# Дзвінки
MAX_CALL_ATTEMPTS = int(os.getenv("MAX_CALL_ATTEMPTS", "3"))
PAUSE_BETWEEN_CALLS_SEC = int(os.getenv("PAUSE_BETWEEN_CALLS_SEC", "30"))
CALL_TIMEOUT_SEC = int(os.getenv("CALL_TIMEOUT_SEC", "300"))   # таймаут одного дзвінка (5 хв)

# Silence detection
SILENCE_TIMEOUT_SEC = float(os.getenv("SILENCE_TIMEOUT_SEC", "5.0"))   # секунд тиші → питаємо
SILENCE_START_DELAY_SEC = float(os.getenv("SILENCE_START_DELAY_SEC", "2.5"))  # затримка після turn_complete

# Watchdog: максимальна бездіяльність (ні аудіо, ні tool calls, ні VAD) перед re-inject prompt
WATCHDOG_TIMEOUT_SEC = float(os.getenv("WATCHDOG_TIMEOUT_SEC", "20.0"))

# Echo gate: скільки секунд НЕ передавати аудіо пацієнта в Gemini після interrupted події
# (щоб відлуння від SIP-лінії не викликало хибний повторний interrupted)
INTERRUPTED_PAUSE_SEC = float(os.getenv("INTERRUPTED_PAUSE_SEC", "1.0"))

# Логування розмови: [ПАЦІЄНТ] і [AI] в лог (true = показувати)
GEMINI_LOGS = os.getenv("GEMINI_LOGS", "false").lower() == "true"

# Логування дзвінків у файл logs/calls_YYYY-MM-DD.jsonl
LOG_FILE = os.getenv("LOG_FILE", "false").lower() == "true"
