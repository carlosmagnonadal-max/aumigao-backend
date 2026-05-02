from uuid import uuid4
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from app.core.database import get_db
from app.dependencies.auth import get_current_user
from app.models.pet import Pet
from app.models.user import User
from app.schemas.pet import PetCreate, PetResponse, PetUpdate

router = APIRouter(prefix="/pets", tags=["pets"])

@router.get("", response_model=list[PetResponse])
def list_pets(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    return db.query(Pet).filter(Pet.tutor_id == user.id).order_by(Pet.created_at.desc()).all()

@router.post("", response_model=PetResponse)
def create_pet(payload: PetCreate, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    pet = Pet(id=str(uuid4()), tutor_id=user.id, **payload.model_dump())
    db.add(pet)
    db.commit()
    db.refresh(pet)
    return pet

@router.get("/{pet_id}", response_model=PetResponse)
def get_pet(pet_id: str, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    pet = db.get(Pet, pet_id)
    if not pet or pet.tutor_id != user.id:
        raise HTTPException(status_code=404, detail="Pet nao encontrado")
    return pet

@router.put("/{pet_id}", response_model=PetResponse)
def update_pet(pet_id: str, payload: PetUpdate, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    pet = get_pet(pet_id, user, db)
    for key, value in payload.model_dump(exclude_unset=True).items():
        setattr(pet, key, value)
    db.commit()
    db.refresh(pet)
    return pet

@router.delete("/{pet_id}")
def delete_pet(pet_id: str, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    pet = get_pet(pet_id, user, db)
    db.delete(pet)
    db.commit()
    return {"ok": True}
