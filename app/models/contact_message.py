from datetime import datetime

from sqlalchemy import DateTime, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class ContactMessage(Base):
    """Lead do formulário de contato do site institucional (intake público).

    O backend é o sistema de registro do lead. A notificação (e-mail/n8n) é um hook
    plugável (ver contact_notification_service) — n8n é Sprint 18 e ainda não existe,
    então hoje o contato é persistido e registrado em log até a infra de envio entrar.
    """

    __tablename__ = "contact_messages"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    tenant_id: Mapped[str | None] = mapped_column(String, nullable=True, index=True)
    name: Mapped[str] = mapped_column(String, default="")
    company: Mapped[str] = mapped_column(String, default="")
    email: Mapped[str] = mapped_column(String, index=True)
    phone: Mapped[str] = mapped_column(String, default="")
    city: Mapped[str] = mapped_column(String, default="")
    business_type: Mapped[str] = mapped_column(String, default="")
    interest: Mapped[str] = mapped_column(String, default="")
    message: Mapped[str] = mapped_column(Text, default="")
    source: Mapped[str] = mapped_column(String, default="site", index=True)
    status: Mapped[str] = mapped_column(String, default="new", index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, index=True)
