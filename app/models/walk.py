from datetime import datetime

from sqlalchemy import DateTime, Float, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base


class Walk(Base):
    __tablename__ = "walks"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    tutor_id: Mapped[str] = mapped_column(String, ForeignKey("users.id"), index=True)
    tenant_id: Mapped[str | None] = mapped_column(String, ForeignKey("tenants.id"), nullable=True, index=True)
    walker_id: Mapped[str | None] = mapped_column(String, ForeignKey("users.id"), nullable=True)
    pet_id: Mapped[str] = mapped_column(String, ForeignKey("pets.id"))
    scheduled_date: Mapped[str] = mapped_column(String)
    duration_minutes: Mapped[int] = mapped_column(Integer)
    price: Mapped[float] = mapped_column(Float)
    status: Mapped[str] = mapped_column(String, default="Agendado")
    pickup_method: Mapped[str] = mapped_column(String, default="Buscar em casa")
    address_snapshot: Mapped[str] = mapped_column(Text, default="")
    notes: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    operational_status: Mapped[str] = mapped_column(String, default="ride_scheduled", index=True)
    walker_selection_mode: Mapped[str] = mapped_column(String, default="auto")
    assigned_walker_id: Mapped[str | None] = mapped_column(String, ForeignKey("users.id"), nullable=True, index=True)
    current_attempt: Mapped[int] = mapped_column(Integer, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, default=3)
    confirmation_expires_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    matching_started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    matching_finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    no_walker_reason: Mapped[str | None] = mapped_column(Text, nullable=True)

    tutor = relationship("User", back_populates="walks", foreign_keys=[tutor_id])


class WalkMatchingAttempt(Base):
    __tablename__ = "walk_matching_attempts"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    walk_id: Mapped[str] = mapped_column(String, ForeignKey("walks.id"), index=True)
    walker_id: Mapped[str] = mapped_column(String, ForeignKey("users.id"), index=True)
    attempt_number: Mapped[int] = mapped_column(Integer, index=True)
    status: Mapped[str] = mapped_column(String, default="pending", index=True)
    score: Mapped[float] = mapped_column(Float, default=0)
    score_breakdown: Mapped[str] = mapped_column(Text, default="{}")
    sent_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    responded_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime, index=True)
    response_time_seconds: Mapped[int | None] = mapped_column(Integer, nullable=True)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class WalkOperationalLog(Base):
    __tablename__ = "walk_operational_logs"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    walk_id: Mapped[str] = mapped_column(String, ForeignKey("walks.id"), index=True)
    actor_type: Mapped[str] = mapped_column(String, default="system", index=True)
    actor_id: Mapped[str | None] = mapped_column(String, nullable=True, index=True)
    event_type: Mapped[str] = mapped_column(String, index=True)
    metadata_json: Mapped[str] = mapped_column(Text, default="{}")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
