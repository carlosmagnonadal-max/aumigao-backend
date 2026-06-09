"""Cálculo de split de receita (Sprint 16, Fase A).

Determina como o valor de um pagamento se divide entre a plataforma/tenant
(comissão) e o walker. A comissão vem da config do tenant; na ausência dela,
usa o padrão. Esta fase apenas REGISTRA o split (contábil) — o repasse real ao
walker via gateway é a Fase B.
"""
from __future__ import annotations

from sqlalchemy.orm import Session

from app.models.tenant_payment_config import DEFAULT_COMMISSION_PERCENT, TenantPaymentConfig


def get_commission_percent(db: Session, tenant_id: str | None) -> float:
    if tenant_id:
        config = (
            db.query(TenantPaymentConfig)
            .filter(
                TenantPaymentConfig.tenant_id == tenant_id,
                TenantPaymentConfig.active.is_(True),
            )
            .first()
        )
        if config and config.commission_percent is not None:
            return float(config.commission_percent)
    return DEFAULT_COMMISSION_PERCENT


def compute_split(amount: float, commission_percent: float) -> dict[str, float]:
    amount = round(float(amount or 0), 2)
    commission_percent = max(0.0, min(100.0, float(commission_percent)))
    platform_amount = round(amount * commission_percent / 100.0, 2)
    walker_amount = round(amount - platform_amount, 2)
    return {
        "commission_percent": commission_percent,
        "platform_amount": platform_amount,
        "walker_amount": walker_amount,
    }


def build_payment_split(db: Session, tenant_id: str | None, amount: float) -> dict[str, float]:
    return compute_split(amount, get_commission_percent(db, tenant_id))
