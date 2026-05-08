from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base


class WalkerBoost(Base):
    __tablename__ = "walker_boosts"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    walker_id: Mapped[str] = mapped_column(String, ForeignKey("users.id"), index=True)
    boost_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    boost_type: Mapped[str | None] = mapped_column(String, nullable=True)
    boost_score: Mapped[int] = mapped_column(Integer, default=0)
    boost_start_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    boost_end_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    boost_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    boost_status: Mapped[str] = mapped_column(String, default="inactive")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    walker = relationship("User")
