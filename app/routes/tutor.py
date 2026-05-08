from uuid import uuid4
from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session
from app.core.database import get_db
from app.dependencies.auth import get_current_user
from app.models.tutor_profile import TutorProfile
from app.models.user import User
from app.schemas.tutor_profile import TutorProfileCreate, TutorProfileResponse, TutorProfileUpdate
from app.utils.registration_validation import normalize_cpf_or_raise, normalize_phone_or_raise

router = APIRouter(prefix="/tutor", tags=["tutor"])


def _normalized_profile_payload(payload: TutorProfileCreate | TutorProfileUpdate):
    data = payload.model_dump()
    try:
        if "cpf" in data and data.get("cpf"):
            data["cpf"] = normalize_cpf_or_raise(data.get("cpf"))
        if "phone" in data and data.get("phone"):
            data["phone"] = normalize_phone_or_raise(data.get("phone"))
    except ValueError as exc:
        from fastapi import HTTPException
        raise HTTPException(status_code=400, detail=str(exc))
    return data

@router.get("/profile", response_model=TutorProfileResponse | None)
def get_profile(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    return db.query(TutorProfile).filter(TutorProfile.user_id == user.id).first()

@router.post("/profile", response_model=TutorProfileResponse)
def create_profile(payload: TutorProfileCreate, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    profile = db.query(TutorProfile).filter(TutorProfile.user_id == user.id).first()
    if profile:
        return update_profile(payload, user, db)
    profile = TutorProfile(id=str(uuid4()), user_id=user.id, **_normalized_profile_payload(payload))
    db.add(profile)
    db.commit()
    db.refresh(profile)
    return profile

@router.put("/profile", response_model=TutorProfileResponse)
def update_profile(payload: TutorProfileUpdate, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    profile = db.query(TutorProfile).filter(TutorProfile.user_id == user.id).first()
    if not profile:
        profile = TutorProfile(id=str(uuid4()), user_id=user.id)
        db.add(profile)
    for key, value in _normalized_profile_payload(payload).items():
        setattr(profile, key, value)
    db.commit()
    db.refresh(profile)
    return profile
