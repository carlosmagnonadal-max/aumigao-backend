import json
import logging
from fastapi import APIRouter, Depends, Query, Request
from fastapi import HTTPException
from pydantic import BaseModel, Field
from typing import Any
from datetime import datetime, timedelta
from uuid import uuid4

_logger = logging.getLogger("aumigao.admin")

from sqlalchemy import and_, exists, func, inspect, not_, or_
from sqlalchemy.orm import Session, selectinload
from app.core.database import get_db
from app.services.app_settings_service import (
    append_walker_program_action,
    get_setting,
    recent_walker_program_actions,
    save_setting,
)
from app.dependencies.rbac import require_permission
from app.services.audit_service import record_audit_log
from app.services.payment_split_service import build_payment_split, get_or_create_payment_config, update_payment_config
from app.services.tenant_context import resolve_current_tenant_id
from app.schemas.tenant_payment_config import TenantPaymentConfigResponse, TenantPaymentConfigUpdate
from app.dependencies.tenant_scope import apply_tenant_filter, ensure_tenant_access, get_admin_tenant_scope
from app.models.payment import Payment
from app.models.pet import Pet
from app.models.user import User
from app.models.audit_log import AuditLog
from app.models.admin_operational_event import AdminOperationalEvent
from app.models.walk import Walk
from app.models.walk_completion_review import WalkCompletionReview
from app.models.walk_operational_event import WalkOperationalEvent
from app.models.walk_review import WalkReview
from app.models.walk_tip import WalkTip
from app.models.walker_kit_submission import WalkerKitSubmission
from app.models.walker_profile import WalkerProfile
from app.services.walker_referrals import mark_referral_approved, mark_referral_rejected
from app.services.admin_operational_event_service import (
    record_admin_operational_event,
    serialize_admin_operational_event,
)
from app.services.operational_matching_service import (
    log_event,
    process_expired_attempts,
    serialize_operational_walk,
    start_matching,
    _batch_live_tracking,
)
from app.services.operational_reliability_service import (
    detect_reliability_events,
    record_late_cancellation_if_applicable,
    record_operational_recovery,
)
from app.services.operational_observability_service import (
    get_operational_observability_snapshot,
    record_operational_exception,
    record_operational_log,
)
from app.services.beta_readiness_service import build_beta_readiness_checklist
from app.services.operational_scheduler_service import get_operational_scheduler_status
from app.services.walker_operational_score_service import (
    calculate_walker_operational_scores,
)
from app.routes.notifications import NotificationCreate, _create_notification
from app.lib.admin_serializers import (
    FAKE_ENTITY_TOKENS,
    _now,
    _merge_dict,
    _walker_name,
    _profile_user,
    _has_fake_token,
    _is_valid_email,
    _is_fake_user,
    _is_real_tutor,
    _is_real_pet,
    _is_fake_walker_profile,
    _is_real_active_walker_profile,
    _is_real_walker_user,
    _walk_walker_user,
    _is_real_admin_walk,
    _preload_admin_walk_realness,
    _is_real_walker_user_preloaded,
    _is_real_admin_walk_preloaded,
    _is_completed_admin_walk,
    _is_real_paid_payment,
    _status_label,
    _canonical_application_status,
    _serialize_walker_profile,
    _document_key_list,
    _split_scheduled_date,
    _serialize_admin_walk,
    _preload_admin_payment_refs,
    _serialize_admin_payment,
    _serialize_admin_user,
    _serialize_payment_config,
    _serialize_admin_tutor,
    _serialize_admin_pet,
    _serialize_walker_kit_submission,
    _walk_completion_checklist,
    _serialize_walk_completion_review,
)
from app.models.walker_background_certificate import WalkerBackgroundCertificate
from app.services.background_check_service import (
    compute_background_status,
    official_validation_url as background_official_validation_url,
    DEFAULT_CERT_VALIDITY_DAYS,
)
from app.services.tenant_feature_runtime_service import is_tenant_feature_enabled

router = APIRouter(prefix="/admin", tags=["admin"], dependencies=[Depends(require_permission("admin.access"))])
api_router = APIRouter(prefix="/api/admin", tags=["admin"], dependencies=[Depends(require_permission("admin.access"))])

APPROVED_WALKER_STATUSES = {"active"}
PAID_PAYMENT_STATUSES = {"paid", "Pago", "pagamento_confirmado_sandbox", "payment_confirmed", "confirmed"}
IN_PROGRESS_WALK_STATUSES = {"Indo buscar o pet", "Passeando agora", "walker_arriving", "ride_in_progress"}
DIRECT_COMPLETION_STATUSES = {"ride_completed", "Finalizado", "finalizado", "completed", "finished"}
COMPLETION_REVIEW_MUTABLE_STATUSES = {"pending", "pending_review", "under_review"}
COMPLETION_REVIEW_APPROVED_STATUSES = {"approved"}
COMPLETION_REVIEW_REJECTED_STATUSES = {"rejected", "completion_rejected"}

# Teto de itens serializados na lista `critical_walks` do dashboard. O CONTADOR
# (critical_operational_alerts / beta_operational_health.critical_recovery_walks)
# continua refletindo o total real; só a lista (payload pesado) é limitada.
CRITICAL_WALKS_LIST_CAP = 50

RECOVERY_WALK_STATUSES = {
    "no_walker_found",
    "walker_declined",
    "extended_matching",
    "priority_matching",
    "operational_recovery",
    "support_followup",
    "auto_rematching",
}

OPERATIONAL_EVENT_ENTITY_TYPES = {
    "walk",
    "walker",
    "tutor",
    "pet",
    "complaint",
    "finalization",
    "payment",
    "kit",
    "referral",
    "mission",
    "incentive",
    "system",
}


def _validate_operational_event_payload(payload: dict) -> dict:
    entity_type = str(payload.get("entity_type") or "").strip().lower()
    entity_id = str(payload.get("entity_id") or "").strip()
    title = str(payload.get("title") or "").strip()
    if entity_type not in OPERATIONAL_EVENT_ENTITY_TYPES:
        raise HTTPException(status_code=400, detail="entity_type invalido.")
    if not entity_id:
        raise HTTPException(status_code=400, detail="entity_id obrigatorio.")
    if not title:
        raise HTTPException(status_code=400, detail="title obrigatorio.")
    return {
        "entity_type": entity_type,
        "entity_id": entity_id,
        "title": title,
        "event_type": str(payload.get("event_type") or "admin_note_added").strip() or "admin_note_added",
        "severity": str(payload.get("severity") or "info").strip() or "info",
        "description": str(payload.get("description") or "").strip(),
        "source": str(payload.get("source") or "admin-web.manual").strip() or "admin-web.manual",
        "metadata": payload.get("metadata") if isinstance(payload.get("metadata"), dict) else {},
    }


@router.get("/operational-events")
@api_router.get("/operational-events")
def list_operational_events(
    entity_type: str | None = Query(None),
    entity_id: str | None = Query(None),
    event_type: str | None = Query(None),
    severity: str | None = Query(None),
    limit: int = Query(50, ge=1, le=200),
    db: Session = Depends(get_db),
):
    query = db.query(AdminOperationalEvent)
    if entity_type:
        query = query.filter(AdminOperationalEvent.entity_type == entity_type)
    if entity_id:
        query = query.filter(AdminOperationalEvent.entity_id == entity_id)
    if event_type:
        query = query.filter(AdminOperationalEvent.event_type == event_type)
    if severity:
        query = query.filter(AdminOperationalEvent.severity == severity)
    rows = query.order_by(AdminOperationalEvent.created_at.desc()).limit(limit).all()
    return {"items": [serialize_admin_operational_event(row) for row in rows], "total": len(rows)}


# api-T2: schema permissivo do evento operacional manual. Os campos espelham os lidos
# pelo helper _validate_operational_event_payload; o endpoint converte via model_dump e
# mantem o helper intacto (que ja valida entity_type/entity_id/title obrigatorios).
class OperationalEventRequest(BaseModel):
    entity_type: str | None = None
    entity_id: str | None = None
    title: str | None = None
    event_type: str | None = None
    severity: str | None = None
    description: str | None = None
    source: str | None = None
    metadata: dict | None = None


@router.post("/operational-events")
@api_router.post("/operational-events")
def create_operational_event(payload: OperationalEventRequest, admin: User = Depends(require_permission("alerts.resolve")), db: Session = Depends(get_db)):
    data = _validate_operational_event_payload(payload.model_dump())
    event = record_admin_operational_event(
        db,
        event_type=data["event_type"],
        entity_type=data["entity_type"],
        entity_id=data["entity_id"],
        severity=data["severity"],
        title=data["title"],
        description=data["description"],
        actor=admin,
        source=data["source"],
        metadata=data["metadata"],
    )
    db.commit()
    db.refresh(event)
    return serialize_admin_operational_event(event)


def _ensure_completion_review_can_transition(review: WalkCompletionReview, action: str) -> None:
    status = (review.status or "").strip().lower()
    if status in COMPLETION_REVIEW_MUTABLE_STATUSES:
        return
    if status in COMPLETION_REVIEW_APPROVED_STATUSES:
        detail = "Revisao de finalizacao ja aprovada." if action == "approve" else "Revisao ja aprovada nao pode ser rejeitada."
        raise HTTPException(status_code=409, detail=detail)
    if status in COMPLETION_REVIEW_REJECTED_STATUSES:
        detail = "Revisao rejeitada exige novo reenvio antes da aprovacao." if action == "approve" else "Revisao de finalizacao ja rejeitada."
        raise HTTPException(status_code=409, detail=detail)
    raise HTTPException(status_code=409, detail="Revisao de finalizacao nao esta pendente para decisao operacional.")


def _ensure_internal_walk_payment(walk: Walk, db: Session):
    existing_paid = db.query(Payment).filter(
        Payment.walk_id == walk.id,
        Payment.status.in_(PAID_PAYMENT_STATUSES),
    ).first()
    if existing_paid:
        return existing_paid
    # A-02: registra o split de comissão (antes ficava None -> saldo do walker e
    # relatórios financeiros não tinham o repasse das finalizações manuais).
    amount = float(walk.price or 0)
    split = build_payment_split(db, walk.tenant_id, amount)
    payment = Payment(
        id=str(uuid4()),
        tenant_id=walk.tenant_id,
        tutor_id=walk.tutor_id,
        walk_id=walk.id,
        amount=amount,
        status="paid",
        provider="internal",
        commission_percent=split["commission_percent"],
        platform_amount=split["platform_amount"],
        walker_amount=split["walker_amount"],
    )
    db.add(payment)
    return payment


TUTOR_RECONFIRMATION_STATUSES = {
    "awaiting_tutor_reconfirmation",
}

DEFAULT_REFERRAL_PROGRAM_SETTINGS = {
    "program_enabled": False,
    "client_referral_enabled": False,
    "walker_referral_enabled": False,
    "app_visible": False,
    "client_rules": {
        "indicated_discount_amount": 20,
        "referrer_coupon_credit_amount": 20,
        "min_paid_walks_for_referrer_bonus": 2,
        "referral_limit_per_user": 20,
        "benefit_validity_days": 45,
    },
    "walker_rules": {
        "fixed_bonus_amount": 100,
        "min_completed_walks": 20,
        "min_rating_required": 4.7,
        "max_no_show_rate": 4,
        "eligibility_window_days": 60,
    },
    "updated_at": "",
    "updated_by": "sistema",
}

