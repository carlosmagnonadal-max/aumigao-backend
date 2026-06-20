"""Testa POST /auth/logout — revogação real de sessão via token_version.

Padrão do projeto (ver test_routes_auth.py): FastAPI mínimo, SQLite em memória,
StaticPool, override de get_db. get_current_user REAL permanece ativo para
exercitar a revogação end-to-end.
"""
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import app.models  # noqa: F401 — registra todas as tabelas no Base.metadata
from app.core.database import Base, get_db
from app.core.security import create_access_token, get_password_hash
from app.models.tenant import Tenant
from app.models.user import User
from app.routes import auth
from app.services.tenant_seed_service import DEFAULT_TENANT_SLUG

TENANT_ID = "t-logout-test"
USER_ID = "u-logout-test"
USER_EMAIL = "logout@test.com"
USER_PASSWORD = "senha1234"


def _build():
    """Monta app mínimo com o router de auth + SQLite isolado + usuário pré-criado."""
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    db = Session()

    db.add(Tenant(id=TENANT_ID, name="Aumigao", slug=DEFAULT_TENANT_SLUG, status="active", plan="business"))
    db.add(User(
        id=USER_ID,
        email=USER_EMAIL,
        password_hash=get_password_hash(USER_PASSWORD),
        role="tutor",
        tenant_id=TENANT_ID,
        is_active=True,
        token_version=0,
    ))
    db.commit()

    test_app = FastAPI()
    test_app.include_router(auth.router)

    def _get_db_override():
        d = Session()
        try:
            yield d
        finally:
            d.close()

    test_app.dependency_overrides[get_db] = _get_db_override
    # Expõe a Session p/ os testes inspecionarem o DB após o logout.
    test_app.state._session = Session
    return TestClient(test_app)


def _bearer(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


# ---------------------------------------------------------------------------
# Cenário principal: login → logout → token antigo rejeitado
# ---------------------------------------------------------------------------

def test_logout_revokes_access_token():
    """Fluxo completo: login → POST /auth/logout → token antigo → 401."""
    client = _build()

    # 1. Login para obter access token válido.
    r_login = client.post("/auth/login", json={"email": USER_EMAIL, "password": USER_PASSWORD})
    assert r_login.status_code == 200, r_login.text
    access_token = r_login.json()["access_token"]

    # 2. Token funciona antes do logout.
    r_me_before = client.get("/auth/me", headers=_bearer(access_token))
    assert r_me_before.status_code == 200, r_me_before.text

    # 3. Logout — deve retornar 200 com {"ok": true}.
    r_logout = client.post("/auth/logout", headers=_bearer(access_token))
    assert r_logout.status_code == 200, r_logout.text
    assert r_logout.json().get("ok") is True

    # 4. Token antigo agora é rejeitado (ver desatualizado → 401).
    r_me_after = client.get("/auth/me", headers=_bearer(access_token))
    assert r_me_after.status_code == 401, (
        f"Token antigo deveria ser 401 após logout, mas foi {r_me_after.status_code}"
    )


def test_logout_increments_token_version_in_db():
    """Após logout, token_version do usuário no banco deve ser 1 (era 0)."""
    client = _build()
    Session = client.app.state._session

    r_login = client.post("/auth/login", json={"email": USER_EMAIL, "password": USER_PASSWORD})
    access_token = r_login.json()["access_token"]

    client.post("/auth/logout", headers=_bearer(access_token))

    db = Session()
    user = db.get(User, USER_ID)
    assert user.token_version == 1, f"Esperado token_version=1, obtido {user.token_version}"
    db.close()


def test_logout_without_token_returns_401():
    """Sem Authorization header, /auth/logout deve retornar 401."""
    client = _build()
    r = client.post("/auth/logout")
    assert r.status_code == 401
