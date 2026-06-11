"""Configuracoes administrativas persistidas no banco (key-value JSON).

Substitui os dicts em memoria REFERRAL_PROGRAM_SETTINGS e
WALKER_PROGRAM_SETTINGS que sumiam a cada deploy do Railway.
"""
from datetime import datetime

from sqlalchemy import DateTime, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class AppSetting(Base):
    """Armazena configuracoes de programa (referral, walker, etc.) como JSON."""

    __tablename__ = "app_settings"

    key: Mapped[str] = mapped_column(String, primary_key=True)
    value_json: Mapped[str] = mapped_column(Text, default="{}")
    updated_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    updated_by: Mapped[str | None] = mapped_column(String, nullable=True)
