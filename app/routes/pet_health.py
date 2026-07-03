"""pet_health.py — Carteira de saúde + health-card + briefing do passeio (Perfil Vivo 2.0, Fase A).

Rotas:
  - POST/GET/DELETE /api/pets/{pet_id}/health-records  (tutor dono / admin do tenant)
  - GET            /api/pets/{pet_id}/health-card       (tutor dono / admin do tenant)
  - GET            /api/walks/{walk_id}/pet-briefing     (passeador dono / admin do tenant)

GATING (Fase A = Pro+): sobre o gate de 3 camadas do pet_live_profile (dormente = 404),
aplica-se o gate por PLANO (free sem trial → 403 plan_upgrade_required). O briefing usa
o gate do tenant DO PASSEIO (não do usuário logado).
"""
from __future__ import annotations

from datetime import date, datetime

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, field_validator, model_validator
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.dependencies.auth import get_current_user
from app.models.pet import Pet
from app.models.pet_health_record import HEALTH_RECORD_KINDS, HEALTH_RECORD_ROLES
from app.models.tenant import Tenant
from app.models.user import User
from app.models.walk import Walk
from app.services import pet_achievement_service as achievements
from app.services import pet_health_service as health
from app.services import pet_profile_service as svc
from app.services import pet_wellness_service as wellness
from app.services.tenant_free_plan_service import enforce_pet_evolution_allowed

api_router = APIRouter(prefix="/api/pets", tags=["pet-health"])
router = APIRouter(prefix="/pets", tags=["pet-health"])
api_walk_router = APIRouter(prefix="/api/walks", tags=["pet-health"])
walk_router = APIRouter(prefix="/walks", tags=["pet-health"])


# ---------------------------------------------------------------------------
# Gates e ownership
# ---------------------------------------------------------------------------

def _tenant_of(db: Session, tenant_id: str | None) -> Tenant | None:
    return db.get(Tenant, tenant_id) if tenant_id else None


def _require_active_and_pro(db: Session, tenant: Tenant | None, *, feature: str, label: str) -> None:
    """Gate 3-camadas (dormente = 404) + gate por PLANO (free = 403 teaser)."""
    if not tenant or not svc.pet_profile_active(tenant, db):
        raise HTTPException(status_code=404, detail="Not found")
    enforce_pet_evolution_allowed(tenant, feature=feature, label=label)


def _is_tenant_admin(user: User, tenant_id: str | None) -> bool:
    """True se o usuário é admin do tenant do recurso (co-edição/visão de negócio)."""
    return (
        getattr(user, "role", None) in {"admin", "super_admin"}
        and tenant_id is not None
        and getattr(user, "tenant_id", None) == tenant_id
    )


def _get_pet_for_health(db: Session, pet_id: str, user: User) -> tuple[Pet, str]:
    """Busca o pet exigindo tutor dono OU admin do tenant do pet.

    Retorna (pet, actor_role) onde actor_role ∈ {tutor, admin}. 404 se não
    encontrado / sem acesso (mesma mensagem para não vazar existência).
    """
    pet = db.get(Pet, pet_id)
    if not pet:
        raise HTTPException(status_code=404, detail="Not found")
    if pet.tutor_id == user.id:
        return pet, "tutor"
    if _is_tenant_admin(user, pet.tenant_id):
        return pet, "admin"
    raise HTTPException(status_code=404, detail="Not found")


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------

class HealthRecordCreate(BaseModel):
    kind: str
    name: str = Field(..., min_length=1, max_length=200)
    applied_at: date
    valid_until: date | None = None
    notes: str = Field("", max_length=2000)

    @field_validator("kind")
    @classmethod
    def _kind(cls, v: str) -> str:
        if v not in HEALTH_RECORD_KINDS:
            raise ValueError(f"kind inválido: {v!r}. Válidos: {sorted(HEALTH_RECORD_KINDS)}")
        return v

    @field_validator("applied_at")
    @classmethod
    def _applied_not_future(cls, v: date) -> date:
        if v > date.today():
            raise ValueError("applied_at não pode ser no futuro")
        return v

    @model_validator(mode="after")
    def _valid_after_applied(self):
        if self.valid_until is not None and self.valid_until < self.applied_at:
            raise ValueError("valid_until não pode ser anterior a applied_at")
        return self


# ---------------------------------------------------------------------------
# CRUD /health-records
# ---------------------------------------------------------------------------