# LEGADO: endpoint GET /admin/referrals é legado de tela demo.
# O sistema real de indicações está em routes/referrals.py (GET /admin/referrals/walkers).
# Dados demo removidos; lista vazia até eventual remoção do endpoint legado.
REFERRAL_RECORDS: list[dict] = []

DEFAULT_WALKER_PROGRAM_SETTINGS = {
    "tips": {
        "enabled": True,
        "separate_from_earnings": True,
        "post_delivery_only": True,
        "score_impact_cap_points": 0,
        "review_required_above_amount": 80,
        "policy": "Gorjetas sao opcionais, liberadas apos entrega do pet, exibidas separadas dos ganhos e nao alteram reputacao, matching ou boost.",
    },
    "kit": {
        "enabled": True,
        "public_visibility": True,
        "ranking_bonus_basic": 4,
        "ranking_bonus_essential": 8,
        "ranking_bonus_premium": 12,
        "tiers": [
            {"key": "basic", "label": "Basico", "items": ["Agua", "Vasilha para agua", "Saquinho para necessidades"], "ranking_bonus": 4},
            {"key": "intermediate", "label": "Intermediario", "items": ["Agua", "Vasilha para agua", "Saquinho para necessidades", "Primeiros socorros", "Toalha/pano"], "ranking_bonus": 8},
            {"key": "premium", "label": "Premium", "items": ["Agua", "Vasilha para agua", "Saquinho para necessidades", "Primeiros socorros", "Toalha/pano", "Itens premium"], "ranking_bonus": 12},
        ],
        "required_items": ["Agua", "Vasilha para agua", "Saquinho para necessidades"],
        "premium_items": ["Primeiros socorros", "Toalha/pano", "Itens premium"],
    },
    "cr": {
        "enabled": True,
        "purchase_allowed": False,
        "daily_use_limit": 3,
        "actions": [
            {"key": "matching_boost", "label": "Boost matching", "cost": 4, "duration_minutes": 45},
            {"key": "early_wave", "label": "Entrada antecipada", "cost": 3, "duration_minutes": 20},
            {"key": "visual_highlight", "label": "Destaque visual", "cost": 2, "duration_minutes": 60},
        ],
        "earning_rules": [
            {"key": "five_star_walk", "label": "Passeio 5 estrelas", "credits": 1},
            {"key": "no_delay_week", "label": "Semana sem atraso grave", "credits": 3},
            {"key": "kit_verified", "label": "Kit auditado aprovado", "credits": 2},
        ],
    },
    "matching": {
        "enabled": True,
        "weights": {
            "experience": 25,
            "distance": 20,
            "rating": 20,
            "availability": 15,
            "schedule_safety": 10,
            "kit": 5,
            "cr_boost": 5,
        },
        "cr_boost_cap_points": 8,
        "max_distance_km": 8,
    },
    "rating": {
        "enabled": True,
        "min_reviews_for_public_rating": 5,
        "recent_window_walks": 20,
        "tip_score_impact_cap_points": 0,
        "severe_delay_penalty_points": 12,
        "no_show_penalty_points": 25,
    },
    "schedule": {
        "min_interval_minutes": 15,
        "block_conflicting_acceptance": True,
        "message": "Novos aceites exigem pelo menos 15 min entre o fim de um passeio e o inicio do outro.",
    },
    "updated_at": "",
    "updated_by": "sistema",
}

def _apply_application_status(profile: WalkerProfile, status: str, reason: str | None = None):
    raw_status = _canonical_application_status(status)
    profile.status = raw_status
    profile.updated_at = datetime.utcnow()
    if raw_status == "active":
        profile.active_as_walker = True
        profile.approved_at = profile.approved_at or datetime.utcnow()
        profile.rejected_at = None
        profile.rejection_reason = None
    elif raw_status == "approved":
        profile.active_as_walker = False
        profile.approved_at = datetime.utcnow()
        profile.rejected_at = None
        profile.rejection_reason = None
    elif raw_status == "rejected":
        profile.active_as_walker = False
        profile.approved_at = None
        profile.rejected_at = datetime.utcnow()
        profile.rejection_reason = reason
    elif raw_status == "resubmission_requested":
        profile.active_as_walker = False
        profile.approved_at = None
        profile.rejected_at = None
        profile.rejection_reason = reason
    else:
        profile.active_as_walker = False
        profile.approved_at = None
        profile.rejected_at = None
        if raw_status in {"submitted", "under_review"}:
            profile.rejection_reason = None


def _unique_walker_profiles(db: Session, include_internal: bool = True) -> list[dict]:
    rows = []
    seen_keys = set()
    # selectinload do user (relationship WalkerProfile.user): popula o identity map
    # da sessão numa query batch, de modo que os db.get(User, profile.user_id) em
    # _profile_user / _serialize_walker_profile / _is_fake_walker_profile virem
    # cache hit (elimina o N+1 de 1 SELECT de user por passeador).
    profiles_query = (
        db.query(WalkerProfile)
        .options(selectinload(WalkerProfile.user))
        .order_by(WalkerProfile.created_at.desc())
    )
    surviving = []
    for profile in profiles_query.all():
        user = _profile_user(profile, db)
        if _is_fake_walker_profile(profile, user):
            continue
        key = (profile.cpf or profile.user_id or profile.id or profile.phone or (user.email if user else "")).strip().lower()
        if key in seen_keys:
            continue
        seen_keys.add(key)
        surviving.append(profile)
    # B-ALT-006 follow-up: score operacional de todos os passeadores em LOTE (4 queries
    # totais) em vez de 4 por passeador — elimina o N+1 residual da listagem.
    scores = calculate_walker_operational_scores([p.user_id for p in surviving], db)
    for profile in surviving:
        rows.append(_serialize_walker_profile(
            profile, db, include_internal=include_internal,
            operational_score=scores.get(profile.user_id),
        ))
    return rows


def _table_exists(db: Session, table_name: str) -> bool:
    try:
        bind = db.get_bind()
        return inspect(bind).has_table(table_name)
    except Exception as _exc:  # F17: loga em vez de silenciar
        _logger.warning("Erro ao verificar existência da tabela '%s': %s", table_name, _exc)
        return False


def _refresh_reliability_events(walks: list[Walk], db: Session) -> None:
    if not _table_exists(db, "walk_operational_events"):
        return

    created = False
    for walk in walks:
        created = bool(detect_reliability_events(walk, db)) or created
    if created:
        db.commit()


def _build_beta_operational_health(
    db: Session,
    real_walks: list[Walk],
    completed_real_walks: list[Walk],
    critical_walks: list[Walk],
) -> dict:
    real_walk_ids = {walk.id for walk in real_walks}
    completed_walk_ids = {walk.id for walk in completed_real_walks}
    recent_cutoff = datetime.utcnow() - timedelta(hours=24)
    has_completion_reviews_table = _table_exists(db, "walk_completion_reviews")
    has_operational_events_table = _table_exists(db, "walk_operational_events")
    has_reviews_table = _table_exists(db, "walk_reviews")
    has_tips_table = _table_exists(db, "walk_tips")

    if real_walk_ids:
        completion_reviews = db.query(WalkCompletionReview).filter(WalkCompletionReview.walk_id.in_(real_walk_ids)).all() if has_completion_reviews_table else []
        operational_events = db.query(WalkOperationalEvent).filter(WalkOperationalEvent.walk_id.in_(real_walk_ids)).all() if has_operational_events_table else []
        reviews = db.query(WalkReview).filter(WalkReview.walk_id.in_(real_walk_ids)).all() if has_reviews_table else []
        tips = db.query(WalkTip).filter(WalkTip.walk_id.in_(real_walk_ids)).all() if has_tips_table else []
    else:
        completion_reviews = []
        operational_events = []
        reviews = []
        tips = []

    pending_completion_reviews = len([review for review in completion_reviews if review.status in COMPLETION_REVIEW_MUTABLE_STATUSES])
    rejected_completion_reviews = len([review for review in completion_reviews if review.status in COMPLETION_REVIEW_REJECTED_STATUSES])
    approved_completion_reviews = len([review for review in completion_reviews if review.status in COMPLETION_REVIEW_APPROVED_STATUSES])

    high_severity_events = len([event for event in operational_events if event.severity == "high"])
    medium_severity_events = len([event for event in operational_events if event.severity == "medium"])
    recent_events = len([event for event in operational_events if event.created_at and event.created_at >= recent_cutoff])
    missing_checkins = len([event for event in operational_events if event.event_type == "missing_checkin"])
    late_events = len([event for event in operational_events if event.event_type in {"walker_late", "late_cancellation"}])

    paid_tips = [tip for tip in tips if tip.status == "paid"]
    pending_tips = [tip for tip in tips if tip.status == "pending"]
    reviewed_completed_walk_ids = {review.walk_id for review in reviews}

    attention_points = (
        pending_completion_reviews
        + rejected_completion_reviews
        + high_severity_events
        + len(critical_walks)
        + missing_checkins
    )
    if high_severity_events > 0 or missing_checkins > 0 or attention_points >= 5:
        status = "attention"
        status_label = "Atenção operacional"
        summary = "Há pontos pendentes que exigem acompanhamento ativo da operação beta."
    elif attention_points > 0 or medium_severity_events > 0:
        status = "watch"
        status_label = "Monitoramento assistido"
        summary = "Operação está controlada, com sinais pontuais em acompanhamento."
    else:
        status = "stable"
        status_label = "Operação estável"
        summary = "Sem sinais críticos no fluxo auditável do beta neste momento."

    return {
        "status": status,
        "status_label": status_label,
        "summary": summary,
        "pending_completion_reviews": pending_completion_reviews,
        "approved_completion_reviews": approved_completion_reviews,
        "rejected_completion_reviews": rejected_completion_reviews,
        "active_walks": len([walk for walk in real_walks if walk.status in IN_PROGRESS_WALK_STATUSES or walk.operational_status in IN_PROGRESS_WALK_STATUSES]),
        "critical_recovery_walks": len(critical_walks),
        "high_severity_events": high_severity_events,
        "medium_severity_events": medium_severity_events,
        "recent_operational_events": recent_events,
        "missing_checkins": missing_checkins,
        "late_events": late_events,
        "completed_walks": len(completed_real_walks),
        "completed_walks_reviewed": len(completed_walk_ids.intersection(reviewed_completed_walk_ids)),
        "reviews_submitted": len(reviews),
        "tips_paid": len(paid_tips),
        "tips_pending": len(pending_tips),
        "tips_paid_amount": round(sum(float(tip.amount or 0) for tip in paid_tips), 2),
        "data_availability": {
            "walk_completion_reviews": has_completion_reviews_table,
            "walk_operational_events": has_operational_events_table,
            "walk_reviews": has_reviews_table,
            "walk_tips": has_tips_table,
        },
    }


def _weekly_walk_tip_amount(db: Session, real_walk_ids: set[str]) -> float:
    if not real_walk_ids or not _table_exists(db, "walk_tips"):
        return 0

    week_cutoff = datetime.utcnow() - timedelta(days=7)
    tips = (
        db.query(WalkTip)
        .filter(
            WalkTip.walk_id.in_(real_walk_ids),
            WalkTip.status == "paid",
            WalkTip.paid_at >= week_cutoff,
        )
        .all()
    )
    return round(sum(float(tip.amount or 0) for tip in tips), 2)


