"""Cupons de desconto por tenant (Onda 2 — monetização).

Cada tenant cria cupons (código, desconto, validade, limites). Antifraude: limite
total de usos + limite por usuário, com registro de cada resgate. Gated pela
feature flag por tenant `coupons`. Valor do desconto é configurável pelo tenant
(ver memória precos-mutaveis-por-tenant). O resgate no pagamento é integração do
checkout (mobile) — aqui ficam catálogo, validação e registro.
"""
from datetime import datetime
from uuid import uuid4

from sqlalchemy import Boolean, DateTime, Float, ForeignKey, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base

COUPONS_FEATURE_KEY = "coupons"

DISCOUNT_PERCENT = "percent"
DISCOUNT_FIXED = "fixed"


def _uuid() -> str:
    return str(uuid4())


class Coupon(Base):
    __tablename__ = "coupons"
    # Código único por tenant (o mesmo código pode existir em tenants diferentes).
    __table_args__ = (UniqueConstraint("tenant_id", "code", name="uq_coupons_tenant_code"),)

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    tenant_id: Mapped[str] = mapped_column(String, ForeignKey("tenants.id"), nullable=False, index=True)
    code: Mapped[str] = mapped_column(String, nullable=False, index=True)
    discount_type: Mapped[str] = mapped_column(String, nullable=False, default=DISCOUNT_PERCENT)
    # percent: 0-100; fixed: valor absoluto na moeda do tenant.
    discount_value: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    min_amount: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    # null = ilimitado.
    max_uses: Mapped[int | None] = mapped_column(Integer, nullable=True)
    max_uses_per_user: Mapped[int | None] = mapped_column(Integer, nullable=True, default=1)
    uses_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    valid_from: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    valid_until: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class CouponRedemption(Base):
    """Registro de cada resgate (antifraude + limite por usuário)."""

    __tablename__ = "coupon_redemptions"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    coupon_id: Mapped[str] = mapped_column(String, ForeignKey("coupons.id"), nullable=False, index=True)
    tenant_id: Mapped[str] = mapped_column(String, ForeignKey("tenants.id"), nullable=False, index=True)
    user_id: Mapped[str] = mapped_column(String, nullable=False, index=True)
    walk_id: Mapped[str | None] = mapped_column(String, nullable=True)
    amount_discounted: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
