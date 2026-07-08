"""Testes de rota para GET /admin/users/{user_id}/legal-acceptances.

Ficha do tutor/passeador no admin-web precisa mostrar o historico de aceite dos
termos (pedido do dono, teste real 08/07). Cobre:
- 200 com a lista de aceites do usuario, mais recente primeiro
- Uma linha de LegalAcceptance pode conter VARIOS documentos (colunas de versao
  independentes: terms/privacy/cancellation/lgpd/geolocation) -> expande em um
  item por documento com version preenchida; documentos com version="" (nao
  fizeram parte daquele evento) NAO aparecem.
- scope "platform" (tenant_id nulo) vs "tenant" (tenant_id preenchido)
- 404 para usuario inexistente
- 404 para usuario de OUTRO tenant quando visto por admin de tenant (nao vaza)
- super_admin (escopo global) enxerga qualquer tenant
- 403 para quem nao tem users.read (ex.: tutor comum)

Padrao: espelha tests/test_admin_detail_endpoints.py (FastAPI minimo + SQLite
em memoria, StaticPool, overrides de get_db/get_current_user, RBAC explicito
para o admin de tenant porque so super_admin bypassa user_has_permission).
"""
from datetime import datetime, timedelta

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import app.models  # noqa: F401 - registra todas as tabelas no Base.metadata
from app.core.database import Base, get_db
from app.dependencies.auth import get_current_user
from app.models.legal_acceptance import LegalAcceptance
from app.models.rbac import Permission, Role, RolePermission, UserRoleAssignment
from app.models.user import User
from app.routes import admin as admin_routes

SUPER_ADMIN_ID = "super-admin-1"
TENANT_ADMIN_ID = "tenant-admin-1"
TUTOR_ID = "tutor-1"
TENANT_A = "tenant-a"
TENANT_B = "tenant-b"


def _seed_rbac_for_tenant_admin(db, user_id, permissions: list[str], tenant_id: str):
    role = Role(id=f"role-{user_id}", name=f"tenant_admin_{user_id}", scope_type="tenant")
    db.add(role)
    db.flush()
    for i, perm_key in enumerate(permissions):
        module, _, action = perm_key.partition(".")
        perm = Permission(id=f"perm-{user_id}-{i}", key=perm_key, module=module, action=action or perm_key)
        db.add(perm)
        db.flush()
        db.add(RolePermission(id=f"rp-{user_id}-{i}", role_id=role.id, permission_id=perm.id))
    db.add(UserRoleAssignment(id=f"ura-{user_id}", user_id=user_id, role_id=role.id, tenant_id=tenant_id))
    db.flush()


def build(*, current: str = SUPER_ADMIN_ID):
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    db = Session()

    db.add(User(id=SUPER_ADMIN_ID, email="superadmin@test.com", password_hash="x",
                role="super_admin", full_name="Super Admin"))
    db.add(User(id=TENANT_ADMIN_ID, email="admin-a@test.com", password_hash="x",
                role="admin", full_name="Admin A", tenant_id=TENANT_A))
    db.add(User(id=TUTOR_ID, email="tutor@test.com", password_hash="x",
                role="tutor", full_name="Tutor", tenant_id=TENANT_A))
    db.flush()

    _seed_rbac_for_tenant_admin(db, TENANT_ADMIN_ID, ["admin.access", "users.read"], TENANT_A)
    db.commit()

    test_app = FastAPI()
    test_app.include_router(admin_routes.router)
    test_app.dependency_overrides[get_db] = lambda: db
    test_app.dependency_overrides[get_current_user] = lambda: db.get(User, current)
    return TestClient(test_app), db


def set_user(client, db, user_id):
    client.app.dependency_overrides[get_current_user] = lambda: db.get(User, user_id)


def _add_acceptance(db, **kwargs):
    defaults = dict(
        id=f"acc-{db.query(LegalAcceptance).count() + 1}",
        user_id=TUTOR_ID,
        user_role="tutor",
        tenant_id=None,
        terms_version="",
        privacy_version="",
        cancellation_version="",
        lgpd_version="",
        geolocation_version="",
        accepted_at=datetime.utcnow(),
    )
    defaults.update(kwargs)
    row = LegalAcceptance(**defaults)
    db.add(row)
    db.commit()
    return row


