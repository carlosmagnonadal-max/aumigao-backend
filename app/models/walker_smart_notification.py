from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base


class WalkerSmartNotification(Base):
    __tablename__ = "walker_smart_notifications"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    walker_user_id: Mapped[str] = mapped_column(
        String, ForeignKey("users.id"), index=True
    )
    notification_type: Mapped[str] = mapped_column(String)
    title: Mapped[str] = mapped_column(String)
    message: Mapped[str] = mapped_column(Text, default="")
    # low | normal | high
    priority: Mapped[str] = mapped_column(String, default="normal")
    trigger_source: Mapped[str] = mapped_column(String)
    read_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    sent_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    walker = relationship("User")
