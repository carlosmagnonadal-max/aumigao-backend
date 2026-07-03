from __future__ import annotations

import json
from datetime import date, datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field, field_validator, model_validator
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.dependencies.auth import get_current_user
from app.dependencies.rbac import require_permission
from app.dependencies.tenant_scope import get_admin_tenant_scope
from app.models.pet import Pet
from app.models.pet_timeline_event import (
    DIARY_MOODS,
    EVENT_TYPES,
    QUICK_EVENT_TYPES,
    PetTimelineEvent,
)
from app.models.tenant import Tenant, TenantFeature
from app.models.user import User
from app.models.walk import Walk
from app.models.walk_observation import MOOD_VALUES, ENERGY_VALUES, SOCIALIZATION_VALUES, WalkObservation
from app.services import pet_profile_service as svc

router = APIRouter(prefix="/pets", tags=["pet-profile"])
api_router = APIRouter(prefix="/api/pets", tags=["pet-profile"])

admin_router = APIRouter(
    prefix="/admin/pet-profile",
    tags=["pet-profile-admin"],
    dependencies=[Depends(require_permission("admin.access"))],
)
api_admin_router = APIRouter(
    prefix="/api/admin/pet-profile",
    tags=["pet-profile-admin"],
    dependencies=[Depends(require_permission("admin.access"))],
)

# Routers para observação do passeador (prefixo /walks e /api/walks)
walk_obs_router = APIRouter(prefix="/walks", tags=["pet-profile"])
api_walk_obs_router = APIRouter(prefix="/api/walks", tags=["pet-profile"])


def _get_owned_pet(db: Session, pet_id: str, user: User) -> Pet:
    pet = db.query(Pet).filter(Pet.id == pet_id, Pet.tutor_id == user.id).first()
    if not pet:
        raise HTTPException(status_code=404, detail="Pet não encontrado")
    return pet


def _require_active(db: Session, user: User) -> None:
    from app.models.tenant import Tenant
    tid = getattr(user, "tenant_id", None)
    tenant = db.get(Tenant, tid) if tid else None
    if not tenant or not svc.pet_profile_active(tenant, db):
        raise HTTPException(status_code=404, detail="Not found")


def _require_pet_evolution_plan(db: Session, user: User, *, feature: str, label: str) -> None:
    """Gate por PLANO das rotas pro-only da Evolução do Pet (timeline/stats).

    Aplicado POR CIMA do gate 3-camadas (_require_active), sem tocá-lo: se a
    feature está dormente continua 404; se está ativa mas o tenant é free (fora
    do trial) → 403 teaser (code=plan_upgrade_required). O cadastro/ficha do pet
    (PATCH /profile) fica LIBERADO no free — por isso o gate é por rota e não
    pela chave pet_live_profile inteira.
    """
    from app.models.tenant import Tenant
    from app.services.tenant_free_plan_service import enforce_pet_evolution_allowed

    tid = getattr(user, "tenant_id", None)
    tenant = db.get(Tenant, tid) if tid else None
    enforce_pet_evolution_allowed(tenant, feature=feature, label=label)


