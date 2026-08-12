"""
Одноразовий setup скрипт — Модуль 2.2 (B3).

Створює в тому ж Google Sheets документі що і Модуль 1 (SHEETS_ID) три нові аркуші:
  - «Результати»    — результати аналізу дзвінків
  - «Конфігурація» — критерії оцінки (замовник редагує самостійно)
  - «Маркери»       — обов'язкові фрази (замовник редагує самостійно)

Після створення автоматично:
  - Записує B2_RESULT_SHEETS_ID у .env (якщо ще не задано)
  - Додає секцію SMTP у .env (якщо ще не задано)

Запуск (один раз):
    python setup_b3_sheets.py
"""

import os
import sys

from dotenv import load_dotenv

load_dotenv()

SPREADSHEET_ID   = os.getenv("B2_RESULT_SHEETS_ID") or os.getenv("SHEETS_ID", "")
CREDENTIALS_PATH = os.getenv("B2_CREDENTIALS_PATH", "credentials.json")
CONFIG_SHEET     = os.getenv("B2_CONFIG_SHEET_NAME",  "Конфігурація")
MARKERS_SHEET    = os.getenv("B2_MARKERS_SHEET_NAME", "Маркери")
RESULTS_SHEET    = os.getenv("B2_RESULT_SHEET_NAME",  "Результати")
ENV_FILE         = os.path.join(os.path.dirname(__file__), ".env")


# ---------------------------------------------------------------------------
# Структура аркушів
# ---------------------------------------------------------------------------

RESULTS_HEADERS = [
    "ID дзвінка", "Дата/час", "Лінія", "Оператор", "Напрям",
    "Тривалість (с)", "Оцінка (%)", "AI коментар", "Маркери (є)", "Маркери (немає)",
]

CONFIG_HEADERS = ["Блок", "Назва", "Індикатор", "Активний", "Вага"]

CONFIG_CRITERIA = [
    ["Стандарти та етикет",
     "Коректне привітання",
     "Оператор привітався фразою «Доброго ранку/дня/вечора! Оператор [Ім'я]. Слухаю вас!»",
     "Так", "1.0"],
    ["Стандарти та етикет",
     "Прощання",
     "Оператор завершив розмову фразою «Чекаємо вас. На все добре!»",
     "Так", "1.0"],
    ["Кваліфікація запиту та експертність",
     "Уточнення локації",
     "При запиті послуги оператор уточнив локацію: мкрн. Сонячний або вул. Гірничих Інженерів "
     "(не потрібно для конкретного лікаря / МРТ / рентгену / ЕНМГ)",
     "Так", "1.0"],
    ["Кваліфікація запиту та експертність",
     "Уточнення вікової групи",
     "При запиті послуги оператор уточнив: для дитини чи дорослого / вік пацієнта "
     "(не потрібно для конкретного лікаря / МРТ / рентгену / ЕНМГ)",
     "Так", "1.0"],
    ["Кваліфікація запиту та експертність",
     "Збір персональних даних",
     "Оператор запросив прізвище, ім'я, по батькові та контактний номер телефону пацієнта",
     "Так", "1.0"],
    ["Комунікативні навички та емпатія",
     "Активне слухання",
     "Оператор не перебивав клієнта, давав завершити думку, реагував доречно",
     "Так", "1.0"],
    ["Комунікативні навички та емпатія",
     "Відсутність грубості",
     "Оператор спілкувався ввічливо, без роздратування, підвищеного тону або зневажливих фраз",
     "Так", "1.0"],
    ["Резюмування розмови",
     "Повторення деталей запису",
     "При успішному записі оператор повторив: лікар/послуга, дата, час, адреса",
     "Так", "1.0"],
]

MARKERS_HEADERS = ["Фраза", "Умова", "Активний"]

