"""Rota de exposicao da Confianca do passeador (selos + certificacoes + nivel).

Compute-on-read via walker_trust_service. O passeador ve a propria Confianca
(sem gating — e o progresso dele). A exibicao para o TUTOR (matching/perfil
publico) e gated pela flag `verified_walkers` do tenant — follow-up.
"""
from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.dependencies.auth import get_current_user
from app.models.user import User
from app.schemas.walker_trust import WalkerTrustResponse
from app.services import walker_trust_service as svc

router = APIRouter(prefix="/walker", tags=["walker-trust"])
api_router = APIRouter(prefix="/api/walker", tags=["walker-trust"])


@router.get("/me/trust", response_model=WalkerTrustResponse)
@api_router.get("/me/trust", response_model=WalkerTrustResponse)
def get_my_trust(
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> WalkerTrustResponse:
    return WalkerTrustResponse.model_validate(svc.compute_walker_trust(db, user.id))
