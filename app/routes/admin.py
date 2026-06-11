import json
from app.models.tutor_profile import TutorProfile
from fastapi import APIRouter, Depends, Query, Request
from fastapi import HTTPException
from datetime import datetime, timedelta
from uuid import uuid4

from sqlalchemy import inspect
from sqlalchemy.orm import Session
from app.core.database import get_db
from app.services.app_settings_service import (
    append_walker_program_action,
    get_setting,
    recent_walker_program_actions,
    save_setting,
)
from app.dependencies.rbac import require_permission
from app.services.audit_service import record_audit_log
from app.services.payment_split_service import get_or_create_payment_config, update_payment_config
from app.services.tenant_context import resolve_current_tenant_id
from app.schemas.tenant_payment_config import TenantPaymentConfigResponse, TenantPaymentConfigUpdate
from app.dependencies.tenant_scope import apply_tenant_filter, get_admin_tenant_scope
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
from app.services.walker_operational_score_service import calculate_walker_operational_score
from app.routes.notifications import NotificationCreate, _create_notification
from app.services.signed_uploads import create_signed_upload_url

router = APIRouter(prefix="/admin", tags=["admin"], dependencies=[Depends(require_permission("admin.access"))])
api_router = APIRouter(prefix="/api/admin", tags=["admin"], dependencies=[Depends(require_permission("admin.access"))])

APPROVED_WALKER_STATUSES = {"active"}
PAID_PAYMENT_STATUSES = {"paid", "Pago", "pagamento_confirmado_sandbox", "payment_confirmed", "confirmed"}
IN_PROGRESS_WALK_STATUSES = {"Indo buscar o pet", "Passeando agora", "walker_arriving", "ride_in_progress"}
DIRECT_COMPLETION_STATUSES = {"ride_completed", "Finalizado", "finalizado", "completed", "finished"}
COMPLETION_REVIEW_MUTABLE_STATUSES = {"pending", "pending_review", "under_review"}
COMPLETION_REVIEW_APPROVED_STATUSES = {"approved"}
COMPLETION_REVIEW_REJECTED_STATUSES = {"rejected", "completion_rejected"}

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


@router.post("/operational-events")
@api_router.post("/operational-events")
def create_operational_event(payload: dict, admin: User = Depends(require_permission("alerts.resolve")), db: Session = Depends(get_db)):
    data = _validate_operational_event_payload(payload or {})
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


def _serialize_walker_kit_submission(submission: WalkerKitSubmission, db: Session) -> dict:
    profile = db.query(WalkerProfile).filter(WalkerProfile.user_id == submission.walker_user_id).first()
    user = db.query(User).filter(User.id == submission.walker_user_id).first()
    return {
        "id": submission.id,
        "walker_user_id": submission.walker_user_id,
        "walker_name": profile.full_name if profile and profile.full_name else user.full_name if user else "",
        "items_json": submission.items_json,
        "audit_status": submission.audit_status,
        "audit_note": submission.audit_note,
        "reviewed_by_admin_id": submission.reviewed_by_admin_id,
        "reviewed_at": submission.reviewed_at.isoformat() if submission.reviewed_at else None,
        "created_at": submission.created_at.isoformat() if submission.created_at else None,
        "updated_at": submission.updated_at.isoformat() if submission.updated_at else None,
    }


def _walk_completion_checklist(review: WalkCompletionReview) -> dict:
    if not review.checklist_json:
        return {}
    try:
        parsed = json.loads(review.checklist_json)
        return parsed if isinstance(parsed, dict) else {}
    except (TypeError, ValueError):
        return {}


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


def _serialize_walk_completion_review(review: WalkCompletionReview, db: Session) -> dict:
    walk = db.get(Walk, review.walk_id)
    walker = db.get(User, review.walker_user_id)
    tutor = db.get(User, review.tutor_user_id)
    pet = db.get(Pet, walk.pet_id) if walk else None
    return {
        "id": review.id,
        "walk_id": review.walk_id,
        "walker_user_id": review.walker_user_id,
        "walker_name": walker.full_name if walker else "",
        "walker_email": walker.email if walker else "",
        "tutor_user_id": review.tutor_user_id,
        "tutor_name": tutor.full_name if tutor else "",
        "tutor_email": tutor.email if tutor else "",
        "pet_name": pet.name if pet else "",
        "scheduled_date": walk.scheduled_date if walk else None,
        "photo_url": review.photo_url,
        "notes": review.notes,
        "checklist": _walk_completion_checklist(review),
        "status": review.status,
        "admin_note": review.admin_note,
        "reviewed_by_admin_id": review.reviewed_by_admin_id,
        "reviewed_at": review.reviewed_at.isoformat() if review.reviewed_at else None,
        "created_at": review.created_at.isoformat() if review.created_at else None,
        "updated_at": review.updated_at.isoformat() if review.updated_at else None,
    }


def _ensure_internal_walk_payment(walk: Walk, db: Session):
    existing_paid = db.query(Payment).filter(
        Payment.walk_id == walk.id,
        Payment.status.in_(PAID_PAYMENT_STATUSES),
    ).first()
    if existing_paid:
        return existing_paid
    payment = Payment(
        id=str(uuid4()),
        tutor_id=walk.tutor_id,
        walk_id=walk.id,
        amount=float(walk.price or 0),
        status="paid",
        provider="internal",
    )
    db.add(payment)
    return payment


