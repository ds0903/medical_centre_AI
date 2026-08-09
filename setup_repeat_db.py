"""
Скрипт: додати колонку idpoint + вставити 10 тестових записів у repeat_call_tasks.
Запуск: python setup_repeat_db.py
"""
import asyncio
import json
import os
import uuid
from datetime import datetime, timezone

from dotenv import load_dotenv
load_dotenv()

DATABASE_URL = os.getenv("DATABASE_URL", "postgresql+asyncpg://danil:danilus15@localhost:5432/medical_centre")
# asyncpg потребує формат без +asyncpg
PG_URL = DATABASE_URL.replace("postgresql+asyncpg://", "postgresql://")

JSON_PATH = os.path.join(os.path.dirname(__file__), "data", "test_repeat_visits.json")


def _normalize_phone(raw: str) -> str:
    p = raw.replace("-", "").replace(" ", "").replace("+", "")
    if p.startswith("380"):
        return "+" + p
    if p.startswith("0") and len(p) == 10:
        return "+38" + p
    return p


async def main():
    import asyncpg
    conn = await asyncpg.connect(PG_URL)

    # 1. Додати колонку idpoint якщо немає
    await conn.execute("ALTER TABLE repeat_call_tasks   ADD COLUMN IF NOT EXISTS idpoint INTEGER;")
    await conn.execute("ALTER TABLE repeat_call_history ADD COLUMN IF NOT EXISTS idpoint INTEGER;")
    print("[OK] Колонки idpoint додано (або вже були)")

    # 2. Очистити старі тестові записи (GLOBALKEY починається з 003100220254810)
    deleted = await conn.execute(
        "DELETE FROM repeat_call_tasks WHERE source_visit_id LIKE '003100220254810%'"
    )
    print(f"[OK] Видалено старих тестових записів: {deleted}")

    # 3. Завантажити тестові дані
    with open(JSON_PATH, encoding="utf-8") as f:
        records = json.load(f)

    now = datetime.now(timezone.utc).replace(tzinfo=None)
    inserted = 0

    for r in records:
        phone = _normalize_phone(str(r.get("PATPHONE", "")))
        repeat_date = None
        try:
            repeat_date = datetime.fromisoformat(r["ONDATE"])
        except Exception:
            pass

        await conn.execute("""
            INSERT INTO repeat_call_tasks
              (id, source_visit_id, patient_name, patient_phone, patient_id,
               previous_visit_date, previous_doctor, doctor_id, doctor_name,
               doctor_specialization, doctor_recommendation, repeat_visit_date,
               idpoint, status, attempts, created_at)
            VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,'pending',0,$14)
            ON CONFLICT (source_visit_id) DO NOTHING
        """,
            uuid.uuid4(),
            str(r["GLOBALKEY"]),
            str(r["PATIENT"]),
            phone,
            str(r.get("GLOBALID", "")),
            None,                            # previous_visit_date
            "",                              # previous_doctor
            str(r.get("PERFORMER_ID", "")),  # doctor_id
            str(r["DOCTORNAME"]),
            str(r.get("SPECIALITY_AI", "")),
            "",                              # doctor_recommendation
            repeat_date,
            int(r.get("IDPOINT", 0)),
            now,
        )
        inserted += 1
        print(f"  + {r['PATIENT']} | {r['DOCTORNAME']} | IDPOINT={r['IDPOINT']} | {phone}")

    total = await conn.fetchval("SELECT COUNT(*) FROM repeat_call_tasks WHERE status='pending'")
    print(f"\n[OK] Вставлено: {inserted} | Всього pending у repeat_call_tasks: {total}")
    await conn.close()


if __name__ == "__main__":
    asyncio.run(main())
