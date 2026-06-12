"""Testes da Fase 5 B — change-password, copy operacional, is_test nos payloads, readiness dinâmico.

Padrão do projeto: FastAPI mínimo, SQLite em memória, StaticPool.
NÃO importa app.main (evita conexão com banco de produção).
"""
from __future__ import annotations

import os
from unittest.mock import patch
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import app.models  # noqa: F401 — registra todas as tabelas no Base.metadata
from app.core.database import Base, get_db
from app.core.security import get_password_hash
from app.dependencies.auth import get_current_user
from app.models.pet import Pet
from app.models.tenant import Tenant
from app.models.user import User
from app.models.walk import Walk
from app.models.payment import Payment
from app.routes import auth as auth_routes
from app.routes import admin as admin_routes
from app.services import beta_readiness_service as readiness_svc
from app.services.login_rate_limiter import InMemoryLoginRateLimiter
from app.services.tenant_seed_service import DEFAULT_TENANT_SLUG

TENANT_ID = "t-fase5b"
SUPER_ID = "super-fase5b"


# ─────────────────────────────────────────────────────── fixtures de DB ──────


def _engine():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    return engine


def _build_auth(*, users: list[dict] | None = None):
    """App mínimo com router de auth + SQLite em memória."""
    engine = _engine()
    Session = sessionmaker(bind=engine)
    db = Session()
    db.add(Tenant(id=TENANT_ID, name="Fase5B", slug=DEFAULT_TENANT_SLUG, status="active", plan="business"))
    for u in users or []:
        db.add(User(**u))
    db.commit()

    test_app = FastAPI()
    test_app.include_router(auth_routes.router)
    test_app.dependency_overrides[get_db] = lambda: db
    return TestClient(test_app), db


def _build_admin():
    """App mínimo com router de admin + super_admin logado."""
    engine = _engine()
    Session = sessionmaker(bind=engine)
    db = Session()
    db.add(User(id=SUPER_ID, email="super@aumigao.app", password_hash="x", role="super_admin", tenant_id=TENANT_ID))
    db.commit()

    test_app = FastAPI()
    test_app.include_router(admin_routes.router)
    test_app.dependency_overrides[get_db] = lambda: db
    test_app.dependency_overrides[get_current_user] = lambda: db.get(User, SUPER_ID)
    return TestClient(test_app), db


def _make_user(uid=None, email="user@aumigao.app", password="senha1234", **extra):
    base = dict(
        id=uid or str(uuid4()),
        email=email,
        password_hash=get_password_hash(password),
        role="tutor",
        tenant_id=TENANT_ID,
        is_active=True,
    )
    base.update(extra)
    return base


# ─────────────────────────────────── Task 1: POST /auth/change-password ──────


@pytest.fixture(autouse=True)
def _reset_change_password_limiter():
    auth_routes._change_password_limiter._failures.clear()
    yield
    auth_routes._change_password_limiter._failures.clear()


UID = "cp-user-1"
EMAIL = "cp@aumigao.app"
CURRENT_PW = "senha1234"
NEW_PW = "novaSenha99"


def _build_auth_with_cp_user():
    client, db = _build_auth(users=[_make_user(uid=UID, email=EMAIL, password=CURRENT_PW)])
    # Override get_current_user para o usuário autenticado
    client.app.dependency_overrides[get_current_user] = lambda: db.get(User, UID)
    return client, db


def test_change_password_success():
    client, db = _build_auth_with_cp_user()
    r = client.post("/auth/change-password", json={
        "current_password": CURRENT_PW,
        "new_password": NEW_PW,
    })
    assert r.status_code == 200, r.text
    body = r.json()
    assert "message" in body
    assert "sucesso" in body["message"].lower()

    # Confirma que o hash mudou no banco
    from app.core.security import verify_password
    user = db.get(User, UID)
    assert verify_password(NEW_PW, user.password_hash)


def test_change_password_wrong_current_400():
    client, _ = _build_auth_with_cp_user()
    r = client.post("/auth/change-password", json={
        "current_password": "errada99",
        "new_password": NEW_PW,
    })
    assert r.status_code == 400, r.text
    assert "atual" in r.json()["detail"].lower() or "incorreta" in r.json()["detail"].lower()


def test_change_password_weak_new_password_400():
    client, _ = _build_auth_with_cp_user()
    # Nova senha sem letra
    r = client.post("/auth/change-password", json={
        "current_password": CURRENT_PW,
        "new_password": "12345678",
    })
    assert r.status_code == 400, r.text
    # Nova senha sem dígito
    r2 = client.post("/auth/change-password", json={
        "current_password": CURRENT_PW,
        "new_password": "soletras",
    })
    assert r2.status_code == 400, r2.text
    # Nova senha muito curta
    r3 = client.post("/auth/change-password", json={
        "current_password": CURRENT_PW,
        "new_password": "ab1",
    })
    assert r3.status_code == 400, r3.text


