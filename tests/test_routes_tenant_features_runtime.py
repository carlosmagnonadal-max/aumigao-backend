"""Testes de ROTA (camada HTTP) do modulo app/routes/tenant_features_runtime.py.

Padrao do projeto (ver tests/test_routes_onda1.py e tests/test_routes_auth.py):
monta um FastAPI MINIMO com apenas os routers de features-runtime, SQLite em
memoria (StaticPool), override de get_db. NAO importa app.main (Neon de PROD).

O runtime de features e calculado como base_allows (capacidade do PLANO) AND
tenant_allows (TenantFeature do tenant). As 4 chaves de runtime sao:
network_access, dedicated_app, custom_products, custom_projects.

Plano starter -> nenhuma capability base liberada -> todas as features False.
Plano business -> network_access/dedicated_app/custom_products base True;
custom_projects base False. Plano enterprise -> todas base True.

As rotas NAO exigem autenticacao (nao dependem de get_current_user), por isso
nao ha cenario 401/403 aqui; o foco e o calculo de defaults e do AND base+tenant,
a serializacao do response_model e a resolucao de tenant (id, slug, 'current').
"""
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import app.models  # noqa: F401 - registra todas as tabelas no Base.metadata
from app.core.database import Base, get_db
from app.models.tenant import Tenant, TenantFeature
from app.routes import tenant_features_runtime
from app.services.tenant_feature_runtime_service import PRODUCT_RUNTIME_FEATURE_KEYS, RUNTIME_FEATURE_KEYS
from app.services.tenant_seed_service import DEFAULT_TENANT_SLUG

ALL_RUNTIME_KEYS = (*RUNTIME_FEATURE_KEYS, *PRODUCT_RUNTIME_FEATURE_KEYS)

DEFAULT_TENANT_ID = "t-default"


def build(*, tenants: list[dict] | None = None, features: list[dict] | None = None):
    """Monta app minimo com os routers de features-runtime + SQLite em memoria.

    Sempre semeia um tenant default (slug = DEFAULT_TENANT_SLUG) para que
    get_default_tenant resolva sem tentar criar/commitar outro.
    """
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    db = Session()

    base_tenants = tenants or [
        dict(id=DEFAULT_TENANT_ID, name="Aumigao", slug=DEFAULT_TENANT_SLUG,
             status="active", plan="business")
    ]
    # Garante que existe SEMPRE um tenant com o slug default (fallback de resolucao).
    has_default = any(t.get("slug") == DEFAULT_TENANT_SLUG for t in base_tenants)
    if not has_default:
        base_tenants = base_tenants + [
            dict(id=DEFAULT_TENANT_ID, name="Aumigao", slug=DEFAULT_TENANT_SLUG,
                 status="active", plan="starter")
        ]
    for t in base_tenants:
        db.add(Tenant(**t))
    for f in features or []:
        db.add(TenantFeature(**f))
    db.commit()

    test_app = FastAPI()
    test_app.include_router(tenant_features_runtime.router)
    test_app.include_router(tenant_features_runtime.api_router)
    test_app.dependency_overrides[get_db] = lambda: db
    return TestClient(test_app), db


# --------------------------------------------------------- defaults / shape ---
def test_response_has_all_runtime_keys():
    client, _ = build()
    r = client.get(f"/tenants/{DEFAULT_TENANT_ID}/features-runtime")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["tenant_id"] == DEFAULT_TENANT_ID
    # response_model garante exatamente as 4 chaves de runtime, todas booleanas.
    assert set(body["features"].keys()) == set(ALL_RUNTIME_KEYS)
    assert all(isinstance(v, bool) for v in body["features"].values())


def test_starter_plan_all_features_off_by_default():
    # Plano starter: nenhuma capability base liberada -> defaults (tudo False).
    client, _ = build(tenants=[
        dict(id="t-starter", name="Starter", slug=DEFAULT_TENANT_SLUG,
             status="active", plan="starter")
    ])
    body = client.get("/tenants/t-starter/features-runtime").json()
    assert body["features"] == {key: False for key in ALL_RUNTIME_KEYS}


