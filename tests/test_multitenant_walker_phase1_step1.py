"""Testes — Fase 1 Passo 1: Passeador Multi-Tenant.

Cobre:
  1. tenant_tem_rede: lógica de override e plano/addon.
  2. Endpoint PATCH /admin/tenants/{id}/network-access (super_admin vs tenant admin).
  3. Regressão / no-op: flag default OFF; defaults das colunas novas.

Padrão do projeto: FastAPI mínimo, SQLite em memória (StaticPool),
dependency_overrides para get_db / get_current_user.
Segue estilo de test_act_as_tenant_security.py.
"""
from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import app.models  # noqa: F401 — registra todas as tabelas no Base.metadata
from app.core.database import Base, get_db
from app.dependencies.auth import get_current_user
from app.models.tenant import Tenant
from app.models.tenant_walker_access import TenantWalkerAccess
from app.models.walker_network_profile import WalkerNetworkProfile
from app.models.user import User
from app.routes import tenants as tenants_routes
from app.services.tenant_plan_service import tenant_tem_rede

# ─── IDs de fixture ───────────────────────────────────────────────────────────

SUPER_ADMIN_ID = "sa-1"
TENANT_ADMIN_ID = "ta-1"
T_STARTER = "tenant-starter"
T_BUSINESS = "tenant-business"
T_BUSINESS_ADDON = "tenant-business-addon"
T_ENTERPRISE = "tenant-enterprise"
T_OVERRIDE_ON = "tenant-override-on"
T_OVERRIDE_OFF = "tenant-override-off"


# ─── Banco em memória ─────────────────────────────────────────────────────────


def _build_db():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    db = Session()

    # Usuários
    db.add(User(id=SUPER_ADMIN_ID, email="sa@test.com", password_hash="x", role="super_admin"))
    db.add(User(id=TENANT_ADMIN_ID, email="ta@test.com", password_hash="x", role="admin", tenant_id=T_STARTER))

    # Tenants com variações de plano/addon/override
    db.add(Tenant(id=T_STARTER, name="Starter", slug="starter", status="active", plan="starter"))
    db.add(Tenant(id=T_BUSINESS, name="Business", slug="business", status="active", plan="business"))
    db.add(
        Tenant(
            id=T_BUSINESS_ADDON,
            name="BusinessAddon",
            slug="business-addon",
            status="active",
            plan="business",
            network_access_addon=True,
        )
    )
    db.add(Tenant(id=T_ENTERPRISE, name="Enterprise", slug="enterprise", status="active", plan="enterprise"))
    db.add(
        Tenant(
            id=T_OVERRIDE_ON,
            name="OverrideOn",
            slug="override-on",
            status="active",
            plan="starter",  # plano starter, mas override=True
            network_access_override=True,
        )
    )
    db.add(
        Tenant(
            id=T_OVERRIDE_OFF,
            name="OverrideOff",
            slug="override-off",
            status="active",
            plan="enterprise",  # plano enterprise, mas override=False
            network_access_override=False,
        )
    )

    db.commit()
    return db


def _build_app(db, *, user_id: str):
    test_app = FastAPI()
    test_app.include_router(tenants_routes.router)
    test_app.dependency_overrides[get_db] = lambda: db
    test_app.dependency_overrides[get_current_user] = lambda: db.get(User, user_id)
    return TestClient(test_app)


# ═══════════════════════════════════════════════════════════════════════════════
# 1. tenant_tem_rede — lógica de override e plano/addon
# ═══════════════════════════════════════════════════════════════════════════════


def test_rede_override_true_sobrepoe_plano():
    """Override=True num plano starter → tem_rede=True."""
    db = _build_db()
    tenant = db.get(Tenant, T_OVERRIDE_ON)
    assert tenant_tem_rede(tenant, db) is True


def test_rede_override_false_sobrepoe_plano():
    """Override=False num plano enterprise → tem_rede=False."""
    db = _build_db()
    tenant = db.get(Tenant, T_OVERRIDE_OFF)
    assert tenant_tem_rede(tenant, db) is False


def test_rede_enterprise_sem_override_true():
    """Enterprise sem override → tem_rede=True."""
    db = _build_db()
    tenant = db.get(Tenant, T_ENTERPRISE)
    assert tenant_tem_rede(tenant, db) is True


def test_rede_business_sem_addon_false():
    """Business sem addon → tem_rede=False (decisão 5 PRD)."""
    db = _build_db()
    tenant = db.get(Tenant, T_BUSINESS)
    assert tenant_tem_rede(tenant, db) is False


def test_rede_business_com_addon_true():
    """Business com addon=True → tem_rede=True."""
    db = _build_db()
    tenant = db.get(Tenant, T_BUSINESS_ADDON)
    assert tenant_tem_rede(tenant, db) is True


def test_rede_starter_false():
    """Starter sem override → tem_rede=False."""
    db = _build_db()
    tenant = db.get(Tenant, T_STARTER)
    assert tenant_tem_rede(tenant, db) is False


