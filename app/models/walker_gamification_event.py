from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base


class WalkerGamificationEvent(Base):
    __tablename__ = "walker_gamification_events"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    walker_user_id: Mapped[str] = mapped_column(
        String, ForeignKey("users.id"), index=True
    )
    # mission_completed | badge_earned | level_up | cr_granted | boost_activated
    event_type: Mapped[str] = mapped_column(String)
    title: Mapped[str] = mapped_column(String)
    description: Mapped[str] = mapped_column(Text, default="")
    cr_amount: Mapped[int | None] = mapped_column(Integer, nullable=True)
    related_entity_type: Mapped[str | None] = mapped_column(String, nullable=True)
    related_entity_id: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    walker = relationship("User")
