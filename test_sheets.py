"""Перевірка завантаження обох аркушів з Google Sheets."""
import asyncio
import logging
import sys

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")


async def test_aliases():
    from services.sheets_loader import fetch_procedure_aliases
    print("=== Аркуш 1: синоніми процедур ===")
    result = await fetch_procedure_aliases()
    if not result:
        print("ПОМИЛКА: аркуш 1 не завантажено або порожній")
        return False
    lines = result.splitlines()
    print(f"OK: {len(lines) - 1} записів\n")
    print("--- Перші 5 ---")
    for line in lines[1:6]:
        print(line)
    print("...\n--- Останні 3 ---")
    for line in lines[-3:]:
        print(line)
    return True


async def test_services():
    from services.sheets_loader import fetch_services_catalog, search_service
    print("\n=== Аркуш 2: прайс + підготовка ===")
    items = await fetch_services_catalog()
    if not items:
        print("ПОМИЛКА: аркуш 2 не завантажено або порожній")
        return False
    print(f"OK: {len(items)} записів\n")
    print("--- Перші 5 ---")
    for item in items[:5]:
        print(f"  {item['name']} | {item['price']} грн | {item['preparation'][:60]}...")
    print("...\n--- Останні 3 ---")
    for item in items[-3:]:
        print(f"  {item['name']} | {item['price']} грн | {item['preparation'][:60]}...")

    # Тест пошуку
    print("\n--- Тест пошуку ---")
    # Оновлюємо кеш щоб search_service працював
    from services import sheets_loader
    sheets_loader._services_cache = items

    for query in ["гастроентеролог", "УЗД", "гінеколог"]:
        results = search_service(query)
        print(f"  search('{query}') → {len(results)} збігів")
        for r in results:
            print(f"    • {r['name']}")
    return True


async def main():
    ok1 = await test_aliases()
    ok2 = await test_services()
    if not ok1 or not ok2:
        sys.exit(1)


asyncio.run(main())
