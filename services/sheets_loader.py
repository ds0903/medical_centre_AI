"""
Завантаження довідника синонімів процедур з Google Sheets (публічний CSV export).
"""

import csv
import io
import logging

import aiohttp

from config import SHEETS_ID, SHEETS_GID

logger = logging.getLogger(__name__)

_EXPORT_URL = (
    "https://docs.google.com/spreadsheets/d/{id}/export"
    "?format=csv&gid={gid}"
)


async def fetch_procedure_aliases() -> str:
    """
    Завантажує таблицю з Google Sheets та повертає відформатований рядок для промпту.

    Формат: "Офіційна назва | синонім1, синонім2, ..."
    Рядки без синонімів пропускаються.
    Повертає порожній рядок якщо SHEETS_ID не задано або помилка.
    """
    if not SHEETS_ID:
        logger.warning("[SHEETS] SHEETS_ID не задано — довідник процедур недоступний")
        return ""

    url = _EXPORT_URL.format(id=SHEETS_ID, gid=SHEETS_GID)
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                resp.raise_for_status()
                text = await resp.text(encoding="utf-8")
    except Exception as e:
        logger.warning("[SHEETS] Не вдалося завантажити таблицю: %s", e)
        return ""

    reader = csv.reader(io.StringIO(text))
    lines: list[str] = []
    for row in reader:
        name = row[0].strip() if len(row) > 0 else ""
        aliases = row[1].strip() if len(row) > 1 else ""
        # пропускаємо порожні рядки та заголовок
        if not name or name.lower() == "найменування":
            continue
        if aliases:
            lines.append(f"{name} | {aliases}")

    if not lines:
        logger.warning("[SHEETS] Таблиця завантажена але порожня")
        return ""

    logger.info("[SHEETS] Довідник процедур: %d записів", len(lines))
    header = (
        "ДОВІДНИК ПРОЦЕДУР — ТІЛЬКИ для пояснення назв. БІЛЬШЕ НІЧОГО.\n"
        "Використовуй ВИКЛЮЧНО якщо пацієнт сам запитав: 'що це таке?', 'що означає X?', 'для чого ця процедура?'.\n"
        "Ми займаємось ЗАПИСОМ до лікаря — не самим прийомом. Довідник НЕ впливає на запис.\n"
        "⚠ ЗАБОРОНЕНО: використовувати довідник для уточнення {doctor_name}, пропонувати запис на процедури з довідника.\n"
        "⚠ ЗАБОРОНЕНО: активувати довідник якщо пацієнт не питав про процедуру явно.\n"
        "Алгоритм:\n"
        "  1. Пацієнт запитав назву/суть процедури → шукай в довіднику.\n"
        "  2. Знайшов → поясни 1-2 реченнями. Потім: 'Ви плануєте прийти чи перенести запис?'\n"
        "  3. Не знайшов → 'Для уточнення з'єдную з менеджером.' → transfer_to_operator.\n"
        "Формат: Офіційна назва | синоніми пацієнта\n"
    )
    return header + "\n".join(lines)
