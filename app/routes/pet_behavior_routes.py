"""Comportamento multi-fonte + convivência do pet (Perfil Vivo 2.0 — Fase E).

Reusa os routers e helpers de pet_profile.py (mesmos prefixos/gates) para manter
aquele arquivo enxuto. Duas superfícies novas:

- POST /admin/pet-profile/pets/{pet_id}/timeline — observação estruturada do TENANT
  (event_type="tenant_note", source="admin", payload montado no servidor).
  Incidente/restrição → notifica o TUTOR dono (best-effort).
- GET  /pets/{pet_id}/companions — mapa de convivência a partir dos shared walks
  concluídos (sanitizado: só nome/foto/raça do outro pet).
"""
from __future__ import annotations

from datetime import datetime

from fastapi import Depends, HTTPException
from pydantic import BaseModel, Field, field_validator
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.dependencies.auth import get_current_user
from app.dependencies.tenant_scope import get_admin_tenant_scope
from app.models.pet import Pet
from app.models.pet_timeline_event import TENANT_NOTE_CONTEXTS, TIMELINE_CATEGORIES
from app.models.tenant import Tenant
from app.models.user import User
from app.routes.pet_profile import (
    _event_dict,
    _get_owned_pet,
    _require_active,
    _require_pet_evolution_plan,
    admin_router,
    api_admin_router,
    api_router,
    router,
)
from app.services import pet_profile_service as svc
from app.services.tenant_free_plan_service import enforce_pet_evolution_allowed


# ---------------------------------------------------------------------------
# Observação estruturada do TENANT (Fase E) — event_type="tenant_note"
# ---------------------------------------------------------------------------

class TenantNoteCreate(BaseModel):
    context: str
    category: str
    text: str = Field(..., min_length=1, max_length=2000)
    title: str | None = Field(None, max_length=200)

    @field_validator("context")
    @classmethod
    def _ctx(cls, v: str) -> str:
        if v not in TENANT_NOTE_CONTEXTS:
            raise ValueError(f"context inválido: {v!r}. Válidos: {sorted(TENANT_NOTE_CONTEXTS)}")
        return v

    @field_validator("category")
    @classmethod
    def _cat(cls, v: str) -> str:
        if v not in TIMELINE_CATEGORIES:
            raise ValueError(f"category inválida: {v!r}. Válidos: {sorted(TIMELINE_CATEGORIES)}")
        return v

    @field_validator("text")
    @classmethod
    def _text(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("text é obrigatório")
        return v


def _admin_scoped_pet(db: Session, pet_id: str, admin: User) -> Pet:
    """Resolve o pet dentro do escopo de tenant do admin (RBAC de pets/tenant).

    super_admin global vê qualquer pet; admin de tenant só do próprio tenant.
    Fora do escopo → 404 (não vaza existência).
    """
    scope = get_admin_tenant_scope(admin, db)
    pet = db.query(Pet).filter(Pet.id == pet_id).first()
    if not pet:
        raise HTTPException(status_code=404, detail="Pet não encontrado")
    if not scope.is_global and pet.tenant_id != scope.tenant_id:
        raise HTTPException(status_code=404, detail="Pet não encontrado")
    return pet


@admin_router.post("/pets/{pet_id}/timeline", status_code=201)
@api_admin_router.post("/pets/{pet_id}/timeline", status_code=201)
def add_tenant_note(pet_id: str, payload: TenantNoteCreate,
                    admin: User = Depends(get_current_user), db: Session = Depends(get_db)):
    pet = _admin_scoped_pet(db, pet_id, admin)
    # Gate 3-camadas + plano (Pro+) resolvidos pelo TENANT DO PET (não do admin):
    # feature dormente → 404; ativa mas free → 403 teaser.
    tenant = db.get(Tenant, pet.tenant_id) if pet.tenant_id else None
    if not tenant or not svc.pet_profile_active(tenant, db):
        raise HTTPException(status_code=404, detail="Not found")
    enforce_pet_evolution_allowed(tenant, feature="pet_tenant_note", label="Observação da equipe")

    # Payload montado NO SERVIDOR (padrão diary) — payload cru do cliente ignorado.
    title, payload_json = svc.build_tenant_note(
        context=payload.context, category=payload.category,
        text=payload.text, title=payload.title,
    )
    ev = svc.record_timeline_event(
        db, pet, event_type="tenant_note", title=title,
        occurred_at=datetime.utcnow(), payload_json=payload_json,
        source="admin", created_by_user_id=admin.id,
    )
    # Incidente/restrição → notifica o TUTOR dono (best-effort).
    svc.notify_owner_of_tenant_note(db, pet, category=payload.category, text=payload.text)
    db.commit()
    db.refresh(ev)
    return {"event": _event_dict(ev)}


# ---------------------------------------------------------------------------
# Mapa de convivência (Fase E) — companheiros de shared walk
# ---------------------------------------------------------------------------

def _get_pet_owner_or_admin(db: Session, pet_id: str, user: User) -> Pet:
    """Resolve o pet permitindo o TUTOR dono OU um admin do MESMO tenant.

    Tutor: só o próprio pet. Admin/super_admin: qualquer pet do seu tenant scope.
    Fora disso → 404 (mesmo detail, não vaza existência).
    """
    role = getattr(user, "role", None)
    if role in ("admin", "super_admin"):
        pet = db.query(Pet).filter(Pet.id == pet_id).first()
        if not pet:
            raise HTTPException(status_code=404, detail="Pet não encontrado")
        if role == "admin" and pet.tenant_id != getattr(user, "tenant_id", None):
            raise HTTPException(status_code=404, detail="Pet não encontrado")
        return pet
    return _get_owned_pet(db, pet_id, user)


@router.get("/{pet_id}/companions")
@api_router.get("/{pet_id}/companions")
def get_pet_companions(
    pet_id: str,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    _require_active(db, user)
    _require_pet_evolution_plan(db, user, feature="pet_companions", label="Mapa de convivência do pet")
    pet = _get_pet_owner_or_admin(db, pet_id, user)
    companions = svc.list_pet_companions(db, pet)
    return {"pet_id": pet.id, "companions": companions, "total": len(companions)}
