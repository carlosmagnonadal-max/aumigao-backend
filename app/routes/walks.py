import logging
import os
import traceback
from typing import Any
from uuid import uuid4
from datetime import date, datetime, timedelta
import json
import httpx
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy import func
from sqlalchemy.orm import Session, aliased
from app.core.database import get_db
from app.dependencies.auth import get_current_user
from app.dependencies.tenant_scope import apply_tenant_filter, ensure_tenant_access, get_admin_tenant_scope
from app.models.payment import Payment
from app.models.walk import Walk, WalkMatchingAttempt
from app.models.walk_completion_review import WalkCompletionReview
from app.models.walk_review import WalkReview
from app.models.walker_review import WalkerReview
from app.models.walk_tip import WalkTip
from app.models.user import User
from app.models.pet import Pet
from app.models.tenant import Tenant
from app.models.pet_tour import PET_TOUR_MODALITY
from app.services.pet_tour_service import validate_booking as validate_pet_tour_booking
from app.schemas.walk import WalkCreate, WalkResponse, WalkUpdateStatus
from app.schemas.walk_review import ALLOWED_WALK_REVIEW_TAGS, WalkReviewCreate
from app.schemas.walk_tip import WalkTipCheckoutCreate
from app.schemas.complaint import ComplaintCreate, ComplaintEvidenceCreate
from app.services.complaint_service import create_complaint
from app.services.tenant_plan_service import tenant_feature_enabled
from app.services.operational_matching_service import (
    LEGACY_STATUS_TO_OPERATIONAL,
    RIDE_SCHEDULED,
    log_event,
    process_expired_attempts,
    serialize_operational_walk,
    start_matching,
    update_operational_status,
    _batch_live_tracking,
)
from app.services.operational_reliability_service import detect_reliability_events, record_late_cancellation_if_applicable
from app.services.tenant_seed_service import default_tenant_id
from app.models.tenant_walker_access import TenantWalkerAccess
from app.core.feature_flags import multi_tenant_tutor_enabled
from app.services.tutor_network_service import is_tutor_eligible_for_tenant
from app.constants import PAID_PAYMENT_STATUSES as _PAID_PAYMENT_STATUSES
from app.constants import WALK_COMPLETED_STATUSES as COMPLETED_WALK_STATUSES
from app.constants import WALK_COMPLETED_STATUSES as DIRECT_COMPLETION_STATUSES
from app.services.recurring_plan_service import consume_credit_if_available, refund_credit_for_walk

# ── CR / gamificação (Fase 4) ────────────────────────────────────────────────
import app.services.walker_cr_service as _cr_svc
from app.services.walker_cr_rules import CR_EARN

router = APIRouter(prefix="/walks", tags=["walks"])
logger = logging.getLogger(__name__)
REVIEWABLE_COMPLETION_STATUSES = {"ride_completed"}
TIP_STATUSES = {"pending", "paid", "failed", "cancelled"}
TIP_PROVIDER = "internal_mock"


class WalkTipCreate(BaseModel):
    amount: float = Field(gt=0, le=500)
    note: str | None = None


class WalkReconfirmationDecision(BaseModel):
    action: str | None = None
    decision: str | None = None


FORBIDDEN_RESCHEDULE_FIELDS = {
    "price",
    "duration_minutes",
    "pet_id",
    "walker_id",
    "assigned_walker_id",
    "walker_selection_mode",
}


def _split_scheduled_date(value: str) -> tuple[str | None, str | None]:
    if not value:
        return None, None
    date_part, _, time_part = value.partition("T")
    return date_part or None, time_part[:5] or None


def _walk_create_log_payload(payload: WalkCreate, user: User) -> dict:
    return {
        "user_id": user.id,
        "tenant_id": user.tenant_id,
        "pet_id": payload.pet_id,
        "walker_id": payload.walker_id,
        "walker_selection_mode": payload.walker_selection_mode,
        "scheduled_date": payload.scheduled_date,
        "duration_minutes": payload.duration_minutes,
        "pickup_method": payload.pickup_method,
    }


def _parse_scheduled_at(value: str) -> datetime:
    normalized = value.strip()
    if not normalized:
        raise HTTPException(status_code=400, detail="Informe o novo horario do passeio.")
    try:
        return datetime.fromisoformat(normalized.replace("Z", "+00:00")).replace(tzinfo=None)
    except ValueError:
        raise HTTPException(status_code=400, detail="Horario do passeio invalido.")


def _serialize_walk(walk: Walk, db: Session) -> dict:
    return serialize_operational_walk(walk, db, include_private=True)


def _serialize_walk_list_item(
    walk: Walk,
    pet: Pet | None,
    tutor: User | None,
    walker: User | None,
    user: User,
) -> dict:
    walker_id = walk.walker_id or walk.assigned_walker_id
    walk_date, _, walk_time = (walk.scheduled_date or "").partition("T")
    can_see_full = user.role in {"admin", "super_admin"} or walk.tutor_id == user.id
    pet_photo_url = (pet.photo_url if pet else "") or ""
    if pet_photo_url.startswith(("file://", "content://", "blob:")):
        pet_photo_url = ""

    return {
        "id": walk.id,
        "tutor_id": walk.tutor_id,
        "walker_id": walker_id,
        "assigned_walker_id": walk.assigned_walker_id,
        "assignedWalkerId": walk.assigned_walker_id,
        "pet_id": walk.pet_id,
        "pet_name": pet.name if pet else None,
        "pet_photo_url": pet_photo_url,
        "tutor_name": (tutor.full_name if tutor else None) or (tutor.email if tutor else None),
        "client_name": (tutor.full_name if tutor else None) or (tutor.email if tutor else None),
        "walker_name": (walker.full_name if walker else None) or (walker.email if walker else None),
        "scheduled_date": walk.scheduled_date,
        "walk_date": walk_date or None,
        "walk_time": walk_time[:5] if walk_time else None,
        "duration_minutes": walk.duration_minutes,
        "price": walk.price,
        "status": walk.status,
        "operational_status": walk.operational_status,
        "operationalStatus": walk.operational_status,
        "walker_selection_mode": walk.walker_selection_mode or "auto",
        "walkerSelectionMode": walk.walker_selection_mode or "auto",
        "pickup_method": walk.pickup_method,
        "address_snapshot": walk.address_snapshot if can_see_full else "",
        "notes": walk.notes if can_see_full else "",
        "pickup_privacy_level": "full" if can_see_full else "coarse",
        "current_attempt": walk.current_attempt,
        "current_matching_attempt": walk.current_attempt,
        "max_attempts": walk.max_attempts,
        "max_matching_attempts": walk.max_attempts,
        "confirmation_expires_at": walk.confirmation_expires_at,
        "walker_confirmation_expires_at": walk.confirmation_expires_at,
        "matching_started_at": walk.matching_started_at,
        "matching_finished_at": walk.matching_finished_at,
        "no_walker_reason": walk.no_walker_reason,
        "matching_attempts": [],
        "operational_logs": [],
        "created_at": walk.created_at,
    }


