"""Testes de ROTA para POST /auth/link-apple (vinculação explícita de Apple ID).

Padrão do projeto (espelha test_social_login.py): FastAPI mínimo com router de auth,
SQLite StaticPool, override de get_db e de get_current_user (usuário autenticado).
_decode_apple_jwt_payload é mockado via monkeypatch para evitar rede.

Cobre:
- sucesso: vincula apple_sub e retorna {apple_linked: true}
- idempotente: mesma Apple ID já vinculada → 200
- 409 apple_ja_vinculada: conta já tem OUTRA Apple ID
- 409 apple_em_uso: sub já pertence a outro usuário
- 409 apple_em_uso via corrida: IntegrityError no commit
- 401: token Apple inválido (propagado de _decode_apple_jwt_payload)
- sem auth → 401
- perfil /auth/me expõe apple_linked (true/false), nunca apple_sub
"""
import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import app.models  # noqa: F401 – registra todas as tabelas
from app.core.database import Base, get_db
from app.dependencies.auth import get_current_user
from app.models.tenant import Tenant
from app.models.user import User
from app.routes import auth
from app.services.tenant_seed_service import DEFAULT_TENANT_SLUG

TENANT_ID = "t-link-apple"


def build(*, auth_user_id: str | None = None, extra_users: list[dict] | None = None):
    """Monta app mínimo com router de auth e SQLite isolado.

    Se auth_user_id for dado, override de get_current_user devolve esse usuário
    (busca no MESMO db para que commits funcionem). Sem auth_user_id, a rota exige
    Bearer e devolve 401.
    """
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    db = Session()

    db.add(Tenant(id=TENANT_ID, name="Aumigao", slug=DEFAULT_TENANT_SLUG, status="active", plan="business"))
    for u in extra_users or []:
        db.add(User(**u))
    db.commit()

    test_app = FastAPI()
    test_app.include_router(auth.router)
    test_app.dependency_overrides[get_db] = lambda: db
    if auth_user_id is not None:
        test_app.dependency_overrides[get_current_user] = lambda: db.get(User, auth_user_id)
    return TestClient(test_app), db


def _mock_apple(sub: str):
    """Imita _decode_apple_jwt_payload com sub fixo (token válido)."""
    def _decode(token: str):
        return {"sub": sub}
    return _decode


def _mock_apple_invalid():
    """Imita _decode_apple_jwt_payload rejeitando o token (401)."""
    def _decode(token: str):
        raise HTTPException(status_code=401, detail="Token Apple expirado.")
    return _decode


def _user(uid: str, email: str, **kw):
    return dict(id=uid, email=email, password_hash="hash", role="tutor",
                tenant_id=TENANT_ID, is_active=True, **kw)


# ------------------------------------------------------------------ testes ---

def test_link_apple_success_sets_sub(monkeypatch):
    """Conta sem apple_sub vincula com sucesso e persiste o sub."""
    monkeypatch.setattr(auth, "_decode_apple_jwt_payload", _mock_apple("sub-novo"))
    client, db = build(auth_user_id="u1", extra_users=[_user("u1", "a@x.com")])

    r = client.post("/auth/link-apple", json={"token": "apple-tok"})

    assert r.status_code == 200, r.text
    assert r.json() == {"apple_linked": True}
    db.expire_all()
    assert db.get(User, "u1").apple_sub == "sub-novo"


def test_link_apple_idempotent_same_sub(monkeypatch):
    """Token com a MESMA Apple ID já vinculada retorna 200 sem alterar nada."""
    monkeypatch.setattr(auth, "_decode_apple_jwt_payload", _mock_apple("sub-igual"))
    client, db = build(auth_user_id="u1", extra_users=[_user("u1", "a@x.com", apple_sub="sub-igual")])

    r = client.post("/auth/link-apple", json={"token": "apple-tok"})

    assert r.status_code == 200, r.text
    assert r.json() == {"apple_linked": True}
    db.expire_all()
    assert db.get(User, "u1").apple_sub == "sub-igual"


