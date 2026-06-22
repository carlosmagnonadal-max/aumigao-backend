from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class ProtectedChatMessage(Base):
    __tablename__ = "protected_chat_messages"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    walk_id: Mapped[str] = mapped_column(String, ForeignKey("walks.id"), index=True)
    sender_user_id: Mapped[str] = mapped_column(String, ForeignKey("users.id"), index=True)
    sender_role: Mapped[str] = mapped_column(String, index=True)
    body: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, index=True)
    read_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # Desnormalizado de walks.tenant_id para permitir RLS direto na tabela.
    # Nullable para compatibilidade retroativa; backfill em 0046_.
    tenant_id: Mapped[str | None] = mapped_column(
        String, ForeignKey("tenants.id"), nullable=True, index=True
    )
