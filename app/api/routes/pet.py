import os
import uuid
from typing import List

from fastapi import APIRouter, Depends, HTTPException, UploadFile, File
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.deps import get_current_user
from app.models.user import User
from app.models.pet import Pet
from app.schemas.pet import PetCreate, PetUpdate, PetResponse


router = APIRouter(prefix="/pets", tags=["Pets"])


UPLOAD_DIR = "uploads/pets"
os.makedirs(UPLOAD_DIR, exist_ok=True)


@router.post("/upload-photo")
def upload_pet_photo(
    file: UploadFile = File(...),
    current_user: User = Depends(get_current_user)
):
    allowed_types = ["image/jpeg", "image/png", "image/webp"]

    if file.content_type not in allowed_types:
        raise HTTPException(
            status_code=400,
            detail="Formato inválido. Envie JPG, PNG ou WEBP."
        )

    file_extension = file.filename.split(".")[-1]
    unique_filename = f"{uuid.uuid4()}.{file_extension}"
    file_path = os.path.join(UPLOAD_DIR, unique_filename)

    with open(file_path, "wb") as buffer:
        buffer.write(file.file.read())

    return {
        "photo_url": f"/uploads/pets/{unique_filename}"
    }


@router.post("/", response_model=PetResponse)
def create_pet(
    pet: PetCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    new_pet = Pet(
        name=pet.name,
        photo_url=pet.photo_url,
        breed=pet.breed,
        size=pet.size,
        age=pet.age,
        castrated=pet.castrated,
        behavior=pet.behavior,
        allergies=pet.allergies,
        health_notes=pet.health_notes,
        general_notes=pet.general_notes,
        owner_id=current_user.id,
    )

    db.add(new_pet)
    db.commit()
    db.refresh(new_pet)

    return new_pet


@router.get("/", response_model=List[PetResponse])
def list_my_pets(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    return db.query(Pet).filter(Pet.owner_id == current_user.id).all()


@router.get("/{pet_id}", response_model=PetResponse)
def get_pet(
    pet_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    pet = db.query(Pet).filter(
        Pet.id == pet_id,
        Pet.owner_id == current_user.id
    ).first()

    if not pet:
        raise HTTPException(status_code=404, detail="Pet não encontrado")

    return pet


@router.put("/{pet_id}", response_model=PetResponse)
def update_pet(
    pet_id: int,
    pet_update: PetUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    pet = db.query(Pet).filter(
        Pet.id == pet_id,
        Pet.owner_id == current_user.id
    ).first()

    if not pet:
        raise HTTPException(status_code=404, detail="Pet não encontrado")

    update_data = pet_update.model_dump(exclude_unset=True)

    for field, value in update_data.items():
        setattr(pet, field, value)

    db.commit()
    db.refresh(pet)

    return pet


@router.delete("/{pet_id}")
def delete_pet(
    pet_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    pet = db.query(Pet).filter(
        Pet.id == pet_id,
        Pet.owner_id == current_user.id
    ).first()

    if not pet:
        raise HTTPException(status_code=404, detail="Pet não encontrado")

    db.delete(pet)
    db.commit()

    return {"message": "Pet excluído com sucesso"}