def test_business_plan_enables_base_allowed_features():
    # Business: base libera network_access, dedicated_app, custom_products;
    # custom_projects continua False (base do plano nao permite).
    client, _ = build()  # default = business
    body = client.get(f"/tenants/{DEFAULT_TENANT_ID}/features-runtime").json()
    feats = body["features"]
    assert feats["network_access"] is True
    assert feats["dedicated_app"] is True
    assert feats["custom_products"] is True
    assert feats["custom_projects"] is False


def test_enterprise_plan_enables_all_features():
    client, _ = build(tenants=[
        dict(id="t-ent", name="Ent", slug="ent-slug",
             status="active", plan="enterprise")
    ])
    body = client.get("/tenants/t-ent/features-runtime").json()
    # comerciais True (enterprise libera tudo); verified_walkers e flag de produto -> False sem TenantFeature.
    assert body["features"] == {**{key: True for key in RUNTIME_FEATURE_KEYS}, "verified_walkers": False}


# ---------------------------------------------------- AND base + tenant -------
def test_tenant_feature_disabled_overrides_base_allowed():
    # business permite network_access na base, mas o tenant desligou a feature.
    # tenant_allows=False -> AND -> False.
    client, _ = build(features=[
        dict(tenant_id=DEFAULT_TENANT_ID, feature_key="network_access", enabled=False)
    ])
    body = client.get(f"/tenants/{DEFAULT_TENANT_ID}/features-runtime").json()
    assert body["features"]["network_access"] is False
    # dedicated_app (sem row) permanece liberado pela base.
    assert body["features"]["dedicated_app"] is True


def test_tenant_feature_enabled_cannot_unlock_disallowed_base():
    # Mesmo ligando custom_projects no tenant, a base do business NAO permite ->
    # base_allows=False -> AND -> permanece False. (gating comercial respeitado)
    client, _ = build(features=[
        dict(tenant_id=DEFAULT_TENANT_ID, feature_key="custom_projects", enabled=True)
    ])
    body = client.get(f"/tenants/{DEFAULT_TENANT_ID}/features-runtime").json()
    assert body["features"]["custom_projects"] is False


def test_starter_tenant_feature_enabled_still_off_due_to_base():
    # starter: base nega tudo. Ligar network_access no tenant nao habilita.
    client, _ = build(
        tenants=[dict(id="t-st", name="St", slug=DEFAULT_TENANT_SLUG,
                      status="active", plan="starter")],
        features=[dict(tenant_id="t-st", feature_key="network_access", enabled=True)],
    )
    body = client.get("/tenants/t-st/features-runtime").json()
    assert body["features"]["network_access"] is False


# ---------------------------------------------------- resolucao de tenant -----
def test_resolve_by_slug():
    client, _ = build(tenants=[
        dict(id="t-ent", name="Ent", slug="acme", status="active", plan="enterprise")
    ])
    # passa o SLUG no path; _resolve_tenant cai no lookup por slug.
    body = client.get("/tenants/acme/features-runtime").json()
    assert body["tenant_id"] == "t-ent"
    assert body["features"]["custom_projects"] is True


def test_unknown_tenant_id_falls_back_to_default():
    # id inexistente -> _resolve_tenant retorna o tenant default (slug DEFAULT).
    client, _ = build()  # default business
    body = client.get("/tenants/nao-existe/features-runtime").json()
    assert body["tenant_id"] == DEFAULT_TENANT_ID


def test_current_path_resolves_default_tenant():
    # /current sem middleware de tenant -> request.state.tenant_id None ->
    # resolve para o tenant default.
    client, _ = build()
    body = client.get("/tenants/current/features-runtime").json()
    assert body["tenant_id"] == DEFAULT_TENANT_ID
    assert body["features"]["network_access"] is True


def test_api_prefixed_router_works():
    # mesmo endpoint exposto sob /api/tenants.
    client, _ = build()
    r = client.get(f"/api/tenants/{DEFAULT_TENANT_ID}/features-runtime")
    assert r.status_code == 200, r.text
    assert r.json()["tenant_id"] == DEFAULT_TENANT_ID
