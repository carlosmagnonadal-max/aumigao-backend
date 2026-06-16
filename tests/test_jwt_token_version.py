"""B-ALT-011 (passo 2b) — revogacao de sessao via token_version no User.

O access token passa a carregar "ver" = User.token_version. O get_current_user, depois de
carregar o usuario, compara: se o token traz "ver" e ele NAO bate com o token_version
atual do usuario, o token e REVOGADO (401). Trocar/redefinir a senha INCREMENTA o
token_version -> invalida TODAS as sessoes antigas (logout-everywhere / recuperacao de
conta comprometida).

RETROCOMPAT: tokens legados sem "ver" continuam aceitos durante a janela de TTL (mesma
filosofia do passo 2a). Quando expirarem, da p/ exigir "ver".
"""
from datetime import datetime, timedelta

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import app.models  # noqa: F401 — registra as tabelas
from app.core.database import Base, get_db
from app.core.security import create_access_token, decode_access_token, get_password_hash
from app.dependencies.auth import get_current_user
from app.models.password_reset_code import PasswordResetCode
from app.models.user import User
from app.routes.auth import (
    ChangePasswordRequest,
    ResetPasswordRequest,
    _hash_reset_code,
    build_session,
    change_password,
    reset_password,
)

USER_ID = "user-tv-1"


def _session_factory():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, autoflush=False, autocommit=False)


# --- emissao -------------------------------------------------------------------

def test_build_session_emits_token_version():
    # full_name/created_at preenchidos: UserResponse exige (em prod o user vem do DB).
    user = User(
        id=USER_ID, email="tv@example.com", password_hash="h", role="tutor",
        is_active=True, token_version=7, full_name="TV", created_at=datetime.utcnow(),
    )
    resp = build_session(user)
    payload = decode_access_token(resp.access_token)
    assert payload["ver"] == 7


# --- bump nas trocas de senha --------------------------------------------------

def test_change_password_bumps_token_version():
    Session = _session_factory()
    db = Session()
    user = User(id=USER_ID, email="tv@example.com", password_hash=get_password_hash("OldPass1"), role="tutor", is_active=True, token_version=0)
    db.add(user)
    db.commit()

    change_password(ChangePasswordRequest(current_password="OldPass1", new_password="NewPass2"), user, db)

    assert user.token_version == 1


def test_reset_password_bumps_token_version():
    Session = _session_factory()
    db = Session()
    user = User(id=USER_ID, email="tv@example.com", password_hash=get_password_hash("OldPass1"), role="tutor", is_active=True, token_version=0)
    db.add(user)
    db.add(PasswordResetCode(
        id="rc-1", user_id=USER_ID, code_hash=_hash_reset_code("123456"),
        expires_at=datetime.utcnow() + timedelta(minutes=15), attempts=0,
    ))
    db.commit()

    reset_password(ResetPasswordRequest(email="tv@example.com", code="123456", new_password="NewPass2"), db)

    db.refresh(user)
    assert user.token_version == 1


# --- enforcement no get_current_user (integração) ------------------------------

@pytest.fixture()
def client():
    Session = _session_factory()
    db = Session()
    db.add(User(id=USER_ID, email="tv@example.com", password_hash="h", role="tutor", is_active=True, token_version=0))
    db.commit()
    db.close()

    def _override_get_db():
        d = Session()
        try:
            yield d
        finally:
            d.close()

    test_app = FastAPI()

    @test_app.get("/me")
    def me(user: User = Depends(get_current_user)):
        return {"id": user.id, "ver": user.token_version}

    test_app.dependency_overrides[get_db] = _override_get_db
    test_app.state._session = Session  # exposto p/ o teste mexer no token_version
    yield TestClient(test_app)
    test_app.dependency_overrides.clear()


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def test_token_with_matching_version_authenticates(client):
    token = create_access_token(USER_ID, {"ver": 0})
    resp = client.get("/me", headers=_auth(token))
    assert resp.status_code == 200


def test_legacy_token_without_version_still_authenticates(client):
    # Sem "ver" -> token legado -> aceito na transicao (mesmo com user.token_version=0).
    token = create_access_token(USER_ID)  # sem ver
    resp = client.get("/me", headers=_auth(token))
    assert resp.status_code == 200


def test_token_with_stale_version_is_revoked(client):
    token = create_access_token(USER_ID, {"ver": 0})
    assert client.get("/me", headers=_auth(token)).status_code == 200
    # Bump do token_version (simula troca de senha / logout-everywhere).
    db = client.app.state._session()
    db.query(User).filter(User.id == USER_ID).update({"token_version": 1})
    db.commit()
    db.close()
    # O token antigo (ver=0) deve ser REVOGADO.
    assert client.get("/me", headers=_auth(token)).status_code == 401
