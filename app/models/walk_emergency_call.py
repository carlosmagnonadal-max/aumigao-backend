"""Acionamentos do Botão de Emergência (S1 — migration 0109).

Uma linha por acionamento ACEITO (o excedente do rate-limit não grava).
tenant_id = walk.tenant_id, nullable como walks.tenant_id. RLS: USING com NULL
allowance + WITH CHECK estrito (padrão do projeto).
NUNCA guarda telefone: o número do tutor só existe na resposta HTTP do acionamento.
"""
from datetime import datetime
from uuid import uuid4

from sqlalchemy import DateTime, ForeignKey, Index, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base

EMERGENCY_CALL_MODES = ("direct", "masked", "unavailable")
EMERGENCY_CALL_STATUSES = ("initiated", "connected", "failed", "fallback_direct")


class WalkEmergencyCall(Base):
    __tablename__ = "walk_emergency_calls"
    __table_args__ = (
        Index("ix_walk_emergency_calls_walk_created", "walk_id", "created_at"),
    )

    id: Mapped[str] = mapped_column(String, primary_key=True, default=lambda: str(uuid4()))
    tenant_id: Mapped[str | None] = mapped_column(String, ForeignKey("tenants.id"), nullable=True, index=True)
    walk_id: Mapped[str] = mapped_column(String, ForeignKey("walks.id"), nullable=False)
    walker_user_id: Mapped[str] = mapped_column(String, ForeignKey("users.id"), nullable=False)
    tutor_user_id: Mapped[str | None] = mapped_column(String, ForeignKey("users.id"), nullable=True)
    reason: Mapped[str | None] = mapped_column(String, nullable=True)
    mode: Mapped[str] = mapped_column(String, nullable=False)
    provider: Mapped[str] = mapped_column(String, nullable=False, default="none")
    provider_call_id: Mapped[str | None] = mapped_column(String, nullable=True)
    status: Mapped[str] = mapped_column(String, nullable=False)
    fallback_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=datetime.utcnow)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
