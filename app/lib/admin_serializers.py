"""Helpers de serialização e normalização usados pelos endpoints do admin.

Este módulo contém SOMENTE funções puras (sem side-effects de banco, sem
decoradores de rota). Importa de models/, services/ e schemas/ — nunca de
app.routes.admin (evita import circular).
"""
from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy.orm import Session

from app.models.payment import Payment
from app.models.pet import Pet
from app.models.user import User
from app.models.walk import Walk
from app.models.walk_completion_review import WalkCompletionReview
from app.models.walker_kit_submission import WalkerKitSubmission
from app.models.walker_profile import WalkerProfile
from app.models.tutor_profile import TutorProfile
from app.schemas.tenant_payment_config import TenantPaymentConfigResponse
from app.services.signed_uploads import create_signed_upload_url
from app.services.walker_operational_score_service import (
    calculate_walker_operational_score,
)
from app.services.operational_matching_service import serialize_operational_walk

_logger = logging.getLogger("aumigao.admin")

# ---------------------------------------------------------------------------
# Constante compartilhada de tokens de entidades fake/demo
# ---------------------------------------------------------------------------

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

# Status sets importados para evitar dependência circular com admin.py
PAID_PAYMENT_STATUSES = {"paid", "Pago", "pagamento_confirmado_sandbox", "payment_confirmed", "confirmed"}
COMPLETION_REVIEW_MUTABLE_STATUSES = {"pending", "pending_review", "under_review"}

# ---------------------------------------------------------------------------
# Helpers utilitários básicos
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# Helpers de detecção de entidades fake
# ---------------------------------------------------------------------------

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


def _is_real_walker_user_preloaded(user: User | None, profile: WalkerProfile | None) -> bool:
    if not user or user.role not in {"walker", "passeador"}:
        return False
    if _is_fake_user(user):
        return False
    return not profile or not _is_fake_walker_profile(profile, user)


# ---------------------------------------------------------------------------
# Helpers de perfil / lookup
# ---------------------------------------------------------------------------

def _profile_user(profile: WalkerProfile, db: Session) -> User | None:
    return db.get(User, profile.user_id) if profile.user_id else None


def _walker_name(profile: WalkerProfile, db: Session) -> str:
    user = db.get(User, profile.user_id) if profile.user_id else None
    return (user.full_name if user else None) or (user.email if user else None) or "Passeador"


# ---------------------------------------------------------------------------
# Helpers de realness de passeio
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# Helpers de normalização de status
# ---------------------------------------------------------------------------

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


def _split_scheduled_date(value: str) -> tuple[str | None, str | None]:
    if not value:
        return None, None
    date_part, _, time_part = value.partition("T")
    return date_part or None, time_part[:5] or None


# ---------------------------------------------------------------------------
# Helpers de serialização de entidades
# ---------------------------------------------------------------------------

def _document_key_list(values: list[str] | None) -> str:
    return ",".join([str(item).strip() for item in (values or []) if str(item).strip()])


def _serialize_admin_user(user: User) -> dict:
    return {
        "id": user.id,
        "is_test": _is_fake_user(user),
        "email": user.email,
        "full_name": user.full_name,
        "role": user.role,
        "is_active": user.is_active,
        "tenant_id": user.tenant_id,
        "created_at": user.created_at,
    }


def _serialize_payment_config(config) -> TenantPaymentConfigResponse:
    return TenantPaymentConfigResponse(
        tenant_id=config.tenant_id,
        provider=config.provider,
        commission_percent=config.commission_percent,
        commission_is_custom=getattr(config, "commission_is_custom", False),
        tenant_margin_percent=getattr(config, "tenant_margin_percent", 0.0) or 0.0,
        split_enabled=config.split_enabled,
        active=config.active,
    )


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
        "is_test": _is_fake_user(user),
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


