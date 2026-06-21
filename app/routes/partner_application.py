"""Endpoints de candidatura de parceiros (partner-applications).

Extraído do god-module app/routes/walker.py para manter coesão.
O APIRouter replica exatamente o prefixo/tags do partner_router original,
portanto as rotas expostas pelo app são idênticas.
"""
import logging
import os
from datetime import datetime
from uuid import uuid4

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, Response, UploadFile
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.security import get_password_hash, verify_password
from app.dependencies.rbac import require_permission
from app.models.user import User
from app.models.walker_profile import WalkerProfile
from app.services import object_storage
from app.services.identity_uniqueness import ensure_unique_identity
from app.services.signed_uploads import create_signed_upload_url
from app.services.tenant_seed_service import default_tenant_id
from app.services.upload_registry import record_upload
from app.services.upload_validation import enforce_application_rate_limit, enforce_upload_rate_limit, read_document_upload_safely, read_image_upload_safely
from app.services.walker_referrals import mark_referral_approved, mark_referral_rejected
from app.utils.registration_validation import normalize_cpf_or_raise, normalize_email_or_raise, normalize_phone_or_raise

# Importa funções/constantes compartilhadas com walker.py (sem circularidade:
# walker.py NÃO importa deste módulo).
from app.routes.walker import (
    ALLOWED_UPLOAD_EXTENSIONS,
    ALLOWED_UPLOAD_TYPES,
    UPLOAD_ROOT,
    _canonical_application_status,
    _ensure_application_complete,
    _normalize_public_walker_image_url,
    _public_status_label,
    _public_upload_url,
    _raw_status_from_label,
    _safe_upload_extension,
)
from app.lib.admin_serializers import _document_key_list

router = APIRouter(prefix="/api/partner-applications", tags=["partner-applications"])

LOGGER = logging.getLogger("aumigao.walker_applications")

# G6: extensões permitidas para documentos (identidade/endereço) incluem PDF.
_DOCUMENT_TYPES_ALLOW_PDF = {"identity_front", "identity_back", "address_proof"}
_DOCUMENT_UPLOAD_EXTENSIONS = ALLOWED_UPLOAD_EXTENSIONS | {".pdf"}


def _safe_document_extension(filename: str | None, content_type: str | None) -> str:
    """Variante de _safe_upload_extension que também aceita .pdf para documentos.

    Chamada apenas para uploads de identidade/endereço (G6). Para fotos, o
    _safe_upload_extension original (imagens apenas) continua sendo usado.
    """
    from pathlib import Path as _Path
    suffix = _Path(filename or "").suffix.lower()
    if suffix in _DOCUMENT_UPLOAD_EXTENSIONS:
        return suffix
    if content_type == "application/pdf":
        return ".pdf"
    if content_type == "image/png":
        return ".png"
    if content_type == "image/webp":
        return ".webp"
    if content_type in {"image/heic", "image/heif"}:
        return ".heic"
    if content_type == "image/jpeg":
        return ".jpg"
    raise HTTPException(status_code=400, detail="Tipo de arquivo nao suportado.")


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------

class PartnerApplicationCreate(BaseModel):
    # Sec-P3: max_length defensivos — anti-DoS/log-injection. Limites generosos.
    full_name: str = Field(..., max_length=200)
    cpf: str = Field(..., max_length=20)
    password: str = Field("", max_length=128)
    phone: str = Field("", max_length=30)
    email: str = Field(..., max_length=254)
    neighborhood_region: str = Field("", max_length=200)
    has_pet_experience: bool = False
    has_third_party_experience: bool = False
    experience_description: str = Field("", max_length=2000)
    bio: str = Field("", max_length=2000)
    experience_options: list[str] = Field(default_factory=list)
    availability: str = Field("", max_length=500)
    profile_photo_url: str | None = Field(None, max_length=2000)
    document_url: str | None = Field(None, max_length=2000)
    identity_document_front_url: str | None = Field(None, max_length=2000)
    identity_document_back_url: str | None = Field(None, max_length=2000)
    proof_of_address_url: str | None = Field(None, max_length=2000)
    selfie_url: str | None = Field(None, max_length=2000)
    accepted_declaration: bool = Field(default=False)


class PartnerApplicationStatusUpdate(BaseModel):
    status: str
    reason: str | None = None
    resubmission_requested_documents: list[str] = Field(default_factory=list)


class PartnerApplicationAdminFieldsUpdate(BaseModel):
    internal_notes: str | None = None
    active_as_walker: bool | None = None
    status: str | None = None
    reason: str | None = None
    reviewed_by_admin_id: str | None = None
    resubmission_requested_documents: list[str] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Helpers exclusivos deste módulo
# ---------------------------------------------------------------------------

