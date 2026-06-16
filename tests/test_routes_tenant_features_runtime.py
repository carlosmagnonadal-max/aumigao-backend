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

A rota /current NAO exige autenticacao (escopada pelo middleware). A rota
/{tenant_id} EXIGE auth + escopo de tenant (Onda 1 / mt-MT2): super_admin ve
qualquer tenant, admin so o proprio. O foco dos testes de calculo e o AND
base+tenant e a resolucao de tenant; ha tambem testes de auth/isolamento.
"""
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import app.models  # noqa: F401 - registra todas as tabelas no Base.metadata
from app.core.database import Base, get_db
from app.dependencies.auth import get_current_user
from app.models.tenant import Tenant, TenantFeature
from app.models.user import User
from app.routes import tenant_features_runtime
from app.services.tenant_feature_runtime_service import PRODUCT_RUNTIME_FEATURE_KEYS, RUNTIME_FEATURE_KEYS
from app.services.tenant_plan_service import DEFAULT_ON_FEATURE_KEYS
from app.services.tenant_seed_service import DEFAULT_TENANT_SLUG

ALL_RUNTIME_KEYS = (*RUNTIME_FEATURE_KEYS, *PRODUCT_RUNTIME_FEATURE_KEYS)

DEFAULT_TENANT_ID = "t-default"

# Sentinela: por padrão os testes de cálculo rodam como super_admin (escopo global),
# para acessar /{tenant_id} livremente. Os testes de auth passam auth_user explícito.
_DEFAULT_SUPER_ADMIN = object()


def build(*, tenants: list[dict] | None = None, features: list[dict] | None = None, auth_user=_DEFAULT_SUPER_ADMIN):
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
    if auth_user is _DEFAULT_SUPER_ADMIN:
        auth_user = User(id="u-sa", role="super_admin", is_active=True)
    if auth_user is not None:
        test_app.dependency_overrides[get_current_user] = lambda: auth_user
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
    # Plano starter: nenhuma capability comercial base liberada -> False para RUNTIME_FEATURE_KEYS.
    # Fase 3 T1: chaves default-on de produto retornam True (gated por TenantFeature, nao por plano).
    client, _ = build(tenants=[
        dict(id="t-starter", name="Starter", slug=DEFAULT_TENANT_SLUG,
             status="active", plan="starter")
    ])
    body = client.get("/tenants/t-starter/features-runtime").json()
    features = body["features"]
    # Chaves comerciais: False para starter
    for key in RUNTIME_FEATURE_KEYS:
        assert features[key] is False, f"Commercial key {key!r} should be False for starter"
    # Chaves default-on: True (sem linha na tabela → default-on)
    for key in DEFAULT_ON_FEATURE_KEYS:
        assert features[key] is True, f"Default-on key {key!r} should be True"
    # verified_walkers: False (default-off)
    assert features.get("verified_walkers") is False


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
    """Fase 3 T1: enterprise libera chaves comerciais; chaves default-on True; verified_walkers False."""
    client, _ = build(tenants=[
        dict(id="t-ent", name="Ent", slug="ent-slug",
             status="active", plan="enterprise")
    ])
    body = client.get("/tenants/t-ent/features-runtime").json()
    features = body["features"]
    # Chaves comerciais: True para enterprise
    for key in RUNTIME_FEATURE_KEYS:
        assert features[key] is True, f"Commercial key {key!r} should be True for enterprise"
    # Chaves default-on: True
    for key in DEFAULT_ON_FEATURE_KEYS:
        assert features[key] is True, f"Default-on key {key!r} should be True"
    # verified_walkers: False (default-off)
    assert features["verified_walkers"] is False


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


# ---------------------------------------------- auth / isolamento (Onda 1) ----
def test_features_runtime_by_id_requires_auth():
    # Sem usuário autenticado, a rota por ID nega (401) — não vaza features de tenant.
    client, _ = build(auth_user=None)
    r = client.get(f"/tenants/{DEFAULT_TENANT_ID}/features-runtime")
    assert r.status_code == 401, r.text


def test_features_runtime_by_id_blocks_cross_tenant():
    # Admin do tenant A NÃO pode ler as features do tenant B (404, sem vazamento).
    admin_a = User(id="u-a", role="admin", tenant_id="t-A", is_active=True)
    client, _ = build(
        tenants=[
            dict(id="t-A", name="A", slug=DEFAULT_TENANT_SLUG, status="active", plan="business"),
            dict(id="t-B", name="B", slug="b-slug", status="active", plan="enterprise"),
        ],
        auth_user=admin_a,
    )
    r = client.get("/tenants/t-B/features-runtime")
    assert r.status_code == 404, r.text


def test_features_runtime_admin_reads_own_tenant():
    # Controle positivo: admin do próprio tenant consegue ler.
    admin_a = User(id="u-a", role="admin", tenant_id="t-A", is_active=True)
    client, _ = build(
        tenants=[dict(id="t-A", name="A", slug=DEFAULT_TENANT_SLUG, status="active", plan="business")],
        auth_user=admin_a,
    )
    r = client.get("/tenants/t-A/features-runtime")
    assert r.status_code == 200, r.text
    assert r.json()["tenant_id"] == "t-A"


def test_features_runtime_super_admin_sees_any_tenant():
    # super_admin (escopo global) pode ler qualquer tenant por ID.
    client, _ = build(tenants=[
        dict(id="t-B", name="B", slug="b-slug", status="active", plan="enterprise"),
        dict(id=DEFAULT_TENANT_ID, name="Aumigao", slug=DEFAULT_TENANT_SLUG, status="active", plan="business"),
    ])  # auth padrão = super_admin
    r = client.get("/tenants/t-B/features-runtime")
    assert r.status_code == 200, r.text
    assert r.json()["tenant_id"] == "t-B"
