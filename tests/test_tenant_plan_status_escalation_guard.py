"""Guard de escalação de privilégio / bypass de billing no PATCH de tenant.

Auditoria de segurança (ALTA): `PATCH /admin/tenants/{tenant_id}` (update_tenant)
é gated no router apenas por `tenants.read`. O papel `tenant_admin` TEM `tenants.read`,
logo um admin de tenant autenticado conseguia:
  1. `{"plan": "enterprise"}` — recalcula a comissão para o default do novo tier,
     auto-promovendo-se (sai dos caps/comissão do plano atual) sem pagar nada;
  2. `{"status": "active"}` — reativa o próprio tenant após suspensão por
     inadimplência (tenant_saas_billing_service seta status="suspended").

Correção: mutação de `plan`/`status` exige `tenants.manage` E super_admin (defesa em
profundidade), escopada a esses campos — os demais campos de TenantUpdate seguem
editáveis pelo admin de tenant com `tenants.read`.

Padrão do harness: FastAPI mínimo + SQLite em memória (StaticPool) + overrides de
get_db / get_current_user. NUNCA importa app.main.
"""
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import app.models  # noqa: F401 — registra todas as tabelas no Base.metadata
from app.core.database import Base, get_db
from app.dependencies.auth import get_current_user
from app.models.rbac import Permission, Role, RolePermission, UserRoleAssignment
from app.models.tenant import Tenant
from app.models.user import User
from app.routes import tenants

TENANT_A = "tenant-a"
SUPER_ADMIN_ID = "super-admin"
ADMIN_A_ID = "admin-tenant-a"


def _new_db():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    return Session()


def _grant_permission(db, user_id: str, tenant_id: str, *perm_keys: str) -> None:
    """Semeia Permission -> Role -> RolePermission -> UserRoleAssignment.

    Mesmo padrão de test_authz_hardening.py: dá permissões reais a um usuário
    não-super_admin para que require_permission() no nível do router passe.
    """
    for perm_key in perm_keys:
        perm_id = f"perm-{perm_key}"
        with db.no_autoflush:
            existing_perm = db.query(Permission).filter(Permission.key == perm_key).first()
            if existing_perm is None:
                db.add(Permission(id=perm_id, key=perm_key,
                                  module=perm_key.split(".")[0],
                                  action=perm_key.split(".")[-1]))
            else:
                perm_id = existing_perm.id
        db.flush()

        role_id = f"role-{perm_key}-{tenant_id}-{user_id}"
        with db.no_autoflush:
            if not db.get(Role, role_id):
                db.add(Role(id=role_id, name=role_id, scope_type="tenant"))
        db.flush()

        existing_rp = db.query(RolePermission).filter(
            RolePermission.role_id == role_id,
            RolePermission.permission_id == perm_id,
        ).first()
        if not existing_rp:
            db.add(RolePermission(role_id=role_id, permission_id=perm_id))
        db.flush()

        db.add(UserRoleAssignment(user_id=user_id, role_id=role_id, tenant_id=tenant_id))
    db.commit()


def _build(*, tenant_manage_by_mistake: bool = False):
    db = _new_db()
    db.add(Tenant(id=TENANT_A, name="Alpha", slug="alpha", status="suspended", plan="pro"))
    db.add(User(id=SUPER_ADMIN_ID, email="sa@test.com", password_hash="x",
                role="super_admin", tenant_id=TENANT_A))
    db.add(User(id=ADMIN_A_ID, email="aa@test.com", password_hash="x",
                role="admin", tenant_id=TENANT_A))
    db.commit()

    # Admin de tenant tem tenants.read (passa o gate do router). Sem tenants.manage,
    # salvo no cenário de "concedida por engano" (para exercitar a defesa em profundidade).
    perms = ["tenants.read"]
    if tenant_manage_by_mistake:
        perms.append("tenants.manage")
    _grant_permission(db, ADMIN_A_ID, TENANT_A, *perms)

    app_ = FastAPI()
    app_.include_router(tenants.router)
    app_.dependency_overrides[get_db] = lambda: db
    return app_, db


def _as(app_, db, user_id):
    app_.dependency_overrides[get_current_user] = lambda: db.get(User, user_id)
    return TestClient(app_)


# ── Fluxo legítimo self-service: admin de tenant edita campo NÃO-sensível ─────────

def test_tenant_admin_can_edit_non_sensitive_field():
    """Admin de tenant continua editando name (self-service) com tenants.read -> 200."""
    app_, db = _build()
    client = _as(app_, db, ADMIN_A_ID)
    r = client.patch(f"/admin/tenants/{TENANT_A}", json={"name": "Alpha Renamed"})
    assert r.status_code == 200, r.text
    assert r.json()["name"] == "Alpha Renamed"


# ── Vulnerabilidade fechada: admin de tenant NÃO muda plan/status ─────────────────

def test_tenant_admin_cannot_self_promote_plan():
    """Admin de tenant tentando `{"plan": "enterprise"}` -> 403 (sem auto-promoção)."""
    app_, db = _build()
    client = _as(app_, db, ADMIN_A_ID)
    r = client.patch(f"/admin/tenants/{TENANT_A}", json={"plan": "enterprise"})
    assert r.status_code == 403, r.text
    assert "Aumigão" in r.json()["detail"]
    # Estado no banco permanece inalterado.
    db.expire_all()
    assert db.get(Tenant, TENANT_A).plan == "pro"


def test_tenant_admin_cannot_reactivate_after_suspension():
    """Admin de tenant tentando `{"status": "active"}` após suspensão -> 403."""
    app_, db = _build()
    client = _as(app_, db, ADMIN_A_ID)
    r = client.patch(f"/admin/tenants/{TENANT_A}", json={"status": "active"})
    assert r.status_code == 403, r.text
    db.expire_all()
    assert db.get(Tenant, TENANT_A).status == "suspended"


def test_tenant_admin_with_manage_by_mistake_still_blocked():
    """Defesa em profundidade: mesmo com tenants.manage (concedida por engano),
    o admin de tenant é barrado pelo guard de super_admin -> 403."""
    app_, db = _build(tenant_manage_by_mistake=True)
    client = _as(app_, db, ADMIN_A_ID)
    r = client.patch(f"/admin/tenants/{TENANT_A}", json={"plan": "enterprise"})
    assert r.status_code == 403, r.text


# ── Fluxo legítimo do backoffice: super_admin muda plan/status livremente ─────────

def test_super_admin_can_change_status():
    """super_admin reativa o tenant (status) normalmente -> 200."""
    app_, db = _build()
    client = _as(app_, db, SUPER_ADMIN_ID)
    r = client.patch(f"/admin/tenants/{TENANT_A}", json={"status": "active"})
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "active"


def test_super_admin_can_change_plan():
    """super_admin altera o plano (recalcula comissão) normalmente -> 200."""
    app_, db = _build()
    client = _as(app_, db, SUPER_ADMIN_ID)
    r = client.patch(f"/admin/tenants/{TENANT_A}", json={"plan": "enterprise"})
    assert r.status_code == 200, r.text
    assert r.json()["plan"] == "enterprise"