# ------------------------------------------------------------------ happy path
def test_lists_acceptances_most_recent_first():
    client, db = build()
    older = _add_acceptance(
        db, id="acc-old", terms_version="2026-01-01", privacy_version="2026-01-01",
        cancellation_version="2026-01-01", lgpd_version="2026-01-01", geolocation_version="2026-01-01",
        accepted_at=datetime.utcnow() - timedelta(days=30),
    )
    newer = _add_acceptance(
        db, id="acc-new", terms_version="2026-06-29", privacy_version="2026-06-29",
        cancellation_version="2026-06-29", lgpd_version="2026-06-29", geolocation_version="2026-06-29",
        accepted_at=datetime.utcnow(),
    )
    r = client.get(f"/admin/users/{TUTOR_ID}/legal-acceptances")
    assert r.status_code == 200, r.text
    body = r.json()
    # 2 linhas x 5 documentos cada = 10 itens
    assert len(body) == 10
    acceptance_ids = [item["acceptance_id"] for item in body]
    assert acceptance_ids.index(newer.id) < acceptance_ids.index(older.id)


def test_row_with_partial_versions_only_expands_filled_documents():
    # Aceite POR TENANT tipicamente so preenche as colunas aplicaveis ao role;
    # colunas vazias ("") nao devem virar itens fantasmas na ficha.
    client, db = build()
    _add_acceptance(
        db, id="acc-tenant-partial", tenant_id=TENANT_A,
        terms_version="2026-07-01", privacy_version="2026-07-01",
        cancellation_version="", lgpd_version="", geolocation_version="",
    )
    r = client.get(f"/admin/users/{TUTOR_ID}/legal-acceptances")
    assert r.status_code == 200, r.text
    body = r.json()
    assert len(body) == 2
    doc_types = {item["document_type"] for item in body}
    assert doc_types == {"terms", "privacy"}
    assert all(item["scope"] == "tenant" for item in body)
    assert all(item["tenant_id"] == TENANT_A for item in body)


def test_platform_scope_when_tenant_id_is_null():
    client, db = build()
    _add_acceptance(db, id="acc-platform", tenant_id=None, terms_version="2026-06-29")
    r = client.get(f"/admin/users/{TUTOR_ID}/legal-acceptances")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body[0]["scope"] == "platform"
    assert body[0]["tenant_id"] is None


def test_empty_list_when_user_never_accepted():
    client, db = build()
    r = client.get(f"/admin/users/{TUTOR_ID}/legal-acceptances")
    assert r.status_code == 200, r.text
    assert r.json() == []


# ------------------------------------------------------------------------ 404
def test_404_for_unknown_user():
    client, _ = build()
    r = client.get("/admin/users/nao-existe/legal-acceptances")
    assert r.status_code == 404


def test_404_for_other_tenant_user_seen_by_tenant_admin():
    client, db = build()
    db.add(User(id="user-b-1", email="user-b@test.com", password_hash="x",
                role="tutor", full_name="User B", tenant_id=TENANT_B))
    db.commit()
    set_user(client, db, TENANT_ADMIN_ID)
    r = client.get("/admin/users/user-b-1/legal-acceptances")
    assert r.status_code == 404


def test_super_admin_sees_any_tenant():
    client, db = build()
    db.add(User(id="user-b-2", email="user-b2@test.com", password_hash="x",
                role="tutor", full_name="User B2", tenant_id=TENANT_B))
    db.commit()
    _add_acceptance(db, id="acc-b2", user_id="user-b-2", terms_version="2026-06-29")
    r = client.get("/admin/users/user-b-2/legal-acceptances")
    assert r.status_code == 200, r.text
    assert len(r.json()) == 1


def test_tenant_admin_sees_own_tenant_user():
    client, db = build()
    _add_acceptance(db, id="acc-own", terms_version="2026-06-29")
    set_user(client, db, TENANT_ADMIN_ID)
    r = client.get(f"/admin/users/{TUTOR_ID}/legal-acceptances")
    assert r.status_code == 200, r.text
    assert len(r.json()) == 1


# ------------------------------------------------------------------------ 403
def test_forbidden_for_non_admin():
    client, db = build()
    set_user(client, db, TUTOR_ID)
    r = client.get(f"/admin/users/{TUTOR_ID}/legal-acceptances")
    assert r.status_code == 403
