"""
Seed script: 25 repeat_call_tasks для тестування М2.
5 пацієнтів × 5 задач. Лікар: МАТЯХ АНАСТАСІЯ ВОЛОДИМИРІВНА (IDPOINT=1).
Запуск: python scripts/seed_repeat_tasks.py
"""

import asyncio
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from db.database import get_session, init_db
from db.models import RepeatCallStatus, RepeatCallTask

DOCTOR_NAME = "МАТЯХ АНАСТАСІЯ ВОЛОДИМИРІВНА"
IDPOINT     = 1

PATIENTS = [
    ("ГЕРАСИМОВА ЛЮДМИЛА ВІТАЛІЇВНА",      "0973812619"),
    ("ГЛІНИСТА МАРИНА",                    "0685177966"),
    ("МАЛИХІНА АНАСТАСІЯ ОЛЕКСАНДРІВНА",   "0986115758"),
    ("ПРОНЬКО ЛІНА ОЛЕКСАНДРІВНА",         "0983871435"),
    ("ДУБОВА ТЕТЯНА ОЛЕГІВНА",             "0967128945"),
]

CALLS_PER_PATIENT = 5


async def main() -> None:
    await init_db()

    now   = datetime.now(timezone.utc).replace(tzinfo=None)
    today = datetime.now().date()

    created = 0
    async with get_session() as session:
        for patient_name, phone in PATIENTS:
            for i in range(CALLS_PER_PATIENT):
                source_id = f"TEST_{patient_name[:8].replace(' ', '_')}_{i+1:02d}"
                task = RepeatCallTask(
                    id                  = uuid.uuid4(),
                    source_visit_id     = source_id,
                    patient_name        = patient_name,
                    patient_phone       = phone,
                    patient_id          = None,
                    previous_visit_date = now - timedelta(days=30),
                    previous_doctor     = DOCTOR_NAME,
                    doctor_id           = None,
                    doctor_name         = DOCTOR_NAME,
                    doctor_specialization = "",
                    doctor_recommendation = "Повторний огляд через місяць",
                    repeat_visit_date   = datetime.combine(today + timedelta(days=2), datetime.min.time()),
                    idpoint             = IDPOINT,
                    status              = RepeatCallStatus.pending,
                    attempts            = 0,
                )
                session.add(task)
                created += 1

    print(f"✓ Створено {created} задач у repeat_call_tasks")


if __name__ == "__main__":
    asyncio.run(main())
