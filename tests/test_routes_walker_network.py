"""Testes de ROTA (camada HTTP) do modulo app/routes/walker_network.py.

Cobre o wiring real das rotas admin da Rede de Passeadores:
- listagem de perfis de rede e de acessos por tenant
- vinculo de passeador a tenant (POST) com gating por network_access (403 quando
  o plano do tenant nao libera a Rede Aumigao)
- update de acesso (PATCH) + validacoes de access_type/status
- 403 de permissao (router depende de require_permission("walkers.read"))

Padrao igual a tests/test_routes_onda1.py: monta FastAPI minimo (NUNCA app.main, que
conecta no Neon), SQLite em memoria com StaticPool, overrides de get_db e
get_current_user. O 403 de "feature off" vem do plano do tenant (network_access_available
e False no plano starter, True no business) — ver tenant_plan_service.

Nota sobre 401: get_current_user esta sempre sobrescrito (nao da pra exercitar o
fluxo de token ausente neste app minimo); o caso "sem permissao" e coberto via
usuario sem RBAC -> 403.
"""
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import app.models  # noqa: F401  — registra todas as tabelas no Base.metadata
from app.core.database import Base, get_db
from app.dependencies.auth import get_current_user
from app.models.tenant import Tenant
from app.models.tenant_walker_access import TenantWalkerAccess
from app.models.user import User
from app.routes import walker_network

ADMIN_ID = "admin-test"
WALKER_ID = "walker-test"


def build(*, plan: str = "business", admin_role: str = "super_admin"):
    """SQLite em memoria + FastAPI minimo so com o router de walker_network.

    admin_role=super_admin passa em require_permission (rede de seguranca do RBAC).
    plan controla network_access_available (business=True, starter=False).
    """
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    db = Session()

    # Fase 1 Passo 1 (decisão 5 PRD): business agora exige network_access_addon=True
    # para ter acesso à rede. Nos testes com plan="business" o addon é ligado para
    # exercitar o caminho "rede habilitada"; plan="starter" continua sem addon.
    addon = plan in {"business", "enterprise"}
    db.add(Tenant(id="t-test", name="Aumigao", slug="aumigao", status="active", plan=plan, network_access_addon=addon))
    db.add(User(id=ADMIN_ID, email="admin@test.com", password_hash="x", role=admin_role, tenant_id="t-test"))
    db.add(User(id=WALKER_ID, email="walker@test.com", password_hash="x", role="walker", tenant_id="t-test"))
    db.commit()

    test_app = FastAPI()
    test_app.include_router(walker_network.router)
    test_app.dependency_overrides[get_db] = lambda: db
    test_app.dependency_overrides[get_current_user] = lambda: db.get(User, ADMIN_ID)
    return TestClient(test_app), db


# ----- listagem (happy path) -----
def test_list_walker_network_empty():
    client, _ = build()
    r = client.get("/admin/walker-network")
    assert r.status_code == 200, r.text
    assert r.json() == []


def test_list_tenant_walkers_empty():
    client, _ = build()
    r = client.get("/admin/walker-network/tenants/t-test")
    assert r.status_code == 200, r.text
    assert r.json() == []


def test_list_tenant_walkers_404_unknown_tenant():
    client, _ = build()
    r = client.get("/admin/walker-network/tenants/nope")
    assert r.status_code == 404


# ----- gating por network_access -----
def test_link_walker_blocked_when_plan_off():
    # plano starter => network_access_available False => 403
    client, _ = build(plan="starter")
    r = client.post(
        "/admin/walker-network/tenants/t-test",
        json={"walker_user_id": WALKER_ID, "access_type": "shared_network", "status": "active"},
    )
    assert r.status_code == 403, r.text
    assert "Rede" in r.json()["detail"]


