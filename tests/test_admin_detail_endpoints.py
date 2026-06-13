"""Testes para os endpoints de DETALHE do admin (pacote AW1).

Cobre:
- GET /admin/walks/{walk_id}     — detalhe de walk
- GET /admin/payments/{payment_id} — detalhe de payment (inclui invoice_url)
- GET /admin/users/{user_id}     — detalhe de usuario

Regras verificadas:
- 200 com item correto para super_admin (scope global)
- 404 para id inexistente
- 404 para item de outro tenant acessado por admin de tenant (nao vaza existencia)
- super_admin global acessa items de qualquer tenant
- invoice_url presente no serializer de payment
- Limits de listagem aceitam ate 1000 (walk, payment, user, tutor)

Padrao: FastAPI minimo + SQLite em memoria (StaticPool), sem importar app.main.
"""
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import app.models  # noqa: F401 — registra todas as tabelas
from app.core.database import Base, get_db
from app.dependencies.auth import get_current_user
from app.models.payment import Payment
from app.models.rbac import Permission, Role, RolePermission, UserRoleAssignment
from app.models.user import User
from app.models.walk import Walk
from app.routes import admin as admin_routes

SUPER_ADMIN_ID = "super-admin-1"
TENANT_ADMIN_ID = "tenant-admin-1"
TUTOR_ID = "tutor-1"
WALKER_ID = "walker-1"
TENANT_A = "tenant-a"
TENANT_B = "tenant-b"


def _seed_rbac_for_tenant_admin(db, user_id, permissions: list[str]):
    """Cria Role + Permission + RolePermission + UserRoleAssignment para um admin de tenant.

    Isso e necessario porque user_has_permission so bypassa para super_admin;
    para role="admin" precisa de registros RBAC no banco.
    """
    role = Role(id=f"role-{user_id}", name=f"tenant_admin_{user_id}", scope_type="tenant")
    db.add(role)
    db.flush()
    for i, perm_key in enumerate(permissions):
        module, _, action = perm_key.partition(".")
        perm = Permission(id=f"perm-{user_id}-{i}", key=perm_key, module=module, action=action or perm_key)
        db.add(perm)
        db.flush()
        db.add(RolePermission(id=f"rp-{user_id}-{i}", role_id=role.id, permission_id=perm.id))
    db.add(UserRoleAssignment(
        id=f"ura-{user_id}",
        user_id=user_id,
        role_id=role.id,
        tenant_id=TENANT_A,
    ))
    db.flush()


def build(*, current: str = SUPER_ADMIN_ID):
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    db = Session()

    # super_admin -> bypassa RBAC; scope global (sem filtro de tenant)
    db.add(User(id=SUPER_ADMIN_ID, email="superadmin@test.com", password_hash="x",
                role="super_admin", full_name="Super Admin"))
    # admin de tenant A — precisa de RBAC explicito
    db.add(User(id=TENANT_ADMIN_ID, email="admin-a@test.com", password_hash="x",
                role="admin", full_name="Admin A", tenant_id=TENANT_A))
    # tutor comum (sem permissao de admin.access) — gera 403
    db.add(User(id=TUTOR_ID, email="tutor@test.com", password_hash="x",
                role="tutor", full_name="Tutor", tenant_id=TENANT_A))
    db.add(User(id=WALKER_ID, email="walker@test.com", password_hash="x",
                role="walker", full_name="Walker", tenant_id=TENANT_A))
    db.flush()

    # Permissoes necessarias para o tenant admin acessar os endpoints de detalhe
    _seed_rbac_for_tenant_admin(db, TENANT_ADMIN_ID, [
        "admin.access",
        "walks.read",
        "finance.read",
        "users.read",
    ])
    db.commit()

    test_app = FastAPI()
    test_app.include_router(admin_routes.router)
    test_app.dependency_overrides[get_db] = lambda: db
    test_app.dependency_overrides[get_current_user] = lambda: db.get(User, current)
    return TestClient(test_app), db


def set_user(client, db, user_id):
    client.app.dependency_overrides[get_current_user] = lambda: db.get(User, user_id)


def _add_walk(db, walk_id="walk-1", tenant_id=TENANT_A):
    walk = Walk(
        id=walk_id,
        tutor_id=TUTOR_ID,
        walker_id=WALKER_ID,
        pet_id="pet-1",
        scheduled_date="2026-06-10T10:00:00",
        duration_minutes=30,
        price=50.0,
        status="Agendado",
        operational_status="ride_scheduled",
        tenant_id=tenant_id,
    )
    db.add(walk)
    db.commit()
    return walk


def _add_payment(db, payment_id="pay-1", tenant_id=TENANT_A, invoice_url=None):
    payment = Payment(
        id=payment_id,
        tenant_id=tenant_id,
        tutor_id=TUTOR_ID,
        walk_id=None,
        amount=75.0,
        status="paid",
        provider="asaas",
        invoice_url=invoice_url,
    )
    db.add(payment)
    db.commit()
    return payment


# ============================================================
# GET /admin/walks/{walk_id}
# ============================================================

def test_get_walk_detail_happy_path():
    client, db = build()
    _add_walk(db, "walk-detail-1", TENANT_A)
    r = client.get("/admin/walks/walk-detail-1")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["id"] == "walk-detail-1"


def test_get_walk_detail_not_found_returns_404():
    client, db = build()
    r = client.get("/admin/walks/nao-existe")
    assert r.status_code == 404


def test_get_walk_detail_other_tenant_returns_404_for_tenant_admin():
    client, db = build()
    # walk do tenant B
    _add_walk(db, "walk-tenant-b", TENANT_B)
    # admin do tenant A nao pode ver walk do tenant B -> 404 (nao vaza existencia)
    set_user(client, db, TENANT_ADMIN_ID)
    r = client.get("/admin/walks/walk-tenant-b")
    assert r.status_code == 404


