from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.deps import get_current_user
from app.models.user import User
from app.models.tutor_profile import TutorProfile
from app.schemas.tutor_profile import (
    TutorProfileCreate,
    TutorProfileUpdate,
    TutorProfileResponse,
)


router = APIRouter(prefix="/tutor-profile", tags=["Tutor Profile"])


@router.post("/", response_model=TutorProfileResponse)
def create_tutor_profile(
    profile_data: TutorProfileCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    existing_profile = db.query(TutorProfile).filter(
        TutorProfile.user_id == current_user.id
    ).first()

    if existing_profile:
        raise HTTPException(
            status_code=400,
            detail="Perfil do tutor já existe"
        )

    profile = TutorProfile(
        full_name=profile_data.full_name,
        phone=profile_data.phone,
        profile_photo_url=profile_data.profile_photo_url,
        cep=profile_data.cep,
        street=profile_data.street,
        number=profile_data.number,
        complement=profile_data.complement,
        neighborhood=profile_data.neighborhood,
        city=profile_data.city,
        state=profile_data.state,
        reference_point=profile_data.reference_point,
        access_instructions=profile_data.access_instructions,
        pet_pickup_notes=profile_data.pet_pickup_notes,
        preferred_pickup_method=profile_data.preferred_pickup_method,
        user_id=current_user.id,
    )

    db.add(profile)
    db.commit()
    db.refresh(profile)

    return profile


@router.get("/me", response_model=TutorProfileResponse)
def get_my_tutor_profile(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    profile = db.query(TutorProfile).filter(
        TutorProfile.user_id == current_user.id
    ).first()

    if not profile:
        raise HTTPException(
            status_code=404,
            detail="Perfil do tutor ainda não foi criado"
        )

    return profile


@router.put("/me", response_model=TutorProfileResponse)
def update_my_tutor_profile(
    profile_update: TutorProfileUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    profile = db.query(TutorProfile).filter(
        TutorProfile.user_id == current_user.id
    ).first()

    if not profile:
        profile = TutorProfile(user_id=current_user.id)
        db.add(profile)
        db.commit()
        db.refresh(profile)

    update_data = profile_update.model_dump(exclude_unset=True)

    for field, value in update_data.items():
        setattr(profile, field, value)

    db.commit()
    db.refresh(profile)

    return profile