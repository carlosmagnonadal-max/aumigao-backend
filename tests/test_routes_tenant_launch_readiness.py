"""Testes de ROTA (camada HTTP) do modulo app/routes/tenant_launch_readiness.py.

Padrao do projeto (ver tests/test_routes_onda1.py e tests/test_routes_auth.py):
monta um FastAPI MINIMO com apenas os routers de launch-readiness, SQLite em
memoria (StaticPool), override de get_db. NAO importa app.main (que conecta no
banco de PROD).

O modulo expoe checklist/prontidao de lancamento do tenant em dois endpoints
(GET /tenants/current/launch-readiness e GET /tenants/{tenant_id}/launch-readiness),
espelhados tambem sob /api/tenants/... pelo api_router.

NOTA sobre AUTH: as rotas exigem admin.access (401 sem auth) e aplicam escopo de
tenant ao admin de tenant ("current" = proprio tenant; /{tenant_id} de outro
tenant -> 404). super_admin mantem acesso a qualquer tenant.
"""
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import app.models  # noqa: F401 - registra todas as tabelas no Base.metadata
from app.core.database import Base, get_db
from app.dependencies.auth import get_current_user
from app.models.tenant import Tenant, TenantBranding, TenantUnit
from app.models.user import User
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


def build(*, plan: str = "starter", branding: dict | None = None, units: list[dict] | None = None, authed: bool = True):
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
    if authed:
        admin = User(id="admin-test", email="admin@aumigao.test", full_name="Admin", role="super_admin", is_active=True, password_hash="x")
        test_app.dependency_overrides[get_current_user] = lambda: admin
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


# ------------------------------------------------------ auth obrigatoria -----
def test_endpoint_requires_auth():
    # Agora exige acesso admin: sem Authorization header -> 401.
    client, _ = build(authed=False)
    assert client.get("/tenants/current/launch-readiness").status_code == 401
    assert client.get(f"/tenants/{TENANT_ID}/launch-readiness").status_code == 401


# ------------------------------------------- isolamento admin de tenant -------
# Regressao (white-label do admin-web): admin de tenant X via BFF nao envia
# X-Tenant-Slug; "current" caia no tenant default e /{tenant_id} nao checava
# escopo -> admin de X lia a prontidao (branding/plano/unidades) de outro tenant.
OTHER_TENANT_ID = "t-parceiro"
TENANT_ADMIN_ID = "admin-parceiro"


def _build_multi_tenant():
    from app.models.rbac import Permission, Role, RolePermission, UserRoleAssignment

    client, db = build(plan="starter", authed=False)
    db.add(Tenant(id=OTHER_TENANT_ID, name="Parceiro", slug="parceiro", status="active", plan="business"))
    db.add(TenantBranding(tenant_id=OTHER_TENANT_ID, **_ready_branding()))
    db.add(User(id=TENANT_ADMIN_ID, email="admin@parceiro.test", full_name="Admin Parceiro",
                role="admin", is_active=True, password_hash="x", tenant_id=OTHER_TENANT_ID))
    db.add(Role(id="role-parceiro", name="tenant_admin_parceiro", scope_type="tenant"))
    db.add(Permission(id="perm-admin-access", key="admin.access", module="admin", action="access"))
    db.flush()
    db.add(RolePermission(id="rp-parceiro", role_id="role-parceiro", permission_id="perm-admin-access"))
    db.add(UserRoleAssignment(id="ura-parceiro", user_id=TENANT_ADMIN_ID, role_id="role-parceiro",
                              tenant_id=OTHER_TENANT_ID))
    db.commit()
    client.app.dependency_overrides[get_current_user] = lambda: db.get(User, TENANT_ADMIN_ID)
    return client, db


def test_tenant_admin_current_resolves_own_tenant_not_default():
    client, _ = _build_multi_tenant()
    for prefix in ("/tenants", "/api/tenants"):
        r = client.get(f"{prefix}/current/launch-readiness")
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["tenant_id"] == OTHER_TENANT_ID
        assert body["checks"]["logo"] is True  # branding do tenant X, nao do default


def test_tenant_admin_reads_own_tenant_by_id():
    client, _ = _build_multi_tenant()
    r = client.get(f"/api/tenants/{OTHER_TENANT_ID}/launch-readiness")
    assert r.status_code == 200, r.text
    assert r.json()["tenant_id"] == OTHER_TENANT_ID


def test_tenant_admin_cannot_read_other_tenant_by_id_404():
    client, _ = _build_multi_tenant()
    assert client.get(f"/api/tenants/{TENANT_ID}/launch-readiness").status_code == 404
    assert client.get(f"/tenants/{TENANT_ID}/launch-readiness").status_code == 404
    # id inexistente nao pode cair no fallback do default para admin de tenant
    assert client.get("/api/tenants/does-not-exist/launch-readiness").status_code == 404


def test_super_admin_still_reads_any_tenant_by_id():
    client, db = _build_multi_tenant()
    super_admin = User(id="super-x", email="s@aumigao.test", full_name="S", role="super_admin",
                       is_active=True, password_hash="x")
    client.app.dependency_overrides[get_current_user] = lambda: super_admin
    r = client.get(f"/api/tenants/{OTHER_TENANT_ID}/launch-readiness")
    assert r.status_code == 200, r.text
    assert r.json()["tenant_id"] == OTHER_TENANT_ID
    # current do super_admin segue a resolucao da requisicao (default no TestClient)
    assert client.get("/api/tenants/current/launch-readiness").json()["tenant_id"] == TENANT_ID
