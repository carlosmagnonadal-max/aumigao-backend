from typing import Optional, List

from fastapi import APIRouter, Depends, HTTPException
from fastapi.security import OAuth2PasswordRequestForm
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.deps import get_current_user
from app.models.user import User
from app.models.tutor_profile import TutorProfile
from app.schemas.user import UserCreate, UserResponse
from app.core.security import hash_password, verify_password, create_access_token


router = APIRouter(prefix="/auth", tags=["Auth"])


class TutorRegisterRequest(BaseModel):
    email: str
    password: str

    full_name: Optional[str] = None
    phone: Optional[str] = None
    birth_date: Optional[str] = None
    cpf: Optional[str] = None

    cep: Optional[str] = None
    street: Optional[str] = None
    number: Optional[str] = None
    complement: Optional[str] = None
    neighborhood: Optional[str] = None
    city: Optional[str] = None
    state: Optional[str] = None
    reference_point: Optional[str] = None

    preferred_periods: Optional[List[str]] = None
    walk_frequency: Optional[str] = None
    preferred_walk_type: Optional[str] = None


@router.post("/register", response_model=UserResponse)
def register(user: UserCreate, db: Session = Depends(get_db)):
    existing_user = db.query(User).filter(User.email == user.email).first()

    if existing_user:
        raise HTTPException(status_code=400, detail="Email já cadastrado")

    new_user = User(
        email=user.email,
        password=hash_password(user.password)
    )

    db.add(new_user)
    db.commit()
    db.refresh(new_user)

    return new_user


@router.post("/register-tutor")
def register_tutor(data: TutorRegisterRequest, db: Session = Depends(get_db)):
    existing_user = db.query(User).filter(User.email == data.email).first()

    if existing_user:
        raise HTTPException(status_code=400, detail="Email já cadastrado")

    new_user = User(
        email=data.email,
        password=hash_password(data.password)
    )

    db.add(new_user)
    db.commit()
    db.refresh(new_user)

    profile = TutorProfile(
        full_name=data.full_name,
        phone=data.phone,
        cep=data.cep,
        street=data.street,
        number=data.number,
        complement=data.complement,
        neighborhood=data.neighborhood,
        city=data.city,
        state=data.state,
        reference_point=data.reference_point,
        user_id=new_user.id,
    )

    db.add(profile)
    db.commit()
    db.refresh(profile)

    token = create_access_token({"sub": new_user.email})

    return {
        "access_token": token,
        "token_type": "bearer",
        "user": {
            "id": new_user.id,
            "email": new_user.email,
        },
        "tutor_profile": {
            "id": profile.id,
            "full_name": profile.full_name,
            "phone": profile.phone,
            "city": profile.city,
            "state": profile.state,
        }
    }


@router.post("/login")
def login(
    form_data: OAuth2PasswordRequestForm = Depends(),
    db: Session = Depends(get_db)
):
    db_user = db.query(User).filter(User.email == form_data.username).first()

    if not db_user:
        raise HTTPException(status_code=400, detail="Usuário não encontrado")

    try:
        password_ok = verify_password(form_data.password, db_user.password)
    except Exception:
        raise HTTPException(
            status_code=400,
            detail="Senha antiga incompatível. Cadastre novamente."
        )

    if not password_ok:
        raise HTTPException(status_code=400, detail="Senha inválida")

    token = create_access_token({"sub": db_user.email})

    return {
        "access_token": token,
        "token_type": "bearer",
        "user": {
            "id": db_user.id,
            "email": db_user.email
        }
    }


@router.get("/me", response_model=UserResponse)
def get_me(current_user: User = Depends(get_current_user)):
    return current_user