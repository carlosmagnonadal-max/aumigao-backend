"""Testes de rota para CRUD de contas admin (Feature 1).

Padrão do projeto: FastAPI mínimo com SQLite em memória (StaticPool),
overrides de get_db / get_current_user. NÃO importa app.main.

Nota de RBAC: user_has_permission só bypassa para role="super_admin" na rede de
segurança. Um "admin" regular sem seed RBAC toma 403 nas rotas gateadas — igual
ao padrão observado em test_routes_admin_dashboard.py. Por isso os testes HTTP
usam super_admin como ator padrão. O tenant-scoping é validado diretamente via
helper (sem HTTP), seguindo test_dashboard_tenant_scope_via_scope_helper.

Cobre:
- GET  /admin/accounts  → lista, shape da resposta, 403 sem permissão
- POST /admin/accounts  → criação, 409 duplicado, 403 escalação de role/cross-tenant
- PATCH /admin/accounts/{id} → atualiza full_name/role/is_active, proteção auto-desativação
- cross-tenant scoping via get_admin_tenant_scope + apply_tenant_filter (helper direto)
"""
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import app.models  # noqa: F401 — registra todos os modelos no Base.metadata
from app.core.database import Base, get_db
from app.dependencies.auth import get_current_user
from app.dependencies.tenant_scope import apply_tenant_filter, get_admin_tenant_scope
from app.models.user import User
from app.routes import admin_accounts

SUPER_ID = "super-1"
SUPER2_ID = "super-2"
ADMIN_A_ID = "admin-a"
ADMIN_B_ID = "admin-b"
TUTOR_ID = "tutor-1"

TENANT_A = "tenant-a"
TENANT_B = "tenant-b"


def build(*, current: str = SUPER_ID):
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    db = Session()

    # super_admin — bypassa RBAC
    db.add(User(id=SUPER_ID, email="super@aumigao.app", password_hash="x", role="super_admin"))
    # segundo super_admin para teste de auto-desativação
    db.add(User(id=SUPER2_ID, email="super2@aumigao.app", password_hash="x", role="super_admin"))
    # admin do tenant A
    db.add(User(id=ADMIN_A_ID, email="admin-a@aumigao.app", password_hash="x", role="admin", tenant_id=TENANT_A))
    # admin do tenant B (para testes cross-tenant)
    db.add(User(id=ADMIN_B_ID, email="admin-b@aumigao.app", password_hash="x", role="admin", tenant_id=TENANT_B))
    # tutor sem permissão
    db.add(User(id=TUTOR_ID, email="tutor@aumigao.app", password_hash="x", role="tutor", tenant_id=TENANT_A))
    db.commit()

    test_app = FastAPI()
    test_app.include_router(admin_accounts.router)
    test_app.dependency_overrides[get_db] = lambda: db
    test_app.dependency_overrides[get_current_user] = lambda: db.get(User, current)
    return TestClient(test_app), db


def set_user(client, db, user_id):
    client.app.dependency_overrides[get_current_user] = lambda: db.get(User, user_id)


# ------------------------------------------------------------------ GET list --

def test_list_accounts_forbidden_for_non_admin():
    client, db = build(current=TUTOR_ID)
    r = client.get("/admin/accounts")
    assert r.status_code == 403


def test_list_accounts_super_admin_sees_all():
    client, db = build(current=SUPER_ID)
    r = client.get("/admin/accounts")
    assert r.status_code == 200, r.text
    body = r.json()
    assert "items" in body
    assert "total" in body
    # seed tem super + super2 + admin_a + admin_b = 4 contas admin
    assert body["total"] == 4
    ids = {item["id"] for item in body["items"]}
    assert SUPER_ID in ids
    assert ADMIN_A_ID in ids
    assert ADMIN_B_ID in ids


def test_list_accounts_response_shape():
    client, _ = build(current=SUPER_ID)
    body = client.get("/admin/accounts").json()
    item = body["items"][0]
    for field in ("id", "email", "full_name", "role", "tenant_id", "is_active", "created_at"):
        assert field in item, f"campo faltando: {field}"