def _walker_program_rows(db: Session) -> list[dict]:
    """F02: dados reais do banco; sem index%2 nem demo rows.

    B-03: batch queries — um query por relação em vez de N por passeador.
    """
    from app.services.walker_operational_score_service import calculate_walker_operational_scores

    now = datetime.utcnow()
    week_start = datetime(now.year, now.month, now.day) - timedelta(days=now.weekday())

    profiles = db.query(WalkerProfile).all()

    # --- pré-filtragem de fake profiles (requer User) ---
    # Batch: busca todos os users relevantes de uma vez.
    all_user_ids = [p.user_id for p in profiles if p.user_id]
    users_by_id: dict[str, User] = {
        u.id: u
        for u in db.query(User).filter(User.id.in_(all_user_ids)).all()
    } if all_user_ids else {}

    real_profiles = [
        p for p in profiles
        if not _is_fake_walker_profile(p, users_by_id.get(p.user_id) if p.user_id else None)
    ]
    if not real_profiles:
        return []

    walker_user_ids = [p.user_id for p in real_profiles if p.user_id]
    id_set = set(walker_user_ids)

    # --- batch: passeios concluídos por walker ---
    completed_by: dict[str, int] = {wid: 0 for wid in walker_user_ids}
    for walk_id, count in (
        db.query(Walk.walker_id, func.count(Walk.id))
        .filter(Walk.walker_id.in_(id_set), Walk.status == "Finalizado")
        .group_by(Walk.walker_id)
        .all()
    ):
        completed_by[walk_id] = count

    # --- batch: avaliações por walker ---
    reviews_by: dict[str, list] = {wid: [] for wid in walker_user_ids}
    for review in db.query(WalkReview).filter(WalkReview.walker_id.in_(id_set)).all():
        if review.walker_id in reviews_by:
            reviews_by[review.walker_id].append(review)

    # --- batch: score operacional (já tem função de batch no serviço) ---
    op_scores = calculate_walker_operational_scores(walker_user_ids, db)

    # --- batch: kit submissions por walker ---
    kit_by: dict[str, WalkerKitSubmission | None] = {wid: None for wid in walker_user_ids}
    for kit in db.query(WalkerKitSubmission).filter(WalkerKitSubmission.walker_user_id.in_(id_set)).all():
        # first() semântica: keep first seen (subsequent ignored)
        if kit_by.get(kit.walker_user_id) is None:
            kit_by[kit.walker_user_id] = kit

    # --- batch: gorjetas da semana por walker ---
    tips_by: dict[str, float] = {wid: 0.0 for wid in walker_user_ids}
    for tip in db.query(WalkTip).filter(
        WalkTip.walker_id.in_(id_set),
        WalkTip.status == "paid",
        WalkTip.created_at >= week_start,
    ).all():
        if tip.walker_id in tips_by:
            tips_by[tip.walker_id] += float(tip.amount or 0)

    # --- assembly ---
    rows = []
    for profile in real_profiles:
        wid = profile.user_id
        user = users_by_id.get(wid) if wid else None

        # Avaliações
        reviews = reviews_by.get(wid, [])
        rating_count = len(reviews)
        rating_avg = round(sum(r.rating for r in reviews) / rating_count, 2) if rating_count else None

        # Score operacional — reusa _score_from_inputs via batch service
        op_score = op_scores.get(wid) if wid else None
        score = op_score.get("operational_score") if op_score else None
        matching_score = score  # mesmo score como proxy

        # Kit
        kit_sub = kit_by.get(wid) if wid else None
        kit_audit_status = kit_sub.audit_status if kit_sub else "sem_kit"
        kit_level = None  # sem tabela de nível de kit calculado; kit_sub tem items_json mas não nível numérico

        # Nome: user já carregado no batch
        name = (user.full_name if user else None) or (user.email if user else None) or "Passeador"

        rows.append({
            "walker_id": profile.id,
            "user_id": wid,
            "name": name,
            "status": profile.status,
            "kit_level": kit_level,
            "kit_audit_status": kit_audit_status,
            "cr_balance": 0,          # sem tabela de saldo CR; honesto = 0
            "cr_earned_this_week": 0,
            "rating_avg": rating_avg,
            "rating_count": rating_count,
            "score": score,
            "matching_score": matching_score,
            "tips_week": tips_by.get(wid, 0.0),
            "tips_pending_review": 0,  # sem fila de revisão real implementada
            "completed_walks": completed_by.get(wid, 0),
            "schedule_conflicts_blocked": 0,  # sem tabela; honesto = 0
        })
    # F02: sem demo row de fallback — lista vazia é honesta
    return rows


def _walker_program_metrics(rows: list[dict]) -> dict:
    return {
        "total_walkers": len(rows),
        "kit_pending_audit": len([row for row in rows if row["kit_audit_status"] == "pendente"]),
        "tips_pending_review": sum(int(row["tips_pending_review"]) for row in rows),
        "cr_circulating": sum(int(row["cr_balance"]) for row in rows),
        "avg_matching_score": round(sum(float(row["matching_score"]) for row in rows) / max(1, len(rows)), 1),
        "schedule_conflicts_blocked": sum(int(row["schedule_conflicts_blocked"]) for row in rows),
    }
@router.get("/operational-alerts")
@api_router.get("/operational-alerts")
def operational_alerts(admin: User = Depends(require_permission("walks.read")), db: Session = Depends(get_db)):
    process_expired_attempts(db)
    scope = get_admin_tenant_scope(admin)

    real_walks = [
        walk
        for walk in apply_tenant_filter(db.query(Walk), Walk, scope).order_by(Walk.created_at.desc()).all()
        if _is_real_admin_walk(walk, db)
    ]
    _refresh_reliability_events(real_walks, db)

    alert_walks = [
        walk
        for walk in real_walks
        if str(walk.operational_status or walk.status or "").lower() in RECOVERY_WALK_STATUSES
    ]

    return {
        "total": len(alert_walks),
        "items": [_serialize_admin_walk(walk, db) for walk in alert_walks],
    }

def _not_fake_token_conditions(columns: list):
    """Gera clausulas NOT LIKE para cada token fake em cada coluna informada.

    Retorna uma lista de clausulas AND (cada uma garante que nenhum token aparece
    na concatenacao das colunas em lowercase).  Colunas devem ser atributos ORM.
    """
    conditions = []
    for token in FAKE_ENTITY_TOKENS:
        token_lower = token.lower()
        # Qualquer coluna contendo o token => entidade fake => excluir
        col_conditions = [func.lower(func.coalesce(col, "")).contains(token_lower) for col in columns]
        conditions.append(not_(or_(*col_conditions)))
    return conditions


def _sql_real_tutor_filters(scope):
    """Filtros SQL equivalentes a _is_real_tutor (sem apply_tenant_filter)."""
    tutor_roles = ("tutor", "cliente", "client", "customer")
    fake_free = _not_fake_token_conditions([User.id, User.email, User.full_name])
    return [
        User.role.in_(tutor_roles),
        User.email.ilike("%@%"),
        *fake_free,
    ]


def _sql_count_real_tutors(db: Session, scope) -> int:
    """Conta tutores reais em SQL substituindo User.all() + _is_real_tutor."""
    q = apply_tenant_filter(db.query(func.count(User.id)), User, scope)
    q = q.filter(*_sql_real_tutor_filters(scope))
    return q.scalar() or 0


def _sql_count_real_pets(db: Session, scope) -> int:
    """Conta pets reais em SQL substituindo Pet.all() + _is_real_pet."""
    # Pet nao pode ter tokens fake nas colunas chave
    fake_free_pet = _not_fake_token_conditions([Pet.id, Pet.name, Pet.photo_url, Pet.tutor_id])
    # Tutor do pet tambem precisa ser real (EXISTS subquery)
    real_tutor_sub = (
        db.query(User.id)
        .filter(User.id == Pet.tutor_id, *_sql_real_tutor_filters(scope))
        .exists()
    )
    q = apply_tenant_filter(db.query(func.count(Pet.id)), Pet, scope)
    q = q.filter(*fake_free_pet, real_tutor_sub)
    return q.scalar() or 0


def _sql_count_real_active_walkers(db: Session) -> int:
    """Conta walkers reais ativos em SQL substituindo WalkerProfile.all() + _is_real_active_walker_profile.

    WalkerProfile e global (nao passa por apply_tenant_filter) — mantido assim por design.
    """
    walker_roles = ("walker", "passeador")
    fake_free_profile = _not_fake_token_conditions([
        WalkerProfile.full_name, WalkerProfile.cpf, WalkerProfile.phone,
        WalkerProfile.id, WalkerProfile.user_id,
    ])
    # O usuario do walker tambem nao pode ser fake
    real_walker_user_sub = (
        db.query(User.id)
        .filter(
            User.id == WalkerProfile.user_id,
            User.role.in_(walker_roles),
            *_not_fake_token_conditions([User.email, User.full_name]),
        )
        .exists()
    )
    q = db.query(func.count(WalkerProfile.id)).filter(
        WalkerProfile.status == "active",
        WalkerProfile.active_as_walker.is_(True),
        *fake_free_profile,
        real_walker_user_sub,
    )
    return q.scalar() or 0


def _sql_count_risk_walkers(db: Session) -> int:
    """Conta walkers em status de risco nao-fake em SQL.

    WalkerProfile e global por design.
    """
    fake_free_profile = _not_fake_token_conditions([
        WalkerProfile.full_name, WalkerProfile.cpf, WalkerProfile.phone,
        WalkerProfile.id, WalkerProfile.user_id,
    ])
    real_user_sub = (
        db.query(User.id)
        .filter(
            User.id == WalkerProfile.user_id,
            *_not_fake_token_conditions([User.email, User.full_name]),
        )
        .exists()
    )
    q = db.query(func.count(WalkerProfile.id)).filter(
        WalkerProfile.status.in_(["restricted", "suspended"]),
        *fake_free_profile,
        real_user_sub,
    )
    return q.scalar() or 0


