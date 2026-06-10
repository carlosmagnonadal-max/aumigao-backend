"""Rota da rotina/evolucao do pet (compute-based, sem tabela).

Substitui o mock getPetRoutine do front. Calcula tudo a partir dos passeios
concluidos do pet (ver pet_routine_service). Apenas o tutor dono do pet acessa.
"""
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.dependencies.auth import get_current_user
from app.models.pet import Pet
from app.models.user import User
from app.schemas.pet_routine import PetRoutineView
from app.services import pet_routine_service as svc

router = APIRouter(prefix="/pets", tags=["pet-routine"])
api_router = APIRouter(prefix="/api/pets", tags=["pet-routine"])


@router.get("/{pet_id}/routine", response_model=PetRoutineView)
@api_router.get("/{pet_id}/routine", response_model=PetRoutineView)
def get_pet_routine(
    pet_id: str,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    pet = db.get(Pet, pet_id)
    if not pet:
        raise HTTPException(status_code=404, detail="Pet nao encontrado")
    if str(pet.tutor_id) != str(user.id):
        raise HTTPException(status_code=403, detail="Apenas o tutor dono do pet pode ver a rotina.")
    return svc.build_pet_routine(pet, db)