class TimelineEventCreate(BaseModel):
    event_type: str
    # title é opcional para o diário (derivado do texto); obrigatório nos demais.
    title: str = Field("", max_length=200)
    notes: str = Field("", max_length=4000)
    occurred_at: datetime
    payload_json: str | None = None
    # Campo opcional (Fase 3): data-alvo de lembrete de vacina/vermífugo.
    # Só tem efeito quando event_type in ("vaccine", "medication"). Deve ser futura.
    reminder_due_date: date | None = None
    # Campos do DIÁRIO do tutor (Fase B). Só têm efeito quando event_type=="diary";
    # o payload_json é montado pelo servidor a partir deles (não do payload cru).
    diary_text: str | None = Field(None, max_length=2000)
    diary_mood: str | None = None

    @field_validator("event_type")
    @classmethod
    def _ev(cls, v: str) -> str:
        if v not in EVENT_TYPES:
            raise ValueError(f"event_type inválido: {v!r}")
        return v

    @field_validator("occurred_at")
    @classmethod
    def _not_future(cls, v: datetime) -> datetime:
        if v > datetime.utcnow():
            raise ValueError("occurred_at não pode ser no futuro")
        return v

    @field_validator("reminder_due_date")
    @classmethod
    def _reminder_future(cls, v: date | None) -> date | None:
        if v is not None and v <= date.today():
            raise ValueError("reminder_due_date deve ser uma data futura")
        return v

    @field_validator("diary_mood")
    @classmethod
    def _diary_mood(cls, v: str | None) -> str | None:
        if v is not None and v not in DIARY_MOODS:
            raise ValueError(f"diary_mood inválido: {v!r}. Válidos: {sorted(DIARY_MOODS)}")
        return v

    @model_validator(mode="after")
    def _diary_rules(self):
        """Regras de título: diário exige texto; registro rápido (P0) tem título
        default no servidor (opcional no cliente); demais tipos exigem título."""
        if self.event_type == "diary":
            text = (self.diary_text or "").strip()
            if not text:
                raise ValueError("diary_text é obrigatório para event_type=diary")
        elif self.event_type in QUICK_EVENT_TYPES:
            pass  # título opcional — servidor aplica QUICK_EVENT_TITLES quando ausente
        elif not (self.title or "").strip():
            raise ValueError("title é obrigatório")
        return self


_DIET_TYPES = {"seca", "umida", "natural", "mista", "outro"}
# Enums livres de comportamento (Perfil Vivo P0). "" é aceito (limpar o campo).
_BEHAVIOR_VALUES = {"amigavel", "indiferente", "reativo", "desconhecido"}


class PetHealthUpdate(BaseModel):
    birth_date: date | None = None
    chip_number: str | None = None
    vet_name: str | None = None
    vet_phone: str | None = None
    emergency_contact: str | None = None
    weight: float | None = None
    allergies: str | None = None
    medications: str | None = None
    restrictions: str | None = None
    health_notes: str | None = None
    behavior_notes: str | None = None
    # Ficha rica (Fase A) — aditivos.
    microchip: str | None = None
    diet_type: str | None = None
    diet_brand: str | None = None
    diet_line: str | None = None
    diet_grams_per_meal: int | None = Field(None, ge=0)
    diet_meals_per_day: int | None = Field(None, ge=0)
    diet_meal_times: str | None = None
    diet_notes: str | None = None
    # Ficha expandida (Perfil Vivo P0 — 0094). Aditivos, todos opcionais.
    supplements_json: str | None = None  # JSON: [{name,dose,frequency}]
    food_bag_weight_kg: float | None = Field(None, ge=0)
    food_bag_opened_at: date | None = None
    vet_clinic: str | None = None
    insurance_provider: str | None = None
    insurance_policy: str | None = None
    behavior_with_dogs: str | None = None  # amigavel|indiferente|reativo|desconhecido
    behavior_with_children: str | None = None
    behavior_with_cats: str | None = None
    fear_triggers_json: str | None = None  # JSON: ["trovão","fogos",...]

    @field_validator("diet_type")
    @classmethod
    def _diet_type(cls, v: str | None) -> str | None:
        if v is not None and v != "" and v not in _DIET_TYPES:
            raise ValueError(f"diet_type inválido: {v!r}. Válidos: {sorted(_DIET_TYPES)}")
        return v

    @field_validator("behavior_with_dogs", "behavior_with_children", "behavior_with_cats")
    @classmethod
    def _behavior(cls, v: str | None) -> str | None:
        if v is not None and v != "" and v not in _BEHAVIOR_VALUES:
            raise ValueError(f"comportamento inválido: {v!r}. Válidos: {sorted(_BEHAVIOR_VALUES)}")
        return v


