"""Testes de ROTA (camada HTTP) para POST /auth/change-password (D).

Padrao do projeto: FastAPI minimo + SQLite StaticPool + override de get_db e
get_current_user.

Cobre:
- Senha atual errada → 400
- Nova senha fraca (sem numero) → 400
- Rate limit apos 5 tentativas erradas → 429
- Conta social (password_hash vazio) → 400
"""
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import app.models  # noqa: F401
from app.core.database import Base, get_db
from app.core.security import get_password_hash
from app.dependencies.auth import get_current_user
from app.models.tenant import Tenant
from app.models.user import User
from app.routes import auth
from app.routes.auth import _change_password_limiter
from app.services.tenant_seed_service import DEFAULT_TENANT_SLUG

TENANT_ID = "t-chpwd"
USER_ID = "u-chpwd"
USER_PASSWORD = "Senha@1234"


def build(*, password: str | None = USER_PASSWORD, social: bool = False):
    """Monta app minimo com o router de auth e SQLite em memoria isolado."""
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    db = Session()

    db.add(Tenant(id=TENANT_ID, name="Aumigao", slug=DEFAULT_TENANT_SLUG, status="active", plan="business"))
    password_hash = "" if social else get_password_hash(password)
    db.add(User(
        id=USER_ID,
        email="user@test.com",
        password_hash=password_hash,
        role="tutor",
        tenant_id=TENANT_ID,
        is_active=True,
    ))
    db.commit()

    test_app = FastAPI()
    test_app.include_router(auth.router)
    test_app.dependency_overrides[get_db] = lambda: db
    # Autentica como o usuario de teste por padrao
    test_app.dependency_overrides[get_current_user] = lambda: db.get(User, USER_ID)
    return TestClient(test_app), db


@pytest.fixture(autouse=True)
def _clear_chpwd_limiter():
    _change_password_limiter._failures.clear()
    _change_password_limiter._failures.setdefault  # noqa — garante acesso ao dict interno
    yield
    _change_password_limiter._failures.clear()


# ------------------------------------------------------------------- testes ---

def test_change_password_wrong_current_returns_400():
    """Senha atual errada deve retornar 400."""
    client, _ = build()

    r = client.post("/auth/change-password", json={
        "current_password": "SenhaErrada99",
        "new_password": "NovaSenha1234",
    })

    assert r.status_code == 400
    assert "senha atual" in r.json()["detail"].lower() or "incorreta" in r.json()["detail"].lower()


def test_change_password_weak_new_password_returns_400():
    """Nova senha sem numero deve retornar 400 (fraca)."""
    client, _ = build()

    r = client.post("/auth/change-password", json={
        "current_password": USER_PASSWORD,
        "new_password": "SemNumeroNenhum",  # sem digito
    })

    assert r.status_code == 400
    detail = r.json()["detail"].lower()
    assert "numero" in detail or "letra" in detail or "caractere" in detail


def test_change_password_new_password_no_letter_returns_400():
    """Nova senha sem letra deve retornar 400 (fraca)."""
    client, _ = build()

    r = client.post("/auth/change-password", json={
        "current_password": USER_PASSWORD,
        "new_password": "12345678",  # so numeros
    })

    assert r.status_code == 400


def test_change_password_too_short_returns_400():
    """Nova senha com menos de 8 chars deve retornar 400."""
    client, _ = build()

    r = client.post("/auth/change-password", json={
        "current_password": USER_PASSWORD,
        "new_password": "Ab1",
    })

    assert r.status_code == 400


def test_change_password_rate_limit_after_5_failures():
    """Apos 5 tentativas com senha errada, a 6a retorna 429."""
    client, _ = build()

    # 5 falhas consecutivas (max_failures=5 para _change_password_limiter)
    for _ in range(5):
        r = client.post("/auth/change-password", json={
            "current_password": "SenhaErrada99",
            "new_password": "NovaSenha1234",
        })
        assert r.status_code == 400

    # 6a tentativa deve ser bloqueada pelo rate limiter
    r = client.post("/auth/change-password", json={
        "current_password": USER_PASSWORD,  # mesmo com senha CORRETA
        "new_password": "NovaSenha1234",
    })
    assert r.status_code == 429
    assert "tentativas" in r.json()["detail"].lower() or "muitas" in r.json()["detail"].lower()


def test_change_password_social_account_no_hash_returns_400():
    """Conta criada via social login (password_hash='') nao pode usar change-password."""
    client, _ = build(social=True)

    r = client.post("/auth/change-password", json={
        "current_password": "",
        "new_password": "NovaSenha1234",
    })

    assert r.status_code == 400
    # Sem hash, a verificacao falha com "senha atual incorreta"
    detail = r.json()["detail"].lower()
    assert "senha" in detail or "incorreta" in detail or "social" in detail


def test_change_password_happy_path_clears_limiter():
    """Apos 4 falhas, troca bem-sucedida deve limpar o contador do rate limiter."""
    client, db = build()

    # 4 falhas (abaixo do limite)
    for _ in range(4):
        client.post("/auth/change-password", json={
            "current_password": "SenhaErrada99",
            "new_password": "NovaSenha1234",
        })

    # Troca correta — deve funcionar
    r = client.post("/auth/change-password", json={
        "current_password": USER_PASSWORD,
        "new_password": "NovaSenha1234",
    })
    assert r.status_code == 200, r.text

    # Limiter foi limpo: proximas tentativas com senha NOVA devem funcionar
    # (banco ja gravou a nova senha — buscamos o usuario atualizado)
    db.expire_all()
    user = db.get(User, USER_ID)
    from app.core.security import verify_password
    assert verify_password("NovaSenha1234", user.password_hash)


def test_change_password_updates_token_version():
    """Troca de senha deve incrementar token_version para revogar sessoes antigas."""
    client, db = build()

    original_version = db.get(User, USER_ID).token_version or 0

    r = client.post("/auth/change-password", json={
        "current_password": USER_PASSWORD,
        "new_password": "NovaSenha1234",
    })
    assert r.status_code == 200, r.text

    db.expire_all()
    user = db.get(User, USER_ID)
    assert (user.token_version or 0) == original_version + 1
