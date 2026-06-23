from datetime import datetime
from decimal import Decimal
from uuid import uuid4

import sqlalchemy as sa
from sqlalchemy import Boolean, DateTime, ForeignKey, Numeric, String, UniqueConstraint
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

    # ── Fase 1 Passo 1 (migration 0048) ──────────────────────────────────────
    # Comissão negociada individualmente para este tenant (NULL = usa o default do plano).
    commission_percent: Mapped[Decimal | None] = mapped_column(Numeric(5, 2), nullable=True)
    # Indica que o passeador cumpriu todos os requisitos deste tenant para ficar ativo.
    # server_default sa.text("true") funciona em Postgres (true) e SQLite (1=truthy).
    requirements_met: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default=sa.text("true")
    )
    # F3.2: quando o passeador sinaliza "já cumpri" (alimenta a fila de revisão do admin). NULL = não submetido.
    requirements_submitted_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # Quem iniciou a relação: "tenant" (tenant convidou) ou "network" (rede indicou).
    initiated_by: Mapped[str] = mapped_column(
        String(16), nullable=False, default="tenant", server_default="tenant"
    )
