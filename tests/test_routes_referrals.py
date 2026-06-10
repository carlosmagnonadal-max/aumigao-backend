"""Testes de ROTA (camada HTTP) do modulo app/routes/referrals.py.

Cobre o wiring real: criar indicacao de passeador, listar/summary do usuario,
validar codigo, vincular usuario a indicacao e rotas admin (gated por
require_permission). Monta um FastAPI minimo so com os routers do modulo + overrides
de get_db / get_current_user (SQLite em memoria) — NAO importa app.main.

Padrao copiado de tests/test_routes_onda1.py: StaticPool + check_same_thread False.
"""
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import app.models  # noqa: F401  — registra todas as tabelas no Base.metadata
from app.core.database import Base, get_db
from app.dependencies.auth import get_current_user
from app.models.user import User
from app.routes import referrals

TENANT_ID = "t-test"
TUTOR_ID = "tutor-test"
GUEST_ID = "guest-test"
ADMIN_ID = "admin-test"


def build():
    # StaticPool: uma unica conexao compartilhada — senao cada thread do TestClient
    # abre um SQLite em memoria vazio (tabelas somem).
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    db = Session()

    # tutor (pode indicar), guest (vai vincular-se), admin (super_admin -> passa rbac)
    db.add(User(id=TUTOR_ID, email="tutor@test.com", password_hash="x", role="cliente", full_name="Tutor Um", is_active=True))
    db.add(User(id=GUEST_ID, email="guest@test.com", password_hash="x", role="walker", full_name="Guest Dois", is_active=True))
    db.add(User(id=ADMIN_ID, email="admin@test.com", password_hash="x", role="super_admin", full_name="Admin", is_active=True))
    db.commit()

    test_app = FastAPI()
    test_app.include_router(referrals.router)
    test_app.include_router(referrals.admin_router)
    test_app.dependency_overrides[get_db] = lambda: db
    # default: tutor autenticado
    test_app.dependency_overrides[get_current_user] = lambda: db.get(User, TUTOR_ID)
    return TestClient(test_app), db


def auth_as(client, db, user_id):
    client.app.dependency_overrides[get_current_user] = lambda: db.get(User, user_id)


VALID_CREATE = {
    "referred_name": "Joao Passeador",
    "referred_phone": "11987654321",
    "city": "Sao Paulo",
    "neighborhood": "Pinheiros",
    "notes": "amigo confiavel",
}


# ----- create -----
def test_create_walker_referral_happy_path():
    client, _ = build()
    r = client.post("/referrals/walkers", json=VALID_CREATE)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["referred_name"] == "Joao Passeador"
    assert body["status"] == "pending"
    assert body["reward_status"] == "not_eligible"
    assert body["referral_code"].startswith("AUM-")
    assert body["invite_link"].endswith(body["referral_code"])
    assert body["referrer_user_id"] == TUTOR_ID


def test_create_walker_referral_invalid_phone_422():
    client, _ = build()
    payload = {**VALID_CREATE, "referred_phone": "1199999999999"}  # 13 digitos -> invalido
    r = client.post("/referrals/walkers", json=payload)
    assert r.status_code == 422, r.text


def test_create_walker_referral_short_name_validation_422():
    client, _ = build()
    payload = {**VALID_CREATE, "referred_name": "J"}  # min_length=2 (pydantic)
    r = client.post("/referrals/walkers", json=payload)
    assert r.status_code == 422, r.text


def test_create_walker_referral_blocked_for_unapproved_walker_403():
    # guest tem role "walker" sem WalkerProfile aprovado -> ensure_can_refer barra
    client, db = build()
    auth_as(client, db, GUEST_ID)
    r = client.post("/referrals/walkers", json=VALID_CREATE)
    assert r.status_code == 403, r.text


def test_create_walker_referral_duplicate_phone_409():
    client, _ = build()
    assert client.post("/referrals/walkers", json=VALID_CREATE).status_code == 200
    r = client.post("/referrals/walkers", json=VALID_CREATE)
    assert r.status_code == 409, r.text


def test_create_walker_referral_requires_auth_401():
    client, _ = build()
    # remove o override -> get_current_user real exige bearer token
    client.app.dependency_overrides.pop(get_current_user, None)
    r = client.post("/referrals/walkers", json=VALID_CREATE)
    assert r.status_code == 401, r.text


# ----- my list / summary -----
def test_my_walker_referrals_lists_only_own():
    client, db = build()
    client.post("/referrals/walkers", json=VALID_CREATE)
    r = client.get("/referrals/walkers/my")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["total"] == 1
    assert body["items"][0]["referred_name"] == "Joao Passeador"

    # outro usuario nao ve a indicacao do tutor
    auth_as(client, db, ADMIN_ID)
    other = client.get("/referrals/walkers/my").json()
    assert other["total"] == 0


def test_my_walker_referral_summary():
    client, _ = build()
    client.post("/referrals/walkers", json=VALID_CREATE)
    r = client.get("/referrals/walkers/my/summary")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["total"] == 1
    assert body["pending"] == 1  # status "pending" conta como pending
    assert body["approved"] == 0
    assert body["eligible_reward"] == 0.0


