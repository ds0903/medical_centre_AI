"""
Завантаження каталогу послуг з Google Sheets (публічний CSV export).

Один аркуш (SHEETS_GID):
  A: Коди каси (ігноруємо)
  B: Найменування згідно Прайсу  ← шукаємо по цьому полю (= APPOINTMENT з API)
  C: Аналог назви                ← говоримо пацієнту
  D: Підготовка                  ← відповідаємо якщо пацієнт питає
"""

import csv
import io
import logging
import re

import aiohttp

from config import SHEETS_ID, SHEETS_GID

logger = logging.getLogger(__name__)

_EXPORT_URL = (
    "https://docs.google.com/spreadsheets/d/{id}/export"
    "?format=csv&gid={gid}"
)

# Кеш: [{official_name, analog_name, preparation}, ...]
_services_cache: list[dict] = []

# AI-верифікований кеш: appointment_name → {analog_name, preparation, row_num} | None
# Заповнюється під час warmup через ai_verify_service().
# find_by_appointment() перевіряє його першим — AI результат має пріоритет над fuzzy.
_ai_verified_cache: dict[str, dict | None] = {}


async def _fetch_csv(gid: str) -> str | None:
    if not SHEETS_ID:
        return None
    url = _EXPORT_URL.format(id=SHEETS_ID, gid=gid)
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                resp.raise_for_status()
                return await resp.text(encoding="utf-8")
    except Exception as e:
        logger.warning("[SHEETS] Не вдалося завантажити (gid=%s): %s", gid, e)
        return None


async def fetch_services_catalog() -> list[dict]:
    """
    Завантажує каталог з одного аркуша.
    Структура CSV: код | офіційна назва | аналог назви | підготовка
    """
    if not SHEETS_ID:
        logger.warning("[SHEETS] SHEETS_ID не задано — каталог послуг недоступний")
        return []

    text = await _fetch_csv(SHEETS_GID)
    if text is None:
        return []

    reader = csv.reader(io.StringIO(text))
    items: list[dict] = []
    for row_num, row in enumerate(reader, start=1):
        # A=код | B=офіційна назва | C=ціна (ігноруємо) | D=аналог назви | E=підготовка
        official_name = row[1].strip() if len(row) > 1 else ""
        analog_name   = row[3].strip() if len(row) > 3 else ""
        preparation   = row[4].strip() if len(row) > 4 else ""
        if not official_name or official_name.lower() in ("найменування згідно прайсу", "найменування", "назва"):
            continue
        items.append({
            "official_name": official_name,
            "analog_name":   analog_name or official_name,
            "preparation":   preparation,
            "row_num":       row_num,
        })

    logger.info("[SHEETS] Каталог послуг: %d записів", len(items))
    return items


async def load_services_catalog() -> None:
    """Завантажує каталог і зберігає в модульний кеш."""
    global _services_cache
    _services_cache = await fetch_services_catalog()


_SPLIT_RE = re.compile(r"[\s\-\(\)/,\.;:]+")

# Бухгалтерські/юридичні слова що присутні в 50+ рядках каталогу — не описують процедуру,
# тільки спотворюють fuzzy score (напр. "без урахування витрат по контрастуванню").
_FUZZY_STOPWORDS = frozenset({
    "без", "по", "та", "для", "при", "або", "що", "від", "до", "на", "за",
    "урахування", "витрат", "контрастуванню", "посиленням", "контрастним",
    "посилення", "контраст", "включно", "включаючи", "уточнення",
})


def _score(query_words: list[str], name: str) -> int:
    name_words = _SPLIT_RE.split(name.lower())
    return sum(
        1 for w in query_words
        if w not in _FUZZY_STOPWORDS and any(nw == w or nw.startswith(w) for nw in name_words if nw)
    )


_LEADING_DIGITS_RE = re.compile(r"^\d+[\s\.\-]*")
_TRAILING_PUNCT_RE = re.compile(r"[\s;:,.\-]+$")


def clean_appointment_name(appointment_name: str) -> str:
    """Прибирає касовий код (лідируючі цифри з API) і кінцеву пунктуацію — використовується,
    коли аналог з довідника не знайдено і озвучується сире поле 'Процедура'."""
    if not appointment_name:
        return appointment_name
    cleaned = _LEADING_DIGITS_RE.sub("", appointment_name.strip())
    return _TRAILING_PUNCT_RE.sub("", cleaned)


def _find_exact(appt_lower: str) -> dict | None:
    """Точний збіг по очищеній lowercase назві."""
    for item in _services_cache:
        official_clean = _TRAILING_PUNCT_RE.sub("", item["official_name"].strip()).lower()
        if official_clean == appt_lower:
            return {"analog_name": item["analog_name"], "preparation": item["preparation"], "row_num": item["row_num"]}
    return None


def _get_fuzzy_candidates(appointment_name: str, top_n: int = 5) -> list[dict]:
    """Топ-N fuzzy кандидатів по словах (без стоп-слів, score > len(words)//2)."""
    appt_lower = clean_appointment_name(appointment_name).lower()
    words = [w for w in _SPLIT_RE.split(appt_lower) if len(w) > 2 and w not in _FUZZY_STOPWORDS]
    if not words:
        return []
    threshold = len(words) // 2
    scored = []
    for item in _services_cache:
        s = _score(words, item["official_name"])
        if s > threshold:
            scored.append((s, item))
    scored.sort(key=lambda x: x[0], reverse=True)
    return [item for _, item in scored[:top_n]]


