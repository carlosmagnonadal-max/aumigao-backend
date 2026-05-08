from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.models.tutor_profile import TutorProfile
from app.models.user import User
from app.models.walker_profile import WalkerProfile


def ensure_unique_identity(
    db: Session,
    *,
    email: str | None = None,
    cpf: str | None = None,
    phone: str | None = None,
    current_user_id: str | None = None,
):
    if email:
        existing_user = db.query(User).filter(User.email == email).first()
        if existing_user and existing_user.id != current_user_id:
            raise HTTPException(status_code=409, detail="Este e-mail já está cadastrado.")

    if cpf:
        tutor_query = db.query(TutorProfile).filter(TutorProfile.cpf == cpf)
        walker_query = db.query(WalkerProfile).filter(WalkerProfile.cpf == cpf)
        if current_user_id:
            tutor_query = tutor_query.filter(TutorProfile.user_id != current_user_id)
            walker_query = walker_query.filter(WalkerProfile.user_id != current_user_id)
        if tutor_query.first() or walker_query.first():
            raise HTTPException(status_code=409, detail="Este CPF já está cadastrado.")

    if phone:
        tutor_query = db.query(TutorProfile).filter(TutorProfile.phone == phone)
        walker_query = db.query(WalkerProfile).filter(WalkerProfile.phone == phone)
        if current_user_id:
            tutor_query = tutor_query.filter(TutorProfile.user_id != current_user_id)
            walker_query = walker_query.filter(WalkerProfile.user_id != current_user_id)
        if tutor_query.first() or walker_query.first():
            raise HTTPException(status_code=409, detail="Este telefone já está cadastrado.")
