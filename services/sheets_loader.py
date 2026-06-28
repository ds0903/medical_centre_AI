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


def find_by_appointment(appointment_name: str) -> dict | None:
    """
    Шукає запис по офіційній назві послуги (поле APPOINTMENT з API).
    Спочатку точний збіг, потім нечіткий по словах.
    Повертає {analog_name, preparation} або None якщо не знайдено.
    """
    if not _services_cache or not appointment_name:
        return None

    # Касовий код / кінцева пунктуація з API ламають точний збіг з довідником.
    appt_lower = clean_appointment_name(appointment_name).lower()

    # Точний збіг (порівнюємо теж очищену від кінцевої пунктуації назву з довідника)
    for item in _services_cache:
        official_clean = _TRAILING_PUNCT_RE.sub("", item["official_name"].strip()).lower()
        if official_clean == appt_lower:
            return {"analog_name": item["analog_name"], "preparation": item["preparation"], "row_num": item["row_num"]}

    # Нечіткий по словах (без стоп-слів — вони є в десятках рядків і спотворюють score)
    words = [w for w in _SPLIT_RE.split(appt_lower) if len(w) > 2 and w not in _FUZZY_STOPWORDS]
    if not words:
        return None

    best_score, best_item = 0, None
    for item in _services_cache:
        s = _score(words, item["official_name"])
        if s > best_score:
            best_score, best_item = s, item

    # Поріг підвищено до "більшість слів" (раніше "половина") — щоб загальні
    # слова типу "прийом лікаря дитячий" не перебивали реальну спеціалізацію,
    # коли вона не зматчилась через пунктуацію/відмінок.
    if best_item and best_score > len(words) // 2:
        return {"analog_name": best_item["analog_name"], "preparation": best_item["preparation"], "row_num": best_item["row_num"]}

    return None


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
