import os
from dotenv import load_dotenv

load_dotenv()

# Режим роботи
ENV = os.getenv("ENV", "debug")
DEBUG = ENV == "debug"

# Gemini
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
GEMINI_MODEL = "gemini-2.5-flash-native-audio-preview-12-2025"

# PostgreSQL
DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "postgresql+asyncpg://postgres:postgres@localhost:5432/medical_centre"
)

# Binotel
BINOTEL_API_KEY = os.getenv("BINOTEL_API_KEY", "")
BINOTEL_API_SECRET = os.getenv("BINOTEL_API_SECRET", "")
BINOTEL_API_URL = os.getenv("BINOTEL_API_URL", "https://api.binotel.com")

# API клініки
CLINIC_API_URL = os.getenv("CLINIC_API_URL", "")
CLINIC_API_KEY = os.getenv("CLINIC_API_KEY", "")

# Планувальник
SCHEDULER_FETCH_HOUR = int(os.getenv("SCHEDULER_FETCH_HOUR", "8"))
SCHEDULER_CALL_HOUR = int(os.getenv("SCHEDULER_CALL_HOUR", "10"))
DAYS_BEFORE_VISIT = int(os.getenv("DAYS_BEFORE_VISIT", "1"))

# Дзвінки
MAX_CALL_ATTEMPTS = int(os.getenv("MAX_CALL_ATTEMPTS", "3"))
PAUSE_BETWEEN_CALLS_SEC = int(os.getenv("PAUSE_BETWEEN_CALLS_SEC", "30"))
