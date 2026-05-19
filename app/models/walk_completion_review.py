from datetime import datetime
from uuid import uuid4

from sqlalchemy import DateTime, ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class WalkCompletionReview(Base):
    __tablename__ = "walk_completion_reviews"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=lambda: str(uuid4()))
    walk_id: Mapped[str] = mapped_column(String, ForeignKey("walks.id"), index=True)
    walker_user_id: Mapped[str] = mapped_column(String, ForeignKey("users.id"), index=True)
    tutor_user_id: Mapped[str] = mapped_column(String, ForeignKey("users.id"), index=True)
    status: Mapped[str] = mapped_column(String, default="pending_review", index=True)
    photo_url: Mapped[str | None] = mapped_column(String, nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    checklist_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    admin_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    reviewed_by_admin_id: Mapped[str | None] = mapped_column(String, nullable=True)
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