def _validate_password_or_raise(password: str):
    if len(password or "") < 8 or not any(char.isalpha() for char in password) or not any(char.isdigit() for char in password):
        raise HTTPException(status_code=400, detail="A senha deve ter pelo menos 8 caracteres, incluindo 1 letra e 1 numero.")


def _extract_experience_options(value: str) -> list[str]:
    parts = [part.strip() for part in (value or "").split("|")]
    return [part for part in parts[1:] if part]


# _document_key_list importado de app.lib.admin_serializers

def _serialize_partner_application(profile: WalkerProfile, db: Session, include_internal: bool = False) -> dict:
    user = db.get(User, profile.user_id) if profile.user_id else None
    identity_front_url = profile.document_url or ""
    identity_back_url = profile.identity_document_back_url or ""
    presentation = profile.bio or profile.experience or ""
    profile_photo_url = _normalize_public_walker_image_url(profile.profile_photo_url) or ""
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
        "bio": presentation,
        "experience_options": _extract_experience_options(profile.experience or ""),
        "availability": "",
        "profile_photo_url": profile_photo_url,
        "document_url": create_signed_upload_url(identity_front_url) or "",
        "identity_document_front_url": create_signed_upload_url(identity_front_url) or "",
        "identity_document_back_url": create_signed_upload_url(identity_back_url) or "",
        "proof_of_address_url": create_signed_upload_url(profile.proof_of_address_url) or "",
        "selfie_url": create_signed_upload_url(profile.selfie_url) or "",
        "accepted_declaration": True,
        "status": _public_status_label(profile.status),
        "raw_status": _canonical_application_status(profile.status),
        "operational_status": _canonical_application_status(profile.status),
        "active_as_walker": bool(profile.active_as_walker and _canonical_application_status(profile.status) == "active"),
        "approved_at": profile.approved_at,
        "rejected_at": profile.rejected_at,
        "rejection_reason": profile.rejection_reason,
        "status_reason": profile.rejection_reason,
        "reviewed_by_admin_id": profile.reviewed_by_admin_id,
        "resubmission_requested_documents": [item for item in (profile.resubmission_requested_documents or "").split(",") if item],
        "created_at": profile.created_at,
        "updated_at": profile.updated_at or profile.created_at,
    }
    if include_internal:
        payload["internal_notes"] = profile.internal_notes or ""
    return payload


def _apply_partner_application_payload(profile: WalkerProfile, payload: PartnerApplicationCreate, *, cpf: str, phone: str):
    identity_front_url = payload.identity_document_front_url or payload.document_url
    presentation = (payload.bio or payload.experience_description or "").strip()
    experience_parts = [presentation, *[item.strip() for item in payload.experience_options if item.strip()]]

    profile.full_name = payload.full_name.strip()
    profile.cpf = cpf
    profile.phone = phone
    profile.city = payload.neighborhood_region.strip()
    profile.state = payload.neighborhood_region.strip()
    profile.experience = " | ".join([item for item in experience_parts if item])
    profile.bio = presentation
    profile.profile_photo_url = _normalize_public_walker_image_url(payload.profile_photo_url)
    profile.document_url = identity_front_url
    profile.identity_document_back_url = payload.identity_document_back_url
    profile.proof_of_address_url = payload.proof_of_address_url
    profile.selfie_url = payload.selfie_url
    profile.status = "submitted"
    profile.active_as_walker = False
    profile.approved_at = None
    profile.rejected_at = None
    profile.updated_at = datetime.utcnow()
    profile.rejection_reason = None


