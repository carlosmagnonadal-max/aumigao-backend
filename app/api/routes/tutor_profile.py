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
from app.utils.registration_validation import normalize_cpf_or_raise, normalize_phone_or_raise


router = APIRouter(prefix="/tutor-profile", tags=["Tutor Profile"])


def _normalized_profile_payload(payload: TutorProfileCreate | TutorProfileUpdate):
    data = payload.model_dump(exclude_unset=True)
    try:
        if data.get("cpf"):
            data["cpf"] = normalize_cpf_or_raise(data.get("cpf"))
        if data.get("phone"):
            data["phone"] = normalize_phone_or_raise(data.get("phone"))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return data


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

    profile = TutorProfile(user_id=current_user.id, **_normalized_profile_payload(profile_data))

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

    update_data = _normalized_profile_payload(profile_update)

    for field, value in update_data.items():
        setattr(profile, field, value)

    db.commit()
    db.refresh(profile)

    return profile
