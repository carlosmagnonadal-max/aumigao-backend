"""sec/jwt-refresh — testes TDD para infraestrutura de refresh token.

Cobre (em ordem):
1. Refresh token NÃO autentica endpoint protegido (Bearer → 401).
2. Access token legado (sem `type`) CONTINUA autenticando (retrocompat).
3. /auth/refresh com refresh válido → 200 + novo access_token que AUTENTICA.
4. /auth/refresh com access token (type errado) → 401.
5. /auth/refresh após bump de token_version → 401 (revogado).
6. login retorna access_token e refresh_token DIFERENTES, ambos válidos no seu papel.
"""
from datetime import datetime, timedelta, timezone

import jwt
import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import app.models  # noqa: F401 — registra tabelas no Base.metadata
from app.core.database import Base, get_db
from app.core.security import (
    ALGORITHM,
    JWT_AUDIENCE,
    JWT_ISSUER,
    SECRET_KEY,
    ACCESS_TOKEN_EXPIRE_MINUTES,
    create_access_token,
    create_refresh_token,
    decode_access_token,
)
from app.dependencies.auth import get_current_user
from app.models.tenant import Tenant
from app.models.user import User
from app.routes import auth
from app.services.login_rate_limiter import login_rate_limiter
from app.services.tenant_seed_service import DEFAULT_TENANT_SLUG
from app.core.security import get_password_hash

TENANT_ID = "t-refresh-test"
USER_ID = "user-refresh-1"


# ---------------------------------------------------------------------------
# Infraestrutura compartilhada
# ---------------------------------------------------------------------------

def _build_db():
    """Banco SQLite em memória isolado com user+tenant pré-criado."""
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    db = Session()
    db.add(Tenant(id=TENANT_ID, name="Aumigao", slug=DEFAULT_TENANT_SLUG, status="active", plan="business"))
    db.add(User(
        id=USER_ID,
        email="refresh@example.com",
        password_hash=get_password_hash("Pass123x"),
        role="tutor",
        is_active=True,
        token_version=0,
        full_name="Refresh User",
        created_at=datetime.utcnow(),
    ))
    db.commit()
    return Session, db


def _build_app(Session=None, db=None):
    """Monta FastAPI mínimo com router de auth + endpoint /me protegido."""
    if db is None:
        Session, db = _build_db()

    test_app = FastAPI()
    test_app.include_router(auth.router)

    @test_app.get("/me")
    def me(user: User = Depends(get_current_user)):
        return {"id": user.id, "role": user.role}

    test_app.dependency_overrides[get_db] = lambda: db
    test_app.state._session = Session
    test_app.state._db = db
    return TestClient(test_app), db, Session


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


# ---------------------------------------------------------------------------
# Testes de unidade: create_refresh_token / decode
# ---------------------------------------------------------------------------

def test_create_refresh_token_has_type_refresh():
    """create_refresh_token deve emitir claim type=refresh."""
    user = User(id=USER_ID, email="x@x.com", password_hash="h", role="tutor",
                is_active=True, token_version=3, full_name="X", created_at=datetime.utcnow())
    token = create_refresh_token(user)
    payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM], options={"verify_aud": False})
    assert payload["type"] == "refresh"
    assert payload["sub"] == USER_ID
    assert payload["ver"] == 3


def test_create_refresh_token_has_long_expiry():
    """Refresh token deve expirar em ~30 dias (TTL > access token TTL)."""
    user = User(id=USER_ID, email="x@x.com", password_hash="h", role="tutor",
                is_active=True, token_version=0, full_name="X", created_at=datetime.utcnow())
    token = create_refresh_token(user)
    payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM], options={"verify_aud": False})
    exp = datetime.fromtimestamp(payload["exp"], tz=timezone.utc)
    now = datetime.now(timezone.utc)
    days_left = (exp - now).days
    # Deve ser significativamente maior que o access token (7 dias)
    assert days_left >= 25, f"refresh expiry muito curto: {days_left} dias"


def test_access_token_ttl_unchanged():
    """ACCESS_TOKEN_EXPIRE_MINUTES NÃO deve ter mudado (continua 7 dias = 10080 min)."""
    assert ACCESS_TOKEN_EXPIRE_MINUTES == 60 * 24 * 7, (
        f"TTL do access token mudou! Esperado 10080, got {ACCESS_TOKEN_EXPIRE_MINUTES}"
    )


def test_refresh_token_not_equal_to_access_token():
    """Refresh token deve ser DIFERENTE do access token (não mais um alias)."""
    user = User(id=USER_ID, email="x@x.com", password_hash="h", role="tutor",
                is_active=True, token_version=0, full_name="X", created_at=datetime.utcnow())
    from app.routes.auth import build_session
    resp = build_session(user)
    assert resp.access_token != resp.refresh_token, (
        "refresh_token ainda é alias do access_token — build_session não foi atualizado"
    )