@router.get("/dashboard")
@api_router.get("/dashboard")
def dashboard(admin: User = Depends(require_permission("admin.access")), db: Session = Depends(get_db)):
    scope = get_admin_tenant_scope(admin)

    # --- Agregacoes SQL: O(1) queries em vez de carregar tabelas inteiras ---
    total_real_clients = _sql_count_real_tutors(db, scope)
    total_real_pets = _sql_count_real_pets(db, scope)
    real_active_walkers_count = _sql_count_real_active_walkers(db)
    real_risk_walkers_count = _sql_count_risk_walkers(db)

    # Walks: pré-filtra os FAKE em SQL antes de carregar (B-ALT-005). Os mesmos campos
    # checados por _has_fake_token no realness Python; como _not_fake_token_conditions
    # usa substring por coluna (tokens sem espaço), o conjunto carregado é IDÊNTICO ao
    # que o passo 1 do realness manteria — só evita trazer walks de demo/teste para a
    # memória. O filtro de tutor/pet real (join-coupled) segue em Python.
    walk_rows = (
        apply_tenant_filter(db.query(Walk), Walk, scope)
        .filter(*_not_fake_token_conditions([
            Walk.id, Walk.tutor_id, Walk.walker_id, Walk.assigned_walker_id,
            Walk.pet_id, Walk.address_snapshot, Walk.notes,
        ]))
        .all()
    )
    walk_users_by_id, walk_pets_by_id, walk_profiles_by_user_id = _preload_admin_walk_realness(walk_rows, db)
    real_walks = [
        walk
        for walk in walk_rows
        if _is_real_admin_walk_preloaded(walk, walk_users_by_id, walk_pets_by_id, walk_profiles_by_user_id)
    ]
    critical_walks = [
        walk
        for walk in real_walks
        if str(walk.operational_status or walk.status or "").lower() in RECOVERY_WALK_STATUSES
    ]
    completed_real_walks = [
        walk
        for walk in real_walks
        if _is_completed_admin_walk(walk)
        and _is_real_admin_walk_preloaded(walk, walk_users_by_id, walk_pets_by_id, walk_profiles_by_user_id, require_walker=True)
    ]
    real_revenue_walk_ids = {walk.id for walk in completed_real_walks}

    # Payments: soma em SQL limitando por status e walk_ids reais
    if real_revenue_walk_ids:
        revenue_q = (
            apply_tenant_filter(db.query(func.sum(Payment.amount)), Payment, scope)
            .filter(
                Payment.status.in_(PAID_PAYMENT_STATUSES),
                Payment.walk_id.in_(real_revenue_walk_ids),
                *_not_fake_token_conditions([Payment.id, Payment.tutor_id, Payment.walk_id, Payment.provider, Payment.provider_payment_id]),
            )
        )
        estimated_revenue = float(revenue_q.scalar() or 0)
    else:
        estimated_revenue = 0.0

    no_show_total = len([walk for walk in real_walks if walk.status in {"Não comparecimento do cliente", "Não comparecimento do passeador"}])
    walk_total = len(real_walks)
    beta_operational_health = _build_beta_operational_health(db, real_walks, completed_real_walks, critical_walks)
    operational_observability = get_operational_observability_snapshot(db)
    operational_scheduler = get_operational_scheduler_status()
    beta_readiness = build_beta_readiness_checklist(
        db,
        beta_operational_health=beta_operational_health,
        operational_observability=operational_observability,
        operational_scheduler=operational_scheduler,
        recovery_statuses=RECOVERY_WALK_STATUSES,
    )
    return {
        "total_clients": total_real_clients,
        "total_tutors": total_real_clients,
        "total_pets": total_real_pets,
        "total_active_walkers": real_active_walkers_count,
        "total_walkers": real_active_walkers_count,
        "total_walks_scheduled": len([walk for walk in real_walks if walk.status == "Agendado"]),
        "scheduled_walks": len([walk for walk in real_walks if walk.status == "Agendado"]),
        "total_walks_finished": len(completed_real_walks),
        "completed_walks": len(completed_real_walks),
        "total_walks_in_progress": len([walk for walk in real_walks if walk.status in IN_PROGRESS_WALK_STATUSES or walk.operational_status in IN_PROGRESS_WALK_STATUSES]),
        "estimated_revenue_paid": estimated_revenue,
        "estimated_revenue": estimated_revenue,
        "pending_occurrences": 0,
        "open_disputes": 0,
        "walkers_at_risk": real_risk_walkers_count,
        "top_rated_walkers": 0,
        "disintermediation_alerts": 0,
        "critical_operational_alerts": len(critical_walks),

        # Lista limitada a CRITICAL_WALKS_LIST_CAP para não estourar payload em
        # tenants grandes. O contador critical_operational_alerts (acima) e o
        # beta_operational_health.critical_recovery_walks seguem contando TODOS.
        "critical_walks": [
            {
                "id": walk.id,
                "pet_id": walk.pet_id,
                "tutor_id": walk.tutor_id,
                "status": walk.status,
                "operational_status": walk.operational_status,
                "scheduled_date": walk.scheduled_date,
            }
            for walk in critical_walks[:CRITICAL_WALKS_LIST_CAP]
        ],
        "weekly_tips_amount": _weekly_walk_tip_amount(db, {walk.id for walk in real_walks}),
        "no_show_rate": round((no_show_total / walk_total) * 100, 2) if walk_total else 0,
        "beta_operational_health": beta_operational_health,
        "operational_observability": operational_observability,
        "operational_scheduler": operational_scheduler,
        "beta_readiness": beta_readiness,
    }

@router.get("/users")
@api_router.get("/users")
def users(
    admin: User = Depends(require_permission("users.read")),
    db: Session = Depends(get_db),
    limit: int = Query(200, ge=1, le=1000),
    offset: int = Query(0, ge=0),
):
    # super_admin enxerga todos os tenants; admin regular fica restrito ao seu.
    query = apply_tenant_filter(db.query(User), User, get_admin_tenant_scope(admin))
    return [_serialize_admin_user(u) for u in query.order_by(User.created_at.desc()).offset(offset).limit(limit).all()]


@router.get("/users/{user_id}")
@api_router.get("/users/{user_id}")
def get_admin_user(
    user_id: str,
    admin: User = Depends(require_permission("users.read")),
    db: Session = Depends(get_db),
):
    user = db.get(User, user_id)
    if not user:
        raise HTTPException(status_code=404, detail="Usuario nao encontrado")
    scope = get_admin_tenant_scope(admin)
    ensure_tenant_access(user.tenant_id, scope)
    return _serialize_admin_user(user)


@router.get("/audit-logs")
@api_router.get("/audit-logs")
def list_audit_logs(
    admin: User = Depends(require_permission("audit_logs.read")),
    db: Session = Depends(get_db),
    limit: int = Query(100, ge=1, le=500),
):
    query = apply_tenant_filter(db.query(AuditLog), AuditLog, get_admin_tenant_scope(admin))
    rows = query.order_by(AuditLog.created_at.desc()).limit(limit).all()
    return [
        {
            "id": r.id,
            "actor_user_id": r.actor_user_id,
            "actor_type": r.actor_type,
            "tenant_id": r.tenant_id,
            "action": r.action,
            "entity_type": r.entity_type,
            "entity_id": r.entity_id,
            "before_data": r.before_data,
            "after_data": r.after_data,
            "ip_address": r.ip_address,
            "created_at": r.created_at,
        }
        for r in rows
    ]


@router.get("/operational-observability")
@api_router.get("/operational-observability")
def get_operational_observability(
    admin: User = Depends(require_permission("walks.read")),
    db: Session = Depends(get_db),
    limit: int = Query(20, ge=1, le=25),
):
    """Snapshot de logs operacionais (OperationalBetaLog) para a tela Saúde do Sistema."""
    snapshot = get_operational_observability_snapshot(db, limit=limit)
    return snapshot


@router.get("/payment-config", response_model=TenantPaymentConfigResponse)
@api_router.get("/payment-config", response_model=TenantPaymentConfigResponse)
def get_payment_config(
    admin: User = Depends(require_permission("finance.read")),
    db: Session = Depends(get_db),
):
    scope = get_admin_tenant_scope(admin)
    tenant_id = scope.tenant_id or resolve_current_tenant_id(db)
    config = get_or_create_payment_config(db, tenant_id)
    db.commit()
    return _serialize_payment_config(config)


@router.put("/payment-config", response_model=TenantPaymentConfigResponse)
@api_router.put("/payment-config", response_model=TenantPaymentConfigResponse)
def update_payment_config_endpoint(
    payload: TenantPaymentConfigUpdate,
    admin: User = Depends(require_permission("finance.manage")),
    db: Session = Depends(get_db),
):
    scope = get_admin_tenant_scope(admin)
    tenant_id = scope.tenant_id or resolve_current_tenant_id(db)
    # Comissão da plataforma: somente super_admin pode alterar.
    if payload.commission_percent is not None and admin.role != "super_admin":
        raise HTTPException(status_code=403, detail="O percentual da plataforma só pode ser alterado pela operadora.")
    config = update_payment_config(
        db,
        tenant_id,
        commission_percent=payload.commission_percent,
        tenant_margin_percent=payload.tenant_margin_percent,
        provider=payload.provider,
        split_enabled=payload.split_enabled,
        actor=admin,
    )
    return _serialize_payment_config(config)


@router.get("/tutors")
@api_router.get("/tutors")
def tutors(
    admin: User = Depends(require_permission("users.read")),
    db: Session = Depends(get_db),
    limit: int = Query(200, ge=1, le=1000),
    offset: int = Query(0, ge=0),
):
    users = [
        user
        for user in apply_tenant_filter(db.query(User), User, get_admin_tenant_scope(admin)).order_by(User.created_at.desc()).all()
        if _is_real_tutor(user)
    ]
    paginated = users[offset: offset + limit]
    return [_serialize_admin_tutor(user, db) for user in paginated]

@router.get("/pets")
@api_router.get("/pets")
def admin_pets(admin: User = Depends(require_permission("tutors.read")), db: Session = Depends(get_db)):
    pets = [
        pet
        for pet in apply_tenant_filter(db.query(Pet), Pet, get_admin_tenant_scope(admin)).order_by(Pet.created_at.desc()).all()
        if _is_real_pet(pet, db.get(User, pet.tutor_id) if pet.tutor_id else None)
    ]
    return [_serialize_admin_pet(pet, db) for pet in pets]

@router.get("/walkers")
@api_router.get("/walkers")
def walkers(_admin: User = Depends(require_permission("walkers.read")), db: Session = Depends(get_db)):
    # WalkerProfile nao possui tenant_id (walkers sao globais da plataforma).
    # A autenticacao/permissao acima e suficiente para proteger o endpoint.
    return _unique_walker_profiles(db)

@router.get("/partner-applications")
@api_router.get("/partner-applications")
def partner_applications(_admin: User = Depends(require_permission("walkers.read")), db: Session = Depends(get_db)):
    # WalkerProfile nao possui tenant_id (walkers sao globais da plataforma).
    return _unique_walker_profiles(db, include_internal=False)


@router.get("/partner-applications/{candidate_id}")
@api_router.get("/partner-applications/{candidate_id}")
def partner_application_detail(candidate_id: str, db: Session = Depends(get_db)):
    profile = db.get(WalkerProfile, candidate_id)
    if not profile:
        raise HTTPException(status_code=404, detail="Candidatura nao encontrada")
    return _serialize_walker_profile(profile, db)


