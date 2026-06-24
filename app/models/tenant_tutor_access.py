from __future__ import annotations

from datetime import datetime
from uuid import uuid4

import sqlalchemy as sa
from sqlalchemy import DateTime, ForeignKey, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


def _uuid() -> str:
    return str(uuid4())


class TenantTutorAccess(Base):
    """Vínculo explícito tutor↔tenant (Modelo B white-label). Espelho enxuto de TenantWalkerAccess."""

    __tablename__ = "tenant_tutor_access"
    __table_args__ = (
        UniqueConstraint("tenant_id", "tutor_user_id", name="uq_tenant_tutor_access_tenant_tutor"),
    )

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    tenant_id: Mapped[str] = mapped_column(String, ForeignKey("tenants.id"), nullable=False, index=True)
    tutor_user_id: Mapped[str] = mapped_column(String, ForeignKey("users.id"), nullable=False, index=True)
    # Máquina de estados: active | pending | declined | revoked
    status: Mapped[str] = mapped_column(String, default="active", index=True)
    # Quem iniciou a relação: "tutor" (tutor entrou pelo QR/código) ou "tenant" (admin convidou).
    initiated_by: Mapped[str] = mapped_column(
        String(16), nullable=False, default="tutor", server_default="tutor"
    )
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