def _apply_profile_status(profile: WalkerProfile, status: str, reason: str | None = None, db: Session | None = None):
    raw_status = _raw_status_from_label(status)
    profile.status = raw_status
    if raw_status == "active":
        profile.active_as_walker = True
        profile.approved_at = profile.approved_at or datetime.utcnow()
        profile.rejected_at = None
        profile.rejection_reason = None
        if db:
            user = db.get(User, profile.user_id)
            if user:
                user.role = "walker"
    elif raw_status == "approved":
        profile.active_as_walker = False
        profile.approved_at = datetime.utcnow()
        profile.rejected_at = None
        profile.rejection_reason = None
    elif raw_status == "rejected":
        profile.active_as_walker = False
        profile.rejected_at = datetime.utcnow()
        profile.approved_at = None
        profile.rejection_reason = reason
    elif raw_status == "resubmission_requested":
        profile.active_as_walker = False
        profile.rejected_at = None
        profile.approved_at = None
        profile.rejection_reason = reason
    else:
        profile.active_as_walker = False
        profile.approved_at = None
        if raw_status != "rejected":
            profile.rejected_at = None
            if raw_status in {"submitted", "under_review"}:
                profile.rejection_reason = None
    profile.updated_at = datetime.utcnow()


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@router.post("/uploads", status_code=201)
async def upload_partner_application_document(
    request: Request,
    document_type: str = Form(...),
    owner_id: str = Form("anonymous"),
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
):
    enforce_upload_rate_limit(request)
    normalized_type = document_type.strip().lower()
    if normalized_type not in ALLOWED_UPLOAD_TYPES:
        raise HTTPException(status_code=400, detail="Tipo de documento invalido.")

    # G6: documentos de identidade/endereço aceitam PDF ou imagem (até 10 MB).
    # Fotos de perfil e selfie aceitam apenas imagem (até 5 MB).
    if normalized_type in _DOCUMENT_TYPES_ALLOW_PDF:
        # G7: limite de 10 MB para documentos (PDF ou imagem).
        validated_bytes = await read_document_upload_safely(file, max_bytes=10 * 1024 * 1024)
    else:
        if not file.content_type or not file.content_type.startswith("image/"):
            raise HTTPException(status_code=400, detail="Envie uma imagem valida.")
        # G7: fotos de perfil/selfie limitadas a 5 MB.
        validated_bytes = await read_image_upload_safely(file, max_bytes=5 * 1024 * 1024)

    safe_owner = "".join(char for char in owner_id.strip().lower() if char.isalnum() or char in {"-", "_", "@"})[:80] or "anonymous"
    destination_dir = UPLOAD_ROOT / safe_owner
    destination_dir.mkdir(parents=True, exist_ok=True)
    # G6: usa helper ciente de PDF para documentos; helper de imagem para fotos.
    if normalized_type in _DOCUMENT_TYPES_ALLOW_PDF:
        extension = _safe_document_extension(file.filename, file.content_type)
    else:
        extension = _safe_upload_extension(file.filename, file.content_type)
    destination = destination_dir / f"{normalized_type}-{uuid4().hex}{extension}"

    object_storage.save(destination, validated_bytes, file.content_type)
    await file.close()

    record_upload(
        db, context="partner_application", owner_id=safe_owner,
        document_type=normalized_type, storage_path=str(destination),
        mime_type=file.content_type, size_bytes=len(validated_bytes),
    )
    db.commit()

    file_url = _public_upload_url(request, destination)
    LOGGER.info("upload de documento walker concluido", extra={"document_type": normalized_type, "owner_id": safe_owner})
    return {
        "documentType": normalized_type,
        "fileUrl": file_url,
        "uploadedAt": datetime.utcnow().isoformat(),
        "reviewStatus": "pending_review",
    }


@router.post("", status_code=201)
def create_partner_application(payload: PartnerApplicationCreate, response: Response, request: Request, db: Session = Depends(get_db)):
    enforce_application_rate_limit(request)
    LOGGER.info("candidatura recebida", extra={"email": payload.email, "full_name": payload.full_name})
    _validate_password_or_raise(payload.password)
    if not payload.accepted_declaration:
        raise HTTPException(status_code=400, detail="Declaracao obrigatoria precisa ser aceita.")
    identity_front_url = payload.identity_document_front_url or payload.document_url
    presentation = (payload.bio or payload.experience_description or "").strip()
    _ensure_application_complete(
        profile_photo_url=payload.profile_photo_url,
        document_url=identity_front_url,
        identity_document_back_url=payload.identity_document_back_url,
        proof_of_address_url=payload.proof_of_address_url,
        bio=presentation,
    )

    try:
        email = normalize_email_or_raise(payload.email)
        cpf = normalize_cpf_or_raise(payload.cpf)
        phone = normalize_phone_or_raise(payload.phone)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    existing_user = db.query(User).filter(User.email == email).first()
    if existing_user:
        existing_profile = db.query(WalkerProfile).filter(WalkerProfile.user_id == existing_user.id).first()
        if existing_profile and verify_password(payload.password, existing_user.password_hash):
            ensure_unique_identity(db, cpf=cpf, phone=phone, current_user_id=existing_user.id)
            previous_status = _canonical_application_status(existing_profile.status)
            previous_active_as_walker = existing_profile.active_as_walker
            previous_approved_at = existing_profile.approved_at
            _apply_partner_application_payload(existing_profile, payload, cpf=cpf, phone=phone)
            if previous_status in {"approved", "active"}:
                existing_profile.status = previous_status
                existing_profile.active_as_walker = previous_active_as_walker
                existing_profile.approved_at = previous_approved_at
            existing_user.full_name = payload.full_name.strip() or existing_user.full_name
            existing_user.role = "walker"
            db.commit()
            db.refresh(existing_profile)
            # mark_referral_under_review(existing_user.id, db)
            response.status_code = 200
            LOGGER.info(
                "candidatura existente reaproveitada",
                extra={"walker_profile_id": existing_profile.id, "user_id": existing_user.id, "status": existing_profile.status},
            )
            return {
                "code": "WALKER_APPLICATION_ALREADY_EXISTS",
                "application": _serialize_partner_application(existing_profile, db),
            }
        raise HTTPException(status_code=409, detail="Este e-mail já está cadastrado.")

    ensure_unique_identity(db, email=email, cpf=cpf, phone=phone)

    tenant_id = default_tenant_id(db)
    user = User(
        id=str(uuid4()),
        email=email,
        password_hash=get_password_hash(payload.password),
        full_name=payload.full_name.strip(),
        role="walker",
        tenant_id=tenant_id,
    )
    db.add(user)
    db.flush()

    profile = db.query(WalkerProfile).filter(WalkerProfile.user_id == user.id).first()
    if not profile:
        profile = WalkerProfile(id=str(uuid4()), user_id=user.id)
        db.add(profile)

    _apply_partner_application_payload(profile, payload, cpf=cpf, phone=phone)
    db.commit()
    db.refresh(profile)
    LOGGER.info(
        "candidatura criada",
        extra={
            "walker_profile_id": profile.id,
            "user_id": profile.user_id,
            "status": profile.status,
            "active_as_walker": profile.active_as_walker,
        },
    )
    # mark_referral_under_review(user.id, db)
    LOGGER.info("candidatura salva", extra={"walker_profile_id": profile.id, "status": profile.status})
    return _serialize_partner_application(profile, db)


