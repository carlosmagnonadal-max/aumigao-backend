from datetime import datetime

from sqlalchemy import DateTime, Float, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base


class WalkerReferral(Base):
    __tablename__ = "walker_referrals"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    # Tenant do passeador que indicou (isolamento multi-tenant). Nullable para
    # linhas legadas; novas indicações gravam o tenant do referrer. Sem isso, o
    # WalkerEarning de referral ficava com tenant_id=None e quebrava o isolamento
    # ao ligar WALKER_REFERRAL_PAYOUT_ENABLED.
    tenant_id: Mapped[str | None] = mapped_column(String, ForeignKey("tenants.id"), nullable=True, index=True)
    referrer_user_id: Mapped[str] = mapped_column(String, ForeignKey("users.id"), index=True)
    referred_user_id: Mapped[str | None] = mapped_column(String, ForeignKey("users.id"), nullable=True, index=True)
    referred_name: Mapped[str] = mapped_column(String)
    referred_phone: Mapped[str] = mapped_column(String)
    referred_phone_normalized: Mapped[str] = mapped_column(String, index=True)
    city: Mapped[str] = mapped_column(String)
    neighborhood: Mapped[str] = mapped_column(String)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    referral_code: Mapped[str] = mapped_column(String, unique=True, index=True)
    invite_link: Mapped[str | None] = mapped_column(String, nullable=True)
    status: Mapped[str] = mapped_column(String, default="pending", index=True)
    reward_status: Mapped[str] = mapped_column(String, default="not_eligible", index=True)
    reward_amount: Mapped[float | None] = mapped_column(Float, nullable=True)
    completed_walks_count: Mapped[int] = mapped_column(Integer, default=0)
    average_rating: Mapped[float | None] = mapped_column(Float, nullable=True)
    performance_status: Mapped[str | None] = mapped_column(String, default="neutral", nullable=True)
    rejection_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    approved_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    rejected_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    converted_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    referrer = relationship("User", foreign_keys=[referrer_user_id])
    referred = relationship("User", foreign_keys=[referred_user_id])
