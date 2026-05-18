"""Перевірка завантаження довідника процедур з Google Sheets."""
import asyncio
import logging
import sys

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")

async def main():
    from services.sheets_loader import fetch_procedure_aliases
    result = await fetch_procedure_aliases()
    if not result:
        print("ПОМИЛКА: таблиця не завантажена або порожня")
        sys.exit(1)
    lines = result.splitlines()
    print(f"OK: завантажено {len(lines) - 1} записів\n")
    print("--- Перші 5 рядків ---")
    for line in lines[1:6]:
        print(line)
    print("...\n--- Останні 3 рядки ---")
    for line in lines[-3:]:
        print(line)

asyncio.run(main())
