from datetime import datetime
from uuid import uuid4

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class PetProfileConfig(Base):
    """Config por-tenant do Perfil Vivo do Pet. 1 linha por tenant, default OFF."""

    __tablename__ = "pet_profile_configs"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=lambda: str(uuid4()))
    tenant_id: Mapped[str] = mapped_column(
        String, ForeignKey("tenants.id"), nullable=False, unique=True, index=True
    )
    profile_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    observations_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    reminders_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    vaccine_lead_days: Mapped[int] = mapped_column(Integer, nullable=False, default=15)
    inactivity_days: Mapped[int] = mapped_column(Integer, nullable=False, default=10)
    share_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
