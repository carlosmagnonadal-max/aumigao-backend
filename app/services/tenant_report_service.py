"""F3.3 — agregação do relatório de saúde do pool por tenant (read-only)."""
from datetime import datetime

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.models.payment import Payment
from app.models.tenant_walker_access import TenantWalkerAccess
from app.models.user import User
from app.models.walk import Walk

# Estados operacionais (mesmos literais usados em operational_matching_service).
RIDE_COMPLETED = "ride_completed"
RIDE_CANCELLED = "ride_cancelled"


def _completion_rate(completed: int, total: int) -> float:
    """Taxa de conclusão (0..1, 2 casas). 0.0 quando não há passeios."""
    if total <= 0:
        return 0.0
    return round(completed / total, 2)


def build_tenant_pool_report(
    db: Session, tenant_id: str, date_from: datetime, date_to: datetime
) -> dict:
    """Resumo operacional do pool de um tenant no período [date_from, date_to].

    Read-only; agrega sobre Walk/Payment/TenantWalkerAccess já isolados por tenant.
    Período filtrado por Walk.created_at.
    """
    base = db.query(Walk).filter(
        Walk.tenant_id == tenant_id,
        Walk.created_at >= date_from,
        Walk.created_at <= date_to,
    )
    completed = base.filter(Walk.operational_status == RIDE_COMPLETED).count()
    cancelled = base.filter(Walk.operational_status == RIDE_CANCELLED).count()
    total = completed + cancelled

    active = (
        db.query(TenantWalkerAccess.walker_user_id)
        .filter(
            TenantWalkerAccess.tenant_id == tenant_id,
            TenantWalkerAccess.status == "active",
        )
        .distinct()
        .count()
    )

    revenue = (
        db.query(func.coalesce(func.sum(Payment.walker_amount), 0.0))
        .join(Walk, Walk.id == Payment.walk_id)
        .filter(
            Walk.tenant_id == tenant_id,
            Walk.operational_status == RIDE_COMPLETED,
            Walk.created_at >= date_from,
            Walk.created_at <= date_to,
        )
        .scalar()
    ) or 0.0

    top_rows = (
        db.query(Walk.walker_id, func.count(Walk.id).label("n"))
        .filter(
            Walk.tenant_id == tenant_id,
            Walk.operational_status == RIDE_COMPLETED,
            Walk.created_at >= date_from,
            Walk.created_at <= date_to,
            Walk.walker_id.isnot(None),
        )
        .group_by(Walk.walker_id)
        .order_by(func.count(Walk.id).desc())
        .limit(5)
        .all()
    )
    top_walkers = []
    for walker_id, n in top_rows:
        user = db.get(User, walker_id)
        top_walkers.append({
            "walker_user_id": walker_id,
            "name": getattr(user, "name", None) or getattr(user, "email", None) or walker_id,
            "completed_walks": int(n),
        })

    return {
        "tenant_id": tenant_id,
        "period": {"from": date_from.date().isoformat(), "to": date_to.date().isoformat()},
        "walks": {
            "completed": completed,
            "cancelled": cancelled,
            "total": total,
            "completion_rate": _completion_rate(completed, total),
        },
        "walkers": {"active": active},
        "revenue": {"walker_amount_total": round(float(revenue), 2)},
        "top_walkers": top_walkers,
    }
