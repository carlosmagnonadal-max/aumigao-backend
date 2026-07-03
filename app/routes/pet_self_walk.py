"""pet_self_walk.py — Passeio self-serve do tutor (Perfil Vivo 2.0, Fase D).

Rotas (tutor DONO do pet — self-serve; NÃO é transação):
  - POST   /api/pets/{pet_id}/self-walks
  - GET    /api/pets/{pet_id}/self-walks?limit=20
  - DELETE /api/pets/{pet_id}/self-walks/{self_walk_id}

GATING (Fase D = Pro+): gate de 3 camadas do pet_live_profile (dormente = 404) +
gate por PLANO (free sem trial → 403 plan_upgrade_required). MESMO padrão da
Fase A (pet_health). Ao contrário da carteira/briefing (co-editáveis por admin),
o self-walk é EXCLUSIVO do tutor dono — o passeio é dele, com o cão dele.
"""
from __future__ import annotations

from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field, field_validator
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.dependencies.auth import get_current_user
from app.models.pet import Pet
from app.models.pet_self_walk import (
    DISTANCE_MAX_KM,
    DURATION_MAX_SECONDS,
    DURATION_MIN_SECONDS,
    MAX_SELF_WALKS_PER_DAY,
    SELF_WALK_INTENSITIES,
    SELF_WALK_TYPES,
    STARTED_MAX_AGE_HOURS,
)
from app.models.tenant import Tenant
from app.models.user import User
from app.services import pet_profile_service as svc
from app.services import pet_self_walk_service as self_walks
from app.services.tenant_free_plan_service import enforce_pet_evolution_allowed

api_router = APIRouter(prefix="/api/pets", tags=["pet-self-walk"])
router = APIRouter(prefix="/pets", tags=["pet-self-walk"])

_FEATURE = "pet_self_walk"
_LABEL = "Passeio do tutor"


# ---------------------------------------------------------------------------
# Gate + ownership (tutor DONO — self-serve)
# ---------------------------------------------------------------------------

def _tenant_of(db: Session, tenant_id: str | None) -> Tenant | None:
    return db.get(Tenant, tenant_id) if tenant_id else None


def _require_active_and_pro(db: Session, tenant: Tenant | None) -> None:
    """Gate 3-camadas (dormente = 404) + gate por PLANO (free = 403 teaser)."""
    if not tenant or not svc.pet_profile_active(tenant, db):
        raise HTTPException(status_code=404, detail="Not found")
    enforce_pet_evolution_allowed(tenant, feature=_FEATURE, label=_LABEL)


def _get_owned_pet(db: Session, pet_id: str, user: User) -> Pet:
    """Exige tutor DONO do pet (self-serve). 404 se não encontrado / sem acesso."""
    pet = db.get(Pet, pet_id)
    if not pet or pet.tutor_id != user.id:
        raise HTTPException(status_code=404, detail="Not found")
    return pet


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------

class SelfWalkNeeds(BaseModel):
    pee: bool = False
    poop: bool = False
    water: bool = False


class SelfWalkBehavior(BaseModel):
    interacted_dogs: bool = False
    interacted_people: bool = False
    pulled_leash: bool = False
    showed_fear: bool = False
    showed_reactivity: bool = False


