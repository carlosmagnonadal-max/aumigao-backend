"""Diário do tutor + estatísticas de evolução do pet (Perfil Vivo 2.0, Fases B e 5).

Reusa os routers e helpers de pet_profile.py (mesmos prefixos/gates) para manter
aquele arquivo abaixo de 500 linhas. Superfícies registradas aqui:

- GET  /pets/{pet_id}/timeline   — timeline paginada com filtro de categoria
- POST /pets/{pet_id}/timeline   — cria evento (inclui diário, Fase B)
- DELETE /pets/{pet_id}/timeline/{event_id} — remove evento do tutor
- GET  /pets/{pet_id}/stats      — séries de peso, passeios/semana, observações (Fase 5)

Todos duplicados no prefix /api/* (padrão do projeto).
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta
from typing import Optional

from fastapi import Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.constants import WALK_COMPLETED_STATUSES
from app.core.database import get_db
from app.dependencies.auth import get_current_user
from app.models.pet_timeline_event import (
    EVENT_TYPE_CATEGORY,
    PetTimelineEvent,
    TIMELINE_CATEGORIES,
)
from app.models.user import User
from app.models.walk import Walk
from app.models.walk_observation import MOOD_VALUES, ENERGY_VALUES, SOCIALIZATION_VALUES, WalkObservation
from app.routes.pet_profile import (
    TimelineEventCreate,
    _event_dict,
    _get_owned_pet,
    _require_active,
    _require_pet_evolution_plan,
    api_router,
    router,
)
from app.services import pet_profile_service as svc

# Tipos que NÃO têm categoria fixa e aparecem em TODAS as categorias (Fase E):
# diary (entrada livre do tutor). Somado ao mapa EVENT_TYPE_CATEGORY.
_CATEGORY_ALWAYS_TYPES = {"diary"}


def _category_matches(ev: PetTimelineEvent, category: str) -> bool:
    """True se o evento pertence à `category` (Fase E).

    - diary: aparece em todas as categorias (sem categoria fixa);
    - tenant_note: categoria vem do payload_json.category (por-evento);
    - demais tipos: mapa EVENT_TYPE_CATEGORY (tipo→categoria default).
    """
    if ev.event_type in _CATEGORY_ALWAYS_TYPES:
        return True
    if ev.event_type == "tenant_note":
        try:
            return (json.loads(ev.payload_json or "{}") or {}).get("category") == category
        except (json.JSONDecodeError, TypeError):
            return False
    return EVENT_TYPE_CATEGORY.get(ev.event_type) == category


@router.get("/{pet_id}/timeline")
@api_router.get("/{pet_id}/timeline")
def get_timeline(pet_id: str, cursor: str | None = Query(None), limit: int = Query(20, ge=1, le=100),
                 category: str | None = Query(None),
                 user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    _require_active(db, user)
    _require_pet_evolution_plan(db, user, feature="pet_timeline", label="Timeline do pet")
    _get_owned_pet(db, pet_id, user)
    if category is not None and category not in TIMELINE_CATEGORIES:
        raise HTTPException(status_code=422, detail=f"category inválida: {category!r}")
    q = db.query(PetTimelineEvent).filter(PetTimelineEvent.pet_id == pet_id)
    if cursor:
        try:
            _cur = datetime.fromisoformat(cursor)
        except ValueError:
            raise HTTPException(status_code=400, detail="cursor inválido")
        q = q.filter(PetTimelineEvent.occurred_at < _cur)

    # Sem filtro de categoria: comportamento atual intacto (aditivo).
    if category is None:
        rows = q.order_by(PetTimelineEvent.occurred_at.desc()).limit(limit + 1).all()
        events = rows[:limit]
        next_cursor = events[-1].occurred_at.isoformat() if len(rows) > limit and events else None
        return {"events": [_event_dict(e) for e in events], "next_cursor": next_cursor}

    # Com categoria: pré-filtra por tipo elegível no SQL (mapa + diary + tenant_note),
    # depois refina tenant_note pela category do payload em Python. Pagina após o refino.
    eligible_types = {t for t, c in EVENT_TYPE_CATEGORY.items() if c == category}
    eligible_types |= _CATEGORY_ALWAYS_TYPES | {"tenant_note"}
    q = q.filter(PetTimelineEvent.event_type.in_(list(eligible_types)))
    candidates = q.order_by(PetTimelineEvent.occurred_at.desc()).all()
    filtered = [e for e in candidates if _category_matches(e, category)]
    events = filtered[:limit]
    next_cursor = events[-1].occurred_at.isoformat() if len(filtered) > limit and events else None
    return {"events": [_event_dict(e) for e in events], "next_cursor": next_cursor}


@router.post("/{pet_id}/timeline", status_code=201)
@api_router.post("/{pet_id}/timeline", status_code=201)
def add_event(pet_id: str, payload: TimelineEventCreate,
              user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    _require_active(db, user)
    _require_pet_evolution_plan(db, user, feature="pet_timeline", label="Timeline do pet")
    pet = _get_owned_pet(db, pet_id, user)

    # DIÁRIO (Fase B): título/payload são montados no serviço a partir dos campos
    # já sanitizados; o payload cru do cliente é ignorado para o diário.
    title = payload.title
    payload_json = payload.payload_json
    if payload.event_type == "diary":
        title, payload_json = svc.build_diary_entry(
            text=payload.diary_text or "", mood=payload.diary_mood, title=payload.title,
        )

    ev = svc.record_timeline_event(
        db, pet, event_type=payload.event_type, title=title, notes=payload.notes,
        occurred_at=payload.occurred_at, payload_json=payload_json,
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


@router.delete("/{pet_id}/timeline/{event_id}")
@api_router.delete("/{pet_id}/timeline/{event_id}")
def delete_event(pet_id: str, event_id: str,
                 user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    _require_active(db, user)
    _require_pet_evolution_plan(db, user, feature="pet_timeline", label="Timeline do pet")
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
# Stats de evolução do pet (Fase 5)
# GET /pets/{pet_id}/stats  e  /api/pets/{pet_id}/stats
# ---------------------------------------------------------------------------

from datetime import date  # noqa: E402 — import local para não poluir o topo com date+datetime


def _monday_of(d: date) -> date:
    """Segunda-feira da semana de `d`."""
    return d - timedelta(days=d.weekday())


def _pet_stats(pet_id: str, db: Session) -> dict:
    from app.models.pet_timeline_event import PetTimelineEvent as _PTE

    # 1. Série de pesos (últimos 100 eventos weight, asc)
    weight_events = (
        db.query(_PTE)
        .filter(_PTE.pet_id == pet_id, _PTE.event_type == "weight")
        .order_by(_PTE.occurred_at.asc())
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
    _require_pet_evolution_plan(db, user, feature="pet_stats", label="Gráficos e estatísticas do pet")
    _get_owned_pet(db, pet_id, user)
    return _pet_stats(pet_id, db)
