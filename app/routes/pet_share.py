"""Compartilhamento público do perfil do pet via token (Fase 4 — LGPD).

POST /api/pets/{pet_id}/share-link  — (auth, dono) cria/recupera token.
DELETE /api/pets/{pet_id}/share-link — (auth, dono) revoga link ativo.
GET  /public/pet/{token}            — (sem auth) payload sanitizado do perfil.

LGPD: body exige {"consent": true} explícito para criar link. Payload público
exclui emergency_contact, chip_number, dados do tutor, birth_date crua, ids
internos e eventos walk_observation/custom/birthday. Idade é calculada (anos/meses).
"""
from __future__ import annotations

import json
import logging
import os
import secrets
from datetime import datetime, timedelta
from typing import Optional
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, field_validator
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.tenant_session import global_scope_session
from app.dependencies.auth import get_current_user
from app.models.pet import Pet
from app.models.pet_share_link import PetShareLink
from app.models.pet_timeline_event import PetTimelineEvent
from app.models.tenant import Tenant
from app.models.user import User
from app.services import pet_profile_service as svc
from app.services.live_share_service import pet_first_name

logger = logging.getLogger(__name__)

# Router sem prefix para a rota pública (igual ao padrão de live_share.py)
public_router = APIRouter(tags=["pet-share"])
# Router autenticado com prefix /api
api_router = APIRouter(prefix="/api", tags=["pet-share"])
# Router bare (sem prefix /api) para paridade com os demais arquivos de rota pet
bare_router = APIRouter(tags=["pet-share"])

PUBLIC_BASE = "https://app.aumigaowalk.com.br"
_SHARE_LINK_TTL_DAYS = 30

# Tipos de evento permitidos no payload público (§4.4)
_PUBLIC_EVENT_TYPES = {"vaccine", "weight", "medication", "health_note"}


def _env_share_on() -> bool:
    return os.getenv("PET_SHARE_ENABLED", "false").lower() in {"1", "true", "yes", "on"}


class ShareLinkCreate(BaseModel):
    consent: bool

    @field_validator("consent")
    @classmethod
    def _require_consent(cls, v: bool) -> bool:
        if not v:
            raise ValueError("consentimento LGPD obrigatório: consent deve ser true")
        return v


def _get_owned_pet(db: Session, pet_id: str, user: User) -> Pet:
    """Retorna o pet se pertence ao user, 404 caso contrário (mesmo padrão de pet_profile.py)."""
    pet = db.query(Pet).filter(Pet.id == pet_id, Pet.tutor_id == user.id).first()
    if not pet:
        raise HTTPException(status_code=404, detail="Pet não encontrado")
    return pet


def _get_tenant(db: Session, user: User) -> Optional[Tenant]:
    tid = getattr(user, "tenant_id", None)
    return db.get(Tenant, tid) if tid else None


def _require_gates(db: Session, user: User) -> Tenant:
    """Valida gate pet_profile_active E share_active. 404 se qualquer um OFF.

    Plano free: share do pet é pro-only — antes do 404 genérico, responde o 403
    de teaser (code=plan_upgrade_required) para o cliente renderizar o CTA de
    upgrade. A chave pet_share está em FREE_PLAN_BLOCKED_FEATURE_KEYS, então
    share_active é False para free fora do trial (o teaser explica o porquê).
    """
    from app.services.tenant_free_plan_service import enforce_pet_evolution_allowed

    tenant = _get_tenant(db, user)
    if not tenant or not svc.pet_profile_active(tenant, db):
        raise HTTPException(status_code=404, detail="Not found")
    enforce_pet_evolution_allowed(tenant, feature="pet_share", label="Compartilhamento do perfil do pet")
    if not svc.share_active(tenant, db):
        raise HTTPException(status_code=404, detail="Not found")
    return tenant


