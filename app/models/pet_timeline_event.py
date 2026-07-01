from datetime import datetime
from uuid import uuid4

from sqlalchemy import DateTime, ForeignKey, Index, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base

EVENT_TYPES = {"vaccine", "weight", "health_note", "medication", "walk_observation", "birthday", "custom"}
EVENT_SOURCES = {"tutor", "walker", "admin", "system"}


class PetTimelineEvent(Base):
    """Timeline unificada de eventos do pet (tutor + passeador + sistema)."""

    __tablename__ = "pet_timeline_events"
    __table_args__ = (
        Index("ix_pet_timeline_events_pet_occurred", "pet_id", "occurred_at"),
    )

    id: Mapped[str] = mapped_column(String, primary_key=True, default=lambda: str(uuid4()))
    pet_id: Mapped[str] = mapped_column(String, ForeignKey("pets.id"), nullable=False, index=True)
    tenant_id: Mapped[str | None] = mapped_column(String, ForeignKey("tenants.id"), nullable=True, index=True)
    event_type: Mapped[str] = mapped_column(String, index=True)
    title: Mapped[str] = mapped_column(String)
    notes: Mapped[str] = mapped_column(Text, default="")
    payload_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    occurred_at: Mapped[datetime] = mapped_column(DateTime, index=True)
    source: Mapped[str] = mapped_column(String, default="tutor")
    created_by_user_id: Mapped[str | None] = mapped_column(String, ForeignKey("users.id"), nullable=True)
    related_entity_type: Mapped[str | None] = mapped_column(String, nullable=True)
    related_entity_id: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
