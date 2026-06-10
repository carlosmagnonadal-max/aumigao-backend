"""Testes de ROTA (camada HTTP) do modulo app/routes/tenant_commercial.py.

Padrao do projeto (ver tests/test_routes_onda1.py e tests/test_routes_auth.py):
monta um FastAPI MINIMO com apenas o(s) router(s) de tenant_commercial, SQLite em
memoria (StaticPool), override de get_db. NAO importa app.main (que conecta no Neon).

OBS: ao contrario do que a descricao do alvo sugere, este modulo NAO expoe
PATCH /features nem exige auth/RBAC. Ele tem 3 endpoints GET publicos (e versoes
/api/...):
  - GET /tenants/commercial/plans          -> catalogo de planos
  - GET /tenants/current/commercial-runtime -> runtime do tenant atual (request.state)
  - GET /tenants/{tenant_id}/commercial-runtime -> runtime de um tenant especifico

O "gating por plano" aparece no campo `features` do runtime: o plano determina
quais features comerciais (network_access, dedicated_app, custom_products,
custom_projects) ficam True/False. Cobrimos isso aqui (happy path + variacoes de
plano + resolucao por id/slug/inexistente + fallback do current sem middleware).
"""
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import app.models  # noqa: F401 - registra todas as tabelas no Base.metadata
from app.core.database import Base, get_db
from app.models.tenant import Tenant, TenantFeature
from app.routes import tenant_commercial
from app.services.tenant_seed_service import DEFAULT_TENANT_SLUG

DEFAULT_TENANT_ID = "t-default"


def build(*, plan: str = "business", extra_tenants: list[dict] | None = None,
          features: list[dict] | None = None):
    """Monta app minimo com os routers de tenant_commercial e SQLite em memoria.

    O tenant default (slug = DEFAULT) e sempre criado para que get_default_tenant
    o resolva sem tentar criar/commitar outro.
    """
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    db = Session()

    db.add(Tenant(id=DEFAULT_TENANT_ID, name="Aumigao", slug=DEFAULT_TENANT_SLUG,
                  status="active", plan=plan))
    for t in extra_tenants or []:
        db.add(Tenant(**t))
    for f in features or []:
        db.add(TenantFeature(**f))
    db.commit()

    test_app = FastAPI()
    test_app.include_router(tenant_commercial.router)
    test_app.include_router(tenant_commercial.api_router)
    test_app.dependency_overrides[get_db] = lambda: db
    return TestClient(test_app), db


# ----------------------------------------------------------- commercial/plans ---
def test_list_commercial_plans_returns_three_plans():
    client, _ = build()
    r = client.get("/tenants/commercial/plans")
    assert r.status_code == 200, r.text
    plans = r.json()["plans"]
    keys = [p["key"] for p in plans]
    assert keys == ["starter", "business", "enterprise"]
    # cada plano traz o shape esperado pelo response_model
    for p in plans:
        assert set(p.keys()) == {"key", "label", "description", "capabilities", "recommended_for"}
        assert isinstance(p["capabilities"], dict)
        assert isinstance(p["recommended_for"], list)


def test_list_commercial_plans_capabilities_increase_with_tier():
    client, _ = build()
    plans = {p["key"]: p["capabilities"] for p in client.get("/tenants/commercial/plans").json()["plans"]}
    # starter nao tem nenhuma feature comercial
    assert plans["starter"] == {
        "network_access": False, "dedicated_app": False,
        "custom_products": False, "custom_projects": False,
    }
    # business libera tudo menos custom_projects
    assert plans["business"]["custom_products"] is True
    assert plans["business"]["custom_projects"] is False
    # enterprise libera tudo, inclusive custom_projects
    assert plans["enterprise"]["custom_projects"] is True


def test_list_commercial_plans_api_prefix_works():
    client, _ = build()
    r = client.get("/api/tenants/commercial/plans")
    assert r.status_code == 200
    assert len(r.json()["plans"]) == 3