@router.patch("/partner-applications/{candidate_id}/background-certificate/{cert_id}")
@api_router.patch("/partner-applications/{candidate_id}/background-certificate/{cert_id}")
def update_background_certificate(
    candidate_id: str,
    cert_id: str,
    request: Request,
    payload: dict | None = None,
    admin: User = Depends(require_permission("walkers.validate")),
    db: Session = Depends(get_db),
):
    """Valida / rejeita / expira UMA certidao de antecedentes (semi-manual).

    payload = {"status": "validated"|"rejected"|"expired", "notes": str?}.
    Recomputa o status agregado do passeador.
    """
    profile = db.get(WalkerProfile, candidate_id)
    if not profile:
        raise HTTPException(status_code=404, detail="Candidatura nao encontrada")
    cert = db.get(WalkerBackgroundCertificate, cert_id)
    if not cert or cert.walker_profile_id != profile.id:
        raise HTTPException(status_code=404, detail="Certidao nao encontrada")
    payload = payload or {}
    new_status = (payload.get("status") or "").strip().lower()
    if new_status not in {"validated", "rejected", "expired"}:
        raise HTTPException(status_code=400, detail="Status de certidao invalido.")
    notes = payload.get("notes")
    cert.status = new_status
    if notes is not None:
        cert.notes = str(notes)
    if new_status == "validated":
        cert.validated_by_admin_id = admin.id
        cert.validated_at = datetime.utcnow()
        if not cert.expires_at:
            cert.expires_at = datetime.utcnow() + timedelta(days=DEFAULT_CERT_VALIDITY_DAYS)
    else:
        cert.validated_by_admin_id = admin.id
        cert.validated_at = None
    cert.updated_at = datetime.utcnow()

    certificates = (
        db.query(WalkerBackgroundCertificate)
        .filter(WalkerBackgroundCertificate.walker_profile_id == profile.id)
        .all()
    )
    aggregate = compute_background_status(profile, certificates)
    record_admin_operational_event(
        db,
        event_type="background_cert_validated" if new_status == "validated" else "background_cert_rejected",
        entity_type="walker",
        entity_id=profile.user_id,
        severity="info" if new_status == "validated" else "warning",
        title="Certidao de antecedentes atualizada",
        description=f"Certidao {cert.cert_type} marcada como {new_status} pela administracao.",
        actor=admin,
        source="admin.walker.background_certificate",
        metadata={
            "candidate_id": profile.id,
            "cert_id": cert.id,
            "cert_type": cert.cert_type,
            "status": new_status,
            "aggregate": aggregate,
        },
        request=request,
    )
    db.commit()
    db.refresh(profile)
    return _serialize_walker_profile(profile, db)


# api-T2: schema permissivo dos campos administrativos da candidatura (PATCH). Todos os
# campos sao opcionais; usamos model_dump(exclude_unset=True) para obter SO as chaves que
# o cliente enviou, preservando a semantica PATCH original (`"x" in payload`). Pydantic v2
# ignora extras -> nenhum payload legitimo e rejeitado.
class UpdatePartnerApplicationRequest(BaseModel):
    internal_notes: str | None = None
    status: str | None = None
    reason: str | None = None
    reviewed_by_admin_id: str | None = None
    resubmission_requested_documents: Any | None = None
    active_as_walker: bool | None = None


@router.patch("/partner-applications/{candidate_id}/admin-fields")
@api_router.patch("/partner-applications/{candidate_id}/admin-fields")
def update_partner_application_admin_fields(candidate_id: str, payload: UpdatePartnerApplicationRequest | None = None, admin: User = Depends(require_permission("walkers.validate")), db: Session = Depends(get_db)):
    profile = db.get(WalkerProfile, candidate_id)
    if not profile:
        raise HTTPException(status_code=404, detail="Candidatura nao encontrada")
    data = payload.model_dump(exclude_unset=True) if payload else {}
    if "internal_notes" in data:
        profile.internal_notes = data.get("internal_notes") or ""
    if "status" in data:
        _apply_application_status(profile, data.get("status") or "submitted", data.get("reason"))
    if "reviewed_by_admin_id" in data:
        profile.reviewed_by_admin_id = data.get("reviewed_by_admin_id") or None
    if "resubmission_requested_documents" in data:
        profile.resubmission_requested_documents = _document_key_list(data.get("resubmission_requested_documents") or [])
    if "active_as_walker" in data:
        active_as_walker = bool(data.get("active_as_walker"))
        if active_as_walker and profile.status not in {"approved", "active"}:
            raise HTTPException(status_code=400, detail="Apenas candidatos aprovados podem ser ativados como passeador.")
        _apply_application_status(profile, "active" if active_as_walker else "approved")
        user = db.get(User, profile.user_id)
        if active_as_walker and user:
            user.role = "walker"
        if active_as_walker:
            # Marca referral antes do commit para que tudo persista em uma unica transacao.
            mark_referral_approved(profile.user_id, db, commit=False)
    if any(key in data for key in ("internal_notes", "status", "active_as_walker")):
        event_type = "admin_note_added" if "internal_notes" in data else "status_changed"
        if data.get("active_as_walker"):
            event_type = "approved"
        record_admin_operational_event(
            db,
            event_type=event_type,
            entity_type="walker",
            entity_id=profile.user_id,
            severity="info",
            title="Candidatura atualizada",
            description=data.get("internal_notes") or data.get("reason") or "Campos administrativos da candidatura atualizados.",
            actor=admin,
            source="admin.partner_application.update",
            metadata={"candidate_id": profile.id, "fields": sorted(data.keys())},
        )
    db.commit()
    db.refresh(profile)
    return _serialize_walker_profile(profile, db)


@router.post("/walkers/{walker_id}/approve")
@api_router.post("/walkers/{walker_id}/approve")
def approve_walker(walker_id: str, request: Request, payload: dict | None = None, admin: User = Depends(require_permission("walkers.validate")), db: Session = Depends(get_db)):
    profile = db.get(WalkerProfile, walker_id)
    if not profile:
        raise HTTPException(status_code=404, detail="Passeador nao encontrado")
    # GATE Background Check (Fase 0) — dormente: so age quando a flag de tenant
    # `background_checks` esta LIGADA. Flag OFF (default) => comportamento IDENTICO ao
    # anterior (zero regressao).
    payload = payload or {}
    _bg_user = db.get(User, profile.user_id)
    _bg_tenant_id = getattr(_bg_user, "tenant_id", None) if _bg_user else None
    if is_tenant_feature_enabled(db, "background_checks", tenant_id=_bg_tenant_id):
        certificates = (
            db.query(WalkerBackgroundCertificate)
            .filter(WalkerBackgroundCertificate.walker_profile_id == profile.id)
            .all()
        )
        aggregate = compute_background_status(profile, certificates)
        if aggregate != "verified":
            override = bool(payload.get("override"))
            justification = (payload.get("override_justification") or payload.get("justification") or "").strip()
            if not (override and justification):
                raise HTTPException(
                    status_code=409,
                    detail="Antecedentes nao verificados. Aprovacao bloqueada; envie override=true com justificativa para prosseguir.",
                )
            record_admin_operational_event(
                db,
                event_type="background_gate_override",
                entity_type="walker",
                entity_id=profile.user_id,
                severity="warning",
                title="Aprovacao com override de antecedentes",
                description=justification,
                actor=admin,
                source="admin.walker.approve.override",
                metadata={
                    "candidate_id": profile.id,
                    "background_check_status": aggregate,
                    "justification": justification,
                },
                request=request,
            )
    # Aprovacao em um passo: aprova E ativa operacionalmente (libera o passeador no app).
    # Espelha o que "Ativar operacionalmente" fazia: status=active, role=walker e referral.
    _apply_application_status(profile, "active")
    user = db.get(User, profile.user_id)
    if user:
        user.role = "walker"
    mark_referral_approved(profile.user_id, db, commit=False)
    record_admin_operational_event(
        db,
        event_type="approved",
        entity_type="walker",
        entity_id=profile.user_id,
        severity="info",
        title="Candidatura aprovada",
        description="Candidatura de passeador aprovada e ativada pela administracao.",
        actor=admin,
        source="admin.walker.approve",
        metadata={"candidate_id": profile.id},
        request=request,
    )
    db.commit()
    db.refresh(profile)
    return _serialize_walker_profile(profile, db)

# api-T2: schema permissivo da reprovacao de candidatura (campo unico `reason`).
class RejectWalkerRequest(BaseModel):
    reason: str | None = None


@router.post("/walkers/{walker_id}/reject")
@api_router.post("/walkers/{walker_id}/reject")
def reject_walker(walker_id: str, request: Request, payload: RejectWalkerRequest | None = None, admin: User = Depends(require_permission("walkers.validate")), db: Session = Depends(get_db)):
    profile = db.get(WalkerProfile, walker_id)
    if not profile:
        raise HTTPException(status_code=404, detail="Passeador nao encontrado")
    reason = payload.reason if payload else None
    _apply_application_status(profile, "rejected", reason)
    record_admin_operational_event(
        db,
        event_type="rejected",
        entity_type="walker",
        entity_id=profile.user_id,
        severity="warning",
        title="Candidatura reprovada",
        description=reason or "Candidatura reprovada pela administracao.",
        actor=admin,
        source="admin.walker.reject",
        metadata={"candidate_id": profile.id},
        request=request,
    )
    # Marca referral antes do commit para que tudo persista em uma unica transacao.
    mark_referral_rejected(profile.user_id, profile.rejection_reason, db, commit=False)
    db.commit()
    db.refresh(profile)
    return _serialize_walker_profile(profile, db)

@router.get("/walks")
@api_router.get("/walks")
def walks(
    admin: User = Depends(require_permission("walks.read")),
    db: Session = Depends(get_db),
    limit: int = Query(200, ge=1, le=1000),
    offset: int = Query(0, ge=0),
):
    """Listagem paginada de walks reais do admin.

    Estratégia de performance (item 3 HF):
    - ORDER BY created_at DESC é executado no banco (SQL).
    - _is_real_admin_walk requer critérios em Python (email válido, fake-token em múltiplos
      campos) que não se traduzem trivialmente em SQL sem JOINs pesados. Mantemos o filtro
      em Python, mas eliminamos o N+1 de 2×db.get/walk usando _preload_admin_walk_realness
      (3 queries de batch: users, pets, walker_profiles).
    - has_live_tracking é resolvido em 1 query batch (_batch_live_tracking).
    - A contagem real pode diferir de uma paginação SQL pura porque os fake-tokens são
      avaliados após o fetch; para a página solicitada isso é transparente ao client.
    """
    process_expired_attempts(db)
    scope = get_admin_tenant_scope(admin)
    # Todos os walks do tenant (ordem SQL), sem paginação antecipada pois o filtro de
    # "real" acontece em Python. Para tenants grandes considerar adicionar filtros SQL
    # básicos de role e e-mail via JOIN (fase futura de otimização).
    all_walks = (
        apply_tenant_filter(db.query(Walk), Walk, scope)
        .order_by(Walk.created_at.desc())
        .all()
    )
    # Batch preload: elimina N+1 de db.get(User) e db.get(Pet) por walk
    users_by_id, pets_by_id, profiles_by_user_id = _preload_admin_walk_realness(all_walks, db)
    real_walks = [
        walk
        for walk in all_walks
        if _is_real_admin_walk_preloaded(walk, users_by_id, pets_by_id, profiles_by_user_id)
    ]
    _refresh_reliability_events(real_walks, db)
    paginated = real_walks[offset: offset + limit]
    # Batch live-tracking: 1 query para toda a página
    live_ids = _batch_live_tracking([w.id for w in paginated], db)
    rows = [
        _serialize_admin_walk(walk, db, live_tracking_ids=live_ids, users_by_id=users_by_id, pets_by_id=pets_by_id)
        for walk in paginated
    ]
    return rows

