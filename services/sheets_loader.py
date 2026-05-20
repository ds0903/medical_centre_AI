"""
Завантаження довідників з Google Sheets (публічний CSV export).

Аркуш 1 (SHEETS_GID)   — синоніми процедур → рядок для промпту
Аркуш 2 (SERVICES_GID) — прайс + підготовка → list[dict] для function calling
"""

import csv
import io
import logging
import re

import aiohttp

from config import SHEETS_ID, SHEETS_GID, SERVICES_GID

logger = logging.getLogger(__name__)

_EXPORT_URL = (
    "https://docs.google.com/spreadsheets/d/{id}/export"
    "?format=csv&gid={gid}"
)

# Кеш Аркуша 2: [{name, price, preparation}, ...]
_services_cache: list[dict] = []


async def _fetch_csv(gid: str) -> str | None:
    """Завантажує CSV з Google Sheets. Повертає None при помилці."""
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


async def fetch_procedure_aliases() -> str:
    """
    Аркуш 1: синоніми процедур → відформатований рядок для системного промпту.
    Формат: "Офіційна назва | синонім1, синонім2, ..."
    """
    if not SHEETS_ID:
        logger.warning("[SHEETS] SHEETS_ID не задано — довідник синонімів недоступний")
        return ""

    text = await _fetch_csv(SHEETS_GID)
    if text is None:
        return ""

    reader = csv.reader(io.StringIO(text))
    lines: list[str] = []
    for row in reader:
        name = row[0].strip() if len(row) > 0 else ""
        aliases = row[1].strip() if len(row) > 1 else ""
        if not name or name.lower() == "найменування":
            continue
        if aliases:
            lines.append(f"{name} | {aliases}")

    if not lines:
        logger.warning("[SHEETS] Аркуш 1 завантажено але порожній")
        return ""

    logger.info("[SHEETS] Довідник синонімів: %d записів", len(lines))
    header = (
        "ДОВІДНИК ПРОЦЕДУР — ТІЛЬКИ для пояснення назв. БІЛЬШЕ НІЧОГО.\n"
        "Використовуй ВИКЛЮЧНО якщо пацієнт сам запитав: 'що це таке?', 'що означає X?', 'для чого ця процедура?'.\n"
        "Ми займаємось ЗАПИСОМ до лікаря — не самим прийомом. Довідник НЕ впливає на запис.\n"
        "⚠ ЗАБОРОНЕНО: використовувати довідник для уточнення {doctor_name}, пропонувати запис на процедури з довідника.\n"
        "⚠ ЗАБОРОНЕНО: активувати довідник якщо пацієнт не питав про процедуру явно.\n"
        "Алгоритм (ТІЛЬКИ для питань 'що це таке?' / 'що означає X?'):\n"
        "  1. Знайшов в довіднику → поясни 3-4 реченнями → 'Ви плануєте прийти?' НЕ пропонуй менеджера.\n"
        "  2. Не знайшов → поверни пошук на підготовку: виклич get_preparation_info(service_name=X).\n"
        "⚠ ЦЕЙ ДОВІДНИК НЕ відповідає на 'як підготуватись?' або 'що взяти?' — для цього є get_preparation_info.\n"
        "Формат: Офіційна назва | синоніми пацієнта\n"
    )
    return header + "\n".join(lines)


async def fetch_services_catalog() -> list[dict]:
    """
    Аркуш 2: прайс + підготовка → list[dict] для function calling.
    Структура CSV: код | назва | ціна | підготовка
    Повертає [{name, price, preparation}, ...]
    """
    if not SHEETS_ID:
        logger.warning("[SHEETS] SHEETS_ID не задано — каталог послуг недоступний")
        return []

    if not SERVICES_GID:
        logger.warning("[SHEETS] SERVICES_GID не задано — каталог послуг недоступний")
        return []

    text = await _fetch_csv(SERVICES_GID)
    if text is None:
        return []

    reader = csv.reader(io.StringIO(text))
    items: list[dict] = []
    for row in reader:
        name = row[1].strip() if len(row) > 1 else ""
        price = row[2].strip() if len(row) > 2 else ""
        preparation = row[3].strip() if len(row) > 3 else ""
        if not name or name.lower() in ("найменування", "назва"):
            continue
        items.append({
            "name": name,
            "price": price,
            "preparation": preparation,
        })

    logger.info("[SHEETS] Каталог послуг: %d записів", len(items))
    return items


async def load_services_catalog() -> None:
    """Завантажує каталог послуг і зберігає в модульний кеш."""
    global _services_cache
    _services_cache = await fetch_services_catalog()


_SPLIT_RE = re.compile(r"[\s\-\(\)/,\.]+")


def search_service(query: str) -> list[dict]:
    """
    Пошук по _services_cache з матчингом по цілих словах.
    "УЗД" не матчить "вуздечки" — тільки окреме слово або початок слова.
    Повертає до 3 найбільш релевантних збігів.
    """
    if not _services_cache:
        return []

    query_lower = query.lower().strip()
    words = [w for w in query_lower.split() if len(w) > 2]
    if not words:
        return []

    results: list[tuple[int, dict]] = []
    for item in _services_cache:
        name_words = _SPLIT_RE.split(item["name"].lower())
        # слово з query матчить якщо є слово в назві що з нього починається
        # "гастроентеролог" матчить "гастроентеролога" (відмінок), але "узд" ≠ "вуздечки"
        score = sum(
            1 for w in words
            if any(nw == w or nw.startswith(w) for nw in name_words if nw)
        )
        if score > 0:
            results.append((score, item))

    results.sort(key=lambda x: x[0], reverse=True)
    return [item for _, item in results[:3]]