TUTOR_RECONFIRMATION_STATUSES = {
    "awaiting_tutor_reconfirmation",
}

FAKE_ENTITY_TOKENS = (
    "passeador fluxo real",
    "passeador login",
    "passeador ativado",
    "passeador auditoria",
    "passeador docs",
    "auditoria real",
    "fluxo real",
    "login",
    "docs",
    "teste",
    "test",
    "demo",
    "mock",
    "fallback",
    "sample",
    "seed",
    "local",
    "auditoria",
    "ficticio",
    "fictício",
    "fake",
    "pet-demo",
    "walk-demo",
    "request-demo",
)

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

# LEGADO/DEMO: consolidar com routes/referrals.py (não persistido de propósito — Fase de limpeza)
REFERRAL_RECORDS = [
    {
        "id": "ref-demo-1",
        "referral_code": "DOG-CARLO",
        "referral_type": "passeador_para_passeador",
        "status": "criada",
        "referrer_user_id": "walker-demo-1",
        "referred_user_id": None,
        "referrer_role": "passeador",
        "referred_role": "passeador",
        "created_at": "2026-05-02T12:00:00",
        "activated_at": None,
        "unlock_condition": {"min_completed_walks": 20, "min_rating_required": 4.7},
        "reward_amount": 100,
        "reward_released_at": None,
        "benefit_released_at": None,
        "condition_progress": {"completed_walks": 11, "rating_avg": 4.9},
        "fraud_flags": [],
    }
]

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

def _now() -> str:
    return datetime.utcnow().isoformat()


def _merge_dict(base: dict, updates: dict) -> dict:
    merged = {**base}
    for key, value in updates.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _merge_dict(merged[key], value)
        else:
            merged[key] = value
    return merged


def _walker_name(profile: WalkerProfile, db: Session) -> str:
    user = db.get(User, profile.user_id) if profile.user_id else None
    return (user.full_name if user else None) or (user.email if user else None) or "Passeador"


def _profile_user(profile: WalkerProfile, db: Session) -> User | None:
    return db.get(User, profile.user_id) if profile.user_id else None


def _has_fake_token(*values: object) -> bool:
    searchable = " ".join(str(value or "").strip().lower() for value in values)
    return any(token in searchable for token in FAKE_ENTITY_TOKENS)


def _is_valid_email(value: str | None) -> bool:
    email = (value or "").strip()
    return "@" in email and "." in email.rsplit("@", 1)[-1]


def _is_fake_user(user: User | None) -> bool:
    return bool(user and _has_fake_token(user.id, user.email, user.full_name))


def _is_real_tutor(user: User | None) -> bool:
    if not user or user.role not in {"tutor", "cliente", "client", "customer"}:
        return False
    if not _is_valid_email(user.email):
        return False
    return not _is_fake_user(user)


def _is_real_pet(pet: Pet | None, tutor: User | None = None) -> bool:
    if not pet:
        return False
    if _has_fake_token(pet.id, pet.name, pet.photo_url, pet.tutor_id):
        return False
    return _is_real_tutor(tutor) if tutor else True


def _is_fake_walker_profile(profile: WalkerProfile, user: User | None) -> bool:
    return _has_fake_token(
        profile.full_name,
        profile.cpf,
        profile.phone,
        profile.id,
        profile.user_id,
        user.email if user else "",
        user.full_name if user else "",
    )


def _is_real_active_walker_profile(profile: WalkerProfile, db: Session) -> bool:
    user = _profile_user(profile, db)
    if _is_fake_walker_profile(profile, user):
        return False
    if not user or user.role not in {"walker", "passeador"}:
        return False
    return bool(profile.status == "active" and profile.active_as_walker)


def _is_real_walker_user(user: User | None, db: Session) -> bool:
    if not user or user.role not in {"walker", "passeador"}:
        return False
    if _is_fake_user(user):
        return False
    profile = db.query(WalkerProfile).filter(WalkerProfile.user_id == user.id).first()
    return not profile or not _is_fake_walker_profile(profile, user)


def _walk_walker_user(walk: Walk, db: Session) -> User | None:
    walker_id = walk.walker_id or walk.assigned_walker_id
    return db.get(User, walker_id) if walker_id else None


def _is_real_admin_walk(walk: Walk, db: Session, require_walker: bool = False) -> bool:
    tutor = db.get(User, walk.tutor_id) if walk.tutor_id else None
    pet = db.get(Pet, walk.pet_id) if walk.pet_id else None
    walker = _walk_walker_user(walk, db)
    if _has_fake_token(walk.id, walk.tutor_id, walk.walker_id, walk.assigned_walker_id, walk.pet_id, walk.address_snapshot, walk.notes):
        return False
    if not _is_real_tutor(tutor):
        return False
    if not _is_real_pet(pet, tutor):
        return False
    if require_walker and not _is_real_walker_user(walker, db):
        return False
    return True


