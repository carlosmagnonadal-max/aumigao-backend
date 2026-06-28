import os
import logging
import json
from typing import Any
from datetime import date, datetime, timedelta
from pathlib import Path
from uuid import uuid4

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, Request, UploadFile
from pydantic import BaseModel, Field, field_validator
from sqlalchemy.orm import Session

from app.core.database import get_db, get_walker_self_db
from app.dependencies.auth import get_current_user
from app.dependencies.rbac import require_permission
from app.services.upload_validation import enforce_upload_rate_limit, read_image_upload_safely
from app.services import object_storage
from app.services.signed_uploads import UPLOAD_ROOT as UPLOADS_BASE
from app.services.upload_registry import record_upload
from app.models.payment import Payment
from app.models.pet import Pet
from app.models.tenant import Tenant
from app.models.tenant_walker_access import TenantWalkerAccess
from app.models.user import User
from app.models.walk import Walk, WalkMatchingAttempt
from app.models.walk_completion_review import WalkCompletionReview
from app.models.walk_review import WalkReview
from app.models.walk_tip import WalkTip
from app.models.walker_kit_submission import WalkerKitSubmission
from app.models.walker_profile import WalkerProfile
from app.models.walker_background_certificate import WalkerBackgroundCertificate
from app.services.background_check_service import (
    compute_background_status,
    official_validation_url as background_check_official_url,
)
from app.services.background.registry import get_background_provider
from app.models.walker_availability import WalkerAvailability
from app.models.walker_availability_exception import WalkerAvailabilityException
from app.schemas.walker_availability import WalkerAvailabilityUpdate
from app.schemas.walker_presence import WalkerOnlineUpdate
from app.schemas.walker_profile import WalkerProfileCreate, WalkerProfileResponse, WalkerProfileUpdate
from app.schemas.complaint import ComplaintCreate, ComplaintEvidenceCreate
from app.services.complaint_service import create_complaint
from app.services.identity_uniqueness import ensure_unique_identity
from app.models.walker_review import WalkerReview
from app.services.reputation_service import COMPLETED_STATUSES as _WALK_COMPLETED_STATUSES, reputation_summary, walker_level
from app.services.walker_referrals import mark_referral_under_review
from app.utils.registration_validation import normalize_cpf_or_raise, normalize_email_or_raise, normalize_phone_or_raise
from app.services.operational_matching_service import (
    accept_walk as accept_operational_walk,
    decline_walk as decline_operational_walk,
    log_event,
    process_expired_attempts,
    serialize_operational_walk,
    start_matching,
    update_operational_status,
    _batch_live_tracking,
)
from app.services.walker_operational_score_service import calculate_walker_operational_score
from app.routes.notifications import NotificationCreate, _create_notification
from app.constants import PAID_PAYMENT_STATUSES as _PAID_PAYMENT_STATUSES_CONST
from app.constants import WALK_COMPLETED_STATUSES as _WALK_COMPLETED_STATUSES
from app.constants import (
    LEVEL_PRATA_MIN_WALKS,
    LEVEL_PRATA_MIN_RATING,
    LEVEL_OURO_MIN_WALKS,
    LEVEL_OURO_MIN_RATING,
    LEVEL_DIAMANTE_MIN_WALKS,
    LEVEL_DIAMANTE_MIN_RATING,
)
from app.enums import canonical_application_status as _canonical_application_status_impl
from app.utils.url_utils import normalize_media_url as _normalize_media_url
router = APIRouter(prefix="/walker", tags=["walker"])
api_public_router = APIRouter(prefix="/api", tags=["walkers"])

DEMO_MODE = os.getenv("EXPO_PUBLIC_DEMO_MODE", os.getenv("DEMO_MODE", "false")).strip().lower() in {"1", "true", "yes", "on"}

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

LOGGER = logging.getLogger("aumigao.walker_applications")
UPLOAD_ROOT = UPLOADS_BASE / "walker-documents"
WALK_COMPLETION_UPLOAD_ROOT = UPLOADS_BASE / "walk-completions"
WALKER_KIT_UPLOAD_ROOT = UPLOADS_BASE / "walker-kit"
ALLOWED_UPLOAD_TYPES = {
    "profile_photo",
    "identity_front",
    "identity_back",
    "address_proof",
    "selfie",
    "background_certificate",  # FIX 5: tipo dedicado para certidão de antecedentes
}
ALLOWED_UPLOAD_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".heic"}
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
    "local",
    "auditoria",
)



def _public_upload_url(request: Request, path: Path) -> str:
    relative = path.relative_to(UPLOADS_BASE).as_posix()
    configured_base_url = os.getenv("PUBLIC_BACKEND_URL", "").strip().rstrip("/")
    base_url = configured_base_url or str(request.base_url).rstrip("/")
    if "railway.app" in base_url and base_url.startswith("http://"):
        base_url = base_url.replace("http://", "https://", 1)
    return f"{base_url}/uploads/{relative}"


# Alias: mantém o nome exportado para partner_application.py e outros call sites.
_normalize_public_walker_image_url = _normalize_media_url


def _public_walker_avatar_url(profile: WalkerProfile) -> str:
    return _normalize_public_walker_image_url(profile.profile_photo_url) or ""


def _safe_upload_extension(filename: str | None, content_type: str | None) -> str:
    suffix = Path(filename or "").suffix.lower()
    if suffix in ALLOWED_UPLOAD_EXTENSIONS:
        return suffix
    if content_type == "image/png":
        return ".png"
    if content_type == "image/webp":
        return ".webp"
    if content_type in {"image/heic", "image/heif"}:
        return ".heic"
    if content_type == "image/jpeg":
        return ".jpg"
    # G3: extensão/tipo não reconhecido — rejeitar explicitamente.
    raise HTTPException(status_code=400, detail="Tipo de arquivo nao suportado.")


# Alias para retrocompat: partner_application.py importa este nome deste módulo.
_canonical_application_status = _canonical_application_status_impl


def _raw_status_from_label(status: str) -> str:
    return _canonical_application_status(status)


def _public_status_label(status: str | None) -> str:
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


def _missing_application_fields(*, profile_photo_url: str | None, document_url: str | None, identity_document_back_url: str | None, proof_of_address_url: str | None, bio: str | None) -> list[str]:
    missing = []
    if not _is_persistent_upload_url(profile_photo_url):
        missing.append("Envie sua foto de perfil.")
    if len((bio or "").strip()) < 80:
        missing.append("Escreva uma breve apresentação para os tutores.")
    if not _is_persistent_upload_url(document_url):
        missing.append("Envie a frente do documento de identidade.")
    if not _is_persistent_upload_url(identity_document_back_url):
        missing.append("Envie o verso do documento de identidade.")
    if not _is_persistent_upload_url(proof_of_address_url):
        missing.append("Complete os documentos para enviar sua candidatura.")
    return missing


def _is_persistent_upload_url(value: str | None) -> bool:
    normalized = (value or "").strip().lower()
    if not normalized:
        return False
    if normalized.startswith(("demo://", "mock://", "fallback://", "sample://", "local://", "beta://", "file://", "content://", "blob:", "data:image")):
        return DEMO_MODE
    return normalized.startswith(("http://", "https://", "/uploads/"))


def _ensure_application_complete(*, profile_photo_url: str | None, document_url: str | None, identity_document_back_url: str | None, proof_of_address_url: str | None, bio: str | None):
    missing = _missing_application_fields(
        profile_photo_url=profile_photo_url,
        document_url=document_url,
        identity_document_back_url=identity_document_back_url,
        proof_of_address_url=proof_of_address_url,
        bio=bio,
    )
    if missing:
        raise HTTPException(status_code=400, detail={"message": "Cadastro de passeador incompleto.", "errors": missing})


def _require_active_walker(user: User, db: Session) -> WalkerProfile:
    profile = db.query(WalkerProfile).filter(WalkerProfile.user_id == user.id).first()
    if not profile:
        raise HTTPException(status_code=403, detail="Cadastro de passeador nao encontrado.")
    if _canonical_application_status(profile.status) in {"submitted", "under_review", "resubmission_requested", "approved"} or not profile.active_as_walker:
        raise HTTPException(status_code=403, detail="Candidatura ainda em analise.")
    if profile.status == "rejected":
        raise HTTPException(status_code=403, detail=profile.rejection_reason or "Candidatura rejeitada.")
    if _canonical_application_status(profile.status) == "blocked":
        raise HTTPException(status_code=403, detail="Perfil com bloqueio operacional.")
    if user.role not in {"walker", "passeador"}:
        raise HTTPException(status_code=403, detail="Usuario ainda nao liberado como passeador.")
    return profile


def _is_public_real_walker(profile: WalkerProfile, user: User | None) -> bool:
    if DEMO_MODE:
        return True
    if profile.status != "active" or not profile.active_as_walker:
        return False
    if not user or user.role not in {"walker", "passeador"}:
        return False

    searchable = " ".join([
        profile.full_name or "",
        profile.cpf or "",
        profile.phone or "",
        profile.id or "",
        profile.user_id or "",
        user.email if user else "",
        user.full_name if user else "",
    ]).strip().lower()
    return not any(token in searchable for token in FAKE_WALKER_TOKENS)


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

def _public_pet_photo_url(pet: Pet | None, pet_name: str) -> str:
    photo_url = (pet.photo_url if pet else "") or ""
    if photo_url and not photo_url.startswith(("file://", "content://", "blob:")):
        return photo_url
    return ""

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
        "pet_photo_url": _public_pet_photo_url(pet, pet_name),
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


def _completed_walks(user: User, db: Session, limit: int = 200) -> list[Walk]:
    # F16: limite são para evitar carga excessiva.
    # FIX 1: usar conjunto canônico de status concluídos (_WALK_COMPLETED_STATUSES)
    # em vez de apenas "Finalizado", para não zerar ganhos de walks com status
    # canônico "ride_completed" ou outros variantes.
    return (
        db.query(Walk)
        .filter(Walk.walker_id == user.id, Walk.status.in_(_WALK_COMPLETED_STATUSES))
        .order_by(Walk.created_at.desc())
        .limit(limit)
        .all()
    )


def _walk_started_at(walk: Walk) -> datetime | None:
    return _parse_date(walk.scheduled_date) or walk.created_at


def _period_walks(walks: list[Walk], start: datetime, end: datetime) -> list[Walk]:
    return [walk for walk in walks if (started := _walk_started_at(walk)) and start <= started < end]


def _sum_walk_values(walks: list[Walk]) -> float:
    return sum(float(walk.price or 0) for walk in walks)


def _walker_tips_total(walker_id: str, db: Session) -> float:
    """Soma gorjetas pagas (status='paid') do walker a partir de WalkTip."""
    tips = (
        db.query(WalkTip)
        .filter(WalkTip.walker_id == walker_id, WalkTip.status == "paid")
        .all()
    )
    return sum(float(t.amount or 0) for t in tips)


def _walker_tips_week(walker_id: str, db: Session) -> float:
    """Soma gorjetas pagas na semana corrente."""
    now = datetime.utcnow()
    week_start = datetime(now.year, now.month, now.day) - timedelta(days=datetime.utcnow().weekday())
    tips = (
        db.query(WalkTip)
        .filter(
            WalkTip.walker_id == walker_id,
            WalkTip.status == "paid",
            WalkTip.created_at >= week_start,
        )
        .all()
    )
    return sum(float(t.amount or 0) for t in tips)


def _goal_progress(current: int, target: int) -> int:
    if target <= 0:
        return 0
    return min(100, round((current / target) * 100))


def _walker_level(total_completed: int, rating_avg: float, acceptance_rate: int, cancellation_rate: int, regularity: int) -> dict:
    # Cortes referenciam as constantes canônicas de app.constants (B3).
    levels = [
        {
            "key": "iniciante",
            "name": "Bronze",
            "min_completed_walks": 0,
            "min_rating": 0,
            "benefit": "Primeiros passos com acompanhamento e orientacoes da plataforma.",
        },
        {
            "key": "confiavel",
            "name": "Prata",
            "min_completed_walks": LEVEL_PRATA_MIN_WALKS,
            "min_rating": LEVEL_PRATA_MIN_RATING,
            "benefit": "Mais consistencia para aparecer em boas oportunidades.",
        },
        {
            "key": "destaque",
            "name": "Ouro",
            "min_completed_walks": LEVEL_OURO_MIN_WALKS,
            "min_rating": LEVEL_OURO_MIN_RATING,
            "benefit": "Perfil com potencial para selos e campanhas futuras.",
        },
        {
            "key": "elite_aumigao",
            "name": "Diamante",
            "min_completed_walks": LEVEL_DIAMANTE_MIN_WALKS,
            "min_rating": LEVEL_DIAMANTE_MIN_RATING,
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
            "water": {"available": False, "photo_urls": []},
            "bowl": {"available": False, "photo_urls": []},
            "bags": {"available": False, "photo_urls": []},
            "first_aid": {"available": False, "photo_urls": []},
            "towel": {"available": False, "photo_urls": []},
            "premium_treats": {"available": False, "photo_urls": []},
        },
        "audit_status": "rascunho",
        "audit_note": "Envie fotos dos itens para solicitar auditoria de nivel.",
        "updated_at": None,
    }


