from datetime import datetime
from uuid import uuid4

from sqlalchemy import DateTime, ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base
from app.models.types import Money


class WalkCompletionReview(Base):
    __tablename__ = "walk_completion_reviews"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=lambda: str(uuid4()))
    tenant_id: Mapped[str | None] = mapped_column(String, nullable=True, index=True)
    walk_id: Mapped[str] = mapped_column(String, ForeignKey("walks.id"), index=True)
    walker_user_id: Mapped[str] = mapped_column(String, ForeignKey("users.id"), index=True)
    tutor_user_id: Mapped[str] = mapped_column(String, ForeignKey("users.id"), index=True)
    status: Mapped[str] = mapped_column(String, default="pending_review", index=True)
    photo_url: Mapped[str | None] = mapped_column(String, nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    checklist_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    admin_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    reviewed_by_admin_id: Mapped[str | None] = mapped_column(String, nullable=True)
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    # Migration 0107 (motor de cancelamento): distingue a fila de aprovação da
    # revisão NORMAL de finalização ("completion", default — zero-regressão) da
    # revisão de COMPENSAÇÃO DE CANCELAMENTO tardio do walker ("cancellation_compensation").
    # Reusa a MESMA fila (mesmo endpoint /walk-completions/pending + approve/reject) —
    # ver ramificação de kind em admin.approve_walk_completion/reject_walk_completion.
    kind: Mapped[str] = mapped_column(String, nullable=False, default="completion", server_default="completion")
    # Preenchido só quando kind="cancellation_compensation": valor a virar
    # WalkerEarning quando o admin aprova. NULL em revisões de finalização normal.
    compensation_amount: Mapped[float | None] = mapped_column(Money, nullable=True)
