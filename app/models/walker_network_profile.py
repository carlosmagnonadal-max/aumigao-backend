from datetime import datetime
from uuid import uuid4

from sqlalchemy import Boolean, DateTime, Float, ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


def _uuid() -> str:
    return str(uuid4())


class WalkerNetworkProfile(Base):
    __tablename__ = "walker_network_profile"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    walker_user_id: Mapped[str] = mapped_column(String, ForeignKey("users.id"), nullable=False, unique=True, index=True)
    network_status: Mapped[str] = mapped_column(String, default="active", index=True)
    global_reputation_score: Mapped[float] = mapped_column(Float, default=0)
    total_completed_walks: Mapped[int] = mapped_column(Integer, default=0)
    total_tenants_served: Mapped[int] = mapped_column(Integer, default=0)
    network_enabled: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    # ── Fase 1 Passo 1 (migration 0048) ──────────────────────────────────────
    # Tenant ao qual o passeador está vinculado exclusivamente (NULL = rede compartilhada).
    # FK para tenants.id declarada como index=True; a constraint FK real no banco é
    # criada via migration (não inline no ADD COLUMN do SQLite).
    exclusive_tenant_id: Mapped[str | None] = mapped_column(
        String, ForeignKey("tenants.id"), nullable=True, index=True
    )
