"""Rotas de Incentivos (Incentivos — spec 2026-06-10).

- Admin do tenant: CRUD das regras de incentivo (IncentiveRule), concessao manual,
  revoke e listagem das concessoes. Gated (display/engine) pela flag `incentives`.
- Walker: ve seus proprios incentivos (avalia regras do tenant via engine).

Monetario apenas REGISTRA amount; payout/split/withdrawal e follow-up (NAO tocar).
"""
from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.dependencies.auth import get_current_user
from app.dependencies.rbac import require_permission
from app.dependencies.tenant_scope import get_admin_tenant_scope
from app.models.user import User
from app.schemas.incentive import (
    GrantedIncentiveListResponse,
    GrantedIncentiveResponse,
    IncentiveGrantRequest,
    IncentiveRevokeRequest,
    IncentiveRuleCreate,
    IncentiveRuleResponse,
    IncentiveRuleUpdate,
)
from app.schemas.metrics import IncentiveMetricsResponse
from app.services import incentive_rule_service as svc
from app.services.incentive_engine_service import evaluate_incentives, incentive_payload
from app.services.metrics_service import get_incentive_metrics
from app.services.tenant_context import resolve_current_tenant_id

# Rotas walker (self-service): ve seus incentivos.
walker_router = APIRouter(prefix="/walker/me", tags=["incentives"])
api_walker_router = APIRouter(prefix="/api/walker/me", tags=["incentives"])

# Rotas admin do tenant.
admin_router = APIRouter(prefix="/admin", tags=["incentives-admin"], dependencies=[Depends(require_permission("admin.access"))])
api_admin_router = APIRouter(prefix="/api/admin", tags=["incentives-admin"], dependencies=[Depends(require_permission("admin.access"))])


def _admin_tenant_id(admin: User, db: Session) -> str:
    scope = get_admin_tenant_scope(admin, db)
    return scope.tenant_id or resolve_current_tenant_id(db)


# --------------------------------------------------------------------------- #
# Walker — meus incentivos (avalia regras ativas do tenant)
# --------------------------------------------------------------------------- #
@walker_router.get("/incentives", response_model=GrantedIncentiveListResponse)
@api_walker_router.get("/incentives", response_model=GrantedIncentiveListResponse)
def my_incentives(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    incentives = evaluate_incentives(user.id, db)
    items = [incentive_payload(inc) for inc in incentives]
    return {"items": items, "total": len(items)}


# --------------------------------------------------------------------------- #
# Admin — CRUD das regras de incentivo
# --------------------------------------------------------------------------- #
@admin_router.get("/incentive-rules", response_model=list[IncentiveRuleResponse])
@api_admin_router.get("/incentive-rules", response_model=list[IncentiveRuleResponse])
def admin_list_rules(admin: User = Depends(require_permission("admin.access")), db: Session = Depends(get_db)):
    return svc.list_rules(_admin_tenant_id(admin, db), db)


@admin_router.post("/incentive-rules", response_model=IncentiveRuleResponse)
@api_admin_router.post("/incentive-rules", response_model=IncentiveRuleResponse)
def admin_create_rule(payload: IncentiveRuleCreate, admin: User = Depends(require_permission("admin.access")), db: Session = Depends(get_db)):
    return svc.create_rule(_admin_tenant_id(admin, db), payload.model_dump(), db)


@admin_router.patch("/incentive-rules/{rule_id}", response_model=IncentiveRuleResponse)
@api_admin_router.patch("/incentive-rules/{rule_id}", response_model=IncentiveRuleResponse)
def admin_update_rule(rule_id: str, payload: IncentiveRuleUpdate, admin: User = Depends(require_permission("admin.access")), db: Session = Depends(get_db)):
    return svc.update_rule(_admin_tenant_id(admin, db), rule_id, payload.model_dump(exclude_unset=True), db)


# --------------------------------------------------------------------------- #
# Admin — concessao manual / revoke / listagem das concessoes
# --------------------------------------------------------------------------- #
@admin_router.post("/walkers/{walker_id}/incentives", response_model=GrantedIncentiveResponse)
@api_admin_router.post("/walkers/{walker_id}/incentives", response_model=GrantedIncentiveResponse)
def admin_grant_incentive(walker_id: str, payload: IncentiveGrantRequest, admin: User = Depends(require_permission("admin.access")), db: Session = Depends(get_db)):
    return svc.grant_manual(_admin_tenant_id(admin, db), walker_id, payload.model_dump(), db)


@admin_router.post("/incentives/{incentive_id}/revoke", response_model=GrantedIncentiveResponse)
@api_admin_router.post("/incentives/{incentive_id}/revoke", response_model=GrantedIncentiveResponse)
def admin_revoke_incentive(incentive_id: str, payload: IncentiveRevokeRequest | None = None, admin: User = Depends(require_permission("admin.access")), db: Session = Depends(get_db)):
    notes = payload.admin_notes if payload else None
    return svc.revoke_granted(_admin_tenant_id(admin, db), incentive_id, db, admin_notes=notes)


@admin_router.get("/incentives/metrics", response_model=IncentiveMetricsResponse)
@api_admin_router.get("/incentives/metrics", response_model=IncentiveMetricsResponse)
def admin_incentive_metrics(
    admin: User = Depends(require_permission("admin.access")),
    db: Session = Depends(get_db),
):
    """Métricas de incentivos: regras, concessões, breakdown por tipo e série semanal."""
    scope = get_admin_tenant_scope(admin, db)
    data = get_incentive_metrics(db, scope)
    return IncentiveMetricsResponse(**data)


@admin_router.get("/incentives", response_model=GrantedIncentiveListResponse)
@api_admin_router.get("/incentives", response_model=GrantedIncentiveListResponse)
def admin_list_incentives(
    walker_id: str | None = Query(None),
    status: str | None = Query(None),
    admin: User = Depends(require_permission("admin.access")),
    db: Session = Depends(get_db),
):
    items = svc.list_granted(_admin_tenant_id(admin, db), db, walker_id=walker_id, status=status)
    return {"items": items, "total": len(items)}
