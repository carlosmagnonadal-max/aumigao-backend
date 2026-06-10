"""Testes de ROTA (camada HTTP) do modulo app/routes/tenant_launch_readiness.py.

Padrao do projeto (ver tests/test_routes_onda1.py e tests/test_routes_auth.py):
monta um FastAPI MINIMO com apenas os routers de launch-readiness, SQLite em
memoria (StaticPool), override de get_db. NAO importa app.main (que conecta no
banco de PROD).

O modulo expoe checklist/prontidao de lancamento do tenant em dois endpoints
(GET /tenants/current/launch-readiness e GET /tenants/{tenant_id}/launch-readiness),
espelhados tambem sob /api/tenants/... pelo api_router.

NOTA sobre AUTH: as rotas NAO declaram nenhuma dependencia de autenticacao
(sem get_current_user, sem require_*). O endpoint e publico no comportamento
atual; ver bug_or_gap. Por isso nao ha teste 401/403 — nao existe gate de auth
a exercitar nesta camada.
"""
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import app.models  # noqa: F401 - registra todas as tabelas no Base.metadata
from app.core.database import Base, get_db
from app.models.tenant import Tenant, TenantBranding, TenantUnit
from app.routes import tenant_launch_readiness
from app.services.tenant_seed_service import DEFAULT_TENANT_SLUG

TENANT_ID = "t-test"

# 12 checks no total; "billing" e sempre False no comportamento atual (hardcoded),
# logo o score maximo possivel e 11/12 = 91.67 -> arredonda para 92.
EXPECTED_CHECK_KEYS = {
    "branding",
    "app_name",
    "display_name",
    "primary_color",
    "secondary_color",
    "logo",
    "icon",
    "splash",
    "dedicated_app",
    "plan",
    "billing",
    "units",
}


def build(*, plan: str = "starter", branding: dict | None = None, units: list[dict] | None = None):
    """Monta app minimo com os routers de launch-readiness e um SQLite isolado.

    slug = DEFAULT para get_default_tenant resolver este tenant sem criar outro.
    """
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    db = Session()

    db.add(Tenant(id=TENANT_ID, name="Aumigao", slug=DEFAULT_TENANT_SLUG, status="active", plan=plan))
    if branding is not None:
        db.add(TenantBranding(tenant_id=TENANT_ID, **branding))
    for unit in units or []:
        db.add(TenantUnit(tenant_id=TENANT_ID, **unit))
    db.commit()

    test_app = FastAPI()
    test_app.include_router(tenant_launch_readiness.router)
    test_app.include_router(tenant_launch_readiness.api_router)
    test_app.dependency_overrides[get_db] = lambda: db
    return TestClient(test_app), db


def _ready_branding() -> dict:
    """Branding completo: nome/app/cores + assets de logo/icone/splash preenchidos."""
    return dict(
        display_name="Pet Walk Co",
        app_name="Pet Walk",
        logo_url="https://cdn.example.com/logo.png",
        icon_url="https://cdn.example.com/icon.png",
        splash_image_url="https://cdn.example.com/splash.png",
        primary_color="#112233",
        secondary_color="#445566",
    )


# ---------------------------------------------------------------- current ----
def test_current_launch_readiness_default_tenant_shape():
    client, _ = build()
    r = client.get("/tenants/current/launch-readiness")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["tenant_id"] == TENANT_ID
    assert isinstance(body["score"], int)
    assert 0 <= body["score"] <= 100
    assert body["status"] in {"ready", "not_ready"}
    assert set(body["checks"].keys()) == EXPECTED_CHECK_KEYS
    assert isinstance(body["blocking_items"], list)
    assert isinstance(body["warnings"], list)
    assert isinstance(body["summary"], str) and body["summary"]


