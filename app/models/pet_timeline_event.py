from datetime import datetime
from uuid import uuid4

from sqlalchemy import DateTime, ForeignKey, Index, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base

EVENT_TYPES = {"vaccine", "weight", "health_note", "medication", "walk_observation", "birthday", "custom", "diary", "self_walk", "tenant_note"}
EVENT_SOURCES = {"tutor", "walker", "admin", "system"}
# Humores válidos do diário do tutor (Fase B) — armazenados no payload_json.
DIARY_MOODS = {"bom", "neutro", "ruim"}

# ── Comportamento multi-fonte (Fase E) ─────────────────────────────────────
# Contextos (fonte B2B) e categorias da observação estruturada do TENANT
# (event_type="tenant_note", payload montado no servidor — padrão diary).
TENANT_NOTE_CONTEXTS = {"creche", "hospedagem", "banho_tosa", "adestramento", "atendimento", "outro"}
TIMELINE_CATEGORIES = {"evolucao", "aprendizado", "cuidado", "convivencia", "incidente", "restricao"}
# Categorias que disparam notificação ao TUTOR dono quando o tenant registra.
TENANT_NOTE_ALERT_CATEGORIES = {"incidente", "restricao"}

# Mapa tipo→categoria default (Fase E): usado no filtro ?category= da timeline.
# tenant_note NÃO está aqui — sua categoria vem do payload_json.category (por-evento).
# diary é DELIBERADAMENTE omitido: sem categoria fixa → aparece em TODAS as categorias.
EVENT_TYPE_CATEGORY = {
    "walk_observation": "convivencia",  # socialização/interação registrada no passeio
    "self_walk": "convivencia",         # passeio self-serve do tutor
    "health_note": "cuidado",           # ficha de saúde / clínico
    "vaccine": "cuidado",               # carteira de vacinas
    "medication": "cuidado",            # tratamentos/medicação
    "weight": "cuidado",                # acompanhamento de peso
    "birthday": "convivencia",          # marco social
    # custom: sem categoria fixa (fica fora dos filtros específicos)
}


class PetTimelineEvent(Base):
    """Timeline unificada de eventos do pet (tutor + passeador + sistema)."""

    __tablename__ = "pet_timeline_events"
    __table_args__ = (
        Index("ix_pet_timeline_events_pet_occurred", "pet_id", "occurred_at"),
    )

    id: Mapped[str] = mapped_column(String, primary_key=True, default=lambda: str(uuid4()))
    pet_id: Mapped[str] = mapped_column(String, ForeignKey("pets.id"), nullable=False, index=True)
    tenant_id: Mapped[str | None] = mapped_column(String, ForeignKey("tenants.id"), nullable=True, index=True)
    event_type: Mapped[str] = mapped_column(String, index=True)
    title: Mapped[str] = mapped_column(String)
    notes: Mapped[str] = mapped_column(Text, default="")
    payload_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    occurred_at: Mapped[datetime] = mapped_column(DateTime, index=True)
    source: Mapped[str] = mapped_column(String, default="tutor")
    created_by_user_id: Mapped[str | None] = mapped_column(String, ForeignKey("users.id"), nullable=True)
    related_entity_type: Mapped[str | None] = mapped_column(String, nullable=True)
    related_entity_id: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
