"""Rotas de verificação de passeador (Onda 2 — premium/verificados).

Verificação é GLOBAL (passeador é da plataforma): admin concede/remove o selo.
A exibição por tenant é gated pela flag `verified_walkers` (consumida no mobile).
"""
from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.dependencies.rbac import require_permission
from app.models.user import User
from app.schemas.walker_profile import WalkerProfileResponse
from app.services import verified_walker_service as svc
from app.services.audit_service import record_audit_log

admin_router = APIRouter(
    prefix="/admin/walkers",
    tags=["verified-walkers"],
    dependencies=[Depends(require_permission("admin.access"))],
)
api_admin_router = APIRouter(
    prefix="/api/admin/walkers",
    tags=["verified-walkers"],
    dependencies=[Depends(require_permission("admin.access"))],
)


def _set(walker_user_id: str, verified: bool, admin: User, db: Session) -> WalkerProfileResponse:
    profile = svc.set_verified(db, walker_user_id, verified, admin.id)
    record_audit_log(
        db,
        action="walker.verified" if verified else "walker.unverified",
        entity_type="walker_profile", entity_id=profile.id, actor=admin,
        after={"verified": verified, "walker_user_id": walker_user_id},
    )
    db.commit()
    return WalkerProfileResponse.model_validate(profile)


@admin_router.post("/{walker_user_id}/verify", response_model=WalkerProfileResponse)
@api_admin_router.post("/{walker_user_id}/verify", response_model=WalkerProfileResponse)
def verify_walker(walker_user_id: str, admin: User = Depends(require_permission("admin.access")), db: Session = Depends(get_db)):
    return _set(walker_user_id, True, admin, db)


@admin_router.post("/{walker_user_id}/unverify", response_model=WalkerProfileResponse)
@api_admin_router.post("/{walker_user_id}/unverify", response_model=WalkerProfileResponse)
def unverify_walker(walker_user_id: str, admin: User = Depends(require_permission("admin.access")), db: Session = Depends(get_db)):
    return _set(walker_user_id, False, admin, db)