def _walk_list_query(db: Session):
    tutor_user = aliased(User)
    walker_user = aliased(User)
    walker_join_id = func.coalesce(Walk.walker_id, Walk.assigned_walker_id)
    return (
        db.query(Walk, Pet, tutor_user, walker_user)
        .outerjoin(Pet, Pet.id == Walk.pet_id)
        .outerjoin(tutor_user, tutor_user.id == Walk.tutor_id)
        .outerjoin(walker_user, walker_user.id == walker_join_id)
    )


def _serialize_walk_list(rows: list[tuple[Walk, Pet | None, User | None, User | None]], user: User) -> list[dict]:
    return [
        _serialize_walk_list_item(walk, pet, tutor, walker, user)
        for walk, pet, tutor, walker in rows
    ]


def _serialize_walk_review(review: WalkReview) -> dict:
    try:
        tags = json.loads(review.tags_json or "[]")
    except (TypeError, ValueError):
        tags = []
    return {
        "id": review.id,
        "walk_id": review.walk_id,
        "tutor_id": review.tutor_id,
        "walker_id": review.walker_id,
        "rating": review.rating,
        "comment": review.comment,
        "tags": tags if isinstance(tags, list) else [],
        "created_at": review.created_at,
    }


def _serialize_walk_tip(tip: WalkTip) -> dict:
    return {
        "id": tip.id,
        "tip_id": tip.id,
        "walk_id": tip.walk_id,
        "tutor_id": tip.tutor_id,
        "walker_id": tip.walker_id,
        "amount": tip.amount,
        "status": tip.status,
        "provider": tip.provider,
        "checkout_url": tip.checkout_url,
        "invoice_url": getattr(tip, "invoice_url", None),
        "provider_payment_id": getattr(tip, "provider_payment_id", None),
        "created_at": tip.created_at,
        "paid_at": tip.paid_at,
    }


def _approved_completion_review_exists(walk_id: str, db: Session) -> bool:
    return (
        db.query(WalkCompletionReview)
        .filter(WalkCompletionReview.walk_id == walk_id, WalkCompletionReview.status == "approved")
        .first()
        is not None
    )


def _walk_observations_enabled(walk: Walk, db: Session) -> bool:
    """Review P2 #3: booleano ADITIVO no GET do walk — o app do passeador esconde
    o formulário de observação quando False. Sem tenant no walk → False."""
    if not walk.tenant_id:
        return False
    tenant = db.get(Tenant, walk.tenant_id)
    if not tenant:
        return False
    from app.services import pet_profile_service as _pet_profile_svc
    return _pet_profile_svc.observations_active(tenant, db)


def _get_walk_for_user(walk_id: str, user: User, db: Session) -> Walk:
    walk = db.get(Walk, walk_id)
    if not walk:
        raise HTTPException(status_code=404, detail="Passeio nao encontrado")
    if user.role in {"admin", "super_admin"}:
        scope = get_admin_tenant_scope(user, db)
        ensure_tenant_access(walk.tenant_id, scope)
        return walk
    if user.role not in {"admin", "super_admin"} and walk.tutor_id != user.id and walk.walker_id != user.id and walk.assigned_walker_id != user.id:
        raise HTTPException(status_code=403, detail="Sem permissao")
    return walk


def _refresh_reliability_events(walks: list[Walk], db: Session) -> None:
    created = False
    for walk in walks:
        created = bool(detect_reliability_events(walk, db)) or created
    if created:
        db.commit()

def _walker_allowed_tenant_ids(user_id: str, db: Session) -> list[str]:
    """Retorna os tenant_ids que o passeador tem acesso via TenantWalkerAccess."""
    rows = (
        db.query(TenantWalkerAccess.tenant_id)
        .filter(
            TenantWalkerAccess.walker_user_id == user_id,
            TenantWalkerAccess.status == "active",
        )
        .distinct()
        .all()
    )
    return [row[0] for row in rows]


@router.get("", response_model=list[WalkResponse])
def list_walks(
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    limit: int = Query(50, ge=1, le=200),
    full: bool = Query(False),
):
    if full:
        process_expired_attempts(db)
        query = db.query(Walk)
        if user.role == "walker":
            allowed_tenant_ids = _walker_allowed_tenant_ids(user.id, db)
            # Walker ve seus proprios passeios + passeios sem walker atribuido
            # restritos aos tenants que ele atende.
            # M-04: removido o allow tenant_id IS NULL (vazamento cross-tenant).
            # Auditoria 2026-06-17 confirmou 0 walks orfaos em prod.
            # R7: defesa explícita — passeios AGUARDANDO PAGAMENTO nunca aparecem para
            # o walker no pool disponível (o gate já muda o status, isto evita regressão).
            query = query.filter(
                (Walk.walker_id == user.id)
                | (
                    Walk.walker_id.is_(None)
                    & Walk.tenant_id.in_(allowed_tenant_ids)
                    & (Walk.operational_status != "awaiting_payment")
                )
            )
        elif user.role in {"admin", "super_admin"}:
            query = apply_tenant_filter(query, Walk, get_admin_tenant_scope(user, db))
        elif user.role not in {"admin", "super_admin"}:
            query = query.filter(Walk.tutor_id == user.id)
        walks = query.order_by(Walk.created_at.desc()).limit(limit).all()
        _refresh_reliability_events(walks, db)
        # Batch: 1 query para live-tracking de toda a listagem (elimina N+1)
        _live_ids = _batch_live_tracking([w.id for w in walks], db)
        return [serialize_operational_walk(walk, db, user=user, live_tracking_ids=_live_ids) for walk in walks]

    query = _walk_list_query(db)
    if user.role == "walker":
        allowed_tenant_ids = _walker_allowed_tenant_ids(user.id, db)
        # Walker ve seus proprios passeios + passeios sem walker atribuido
        # restritos aos tenants que ele atende.
        # M-04: removido o allow tenant_id IS NULL (vazamento cross-tenant).
        # Auditoria 2026-06-17 confirmou 0 walks orfaos em prod.
        # R7: defesa explícita — passeios AGUARDANDO PAGAMENTO nunca aparecem para
        # o walker no pool disponível (o gate já muda o status, isto evita regressão).
        query = query.filter(
            (Walk.walker_id == user.id)
            | (
                Walk.walker_id.is_(None)
                & Walk.tenant_id.in_(allowed_tenant_ids)
                & (Walk.operational_status != "awaiting_payment")
            )
        )
    elif user.role in {"admin", "super_admin"}:
        query = apply_tenant_filter(query, Walk, get_admin_tenant_scope(user, db))
    elif user.role not in {"admin", "super_admin"}:
        query = query.filter(Walk.tutor_id == user.id)
    rows = query.order_by(Walk.created_at.desc()).limit(limit).all()
    return _serialize_walk_list(rows, user)