def find_by_appointment(appointment_name: str) -> dict | None:
    """
    Шукає запис по офіційній назві послуги (поле APPOINTMENT з API).
    Пріоритет: AI-верифікований кеш → точний збіг → fuzzy.
    Повертає {analog_name, preparation, row_num} або None якщо не знайдено.
    """
    if not _services_cache or not appointment_name:
        return None

    # AI-верифікований кеш (заповнюється під час warmup)
    if appointment_name in _ai_verified_cache:
        return _ai_verified_cache[appointment_name]

    # Касовий код / кінцева пунктуація з API ламають точний збіг з довідником.
    appt_lower = clean_appointment_name(appointment_name).lower()

    # Точний збіг
    exact = _find_exact(appt_lower)
    if exact:
        return exact

    # Fuzzy — найкращий кандидат (той самий алгоритм що й раніше)
    candidates = _get_fuzzy_candidates(appointment_name, top_n=1)
    if candidates:
        item = candidates[0]
        return {"analog_name": item["analog_name"], "preparation": item["preparation"], "row_num": item["row_num"]}

    return None


async def ai_verify_service(appointment_name: str, doctor_specialization: str = "") -> None:
    """
    AI-верифікація вибору з каталогу — запускається під час warmup паралельно з гудками.
    Знаходить найкращий матч серед fuzzy кандидатів і кешує в _ai_verified_cache.
    Якщо є точний збіг — кешує без AI виклику.
    """
    if not appointment_name or not _services_cache:
        return
    if appointment_name in _ai_verified_cache:
        return  # вже в кеші

    appt_lower = clean_appointment_name(appointment_name).lower()

    # Exact match — надійний, AI не потрібен
    exact = _find_exact(appt_lower)
    if exact:
        _ai_verified_cache[appointment_name] = exact
        logger.debug(f"[SHEETS] AI verify: точний збіг → '{exact['analog_name']}'")
        return

    # Топ-5 fuzzy кандидатів
    candidates = _get_fuzzy_candidates(appointment_name, top_n=5)
    if not candidates:
        _ai_verified_cache[appointment_name] = None
        logger.debug(f"[SHEETS] AI verify: кандидатів не знайдено для '{appointment_name}'")
        return

    if len(candidates) == 1:
        # Єдиний кандидат — AI не потрібен
        item = candidates[0]
        _ai_verified_cache[appointment_name] = {
            "analog_name": item["analog_name"],
            "preparation": item["preparation"],
            "row_num": item["row_num"],
        }
        logger.debug(f"[SHEETS] AI verify: єдиний кандидат → '{item['analog_name']}'")
        return

    # Кілька кандидатів — питаємо AI
    candidates_text = "\n".join(
        f"{i+1}. \"{c['official_name']}\" → назва для пацієнта: \"{c['analog_name']}\""
        for i, c in enumerate(candidates)
    )
    prompt = (
        f"Поле APPOINTMENT з API клініки: \"{appointment_name}\"\n"
        f"Спеціалізація лікаря: \"{doctor_specialization}\"\n"
        f"Кандидати з каталогу:\n{candidates_text}\n\n"
        f"Який номер кандидата найкраще відповідає полю APPOINTMENT? "
        f"Відповідь: ТІЛЬКИ одна цифра від 1 до {len(candidates)}, або 0 якщо жоден не підходить."
    )

    try:
        from config import GEMINI_API_KEY, GEMINI_FLASH_MODEL
        import google.genai as genai
        client = genai.Client(api_key=GEMINI_API_KEY)
        response = await client.aio.models.generate_content(
            model=GEMINI_FLASH_MODEL,
            contents=prompt,
        )
        raw = (response.text or "").strip()
        digit = int(raw[0]) if raw and raw[0].isdigit() else 0

        if 1 <= digit <= len(candidates):
            chosen = candidates[digit - 1]
            _ai_verified_cache[appointment_name] = {
                "analog_name": chosen["analog_name"],
                "preparation": chosen["preparation"],
                "row_num": chosen["row_num"],
            }
            logger.info(
                f"[SHEETS] AI verify: '{appointment_name}' → #{digit} '{chosen['analog_name']}'"
            )
        else:
            # AI не впевнений або відповів некоректно — беремо найкращий fuzzy
            item = candidates[0]
            _ai_verified_cache[appointment_name] = {
                "analog_name": item["analog_name"],
                "preparation": item["preparation"],
                "row_num": item["row_num"],
            }
            logger.info(
                f"[SHEETS] AI verify: '{appointment_name}' → відповідь '{raw}', "
                f"fallback fuzzy best '{item['analog_name']}'"
            )
    except Exception as e:
        logger.warning(f"[SHEETS] AI verify error (non-critical): {e!r}")
        # Кеш НЕ встановлюємо → find_by_appointment() використає звичайний fuzzy


def search_service(query: str) -> list[dict]:
    """
    Пошук під час дзвінка (get_preparation_info tool).
    Шукає по запиту пацієнта → повертає до 3 збігів з analog_name та preparation.
    """
    if not _services_cache:
        return []

    query_lower = query.lower().strip()
    words = [w for w in _SPLIT_RE.split(query_lower) if len(w) > 2]
    if not words:
        return []

    results: list[tuple[int, dict]] = []
    for item in _services_cache:
        # шукаємо і по офіційній, і по аналогу
        s = max(_score(words, item["official_name"]), _score(words, item["analog_name"]))
        if s > 0:
            results.append((s, item))

    results.sort(key=lambda x: x[0], reverse=True)
    return [item for _, item in results[:3]]
