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
    error_message = Column(String, nullable=True)
    operator_note = Column(String, nullable=True)  # Причина передачі оператору

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
    place_id = Column(String, nullable=True)
    visit_datetime = Column(DateTime, nullable=False)
    message = Column(String, nullable=True)

    status = Column(SAEnum(CallStatus), nullable=False)
    result = Column(SAEnum(CallResult), nullable=True)
    attempts = Column(Integer, nullable=False, default=0)
    call_duration = Column(Float, nullable=True)

    created_at = Column(DateTime, nullable=False)
    completed_at = Column(DateTime, nullable=True)
    error_message = Column(String, nullable=True)
    operator_note = Column(String, nullable=True)

    archived_at = Column(DateTime, nullable=False, default=lambda: datetime.now(timezone.utc).replace(tzinfo=None))

    def __repr__(self) -> str:
        return (
            f"<CallHistory visit={self.visit_id} patient={self.patient_name} "
            f"result={self.result}>"
        )
