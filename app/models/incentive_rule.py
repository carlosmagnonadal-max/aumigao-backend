"""Regras de incentivo configuraveis por tenant (Incentivos — spec 2026-06-10).

Substitui as regras HARDCODED do incentive_engine_service por regras que cada
tenant cria/configura no admin. Gated pela feature flag por tenant `incentives`.

- trigger_type: rating | completed_missions | hybrid_score | completed_walks
- reward_type: recognition | visibility | monetary
  (monetario apenas REGISTRA amount = reward_value; payout/split e follow-up).
"""
from datetime import datetime
from uuid import uuid4

from sqlalchemy import Boolean, DateTime, Float, ForeignKey, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base
from app.models.types import Money

INCENTIVES_FEATURE_KEY = "incentives"

# trigger_type aceitos
TRIGGER_RATING = "rating"
TRIGGER_COMPLETED_MISSIONS = "completed_missions"
TRIGGER_HYBRID_SCORE = "hybrid_score"
TRIGGER_COMPLETED_WALKS = "completed_walks"
TRIGGER_TYPES = {
    TRIGGER_RATING,
    TRIGGER_COMPLETED_MISSIONS,
    TRIGGER_HYBRID_SCORE,
    TRIGGER_COMPLETED_WALKS,
}

# reward_type aceitos
REWARD_RECOGNITION = "recognition"
REWARD_VISIBILITY = "visibility"
REWARD_MONETARY = "monetary"
REWARD_TYPES = {REWARD_RECOGNITION, REWARD_VISIBILITY, REWARD_MONETARY}


def _uuid() -> str:
    return str(uuid4())


class IncentiveRule(Base):
    __tablename__ = "incentive_rules"
    # Chave unica por tenant (a mesma key pode existir em tenants diferentes).
    __table_args__ = (UniqueConstraint("tenant_id", "key", name="uq_incentive_rules_tenant_key"),)

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    tenant_id: Mapped[str] = mapped_column(String, ForeignKey("tenants.id"), nullable=False, index=True)
    key: Mapped[str] = mapped_column(String, nullable=False, index=True)
    title: Mapped[str] = mapped_column(String, nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")
    trigger_type: Mapped[str] = mapped_column(String, nullable=False)
    threshold: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    reward_type: Mapped[str] = mapped_column(String, nullable=False, default=REWARD_RECOGNITION)
    # monetary: bonus em R$ (apenas REGISTRA amount; nao integra payout).
    reward_value: Mapped[float] = mapped_column(Money, nullable=False, default=0.0)
    visibility_effect: Mapped[str] = mapped_column(String, nullable=False, default="none")
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