@router.get("/walks/{walk_id}")
@api_router.get("/walks/{walk_id}")
def get_admin_walk(
    walk_id: str,
    admin: User = Depends(require_permission("walks.read")),
    db: Session = Depends(get_db),
):
    walk = db.get(Walk, walk_id)
    if not walk:
        raise HTTPException(status_code=404, detail="Passeio nao encontrado")
    scope = get_admin_tenant_scope(admin)
    ensure_tenant_access(walk.tenant_id, scope)
    return _serialize_admin_walk(walk, db)


# api-T2: schema permissivo da mudanca de status do passeio pelo admin (campo `status`).
class AdminWalkStatusRequest(BaseModel):
    status: str | None = None


@router.patch("/walks/{walk_id}/status")
@api_router.patch("/walks/{walk_id}/status")
def update_admin_walk_status(walk_id: str, payload: AdminWalkStatusRequest, admin: User = Depends(require_permission("walks.update_status")), db: Session = Depends(get_db)):
    walk = db.get(Walk, walk_id)

    if not walk:
        raise HTTPException(status_code=404, detail="Passeio nao encontrado")

    status = payload.status

    if not status:
        raise HTTPException(status_code=400, detail="Status nao informado")

    operational_status_by_label = {
        "pending_walker_confirmation": "pending_walker_confirmation",
        "walker_confirmation_pending": "pending_walker_confirmation",

        "walker_accepted": "walker_accepted",
        "walker_confirmed": "walker_accepted",

        "walker_declined": "walker_declined",

        "auto_rematching": "auto_rematching",
        "matching_walkers": "auto_rematching",

        "no_walker_found": "no_walker_found",

        "awaiting_tutor_reconfirmation": "awaiting_tutor_reconfirmation",

        "ride_scheduled": "ride_scheduled",

        "walker_arriving": "walker_arriving",
        "walker_heading_to_pickup": "walker_arriving",

        "ride_in_progress": "ride_in_progress",
        "ride_completed": "ride_completed",

        "ride_cancelled": "ride_cancelled",
        "cancelled": "ride_cancelled",
    }

    status_label_by_operational_status = {
        "pending_walker_confirmation": "Confirmando disponibilidade do passeador",
        "walker_accepted": "Passeador confirmado",
        "walker_declined": "Passeador recusou o passeio",
        "auto_rematching": "Buscando substituto",
        "no_walker_found": "Nenhum passeador encontrado",
        "awaiting_tutor_reconfirmation": "Aguardando confirmação do tutor",
        "ride_scheduled": "Agendado",
        "walker_arriving": "Passeador a caminho",
        "ride_in_progress": "Passeio em andamento",
        "ride_completed": "Passeio finalizado",
        "ride_cancelled": "Cancelado",
    }

    next_operational_status = operational_status_by_label.get(status, status)
    if status in DIRECT_COMPLETION_STATUSES or next_operational_status in DIRECT_COMPLETION_STATUSES:
        raise HTTPException(status_code=400, detail="Finalização deve ocorrer via revisão operacional.")

    previous_operational_status = walk.operational_status

    walk.operational_status = next_operational_status
    walk.status = status_label_by_operational_status.get(next_operational_status, status)
    record_late_cancellation_if_applicable(walk, db)

    log_event(
        db,
        walk.id,
        walk.operational_status,
        actor_type="admin",
        metadata={
            "source": "admin_panel",
            "previous_operational_status": previous_operational_status,
            "status": walk.status,
            "operational_status": walk.operational_status,
        },
    )
    record_admin_operational_event(
        db,
        event_type="status_changed",
        entity_type="walk",
        entity_id=walk.id,
        severity="info",
        title="Status do passeio alterado",
        description=f"{previous_operational_status or ''} -> {walk.operational_status}",
        actor=admin,
        source="admin.walk.status",
        metadata={"previous_operational_status": previous_operational_status, "status": walk.status},
    )

    notification_copy_by_status = {
        "pending_walker_confirmation": {
            "title": "Estamos confirmando seu passeio",
            "message": "Estamos confirmando a disponibilidade do passeador para o passeio do seu pet.",
            "priority": "medium",
        },
        "walker_accepted": {
            "title": "Passeador confirmado",
            "message": "O passeador aceitou o passeio do seu pet.",
            "priority": "medium",
        },
        "walker_declined": {
            "title": "Passeador indisponível",
            "message": "O passeador não pôde atender este passeio. Estamos avaliando a melhor alternativa.",
            "priority": "high",
        },
        "auto_rematching": {
            "title": "Buscando substituto",
            "message": "Estamos buscando outro passeador disponível para manter seu passeio.",
            "priority": "high",
        },
        "no_walker_found": {
            "title": "Nenhum passeador encontrado",
            "message": "Ainda não encontramos um passeador disponível para este horário. Nossa equipe pode orientar os próximos passos.",
            "priority": "high",
        },
        "awaiting_tutor_reconfirmation": {
            "title": "Confirme seu passeio",
            "message": "Precisamos que você confirme se deseja continuar a busca, reagendar ou cancelar sem custo.",
            "priority": "high",
        },
        "ride_scheduled": {
            "title": "Passeio agendado",
            "message": "Seu passeio está agendado e pronto para acompanhamento.",
            "priority": "medium",
        },
        "walker_arriving": {
            "title": "Passeador a caminho",
            "message": "O passeador está a caminho para buscar seu pet.",
            "priority": "high",
        },
        "ride_in_progress": {
            "title": "Passeio iniciado",
            "message": "O passeio do seu pet está em andamento.",
            "priority": "high",
        },
        "ride_completed": {
            "title": "Passeio finalizado",
            "message": "O passeio do seu pet foi finalizado.",
            "priority": "medium",
        },
        "ride_cancelled": {
            "title": "Passeio cancelado",
            "message": "O passeio foi cancelado.",
            "priority": "high",
        },
    }

    notification_copy = notification_copy_by_status.get(walk.operational_status)

    if notification_copy and walk.tutor_id:
        _create_notification(
            db,
            NotificationCreate(
                user_id=walk.tutor_id,
                user_role="tutor",
                title=notification_copy["title"],
                message=notification_copy["message"],
                type="walk_status",
                related_entity_type="walk",
                related_entity_id=walk.id,
                metadata={
                    "priority": notification_copy["priority"],
                    "channel": "in_app",
                    "action": walk.operational_status,
                    "previous_operational_status": previous_operational_status,
                    "status": walk.status,
                },
            ),
        )

    db.commit()
    db.refresh(walk)

    return _serialize_admin_walk(walk, db)

@router.post("/walks/{walk_id}/recovery")
@api_router.post("/walks/{walk_id}/recovery")
def recover_walk(walk_id: str, admin: User = Depends(require_permission("walks.recover")), db: Session = Depends(get_db)):
    walk = db.get(Walk, walk_id)

    if not walk:
        raise HTTPException(status_code=404, detail="Passeio nao encontrado")

    process_expired_attempts(db)

    walk.walker_id = None
    walk.assigned_walker_id = None
    walk.operational_status = "awaiting_tutor_reconfirmation"
    walk.status = "Aguardando confirmação do tutor"
    walk.confirmation_expires_at = None
    walk.matching_finished_at = None
    walk.no_walker_reason = (
        "Recuperacao operacional iniciada pelo admin. "
        "Aguardando o tutor confirmar se deseja continuar a busca, alterar horario ou cancelar sem custo."
    )

    log_event( 
        db,
        walk.id,
        "awaiting_tutor_reconfirmation",
        actor_type="admin",
        metadata={
            "source": "admin_panel",
            "reason": walk.no_walker_reason,
            "available_options": ["continue_search", "reschedule", "cancel_without_fee"],
        },
    )
    record_operational_recovery(walk, db)
    record_operational_log(
        db,
        event_type="operational_recovery_triggered",
        severity="warning",
        source="admin.recovery",
        message="Recovery operacional acionado pelo admin.",
        context={"walk_id": walk.id, "status": walk.operational_status},
    )
    record_admin_operational_event(
        db,
        event_type="recovered",
        entity_type="walk",
        entity_id=walk.id,
        severity="high",
        title="Recovery operacional iniciado",
        description=walk.no_walker_reason,
        actor=admin,
        source="admin.walk.recovery",
        metadata={"status": walk.operational_status},
    )

    _create_notification(
        db,
        NotificationCreate(
            user_id=walk.tutor_id,
            user_role="tutor",
            title="Confirme seu passeio",
            message=(
                "Encontramos uma situação operacional neste passeio. "
                "Você pode continuar a busca por um passeador, reagendar ou cancelar sem custo."
            ),
            type="walk_recovery",
            related_entity_type="walk",
            related_entity_id=walk.id,
            metadata={
                "priority": "high",
                "channel": "in_app",
                "action": "awaiting_tutor_reconfirmation",
                "available_options": ["continue_search", "reschedule", "cancel_without_fee"],
            },
        ),
    )

    db.commit()
    db.refresh(walk)

    return _serialize_admin_walk(walk, db)

@router.get("/payments")
@api_router.get("/payments")
def payments(
    admin: User = Depends(require_permission("finance.read")),
    db: Session = Depends(get_db),
    limit: int = Query(200, ge=1, le=1000),
    offset: int = Query(0, ge=0),
):
    # require_permission convive com o require_admin do router durante a migração.
    query = apply_tenant_filter(db.query(Payment), Payment, get_admin_tenant_scope(admin))
    rows = query.order_by(Payment.created_at.desc()).offset(offset).limit(limit).all()
    # Batch preload (3 queries) — elimina o N+1 de 3×db.get por pagamento.
    walks_by_id, tutors_by_id, pets_by_id = _preload_admin_payment_refs(rows, db)
    return [
        _serialize_admin_payment(payment, db, walks_by_id, tutors_by_id, pets_by_id)
        for payment in rows
    ]


@router.get("/payments/{payment_id}")
@api_router.get("/payments/{payment_id}")
def get_admin_payment(
    payment_id: str,
    admin: User = Depends(require_permission("finance.read")),
    db: Session = Depends(get_db),
):
    payment = db.get(Payment, payment_id)
    if not payment:
        raise HTTPException(status_code=404, detail="Pagamento nao encontrado")
    scope = get_admin_tenant_scope(admin)
    ensure_tenant_access(payment.tenant_id, scope)
    return _serialize_admin_payment(payment, db)


@router.get("/walk-completions/pending")
@api_router.get("/walk-completions/pending")
def pending_walk_completions(admin: User = Depends(require_permission("walks.read")), db: Session = Depends(get_db)):
    rows = apply_tenant_filter(
        db.query(WalkCompletionReview), WalkCompletionReview, get_admin_tenant_scope(admin)
    ).filter(
        WalkCompletionReview.status == "pending_review"
    ).order_by(WalkCompletionReview.created_at.desc()).all()
    return {
        "items": [_serialize_walk_completion_review(row, db) for row in rows],
        "total": len(rows),
    }


# api-T2: schema permissivo das decisoes de finalizacao. Campos opcionais (admin_note/
# note/reason) espelhando os (payload or {}).get; Pydantic v2 ignora extras. Nenhum
# payload legitimo e rejeitado — ganho de validacao de tipo e contrato OpenAPI.
class WalkCompletionDecisionRequest(BaseModel):
    admin_note: str | None = None
    note: str | None = None
    reason: str | None = None