MARKERS_DATA = [
    ["Слухаю вас",
     "У вітанні на початку кожного дзвінка",
     "Так"],
    ["Де вам буде зручно? Мкрн. Сонячний або вул. Гірничих Інженерів",
     "Коли клієнт запитує ПОСЛУГУ (не конкретного лікаря / МРТ)",
     "Так"],
    ["Для дитини або для дорослого",
     "Коли клієнт запитує ПОСЛУГУ (не конкретного лікаря / МРТ / рентгену / ЕНМГ)",
     "Так"],
    ["Назвіть прізвище, ім'я, по батькові",
     "При оформленні запису або заявки",
     "Так"],
    ["Ви записані до",
     "Після успішного запису — підтвердження деталей",
     "Так"],
    ["Чекаємо вас. На все добре",
     "Наприкінці кожної розмови",
     "Так"],
    ["Невідкладна допомога, диспетчер",
     "Привітання диспетчера НП (лінія 908) на початку дзвінка",
     "Так"],
    ["Ваш виклик прийнято",
     "Після підтвердження виклику бригади НП",
     "Так"],
]

SMTP_BLOCK = """
# ---------------------------------------------------------------------------
# Модуль 3 (2.2) — Email звітність
# ---------------------------------------------------------------------------
SMTP_HOST=smtp.gmail.com
SMTP_PORT=587
SMTP_USER=
SMTP_PASSWORD=
SMTP_FROM=
B3_REPORT_EMAILS=
"""


# ---------------------------------------------------------------------------
# Sheets API helpers
# ---------------------------------------------------------------------------

def get_service(credentials_path: str):
    from google.oauth2 import service_account
    from googleapiclient.discovery import build
    creds = service_account.Credentials.from_service_account_file(
        credentials_path,
        scopes=["https://www.googleapis.com/auth/spreadsheets"],
    )
    return build("sheets", "v4", credentials=creds, cache_discovery=False)


def get_existing_sheets(service, spreadsheet_id: str) -> dict[str, int]:
    meta = service.spreadsheets().get(spreadsheetId=spreadsheet_id).execute()
    return {s["properties"]["title"]: s["properties"]["sheetId"] for s in meta["sheets"]}


def create_sheet(service, spreadsheet_id: str, title: str) -> int:
    resp = service.spreadsheets().batchUpdate(
        spreadsheetId=spreadsheet_id,
        body={"requests": [{"addSheet": {"properties": {"title": title}}}]},
    ).execute()
    return resp["replies"][0]["addSheet"]["properties"]["sheetId"]


def write_rows(service, spreadsheet_id: str, sheet_name: str, rows: list[list]) -> None:
    service.spreadsheets().values().update(
        spreadsheetId=spreadsheet_id,
        range=f"{sheet_name}!A1",
        valueInputOption="RAW",
        body={"values": rows},
    ).execute()


def format_sheet(service, spreadsheet_id: str, sheet_id: int, num_cols: int) -> None:
    service.spreadsheets().batchUpdate(
        spreadsheetId=spreadsheet_id,
        body={"requests": [
            # Жирний заголовок
            {"repeatCell": {
                "range": {
                    "sheetId": sheet_id,
                    "startRowIndex": 0, "endRowIndex": 1,
                    "startColumnIndex": 0, "endColumnIndex": num_cols,
                },
                "cell": {"userEnteredFormat": {"textFormat": {"bold": True}}},
                "fields": "userEnteredFormat.textFormat.bold",
            }},
            # Заморозити перший рядок
            {"updateSheetProperties": {
                "properties": {
                    "sheetId": sheet_id,
                    "gridProperties": {"frozenRowCount": 1},
                },
                "fields": "gridProperties.frozenRowCount",
            }},
        ]},
    ).execute()


# ---------------------------------------------------------------------------
# .env helpers
# ---------------------------------------------------------------------------

def env_has_key(key: str) -> bool:
    if not os.path.exists(ENV_FILE):
        return False
    with open(ENV_FILE, encoding="utf-8") as f:
        for line in f:
            if line.strip().startswith(f"{key}="):
                return True
    return False


