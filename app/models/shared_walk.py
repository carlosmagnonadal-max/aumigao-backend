"""Passeios compartilhados (Onda 1).

Sessão compartilhada com múltiplos participantes (pet+tutor), preço por pet,
1 walker pro grupo. Gated pela feature flag por tenant `shared_walks`.

Formas de entrar (origin): "same_tutor" (cães do mesmo dono), "invite" (convite a
outro tutor) e "pool" (descoberta automática — toggle do admin, default off).
Ver memória passeios-compartilhados.
"""
from datetime import datetime
from uuid import uuid4

from sqlalchemy import Boolean, DateTime, Float, ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base
from app.models.types import Money

SHARED_WALKS_FEATURE_KEY = "shared_walks"

# Estados da sessão.
SHARED_FORMING = "forming"      # montando: aguardando aceite/pagamento dos participantes
SHARED_CONFIRMED = "confirmed"  # todos pagaram -> libera matching
SHARED_MATCHED = "matched"
SHARED_CANCELLED = "cancelled"

# Estados do participante.
PARTICIPANT_INVITED = "invited"
PARTICIPANT_ACCEPTED = "accepted"
PARTICIPANT_PAID = "paid"
PARTICIPANT_DECLINED = "declined"
PARTICIPANT_CANCELLED = "cancelled"

ORIGIN_SAME_TUTOR = "same_tutor"
ORIGIN_INVITE = "invite"
ORIGIN_POOL = "pool"


def _uuid() -> str:
    return str(uuid4())


class TenantSharedWalkConfig(Base):
    __tablename__ = "tenant_shared_walk_configs"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    tenant_id: Mapped[str] = mapped_column(String, ForeignKey("tenants.id"), nullable=False, unique=True, index=True)
    # Preço cobrado por pet (mutável por tenant) — mantido para compatibilidade.
    price_per_pet: Mapped[float] = mapped_column(Money, nullable=False, default=29.90)
    # Preço por duração (white label, Etapa 2). Fallback = price_per_pet.
    price_30: Mapped[float] = mapped_column(Money, nullable=False, default=29.90)
    price_45: Mapped[float] = mapped_column(Money, nullable=False, default=39.50)
    price_60: Mapped[float] = mapped_column(Money, nullable=False, default=49.90)
    # Limites configuráveis pelo admin do tenant.
    max_pets_same_tutor: Mapped[int] = mapped_column(Integer, nullable=False, default=3)
    max_tutors: Mapped[int] = mapped_column(Integer, nullable=False, default=2)
    # Pool "aberto a compartilhar": toggle do admin (default off) + parâmetros.
    pool_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    pool_radius_km: Mapped[float] = mapped_column(Float, nullable=False, default=3.0)
    pool_time_window_min: Mapped[int] = mapped_column(Integer, nullable=False, default=30)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class SharedWalk(Base):
    __tablename__ = "shared_walks"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    tenant_id: Mapped[str] = mapped_column(String, ForeignKey("tenants.id"), nullable=False, index=True)
    created_by_tutor_id: Mapped[str] = mapped_column(String, index=True)
    status: Mapped[str] = mapped_column(String, nullable=False, default=SHARED_FORMING, index=True)
    origin: Mapped[str] = mapped_column(String, nullable=False, default=ORIGIN_INVITE)
    scheduled_date: Mapped[str] = mapped_column(String, default="")
    duration_minutes: Mapped[int] = mapped_column(Integer, default=45)
    # Snapshots no momento da criação (config pode mudar depois).
    price_per_pet: Mapped[float] = mapped_column(Money, default=0.0)
    max_tutors: Mapped[int] = mapped_column(Integer, default=2)
    # Disponível no pool (só relevante quando o tenant tem pool_enabled).
    open_to_pool: Mapped[bool] = mapped_column(Boolean, default=False)
    walker_id: Mapped[str | None] = mapped_column(String, nullable=True)
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    participants: Mapped[list["SharedWalkParticipant"]] = relationship(
        back_populates="shared_walk", cascade="all, delete-orphan"
    )


class SharedWalkParticipant(Base):
    __tablename__ = "shared_walk_participants"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    shared_walk_id: Mapped[str] = mapped_column(String, ForeignKey("shared_walks.id"), nullable=False, index=True)
    tutor_id: Mapped[str] = mapped_column(String, nullable=False, index=True)
    pet_id: Mapped[str] = mapped_column(String, nullable=False)
    # "host" = quem criou; "guest" = convidado/entrou pelo pool.
    role: Mapped[str] = mapped_column(String, default="guest")
    status: Mapped[str] = mapped_column(String, nullable=False, default=PARTICIPANT_INVITED, index=True)
    price: Mapped[float] = mapped_column(Money, default=0.0)
    payment_id: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    # Desnormalizado de shared_walks.tenant_id para permitir RLS direto.
    # Nullable para compatibilidade retroativa; backfill em 0046_.
    tenant_id: Mapped[str | None] = mapped_column(
        String, ForeignKey("tenants.id"), nullable=True, index=True
    )

    shared_walk: Mapped[SharedWalk] = relationship(back_populates="participants")