def _kit_submission_payload(row: WalkerKitSubmission | None) -> dict:
    if not row:
        return _default_kit_submission()
    try:
        items = json.loads(row.items_json or "{}")
    except (TypeError, ValueError):
        items = {}
    return {
        "items": items if isinstance(items, dict) else {},
        "audit_status": row.audit_status,
        "audit_note": row.audit_note,
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
    }


def _build_walker_kit(user_id: str | None, db: Session | None = None) -> dict:
    row = None
    if db is not None and user_id:
        row = db.query(WalkerKitSubmission).filter(WalkerKitSubmission.walker_user_id == user_id).first()
    submission = _kit_submission_payload(row)
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


def _walk_review_tags(review: WalkReview) -> list[str]:
    try:
        parsed = json.loads(review.tags_json or "[]")
        return [str(item) for item in parsed if item]
    except (TypeError, ValueError):
        return []


def _walk_review_reputation_summary(walker_id: str | None, db: Session) -> dict:
    if not walker_id:
        return {
            "rating_avg": 0,
            "rating_count": 0,
            "recent_review_comments": [],
            "top_review_tags": [],
        }

    reviews = (
        db.query(WalkReview)
        .filter(WalkReview.walker_id == walker_id)
        .order_by(WalkReview.created_at.desc())
        .all()
    )
    rating_count = len(reviews)
    rating_avg = round(sum(review.rating for review in reviews) / rating_count, 2) if rating_count else 0
    tag_counts: dict[str, int] = {}
    for review in reviews:
        for tag in _walk_review_tags(review):
            tag_counts[tag] = tag_counts.get(tag, 0) + 1
    top_review_tags = [
        {"tag": tag, "count": count}
        for tag, count in sorted(tag_counts.items(), key=lambda item: (-item[1], item[0]))[:5]
    ]
    recent_review_comments = [
        {
            "id": review.id,
            "walk_id": review.walk_id,
            "rating": review.rating,
            "comment": review.comment,
            "created_at": review.created_at,
        }
        for review in reviews
        if review.comment
    ][:5]
    return {
        "rating_avg": rating_avg,
        "rating_count": rating_count,
        "recent_review_comments": recent_review_comments,
        "top_review_tags": top_review_tags,
    }


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


def _resolve_tenant_name(db: Session, tenant_id: str | None) -> str | None:
    """Retorna o display_name (ou name) do tenant, ou None se não encontrado."""
    if not tenant_id:
        return None
    try:
        from app.models.tenant import Tenant as _Tenant
        t = db.get(_Tenant, tenant_id)
        if not t:
            return None
        branding = getattr(t, "branding", None)
        if branding and getattr(branding, "display_name", None):
            return branding.display_name
        return t.name
    except Exception:
        return None


# ─── Constantes de status de pagamento para bucketização ───────────────────
# Status "pendentes" do ponto de vista do walker (aguardando confirmação):
_PENDING_PAYMENT_STATUSES: frozenset[str] = frozenset({
    "pagamento_sandbox_criado",
    "aguardando_pagamento",
    "pending",
})
# Status "em processamento" (análise de risco, risco de chargeback etc.):
_PROCESSING_PAYMENT_STATUSES: frozenset[str] = frozenset({
    "em_processamento",
    "AWAITING_RISK_ANALYSIS",
    "awaiting_chargeback_reversal",
    "AWAITING_CHARGEBACK_REVERSAL",
})
# Status de saque que REDUZEM o saldo do walker (Task 4 — fix double-spend).
# Regra correta: {pending, paid} descontam; rejected NÃO desconta.
# "approved" removido: admin.py/approve_withdrawal grava status="paid" diretamente
# (nunca grava "approved" em Payment de saque), portanto "approved" era status morto.
# Antes: _balance_by_tenant descontava todos (incluindo rejected) e
#        _available_balance só descontava {pending, approved} (omitia paid).
_WITHDRAWAL_DEDUCT_STATUSES: frozenset[str] = _PENDING_PAYMENT_STATUSES | frozenset({"paid"})


def _balance_by_tenant(user: User, db: Session) -> dict[str | None, dict]:
    """Saldo do walker desagregado por tenant_id.

    Retorna dict cujas chaves são tenant_id (str | None). A entrada com chave
    None (sentinel "_untenanted") representa saques/pagamentos SEM tenant_id —
    legado anterior à Fase 1. NÃO é distribuído entre os outros tenants para
    não corromper saldos por tenant. O caller pode exibir ou ignorar.

    Estrutura de cada valor:
      {
        "available": float,   # soma de walker_amount dos pagamentos pagos
        "pending":   float,   # idem, status pendente
        "processing": float,  # idem, status processando
        "total":     float,   # available + pending + processing
      }

    Notas de escopo:
    - Apenas pagamentos com walker_amount IS NOT NULL são incluídos (exige que
      o split tenha sido calculado). Pagamentos sem split (walk.price fallback)
      não têm tenant_id útil e seriam impossíveis de atribuir de forma confiável.
    - Tips NÃO estão incluídas aqui: WalkTip não carrega tenant_id de forma
      explícita. Tips continuam somente no consolidado flat (campo `tips`).
    - Saques são identificados por provider="pix" e walk_id IS NULL, usando
      tutor_id == user.id (o campo é reaproveitado para o walker no saque).
    - Saques com tenant_id NULL (legado) ficam na entrada None e reduzem apenas
      o available da entrada None — NÃO subtraídos de outros tenants.
    """
    buckets: dict[str | None, dict] = {}

    def _bucket(tid: str | None) -> dict:
        if tid not in buckets:
            buckets[tid] = {"available": 0.0, "pending": 0.0, "processing": 0.0, "total": 0.0}
        return buckets[tid]

    # Créditos: pagamentos de walk vinculados ao walker (via Walk.walker_id)
    # que possuem walker_amount calculado.
    credit_payments = (
        db.query(Payment)
        .join(Walk, Payment.walk_id == Walk.id)
        .filter(
            Walk.walker_id == user.id,
            Payment.walker_amount.isnot(None),
        )
        .all()
    )
    for p in credit_payments:
        b = _bucket(p.tenant_id)
        val = float(p.walker_amount or 0)
        if p.status in _PAID_PAYMENT_STATUSES_CONST:
            b["available"] += val
        elif p.status in _PENDING_PAYMENT_STATUSES:
            b["pending"] += val
        elif p.status in _PROCESSING_PAYMENT_STATUSES:
            b["processing"] += val
        # status "falha_pagamento" ou outros: ignorar (não afeta saldo)

    # Débitos: saques do walker (provider="pix", walk_id IS NULL, tutor_id=walker).
    # Reduzem o `available` do tenant correspondente (ou None se sem tenant_id).
    # Filtro de status: só {pending, paid} descontam; rejected NÃO desconta
    # (Task 4 — fix: antes todos os status eram descontados, incluindo rejected).
    # "approved" removido: admin.py grava "paid" diretamente ao aprovar (nunca "approved").
    debit_payments = (
        db.query(Payment)
        .filter(
            Payment.tutor_id == user.id,
            Payment.provider == "pix",
            Payment.walk_id.is_(None),
            Payment.amount < 0,
            Payment.status.in_(_WITHDRAWAL_DEDUCT_STATUSES),
        )
        .all()
    )
    for p in debit_payments:
        b = _bucket(p.tenant_id)
        # amount já é negativo; abatemos do available (pode ir negativo em edge cases)
        b["available"] += float(p.amount or 0)

    # Fase 2: ganhos da REDE vêm do ledger WalkerEarning (não de Payment.walker_amount).
    from app.services.walker_earning_service import network_earnings_by_tenant
    for tid, vals in network_earnings_by_tenant(db, user.id).items():
        b = _bucket(tid)
        b["available"] += vals["available"]
        b["pending"] += vals["areceber"]   # 'a receber' = liberação futura (cadência)

    # Arredondamento e cálculo do total
    for b in buckets.values():
        b["available"] = round(b["available"], 2)
        b["pending"] = round(b["pending"], 2)
        b["processing"] = round(b["processing"], 2)
        b["total"] = round(b["available"] + b["pending"] + b["processing"], 2)

    return buckets


def _available_balance(user: User, db: Session) -> float:
    """Saldo disponível do walker.

    F07: O walker RECEBE pelos passeios — Payment.tutor_id é o pagador (tutor),
    não o recebedor. Usa walker_amount se preenchido; caso contrário, calcula
    como soma dos preços dos walks concluídos.
    Desconta saques (payments com provider='pix' onde o walker_id está no walk_id).
    Fonte real: Walk concluídos + Payment.walker_amount quando disponível.

    Correção fallback per-walk (Fase 2):
    A Fase 2 grava Payment.walker_amount = 0.0 (NOT NULL) para passeios de REDE.
    O antigo fallback all-or-nothing ignorava passeios LEGADOS (walker_amount IS NULL)
    sempre que havia qualquer Payment com walker_amount gravado (mesmo 0.0).
    Novo comportamento: fallback POR PASSEIO — walks sem Payment pago com split
    entram pelo preço cheio; walks com split entram pelo walker_amount. Sem dupla contagem.
    """
    # Pagamentos pagos com split calculado (walker_amount IS NOT NULL).
    # Status de pagamento CONFIRMADO — inclui variantes do Asaas (PAID_PAYMENT_STATUSES).
    payments_with_split = (
        db.query(Payment)
        .join(Walk, Payment.walk_id == Walk.id)
        .filter(
            Walk.walker_id == user.id,
            Payment.status.in_(_PAID_PAYMENT_STATUSES_CONST),
            Payment.walker_amount.isnot(None),
        )
        .all()
    )

    # Conjunto de walk_id que já têm Payment pago com split — usados para excluir
    # esses walks do fallback de walk.price (evita dupla contagem).
    walk_ids_with_split: set[str] = {p.walk_id for p in payments_with_split if p.walk_id}

    # Crédito dos passeios com split calculado: usa walker_amount (pode ser 0.0 na REDE).
    gross = sum(float(p.walker_amount or 0) for p in payments_with_split)

    # Fallback per-walk: walks concluídos SEM Payment pago com split → preço cheio.
    # Captura passeios legados (pré-split) mesmo quando existem passeios de REDE.
    legacy_walks = [
        walk for walk in _completed_walks(user, db)
        if walk.id not in walk_ids_with_split
    ]
    gross += sum(float(walk.price or 0) for walk in legacy_walks)

    # Gorjetas pagas
    gross += _walker_tips_total(user.id, db)

    # FIX 7 + Task 4: descontar saques em {pending, paid} para evitar double-spend.
    # 'paid' foi adicionado (Task 4): antes só {pending} eram descontados,
    # permitindo que o passeador sacasse e, após aprovação (→paid), o saldo voltasse inteiro.
    # 'approved' removido: admin.py grava "paid" diretamente (nunca grava "approved" em saque).
    # Espelha a lógica de débito de _balance_by_tenant (provider='pix', walk_id IS NULL, amount<0).
    pending_withdrawals = (
        db.query(Payment)
        .filter(
            Payment.tutor_id == user.id,
            Payment.provider == "pix",
            Payment.walk_id.is_(None),
            Payment.amount < 0,
            Payment.status.in_(_WITHDRAWAL_DEDUCT_STATUSES),
        )
        .all()
    )
    # amount já é negativo; somar reduz o saldo disponível
    gross += sum(float(p.amount or 0) for p in pending_withdrawals)

    # Fase 2: somar ganhos da REDE já liberados (payable_at <= now) do ledger.
    from app.services.walker_earning_service import network_earnings_by_tenant
    _net = network_earnings_by_tenant(db, user.id)
    gross += sum(v["available"] for v in _net.values())

    return round(gross, 2)


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

    # M-05 fix: when the walker has real data, compute the three metrics from real
    # sources (was hardcoded 4.9/88/3 even for real walkers). In demo mode (no
    # completed walks) keep the demo values so the demo screen stays coherent with
    # the other fabricated numbers above. Real-but-empty (completed walks but zero
    # reviews/attempts) => 0, never a fabricated value. Queries run only in the
    # real-data branch to avoid wasted work in demo mode.
    if has_real_data:
        # rating_avg: average of WalkReview.rating for this walker (0 when no reviews).
        rating_avg = _walk_review_reputation_summary(user.id, db)["rating_avg"]

        # acceptance_rate: accepted / (accepted + declined + expired) * 100.
        # "skipped" and "pending" excluded — skipped was never offered, pending is open.
        attempts = (
            db.query(WalkMatchingAttempt)
            .filter(
                WalkMatchingAttempt.walker_id == user.id,
                WalkMatchingAttempt.status.in_(("accepted", "declined", "expired")),
            )
            .all()
        )
        total_decided = len(attempts)
        accepted_count = sum(1 for a in attempts if a.status == "accepted")
        acceptance_rate = round((accepted_count / total_decided) * 100) if total_decided else 0

        # cancellation_rate: cancelled walks / total walks * 100.
        # Uses Walk.status == "Cancelado" (canonical value per behavior_score_service).
        all_walks = db.query(Walk).filter(Walk.walker_id == user.id).all()
        total_walks = len(all_walks)
        cancelled_walks = sum(1 for w in all_walks if (w.status or "").strip().lower() == "cancelado")
        cancellation_rate = round((cancelled_walks / total_walks) * 100) if total_walks else 0
    else:
        rating_avg = 4.9
        acceptance_rate = 88
        cancellation_rate = 3
    regularity = min(100, round((active_days / 5) * 100)) if active_days else 72
    total_completed = len(completed) if has_real_data else 38
    level = _walker_level(total_completed, rating_avg, acceptance_rate, cancellation_rate, regularity)
    # B3: corrige o `current.name` para o nível oficial (com fatores de trust).
    # _walker_level já computa score/progress/critérios corretamente; apenas o nome
    # do nível atual é sobrescrito pela fonte de verdade (compute_walker_trust).
    if has_real_data:
        from app.services.walker_trust_service import compute_walker_trust as _compute_trust
        official_level = _compute_trust(db, user.id)["level"]
        level["current"] = {**level["current"], "name": official_level}

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
    profile = db.query(WalkerProfile).filter(WalkerProfile.user_id == user.id).first()
    if not profile:
        return None
    return {
        **profile.__dict__,
        **_walk_review_reputation_summary(user.id, db),
        **calculate_walker_operational_score(user.id, db),
    }


