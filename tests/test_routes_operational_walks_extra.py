"""Testes de ROTA (camada HTTP) de app/routes/tenant_dedicated_app_readiness.py.

O modulo-alvo original (operational_walks.py) ja esta integralmente coberto por
tests/test_routes_operational_walks.py (start matching, accept/decline, rematch,
operational-status, admin metrics/logs e 401/403). Portanto, conforme o FOCO,
este arquivo cobre tenant_dedicated_app_readiness.py.

Endpoints cobertos (router publico, sem auth):
- GET /tenants/current/dedicated-app-readiness
- GET /tenants/{tenant_id}/dedicated-app-readiness

Cobre: tenant Starter (nao pronto), tenant Business com dedicated_app habilitado +
branding completo (pronto), fallback de tenant inexistente para o default, e
resposta serializada conforme TenantDedicatedAppReadinessResponse.

Segue o padrao de tests/test_routes_onda1.py: FastAPI MINIMO so com os routers do
modulo, SQLite em memoria (StaticPool + check_same_thread False),
Base.metadata.create_all e dependency_override de get_db. NUNCA importa app.main
(que conecta no banco de PROD).
"""
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import app.models  # noqa: F401 — registra todas as tabelas no Base.metadata
from app.core.database import Base, get_db
from app.models.tenant import Tenant, TenantBranding, TenantFeature
from app.routes import tenant_dedicated_app_readiness
from app.services.tenant_seed_service import DEFAULT_TENANT_SLUG

DEFAULT_TENANT_ID = "t-default"
BIZ_TENANT_ID = "t-biz"


def build(seed=None):
    """Monta o app de teste. `seed(db)` popula o banco antes de servir."""
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine)()

    if seed:
        seed(db)
        db.commit()

    test_app = FastAPI()
    test_app.include_router(tenant_dedicated_app_readiness.router)
    test_app.dependency_overrides[get_db] = lambda: db
    return TestClient(test_app, raise_server_exceptions=True), db


def _seed_default_starter(db):
    # slug = DEFAULT para get_default_tenant resolver este tenant (sem criar outro).
    db.add(
        Tenant(
            id=DEFAULT_TENANT_ID,
            name="Aumigao",
            slug=DEFAULT_TENANT_SLUG,
            status="active",
            plan="starter",
        )
    )


def _seed_default_plus_business_ready(db):
    _seed_default_starter(db)
    db.add(
        Tenant(
            id=BIZ_TENANT_ID,
            name="PetShop Premium",
            slug="petshop-premium",
            status="active",
            plan="business",
        )
    )
    # business permite dedicated_app no plano; a TenantFeature liga para o tenant.
    db.add(TenantFeature(tenant_id=BIZ_TENANT_ID, feature_key="dedicated_app", enabled=True))
    db.add(
        TenantBranding(
            tenant_id=BIZ_TENANT_ID,
            display_name="PetShop Premium",
            app_name="PetShop App",
            logo_url="https://cdn/logo.png",
            icon_url="https://cdn/icon.png",
            splash_image_url="https://cdn/splash.png",
            primary_color="#112233",
            secondary_color="#445566",
            powered_by_enabled=False,
        )
    )


# ---------- /current (resolve o tenant default) ----------
def test_current_readiness_starter_not_ready():
    client, _ = build(seed=_seed_default_starter)
    r = client.get("/tenants/current/dedicated-app-readiness")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["tenant_id"] == DEFAULT_TENANT_ID
    # plano starter: dedicated_app indisponivel -> nao habilitado e nao pronto.
    assert body["dedicated_app_enabled"] is False
    assert body["ready_for_dedicated_app"] is False
    assert "dedicated_app" in body["missing"]
    # sem branding cadastrado -> assets ausentes (defaults vazios).
    assert body["asset_readiness"]["logo_missing"] is True
    assert body["asset_readiness"]["icon_missing"] is True
    assert body["asset_readiness"]["splash_missing"] is True
    # features no shape do schema (4 chaves comerciais).
    assert set(body["features"].keys()) == {
        "network_access",
        "dedicated_app",
        "custom_products",
        "custom_projects",
    }
    assert body["features"]["dedicated_app"] is False


def test_current_readiness_response_shape():
    client, _ = build(seed=_seed_default_starter)
    body = client.get("/tenants/current/dedicated-app-readiness").json()
    # response_model TenantDedicatedAppReadinessResponse: campos obrigatorios presentes.
    for key in (
        "tenant_id",
        "ready_for_dedicated_app",
        "dedicated_app_enabled",
        "missing",
        "asset_readiness",
        "branding",
        "commercial",
        "features",
        "capabilities",
    ):
        assert key in body
    assert isinstance(body["missing"], list)
    # branding default vem com nome do tenant e cores nao vazias.
    assert body["branding"]["display_name"]
    assert body["branding"]["primary_color"]
    # commercial reflete o plano starter.
    assert body["commercial"]["plan"] == "starter"
    assert body["commercial"]["upgrade_available"] is True


# ---------- /{tenant_id} (resolve por id) ----------
def test_readiness_business_ready_by_id():
    client, _ = build(seed=_seed_default_plus_business_ready)
    r = client.get(f"/tenants/{BIZ_TENANT_ID}/dedicated-app-readiness")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["tenant_id"] == BIZ_TENANT_ID
    # business + TenantFeature ligada -> dedicated_app habilitado.
    assert body["features"]["dedicated_app"] is True
    assert body["dedicated_app_enabled"] is True
    # branding completo (nomes, cores e assets) -> pronto e sem pendencias.
    assert body["ready_for_dedicated_app"] is True
    assert body["missing"] == []
    assert body["asset_readiness"] == {
        "logo_missing": False,
        "icon_missing": False,
        "splash_missing": False,
    }
    assert body["branding"]["app_name"] == "PetShop App"
    assert body["commercial"]["plan"] == "business"


def test_readiness_business_starter_sibling_still_not_ready_by_id():
    # garante isolamento por tenant: o default starter continua nao-pronto.
    client, _ = build(seed=_seed_default_plus_business_ready)
    body = client.get(f"/tenants/{DEFAULT_TENANT_ID}/dedicated-app-readiness").json()
    assert body["tenant_id"] == DEFAULT_TENANT_ID
    assert body["ready_for_dedicated_app"] is False
    assert body["dedicated_app_enabled"] is False


def test_readiness_unknown_tenant_falls_back_to_default():
    # tenant inexistente -> service cai no get_default_tenant (slug default).
    client, _ = build(seed=_seed_default_starter)
    r = client.get("/tenants/inexistente-xyz/dedicated-app-readiness")
    assert r.status_code == 200, r.text
    body = r.json()
    # resolve para o tenant default seeded (mesmo id), nao 404.
    assert body["tenant_id"] == DEFAULT_TENANT_ID
    assert body["ready_for_dedicated_app"] is False


def test_readiness_no_tenant_seeded_uses_safe_fallback():
    # banco vazio: get_default_tenant criaria via ensure_default_tenant; o service
    # nao deve estourar 500 e devolve um payload valido com o shape esperado.
    client, _ = build(seed=None)
    r = client.get(f"/tenants/{BIZ_TENANT_ID}/dedicated-app-readiness")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["dedicated_app_enabled"] is False
    assert body["ready_for_dedicated_app"] is False
    assert set(body["features"].keys()) == {
        "network_access",
        "dedicated_app",
        "custom_products",
        "custom_projects",
    }
