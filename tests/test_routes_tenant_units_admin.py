"""Testes de ROTA para o CRUD self-service de unidades do tenant.

Padrão do projeto: FastAPI mínimo com SQLite em memória (StaticPool), override de
get_current_user (a raiz da cadeia RBAC). NÃO importa app.main.

Cobertura:
  - GET: lista + cap + used
  - POST: cria até o cap; 422 acima do cap; slug único com colisão
  - PATCH: renomeia (slug recalculado); desativa; re-ativa; re-ativa acima do cap → 422
  - 404 cross-tenant
  - RBAC 403 (permissão ausente — usuário sem papel com units.read/units.update)
"""
from __future__ import annotations

from datetime import datetime
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import app.models  # noqa: F401 — registra tabelas no Base.metadata
from app.core.database import Base, get_db
from app.dependencies.auth import get_current_user
from app.models.tenant import Tenant, TenantUnit
from app.models.user import User
from app.routes.tenant_units_admin import admin_api_router, admin_router
from app.services.tenant_seed_service import DEFAULT_TENANT_SLUG

# ── constantes ───────────────────────────────────────────────────────────────
TENANT_ID = "t-alpha"
OTHER_TENANT_ID = "t-beta"
ADMIN_ID = "admin-1"
OTHER_ADMIN_ID = "admin-2"


# ── helpers ──────────────────────────────────────────────────────────────────

def _make_user(db, *, user_id=ADMIN_ID, tenant_id=TENANT_ID, role="admin") -> User:
    user = User(
        id=user_id,
        email=f"{user_id}@test.com",
        password_hash="hashed",
        role=role,
        tenant_id=tenant_id,
    )
    db.add(user)
    return user


def _make_unit(tenant_id=TENANT_ID, name="Unidade Centro", status="active", uid=None) -> TenantUnit:
    return TenantUnit(
        id=uid or str(uuid4()),
        tenant_id=tenant_id,
        name=name,
        slug=None,
        status=status,
        created_at=datetime(2026, 1, 1),
    )


def build(
    *,
    plan: str = "business",
    units: list | None = None,
    extra_tenants: list | None = None,
    extra_units: list | None = None,
    user_role: str = "super_admin",  # super_admin bypassa RBAC
    user_tenant_id: str | None = TENANT_ID,
):
    """Monta app mínimo com SQLite em memória isolado.

    Usa super_admin por padrão (bypassa RBAC) para os testes funcionais.
    Para testes de RBAC 403 passe user_role='walker' (sem permissions no seed).
    """
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    db = Session()

    tenant = Tenant(
        id=TENANT_ID, name="Aumigao", slug=DEFAULT_TENANT_SLUG,
        status="active", plan=plan,
    )
    db.add(tenant)

    for t in extra_tenants or []:
        db.add(Tenant(**t))

    user = _make_user(db, role=user_role, tenant_id=user_tenant_id or TENANT_ID)
    # super_admin com _act_as_tenant_id para scope correto
    if user_role == "super_admin":
        user._act_as_tenant_id = TENANT_ID  # type: ignore[attr-defined]

    db.flush()
    for u in units or []:
        db.add(u)
    for u in extra_units or []:
        db.add(u)
    db.commit()

    test_app = FastAPI()
    test_app.include_router(admin_router)
    test_app.include_router(admin_api_router)
    test_app.dependency_overrides[get_db] = lambda: db
    # Sobrescreve a raiz de auth — require_permission usa get_current_user
    test_app.dependency_overrides[get_current_user] = lambda: user

    return TestClient(test_app), db, user


# ── GET /current/units ────────────────────────────────────────────────────────

def test_list_units_empty_returns_cap_and_used():
    client, _, _ = build(plan="business")
    r = client.get("/api/admin/tenants/current/units")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["units"] == []
    assert body["used"] == 0
    assert body["max_units"] is not None
    assert body["max_units"] >= 2


def test_list_units_counts_only_active_as_used():
    client, _, _ = build(
        plan="business",
        units=[
            _make_unit(name="Ativa"),
            _make_unit(name="Inativa", status="inactive"),
        ],
    )
    r = client.get("/api/admin/tenants/current/units")
    assert r.status_code == 200
    body = r.json()
    assert body["used"] == 1
    assert len(body["units"]) == 2


def test_list_units_slug_derived_from_name():
    client, _, _ = build(
        units=[_make_unit(name="Filial Sao Paulo")]
    )
    r = client.get("/admin/tenants/current/units")
    assert r.status_code == 200, r.text
    unit = r.json()["units"][0]
    assert unit["slug"]
    assert "filial" in unit["slug"]


# ── POST /current/units ───────────────────────────────────────────────────────

def test_create_unit_happy_path():
    client, _, _ = build(plan="business")
    r = client.post("/api/admin/tenants/current/units", json={"name": "Filial Norte"})
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["name"] == "Filial Norte"
    assert body["slug"] == "filial-norte"
    assert body["enabled"] is True


def test_create_unit_slug_collision_gets_suffix():
    client, _, _ = build(plan="business")
    r1 = client.post("/api/admin/tenants/current/units", json={"name": "Centro"})
    assert r1.status_code == 201, r1.text
    r2 = client.post("/api/admin/tenants/current/units", json={"name": "Centro"})
    assert r2.status_code == 201, r2.text
    slugs = {r1.json()["slug"], r2.json()["slug"]}
    assert "centro" in slugs
    assert any(s.startswith("centro-") for s in slugs)


