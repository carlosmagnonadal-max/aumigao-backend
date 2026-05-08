from uuid import uuid4
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from app.core.database import get_db
from app.core.security import create_access_token, get_password_hash, verify_password
from app.dependencies.auth import get_current_user
from app.models.user import User
from app.models.tutor_profile import TutorProfile
from app.models.walker_profile import WalkerProfile
from app.schemas.auth import LoginRequest, TokenResponse
from app.schemas.user import UserCreate, UserResponse
from app.services.walker_referrals import link_referral_to_user, validate_referral_code
from app.utils.registration_validation import normalize_cpf_or_raise, normalize_email_or_raise, normalize_phone_or_raise

router = APIRouter(prefix="/auth", tags=["auth"])

def build_session(user: User) -> TokenResponse:
    token = create_access_token(user.id, {"role": user.role})
    return TokenResponse(access_token=token, refresh_token=token, user=UserResponse.model_validate(user))

@router.post("/register", response_model=TokenResponse)
def register(payload: UserCreate, db: Session = Depends(get_db)):
    try:
        email = normalize_email_or_raise(str(payload.email))
        profile_payload = payload.profile or {}
        personal = profile_payload.get("personal", {}) if isinstance(profile_payload, dict) else {}
        cpf_source = payload.cpf or personal.get("cpf")
        phone_source = payload.phone or personal.get("telefone") or personal.get("phone")
        cpf = normalize_cpf_or_raise(cpf_source) if cpf_source is not None else ""
        phone = normalize_phone_or_raise(phone_source) if phone_source is not None else ""
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    if db.query(User).filter(User.email == email).first():
        raise HTTPException(status_code=409, detail="E-mail ja cadastrado")
    role = "tutor" if payload.role in {"cliente", "tutor"} else payload.role
    if role in {"walker", "passeador"}:
        documents = profile_payload.get("documents", {}) if isinstance(profile_payload, dict) else {}
        profile_info = profile_payload.get("profile", {}) if isinstance(profile_payload, dict) else {}
        profile_photo_url = profile_payload.get("profile_photo_url") or profile_info.get("photoUri") or profile_info.get("photo_url")
        document_url = profile_payload.get("document_url") or documents.get("identity")
        proof_of_address_url = profile_payload.get("proof_of_address_url") or documents.get("residence")
        missing = []
        if not profile_photo_url:
            missing.append("Envie sua foto de perfil.")
        if not document_url:
            missing.append("Envie o documento obrigatório.")
        if not proof_of_address_url:
            missing.append("Complete os documentos para enviar sua candidatura.")
        if missing:
            raise HTTPException(status_code=400, detail={"message": "Cadastro de passeador incompleto.", "errors": missing})
    if payload.referral_code and role in {"walker", "passeador"}:
        validate_referral_code(payload.referral_code, db)
    user = User(id=str(uuid4()), email=email, full_name=payload.full_name, role=role, password_hash=get_password_hash(payload.password))
    db.add(user)
    if role == "tutor" and (cpf or phone):
        address = profile_payload.get("address", {}) if isinstance(profile_payload, dict) else {}
        db.add(TutorProfile(
            id=str(uuid4()),
            user_id=user.id,
            full_name=payload.full_name or personal.get("nome", ""),
            cpf=cpf,
            phone=phone,
            cep=address.get("cep", ""),
            street=address.get("rua", ""),
            number=address.get("numero", ""),
            complement=address.get("complemento", ""),
            neighborhood=address.get("bairro", ""),
            city=address.get("cidade", ""),
            state=address.get("estado", ""),
        ))
    elif role in {"walker", "passeador"} and (cpf or phone):
        documents = profile_payload.get("documents", {}) if isinstance(profile_payload, dict) else {}
        profile_info = profile_payload.get("profile", {}) if isinstance(profile_payload, dict) else {}
        db.add(WalkerProfile(
            id=str(uuid4()),
            user_id=user.id,
            full_name=payload.full_name,
            cpf=cpf,
            phone=phone,
            profile_photo_url=profile_payload.get("profile_photo_url") or profile_info.get("photoUri") or profile_info.get("photo_url"),
            document_url=profile_payload.get("document_url") or documents.get("identity"),
            proof_of_address_url=profile_payload.get("proof_of_address_url") or documents.get("residence"),
            selfie_url=profile_payload.get("selfie_url") or documents.get("petPhoto"),
            status="document_review",
            active_as_walker=False,
        ))
    db.commit()
    db.refresh(user)
    if payload.referral_code and role in {"walker", "passeador"}:
        link_referral_to_user(payload.referral_code, user, db)
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