def test_change_password_rate_limit_429():
    client, _ = _build_auth_with_cp_user()
    # 5 tentativas com senha errada (max_failures=5, janela=900s)
    for _ in range(5):
        client.post("/auth/change-password", json={
            "current_password": "errada99",
            "new_password": NEW_PW,
        })
    # 6ª tentativa deve ser bloqueada com 429
    r = client.post("/auth/change-password", json={
        "current_password": CURRENT_PW,  # senha certa — mas rate limit já bloqueia
        "new_password": NEW_PW,
    })
    assert r.status_code == 429, r.text
    assert "tentativas" in r.json()["detail"].lower() or "muitas" in r.json()["detail"].lower()


def test_change_password_requires_auth_401():
    """Sem override de get_current_user, deve retornar 401."""
    client, db = _build_auth(users=[_make_user(uid=UID, email=EMAIL, password=CURRENT_PW)])
    # Sem override: get_current_user real vai buscar token Bearer no header
    r = client.post("/auth/change-password", json={
        "current_password": CURRENT_PW,
        "new_password": NEW_PW,
    })
    assert r.status_code == 401, r.text


# ─────────────────────────────────── Task 4: beta_readiness dinâmico ──────────


def test_readiness_payment_ok_asaas_sandbox():
    with patch.dict(os.environ, {"PAYMENT_MODE": "asaas_sandbox"}, clear=False):
        item = readiness_svc._payment_provider_item()
    assert item.status == readiness_svc.OK
    assert "asaas_sandbox" in item.message


def test_readiness_payment_ok_asaas_live_with_key():
    with patch.dict(os.environ, {"PAYMENT_MODE": "asaas_live", "ASAAS_LIVE_API_KEY": "key_live_xxx"}, clear=False):
        item = readiness_svc._payment_provider_item()
    assert item.status == readiness_svc.OK
    assert "asaas_live" in item.message


def test_readiness_payment_attention_asaas_live_no_key():
    env = {"PAYMENT_MODE": "asaas_live"}
    # Remove ASAAS_LIVE_API_KEY se existir
    env_without_key = {k: v for k, v in os.environ.items() if k != "ASAAS_LIVE_API_KEY"}
    env_without_key["PAYMENT_MODE"] = "asaas_live"
    with patch.dict(os.environ, env_without_key, clear=True):
        item = readiness_svc._payment_provider_item()
    assert item.status == readiness_svc.ATTENTION
    assert "ASAAS_LIVE_API_KEY" in item.message


def test_readiness_payment_attention_unknown_mode():
    with patch.dict(os.environ, {"PAYMENT_MODE": "stripe"}, clear=False):
        item = readiness_svc._payment_provider_item()
    assert item.status == readiness_svc.ATTENTION
    assert "stripe" in item.message or "aceitos" in item.message


def test_readiness_payment_attention_empty_mode():
    env_without_mode = {k: v for k, v in os.environ.items() if k != "PAYMENT_MODE"}
    with patch.dict(os.environ, env_without_mode, clear=True):
        item = readiness_svc._payment_provider_item()
    assert item.status == readiness_svc.ATTENTION


def test_readiness_overall_status_renamed():
    """_overall_status deve retornar 'Pronto para operação' quando todos OK."""
    # Usa um item de status OK para forçar o caminho feliz
    item = readiness_svc.ReadinessItem(
        key="test", label="Test", status=readiness_svc.OK, message="ok"
    )
    result = readiness_svc._overall_status([item])
    assert result == "Pronto para operação"


# ─────────────────────────────── Task 5: is_test nos serializers de admin ─────


def test_is_test_user_false_for_real_user():
    _, db = _build_admin()
    # Usuário real: email sem tokens fake
    real_uid = str(uuid4())
    db.add(User(id=real_uid, email="real@aumigao.app", password_hash="x", role="tutor", tenant_id=TENANT_ID))
    db.commit()
    user = db.get(User, real_uid)
    assert admin_routes._is_fake_user(user) is False
    result = admin_routes._serialize_admin_user(user)
    assert result["is_test"] is False


def test_is_test_user_true_for_fake_user():
    _, db = _build_admin()
    fake_uid = str(uuid4())
    db.add(User(id=fake_uid, email="test@test.local", password_hash="x", role="tutor", tenant_id=TENANT_ID))
    db.commit()
    user = db.get(User, fake_uid)
    assert admin_routes._is_fake_user(user) is True
    result = admin_routes._serialize_admin_user(user)
    assert result["is_test"] is True


