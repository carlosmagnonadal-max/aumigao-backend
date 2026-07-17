"""Rotas dos planos recorrentes (Onda 1).

- Cliente-final (tutor): vê o catálogo (gated pela feature flag), assina e cancela.
- Admin do tenant: CRUD do catálogo (gated por permissão finance.*).
"""
import logging

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query, Request
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


_COVERAGE_WALK_TYPES = ("individual", "shared", "pet_tour")


@router.get("/coverage")
@api_router.get("/coverage")
def plan_coverage_precheck(
    request: Request,
    walk_type: str = Query(..., description="individual | shared | pet_tour"),
    duration: int | None = Query(None, description="30 | 45 | 60 — informativo"),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Pré-checagem de cobertura de plano (Projeto A / D1) para o app decidir se
    oferece "usar meu plano" antes de criar o passeio/cobrança.

    Sempre 200 (sem 404 de negócio). `duration` é informativa (a cobertura não
    depende da duração — todas 30/45/60 do individual/compartilhado são cobertas).
    Ver recurring_plan_service.plan_coverage para o shape e a precedência do reason.
    """
    if walk_type not in _COVERAGE_WALK_TYPES:
        raise HTTPException(status_code=400, detail="walk_type inválido.")
    tenant = _resolve_user_tenant(user, db, request)
    return svc.plan_coverage(db, tenant, user.id, walk_type)


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


@router.get("/my-cycle-walks")
@api_router.get("/my-cycle-walks")
def my_cycle_walks(request: Request, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """Histórico dos passeios do CICLO corrente do plano do tutor.

    Retorna os passeios cobertos pela assinatura atual (subscription_id) criados
    dentro do período vigente (current_period_start/end), com o essencial para a UI
    de acompanhamento do plano. Sem assinatura ativa → 200 com subscription:null e
    lista vazia (a tela mostra o estado "sem plano" sem tratar erro).
    """
    from app.models.pet import Pet
    from app.models.walk import Walk

    tenant = _resolve_user_tenant(user, db, request)
    subscription = svc.get_active_subscription(db, tenant.id, user.id)
    if subscription is None:
        return {
            "subscription": None,
            "walks": [],
            "period_start": None,
            "period_end": None,
            "walks_per_cycle": None,
            "credits_remaining": None,
        }

    period_start = subscription.current_period_start
    period_end = subscription.current_period_end

    query = db.query(Walk).filter(Walk.subscription_id == subscription.id)
    if period_start is not None:
        query = query.filter(Walk.created_at >= period_start)
    if period_end is not None:
        query = query.filter(Walk.created_at <= period_end)
    # scheduled_date é ISO ("YYYY-MM-DDThh:mm:ss") — ordenação lexicográfica = cronológica.
    walks = query.order_by(Walk.scheduled_date.desc(), Walk.created_at.desc()).all()

    pet_ids = {w.pet_id for w in walks if w.pet_id}
    pet_names = (
        {p.id: p.name for p in db.query(Pet).filter(Pet.id.in_(pet_ids)).all()}
        if pet_ids
        else {}
    )

    def _serialize(walk) -> dict:
        date_part, _, time_part = (walk.scheduled_date or "").partition("T")
        pet_name = pet_names.get(walk.pet_id)
        return {
            "id": walk.id,
            "scheduled_date": walk.scheduled_date,
            "walk_date": date_part or None,
            "walk_time": (time_part[:5] if time_part else None),
            "status": walk.status,
            "operational_status": walk.operational_status,
            "pet_id": walk.pet_id,
            "pet_name": pet_name,
            "pet_names": [pet_name] if pet_name else [],
            "credit_refunded": bool(getattr(walk, "credit_refunded", False)),
        }

    return {
        "subscription": {
            "id": subscription.id,
            "plan_id": subscription.plan_id,
            "plan_name": svc.plan_name_for(db, subscription),
            "status": subscription.status,
        },
        "walks": [_serialize(w) for w in walks],
        "period_start": period_start,
        "period_end": period_end,
        "walks_per_cycle": subscription.walks_per_cycle,
        "credits_remaining": subscription.credits_remaining,
    }


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


@admin_router.get("/economics")
@api_admin_router.get("/economics")
def admin_plan_economics(admin: User = Depends(require_permission("finance.read")), db: Session = Depends(get_db)):
    """Números pro painel de margem do admin (decisão 07/07): âncora avulsa,
    fatias (comissão/margem/take de rede), repasse do passeador (intocável),
    teto de desconto e utilização média medida dos créditos (quebra real).
    """
    from sqlalchemy import func

    from app.models.recurring_plan import TutorSubscription
    from app.services.plan_walk_economics import plan_pricing_floor

    tenant_id = _admin_tenant_id(admin, db)
    floor = plan_pricing_floor(db, tenant_id)

    # Utilização medida: créditos consumidos ÷ concedidos, sobre assinaturas do
    # tenant que já conceberam créditos. Sem dados → null (o painel mostra "—").
    totals = (
        db.query(
            func.coalesce(func.sum(TutorSubscription.walks_per_cycle), 0),
            func.coalesce(func.sum(TutorSubscription.credits_remaining), 0),
            func.count(TutorSubscription.id),
        )
        .filter(
            TutorSubscription.tenant_id == tenant_id,
            TutorSubscription.credits_granted.is_(True),
        )
        .first()
    )
    granted, remaining, cycles = (int(totals[0]), int(totals[1]), int(totals[2])) if totals else (0, 0, 0)
    utilization = round((granted - remaining) / granted * 100, 1) if granted > 0 else None

    return {
        **floor,
        "utilization_percent": utilization,
        "utilization_cycles_measured": cycles,
    }


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
    # Trava do piso (decisão 07/07): preço/passeio nunca abaixo do mínimo
    # sustentável do tenant — nenhum elo pode ficar negativo.
    from app.services.plan_walk_economics import enforce_plan_pricing_floor
    enforce_plan_pricing_floor(db, tenant_id, payload.price, payload.walks_per_cycle)
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
    # Trava do piso (decisão 07/07): valida o estado FINAL (payload + atuais).
    if "price" in values or "walks_per_cycle" in values:
        from app.services.plan_walk_economics import enforce_plan_pricing_floor
        enforce_plan_pricing_floor(
            db,
            tenant_id,
            float(values.get("price", plan.price)),
            int(values.get("walks_per_cycle", plan.walks_per_cycle)),
        )
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