# ═══════════════════════════════════════════════════════════════════════════════
# 2. Endpoint PATCH /admin/tenants/{id}/network-access
# ═══════════════════════════════════════════════════════════════════════════════


def test_endpoint_super_admin_seta_override_true():
    """super_admin consegue setar override=True e o valor persiste."""
    db = _build_db()
    client = _build_app(db, user_id=SUPER_ADMIN_ID)

    r = client.patch(f"/admin/tenants/{T_STARTER}/network-access", json={"override": True})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["network_access_override"] is True
    assert body["tem_rede"] is True

    # Confirma persistência no banco
    tenant = db.get(Tenant, T_STARTER)
    db.refresh(tenant)
    assert tenant.network_access_override is True


def test_endpoint_super_admin_seta_override_false():
    """super_admin consegue setar override=False num tenant enterprise → tem_rede=False."""
    db = _build_db()
    client = _build_app(db, user_id=SUPER_ADMIN_ID)

    r = client.patch(f"/admin/tenants/{T_ENTERPRISE}/network-access", json={"override": False})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["network_access_override"] is False
    assert body["tem_rede"] is False


def test_endpoint_super_admin_limpa_override_com_null():
    """super_admin envia override=null → limpa o override; regra de plano volta."""
    db = _build_db()
    client = _build_app(db, user_id=SUPER_ADMIN_ID)

    # Primeiro seta override=False num enterprise
    client.patch(f"/admin/tenants/{T_ENTERPRISE}/network-access", json={"override": False})

    # Agora limpa com null
    r = client.patch(f"/admin/tenants/{T_ENTERPRISE}/network-access", json={"override": None})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["network_access_override"] is None
    # enterprise sem override → tem_rede=True
    assert body["tem_rede"] is True


def test_endpoint_tenant_admin_recebe_403():
    """Admin de tenant (não super_admin) tenta chamar o endpoint → 403."""
    db = _build_db()
    client = _build_app(db, user_id=TENANT_ADMIN_ID)

    r = client.patch(f"/admin/tenants/{T_STARTER}/network-access", json={"override": True})
    assert r.status_code == 403, r.text


# ═══════════════════════════════════════════════════════════════════════════════
# 3. Regressão / no-op
# ═══════════════════════════════════════════════════════════════════════════════


def test_flag_default_off():
    """MULTI_TENANT_WALKER deve ser False por padrão (sem env var setada)."""
    import os
    # Garante que a var não está no ambiente do teste
    os.environ.pop("MULTI_TENANT_WALKER", None)

    # Reimporta para avaliar com o ambiente limpo
    import importlib
    import app.core.feature_flags as ff_module
    importlib.reload(ff_module)

    assert ff_module.MULTI_TENANT_WALKER is False


def test_tenant_walker_access_defaults():
    """TenantWalkerAccess novo → requirements_met=True, initiated_by='tenant'."""
    db = _build_db()
    # Precisamos de um usuário walker para FK
    walker = User(id="walker-1", email="walker@test.com", password_hash="x", role="walker")
    db.add(walker)
    db.flush()

    access = TenantWalkerAccess(
        tenant_id=T_STARTER,
        walker_user_id="walker-1",
        access_type="shared_network",
        status="active",
    )
    db.add(access)
    db.commit()
    db.refresh(access)

    assert access.requirements_met is True
    assert access.initiated_by == "tenant"
    assert access.commission_percent is None


def test_tenant_novo_network_defaults():
    """Tenant novo (criado pelo ORM) → network_access_addon=False, override=None."""
    db = _build_db()
    tenant = Tenant(name="Novo", slug="novo-t", status="draft", plan="starter")
    db.add(tenant)
    db.commit()
    db.refresh(tenant)

    assert tenant.network_access_addon is False
    assert tenant.network_access_override is None


def test_walker_network_profile_exclusive_tenant_default():
    """WalkerNetworkProfile novo → exclusive_tenant_id=None."""
    db = _build_db()
    walker = User(id="walker-2", email="w2@test.com", password_hash="x", role="walker")
    db.add(walker)
    db.flush()

    profile = WalkerNetworkProfile(walker_user_id="walker-2")
    db.add(profile)
    db.commit()
    db.refresh(profile)

    assert profile.exclusive_tenant_id is None


def test_enforce_network_access_usa_tenant_tem_rede():
    """enforce_network_access_allowed levanta 403 quando tenant_tem_rede=False."""
    from fastapi import HTTPException
    from app.services.tenant_plan_service import enforce_network_access_allowed

    db = _build_db()
    # Business sem addon → tem_rede=False → deve levantar 403
    tenant = db.get(Tenant, T_BUSINESS)
    with pytest.raises(HTTPException) as exc_info:
        enforce_network_access_allowed(tenant, db)
    assert exc_info.value.status_code == 403


def test_enforce_network_access_ok_quando_tem_rede():
    """enforce_network_access_allowed não levanta para enterprise (tem_rede=True)."""
    from app.services.tenant_plan_service import enforce_network_access_allowed

    db = _build_db()
    tenant = db.get(Tenant, T_ENTERPRISE)
    # Não deve levantar
    enforce_network_access_allowed(tenant, db)
