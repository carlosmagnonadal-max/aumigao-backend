import base64
import json
import logging
import secrets
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import uuid4

import httpx
import jwt
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy.orm import Session

_apple_logger = logging.getLogger("aumigao.apple_auth")

# ---------------------------------------------------------------------------
# Apple JWKS cache (in-memory, refreshed after TTL or on key-miss)
# ---------------------------------------------------------------------------
_APPLE_JWKS_URL = "https://appleid.apple.com/auth/keys"
_APPLE_JWKS_TTL = 3600  # segundos — chaves Apple mudam raramente

_apple_jwks_lock = threading.Lock()
_apple_jwks_cache: dict[str, Any] = {}          # kid -> JWK dict
_apple_jwks_fetched_at: float = 0.0


def _fetch_apple_jwks(force: bool = False) -> dict[str, Any]:
    """Retorna dicionário kid->JWK buscado de Apple JWKS, com cache TTL."""
    global _apple_jwks_cache, _apple_jwks_fetched_at
    now = time.monotonic()
    with _apple_jwks_lock:
        if not force and _apple_jwks_cache and (now - _apple_jwks_fetched_at) < _APPLE_JWKS_TTL:
            return _apple_jwks_cache
        try:
            resp = httpx.get(_APPLE_JWKS_URL, timeout=5)
            resp.raise_for_status()
            keys = resp.json().get("keys", [])
            _apple_jwks_cache = {k["kid"]: k for k in keys if k.get("kid")}
            _apple_jwks_fetched_at = now
        except Exception as exc:
            _apple_logger.warning("Falha ao buscar Apple JWKS: %s", exc)
            # Se já temos cache (mesmo expirado), usa-o; caso contrário propaga vazio.
            if not _apple_jwks_cache:
                return {}
        return _apple_jwks_cache


# Bundle IDs aceitos como audience do token Apple.
# Env APPLE_CLIENT_ID pode ser CSV para suportar múltiplos targets.
def _apple_allowed_audiences() -> list[str]:
    env_val = (
        __import__("os").getenv("APPLE_CLIENT_ID", "").strip()
    )
    bases = [a.strip() for a in env_val.split(",") if a.strip()]
    if not bases:
        bases = ["com.aumigao.tutor", "com.aumigao.walker"]
    return bases

from app.core.database import get_db
from app.core.security import create_access_token, get_password_hash, verify_password
from app.dependencies.auth import get_current_user
from app.models.password_reset_code import PasswordResetCode
from app.models.user import User
from app.models.tutor_profile import TutorProfile
from app.models.walker_profile import WalkerProfile
from app.schemas.auth import LoginRequest, SocialLoginPayload, TokenResponse
from app.schemas.user import UserCreate, UserResponse
from app.services.identity_uniqueness import ensure_unique_identity
from app.services.login_rate_limiter import InMemoryLoginRateLimiter, login_rate_limiter
from app.services.tenant_seed_service import default_tenant_id
from app.services.transactional_email_service import send_password_reset_email, send_welcome_email
from app.services.walker_referrals import link_referral_to_user, validate_referral_code
from app.utils.registration_validation import normalize_cpf_or_raise, normalize_email_or_raise, normalize_phone_or_raise

# Rate limiter dedicado para forgot-password: 3 tentativas por 15 min (por e-mail).
# Compartilhado com IP embedado na chave quando disponível.
_forgot_password_limiter = InMemoryLoginRateLimiter(max_failures=3, window_seconds=900)

_PASSWORD_RESET_TTL_MINUTES = 15
_PASSWORD_RESET_MAX_ATTEMPTS = 5


class ForgotPasswordRequest(BaseModel):
    email: str


class ResetPasswordRequest(BaseModel):
    email: str
    code: str
    new_password: str


class ChangePasswordRequest(BaseModel):
    current_password: str
    new_password: str


# Rate limiter dedicado para change-password: 5 tentativas por 15 min (por user_id).
_change_password_limiter = InMemoryLoginRateLimiter(max_failures=5, window_seconds=900)

router = APIRouter(prefix="/auth", tags=["auth"])
# api_router removido: estava declarado mas nunca registrado em main.py e sem rotas.
# Duplicar todos os decorators @router.xxx com @api_router.xxx seria invasivo sem
# benefício comprovado em prod. Se necessário no futuro, adicionar espelhamento aqui.