@router.post("/walk-completions/{review_id}/approve")
@api_router.post("/walk-completions/{review_id}/approve")
def approve_walk_completion(review_id: str, payload: WalkCompletionDecisionRequest | None = None, admin: User = Depends(require_permission("walks.update_status")), db: Session = Depends(get_db)):
    review = db.get(WalkCompletionReview, review_id)
    if not review:
        record_operational_log(
            db,
            event_type="completion_approve_failed",
            severity="warning",
            source="admin.approve_completion",
            message="Tentativa de aprovar finalização inexistente.",
            context={"review_id": review_id, "admin_id": admin.id},
        )
        db.commit()
        raise HTTPException(status_code=404, detail="Revisao de finalizacao nao encontrada.")
    walk = db.get(Walk, review.walk_id)
    if not walk:
        record_operational_log(
            db,
            event_type="completion_approve_failed",
            severity="error",
            source="admin.approve_completion",
            message="Finalização sem passeio associado para aprovação.",
            context={"review_id": review.id, "walk_id": review.walk_id, "admin_id": admin.id},
        )
        db.commit()
        raise HTTPException(status_code=404, detail="Passeio nao encontrado.")
    try:
        _ensure_completion_review_can_transition(review, "approve")
    except HTTPException as exc:
        record_operational_log(
            db,
            event_type="completion_approve_blocked",
            severity="warning",
            source="admin.approve_completion",
            message=str(exc.detail),
            context={"review_id": review.id, "walk_id": walk.id, "status": review.status, "admin_id": admin.id},
        )
        db.commit()
        raise

    now = datetime.utcnow()
    review.status = "approved"
    review.admin_note = (payload.admin_note or payload.note) if payload else None
    review.reviewed_by_admin_id = admin.id
    review.reviewed_at = now
    review.updated_at = now
    walk.operational_status = "ride_completed"
    walk.status = "Finalizado"
    walk.matching_finished_at = walk.matching_finished_at or now
    _ensure_internal_walk_payment(walk, db)
    log_event(db, walk.id, "completion_review_approved", actor_type="admin", actor_id=admin.id, metadata={"review_id": review.id})
    record_admin_operational_event(
        db,
        event_type="finalization_approved",
        entity_type="finalization",
        entity_id=review.id,
        severity="info",
        title="Finalizacao aprovada",
        description=review.admin_note or "Finalizacao aprovada pela revisao operacional.",
        actor=admin,
        source="admin.finalization.approve",
        metadata={"walk_id": walk.id, "walker_user_id": review.walker_user_id},
    )
    tutor = db.get(User, walk.tutor_id) if walk.tutor_id else None
    if tutor:
        _create_notification(
            db,
            NotificationCreate(
                user_id=tutor.id,
                user_role=tutor.role,
                title="Passeio finalizado com sucesso",
                message="A finalização do passeio foi validada pela equipe operacional. Evidências e resumo já estão disponíveis; você também pode avaliar o passeio e enviar uma gorjeta opcional.",
                type="walk_completion_review_approved",
                related_entity_type="walk",
                related_entity_id=walk.id,
                metadata={
                    "walk_id": walk.id,
                    "review_id": review.id,
                    "priority": "normal",
                    "channel": "in_app",
                },
            ),
        )
    walker_id = review.walker_user_id or walk.walker_id
    walker = db.get(User, walker_id) if walker_id else None
    if walker:
        _create_notification(
            db,
            NotificationCreate(
                user_id=walker.id,
                user_role=walker.role,
                title="Pagamento operacional liberado",
                message="A finalização do passeio foi aprovada pela revisão operacional. O pagamento operacional foi liberado para o seu extrato.",
                type="walk_payment_released",
                related_entity_type="walk",
                related_entity_id=walk.id,
                metadata={
                    "walk_id": walk.id,
                    "review_id": review.id,
                    "payment_provider": "internal",
                    "priority": "normal",
                    "channel": "in_app",
                },
            ),
        )
    db.commit()
    db.refresh(review)
    db.refresh(walk)
    return {"ok": True, "review": _serialize_walk_completion_review(review, db), "walk": serialize_operational_walk(walk, db)}


@router.post("/walk-completions/{review_id}/reject")
@api_router.post("/walk-completions/{review_id}/reject")
def reject_walk_completion(review_id: str, payload: WalkCompletionDecisionRequest | None = None, admin: User = Depends(require_permission("walks.update_status")), db: Session = Depends(get_db)):
    review = db.get(WalkCompletionReview, review_id)
    if not review:
        record_operational_log(
            db,
            event_type="completion_reject_failed",
            severity="warning",
            source="admin.reject_completion",
            message="Tentativa de rejeitar finalização inexistente.",
            context={"review_id": review_id, "admin_id": admin.id},
        )
        db.commit()
        raise HTTPException(status_code=404, detail="Revisao de finalizacao nao encontrada.")
    walk = db.get(Walk, review.walk_id)
    if not walk:
        record_operational_log(
            db,
            event_type="completion_reject_failed",
            severity="error",
            source="admin.reject_completion",
            message="Finalização sem passeio associado para rejeição.",
            context={"review_id": review.id, "walk_id": review.walk_id, "admin_id": admin.id},
        )
        db.commit()
        raise HTTPException(status_code=404, detail="Passeio nao encontrado.")
    try:
        _ensure_completion_review_can_transition(review, "reject")
    except HTTPException as exc:
        record_operational_log(
            db,
            event_type="completion_reject_blocked",
            severity="warning",
            source="admin.reject_completion",
            message=str(exc.detail),
            context={"review_id": review.id, "walk_id": walk.id, "status": review.status, "admin_id": admin.id},
        )
        db.commit()
        raise

    now = datetime.utcnow()
    review.status = "rejected"
    review.admin_note = ((payload.admin_note or payload.reason) if payload else None) or "Finalizacao rejeitada pela revisao administrativa."
    review.reviewed_by_admin_id = admin.id
    review.reviewed_at = now
    review.updated_at = now
    walk.operational_status = "completion_rejected"
    walk.status = "Finalização rejeitada"
    log_event(db, walk.id, "completion_review_rejected", actor_type="admin", actor_id=admin.id, metadata={"review_id": review.id})
    record_admin_operational_event(
        db,
        event_type="finalization_rejected",
        entity_type="finalization",
        entity_id=review.id,
        severity="warning",
        title="Finalizacao rejeitada",
        description=review.admin_note or "Finalizacao rejeitada pela revisao operacional.",
        actor=admin,
        source="admin.finalization.reject",
        metadata={"walk_id": walk.id, "walker_user_id": review.walker_user_id},
    )
    walker = db.get(User, review.walker_user_id) if review.walker_user_id else None
    if walker:
        admin_note = review.admin_note.strip() if review.admin_note else ""
        message = "A finalização do passeio foi rejeitada pela revisão operacional. Ajuste as informações e reenvie a finalização."
        if admin_note:
            message = f"{message} Motivo: {admin_note}"
        _create_notification(
            db,
            NotificationCreate(
                user_id=walker.id,
                user_role=walker.role,
                title="Finalização precisa de ajuste",
                message=message,
                type="walk_completion_review_rejected",
                related_entity_type="walk_completion_review",
                related_entity_id=review.id,
                metadata={
                    "walk_id": walk.id,
                    "review_id": review.id,
                    "priority": "high",
                    "channel": "in_app",
                },
            ),
        )
    db.commit()
    db.refresh(review)
    db.refresh(walk)
    return {"ok": True, "review": _serialize_walk_completion_review(review, db), "walk": serialize_operational_walk(walk, db)}


@router.get("/walker-kits/pending")
@api_router.get("/walker-kits/pending")
def pending_walker_kits(db: Session = Depends(get_db)):
    rows = db.query(WalkerKitSubmission).filter(
        WalkerKitSubmission.audit_status == "pending_review"
    ).order_by(WalkerKitSubmission.updated_at.desc()).all()
    return {
        "items": [_serialize_walker_kit_submission(row, db) for row in rows],
        "total": len(rows),
    }


@router.post("/walker-kits/{submission_id}/approve")
@api_router.post("/walker-kits/{submission_id}/approve")
def approve_walker_kit(submission_id: str, admin: User = Depends(require_permission("walkers.validate")), db: Session = Depends(get_db)):
    submission = db.query(WalkerKitSubmission).filter(WalkerKitSubmission.id == submission_id).first()
    if not submission:
        raise HTTPException(status_code=404, detail="Envio de kit nao encontrado.")

    now = datetime.utcnow()
    submission.audit_status = "approved"
    submission.audit_note = "Kit aprovado pela auditoria administrativa."
    submission.reviewed_by_admin_id = admin.id
    submission.reviewed_at = now
    submission.updated_at = now
    record_admin_operational_event(
        db,
        event_type="approved",
        entity_type="kit",
        entity_id=submission.id,
        severity="info",
        title="Kit aprovado",
        description=submission.audit_note,
        actor=admin,
        source="admin.kit.approve",
        metadata={"walker_user_id": submission.walker_user_id},
    )
    db.commit()
    db.refresh(submission)
    return _serialize_walker_kit_submission(submission, db)


# api-T2: schema permissivo da rejeicao de kit (audit_note/reason opcionais).
class RejectWalkerKitRequest(BaseModel):
    audit_note: str | None = None
    reason: str | None = None


@router.post("/walker-kits/{submission_id}/reject")
@api_router.post("/walker-kits/{submission_id}/reject")
def reject_walker_kit(submission_id: str, payload: RejectWalkerKitRequest | None = None, admin: User = Depends(require_permission("walkers.validate")), db: Session = Depends(get_db)):
    submission = db.query(WalkerKitSubmission).filter(WalkerKitSubmission.id == submission_id).first()
    if not submission:
        raise HTTPException(status_code=404, detail="Envio de kit nao encontrado.")

    now = datetime.utcnow()
    submission.audit_status = "rejected"
    submission.audit_note = ((payload.audit_note or payload.reason) if payload else None) or "Kit rejeitado pela auditoria administrativa."
    submission.reviewed_by_admin_id = admin.id
    submission.reviewed_at = now
    submission.updated_at = now
    record_admin_operational_event(
        db,
        event_type="rejected",
        entity_type="kit",
        entity_id=submission.id,
        severity="warning",
        title="Kit rejeitado",
        description=submission.audit_note,
        actor=admin,
        source="admin.kit.reject",
        metadata={"walker_user_id": submission.walker_user_id},
    )
    db.commit()
    db.refresh(submission)
    return _serialize_walker_kit_submission(submission, db)


@router.get("/walker-operations")
def walker_operations(admin: User = Depends(require_permission("walkers.read")), db: Session = Depends(get_db)):
    scope = get_admin_tenant_scope(admin)
    # F09: WalkerProfile não possui tenant_id — walkers são globais da plataforma
    # (conforme comentário em _sql_count_real_active_walkers e endpoint /walkers).
    # Coerente com todos os outros endpoints que listam walkers sem filtro de tenant.
    walkers = db.query(WalkerProfile).all()
    pending_walks = apply_tenant_filter(db.query(Walk), Walk, scope).filter(Walk.walker_id.is_(None), Walk.status == "Agendado").all()
    active_walks = apply_tenant_filter(db.query(Walk), Walk, scope).filter(Walk.status.in_(["Indo buscar o pet", "Passeando agora"])).all()
    withdrawals = apply_tenant_filter(db.query(Payment), Payment, scope).filter(Payment.provider == "pix").all()
    return {
        "walkers": walkers,
        "pending_requests": pending_walks,
        "active_walks": active_walks,
        "withdrawals": withdrawals,
        "metrics": {
            "pending_approvals": db.query(WalkerProfile).filter(WalkerProfile.status == "pending").count(),
            "approved_walkers": db.query(WalkerProfile).filter(WalkerProfile.status == "approved").count(),
            "available_requests": len(pending_walks),
            "active_walks": len(active_walks),
            "pending_withdrawals": len([item for item in withdrawals if item.status == "pending"]),
        },
    }


