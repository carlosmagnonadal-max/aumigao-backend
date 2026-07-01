from datetime import date, datetime
from uuid import uuid4

from sqlalchemy import Boolean, Date, DateTime, ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base

REMINDER_KINDS: frozenset[str] = frozenset({"vaccine", "vermifuge", "birthday", "inactivity"})


class PetReminder(Base):
    """Lembrete determinístico de saúde/atividade do pet (Fase 3 do Perfil Vivo).

    Cada lembrete representa uma instância de disparo: vacina/vermífugo com data-alvo,
    aniversário (1 por ano), ou inatividade (criado ao detectar janela vencida).
    Idempotência garantida por (pet_id, kind, source_event_id) no serviço.
    """

    __tablename__ = "pet_reminders"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=lambda: str(uuid4()))
    pet_id: Mapped[str] = mapped_column(String, ForeignKey("pets.id"), nullable=False, index=True)
    tenant_id: Mapped[str | None] = mapped_column(
        String, ForeignKey("tenants.id"), nullable=True, index=True
    )
    # Tipo do lembrete: "vaccine" | "vermifuge" | "birthday" | "inactivity"
    kind: Mapped[str] = mapped_column(String, nullable=False, index=True)
    # Data-alvo do lembrete (vencimento da vacina, aniversário, etc.)
    due_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    # False quando o reminder foi cancelado/substituído
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    # Controle de idempotência do disparo — atualizado em cada envio
    last_notified_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # Liga ao evento da timeline que originou este reminder (vacina/medicação manual)
    source_event_id: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=datetime.utcnow)