# --------------------------------------------------- {tenant_id}/commercial-runtime ---
def test_runtime_by_id_business_plan_gating():
    client, _ = build(plan="business")
    r = client.get(f"/tenants/{DEFAULT_TENANT_ID}/commercial-runtime")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["tenant_id"] == DEFAULT_TENANT_ID
    assert body["plan"] == "business"
    assert body["plan_label"] == "Business"
    # gating por plano refletido em `features`: business NAO permite custom_projects
    assert body["features"]["custom_products"] is True
    assert body["features"]["custom_projects"] is False
    assert body["features"]["network_access"] is True
    # business pode subir para enterprise
    assert body["upgrade_available"] is True
    assert body["next_recommended_plan"] == "enterprise"
    # billing ainda nao configurado
    assert body["billing_enabled"] is False
    assert body["billing_status"] == "not_configured"


def test_runtime_starter_plan_blocks_all_commercial_features():
    client, _ = build(plan="starter")
    body = client.get(f"/tenants/{DEFAULT_TENANT_ID}/commercial-runtime").json()
    assert body["plan"] == "starter"
    assert body["features"] == {
        "network_access": False, "dedicated_app": False,
        "custom_products": False, "custom_projects": False,
    }
    assert body["upgrade_available"] is True
    assert body["next_recommended_plan"] == "business"


def test_runtime_enterprise_plan_unlocks_everything_no_upgrade():
    client, _ = build(plan="enterprise")
    body = client.get(f"/tenants/{DEFAULT_TENANT_ID}/commercial-runtime").json()
    assert body["plan"] == "enterprise"
    assert body["features"]["custom_projects"] is True
    # topo da escada: sem upgrade
    assert body["upgrade_available"] is False
    assert body["next_recommended_plan"] is None


def test_runtime_unknown_plan_normalizes_to_starter():
    client, _ = build(plan="plano-inexistente")
    body = client.get(f"/tenants/{DEFAULT_TENANT_ID}/commercial-runtime").json()
    assert body["plan"] == "starter"
    assert body["plan_label"] == "Starter"


def test_runtime_resolves_tenant_by_slug():
    # cria um tenant extra com slug proprio (nao o default)
    client, _ = build(extra_tenants=[dict(
        id="t-acme", name="Acme", slug="acme", status="active", plan="enterprise",
    )])
    body = client.get("/tenants/acme/commercial-runtime").json()
    assert body["tenant_id"] == "t-acme"
    assert body["plan"] == "enterprise"


def test_runtime_unknown_tenant_falls_back_to_default():
    client, _ = build(plan="business")
    # tenant_id que nao existe nem por id nem por slug -> cai no default
    body = client.get("/tenants/nao-existe/commercial-runtime").json()
    assert body["tenant_id"] == DEFAULT_TENANT_ID
    assert body["plan"] == "business"


def test_runtime_override_cannot_exceed_plan_ceiling():
    # Tenant business com override TENTANDO habilitar custom_projects (que o plano
    # business NAO permite). A feature efetiva e `base_plan AND tenant_override`,
    # entao o teto do plano prevalece: continua False.
    client, _ = build(plan="business", features=[dict(
        tenant_id=DEFAULT_TENANT_ID, feature_key="custom_projects", enabled=True,
    )])
    body = client.get(f"/tenants/{DEFAULT_TENANT_ID}/commercial-runtime").json()
    assert body["features"]["custom_projects"] is False


def test_runtime_override_can_disable_feature_allowed_by_plan():
    # Tenant business: plano permite custom_products, mas override desliga.
    # base_plan(True) AND tenant_override(False) -> efetiva False.
    client, _ = build(plan="business", features=[dict(
        tenant_id=DEFAULT_TENANT_ID, feature_key="custom_products", enabled=False,
    )])
    body = client.get(f"/tenants/{DEFAULT_TENANT_ID}/commercial-runtime").json()
    assert body["features"]["custom_products"] is False


# --------------------------------------------------- current/commercial-runtime ---
def test_current_runtime_without_middleware_uses_default_tenant():
    # No app minimo nao ha middleware setando request.state.tenant_id -> getattr None
    # -> _resolve_tenant cai no tenant default.
    client, _ = build(plan="business")
    r = client.get("/tenants/current/commercial-runtime")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["tenant_id"] == DEFAULT_TENANT_ID
    assert body["plan"] == "business"


def test_current_runtime_api_prefix_works():
    client, _ = build(plan="starter")
    r = client.get("/api/tenants/current/commercial-runtime")
    assert r.status_code == 200
    assert r.json()["plan"] == "starter"
