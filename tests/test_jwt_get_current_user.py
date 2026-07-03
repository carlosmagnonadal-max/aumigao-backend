"""B-ALT-011 (passo 2a) — enforcement no get_current_user (integração).

Diferente dos testes de scope (que dão override em get_current_user), aqui exercemos o
get_current_user REAL — só damos override em get_db. Garante que:
  - token recém-emitido (agora COM aud) autentica (o decode antigo, jwt.decode sem
    audience, REJEITAVA token com aud — esta é a quebra que o passo 2a conserta);
  - token legado (sem aud/iss) continua autenticando (retrocompat);
  - token com aud de outro serviço é REJEITADO (401).
"""
from datetime import datetime, timedelta, timezone

import jwt
import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import app.models  # noqa: F401 — registra as tabelas
from app.core.database import Base, get_db
from app.core.security import ALGORITHM, JWT_AUDIENCE, SECRET_KEY, create_access_token
from app.dependencies.auth import get_current_user
from app.models.user import User

USER_ID = "user-jwt-1"


@pytest.fixture()
def client():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    TestingSession = sessionmaker(bind=engine, autoflush=False, autocommit=False)

    session = TestingSession()
    session.add(User(id=USER_ID, email="jwt@example.com", password_hash="hash", role="cliente", is_active=True))
    session.commit()
    session.close()

    def _override_get_db():
        db = TestingSession()
        try:
            yield db
        finally:
            db.close()

    test_app = FastAPI()

    @test_app.get("/me")
    def me(user: User = Depends(get_current_user)):
        return {"id": user.id}

    test_app.dependency_overrides[get_db] = _override_get_db
    yield TestClient(test_app)
    test_app.dependency_overrides.clear()


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def test_fresh_token_with_aud_authenticates(client):
    token = create_access_token(USER_ID)
    resp = client.get("/me", headers=_auth(token))
    assert resp.status_code == 200
    assert resp.json()["id"] == USER_ID


def test_legacy_token_without_aud_still_authenticates(client):
    legacy = jwt.encode(
        {"sub": USER_ID, "exp": datetime.now(timezone.utc) + timedelta(hours=1)},
        SECRET_KEY, algorithm=ALGORITHM,
    )
    resp = client.get("/me", headers=_auth(legacy))
    assert resp.status_code == 200
    assert resp.json()["id"] == USER_ID


def test_token_with_foreign_audience_is_rejected(client):
    foreign = jwt.encode(
        {"sub": USER_ID, "aud": "outro-servico",
         "exp": datetime.now(timezone.utc) + timedelta(hours=1)},
        SECRET_KEY, algorithm=ALGORITHM,
    )
    resp = client.get("/me", headers=_auth(foreign))
    assert resp.status_code == 401


def test_user_guc_is_set_before_lookup(monkeypatch):
    """HOTFIX identidade global (migration 0091): o GUC app.current_user_id precisa
    estar publicado ANTES do db.get(User, ...), senão o after_begin injeta o default
    '-' e a policy self-identity da tabela `users` não casa a própria linha sob escopo
    de outro tenant. Aqui provamos a ORDEM: set_session_user(user_id) roda antes do
    lookup, com o user_id vindo do token.

    (RLS não é enforced em SQLite; este teste blinda a ordem de chamadas, que é o que
     torna a policy 0091 efetiva em Postgres — coberto de ponta a ponta em tests/pg_rls.)
    """
    import app.dependencies.auth as auth_mod

    calls: list[tuple[str, str]] = []

    class _FakeUser:
        id = USER_ID
        is_active = True
        token_version = 0
        role = "cliente"

    class _FakeDB:
        def get(self, model, pk):
            calls.append(("lookup", str(pk)))
            return _FakeUser()

    def _fake_set_session_user(db, user_id):
        calls.append(("set_user_guc", str(user_id)))

    monkeypatch.setattr(auth_mod, "set_session_user", _fake_set_session_user)

    token = create_access_token(USER_ID)
    creds = type("C", (), {"credentials": token})()
    user = auth_mod.get_current_user(credentials=creds, db=_FakeDB(), x_act_as_tenant=None)

    assert user.id == USER_ID
    # A publicação do GUC do usuário DEVE preceder o lookup da linha.
    kinds = [c[0] for c in calls]
    assert kinds.index("set_user_guc") < kinds.index("lookup"), (
        f"GUC do usuário não foi setado antes do lookup: {calls}"
    )
    # E com o user_id correto (vindo do sub do token).
    first_set = next(c for c in calls if c[0] == "set_user_guc")
    assert first_set[1] == USER_ID
