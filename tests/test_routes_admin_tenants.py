"""Testes de ROTA (camada HTTP) do grupo "tenants-master": app/routes/tenants.py.

NOTA DE DESCOBERTA: o prompt apontava app/routes/admin.py, mas a gestao MASTER de
tenants (listar/detalhar/atualizar tenant, PATCH de features com gating comercial,
auditoria) vive em app/routes/tenants.py — router com prefixo "/admin/tenants",
gateado por require_permission("tenants.read"). admin.py NAO tem esses endpoints.

Padrao do projeto (ver tests/test_routes_walker_quality.py e test_routes_auth.py):
monta um FastAPI MINIMO so com o router de tenants + overrides de get_db /
get_current_user, SQLite em memoria (StaticPool). NAO importa app.main (Neon PROD).

Cobre:
- GET ""            list_tenants            (happy + 403)
- GET "/{id}"       get_tenant detalhe      (happy + 404)
- PATCH "/{id}"     update_tenant           (happy + valida plan/status invalidos + audit)
- GET/PATCH "/{id}/features"  gating comercial por plano (403 quando plano nao permite)
- auditoria de config registrada em update_tenant e update_tenant_features

Gating (lido de app/services/tenant_plan_service.py):
- ENFORCED_COMMERCIAL_FEATURES = {dedicated_app, network_access, custom_products, custom_projects}
- starter: network_access/custom_products/custom_projects/dedicated_app => indisponiveis
- business: custom_projects/dedicated_app_required... custom_projects ainda indisponivel
- enable de feature comercial indisponivel no plano -> HTTP 403.
"""
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import app.models  # noqa: F401 — registra todas as tabelas no Base.metadata
from app.core.database import Base, get_db
from app.dependencies.auth import get_current_user
from app.dependencies.rbac import require_permission  # noqa: F401 (doc)
from app.models.audit_log import AuditLog
from app.models.tenant import Tenant, TenantFeature
from app.models.user import User
from app.routes import tenants

ADMIN_ID = "admin-1"
TUTOR_ID = "tutor-1"
T_STARTER = "t-starter"
T_BUSINESS = "t-business"
T_ENTERPRISE = "t-enterprise"


def build(*, current: str = ADMIN_ID):
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    db = Session()

    # super_admin -> bypassa RBAC em user_has_permission (rede de seguranca)
    db.add(User(id=ADMIN_ID, email="admin@test.com", password_hash="x", role="super_admin"))
    # tutor comum -> sem tenants.read -> 403
    db.add(User(id=TUTOR_ID, email="tutor@test.com", password_hash="x", role="tutor"))

    db.add(Tenant(id=T_STARTER, name="Starter Co", slug="starter-co", status="active", plan="starter"))
    db.add(Tenant(id=T_BUSINESS, name="Business Co", slug="business-co", status="active", plan="business"))
    db.add(Tenant(id=T_ENTERPRISE, name="Enterprise Co", slug="enterprise-co", status="active", plan="enterprise"))
    db.commit()

    test_app = FastAPI()
    test_app.include_router(tenants.router)
    test_app.dependency_overrides[get_db] = lambda: db
    test_app.dependency_overrides[get_current_user] = lambda: db.get(User, current)
    return TestClient(test_app), db


def set_user(client, db, user_id):
    client.app.dependency_overrides[get_current_user] = lambda: db.get(User, user_id)


# ----------------------------------------------------------- listar tenants ---
def test_list_tenants_happy_path():
    client, _ = build()
    r = client.get("/admin/tenants")
    assert r.status_code == 200, r.text
    body = r.json()
    assert isinstance(body, list)
    ids = {t["id"] for t in body}
    assert {T_STARTER, T_BUSINESS, T_ENTERPRISE} <= ids


def test_list_tenants_forbidden_without_permission():
    client, db = build()
    set_user(client, db, TUTOR_ID)
    r = client.get("/admin/tenants")
    assert r.status_code == 403


# --------------------------------------------------------- detalhar tenant ---
def test_get_tenant_detail_happy_path():
    client, _ = build()
    r = client.get(f"/admin/tenants/{T_BUSINESS}")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["id"] == T_BUSINESS
    assert body["plan"] == "business"
    # TenantDetailResponse expoe os blocos relacionados
    assert "branding" in body and "settings" in body
    assert "features" in body and "units" in body


def test_get_tenant_detail_404():
    client, _ = build()
    r = client.get("/admin/tenants/nao-existe")
    assert r.status_code == 404


def test_get_tenant_detail_forbidden():
    client, db = build()
    set_user(client, db, TUTOR_ID)
    assert client.get(f"/admin/tenants/{T_BUSINESS}").status_code == 403


