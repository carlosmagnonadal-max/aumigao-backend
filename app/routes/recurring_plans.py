"""Rotas dos planos recorrentes (Onda 1).

- Cliente-final (tutor): vê o catálogo (gated pela feature flag), assina e cancela.
- Admin do tenant: CRUD do catálogo (gated por permissão finance.*).
"""
from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.dependencies.auth import get_current_user
from app.dependencies.rbac import require_permission
from app.dependencies.tenant_scope import get_admin_tenant_scope
from app.models.recurring_plan import RecurringPlan
from app.models.user import User
from app.schemas.recurring_plan import (
    RecurringPlanCreate,
    RecurringPlanResponse,
    RecurringPlanUpdate,
    RecurringPlansView,
    TutorSubscriptionResponse,
)
from app.services import recurring_plan_service as svc
from app.services.audit_service import record_audit_log
from app.services.tenant_context import resolve_current_tenant, resolve_current_tenant_id

# Cliente-final.
router = APIRouter(prefix="/recurring-plans", tags=["recurring-plans"])
api_router = APIRouter(prefix="/api/recurring-plans", tags=["recurring-plans"])

# Admin do tenant.
admin_router = APIRouter(
    prefix="/admin/recurring-plans",
    tags=["recurring-plans-admin"],
    dependencies=[Depends(require_permission("admin.access"))],
)
api_admin_router = APIRouter(
    prefix="/api/admin/recurring-plans",
    tags=["recurring-plans-admin"],
    dependencies=[Depends(require_permission("admin.access"))],
)


def _subscription_response(db: Session, subscription) -> TutorSubscriptionResponse:
    response = TutorSubscriptionResponse.model_validate(subscription)
    response.plan_name = svc.plan_name_for(db, subscription)
    return response


def _resolve_user_tenant(user: User, db: Session, request: Request):
    tenant = resolve_current_tenant(db, request)
    if user.tenant_id and user.tenant_id != tenant.id:
        # Usuário pertence a outro tenant: respeita o vínculo do usuário.
        from app.models.tenant import Tenant

        owned = db.get(Tenant, user.tenant_id)
        if owned:
            return owned
    return tenant


# --------------------------------------------------------------------------- #
# Cliente-final (tutor)
# --------------------------------------------------------------------------- #
@router.get("", response_model=RecurringPlansView)
@api_router.get("", response_model=RecurringPlansView)
def list_recurring_plans(request: Request, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    tenant = _resolve_user_tenant(user, db, request)
    if not svc.recurring_plans_enabled(tenant, db):
        return RecurringPlansView(available=False, plans=[], subscription=None)

    plans = svc.list_plans(db, tenant.id, only_active=True)
    subscription = svc.get_active_subscription(db, tenant.id, user.id)
    return RecurringPlansView(
        available=True,
        plans=[RecurringPlanResponse.model_validate(plan) for plan in plans],
        subscription=_subscription_response(db, subscription) if subscription else None,
    )


@router.post("/{plan_id}/subscribe", response_model=TutorSubscriptionResponse)
@api_router.post("/{plan_id}/subscribe", response_model=TutorSubscriptionResponse)
async def subscribe_to_plan(
    plan_id: str,
    request: Request,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    tenant = _resolve_user_tenant(user, db, request)
    subscription = await svc.subscribe_async(db, tenant, user.id, plan_id, tutor_user=user)
    return _subscription_response(db, subscription)


@router.post("/cancel", response_model=TutorSubscriptionResponse)
@api_router.post("/cancel", response_model=TutorSubscriptionResponse)
async def cancel_my_subscription(request: Request, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    tenant = _resolve_user_tenant(user, db, request)
    subscription = await svc.cancel_subscription_async(db, tenant.id, user.id)
    return _subscription_response(db, subscription)


# --------------------------------------------------------------------------- #
# Admin do tenant (catálogo)
# --------------------------------------------------------------------------- #
def _admin_tenant_id(admin: User, db: Session) -> str:
    scope = get_admin_tenant_scope(admin)
    return scope.tenant_id or resolve_current_tenant_id(db)


@admin_router.get("", response_model=list[RecurringPlanResponse])
@api_admin_router.get("", response_model=list[RecurringPlanResponse])
def admin_list_plans(admin: User = Depends(require_permission("finance.read")), db: Session = Depends(get_db)):
    tenant_id = _admin_tenant_id(admin, db)
    return svc.list_plans(db, tenant_id, only_active=False)


@admin_router.post("", response_model=RecurringPlanResponse)
@api_admin_router.post("", response_model=RecurringPlanResponse)
def admin_create_plan(
    payload: RecurringPlanCreate,
    admin: User = Depends(require_permission("finance.manage")),
    db: Session = Depends(get_db),
):
    tenant_id = _admin_tenant_id(admin, db)
    plan = RecurringPlan(tenant_id=tenant_id, **payload.model_dump())
    db.add(plan)
    record_audit_log(
        db, action="recurring_plan.created", entity_type="recurring_plan", entity_id=plan.id,
        actor=admin, after=payload.model_dump(), tenant_id=tenant_id,
    )
    db.commit()
    db.refresh(plan)
    return plan


@admin_router.patch("/{plan_id}", response_model=RecurringPlanResponse)
@api_admin_router.patch("/{plan_id}", response_model=RecurringPlanResponse)
def admin_update_plan(
    plan_id: str,
    payload: RecurringPlanUpdate,
    admin: User = Depends(require_permission("finance.manage")),
    db: Session = Depends(get_db),
):
    tenant_id = _admin_tenant_id(admin, db)
    plan = svc.get_plan_or_404(db, tenant_id, plan_id)
    values = payload.model_dump(exclude_unset=True)
    for field, value in values.items():
        setattr(plan, field, value)
    db.add(plan)
    record_audit_log(
        db, action="recurring_plan.updated", entity_type="recurring_plan", entity_id=plan.id,
        actor=admin, after=values, tenant_id=tenant_id,
    )
    db.commit()
    db.refresh(plan)
    return plan
