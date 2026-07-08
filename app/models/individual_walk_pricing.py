"""Preço de passeio individual configurável por tenant (white label).

Espelha o padrão de `TenantSharedWalkConfig`: 1 linha por tenant com os preços
das durações 30/45/60 min, mutáveis pelo admin do tenant. Ver memória
pacote-white-label.
"""
from datetime import datetime
from uuid import uuid4

from sqlalchemy import Boolean, DateTime, ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base
from app.models.types import Money


def _uuid() -> str:
    return str(uuid4())


class TenantIndividualWalkPricing(Base):
    __tablename__ = "tenant_individual_walk_pricing"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    tenant_id: Mapped[str] = mapped_column(String, ForeignKey("tenants.id"), nullable=False, unique=True, index=True)
    # Preços por duração (mutáveis por tenant). Defaults = âncora 07/07/2026.
    price_30: Mapped[float] = mapped_column(Money, nullable=False, default=40.90)
    price_45: Mapped[float] = mapped_column(Money, nullable=False, default=54.90)
    price_60: Mapped[float] = mapped_column(Money, nullable=False, default=69.90)
    # Desconto flat quando o TUTOR leva o pet até o ponto de encontro (o padrão
    # do produto é o passeador buscar em casa, já embutido na âncora). Só vale
    # na modalidade standard — Pet Tour é busca de carro por definição.
    meeting_point_discount: Mapped[float] = mapped_column(
        Money, nullable=False, default=0.0, server_default="0"
    )
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