# ----- validate code -----
def test_validate_code_valid():
    client, _ = build()
    code = client.post("/referrals/walkers", json=VALID_CREATE).json()["referral_code"]
    r = client.post("/referrals/walkers/validate-code", json={"referral_code": code})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["valid"] is True
    assert body["referral_code"] == code
    assert body["referred_name"] == "Joao Passeador"
    assert body["city"] == "Sao Paulo"


def test_validate_code_invalid_404():
    client, _ = build()
    r = client.post("/referrals/walkers/validate-code", json={"referral_code": "AUM-XXXX-NOPE"})
    assert r.status_code == 404, r.text


# ----- link user -----
def test_link_user_happy_path():
    client, db = build()
    created = client.post("/referrals/walkers", json=VALID_CREATE).json()
    referral_id = created["id"]
    code = created["referral_code"]

    # guest (outro usuario) vincula-se a indicacao
    auth_as(client, db, GUEST_ID)
    r = client.patch(f"/referrals/walkers/{referral_id}/link-user", json={"referral_code": code})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["referred_user_id"] == GUEST_ID
    assert body["status"] == "registered"


def test_link_user_referral_not_found_404():
    client, db = build()
    auth_as(client, db, GUEST_ID)
    r = client.patch("/referrals/walkers/does-not-exist/link-user", json={"referral_code": "AUM-AAA"})
    assert r.status_code == 404, r.text


def test_link_user_code_mismatch_409():
    client, db = build()
    created = client.post("/referrals/walkers", json=VALID_CREATE).json()
    referral_id = created["id"]
    auth_as(client, db, GUEST_ID)
    r = client.patch(f"/referrals/walkers/{referral_id}/link-user", json={"referral_code": "AUM-WRONG-CODE"})
    assert r.status_code == 409, r.text


def test_link_user_own_referral_409():
    client, _ = build()
    created = client.post("/referrals/walkers", json=VALID_CREATE).json()
    referral_id = created["id"]
    code = created["referral_code"]
    # ainda autenticado como o proprio referrer (tutor) -> nao pode usar a propria indicacao
    r = client.patch(f"/referrals/walkers/{referral_id}/link-user", json={"referral_code": code})
    assert r.status_code == 409, r.text


def test_validate_code_already_linked_409():
    client, db = build()
    created = client.post("/referrals/walkers", json=VALID_CREATE).json()
    code = created["referral_code"]
    referral_id = created["id"]
    auth_as(client, db, GUEST_ID)
    client.patch(f"/referrals/walkers/{referral_id}/link-user", json={"referral_code": code})
    # agora a indicacao tem referred_user_id -> validate-code retorna 409
    r = client.post("/referrals/walkers/validate-code", json={"referral_code": code})
    assert r.status_code == 409, r.text


# ----- admin (require_permission referrals.read) -----
def test_admin_list_referrals_as_super_admin():
    client, db = build()
    client.post("/referrals/walkers", json=VALID_CREATE)
    auth_as(client, db, ADMIN_ID)  # super_admin sempre passa em user_has_permission
    r = client.get("/admin/referrals/walkers")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["total"] == 1
    item = body["items"][0]
    assert item["referrer_name"] == "Tutor Um"
    assert item["referrer_role"] == "cliente"


def test_admin_list_referrals_filter_by_status():
    client, db = build()
    client.post("/referrals/walkers", json=VALID_CREATE)
    auth_as(client, db, ADMIN_ID)
    # status "pending" existe
    assert client.get("/admin/referrals/walkers?status=pending").json()["total"] == 1
    # status "approved" nao existe ainda
    assert client.get("/admin/referrals/walkers?status=approved").json()["total"] == 0


def test_admin_list_referrals_forbidden_for_regular_user_403():
    client, _ = build()
    # tutor (cliente) sem assignment de role com permissao referrals.read
    r = client.get("/admin/referrals/walkers")
    assert r.status_code == 403, r.text


def test_admin_update_status_approves_and_sets_reward():
    client, db = build()
    created = client.post("/referrals/walkers", json=VALID_CREATE).json()
    referral_id = created["id"]
    auth_as(client, db, ADMIN_ID)
    r = client.patch(
        f"/admin/referrals/walkers/{referral_id}/status",
        json={"status": "approved"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "approved"
    assert body["reward_status"] == "pending"
    assert body["reward_amount"] == 20.0  # DEFAULT_REWARD_AMOUNT


def test_admin_update_status_invalid_status_422():
    client, db = build()
    created = client.post("/referrals/walkers", json=VALID_CREATE).json()
    referral_id = created["id"]
    auth_as(client, db, ADMIN_ID)
    r = client.patch(
        f"/admin/referrals/walkers/{referral_id}/status",
        json={"status": "nao_existe"},
    )
    assert r.status_code == 422, r.text


def test_admin_update_status_referral_not_found_404():
    client, db = build()
    auth_as(client, db, ADMIN_ID)
    r = client.patch(
        "/admin/referrals/walkers/nope/status",
        json={"status": "approved"},
    )
    assert r.status_code == 404, r.text
