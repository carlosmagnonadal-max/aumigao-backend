"""Testes de ROTA (camada HTTP) do modulo app/routes/tenant_app_config.py.

Padrao do projeto (ver tests/test_routes_onda1.py e tests/test_routes_auth.py):
monta um FastAPI MINIMO com apenas os routers de tenant_app_config, SQLite em
memoria (StaticPool), override de get_db. NAO importa app.main (que conecta no
banco de PROD).

O modulo expoe SO endpoints GET (publicos, sem auth/RBAC e sem update):
  - GET /tenants/current/app-config       (+ /api/tenants/...)
  - GET /tenants/{tenant_id}/app-config   (+ /api/tenants/...)

Cobre: happy path do tenant "current" (resolve o tenant default), busca por id
e por slug, fallback de id desconhecido para o default, mirror /api/, e a
serializacao do response_model (branding/features/commercial/capabilities).
"""
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import app.models  # noqa: F401 - registra todas as tabelas no Base.metadata
from app.core.database import Base, get_db
from app.models.tenant import Tenant
from app.routes import tenant_app_config
from app.services.tenant_seed_service import DEFAULT_TENANT_SLUG

TENANT_ID = "t-test"
OTHER_TENANT_ID = "t-other"


def build(*, plan: str = "business"):
    """Monta app minimo com os routers de tenant_app_config e SQLite em memoria.

    O tenant default (slug = DEFAULT_TENANT_SLUG) recebe `plan`, para que a
    resolucao de "current" caia neste tenant sem criar/commitar outro.
    """
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    db = Session()

    db.add(Tenant(id=TENANT_ID, name="Aumigao", slug=DEFAULT_TENANT_SLUG, status="active", plan=plan))
    db.commit()

    test_app = FastAPI()
    test_app.include_router(tenant_app_config.router)
    test_app.include_router(tenant_app_config.api_router)
    test_app.dependency_overrides[get_db] = lambda: db
    return TestClient(test_app), db


def _assert_shape(body: dict):
    """Valida que o response_model serializou todos os blocos esperados."""
    assert set(body.keys()) >= {"tenant_id", "branding", "features", "units", "commercial", "capabilities"}
    branding = body["branding"]
    assert set(branding.keys()) == {
        "display_name", "app_name", "logo_url", "icon_url", "splash_image_url",
        "primary_color", "secondary_color", "powered_by_enabled",
    }
    features = body["features"]
    assert set(features.keys()) == {"network_access", "dedicated_app", "custom_products", "custom_projects"}
    commercial = body["commercial"]
    assert set(commercial.keys()) == {
        "plan", "plan_label", "upgrade_available", "next_recommended_plan",
        "billing_enabled", "billing_status",
    }
    assert isinstance(body["units"], list)
    assert isinstance(body["capabilities"], dict)


# ------------------------------------------------------------------- current ---
def test_current_app_config_happy_path():
    client, _ = build(plan="business")
    r = client.get("/tenants/current/app-config")
    assert r.status_code == 200, r.text
    body = r.json()
    _assert_shape(body)
    assert body["tenant_id"] == TENANT_ID


def test_current_app_config_business_features_and_commercial():
    """Plano business: features comerciais ligadas (exceto custom_projects)."""
    client, _ = build(plan="business")
    body = client.get("/tenants/current/app-config").json()
    features = body["features"]
    assert features["network_access"] is True
    assert features["dedicated_app"] is True
    assert features["custom_products"] is True
    assert features["custom_projects"] is False  # custom_projects e enterprise-only

    commercial = body["commercial"]
    assert commercial["plan"] == "business"
    assert commercial["plan_label"] == "Business"
    assert commercial["next_recommended_plan"] == "enterprise"
    assert commercial["upgrade_available"] is True
    assert commercial["billing_enabled"] is False
    assert commercial["billing_status"] == "not_configured"


def test_current_app_config_starter_disables_commercial_features():
    """Plano starter: nenhuma feature comercial ligada."""
    client, _ = build(plan="starter")
    body = client.get("/tenants/current/app-config").json()
    assert body["features"] == {
        "network_access": False,
        "dedicated_app": False,
        "custom_products": False,
        "custom_projects": False,
    }
    commercial = body["commercial"]
    assert commercial["plan"] == "starter"
    assert commercial["plan_label"] == "Starter"
    assert commercial["next_recommended_plan"] == "business"


def test_current_app_config_enterprise_all_features():
    client, _ = build(plan="enterprise")
    body = client.get("/tenants/current/app-config").json()
    assert all(body["features"].values())  # todas as 4 features ligadas
    commercial = body["commercial"]
    assert commercial["plan"] == "enterprise"
    assert commercial["plan_label"] == "Enterprise"
    assert commercial["next_recommended_plan"] is None
    assert commercial["upgrade_available"] is False


def test_current_app_config_branding_defaults():
    """Sem TenantBranding cadastrado, branding cai nos defaults do projeto."""
    client, _ = build()
    branding = client.get("/tenants/current/app-config").json()["branding"]
    assert branding["display_name"] == "Aumigao"  # tenant.name
    assert branding["app_name"] == "Aumigao"
    assert branding["logo_url"] == ""
    assert branding["primary_color"] == "#315f29"  # DEFAULT_PRIMARY_COLOR
    assert branding["secondary_color"] == "#101811"  # DEFAULT_SECONDARY_COLOR


# ----------------------------------------------------------------- by tenant ---
def test_app_config_by_tenant_id():
    client, db = build(plan="business")
    # segundo tenant com plano diferente, buscado explicitamente por id
    db.add(Tenant(id=OTHER_TENANT_ID, name="Outro", slug="outro", status="active", plan="starter"))
    db.commit()
    r = client.get(f"/tenants/{OTHER_TENANT_ID}/app-config")
    assert r.status_code == 200, r.text
    body = r.json()
    _assert_shape(body)
    assert body["tenant_id"] == OTHER_TENANT_ID
    assert body["commercial"]["plan"] == "starter"
    assert body["features"]["network_access"] is False


def test_app_config_by_tenant_slug():
    client, db = build(plan="business")
    db.add(Tenant(id=OTHER_TENANT_ID, name="Outro", slug="outro-slug", status="active", plan="enterprise"))
    db.commit()
    body = client.get("/tenants/outro-slug/app-config").json()
    assert body["tenant_id"] == OTHER_TENANT_ID
    assert body["commercial"]["plan"] == "enterprise"


def test_app_config_unknown_id_falls_back_to_default_tenant():
    """ID inexistente nao retorna 404: resolve para o tenant default."""
    client, _ = build(plan="business")
    r = client.get("/tenants/nao-existe-id/app-config")
    assert r.status_code == 200, r.text
    assert r.json()["tenant_id"] == TENANT_ID  # caiu no default


# -------------------------------------------------------------- mirror /api/ ---
def test_api_mirror_current():
    client, _ = build()
    r = client.get("/api/tenants/current/app-config")
    assert r.status_code == 200, r.text
    assert r.json()["tenant_id"] == TENANT_ID


def test_api_mirror_by_id():
    client, db = build()
    db.add(Tenant(id=OTHER_TENANT_ID, name="Outro", slug="outro", status="active", plan="business"))
    db.commit()
    body = client.get(f"/api/tenants/{OTHER_TENANT_ID}/app-config").json()
    assert body["tenant_id"] == OTHER_TENANT_ID
