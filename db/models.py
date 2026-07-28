import enum
import uuid
from datetime import datetime, timezone

from sqlalchemy import Column, String, Integer, Float, DateTime, Enum as SAEnum
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    pass


class CallStatus(enum.Enum):
    pending = "pending"           # Очікує дзвінка
    in_progress = "in_progress"   # Дзвінок в процесі
    completed = "completed"       # Завершено успішно
    failed = "failed"             # Не вдалося (вичерпано спроби)
    skipped = "skipped"           # Пропущено (ENABLED=0 від ai_checkstate)


class CallResult(enum.Enum):
    confirmed = "confirmed"       # Пацієнт підтвердив
    cancelled = "cancelled"       # Скасовано
    rescheduled = "rescheduled"   # Перенесено на інший час
    no_result = "no_result"       # Не зрозумів / не міг говорити
    no_answer = "no_answer"       # Не відповів
    transferred = "transferred"   # Передано менеджеру
    wrong_number = "wrong_number" # Неправильний номер / не записувався


class CallTask(Base):
    """Задача на дзвінок пацієнту — Блок 1: нагадування."""

    __tablename__ = "call_tasks"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)

    # Дані з API клініки
    visit_id = Column(String, nullable=False, unique=True, index=True)
    patient_name = Column(String, nullable=False)
    patient_phone = Column(String, nullable=False)
    doctor_name = Column(String, nullable=False)
    doctor_specialization = Column(String, nullable=True)
    place_id = Column(String, nullable=False)
    visit_datetime = Column(DateTime, nullable=False)

    # Статус та результат
    status = Column(SAEnum(CallStatus), nullable=False, default=CallStatus.pending)
    result = Column(SAEnum(CallResult), nullable=True)
    attempts = Column(Integer, nullable=False, default=0)
    call_duration = Column(Float, nullable=True)   # секунди

    # Часові мітки
    created_at = Column(DateTime, nullable=False, default=lambda: datetime.now(timezone.utc).replace(tzinfo=None))
    updated_at = Column(DateTime, nullable=False, default=lambda: datetime.now(timezone.utc).replace(tzinfo=None), onupdate=lambda: datetime.now(timezone.utc).replace(tzinfo=None))
    completed_at = Column(DateTime, nullable=True)

    # Додаткова інформація
    message = Column(String, nullable=True)        # MESSAGE з API клініки (час/нотатки)
    appointment = Column(String, nullable=True)    # APPOINTMENT з API — назва процедури
    preparation = Column(String, nullable=True)    # підготовка до процедури
    call_session = Column(String, nullable=True)   # "morning" або "evening"
    error_message = Column(String, nullable=True)
    operator_note = Column(String, nullable=True)  # Причина передачі оператору
    commentary = Column(String(512), nullable=True)  # AI-коментар до дзвінка

    def __repr__(self) -> str:
        return (
            f"<CallTask visit={self.visit_id} patient={self.patient_name} "
            f"status={self.status.value} attempts={self.attempts}>"
        )


class CallHistory(Base):
    """Архів виконаних задач — переноситься з call_tasks після кожного робочого дня."""

    __tablename__ = "call_history"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)

    visit_id = Column(String, nullable=False, index=True)
    patient_name = Column(String, nullable=False)
    patient_phone = Column(String, nullable=False)
    doctor_name = Column(String, nullable=False)
    doctor_specialization = Column(String, nullable=True)
    place_id = Column(String, nullable=True)
    visit_datetime = Column(DateTime, nullable=False)
    message = Column(String, nullable=True)
    appointment = Column(String, nullable=True)    # APPOINTMENT з API — назва процедури
    preparation = Column(String, nullable=True)    # підготовка до процедури
    call_session = Column(String, nullable=True)   # "morning" або "evening"

    status = Column(SAEnum(CallStatus), nullable=False)
    result = Column(SAEnum(CallResult), nullable=True)
    attempts = Column(Integer, nullable=False, default=0)
    call_duration = Column(Float, nullable=True)

    created_at = Column(DateTime, nullable=False)
    completed_at = Column(DateTime, nullable=True)
    error_message = Column(String, nullable=True)
    operator_note = Column(String, nullable=True)
    commentary = Column(String(512), nullable=True)  # AI-коментар до дзвінка

    archived_at = Column(DateTime, nullable=False, default=lambda: datetime.now(timezone.utc).replace(tzinfo=None))

    def __repr__(self) -> str:
        return (
            f"<CallHistory visit={self.visit_id} patient={self.patient_name} "
            f"result={self.result}>"
        )


