"""Disponibilidade semanal do passeador (WK-01).

Uma linha por passeador. O schedule editável da tela (Record<dia,{enabled,slots}>)
é persistido como JSON. Antes vivia só em AsyncStorage local no app; agora é a
fonte de verdade no backend, consumível pelo matching (WK-10) e pelo admin.
"""
from datetime import datetime
from uuid import uuid4

from sqlalchemy import DateTime, ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class WalkerAvailability(Base):
    __tablename__ = "walker_availability"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=lambda: str(uuid4()))
    walker_user_id: Mapped[str] = mapped_column(
        String, ForeignKey("users.id"), nullable=False, unique=True, index=True
    )
    # JSON do dict {dia: {enabled: bool, slots: [str]}}. Default "{}" = sem disponibilidade.
    schedule_json: Mapped[str] = mapped_column(Text, default="{}", server_default="{}")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