def _preload_admin_walk_realness(walks: list[Walk], db: Session) -> tuple[dict[str, User], dict[str, Pet], dict[str, WalkerProfile]]:
    user_ids = {
        user_id
        for walk in walks
        for user_id in (walk.tutor_id, walk.walker_id or walk.assigned_walker_id)
        if user_id
    }
    pet_ids = {walk.pet_id for walk in walks if walk.pet_id}

    users_by_id = {user.id: user for user in db.query(User).filter(User.id.in_(user_ids)).all()} if user_ids else {}
    pets_by_id = {pet.id: pet for pet in db.query(Pet).filter(Pet.id.in_(pet_ids)).all()} if pet_ids else {}
    profiles_by_user_id = (
        {profile.user_id: profile for profile in db.query(WalkerProfile).filter(WalkerProfile.user_id.in_(user_ids)).all()}
        if user_ids
        else {}
    )
    return users_by_id, pets_by_id, profiles_by_user_id


def _is_real_walker_user_preloaded(user: User | None, profile: WalkerProfile | None) -> bool:
    if not user or user.role not in {"walker", "passeador"}:
        return False
    if _is_fake_user(user):
        return False
    return not profile or not _is_fake_walker_profile(profile, user)


def _is_real_admin_walk_preloaded(
    walk: Walk,
    users_by_id: dict[str, User],
    pets_by_id: dict[str, Pet],
    profiles_by_user_id: dict[str, WalkerProfile],
    require_walker: bool = False,
) -> bool:
    tutor = users_by_id.get(walk.tutor_id) if walk.tutor_id else None
    pet = pets_by_id.get(walk.pet_id) if walk.pet_id else None
    walker_id = walk.walker_id or walk.assigned_walker_id
    walker = users_by_id.get(walker_id) if walker_id else None
    if _has_fake_token(walk.id, walk.tutor_id, walk.walker_id, walk.assigned_walker_id, walk.pet_id, walk.address_snapshot, walk.notes):
        return False
    if not _is_real_tutor(tutor):
        return False
    if not _is_real_pet(pet, tutor):
        return False
    if require_walker and not _is_real_walker_user_preloaded(walker, profiles_by_user_id.get(walker.id) if walker else None):
        return False
    return True


def _is_completed_admin_walk(walk: Walk) -> bool:
    return (walk.status or "").strip().lower() in {"finalizado", "completed", "finished"} or (walk.operational_status or "").strip().lower() == "ride_completed"


def _is_real_paid_payment(payment: Payment, real_walk_ids: set[str]) -> bool:
    if payment.status not in PAID_PAYMENT_STATUSES:
        return False
    if not payment.walk_id or payment.walk_id not in real_walk_ids:
        return False
    return not _has_fake_token(payment.id, payment.tutor_id, payment.walk_id, payment.provider, payment.provider_payment_id)


def _status_label(status: str | None) -> str:
    labels = {
        "submitted": "Cadastro enviado",
        "under_review": "Documentos em análise",
        "resubmission_requested": "Reenvio solicitado",
        "approved": "Candidato aprovado",
        "active": "Passeador ativo",
        "rejected": "Candidatura recusada",
        "blocked": "Bloqueado",
    }
    return labels.get(_canonical_application_status(status), labels["submitted"])


def _canonical_application_status(status: str | None) -> str:
    normalized = (status or "submitted").strip().lower()
    if normalized in {"active", "ativo", "passeador ativo"}:
        return "active"
    if normalized in {"approved", "aprovado", "candidato aprovado"}:
        return "approved"
    if normalized in {"rejected", "reprovado", "rejeitado", "candidatura recusada"}:
        return "rejected"
    if normalized in {"under_review", "document_review", "documents_review", "aprovação documental", "aprovacao documental", "documentos em análise", "documentos em analise", "em análise", "em analise"}:
        return "under_review"
    if normalized in {"resubmission_requested", "reenvio solicitado", "documents_pending"}:
        return "resubmission_requested"
    if normalized in {"blocked", "bloqueado", "restrito", "restricted", "suspenso", "suspended"}:
        return "blocked"
    if normalized in {"submitted", "cadastro enviado", "pending"}:
        return "submitted"
    return "submitted"


