"""Progresso da Capacitação do passeador por módulo e versão do conteúdo (S2, migration 0110).

GLOBAL (sem tenant_id / sem RLS): o passeador é da rede global, como walker_profiles.
"""
from datetime import datetime
from uuid import uuid4

from sqlalchemy import DateTime, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class WalkerTrainingProgress(Base):
    __tablename__ = "walker_training_progress"
    __table_args__ = (
        UniqueConstraint("walker_user_id", "content_version", "module_id", name="uq_walker_training_progress_module"),
    )

    id: Mapped[str] = mapped_column(String, primary_key=True, default=lambda: str(uuid4()))
    walker_user_id: Mapped[str] = mapped_column(String, index=True, nullable=False)
    content_version: Mapped[str] = mapped_column(String, nullable=False)
    module_id: Mapped[str] = mapped_column(String, nullable=False)
    best_score: Mapped[int] = mapped_column(Integer, default=0, server_default="0", nullable=False)
    attempts: Mapped[int] = mapped_column(Integer, default=0, server_default="0", nullable=False)
    passed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime | None] = mapped_column(DateTime, default=datetime.utcnow, nullable=True)
