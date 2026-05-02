from uuid import uuid4
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from app.core.database import get_db
from app.core.security import create_access_token, get_password_hash, verify_password
from app.dependencies.auth import get_current_user
from app.models.user import User
from app.schemas.auth import LoginRequest, TokenResponse
from app.schemas.user import UserCreate, UserResponse

router = APIRouter(prefix="/auth", tags=["auth"])

def build_session(user: User) -> TokenResponse:
    token = create_access_token(user.id, {"role": user.role})
    return TokenResponse(access_token=token, refresh_token=token, user=UserResponse.model_validate(user))

@router.post("/register", response_model=TokenResponse)
def register(payload: UserCreate, db: Session = Depends(get_db)):
    if db.query(User).filter(User.email == payload.email).first():
        raise HTTPException(status_code=409, detail="E-mail ja cadastrado")
    role = "tutor" if payload.role in {"cliente", "tutor"} else payload.role
    user = User(id=str(uuid4()), email=payload.email, full_name=payload.full_name, role=role, password_hash=get_password_hash(payload.password))
    db.add(user)
    db.commit()
    db.refresh(user)
    return build_session(user)

@router.post("/login", response_model=TokenResponse)
def login(payload: LoginRequest, db: Session = Depends(get_db)):
    user = db.query(User).filter(User.email == payload.email).first()
    if not user or not verify_password(payload.password, user.password_hash):
        raise HTTPException(status_code=401, detail="Credenciais invalidas")
    return build_session(user)

@router.get("/me", response_model=UserResponse)
def me(user: User = Depends(get_current_user)):
    return user
