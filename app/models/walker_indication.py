"""Modelos de indicação de passeador pelo tutor.

walker_indications: o tutor indica um passeador conhecido.
walker_leads:       lead público gerado pela página /seja-passeador.
"""
from datetime import datetime
from uuid import uuid4

from sqlalchemy import DateTime, ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class WalkerIndication(Base):
    """Indicação de passeador feita por um tutor autenticado (tenant-scoped)."""

    __tablename__ = "walker_indications"

    id: Mapped[str] = mapped_column(
        String, primary_key=True, default=lambda: str(uuid4())
    )
    tenant_id: Mapped[str] = mapped_column(
        String, ForeignKey("tenants.id"), nullable=False, index=True
    )
    tutor_user_id: Mapped[str] = mapped_column(
        String, ForeignKey("users.id"), nullable=False, index=True
    )
    walker_name: Mapped[str] = mapped_column(String(200), nullable=False)
    walker_phone: Mapped[str | None] = mapped_column(String(30), nullable=True)
    note: Mapped[str | None] = mapped_column(String(500), nullable=True)
    # Valores possíveis: enviada | lead_criado
    status: Mapped[str] = mapped_column(String(30), nullable=False, default="enviada", index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow
    )


class WalkerLead(Base):
    """Lead de passeador gerado via página pública /seja-passeador."""

    __tablename__ = "walker_leads"

    id: Mapped[str] = mapped_column(
        String, primary_key=True, default=lambda: str(uuid4())
    )
    tenant_id: Mapped[str] = mapped_column(
        String, ForeignKey("tenants.id"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    phone: Mapped[str] = mapped_column(String(30), nullable=False)
    city: Mapped[str | None] = mapped_column(String(120), nullable=True)
    indication_id: Mapped[str | None] = mapped_column(
        String, ForeignKey("walker_indications.id"), nullable=True, index=True
    )
    # Valores possíveis: novo | contatado | descartado
    status: Mapped[str] = mapped_column(String(30), nullable=False, default="novo", index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
