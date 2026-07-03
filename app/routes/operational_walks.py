from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.dependencies.auth import get_current_user, require_admin
from app.dependencies.rbac import require_permission
from app.dependencies.tenant_scope import apply_tenant_filter, get_admin_tenant_scope
from app.models.user import User
from app.models.walk import Walk, WalkOperationalLog
from app.services.operational_matching_service import (
    accept_walk,
    decline_walk,
    operational_metrics,
    process_expired_attempts,
    rematch,
    serialize_log,
    serialize_operational_walk,
    start_matching,
)
from app.services.admin_operational_event_service import record_admin_operational_event

router = APIRouter(prefix="/walks", tags=["walk-operational"])
api_router = APIRouter(prefix="/api/walks", tags=["walk-operational"])
admin_router = APIRouter(prefix="/admin/walks", tags=["admin-walk-operational"], dependencies=[Depends(require_permission("walks.read"))])
api_admin_router = APIRouter(prefix="/api/admin/walks", tags=["admin-walk-operational"], dependencies=[Depends(require_permission("walks.read"))])


def _get_walk(walk_id: str, db: Session) -> Walk:
    walk = db.get(Walk, walk_id)
    if not walk:
        raise HTTPException(status_code=404, detail="Passeio nao encontrado")
    return walk


def _can_manage_matching(walk: Walk, user: User) -> bool:
    return user.role in {"admin", "super_admin"} or walk.tutor_id == user.id


@router.post("/{walk_id}/matching/start")
@api_router.post("/{walk_id}/matching/start")
def start_walk_matching(walk_id: str, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    walk = _get_walk(walk_id, db)
    if not _can_manage_matching(walk, user):
        raise HTTPException(status_code=403, detail="Sem permissao")
    start_matching(walk, db, actor=user)
    db.commit()
    db.refresh(walk)
    return serialize_operational_walk(walk, db, user=user)


@router.post("/{walk_id}/accept")
@api_router.post("/{walk_id}/accept")
def accept_walk_request(walk_id: str, request: Request, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    from app.dependencies.legal_gate import enforce_legal_acceptance
    enforce_legal_acceptance(request, user, db)
    if user.role != "walker":
        raise HTTPException(status_code=403, detail="Apenas passeadores podem aceitar.")
    # with_for_update() garante exclusao mutua em Postgres (no-op em SQLite nos testes).
    walk = db.query(Walk).filter(Walk.id == walk_id).with_for_update().first()
    if not walk:
        raise HTTPException(status_code=404, detail="Passeio nao encontrado")
    # Re-valida disponibilidade apos obter o lock: rejeita apenas se outro passeador
    # ja aceitou. O servico de matching ainda aplica sua propria verificacao atomica.
    if walk.walker_id is not None and walk.walker_id != user.id:
        raise HTTPException(status_code=409, detail="Este passeio ja foi aceito por outro passeador.")
    accept_walk(walk, user, db)
    db.commit()
    db.refresh(walk)
    return {"ok": True, "walk_id": walk.id, "walk": serialize_operational_walk(walk, db, user=user)}


@router.post("/{walk_id}/decline")
@api_router.post("/{walk_id}/decline")
def decline_walk_request(walk_id: str, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    if user.role != "walker":
        raise HTTPException(status_code=403, detail="Apenas passeadores podem recusar.")
    walk = _get_walk(walk_id, db)
    decline_walk(walk, user, db)
    db.commit()
    db.refresh(walk)
    return {"ok": True, "walk_id": walk.id, "walk": serialize_operational_walk(walk, db, user=user)}


@router.post("/{walk_id}/rematch")
@api_router.post("/{walk_id}/rematch")
def rematch_walk_request(walk_id: str, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    walk = _get_walk(walk_id, db)
    if not _can_manage_matching(walk, user):
        raise HTTPException(status_code=403, detail="Sem permissao")
    rematch(walk, db, reason="manual")
    if user.role in {"admin", "super_admin"}:
        record_admin_operational_event(
            db,
            event_type="rematch_started",
            entity_type="walk",
            entity_id=walk.id,
            severity="high",
            title="Rematch manual iniciado",
            description="Rematch manual iniciado pela operacao administrativa.",
            actor=user,
            source="admin.walk.rematch",
            metadata={"reason": "manual"},
        )
    db.commit()
    db.refresh(walk)
    return serialize_operational_walk(walk, db, user=user)


@router.get("/{walk_id}/operational-status")
@api_router.get("/{walk_id}/operational-status")
def get_operational_status(walk_id: str, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    process_expired_attempts(db)
    walk = _get_walk(walk_id, db)
    if user.role not in {"admin", "super_admin"} and walk.tutor_id != user.id and walk.walker_id != user.id and walk.assigned_walker_id != user.id:
        raise HTTPException(status_code=403, detail="Sem permissao")
    return serialize_operational_walk(walk, db, user=user)


@admin_router.get("/operational-metrics")
@api_admin_router.get("/operational-metrics")
def admin_operational_metrics(
    admin: User = Depends(require_permission("walks.read")),
    db: Session = Depends(get_db),
):
    # operational_metrics retorna dados agregados — sem tenant filter (e por design global).
    # A permissao walks.read (no router) + autenticacao e suficiente.
    process_expired_attempts(db)
    return operational_metrics(db)


@admin_router.get("/{walk_id}/operational-logs")
@api_admin_router.get("/{walk_id}/operational-logs")
def admin_operational_logs(
    walk_id: str,
    admin: User = Depends(require_permission("walks.read")),
    db: Session = Depends(get_db),
):
    process_expired_attempts(db)
    walk = _get_walk(walk_id, db)
    # Isolamento: admin de tenant so ve logs de walks do seu tenant.
    scope = get_admin_tenant_scope(admin, db)
    from app.dependencies.tenant_scope import ensure_tenant_access
    ensure_tenant_access(walk.tenant_id, scope)
    logs = (
        db.query(WalkOperationalLog)
        .filter(WalkOperationalLog.walk_id == walk_id)
        .order_by(WalkOperationalLog.created_at.asc())
        .all()
    )
    return {"items": [serialize_log(item) for item in logs], "total": len(logs)}
