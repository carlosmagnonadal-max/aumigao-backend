"""pet_self_walk.py — Passeio SELF-SERVE do tutor (Perfil Vivo 2.0, Fase D).

O tutor registra um passeio que ELE MESMO fez com o próprio cão. É engajamento/
dado — NÃO é transação: sem comissão, sem passeador, sem ledger. O cliente
rastreia localmente (mapa Leaflet/OSM no app) e envia UMA vez no fim; o servidor
persiste APENAS O RESUMO (sem rota GPS — o mapa vive no cliente).

Colunas explícitas vs JSON — DECISÃO: `needs` e `behavior` viram COLUNAS bool
explícitas (não details_json). Motivo: a agregação futura do wellness (o
componente Rotina já soma self-walks; a evolução natural é filtrar/contar por
comportamento — ex. "% de passeios do tutor com reatividade") exige campos
consultáveis/indexáveis. Espelha o padrão da casa: walk_observations usa bool
explícito (incident), pet_health_records usa colunas explícitas. JSON só serviria
se o shape fosse aberto — aqui o contrato é fixo.
"""
from datetime import datetime
from uuid import uuid4

from sqlalchemy import Boolean, DateTime, ForeignKey, Index, Integer, Numeric, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base

# Enums do contrato (validados também no schema da rota).
SELF_WALK_TYPES: frozenset[str] = frozenset(
    {"rua", "parque", "praia", "trilha", "interno", "outro"}
)
SELF_WALK_INTENSITIES: frozenset[str] = frozenset({"leve", "moderado", "intenso"})

# Limites de validação (fonte única — reusados pela rota).
DURATION_MIN_SECONDS = 60
DURATION_MAX_SECONDS = 21600  # 6h
DISTANCE_MAX_KM = 30.0
STARTED_MAX_AGE_HOURS = 48

# Rate-limit de bom senso: evita spam de score (o componente Rotina do wellness
# conta self-walks). Máx N self-walks por pet por dia (janela = mesmo dia UTC).
MAX_SELF_WALKS_PER_DAY = 6


class PetSelfWalk(Base):
    """Resumo de um passeio self-serve do tutor. Sem estado vivo, sem GPS."""

    __tablename__ = "pet_self_walks"
    __table_args__ = (
        Index("ix_pet_self_walks_pet_started", "pet_id", "started_at"),
    )

    id: Mapped[str] = mapped_column(String, primary_key=True, default=lambda: str(uuid4()))
    pet_id: Mapped[str] = mapped_column(String, ForeignKey("pets.id"), nullable=False, index=True)
    tutor_id: Mapped[str] = mapped_column(String, ForeignKey("users.id"), nullable=False, index=True)
    tenant_id: Mapped[str | None] = mapped_column(
        String, ForeignKey("tenants.id"), nullable=True, index=True
    )

    started_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, index=True)
    duration_seconds: Mapped[int] = mapped_column(Integer, nullable=False)
    distance_km: Mapped[float | None] = mapped_column(Numeric(6, 2), nullable=True)
    walk_type: Mapped[str] = mapped_column(String, nullable=False)
    intensity: Mapped[str] = mapped_column(String, nullable=False)
    had_gps: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    # needs (necessidades) — colunas bool explícitas.
    need_pee: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    need_poop: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    need_water: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    # behavior (comportamento) — colunas bool explícitas.
    interacted_dogs: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    interacted_people: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    pulled_leash: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    showed_fear: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    showed_reactivity: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    notes: Mapped[str] = mapped_column(Text, nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=datetime.utcnow)
