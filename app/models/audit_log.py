"""Trilha de auditoria (spec §14) — registra QUEM fez O QUÊ em ações críticas.

before_data/after_data são JSON serializado (Text) para portabilidade. NÃO devem
conter senha, token nem documento bruto (payload minimizado — spec §14.4).
"""
from datetime import datetime
from uuid import uuid4

from sqlalchemy import DateTime, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class AuditLog(Base):
    __tablename__ = "audit_logs"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=lambda: str(uuid4()))
    actor_user_id: Mapped[str | None] = mapped_column(String, nullable=True, index=True)
    actor_type: Mapped[str] = mapped_column(String, default="user")  # user | system | admin
    tenant_id: Mapped[str | None] = mapped_column(String, nullable=True, index=True)
    tenant_unit_id: Mapped[str | None] = mapped_column(String, nullable=True)
    action: Mapped[str] = mapped_column(String, index=True)  # ex: walker.approved
    entity_type: Mapped[str] = mapped_column(String, index=True)  # ex: walker
    entity_id: Mapped[str | None] = mapped_column(String, nullable=True, index=True)
    before_data: Mapped[str | None] = mapped_column(Text, nullable=True)
    after_data: Mapped[str | None] = mapped_column(Text, nullable=True)
    ip_address: Mapped[str | None] = mapped_column(String, nullable=True)
    user_agent: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, index=True)