def test_current_default_tenant_not_ready_starter_plan():
    # Tenant starter sem branding/assets/unidades: longe de pronto.
    client, _ = build(plan="starter")
    body = client.get("/tenants/current/launch-readiness").json()
    assert body["ready"] is False
    assert body["status"] == "not_ready"
    # plan starter nao esta em LAUNCH_PLANS -> reprovado
    assert body["checks"]["plan"] is False
    # dedicated_app indisponivel no plano starter
    assert body["checks"]["dedicated_app"] is False
    # assets ausentes por padrao
    assert body["checks"]["logo"] is False
    assert body["checks"]["units"] is False
    # itens bloqueantes presentes (plan, dedicated_app, logo, icon, splash, units...)
    assert "plan" in body["blocking_items"]
    assert "dedicated_app" in body["blocking_items"]
    assert "units" in body["blocking_items"]


def test_billing_always_warning_and_never_in_checks_true():
    # billing e hardcoded False -> sempre gera o warning, nunca passa.
    client, _ = build()
    body = client.get("/tenants/current/launch-readiness").json()
    assert body["checks"]["billing"] is False
    assert "billing_not_configured" in body["warnings"]


# ----------------------------------------------------------- fully ready -----
def test_fully_ready_business_tenant():
    client, _ = build(
        plan="business",
        branding=_ready_branding(),
        units=[{"id": "u1", "name": "Unidade Centro", "status": "active"}],
    )
    body = client.get("/tenants/current/launch-readiness").json()
    assert body["ready"] is True, body
    assert body["status"] == "ready"
    assert body["blocking_items"] == []
    # todos os checks bloqueantes passam
    assert body["checks"]["branding"] is True
    assert body["checks"]["logo"] is True
    assert body["checks"]["icon"] is True
    assert body["checks"]["splash"] is True
    assert body["checks"]["plan"] is True
    assert body["checks"]["dedicated_app"] is True
    assert body["checks"]["units"] is True
    # billing continua False (nao bloqueia) e gera warning
    assert body["checks"]["billing"] is False
    assert body["warnings"] == ["billing_not_configured"]
    # 11 de 12 checks passam -> 92%
    assert body["score"] == 92
    assert "pronto para lancar" in body["summary"].lower()


def test_inactive_unit_does_not_count():
    # unidade existente mas inativa -> check units permanece False
    client, _ = build(
        plan="business",
        branding=_ready_branding(),
        units=[{"id": "u1", "name": "Unidade Inativa", "status": "inactive"}],
    )
    body = client.get("/tenants/current/launch-readiness").json()
    assert body["checks"]["units"] is False
    assert body["ready"] is False
    assert "units" in body["blocking_items"]


# ----------------------------------------------------- explicit tenant_id ----
def test_launch_readiness_by_tenant_id():
    client, _ = build(
        plan="business",
        branding=_ready_branding(),
        units=[{"id": "u1", "name": "Unidade Centro", "status": "active"}],
    )
    r = client.get(f"/tenants/{TENANT_ID}/launch-readiness")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["tenant_id"] == TENANT_ID
    assert body["ready"] is True


def test_unknown_tenant_id_falls_back_to_default():
    # tenant_id inexistente -> servico cai no default tenant (sem erro).
    client, _ = build(plan="starter")
    r = client.get("/tenants/does-not-exist/launch-readiness")
    assert r.status_code == 200, r.text
    body = r.json()
    # resolve para o default (nosso unico tenant seedado)
    assert body["tenant_id"] == TENANT_ID


# ------------------------------------------------------------ api_router -----
def test_api_router_mirror_current():
    client, _ = build()
    r = client.get("/api/tenants/current/launch-readiness")
    assert r.status_code == 200, r.text
    assert set(r.json()["checks"].keys()) == EXPECTED_CHECK_KEYS


def test_api_router_mirror_by_tenant_id():
    client, _ = build(
        plan="business",
        branding=_ready_branding(),
        units=[{"id": "u1", "name": "Unidade Centro", "status": "active"}],
    )
    r = client.get(f"/api/tenants/{TENANT_ID}/launch-readiness")
    assert r.status_code == 200, r.text
    assert r.json()["ready"] is True


# ------------------------------------------------------ no-auth (publico) ----
def test_endpoint_is_public_no_auth_required():
    # Rotas nao declaram dependencia de auth: acessivel sem Authorization header.
    # Documenta o comportamento atual (ver bug_or_gap).
    client, _ = build()
    r = client.get("/tenants/current/launch-readiness")
    assert r.status_code == 200
