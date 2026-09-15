"""local_rules — regras locais de condução de cães como DADO estruturado (S3, mig 0111).

Tabela GLOBAL (sem tenant_id): referência jurídica pública, igual para todos os
tenants. RLS (Postgres): leitura liberada em qualquer escopo; escrita só no
escopo global '*'. Ver alembic/versions/0111_local_rules_pet_reactive.py.

municipio NULL = regra estadual (vale para todos os municípios da UF).
params_json / exigencia_json = JSON simples em TEXT (padrão diet_meal_times).
"""
from datetime import date, datetime
from uuid import uuid4

from sqlalchemy import Date, DateTime, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base

NIVEL_MUNICIPAL = "municipal"
NIVEL_ESTADUAL = "estadual"

CRITERIO_BREED_LIST = "breed_list"
CRITERIO_WEIGHT_MIN_KG = "weight_min_kg"
CRITERIO_REACTIVE = "reactive"
CRITERIO_LOCATION = "location"
CRITERIO_OPERATIONAL = "operational"  # DV4: regras sem critério de pet (limite_caes)
PET_CRITERIA = frozenset({CRITERIO_BREED_LIST, CRITERIO_WEIGHT_MIN_KG, CRITERIO_REACTIVE})

TEMA_FOCINHEIRA = "focinheira"
TEMA_GUIA = "guia"
TEMA_PRAIA = "praia"
TEMA_DEJETOS = "dejetos"
TEMA_LIMITE_CAES = "limite_caes"

STATUS_VIGENTE = "vigente"
STATUS_TRAMITACAO = "tramitacao"
STATUS_CONFLITO = "conflito"


class LocalRule(Base):
    __tablename__ = "local_rules"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=lambda: str(uuid4()))
    uf: Mapped[str] = mapped_column(String(2), nullable=False, index=True)
    municipio: Mapped[str | None] = mapped_column(String, nullable=True)
    nivel: Mapped[str] = mapped_column(String, nullable=False, default=NIVEL_MUNICIPAL)
    tema: Mapped[str] = mapped_column(String, nullable=False, index=True)
    criterio: Mapped[str] = mapped_column(String, nullable=False)
    params_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    exigencia_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    norma: Mapped[str] = mapped_column(String, nullable=False)
    fonte_url: Mapped[str | None] = mapped_column(String, nullable=True)
    verificado_em: Mapped[date | None] = mapped_column(Date, nullable=True)
    confianca: Mapped[str] = mapped_column(String, nullable=False, default="media")
    status: Mapped[str] = mapped_column(String, nullable=False, default=STATUS_VIGENTE, index=True)
    confirmado_por: Mapped[str] = mapped_column(String, nullable=False, default="plataforma")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