def test_list_accounts_tenant_scoping_via_helper():
    """Valida scoping de tenant no nivel do helper (sem HTTP, sem RBAC seed).

    Admin de tenant A enxerga apenas contas do seu tenant;
    super_admin enxerga todos — igual ao padrão de test_dashboard_tenant_scope_via_scope_helper.
    """
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    db = Session()

    db.add(User(id=SUPER_ID, email="super@aumigao.app", password_hash="x", role="super_admin"))
    db.add(User(id=ADMIN_A_ID, email="admin-a@aumigao.app", password_hash="x", role="admin", tenant_id=TENANT_A))
    db.add(User(id=ADMIN_B_ID, email="admin-b@aumigao.app", password_hash="x", role="admin", tenant_id=TENANT_B))
    db.commit()

    # Escopo do admin A: só vê contas do tenant A
    admin_a = db.get(User, ADMIN_A_ID)
    scope_a = get_admin_tenant_scope(admin_a)
    assert scope_a.is_global is False
    assert scope_a.tenant_id == TENANT_A

    query_a = db.query(User).filter(User.role.in_({"admin", "super_admin"}))
    rows_a = apply_tenant_filter(query_a, User, scope_a).all()
    ids_a = {u.id for u in rows_a}
    assert ADMIN_A_ID in ids_a
    assert ADMIN_B_ID not in ids_a  # outro tenant
    assert SUPER_ID not in ids_a   # sem tenant_id

    # Escopo do super_admin: vê todos
    super_user = db.get(User, SUPER_ID)
    scope_super = get_admin_tenant_scope(super_user)
    assert scope_super.is_global is True
    query_super = db.query(User).filter(User.role.in_({"admin", "super_admin"}))
    rows_super = apply_tenant_filter(query_super, User, scope_super).all()
    ids_super = {u.id for u in rows_super}
    assert ADMIN_A_ID in ids_super
    assert ADMIN_B_ID in ids_super
    assert SUPER_ID in ids_super


# --------------------------------------------------------------- POST create --

def test_create_account_super_admin_creates_admin():
    client, db = build(current=SUPER_ID)
    r = client.post("/admin/accounts", json={
        "email": "novo-admin@aumigao.app",
        "full_name": "Novo Admin",
        "role": "admin",
        "tenant_id": TENANT_A,
        "password": "Senha@1234",
    })
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["email"] == "novo-admin@aumigao.app"
    assert body["role"] == "admin"
    assert body["tenant_id"] == TENANT_A
    assert body["is_active"] is True
    # Confirma que password_hash não vaza
    assert "password" not in body
    assert "password_hash" not in body


def test_create_account_super_admin_creates_super_admin():
    client, _ = build(current=SUPER_ID)
    r = client.post("/admin/accounts", json={
        "email": "novo-super@aumigao.app",
        "full_name": "Novo Super",
        "role": "super_admin",
        "password": "Senha@1234",
    })
    assert r.status_code == 201, r.text
    assert r.json()["role"] == "super_admin"


def test_create_account_admin_cannot_create_super_admin():
    """_assert_can_manage_target: admin de tenant não pode criar super_admin.

    Nota: a rota bloqueia com 403 via _assert_can_manage_target ANTES do RBAC
    — mas como admin regular tomaria 403 pelo RBAC, testamos via helper direto.
    """
    from fastapi import HTTPException
    from app.routes.admin_accounts import _assert_can_manage_target

    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    db = Session()
    db.add(User(id=ADMIN_A_ID, email="admin-a@aumigao.app", password_hash="x", role="admin", tenant_id=TENANT_A))
    db.commit()

    admin_a = db.get(User, ADMIN_A_ID)
    with pytest.raises(HTTPException) as exc_info:
        _assert_can_manage_target(admin_a, None, "super_admin")
    assert exc_info.value.status_code == 403


def test_create_account_admin_cannot_create_in_other_tenant():
    """_assert_can_manage_target: admin do tenant A não cria conta no tenant B."""
    from fastapi import HTTPException
    from app.routes.admin_accounts import _assert_can_manage_target

    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    db = Session()
    db.add(User(id=ADMIN_A_ID, email="admin-a@aumigao.app", password_hash="x", role="admin", tenant_id=TENANT_A))
    db.commit()

    admin_a = db.get(User, ADMIN_A_ID)
    with pytest.raises(HTTPException) as exc_info:
        _assert_can_manage_target(admin_a, TENANT_B, "admin")
    assert exc_info.value.status_code == 403