class SelfWalkCreate(BaseModel):
    started_at: datetime
    duration_seconds: int
    distance_km: float | None = None
    walk_type: str
    intensity: str
    had_gps: bool = False
    needs: SelfWalkNeeds = Field(default_factory=SelfWalkNeeds)
    behavior: SelfWalkBehavior = Field(default_factory=SelfWalkBehavior)
    notes: str = Field("", max_length=1000)

    @field_validator("duration_seconds")
    @classmethod
    def _duration(cls, v: int) -> int:
        if not (DURATION_MIN_SECONDS <= v <= DURATION_MAX_SECONDS):
            raise ValueError(
                f"duration_seconds deve estar entre {DURATION_MIN_SECONDS} e "
                f"{DURATION_MAX_SECONDS}"
            )
        return v

    @field_validator("distance_km")
    @classmethod
    def _distance(cls, v: float | None) -> float | None:
        if v is None:
            return v
        if not (0 <= v <= DISTANCE_MAX_KM):
            raise ValueError(f"distance_km deve estar entre 0 e {DISTANCE_MAX_KM}")
        return v

    @field_validator("walk_type")
    @classmethod
    def _walk_type(cls, v: str) -> str:
        if v not in SELF_WALK_TYPES:
            raise ValueError(f"walk_type inválido: {v!r}. Válidos: {sorted(SELF_WALK_TYPES)}")
        return v

    @field_validator("intensity")
    @classmethod
    def _intensity(cls, v: str) -> str:
        if v not in SELF_WALK_INTENSITIES:
            raise ValueError(f"intensity inválido: {v!r}. Válidos: {sorted(SELF_WALK_INTENSITIES)}")
        return v

    @field_validator("started_at")
    @classmethod
    def _started_at(cls, v: datetime) -> datetime:
        # Compara em naive-UTC (o contrato manda ISO sem tz — ex. 2026-07-02T18:00:00).
        ref = v.replace(tzinfo=None) if v.tzinfo is not None else v
        now = datetime.utcnow()
        if ref > now + timedelta(minutes=5):  # tolerância de clock skew
            raise ValueError("started_at não pode ser no futuro")
        if ref < now - timedelta(hours=STARTED_MAX_AGE_HOURS):
            raise ValueError(f"started_at não pode ser mais de {STARTED_MAX_AGE_HOURS}h atrás")
        return ref


# ---------------------------------------------------------------------------
# Rotas
# ---------------------------------------------------------------------------

@router.post("/{pet_id}/self-walks", status_code=201)
@api_router.post("/{pet_id}/self-walks", status_code=201)
def create_self_walk(pet_id: str, payload: SelfWalkCreate,
                     user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    pet = _get_owned_pet(db, pet_id, user)
    _require_active_and_pro(db, _tenant_of(db, pet.tenant_id))

    # Rate-limit de bom senso: teto diário por pet (evita spam de score).
    if self_walks.day_limit_reached(db, pet.id, payload.started_at.date()):
        raise HTTPException(
            status_code=429,
            detail=f"Limite de {MAX_SELF_WALKS_PER_DAY} passeios do tutor por dia atingido para este pet",
        )

    sw = self_walks.create_self_walk(
        db, pet, user.id,
        started_at=payload.started_at,
        duration_seconds=payload.duration_seconds,
        distance_km=payload.distance_km,
        walk_type=payload.walk_type,
        intensity=payload.intensity,
        had_gps=payload.had_gps,
        needs=payload.needs.model_dump(),
        behavior=payload.behavior.model_dump(),
        notes=payload.notes,
    )
    db.commit()
    db.refresh(sw)
    return {"self_walk": self_walks.self_walk_dict(sw)}


@router.get("/{pet_id}/self-walks")
@api_router.get("/{pet_id}/self-walks")
def list_self_walks(pet_id: str, limit: int = Query(20, ge=1, le=100),
                    user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    pet = _get_owned_pet(db, pet_id, user)
    _require_active_and_pro(db, _tenant_of(db, pet.tenant_id))
    rows = self_walks.list_self_walks(db, pet.id, limit=limit)
    return {"self_walks": [self_walks.self_walk_dict(r) for r in rows]}


@router.delete("/{pet_id}/self-walks/{self_walk_id}", status_code=204)
@api_router.delete("/{pet_id}/self-walks/{self_walk_id}", status_code=204)
def delete_self_walk(pet_id: str, self_walk_id: str,
                     user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    pet = _get_owned_pet(db, pet_id, user)
    _require_active_and_pro(db, _tenant_of(db, pet.tenant_id))
    if not self_walks.delete_self_walk(db, pet.id, self_walk_id):
        raise HTTPException(status_code=404, detail="Passeio não encontrado")
    db.commit()
    return None
