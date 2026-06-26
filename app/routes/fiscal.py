from datetime import datetime
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from app.core.database import get_db
from app.dependencies.auth import get_current_user
from app.dependencies.rbac import require_permission
from app.dependencies.tenant_scope import get_admin_tenant_scope
from app.models.tenant import Tenant
from app.models.user import User
from app.schemas.fiscal import FiscalConfigUpdate
from app.services import fiscal_config_service as svc
from app.services import provision_service as prov_svc
from app.services.audit_service import record_audit_log

router = APIRouter(prefix="/admin/tenants", tags=["fiscal"])
api_router = APIRouter(prefix="/api/admin/tenants", tags=["fiscal"])
payments_router = APIRouter(prefix="/admin/payments", tags=["fiscal"])
api_payments_router = APIRouter(prefix="/api/admin/payments", tags=["fiscal"])


def _ensure_scope(admin: User, tenant_id: str, db: Session):
    scope = get_admin_tenant_scope(admin, db)
    if scope.tenant_id is not None and scope.tenant_id != tenant_id:
        raise HTTPException(status_code=404, detail="Tenant não encontrado.")
    if db.get(Tenant, tenant_id) is None:
        raise HTTPException(status_code=404, detail="Tenant não encontrado.")


def _serialize(tenant_id, cfg) -> dict:
    return {
        "tenant_id": tenant_id,
        "commission_tax_percent": float(cfg.commission_tax_percent or 0),
        "subscription_tax_percent": float(cfg.subscription_tax_percent or 0),
        "walker_tax_percent": float(cfg.walker_tax_percent or 0),
        "iss_percent": float(cfg.iss_percent) if cfg.iss_percent is not None else None,
        "municipal_service_code": cfg.municipal_service_code,
        "simples_nacional": cfg.simples_nacional,
        "cnae": cfg.cnae,
        "service_description": cfg.service_description,
        "active": bool(cfg.active) if cfg.active is not None else True,
    }


@router.get("/{tenant_id}/fiscal-config")
@api_router.get("/{tenant_id}/fiscal-config")
def get_fiscal_config(tenant_id: str, admin: User = Depends(require_permission("finance.read")), db: Session = Depends(get_db)):
    _ensure_scope(admin, tenant_id, db)
    return _serialize(tenant_id, svc.resolve_fiscal_config(db, tenant_id))


@router.put("/{tenant_id}/fiscal-config")
@api_router.put("/{tenant_id}/fiscal-config")
def put_fiscal_config(tenant_id: str, payload: FiscalConfigUpdate, admin: User = Depends(require_permission("finance.manage")), db: Session = Depends(get_db)):
    get_admin_tenant_scope(admin, db)  # injeta escopo RLS no topo da escrita
    _ensure_scope(admin, tenant_id, db)
    values = payload.model_dump(exclude_unset=True)
    cfg = svc.upsert_fiscal_config(db, tenant_id, values)
    record_audit_log(db, action="fiscal_config.updated", entity_type="tenant_fiscal_config",
                     entity_id=cfg.id, actor=admin, after=values, tenant_id=tenant_id)
    db.commit()
    return _serialize(tenant_id, cfg)


@router.get("/{tenant_id}/financial-summary")
@api_router.get("/{tenant_id}/financial-summary")
def financial_summary(tenant_id: str, date_from: str | None = None, date_to: str | None = None,
                      admin: User = Depends(require_permission("finance.read")), db: Session = Depends(get_db)):
    _ensure_scope(admin, tenant_id, db)
    df = datetime.fromisoformat(date_from) if date_from else None
    dt = datetime.fromisoformat(date_to) if date_to else None
    return prov_svc.financial_summary(db, tenant_id, date_from=df, date_to=dt)


@router.get("/{tenant_id}/provisions")
@api_router.get("/{tenant_id}/provisions")
def list_provisions(
    tenant_id: str,
    limit: int = 25,
    offset: int = 0,
    date_from: str | None = None,
    date_to: str | None = None,
    admin: User = Depends(require_permission("finance.read")),
    db: Session = Depends(get_db),
):
    _ensure_scope(admin, tenant_id, db)
    df = datetime.fromisoformat(date_from) if date_from else None
    dt = datetime.fromisoformat(date_to) if date_to else None
    rows = prov_svc.list_provisions(db, tenant_id, limit=limit, offset=offset, date_from=df, date_to=dt)

    def _row(p):
        g = float(p.platform_gross) + float(p.walker_gross)
        t = float(p.platform_tax) + float(p.walker_tax)
        n = float(p.platform_net) + float(p.walker_net)
        return {
            "payment_id": p.payment_id,
            "revenue_type": p.revenue_type,
            "created_at": p.created_at.isoformat() if p.created_at else None,
            "walker_gross": float(p.walker_gross),
            "walker_tax": float(p.walker_tax),
            "walker_net": float(p.walker_net),
            "platform_gross": float(p.platform_gross),
            "platform_tax": float(p.platform_tax),
            "platform_net": float(p.platform_net),
            "gross": round(g, 2),
            "tax": round(t, 2),
            "net": round(n, 2),
        }

    return {"items": [_row(p) for p in rows], "limit": limit, "offset": offset}


@payments_router.get("/{payment_id}/provision")
@api_payments_router.get("/{payment_id}/provision")
def get_payment_provision(payment_id: str, admin: User = Depends(require_permission("finance.read")), db: Session = Depends(get_db)):
    prov = prov_svc.get_provision(db, payment_id)
    if prov is None:
        raise HTTPException(status_code=404, detail="Provisão não encontrada.")
    _ensure_scope(admin, prov.tenant_id, db)
    return {
        "payment_id": prov.payment_id, "tenant_id": prov.tenant_id, "revenue_type": prov.revenue_type,
        "walker_gross": float(prov.walker_gross), "walker_tax": float(prov.walker_tax), "walker_net": float(prov.walker_net),
        "platform_gross": float(prov.platform_gross), "platform_tax": float(prov.platform_tax), "platform_net": float(prov.platform_net),
    }