def test_get_walk_detail_super_admin_sees_any_tenant():
    client, db = build()
    _add_walk(db, "walk-any-tenant", TENANT_B)
    # super_admin global enxerga qualquer tenant
    r = client.get("/admin/walks/walk-any-tenant")
    assert r.status_code == 200, r.text
    assert r.json()["id"] == "walk-any-tenant"


def test_get_walk_detail_forbidden_for_non_admin():
    client, db = build()
    _add_walk(db, "walk-forbidden", TENANT_A)
    set_user(client, db, TUTOR_ID)
    r = client.get("/admin/walks/walk-forbidden")
    assert r.status_code == 403


# ============================================================
# GET /admin/payments/{payment_id}
# ============================================================

def test_get_payment_detail_happy_path():
    client, db = build()
    _add_payment(db, "pay-detail-1", TENANT_A, invoice_url="https://asaas.com/inv/abc")
    r = client.get("/admin/payments/pay-detail-1")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["id"] == "pay-detail-1"
    assert body["invoice_url"] == "https://asaas.com/inv/abc"


def test_get_payment_detail_invoice_url_present_when_null():
    # invoice_url nulo deve aparecer no payload (campo aditivo, nao quebra nada)
    client, db = build()
    _add_payment(db, "pay-null-inv", TENANT_A, invoice_url=None)
    r = client.get("/admin/payments/pay-null-inv")
    assert r.status_code == 200, r.text
    body = r.json()
    assert "invoice_url" in body
    assert body["invoice_url"] is None


def test_get_payment_detail_not_found_returns_404():
    client, db = build()
    r = client.get("/admin/payments/nao-existe")
    assert r.status_code == 404


def test_get_payment_detail_other_tenant_returns_404_for_tenant_admin():
    client, db = build()
    _add_payment(db, "pay-tenant-b", TENANT_B)
    set_user(client, db, TENANT_ADMIN_ID)
    r = client.get("/admin/payments/pay-tenant-b")
    assert r.status_code == 404


def test_get_payment_detail_super_admin_sees_any_tenant():
    client, db = build()
    _add_payment(db, "pay-any-tenant", TENANT_B)
    r = client.get("/admin/payments/pay-any-tenant")
    assert r.status_code == 200, r.text
    assert r.json()["id"] == "pay-any-tenant"


def test_get_payment_detail_forbidden_for_non_admin():
    client, db = build()
    _add_payment(db, "pay-forbidden", TENANT_A)
    set_user(client, db, TUTOR_ID)
    r = client.get("/admin/payments/pay-forbidden")
    assert r.status_code == 403


# ============================================================
# GET /admin/users/{user_id}
# ============================================================

def test_get_user_detail_happy_path():
    client, db = build()
    r = client.get(f"/admin/users/{TUTOR_ID}")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["id"] == TUTOR_ID
    # Nao vazar credenciais: o detalhe deve usar o serializer enxuto, nunca o ORM cru.
    assert "password_hash" not in body


def test_get_user_detail_not_found_returns_404():
    client, db = build()
    r = client.get("/admin/users/nao-existe")
    assert r.status_code == 404


def test_get_user_detail_other_tenant_returns_404_for_tenant_admin():
    client, db = build()
    # usuario do tenant B
    db.add(User(id="user-b-1", email="user-b@test.com", password_hash="x",
                role="tutor", full_name="User B", tenant_id=TENANT_B))
    db.commit()
    set_user(client, db, TENANT_ADMIN_ID)
    r = client.get("/admin/users/user-b-1")
    assert r.status_code == 404


def test_get_user_detail_super_admin_sees_any_tenant():
    client, db = build()
    db.add(User(id="user-b-2", email="user-b2@test.com", password_hash="x",
                role="tutor", full_name="User B2", tenant_id=TENANT_B))
    db.commit()
    r = client.get("/admin/users/user-b-2")
    assert r.status_code == 200, r.text
    assert r.json()["id"] == "user-b-2"


def test_get_user_detail_forbidden_for_non_admin():
    client, db = build()
    set_user(client, db, TUTOR_ID)
    r = client.get(f"/admin/users/{SUPER_ADMIN_ID}")
    assert r.status_code == 403


# ============================================================
# Sanidade dos limits das listagens (le=1000)
# ============================================================

def test_walks_list_accepts_limit_1000():
    client, db = build()
    r = client.get("/admin/walks?limit=1000")
    assert r.status_code == 200, r.text


def test_walks_list_rejects_limit_above_1000():
    client, db = build()
    r = client.get("/admin/walks?limit=1001")
    assert r.status_code == 422


def test_payments_list_accepts_limit_1000():
    client, db = build()
    r = client.get("/admin/payments?limit=1000")
    assert r.status_code == 200, r.text


def test_payments_list_rejects_limit_above_1000():
    client, db = build()
    r = client.get("/admin/payments?limit=1001")
    assert r.status_code == 422


def test_users_list_accepts_limit_1000():
    client, db = build()
    r = client.get("/admin/users?limit=1000")
    assert r.status_code == 200, r.text


def test_users_list_rejects_limit_above_1000():
    client, db = build()
    r = client.get("/admin/users?limit=1001")
    assert r.status_code == 422


def test_tutors_list_accepts_limit_1000():
    client, db = build()
    r = client.get("/admin/tutors?limit=1000")
    assert r.status_code == 200, r.text


def test_tutors_list_rejects_limit_above_1000():
    client, db = build()
    r = client.get("/admin/tutors?limit=1001")
    assert r.status_code == 422
