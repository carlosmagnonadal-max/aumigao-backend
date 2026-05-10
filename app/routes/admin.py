from fastapi import APIRouter, Depends
from fastapi import HTTPException
from datetime import datetime

from sqlalchemy.orm import Session
from app.core.database import get_db
from app.dependencies.auth import require_admin
from app.models.payment import Payment
from app.models.pet import Pet
from app.models.user import User
from app.models.walk import Walk
from app.models.walker_profile import WalkerProfile
from app.services.walker_referrals import mark_referral_approved, mark_referral_rejected
from app.services.operational_matching_service import process_expired_attempts, serialize_operational_walk

router = APIRouter(prefix="/admin", tags=["admin"], dependencies=[Depends(require_admin)])
api_router = APIRouter(prefix="/api/admin", tags=["admin"], dependencies=[Depends(require_admin)])

APPROVED_WALKER_STATUSES = {"active"}
PAID_PAYMENT_STATUSES = {"paid", "Pago", "pagamento_confirmado_sandbox", "payment_confirmed", "confirmed"}
IN_PROGRESS_WALK_STATUSES = {"Indo buscar o pet", "Passeando agora", "walker_arriving", "ride_in_progress"}
FAKE_WALKER_TOKENS = (
    "passeador fluxo real",
    "passeador login",
    "passeador ativado",
    "passeador auditoria",
    "passeador docs",
    "auditoria real",
    "teste",
    "test",
    "demo",
    "mock",
    "fallback",
    "sample",
    "seed",
    "local",
    "auditoria",
)

