"""Passeador Verificado (Onda 2 — premium/verificados).

O passeador é da PLATAFORMA, então a verificação é GLOBAL (admin concede o selo
uma vez). A feature flag por tenant `verified_walkers` controla apenas a EXIBIÇÃO
do selo aos tutores daquele tenant (ver mobile). Ver propriedade-tutor-walker-dois-apps.
"""
from datetime import datetime

from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.models.tenant import Tenant
from app.models.walker_profile import WalkerProfile
from app.services.tenant_plan_service import tenant_has_feature

VERIFIED_WALKERS_FEATURE_KEY = "verified_walkers"


def verified_walkers_enabled(tenant: Tenant, db: Session) -> bool:
    """O tenant exibe selos de verificado? (controla só a exibição, não a concessão)."""
    return tenant_has_feature(tenant, db, VERIFIED_WALKERS_FEATURE_KEY)


def _walker_or_404(db: Session, walker_user_id: str) -> WalkerProfile:
    profile = db.query(WalkerProfile).filter(WalkerProfile.user_id == walker_user_id).first()
    if not profile:
        raise HTTPException(status_code=404, detail="Passeador não encontrado.")
    return profile


def set_verified(db: Session, walker_user_id: str, verified: bool, admin_id: str | None) -> WalkerProfile:
    profile = _walker_or_404(db, walker_user_id)
    profile.verified = verified
    profile.verified_at = datetime.utcnow() if verified else None
    profile.verified_by_admin_id = admin_id if verified else None
    profile.updated_at = datetime.utcnow()
    db.add(profile)
    db.commit()
    db.refresh(profile)
    return profile
