from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.dependencies.auth import get_current_user
from app.models.tutor_referral import TutorReferral
from app.models.user import User
from app.services import tutor_referrals as svc

router = APIRouter(prefix="/referrals/tutors", tags=["tutor-referrals"])
api_router = APIRouter(prefix="/api/referrals/tutors", tags=["tutor-referrals"])


class ValidateCodeRequest(BaseModel):
    code: str


class LinkUserRequest(BaseModel):
    code: str


def _referral_dict(ref: TutorReferral) -> dict:
    return {
        "id": ref.id,
        "referral_code": ref.referral_code,
        "invite_link": ref.invite_link,
        "status": ref.status,
        "reward_status": ref.reward_status,
        "completed_paid_walks_count": ref.completed_paid_walks_count,
    }


def _tenant_id_for(user: User) -> str:
    tid = getattr(user, "tenant_id", None)
    if not tid:
        raise HTTPException(status_code=400, detail="tutor sem tenant.")
    return tid


@router.post("")
@api_router.post("")
def create_referral(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    ref = svc.create_tutor_referral(db, user, _tenant_id_for(user))
    return _referral_dict(ref)


@router.get("/my")
@api_router.get("/my")
def my_referrals(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    rows = (
        db.query(TutorReferral)
        .filter(TutorReferral.referrer_user_id == user.id)
        .order_by(TutorReferral.created_at.desc())
        .all()
    )
    return {"items": [_referral_dict(r) for r in rows]}


@router.post("/validate-code")
@api_router.post("/validate-code")
def validate_code(payload: ValidateCodeRequest, db: Session = Depends(get_db)):
    return svc.validate_tutor_referral_code(db, payload.code)


@router.patch("/link-user")
@api_router.patch("/link-user")
def link_user(payload: LinkUserRequest, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    ref = svc.link_tutor_referral(db, payload.code, user.id, _tenant_id_for(user))
    return _referral_dict(ref)
