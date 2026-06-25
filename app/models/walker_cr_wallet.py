from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base


class WalkerCrWallet(Base):
    __tablename__ = "walker_cr_wallets"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    walker_user_id: Mapped[str] = mapped_column(
        String, ForeignKey("users.id"), unique=True, index=True
    )
    balance: Mapped[int] = mapped_column(Integer, default=0)
    lifetime_earned: Mapped[int] = mapped_column(Integer, default=0)
    lifetime_spent: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow
    )

    walker = relationship("User")
