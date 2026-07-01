"""Testes do endpoint GET /api/admin/tenant-network/stats (Cunha ② — B2B2C seeding).

Usa SQLite in-memory. Verifica: contagem de active, exclusão de pending/revoked do
active, isolamento por tenant, e 400 para super_admin global sem tenant_id.

Auth nos testes: sobrescreve get_current_user (require_permission usa get_current_user
internamente; super_admin bypassa RBAC). get_admin_tenant_scope deriva o escopo do
tenant_id do usuário.
"""
from __future__ import annotations

import app.models  # noqa: F401 — registra todos os modelos no Base.metadata

from uuid import uuid4

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.core.database import Base, get_db
from app.dependencies.auth import get_current_user
from app.models.tenant import Tenant
from app.models.tenant_tutor_access import TenantTutorAccess
from app.models.user import User
from app.routes import admin as admin_routes

TENANT_A = "tenant-a"
TENANT_B = "tenant-b"


def _setup_db() -> Session:
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    Sm = sessionmaker(bind=engine)
    db = Sm()
    for tid, slug in [(TENANT_A, "slug-a"), (TENANT_B, "slug-b")]:
        db.add(Tenant(id=tid, name=tid, slug=slug, status="active", plan="business"))
    db.commit()
    return db


def _add_access(db: Session, tenant_id: str, status: str) -> None:
    db.add(
        TenantTutorAccess(
            id=str(uuid4()),
            tenant_id=tenant_id,
            tutor_user_id=str(uuid4()),
            status=status,
            initiated_by="tutor",
        )
    )
    db.commit()


def _client(db: Session, user: User) -> TestClient:
    app = FastAPI()
    app.include_router(admin_routes.api_router)
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_current_user] = lambda: user
    return TestClient(app)


def test_counts_active_and_pending_for_tenant_admin():
    db = _setup_db()
    _add_access(db, TENANT_A, "active")
    _add_access(db, TENANT_A, "active")
    _add_access(db, TENANT_A, "pending")
    _add_access(db, TENANT_A, "revoked")  # ignorado em ambos
    admin = User(id="admin-a", email="a@x.com", password_hash="x", role="super_admin", tenant_id=TENANT_A)
    admin._act_as_tenant_id = TENANT_A  # simula super_admin operando como tenant-A
    client = _client(db, admin)

    resp = client.get("/api/admin/tenant-network/stats")

    assert resp.status_code == 200
    assert resp.json() == {"active_tutors": 2, "pending_tutors": 1}


def test_isolates_by_tenant():
    db = _setup_db()
    _add_access(db, TENANT_A, "active")
    _add_access(db, TENANT_B, "active")
    _add_access(db, TENANT_B, "active")
    admin = User(id="admin-a", email="a@x.com", password_hash="x", role="super_admin", tenant_id=TENANT_A)
    admin._act_as_tenant_id = TENANT_A  # simula super_admin operando como tenant-A
    client = _client(db, admin)

    resp = client.get("/api/admin/tenant-network/stats")

    assert resp.status_code == 200
    assert resp.json()["active_tutors"] == 1  # não conta os 2 do tenant B


def test_super_admin_global_without_tenant_id_returns_400():
    db = _setup_db()
    _add_access(db, TENANT_A, "active")
    admin = User(id="super-1", email="s@x.com", password_hash="x", role="super_admin")  # sem tenant_id
    client = _client(db, admin)

    resp = client.get("/api/admin/tenant-network/stats")

    assert resp.status_code == 400


def test_super_admin_global_with_tenant_id_query_counts():
    db = _setup_db()
    _add_access(db, TENANT_A, "active")
    _add_access(db, TENANT_A, "active")
    admin = User(id="super-1", email="s@x.com", password_hash="x", role="super_admin")  # sem tenant_id
    client = _client(db, admin)

    resp = client.get(f"/api/admin/tenant-network/stats?tenant_id={TENANT_A}")

    assert resp.status_code == 200
    assert resp.json()["active_tutors"] == 2