def _calculate_age(birth_date) -> Optional[dict]:
    """Retorna idade calculada como objeto {"years": int, "months": int} a partir
    de birth_date (date). Nunca expõe a data crua. None se sem birth_date.
    Contrato do site (review P1): objeto estruturado, não string.
    """
    if birth_date is None:
        return None
    today = datetime.utcnow().date()
    try:
        bd = birth_date if hasattr(birth_date, "year") else datetime.fromisoformat(str(birth_date)).date()
    except (ValueError, TypeError):
        return None
    total_months = (today.year - bd.year) * 12 + (today.month - bd.month)
    if today.day < bd.day:
        total_months -= 1
    total_months = max(total_months, 0)
    return {"years": total_months // 12, "months": total_months % 12}


def _latest_weight_kg(db: Session, pet_id: str) -> Optional[float]:
    """Extrai o peso mais recente (kg) do evento weight na timeline.
    Tolerante a payload malformado.
    """
    ev = (
        db.query(PetTimelineEvent)
        .filter(
            PetTimelineEvent.pet_id == pet_id,
            PetTimelineEvent.event_type == "weight",
        )
        .order_by(PetTimelineEvent.occurred_at.desc())
        .first()
    )
    if not ev or not ev.payload_json:
        return None
    try:
        data = json.loads(ev.payload_json)
        return float(data["kg"]) if "kg" in data else None
    except (json.JSONDecodeError, TypeError, ValueError, KeyError):
        return None


# Allow-list de chaves do payload_json por tipo de evento no público (review P2):
# nunca repassa o JSON cru — parse, filtra e re-serializa. Malformado → None.
_PUBLIC_PAYLOAD_KEYS: dict[str, set[str]] = {
    "weight": {"kg"},
    "vaccine": {"name", "next_due_date"},
    "medication": {"name", "dosage"},
    "health_note": set(),
}


def _sanitize_event_payload(event_type: str, payload_json: Optional[str]) -> Optional[str]:
    """Filtra o payload_json pelo allow-list de chaves do tipo. Malformado/vazio → None."""
    if not payload_json:
        return None
    allowed = _PUBLIC_PAYLOAD_KEYS.get(event_type, set())
    if not allowed:
        return None
    try:
        data = json.loads(payload_json)
        if not isinstance(data, dict):
            return None
    except (json.JSONDecodeError, TypeError, ValueError):
        return None
    filtered = {k: v for k, v in data.items() if k in allowed}
    return json.dumps(filtered) if filtered else None


def _public_timeline(db: Session, pet_id: str) -> list[dict]:
    """Últimos 50 eventos dos tipos permitidos, sem ids internos, payload filtrado."""
    events = (
        db.query(PetTimelineEvent)
        .filter(
            PetTimelineEvent.pet_id == pet_id,
            PetTimelineEvent.event_type.in_(list(_PUBLIC_EVENT_TYPES)),
        )
        .order_by(PetTimelineEvent.occurred_at.desc())
        .limit(50)
        .all()
    )
    return [
        {
            "event_type": e.event_type,
            "title": e.title,
            "occurred_at": e.occurred_at.isoformat() if e.occurred_at else None,
            "payload_json": _sanitize_event_payload(e.event_type, e.payload_json),
        }
        for e in events
    ]


# ---------------------------------------------------------------------------
# POST /api/pets/{pet_id}/share-link
# ---------------------------------------------------------------------------

def _create_share_link(pet_id: str, body: ShareLinkCreate, user: User, db: Session) -> dict:
    # Gates
    _require_gates(db, user)
    pet = _get_owned_pet(db, pet_id, user)

    now = datetime.utcnow()
    # Reusar link ativo não-expirado (padrão live_share.py)
    existing = (
        db.query(PetShareLink)
        .filter(
            PetShareLink.pet_id == pet_id,
            PetShareLink.revoked_at.is_(None),
            PetShareLink.expires_at > now,
        )
        .order_by(PetShareLink.created_at.desc())
        .first()
    )
    if existing:
        link = existing
    else:
        link = PetShareLink(
            id=str(uuid4()),
            token=secrets.token_urlsafe(32),
            pet_id=pet.id,
            tenant_id=pet.tenant_id,
            created_by=user.id,
            consent_at=now,
            expires_at=now + timedelta(days=_SHARE_LINK_TTL_DAYS),
            revoked_at=None,
            created_at=now,
        )
        db.add(link)
        db.commit()

    return {
        "token": link.token,
        "url": f"{PUBLIC_BASE}/pet/{link.token}",
        "expires_at": link.expires_at.isoformat(),
    }


@api_router.post("/pets/{pet_id}/share-link")
@bare_router.post("/pets/{pet_id}/share-link")
def create_share_link(
    pet_id: str,
    body: ShareLinkCreate,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    return _create_share_link(pet_id, body, user, db)


# ---------------------------------------------------------------------------
# DELETE /api/pets/{pet_id}/share-link
# ---------------------------------------------------------------------------

def _revoke_share_link(pet_id: str, user: User, db: Session) -> dict:
    # Só precisa do gate de perfil ativo (dono pode revogar mesmo sem share_active)
    tenant = _get_tenant(db, user)
    if not tenant or not svc.pet_profile_active(tenant, db):
        raise HTTPException(status_code=404, detail="Not found")
    _get_owned_pet(db, pet_id, user)

    now = datetime.utcnow()
    active_links = (
        db.query(PetShareLink)
        .filter(
            PetShareLink.pet_id == pet_id,
            PetShareLink.revoked_at.is_(None),
        )
        .all()
    )
    for link in active_links:
        link.revoked_at = now
    db.commit()
    return {"ok": True}


@api_router.delete("/pets/{pet_id}/share-link")
@bare_router.delete("/pets/{pet_id}/share-link")
def revoke_share_link(
    pet_id: str,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    return _revoke_share_link(pet_id, user, db)


# ---------------------------------------------------------------------------
# GET /public/pet/{token} — sem auth, global_scope_session
# ---------------------------------------------------------------------------

def _public_pet_profile(token: str) -> dict:
    # Kill-switch global: env OFF → 404 também no público
    if not _env_share_on():
        raise HTTPException(status_code=404, detail="Recurso indisponivel")

    with global_scope_session() as db:
        link = db.query(PetShareLink).filter(PetShareLink.token == token).first()
        if not link:
            raise HTTPException(status_code=404, detail="Link nao encontrado")

        now = datetime.utcnow()
        if link.revoked_at is not None or link.expires_at <= now:
            raise HTTPException(status_code=410, detail="Link expirado ou revogado")

        pet = db.get(Pet, link.pet_id)
        if not pet:
            raise HTTPException(status_code=410, detail="Pet nao encontrado")

        tenant = db.get(Tenant, link.tenant_id) if link.tenant_id else None

        age = _calculate_age(pet.birth_date)
        weight_kg = _latest_weight_kg(db, pet.id)
        timeline = _public_timeline(db, pet.id)

        return {
            # Identidade — nunca expõe nome completo ou dados do tutor.
            # Chaves alinhadas ao contrato do site (review P1).
            "pet_first_name": pet_first_name(pet.name),
            "pet_photo_url": pet.photo_url,
            "species": pet.species,
            "breed": pet.breed,
            "size": pet.size,
            # Idade calculada {"years", "months"} — NUNCA birth_date cru
            "age": age,
            "latest_weight_kg": weight_kg,
            # Saúde (dados do pet, não do tutor)
            "allergies": pet.allergies or None,
            "medications": pet.medications or None,
            "health_notes": pet.health_notes or None,
            # Contato profissional (útil para vet/hotel)
            "vet_name": pet.vet_name,
            "vet_phone": pet.vet_phone,
            # Timeline pública (apenas vaccine/weight/medication/health_note)
            "timeline": timeline,
            # Branding do tenant
            "tenant": {
                "name": getattr(tenant, "name", None) if tenant else None,
                "slug": getattr(tenant, "slug", None) if tenant else None,
                "logo_url": getattr(tenant, "logo_url", None) if tenant else None,
            },
        }


@public_router.get("/public/pet/{token}")
def public_pet_profile(token: str):
    return _public_pet_profile(token)


# Review P0: o site consome /api/public/pet/{token} — variante no api_router
# (mesmo padrão de live_share.py, que registra o público nos dois prefixos).
@api_router.get("/public/pet/{token}")
def api_public_pet_profile(token: str):
    return _public_pet_profile(token)
