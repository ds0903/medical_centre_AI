import enum
import uuid
from datetime import datetime

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
    rescheduled = "rescheduled"   # Перенесено
    cancelled = "cancelled"       # Скасовано
    no_answer = "no_answer"       # Не відповів
    transferred = "transferred"   # Передано оператору
    error = "error"               # Технічна помилка


class CallTask(Base):
    """Задача на дзвінок пацієнту — Блок 1: нагадування."""

    __tablename__ = "call_tasks"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)

    # Дані з API клініки
    visit_id = Column(String, nullable=False, unique=True, index=True)
    patient_name = Column(String, nullable=False)
    patient_phone = Column(String, nullable=False)
    doctor_name = Column(String, nullable=False)
    doctor_id = Column(String, nullable=False)
    visit_datetime = Column(DateTime, nullable=False)

    # Статус та результат
    status = Column(SAEnum(CallStatus), nullable=False, default=CallStatus.pending)
    result = Column(SAEnum(CallResult), nullable=True)
    attempts = Column(Integer, nullable=False, default=0)
    call_duration = Column(Float, nullable=True)   # секунди

    # Часові мітки
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    updated_at = Column(DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow)
    completed_at = Column(DateTime, nullable=True)

    # Додаткова інформація
    error_message = Column(String, nullable=True)
    operator_note = Column(String, nullable=True)  # Причина передачі оператору

    def __repr__(self) -> str:
        return (
            f"<CallTask visit={self.visit_id} patient={self.patient_name} "
            f"status={self.status.value} attempts={self.attempts}>"
        )
