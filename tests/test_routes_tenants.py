"""Testes de ROTA (camada HTTP) do modulo app/routes/tenants.py.

Cobre o wiring real do router admin de tenants: list/get/create/update, gating
de permissao (require_permission("tenants.read") no nivel do router), normalizacao
de slug, conflito de slug (409), validacao de status/plan invalidos e 404.

Monta um FastAPI minimo so com o router de tenants + overrides de get_db /
get_current_user (SQLite em memoria com StaticPool) — NAO importa app.main (que
conecta no banco de PROD).

Observacao sobre o FOCO ("resolver tenant por slug"): o modulo tenants.py NAO
expoe nenhum endpoint GET de resolucao por slug — slug so e usado na criacao
(normalizacao + checagem de unicidade). Testamos o comportamento real existente.
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
from app.models.user import User
from app.routes import tenants

ADMIN_ID = "admin-test"
PLAIN_ID = "plain-test"


def _seed(db):
    # super_admin passa em user_has_permission sem precisar de role assignments.
    db.add(User(id=ADMIN_ID, email="admin@test.com", password_hash="x", role="super_admin"))
    # usuario comum: sem permissao tenants.read -> deve receber 403.
    db.add(User(id=PLAIN_ID, email="plain@test.com", password_hash="x", role="cliente"))
    db.add(Tenant(id="t-1", name="Alpha", slug="alpha", status="active", plan="business"))
    db.add(Tenant(id="t-2", name="Beta", slug="beta", status="draft", plan="starter"))
    db.commit()


def build(*, authed: bool = True, user_id: str = ADMIN_ID):
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    db = Session()
    _seed(db)

    test_app = FastAPI()
    test_app.include_router(tenants.router)
    test_app.dependency_overrides[get_db] = lambda: db
    if authed:
        test_app.dependency_overrides[get_current_user] = lambda: db.get(User, user_id)
    # se authed=False: NAO sobrescreve get_current_user -> roda o HTTPBearer real
    # (sem Authorization header) que dispara 401.
    return TestClient(test_app), db


# ----- AUTH / RBAC gating -----
def test_list_requires_auth_401():
    client, _ = build(authed=False)
    r = client.get("/admin/tenants")
    assert r.status_code == 401


def test_get_requires_auth_401():
    client, _ = build(authed=False)
    r = client.get("/admin/tenants/t-1")
    assert r.status_code == 401


def test_list_forbidden_without_permission_403():
    client, _ = build(authed=True, user_id=PLAIN_ID)
    r = client.get("/admin/tenants")
    assert r.status_code == 403


def test_create_forbidden_without_permission_403():
    client, _ = build(authed=True, user_id=PLAIN_ID)
    r = client.post("/admin/tenants", json={"name": "Gamma", "slug": "gamma"})
    assert r.status_code == 403


# ----- LIST (happy path) -----
def test_list_returns_all_tenants():
    client, _ = build()
    r = client.get("/admin/tenants")
    assert r.status_code == 200, r.text
    body = r.json()
    assert {t["slug"] for t in body} == {"alpha", "beta"}


# ----- GET por id -----
def test_get_tenant_happy_path():
    client, _ = build()
    r = client.get("/admin/tenants/t-1")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["id"] == "t-1"
    assert body["slug"] == "alpha"
    # TenantDetailResponse expoe colecoes (vazias aqui).
    assert body["features"] == []
    assert body["units"] == []


def test_get_tenant_not_found_404():
    client, _ = build()
    r = client.get("/admin/tenants/does-not-exist")
    assert r.status_code == 404


# ----- CREATE -----
def test_create_tenant_happy_path_creates_defaults():
    client, db = build()
    r = client.post(
        "/admin/tenants",
        json={"name": "Gamma", "slug": "Gamma", "status": "active", "plan": "business"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    # slug normalizado para lowercase.
    assert body["slug"] == "gamma"
    assert body["name"] == "Gamma"
    # branding/settings default criados junto.
    created = db.query(Tenant).filter(Tenant.slug == "gamma").first()
    assert created is not None
    assert created.branding is not None
    assert created.settings is not None
    assert created.onboarding is not None


def test_create_tenant_normalizes_slug_whitespace():
    client, db = build()
    r = client.post("/admin/tenants", json={"name": "Delta", "slug": "  DELTA  "})
    assert r.status_code == 200, r.text
    assert r.json()["slug"] == "delta"


def test_create_tenant_duplicate_slug_409():
    client, _ = build()
    r = client.post("/admin/tenants", json={"name": "Alpha2", "slug": "alpha"})
    assert r.status_code == 409


def test_create_tenant_empty_slug_400():
    client, _ = build()
    r = client.post("/admin/tenants", json={"name": "NoSlug", "slug": "   "})
    assert r.status_code == 400


def test_create_tenant_invalid_status_400():
    client, _ = build()
    r = client.post(
        "/admin/tenants",
        json={"name": "BadStatus", "slug": "badstatus", "status": "bogus"},
    )
    assert r.status_code == 400


def test_create_tenant_invalid_plan_400():
    client, _ = build()
    r = client.post(
        "/admin/tenants",
        json={"name": "BadPlan", "slug": "badplan", "plan": "ultra"},
    )
    assert r.status_code == 400


# ----- UPDATE -----
def test_update_tenant_happy_path():
    client, db = build()
    r = client.patch("/admin/tenants/t-2", json={"name": "Beta Renamed", "status": "active"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["name"] == "Beta Renamed"
    assert body["status"] == "active"


def test_update_tenant_not_found_404():
    client, _ = build()
    r = client.patch("/admin/tenants/nope", json={"name": "x"})
    assert r.status_code == 404


def test_update_tenant_invalid_status_400():
    client, _ = build()
    r = client.patch("/admin/tenants/t-1", json={"status": "bogus"})
    assert r.status_code == 400


def test_update_tenant_invalid_plan_400():
    client, _ = build()
    r = client.patch("/admin/tenants/t-1", json={"plan": "ultra"})
    assert r.status_code == 400