@router.post("/{pet_id}/health-records", status_code=201)
@api_router.post("/{pet_id}/health-records", status_code=201)
def create_record(pet_id: str, payload: HealthRecordCreate,
                  user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    pet, actor_role = _get_pet_for_health(db, pet_id, user)
    _require_active_and_pro(db, _tenant_of(db, pet.tenant_id),
                            feature="pet_health_card", label="Carteira de saúde do pet")
    record = health.create_health_record(
        db, pet,
        kind=payload.kind, name=payload.name, applied_at=payload.applied_at,
        valid_until=payload.valid_until, notes=payload.notes,
        created_by_role=actor_role if actor_role in HEALTH_RECORD_ROLES else "tutor",
    )
    db.commit()
    db.refresh(record)
    return {"record": health.record_dict(record)}


@router.get("/{pet_id}/health-records")
@api_router.get("/{pet_id}/health-records")
def list_records(pet_id: str,
                 user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    pet, _ = _get_pet_for_health(db, pet_id, user)
    _require_active_and_pro(db, _tenant_of(db, pet.tenant_id),
                            feature="pet_health_card", label="Carteira de saúde do pet")
    records = health.list_health_records(db, pet.id)
    return {"records": [health.record_dict(r) for r in records]}


@router.delete("/{pet_id}/health-records/{record_id}")
@api_router.delete("/{pet_id}/health-records/{record_id}")
def delete_record(pet_id: str, record_id: str,
                  user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    pet, _ = _get_pet_for_health(db, pet_id, user)
    _require_active_and_pro(db, _tenant_of(db, pet.tenant_id),
                            feature="pet_health_card", label="Carteira de saúde do pet")
    if not health.delete_health_record(db, pet.id, record_id):
        raise HTTPException(status_code=404, detail="Registro não encontrado")
    db.commit()
    return {"ok": True}


# ---------------------------------------------------------------------------
# /health-card
# ---------------------------------------------------------------------------

@router.get("/{pet_id}/health-card")
@api_router.get("/{pet_id}/health-card")
def get_health_card(pet_id: str,
                    user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    pet, _ = _get_pet_for_health(db, pet_id, user)
    _require_active_and_pro(db, _tenant_of(db, pet.tenant_id),
                            feature="pet_health_card", label="Carteira de saúde do pet")
    return health.build_health_card(db, pet)


# ---------------------------------------------------------------------------
# /wellness — Índice de Bem-estar (Fase B, runtime, sem persistência)
# ---------------------------------------------------------------------------

@router.get("/{pet_id}/wellness")
@api_router.get("/{pet_id}/wellness")
def get_wellness(pet_id: str,
                 user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """Score 0-100 composto (clínico/rotina/comportamento) + tendência 30d.

    Mesmo gate/ownership da carteira (Fase A): tutor dono OU admin do tenant do
    pet; feature ativa + plano Pro+ (free → 403 teaser). 100% runtime.
    """
    pet, _ = _get_pet_for_health(db, pet_id, user)
    _require_active_and_pro(db, _tenant_of(db, pet.tenant_id),
                            feature="pet_wellness", label="Índice de Bem-estar do pet")
    return wellness.compute_wellness(db, pet.id)


# ---------------------------------------------------------------------------
# /achievements — Conquistas do pet (Fase C, runtime, sem persistência)
# ---------------------------------------------------------------------------

@router.get("/{pet_id}/achievements")
@api_router.get("/{pet_id}/achievements")
def get_achievements(pet_id: str,
                     user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """Marcos transacionais do pet (passeios/saúde/perfil) + gancho de oferta.

    Mesmo gate/ownership de A/B: tutor dono OU admin do tenant do pet; feature
    ativa + plano Pro+ (free → 403 teaser). 100% runtime, sem persistência.
    """
    pet, _ = _get_pet_for_health(db, pet_id, user)
    _require_active_and_pro(db, _tenant_of(db, pet.tenant_id),
                            feature="pet_achievements", label="Conquistas do pet")
    return achievements.compute_achievements(db, pet)


# ---------------------------------------------------------------------------
# /walks/{walk_id}/pet-briefing (passeador dono / admin do tenant)
# ---------------------------------------------------------------------------

@walk_router.get("/{walk_id}/pet-briefing")
@api_walk_router.get("/{walk_id}/pet-briefing")
def get_pet_briefing(walk_id: str,
                     user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    # 1. Walk existe? (mesma mensagem do gate para não vazar existência).
    walk = db.get(Walk, walk_id)
    if not walk:
        raise HTTPException(status_code=404, detail="Not found")

    # 2. Gate: feature ativa + Pro (usa o TENANT DO PASSEIO).
    tenant = _tenant_of(db, walk.tenant_id)
    _require_active_and_pro(db, tenant, feature="pet_briefing", label="Briefing do passeio")

    # 3. Ownership: passeador do passeio OU admin do tenant do passeio.
    is_walker = user.id in {walk.walker_id, walk.assigned_walker_id}
    if not (is_walker or _is_tenant_admin(user, walk.tenant_id)):
        raise HTTPException(status_code=403, detail="Sem acesso ao briefing deste passeio")

    pet = db.get(Pet, walk.pet_id)
    if not pet:
        raise HTTPException(status_code=404, detail="Not found")
    return health.build_pet_briefing(db, pet)
