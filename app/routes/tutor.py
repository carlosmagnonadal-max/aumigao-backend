from uuid import uuid4
from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session
from app.core.database import get_db
from app.dependencies.auth import get_current_user
from app.models.tutor_profile import TutorProfile
from app.models.user import User
from app.schemas.tutor_profile import TutorProfileCreate, TutorProfileResponse, TutorProfileUpdate
from app.services.identity_uniqueness import ensure_unique_identity
from app.services.tenant_seed_service import default_tenant_id
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
    data = _normalized_profile_payload(payload)
    ensure_unique_identity(db, cpf=data.get("cpf") or None, phone=data.get("phone") or None, current_user_id=user.id)
    tenant_id = user.tenant_id or default_tenant_id(db)
    user.tenant_id = user.tenant_id or tenant_id
    profile = TutorProfile(id=str(uuid4()), user_id=user.id, tenant_id=tenant_id, **data)
    db.add(profile)
    db.commit()
    db.refresh(profile)
    return profile

@router.put("/profile", response_model=TutorProfileResponse)
def update_profile(payload: TutorProfileUpdate, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    profile = db.query(TutorProfile).filter(TutorProfile.user_id == user.id).first()
    if not profile:
        tenant_id = user.tenant_id or default_tenant_id(db)
        user.tenant_id = user.tenant_id or tenant_id
        profile = TutorProfile(id=str(uuid4()), user_id=user.id, tenant_id=tenant_id)
        db.add(profile)
    elif not profile.tenant_id:
        profile.tenant_id = user.tenant_id or default_tenant_id(db)
    data = _normalized_profile_payload(payload)
    ensure_unique_identity(db, cpf=data.get("cpf") or None, phone=data.get("phone") or None, current_user_id=user.id)
    for key, value in data.items():
        setattr(profile, key, value)
    db.commit()
    db.refresh(profile)
    return profile
