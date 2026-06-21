"""Testes de ROTA (camada HTTP) para o bloco de cadastro de passeador em
POST /auth/register (role=walker / passeador).

Padrao do projeto: FastAPI minimo + SQLite StaticPool + dependency_overrides.

Cobre:
- Happy path com todos os campos → WalkerProfile criado, status=document_review
- Bio ausente → 400
- Bio curta (< 80 chars) → 400
- Foto de perfil ausente → 400
- Documento frente ausente → 400
- Documento verso ausente → 400
- Comprovante de residencia ausente → 400
- com referral_code valido cria vinculo WalkerReferral
"""
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import app.models  # noqa: F401
from app.core.database import Base, get_db
from app.models.tenant import Tenant
from app.models.user import User
from app.models.walker_profile import WalkerProfile
from app.models.walker_referral import WalkerReferral
from app.routes import auth
from app.routes.auth import _register_rate_limiter
from app.services.tenant_seed_service import DEFAULT_TENANT_SLUG

TENANT_ID = "t-walker-reg"
VALID_CPF = "52998224725"
VALID_PHONE = "11987654321"
LONG_BIO = "A" * 80  # exatamente 80 chars (limite minimo)

FULL_PAYLOAD = {
    "email": "walker@test.com",
    "password": "senha1234",
    "full_name": "Walker Teste",
    "role": "walker",
    "cpf": VALID_CPF,
    "phone": VALID_PHONE,
    "profile": {
        "bio": LONG_BIO,
        "profile_photo_url": "https://cdn.example.com/foto.jpg",
        "identity_document_front_url": "https://cdn.example.com/frente.jpg",
        "identity_document_back_url": "https://cdn.example.com/verso.jpg",
        "proof_of_address_url": "https://cdn.example.com/comprovante.jpg",
    },
}


def build(*, extra_users: list[dict] | None = None, extra_referrals: list[dict] | None = None):
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    db = Session()

    db.add(Tenant(id=TENANT_ID, name="Aumigao", slug=DEFAULT_TENANT_SLUG, status="active", plan="business"))
    for u in extra_users or []:
        db.add(User(**u))
    for ref in extra_referrals or []:
        db.add(WalkerReferral(**ref))
    db.commit()

    test_app = FastAPI()
    test_app.include_router(auth.router)
    test_app.dependency_overrides[get_db] = lambda: db
    return TestClient(test_app), db


@pytest.fixture(autouse=True)
def _clear_register_limiter():
    _register_rate_limiter._failures.clear()
    yield
    _register_rate_limiter._failures.clear()


# ---------------------------------------------------------------- happy path ---

def test_register_walker_happy_path_creates_profile():
    """Cadastro completo de passeador cria WalkerProfile com status=document_review."""
    client, db = build()
    import copy
    payload = copy.deepcopy(FULL_PAYLOAD)
    payload["email"] = "walker-happy@test.com"

    r = client.post("/auth/register", json=payload)

    assert r.status_code == 200, r.text
    body = r.json()
    assert body["access_token"]
    assert body["user"]["role"] == "walker"

    user = db.query(User).filter(User.email == "walker-happy@test.com").first()
    assert user is not None
    profile = db.query(WalkerProfile).filter(WalkerProfile.user_id == user.id).first()
    assert profile is not None
    assert profile.status == "document_review"
    assert profile.active_as_walker is False


def test_register_walker_bio_exactly_80_chars_is_accepted():
    """Bio com exatamente 80 caracteres deve ser aceita (limite minimo inclusivo)."""
    client, _ = build()
    import copy
    payload = copy.deepcopy(FULL_PAYLOAD)
    payload["email"] = "walker-bio80@test.com"
    payload["profile"]["bio"] = "X" * 80

    r = client.post("/auth/register", json=payload)

    assert r.status_code == 200, r.text


# ------------------------------------------------------------------- erros ---

def test_register_walker_missing_bio_returns_400():
    """Ausencia de bio (campo ausente) deve retornar 400."""
    client, _ = build()
    import copy
    payload = copy.deepcopy(FULL_PAYLOAD)
    payload["email"] = "walker-nobio@test.com"
    del payload["profile"]["bio"]

    r = client.post("/auth/register", json=payload)

    assert r.status_code == 400
    body = r.json()
    detail = body["detail"]
    # O detalhe pode vir como dict {"message": ..., "errors": [...]}
    detail_str = str(detail).lower()
    assert "apresenta" in detail_str or "bio" in detail_str or "breve" in detail_str