# ---------------------------------------------------------------------------
# Teste 1: Refresh token NÃO autentica endpoint protegido
# ---------------------------------------------------------------------------

def test_refresh_token_rejected_as_bearer():
    """Refresh token enviado como Bearer → 401 (não pode autenticar rotas normais)."""
    client, db, Session = _build_app()
    user = db.query(User).filter(User.id == USER_ID).first()
    refresh = create_refresh_token(user)

    resp = client.get("/me", headers=_auth(refresh))
    assert resp.status_code == 401, (
        f"Refresh token deveria ser rejeitado como Bearer, mas retornou {resp.status_code}"
    )


# ---------------------------------------------------------------------------
# Teste 2: Access token legado (sem `type`) CONTINUA autenticando
# ---------------------------------------------------------------------------

def test_legacy_access_token_without_type_still_authenticates():
    """Token legado sem claim `type` deve continuar autenticando (retrocompat)."""
    client, db, Session = _build_app()
    # Emite token com a mesma assinatura mas sem claim `type` (simula token legado)
    legacy_token = jwt.encode(
        {
            "sub": USER_ID,
            "iat": datetime.now(timezone.utc),
            "exp": datetime.now(timezone.utc) + timedelta(hours=1),
            "iss": JWT_ISSUER,
            "aud": JWT_AUDIENCE,
            "jti": "legacy-jti-001",
            "ver": 0,
            # SEM `type` — token legado
        },
        SECRET_KEY,
        algorithm=ALGORITHM,
    )
    resp = client.get("/me", headers=_auth(legacy_token))
    assert resp.status_code == 200, (
        f"Token legado sem `type` foi rejeitado (quebrou retrocompat): {resp.status_code}"
    )


def test_access_token_with_type_access_authenticates():
    """Token com type=access (tokens novos) deve autenticar normalmente."""
    client, db, Session = _build_app()
    token = create_access_token(USER_ID, {"ver": 0, "role": "tutor"})
    resp = client.get("/me", headers=_auth(token))
    assert resp.status_code == 200


# ---------------------------------------------------------------------------
# Teste 3: /auth/refresh com refresh válido → 200 + novo access_token que autentica
# ---------------------------------------------------------------------------

def test_refresh_endpoint_returns_new_access_token():
    """/auth/refresh com refresh válido → 200 + access_token funcional."""
    client, db, Session = _build_app()
    user = db.query(User).filter(User.id == USER_ID).first()
    refresh = create_refresh_token(user)

    resp = client.post("/auth/refresh", json={"refresh_token": refresh})
    assert resp.status_code == 200, f"Expected 200, got {resp.status_code}: {resp.text}"
    body = resp.json()
    assert "access_token" in body

    new_access = body["access_token"]
    # O novo access_token deve autenticar no /me
    me_resp = client.get("/me", headers=_auth(new_access))
    assert me_resp.status_code == 200, (
        f"Novo access_token emitido pelo /auth/refresh não autenticou: {me_resp.status_code}"
    )


def test_refresh_endpoint_new_access_is_access_type():
    """O access_token retornado pelo /auth/refresh não deve ter type=refresh."""
    client, db, Session = _build_app()
    user = db.query(User).filter(User.id == USER_ID).first()
    refresh = create_refresh_token(user)

    resp = client.post("/auth/refresh", json={"refresh_token": refresh})
    assert resp.status_code == 200
    new_access = resp.json()["access_token"]

    payload = jwt.decode(new_access, SECRET_KEY, algorithms=[ALGORITHM], options={"verify_aud": False})
    assert payload.get("type") != "refresh", "access_token retornado é um refresh token!"


# ---------------------------------------------------------------------------
# Teste 4: /auth/refresh com access token (type errado) → 401
# ---------------------------------------------------------------------------

def test_refresh_endpoint_rejects_access_token():
    """/auth/refresh com access token (type!=refresh) → 401."""
    client, db, Session = _build_app()
    # Usa um access token normal (sem type=refresh)
    access = create_access_token(USER_ID, {"ver": 0, "role": "tutor"})

    resp = client.post("/auth/refresh", json={"refresh_token": access})
    assert resp.status_code == 401, (
        f"/auth/refresh deveria rejeitar access token, mas retornou {resp.status_code}"
    )


def test_refresh_endpoint_rejects_random_jwt():
    """/auth/refresh com JWT de tipo desconhecido → 401."""
    client, db, Session = _build_app()
    random_token = jwt.encode(
        {"sub": USER_ID, "exp": datetime.now(timezone.utc) + timedelta(hours=1)},
        SECRET_KEY,
        algorithm=ALGORITHM,
    )
    resp = client.post("/auth/refresh", json={"refresh_token": random_token})
    assert resp.status_code == 401


