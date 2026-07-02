from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base
from app.models.types import Money


class WalkerIncentive(Base):
    __tablename__ = "walker_incentives"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    walker_id: Mapped[str] = mapped_column(String, ForeignKey("users.id"), index=True)
    incentive_type: Mapped[str] = mapped_column(String, index=True)
    title: Mapped[str] = mapped_column(String)
    description: Mapped[str] = mapped_column(Text, default="")
    source: Mapped[str] = mapped_column(String, default="system")
    # Tipo de recompensa (recognition | visibility | monetary). Incentivos —
    # spec 2026-06-10. Monetario REGISTRA amount; payout/split e follow-up.
    reward_type: Mapped[str] = mapped_column(String, default="recognition")
    amount: Mapped[float] = mapped_column(Money, default=0.0)
    status: Mapped[str] = mapped_column(String, default="pending", index=True)
    visibility_effect: Mapped[str] = mapped_column(String, default="none")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    granted_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    admin_notes: Mapped[str | None] = mapped_column(Text, nullable=True)

    walker = relationship("User")