def test_link_apple_conflict_already_linked_other_apple(monkeypatch):
    """Conta já vinculada a OUTRA Apple ID → 409 apple_ja_vinculada, sem sobrescrever."""
    monkeypatch.setattr(auth, "_decode_apple_jwt_payload", _mock_apple("sub-outro"))
    client, db = build(auth_user_id="u1", extra_users=[_user("u1", "a@x.com", apple_sub="sub-atual")])

    r = client.post("/auth/link-apple", json={"token": "apple-tok"})

    assert r.status_code == 409, r.text
    assert r.json()["detail"]["code"] == "apple_ja_vinculada"
    db.expire_all()
    assert db.get(User, "u1").apple_sub == "sub-atual"  # inalterado


def test_link_apple_conflict_sub_in_use_by_another_user(monkeypatch):
    """Sub já pertence a OUTRO usuário → 409 apple_em_uso; NÃO desvincula o outro."""
    monkeypatch.setattr(auth, "_decode_apple_jwt_payload", _mock_apple("sub-da-outra"))
    client, db = build(
        auth_user_id="u1",
        extra_users=[
            _user("u1", "a@x.com"),
            _user("u2", "b@x.com", apple_sub="sub-da-outra"),
        ],
    )

    r = client.post("/auth/link-apple", json={"token": "apple-tok"})

    assert r.status_code == 409, r.text
    assert r.json()["detail"]["code"] == "apple_em_uso"
    db.expire_all()
    assert db.get(User, "u1").apple_sub is None          # não vinculou
    assert db.get(User, "u2").apple_sub == "sub-da-outra"  # outro intacto


def test_link_apple_race_integrityerror_maps_to_apple_em_uso(monkeypatch):
    """Corrida: commit levanta IntegrityError (UNIQUE) → 409 apple_em_uso."""
    monkeypatch.setattr(auth, "_decode_apple_jwt_payload", _mock_apple("sub-corrida"))
    client, db = build(auth_user_id="u1", extra_users=[_user("u1", "a@x.com")])

    # Força o commit a falhar como se outro request tivesse gravado o mesmo sub
    # entre o SELECT e o commit (a checagem prévia não encontrou o conflito).
    def _boom():
        raise IntegrityError("INSERT", {}, Exception("UNIQUE constraint failed: users.apple_sub"))
    monkeypatch.setattr(db, "commit", _boom)

    r = client.post("/auth/link-apple", json={"token": "apple-tok"})

    assert r.status_code == 409, r.text
    assert r.json()["detail"]["code"] == "apple_em_uso"


def test_link_apple_invalid_token_returns_401(monkeypatch):
    """Token Apple inválido/expirado → 401 propagado de _decode_apple_jwt_payload."""
    monkeypatch.setattr(auth, "_decode_apple_jwt_payload", _mock_apple_invalid())
    client, db = build(auth_user_id="u1", extra_users=[_user("u1", "a@x.com")])

    r = client.post("/auth/link-apple", json={"token": "token-lixo"})

    assert r.status_code == 401, r.text
    db.expire_all()
    assert db.get(User, "u1").apple_sub is None  # nada gravado


def test_link_apple_without_auth_returns_401():
    """Sem Bearer (get_current_user real, sem override) → 401 não autenticado."""
    client, _ = build()  # sem auth_user_id → dependência real exige credenciais

    r = client.post("/auth/link-apple", json={"token": "apple-tok"})

    assert r.status_code == 401, r.text


# ------------------------------------------------------ perfil apple_linked ---

def test_me_exposes_apple_linked_true_and_hides_sub(monkeypatch):
    """GET /auth/me expõe apple_linked=true quando há apple_sub, sem vazar o sub."""
    client, _ = build(auth_user_id="u1", extra_users=[_user("u1", "a@x.com", apple_sub="s")])

    r = client.get("/auth/me")

    assert r.status_code == 200, r.text
    body = r.json()
    assert body["apple_linked"] is True
    assert "apple_sub" not in body


def test_me_apple_linked_false_when_no_sub():
    """GET /auth/me → apple_linked=false quando a conta não tem Apple ID."""
    client, _ = build(auth_user_id="u1", extra_users=[_user("u1", "a@x.com")])

    r = client.get("/auth/me")

    assert r.status_code == 200, r.text
    assert r.json()["apple_linked"] is False
