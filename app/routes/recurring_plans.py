"""Rotas dos planos recorrentes (Onda 1).

- Cliente-final (tutor): vê o catálogo (gated pela feature flag), assina e cancela.
- Admin do tenant: CRUD do catálogo (gated por permissão finance.*).
"""
import logging

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session

logger = logging.getLogger("aumigao.routes.recurring_plans")

from app.core.database import get_db
from app.dependencies.auth import get_current_user
from app.dependencies.rbac import require_permission
from app.dependencies.tenant_scope import get_admin_tenant_scope
from app.models.recurring_plan import RECURRING_PLANS_FEATURE_KEY, RecurringPlan
from app.models.tenant import Tenant
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
from app.services.tenant_plan_service import enforce_plan_allows_product_feature

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
    response.payment_status = "ativa" if subscription.credits_granted else "aguardando_pagamento"
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
    from app.dependencies.legal_gate import enforce_legal_acceptance
    enforce_legal_acceptance(request, user, db)
    tenant = _resolve_user_tenant(user, db, request)
    subscription = await svc.subscribe_async(db, tenant, user.id, plan_id, tutor_user=user)
    return _subscription_response(db, subscription)


@router.post("/cancel", response_model=TutorSubscriptionResponse)
@api_router.post("/cancel", response_model=TutorSubscriptionResponse)
async def cancel_my_subscription(request: Request, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    tenant = _resolve_user_tenant(user, db, request)
    subscription = await svc.cancel_subscription_async(db, tenant.id, user.id)
    return _subscription_response(db, subscription)


@router.get("/subscription/payment")
@api_router.get("/subscription/payment")
async def get_subscription_payment(
    request: Request,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Retorna a cobrança PIX pendente mais recente da assinatura ativa do tutor.

    Busca a cobrança diretamente no Asaas via
    ``GET /payments?subscription={asaas_subscription_id}&status=PENDING``.

    Respostas:
    - 200: cobrança encontrada (payment_id, value, due_date, status,
      pix_qr_code, pix_payload, invoice_url). pix_* podem ser null quando
      o Asaas ainda não gerou o QR Code.
    - 404 sem assinatura ativa.
    - 404 sem cobrança pendente (assinatura em dia).
    - 502 em falha do Asaas.

    Segurança de tenant: usa db.info["rls_tenant"] (tenant da request) e
    nunca user.tenant_id (tenant de nascimento) — evita cross-tenant leak.
    """
    # Tenant da request (não o tenant de nascimento do usuário).
    rls_tenant = db.info.get("rls_tenant")
    if rls_tenant and rls_tenant not in ("*", ""):
        tenant_id = rls_tenant
    else:
        tenant = _resolve_user_tenant(user, db, request)
        tenant_id = tenant.id

    subscription = svc.get_active_subscription(db, tenant_id, user.id)
    if subscription is None:
        raise HTTPException(status_code=404, detail="Nenhuma assinatura ativa encontrada.")

    asaas_sub_id = subscription.asaas_subscription_id
    if not asaas_sub_id:
        raise HTTPException(
            status_code=404,
            detail="Assinatura sem vínculo com o gateway de pagamento; aguardando sincronização.",
        )

    # Busca a cobrança pendente no Asaas.
    try:
        from app.routes.payments import _get_asaas_config, asaas_headers as _asaas_headers
    except Exception as exc:
        logger.error("get_subscription_payment: falha ao importar client Asaas: %s", exc)
        raise HTTPException(
            status_code=502,
            detail="Serviço de pagamento temporariamente indisponível. Tente novamente em instantes.",
        )

    try:
        cfg = _get_asaas_config()
        hdrs = _asaas_headers(cfg["api_key"], mode="live" if cfg["is_live"] else "sandbox")
    except HTTPException:
        raise
    except Exception as exc:
        logger.error("get_subscription_payment: falha ao obter configuração Asaas: %s", exc)
        raise HTTPException(
            status_code=502,
            detail="Serviço de pagamento temporariamente indisponível. Tente novamente em instantes.",
        )

    try:
        async with httpx.AsyncClient(
            base_url=cfg["base_url"],
            headers=hdrs,
            timeout=15,
        ) as client:
            resp = await client.get(
                "/payments",
                params={"subscription": asaas_sub_id, "status": "PENDING"},
            )
            if resp.status_code >= 400:
                logger.error(
                    "get_subscription_payment: Asaas retornou status_http=%s asaas_sub_id=%s",
                    resp.status_code, asaas_sub_id,
                )
                raise HTTPException(
                    status_code=502,
                    detail="Erro ao consultar cobrança no gateway de pagamento. Tente novamente em instantes.",
                )

            data = resp.json()
            payments_list = data.get("data") or []
            if not payments_list:
                raise HTTPException(
                    status_code=404,
                    detail="Nenhuma cobrança pendente encontrada. A assinatura pode já estar em dia.",
                )

            # Cobrança PENDING mais recente (primeira da lista — Asaas retorna desc por dueDate).
            payment_data = payments_list[0]
            payment_id = payment_data.get("id")
            value = payment_data.get("value")
            due_date = payment_data.get("dueDate")
            status = payment_data.get("status")
            invoice_url = payment_data.get("invoiceUrl") or payment_data.get("bankSlipUrl")

            # Busca PIX QR Code — pode ainda não estar disponível (Asaas pode demorar alguns segundos).
            pix_qr_code = None
            pix_payload = None
            if payment_id:
                try:
                    pix_resp = await client.get(f"/payments/{payment_id}/pixQrCode")
                    if pix_resp.status_code == 200:
                        pix_data = pix_resp.json()
                        pix_qr_code = pix_data.get("encodedImage")
                        pix_payload = pix_data.get("payload")
                except Exception as exc:
                    logger.warning(
                        "get_subscription_payment: falha ao buscar pixQrCode payment_id=%s: %s",
                        payment_id, exc,
                    )

    except HTTPException:
        raise
    except Exception as exc:
        logger.exception(
            "get_subscription_payment: falha de rede ao consultar Asaas asaas_sub_id=%s: %s",
            asaas_sub_id, exc,
        )
        raise HTTPException(
            status_code=502,
            detail="Serviço de pagamento temporariamente indisponível. Tente novamente em instantes.",
        )

    return {
        "payment_id": payment_id,
        "value": value,
        "due_date": due_date,
        "status": status,
        "pix_qr_code": pix_qr_code,
        "pix_payload": pix_payload,
        "invoice_url": invoice_url,
    }


# --------------------------------------------------------------------------- #
# Admin do tenant (catálogo)
# --------------------------------------------------------------------------- #
def _admin_tenant_id(admin: User, db: Session) -> str:
    scope = get_admin_tenant_scope(admin, db)
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
    tenant = db.get(Tenant, tenant_id)
    if tenant is not None:
        enforce_plan_allows_product_feature(tenant, RECURRING_PLANS_FEATURE_KEY, "Planos recorrentes")
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
