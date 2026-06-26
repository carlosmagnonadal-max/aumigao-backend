import logging
from datetime import datetime
from sqlalchemy.orm import Session
from app.models.fiscal import (
    PaymentProvision, REVENUE_WALK_COMMISSION, REVENUE_SAAS_SUBSCRIPTION, REVENUE_TIP,
)
from app.services.fiscal_config_service import resolve_fiscal_config

logger = logging.getLogger("aumigao.provision_service")

def _f(v) -> float:
    return float(v) if v is not None else 0.0

def _bases(payment, revenue_type, cfg):
    """Retorna (walker_gross, walker_pct, platform_gross, platform_pct)."""
    if revenue_type == REVENUE_SAAS_SUBSCRIPTION:
        return 0.0, 0.0, _f(payment.amount), _f(cfg.subscription_tax_percent)
    if revenue_type == REVENUE_TIP:
        return _f(payment.amount), _f(cfg.walker_tax_percent), 0.0, 0.0
    # default: walk_commission
    return (
        _f(getattr(payment, "walker_amount", None)), _f(cfg.walker_tax_percent),
        _f(getattr(payment, "platform_amount", None)), _f(cfg.commission_tax_percent),
    )

def get_provision(db: Session, payment_id: str) -> PaymentProvision | None:
    return db.query(PaymentProvision).filter(PaymentProvision.payment_id == payment_id).first()

def compute_and_store_provision(db: Session, tenant_id: str, payment, revenue_type: str) -> PaymentProvision:
    existing = get_provision(db, payment.id)
    if existing is not None:
        return existing  # idempotente + imutável
    cfg = resolve_fiscal_config(db, tenant_id)
    wg, wpct, pg, ppct = _bases(payment, revenue_type, cfg)
    wtax = round(wg * wpct / 100.0, 2); ptax = round(pg * ppct / 100.0, 2)
    prov = PaymentProvision(
        tenant_id=tenant_id, payment_id=payment.id, revenue_type=revenue_type,
        walker_gross=wg, walker_tax=wtax, walker_net=round(wg - wtax, 2),
        platform_gross=pg, platform_tax=ptax, platform_net=round(pg - ptax, 2),
        walker_tax_percent_applied=wpct, platform_tax_percent_applied=ppct,
    )
    db.add(prov); db.commit(); db.refresh(prov)
    return prov

def list_provisions(
    db: Session,
    tenant_id: str,
    *,
    limit: int = 25,
    offset: int = 0,
    date_from: datetime | None = None,
    date_to: datetime | None = None,
) -> list[PaymentProvision]:
    q = db.query(PaymentProvision).filter(PaymentProvision.tenant_id == tenant_id)
    if date_from is not None:
        q = q.filter(PaymentProvision.created_at >= date_from)
    if date_to is not None:
        q = q.filter(PaymentProvision.created_at <= date_to)
    q = q.order_by(PaymentProvision.created_at.desc())
    return q.limit(max(1, min(limit, 200))).offset(max(0, offset)).all()


def financial_summary(db: Session, tenant_id: str, *, date_from: datetime | None = None, date_to: datetime | None = None) -> dict:
    q = db.query(PaymentProvision).filter(PaymentProvision.tenant_id == tenant_id)
    if date_from is not None:
        q = q.filter(PaymentProvision.created_at >= date_from)
    if date_to is not None:
        q = q.filter(PaymentProvision.created_at <= date_to)
    rows = q.all()
    agg = {
        "count": len(rows),
        "gross_total": 0.0,
        "platform_gross": 0.0, "platform_tax_reserved": 0.0, "platform_net": 0.0,
        "walker_gross": 0.0, "walker_tax_reserved": 0.0, "walker_net": 0.0,
    }
    for r in rows:
        agg["platform_gross"] += _f(r.platform_gross)
        agg["platform_tax_reserved"] += _f(r.platform_tax)
        agg["platform_net"] += _f(r.platform_net)
        agg["walker_gross"] += _f(r.walker_gross)
        agg["walker_tax_reserved"] += _f(r.walker_tax)
        agg["walker_net"] += _f(r.walker_net)
    agg["gross_total"] = round(agg["platform_gross"] + agg["walker_gross"], 2)
    return {k: (round(v, 2) if isinstance(v, float) else v) for k, v in agg.items()}
