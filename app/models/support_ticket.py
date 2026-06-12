from datetime import datetime
from uuid import uuid4

from sqlalchemy import DateTime, ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


def _uuid() -> str:
    return str(uuid4())


class SupportTicket(Base):
    __tablename__ = "support_tickets"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    tenant_id: Mapped[str | None] = mapped_column(
        String, ForeignKey("tenants.id"), nullable=True, index=True
    )
    subject: Mapped[str] = mapped_column(String, nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    requester_name: Mapped[str | None] = mapped_column(String, nullable=True)
    requester_email: Mapped[str | None] = mapped_column(String, nullable=True)
    requester_role: Mapped[str | None] = mapped_column(
        String, nullable=True  # "tutor" | "walker" | "interno"
    )
    status: Mapped[str] = mapped_column(
        String, nullable=False, default="open", index=True
    )  # open | in_progress | resolved | closed
    priority: Mapped[str] = mapped_column(
        String, nullable=False, default="normal"
    )  # low | normal | high
    assignee_user_id: Mapped[str | None] = mapped_column(String, nullable=True)
    internal_notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, index=True
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow
    )
