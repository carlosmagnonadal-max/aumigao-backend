"""Configuração do Pet Tour por tenant (Onda 1 — modalidade especial).

Pet Tour: passeio especial em que o passeador busca o pet de carro e o leva a um
destino escolhido pelo tutor, com duração estendida (>60min). É uma modalidade
premium, ligada por feature flag `pet_tour` e com preço configurável pelo tenant
(ver memória precos-mutaveis-por-tenant). A exigência de veículo do passeador e o
gating de matching por carro são follow-up (lado walker).
"""
from datetime import datetime
from uuid import uuid4

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base
from app.models.types import Money

# Chave de feature flag por tenant que libera o Pet Tour.
PET_TOUR_FEATURE_KEY = "pet_tour"
# Modalidade gravada no Walk.modality.
PET_TOUR_MODALITY = "pet_tour"
STANDARD_MODALITY = "standard"

DEFAULT_PET_TOUR_PRICE = 169.90
DEFAULT_PET_TOUR_MIN_DURATION = 90


class TenantPetTourConfig(Base):
    __tablename__ = "tenant_pet_tour_configs"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=lambda: str(uuid4()))
    tenant_id: Mapped[str] = mapped_column(
        String, ForeignKey("tenants.id"), nullable=False, unique=True, index=True
    )
    # Preço do Pet Tour definido pelo tenant (mutável por tenant).
    base_price: Mapped[float] = mapped_column(Money, nullable=False, default=DEFAULT_PET_TOUR_PRICE)
    # Duração mínima em minutos (>60 por definição da modalidade).
    min_duration_minutes: Mapped[int] = mapped_column(Integer, nullable=False, default=DEFAULT_PET_TOUR_MIN_DURATION)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
