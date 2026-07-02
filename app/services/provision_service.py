import logging
from datetime import datetime
from decimal import Decimal
from sqlalchemy.orm import Session
from app.core.money import q2, to_float, to_money
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
    wg_d, pg_d = to_money(wg), to_money(pg)
    wtax_d = q2(wg_d * to_money(wpct) / Decimal("100"))
    ptax_d = q2(pg_d * to_money(ppct) / Decimal("100"))
    prov = PaymentProvision(
        tenant_id=tenant_id, payment_id=payment.id, revenue_type=revenue_type,
        walker_gross=to_float(wg_d), walker_tax=to_float(wtax_d), walker_net=to_float(q2(wg_d - wtax_d)),
        platform_gross=to_float(pg_d), platform_tax=to_float(ptax_d), platform_net=to_float(q2(pg_d - ptax_d)),
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
        "platform_gross": to_money(0), "platform_tax_reserved": to_money(0), "platform_net": to_money(0),
        "walker_gross": to_money(0), "walker_tax_reserved": to_money(0), "walker_net": to_money(0),
    }
    for r in rows:
        agg["platform_gross"] += to_money(r.platform_gross)
        agg["platform_tax_reserved"] += to_money(r.platform_tax)
        agg["platform_net"] += to_money(r.platform_net)
        agg["walker_gross"] += to_money(r.walker_gross)
        agg["walker_tax_reserved"] += to_money(r.walker_tax)
        agg["walker_net"] += to_money(r.walker_net)
    # Borda: soma em Decimal, entrega float (contrato do endpoint financeiro).
    out = {k: to_float(q2(v)) for k, v in agg.items()}
    out["count"] = len(rows)
    out["gross_total"] = to_float(q2(agg["platform_gross"] + agg["walker_gross"]))
    return out
