"""Rastreamento GPS ao vivo de passeios.

POST /walks/{walk_id}/locations  — passeador envia lote de pings GPS.
GET  /walks/{walk_id}/locations  — tutor/walker/admin consulta trajeto (polling incremental).
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import List
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field, field_validator
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.dependencies.auth import get_current_user
from app.dependencies.tenant_scope import get_admin_tenant_scope
from app.models.user import User
from app.models.walk import Walk
from app.models.walk_location_ping import WalkLocationPing
from app.services.operational_matching_service import WALKER_ARRIVING, RIDE_IN_PROGRESS

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/walks", tags=["walk-locations"])
api_router = APIRouter(prefix="/api/walks", tags=["walk-locations"])

# Operational statuses que indicam passeio em execução ativa (indo buscar + passeando).
ACTIVE_WALK_STATUSES = {WALKER_ARRIVING, RIDE_IN_PROGRESS}

# Limite de pings antes de parar de aceitar novos (proteção de volume).
MAX_PINGS_PER_WALK = 5_000


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------

class PingIn(BaseModel):
    latitude: float = Field(..., ge=-90.0, le=90.0)
    longitude: float = Field(..., ge=-180.0, le=180.0)
    accuracy: float | None = None
    recorded_at: datetime

    @field_validator("recorded_at", mode="before")
    @classmethod
    def _ensure_aware(cls, v):
        if isinstance(v, datetime) and v.tzinfo is None:
            # trata como UTC se sem timezone
            return v.replace(tzinfo=timezone.utc)
        return v


class PingBatchIn(BaseModel):
    pings: List[PingIn] = Field(..., min_length=1, max_length=30)


class PingOut(BaseModel):
    latitude: float
    longitude: float
    accuracy: float | None
    recorded_at: datetime

    model_config = {"from_attributes": True}


# ---------------------------------------------------------------------------
# Helpers privados
# ---------------------------------------------------------------------------

def _get_walk_or_404(walk_id: str, db: Session) -> Walk:
    walk = db.get(Walk, walk_id)
    if not walk:
        raise HTTPException(status_code=404, detail="Passeio nao encontrado")
    return walk


def _assert_active(walk: Walk) -> None:
    if walk.operational_status not in ACTIVE_WALK_STATUSES:
        raise HTTPException(
            status_code=409,
            detail=(
                f"Rastreamento disponivel apenas durante passeio ativo "
                f"(status atual: {walk.operational_status!r}). "
                f"Status validos: {sorted(ACTIVE_WALK_STATUSES)}"
            ),
        )


def _is_admin_or_super(user: User) -> bool:
    return getattr(user, "role", "") in {"admin", "super_admin"}


# ---------------------------------------------------------------------------
# POST /walks/{walk_id}/locations
# ---------------------------------------------------------------------------

def _post_locations(walk_id: str, body: PingBatchIn, user: User, db: Session):
    walk = _get_walk_or_404(walk_id, db)

    # Apenas o walker atribuído ao passeio pode enviar pings.
    if walk.walker_id != user.id:
        raise HTTPException(status_code=403, detail="Apenas o passeador atribuido pode enviar localizacao")

    _assert_active(walk)

    # Proteção de volume: evita acúmulo ilimitado sem retornar erro para o app.
    existing_count = (
        db.query(WalkLocationPing)
        .filter(WalkLocationPing.walk_id == walk_id)
        .count()
    )
    if existing_count >= MAX_PINGS_PER_WALK:
        return {"saved": 0, "limit_reached": True}

    now = datetime.utcnow()
    pings = [
        WalkLocationPing(
            id=str(uuid4()),
            walk_id=walk_id,
            walker_id=user.id,
            latitude=ping.latitude,
            longitude=ping.longitude,
            accuracy=ping.accuracy,
            recorded_at=ping.recorded_at.replace(tzinfo=None) if ping.recorded_at.tzinfo else ping.recorded_at,
            created_at=now,
        )
        for ping in body.pings
    ]
    db.add_all(pings)
    db.commit()

    logger.info(
        "walk_location_pings_saved walk_id=%s walker_id=%s count=%d",
        walk_id,
        user.id,
        len(pings),
    )
    return {"saved": len(pings)}


@router.post("/{walk_id}/locations")
def post_walk_locations(
    walk_id: str,
    body: PingBatchIn,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    return _post_locations(walk_id, body, user, db)


@api_router.post("/{walk_id}/locations")
def api_post_walk_locations(
    walk_id: str,
    body: PingBatchIn,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    return _post_locations(walk_id, body, user, db)


# ---------------------------------------------------------------------------
# GET /walks/{walk_id}/locations
# ---------------------------------------------------------------------------

def _get_locations(
    walk_id: str,
    user: User,
    db: Session,
    since: datetime | None,
    limit: int,
):
    walk = _get_walk_or_404(walk_id, db)

    # Controle de acesso: tutor dono, walker atribuído ou admin/super_admin.
    if walk.tutor_id == user.id:
        pass  # tutor: acesso liberado
    elif walk.walker_id == user.id:
        pass  # walker atribuído: acesso liberado
    elif _is_admin_or_super(user):
        # Admin/super_admin: valida escopo de tenant (não bloqueia super_admin global).
        get_admin_tenant_scope(user)  # lança 403 se não for admin/super_admin
    else:
        raise HTTPException(status_code=403, detail="Sem permissao para visualizar localizacao deste passeio")

    query = (
        db.query(WalkLocationPing)
        .filter(WalkLocationPing.walk_id == walk_id)
    )

    if since is not None:
        # Polling incremental: retorna apenas pings após `since`.
        since_naive = since.replace(tzinfo=None) if since.tzinfo else since
        query = query.filter(WalkLocationPing.recorded_at > since_naive)

    pings = (
        query
        .order_by(WalkLocationPing.recorded_at.asc())
        .limit(limit)
        .all()
    )

    return {
        "walk_id": walk_id,
        "walk_status": walk.operational_status,
        "pings": [
            {
                "latitude": p.latitude,
                "longitude": p.longitude,
                "accuracy": p.accuracy,
                "recorded_at": p.recorded_at,
            }
            for p in pings
        ],
        "count": len(pings),
    }


@router.get("/{walk_id}/locations")
def get_walk_locations(
    walk_id: str,
    since: datetime | None = Query(default=None),
    limit: int = Query(default=500, ge=1, le=2000),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    return _get_locations(walk_id, user, db, since, limit)


@api_router.get("/{walk_id}/locations")
def api_get_walk_locations(
    walk_id: str,
    since: datetime | None = Query(default=None),
    limit: int = Query(default=500, ge=1, le=2000),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    return _get_locations(walk_id, user, db, since, limit)
