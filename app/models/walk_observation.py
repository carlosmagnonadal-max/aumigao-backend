from datetime import datetime
from uuid import uuid4

from sqlalchemy import Boolean, DateTime, ForeignKey, Index, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base

MOOD_VALUES = {"calm", "happy", "anxious", "agitated"}
ENERGY_VALUES = {"low", "normal", "high"}
SOCIALIZATION_VALUES = {"good", "neutral", "reactive"}


class WalkObservation(Base):
    """Observação estruturada do passeador na finalização do passeio. 1:1 com o passeio."""

    __tablename__ = "walk_observations"
    __table_args__ = (
        UniqueConstraint("walk_id", name="uq_walk_observations_walk_id"),
        Index("ix_walk_observations_pet_id", "pet_id"),
        Index("ix_walk_observations_tenant_id", "tenant_id"),
    )

    id: Mapped[str] = mapped_column(String, primary_key=True, default=lambda: str(uuid4()))
    walk_id: Mapped[str] = mapped_column(String, ForeignKey("walks.id"), nullable=False, index=True)
    pet_id: Mapped[str] = mapped_column(String, ForeignKey("pets.id"), nullable=False)
    tenant_id: Mapped[str | None] = mapped_column(String, ForeignKey("tenants.id"), nullable=True)
    walker_user_id: Mapped[str] = mapped_column(String, ForeignKey("users.id"), nullable=False)

    mood: Mapped[str | None] = mapped_column(String, nullable=True)
    energy: Mapped[str | None] = mapped_column(String, nullable=True)
    socialization: Mapped[str | None] = mapped_column(String, nullable=True)

    peed: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    pooped: Mapped[bool | None] = mapped_column(Boolean, nullable=True)

    incident: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    incident_notes: Mapped[str] = mapped_column(Text, nullable=False, default="")

    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
