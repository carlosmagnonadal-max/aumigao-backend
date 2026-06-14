"""Preço de passeio individual configurável por tenant (white label).

Espelha o padrão de `TenantSharedWalkConfig`: 1 linha por tenant com os preços
das durações 30/45/60 min, mutáveis pelo admin do tenant. Ver memória
pacote-white-label.
"""
from datetime import datetime
from uuid import uuid4

from sqlalchemy import Boolean, DateTime, Float, ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


def _uuid() -> str:
    return str(uuid4())


class TenantIndividualWalkPricing(Base):
    __tablename__ = "tenant_individual_walk_pricing"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    tenant_id: Mapped[str] = mapped_column(String, ForeignKey("tenants.id"), nullable=False, unique=True, index=True)
    # Preços por duração (mutáveis por tenant).
    price_30: Mapped[float] = mapped_column(Float, nullable=False, default=36.90)
    price_45: Mapped[float] = mapped_column(Float, nullable=False, default=49.90)
    price_60: Mapped[float] = mapped_column(Float, nullable=False, default=62.90)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