@router.get("/referral-program/settings")
@api_router.get("/referral-program/settings")
def referral_program_settings(admin: User = Depends(require_permission("admin.access")), db: Session = Depends(get_db)):
    scope = get_admin_tenant_scope(admin)
    # super_admin sem act-as-tenant edita a global (tenant_id=None)
    tenant_id = scope.tenant_id if not scope.is_global else None
    return get_setting(db, "referral_program", DEFAULT_REFERRAL_PROGRAM_SETTINGS, tenant_id=tenant_id)


@router.put("/referral-program/settings")
@api_router.put("/referral-program/settings")
def update_referral_program_settings(payload: dict, admin: User = Depends(require_permission("admin.access")), db: Session = Depends(get_db)):
    scope = get_admin_tenant_scope(admin)
    tenant_id = scope.tenant_id if not scope.is_global else None
    current = get_setting(db, "referral_program", DEFAULT_REFERRAL_PROGRAM_SETTINGS, tenant_id=tenant_id)
    merged = _merge_dict(current, payload or {})
    merged["updated_at"] = _now()
    merged["updated_by"] = "admin"
    save_setting(db, "referral_program", merged, updated_by="admin", tenant_id=tenant_id)
    return merged


@router.get("/referrals")
def referrals(limit: int = 20):
    items = REFERRAL_RECORDS[: max(0, limit)]
    return {"items": items, "total": len(REFERRAL_RECORDS)}


# api-T2: schema permissivo da mudanca de status de indicacao (demo in-memory).
class ReferralStatusRequest(BaseModel):
    status: str | None = None
    note: str = ""


@router.post("/referrals/{referral_id}/status")
def update_referral_status(referral_id: str, payload: ReferralStatusRequest):
    status = payload.status
    note = payload.note or ""
    for item in REFERRAL_RECORDS:
        if item["id"] == referral_id:
            item["status"] = status or item["status"]
            if status == "invalida_fraude":
                item["fraud_flags"] = [note or "Marcado manualmente pelo admin"]
            return item
    return {"id": referral_id, "status": status, "note": note}


@router.get("/walker-programs")
@api_router.get("/walker-programs")
def walker_programs(admin: User = Depends(require_permission("admin.access")), db: Session = Depends(get_db)):
    scope = get_admin_tenant_scope(admin)
    tenant_id = scope.tenant_id if not scope.is_global else None
    rows = _walker_program_rows(db)
    return {
        "settings": get_setting(db, "walker_program", DEFAULT_WALKER_PROGRAM_SETTINGS, tenant_id=tenant_id),
        "metrics": _walker_program_metrics(rows),
        "walkers": rows,
        # F02: fila real de gorjetas sob revisão — WalkTip com status "pending_review".
        # Sem registros reais → lista vazia (honesto).
        "tips_review_queue": [
            {
                "id": tip.id,
                "walker_id": tip.walker_id,
                "walker_name": next(
                    (r["name"] for r in rows if r["user_id"] == tip.walker_id), "—"
                ),
                "amount": float(tip.amount or 0),
                "reason": "Gorjeta aguardando revisao.",
                "status": tip.status,
            }
            for tip in db.query(WalkTip).filter(WalkTip.status == "pending_review").all()
        ],
        "actions": recent_walker_program_actions(db, limit=20),
    }


@router.put("/walker-programs/settings")
@api_router.put("/walker-programs/settings")
def update_walker_program_settings(payload: dict, admin: User = Depends(require_permission("admin.access")), db: Session = Depends(get_db)):
    scope = get_admin_tenant_scope(admin)
    tenant_id = scope.tenant_id if not scope.is_global else None
    current = get_setting(db, "walker_program", DEFAULT_WALKER_PROGRAM_SETTINGS, tenant_id=tenant_id)
    merged = _merge_dict(current, payload or {})
    merged["updated_at"] = _now()
    merged["updated_by"] = "admin"
    save_setting(db, "walker_program", merged, updated_by="admin", tenant_id=tenant_id)
    return merged


# api-T2: schema permissivo do ajuste de CR do passeador.
class AdjustWalkerCrRequest(BaseModel):
    amount: int = 0
    reason: str = "Ajuste administrativo"


@router.post("/walker-programs/walkers/{walker_id}/cr")
def adjust_walker_cr(walker_id: str, payload: AdjustWalkerCrRequest, db: Session = Depends(get_db)):
    action = {
        "id": str(uuid4()),
        "type": "cr_adjustment",
        "walker_id": walker_id,
        "amount": int(payload.amount),
        "reason": payload.reason or "Ajuste administrativo",
        "created_at": _now(),
    }
    append_walker_program_action(db, action_type="cr", walker_id=walker_id, payload=action)
    return {"ok": True, "action": action}


# api-T2: schema permissivo da auditoria de kit (defaults preservados).
class KitAuditActionRequest(BaseModel):
    status: str = "aprovado"
    note: str = ""


@router.post("/walker-programs/walkers/{walker_id}/kit-audit")
def audit_walker_kit(walker_id: str, payload: KitAuditActionRequest, db: Session = Depends(get_db)):
    action = {
        "id": str(uuid4()),
        "type": "kit_audit",
        "walker_id": walker_id,
        "status": payload.status,
        "note": payload.note,
        "created_at": _now(),
    }
    append_walker_program_action(db, action_type="kit", walker_id=walker_id, payload=action)
    return {"ok": True, "action": action}


# api-T2: schema permissivo da revisao de gorjeta (defaults preservados).
class TipReviewActionRequest(BaseModel):
    status: str = "approved"
    note: str = ""


@router.post("/walker-programs/tips/{tip_id}/review")
def review_tip(tip_id: str, payload: TipReviewActionRequest, db: Session = Depends(get_db)):
    action = {
        "id": str(uuid4()),
        "type": "tip_review",
        "tip_id": tip_id,
        "status": payload.status,
        "note": payload.note,
        "created_at": _now(),
    }
    append_walker_program_action(db, action_type="tip", walker_id=None, payload=action)
    return {"ok": True, "action": action}

@router.post("/withdrawals/{payment_id}/approve")
def approve_withdrawal(payment_id: str, admin: User = Depends(require_permission("finance.manage")), db: Session = Depends(get_db)):
    payment = db.get(Payment, payment_id)
    if payment:
        # B-02b: guard — só Payment rows criadas via /walker/withdrawals são saques.
        # Discriminador canônico: provider == "pix" (walker.py:2791 cria com provider="pix").
        if payment.provider != "pix":
            raise HTTPException(status_code=400, detail="Payment nao e um saque de passeador.")
        # Isolamento multi-tenant: admin de tenant não aprova saque de outro tenant.
        ensure_tenant_access(payment.tenant_id, get_admin_tenant_scope(admin))
        payment.status = "paid"
        record_admin_operational_event(
            db,
            event_type="payout_approved",
            entity_type="payment",
            entity_id=payment.id,
            severity="info",
            title="Saque aprovado",
            description="Saque aprovado pela operacao administrativa.",
            actor=admin,
            source="admin.withdrawal.approve",
            metadata={"walk_id": payment.walk_id, "provider": payment.provider},
        )
        db.commit()
    return {"ok": True}

@router.post("/withdrawals/{payment_id}/reject")
def reject_withdrawal(payment_id: str, admin: User = Depends(require_permission("finance.manage")), db: Session = Depends(get_db)):
    payment = db.get(Payment, payment_id)
    if payment:
        # B-02b: guard — mesmo discriminador do approve.
        if payment.provider != "pix":
            raise HTTPException(status_code=400, detail="Payment nao e um saque de passeador.")
        # Isolamento multi-tenant: admin de tenant não rejeita saque de outro tenant.
        ensure_tenant_access(payment.tenant_id, get_admin_tenant_scope(admin))
        payment.status = "rejected"
        record_admin_operational_event(
            db,
            event_type="payout_rejected",
            entity_type="payment",
            entity_id=payment.id,
            severity="warning",
            title="Saque rejeitado",
            description="Saque rejeitado pela operacao administrativa.",
            actor=admin,
            source="admin.withdrawal.reject",
            metadata={"walk_id": payment.walk_id, "provider": payment.provider},
        )
        db.commit()
    return {"ok": True}


# ---------------------------------------------------------------------------
# Configuração de carteira Asaas por walker (split real — dormente no sandbox)
# ---------------------------------------------------------------------------

# api-T2: schema permissivo da config de carteira Asaas. asaas_wallet_id e Any (aceita
# string ou null); usamos model_fields_set para exigir que a chave seja ENVIADA (mesmo que
# null para limpar) — mesma semantica do `"asaas_wallet_id" not in payload` anterior.
class SetWalkerWalletRequest(BaseModel):
    asaas_wallet_id: Any | None = None


@router.patch("/walkers/{user_id}/wallet")
@api_router.patch("/walkers/{user_id}/wallet")
def set_walker_wallet(
    user_id: str,
    payload: SetWalkerWalletRequest,
    admin: User = Depends(require_permission("finance.manage")),
    db: Session = Depends(get_db),
):
    """Configura o asaas_wallet_id de um walker para split real no modo live.

    Body: {"asaas_wallet_id": "<id>"} para configurar, ou {"asaas_wallet_id": null} para limpar.
    Requer permissão finance.manage.
    """
    profile = db.query(WalkerProfile).filter(WalkerProfile.user_id == user_id).first()
    if not profile:
        raise HTTPException(status_code=404, detail="Perfil de walker nao encontrado para este user_id.")

    if "asaas_wallet_id" not in payload.model_fields_set:
        raise HTTPException(status_code=422, detail="Campo 'asaas_wallet_id' obrigatorio no body.")

    new_wallet_id = payload.asaas_wallet_id
    if new_wallet_id is not None:
        new_wallet_id = str(new_wallet_id).strip()
        if not new_wallet_id:
            raise HTTPException(status_code=422, detail="'asaas_wallet_id' nao pode ser string vazia. Use null para limpar.")

    old_wallet_id = profile.asaas_wallet_id
    profile.asaas_wallet_id = new_wallet_id or None
    db.add(profile)

    try:
        record_audit_log(
            db,
            action="walker_profile.wallet_updated",
            entity_type="walker_profile",
            entity_id=profile.id,
            actor=admin,
            before={"asaas_wallet_id": old_wallet_id},
            after={"asaas_wallet_id": profile.asaas_wallet_id},
            tenant_id=None,
        )
    except Exception as _audit_exc:  # F17: loga em vez de silenciar
        _logger.warning("Falha ao registrar audit log de wallet update: %s", _audit_exc)

    db.commit()
    db.refresh(profile)
    return {
        "ok": True,
        "user_id": user_id,
        "asaas_wallet_id": profile.asaas_wallet_id,
    }
