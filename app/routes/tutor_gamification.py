"""Rota da gamificacao do TUTOR.

GET /tutors/me/gamification — computa nivel/XP/streak/badges do tutor logado a
partir dos seus passeios (sem tabela nova). Espelha a logica que hoje roda no
front (frontend/lib/api.ts -> getTutorGamification).
"""
from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.dependencies.auth import get_current_user
from app.models.user import User
from app.schemas.tutor_gamification import TutorGamification
from app.services import tutor_gamification_service as svc

router = APIRouter(prefix="/tutors", tags=["tutor-gamification"])
api_router = APIRouter(prefix="/api/tutors", tags=["tutor-gamification"])


@router.get("/me/gamification", response_model=TutorGamification)
@api_router.get("/me/gamification", response_model=TutorGamification)
def get_my_tutor_gamification(
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> TutorGamification:
    return svc.get_tutor_gamification(user, db)
