from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, JSON, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base


class WalkerRecoveryPlan(Base):
    __tablename__ = "walker_recovery_plans"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    walker_id: Mapped[str] = mapped_column(String, ForeignKey("users.id"), index=True)
    risk_level_at_start: Mapped[str] = mapped_column(String, default="attention")
    status: Mapped[str] = mapped_column(String, default="active", index=True)
    reason: Mapped[str] = mapped_column(Text, default="")
    recommended_actions: Mapped[list] = mapped_column(JSON, default=list)
    started_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    ends_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    walker = relationship("User")
