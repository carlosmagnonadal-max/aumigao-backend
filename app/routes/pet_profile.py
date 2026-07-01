from __future__ import annotations

import json
from datetime import date, datetime, timedelta
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field, field_validator, model_validator
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.constants import WALK_COMPLETED_STATUSES
from app.core.database import get_db
from app.dependencies.auth import get_current_user
from app.dependencies.rbac import require_permission
from app.dependencies.tenant_scope import get_admin_tenant_scope
from app.models.pet import Pet
from app.models.pet_timeline_event import PetTimelineEvent, EVENT_TYPES
from app.models.tenant import Tenant
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


class TimelineEventCreate(BaseModel):
    event_type: str
    title: str = Field(..., max_length=200)
    notes: str = Field("", max_length=4000)
    occurred_at: datetime
    payload_json: str | None = None
    # Campo opcional (Fase 3): data-alvo de lembrete de vacina/vermífugo.
    # Só tem efeito quando event_type in ("vaccine", "medication"). Deve ser futura.
    reminder_due_date: date | None = None

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


class PetHealthUpdate(BaseModel):
    birth_date: date | None = None
    chip_number: str | None = None
    vet_name: str | None = None
    vet_phone: str | None = None
    emergency_contact: str | None = None
    weight: float | None = None
    allergies: str | None = None
    medications: str | None = None
    health_notes: str | None = None


def _event_dict(e: PetTimelineEvent) -> dict:
    return {
        "id": e.id, "event_type": e.event_type, "title": e.title, "notes": e.notes,
        "payload_json": e.payload_json, "occurred_at": e.occurred_at.isoformat() if e.occurred_at else None,
        "source": e.source, "created_at": e.created_at.isoformat() if e.created_at else None,
    }