def _serialize_walker_profile(profile: WalkerProfile, db: Session, include_internal: bool = True) -> dict:
    user = _profile_user(profile, db)
    document_count = len([value for value in [profile.document_url, profile.identity_document_back_url, profile.selfie_url, profile.proof_of_address_url] if value])
    raw_status = _canonical_application_status(profile.status)
    active_as_walker = bool(profile.active_as_walker and raw_status == "active")
    payload = {
        "id": profile.id,
        "walker_id": profile.id,
        "user_id": profile.user_id,
        "full_name": profile.full_name or (user.full_name if user else "") or "Passeador",
        "name": profile.full_name or (user.full_name if user else "") or "Passeador",
        "cpf": profile.cpf or "",
        "phone": profile.phone or "",
        "email": user.email if user else "",
        "birth_date": profile.birth_date or "",
        "city": profile.city or "",
        "state": profile.state or "",
        "neighborhood_region": profile.state or profile.city or "",
        "region": profile.state or profile.city or "",
        "experience": profile.experience or "",
        "experience_description": profile.experience or "",
        "bio": profile.bio or "",
        "experience_options": [part.strip() for part in (profile.experience or "").split("|")[1:] if part.strip()],
        "rg": profile.rg or "",
        "document_url": create_signed_upload_url(profile.document_url),
        "identity_document_front_url": create_signed_upload_url(profile.document_url),
        "identity_document_back_url": create_signed_upload_url(profile.identity_document_back_url),
        "selfie_url": create_signed_upload_url(profile.selfie_url),
        "proof_of_address_url": create_signed_upload_url(profile.proof_of_address_url),
        "documents_count": document_count,
        "profile_photo_url": profile.profile_photo_url or "",
        "photo_url": profile.profile_photo_url or "",
        "accepted_declaration": True,
        "has_pet_experience": bool(profile.experience or profile.bio),
        "has_third_party_experience": bool(profile.experience),
        "availability": "",
        "status": _status_label(profile.status),
        "raw_status": raw_status,
        "operational_status": raw_status,
        "active_as_walker": active_as_walker,
        "approved_at": profile.approved_at,
        "rejected_at": profile.rejected_at,
        "rejection_reason": profile.rejection_reason,
        "status_reason": profile.rejection_reason,
        "reviewed_by_admin_id": profile.reviewed_by_admin_id,
        "resubmission_requested_documents": [item for item in (profile.resubmission_requested_documents or "").split(",") if item],
        "created_at": profile.created_at,
        "updated_at": profile.updated_at or profile.created_at,
    }
    payload.update(calculate_walker_operational_score(profile.user_id, db))
    if include_internal:
        payload["internal_notes"] = profile.internal_notes or ""
    return payload


def _document_key_list(values: list[str] | None) -> str:
    return ",".join([str(item).strip() for item in (values or []) if str(item).strip()])


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
    for profile in db.query(WalkerProfile).order_by(WalkerProfile.created_at.desc()).all():
        user = _profile_user(profile, db)
        if _is_fake_walker_profile(profile, user):
            continue
        key = (profile.cpf or profile.user_id or profile.id or profile.phone or (user.email if user else "")).strip().lower()
        if key in seen_keys:
            continue
        seen_keys.add(key)
        rows.append(_serialize_walker_profile(profile, db, include_internal=include_internal))
    return rows


def _split_scheduled_date(value: str) -> tuple[str | None, str | None]:
    if not value:
        return None, None
    date_part, _, time_part = value.partition("T")
    return date_part or None, time_part[:5] or None


def _serialize_admin_walk(walk: Walk, db: Session) -> dict:
    return serialize_operational_walk(walk, db, include_private=True)


def _table_exists(db: Session, table_name: str) -> bool:
    try:
        bind = db.get_bind()
        return inspect(bind).has_table(table_name)
    except Exception:
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


def _serialize_admin_payment(payment: Payment, db: Session) -> dict:
    walk = db.get(Walk, payment.walk_id) if payment.walk_id else None
    tutor = db.get(User, payment.tutor_id) if payment.tutor_id else None
    pet = db.get(Pet, walk.pet_id) if walk and walk.pet_id else None
    walk_date, walk_time = _split_scheduled_date(walk.scheduled_date) if walk else (None, None)
    return {
        "id": payment.id,
        "tutor_id": payment.tutor_id,
        "tutor_name": (tutor.full_name if tutor else None) or (tutor.email if tutor else None),
        "client_name": (tutor.full_name if tutor else None) or (tutor.email if tutor else None),
        "walk_id": payment.walk_id,
        "pet_id": walk.pet_id if walk else None,
        "pet_name": pet.name if pet else None,
        "walk_date": walk_date,
        "walk_time": walk_time,
        "amount": payment.amount,
        "value": payment.amount,
        "status": payment.status,
        "payment_status": payment.status,
        "provider": payment.provider,
        "provider_payment_id": payment.provider_payment_id,
        "plan_type": "Passeio avulso",
        "tipoPlano": "Passeio avulso",
        "created_at": payment.created_at,
    }


