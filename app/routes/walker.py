import os
from datetime import datetime, timedelta
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.security import get_password_hash
from app.dependencies.auth import get_current_user
from app.models.payment import Payment
from app.models.pet import Pet
from app.models.user import User
from app.models.walk import Walk
from app.models.walker_profile import WalkerProfile
from app.schemas.walker_profile import WalkerProfileCreate, WalkerProfileResponse, WalkerProfileUpdate
from app.schemas.complaint import ComplaintCreate, ComplaintEvidenceCreate
from app.services.complaint_service import create_complaint
from app.services.reputation_service import reputation_summary
from app.services.walker_referrals import mark_referral_approved, mark_referral_rejected, mark_referral_under_review
from app.utils.registration_validation import normalize_cpf_or_raise, normalize_email_or_raise, normalize_phone_or_raise
from app.services.operational_matching_service import (
    accept_walk as accept_operational_walk,
    decline_walk as decline_operational_walk,
    process_expired_attempts,
    serialize_operational_walk,
    update_operational_status,
)

router = APIRouter(prefix="/walker", tags=["walker"])
api_public_router = APIRouter(prefix="/api", tags=["walkers"])
partner_router = APIRouter(prefix="/api/partner-applications", tags=["partner-applications"])

DEMO_MODE = os.getenv("EXPO_PUBLIC_DEMO_MODE", os.getenv("DEMO_MODE", "false")).strip().lower() in {"1", "true", "yes", "on"}

DOG_PHOTOS = {
    "Thor": "https://images.unsplash.com/photo-1552053831-71594a27632d?auto=format&fit=crop&w=500&q=85",
    "Luna": "https://images.unsplash.com/photo-1551717743-49959800b1f6?auto=format&fit=crop&w=500&q=85",
    "Mel": "https://images.unsplash.com/photo-1586671267731-da2cf3ceeb80?auto=format&fit=crop&w=500&q=85",
    "Buddy": "https://images.unsplash.com/photo-1587300003388-59208cc962cb?auto=format&fit=crop&w=500&q=85",
    "Tequila": "https://images.unsplash.com/photo-1537151625747-768eb6cf92b2?auto=format&fit=crop&w=500&q=85",
}

KIT_TIERS = [
    {
        "key": "basic",
        "label": "Basico",
        "ranking_bonus": 4,
        "items": ["water", "bowl", "bags"],
    },
    {
        "key": "intermediate",
        "label": "Intermediario",
        "ranking_bonus": 8,
        "items": ["water", "bowl", "bags", "first_aid", "towel"],
    },
    {
        "key": "premium",
        "label": "Premium",
        "ranking_bonus": 12,
        "items": ["water", "bowl", "bags", "first_aid", "towel", "premium_treats"],
    },
]

KIT_ITEM_DEFINITIONS = [
    {"key": "water", "label": "Agua", "description": "Garrafa lacrada ou propria para hidratacao."},
    {"key": "bowl", "label": "Vasilha para agua", "description": "Vasilha ou pote portatil para oferecer agua."},
    {"key": "bags", "label": "Saquinho para necessidades", "description": "Saquinhos higienicos suficientes para o passeio."},
    {"key": "first_aid", "label": "Primeiros socorros", "description": "Kit simples para pequenas ocorrencias."},
    {"key": "towel", "label": "Toalha/pano", "description": "Pano limpo para secar patas ou pequenas sujeiras."},
    {"key": "premium_treats", "label": "Itens premium", "description": "Petiscos autorizados e outros itens de conforto."},
]

WALKER_KIT_SUBMISSIONS: dict[str, dict] = {}


class PartnerApplicationCreate(BaseModel):
    full_name: str
    cpf: str
    phone: str = ""
    email: str
    neighborhood_region: str = ""
    has_pet_experience: bool = False
    has_third_party_experience: bool = False
    experience_description: str = ""
    availability: str = ""
    profile_photo_url: str | None = None
    document_url: str | None = None
    proof_of_address_url: str | None = None
    selfie_url: str | None = None
    accepted_declaration: bool = Field(default=False)


class PartnerApplicationStatusUpdate(BaseModel):
    status: str
    reason: str | None = None


class PartnerApplicationAdminFieldsUpdate(BaseModel):
    internal_notes: str | None = None
    active_as_walker: bool | None = None


def _public_status_label(status: str | None) -> str:
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


def _raw_status_from_label(status: str) -> str:
    normalized = status.strip().lower()
    if normalized in {"aprovado", "approved", "ativo", "active"}:
        return "active"
    if normalized in {"reprovado", "rejected", "rejeitado"}:
        return "rejected"
    if normalized in {"aprovação documental", "aprovacao documental", "document_review"}:
        return "document_review"
    if normalized in {"restrito", "restricted"}:
        return "restricted"
    if normalized in {"suspenso", "suspended"}:
        return "suspended"
    return "pending"


def _serialize_partner_application(profile: WalkerProfile, db: Session, include_internal: bool = False) -> dict:
    user = db.get(User, profile.user_id) if profile.user_id else None
    payload = {
        "id": profile.id,
        "user_id": profile.user_id,
        "full_name": profile.full_name or (user.full_name if user else "") or "Passeador",
        "cpf": profile.cpf or "",
        "phone": profile.phone or "",
        "email": user.email if user else "",
        "neighborhood_region": profile.state or profile.city or "",
        "has_pet_experience": bool(profile.experience or profile.bio),
        "has_third_party_experience": bool(profile.experience),
        "experience_description": profile.experience or "",
        "availability": "",
        "profile_photo_url": profile.profile_photo_url or "",
        "document_url": profile.document_url or "",
        "proof_of_address_url": profile.proof_of_address_url or "",
        "selfie_url": profile.selfie_url or "",
        "accepted_declaration": True,
        "status": _public_status_label(profile.status),
        "raw_status": profile.status,
        "active_as_walker": bool(profile.active_as_walker and profile.status == "active"),
        "approved_at": profile.approved_at,
        "rejected_at": profile.rejected_at,
        "rejection_reason": profile.rejection_reason,
        "created_at": profile.created_at,
        "updated_at": profile.created_at,
    }
    if include_internal:
        payload["internal_notes"] = profile.internal_notes or ""
    return payload