@router.post("/profile", response_model=WalkerProfileResponse)
def create_profile(payload: WalkerProfileCreate, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    profile = db.query(WalkerProfile).filter(WalkerProfile.user_id == user.id).first()
    if profile:
        return update_profile(payload, user, db)
    data = payload.model_dump()
    if data.get("identity_document_front_url") and not data.get("document_url"):
        data["document_url"] = data.pop("identity_document_front_url")
    else:
        data.pop("identity_document_front_url", None)
    data["profile_photo_url"] = _normalize_public_walker_image_url(data.get("profile_photo_url"))
    _ensure_application_complete(
        profile_photo_url=data.get("profile_photo_url"),
        document_url=data.get("document_url"),
        identity_document_back_url=data.get("identity_document_back_url"),
        proof_of_address_url=data.get("proof_of_address_url"),
        bio=data.get("bio") or data.get("experience"),
    )
    try:
        if data.get("cpf"):
            data["cpf"] = normalize_cpf_or_raise(data.get("cpf"))
        if data.get("phone"):
            data["phone"] = normalize_phone_or_raise(data.get("phone"))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    ensure_unique_identity(db, cpf=data.get("cpf") or None, phone=data.get("phone") or None, current_user_id=user.id)
    profile = WalkerProfile(
        id=str(uuid4()),
        user_id=user.id,
        status="submitted",
        active_as_walker=False,
        updated_at=datetime.utcnow(),
        **data,
    )    
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
    if data.get("identity_document_front_url") and not data.get("document_url"):
        data["document_url"] = data.pop("identity_document_front_url")
    else:
        data.pop("identity_document_front_url", None)
    if "profile_photo_url" in data:
        data["profile_photo_url"] = _normalize_public_walker_image_url(data.get("profile_photo_url"))
    try:
        if data.get("cpf"):
            data["cpf"] = normalize_cpf_or_raise(data.get("cpf"))
        if data.get("phone"):
            data["phone"] = normalize_phone_or_raise(data.get("phone"))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    ensure_unique_identity(db, cpf=data.get("cpf") or None, phone=data.get("phone") or None, current_user_id=user.id)
    for key, value in data.items():
        setattr(profile, key, value)
    current_status = _canonical_application_status(profile.status)

    if current_status in {"resubmission_requested", "rejected"} and (
         profile.document_url or profile.selfie_url or profile.proof_of_address_url
    ):
        profile.status = "under_review"
        profile.rejection_reason = None
        profile.resubmission_requested_documents = ""    
    if _canonical_application_status(profile.status) in {"submitted", "under_review"}:
        _ensure_application_complete(
            profile_photo_url=profile.profile_photo_url,
            document_url=profile.document_url,
            identity_document_back_url=profile.identity_document_back_url,
            proof_of_address_url=profile.proof_of_address_url,
            bio=profile.bio,
        )
    profile.updated_at = datetime.utcnow()
    db.commit()
    db.refresh(profile)
    return profile


# --------------------------------------------------------------------------- #
# Background Check Fase 0 — consentimento + envio de certidoes + status.        #
# Tudo dormente em producao ate a flag de tenant `background_checks` ligar.     #
# --------------------------------------------------------------------------- #
BACKGROUND_CERT_TYPES = {"pf", "tj", "trf", "tse"}
DEFAULT_BACKGROUND_CONSENT_VERSION = "v1"


class BackgroundConsentPayload(BaseModel):
    consent_version: str | None = None


class BackgroundCertificatePayload(BaseModel):
    cert_type: str
    uf: str | None = None
    cert_number: str | None = None  # FIX 4: opcional — front-end pode não enviar
    document_url: str | None = None

    @field_validator("document_url", mode="before")
    @classmethod
    def _validate_document_url(cls, v):
        """G9: rejeita URLs externas em document_url — aceita apenas caminhos internos."""
        if v is None:
            return v
        normalized = str(v).strip()
        if not normalized:
            return normalized
        # Aceita: caminhos relativos internos (/uploads/...) e URLs do próprio backend.
        # Caminho relativo interno.
        if normalized.startswith("/uploads/"):
            return normalized
        # URL completa: aceita se o path contem /uploads/ (upload retornado pelo backend).
        if normalized.startswith(("http://", "https://")):
            from urllib.parse import urlsplit as _urlsplit
            parsed = _urlsplit(normalized)
            if "/uploads/" in parsed.path:
                return normalized
        raise ValueError("document_url deve apontar para um upload interno (/uploads/).")


def _walker_profile_for_user(user: User, db: Session) -> WalkerProfile:
    profile = db.query(WalkerProfile).filter(WalkerProfile.user_id == user.id).first()
    if not profile:
        raise HTTPException(status_code=404, detail="Cadastro de passeador nao encontrado.")
    return profile


def _serialize_background_certificate(cert) -> dict:
    return {
        "id": cert.id,
        "cert_type": cert.cert_type,
        "issuer_uf": cert.issuer_uf,
        "cert_number": cert.cert_number,
        "document_url": cert.document_url,
        "status": cert.status,
        "validated_at": cert.validated_at,
        "expires_at": cert.expires_at,
        "official_validation_url": background_check_official_url(cert.cert_type, cert.issuer_uf, cert.cert_number),
        "created_at": cert.created_at,
        "updated_at": cert.updated_at,
    }


@router.post("/background/consent")
def submit_background_consent(
    payload: BackgroundConsentPayload | None = None,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    profile = _walker_profile_for_user(user, db)
    version = (payload.consent_version if payload else None) or DEFAULT_BACKGROUND_CONSENT_VERSION
    provider = get_background_provider(db, getattr(user, "tenant_id", None))
    return provider.register_consent(profile, version, db)


@router.post("/background/certificate", status_code=201)
def submit_background_certificate(
    payload: BackgroundCertificatePayload,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    profile = _walker_profile_for_user(user, db)
    provider = get_background_provider(db, getattr(user, "tenant_id", None))
    return provider.submit_certificate(profile, payload, db)


@router.get("/background")
def get_background_status(
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    profile = _walker_profile_for_user(user, db)
    certificates = (
        db.query(WalkerBackgroundCertificate)
        .filter(WalkerBackgroundCertificate.walker_profile_id == profile.id)
        .all()
    )
    provider = get_background_provider(db, getattr(user, "tenant_id", None))
    result = provider.get_background_status(profile, certificates)
    db.commit()
    return result


@router.get("/dashboard")
def dashboard(user: User = Depends(get_current_user), db: Session = Depends(get_walker_self_db)):
    _require_active_walker(user, db)
    # F16: carrega only active/accepted/available sem limite; completed com limite 200.
    active = db.query(Walk).filter(Walk.walker_id == user.id, Walk.status.in_(["Indo buscar o pet", "Passeando agora"])).all()
    accepted = db.query(Walk).filter(Walk.walker_id == user.id).all()
    # FIX 2: filtrar pelos tenants permitidos ao walker para não vazar walks de outros tenants.
    _allowed_tenant_ids = [
        row[0] for row in db.query(TenantWalkerAccess.tenant_id).filter(
            TenantWalkerAccess.walker_user_id == user.id,
            TenantWalkerAccess.status == "active",
        ).distinct().all()
    ]
    available = (
        db.query(Walk)
        .filter(
            Walk.walker_id.is_(None),
            Walk.status == "Agendado",
            Walk.tenant_id.in_(_allowed_tenant_ids),
        )
        .all()
    )
    completed = _completed_walks(user, db)  # limitado a 200

    # F01: valores reais do banco; sem fallback fake
    now = datetime.utcnow()
    day_start = datetime(now.year, now.month, now.day)
    today_walks = _period_walks(completed, day_start, day_start + timedelta(days=1))
    today_total = _sum_walk_values(today_walks)
    # BUG 2 fix: contagem de passeios concluídos hoje usando conjunto canônico de status
    completed_walks_today = sum(1 for w in today_walks if w.status in _WALK_COMPLETED_STATUSES)

    # Gorjetas reais da semana
    tips_week = _walker_tips_week(user.id, db)

    potential = sum(float(walk.price or 0) for walk in available[:3])

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

    # F01 + F05: reputação real via WalkReview
    rep = _walk_review_reputation_summary(user.id, db)
    rating_avg = rep["rating_avg"]       # 0 se sem avaliações
    rating_count = rep["rating_count"]   # 0 se sem avaliações

    # Score operacional real (já existente no sistema)
    op_score = calculate_walker_operational_score(user.id, db)
    score = op_score.get("score") if op_score else None  # None se sem dados

    # Nível oficial via compute_walker_trust (fonte de verdade com trust — B3)
    from app.services.walker_trust_service import compute_walker_trust as _compute_trust
    level_str = _compute_trust(db, user.id)["level"]
    # Mapeamento para chave interna usada pelo frontend
    level_map = {"Bronze": "BRONZE", "Prata": "SILVER", "Ouro": "GOLD", "Diamante": "DIAMOND"}
    level_key = level_map.get(level_str, "BRONZE")

    # F05: grade semana com datas REAIS (próximos 7 dias a partir de hoje)
    week_days_pt = ["Seg", "Ter", "Qua", "Qui", "Sex", "Sab", "Dom"]
    week_grid = []
    for i in range(7):
        d = (day_start + timedelta(days=i))
        week_grid.append({
            "day": week_days_pt[d.weekday()],
            "date": str(d.day),
            "month": d.strftime("%m/%Y"),
            "status": "available",
        })

    return {
        "available_requests": len(available),
        "active_walks": len(active),
        "accepted_walks": len(accepted),
        "completed_walks_today": completed_walks_today,
        "today_earnings": today_total,
        "walk_earnings_today": today_total,
        "tips_today": 0.0,
        "tips_week": tips_week,
        "potential_earnings": potential,
        "level": level_key,
        "level_label": level_str,
        "next_level": None,  # sem hardcode; regra de progressão calculada em _walker_level
        "score": score,
        "rating_avg": rating_avg,
        "rating_count": rating_count,
        "level_progress": None,  # sem tabela de metas por nível; não inventar
        "bonus_missing_walks": max(0, 14 - len(completed)),
        "boost_credits": 0,  # CR não tem tabela de saldo ainda; honesto = 0
        "next_request": next_request_payload,
        "active_walk": active_walk,
        "tips_summary": {
            "today": 0.0,
            "week": tips_week,
            "month": _walker_tips_total(user.id, db),  # total pago histórico como proxy
            "pending_review": 0,
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
        "walker_kit": _build_walker_kit(user.id, db),
        "cr_wallet": {
            "balance": 0,  # CR não possui tabela de saldo; sem fake
            "earned_this_week": 0,
            "source_policy": "CR e concedido pela plataforma por performance; nao e comprado pelo passeador.",
            "actions": [
                {"key": "matching_boost", "label": "Boost matching", "cost": 4, "description": "Melhora prioridade no ranking por janela curta."},
                {"key": "early_wave", "label": "Entrada antecipada", "cost": 3, "description": "Libera solicitacoes alguns minutos antes da fila comum."},
                {"key": "visual_highlight", "label": "Destaque visual", "cost": 2, "description": "Selo temporario no card do passeador."},
            ],
        },
        "matching_intelligence": {
            # F05: score real do sistema de matching
            "score": op_score.get("score") if op_score else None,
            "summary": "Ranking combina experiencia, distancia, disponibilidade, score, avaliacao e historico.",
            "signals": [
                {"label": "Experiencia", "value": op_score.get("experience_score") if op_score else None},
                {"label": "Distancia", "value": None},  # sem dados de distância no banco
                {"label": "Agenda", "value": None},      # sem tabela de disponibilidade
                {"label": "Avaliacao", "value": op_score.get("rating_score") if op_score else None},
            ],
            "next_improvement": "Manter horarios 17h-20h ativos melhora a posicao em alta demanda.",
        },
        "rating_summary": {
            # F05: dados reais de WalkReview
            "rating_avg": rating_avg,
            "rating_count": rating_count,
            "score": score,
            "components": [
                {"label": "Avaliacoes", "value": op_score.get("rating_score") if op_score else None},
                {"label": "Pontualidade", "value": None},  # sem tabela de pontualidade
                {"label": "Conclusao", "value": op_score.get("experience_score") if op_score else None},
                {"label": "Ocorrencias", "value": None},   # sem dados específicos
            ],
        },
        "schedule_rules": {
            "min_interval_minutes": buffer_minutes,
            "message": "Novos aceites exigem pelo menos 15 min entre o fim de um passeio e o inicio do outro.",
            "can_accept_next_request": not (_has_schedule_conflict(next_request, accepted, buffer_minutes) if next_request else False),
        },
        "goals_evolution": _goals_evolution_payload(user, db),
        # F05: grade semanal com datas reais
        "week": week_grid,
    }


@router.get("/earnings")
def earnings(user: User = Depends(get_current_user), db: Session = Depends(get_walker_self_db)):
    _require_active_walker(user, db)
    completed = _completed_walks(user, db)
    total = sum(float(walk.price or 0) for walk in completed)

    # Gorjetas reais
    tips = _walker_tips_total(user.id, db)

    # ── Fase 1 Passo 4 §A: saldo por tenant ──────────────────────────────────
    # Mapeia tenant_id (pode ser None para legado) → bucket {available/pending/...}
    by_tenant_raw = _balance_by_tenant(user, db)

    # Resolve tenant_names para a resposta (evita N queries na lista final)
    _tenant_name_cache: dict[str | None, str | None] = {}

    def _get_tenant_name(tid: str | None) -> str | None:
        if tid not in _tenant_name_cache:
            _tenant_name_cache[tid] = _resolve_tenant_name(db, tid)
        return _tenant_name_cache[tid]

    # ── Fase 1 Passo 4 §B: by_tenant list (sem entrada None/untenanted) ──────
    # A entrada None é legado sem tenant_id; não a expõe na lista by_tenant para
    # não confundir o cliente multi-tenant. O consolidated inclui tudo.
    by_tenant_list = []
    for tid, b in by_tenant_raw.items():
        if tid is None:
            # Entrada untenanted (saques/pagamentos sem tenant_id).
            # NÃO incluída na lista by_tenant; sua eventual contribuição ao
            # consolidated mantém a soma correta do available legado.
            continue
        by_tenant_list.append({
            "tenant_id": tid,
            "tenant_name": _get_tenant_name(tid),
            "available": b["available"],
            "pending": b["pending"],
            "processing": b["processing"],
            "total": b["total"],
        })

    # consolidated: soma de TODOS os buckets incluindo o untenanted (None), para
    # representar a visão financeira completa baseada em Payment.walker_amount.
    # Nota: pode diferir de available_balance (campo legado) quando há passeios
    # sem split calculado (walker_amount NULL) — nesses casos available_balance
    # usa o fallback de walk.price e consolidated ignora tais pagamentos.
    # Os dois campos coexistem; o app instalado usa available_balance (inalterado).
    _cons_available = round(sum(b["available"] for b in by_tenant_raw.values()), 2)
    _cons_pending = round(sum(b["pending"] for b in by_tenant_raw.values()), 2)
    _cons_processing = round(sum(b["processing"] for b in by_tenant_raw.values()), 2)
    consolidated = {
        "available": _cons_available,
        "pending": _cons_pending,
        "processing": _cons_processing,
        "total": round(_cons_available + _cons_pending + _cons_processing, 2),
    }

    # F10: lista de transações real; sem demo entries quando vazia.
    # Fase 1 Passo 4 §B: adiciona tenant_id e tenant_name a cada item sem
    # remover nenhum campo existente (superset — zero-regressão).
    transactions = []
    for walk in completed:
        wpl = _walk_payload(walk, db)
        # Tenta encontrar o Payment mais recente pago para obter tenant_id
        _pay = (
            db.query(Payment)
            .filter(Payment.walk_id == walk.id, Payment.status.in_(_PAID_PAYMENT_STATUSES_CONST))
            .order_by(Payment.created_at.desc())
            .first()
        )
        _walk_tenant_id = _pay.tenant_id if _pay else getattr(walk, "tenant_id", None)
        # FIX 8: usar walker_amount (líquido após split) quando disponível;
        # fallback para walk.price apenas quando o Payment não tem split calculado.
        _tx_amount = float(_pay.walker_amount) if (_pay and _pay.walker_amount is not None) else float(walk.price or 0)
        transactions.append({
            "id": f"walk-{walk.id}",
            "type": "walk",
            "description": "Passeio concluido",
            "pet_name": wpl["pet_name"],
            "duration": wpl["duration"],
            "date": wpl["date"],
            "time": wpl["time"],
            "amount": _tx_amount,
            "status": "paid",
            # Campos novos (Fase 1 Passo 4 §B) — não existiam antes:
            "tenant_id": _walk_tenant_id,
            "tenant_name": _get_tenant_name(_walk_tenant_id),
        })
    # Gorjetas pagas como transações reais
    tips_rows = (
        db.query(WalkTip)
        .filter(WalkTip.walker_id == user.id, WalkTip.status == "paid")
        .order_by(WalkTip.created_at.desc())
        .all()
    )
    for tip in tips_rows:
        transactions.append({
            "id": f"tip-{tip.id}",
            "type": "tip",
            "description": "Gorjeta recebida",
            "pet_name": "",
            "duration": "",
            "date": tip.created_at.strftime("%d/%m/%Y") if tip.created_at else "",
            "time": tip.created_at.strftime("%H:%M") if tip.created_at else "",
            "amount": float(tip.amount or 0),
            "status": "paid",
            # Tips: sem tenant_id explícito; campo presente mas null.
            "tenant_id": None,
            "tenant_name": None,
        })
    # F10: NÃO injeta demo-walk-1/demo-tip-1/demo-withdraw-1 — lista vazia é honesta
    # Ordena por data desc (walks já vêm desc pelo _completed_walks)
    transactions.sort(key=lambda t: t.get("date", ""), reverse=True)

    # Reputação real
    rep = _walk_review_reputation_summary(user.id, db)
    op_score = calculate_walker_operational_score(user.id, db)
    # Nível oficial via compute_walker_trust (fonte de verdade com trust — B3)
    from app.services.walker_trust_service import compute_walker_trust as _compute_trust
    level_str = _compute_trust(db, user.id)["level"]

    # FIX 6c: ler pix_key do perfil do walker para expor no response de earnings
    _walker_profile_earnings = db.query(WalkerProfile).filter(WalkerProfile.user_id == user.id).first()
    _pix_key = _walker_profile_earnings.pix_key if _walker_profile_earnings else None

    return {
        # ── Chaves EXISTENTES (contrato inalterado — zero-regressão) ────────
        "available_balance": _available_balance(user, db),
        "weekly_total": total,
        "completed_walks": len(completed),
        "tips": tips,
        "walk_earnings": total,
        "total_with_tips": total + tips,
        "tips_pending_review": 0,
        "tips_policy": "Gorjetas sao opcionais, surgem apos entrega do pet e nao entram nas metas de ganhos.",
        "goal_total_walks": 14,
        "future_reward_preview": "Beneficios futuros podem ser ativados por campanhas, selos e prioridade em solicitacoes.",
        "level": level_str,
        "score": op_score.get("score") if op_score else None,
        "transactions": transactions,
        # ── Chaves NOVAS (Fase 1 Passo 4 §B) — não quebram cliente antigo ───
        "by_tenant": by_tenant_list,
        "consolidated": consolidated,
        # FIX 6c: chave Pix do walker (null se não cadastrada)
        "pix_key": _pix_key,
    }


class PixKeyPayload(BaseModel):
    pix_key: str


@router.patch("/pix-key")
def update_pix_key(
    payload: PixKeyPayload,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """FIX 6d: Self-service para o walker cadastrar/atualizar a própria chave Pix.

    Valida que a chave não está vazia e persiste no WalkerProfile. A chave é
    retornada no GET /walker/earnings para o app exibir e para o processamento
    de saques.
    """
    _require_active_walker(user, db)
    key = (payload.pix_key or "").strip()
    if not key:
        raise HTTPException(status_code=422, detail="pix_key nao pode ser vazio.")
    profile = _walker_profile_for_user(user, db)
    profile.pix_key = key
    profile.updated_at = datetime.utcnow()
    db.commit()
    db.refresh(profile)
    return {"ok": True, "pix_key": profile.pix_key}


@router.get("/goals-evolution")
def goals_evolution(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    _require_active_walker(user, db)
    return _goals_evolution_payload(user, db)


@router.get("/me/level")
def my_walker_level(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """WK-06: nível REAL do passeador (Bronze/Prata/Ouro/Diamante via _walker_level).

    Retorna o MESMO objeto level que o dashboard/goals-evolution (current/next/score/
    progress_percent/levels/criteria), para a tela de Níveis parar de usar array
    hardcoded. Requer passeador ativo (sem fallback demo silencioso no cliente).
    """
    _require_active_walker(user, db)
    return _goals_evolution_payload(user, db)["level"]


# api-T2: schema permissivo do envio do kit. `items` e uma lista de dicts (cada item
# mantem o formato livre original {key, available, photo_urls}); Pydantic v2 ignora extras
# no nivel raiz. Validacao de tipo no topo (items precisa ser lista) sem reescrever o loop.
class UpdateKitRequest(BaseModel):
    items: list[dict] = Field(default_factory=list)


@router.put("/kit")
def update_kit(payload: UpdateKitRequest, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    _require_active_walker(user, db)
    items_payload = payload.items or []
    # WK-05: só URLs HOSPEDADAS (http/https) podem ser persistidas. URIs locais do
    # dispositivo (file://, content://, blob:) não abrem no admin/tutor — rejeita.
    for item in items_payload:
        for url in (item.get("photo_urls") or []):
            if isinstance(url, str) and url.strip().lower().startswith(("file:", "content:", "blob:")):
                raise HTTPException(
                    status_code=422,
                    detail="Foto do kit deve ser uma URL hospedada (http/https), nao um arquivo local do dispositivo.",
                )
    items = {}
    for definition in KIT_ITEM_DEFINITIONS:
        incoming = next((item for item in items_payload if item.get("key") == definition["key"]), {})
        items[definition["key"]] = {
            "available": bool(incoming.get("available")),
            "photo_urls": incoming.get("photo_urls") or [],
        }

    now = datetime.utcnow()
    row = db.query(WalkerKitSubmission).filter(WalkerKitSubmission.walker_user_id == user.id).first()
    if not row:
        row = WalkerKitSubmission(walker_user_id=user.id)
        db.add(row)

    row.items_json = json.dumps(items)
    row.audit_status = "pending_review"
    row.audit_note = "Kit enviado para validacao. As fotos aprovadas ficarao visiveis para o tutor."
    row.reviewed_by_admin_id = None
    row.reviewed_at = None
    row.updated_at = now
    db.commit()
    db.refresh(row)
    return {"ok": True, "walker_kit": _build_walker_kit(user.id, db)}


@router.post("/kit/photo")
async def upload_kit_photo(
    request: Request,
    file: UploadFile = File(...),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """WK-07: recebe uma foto do kit (multipart), hospeda e devolve a URL http.

    O app envia a foto AQUI antes de submeter o kit; o submit (update_kit) passa a
    receber URLs hospedadas (http), não file:// local (que o admin não abria — WK-05).
    Espelha o pipeline de completion-photo (valida imagem + object_storage + registry).
    Dono derivado do token.
    """
    # U3/G2: rate limit; G7: fotos de kit limitadas a 5 MB.
    enforce_upload_rate_limit(request)
    _require_active_walker(user, db)
    if not file.content_type or not file.content_type.startswith("image/"):
        raise HTTPException(status_code=400, detail="Envie uma imagem valida.")

    validated_bytes = await read_image_upload_safely(file, max_bytes=5 * 1024 * 1024)

    safe_walker_id = "".join(char for char in user.id if char.isalnum() or char in {"-", "_"})[:80] or "walker"
    destination_dir = WALKER_KIT_UPLOAD_ROOT / safe_walker_id
    destination_dir.mkdir(parents=True, exist_ok=True)
    extension = _safe_upload_extension(file.filename, file.content_type)
    destination = destination_dir / f"kit-{uuid4().hex}{extension}"

    object_storage.save(destination, validated_bytes, file.content_type)
    await file.close()

    record_upload(
        db, context="walker_kit", owner_id=user.id,
        document_type="kit", storage_path=str(destination),
        mime_type=file.content_type, size_bytes=len(validated_bytes),
    )
    db.commit()

    photo_url = _public_upload_url(request, destination)
    return {
        "ok": True,
        "photo_url": photo_url,
        "url": photo_url,
        "uploaded_at": datetime.utcnow().isoformat(),
    }


def _batch_walk_review_summaries(walker_ids: list[str], db: Session) -> dict[str, dict]:
    """Uma query para todos os WalkReview dos walkers; agrega em Python por walker_id.

    Reproduz exatamente a matematica de _walk_review_reputation_summary:
    - rating_avg: media dos ratings (arredondada 2 casas)
    - rating_count: total de reviews
    - recent_review_comments: ate 5 reviews com comment, por walker, ordem desc
    - top_review_tags: ate 5 tags mais frequentes, por walker
    """
    if not walker_ids:
        return {}
    all_reviews = (
        db.query(WalkReview)
        .filter(WalkReview.walker_id.in_(walker_ids))
        .order_by(WalkReview.created_at.desc())
        .all()
    )
    # Agrupa por walker_id mantendo a ordem desc (query ja ordenou)
    by_walker: dict[str, list[WalkReview]] = {}
    for review in all_reviews:
        by_walker.setdefault(review.walker_id, []).append(review)

    result: dict[str, dict] = {}
    empty = {
        "rating_avg": 0,
        "rating_count": 0,
        "recent_review_comments": [],
        "top_review_tags": [],
    }
    for walker_id in walker_ids:
        reviews = by_walker.get(walker_id, [])
        if not reviews:
            result[walker_id] = dict(empty)
            continue
        rating_count = len(reviews)
        rating_avg = round(sum(r.rating for r in reviews) / rating_count, 2)
        tag_counts: dict[str, int] = {}
        for review in reviews:
            for tag in _walk_review_tags(review):
                tag_counts[tag] = tag_counts.get(tag, 0) + 1
        top_review_tags = [
            {"tag": tag, "count": count}
            for tag, count in sorted(tag_counts.items(), key=lambda item: (-item[1], item[0]))[:5]
        ]
        recent_review_comments = [
            {
                "id": r.id,
                "walk_id": r.walk_id,
                "rating": r.rating,
                "comment": r.comment,
                "created_at": r.created_at,
            }
            for r in reviews
            if r.comment
        ][:5]
        result[walker_id] = {
            "rating_avg": rating_avg,
            "rating_count": rating_count,
            "recent_review_comments": recent_review_comments,
            "top_review_tags": top_review_tags,
        }
    return result


def _batch_reputation_summaries(walker_ids: list[str], db: Session) -> dict[str, dict]:
    """Uma query por tabela (WalkerReview, Walk) para todos os walkers; agrega em Python.

    Reproduz exatamente a matematica de reputation_service.reputation_summary:
    - rating_average, reviews_count, total_walks, level, reputation_score
    """
    if not walker_ids:
        return {}
    all_walker_reviews = (
        db.query(WalkerReview)
        .filter(WalkerReview.walker_id.in_(walker_ids))
        .all()
    )
    walker_reviews_by_id: dict[str, list[WalkerReview]] = {}
    for wr in all_walker_reviews:
        walker_reviews_by_id.setdefault(wr.walker_id, []).append(wr)

    completed_counts: dict[str, int] = {}
    if walker_ids:
        walks_completed = (
            db.query(Walk.walker_id, Walk.id)
            .filter(Walk.walker_id.in_(walker_ids), Walk.status.in_(_WALK_COMPLETED_STATUSES))
            .all()
        )
        for row in walks_completed:
            completed_counts[row.walker_id] = completed_counts.get(row.walker_id, 0) + 1

    result: dict[str, dict] = {}
    for walker_id in walker_ids:
        reviews = walker_reviews_by_id.get(walker_id, [])
        reviews_count = len(reviews)
        rating_average = round(sum(r.rating for r in reviews) / reviews_count, 2) if reviews_count else 0.0
        total_walks = completed_counts.get(walker_id, 0)
        reputation_score = (
            round((rating_average / 5) * 70 + min(total_walks, 80) / 80 * 15, 2)
            if reviews_count else None
        )
        result[walker_id] = {
            "rating_average": rating_average,
            "reviews_count": reviews_count,
            "total_walks": total_walks,
            "level": walker_level(total_walks, rating_average, reviews_count),
            "reputation_score": reputation_score,
            "acceptance_rate": None,
            "cancellation_rate": None,
        }
    return result


def _build_walker_kit_from_row(user_id: str | None, kit_row) -> dict:
    """Variante de _build_walker_kit que aceita a row pre-carregada (evita query por walker)."""
    submission = _kit_submission_payload(kit_row)
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


def _public_walker_rows(db: Session, verified_walkers_enabled: bool = True) -> list[dict]:
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
            "level": "Ouro",
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
                    "rating_avg": 4.9,
                    "rating_count": 126,
                    "top_review_tags": [{"tag": "caring", "count": 48}, {"tag": "punctual", "count": 42}, {"tag": "excellent_walk", "count": 31}],
                    "recent_review_comments": [
                        {"id": "demo-review-1", "walk_id": "demo-walk-1", "rating": 5, "comment": "Muito cuidadoso e pontual.", "created_at": datetime.utcnow()},
                    ],
                    "recent_reviews": [
                        {"id": "demo-review-1", "walk_id": "demo-walk-1", "rating": 5, "comment": "Muito cuidadoso e pontual.", "created_at": datetime.utcnow()},
                    ],
                    "city": "Salvador",
                    "neighborhood": "Pituba",
                    "bio": "Passeador verificado com kit publicado para consulta do tutor.",
                    "walk_price": 35,
                    "verified": verified_walkers_enabled,
                    "walker_kit": _build_walker_kit("walker-demo-user-1", db),
            }
        ]

    # --- Batch pre-load: O(1) queries independente do numero de walkers ---
    walker_user_ids = [p.user_id for p in profiles if p.user_id]

    # 1. Users em uma query
    users_by_id: dict[str, User] = {}
    if walker_user_ids:
        users_by_id = {u.id: u for u in db.query(User).filter(User.id.in_(walker_user_ids)).all()}

    # 2. WalkReview aggregates (para _walk_review_reputation_summary)
    walk_review_summaries = _batch_walk_review_summaries(walker_user_ids, db)

    # 3. WalkerReview + Walk completed (para reputation_summary)
    rep_summaries = _batch_reputation_summaries(walker_user_ids, db)

    # 4. WalkerKitSubmission em uma query
    kit_rows_by_user: dict[str, object] = {}
    if walker_user_ids:
        kit_rows_by_user = {
            r.walker_user_id: r
            for r in db.query(WalkerKitSubmission).filter(WalkerKitSubmission.walker_user_id.in_(walker_user_ids)).all()
        }

    rows = []
    seen_keys = set()
    for profile in profiles:
        user = users_by_id.get(profile.user_id) if profile.user_id else None
        if not _is_public_real_walker(profile, user):
            continue
        dedupe_key = (profile.cpf or profile.user_id or profile.id or profile.phone or (user.email if user else "")).strip().lower()
        if not dedupe_key or dedupe_key in seen_keys:
            continue
        seen_keys.add(dedupe_key)
        summary = rep_summaries.get(profile.user_id, {
            "rating_average": 0.0, "reviews_count": 0, "total_walks": 0,
            "level": "Bronze", "reputation_score": None,
            "acceptance_rate": None, "cancellation_rate": None,
        })
        walk_review_summary = walk_review_summaries.get(profile.user_id, {
            "rating_avg": 0, "rating_count": 0, "recent_review_comments": [], "top_review_tags": [],
        })
        kit_row = kit_rows_by_user.get(profile.user_id)
        rows.append({
                **summary,
                **walk_review_summary,
                "id": profile.user_id,
                "partner_id": profile.id,
                "name": profile.full_name or "Passeador",
                "full_name": profile.full_name or "Passeador",
                "role": user.role if user else "",
                "photo_url": _public_walker_avatar_url(profile),
                "profile_photo_url": _public_walker_avatar_url(profile),
                "status": profile.status,
                "raw_status": profile.status,
                "active_as_walker": bool(profile.active_as_walker),
                "rating": walk_review_summary["rating_avg"] or 0,
                "average_rating": walk_review_summary["rating_avg"] or 0,
                "rating_average": walk_review_summary["rating_avg"] or 0,
                "reviews_count": walk_review_summary["rating_count"],
                "recent_reviews": walk_review_summary["recent_review_comments"],
                "city": profile.city,
                "neighborhood": profile.state,
                "bio": profile.bio or "Passeador disponivel com kit publicado para consulta.",
                "walk_price": 35,
                "verified": verified_walkers_enabled,
                "walker_kit": _build_walker_kit_from_row(profile.user_id, kit_row),
        })
    return rows


def _get_verified_walkers_enabled(db: Session, request) -> bool:
    """Resolve se verified_walkers esta ativo para o tenant da request."""
    from app.services.tenant_context import resolve_current_tenant
    from app.services.tenant_plan_service import tenant_feature_enabled
    try:
        tenant = resolve_current_tenant(db, request)
        return tenant_feature_enabled(tenant, db, "verified_walkers")
    except Exception:
        return False  # default-OFF para verified_walkers


@router.get("/public")
def public_walkers(request: Request = None, db: Session = Depends(get_db)):
    verified_enabled = _get_verified_walkers_enabled(db, request) if request is not None else False
    return {"walkers": _public_walker_rows(db, verified_walkers_enabled=verified_enabled)}


@api_public_router.get("/walkers")
def api_public_walkers(request: Request = None, db: Session = Depends(get_db)):
    verified_enabled = _get_verified_walkers_enabled(db, request) if request is not None else False
    return _public_walker_rows(db, verified_walkers_enabled=verified_enabled)


@router.get("/availability")
def availability(user: User = Depends(get_current_user), db: Session = Depends(get_walker_self_db)):
    profile = _require_active_walker(user, db)
    # F04: próximos 7 dias REAIS a partir de hoje (default "available")
    now = datetime.utcnow()
    today = datetime(now.year, now.month, now.day)
    week_days_pt = ["Seg", "Ter", "Qua", "Qui", "Sex", "Sab", "Dom"]
    week = []
    for i in range(7):
        d = today + timedelta(days=i)
        label = f"{week_days_pt[d.weekday()]} {d.day}"
        week.append({"day": label, "status": "available", "possible_walks": 3})

    # Mês atual real
    month_label = now.strftime("%B %Y").capitalize()
    # Mapeamento mês PT
    _months_pt = {
        "January": "Janeiro", "February": "Fevereiro", "March": "Março",
        "April": "Abril", "May": "Maio", "June": "Junho",
        "July": "Julho", "August": "Agosto", "September": "Setembro",
        "October": "Outubro", "November": "Novembro", "December": "Dezembro",
    }
    for en, pt in _months_pt.items():
        month_label = month_label.replace(en, pt)

    # WK-01: disponibilidade editável REAL persistida (aditivo aos campos legados
    # week/slots/month, que apps distribuídos antigos ainda consomem). Vazio honesto
    # quando o passeador ainda não definiu — nunca slots fictícios.
    row = (
        db.query(WalkerAvailability)
        .filter(WalkerAvailability.walker_user_id == user.id)
        .first()
    )
    schedule = json.loads(row.schedule_json) if row and row.schedule_json else {}

    return {
        "week": week,
        "slots": ["07:00", "08:00", "09:00", "14:00", "15:00", "17:00", "18:00", "19:00", "20:00"],
        "month": {
            "label": month_label,
            # Estimativas honestas (sem tabela de disponibilidade ainda)
            "estimated_earnings": None,
            "possible_walks": None,
            "available_days": None,
        },
        "schedule": schedule,
        # WK-02: presença real (aditivo).
        "is_online": bool(getattr(profile, "is_online", False)),
        "last_seen_at": profile.last_seen_at.isoformat() if getattr(profile, "last_seen_at", None) else None,
    }


@router.post("/online")
def set_online(payload: WalkerOnlineUpdate, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    # WK-02: persiste a presença real do passeador (flag + last_seen). Deriva do token.
    profile = _require_active_walker(user, db)
    profile.is_online = bool(payload.online)
    profile.last_seen_at = datetime.utcnow()
    db.commit()
    return {
        "ok": True,
        "user_id": user.id,
        "is_online": profile.is_online,
        "last_seen_at": profile.last_seen_at.isoformat() if profile.last_seen_at else None,
    }


@router.put("/availability")
def update_availability(payload: WalkerAvailabilityUpdate, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    _require_active_walker(user, db)
    # WK-01: upsert por passeador (uma linha por walker; deriva do token -> ownership).
    schedule_dict = {day: sch.model_dump() for day, sch in payload.schedule.items()}
    schedule_json = json.dumps(schedule_dict)
    row = (
        db.query(WalkerAvailability)
        .filter(WalkerAvailability.walker_user_id == user.id)
        .first()
    )
    if row:
        row.schedule_json = schedule_json
    else:
        db.add(WalkerAvailability(walker_user_id=user.id, schedule_json=schedule_json))
    db.commit()
    return {"ok": True, "user_id": user.id, "schedule": schedule_dict}


# WK-03: CRUD de exceções pontuais de disponibilidade (block/open por data).
class AvailabilityExceptionCreate(BaseModel):
    exception_date: date
    kind: str
    start_time: str | None = None
    end_time: str | None = None
    tenant_id: str | None = None


def _exception_dict(exc: WalkerAvailabilityException) -> dict:
    return {
        "id": exc.id,
        "exception_date": exc.exception_date.isoformat(),
        "kind": exc.kind,
        "start_time": exc.start_time,
        "end_time": exc.end_time,
        "tenant_id": exc.tenant_id,
    }


@router.get("/availability/exceptions")
def list_availability_exceptions(
    user: User = Depends(get_current_user),
    db: Session = Depends(get_walker_self_db),
    from_: date | None = Query(default=None, alias="from"),
    to: date | None = Query(default=None),
):
    _require_active_walker(user, db)
    q = db.query(WalkerAvailabilityException).filter(
        WalkerAvailabilityException.walker_user_id == user.id
    )
    if from_ is not None:
        q = q.filter(WalkerAvailabilityException.exception_date >= from_)
    if to is not None:
        q = q.filter(WalkerAvailabilityException.exception_date <= to)
    return [_exception_dict(exc) for exc in q.all()]


@router.post("/availability/exceptions")
def create_availability_exception(
    payload: AvailabilityExceptionCreate,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    from app.services.walker_network_matching_service import is_walker_eligible_for_tenant
    _require_active_walker(user, db)
    if payload.kind not in {"block", "open"}:
        raise HTTPException(status_code=400, detail="kind deve ser 'block' ou 'open'.")
    if payload.tenant_id is not None and not is_walker_eligible_for_tenant(db, payload.tenant_id, user.id):
        raise HTTPException(status_code=403, detail="Passeador não vinculado a este tenant.")
    exc = WalkerAvailabilityException(
        id=str(uuid4()),
        walker_user_id=user.id,
        exception_date=payload.exception_date,
        kind=payload.kind,
        start_time=payload.start_time,
        end_time=payload.end_time,
        tenant_id=payload.tenant_id,
    )
    db.add(exc)
    db.commit()
    db.refresh(exc)
    return _exception_dict(exc)


@router.delete("/availability/exceptions/{exception_id}")
def delete_availability_exception(
    exception_id: str,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    _require_active_walker(user, db)
    exc = db.get(WalkerAvailabilityException, exception_id)
    if not exc or exc.walker_user_id != user.id:
        raise HTTPException(status_code=404, detail="Exceção não encontrada.")
    db.delete(exc)
    db.commit()
    return {"ok": True}


# api-T2: schema permissivo da reconfirmacao do tutor (campo unico `decision`).
class WalkReconfirmationRequest(BaseModel):
    decision: str | None = None


@router.post("/walks/{walk_id}/reconfirmation")
def tutor_walk_reconfirmation(
    walk_id: str,
    payload: WalkReconfirmationRequest,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    walk = db.get(Walk, walk_id)

    if not walk:
        raise HTTPException(status_code=404, detail="Passeio nao encontrado")

    if str(walk.tutor_id) != str(user.id):
        raise HTTPException(status_code=403, detail="Passeio nao pertence ao tutor")

    decision = str(payload.decision or "").strip()

    if walk.operational_status not in {"awaiting_tutor_reconfirmation", "no_walker_found"}:
        raise HTTPException(status_code=409, detail="Passeio nao aguarda confirmacao do tutor")

    if decision == "continue_search":
        walk.operational_status = "priority_matching"
        walk.status = "Agendado"
        walk.no_walker_reason = None
        walk.matching_finished_at = None
        walk.confirmation_expires_at = None

        log_event(
            db,
            walk.id,
            "tutor_reconfirmed_search",
            actor_type="cliente",
            actor_id=user.id,
            metadata={"decision": decision},
        )

        start_matching(walk, db)

    elif decision == "reschedule":
        walk.operational_status = "reschedule_requested"
        walk.status = "Reagendamento solicitado"
        walk.confirmation_expires_at = None

        log_event(
            db,
            walk.id,
            "tutor_requested_reschedule",
            actor_type="cliente",
            actor_id=user.id,
            metadata={"decision": decision},
        )

    elif decision == "cancel":
        walk.operational_status = "canceled_by_tutor"
        walk.status = "Cancelado"
        walk.confirmation_expires_at = None
        walk.no_walker_reason = "Cancelado sem custo pelo tutor apos falha na busca de passeador."

        log_event(
            db,
            walk.id,
            "tutor_cancelled_after_no_walker",
            actor_type="cliente",
            actor_id=user.id,
            metadata={"decision": decision, "without_fee": True},
        )

    else:
        raise HTTPException(status_code=400, detail="Decisao invalida")

    db.commit()
    db.refresh(walk)

    return serialize_operational_walk(walk, db, user=user)

@router.get("/requests")
def requests(user: User = Depends(get_current_user), db: Session = Depends(get_walker_self_db)):
    _require_active_walker(user, db)
    process_expired_attempts(db)
    pending_attempt_rows = (
        db.query(WalkMatchingAttempt.walk_id)
        .filter(
            WalkMatchingAttempt.walker_id == user.id,
            WalkMatchingAttempt.status == "pending",
        )
        .subquery()
    )

    walks = (
        db.query(Walk)
        .filter(
            Walk.id.in_(db.query(pending_attempt_rows.c.walk_id)),
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
            # Fase 1 Passo 3: tenant info
            "tenant_id": operational["tenant_id"],
            "tenant_name": operational["tenant_name"],
            "tenant_brand_color": operational["tenant_brand_color"],
        })
        payloads.append(payload)
    return payloads


@router.get("/tenants")
def walker_tenants(user: User = Depends(get_current_user), db: Session = Depends(get_walker_self_db)):
    """Tenants em que o passeador está ATIVO (status=active) — com branding. Fase 1 Passo 3."""
    _require_active_walker(user, db)
    accesses = (
        db.query(TenantWalkerAccess)
        .filter(TenantWalkerAccess.walker_user_id == user.id, TenantWalkerAccess.status == "active")
        .order_by(TenantWalkerAccess.created_at.desc())
        .all()
    )
    result = []
    for a in accesses:
        tenant = db.get(Tenant, a.tenant_id)
        if not tenant:
            continue
        b = tenant.branding
        result.append({
            "tenant_id": a.tenant_id,
            "slug": tenant.slug,
            "display_name": (b.display_name if b and b.display_name else tenant.name),
            "brand_color": (b.primary_color if b else None),
            "logo_url": (b.logo_url if b else None),
            "access_status": a.status,
            "access_type": a.access_type,
        })
    return result


@router.get("/walks")
def walker_walks(user: User = Depends(get_current_user), db: Session = Depends(get_walker_self_db)):
    _require_active_walker(user, db)
    process_expired_attempts(db)

    visible_statuses = {
        "walker_accepted",
        "ride_scheduled",
        "walker_arriving",
        "ride_in_progress",
        "ride_completed",
        "ride_cancelled",
    }

    walks = (
        db.query(Walk)
        .filter(
            (Walk.walker_id == user.id) | (Walk.assigned_walker_id == user.id),
            Walk.operational_status.in_(visible_statuses),
        )
        .order_by(Walk.created_at.desc())
        .all()
    )

    # Batch: 1 query para saber quais walks têm live-tracking ativo (elimina N+1)
    walk_ids = [walk.id for walk in walks]
    live_ids = _batch_live_tracking(walk_ids, db)
    return [serialize_operational_walk(walk, db, user=user, live_tracking_ids=live_ids) for walk in walks]


@router.post("/walks/{walk_id}/accept")
def accept_walk(walk_id: str, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    _require_active_walker(user, db)
    # with_for_update() garante exclusao mutua em Postgres (no-op em SQLite nos testes).
    walk = db.query(Walk).filter(Walk.id == walk_id).with_for_update().first()
    if not walk:
        raise HTTPException(status_code=404, detail="Passeio nao encontrado")
    # Re-valida disponibilidade apos obter o lock: rejeita apenas se outro passeador
    # ja aceitou. O servico de matching ainda aplica sua propria verificacao atomica.
    if walk.walker_id is not None and walk.walker_id != user.id:
        raise HTTPException(status_code=409, detail="Este passeio ja foi aceito por outro passeador.")
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


# api-T2: schema permissivo da mudanca de status pelo passeador (campo unico `status`).
class WalkerStatusRequest(BaseModel):
    status: str | None = None


@router.post("/walks/{walk_id}/status")
def walker_status(walk_id: str, payload: WalkerStatusRequest, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
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
    # Preserva o default do .get("status", walk.status): so cai no walk.status se a chave
    # nao foi enviada (model_fields_set), nao quando vem explicitamente nula.
    requested_status = str(payload.status) if "status" in payload.model_fields_set else str(walk.status)
    if requested_status in _WALK_COMPLETED_STATUSES:
        raise HTTPException(status_code=409, detail="Finalizacao exige envio de relatorio para revisao administrativa.")
    update_operational_status(walk, requested_status, db, actor=user)
    db.commit()
    db.refresh(walk)
    return {"ok": True, "status": walk.status, "walk": serialize_operational_walk(walk, db, user=user)}


COMPLETION_REPORT_ALLOWED_STATUSES = {
    "ride_in_progress",
    "completion_rejected",
}

# ITEM 7 — reenquadramento legal: esses campos são REGISTRO OPCIONAL do passeio,
# não um protocolo obrigatório imposto ao passeador. Exigir um método de execução
# específico (como retornar a guia, oferecer água etc.) configura controle de método
# (art. 3º CLT). Os campos ficam disponíveis para que o passeador REGISTRE o que
# aconteceu — o checklist é informativo, não um gate de finalização.
# incident_reported continua disponível para relato de incidentes de segurança.
COMPLETION_CHECKLIST_KNOWN_KEYS = {
    "pet_delivered",
    "leash_returned",
    "water_offered",
    "incident_reported",
}


def _completion_review_payload(review: WalkCompletionReview) -> dict:
    checklist = {}
    if review.checklist_json:
        try:
            parsed = json.loads(review.checklist_json)
            checklist = parsed if isinstance(parsed, dict) else {}
        except (TypeError, ValueError):
            checklist = {}
    return {
        "id": review.id,
        "walk_id": review.walk_id,
        "walker_user_id": review.walker_user_id,
        "tutor_user_id": review.tutor_user_id,
        "status": review.status,
        "photo_url": review.photo_url,
        "notes": review.notes,
        "checklist": checklist,
        "admin_note": review.admin_note,
        "reviewed_by_admin_id": review.reviewed_by_admin_id,
        "reviewed_at": review.reviewed_at.isoformat() if review.reviewed_at else None,
        "created_at": review.created_at.isoformat() if review.created_at else None,
        "updated_at": review.updated_at.isoformat() if review.updated_at else None,
    }


def _normalize_completion_checklist(payload: dict) -> dict:
    """Normaliza o checklist de registro do passeio (OPCIONAL).

    ITEM 7 — reenquadramento legal: o checklist é um REGISTRO/CONFIRMAÇÃO informativo,
    não um protocolo obrigatório de execução. Nenhum campo é obrigatório para finalizar
    o passeio — sua ausência NÃO bloqueia a finalização nem o pagamento.

    Campos conhecidos (todos opcionais — registrados se enviados):
      - pet_delivered:    passeador registra que devolveu o pet ao tutor
      - leash_returned:   passeador registra que devolveu a guia
      - water_offered:    passeador registra que ofereceu água ao pet
      - incident_reported: passeador registra que houve incidente (valor de segurança)

    Chaves desconhecidas enviadas pelo frontend são ignoradas silenciosamente.
    Campos ausentes são gravados como False (não ocorreram / não foram informados).
    """
    checklist = payload.get("checklist") or payload.get("checklist_json") or {}
    if isinstance(checklist, str):
        try:
            checklist = json.loads(checklist)
        except (TypeError, ValueError):
            checklist = {}
    if not isinstance(checklist, dict):
        checklist = {}
    # Registra apenas as chaves conhecidas; campos ausentes = False (não informado).
    # NÃO levanta exceção por campos faltantes — ver docstring acima.
    return {key: bool(checklist.get(key, False)) for key in COMPLETION_CHECKLIST_KNOWN_KEYS}


def _notify_admins_completion_review_pending(db: Session, walk: Walk, review: WalkCompletionReview, walker: User, resubmission: bool) -> None:
    admins = db.query(User).filter(User.role.in_(["admin", "super_admin"])).all()
    if not admins:
        return

    pet = db.get(Pet, walk.pet_id) if walk.pet_id else None
    tutor = db.get(User, walk.tutor_id) if walk.tutor_id else None
    pet_name = pet.name if pet and pet.name else "pet"
    tutor_name = tutor.full_name if tutor and tutor.full_name else "tutor"
    walker_name = walker.full_name if walker.full_name else "passeador"
    message = f"Finalizacao do passeio de {pet_name}, tutor {tutor_name}, enviada por {walker_name} aguarda revisao operacional."

    for admin in admins:
        _create_notification(
            db,
            NotificationCreate(
                user_id=admin.id,
                user_role=admin.role,
                title="Nova finalização aguardando revisão",
                message=message,
                type="walk_completion_review_pending",
                related_entity_type="walk_completion_review",
                related_entity_id=review.id,
                metadata={
                    "walk_id": walk.id,
                    "review_id": review.id,
                    "priority": "high",
                    "channel": "in_app",
                    "resubmission": resubmission,
                },
            ),
        )


@router.post("/walks/{walk_id}/completion-photo")
async def upload_walk_completion_photo(
    walk_id: str,
    request: Request,
    file: UploadFile = File(...),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    # U3/G2: rate limit; G7: fotos de finalizacao limitadas a 5 MB.
    enforce_upload_rate_limit(request)
    _require_active_walker(user, db)
    walk = db.get(Walk, walk_id)
    if not walk:
        raise HTTPException(status_code=404, detail="Passeio nao encontrado")
    if walk.walker_id != user.id:
        raise HTTPException(status_code=403, detail="Passeio nao pertence ao passeador")
    if not file.content_type or not file.content_type.startswith("image/"):
        raise HTTPException(status_code=400, detail="Envie uma imagem valida.")

    validated_bytes = await read_image_upload_safely(file, max_bytes=5 * 1024 * 1024)

    safe_walker_id = "".join(char for char in user.id if char.isalnum() or char in {"-", "_"})[:80] or "walker"
    safe_walk_id = "".join(char for char in walk.id if char.isalnum() or char in {"-", "_"})[:80] or "walk"
    destination_dir = WALK_COMPLETION_UPLOAD_ROOT / safe_walker_id / safe_walk_id
    destination_dir.mkdir(parents=True, exist_ok=True)
    extension = _safe_upload_extension(file.filename, file.content_type)
    destination = destination_dir / f"completion-{uuid4().hex}{extension}"

    object_storage.save(destination, validated_bytes, file.content_type)
    await file.close()

    record_upload(
        db, context="walk_completion", owner_id=user.id,
        document_type="completion", storage_path=str(destination),
        mime_type=file.content_type, size_bytes=len(validated_bytes),
    )
    db.commit()

    photo_url = _public_upload_url(request, destination)
    return {
        "ok": True,
        "photo_url": photo_url,
        "url": photo_url,
        "uploaded_at": datetime.utcnow().isoformat(),
    }


# api-T2: schema permissivo do relatorio de finalizacao. Todos os campos sao opcionais
# (espelham os payload.get do helper); checklist/checklist_json sao Any para aceitar tanto
# dict quanto string JSON, como o _normalize_completion_checklist ja tratava. Pydantic v2
# ignora extras. O endpoint converte para dict (model_dump) e mantem o helper intacto.
class CompletionReportRequest(BaseModel):
    photo_url: str | None = None
    url: str | None = None
    notes: str | None = None
    checklist: Any | None = None
    checklist_json: Any | None = None


def _submit_completion_review(walk: Walk, payload: dict | None, user: User, db: Session) -> WalkCompletionReview:
    if walk.walker_id != user.id:
        raise HTTPException(status_code=403, detail="Passeio nao pertence ao passeador")
    if walk.operational_status not in COMPLETION_REPORT_ALLOWED_STATUSES:
        raise HTTPException(status_code=409, detail="Passeio nao esta em status permitido para solicitar finalizacao.")

    payload = payload or {}
    photo_url = str(payload.get("photo_url") or payload.get("url") or "").strip()
    if not photo_url:
        raise HTTPException(status_code=422, detail="photo_url e obrigatorio para solicitar finalizacao.")
    notes = str(payload.get("notes") or "").strip()
    if len(notes) < 8:
        raise HTTPException(status_code=422, detail="notes deve ter pelo menos 8 caracteres.")
    checklist = _normalize_completion_checklist(payload)
    review = (
        db.query(WalkCompletionReview)
        .filter(WalkCompletionReview.walk_id == walk.id, WalkCompletionReview.walker_user_id == user.id)
        .order_by(WalkCompletionReview.created_at.desc())
        .first()
    )
    resubmission = review is not None
    if not review:
        review = WalkCompletionReview(
            walk_id=walk.id,
            walker_user_id=user.id,
            tutor_user_id=walk.tutor_id,
        )
        db.add(review)
        db.flush()

    now = datetime.utcnow()
    review.status = "pending_review"
    review.photo_url = photo_url
    review.notes = notes
    review.checklist_json = json.dumps(checklist)
    review.admin_note = None
    review.reviewed_by_admin_id = None
    review.reviewed_at = None
    review.updated_at = now

    walk.operational_status = "awaiting_completion_review"
    walk.status = "Aguardando validação da finalização"
    log_event(db, walk.id, "completion_report_submitted", actor_type=user.role, actor_id=user.id, metadata={"review_id": review.id})
    _notify_admins_completion_review_pending(db, walk, review, user, resubmission)
    return review


@router.post("/walks/{walk_id}/completion-report")
def submit_completion_report(walk_id: str, payload: CompletionReportRequest | None = None, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    _require_active_walker(user, db)
    walk = db.get(Walk, walk_id)
    if not walk:
        raise HTTPException(status_code=404, detail="Passeio nao encontrado")
    review = _submit_completion_review(walk, payload.model_dump() if payload else None, user, db)
    db.commit()
    db.refresh(review)
    db.refresh(walk)
    return {"ok": True, "review": _completion_review_payload(review), "walk": serialize_operational_walk(walk, db, user=user)}


@router.post("/walks/{walk_id}/report")
def send_report(walk_id: str, payload: CompletionReportRequest | None = None, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    _require_active_walker(user, db)
    walk = db.get(Walk, walk_id)
    if not walk:
        raise HTTPException(status_code=404, detail="Passeio nao encontrado")
    review = _submit_completion_review(walk, payload.model_dump() if payload else None, user, db)
    db.commit()
    db.refresh(review)
    db.refresh(walk)
    return {"ok": True, "review": _completion_review_payload(review), "walk": serialize_operational_walk(walk, db, user=user)}


# api-T2: schema permissivo da ocorrencia operacional do passeador. Todos opcionais,
# espelhando os payload.get; evidences continua list[dict] (formato livre); Pydantic v2
# ignora extras. Nenhum payload legitimo e rejeitado.
class WalkerOccurrenceRequest(BaseModel):
    type: str | None = None
    category: str | None = None
    message: str | None = None
    description: str | None = None
    notes: str | None = None
    target_type: str | None = None
    target_user_id: str | None = None
    target_pet_id: str | None = None
    title: str | None = None
    evidences: list[dict] = Field(default_factory=list)
    metadata: dict | None = None


@router.post("/walks/{walk_id}/occurrence")
def create_walker_occurrence(walk_id: str, payload: WalkerOccurrenceRequest, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    _require_active_walker(user, db)
    walk = db.get(Walk, walk_id)
    if not walk:
        raise HTTPException(status_code=404, detail="Passeio nao encontrado")
    if walk.walker_id != user.id and walk.assigned_walker_id != user.id:
        raise HTTPException(status_code=403, detail="Passeio nao pertence ao passeador")
    occurrence_type = str(payload.type or payload.category or "operational_issue").strip() or "operational_issue"
    message = str(payload.message or payload.description or payload.notes or "").strip()
    if not message:
        message = "Passeador registrou uma ocorrencia operacional."
    category_by_type = {
        "delay": "atraso",
        "operational_issue": "ocorrencia_operacional",
    }
    title_by_type = {
        "delay": "Atraso informado pelo passeador",
        "operational_issue": "Ocorrencia operacional do passeio",
    }
    target_type = payload.target_type or "walk"
    complaint_payload = ComplaintCreate(
        source="walker",
        target_type=target_type,
        target_user_id=walk.tutor_id if target_type in {"tutor", "address", "service"} else payload.target_user_id,
        target_pet_id=walk.pet_id if target_type == "pet" else payload.target_pet_id,
        walk_id=walk.id,
        category=payload.category or category_by_type.get(occurrence_type, "ocorrencia_operacional"),
        title=payload.title or title_by_type.get(occurrence_type, "Ocorrencia operacional do passeio"),
        description=message,
        evidences=[ComplaintEvidenceCreate(**item) for item in payload.evidences],
        metadata={"origin": "walker_walk", "type": occurrence_type, **(payload.metadata or {})},
    )
    complaint = create_complaint(complaint_payload, user, db)
    return {
        "ok": True,
        "walk_id": walk.id,
        "occurrence_id": complaint.id,
        "complaint_id": complaint.id,
        "status": complaint.status,
        "type": occurrence_type,
    }


# ---------------------------------------------------------------------------
# MÁQUINA DE ESTADOS DO PASSEIO (trecho meio: aceite → finalização)
#
# Fluxo canônico de operational_status:
#   walker_accepted / ride_scheduled
#       → walker_arriving         (check-in: walker chegou ao local)
#       → pet_handover_confirmed  (pet entregue ao walker; passeio iniciando)
#       → ride_in_progress        (início formal; checklist de início confirmado)
#       → awaiting_completion_review  (relatório de finalização enviado)
#       → ride_completed              (admin aprova)
#
# Estados "walker_arriving" e "pet_handover_confirmed" são strings novas no campo
# VARCHAR `operational_status` — não exigem migration. Os demais já existiam.
#
# O que NÃO persiste (sem migration):
#   • Itens individuais do checklist (agua, vasilha, etc.) — aceitos no payload,
#     logados como evento operacional, mas não há coluna dedicada. Melhoria futura.
#   • Texto/nota de experiência (did_pee / did_poop) — não há coluna na tabela
#     `walks`; o endpoint aceita, retorna o walk atualizado com os valores recebidos
#     no JSON mas NÃO persiste no banco. Melhoria futura: adicionar colunas.
# ---------------------------------------------------------------------------

# Estados válidos para cada transição
_CHECKIN_ALLOWED = {
    "walker_accepted",
    "ride_scheduled",
    # Idempotência: já chegou mas ainda não entregou
    "walker_arriving",
}
_PET_HANDOVER_ALLOWED = {
    "walker_arriving",
    # Idempotência
    "pet_handover_confirmed",
}
_START_CHECKLIST_ALLOWED = {
    "pet_handover_confirmed",
    # Idempotência
    "ride_in_progress",
}
_CHECKIN_CHECKLIST_ALLOWED = {
    "walker_arriving",
    "pet_handover_confirmed",
    "ride_in_progress",
}
_EXPERIENCE_ALLOWED = {
    "ride_in_progress",
    "awaiting_completion_review",
    "ride_completed",
    # Permite atualizar após finalização também
    "Finalizado",
}
_ACTIVE_STATUSES = {
    "walker_accepted",
    "ride_scheduled",
    "walker_arriving",
    "pet_handover_confirmed",
    "ride_in_progress",
}


def _get_walk_for_walker(walk_id: str, user: User, db: Session) -> Walk:
    """Busca o walk, valida existência e posse do walker. Lança 404/403."""
    walk = db.get(Walk, walk_id)
    if not walk:
        raise HTTPException(status_code=404, detail="Passeio nao encontrado")
    if walk.walker_id not in {user.id, None} and walk.assigned_walker_id not in {user.id, None}:
        raise HTTPException(status_code=403, detail="Passeio nao pertence ao passeador")
    # Exige que o walker seja de fato o responsável (não só "None")
    if walk.walker_id is not None and walk.walker_id != user.id and walk.assigned_walker_id != user.id:
        raise HTTPException(status_code=403, detail="Passeio nao pertence ao passeador")
    return walk


# api-T2: schemas permissivos dos endpoints da maquina de estados. Os 4 itens de
# checklist sao opcionais (None = nao enviado); usamos model_fields_set para incluir no
# log apenas as chaves que o app realmente mandou — mesma semantica do `key in payload`
# anterior, inclusive quando o valor e False. Pydantic v2 ignora extras: nenhum payload
# legitimo e rejeitado.
class WalkerChecklistInput(BaseModel):
    checklist_confirm_water: bool | None = None
    checklist_confirm_bowl: bool | None = None
    checklist_confirm_bags: bool | None = None
    checklist_confirm_first_aid: bool | None = None


_CHECKLIST_CONFIRM_KEYS = (
    "checklist_confirm_water",
    "checklist_confirm_bowl",
    "checklist_confirm_bags",
    "checklist_confirm_first_aid",
)


def _collect_checklist_items(payload: "WalkerChecklistInput | None") -> dict:
    if not payload:
        return {}
    return {
        key: bool(getattr(payload, key))
        for key in _CHECKLIST_CONFIRM_KEYS
        if key in payload.model_fields_set
    }


class WalkExperienceInput(BaseModel):
    did_pee: bool = False
    did_poop: bool = False


@router.post("/walks/{walk_id}/check-in")
def walker_check_in(
    walk_id: str,
    payload: WalkerChecklistInput | None = None,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Walker registra chegada ao local de retirada do pet.

    Transição: walker_accepted | ride_scheduled → walker_arriving
    Retorno:   walk completo + { checked_in: true }
    """
    _require_active_walker(user, db)
    walk = _get_walk_for_walker(walk_id, user, db)

    if walk.operational_status not in _CHECKIN_ALLOWED:
        raise HTTPException(
            status_code=409,
            detail=f"Transicao invalida: check-in nao permitido no status '{walk.operational_status}'.",
        )

    walk.operational_status = "walker_arriving"
    walk.status = "Indo buscar o pet"

    checklist_items = _collect_checklist_items(payload)

    log_event(
        db,
        walk.id,
        "walker_checked_in",
        actor_type=user.role,
        actor_id=user.id,
        metadata={"checklist": checklist_items, "note": "Walker chegou ao local de retirada."},
    )
    db.commit()
    db.refresh(walk)

    result = serialize_operational_walk(walk, db, user=user)
    result["checked_in"] = True
    return result


@router.post("/walks/{walk_id}/pet-handover")
def pet_handover(
    walk_id: str,
    payload: WalkerChecklistInput | None = None,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Walker confirma que recebeu o pet e o passeio está iniciando.

    Transição: walker_arriving → pet_handover_confirmed
    Retorno:   walk completo + { confirmed: true }
    """
    _require_active_walker(user, db)
    walk = _get_walk_for_walker(walk_id, user, db)

    if walk.operational_status not in _PET_HANDOVER_ALLOWED:
        raise HTTPException(
            status_code=409,
            detail=f"Transicao invalida: pet-handover nao permitido no status '{walk.operational_status}'.",
        )

    walk.operational_status = "pet_handover_confirmed"
    walk.status = "Indo buscar o pet"

    log_event(
        db,
        walk.id,
        "pet_handover_confirmed",
        actor_type=user.role,
        actor_id=user.id,
        metadata={"note": "Pet entregue ao passeador; passeio prestes a iniciar."},
    )
    db.commit()
    db.refresh(walk)

    result = serialize_operational_walk(walk, db, user=user)
    result["confirmed"] = True
    return result


@router.post("/walks/{walk_id}/start-checklist")
def confirm_start_checklist(
    walk_id: str,
    payload: WalkerChecklistInput | None = None,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Walker confirma checklist de início do passeio (água, vasilha, saquinhos, primeiros socorros).

    Transição: pet_handover_confirmed → ride_in_progress
    Retorno:   walk completo + { ok: true }

    NOTA: itens individuais do checklist são logados mas não persistidos em coluna
    dedicada (sem migration). O kit_checklist_start_confirmed não existe na tabela
    walks atual — o frontend usa o campo no objeto retornado; como não temos coluna,
    injetamos True no payload de retorno para compatibilidade.
    """
    _require_active_walker(user, db)
    walk = _get_walk_for_walker(walk_id, user, db)

    if walk.operational_status not in _START_CHECKLIST_ALLOWED:
        raise HTTPException(
            status_code=409,
            detail=f"Transicao invalida: start-checklist nao permitido no status '{walk.operational_status}'.",
        )

    walk.operational_status = "ride_in_progress"
    walk.status = "Passeando agora"

    checklist_items = _collect_checklist_items(payload)

    log_event(
        db,
        walk.id,
        "start_checklist_confirmed",
        actor_type=user.role,
        actor_id=user.id,
        metadata={"checklist": checklist_items, "note": "Checklist de inicio confirmado; passeio em andamento."},
    )
    db.commit()
    db.refresh(walk)

    result = serialize_operational_walk(walk, db, user=user)
    result["ok"] = True
    # Injeta campo de compatibilidade com frontend (não há coluna — melhoria futura)
    result["kit_checklist_start_confirmed"] = True
    return result


@router.post("/walks/{walk_id}/checkin-checklist")
def validate_checkin_checklist(
    walk_id: str,
    payload: WalkerChecklistInput | None = None,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Valida/registra checklist de chegada (originalmente ação de admin no frontend).

    Transição: nenhuma — apenas registra o evento operacional.
    Retorno:   walk completo + { ok: true }

    NOTA: idem start-checklist — itens não persistem em coluna dedicada.
    """
    _require_active_walker(user, db)
    walk = _get_walk_for_walker(walk_id, user, db)

    if walk.operational_status not in _CHECKIN_CHECKLIST_ALLOWED:
        raise HTTPException(
            status_code=409,
            detail=f"Checklist de chegada nao permitido no status '{walk.operational_status}'.",
        )

    checklist_items = _collect_checklist_items(payload)

    log_event(
        db,
        walk.id,
        "checkin_checklist_validated",
        actor_type=user.role,
        actor_id=user.id,
        metadata={"checklist": checklist_items, "note": "Checklist de chegada validado."},
    )
    db.commit()
    db.refresh(walk)

    result = serialize_operational_walk(walk, db, user=user)
    result["ok"] = True
    # Injeta campo de compatibilidade com frontend (não há coluna — melhoria futura)
    result["kit_checklist_check_in_confirmed"] = True
    return result


@router.post("/walks/{walk_id}/experience")
def update_walk_experience(
    walk_id: str,
    payload: WalkExperienceInput | None = None,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Walker registra experiência do passeio (did_pee / did_poop).

    Retorno: walk completo com did_pee / did_poop injetados.

    NOTA: a tabela `walks` não possui colunas `did_pee` / `did_poop`. Os valores
    são recebidos, logados e devolvidos no JSON de retorno para compatibilidade com
    o frontend, mas NÃO são persistidos no banco. Melhoria futura: adicionar colunas
    via migration controlada.
    """
    _require_active_walker(user, db)
    walk = _get_walk_for_walker(walk_id, user, db)

    if walk.operational_status not in _EXPERIENCE_ALLOWED and walk.status != "Finalizado":
        raise HTTPException(
            status_code=409,
            detail=f"Experiencia do passeio nao pode ser registrada no status '{walk.operational_status}'.",
        )

    did_pee = bool(payload.did_pee) if payload else False
    did_poop = bool(payload.did_poop) if payload else False

    log_event(
        db,
        walk.id,
        "walk_experience_updated",
        actor_type=user.role,
        actor_id=user.id,
        # Logado para rastreabilidade; sem coluna dedicada — melhoria futura
        metadata={"did_pee": did_pee, "did_poop": did_poop},
    )
    db.commit()
    db.refresh(walk)

    result = serialize_operational_walk(walk, db, user=user)
    # Injeta valores no retorno para compatibilidade com frontend
    result["did_pee"] = did_pee
    result["did_poop"] = did_poop
    return result


@router.get("/walks/active")
def walker_active_walk(
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Retorna o passeio ativo do walker (em andamento ou a caminho).

    Usado pela tela passeio-andamento para obter o walk atual sem precisar
    de um ID explícito. Retorna 404 se não houver passeio ativo.
    """
    _require_active_walker(user, db)

    walk = (
        db.query(Walk)
        .filter(
            (Walk.walker_id == user.id) | (Walk.assigned_walker_id == user.id),
            Walk.operational_status.in_(_ACTIVE_STATUSES),
        )
        .order_by(Walk.created_at.desc())
        .first()
    )

    if not walk:
        raise HTTPException(status_code=404, detail="Nenhum passeio ativo no momento.")

    return serialize_operational_walk(walk, db, user=user)


# api-T2 / Fase 1 Passo 4 §C: schema permissivo do pedido de saque.
# `amount` coage string numérica → float (Pydantic v2) e devolve 422 honesto.
# `tenant_id` é opcional: quando presente, o saque é vinculado ao tenant e
# valida contra o saldo daquele tenant especificamente (nova funcionalidade).
# Quando ausente, mantém comportamento LEGADO idêntico (zero-regressão).
class WithdrawalRequest(BaseModel):
    amount: float | None = None
    tenant_id: str | None = None


@router.post("/withdrawals")
def request_withdrawal(payload: WithdrawalRequest, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    amount = float(payload.amount or 0)
    if amount < 20:
        raise HTTPException(status_code=400, detail="Valor minimo para saque e R$ 20,00")

    # FIX 6e: validar que walker tem chave Pix cadastrada antes de criar o saque.
    _wp = db.query(WalkerProfile).filter(WalkerProfile.user_id == user.id).first()
    _pix_key_for_withdrawal = (_wp.pix_key or "").strip() if _wp else ""
    if not _pix_key_for_withdrawal:
        raise HTTPException(
            status_code=400,
            detail="Cadastre sua chave Pix em Configuracoes antes de solicitar um saque.",
        )

    if payload.tenant_id is not None:
        # ── Fase 1 Passo 4 §C: saque vinculado a tenant ─────────────────────
        # Valida contra o saldo disponível DESTE tenant especificamente.
        by_tenant = _balance_by_tenant(user, db)
        tenant_bucket = by_tenant.get(payload.tenant_id, {"available": 0.0})
        if amount > tenant_bucket["available"]:
            raise HTTPException(status_code=400, detail="Saldo insuficiente para este tenant")
        payment = Payment(
            id=str(uuid4()),
            tutor_id=user.id,
            walk_id=None,
            amount=-amount,
            status="pending",
            provider="pix",
            tenant_id=payload.tenant_id,
        )
    else:
        # ── Ramo LEGADO — comportamento idêntico ao original ─────────────────
        # Valida contra o saldo global e cria Payment sem tenant_id (NULL),
        # exatamente como antes da Fase 1.
        balance = _available_balance(user, db)
        if amount > balance:
            raise HTTPException(status_code=400, detail="Saldo insuficiente")
        payment = Payment(id=str(uuid4()), tutor_id=user.id, walk_id=None, amount=-amount, status="pending", provider="pix")

    db.add(payment)
    db.commit()
    return {
        "ok": True,
        "withdrawal_id": payment.id,
        "amount": amount,
        "status": "pending",
        "pix_key": _pix_key_for_withdrawal,  # FIX 6e: confirmar a chave usada
    }