def _walker_program_rows(db: Session) -> list[dict]:
    rows = []
    profiles = db.query(WalkerProfile).all()
    for index, profile in enumerate(profiles or []):
        user = _profile_user(profile, db)
        if _is_fake_walker_profile(profile, user):
            continue
        completed = db.query(Walk).filter(Walk.walker_id == profile.user_id, Walk.status == "Finalizado").count()
        rows.append({
            "walker_id": profile.id,
            "user_id": profile.user_id,
            "name": _walker_name(profile, db),
            "status": profile.status,
            "kit_level": 2 if index % 2 == 0 else 1,
            "kit_audit_status": "aprovado" if index % 2 == 0 else "pendente",
            "cr_balance": 24 + index,
            "cr_earned_this_week": 6,
            "rating_avg": 4.9 if index % 2 == 0 else 4.6,
            "rating_count": 126 if index % 2 == 0 else 38,
            "score": 87 if index % 2 == 0 else 74,
            "matching_score": 89 if index % 2 == 0 else 76,
            "tips_week": 52 if index % 2 == 0 else 18,
            "tips_pending_review": 1 if index % 2 == 0 else 0,
            "completed_walks": completed or (11 if index % 2 == 0 else 4),
            "schedule_conflicts_blocked": index,
        })
    if rows:
        return rows
    return [
        {
            "walker_id": "walker-demo-1",
            "user_id": "walker-demo-user-1",
            "name": "Carlos Oliveira",
            "status": "approved",
            "kit_level": 2,
            "kit_audit_status": "aprovado",
            "cr_balance": 24,
            "cr_earned_this_week": 6,
            "rating_avg": 4.9,
            "rating_count": 126,
            "score": 87,
            "matching_score": 89,
            "tips_week": 52,
            "tips_pending_review": 1,
            "completed_walks": 11,
            "schedule_conflicts_blocked": 2,
        }
    ]


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
def operational_alerts(db: Session = Depends(get_db)):
    process_expired_attempts(db)

    real_walks = [
        walk
        for walk in db.query(Walk).order_by(Walk.created_at.desc()).all()
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

@router.get("/dashboard")
@api_router.get("/dashboard")
def dashboard(admin: User = Depends(require_permission("admin.access")), db: Session = Depends(get_db)):
    scope = get_admin_tenant_scope(admin)
    real_clients = [user for user in apply_tenant_filter(db.query(User), User, scope).all() if _is_real_tutor(user)]
    real_pets = [
        pet
        for pet in apply_tenant_filter(db.query(Pet), Pet, scope).all()
        if _is_real_pet(pet, db.get(User, pet.tutor_id) if pet.tutor_id else None)
    ]
    walk_rows = apply_tenant_filter(db.query(Walk), Walk, scope).all()
    walk_users_by_id, walk_pets_by_id, walk_profiles_by_user_id = _preload_admin_walk_realness(walk_rows, db)
    real_walks = [
        walk
        for walk in walk_rows
        if _is_real_admin_walk_preloaded(walk, walk_users_by_id, walk_pets_by_id, walk_profiles_by_user_id)
    ]
    critical_walks = [
    walk
    for walk in real_walks
    if str(
        walk.operational_status or walk.status or ""
    ).lower() in RECOVERY_WALK_STATUSES
]
    completed_real_walks = [
        walk
        for walk in real_walks
        if _is_completed_admin_walk(walk)
        and _is_real_admin_walk_preloaded(walk, walk_users_by_id, walk_pets_by_id, walk_profiles_by_user_id, require_walker=True)
    ]
    real_revenue_walk_ids = {walk.id for walk in completed_real_walks}
    payments = [
        payment
        for payment in apply_tenant_filter(db.query(Payment), Payment, scope).filter(Payment.status.in_(PAID_PAYMENT_STATUSES)).all()
        if _is_real_paid_payment(payment, real_revenue_walk_ids)
    ]
    no_show_total = len([walk for walk in real_walks if walk.status in {"Não comparecimento do cliente", "Não comparecimento do passeador"}])
    walk_total = len(real_walks)
    real_active_walkers_count = sum(
        1
        for profile in db.query(WalkerProfile).all()
        if _is_real_active_walker_profile(profile, db)
    )
    real_risk_walkers_count = sum(
        1
        for profile in db.query(WalkerProfile).filter(WalkerProfile.status.in_(["restricted", "suspended"])).all()
        if not _is_fake_walker_profile(profile, _profile_user(profile, db))
    )
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
        "total_clients": len(real_clients),
        "total_tutors": len(real_clients),
        "total_pets": len(real_pets),
        "total_active_walkers": real_active_walkers_count,
        "total_walkers": real_active_walkers_count,
        "total_walks_scheduled": len([walk for walk in real_walks if walk.status == "Agendado"]),
        "scheduled_walks": len([walk for walk in real_walks if walk.status == "Agendado"]),
        "total_walks_finished": len(completed_real_walks),
        "completed_walks": len(completed_real_walks),
        "total_walks_in_progress": len([walk for walk in real_walks if walk.status in IN_PROGRESS_WALK_STATUSES or walk.operational_status in IN_PROGRESS_WALK_STATUSES]),
        "estimated_revenue_paid": sum(float(payment.amount or 0) for payment in payments),
        "estimated_revenue": sum(float(payment.amount or 0) for payment in payments),
        "pending_occurrences": 0,
        "open_disputes": 0,
        "walkers_at_risk": real_risk_walkers_count,
        "top_rated_walkers": 0,
        "disintermediation_alerts": 0,
        "critical_operational_alerts": len(critical_walks),

        "critical_walks": [
            {
                "id": walk.id,
                "pet_id": walk.pet_id,
                "tutor_id": walk.tutor_id,
                "status": walk.status,
                "operational_status": walk.operational_status,
                "scheduled_date": walk.scheduled_date,
            }
            for walk in critical_walks
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
def users(admin: User = Depends(require_permission("users.read")), db: Session = Depends(get_db)):
    # super_admin enxerga todos os tenants; admin regular fica restrito ao seu.
    query = apply_tenant_filter(db.query(User), User, get_admin_tenant_scope(admin))
    return query.all()


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


def _serialize_payment_config(config) -> TenantPaymentConfigResponse:
    return TenantPaymentConfigResponse(
        tenant_id=config.tenant_id,
        provider=config.provider,
        commission_percent=config.commission_percent,
        split_enabled=config.split_enabled,
        active=config.active,
    )


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
    config = update_payment_config(
        db,
        tenant_id,
        commission_percent=payload.commission_percent,
        provider=payload.provider,
        split_enabled=payload.split_enabled,
        actor=admin,
    )
    return _serialize_payment_config(config)


def _serialize_admin_tutor(user: User, db: Session) -> dict:
    profile = (
        db.query(TutorProfile)
        .filter(TutorProfile.user_id == user.id)
        .first()
    )

    address_parts = []
    if profile:
        street_number = " ".join(
            part for part in [profile.street, profile.number] if part
        ).strip()
        address_parts = [
            street_number,
            profile.complement,
            profile.neighborhood,
            profile.city,
            profile.state,
            profile.cep,
        ]

    address_snapshot = ", ".join(part for part in address_parts if part)

    return {
        "id": user.id,
        "user_id": user.id,
        "email": user.email,
        "full_name": (profile.full_name if profile else None) or user.full_name or user.email,
        "name": (profile.full_name if profile else None) or user.full_name or user.email,
        "role": user.role,
        "created_at": user.created_at,
        "cpf": profile.cpf if profile else "",
        "phone": profile.phone if profile else "",
        "telefone": profile.phone if profile else "",
        "cep": profile.cep if profile else "",
        "street": profile.street if profile else "",
        "rua": profile.street if profile else "",
        "number": profile.number if profile else "",
        "numero": profile.number if profile else "",
        "complement": profile.complement if profile else "",
        "complemento": profile.complement if profile else "",
        "neighborhood": profile.neighborhood if profile else "",
        "bairro": profile.neighborhood if profile else "",
        "city": profile.city if profile else "",
        "cidade": profile.city if profile else "",
        "state": profile.state if profile else "",
        "estado": profile.state if profile else "",
        "address_snapshot": address_snapshot,
    }


@router.get("/tutors")
@api_router.get("/tutors")
def tutors(admin: User = Depends(require_permission("users.read")), db: Session = Depends(get_db)):
    users = [
        user
        for user in apply_tenant_filter(db.query(User), User, get_admin_tenant_scope(admin)).order_by(User.created_at.desc()).all()
        if _is_real_tutor(user)
    ]
    return [_serialize_admin_tutor(user, db) for user in users]

def _serialize_admin_pet(pet: Pet, db: Session) -> dict:
    tutor = db.get(User, pet.tutor_id) if pet.tutor_id else None

    return {
        "id": pet.id,
        "pet_id": pet.id,
        "tutor_id": pet.tutor_id,
        "user_id": pet.tutor_id,
        "owner_id": pet.tutor_id,
        "name": pet.name,
        "pet_name": pet.name,
        "photo_url": pet.photo_url,
        "pet_photo_url": pet.photo_url,
        "species": pet.species,
        "sex": pet.sex,
        "breed": pet.breed,
        "size": pet.size,
        "weight": pet.weight,
        "age": pet.age,
        "behavior_notes": pet.behavior_notes,
        "notes": pet.behavior_notes or pet.health_notes or "",
        "health_notes": pet.health_notes,
        "restrictions": pet.restrictions,
        "owner_name": (tutor.full_name if tutor else None) or (tutor.email if tutor else None) or "",
        "created_at": pet.created_at,
    }


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
def walkers(db: Session = Depends(get_db)):
    return _unique_walker_profiles(db)

@router.get("/partner-applications")
@api_router.get("/partner-applications")
def partner_applications(db: Session = Depends(get_db)):
    return _unique_walker_profiles(db, include_internal=False)


@router.get("/partner-applications/{candidate_id}")
@api_router.get("/partner-applications/{candidate_id}")
def partner_application_detail(candidate_id: str, db: Session = Depends(get_db)):
    profile = db.get(WalkerProfile, candidate_id)
    if not profile:
        raise HTTPException(status_code=404, detail="Candidatura nao encontrada")
    return _serialize_walker_profile(profile, db)


@router.patch("/partner-applications/{candidate_id}/admin-fields")
@api_router.patch("/partner-applications/{candidate_id}/admin-fields")
def update_partner_application_admin_fields(candidate_id: str, payload: dict | None = None, admin: User = Depends(require_permission("walkers.validate")), db: Session = Depends(get_db)):
    profile = db.get(WalkerProfile, candidate_id)
    if not profile:
        raise HTTPException(status_code=404, detail="Candidatura nao encontrada")
    payload = payload or {}
    if "internal_notes" in payload:
        profile.internal_notes = payload.get("internal_notes") or ""
    if "status" in payload:
        _apply_application_status(profile, payload.get("status") or "submitted", payload.get("reason"))
    if "reviewed_by_admin_id" in payload:
        profile.reviewed_by_admin_id = payload.get("reviewed_by_admin_id") or None
    if "resubmission_requested_documents" in payload:
        profile.resubmission_requested_documents = _document_key_list(payload.get("resubmission_requested_documents") or [])
    if "active_as_walker" in payload:
        active_as_walker = bool(payload.get("active_as_walker"))
        if active_as_walker and profile.status not in {"approved", "active"}:
            raise HTTPException(status_code=400, detail="Apenas candidatos aprovados podem ser ativados como passeador.")
        _apply_application_status(profile, "active" if active_as_walker else "approved")
        user = db.get(User, profile.user_id)
        if active_as_walker and user:
            user.role = "walker"
        if active_as_walker:
            mark_referral_approved(profile.user_id, db)
    if any(key in payload for key in ("internal_notes", "status", "active_as_walker")):
        event_type = "admin_note_added" if "internal_notes" in payload else "status_changed"
        if payload.get("active_as_walker"):
            event_type = "approved"
        record_admin_operational_event(
            db,
            event_type=event_type,
            entity_type="walker",
            entity_id=profile.user_id,
            severity="info",
            title="Candidatura atualizada",
            description=payload.get("internal_notes") or payload.get("reason") or "Campos administrativos da candidatura atualizados.",
            actor=admin,
            source="admin.partner_application.update",
            metadata={"candidate_id": profile.id, "fields": sorted(payload.keys())},
        )
    db.commit()
    db.refresh(profile)
    return _serialize_walker_profile(profile, db)


@router.post("/walkers/{walker_id}/approve")
@api_router.post("/walkers/{walker_id}/approve")
def approve_walker(walker_id: str, request: Request, admin: User = Depends(require_permission("walkers.validate")), db: Session = Depends(get_db)):
    profile = db.get(WalkerProfile, walker_id)
    if not profile:
        raise HTTPException(status_code=404, detail="Passeador nao encontrado")
    _apply_application_status(profile, "approved")
    record_admin_operational_event(
        db,
        event_type="approved",
        entity_type="walker",
        entity_id=profile.user_id,
        severity="info",
        title="Candidatura aprovada",
        description="Candidatura de passeador aprovada pela administracao.",
        actor=admin,
        source="admin.walker.approve",
        metadata={"candidate_id": profile.id},
        request=request,
    )
    db.commit()
    db.refresh(profile)
    return _serialize_walker_profile(profile, db)

@router.post("/walkers/{walker_id}/reject")
@api_router.post("/walkers/{walker_id}/reject")
def reject_walker(walker_id: str, request: Request, payload: dict | None = None, admin: User = Depends(require_permission("walkers.validate")), db: Session = Depends(get_db)):
    profile = db.get(WalkerProfile, walker_id)
    if not profile:
        raise HTTPException(status_code=404, detail="Passeador nao encontrado")
    _apply_application_status(profile, "rejected", (payload or {}).get("reason"))
    record_admin_operational_event(
        db,
        event_type="rejected",
        entity_type="walker",
        entity_id=profile.user_id,
        severity="warning",
        title="Candidatura reprovada",
        description=(payload or {}).get("reason") or "Candidatura reprovada pela administracao.",
        actor=admin,
        source="admin.walker.reject",
        metadata={"candidate_id": profile.id},
        request=request,
    )
    db.commit()
    mark_referral_rejected(profile.user_id, profile.rejection_reason, db)
    db.refresh(profile)
    return _serialize_walker_profile(profile, db)

@router.get("/walks")
@api_router.get("/walks")
def walks(admin: User = Depends(require_permission("walks.read")), db: Session = Depends(get_db)):
    process_expired_attempts(db)
    real_walks = [
        walk
        for walk in apply_tenant_filter(db.query(Walk), Walk, get_admin_tenant_scope(admin)).order_by(Walk.created_at.desc()).all()
        if _is_real_admin_walk(walk, db)
    ]
    _refresh_reliability_events(real_walks, db)
    rows = [
        _serialize_admin_walk(walk, db)
        for walk in real_walks
    ]
    return rows

@router.patch("/walks/{walk_id}/status")
@api_router.patch("/walks/{walk_id}/status")
def update_admin_walk_status(walk_id: str, payload: dict, admin: User = Depends(require_permission("walks.update_status")), db: Session = Depends(get_db)):
    walk = db.get(Walk, walk_id)

    if not walk:
        raise HTTPException(status_code=404, detail="Passeio nao encontrado")

    status = payload.get("status")

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
def payments(admin: User = Depends(require_permission("finance.read")), db: Session = Depends(get_db)):
    # require_permission convive com o require_admin do router durante a migração.
    query = apply_tenant_filter(db.query(Payment), Payment, get_admin_tenant_scope(admin))
    return [_serialize_admin_payment(payment, db) for payment in query.order_by(Payment.created_at.desc()).all()]


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


@router.post("/walk-completions/{review_id}/approve")
@api_router.post("/walk-completions/{review_id}/approve")
def approve_walk_completion(review_id: str, payload: dict | None = None, admin: User = Depends(require_permission("walks.update_status")), db: Session = Depends(get_db)):
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
    review.admin_note = (payload or {}).get("admin_note") or (payload or {}).get("note")
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
def reject_walk_completion(review_id: str, payload: dict | None = None, admin: User = Depends(require_permission("walks.update_status")), db: Session = Depends(get_db)):
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
    review.admin_note = (payload or {}).get("admin_note") or (payload or {}).get("reason") or "Finalizacao rejeitada pela revisao administrativa."
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


@router.post("/walker-kits/{submission_id}/reject")
@api_router.post("/walker-kits/{submission_id}/reject")
def reject_walker_kit(submission_id: str, payload: dict | None = None, admin: User = Depends(require_permission("walkers.validate")), db: Session = Depends(get_db)):
    submission = db.query(WalkerKitSubmission).filter(WalkerKitSubmission.id == submission_id).first()
    if not submission:
        raise HTTPException(status_code=404, detail="Envio de kit nao encontrado.")

    now = datetime.utcnow()
    submission.audit_status = "rejected"
    submission.audit_note = (payload or {}).get("audit_note") or (payload or {}).get("reason") or "Kit rejeitado pela auditoria administrativa."
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
def walker_operations(db: Session = Depends(get_db)):
    walkers = db.query(WalkerProfile).all()
    pending_walks = db.query(Walk).filter(Walk.walker_id.is_(None), Walk.status == "Agendado").all()
    active_walks = db.query(Walk).filter(Walk.status.in_(["Indo buscar o pet", "Passeando agora"])).all()
    withdrawals = db.query(Payment).filter(Payment.provider == "pix").all()
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
def referral_program_settings(db: Session = Depends(get_db)):
    return get_setting(db, "referral_program", DEFAULT_REFERRAL_PROGRAM_SETTINGS)


@router.put("/referral-program/settings")
def update_referral_program_settings(payload: dict, db: Session = Depends(get_db)):
    current = get_setting(db, "referral_program", DEFAULT_REFERRAL_PROGRAM_SETTINGS)
    merged = _merge_dict(current, payload or {})
    merged["updated_at"] = _now()
    merged["updated_by"] = "admin"
    save_setting(db, "referral_program", merged, updated_by="admin")
    return merged


@router.get("/referrals")
def referrals(limit: int = 20):
    items = REFERRAL_RECORDS[: max(0, limit)]
    return {"items": items, "total": len(REFERRAL_RECORDS)}


@router.post("/referrals/{referral_id}/status")
def update_referral_status(referral_id: str, payload: dict):
    status = (payload or {}).get("status")
    note = (payload or {}).get("note", "")
    for item in REFERRAL_RECORDS:
        if item["id"] == referral_id:
            item["status"] = status or item["status"]
            if status == "invalida_fraude":
                item["fraud_flags"] = [note or "Marcado manualmente pelo admin"]
            return item
    return {"id": referral_id, "status": status, "note": note}


@router.get("/walker-programs")
def walker_programs(db: Session = Depends(get_db)):
    rows = _walker_program_rows(db)
    return {
        "settings": get_setting(db, "walker_program", DEFAULT_WALKER_PROGRAM_SETTINGS),
        "metrics": _walker_program_metrics(rows),
        "walkers": rows,
        "tips_review_queue": [
            {
                "id": "tip-review-1",
                "walker_id": rows[0]["walker_id"],
                "walker_name": rows[0]["name"],
                "amount": 52,
                "reason": "Concentracao recente de gorjetas acima da media.",
                "status": "pending",
            }
        ] if rows else [],
        "actions": recent_walker_program_actions(db, limit=20),
    }


@router.put("/walker-programs/settings")
def update_walker_program_settings(payload: dict, db: Session = Depends(get_db)):
    current = get_setting(db, "walker_program", DEFAULT_WALKER_PROGRAM_SETTINGS)
    merged = _merge_dict(current, payload or {})
    merged["updated_at"] = _now()
    merged["updated_by"] = "admin"
    save_setting(db, "walker_program", merged, updated_by="admin")
    return merged


@router.post("/walker-programs/walkers/{walker_id}/cr")
def adjust_walker_cr(walker_id: str, payload: dict, db: Session = Depends(get_db)):
    action = {
        "id": str(uuid4()),
        "type": "cr_adjustment",
        "walker_id": walker_id,
        "amount": int((payload or {}).get("amount", 0)),
        "reason": (payload or {}).get("reason", "Ajuste administrativo"),
        "created_at": _now(),
    }
    append_walker_program_action(db, action_type="cr", walker_id=walker_id, payload=action)
    return {"ok": True, "action": action}


@router.post("/walker-programs/walkers/{walker_id}/kit-audit")
def audit_walker_kit(walker_id: str, payload: dict, db: Session = Depends(get_db)):
    action = {
        "id": str(uuid4()),
        "type": "kit_audit",
        "walker_id": walker_id,
        "status": (payload or {}).get("status", "aprovado"),
        "note": (payload or {}).get("note", ""),
        "created_at": _now(),
    }
    append_walker_program_action(db, action_type="kit", walker_id=walker_id, payload=action)
    return {"ok": True, "action": action}


@router.post("/walker-programs/tips/{tip_id}/review")
def review_tip(tip_id: str, payload: dict, db: Session = Depends(get_db)):
    action = {
        "id": str(uuid4()),
        "type": "tip_review",
        "tip_id": tip_id,
        "status": (payload or {}).get("status", "approved"),
        "note": (payload or {}).get("note", ""),
        "created_at": _now(),
    }
    append_walker_program_action(db, action_type="tip", walker_id=None, payload=action)
    return {"ok": True, "action": action}

@router.post("/withdrawals/{payment_id}/approve")
def approve_withdrawal(payment_id: str, admin: User = Depends(require_permission("finance.manage")), db: Session = Depends(get_db)):
    payment = db.get(Payment, payment_id)
    if payment:
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