# Campos da ficha rica cuja alteração gera evento na timeline (padrão fase 1).
# NÃO inclui valores sensíveis no payload — só a lista de chaves alteradas.
_HEALTH_TIMELINE_FIELDS = {
    "allergies", "medications", "restrictions", "health_notes",
    "microchip", "chip_number", "vet_name", "vet_phone", "emergency_contact",
    "diet_type", "diet_brand", "diet_line", "diet_grams_per_meal",
    "diet_meals_per_day", "diet_meal_times", "diet_notes",
    # Perfil Vivo P0 (0094) — ficha expandida.
    "supplements_json", "food_bag_weight_kg", "food_bag_opened_at",
    "vet_clinic", "insurance_provider", "insurance_policy",
    "behavior_with_dogs", "behavior_with_children", "behavior_with_cats",
    "fear_triggers_json",
}


def _event_dict(e: PetTimelineEvent) -> dict:
    return {
        "id": e.id, "event_type": e.event_type, "title": e.title, "notes": e.notes,
        "payload_json": e.payload_json, "occurred_at": e.occurred_at.isoformat() if e.occurred_at else None,
        "source": e.source, "created_at": e.created_at.isoformat() if e.created_at else None,
    }




@router.patch("/{pet_id}/profile")
@api_router.patch("/{pet_id}/profile")
def update_health(pet_id: str, payload: PetHealthUpdate,
                  user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    _require_active(db, user)
    pet = _get_owned_pet(db, pet_id, user)
    fields = payload.model_dump(exclude_unset=True)
    # Coleta quais campos da ficha REALMENTE mudaram (para o evento da timeline).
    changed_health: list[str] = []
    for field, value in fields.items():
        if field in _HEALTH_TIMELINE_FIELDS and getattr(pet, field, None) != value:
            changed_health.append(field)
        setattr(pet, field, value)
    # Evento na timeline (padrão fase 1) — SEM valores sensíveis, só as chaves alteradas.
    if changed_health:
        svc.record_timeline_event(
            db, pet,
            event_type="health_note",
            title="Ficha de saúde atualizada",
            occurred_at=datetime.utcnow(),
            source="tutor",
            created_by_user_id=user.id,
            payload_json=json.dumps({"changed_fields": sorted(changed_health)}),
        )
    db.commit()
    db.refresh(pet)
    return {"ok": True}




# ---------------------------------------------------------------------------
# Admin — config get/patch (tenant-scoped)
# ---------------------------------------------------------------------------

class PetProfileConfigUpdate(BaseModel):
    profile_enabled: bool | None = None
    observations_enabled: bool | None = None
    reminders_enabled: bool | None = None
    vaccine_lead_days: int | None = Field(None, ge=0)
    inactivity_days: int | None = Field(None, ge=1)
    share_enabled: bool | None = None
    # Novo: controla a camada 2 (TenantFeature "pet_live_profile") diretamente nesta tela.
    # Ausente = não toca o TenantFeature (compatibilidade retroativa).
    tenant_feature_enabled: bool | None = None


def _get_tenant_feature_row(db: Session, tenant_id: str, feature_key: str) -> "TenantFeature | None":
    return (
        db.query(TenantFeature)
        .filter(TenantFeature.tenant_id == tenant_id, TenantFeature.feature_key == feature_key)
        .first()
    )


def _plan_gates_for_tenant(tenant: Tenant) -> dict:
    """Retorna o gating por plano para as features do Perfil Vivo.

    Reusa os helpers de tenant_free_plan_service sem reimplementar a regra.
    trial ativo = plano efetivo pro → nenhuma feature bloqueada.
    """
    from app.services.tenant_free_plan_service import plan_blocks_feature

    alerts_blocked = plan_blocks_feature(tenant, "pet_alerts")
    share_blocked = plan_blocks_feature(tenant, "pet_share")
    # pet_live_profile (timeline/stats) — bloqueia no free por enforce_pet_evolution_allowed.
    # Aqui reportamos pelo mesmo critério: is_free_plan(effective_plan).
    from app.services.tenant_free_plan_service import is_free_plan, effective_tenant_plan
    evolution_blocked = is_free_plan(effective_tenant_plan(tenant))

    plan = getattr(tenant, "plan", "")
    return {
        "plan": plan,
        "alerts_allowed": not alerts_blocked,
        "share_allowed": not share_blocked,
        "evolution_allowed": not evolution_blocked,
    }


def _config_dict(c, *, tenant: "Tenant | None" = None, db: "Session | None" = None) -> dict:
    base = {
        "tenant_id": c.tenant_id,
        "profile_enabled": c.profile_enabled,
        "observations_enabled": c.observations_enabled,
        "reminders_enabled": c.reminders_enabled,
        "vaccine_lead_days": c.vaccine_lead_days,
        "inactivity_days": c.inactivity_days,
        "share_enabled": c.share_enabled,
    }
    if tenant is not None and db is not None:
        from app.services.pet_profile_service import _env_on, PET_PROFILE_FEATURE_KEY
        from app.services.tenant_plan_service import tenant_feature_enabled as _tf_enabled

        platform_enabled = _env_on("PET_LIVE_PROFILE_ENABLED")
        tf_row = _get_tenant_feature_row(db, tenant.id, PET_PROFILE_FEATURE_KEY)
        # TenantFeature ausente → False (feature nunca habilitada explicitamente).
        tf_enabled = bool(tf_row.enabled) if tf_row is not None else False
        effective = platform_enabled and tf_enabled and bool(c.profile_enabled)

        base["platform_enabled"] = platform_enabled
        base["tenant_feature_enabled"] = tf_enabled
        base["plan_gates"] = _plan_gates_for_tenant(tenant)
        base["effective_active"] = effective
    return base


def _admin_tenant_id(admin: User, db: Session) -> str:
    scope = get_admin_tenant_scope(admin, db)
    # Super-admin global (sem act-as) retorna scope.tenant_id=None;
    # usa o tenant_id do próprio user como fallback — igual a tutor_referral_config.py.
    tid = scope.tenant_id or getattr(admin, "tenant_id", None)
    if not tid:
        raise HTTPException(status_code=400, detail="tenant_id obrigatório para admin global.")
    return tid


def _upsert_tenant_feature(db: Session, tenant_id: str, feature_key: str, enabled: bool) -> None:
    """Cria ou atualiza a linha em TenantFeature para (tenant_id, feature_key).

    Mesmo padrão do PATCH /tenants/features: get-or-create + set enabled + db.add.
    Respeita o unique constraint (tenant_id, feature_key) via get-or-create (sem INSERT
    duplicado). Não chama db.commit() — o caller comita junto com o restante.
    """
    from datetime import datetime

    row = _get_tenant_feature_row(db, tenant_id, feature_key)
    if row is None:
        row = TenantFeature(tenant_id=tenant_id, feature_key=feature_key)
        db.add(row)
    row.enabled = enabled
    row.updated_at = datetime.utcnow()


# NB: a observação estruturada do TENANT (POST tenant_note, Fase E) e o mapa de
# convivência (GET companions, Fase E) ficam em pet_behavior_routes.py — reusam os
# routers/helpers deste módulo (mantém pet_profile.py mais enxuto).


@admin_router.get("/config")
@api_admin_router.get("/config")
def get_config(admin: User = Depends(get_current_user), db: Session = Depends(get_db)):
    tid = _admin_tenant_id(admin, db)
    cfg = svc.get_or_create_pet_profile_config(db, tid)
    db.commit()
    tenant = db.get(Tenant, tid)
    return _config_dict(cfg, tenant=tenant, db=db)


@admin_router.patch("/config")
@api_admin_router.patch("/config")
def patch_config(payload: PetProfileConfigUpdate,
                 admin: User = Depends(get_current_user), db: Session = Depends(get_db)):
    tid = _admin_tenant_id(admin, db)
    cfg = svc.get_or_create_pet_profile_config(db, tid)
    # Separa os campos do config dos campos de controle de TenantFeature.
    raw = payload.model_dump(exclude_unset=True)
    tf_value: bool | None = raw.pop("tenant_feature_enabled", None)
    for field, value in raw.items():
        setattr(cfg, field, value)
    if tf_value is not None:
        from app.services.pet_profile_service import PET_PROFILE_FEATURE_KEY
        _upsert_tenant_feature(db, tid, PET_PROFILE_FEATURE_KEY, tf_value)
    db.commit()
    db.refresh(cfg)
    tenant = db.get(Tenant, tid)
    return _config_dict(cfg, tenant=tenant, db=db)


# ---------------------------------------------------------------------------
# Observação do passeador (Fase 2)
# POST /walks/{walk_id}/observation  e  /api/walks/{walk_id}/observation
# ---------------------------------------------------------------------------

class WalkObservationCreate(BaseModel):
    mood: Optional[str] = None
    energy: Optional[str] = None
    socialization: Optional[str] = None
    peed: Optional[bool] = None
    pooped: Optional[bool] = None
    incident: bool = False
    incident_notes: str = Field("", max_length=2000)

    @field_validator("mood")
    @classmethod
    def _mood(cls, v: Optional[str]) -> Optional[str]:
        if v is not None and v not in MOOD_VALUES:
            raise ValueError(f"mood inválido: {v!r}. Válidos: {MOOD_VALUES}")
        return v

    @field_validator("energy")
    @classmethod
    def _energy(cls, v: Optional[str]) -> Optional[str]:
        if v is not None and v not in ENERGY_VALUES:
            raise ValueError(f"energy inválido: {v!r}. Válidos: {ENERGY_VALUES}")
        return v

    @field_validator("socialization")
    @classmethod
    def _socialization(cls, v: Optional[str]) -> Optional[str]:
        if v is not None and v not in SOCIALIZATION_VALUES:
            raise ValueError(f"socialization inválido: {v!r}. Válidos: {SOCIALIZATION_VALUES}")
        return v


def _obs_dict(obs) -> dict:
    return {
        "id": obs.id,
        "walk_id": obs.walk_id,
        "pet_id": obs.pet_id,
        "tenant_id": obs.tenant_id,
        "walker_user_id": obs.walker_user_id,
        "mood": obs.mood,
        "energy": obs.energy,
        "socialization": obs.socialization,
        "peed": obs.peed,
        "pooped": obs.pooped,
        "incident": obs.incident,
        "incident_notes": obs.incident_notes,
        "created_at": obs.created_at.isoformat() if obs.created_at else None,
    }


@walk_obs_router.post("/{walk_id}/observation", status_code=201)
@api_walk_obs_router.post("/{walk_id}/observation", status_code=201)
def post_walk_observation(
    walk_id: str,
    payload: WalkObservationCreate,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    # 1. Walk existe? — mesmo detail do gate (review P2 #1): um 404 com mensagem
    # diferente vazaria a existência do passeio quando a feature está OFF.
    walk = db.get(Walk, walk_id)
    if not walk:
        raise HTTPException(status_code=404, detail="Not found")

    # 2. Feature ativa para o tenant deste passeio?
    tenant = db.get(Tenant, walk.tenant_id) if walk.tenant_id else None
    if not tenant or not svc.observations_active(tenant, db):
        raise HTTPException(status_code=404, detail="Not found")

    # 3. Ownership: usuário deve ser o passeador do passeio
    is_walker = (walk.walker_id == user.id) or (walk.assigned_walker_id == user.id)
    if not is_walker:
        raise HTTPException(status_code=403, detail="Apenas o passeador deste passeio pode registrar observações")

    # 4. Registra (idempotente). Review P2 #2: dois POSTs concorrentes podem passar
    # ambos pelo SELECT do serviço e o 2º INSERT viola o unique de walk_id — em vez
    # de estourar 500, faz rollback e retorna a observação que venceu a corrida.
    data = {**payload.model_dump(), "walker_user_id": user.id}
    try:
        obs = svc.record_walk_observation(db, walk, data)
        db.commit()
    except IntegrityError:
        db.rollback()
        obs = db.query(WalkObservation).filter(WalkObservation.walk_id == walk_id).first()
        if not obs:
            raise HTTPException(status_code=409, detail="Conflito ao registrar observação; tente novamente")

    return {"observation": _obs_dict(obs)}


