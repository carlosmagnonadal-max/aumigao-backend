from datetime import datetime
from uuid import uuid4

from sqlalchemy import (
    Boolean, DateTime, Float, ForeignKey, Integer, String, Text, UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base

# Defaults do programa de indicação do tutor (cunha 4).
DEFAULT_TRIGGER_N = 3


class TutorReferralConfig(Base):
    """Config por-tenant do programa de indicação do tutor. 1 linha por tenant, default OFF."""

    __tablename__ = "tutor_referral_configs"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=lambda: str(uuid4()))
    tenant_id: Mapped[str] = mapped_column(
        String, ForeignKey("tenants.id"), nullable=False, unique=True, index=True
    )
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    reward_type: Mapped[str] = mapped_column(String, nullable=False, default="desconto")
    discount_kind: Mapped[str] = mapped_column(String, nullable=False, default="percent")
    discount_value: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    free_walks_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    credit_walks: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    same_reward_both_sides: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    referrer_multiplier: Mapped[float] = mapped_column(Float, nullable=False, default=1.0)
    referred_multiplier: Mapped[float] = mapped_column(Float, nullable=False, default=1.0)
    trigger_type: Mapped[str] = mapped_column(String, nullable=False, default="primeiro_passeio_pago")
    trigger_n: Mapped[int] = mapped_column(Integer, nullable=False, default=DEFAULT_TRIGGER_N)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class TutorReferral(Base):
    """Rastreio de uma indicação tutor->tutor (tenant-scoped). Espelha WalkerReferral."""

    __tablename__ = "tutor_referrals"
    __table_args__ = (
        UniqueConstraint("tenant_id", "referred_user_id", name="uq_tutor_referral_tenant_referred"),
    )

    id: Mapped[str] = mapped_column(String, primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String, ForeignKey("tenants.id"), nullable=False, index=True)
    referrer_user_id: Mapped[str] = mapped_column(String, ForeignKey("users.id"), index=True)
    referred_user_id: Mapped[str | None] = mapped_column(
        String, ForeignKey("users.id"), nullable=True, index=True
    )
    referral_code: Mapped[str] = mapped_column(String, unique=True, index=True)
    invite_link: Mapped[str | None] = mapped_column(String, nullable=True)
    status: Mapped[str] = mapped_column(String, default="pending", index=True)
    reward_status: Mapped[str] = mapped_column(String, default="not_eligible", index=True)
    reward_snapshot_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    held_credits_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    completed_paid_walks_count: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    converted_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
