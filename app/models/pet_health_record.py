from datetime import date, datetime
from uuid import uuid4

from sqlalchemy import Date, DateTime, ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base

# Tipos de registro da carteira de saúde (Perfil Vivo 2.0 — Fase A).
HEALTH_RECORD_KINDS: frozenset[str] = frozenset(
    {"vaccine", "dewormer", "flea_tick", "treatment"}
)

# Papel de quem registrou (co-edição auditada tutor/tenant).
HEALTH_RECORD_ROLES: frozenset[str] = frozenset({"tutor", "admin"})


class PetHealthRecord(Base):
    """Registro da carteira de saúde do pet — uma tabela para tudo.

    kind ∈ {vaccine, dewormer, flea_tick, treatment}. O status (em_dia/vencendo/
    atrasada/sem_validade) é CALCULADO em runtime a partir de valid_until — não é
    persistido (evita drift). Registros de vacina com valid_until alimentam o
    PetReminder de vacina existente (Fase 3) via ensure_vaccine_reminder.
    """

    __tablename__ = "pet_health_records"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=lambda: str(uuid4()))
    pet_id: Mapped[str] = mapped_column(String, ForeignKey("pets.id"), nullable=False, index=True)
    tenant_id: Mapped[str | None] = mapped_column(
        String, ForeignKey("tenants.id"), nullable=True, index=True
    )
    kind: Mapped[str] = mapped_column(String, nullable=False, index=True)
    name: Mapped[str] = mapped_column(String, nullable=False)
    applied_at: Mapped[date] = mapped_column(Date, nullable=False)
    valid_until: Mapped[date | None] = mapped_column(Date, nullable=True)
    notes: Mapped[str] = mapped_column(Text, nullable=False, default="")
    created_by_role: Mapped[str] = mapped_column(String, nullable=False, default="tutor")
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=datetime.utcnow)