def _missing_application_fields(*, profile_photo_url: str | None, document_url: str | None, proof_of_address_url: str | None) -> list[str]:
    missing = []
    if not (profile_photo_url or "").strip():
        missing.append("Envie sua foto de perfil.")
    if not (document_url or "").strip():
        missing.append("Envie o documento obrigatório.")
    if not (proof_of_address_url or "").strip():
        missing.append("Complete os documentos para enviar sua candidatura.")
    return missing


def _ensure_application_complete(*, profile_photo_url: str | None, document_url: str | None, proof_of_address_url: str | None):
    missing = _missing_application_fields(
        profile_photo_url=profile_photo_url,
        document_url=document_url,
        proof_of_address_url=proof_of_address_url,
    )
    if missing:
        raise HTTPException(status_code=400, detail={"message": "Cadastro de passeador incompleto.", "errors": missing})


def _require_active_walker(user: User, db: Session) -> WalkerProfile:
    profile = db.query(WalkerProfile).filter(WalkerProfile.user_id == user.id).first()
    if not profile:
        raise HTTPException(status_code=403, detail="Cadastro de passeador nao encontrado.")
    if profile.status in {"pending", "document_review"} or not profile.active_as_walker:
        raise HTTPException(status_code=403, detail="Candidatura ainda em analise.")
    if profile.status == "rejected":
        raise HTTPException(status_code=403, detail=profile.rejection_reason or "Candidatura rejeitada.")
    if profile.status in {"suspended", "restricted"}:
        raise HTTPException(status_code=403, detail="Perfil com bloqueio operacional.")
    if user.role not in {"walker", "passeador"}:
        raise HTTPException(status_code=403, detail="Usuario ainda nao liberado como passeador.")
    return profile


def _apply_profile_status(profile: WalkerProfile, status: str, reason: str | None = None, db: Session | None = None):
    raw_status = _raw_status_from_label(status)
    profile.status = raw_status
    if raw_status == "active":
        profile.active_as_walker = True
        profile.approved_at = datetime.utcnow()
        profile.rejected_at = None
        profile.rejection_reason = None
        if db:
            user = db.get(User, profile.user_id)
            if user:
                user.role = "walker"
    elif raw_status == "rejected":
        profile.active_as_walker = False
        profile.rejected_at = datetime.utcnow()
        profile.approved_at = None
        profile.rejection_reason = reason
    else:
        profile.active_as_walker = False
        profile.approved_at = None
        if raw_status != "rejected":
            profile.rejected_at = None


def _parse_date(value: str | None) -> datetime | None:
    if not value:
        return None
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d", "%d/%m/%Y %H:%M"):
        try:
            return datetime.strptime(value[:19], fmt)
        except ValueError:
            continue
    return None


