from __future__ import annotations

from datetime import date, datetime

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field, field_validator
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.dependencies.auth import get_current_user
from app.models.pet import Pet
from app.models.pet_timeline_event import PetTimelineEvent, EVENT_TYPES
from app.models.user import User
from app.services import pet_profile_service as svc

router = APIRouter(prefix="/pets", tags=["pet-profile"])
api_router = APIRouter(prefix="/api/pets", tags=["pet-profile"])


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
        q = q.filter(PetTimelineEvent.occurred_at < datetime.fromisoformat(cursor))
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