@router.get("/{pet_id}/timeline")
@api_router.get("/{pet_id}/timeline")
def get_timeline(pet_id: str, cursor: str | None = Query(None), limit: int = Query(20, ge=1, le=100),
                 user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    _require_active(db, user)
    _get_owned_pet(db, pet_id, user)
    q = db.query(PetTimelineEvent).filter(PetTimelineEvent.pet_id == pet_id)
    if cursor:
        try:
            _cur = datetime.fromisoformat(cursor)
        except ValueError:
            raise HTTPException(status_code=400, detail="cursor inválido")
        q = q.filter(PetTimelineEvent.occurred_at < _cur)
    rows = q.order_by(PetTimelineEvent.occurred_at.desc()).limit(limit + 1).all()
    events = rows[:limit]
    next_cursor = events[-1].occurred_at.isoformat() if len(rows) > limit and events else None
    return {"events": [_event_dict(e) for e in events], "next_cursor": next_cursor}


@router.post("/{pet_id}/timeline", status_code=201)
@api_router.post("/{pet_id}/timeline", status_code=201)
def add_event(pet_id: str, payload: TimelineEventCreate,
              user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    _require_active(db, user)
    pet = _get_owned_pet(db, pet_id, user)
    ev = svc.record_timeline_event(
        db, pet, event_type=payload.event_type, title=payload.title, notes=payload.notes,
        occurred_at=payload.occurred_at, payload_json=payload.payload_json,
        source="tutor", created_by_user_id=user.id,
    )
    # Fiação aditiva (Fase 3): se reminder_due_date presente e event_type elegível,
    # cria/atualiza PetReminder ligado ao evento. Sem efeito quando campo ausente.
    _REMINDER_EVENT_TYPES = {"vaccine", "medication"}
    if payload.reminder_due_date is not None and payload.event_type in _REMINDER_EVENT_TYPES:
        reminder_kind = "vaccine" if payload.event_type == "vaccine" else "vermifuge"
        svc.ensure_vaccine_reminder(
            db, pet,
            due_date=payload.reminder_due_date,
            source_event_id=ev.id,
            kind=reminder_kind,
        )
    db.commit()
    db.refresh(ev)
    return {"event": _event_dict(ev)}


@router.patch("/{pet_id}/profile")
@api_router.patch("/{pet_id}/profile")
def update_health(pet_id: str, payload: PetHealthUpdate,
                  user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    _require_active(db, user)
    pet = _get_owned_pet(db, pet_id, user)
    for field, value in payload.model_dump(exclude_unset=True).items():
        setattr(pet, field, value)
    db.commit()
    db.refresh(pet)
    return {"ok": True}


@router.delete("/{pet_id}/timeline/{event_id}")
@api_router.delete("/{pet_id}/timeline/{event_id}")
def delete_event(pet_id: str, event_id: str,
                 user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    _require_active(db, user)
    _get_owned_pet(db, pet_id, user)
    ev = db.query(PetTimelineEvent).filter(
        PetTimelineEvent.id == event_id, PetTimelineEvent.pet_id == pet_id
    ).first()
    if not ev:
        raise HTTPException(status_code=404, detail="Evento não encontrado")
    if ev.source != "tutor":
        raise HTTPException(status_code=403, detail="Só eventos do tutor podem ser removidos")
    db.delete(ev)
    db.commit()
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


def _config_dict(c) -> dict:
    return {
        "tenant_id": c.tenant_id,
        "profile_enabled": c.profile_enabled,
        "observations_enabled": c.observations_enabled,
        "reminders_enabled": c.reminders_enabled,
        "vaccine_lead_days": c.vaccine_lead_days,
        "inactivity_days": c.inactivity_days,
        "share_enabled": c.share_enabled,
    }


def _admin_tenant_id(admin: User, db: Session) -> str:
    scope = get_admin_tenant_scope(admin, db)
    # Super-admin global (sem act-as) retorna scope.tenant_id=None;
    # usa o tenant_id do próprio user como fallback — igual a tutor_referral_config.py.
    tid = scope.tenant_id or getattr(admin, "tenant_id", None)
    if not tid:
        raise HTTPException(status_code=400, detail="tenant_id obrigatório para admin global.")
    return tid


@admin_router.get("/config")
@api_admin_router.get("/config")
def get_config(admin: User = Depends(get_current_user), db: Session = Depends(get_db)):
    tid = _admin_tenant_id(admin, db)
    cfg = svc.get_or_create_pet_profile_config(db, tid)
    db.commit()
    return _config_dict(cfg)


@admin_router.patch("/config")
@api_admin_router.patch("/config")
def patch_config(payload: PetProfileConfigUpdate,
                 admin: User = Depends(get_current_user), db: Session = Depends(get_db)):
    tid = _admin_tenant_id(admin, db)
    cfg = svc.get_or_create_pet_profile_config(db, tid)
    for field, value in payload.model_dump(exclude_unset=True).items():
        setattr(cfg, field, value)
    db.commit()
    db.refresh(cfg)
    return _config_dict(cfg)


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


# ---------------------------------------------------------------------------
# Stats de evolução do pet (Fase 5)
# GET /pets/{pet_id}/stats  e  /api/pets/{pet_id}/stats
# ---------------------------------------------------------------------------

def _monday_of(d: date) -> date:
    """Segunda-feira da semana de `d`."""
    return d - timedelta(days=d.weekday())


def _pet_stats(pet_id: str, db: Session) -> dict:
    # 1. Série de pesos (últimos 100 eventos weight, asc)
    weight_events = (
        db.query(PetTimelineEvent)
        .filter(PetTimelineEvent.pet_id == pet_id, PetTimelineEvent.event_type == "weight")
        .order_by(PetTimelineEvent.occurred_at.asc())
        .limit(100)
        .all()
    )
    weight_series = []
    for ev in weight_events:
        if not ev.payload_json:
            continue
        try:
            data = json.loads(ev.payload_json)
            kg = float(data["kg"])
            weight_series.append({
                "occurred_at": ev.occurred_at.isoformat() if ev.occurred_at else None,
                "kg": kg,
            })
        except (json.JSONDecodeError, TypeError, ValueError, KeyError):
            continue  # payload malformado: ignorado silenciosamente

    # 2. Passeios por semana (12 semanas, status in WALK_COMPLETED_STATUSES, proxy created_at)
    now = datetime.utcnow()
    cutoff = now - timedelta(weeks=12)
    walks = (
        db.query(Walk)
        .filter(
            Walk.pet_id == pet_id,
            Walk.status.in_(list(WALK_COMPLETED_STATUSES)),
            Walk.created_at >= cutoff,
        )
        .all()
    )
    week_counts: dict[date, int] = {}
    for w in walks:
        if not w.created_at:
            continue
        week_start = _monday_of(w.created_at.date())
        week_counts[week_start] = week_counts.get(week_start, 0) + 1

    # Garante 12 semanas contínuas (pode ter semanas com 0)
    weeks_per_week = []
    for i in range(11, -1, -1):
        ws = _monday_of((now - timedelta(weeks=i)).date())
        weeks_per_week.append({"week_start": ws.isoformat(), "count": week_counts.get(ws, 0)})

    # 3. Observações dos últimos 90 dias
    obs_cutoff = now - timedelta(days=90)
    observations_rows = (
        db.query(WalkObservation)
        .filter(
            WalkObservation.pet_id == pet_id,
            WalkObservation.created_at >= obs_cutoff,
        )
        .all()
    )
    total = len(observations_rows)
    mood_dist = {k: 0 for k in MOOD_VALUES}
    energy_dist = {k: 0 for k in ENERGY_VALUES}
    soc_dist = {k: 0 for k in SOCIALIZATION_VALUES}
    peed_vals, pooped_vals, incidents = [], [], 0
    for o in observations_rows:
        if o.mood and o.mood in mood_dist:
            mood_dist[o.mood] += 1
        if o.energy and o.energy in energy_dist:
            energy_dist[o.energy] += 1
        if o.socialization and o.socialization in soc_dist:
            soc_dist[o.socialization] += 1
        if o.peed is not None:
            peed_vals.append(o.peed)
        if o.pooped is not None:
            pooped_vals.append(o.pooped)
        if o.incident:
            incidents += 1

    peed_pct = round(sum(peed_vals) / len(peed_vals) * 100, 1) if peed_vals else None
    pooped_pct = round(sum(pooped_vals) / len(pooped_vals) * 100, 1) if pooped_vals else None

    return {
        "weight_series": weight_series,
        "walks_per_week": weeks_per_week,
        "observations": {
            "total": total,
            "mood": mood_dist,
            "energy": energy_dist,
            "socialization": soc_dist,
            "peed_pct": peed_pct,
            "pooped_pct": pooped_pct,
            "incidents": incidents,
        },
    }


@router.get("/{pet_id}/stats")
@api_router.get("/{pet_id}/stats")
def get_pet_stats(
    pet_id: str,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    _require_active(db, user)
    _get_owned_pet(db, pet_id, user)
    return _pet_stats(pet_id, db)