# --------------------------------------------------------- atualizar tenant ---
def test_update_tenant_happy_path_and_audit():
    client, db = build()
    r = client.patch(f"/admin/tenants/{T_STARTER}", json={"name": "  Novo Nome  ", "plan": "business"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["name"] == "Novo Nome"  # string e .strip()'d na rota
    assert body["plan"] == "business"
    # persistido
    db.expire_all()
    assert db.get(Tenant, T_STARTER).name == "Novo Nome"
    # auditoria de config registrada
    logs = db.query(AuditLog).filter(AuditLog.action == "tenant.updated", AuditLog.entity_id == T_STARTER).all()
    assert len(logs) == 1
    assert logs[0].actor_user_id == ADMIN_ID
    assert logs[0].tenant_id == T_STARTER


def test_update_tenant_rejects_invalid_plan():
    client, _ = build()
    r = client.patch(f"/admin/tenants/{T_STARTER}", json={"plan": "ultra-mega"})
    assert r.status_code == 400
    assert "plan" in r.json()["detail"].lower()


def test_update_tenant_rejects_invalid_status():
    client, _ = build()
    r = client.patch(f"/admin/tenants/{T_STARTER}", json={"status": "voando"})
    assert r.status_code == 400
    assert "status" in r.json()["detail"].lower()


def test_update_tenant_404():
    client, _ = build()
    r = client.patch("/admin/tenants/nao-existe", json={"name": "X"})
    assert r.status_code == 404


def test_update_tenant_forbidden():
    client, db = build()
    set_user(client, db, TUTOR_ID)
    assert client.patch(f"/admin/tenants/{T_STARTER}", json={"name": "X"}).status_code == 403


# ----------------------------------------------------------- features (PATCH) ---
def test_list_features_empty_then_404_tenant():
    client, _ = build()
    # tenant sem features ainda -> lista vazia
    r = client.get(f"/admin/tenants/{T_BUSINESS}/features")
    assert r.status_code == 200, r.text
    assert r.json() == []
    # tenant inexistente -> 404
    assert client.get("/admin/tenants/nao-existe/features").status_code == 404


def test_patch_features_enable_allowed_for_plan():
    # business permite network_access e custom_products -> habilitar deve passar
    client, db = build()
    r = client.patch(
        f"/admin/tenants/{T_BUSINESS}/features",
        json=[{"feature_key": "network_access", "enabled": True}],
    )
    assert r.status_code == 200, r.text
    body = r.json()
    keys = {f["feature_key"]: f["enabled"] for f in body}
    assert keys.get("network_access") is True
    # persistido
    saved = db.query(TenantFeature).filter(
        TenantFeature.tenant_id == T_BUSINESS, TenantFeature.feature_key == "network_access"
    ).first()
    assert saved is not None and saved.enabled is True
    # auditoria de config (features.updated)
    logs = db.query(AuditLog).filter(
        AuditLog.action == "features.updated", AuditLog.entity_id == T_BUSINESS
    ).all()
    assert len(logs) == 1
    assert logs[0].actor_user_id == ADMIN_ID


def test_patch_features_gating_403_when_plan_disallows():
    # starter NAO permite network_access -> enforce_tenant_feature_allowed -> 403
    client, db = build()
    r = client.patch(
        f"/admin/tenants/{T_STARTER}/features",
        json=[{"feature_key": "network_access", "enabled": True}],
    )
    assert r.status_code == 403, r.text
    assert "network_access" in r.json()["detail"]
    # nada persistido apos o 403 (sem commit)
    assert db.query(TenantFeature).filter(TenantFeature.tenant_id == T_STARTER).count() == 0


def test_patch_features_gating_403_custom_projects_on_business():
    # custom_projects so e permitido no enterprise; business -> 403
    client, _ = build()
    r = client.patch(
        f"/admin/tenants/{T_BUSINESS}/features",
        json=[{"feature_key": "custom_projects", "enabled": True}],
    )
    assert r.status_code == 403, r.text
    assert "custom_projects" in r.json()["detail"]


def test_patch_features_custom_projects_allowed_on_enterprise():
    client, _ = build()
    r = client.patch(
        f"/admin/tenants/{T_ENTERPRISE}/features",
        json=[{"feature_key": "custom_projects", "enabled": True}],
    )
    assert r.status_code == 200, r.text
    keys = {f["feature_key"]: f["enabled"] for f in r.json()}
    assert keys.get("custom_projects") is True


def test_patch_features_disable_commercial_not_gated():
    # desabilitar (enabled=False) NAO dispara o gating comercial mesmo no starter
    client, _ = build()
    r = client.patch(
        f"/admin/tenants/{T_STARTER}/features",
        json=[{"feature_key": "network_access", "enabled": False}],
    )
    assert r.status_code == 200, r.text


def test_patch_features_non_commercial_feature_not_gated():
    # feature de produto (nao-comercial) pode ser habilitada em qualquer plano
    client, _ = build()
    r = client.patch(
        f"/admin/tenants/{T_STARTER}/features",
        json=[{"feature_key": "recurring_plans", "enabled": True}],
    )
    assert r.status_code == 200, r.text
    keys = {f["feature_key"]: f["enabled"] for f in r.json()}
    assert keys.get("recurring_plans") is True


def test_patch_features_empty_feature_key_400():
    client, _ = build()
    r = client.patch(
        f"/admin/tenants/{T_BUSINESS}/features",
        json=[{"feature_key": "   ", "enabled": True}],
    )
    assert r.status_code == 400
    assert "feature_key" in r.json()["detail"]


def test_patch_features_forbidden_without_permission():
    client, db = build()
    set_user(client, db, TUTOR_ID)
    r = client.patch(
        f"/admin/tenants/{T_BUSINESS}/features",
        json=[{"feature_key": "network_access", "enabled": True}],
    )
    assert r.status_code == 403
