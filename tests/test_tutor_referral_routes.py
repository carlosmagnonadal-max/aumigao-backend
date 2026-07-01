from __future__ import annotations

from contextlib import contextmanager
from unittest.mock import patch

import app.models  # noqa: F401

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.core.database import Base, get_db
from app.dependencies.auth import get_current_user
from app.models.tenant import Tenant
from app.models.user import User
from app.routes import tutor_referrals as routes


def _ctx():
    eng = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(eng)
    db = sessionmaker(bind=eng)()
    db.add(Tenant(id="t1", name="T1", slug="t1", status="active", plan="business"))
    db.add(User(id="u1", email="u1@x.com", password_hash="x", role="tutor", tenant_id="t1"))
    # Segundo tenant + tutor, para provar que o code resolve o tenant CERTO (convite cross-tenant).
    db.add(Tenant(id="t2", name="T2", slug="t2", status="active", plan="business"))
    db.add(User(id="u2", email="u2@x.com", password_hash="x", role="tutor", tenant_id="t2"))
    db.commit()
    return db


def _client(db, user):
    app = FastAPI()
    app.include_router(routes.api_router)
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_current_user] = lambda: user
    return TestClient(app)


@contextmanager
def _public_client(db):
    """Cliente para a rota publica validate-code (sem auth, escopo global).

    A rota abre um global_scope_session() proprio (como pet_share/live_share),
    entao mockamos esse helper para usar o DB de teste em vez de conexao real.
    O patch precisa continuar ativo DURANTE a requisicao — por isso o context
    manager (o patch nao pode ser desmontado antes do client.post).
    """
    @contextmanager
    def _mock_global_scope():
        db.info["rls_tenant"] = "*"
        yield db

    app = FastAPI()
    app.include_router(routes.api_router)
    with patch("app.routes.tutor_referrals.global_scope_session", _mock_global_scope):
        yield TestClient(app)


def test_create_returns_code():
    db = _ctx()
    c = _client(db, db.get(User, "u1"))
    r = c.post("/api/referrals/tutors")
    assert r.status_code == 200
    assert r.json()["referral_code"].startswith("TUT-")


def test_validate_code_public_resolves_tenant():
    """A validate-code (publica, sem auth) resolve o tenant CERTO pelo code do convite."""
    db = _ctx()
    # u1 (tenant t1) gera o code; o convidado que valida nao tem auth nem tenant proprio.
    code = _client(db, db.get(User, "u1")).post("/api/referrals/tutors").json()["referral_code"]

    with _public_client(db) as c:
        r = c.post("/api/referrals/tutors/validate-code", json={"code": code})
    assert r.status_code == 200
    body = r.json()
    # Resolve o tenant do REFERRER (t1), nao um tenant qualquer.
    assert body["tenant_id"] == "t1"
    assert body["tenant_slug"] == "t1"
    assert body["tenant_name"] == "T1"


def test_validate_code_only_exposes_marketing_fields():
    """O retorno expoe SO os campos de marketing — nada de PII/IDs internos."""
    db = _ctx()
    code = _client(db, db.get(User, "u1")).post("/api/referrals/tutors").json()["referral_code"]

    with _public_client(db) as c:
        body = c.post("/api/referrals/tutors/validate-code", json={"code": code}).json()
    assert set(body.keys()) == {"tenant_id", "tenant_name", "tenant_slug", "referrer_first_name"}


def test_validate_code_invalid_returns_404():
    db = _ctx()
    with _public_client(db) as c:
        r = c.post("/api/referrals/tutors/validate-code", json={"code": "TUT-NOPE-000000"})
    assert r.status_code == 404