def test_register_walker_short_bio_returns_400():
    """Bio com menos de 80 chars deve retornar 400."""
    client, _ = build()
    import copy
    payload = copy.deepcopy(FULL_PAYLOAD)
    payload["email"] = "walker-shortbio@test.com"
    payload["profile"]["bio"] = "curta"  # << 80

    r = client.post("/auth/register", json=payload)

    assert r.status_code == 400


def test_register_walker_missing_profile_photo_returns_400():
    """Ausencia de foto de perfil deve retornar 400."""
    client, _ = build()
    import copy
    payload = copy.deepcopy(FULL_PAYLOAD)
    payload["email"] = "walker-nophoto@test.com"
    del payload["profile"]["profile_photo_url"]

    r = client.post("/auth/register", json=payload)

    assert r.status_code == 400


def test_register_walker_missing_document_front_returns_400():
    """Ausencia da frente do documento de identidade deve retornar 400."""
    client, _ = build()
    import copy
    payload = copy.deepcopy(FULL_PAYLOAD)
    payload["email"] = "walker-nofront@test.com"
    del payload["profile"]["identity_document_front_url"]

    r = client.post("/auth/register", json=payload)

    assert r.status_code == 400


def test_register_walker_missing_document_back_returns_400():
    """Ausencia do verso do documento de identidade deve retornar 400."""
    client, _ = build()
    import copy
    payload = copy.deepcopy(FULL_PAYLOAD)
    payload["email"] = "walker-noback@test.com"
    del payload["profile"]["identity_document_back_url"]

    r = client.post("/auth/register", json=payload)

    assert r.status_code == 400


def test_register_walker_missing_proof_of_address_returns_400():
    """Ausencia de comprovante de residencia deve retornar 400."""
    client, _ = build()
    import copy
    payload = copy.deepcopy(FULL_PAYLOAD)
    payload["email"] = "walker-noproof@test.com"
    del payload["profile"]["proof_of_address_url"]

    r = client.post("/auth/register", json=payload)

    assert r.status_code == 400


# ------------------------------------------------ referral_code valido -------

def test_register_walker_with_valid_referral_code_creates_link():
    """Cadastro com referral_code valido deve vincular WalkerReferral ao novo usuario."""
    referrer_id = "u-referrer"
    referral_code = "CODE-VALID-001"
    client, db = build(
        extra_users=[
            dict(id=referrer_id, email="referrer@test.com", password_hash="x",
                 role="walker", tenant_id=TENANT_ID, is_active=True)
        ],
        extra_referrals=[
            dict(
                id="ref-001",
                referrer_user_id=referrer_id,
                referred_name="Walker Novo",
                referred_phone="11999990001",
                referred_phone_normalized="11999990001",
                city="Salvador",
                neighborhood="Pituba",
                referral_code=referral_code,
                status="pending",
                reward_status="not_eligible",
            )
        ],
    )
    import copy
    payload = copy.deepcopy(FULL_PAYLOAD)
    payload["email"] = "walker-referred@test.com"
    payload["referral_code"] = referral_code

    r = client.post("/auth/register", json=payload)

    assert r.status_code == 200, r.text
    # Referral deve ter sido vinculado ao novo usuario
    db.expire_all()
    referral = db.query(WalkerReferral).filter(
        WalkerReferral.referral_code == referral_code
    ).first()
    assert referral is not None
    assert referral.referred_user_id is not None
    assert referral.status == "registered"


def test_register_walker_with_invalid_referral_code_returns_404():
    """Referral code inexistente deve retornar 404."""
    client, _ = build()
    import copy
    payload = copy.deepcopy(FULL_PAYLOAD)
    payload["email"] = "walker-badreferral@test.com"
    payload["referral_code"] = "CODIGO-INEXISTENTE"

    r = client.post("/auth/register", json=payload)

    # validate_referral_code levanta HTTPException 404
    assert r.status_code == 404
