"""Testes de ROTA (camada HTTP) de isolamento por tenant em POST /matching/walkers (F).

Padrao do projeto: FastAPI minimo + SQLite StaticPool + overrides.

Cobre:
- Tutor do tenant A NAO ve walker do tenant B (isolamento cross-tenant via HTTP)
- Sem auth → 401
- GET /admin/matching/debug sem permissao matching.read → 403
"""
from datetime import datetime

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import app.models  # noqa: F401
from app.core.database import Base, get_db
from app.dependencies.auth import get_current_user
from app.models.tenant import Tenant
from app.models.tenant_walker_access import TenantWalkerAccess
from app.models.user import User
from app.models.walker_profile import WalkerProfile
from app.routes import matching
from app.services.tenant_seed_service import DEFAULT_TENANT_SLUG

TENANT_A = "t-tenant-a"
TENANT_B = "t-tenant-b"
TUTOR_A = "tutor-a"
TUTOR_B = "tutor-b"
WALKER_A = "walker-a"
WALKER_B = "walker-b"


def build_multitenant():
    """Monta app com 2 tenants, cada um com 1 tutor e 1 walker isolados."""
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    db = Session()

    db.add(Tenant(id=TENANT_A, name="TenantA", slug=DEFAULT_TENANT_SLUG, status="active", plan="business"))
    db.add(Tenant(id=TENANT_B, name="TenantB", slug="tenant-b", status="active", plan="business"))

    # Tutores
    db.add(User(id=TUTOR_A, email="tutor-a@test.com", password_hash="x",
                role="tutor", tenant_id=TENANT_A, is_active=True))
    db.add(User(id=TUTOR_B, email="tutor-b@test.com", password_hash="x",
                role="tutor", tenant_id=TENANT_B, is_active=True))

    # Walker do tenant A
    db.add(User(id=WALKER_A, email="walker-a@test.com", password_hash="x",
                role="walker", tenant_id=TENANT_A, is_active=True))
    db.add(WalkerProfile(
        id=f"wp-{WALKER_A}", user_id=WALKER_A, full_name="Walker A",
        status="active", active_as_walker=True, city="salvador",
        created_at=datetime.utcnow(),
    ))
    db.add(TenantWalkerAccess(id=f"twa-{WALKER_A}", tenant_id=TENANT_A,
                              walker_user_id=WALKER_A, status="active",
                              access_type="shared_network"))

    # Walker do tenant B
    db.add(User(id=WALKER_B, email="walker-b@test.com", password_hash="x",
                role="walker", tenant_id=TENANT_B, is_active=True))
    db.add(WalkerProfile(
        id=f"wp-{WALKER_B}", user_id=WALKER_B, full_name="Walker B",
        status="active", active_as_walker=True, city="salvador",
        created_at=datetime.utcnow(),
    ))
    db.add(TenantWalkerAccess(id=f"twa-{WALKER_B}", tenant_id=TENANT_B,
                              walker_user_id=WALKER_B, status="active",
                              access_type="shared_network"))

    db.commit()

    test_app = FastAPI()
    test_app.include_router(matching.router)
    test_app.include_router(matching.admin_router)
    test_app.dependency_overrides[get_db] = lambda: db
    return test_app, db


# ------------------------------------------- isolamento cross-tenant (F) ---

def test_tutor_a_does_not_see_walker_from_tenant_b():
    """Tutor do tenant A so deve enxergar walkers da rede do tenant A."""
    test_app, db = build_multitenant()
    # Autentica como tutor do tenant A
    test_app.dependency_overrides[get_current_user] = lambda: db.get(User, TUTOR_A)
    client = TestClient(test_app)

    r = client.post("/matching/walkers", json={"city": "salvador", "duration_minutes": 45})

    assert r.status_code == 200, r.text
    body = r.json()
    all_walkers = body["top_recommended"] + body["other_options"]
    ids_found = {w["walker_id"] for w in all_walkers}

    assert WALKER_A in ids_found, "Walker do proprio tenant deveria aparecer"
    assert WALKER_B not in ids_found, "Walker de outro tenant NAO deve aparecer (vazamento cross-tenant)"


def test_tutor_b_does_not_see_walker_from_tenant_a():
    """Tutor do tenant B so deve enxergar walkers da rede do tenant B."""
    test_app, db = build_multitenant()
    test_app.dependency_overrides[get_current_user] = lambda: db.get(User, TUTOR_B)
    client = TestClient(test_app)

    r = client.post("/matching/walkers", json={"city": "salvador", "duration_minutes": 45})

    assert r.status_code == 200, r.text
    body = r.json()
    all_walkers = body["top_recommended"] + body["other_options"]
    ids_found = {w["walker_id"] for w in all_walkers}

    assert WALKER_B in ids_found, "Walker do proprio tenant deveria aparecer"
    assert WALKER_A not in ids_found, "Walker de outro tenant NAO deve aparecer (vazamento cross-tenant)"


def test_match_walkers_requires_auth_returns_401():
    """Sem autenticacao, POST /matching/walkers deve retornar 401."""
    test_app, db = build_multitenant()
    # Remove qualquer override de autenticacao
    test_app.dependency_overrides.pop(get_current_user, None)
    client = TestClient(test_app)

    r = client.post("/matching/walkers", json={"city": "salvador", "duration_minutes": 45})

    assert r.status_code == 401


def test_matching_debug_requires_matching_read_permission_403():
    """GET /admin/matching/debug sem permissao matching.read deve retornar 403."""
    test_app, db = build_multitenant()
    # Autentica como tutor comum (sem permissao matching.read)
    test_app.dependency_overrides[get_current_user] = lambda: db.get(User, TUTOR_A)
    client = TestClient(test_app)

    r = client.get("/admin/matching/debug")

    assert r.status_code == 403
    assert "permiss" in r.json()["detail"].lower()


def test_matching_debug_allowed_for_super_admin():
    """GET /admin/matching/debug com super_admin (tem matching.read) deve retornar 200."""
    test_app, db = build_multitenant()
    # Adiciona super_admin
    db.add(User(id="super-admin", email="superadmin@test.com", password_hash="x",
                role="super_admin", tenant_id=TENANT_A, is_active=True))
    db.commit()
    test_app.dependency_overrides[get_current_user] = lambda: db.get(User, "super-admin")
    client = TestClient(test_app)

    r = client.get("/admin/matching/debug")

    assert r.status_code == 200, r.text
    assert "items" in r.json()
    assert "total_found" in r.json()