def test_link_walker_happy_path_when_plan_on():
    client, db = build(plan="business")
    r = client.post(
        "/admin/walker-network/tenants/t-test",
        json={"walker_user_id": WALKER_ID, "access_type": "shared_network", "status": "active"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["tenant_id"] == "t-test"
    assert body["walker_user_id"] == WALKER_ID
    assert body["access_type"] == "shared_network"
    assert body["status"] == "active"
    # perfil de rede criado automaticamente
    profiles = client.get("/admin/walker-network").json()
    assert len(profiles) == 1
    assert profiles[0]["walker_user_id"] == WALKER_ID
    # aparece na listagem do tenant
    tenant_list = client.get("/admin/walker-network/tenants/t-test").json()
    assert len(tenant_list) == 1


# ----- validacoes do link -----
def test_link_walker_404_unknown_tenant():
    client, _ = build()
    r = client.post(
        "/admin/walker-network/tenants/nope",
        json={"walker_user_id": WALKER_ID},
    )
    assert r.status_code == 404


def test_link_walker_404_when_not_a_walker():
    client, db = build()
    # usuario existe mas role != walker
    db.add(User(id="cliente-x", email="c@test.com", password_hash="x", role="cliente", tenant_id="t-test"))
    db.commit()
    r = client.post(
        "/admin/walker-network/tenants/t-test",
        json={"walker_user_id": "cliente-x"},
    )
    assert r.status_code == 404


def test_link_walker_400_invalid_access_type():
    client, _ = build()
    r = client.post(
        "/admin/walker-network/tenants/t-test",
        json={"walker_user_id": WALKER_ID, "access_type": "lixo"},
    )
    assert r.status_code == 400
    assert "access_type" in r.json()["detail"]


def test_link_walker_400_invalid_status():
    client, _ = build()
    r = client.post(
        "/admin/walker-network/tenants/t-test",
        json={"walker_user_id": WALKER_ID, "status": "lixo"},
    )
    assert r.status_code == 400
    assert "status" in r.json()["detail"]


def test_link_walker_upsert_updates_existing():
    client, db = build()
    payload = {"walker_user_id": WALKER_ID, "access_type": "shared_network", "status": "active"}
    first = client.post("/admin/walker-network/tenants/t-test", json=payload).json()
    payload2 = {"walker_user_id": WALKER_ID, "access_type": "tenant_exclusive", "status": "paused"}
    second = client.post("/admin/walker-network/tenants/t-test", json=payload2).json()
    assert first["id"] == second["id"]  # mesmo registro (upsert pelo unique)
    assert second["access_type"] == "tenant_exclusive"
    assert second["status"] == "paused"
    # nao duplicou
    assert len(client.get("/admin/walker-network/tenants/t-test").json()) == 1


# ----- PATCH update -----
def test_update_access_happy_path():
    client, db = build()
    client.post(
        "/admin/walker-network/tenants/t-test",
        json={"walker_user_id": WALKER_ID, "access_type": "shared_network", "status": "active"},
    )
    r = client.patch(
        f"/admin/walker-network/tenants/t-test/walkers/{WALKER_ID}",
        json={"status": "revoked"},
    )
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "revoked"
    assert r.json()["access_type"] == "shared_network"  # inalterado


def test_update_access_404_when_no_link():
    client, _ = build()
    r = client.patch(
        f"/admin/walker-network/tenants/t-test/walkers/{WALKER_ID}",
        json={"status": "revoked"},
    )
    assert r.status_code == 404


def test_update_access_400_invalid_status():
    client, db = build()
    client.post(
        "/admin/walker-network/tenants/t-test",
        json={"walker_user_id": WALKER_ID},
    )
    r = client.patch(
        f"/admin/walker-network/tenants/t-test/walkers/{WALKER_ID}",
        json={"status": "lixo"},
    )
    assert r.status_code == 400


# ----- 403 de permissao (require_permission walkers.read) -----
def test_403_without_permission():
    # usuario sem role super_admin e sem RBAC => require_permission nega
    client, db = build(admin_role="cliente")
    assert client.get("/admin/walker-network").status_code == 403
    assert client.get("/admin/walker-network/tenants/t-test").status_code == 403
    r = client.post(
        "/admin/walker-network/tenants/t-test",
        json={"walker_user_id": WALKER_ID},
    )
    assert r.status_code == 403