def _require_payment_before_matching() -> bool:
    """R7: gate de produto (fail-closed). Quando ligado, o walk só entra no fluxo
    operacional (matching) depois que o pagamento liquida — antes disso fica
    'awaiting_payment'. DEFAULT LIGADO: passeio só fica visível/aceitável pro
    passeador com pagamento LIQUIDADO (regra do dono). Exceções legítimas
    (assinatura, cupom 100%, crédito de rede) têm fluxos próprios que promovem o
    walk. Pode ser DESLIGADO via env REQUIRE_PAYMENT_BEFORE_MATCHING=false."""
    return os.getenv("REQUIRE_PAYMENT_BEFORE_MATCHING", "true").strip().lower() in {"1", "true", "yes", "on"}


@router.post("", response_model=WalkResponse)
def create_walk(payload: WalkCreate, request: Request, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    logger.warning(
        "create_walk.start user_id=%s role=%s payload=%s",
        user.id,
        user.role,
        _walk_create_log_payload(payload, user),
    )

    try:
        data = payload.model_dump()

        selected_walker_id = data.pop("walker_id", None)
        requested_selection_mode = data.pop("walker_selection_mode", None)
        pet = db.get(Pet, data.get("pet_id")) if data.get("pet_id") else None
        _default_tenant = default_tenant_id(db)
        # A4 (Modelo B): o passeio pertence ao TENANT ATIVO do tutor, resolvido pelo
        # header X-Tenant-Slug (request.state.tenant_id, setado pelo TenantResolverMiddleware).
        # Isso também alinha o walk.tenant_id ao GUC RLS da sessão (mesmo header), evitando
        # violação de WITH CHECK. Sem header (app base/single-tenant) cai no default → zero-regressão.
        runtime_tenant_id = getattr(request.state, "tenant_id", None)
        tenant_id = runtime_tenant_id or user.tenant_id or (pet.tenant_id if pet else None) or _default_tenant
        user.tenant_id = user.tenant_id or tenant_id
        if pet and not pet.tenant_id:
            pet.tenant_id = tenant_id

        # Gate de vínculo do tutor (Modelo B). OFF ou tenant default → bypass (= comportamento atual).
        if multi_tenant_tutor_enabled() and tenant_id != _default_tenant:
            if not is_tutor_eligible_for_tenant(db, tenant_id, user.id):
                raise HTTPException(status_code=403, detail="tutor_not_linked_to_tenant")

        # Plano free: cap mensal de passeios (default 40, env FREE_PLAN_WALK_CAP).
        # Conta passeios CRIADOS no mês corrente (BRT), excluindo cancelados.
        # No-op para pro/enterprise e durante o reverse trial (plano efetivo = pro).
        if tenant_id:
            from app.services.tenant_free_plan_service import enforce_free_plan_walk_cap

            _tenant_cap_check = db.get(Tenant, tenant_id)
            if _tenant_cap_check is not None:
                enforce_free_plan_walk_cap(db, _tenant_cap_check)

        # Gate home_pickup: se a flag estiver OFF, bloqueia pickup_method que indique busca em casa.
        _pickup_method = data.get("pickup_method", "")
        _home_pickup_values = {"Buscar em casa", "home_pickup", "buscar_em_casa", "buscar em casa"}
        if str(_pickup_method or "").strip().lower() in {v.lower() for v in _home_pickup_values}:
            _tenant_obj = db.get(Tenant, tenant_id)
            if _tenant_obj and not tenant_feature_enabled(_tenant_obj, db, "home_pickup"):
                raise HTTPException(status_code=400, detail="Modalidade buscar em casa não está disponível.")

        # Pet Tour: valida flag/destino/duração e aplica o preço do tenant (server-authoritative).
        if data.get("modality") == PET_TOUR_MODALITY:
            tenant = db.get(Tenant, tenant_id)
            config = validate_pet_tour_booking(
                db,
                tenant,
                destination=data.get("destination", ""),
                duration_minutes=data.get("duration_minutes", 0),
            )
            data["price"] = config.base_price
        elif data.get("duration_minutes") in (30, 45, 60):
            # Passeio individual: preço é AUTORITATIVO do servidor, pela config do tenant
            # (white label). Ignora qualquer preço que o app envie — não dá pra burlar.
            from app.services import individual_walk_pricing_service as individual_pricing_svc

            _iwp = individual_pricing_svc.get_or_create_config(db, tenant_id)
            data["price"] = {30: _iwp.price_30, 45: _iwp.price_45, 60: _iwp.price_60}[data["duration_minutes"]]

        walker_selection_mode = (
            "only_selected"
            if requested_selection_mode == "only_selected"
            else "auto"
        )

        logger.warning(
            "create_walk.resolved_selection selected_walker_id=%s selection_mode=%s",
            selected_walker_id,
            walker_selection_mode,
        )

        walk = Walk(
            id=str(uuid4()),
            tutor_id=user.id,
            tenant_id=tenant_id,
            walker_id=selected_walker_id,
            assigned_walker_id=selected_walker_id,
            walker_selection_mode=walker_selection_mode,
            operational_status="pending_walker_confirmation",
            current_attempt=0,
            max_attempts=3,
            **data,
        )

        logger.warning(
            "create_walk.walk_initialized walk_id=%s pet_id=%s",
            walk.id,
            walk.pet_id,
        )

        db.add(walk)

        # Projeto A: passeio coberto por crédito de assinatura ativa (sem cobrança avulsa).
        _covered_by_subscription = False
        if walk.tenant_id:
            _tenant = db.get(Tenant, walk.tenant_id)
            if _tenant is not None:
                _sub = consume_credit_if_available(db, _tenant, walk.tutor_id)
                if _sub is not None:
                    walk.subscription_id = _sub.id
                    _covered_by_subscription = True
                    # Item 4: reconhece receita contábil do crédito consumido — best-effort.
                    # NUNCA propaga exceção — ledger jamais pode quebrar criação do passeio.
                    try:
                        from app.services.credit_ledger_service import record_revenue_recognized_safe
                        record_revenue_recognized_safe(db, _sub, walk.id)
                    except Exception:
                        logger.exception(
                            "create_walk: falha best-effort ledger revenue subscription_id=%s walk_id=%s",
                            _sub.id, walk.id,
                        )

        logger.warning(
            "create_walk.matching_deferred walk_id=%s",
            walk.id,
        )

        # R7: com o gate ligado, o walk nasce aguardando pagamento e NÃO entra no
        # matching até o webhook de pagamento confirmado liberá-lo (payments.py).
        if _require_payment_before_matching() and not _covered_by_subscription:
            walk.operational_status = "awaiting_payment"
            walk.status = "aguardando_pagamento"
            walk.no_walker_reason = "Aguardando confirmação do pagamento."
        else:
            walk.operational_status = "pending_walker_confirmation"
            walk.status = "Agendado"
            walk.no_walker_reason = "Buscando o melhor passeador disponível."

        logger.warning(
            "create_walk.before_commit walk_id=%s operational_status=%s",
            walk.id,
            walk.operational_status,
        )

        db.commit()

        logger.warning(
            "create_walk.after_commit walk_id=%s",
            walk.id,
        )

        db.refresh(walk)

        logger.warning(
            "create_walk.success_light_response walk_id=%s",
            walk.id,
        )

        pet = db.get(Pet, walk.pet_id) if walk.pet_id else None
        pet_photo_url = (pet.photo_url if pet else "") or ""
        if pet_photo_url.startswith(("file://", "content://", "blob:")):
            pet_photo_url = ""

        return {
            "id": walk.id,
            "tutor_id": walk.tutor_id,
            "walker_id": walk.walker_id,
            "assigned_walker_id": walk.assigned_walker_id,
            "pet_id": walk.pet_id,
            "pet_name": pet.name if pet else None,
            "pet_photo_url": pet_photo_url,
            "scheduled_date": walk.scheduled_date,
            "walk_date": _split_scheduled_date(walk.scheduled_date)[0],
            "walk_time": _split_scheduled_date(walk.scheduled_date)[1],
            "duration_minutes": walk.duration_minutes,
            "price": walk.price,
            "status": walk.status,
            "operational_status": walk.operational_status,
            "operationalStatus": walk.operational_status,
            "walker_selection_mode": walk.walker_selection_mode,
            "walkerSelectionMode": walk.walker_selection_mode,
            "assignedWalkerId": walk.assigned_walker_id,
            "current_attempt": walk.current_attempt,
            "current_matching_attempt": walk.current_attempt,
            "max_attempts": walk.max_attempts,
            "max_matching_attempts": walk.max_attempts,
            "confirmation_expires_at": walk.confirmation_expires_at,
            "matching_started_at": walk.matching_started_at,
            "matching_finished_at": walk.matching_finished_at,
            "no_walker_reason": walk.no_walker_reason,
            "pickup_method": walk.pickup_method,
            "address_snapshot": walk.address_snapshot,
            "notes": walk.notes,
            "created_at": walk.created_at,
    }

    except HTTPException:
        # Erros de negócio (400/403/409) não precisam de rollback de DB aqui — o
        # walk ainda não foi commitado. Re-raise direto sem logar como erro interno.
        raise
    except Exception as error:
        db.rollback()

        logger.exception(
            "create_walk.failed user_id=%s error=%s traceback=%s",
            user.id,
            error,
            traceback.format_exc(),
        )

        raise

@router.get("/{walk_id}", response_model=WalkResponse)
def get_walk(walk_id: str, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    process_expired_attempts(db)
    walk = _get_walk_for_user(walk_id, user, db)
    _refresh_reliability_events([walk], db)
    data = serialize_operational_walk(walk, db, user=user)
    # Campo ADITIVO (review P2 #3): o app do passeador usa este booleano para
    # esconder o formulário de observação quando a feature está dormente.
    data["walk_observations_enabled"] = _walk_observations_enabled(walk, db)
    return data

@router.put("/{walk_id}/status", response_model=WalkResponse)
def update_status(walk_id: str, payload: WalkUpdateStatus, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    walk = _get_walk_for_user(walk_id, user, db)
    if payload.status in DIRECT_COMPLETION_STATUSES:
        raise HTTPException(status_code=400, detail="Finalização deve ocorrer via revisão operacional.")
    update_operational_status(walk, payload.status, db, actor=user)
    record_late_cancellation_if_applicable(walk, db)
    db.commit()
    db.refresh(walk)
    return serialize_operational_walk(walk, db, user=user)


@router.post("/{walk_id}/review")
def create_walk_review(walk_id: str, payload: WalkReviewCreate, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    walk = _get_walk_for_user(walk_id, user, db)
    if walk.tutor_id != user.id:
        raise HTTPException(status_code=403, detail="Apenas o tutor dono do passeio pode avaliar.")
    # Gate reviews
    _review_tenant_id = walk.tenant_id or user.tenant_id
    if _review_tenant_id:
        _review_tenant = db.get(Tenant, _review_tenant_id)
        if _review_tenant and not tenant_feature_enabled(_review_tenant, db, "reviews"):
            raise HTTPException(status_code=403, detail="Avaliações não estão habilitadas para este tenant.")
    if walk.operational_status not in REVIEWABLE_COMPLETION_STATUSES:
        raise HTTPException(status_code=409, detail="Avaliação disponível apenas após finalização operacional aprovada.")
    if not walk.walker_id and not walk.assigned_walker_id:
        raise HTTPException(status_code=400, detail="Avaliação exige passeador atribuído ao passeio.")

    completion_review = (
        db.query(WalkCompletionReview)
        .filter(WalkCompletionReview.walk_id == walk.id, WalkCompletionReview.status == "approved")
        .order_by(WalkCompletionReview.reviewed_at.desc())
        .first()
    )
    if not completion_review:
        raise HTTPException(status_code=409, detail="Avaliação exige finalização aprovada pela revisão operacional.")

    existing_review = db.query(WalkReview).filter(WalkReview.walk_id == walk.id).first()
    if existing_review:
        raise HTTPException(status_code=409, detail="Este passeio já possui avaliação registrada.")

    tags = []
    for tag in payload.tags or []:
        normalized = str(tag).strip()
        if normalized and normalized in ALLOWED_WALK_REVIEW_TAGS and normalized not in tags:
            tags.append(normalized)

    review = WalkReview(
        id=str(uuid4()),
        walk_id=walk.id,
        tutor_id=user.id,
        walker_id=walk.walker_id or walk.assigned_walker_id,
        rating=payload.rating,
        comment=(payload.comment or "").strip() or None,
        tags_json=json.dumps(tags),
    )
    db.add(review)
    # Bridge: alimenta tambem walker_reviews, que e a fonte lida por reputation_service
    # (nota media, score, risco, flag) e pelas listagens publicas/admin. Sem isto a
    # avaliacao nao afetaria a reputacao do passeador (ver walk_reviews vs walker_reviews).
    if not db.query(WalkerReview).filter(WalkerReview.walk_id == walk.id).first():
        db.add(
            WalkerReview(
                id=str(uuid4()),
                tenant_id=getattr(walk, "tenant_id", None),
                walk_id=walk.id,
                tutor_id=user.id,
                walker_id=walk.walker_id or walk.assigned_walker_id,
                rating=payload.rating,
                comment=(payload.comment or "").strip() or None,
            )
        )

    # ── Gancho B: CR por avaliação 5 estrelas (idempotente via already_awarded) ─
    _review_walker_id = walk.walker_id or walk.assigned_walker_id
    if payload.rating == 5 and _review_walker_id:
        try:
            if not _cr_svc.already_awarded(db, _review_walker_id, "review_5star", review.id):
                _cr_svc.earn_cr(
                    db,
                    _review_walker_id,
                    CR_EARN["review_5star"],
                    "review_5star",
                    description=f"Avaliação 5 estrelas recebida no passeio {walk.id}.",
                    related_entity_type="walk_review",
                    related_entity_id=review.id,
                )
        except Exception as _cr_exc:
            logger.warning("Gancho CR review_5star falhou (review=%s): %s", review.id, _cr_exc)

    db.commit()
    db.refresh(review)
    db.refresh(walk)
    return {
        "ok": True,
        "review": _serialize_walk_review(review),
        "walk": serialize_operational_walk(walk, db, user=user),
    }


@router.post("/{walk_id}/tip-checkout")
async def create_walk_tip_checkout(walk_id: str, payload: WalkTipCheckoutCreate, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    walk = _get_walk_for_user(walk_id, user, db)
    if walk.tutor_id != user.id:
        raise HTTPException(status_code=403, detail="Apenas o tutor dono do passeio pode enviar gorjeta.")
    # Gate tips
    _tip_tenant_id = walk.tenant_id or user.tenant_id
    if _tip_tenant_id:
        _tip_tenant = db.get(Tenant, _tip_tenant_id)
        if _tip_tenant and not tenant_feature_enabled(_tip_tenant, db, "tips"):
            raise HTTPException(status_code=403, detail="Gorjetas não estão habilitadas para esta operação.")
    if walk.operational_status != "ride_completed":
        raise HTTPException(status_code=409, detail="Gorjeta disponível apenas após finalização operacional aprovada.")
    if not _approved_completion_review_exists(walk.id, db):
        raise HTTPException(status_code=409, detail="Gorjeta exige finalização aprovada pela revisão operacional.")

    walker_id = walk.walker_id or walk.assigned_walker_id
    if not walker_id:
        raise HTTPException(status_code=400, detail="Gorjeta exige passeador atribuído ao passeio.")

    recent_cutoff = datetime.utcnow() - timedelta(minutes=15)
    duplicate_paid = (
        db.query(WalkTip)
        .filter(
            WalkTip.walk_id == walk.id,
            WalkTip.tutor_id == user.id,
            WalkTip.walker_id == walker_id,
            WalkTip.amount == float(payload.amount),
            WalkTip.status == "paid",
            WalkTip.paid_at >= recent_cutoff,
        )
        .first()
    )
    if duplicate_paid:
        raise HTTPException(status_code=409, detail="Gorjeta idêntica já confirmada recentemente.")

    tip_id = str(uuid4())
    tip = WalkTip(
        id=tip_id,
        walk_id=walk.id,
        tutor_id=user.id,
        walker_id=walker_id,
        amount=float(payload.amount),
        status="pending",
        provider=TIP_PROVIDER,
    )

    # --- Pagamento real via Asaas quando o modo não é internal_mock ---
    # Importações locais para evitar ciclo de imports
    from app.routes.payments import (
        _get_asaas_config,
        asaas_headers,
        create_asaas_customer,
        normalize_method,
        raise_asaas_error,
    )
    from app.models.tutor_profile import TutorProfile

    try:
        cfg = _get_asaas_config()
    except Exception as _cfg_exc:
        logger.warning(
            "tip_asaas_config_unavailable walk_id=%s reason=%s — falling back to internal_mock",
            walk.id, type(_cfg_exc).__name__,
        )
        cfg = None

    checkout_url: str | None = None
    invoice_url: str | None = None
    provider_payment_id: str | None = None
    provider_name = TIP_PROVIDER

    if cfg is not None:
        is_live = cfg["is_live"]
        base_url = cfg["base_url"]
        api_key = cfg["api_key"]
        provider_name = "asaas_live" if is_live else "asaas_sandbox"

        _tp = db.query(TutorProfile).filter(TutorProfile.user_id == user.id).first()
        tutor_cpf_raw = (_tp.cpf or "").strip() if _tp else ""
        tutor_cpf = tutor_cpf_raw if len(tutor_cpf_raw) == 11 else None

        billing_type = normalize_method("pix", is_live=is_live)

        # Split para gorjeta: 100% para o walker em modo live
        split_payload: list[dict] | None = None
        if is_live:
            from app.models.walker_profile import WalkerProfile
            wp = db.query(WalkerProfile).filter(WalkerProfile.user_id == walker_id).first()
            if wp and wp.asaas_wallet_id:
                split_payload = [{"walletId": wp.asaas_wallet_id, "percentualValue": 100}]
                logger.info("tip split_applied=true walker_id=%s wallet_id=%s", walker_id, wp.asaas_wallet_id)
            else:
                logger.info("tip split_applied=false walker_id=%s (sem asaas_wallet_id)", walker_id)

        try:
            async with httpx.AsyncClient(
                base_url=base_url,
                headers=asaas_headers(api_key, mode="live" if is_live else "sandbox"),
                timeout=20,
            ) as client:
                customer_id = await create_asaas_customer(client, user, is_live=is_live, tutor_cpf=tutor_cpf)
                tip_payment_payload: dict = {
                    "customer": customer_id,
                    "billingType": billing_type,
                    "value": float(payload.amount),
                    "dueDate": str(date.today() + timedelta(days=1)),
                    "description": f"Gorjeta passeio {walk.id}",
                    "externalReference": f"tip:{tip_id}",
                }
                if split_payload:
                    tip_payment_payload["split"] = split_payload

                response = await client.post("/payments", json=tip_payment_payload)
                if response.status_code >= 400:
                    raise_asaas_error("tip.payments.create", response, tip_payment_payload)

                payment_data = response.json()
                provider_payment_id = payment_data.get("id")
                invoice_url = payment_data.get("invoiceUrl") or payment_data.get("bankSlipUrl")
                checkout_url = invoice_url
        except HTTPException:
            raise
        except Exception as exc:
            logger.warning("Asaas indisponivel para gorjeta, usando fallback. error=%s", exc)
            provider_name = TIP_PROVIDER
            checkout_url = f"aumigao://tip-checkout/{tip_id}?status=pending"
    else:
        checkout_url = f"aumigao://tip-checkout/{tip_id}?status=pending"

    tip.provider = provider_name
    tip.checkout_url = checkout_url
    tip.provider_payment_id = provider_payment_id
    tip.invoice_url = invoice_url

    db.add(tip)
    db.commit()
    db.refresh(tip)
    return {
        **_serialize_walk_tip(tip),
        "checkout_url": tip.checkout_url,
        "invoice_url": tip.invoice_url,
        "status": tip.status,
        "tip_id": tip.id,
    }


@router.get("/tips/{tip_id}/status")
def get_walk_tip_status(tip_id: str, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    tip = db.get(WalkTip, tip_id)
    if not tip:
        raise HTTPException(status_code=404, detail="Gorjeta não encontrada.")
    if tip.tutor_id != user.id and tip.walker_id != user.id and user.role not in {"admin", "super_admin"}:
        raise HTTPException(status_code=403, detail="Sem permissão para consultar esta gorjeta.")
    return {
        **_serialize_walk_tip(tip),
        "tip_id": tip.id,
        "payment_status": tip.status,
    }


@router.post("/{walk_id}/reconfirmation")
def respond_walk_reconfirmation(
    walk_id: str,
    payload: WalkReconfirmationDecision,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    walk = db.get(Walk, walk_id)
    if not walk:
        raise HTTPException(status_code=404, detail="Passeio nao encontrado")
    if str(walk.tutor_id) != str(user.id):
        raise HTTPException(status_code=403, detail="Apenas o tutor dono do passeio pode reconfirmar.")

    allowed_statuses = {
        "awaiting_tutor_reconfirmation",
        "no_walker_found",
        "matching_failed",
        "auto_rematching",
    }
    previous_status = walk.operational_status
    if previous_status not in allowed_statuses:
        raise HTTPException(status_code=409, detail="Passeio nao aguarda decisao do tutor.")

    action = (payload.action or payload.decision or "").strip()
    if action == "reschedule":
        action = "keep_waiting"

    log_metadata = {"action": action, "previous_status": previous_status}

    if action == "continue_search":
        walk.walker_selection_mode = "auto"
        walk.walker_id = None
        walk.assigned_walker_id = None
        walk.no_walker_reason = None
        walk.matching_finished_at = None
        walk.confirmation_expires_at = None
        log_event(db, walk.id, "tutor_reconfirmation_action", actor_type="tutor", actor_id=user.id, metadata=log_metadata)
        start_matching(walk, db, actor=user)
    elif action in {"keep_waiting", "accept_reschedule"}:
        log_event(db, walk.id, "tutor_reconfirmation_action", actor_type="tutor", actor_id=user.id, metadata=log_metadata)
    elif action == "cancel":
        raise HTTPException(status_code=400, detail="Use o fluxo de cancelamento existente para cancelar este passeio.")
    else:
        raise HTTPException(status_code=400, detail="Acao de reconfirmacao invalida.")

    db.commit()
    db.refresh(walk)
    response = serialize_operational_walk(walk, db, user=user)
    if action in {"keep_waiting", "accept_reschedule"}:
        response["reconfirmation_message"] = "Decisao registrada. Nenhuma remarcacao automatica foi criada neste fluxo."
    return response


# api-T2: schema permissivo da remarcacao restrita. Os campos de data/horario sao
# opcionais (espelham o payload.get anterior) e o Pydantic v2 ignora extras desconhecidos,
# entao nenhum payload legitimo e rejeitado. Os campos PROIBIDOS sao declarados de
# proposito (tipo Any p/ nao gerar 422 por tipo) so para detecta-los via model_fields_set
# e manter a mesma rejeicao explicita (400) que o payload.keys() fazia antes.
class RescheduleSelectedWalkerRequest(BaseModel):
    scheduled_date: str | None = None
    walk_date: str | None = None
    walk_time: str | None = None
    price: Any | None = None
    duration_minutes: Any | None = None
    pet_id: Any | None = None
    walker_id: Any | None = None
    assigned_walker_id: Any | None = None
    walker_selection_mode: Any | None = None


@router.post("/{walk_id}/reschedule-selected-walker")
def reschedule_selected_walker_walk(
    walk_id: str,
    payload: RescheduleSelectedWalkerRequest,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    forbidden = FORBIDDEN_RESCHEDULE_FIELDS.intersection(payload.model_fields_set)
    if forbidden:
        raise HTTPException(status_code=400, detail="Esta remarcacao permite alterar apenas data e horario.")

    scheduled_date = str(payload.scheduled_date or "").strip()
    scheduled_at = _parse_scheduled_at(scheduled_date)
    if scheduled_at <= datetime.utcnow():
        raise HTTPException(status_code=400, detail="Escolha um horario futuro para remarcar.")

    walk = db.get(Walk, walk_id)
    if not walk:
        raise HTTPException(status_code=404, detail="Passeio nao encontrado")
    if str(walk.tutor_id) != str(user.id):
        raise HTTPException(status_code=403, detail="Apenas o tutor dono do passeio pode remarcar.")
    if walk.operational_status != "awaiting_tutor_reconfirmation":
        raise HTTPException(status_code=409, detail="Passeio nao aguarda remarcacao operacional.")
    if (walk.walker_selection_mode or "auto") != "only_selected":
        raise HTTPException(status_code=409, detail="Remarcacao restrita disponivel apenas para passeador escolhido.")

    selected_walker_id = walk.assigned_walker_id or walk.walker_id
    if not selected_walker_id:
        raise HTTPException(status_code=400, detail="Passeio sem passeador escolhido para remarcacao.")

    pending_attempt = (
        db.query(WalkMatchingAttempt)
        .filter(WalkMatchingAttempt.walk_id == walk.id, WalkMatchingAttempt.status == "pending")
        .first()
    )
    if pending_attempt:
        return serialize_operational_walk(walk, db, user=user)

    previous_scheduled_date = walk.scheduled_date
    walk.scheduled_date = scheduled_date

    if hasattr(walk, "walk_date"):
        walk.walk_date = payload.walk_date

    if hasattr(walk, "walk_time"):
        walk.walk_time = payload.walk_time

    walk.walker_id = selected_walker_id
    walk.assigned_walker_id = selected_walker_id
    walk.walker_selection_mode = "only_selected"
    walk.operational_status = "pending_walker_confirmation"
    walk.status = "Agendado"
    walk.no_walker_reason = None
    walk.matching_finished_at = None
    walk.confirmation_expires_at = None

    log_event(
        db,
        walk.id,
        "selected_walker_reschedule_requested",
        actor_type="tutor",
        actor_id=user.id,
        metadata={
            "previous_scheduled_date": previous_scheduled_date,
            "scheduled_date": scheduled_date,
            "walk_date": payload.walk_date,
            "walk_time": payload.walk_time,
            "walker_id": selected_walker_id,
        },
    )

    start_matching(walk, db, actor=user)

    db.commit()
    db.refresh(walk)

    return serialize_operational_walk(walk, db, user=user)


class TutorDecisionRequest(BaseModel):
    """Contrato FIXO do menu de decisão do tutor (o app é feito em paralelo).

    action:
      - "reschedule": exige scheduled_date + walk_time futuros (respeitando o corte de
        45min a partir de agora). Mantém o pagamento vinculado.
      - "switch_walker": SÓ em modo exclusivo — abre para matching flexível.
      - "refund": estorna o pagamento confirmado (Asaas) e cancela o passeio.
    """
    action: str
    scheduled_date: str | None = None
    walk_time: str | None = None


def _tutor_decision_new_scheduled_date(scheduled_date: str | None, walk_time: str | None) -> str:
    """Monta o novo scheduled_date ISO a partir de data (+hora opcional) e valida que
    o INÍCIO respeita o corte de 45min a partir de agora."""
    date_part = str(scheduled_date or "").strip()
    if not date_part:
        raise HTTPException(status_code=422, detail="Informe a nova data do passeio.")
    time_part = str(walk_time or "").strip()
    # Se a data já vier com hora embutida, usa-a; senão combina com walk_time.
    if "T" in date_part:
        combined = date_part
    elif time_part:
        combined = f"{date_part}T{time_part}"
    else:
        combined = date_part
    start = _parse_scheduled_at(combined)
    cutoff_minutes = _walk_payment_cutoff_minutes_local()
    if start <= datetime.utcnow() + timedelta(minutes=cutoff_minutes):
        raise HTTPException(
            status_code=422,
            detail=f"Escolha um horário com pelo menos {cutoff_minutes} minutos de antecedência.",
        )
    return combined


def _walk_payment_cutoff_minutes_local() -> int:
    try:
        return int(os.getenv("WALK_PAYMENT_CUTOFF_MINUTES", "45"))
    except (TypeError, ValueError):
        return 45


def _confirmed_payment_for_walk(db: Session, walk_id: str) -> Payment | None:
    return (
        db.query(Payment)
        .filter(Payment.walk_id == walk_id, Payment.status.in_(_PAID_PAYMENT_STATUSES))
        .order_by(Payment.created_at.desc())
        .first()
    )


@router.post("/{walk_id}/tutor-decision")
async def tutor_decision(
    walk_id: str,
    payload: TutorDecisionRequest,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Menu de decisão do tutor para passeios em 'awaiting_tutor_reconfirmation'
    (item E). Casos: pagamento pós-corte, passeador exclusivo que não aceitou.

    Auth: tutor DONO do walk (404 se não for). Estado: 409 se não em
    awaiting_tutor_reconfirmation. Payload inválido: 422.
    """
    walk = db.get(Walk, walk_id)
    if not walk or str(walk.tutor_id) != str(user.id):
        # 404 (não 403) para não revelar existência de passeios de outros tutores.
        raise HTTPException(status_code=404, detail="Passeio nao encontrado.")

    if walk.operational_status != "awaiting_tutor_reconfirmation":
        raise HTTPException(status_code=409, detail="Passeio nao aguarda decisao do tutor.")

    action = (payload.action or "").strip()
    is_exclusive = (walk.walker_selection_mode or "auto") == "only_selected"
    confirmed_payment = _confirmed_payment_for_walk(db, walk.id)

    if action == "reschedule":
        new_scheduled = _tutor_decision_new_scheduled_date(payload.scheduled_date, payload.walk_time)
        previous = walk.scheduled_date
        walk.scheduled_date = new_scheduled
        walk.no_walker_reason = None
        walk.matching_finished_at = None
        walk.confirmation_expires_at = None
        if confirmed_payment:
            # Pagamento já vinculado/confirmado → volta ao fluxo de confirmação do passeador.
            # Exclusivo mantém o passeador solicitado (start_matching reusa assigned_walker_id).
            walk.operational_status = "pending_walker_confirmation"
            walk.status = "Agendado"
        else:
            # Sem pagamento confirmado → volta a aguardar pagamento (gate R7).
            walk.operational_status = "awaiting_payment"
            walk.status = "aguardando_pagamento"
        log_event(
            db, walk.id, "tutor_decision", actor_type="tutor", actor_id=user.id,
            metadata={
                "action": "reschedule",
                "previous_scheduled_date": previous,
                "scheduled_date": new_scheduled,
                "had_confirmed_payment": bool(confirmed_payment),
                "is_exclusive": is_exclusive,
            },
        )
        if confirmed_payment:
            start_matching(walk, db, actor=user)

    elif action == "switch_walker":
        if not is_exclusive:
            raise HTTPException(
                status_code=409,
                detail="Trocar de passeador só é possível em passeios de passeador exclusivo.",
            )
        # Limpa a exclusividade → matching flexível escolhe outro passeador.
        walk.walker_selection_mode = "auto"
        walk.walker_id = None
        walk.assigned_walker_id = None
        walk.no_walker_reason = None
        walk.matching_finished_at = None
        walk.confirmation_expires_at = None
        if confirmed_payment:
            walk.operational_status = "pending_walker_confirmation"
            walk.status = "Agendado"
        else:
            walk.operational_status = "awaiting_payment"
            walk.status = "aguardando_pagamento"
        log_event(
            db, walk.id, "tutor_decision", actor_type="tutor", actor_id=user.id,
            metadata={"action": "switch_walker", "had_confirmed_payment": bool(confirmed_payment)},
        )
        if confirmed_payment:
            start_matching(walk, db, actor=user)

    elif action == "refund":
        if not confirmed_payment:
            raise HTTPException(
                status_code=409,
                detail="Não há pagamento confirmado para estornar neste passeio.",
            )
        from app.routes.payments import refund_asaas_charge
        ok = await refund_asaas_charge(confirmed_payment.provider, confirmed_payment.provider_payment_id)
        if not ok:
            raise HTTPException(
                status_code=502,
                detail="Não foi possível solicitar o estorno no gateway. Tente novamente.",
            )
        # O webhook PAYMENT_REFUNDED existente cuida do status do payment + voids.
        walk.operational_status = "ride_cancelled"
        walk.status = "Cancelado"
        walk.no_walker_reason = "Estorno solicitado pelo tutor."
        walk.matching_finished_at = datetime.utcnow()
        walk.confirmation_expires_at = None
        log_event(
            db, walk.id, "tutor_decision", actor_type="tutor", actor_id=user.id,
            metadata={"action": "refund", "payment_id": confirmed_payment.id},
        )

    else:
        raise HTTPException(status_code=422, detail="Ação de decisão inválida.")

    db.commit()
    db.refresh(walk)
    return serialize_operational_walk(walk, db, user=user)


@router.post("/{walk_id}/tip")
async def create_walk_tip(walk_id: str, payload: WalkTipCreate, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    # create_walk_tip_checkout é async (chama o gateway). Antes esta rota era `def`
    # síncrona e retornava a coroutine SEM await => a gorjeta nunca era criada.
    return await create_walk_tip_checkout(
        walk_id,
        WalkTipCheckoutCreate(amount=payload.amount),
        user,
        db,
    )

# _PAID_PAYMENT_STATUSES importado de app.constants (módulo neutro, sem circular)
# Estados em que o passeio ja esta em execucao/concluido — exclusao orfanaria o pagamento
# ou apagaria historico operacional. Defesa em profundidade caso a linha de Payment falte.
_WALK_STATUSES_BLOCKED_FROM_DELETE = {
    "walker_accepted",
    "ride_scheduled",
    "walker_arriving",
    "pet_handover_confirmed",
    "ride_in_progress",
    "awaiting_completion_review",
    "ride_completed",
    "awaiting_tutor_reconfirmation",
    "completion_rejected",
}

@router.delete("/{walk_id}")
def delete_walk(walk_id: str, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    walk = _get_walk_for_user(walk_id, user, db)
    has_paid_payment = (
        db.query(Payment)
        .filter(Payment.walk_id == walk.id, Payment.status.in_(_PAID_PAYMENT_STATUSES))
        .first()
        is not None
    )
    if has_paid_payment or walk.operational_status in _WALK_STATUSES_BLOCKED_FROM_DELETE:
        raise HTTPException(
            status_code=409,
            detail="Nao e possivel excluir um passeio que ja foi pago, esta em andamento ou foi concluido.",
        )
    # Projeto A: deletar passeio coberto por assinatura devolve o crédito.
    refund_credit_for_walk(db, walk)
    db.delete(walk)
    db.commit()
    return {"ok": True}


# api-T2: schemas Pydantic permissivos para as ocorrências de passeio (entrada do
# tutor). Todos os campos são opcionais — espelham os defaults do código anterior — e o
# Pydantic v2 ignora campos extras por padrão; então nenhum payload que os apps já
# enviam é rejeitado. Ganho: validação de tipo, 422 honesto e contrato no OpenAPI.
class WalkComplaintRequest(BaseModel):
    target_type: str | None = None
    target_user_id: str | None = None
    target_pet_id: str | None = None
    category: str | None = None
    title: str | None = None
    description: str | None = None
    notes: str | None = None
    evidences: list[dict] = Field(default_factory=list)
    metadata: dict | None = None


class WalkKitIssueReportRequest(BaseModel):
    confirm_report: bool = False
    missing_items: dict | None = None
    notes: str | None = None


@router.post("/{walk_id}/complaint")
def create_walk_complaint(walk_id: str, payload: WalkComplaintRequest, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    walk = _get_walk_for_user(walk_id, user, db)
    complaint_payload = ComplaintCreate(
        source="tutor",
        target_type=payload.target_type or "walker",
        target_user_id=payload.target_user_id or walk.walker_id,
        target_pet_id=payload.target_pet_id or walk.pet_id,
        walk_id=walk.id,
        category=payload.category or "servico",
        title=payload.title or "Reclamacao sobre passeio",
        description=payload.description or payload.notes or "Tutor registrou uma ocorrencia sobre o passeio.",
        evidences=[ComplaintEvidenceCreate(**item) for item in payload.evidences],
        metadata={"origin": "walk_detail", **(payload.metadata or {})},
    )
    return create_complaint(complaint_payload, user, db)


@router.post("/{walk_id}/kit-issue-report")
def create_walk_kit_issue_report(walk_id: str, payload: WalkKitIssueReportRequest, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    walk = _get_walk_for_user(walk_id, user, db)
    if not payload.confirm_report:
        raise HTTPException(status_code=400, detail="Confirme a ocorrencia antes de enviar.")
    missing = ", ".join([key for key, value in (payload.missing_items or {}).items() if not value]) or "Itens essenciais do kit"
    complaint_payload = ComplaintCreate(
        source="tutor",
        target_type="walker",
        target_user_id=walk.walker_id,
        target_pet_id=walk.pet_id,
        walk_id=walk.id,
        category="falta_cuidado",
        title="Ocorrencia de kit do passeador",
        description=payload.notes or f"Tutor informou problema com kit: {missing}.",
        evidences=[],
        metadata={"origin": "kit_issue_report", "missing_items": payload.missing_items or {}},
    )
    return create_complaint(complaint_payload, user, db)
