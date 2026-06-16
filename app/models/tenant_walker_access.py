from datetime import datetime
from uuid import uuid4

from sqlalchemy import DateTime, ForeignKey, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


def _uuid() -> str:
    return str(uuid4())


class TenantWalkerAccess(Base):
    __tablename__ = "tenant_walker_access"
    __table_args__ = (UniqueConstraint("tenant_id", "walker_user_id", name="uq_tenant_walker_access_tenant_walker"),)

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    tenant_id: Mapped[str] = mapped_column(String, ForeignKey("tenants.id"), nullable=False, index=True)
    walker_user_id: Mapped[str] = mapped_column(String, ForeignKey("users.id"), nullable=False, index=True)
    access_type: Mapped[str] = mapped_column(String, default="shared_network", index=True)
    # Máquina de estados do convite à Rede Aumigão:
    # pending -> convidado, ainda não respondeu (não entra no pool de matching)
    # active  -> aceitou / está na rede do tenant (entra no pool)
    # declined-> recusou o convite
    # revoked -> tenant/rede removeu o acesso depois de ativo
    # (paused é mantido por compatibilidade com dados legados)
    status: Mapped[str] = mapped_column(String, default="active", index=True)
    invited_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    responded_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