REFERRAL_PROGRAM_SETTINGS = {
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

WALKER_PROGRAM_SETTINGS = {
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

WALKER_PROGRAM_ACTIONS = []


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


def _is_fake_walker_profile(profile: WalkerProfile, user: User | None) -> bool:
    searchable = " ".join([
        profile.full_name or "",
        profile.cpf or "",
        profile.phone or "",
        profile.id or "",
        profile.user_id or "",
        user.email if user else "",
        user.full_name if user else "",
    ]).strip().lower()
    return any(token in searchable for token in FAKE_WALKER_TOKENS)


def _is_real_active_walker_profile(profile: WalkerProfile, db: Session) -> bool:
    user = _profile_user(profile, db)
    if _is_fake_walker_profile(profile, user):
        return False
    if not user or user.role not in {"walker", "passeador"}:
        return False
    return bool(profile.status == "active" and profile.active_as_walker)


def _status_label(status: str | None) -> str:
    status = (status or "pending").strip()
    if status in {"approved", "active"}:
        return "Aprovado"
    if status == "rejected":
        return "Reprovado"
    if status in {"document_review", "aprovacao_documental"}:
        return "Aprovação documental"
    if status == "restricted":
        return "Restrito"
    if status == "suspended":
        return "Suspenso"
    return "Em análise"


def _serialize_walker_profile(profile: WalkerProfile, db: Session, include_internal: bool = True) -> dict:
    user = _profile_user(profile, db)
    document_count = len([value for value in [profile.document_url, profile.identity_document_back_url, profile.selfie_url, profile.proof_of_address_url] if value])
    active_as_walker = bool(profile.active_as_walker and profile.status in APPROVED_WALKER_STATUSES)
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
        "document_url": profile.document_url,
        "identity_document_front_url": profile.document_url,
        "identity_document_back_url": profile.identity_document_back_url,
        "selfie_url": profile.selfie_url,
        "proof_of_address_url": profile.proof_of_address_url,
        "documents_count": document_count,
        "profile_photo_url": profile.profile_photo_url or "",
        "photo_url": profile.profile_photo_url or "",
        "accepted_declaration": True,
        "has_pet_experience": bool(profile.experience or profile.bio),
        "has_third_party_experience": bool(profile.experience),
        "availability": "",
        "status": _status_label(profile.status),
        "raw_status": profile.status,
        "operational_status": profile.status,
        "active_as_walker": active_as_walker,
        "approved_at": profile.approved_at,
        "rejected_at": profile.rejected_at,
        "rejection_reason": profile.rejection_reason,
        "created_at": profile.created_at,
        "updated_at": profile.created_at,
    }
    if include_internal:
        payload["internal_notes"] = profile.internal_notes or ""
    return payload


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

@router.get("/dashboard")
@api_router.get("/dashboard")
def dashboard(db: Session = Depends(get_db)):
    payments = db.query(Payment).filter(Payment.status.in_(PAID_PAYMENT_STATUSES)).all()
    no_show_total = db.query(Walk).filter(Walk.status.in_(["Não comparecimento do cliente", "Não comparecimento do passeador"])).count()
    walk_total = db.query(Walk).count()
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
    return {
        "total_clients": db.query(User).filter(User.role.in_(["tutor", "cliente"])).count(),
        "total_tutors": db.query(User).filter(User.role.in_(["tutor", "cliente"])).count(),
        "total_pets": db.query(Pet).count(),
        "total_active_walkers": real_active_walkers_count,
        "total_walkers": real_active_walkers_count,
        "total_walks_scheduled": db.query(Walk).filter(Walk.status == "Agendado").count(),
        "scheduled_walks": db.query(Walk).filter(Walk.status == "Agendado").count(),
        "total_walks_finished": db.query(Walk).filter(Walk.status == "Finalizado").count(),
        "completed_walks": db.query(Walk).filter(Walk.status == "Finalizado").count(),
        "total_walks_in_progress": db.query(Walk).filter(Walk.status.in_(IN_PROGRESS_WALK_STATUSES)).count(),
        "estimated_revenue_paid": sum(float(payment.amount or 0) for payment in payments),
        "estimated_revenue": sum(float(payment.amount or 0) for payment in payments),
        "pending_occurrences": 0,
        "open_disputes": 0,
        "walkers_at_risk": real_risk_walkers_count,
        "top_rated_walkers": 0,
        "disintermediation_alerts": 0,
        "weekly_tips_amount": sum(float(payment.amount or 0) for payment in payments if payment.provider == "internal_tip"),
        "no_show_rate": round((no_show_total / walk_total) * 100, 2) if walk_total else 0,
    }

@router.get("/users")
@api_router.get("/users")
def users(db: Session = Depends(get_db)):
    return db.query(User).all()

@router.get("/tutors")
@api_router.get("/tutors")
def tutors(db: Session = Depends(get_db)):
    return db.query(User).filter(User.role.in_(["tutor", "cliente"])).all()

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
def update_partner_application_admin_fields(candidate_id: str, payload: dict | None = None, db: Session = Depends(get_db)):
    profile = db.get(WalkerProfile, candidate_id)
    if not profile:
        raise HTTPException(status_code=404, detail="Candidatura nao encontrada")
    payload = payload or {}
    if "internal_notes" in payload:
        profile.internal_notes = payload.get("internal_notes") or ""
    if "active_as_walker" in payload:
        active_as_walker = bool(payload.get("active_as_walker"))
        if active_as_walker and profile.status not in APPROVED_WALKER_STATUSES:
            raise HTTPException(status_code=400, detail="Apenas candidatos aprovados podem ser ativados como passeador.")
        profile.active_as_walker = active_as_walker
        profile.status = "active" if active_as_walker else "approved"
        if active_as_walker and not profile.approved_at:
            profile.approved_at = datetime.utcnow()
        user = db.get(User, profile.user_id)
        if active_as_walker and user:
            user.role = "walker"
    db.commit()
    db.refresh(profile)
    return _serialize_walker_profile(profile, db)


@router.post("/walkers/{walker_id}/approve")
@api_router.post("/walkers/{walker_id}/approve")
def approve_walker(walker_id: str, db: Session = Depends(get_db)):
    profile = db.get(WalkerProfile, walker_id)
    if not profile:
        raise HTTPException(status_code=404, detail="Passeador nao encontrado")
    profile.status = "active"
    profile.active_as_walker = True
    profile.approved_at = datetime.utcnow()
    profile.rejected_at = None
    profile.rejection_reason = None
    user = db.get(User, profile.user_id)
    if user:
        user.role = "walker"
    db.commit()
    mark_referral_approved(profile.user_id, db)
    db.refresh(profile)
    return _serialize_walker_profile(profile, db)

@router.post("/walkers/{walker_id}/reject")
@api_router.post("/walkers/{walker_id}/reject")
def reject_walker(walker_id: str, payload: dict | None = None, db: Session = Depends(get_db)):
    profile = db.get(WalkerProfile, walker_id)
    if not profile:
        raise HTTPException(status_code=404, detail="Passeador nao encontrado")
    profile.status = "rejected"
    profile.active_as_walker = False
    profile.rejection_reason = (payload or {}).get("reason")
    profile.rejected_at = datetime.utcnow()
    profile.approved_at = None
    db.commit()
    mark_referral_rejected(profile.user_id, profile.rejection_reason, db)
    db.refresh(profile)
    return _serialize_walker_profile(profile, db)

@router.get("/walks")
@api_router.get("/walks")
def walks(db: Session = Depends(get_db)):
    process_expired_attempts(db)
    return [_serialize_admin_walk(walk, db) for walk in db.query(Walk).order_by(Walk.created_at.desc()).all()]

@router.get("/payments")
@api_router.get("/payments")
def payments(db: Session = Depends(get_db)):
    return [_serialize_admin_payment(payment, db) for payment in db.query(Payment).order_by(Payment.created_at.desc()).all()]

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
def referral_program_settings():
    return REFERRAL_PROGRAM_SETTINGS


@router.put("/referral-program/settings")
def update_referral_program_settings(payload: dict):
    global REFERRAL_PROGRAM_SETTINGS
    REFERRAL_PROGRAM_SETTINGS = _merge_dict(REFERRAL_PROGRAM_SETTINGS, payload or {})
    REFERRAL_PROGRAM_SETTINGS["updated_at"] = _now()
    REFERRAL_PROGRAM_SETTINGS["updated_by"] = "admin"
    return REFERRAL_PROGRAM_SETTINGS


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
        "settings": WALKER_PROGRAM_SETTINGS,
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
        "actions": WALKER_PROGRAM_ACTIONS[-20:],
    }