def _serialize_admin_pet(pet: Pet, db: Session) -> dict:
    tutor = db.get(User, pet.tutor_id) if pet.tutor_id else None

    return {
        "id": pet.id,
        "pet_id": pet.id,
        "is_test": not _is_real_pet(pet, tutor),
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


def _serialize_walker_profile(profile: WalkerProfile, db: Session, include_internal: bool = True, operational_score: dict | None = None) -> dict:
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
        "asaas_wallet_id": getattr(profile, "asaas_wallet_id", None),
        # Porte máximo de cão aceito pelo passeador (alimenta o matching; migration 0034).
        "max_dog_size": getattr(profile, "max_dog_size", None),
        "has_vehicle": bool(getattr(profile, "has_vehicle", False)),
    }
    # operational_score pode vir pré-calculado em lote (evita N+1 nas listagens);
    # senão calcula sob demanda (detalhe de 1 perfil).
    payload.update(operational_score if operational_score is not None else calculate_walker_operational_score(profile.user_id, db))
    if include_internal:
        payload["internal_notes"] = profile.internal_notes or ""
    return payload


def _serialize_admin_walk(
    walk: Walk,
    db: Session,
    live_tracking_ids: set[str] | None = None,
    users_by_id: dict | None = None,
    pets_by_id: dict | None = None,
) -> dict:
    payload = serialize_operational_walk(walk, db, include_private=True, live_tracking_ids=live_tracking_ids)
    # is_test: usa preloads batch quando disponíveis (sem N+1 extra no listing).
    if users_by_id is not None and pets_by_id is not None:
        payload["is_test"] = not _is_real_admin_walk_preloaded(
            walk, users_by_id, pets_by_id, {}
        )
    else:
        payload["is_test"] = not _is_real_admin_walk(walk, db)
    return payload


def _preload_admin_payment_refs(
    payments: list[Payment], db: Session
) -> tuple[dict[str, Walk], dict[str, User], dict[str, Pet]]:
    """Batch preload de walks/tutores/pets das linhas de pagamento (elimina N+1).

    Substitui 3×db.get por pagamento (até 1000 pagamentos => 3000 queries) por 3
    queries IN(...). A semântica de lookup por id é idêntica ao db.get.
    """
    walk_ids = {p.walk_id for p in payments if p.walk_id}
    tutor_ids = {p.tutor_id for p in payments if p.tutor_id}
    walks_by_id = (
        {w.id: w for w in db.query(Walk).filter(Walk.id.in_(walk_ids)).all()}
        if walk_ids else {}
    )
    pet_ids = {w.pet_id for w in walks_by_id.values() if w.pet_id}
    tutors_by_id = (
        {u.id: u for u in db.query(User).filter(User.id.in_(tutor_ids)).all()}
        if tutor_ids else {}
    )
    pets_by_id = (
        {p.id: p for p in db.query(Pet).filter(Pet.id.in_(pet_ids)).all()}
        if pet_ids else {}
    )
    return walks_by_id, tutors_by_id, pets_by_id


def _serialize_admin_payment(
    payment: Payment,
    db: Session,
    walks_by_id: dict | None = None,
    tutors_by_id: dict | None = None,
    pets_by_id: dict | None = None,
) -> dict:
    # Usa preloads batch quando disponíveis (listagem); senão db.get (detalhe único).
    if walks_by_id is not None:
        walk = walks_by_id.get(payment.walk_id) if payment.walk_id else None
    else:
        walk = db.get(Walk, payment.walk_id) if payment.walk_id else None
    if tutors_by_id is not None:
        tutor = tutors_by_id.get(payment.tutor_id) if payment.tutor_id else None
    else:
        tutor = db.get(User, payment.tutor_id) if payment.tutor_id else None
    if pets_by_id is not None:
        pet = pets_by_id.get(walk.pet_id) if walk and walk.pet_id else None
    else:
        pet = db.get(Pet, walk.pet_id) if walk and walk.pet_id else None
    walk_date, walk_time = _split_scheduled_date(walk.scheduled_date) if walk else (None, None)
    return {
        "id": payment.id,
        "is_test": _has_fake_token(payment.id, payment.tutor_id, payment.walk_id, payment.provider, payment.provider_payment_id),
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
        "invoice_url": payment.invoice_url,
        "created_at": payment.created_at,
    }


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