# ---------------------------------------------------------------------------
# Блок 2 — Модуль 2.1: повторні візити
# ---------------------------------------------------------------------------

class RepeatCallStatus(enum.Enum):
    pending     = "pending"
    in_progress = "in_progress"
    completed   = "completed"
    failed      = "failed"
    skipped     = "skipped"


class RepeatCallResult(enum.Enum):
    booked    = "booked"     # Записано на повторний прийом
    declined  = "declined"   # Відмовився
    self_book = "self_book"  # Сам запишеться / передзвонить
    no_answer = "no_answer"  # Не відповів
    no_result = "no_result"  # Незрозуміло / помилка


class RepeatCallTask(Base):
    """Задача на вихідний дзвінок — Блок 2, Модуль 2.1: нагадування про повторний візит."""

    __tablename__ = "repeat_call_tasks"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)

    # Дані попереднього візиту (контекст дзвінка)
    source_visit_id   = Column(String, nullable=False, unique=True, index=True)
    patient_name      = Column(String, nullable=False)
    patient_phone     = Column(String, nullable=False)
    patient_id        = Column(String, nullable=True)
    previous_visit_date = Column(DateTime, nullable=True)
    previous_doctor   = Column(String, nullable=True)
    doctor_id         = Column(String, nullable=True)       # для пошуку слотів
    doctor_name       = Column(String, nullable=False)
    doctor_specialization = Column(String, nullable=True)
    doctor_recommendation = Column(String, nullable=True)   # "через 3 тижні"
    repeat_visit_date = Column(DateTime, nullable=True)     # цільова дата

    # Статус та результат
    status           = Column(SAEnum(RepeatCallStatus), nullable=False, default=RepeatCallStatus.pending)
    result           = Column(SAEnum(RepeatCallResult), nullable=True)
    decline_category = Column(String, nullable=True)         # одна з 26 категорій
    new_visit_id     = Column(String, nullable=True)         # ID нового запису якщо booked
    attempts         = Column(Integer, nullable=False, default=0)
    call_duration    = Column(Float, nullable=True)

    # Часові мітки
    created_at   = Column(DateTime, nullable=False, default=lambda: datetime.now(timezone.utc).replace(tzinfo=None))
    completed_at = Column(DateTime, nullable=True)
    error_message = Column(String, nullable=True)
    commentary    = Column(String(512), nullable=True)

    def __repr__(self) -> str:
        return (
            f"<RepeatCallTask source={self.source_visit_id} patient={self.patient_name} "
            f"status={self.status.value}>"
        )


class RepeatCallHistory(Base):
    """Архів виконаних задач Модуля 2.1."""

    __tablename__ = "repeat_call_history"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)

    source_visit_id   = Column(String, nullable=False, index=True)
    patient_name      = Column(String, nullable=False)
    patient_phone     = Column(String, nullable=False)
    patient_id        = Column(String, nullable=True)
    previous_visit_date = Column(DateTime, nullable=True)
    previous_doctor   = Column(String, nullable=True)
    doctor_id         = Column(String, nullable=True)
    doctor_name       = Column(String, nullable=False)
    doctor_specialization = Column(String, nullable=True)
    doctor_recommendation = Column(String, nullable=True)
    repeat_visit_date = Column(DateTime, nullable=True)

    status           = Column(SAEnum(RepeatCallStatus), nullable=False)
    result           = Column(SAEnum(RepeatCallResult), nullable=True)
    decline_category = Column(String, nullable=True)
    new_visit_id     = Column(String, nullable=True)
    attempts         = Column(Integer, nullable=False, default=0)
    call_duration    = Column(Float, nullable=True)

    created_at   = Column(DateTime, nullable=False)
    completed_at = Column(DateTime, nullable=True)
    error_message = Column(String, nullable=True)
    commentary    = Column(String(512), nullable=True)

    archived_at = Column(DateTime, nullable=False, default=lambda: datetime.now(timezone.utc).replace(tzinfo=None))

    def __repr__(self) -> str:
        return (
            f"<RepeatCallHistory source={self.source_visit_id} patient={self.patient_name} "
            f"result={self.result}>"
        )
