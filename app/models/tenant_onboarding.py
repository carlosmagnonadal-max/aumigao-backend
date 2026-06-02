from datetime import datetime
from uuid import uuid4

from sqlalchemy import Boolean, DateTime, ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base


def _uuid() -> str:
    return str(uuid4())


class TenantOnboarding(Base):
    __tablename__ = "tenant_onboarding"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    tenant_id: Mapped[str] = mapped_column(String, ForeignKey("tenants.id"), nullable=False, unique=True, index=True)
    onboarding_status: Mapped[str] = mapped_column(String, default="created")
    contract_signed: Mapped[bool] = mapped_column(Boolean, default=False)
    setup_paid: Mapped[bool] = mapped_column(Boolean, default=False)
    branding_completed: Mapped[bool] = mapped_column(Boolean, default=False)
    units_completed: Mapped[bool] = mapped_column(Boolean, default=False)
    operators_completed: Mapped[bool] = mapped_column(Boolean, default=False)
    go_live_approved: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    tenant = relationship("Tenant", back_populates="onboarding")