def test_is_test_tutor_serializer():
    _, db = _build_admin()
    real_uid = str(uuid4())
    db.add(User(id=real_uid, email="tutorreal@aumigao.app", password_hash="x", role="tutor", tenant_id=TENANT_ID))
    db.commit()
    user = db.get(User, real_uid)
    result = admin_routes._serialize_admin_tutor(user, db)
    assert "is_test" in result
    assert result["is_test"] is False  # email real → não é teste


def test_is_test_tutor_serializer_fake():
    _, db = _build_admin()
    fake_uid = str(uuid4())
    db.add(User(id=fake_uid, email="demo@test.local", password_hash="x", role="tutor", tenant_id=TENANT_ID))
    db.commit()
    user = db.get(User, fake_uid)
    result = admin_routes._serialize_admin_tutor(user, db)
    assert "is_test" in result
    assert result["is_test"] is True


def test_is_test_pet_serializer_false():
    _, db = _build_admin()
    tutor_uid = str(uuid4())
    pet_uid = str(uuid4())
    db.add(User(id=tutor_uid, email="dono@aumigao.app", password_hash="x", role="tutor", tenant_id=TENANT_ID))
    db.add(Pet(id=pet_uid, tutor_id=tutor_uid, tenant_id=TENANT_ID, name="Bolinha", species="Cachorro"))
    db.commit()
    pet = db.get(Pet, pet_uid)
    result = admin_routes._serialize_admin_pet(pet, db)
    assert "is_test" in result
    assert result["is_test"] is False


def test_is_test_pet_serializer_true():
    _, db = _build_admin()
    fake_uid = str(uuid4())
    pet_uid = str(uuid4())
    db.add(User(id=fake_uid, email="fake-demo@test.local", password_hash="x", role="tutor", tenant_id=TENANT_ID))
    db.add(Pet(id=pet_uid, tutor_id=fake_uid, tenant_id=TENANT_ID, name="PetDemo", species="Cachorro"))
    db.commit()
    pet = db.get(Pet, pet_uid)
    result = admin_routes._serialize_admin_pet(pet, db)
    assert "is_test" in result
    # Pet cujo tutor tem email fake → is_test=True
    assert result["is_test"] is True


def test_is_test_payment_serializer_false():
    _, db = _build_admin()
    tutor_uid = str(uuid4())
    walk_uid = str(uuid4())
    pay_uid = str(uuid4())
    db.add(User(id=tutor_uid, email="paytutor@aumigao.app", password_hash="x", role="tutor", tenant_id=TENANT_ID))
    db.add(Walk(id=walk_uid, tutor_id=tutor_uid, pet_id="pet-x", scheduled_date="2026-07-01T10:00:00",
                duration_minutes=30, price=50.0, status="Agendado", tenant_id=TENANT_ID))
    db.add(Payment(id=pay_uid, tutor_id=tutor_uid, walk_id=walk_uid, amount=50.0, status="paid",
                   provider="asaas_sandbox", provider_payment_id="pay_real_1", tenant_id=TENANT_ID))
    db.commit()
    payment = db.get(Payment, pay_uid)
    result = admin_routes._serialize_admin_payment(payment, db)
    assert "is_test" in result
    assert result["is_test"] is False


def test_is_test_payment_serializer_true():
    _, db = _build_admin()
    tutor_uid = str(uuid4())
    walk_uid = str(uuid4())
    pay_uid = str(uuid4())
    db.add(User(id=tutor_uid, email="paytutor@aumigao.app", password_hash="x", role="tutor", tenant_id=TENANT_ID))
    db.add(Walk(id=walk_uid, tutor_id=tutor_uid, pet_id="pet-x", scheduled_date="2026-07-01T10:00:00",
                duration_minutes=30, price=50.0, status="Agendado", tenant_id=TENANT_ID))
    db.add(Payment(id=pay_uid, tutor_id=tutor_uid, walk_id=walk_uid, amount=50.0, status="paid",
                   provider="test-mock", provider_payment_id="pay_demo_1", tenant_id=TENANT_ID))
    db.commit()
    payment = db.get(Payment, pay_uid)
    result = admin_routes._serialize_admin_payment(payment, db)
    assert "is_test" in result
    assert result["is_test"] is True  # provider="test-mock" contém token "test"


def test_admin_users_endpoint_returns_is_test():
    """GET /admin/users deve incluir campo is_test em cada item."""
    client, db = _build_admin()
    real_uid = str(uuid4())
    db.add(User(id=real_uid, email="real2@aumigao.app", password_hash="x", role="tutor", tenant_id=TENANT_ID))
    db.commit()
    r = client.get("/admin/users")
    assert r.status_code == 200, r.text
    body = r.json()
    assert isinstance(body, list)
    for item in body:
        assert "is_test" in item, f"Faltou is_test em: {item}"
