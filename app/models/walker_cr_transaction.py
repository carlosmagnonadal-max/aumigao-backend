from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base


class WalkerCrTransaction(Base):
    __tablename__ = "walker_cr_transactions"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    walker_user_id: Mapped[str] = mapped_column(
        String, ForeignKey("users.id"), index=True
    )
    # Positive = credit; negative = debit
    amount: Mapped[int] = mapped_column(Integer)
    # earn | spend | penalty | admin_adjust
    tx_type: Mapped[str] = mapped_column(String)
    # walk_completed | review_5star | weekly_mission | kit_approved |
    # boost_24h | no_show | admin
    source: Mapped[str] = mapped_column(String)
    description: Mapped[str] = mapped_column(Text, default="")
    related_entity_type: Mapped[str | None] = mapped_column(String, nullable=True)
    related_entity_id: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    walker = relationship("User")