@router.get("")
def list_partner_applications(
    db: Session = Depends(get_db),
    _admin: User = Depends(require_permission("walkers.read")),
):
    return [_serialize_partner_application(profile, db) for profile in db.query(WalkerProfile).order_by(WalkerProfile.created_at.desc()).all()]


@router.get("/{candidate_id}")
def get_partner_application(
    candidate_id: str,
    db: Session = Depends(get_db),
    _admin: User = Depends(require_permission("walkers.read")),
):
    profile = db.get(WalkerProfile, candidate_id)
    if not profile:
        raise HTTPException(status_code=404, detail="Candidatura nao encontrada")
    return _serialize_partner_application(profile, db)


@router.patch("/{candidate_id}/status")
def update_partner_application_status(
    candidate_id: str,
    payload: PartnerApplicationStatusUpdate,
    db: Session = Depends(get_db),
    _admin: User = Depends(require_permission("walkers.validate")),
):
    profile = db.get(WalkerProfile, candidate_id)
    if not profile:
        raise HTTPException(status_code=404, detail="Candidatura nao encontrada")
    _apply_profile_status(profile, payload.status, payload.reason, db)
    if payload.resubmission_requested_documents:
        profile.resubmission_requested_documents = _document_key_list(payload.resubmission_requested_documents)
    # Marca referral antes do commit para que tudo persista em uma unica transacao.
    if profile.status == "active":
        mark_referral_approved(profile.user_id, db, commit=False)
    elif profile.status == "rejected":
        mark_referral_rejected(profile.user_id, profile.rejection_reason, db, commit=False)
    db.commit()
    db.refresh(profile)
    return _serialize_partner_application(profile, db, include_internal=True)


@router.patch("/{candidate_id}/admin-fields")
def update_partner_application_admin_fields(
    candidate_id: str,
    payload: PartnerApplicationAdminFieldsUpdate,
    db: Session = Depends(get_db),
    _admin: User = Depends(require_permission("walkers.validate")),
):
    profile = db.get(WalkerProfile, candidate_id)
    if not profile:
        raise HTTPException(status_code=404, detail="Candidatura nao encontrada")
    if payload.internal_notes is not None:
        profile.internal_notes = payload.internal_notes
    if payload.status is not None:
        _apply_profile_status(profile, payload.status, payload.reason, db)
    if payload.reviewed_by_admin_id is not None:
        profile.reviewed_by_admin_id = payload.reviewed_by_admin_id
    if payload.resubmission_requested_documents:
        profile.resubmission_requested_documents = _document_key_list(payload.resubmission_requested_documents)
    if payload.active_as_walker is not None:
        if payload.active_as_walker and profile.status not in {"approved", "active"}:
            raise HTTPException(status_code=400, detail="Apenas candidatos aprovados podem ser ativados como passeador.")
        profile.active_as_walker = payload.active_as_walker
        profile.status = "active" if payload.active_as_walker else "approved"
        if payload.active_as_walker and not profile.approved_at:
            profile.approved_at = datetime.utcnow()
        if payload.active_as_walker:
            user = db.get(User, profile.user_id)
            if user:
                user.role = "walker"
            # Marca referral antes do commit para que tudo persista em uma unica transacao.
            mark_referral_approved(profile.user_id, db, commit=False)
    profile.updated_at = datetime.utcnow()
    db.commit()
    db.refresh(profile)
    return _serialize_partner_application(profile, db, include_internal=True)
