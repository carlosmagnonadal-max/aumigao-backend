from datetime import datetime
from uuid import uuid4

from sqlalchemy import DateTime, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class WalkerKitSubmission(Base):
    __tablename__ = "walker_kit_submissions"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=lambda: str(uuid4()))
    walker_user_id: Mapped[str] = mapped_column(String, unique=True, index=True)
    items_json: Mapped[str] = mapped_column(Text, default="{}")
    audit_status: Mapped[str] = mapped_column(String, default="pending_review")
    audit_note: Mapped[str] = mapped_column(Text, default="")
    reviewed_by_admin_id: Mapped[str | None] = mapped_column(String, nullable=True)
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
