from datetime import datetime

from sqlalchemy import DateTime, Float, ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base


class WalkTip(Base):
    __tablename__ = "walk_tips"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    walk_id: Mapped[str] = mapped_column(String, ForeignKey("walks.id"), index=True)
    tutor_id: Mapped[str] = mapped_column(String, ForeignKey("users.id"), index=True)
    walker_id: Mapped[str] = mapped_column(String, ForeignKey("users.id"), index=True)
    amount: Mapped[float] = mapped_column(Float)
    status: Mapped[str] = mapped_column(String, default="pending", index=True)
    provider: Mapped[str] = mapped_column(String, default="internal_mock")
    checkout_url: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, index=True)
    paid_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    walk = relationship("Walk")
    tutor = relationship("User", foreign_keys=[tutor_id])
    walker = relationship("User", foreign_keys=[walker_id])
