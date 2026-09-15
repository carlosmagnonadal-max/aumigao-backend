"""Capacitação do passeador (S2) — rotas do passeador e do admin.

Passeador: GET /walker/training · GET /walker/training/modules/{id} ·
POST /walker/training/modules/{id}/quiz · GET /walker/training/quick-guide ·
GET /walker/training/local-rules (M10-B). Admin: GET /admin/walkers/{user_id}/training.
Passeadores são rede GLOBAL (walker_profiles sem tenant_id) — tabelas sem RLS.
Exige papel walker + WalkerProfile, mas NÃO exige passeador ativo (DV11: dá para estudar antes).
"""
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.dependencies.auth import get_current_user
from app.dependencies.rbac import require_permission
from app.dependencies.tenant_scope import get_admin_tenant_scope
from app.models.user import User
from app.models.walker_profile import WalkerProfile
from app.services import walker_training_service as svc

router = APIRouter(prefix="/walker/training", tags=["walker-training"])
api_router = APIRouter(prefix="/api/walker/training", tags=["walker-training"])
admin_router = APIRouter(prefix="/admin/walkers", tags=["admin-walker-training"])
api_admin_router = APIRouter(prefix="/api/admin/walkers", tags=["admin-walker-training"])

WALKER_ROLES = {"walker", "passeador"}


class QuizSubmission(BaseModel):
    answers: list[int] = Field(..., min_length=1, max_length=50)
    # C1: se enviado e != versão ativa, a correção recusa com 409 training_version_changed
    # (conteúdo mudou entre o passeador abrir o módulo e enviar o quiz).
    version: str | None = None


def _require_walker(user: User, db: Session) -> WalkerProfile:
    profile = db.query(WalkerProfile).filter(WalkerProfile.user_id == user.id).first()
    if user.role not in WALKER_ROLES or profile is None:
        raise HTTPException(status_code=403, detail="Capacitação disponível apenas para passeadores.")
    return profile


@router.get("")
@api_router.get("")
def get_training(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    _require_walker(user, db)
    return svc.training_summary(db, user.id)


@router.get("/modules/{module_id}")
@api_router.get("/modules/{module_id}")
def get_training_module(module_id: str, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    _require_walker(user, db)
    return svc.module_detail(db, user.id, module_id)


@router.post("/modules/{module_id}/quiz")
@api_router.post("/modules/{module_id}/quiz")
def submit_training_quiz(
    module_id: str,
    payload: QuizSubmission,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    _require_walker(user, db)
    return svc.grade_quiz(db, user.id, module_id, payload.answers, payload.version)


@router.get("/quick-guide")
@api_router.get("/quick-guide")
def get_training_quick_guide(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    _require_walker(user, db)
    return svc.quick_guide()


@router.get("/local-rules")
@api_router.get("/local-rules")
def get_training_local_rules(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    _require_walker(user, db)
    return svc.local_rules(db, user.id)


@admin_router.get("/{walker_user_id}/training")
@api_admin_router.get("/{walker_user_id}/training")
def admin_walker_training(
    walker_user_id: str,
    admin: User = Depends(require_permission("walkers.read")),
    db: Session = Depends(get_db),
):
    # Mesmo escopo do detalhe de candidatura (admin.py partner_application_detail):
    # admin de tenant só vê passeador cujo user.tenant_id é o seu.
    scope = get_admin_tenant_scope(admin, db)
    if not scope.is_global:
        walker_user = db.get(User, walker_user_id)
        if not walker_user or walker_user.tenant_id != scope.tenant_id:
            raise HTTPException(status_code=404, detail="Passeador não encontrado.")
    return svc.admin_training_detail(db, walker_user_id)