def test_create_unit_blocked_when_at_cap():
    """business (legado) tem max_units_with_addon=3. Preenche o cap e verifica 422."""
    from app.services.tenant_plan_service import TENANT_PLAN_CAPABILITIES
    cap = TENANT_PLAN_CAPABILITIES.get("business", {}).get("max_units_with_addon", 3)

    units = [_make_unit(name=f"Unidade {i}", uid=f"u{i}") for i in range(cap)]
    client, _, _ = build(plan="business", units=units)

    r = client.post("/api/admin/tenants/current/units", json={"name": "Unidade Extra"})
    assert r.status_code == 422, r.text
    assert "plano" in r.json()["detail"].lower()


def test_create_unit_free_plan_blocked_at_1():
    """Plano free: max_units=1. A unidade principal já ocupa o cap."""
    units = [_make_unit(name="Principal", uid="u0")]
    client, _, _ = build(plan="free", units=units)
    r = client.post("/api/admin/tenants/current/units", json={"name": "Segunda"})
    assert r.status_code == 422, r.text
    assert "plano" in r.json()["detail"].lower()


def test_create_unit_enterprise_unlimited(monkeypatch):
    """Enterprise tem max_units=None → pode criar sem limite."""
    from app.services import tenant_plan_service
    orig = tenant_plan_service.get_tenant_capabilities

    def _mock_caps(tenant, db):
        caps = orig(tenant, db)
        caps["max_units"] = None
        caps["max_units_with_addon"] = None
        return caps

    monkeypatch.setattr(tenant_plan_service, "get_tenant_capabilities", _mock_caps)

    units = [_make_unit(name=f"U{i}", uid=f"u{i}") for i in range(5)]
    client, _, _ = build(plan="enterprise", units=units)
    r = client.post("/api/admin/tenants/current/units", json={"name": "Nova"})
    assert r.status_code == 201, r.text


def test_create_unit_rbac_403():
    """Usuário sem units.update (ex: role walker sem seed RBAC) deve receber 403."""
    client, _, _ = build(user_role="walker")
    r = client.post("/api/admin/tenants/current/units", json={"name": "Teste"})
    # walker não tem units.update → 403
    assert r.status_code == 403


# ── PATCH /current/units/{unit_id} ────────────────────────────────────────────

def test_patch_unit_rename():
    uid = str(uuid4())
    client, _, _ = build(units=[_make_unit(name="Antiga", uid=uid)])
    r = client.patch(f"/api/admin/tenants/current/units/{uid}", json={"name": "Nova Nome"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["name"] == "Nova Nome"
    assert body["slug"] == "nova-nome"


def test_patch_unit_disable():
    uid = str(uuid4())
    client, _, _ = build(units=[_make_unit(name="Ativa", uid=uid)])
    r = client.patch(f"/api/admin/tenants/current/units/{uid}", json={"enabled": False})
    assert r.status_code == 200, r.text
    assert r.json()["enabled"] is False


def test_patch_unit_reenable():
    uid = str(uuid4())
    client, _, _ = build(units=[_make_unit(name="Inativa", uid=uid, status="inactive")])
    r = client.patch(f"/api/admin/tenants/current/units/{uid}", json={"enabled": True})
    assert r.status_code == 200, r.text
    assert r.json()["enabled"] is True


def test_patch_unit_reenable_above_cap_raises_422():
    """Re-ativar quando já há unidades ativas no cap deve dar 422."""
    from app.services.tenant_plan_service import TENANT_PLAN_CAPABILITIES
    cap = TENANT_PLAN_CAPABILITIES.get("business", {}).get("max_units_with_addon", 3)

    active_units = [_make_unit(name=f"A{i}", uid=f"a{i}") for i in range(cap)]
    inactive_uid = str(uuid4())
    inactive = _make_unit(name="Desativada", uid=inactive_uid, status="inactive")

    client, _, _ = build(plan="business", units=active_units + [inactive])
    r = client.patch(f"/api/admin/tenants/current/units/{inactive_uid}", json={"enabled": True})
    assert r.status_code == 422, r.text
    assert "plano" in r.json()["detail"].lower()


def test_patch_unit_404_wrong_tenant():
    other_uid = str(uuid4())
    client, db, _ = build(
        extra_tenants=[dict(id=OTHER_TENANT_ID, name="Outro", slug="outro", status="active", plan="business")],
        extra_units=[_make_unit(tenant_id=OTHER_TENANT_ID, name="Deles", uid=other_uid)],
    )
    r = client.patch(f"/api/admin/tenants/current/units/{other_uid}", json={"name": "Hack"})
    assert r.status_code == 404


def test_patch_unit_rbac_403():
    """Usuário sem units.update recebe 403 no PATCH."""
    uid = str(uuid4())
    client, _, _ = build(units=[_make_unit(uid=uid)], user_role="walker")
    r = client.patch(f"/api/admin/tenants/current/units/{uid}", json={"name": "X"})
    assert r.status_code == 403


# ── par sem /api prefix ───────────────────────────────────────────────────────

def test_non_api_prefix_get_works():
    client, _, _ = build()
    r = client.get("/admin/tenants/current/units")
    assert r.status_code == 200, r.text


def test_non_api_prefix_post_works():
    client, _, _ = build(plan="business")
    r = client.post("/admin/tenants/current/units", json={"name": "Teste"})
    assert r.status_code == 201, r.text


def test_non_api_prefix_patch_works():
    uid = str(uuid4())
    client, _, _ = build(units=[_make_unit(uid=uid)])
    r = client.patch(f"/admin/tenants/current/units/{uid}", json={"enabled": False})
    assert r.status_code == 200, r.text