def test_create_account_duplicate_email_returns_409():
    client, _ = build(current=SUPER_ID)
    payload = {
        "email": "duplicado@aumigao.app",
        "role": "admin",
        "tenant_id": TENANT_A,
        "password": "Senha@1234",
    }
    r1 = client.post("/admin/accounts", json=payload)
    assert r1.status_code == 201
    r2 = client.post("/admin/accounts", json=payload)
    assert r2.status_code == 409


def test_create_account_admin_requires_tenant_id():
    """role=admin sem tenant_id → 422 ou 400."""
    client, _ = build(current=SUPER_ID)
    r = client.post("/admin/accounts", json={
        "email": "sem-tenant@aumigao.app",
        "role": "admin",
        "password": "Senha@1234",
    })
    assert r.status_code in (400, 422)


# -------------------------------------------------------------- PATCH update --

def test_update_account_full_name():
    client, db = build(current=SUPER_ID)
    r = client.patch(f"/admin/accounts/{ADMIN_A_ID}", json={"full_name": "Nome Atualizado"})
    assert r.status_code == 200, r.text
    assert r.json()["full_name"] == "Nome Atualizado"


def test_update_account_deactivate():
    client, db = build(current=SUPER_ID)
    # super_admin desativa admin_a (não é si mesmo)
    r = client.patch(f"/admin/accounts/{ADMIN_A_ID}", json={"is_active": False})
    assert r.status_code == 200, r.text
    assert r.json()["is_active"] is False


def test_update_account_cannot_self_deactivate():
    """super_admin não pode desativar a própria conta."""
    client, _ = build(current=SUPER_ID)
    r = client.patch(f"/admin/accounts/{SUPER_ID}", json={"is_active": False})
    assert r.status_code == 400


def test_update_account_admin_cannot_escalate_to_super_admin():
    """_assert_can_manage_target: admin de tenant não pode promover a super_admin."""
    from fastapi import HTTPException
    from app.routes.admin_accounts import _assert_can_manage_target

    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    db = Session()
    db.add(User(id=ADMIN_A_ID, email="admin-a@aumigao.app", password_hash="x", role="admin", tenant_id=TENANT_A))
    db.commit()

    admin_a = db.get(User, ADMIN_A_ID)
    with pytest.raises(HTTPException) as exc_info:
        _assert_can_manage_target(admin_a, TENANT_A, "super_admin")
    assert exc_info.value.status_code == 403


def test_update_account_not_found():
    client, _ = build(current=SUPER_ID)
    r = client.patch("/admin/accounts/inexistente-id", json={"full_name": "X"})
    assert r.status_code == 404


def test_update_account_role_change_super_admin_allowed():
    """super_admin pode alterar role de admin para super_admin."""
    client, db = build(current=SUPER_ID)
    r = client.patch(f"/admin/accounts/{ADMIN_A_ID}", json={"role": "super_admin"})
    assert r.status_code == 200, r.text
    assert r.json()["role"] == "super_admin"


def test_update_account_cross_tenant_blocked_via_helper():
    """Valida que _assert_can_manage_target bloqueia edição cross-tenant."""
    from fastapi import HTTPException
    from app.routes.admin_accounts import _assert_can_manage_target

    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    db = Session()
    db.add(User(id=ADMIN_A_ID, email="admin-a@aumigao.app", password_hash="x", role="admin", tenant_id=TENANT_A))
    db.add(User(id=ADMIN_B_ID, email="admin-b@aumigao.app", password_hash="x", role="admin", tenant_id=TENANT_B))
    db.commit()

    admin_a = db.get(User, ADMIN_A_ID)
    # Admin A não pode gerenciar conta do tenant B
    with pytest.raises(HTTPException) as exc_info:
        _assert_can_manage_target(admin_a, TENANT_B, "admin")
    assert exc_info.value.status_code == 403


# -------------------------------------------------------- B2: must_change_password ----

def test_create_account_sets_must_change_password_true():
    """Todo admin criado via POST /admin/accounts deve ter must_change_password=True."""
    client, db = build(current=SUPER_ID)
    r = client.post("/admin/accounts", json={
        "email": "b2-admin@aumigao.app",
        "full_name": "Admin B2",
        "role": "admin",
        "tenant_id": TENANT_A,
        "password": "Senha@1234",
    })
    assert r.status_code == 201, r.text
    body = r.json()

    user = db.query(User).filter(User.email == "b2-admin@aumigao.app").first()
    assert user is not None
    assert user.must_change_password is True, "must_change_password deveria ser True para novo admin"
