"""Compartilhamento público de passeio ao vivo (growth loop cunha 1).

POST /walks/{walk_id}/share-link  — (auth) tutor dono gera/recupera o token.
GET  /public/live/{token}         — (sem auth) payload sanitizado do passeio ao vivo.
"""
from __future__ import annotations

import logging
import secrets
from datetime import datetime, timedelta
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.tenant_session import global_scope_session
from app.dependencies.auth import get_current_user
from app.models.pet import Pet
from app.models.tenant import Tenant
from app.models.user import User
from app.models.walk import Walk
from app.models.walk_location_ping import WalkLocationPing
from app.models.walk_share_link import WalkShareLink
from app.services.live_share_service import (
    compute_share_expiry,
    is_live_share_enabled,
    obfuscate_origin,
    pet_first_name,
)
from app.services.operational_matching_service import RIDE_IN_PROGRESS, WALKER_ARRIVING
from app.services.tenant_plan_service import tenant_feature_enabled

logger = logging.getLogger(__name__)

router = APIRouter(tags=["live-share"])
api_router = APIRouter(prefix="/api", tags=["live-share"])

PUBLIC_BASE = "https://app.aumigaowalk.com.br"
ACTIVE_WALK_STATUSES = {WALKER_ARRIVING, "pet_handover_confirmed", RIDE_IN_PROGRESS}


def _create_share_link(walk_id: str, user: User, db: Session) -> dict:
    if not is_live_share_enabled():
        raise HTTPException(status_code=404, detail="Recurso indisponivel")

    walk = db.get(Walk, walk_id)
    if not walk:
        raise HTTPException(status_code=404, detail="Passeio nao encontrado")
    if walk.tutor_id != user.id:
        raise HTTPException(status_code=403, detail="Apenas o tutor dono do passeio pode compartilhar")

    tenant_id = walk.tenant_id or user.tenant_id
    if tenant_id:
        tenant = db.get(Tenant, tenant_id)
        if tenant and not tenant_feature_enabled(tenant, db, "live_gps"):
            raise HTTPException(status_code=404, detail="Recurso indisponivel")

    now = datetime.utcnow()
    existing = (
        db.query(WalkShareLink)
        .filter(
            WalkShareLink.walk_id == walk_id,
            WalkShareLink.revoked_at.is_(None),
            WalkShareLink.expires_at > now,
        )
        .order_by(WalkShareLink.created_at.desc())
        .first()
    )
    if existing:
        link = existing
    else:
        # compute_share_expiry bases expiry on scheduled_date.  For walks already
        # in progress whose scheduled_date is in the past, the computed expiry may
        # also be in the past.  Clamp to at least now + grace (120 min) so the
        # link is always valid for at least the grace window from creation time.
        _grace = 120
        from app.lib.walk_time import tenant_tz_name

        computed_expiry = compute_share_expiry(walk, tz_name=tenant_tz_name(db, walk.tenant_id))
        expires_at = computed_expiry if computed_expiry > now else now + timedelta(minutes=_grace)
        link = WalkShareLink(
            id=str(uuid4()),
            token=secrets.token_urlsafe(32),
            walk_id=walk_id,
            tenant_id=walk.tenant_id,
            created_by=user.id,
            expires_at=expires_at,
            revoked_at=None,
            created_at=now,
        )
        db.add(link)
        db.commit()

    return {"token": link.token, "url": f"{PUBLIC_BASE}/live/{link.token}"}


def _public_live(token: str) -> dict:
    with global_scope_session() as db:
        link = db.query(WalkShareLink).filter(WalkShareLink.token == token).first()
        if not link:
            raise HTTPException(status_code=404, detail="Link nao encontrado")

        now = datetime.utcnow()
        if link.revoked_at is not None or link.expires_at <= now:
            raise HTTPException(status_code=410, detail="Passeio encerrado")

        walk = db.get(Walk, link.walk_id)
        if not walk:
            raise HTTPException(status_code=410, detail="Passeio encerrado")
        if walk.operational_status not in ACTIVE_WALK_STATUSES:
            raise HTTPException(status_code=410, detail="Passeio encerrado")

        pet = db.get(Pet, walk.pet_id) if walk.pet_id else None
        tenant = db.get(Tenant, walk.tenant_id) if walk.tenant_id else None

        raw_pings = (
            db.query(WalkLocationPing)
            .filter(WalkLocationPing.walk_id == link.walk_id)
            .order_by(WalkLocationPing.recorded_at.asc())
            .limit(2000)
            .all()
        )
        pings = [
            {"latitude": p.latitude, "longitude": p.longitude, "recorded_at": p.recorded_at.isoformat()}
            for p in raw_pings
        ]
        safe_pings = obfuscate_origin(pings)

        return {
            "status": "active",
            "pet_first_name": pet_first_name(pet.name if pet else ""),
            "pet_photo_url": getattr(pet, "photo_url", None) if pet else None,
            "tenant": {
                "name": getattr(tenant, "name", None) if tenant else None,
                "slug": getattr(tenant, "slug", None) if tenant else None,
                "logo_url": getattr(tenant, "logo_url", None) if tenant else None,
            },
            "started_at": getattr(walk, "scheduled_date", None),
            "pings": safe_pings,
            "count": len(safe_pings),
        }


@router.post("/walks/{walk_id}/share-link")
def create_share_link(walk_id: str, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    return _create_share_link(walk_id, user, db)


@api_router.post("/walks/{walk_id}/share-link")
def api_create_share_link(walk_id: str, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    return _create_share_link(walk_id, user, db)


@router.get("/public/live/{token}")
def public_live(token: str):
    return _public_live(token)


@api_router.get("/public/live/{token}")
def api_public_live(token: str):
    return _public_live(token)