@router.put("/walker-programs/settings")
def update_walker_program_settings(payload: dict):
    global WALKER_PROGRAM_SETTINGS
    WALKER_PROGRAM_SETTINGS = _merge_dict(WALKER_PROGRAM_SETTINGS, payload or {})
    WALKER_PROGRAM_SETTINGS["updated_at"] = _now()
    WALKER_PROGRAM_SETTINGS["updated_by"] = "admin"
    return WALKER_PROGRAM_SETTINGS


@router.post("/walker-programs/walkers/{walker_id}/cr")
def adjust_walker_cr(walker_id: str, payload: dict):
    action = {
        "id": f"cr-{len(WALKER_PROGRAM_ACTIONS) + 1}",
        "type": "cr_adjustment",
        "walker_id": walker_id,
        "amount": int((payload or {}).get("amount", 0)),
        "reason": (payload or {}).get("reason", "Ajuste administrativo"),
        "created_at": _now(),
    }
    WALKER_PROGRAM_ACTIONS.append(action)
    return {"ok": True, "action": action}


@router.post("/walker-programs/walkers/{walker_id}/kit-audit")
def audit_walker_kit(walker_id: str, payload: dict):
    action = {
        "id": f"kit-{len(WALKER_PROGRAM_ACTIONS) + 1}",
        "type": "kit_audit",
        "walker_id": walker_id,
        "status": (payload or {}).get("status", "aprovado"),
        "note": (payload or {}).get("note", ""),
        "created_at": _now(),
    }
    WALKER_PROGRAM_ACTIONS.append(action)
    return {"ok": True, "action": action}


@router.post("/walker-programs/tips/{tip_id}/review")
def review_tip(tip_id: str, payload: dict):
    action = {
        "id": f"tip-{len(WALKER_PROGRAM_ACTIONS) + 1}",
        "type": "tip_review",
        "tip_id": tip_id,
        "status": (payload or {}).get("status", "approved"),
        "note": (payload or {}).get("note", ""),
        "created_at": _now(),
    }
    WALKER_PROGRAM_ACTIONS.append(action)
    return {"ok": True, "action": action}

@router.post("/withdrawals/{payment_id}/approve")
def approve_withdrawal(payment_id: str, db: Session = Depends(get_db)):
    payment = db.get(Payment, payment_id)
    if payment:
        payment.status = "paid"
        db.commit()
    return {"ok": True}

@router.post("/withdrawals/{payment_id}/reject")
def reject_withdrawal(payment_id: str, db: Session = Depends(get_db)):
    payment = db.get(Payment, payment_id)
    if payment:
        payment.status = "rejected"
        db.commit()
    return {"ok": True}