def env_append(text: str) -> None:
    with open(ENV_FILE, "a", encoding="utf-8") as f:
        f.write(text)


def env_set_key(key: str, value: str) -> None:
    """Записує KEY=value в .env якщо ключа ще немає."""
    if env_has_key(key):
        return
    with open(ENV_FILE, "a", encoding="utf-8") as f:
        f.write(f"\n{key}={value}\n")
    print(f"  → .env: {key}={value}")


# ---------------------------------------------------------------------------
# Setup одного аркуша
# ---------------------------------------------------------------------------

def setup_sheet(service, spreadsheet_id: str, existing: dict[str, int],
                title: str, rows: list[list]) -> None:
    if title in existing:
        print(f"  ⚡ «{title}» вже існує — пропускаємо")
        return
    print(f"  + Створюємо «{title}»...")
    sheet_id = create_sheet(service, spreadsheet_id, title)
    write_rows(service, spreadsheet_id, title, rows)
    format_sheet(service, spreadsheet_id, sheet_id, len(rows[0]))
    print(f"    ✓ «{title}» готовий ({len(rows) - 1} рядків даних)")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    if not SPREADSHEET_ID:
        print("ПОМИЛКА: SHEETS_ID (або B2_RESULT_SHEETS_ID) не задано в .env")
        sys.exit(1)
    if not os.path.exists(CREDENTIALS_PATH):
        print(f"ПОМИЛКА: credentials файл не знайдено: {CREDENTIALS_PATH}")
        sys.exit(1)

    print(f"\n=== Setup Модуль 3 (2.2) — Google Sheets ===")
    print(f"Документ ID    : {SPREADSHEET_ID}")
    print(f"Credentials    : {CREDENTIALS_PATH}")
    print()

    try:
        service = get_service(CREDENTIALS_PATH)
    except Exception as e:
        print(f"ПОМИЛКА підключення до Google API: {e}")
        sys.exit(1)

    try:
        existing = get_existing_sheets(service, SPREADSHEET_ID)
    except Exception as e:
        print(f"ПОМИЛКА отримання аркушів: {e}")
        sys.exit(1)

    print(f"Існуючі аркуші: {list(existing.keys())}\n")

    # Створюємо аркуші
    setup_sheet(service, SPREADSHEET_ID, existing,
                RESULTS_SHEET, [RESULTS_HEADERS])

    setup_sheet(service, SPREADSHEET_ID, existing,
                CONFIG_SHEET, [CONFIG_HEADERS] + CONFIG_CRITERIA)

    setup_sheet(service, SPREADSHEET_ID, existing,
                MARKERS_SHEET, [MARKERS_HEADERS] + MARKERS_DATA)

    # Записуємо B2_RESULT_SHEETS_ID в .env якщо ще немає
    print()
    if os.path.exists(ENV_FILE):
        env_set_key("B2_RESULT_SHEETS_ID", SPREADSHEET_ID)

        # Додаємо SMTP секцію якщо ще немає
        if not env_has_key("SMTP_HOST"):
            env_append(SMTP_BLOCK)
            print("  → .env: додано секцію SMTP (заповніть значення)")
        else:
            print("  ⚡ SMTP вже є в .env — не чіпаємо")
    else:
        print(f"  ⚠ .env не знайдено — запишіть вручну:")
        print(f"    B2_RESULT_SHEETS_ID={SPREADSHEET_ID}")

    print(f"\n✓ Готово!")
    print(f"  Документ: https://docs.google.com/spreadsheets/d/{SPREADSHEET_ID}")
    print()
    print("Наступні кроки:")
    print("  1. Заповніть SMTP_* у .env для email звітів")
    print("  2. Відредагуйте аркуш «Конфігурація» під ваш стандарт")
    print("  3. Відредагуйте аркуш «Маркери» — додайте/прибирайте фрази")
    print("  4. python main_b3.py")


if __name__ == "__main__":
    main()
