from datetime import datetime

from sqlalchemy import DateTime, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class PushToken(Base):
    __tablename__ = "push_tokens"
    __table_args__ = (UniqueConstraint("expo_push_token", name="uq_push_tokens_expo_push_token"),)

    id: Mapped[str] = mapped_column(String, primary_key=True)
    user_id: Mapped[str] = mapped_column(String, index=True)
    expo_push_token: Mapped[str] = mapped_column(String, index=True)
    platform: Mapped[str] = mapped_column(String, default="unknown")
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