# ---------------------------------------------------------------------------
# Teste 5: /auth/refresh após bump de token_version → 401 (revogado)
# ---------------------------------------------------------------------------

def test_refresh_revoked_after_token_version_bump():
    """/auth/refresh com refresh emitido ANTES de bump de token_version → 401."""
    Session, db = _build_db()
    client, db, Session = _build_app(Session, db)

    user = db.query(User).filter(User.id == USER_ID).first()
    # Gera refresh com ver=0
    refresh = create_refresh_token(user)

    # Simula troca de senha: bumpa token_version para 1
    user.token_version = 1
    db.commit()

    # O refresh antigo (ver=0) deve ser rejeitado
    resp = client.post("/auth/refresh", json={"refresh_token": refresh})
    assert resp.status_code == 401, (
        f"Refresh revogado deveria retornar 401, got {resp.status_code}"
    )


# ---------------------------------------------------------------------------
# Teste 6: login retorna access_token e refresh_token DIFERENTES
# ---------------------------------------------------------------------------

def test_login_returns_distinct_access_and_refresh_tokens():
    """login deve retornar access_token != refresh_token, ambos válidos no seu papel."""
    client, db, Session = _build_app()
    login_rate_limiter.clear("refresh@example.com")

    resp = client.post("/auth/login", json={"email": "refresh@example.com", "password": "Pass123x"})
    assert resp.status_code == 200, f"Login falhou: {resp.status_code} {resp.text}"
    body = resp.json()

    access = body["access_token"]
    refresh = body["refresh_token"]

    # Devem ser diferentes
    assert access != refresh, "access_token e refresh_token são iguais — build_session não foi atualizado"

    # Access token autentica /me
    me_resp = client.get("/me", headers=_auth(access))
    assert me_resp.status_code == 200, f"access_token não autenticou /me: {me_resp.status_code}"

    # Refresh token NÃO autentica /me
    me_refresh_resp = client.get("/me", headers=_auth(refresh))
    assert me_refresh_resp.status_code == 401, (
        f"refresh_token não deveria autenticar /me, mas retornou {me_refresh_resp.status_code}"
    )

    # Refresh token funciona no /auth/refresh
    refresh_resp = client.post("/auth/refresh", json={"refresh_token": refresh})
    assert refresh_resp.status_code == 200, (
        f"refresh_token não funcionou em /auth/refresh: {refresh_resp.status_code}"
    )


def test_login_refresh_token_has_type_refresh():
    """O refresh_token retornado pelo login deve ter claim type=refresh."""
    client, db, Session = _build_app()
    login_rate_limiter.clear("refresh@example.com")

    resp = client.post("/auth/login", json={"email": "refresh@example.com", "password": "Pass123x"})
    assert resp.status_code == 200
    refresh = resp.json()["refresh_token"]

    payload = jwt.decode(refresh, SECRET_KEY, algorithms=[ALGORITHM], options={"verify_aud": False})
    assert payload.get("type") == "refresh"


# ---------------------------------------------------------------------------
# Testes de edge cases e segurança
# ---------------------------------------------------------------------------

def test_refresh_endpoint_rejects_expired_refresh_token():
    """/auth/refresh com refresh expirado → 401."""
    client, db, Session = _build_app()
    # Cria refresh token com exp no passado
    expired_token = jwt.encode(
        {
            "sub": USER_ID,
            "type": "refresh",
            "ver": 0,
            "jti": "test-expired",
            "exp": datetime.now(timezone.utc) - timedelta(seconds=1),
        },
        SECRET_KEY,
        algorithm=ALGORITHM,
    )
    resp = client.post("/auth/refresh", json={"refresh_token": expired_token})
    assert resp.status_code == 401


def test_refresh_endpoint_rejects_forged_signature():
    """/auth/refresh com assinatura forjada → 401."""
    client, db, Session = _build_app()
    forged = jwt.encode(
        {
            "sub": USER_ID,
            "type": "refresh",
            "ver": 0,
            "jti": "test-forged",
            "exp": datetime.now(timezone.utc) + timedelta(days=30),
        },
        "wrong-secret-key-that-is-long-enough",
        algorithm=ALGORITHM,
    )
    resp = client.post("/auth/refresh", json={"refresh_token": forged})
    assert resp.status_code == 401


def test_refresh_endpoint_rejects_missing_body():
    """/auth/refresh sem body → 422 (validação Pydantic)."""
    client, db, Session = _build_app()
    resp = client.post("/auth/refresh", json={})
    assert resp.status_code == 422


def test_decode_access_token_rejects_refresh_type():
    """decode_access_token deve rejeitar tokens com type=refresh."""
    user = User(id=USER_ID, email="x@x.com", password_hash="h", role="tutor",
                is_active=True, token_version=0, full_name="X", created_at=datetime.utcnow())
    refresh = create_refresh_token(user)
    with pytest.raises(Exception):
        decode_access_token(refresh)