def build_session(user: User) -> TokenResponse:
    # B-ALT-011 (passo 2b): "ver" carrega o token_version do usuario; o get_current_user
    # rejeita tokens cujo "ver" ficou para tras (revogados na troca/reset de senha).
    token = create_access_token(user.id, {"role": user.role, "ver": user.token_version or 0})
    return TokenResponse(access_token=token, refresh_token=token, user=UserResponse.model_validate(user))

@router.post("/register", response_model=TokenResponse)
def register(payload: UserCreate, request: Request, db: Session = Depends(get_db)):
    if len(payload.password or "") < 8 or not any(char.isalpha() for char in payload.password) or not any(char.isdigit() for char in payload.password):
        raise HTTPException(status_code=400, detail="A senha deve ter pelo menos 8 caracteres, incluindo 1 letra e 1 numero.")
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

    ensure_unique_identity(db, email=email, cpf=cpf or None, phone=phone or None)
    role = "tutor" if payload.role in {"cliente", "tutor"} else payload.role
    if role in {"walker", "passeador"}:
        documents = profile_payload.get("documents", {}) if isinstance(profile_payload, dict) else {}
        profile_info = profile_payload.get("profile", {}) if isinstance(profile_payload, dict) else {}
        profile_photo_url = profile_payload.get("profile_photo_url") or profile_info.get("photoUri") or profile_info.get("photo_url")
        document_url = profile_payload.get("identity_document_front_url") or profile_payload.get("document_url") or documents.get("identityFront") or documents.get("identity")
        identity_document_back_url = profile_payload.get("identity_document_back_url") or documents.get("identityBack")
        proof_of_address_url = profile_payload.get("proof_of_address_url") or documents.get("residence")
        bio = str(profile_payload.get("bio") or profile_info.get("bio") or "")
        missing = []
        if not profile_photo_url:
            missing.append("Envie sua foto de perfil.")
        if len(bio.strip()) < 80:
            missing.append("Escreva uma breve apresentação para os tutores.")
        if not document_url:
            missing.append("Envie a frente do documento de identidade.")
        if not identity_document_back_url:
            missing.append("Envie o verso do documento de identidade.")
        if not proof_of_address_url:
            missing.append("Complete os documentos para enviar sua candidatura.")
        if missing:
            raise HTTPException(status_code=400, detail={"message": "Cadastro de passeador incompleto.", "errors": missing})
    if payload.referral_code and role in {"walker", "passeador"}:
        validate_referral_code(payload.referral_code, db)
    # Split: o build dedicado do tenant envia X-Tenant-Slug; o middleware resolve em
    # request.state.tenant_id. Tutor entra no tenant do build; sem header (combined/walker)
    # cai no default. (Passeador e plataforma; o vinculo real e via TenantWalkerAccess.)
    tenant_id = getattr(request.state, "tenant_id", None) or default_tenant_id(db)
    user = User(id=str(uuid4()), email=email, full_name=payload.full_name, role=role, password_hash=get_password_hash(payload.password), tenant_id=tenant_id)
    db.add(user)
    if role == "tutor" and (cpf or phone):
        address = profile_payload.get("address", {}) if isinstance(profile_payload, dict) else {}
        db.add(TutorProfile(
            id=str(uuid4()),
            user_id=user.id,
            tenant_id=tenant_id,
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
        bio = str(profile_payload.get("bio") or profile_info.get("bio") or "").strip()
        experience_options = profile_payload.get("experience_options") or profile_info.get("experience") or []
        experience = " | ".join([bio, *[str(item).strip() for item in experience_options if str(item).strip()]])
        db.add(WalkerProfile(
            id=str(uuid4()),
            user_id=user.id,
            full_name=payload.full_name,
            cpf=cpf,
            phone=phone,
            bio=bio,
            experience=experience,
            profile_photo_url=profile_payload.get("profile_photo_url") or profile_info.get("photoUri") or profile_info.get("photo_url"),
            document_url=profile_payload.get("identity_document_front_url") or profile_payload.get("document_url") or documents.get("identityFront") or documents.get("identity"),
            identity_document_back_url=profile_payload.get("identity_document_back_url") or documents.get("identityBack"),
            proof_of_address_url=profile_payload.get("proof_of_address_url") or documents.get("residence"),
            selfie_url=profile_payload.get("selfie_url") or documents.get("petPhoto"),
            status="document_review",
            active_as_walker=False,
        ))
    db.commit()
    db.refresh(user)
    if payload.referral_code and role in {"walker", "passeador"}:
        link_referral_to_user(payload.referral_code, user, db)
    # F1.2: boas-vindas fire-and-forget — nunca falha o registro
    threading.Thread(
        target=send_welcome_email,
        args=(user.email, user.full_name or ""),
        daemon=True,
    ).start()
    return build_session(user)

@router.post("/login", response_model=TokenResponse)
def login(payload: LoginRequest, db: Session = Depends(get_db)):
    try:
        email = normalize_email_or_raise(payload.email)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    password = str(payload.password or "").strip()
    if login_rate_limiter.is_blocked(email):
        raise HTTPException(status_code=429, detail="Muitas tentativas de login. Tente novamente mais tarde.")

    user = db.query(User).filter(User.email == email).first()
    if not user or not verify_password(password, user.password_hash):
        login_rate_limiter.record_failure(email)
        raise HTTPException(status_code=401, detail="Credenciais invalidas")
    if not user.is_active:
        raise HTTPException(status_code=403, detail="Usuario inativo")
    login_rate_limiter.clear(email)
    return build_session(user)

@router.get("/me", response_model=UserResponse)
def me(user: User = Depends(get_current_user)):
    return user


async def _google_user_info(access_token: str) -> dict:
    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.get(
            "https://www.googleapis.com/oauth2/v3/userinfo",
            headers={"Authorization": f"Bearer {access_token}"},
        )
    if resp.status_code != 200:
        raise HTTPException(status_code=401, detail="Token Google inválido ou expirado.")
    return resp.json()


def _decode_apple_jwt_payload(identity_token: str) -> dict:
    """Valida e decodifica o JWT da Apple com verificação completa de assinatura RS256.

    Passos:
    1. Extrai o header para descobrir o kid.
    2. Busca/usa cache do JWKS da Apple; tenta refresh se kid não encontrado.
    3. Verifica assinatura RS256, iss, aud e exp.
    4. Em ausência de rede (JWKS vazio e sem cache), REJEITA — nunca aceita cego.
    """
    # --- 1. Decode header sem verificação para obter kid/alg ---
    try:
        header = jwt.get_unverified_header(identity_token)
    except Exception:
        raise HTTPException(status_code=400, detail="Token Apple malformado.")

    kid = header.get("kid")
    alg = header.get("alg", "RS256")
    if alg != "RS256":
        raise HTTPException(status_code=401, detail="Algoritmo de token Apple inválido.")

    # --- 2. Buscar chave pública ---
    jwks = _fetch_apple_jwks()
    if kid and kid not in jwks:
        # Key-miss: tenta refresh forçado (chaves podem ter rotacionado).
        jwks = _fetch_apple_jwks(force=True)

    if not jwks:
        # Sem rede e sem cache: rejeitar — NÃO aceitar cegamente.
        _apple_logger.warning("Apple JWKS indisponível; token Apple rejeitado por segurança.")
        raise HTTPException(status_code=503, detail="Serviço Apple temporariamente indisponível. Tente novamente.")

    jwk_data = jwks.get(kid) if kid else next(iter(jwks.values()), None)
    if not jwk_data:
        raise HTTPException(status_code=401, detail="Chave pública Apple não encontrada para este token.")

    try:
        from jwt.algorithms import RSAAlgorithm
        public_key = RSAAlgorithm.from_jwk(json.dumps(jwk_data))
    except Exception as exc:
        _apple_logger.warning("Erro ao construir chave RSA Apple: %s", exc)
        raise HTTPException(status_code=401, detail="Erro ao processar chave pública Apple.")

    # --- 3. Verificar assinatura, iss, aud e exp ---
    allowed_audiences = _apple_allowed_audiences()
    last_exc: Exception | None = None
    for aud in allowed_audiences:
        try:
            payload = jwt.decode(
                identity_token,
                public_key,
                algorithms=["RS256"],
                audience=aud,
                issuer="https://appleid.apple.com",
                options={"require": ["exp", "iss", "aud", "sub"]},
            )
            return payload
        except jwt.ExpiredSignatureError:
            raise HTTPException(status_code=401, detail="Token Apple expirado.")
        except jwt.InvalidAudienceError:
            last_exc = jwt.InvalidAudienceError(aud)
            continue
        except jwt.InvalidIssuerError:
            raise HTTPException(status_code=401, detail="Token Apple com issuer inválido.")
        except jwt.DecodeError as exc:
            raise HTTPException(status_code=401, detail=f"Token Apple com assinatura inválida: {exc}")
        except Exception as exc:
            raise HTTPException(status_code=401, detail=f"Token Apple inválido: {exc}")

    # Nenhum audience bateu
    _apple_logger.warning("Token Apple rejeitado: audience não corresponde a %s", allowed_audiences)
    raise HTTPException(status_code=401, detail="Token Apple com audience inválido.")


@router.post("/social", response_model=TokenResponse)
async def social_login(payload: SocialLoginPayload, request: Request, db: Session = Depends(get_db)):
    if payload.provider == "google":
        info = await _google_user_info(payload.token)
        email = info.get("email", "").strip().lower()
        full_name = info.get("name") or ""
    elif payload.provider == "apple":
        token_data = _decode_apple_jwt_payload(payload.token)
        email = (token_data.get("email") or payload.email or "").strip().lower()
        full_name = payload.full_name or ""
    else:
        raise HTTPException(status_code=400, detail="Provider inválido. Use 'google' ou 'apple'.")

    if not email:
        raise HTTPException(status_code=400, detail="Email não disponível no token. Tente novamente.")

    user = db.query(User).filter(User.email == email).first()
    if not user:
        # Passeadores não podem criar conta via social login — precisam do fluxo de cadastro
        # com documentos. Se app_target=walker, bloqueia a criação de conta nova.
        if payload.app_target == "walker":
            raise HTTPException(
                status_code=403,
                detail="Passeadores devem se cadastrar pelo formulário de candidatura. Use email e senha para entrar se já tiver conta.",
            )
        tenant_id = getattr(request.state, "tenant_id", None) or default_tenant_id(db)
        user = User(
            id=str(uuid4()),
            email=email,
            full_name=full_name or email.split("@")[0],
            role="tutor",
            password_hash="",
            tenant_id=tenant_id,
            is_active=True,
        )
        db.add(user)
        db.commit()
        db.refresh(user)
        # F1.2: boas-vindas para contas novas criadas via social login
        threading.Thread(
            target=send_welcome_email,
            args=(user.email, user.full_name or ""),
            daemon=True,
        ).start()

    return build_session(user)


# --------------------------------------------------------- forgot-password ----

def _hash_reset_code(code: str) -> str:
    """Retorna hash pbkdf2_sha256 de um código de reset (mesma função de security.py)."""
    from os import urandom
    from hashlib import pbkdf2_hmac
    salt = urandom(16)
    digest = pbkdf2_hmac("sha256", code.encode("utf-8"), salt, 120_000)
    return f"pbkdf2_sha256${salt.hex()}${digest.hex()}"


def _verify_reset_code(code: str, code_hash: str) -> bool:
    from hashlib import pbkdf2_hmac
    from hmac import compare_digest
    try:
        algorithm, salt_hex, digest_hex = code_hash.split("$", 2)
        if algorithm != "pbkdf2_sha256":
            return False
        digest = pbkdf2_hmac("sha256", code.encode("utf-8"), bytes.fromhex(salt_hex), 120_000)
        return compare_digest(digest.hex(), digest_hex)
    except Exception:
        return False


def _validate_password_strength(password: str) -> None:
    """Valida força da senha com o mesmo critério do /auth/register."""
    if len(password or "") < 8 or not any(c.isalpha() for c in password) or not any(c.isdigit() for c in password):
        raise HTTPException(
            status_code=400,
            detail="A senha deve ter pelo menos 8 caracteres, incluindo 1 letra e 1 numero.",
        )


@router.post("/forgot-password")
def forgot_password(payload: ForgotPasswordRequest, request: Request, db: Session = Depends(get_db)):
    """Solicita código de 6 dígitos para reset de senha.

    SEMPRE retorna 200 com mensagem neutra (não revela se o e-mail existe).
    Rate limit: 3 tentativas por 15 min por e-mail+IP.
    """
    try:
        email = normalize_email_or_raise(payload.email)
    except ValueError:
        # e-mail inválido → retorno neutro (não vazar informação)
        return {"message": "Se o e-mail estiver cadastrado, você receberá um código em breve."}

    # Rate limit: chave = email:ip
    client_ip = (request.headers.get("X-Forwarded-For") or request.client.host or "unknown").split(",")[0].strip()
    limiter_key = f"{email}:{client_ip}"
    if _forgot_password_limiter.is_blocked(limiter_key):
        raise HTTPException(status_code=429, detail="Muitas tentativas. Tente novamente em 15 minutos.")

    user = db.query(User).filter(User.email == email).first()
    if not user:
        # Registra falha para rate limit mesmo sem usuário (evita enumeração via timing)
        _forgot_password_limiter.record_failure(limiter_key)
        return {"message": "Se o e-mail estiver cadastrado, você receberá um código em breve."}

    _forgot_password_limiter.record_failure(limiter_key)

    # Invalida códigos anteriores do usuário (marca como usados)
    db.query(PasswordResetCode).filter(
        PasswordResetCode.user_id == user.id,
        PasswordResetCode.used_at.is_(None),
    ).update({"used_at": datetime.utcnow()})

    # Gera código de 6 dígitos
    code = f"{secrets.randbelow(1_000_000):06d}"
    expires_at = datetime.utcnow() + timedelta(minutes=_PASSWORD_RESET_TTL_MINUTES)
    reset_code = PasswordResetCode(
        id=str(uuid4()),
        user_id=user.id,
        code_hash=_hash_reset_code(code),
        expires_at=expires_at,
        attempts=0,
    )
    db.add(reset_code)
    db.commit()

    # Envia e-mail de forma fire-and-forget
    threading.Thread(
        target=send_password_reset_email,
        args=(user.email, code, user.full_name or ""),
        daemon=True,
    ).start()

    return {"message": "Se o e-mail estiver cadastrado, você receberá um código em breve."}


@router.post("/reset-password")
def reset_password(payload: ResetPasswordRequest, db: Session = Depends(get_db)):
    """Valida código de 6 dígitos e redefine a senha.

    Incrementa attempts a cada tentativa; após 5 erros invalida o código.
    """
    try:
        email = normalize_email_or_raise(payload.email)
    except ValueError:
        raise HTTPException(status_code=400, detail="E-mail inválido.")

    _validate_password_strength(payload.new_password)

    user = db.query(User).filter(User.email == email).first()
    if not user:
        raise HTTPException(status_code=400, detail="Código inválido ou expirado.")

    now = datetime.utcnow()
    # Busca o código ativo mais recente
    reset_code = (
        db.query(PasswordResetCode)
        .filter(
            PasswordResetCode.user_id == user.id,
            PasswordResetCode.used_at.is_(None),
            PasswordResetCode.expires_at > now,
        )
        .order_by(PasswordResetCode.created_at.desc())
        .first()
    )

    if not reset_code:
        raise HTTPException(status_code=400, detail="Código inválido ou expirado.")

    # Incrementa tentativas antes de validar (conta independente de acerto)
    reset_code.attempts += 1

    if reset_code.attempts > _PASSWORD_RESET_MAX_ATTEMPTS:
        reset_code.used_at = now  # invalida
        db.commit()
        raise HTTPException(status_code=400, detail="Número máximo de tentativas atingido. Solicite um novo código.")

    if not _verify_reset_code(str(payload.code).strip(), reset_code.code_hash):
        if reset_code.attempts >= _PASSWORD_RESET_MAX_ATTEMPTS:
            reset_code.used_at = now  # invalida após 5a falha
        db.commit()
        raise HTTPException(status_code=400, detail="Código inválido ou expirado.")

    # Código correto: atualiza senha e marca como usado
    user.password_hash = get_password_hash(payload.new_password)
    # B-ALT-011 (passo 2b): redefinir a senha revoga TODAS as sessoes antigas
    # (essencial na recuperacao de conta comprometida).
    user.token_version = (user.token_version or 0) + 1
    reset_code.used_at = now
    db.commit()

    return {"message": "Senha redefinida com sucesso."}


@router.post("/change-password")
def change_password(
    payload: ChangePasswordRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Altera a senha do usuário autenticado.

    Valida a senha atual; valida a força da nova; troca o hash.
    Rate limit: 5 tentativas por 15 min por usuário.
    """
    limiter_key = current_user.id
    if _change_password_limiter.is_blocked(limiter_key):
        raise HTTPException(status_code=429, detail="Muitas tentativas. Tente novamente em 15 minutos.")

    # Valida senha atual (mesmo que user tenha conta social sem senha)
    current_hash = current_user.password_hash or ""
    if not current_hash or not verify_password(str(payload.current_password or ""), current_hash):
        _change_password_limiter.record_failure(limiter_key)
        raise HTTPException(status_code=400, detail="Senha atual incorreta.")

    _validate_password_strength(payload.new_password)

    current_user.password_hash = get_password_hash(payload.new_password)
    # B-ALT-011 (passo 2b): trocar a senha revoga TODAS as sessoes antigas.
    current_user.token_version = (current_user.token_version or 0) + 1
    db.commit()

    _change_password_limiter.clear(limiter_key)
    return {"message": "Senha alterada com sucesso."}
