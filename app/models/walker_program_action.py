"""Acoes administrativas do programa de passeadores (cr, kit, tip).

Substitui a lista em memoria WALKER_PROGRAM_ACTIONS que sumia a cada deploy.
"""
from datetime import datetime
from uuid import uuid4

from sqlalchemy import DateTime, Index, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class WalkerProgramAction(Base):
    """Registro imutavel de cada acao do programa de passeadores."""

    __tablename__ = "walker_program_actions"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=lambda: str(uuid4()))
    action_type: Mapped[str] = mapped_column(String, nullable=False)  # "cr" | "kit" | "tip"
    walker_id: Mapped[str | None] = mapped_column(String, nullable=True)
    payload_json: Mapped[str] = mapped_column(Text, default="{}")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, index=True)

    __table_args__ = (
        Index("ix_walker_program_actions_action_type", "action_type"),
        Index("ix_walker_program_actions_walker_id", "walker_id"),
    )
