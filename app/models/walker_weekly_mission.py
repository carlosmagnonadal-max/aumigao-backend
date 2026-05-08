from datetime import datetime

from sqlalchemy import DateTime, Float, ForeignKey, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base


class WalkerWeeklyMission(Base):
    __tablename__ = "walker_weekly_missions"
    __table_args__ = (
        UniqueConstraint("walker_id", "mission_type", "week_start", name="uq_walker_weekly_mission"),
    )

    id: Mapped[str] = mapped_column(String, primary_key=True)
    walker_id: Mapped[str] = mapped_column(String, ForeignKey("users.id"), index=True)
    mission_type: Mapped[str] = mapped_column(String, index=True)
    title: Mapped[str] = mapped_column(String)
    description: Mapped[str] = mapped_column(Text)
    metric_key: Mapped[str] = mapped_column(String)
    target_value: Mapped[float] = mapped_column(Float)
    current_value: Mapped[float] = mapped_column(Float, default=0)
    progress_percentage: Mapped[float] = mapped_column(Float, default=0)
    status: Mapped[str] = mapped_column(String, default="not_started", index=True)
    week_start: Mapped[datetime] = mapped_column(DateTime, index=True)
    week_end: Mapped[datetime] = mapped_column(DateTime, index=True)
    reward_status: Mapped[str] = mapped_column(String, default="none")
    reward_description: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    expired_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    walker = relationship("User")
