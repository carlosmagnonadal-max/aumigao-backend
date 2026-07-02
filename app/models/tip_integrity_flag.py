from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base
from app.models.types import Money


class TipIntegrityFlag(Base):
    __tablename__ = "tip_integrity_flags"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    walker_id: Mapped[str] = mapped_column(String, ForeignKey("users.id"), index=True)
    tutor_id: Mapped[str | None] = mapped_column(String, ForeignKey("users.id"), nullable=True, index=True)
    walk_id: Mapped[str | None] = mapped_column(String, ForeignKey("walks.id"), nullable=True, index=True)
    tip_amount: Mapped[float] = mapped_column(Money, default=0.0)
    flag_type: Mapped[str] = mapped_column(String, index=True)
    severity: Mapped[str] = mapped_column(String, default="low", index=True)
    status: Mapped[str] = mapped_column(String, default="open", index=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    walker = relationship("User", foreign_keys=[walker_id])
    tutor = relationship("User", foreign_keys=[tutor_id])
    walk = relationship("Walk")
