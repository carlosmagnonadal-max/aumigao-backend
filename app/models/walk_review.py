from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base


class WalkReview(Base):
    __tablename__ = "walk_reviews"
    __table_args__ = (
        UniqueConstraint("walk_id", name="uq_walk_reviews_walk_id"),
        CheckConstraint("rating >= 1 AND rating <= 5", name="ck_walk_reviews_rating_range"),
    )

    id: Mapped[str] = mapped_column(String, primary_key=True)
    walk_id: Mapped[str] = mapped_column(String, ForeignKey("walks.id"), index=True)
    tutor_id: Mapped[str] = mapped_column(String, ForeignKey("users.id"), index=True)
    walker_id: Mapped[str] = mapped_column(String, ForeignKey("users.id"), index=True)
    rating: Mapped[int] = mapped_column(Integer)
    comment: Mapped[str | None] = mapped_column(Text, nullable=True)
    tags_json: Mapped[str] = mapped_column(Text, default="[]")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    walk = relationship("Walk")
    tutor = relationship("User", foreign_keys=[tutor_id])
    walker = relationship("User", foreign_keys=[walker_id])
