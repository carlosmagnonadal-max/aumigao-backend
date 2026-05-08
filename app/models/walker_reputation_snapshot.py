from datetime import datetime

from sqlalchemy import DateTime, Float, ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base


class WalkerReputationSnapshot(Base):
    __tablename__ = "walker_reputation_snapshots"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    walker_id: Mapped[str] = mapped_column(String, ForeignKey("users.id"), index=True)
    rating_score: Mapped[float] = mapped_column(Float, default=75.0)
    experience_score: Mapped[float] = mapped_column(Float, default=40.0)
    behavior_score: Mapped[float] = mapped_column(Float, default=75.0)
    consistency_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    recent_rating_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    risk_penalty: Mapped[float] = mapped_column(Float, default=0.0)
    hybrid_reputation_score: Mapped[float] = mapped_column(Float, default=75.0)
    risk_level: Mapped[str] = mapped_column(String, default="normal", index=True)
    calculated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    walker = relationship("User")