def _format_expires_in(value: datetime | None) -> str:
    if not value:
        return "5 min"
    seconds = max(0, int((value - datetime.utcnow()).total_seconds()))
    minutes = max(1, (seconds + 59) // 60)
    return f"{minutes} min"


def _walk_time_parts(walk: Walk) -> tuple[str, str]:
    parsed = _parse_date(walk.scheduled_date)
    if parsed:
        return parsed.strftime("%H:%M"), parsed.strftime("%d/%m/%Y")
    return "18:00", walk.scheduled_date or "Hoje"


def _walk_payload(walk: Walk, db: Session) -> dict:
    pet = db.get(Pet, walk.pet_id) if walk.pet_id else None
    tutor = db.get(User, walk.tutor_id) if walk.tutor_id else None
    time, date = _walk_time_parts(walk)
    pet_name = pet.name if pet else "Pet"
    price = float(walk.price or 0)
    return {
        "id": walk.id,
        "pet_id": walk.pet_id,
        "pet_name": pet_name,
        "pet_photo_url": (pet.photo_url if pet else None) or DOG_PHOTOS.get(pet_name) or DOG_PHOTOS["Thor"],
        "breed": pet.breed if pet else "",
        "age": pet.age if pet else None,
        "weight": pet.weight if pet else None,
        "tutor_id": walk.tutor_id,
        "tutor_name": tutor.full_name if tutor else "Tutor",
        "tutor_phone": "",
        "date": date,
        "time": time,
        "scheduled_date": walk.scheduled_date,
        "duration_minutes": walk.duration_minutes,
        "duration": f"{walk.duration_minutes} min",
        "price": price,
        "price_label": f"R$ {price:.2f}".replace(".", ","),
        "status": walk.status,
        "area": walk.address_snapshot or "Pituba, Salvador - BA",
        "distance": "900m de voce",
        "type": "Individual",
        "payment_method": "Pagamento pelo app",
        "notes": walk.notes or "Levar agua sempre. Informe o tutor sobre qualquer ocorrencia.",
        "expires_in": "15 min",
        "is_frequent_client": True,
    }


def _completed_walks(user: User, db: Session) -> list[Walk]:
    return db.query(Walk).filter(Walk.walker_id == user.id, Walk.status == "Finalizado").all()


def _walk_started_at(walk: Walk) -> datetime | None:
    return _parse_date(walk.scheduled_date) or walk.created_at


def _period_walks(walks: list[Walk], start: datetime, end: datetime) -> list[Walk]:
    return [walk for walk in walks if (started := _walk_started_at(walk)) and start <= started < end]


def _sum_walk_values(walks: list[Walk]) -> float:
    return sum(float(walk.price or 0) for walk in walks)


def _goal_progress(current: int, target: int) -> int:
    if target <= 0:
        return 0
    return min(100, round((current / target) * 100))


def _walker_level(total_completed: int, rating_avg: float, acceptance_rate: int, cancellation_rate: int, regularity: int) -> dict:
    levels = [
        {
            "key": "iniciante",
            "name": "Iniciante",
            "min_completed_walks": 0,
            "min_rating": 0,
            "benefit": "Primeiros passos com acompanhamento e orientacoes da plataforma.",
        },
        {
            "key": "confiavel",
            "name": "Confiavel",
            "min_completed_walks": 10,
            "min_rating": 4.5,
            "benefit": "Mais consistencia para aparecer em boas oportunidades.",
        },
        {
            "key": "destaque",
            "name": "Destaque",
            "min_completed_walks": 30,
            "min_rating": 4.7,
            "benefit": "Perfil com potencial para selos e campanhas futuras.",
        },
        {
            "key": "elite_aumigao",
            "name": "Elite Aumigao",
            "min_completed_walks": 60,
            "min_rating": 4.85,
            "benefit": "Prioridade e beneficios especiais quando a regra comercial for ativada.",
        },
    ]

    current = levels[0]
    for level in levels:
        if total_completed >= level["min_completed_walks"] and rating_avg >= level["min_rating"]:
            current = level

    current_index = levels.index(current)
    next_level = levels[current_index + 1] if current_index + 1 < len(levels) else None
    score = min(
        100,
        round(
            min(total_completed, 60) * 0.75
            + rating_avg * 9
            + acceptance_rate * 0.18
            + max(0, 100 - cancellation_rate) * 0.12
            + regularity * 0.1
        ),
    )
    next_target = next_level["min_completed_walks"] if next_level else current["min_completed_walks"]
    previous_target = current["min_completed_walks"]
    range_size = max(1, next_target - previous_target)
    progress = 100 if not next_level else _goal_progress(total_completed - previous_target, range_size)

    return {
        "current": current,
        "next": next_level,
        "score": score,
        "progress_percent": progress,
        "levels": levels,
        "criteria": [
            {"label": "Passeios concluidos", "value": total_completed, "weight": "Historico total"},
            {"label": "Avaliacao media", "value": rating_avg, "weight": "Qualidade percebida"},
            {"label": "Taxa de aceite", "value": acceptance_rate, "weight": "Disponibilidade"},
            {"label": "Taxa de cancelamento", "value": cancellation_rate, "weight": "Confiabilidade"},
            {"label": "Regularidade", "value": regularity, "weight": "Presenca na agenda"},
        ],
    }


def _default_kit_submission() -> dict:
    return {
        "items": {
            "water": {"available": True, "photo_urls": ["https://images.unsplash.com/photo-1523362628745-0c100150b504?auto=format&fit=crop&w=400&q=80"]},
            "bowl": {"available": True, "photo_urls": []},
            "bags": {"available": True, "photo_urls": []},
            "first_aid": {"available": False, "photo_urls": []},
            "towel": {"available": False, "photo_urls": []},
            "premium_treats": {"available": False, "photo_urls": []},
        },
        "audit_status": "rascunho",
        "audit_note": "Envie fotos dos itens para solicitar auditoria de nivel.",
        "updated_at": None,
    }


def _build_walker_kit(user_id: str | None) -> dict:
    submission = WALKER_KIT_SUBMISSIONS.get(user_id or "", _default_kit_submission())
    submitted_items = submission.get("items", {})
    item_payloads = []
    available_keys = set()

    for definition in KIT_ITEM_DEFINITIONS:
        item_state = submitted_items.get(definition["key"], {})
        available = bool(item_state.get("available"))
        photo_urls = item_state.get("photo_urls") or []
        if available:
            available_keys.add(definition["key"])
        item_payloads.append({
            **definition,
            "available": available,
            "photo_urls": photo_urls,
            "has_photo": bool(photo_urls),
            "required_for": [tier["key"] for tier in KIT_TIERS if definition["key"] in tier["items"]],
        })

    current_tier = KIT_TIERS[0]
    for tier in KIT_TIERS:
        if all(key in available_keys for key in tier["items"]):
            current_tier = tier

    next_tier = next((tier for tier in KIT_TIERS if len(tier["items"]) > len(current_tier["items"])), None)
    target_tier = next_tier or current_tier
    missing_for_target = [key for key in target_tier["items"] if key not in available_keys]
    photo_count = sum(len(item.get("photo_urls") or []) for item in submitted_items.values())

    return {
        "level": current_tier["key"],
        "level_number": KIT_TIERS.index(current_tier) + 1,
        "label": f"Kit {current_tier['label']}",
        "ranking_bonus": current_tier["ranking_bonus"],
        "audit_status": submission.get("audit_status", "rascunho"),
        "audit_note": submission.get("audit_note", ""),
        "updated_at": submission.get("updated_at"),
        "tiers": KIT_TIERS,
        "target_level": target_tier["key"],
        "target_label": f"Kit {target_tier['label']}",
        "missing_for_target": missing_for_target,
        "photo_count": photo_count,
        "items": item_payloads,
        "public_photo_urls": [url for item in item_payloads for url in item["photo_urls"]][:6],
        "public_note": "Tutor visualiza o nivel do kit, itens confirmados e fotos enviadas no perfil do passeador.",
        "credential_note": "O nivel do kit e um parametro proprio e nao substitui score, avaliacao ou nivel operacional do passeador.",
    }


@partner_router.post("", status_code=201)
def create_partner_application(payload: PartnerApplicationCreate, db: Session = Depends(get_db)):
    if not payload.accepted_declaration:
        raise HTTPException(status_code=400, detail="Declaracao obrigatoria precisa ser aceita.")
    _ensure_application_complete(
        profile_photo_url=payload.profile_photo_url,
        document_url=payload.document_url,
        proof_of_address_url=payload.proof_of_address_url,
    )

    try:
        email = normalize_email_or_raise(payload.email)
        cpf = normalize_cpf_or_raise(payload.cpf)
        phone = normalize_phone_or_raise(payload.phone)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    user = db.query(User).filter(User.email == email).first()
    if not user:
        user = User(
            id=str(uuid4()),
            email=email,
            password_hash=get_password_hash(str(uuid4())),
            full_name=payload.full_name.strip(),
            role="cliente",
        )
        db.add(user)
        db.flush()
    else:
        user.full_name = payload.full_name.strip() or user.full_name
        if user.role in {"walker", "passeador"}:
            user.role = "cliente"

    profile = db.query(WalkerProfile).filter(WalkerProfile.user_id == user.id).first()
    document_status = "document_review" if payload.profile_photo_url else "pending"
    if not profile:
        profile = WalkerProfile(id=str(uuid4()), user_id=user.id)
        db.add(profile)

    profile.full_name = payload.full_name.strip()
    profile.cpf = cpf
    profile.phone = phone
    profile.city = payload.neighborhood_region.strip()
    profile.state = payload.neighborhood_region.strip()
    profile.experience = payload.experience_description.strip()
    profile.bio = payload.experience_description.strip()
    profile.profile_photo_url = payload.profile_photo_url
    profile.document_url = payload.document_url
    profile.proof_of_address_url = payload.proof_of_address_url
    profile.selfie_url = payload.selfie_url
    profile.status = document_status
    profile.active_as_walker = False
    profile.approved_at = None
    profile.rejected_at = None
    profile.rejection_reason = None
    db.commit()
    db.refresh(profile)
    mark_referral_under_review(user.id, db)
    return _serialize_partner_application(profile, db)


@partner_router.get("")
def list_partner_applications(db: Session = Depends(get_db)):
    return [_serialize_partner_application(profile, db) for profile in db.query(WalkerProfile).order_by(WalkerProfile.created_at.desc()).all()]


@partner_router.get("/{candidate_id}")
def get_partner_application(candidate_id: str, db: Session = Depends(get_db)):
    profile = db.get(WalkerProfile, candidate_id)
    if not profile:
        raise HTTPException(status_code=404, detail="Candidatura nao encontrada")
    return _serialize_partner_application(profile, db)


@partner_router.patch("/{candidate_id}/status")
def update_partner_application_status(candidate_id: str, payload: PartnerApplicationStatusUpdate, db: Session = Depends(get_db)):
    profile = db.get(WalkerProfile, candidate_id)
    if not profile:
        raise HTTPException(status_code=404, detail="Candidatura nao encontrada")
    _apply_profile_status(profile, payload.status, payload.reason, db)
    db.commit()
    db.refresh(profile)
    if profile.status == "active":
        mark_referral_approved(profile.user_id, db)
    elif profile.status == "rejected":
        mark_referral_rejected(profile.user_id, profile.rejection_reason, db)
    return _serialize_partner_application(profile, db, include_internal=True)


@partner_router.patch("/{candidate_id}/admin-fields")
def update_partner_application_admin_fields(candidate_id: str, payload: PartnerApplicationAdminFieldsUpdate, db: Session = Depends(get_db)):
    profile = db.get(WalkerProfile, candidate_id)
    if not profile:
        raise HTTPException(status_code=404, detail="Candidatura nao encontrada")
    if payload.internal_notes is not None:
        profile.internal_notes = payload.internal_notes
    if payload.active_as_walker is not None:
        if payload.active_as_walker and profile.status not in {"approved", "active"}:
            raise HTTPException(status_code=400, detail="Apenas candidatos aprovados podem ser ativados como passeador.")
        profile.active_as_walker = payload.active_as_walker
        profile.status = "active" if payload.active_as_walker else "approved"
        if payload.active_as_walker and not profile.approved_at:
            profile.approved_at = datetime.utcnow()
    db.commit()
    db.refresh(profile)
    return _serialize_partner_application(profile, db, include_internal=True)


def _walk_interval(walk: Walk) -> tuple[datetime, datetime] | None:
    start = _parse_date(walk.scheduled_date)
    if not start:
        return None
    return start, start + timedelta(minutes=int(walk.duration_minutes or 0))


def _has_schedule_conflict(candidate: Walk, accepted: list[Walk], buffer_minutes: int = 15) -> bool:
    candidate_interval = _walk_interval(candidate)
    if not candidate_interval:
        return False

    candidate_start, candidate_end = candidate_interval
    buffer = timedelta(minutes=buffer_minutes)

    for walk in accepted:
        if walk.id == candidate.id:
            continue
        interval = _walk_interval(walk)
        if not interval:
            continue
        start, end = interval
        if candidate_start < end + buffer and candidate_end + buffer > start:
            return True

    return False


def _available_balance(user: User, db: Session) -> float:
    payments = db.query(Payment).filter(Payment.tutor_id == user.id).all()
    if payments:
        return sum(float(payment.amount or 0) for payment in payments)
    completed_total = sum(float(walk.price or 0) for walk in _completed_walks(user, db))
    return completed_total or 245.60


def _goals_evolution_payload(user: User, db: Session) -> dict:
    completed = _completed_walks(user, db)
    now = datetime.utcnow()
    day_start = datetime(now.year, now.month, now.day)
    week_start = day_start - timedelta(days=day_start.weekday())
    month_start = datetime(now.year, now.month, 1)
    next_day = day_start + timedelta(days=1)
    next_week = week_start + timedelta(days=7)
    next_month = datetime(now.year + int(now.month == 12), 1 if now.month == 12 else now.month + 1, 1)

    day_walks = _period_walks(completed, day_start, next_day)
    week_walks = _period_walks(completed, week_start, next_week)
    month_walks = _period_walks(completed, month_start, next_month)

    has_real_data = bool(completed)
    daily_done = len(day_walks) if has_real_data else 2
    weekly_done = len(week_walks) if has_real_data else 9
    monthly_done = len(month_walks) if has_real_data else 38
    daily_earnings = _sum_walk_values(day_walks) if has_real_data else 78.90
    weekly_earnings = _sum_walk_values(week_walks) if has_real_data else 368.00
    monthly_earnings = _sum_walk_values(month_walks) if has_real_data else 1520.00
    active_days = len({(_walk_started_at(walk) or now).date().isoformat() for walk in week_walks}) if has_real_data else 4
    rating_avg = 4.9
    acceptance_rate = 88
    cancellation_rate = 3
    regularity = min(100, round((active_days / 5) * 100)) if active_days else 72
    total_completed = len(completed) if has_real_data else 38
    level = _walker_level(total_completed, rating_avg, acceptance_rate, cancellation_rate, regularity)

    return {
        "title": "Acompanhe sua evolucao",
        "subtitle": "Continue crescendo no Aumigao com referencias saudaveis de desempenho.",
        "source": "real" if has_real_data else "demo",
        "daily": {
            "label": "Meta diaria",
            "completed_walks": daily_done,
            "target_walks": 3,
            "earnings": daily_earnings,
            "progress_percent": _goal_progress(daily_done, 3),
            "message": "Voce esta no caminho certo para fechar um dia consistente.",
        },
        "weekly": {
            "label": "Meta semanal",
            "completed_walks": weekly_done,
            "target_walks": 15,
            "earnings": weekly_earnings,
            "active_days": active_days,
            "progress_percent": _goal_progress(weekly_done, 15),
            "message": "Regularidade ajuda o matching e prepara beneficios futuros.",
        },
        "monthly": {
            "label": "Meta mensal",
            "completed_walks": monthly_done,
            "target_walks": 60,
            "earnings": monthly_earnings,
            "average_rating": rating_avg,
            "progress_percent": _goal_progress(monthly_done, 60),
            "message": "Complete metas para desbloquear beneficios futuros.",
        },
        "level": level,
        "future_rewards": [
            {"label": "Bonus", "description": "Recompensas futuras podem ser ativadas em campanhas."},
            {"label": "Selo de destaque", "description": "Perfil com sinal visual para tutores quando a regra estiver ativa."},
            {"label": "Prioridade em solicitacoes", "description": "Mais chance de aparecer em matches alinhados ao seu perfil."},
            {"label": "Campanhas promocionais", "description": "Acesso a acoes especiais definidas pela plataforma."},
        ],
        "disclaimer": "Metas sao referencias de evolucao e motivacao. Nao representam regra de trabalho, carga minima ou pagamento automatico.",
        "motivational_text": "Voce esta no caminho certo.",
    }


@router.get("/profile", response_model=WalkerProfileResponse | None)
def get_profile(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    return db.query(WalkerProfile).filter(WalkerProfile.user_id == user.id).first()


@router.post("/profile", response_model=WalkerProfileResponse)
def create_profile(payload: WalkerProfileCreate, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    profile = db.query(WalkerProfile).filter(WalkerProfile.user_id == user.id).first()
    if profile:
        return update_profile(payload, user, db)
    data = payload.model_dump()
    _ensure_application_complete(
        profile_photo_url=data.get("profile_photo_url"),
        document_url=data.get("document_url"),
        proof_of_address_url=data.get("proof_of_address_url"),
    )
    try:
        if data.get("cpf"):
            data["cpf"] = normalize_cpf_or_raise(data.get("cpf"))
        if data.get("phone"):
            data["phone"] = normalize_phone_or_raise(data.get("phone"))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    has_documents = bool(data.get("document_url") or data.get("selfie_url") or data.get("proof_of_address_url"))
    profile = WalkerProfile(id=str(uuid4()), user_id=user.id, status="document_review" if has_documents else "pending", **data)
    db.add(profile)
    db.commit()
    db.refresh(profile)
    mark_referral_under_review(user.id, db)
    return profile


@router.put("/profile", response_model=WalkerProfileResponse)
def update_profile(payload: WalkerProfileUpdate, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    profile = db.query(WalkerProfile).filter(WalkerProfile.user_id == user.id).first()
    if not profile:
        profile = WalkerProfile(id=str(uuid4()), user_id=user.id)
        db.add(profile)
    data = payload.model_dump(exclude_unset=True)
    try:
        if data.get("cpf"):
            data["cpf"] = normalize_cpf_or_raise(data.get("cpf"))
        if data.get("phone"):
            data["phone"] = normalize_phone_or_raise(data.get("phone"))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    for key, value in data.items():
        setattr(profile, key, value)
    if profile.status in {"pending", ""} and (profile.document_url or profile.selfie_url or profile.proof_of_address_url):
        profile.status = "document_review"
    if profile.status in {"pending", "document_review"}:
        _ensure_application_complete(
            profile_photo_url=profile.profile_photo_url,
            document_url=profile.document_url,
            proof_of_address_url=profile.proof_of_address_url,
        )
    db.commit()
    db.refresh(profile)
    return profile


@router.get("/dashboard")
def dashboard(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    _require_active_walker(user, db)
    active = db.query(Walk).filter(Walk.walker_id == user.id, Walk.status.in_(["Indo buscar o pet", "Passeando agora"])).all()
    accepted = db.query(Walk).filter(Walk.walker_id == user.id).all()
    available = db.query(Walk).filter(Walk.walker_id.is_(None), Walk.status == "Agendado").all()
    completed = _completed_walks(user, db)
    today_total = sum(float(walk.price or 0) for walk in completed) or 55.86
    tips_total = 52.0
    potential = sum(float(walk.price or 0) for walk in available[:3]) or 180.0
    active_walk = _walk_payload(active[0], db) if active else (_walk_payload(accepted[0], db) if accepted else None)
    next_request = available[0] if available else None
    buffer_minutes = 15
    next_request_payload = _walk_payload(next_request, db) if next_request else None
    if next_request_payload:
        next_request_payload["acceptance_guard"] = {
            "min_interval_minutes": buffer_minutes,
            "has_conflict": _has_schedule_conflict(next_request, accepted, buffer_minutes),
            "message": "Aceite liberado: intervalo minimo de 15 min preservado.",
        }
    return {
        "available_requests": len(available),
        "active_walks": len(active),
        "accepted_walks": len(accepted),
        "today_earnings": today_total,
        "walk_earnings_today": today_total,
        "tips_today": 0.0,
        "tips_week": tips_total,
        "potential_earnings": potential,
        "level": "GOLD",
        "next_level": "ELITE",
        "score": 87,
        "rating_avg": 4.9,
        "rating_count": 126,
        "level_progress": 72,
        "bonus_missing_walks": max(0, 14 - (len(completed) or 11)),
        "boost_credits": 24,
        "next_request": next_request_payload,
        "active_walk": active_walk,
        "tips_summary": {
            "today": 0.0,
            "week": tips_total,
            "month": 148.0,
            "pending_review": 1,
            "policy": "Gorjetas sao opcionais e so aparecem apos passeio finalizado e pet entregue.",
            "score_policy": "Gorjeta fica no financeiro e nao altera reputacao, matching, nivel ou boost.",
        },
        "referral_program": {
            "status": "active",
            "enabled": True,
            "label": "Indicar passeador",
            "code": f"DOG-{(user.full_name or user.email or 'WALKER').split()[0].upper()[:5]}",
            "eligible": len(completed) >= 2,
            "rule_preview": "Envie indicacoes de pessoas de confianca. Aprovacao nao e automatica e bonus futuro depende de desempenho.",
        },
        "walker_kit": _build_walker_kit(user.id),
        "cr_wallet": {
            "balance": 24,
            "earned_this_week": 6,
            "source_policy": "CR e concedido pela plataforma por performance; nao e comprado pelo passeador.",
            "actions": [
                {"key": "matching_boost", "label": "Boost matching", "cost": 4, "description": "Melhora prioridade no ranking por janela curta."},
                {"key": "early_wave", "label": "Entrada antecipada", "cost": 3, "description": "Libera solicitacoes alguns minutos antes da fila comum."},
                {"key": "visual_highlight", "label": "Destaque visual", "cost": 2, "description": "Selo temporario no card do passeador."},
            ],
        },
        "matching_intelligence": {
            "score": 89,
            "summary": "Ranking combina experiencia, distancia, disponibilidade, score, avaliacao e historico.",
            "signals": [
                {"label": "Experiencia", "value": 92},
                {"label": "Distancia", "value": 84},
                {"label": "Agenda", "value": 88},
                {"label": "Avaliacao", "value": 96},
            ],
            "next_improvement": "Manter horarios 17h-20h ativos melhora a posicao em alta demanda.",
        },
        "rating_summary": {
            "rating_avg": 4.9,
            "rating_count": 126,
            "score": 87,
            "components": [
                {"label": "Avaliacoes", "value": 96},
                {"label": "Pontualidade", "value": 91},
                {"label": "Conclusao", "value": 98},
                {"label": "Ocorrencias", "value": 84},
            ],
        },
        "schedule_rules": {
            "min_interval_minutes": buffer_minutes,
            "message": "Novos aceites exigem pelo menos 15 min entre o fim de um passeio e o inicio do outro.",
            "can_accept_next_request": not (_has_schedule_conflict(next_request, accepted, buffer_minutes) if next_request else False),
        },
        "goals_evolution": _goals_evolution_payload(user, db),
        "week": [
            {"day": "Seg", "date": "19", "status": "available"},
            {"day": "Ter", "date": "20", "status": "available"},
            {"day": "Qua", "date": "21", "status": "unavailable"},
            {"day": "Qui", "date": "22", "status": "available"},
            {"day": "Sex", "date": "23", "status": "partial"},
            {"day": "Sab", "date": "24", "status": "available"},
            {"day": "Dom", "date": "25", "status": "partial"},
        ],
    }


@router.get("/earnings")
def earnings(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    _require_active_walker(user, db)
    completed = _completed_walks(user, db)
    total = sum(float(walk.price or 0) for walk in completed)
    tips = 52.0
    transactions = []
    for walk in completed:
        payload = _walk_payload(walk, db)
        transactions.append({
            "id": f"walk-{walk.id}",
            "type": "walk",
            "description": "Passeio concluido",
            "pet_name": payload["pet_name"],
            "duration": payload["duration"],
            "date": payload["date"],
            "time": payload["time"],
            "amount": float(walk.price or 0),
            "status": "paid",
        })
    if not transactions:
        transactions = [
            {"id": "demo-walk-1", "type": "walk", "description": "Passeio concluido", "pet_name": "Thor", "duration": "60 min", "date": "19/05/2025", "time": "18:20", "amount": 35.0, "status": "paid"},
            {"id": "demo-tip-1", "type": "tip", "description": "Gorjeta recebida", "pet_name": "Thor", "duration": "", "date": "19/05/2025", "time": "18:20", "amount": 10.0, "status": "paid"},
            {"id": "demo-withdraw-1", "type": "withdraw", "description": "Saque via PIX", "pet_name": "", "duration": "", "date": "17/04/2025", "time": "21:30", "amount": -120.0, "status": "paid"},
        ]
    weekly_walk_total = total or 368.0
    return {
        "available_balance": _available_balance(user, db),
        "weekly_total": weekly_walk_total,
        "completed_walks": len(completed) or 11,
        "tips": tips,
        "walk_earnings": weekly_walk_total,
        "total_with_tips": weekly_walk_total + tips,
        "tips_pending_review": 1,
        "tips_policy": "Gorjetas sao opcionais, surgem apos entrega do pet e nao entram nas metas de ganhos.",
        "goal_total_walks": 14,
        "future_reward_preview": "Beneficios futuros podem ser ativados por campanhas, selos e prioridade em solicitacoes.",
        "level": "Destaque",
        "score": 87,
        "transactions": transactions,
    }


@router.get("/goals-evolution")
def goals_evolution(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    _require_active_walker(user, db)
    return _goals_evolution_payload(user, db)


@router.put("/kit")
def update_kit(payload: dict, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    _require_active_walker(user, db)
    items_payload = (payload or {}).get("items") or []
    items = {}
    for definition in KIT_ITEM_DEFINITIONS:
        incoming = next((item for item in items_payload if item.get("key") == definition["key"]), {})
        items[definition["key"]] = {
            "available": bool(incoming.get("available")),
            "photo_urls": incoming.get("photo_urls") or [],
        }

    WALKER_KIT_SUBMISSIONS[user.id] = {
        "items": items,
        "audit_status": "em_analise",
        "audit_note": "Kit enviado para validacao. As fotos aprovadas ficarao visiveis para o tutor.",
        "updated_at": datetime.utcnow().isoformat(),
    }
    return {"ok": True, "walker_kit": _build_walker_kit(user.id)}


def _public_walker_rows(db: Session) -> list[dict]:
    profiles = db.query(WalkerProfile).filter(
        WalkerProfile.status == "active",
        WalkerProfile.active_as_walker.is_(True),
    ).order_by(WalkerProfile.created_at.desc()).all()
    if not profiles:
        if not DEMO_MODE:
            return []
        demo_reputation = {
            "rating_average": 4.9,
            "reviews_count": 126,
            "total_walks": 38,
            "level": "Destaque",
            "reputation_score": 83.4,
        }
        return [
            {
                    "id": "walker-demo-1",
                    "name": "Carlos Oliveira",
                    "full_name": "Carlos Oliveira",
                    "rating": 4.9,
                    **demo_reputation,
                    "average_rating": 4.9,
                    "city": "Salvador",
                    "neighborhood": "Pituba",
                    "bio": "Passeador verificado com kit publicado para consulta do tutor.",
                    "walk_price": 35,
                    "verified": True,
                    "walker_kit": _build_walker_kit("walker-demo-user-1"),
            }
        ]
    rows = []
    seen_keys = set()
    for profile in profiles:
        dedupe_key = profile.cpf or profile.user_id or profile.id
        if not dedupe_key or dedupe_key in seen_keys:
            continue
        seen_keys.add(dedupe_key)
        rows.append({
                **(summary := reputation_summary(profile.user_id, db)),
                "id": profile.user_id,
                "partner_id": profile.id,
                "name": profile.full_name or "Passeador",
                "full_name": profile.full_name or "Passeador",
                "photo_url": profile.profile_photo_url or "",
                "profile_photo_url": profile.profile_photo_url or "",
                "status": profile.status,
                "raw_status": profile.status,
                "active_as_walker": bool(profile.active_as_walker),
                "rating": summary["rating_average"] or 0,
                "average_rating": summary["rating_average"] or 0,
                "city": profile.city,
                "neighborhood": profile.state,
                "bio": profile.bio or "Passeador disponivel com kit publicado para consulta.",
                "walk_price": 35,
                "verified": True,
                "walker_kit": _build_walker_kit(profile.user_id),
        })
    return rows


@router.get("/public")
def public_walkers(db: Session = Depends(get_db)):
    return {"walkers": _public_walker_rows(db)}


@api_public_router.get("/walkers")
def api_public_walkers(db: Session = Depends(get_db)):
    return _public_walker_rows(db)


@router.get("/availability")
def availability(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    _require_active_walker(user, db)
    return {
        "week": [
            {"day": "Seg 22", "status": "available", "possible_walks": 3},
            {"day": "Ter 23", "status": "unavailable", "possible_walks": 0},
            {"day": "Qua 24", "status": "partial", "possible_walks": 2},
            {"day": "Qui 25", "status": "available", "possible_walks": 4},
            {"day": "Sex 26", "status": "available", "possible_walks": 3},
        ],
        "slots": ["07:00", "08:00", "09:00", "14:00", "15:00", "17:00", "18:00", "19:00", "20:00"],
        "month": {
            "label": "Abril 2026",
            "estimated_earnings": 3240,
            "possible_walks": 42,
            "available_days": 20,
        },
    }


@router.put("/availability")
def update_availability(payload: dict, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    _require_active_walker(user, db)
    return {"ok": True, "user_id": user.id, **payload}


@router.get("/requests")
def requests(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    _require_active_walker(user, db)
    process_expired_attempts(db)
    walks = (
        db.query(Walk)
        .filter(
            Walk.assigned_walker_id == user.id,
            Walk.operational_status.in_(["pending_walker_confirmation", "auto_rematching"]),
        )
        .all()
    )
    payloads = []
    for walk in walks:
        payload = _walk_payload(walk, db)
        operational = serialize_operational_walk(walk, db, user=user)
        payload.update({
            "operational_status": operational["operational_status"],
            "matching_attempts": operational["matching_attempts"],
            "pickup_privacy_level": operational["pickup_privacy_level"],
            "area": operational.get("pickup_region_label") or payload["area"],
            "distance": operational.get("pickup_distance_label") or payload["distance"],
            "address_snapshot": "",
            "notes": "",
            "expires_in": _format_expires_in(walk.confirmation_expires_at),
            "response_deadline_at": walk.confirmation_expires_at,
        })
        payloads.append(payload)
    return payloads


@router.get("/walks")
def walker_walks(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    _require_active_walker(user, db)
    process_expired_attempts(db)
    walks = db.query(Walk).filter(Walk.walker_id == user.id).all()
    return [serialize_operational_walk(walk, db, user=user) for walk in walks]


@router.post("/walks/{walk_id}/accept")
def accept_walk(walk_id: str, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    _require_active_walker(user, db)
    walk = db.get(Walk, walk_id)
    if not walk:
        raise HTTPException(status_code=404, detail="Passeio nao encontrado")
    accepted = db.query(Walk).filter(Walk.walker_id == user.id, Walk.status.in_(["Agendado", "Indo buscar o pet", "Passeando agora"])).all()
    if _has_schedule_conflict(walk, accepted, 15):
        raise HTTPException(status_code=409, detail="Este passeio conflita com sua agenda. Mantenha ao menos 15 min entre passeios.")
    accept_operational_walk(walk, user, db)
    db.commit()
    db.refresh(walk)
    return {"ok": True, "walk_id": walk_id, "walk": serialize_operational_walk(walk, db, user=user)}


@router.post("/walks/{walk_id}/decline")
def decline_walk(walk_id: str, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    _require_active_walker(user, db)
    walk = db.get(Walk, walk_id)
    if not walk:
        raise HTTPException(status_code=404, detail="Passeio nao encontrado")
    decline_operational_walk(walk, user, db)
    db.commit()
    db.refresh(walk)
    return {"ok": True, "walk_id": walk_id, "walk": serialize_operational_walk(walk, db, user=user)}


@router.post("/walks/{walk_id}/status")
def walker_status(walk_id: str, payload: dict, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    _require_active_walker(user, db)
    walk = db.get(Walk, walk_id)
    if not walk:
        raise HTTPException(status_code=404, detail="Passeio nao encontrado")
    if walk.walker_id not in {None, user.id}:
        raise HTTPException(status_code=403, detail="Passeio nao pertence ao passeador")
    if walk.walker_id not in {None, user.id}:
        raise HTTPException(status_code=403, detail="Passeio nao pertence ao passeador")
    if walk.operational_status in {"pending_walker_confirmation", "auto_rematching"}:
        accept_operational_walk(walk, user, db)
    update_operational_status(walk, payload.get("status", walk.status), db, actor=user)
    db.commit()
    db.refresh(walk)
    return {"ok": True, "status": walk.status, "walk": serialize_operational_walk(walk, db, user=user)}


@router.post("/walks/{walk_id}/report")
def send_report(walk_id: str, payload: dict | None = None, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    walk = db.get(Walk, walk_id)
    if not walk:
        raise HTTPException(status_code=404, detail="Passeio nao encontrado")
    if walk.walker_id != user.id:
        raise HTTPException(status_code=403, detail="Passeio nao pertence ao passeador")
    update_operational_status(walk, "Finalizado", db, actor=user)
    db.add(Payment(id=str(uuid4()), tutor_id=user.id, walk_id=walk.id, amount=float(walk.price or 0), status="paid", provider="internal"))
    db.commit()
    return {"ok": True, "walk_id": walk_id, "status": walk.status, "report": payload or {}}


@router.post("/walks/{walk_id}/occurrence")
def create_walker_occurrence(walk_id: str, payload: dict, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    walk = db.get(Walk, walk_id)
    if not walk:
        raise HTTPException(status_code=404, detail="Passeio nao encontrado")
    if walk.walker_id != user.id:
        raise HTTPException(status_code=403, detail="Passeio nao pertence ao passeador")
    target_type = payload.get("target_type") or "tutor"
    complaint_payload = ComplaintCreate(
        source="walker",
        target_type=target_type,
        target_user_id=walk.tutor_id if target_type in {"tutor", "address", "service"} else payload.get("target_user_id"),
        target_pet_id=walk.pet_id if target_type == "pet" else payload.get("target_pet_id"),
        walk_id=walk.id,
        category=payload.get("category") or "ocorrencia_operacional",
        title=payload.get("title") or "Ocorrencia operacional do passeio",
        description=payload.get("description") or payload.get("notes") or "Passeador registrou uma ocorrencia operacional.",
        evidences=[ComplaintEvidenceCreate(**item) for item in payload.get("evidences", [])],
        metadata={"origin": "walker_walk", **(payload.get("metadata") or {})},
    )
    return create_complaint(complaint_payload, user, db)


@router.post("/withdrawals")
def request_withdrawal(payload: dict, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    amount = float(payload.get("amount") or 0)
    if amount < 20:
        raise HTTPException(status_code=400, detail="Valor minimo para saque e R$ 20,00")
    balance = _available_balance(user, db)
    if amount > balance:
        raise HTTPException(status_code=400, detail="Saldo insuficiente")
    payment = Payment(id=str(uuid4()), tutor_id=user.id, walk_id=None, amount=-amount, status="pending", provider="pix")
    db.add(payment)
    db.commit()
    return {"ok": True, "withdrawal_id": payment.id, "amount": amount, "status": "pending"}
