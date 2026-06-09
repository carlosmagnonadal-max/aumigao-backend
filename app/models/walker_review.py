from datetime import datetime

from sqlalchemy import Boolean, CheckConstraint, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base


class WalkerReview(Base):
    __tablename__ = "walker_reviews"
    __table_args__ = (
        UniqueConstraint("walk_id", name="uq_walker_reviews_walk_id"),
        CheckConstraint("rating >= 1 AND rating <= 5", name="ck_walker_reviews_rating_range"),
    )

    id: Mapped[str] = mapped_column(String, primary_key=True)
    tenant_id: Mapped[str | None] = mapped_column(String, nullable=True, index=True)
    walk_id: Mapped[str] = mapped_column(String, ForeignKey("walks.id"), index=True)
    tutor_id: Mapped[str] = mapped_column(String, ForeignKey("users.id"), index=True)
    walker_id: Mapped[str] = mapped_column(String, ForeignKey("users.id"), index=True)
    rating: Mapped[int] = mapped_column(Integer)
    comment: Mapped[str | None] = mapped_column(Text, nullable=True)
    punctuality_rating: Mapped[int | None] = mapped_column(Integer, nullable=True)
    care_rating: Mapped[int | None] = mapped_column(Integer, nullable=True)
    communication_rating: Mapped[int | None] = mapped_column(Integer, nullable=True)
    is_flagged: Mapped[bool] = mapped_column(Boolean, default=False)
    admin_notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    walk = relationship("Walk")
    tutor = relationship("User", foreign_keys=[tutor_id])
    walker = relationship("User", foreign_keys=[walker_id])
