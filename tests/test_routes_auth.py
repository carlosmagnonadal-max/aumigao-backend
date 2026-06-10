"""Testes de ROTA (camada HTTP) do modulo app/routes/auth.py.

Padrao do projeto (ver tests/test_routes_onda1.py): monta um FastAPI MINIMO com
apenas o router de auth, SQLite em memoria (StaticPool), overrides de get_db /
get_current_user. NAO importa app.main (que conecta no banco de PROD).

Cobre POST /auth/register (validacao de senha/cpf/unicidade), POST /auth/login
(credenciais, usuario inativo, rate limit) e GET /auth/me (happy path + 401).
"""
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import app.models  # noqa: F401 - registra todas as tabelas no Base.metadata
from app.core.database import Base, get_db
from app.core.security import get_password_hash
from app.dependencies.auth import get_current_user
from app.models.tenant import Tenant
from app.models.user import User
from app.routes import auth
from app.services.login_rate_limiter import login_rate_limiter
from app.services.tenant_seed_service import DEFAULT_TENANT_SLUG

TENANT_ID = "t-test"

# CPF e telefone validos (passam pelos validadores do projeto).
VALID_CPF = "52998224725"
VALID_PHONE = "11987654321"


def build(*, users: list[dict] | None = None):
    """Monta app minimo com o router de auth e um SQLite em memoria isolado."""
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    db = Session()

    # slug = DEFAULT para default_tenant_id() resolver este tenant sem criar outro.
    db.add(Tenant(id=TENANT_ID, name="Aumigao", slug=DEFAULT_TENANT_SLUG, status="active", plan="business"))
    for u in users or []:
        db.add(User(**u))
    db.commit()

    test_app = FastAPI()
    test_app.include_router(auth.router)
    test_app.dependency_overrides[get_db] = lambda: db
    # get_current_user real continua valendo, exceto quando o teste sobrescreve.
    return TestClient(test_app), db


def make_user(uid="u-existing", email="existing@test.com", password="senha1234", **extra):
    base = dict(
        id=uid,
        email=email,
        password_hash=get_password_hash(password),
        role="tutor",
        tenant_id=TENANT_ID,
        is_active=True,
    )
    base.update(extra)
    return base


@pytest.fixture(autouse=True)
def _reset_rate_limiter():
    # O limiter e um singleton de modulo: zera o estado entre testes.
    login_rate_limiter._failures.clear()
    yield
    login_rate_limiter._failures.clear()


# ---------------------------------------------------------------- register ---
def test_register_happy_path_tutor():
    client, db = build()
    r = client.post("/auth/register", json={
        "email": "novo@test.com",
        "password": "senha1234",
        "full_name": "Novo Tutor",
        "role": "cliente",  # cliente vira tutor
    })
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["access_token"]
    assert body["token_type"] == "bearer"
    assert body["user"]["email"] == "novo@test.com"
    assert body["user"]["role"] == "tutor"  # cliente -> tutor
    assert body["user"]["is_active"] is True
    # usuario realmente persistido
    assert db.query(User).filter(User.email == "novo@test.com").count() == 1


def test_register_rejects_weak_password_short():
    client, _ = build()
    r = client.post("/auth/register", json={
        "email": "weak@test.com", "password": "abc12", "role": "tutor",
    })
    assert r.status_code == 400
    assert "senha" in r.json()["detail"].lower()


def test_register_rejects_password_without_digit():
    client, _ = build()
    r = client.post("/auth/register", json={
        "email": "weak2@test.com", "password": "somenteletras", "role": "tutor",
    })
    assert r.status_code == 400


def test_register_rejects_password_without_letter():
    client, _ = build()
    r = client.post("/auth/register", json={
        "email": "weak3@test.com", "password": "12345678", "role": "tutor",
    })
    assert r.status_code == 400


def test_register_rejects_invalid_email():
    client, _ = build()
    r = client.post("/auth/register", json={
        "email": "nao-eh-email", "password": "senha1234", "role": "tutor",
    })
    assert r.status_code == 400
    assert "e-mail" in r.json()["detail"].lower()


def test_register_rejects_invalid_cpf():
    client, _ = build()
    r = client.post("/auth/register", json={
        "email": "cpf@test.com", "password": "senha1234", "role": "tutor",
        "cpf": "11111111111",  # CPF invalido (todos iguais)
    })
    assert r.status_code == 400
    assert "cpf" in r.json()["detail"].lower()


def test_register_duplicate_email_returns_409():
    client, _ = build(users=[make_user(email="dup@test.com")])
    r = client.post("/auth/register", json={
        "email": "dup@test.com", "password": "senha1234", "role": "tutor",
    })
    assert r.status_code == 409
    assert "e-mail" in r.json()["detail"].lower()


def test_register_tutor_with_valid_cpf_creates_profile():
    from app.models.tutor_profile import TutorProfile
    client, db = build()
    r = client.post("/auth/register", json={
        "email": "tutorcpf@test.com", "password": "senha1234", "role": "tutor",
        "full_name": "Tutor Com CPF", "cpf": VALID_CPF, "phone": VALID_PHONE,
    })
    assert r.status_code == 200, r.text
    profile = db.query(TutorProfile).filter(TutorProfile.cpf == VALID_CPF).first()
    assert profile is not None
    assert profile.phone == VALID_PHONE


def test_register_duplicate_cpf_returns_409():
    from app.models.tutor_profile import TutorProfile
    client, db = build(users=[make_user()])
    db.add(TutorProfile(id="tp1", user_id="u-existing", tenant_id=TENANT_ID,
                        full_name="X", cpf=VALID_CPF, phone=VALID_PHONE))
    db.commit()
    r = client.post("/auth/register", json={
        "email": "outro@test.com", "password": "senha1234", "role": "tutor",
        "cpf": VALID_CPF,
    })
    assert r.status_code == 409
    assert "cpf" in r.json()["detail"].lower()


# ------------------------------------------------------------------- login ---
def test_login_happy_path():
    client, _ = build(users=[make_user(email="login@test.com", password="senha1234")])
    r = client.post("/auth/login", json={"email": "login@test.com", "password": "senha1234"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["access_token"]
    assert body["user"]["email"] == "login@test.com"


def test_login_wrong_password_401():
    client, _ = build(users=[make_user(email="login@test.com", password="senha1234")])
    r = client.post("/auth/login", json={"email": "login@test.com", "password": "errada99"})
    assert r.status_code == 401
    assert "invalid" in r.json()["detail"].lower() or "credenc" in r.json()["detail"].lower()


def test_login_unknown_email_401():
    client, _ = build()
    r = client.post("/auth/login", json={"email": "naoexiste@test.com", "password": "senha1234"})
    assert r.status_code == 401


def test_login_inactive_user_403():
    client, _ = build(users=[make_user(email="inativo@test.com", password="senha1234", is_active=False)])
    r = client.post("/auth/login", json={"email": "inativo@test.com", "password": "senha1234"})
    assert r.status_code == 403
    assert "inativo" in r.json()["detail"].lower()


def test_login_invalid_email_format_400():
    client, _ = build()
    r = client.post("/auth/login", json={"email": "sem-arroba", "password": "senha1234"})
    assert r.status_code == 400


def test_login_rate_limit_blocks_after_max_failures():
    client, _ = build(users=[make_user(email="rl@test.com", password="senha1234")])
    # 5 falhas consecutivas (max_failures padrao = 5)
    for _ in range(5):
        assert client.post("/auth/login", json={"email": "rl@test.com", "password": "errada99"}).status_code == 401
    # 6a tentativa, mesmo com senha CORRETA, bloqueia com 429
    r = client.post("/auth/login", json={"email": "rl@test.com", "password": "senha1234"})
    assert r.status_code == 429
    assert "tentativas" in r.json()["detail"].lower()


def test_login_success_clears_rate_limit_counter():
    client, _ = build(users=[make_user(email="clear@test.com", password="senha1234")])
    # 4 falhas (abaixo do limite), depois sucesso -> limpa contador
    for _ in range(4):
        client.post("/auth/login", json={"email": "clear@test.com", "password": "errada99"})
    assert client.post("/auth/login", json={"email": "clear@test.com", "password": "senha1234"}).status_code == 200
    # contador limpo: 4 novas falhas ainda nao bloqueiam
    for _ in range(4):
        assert client.post("/auth/login", json={"email": "clear@test.com", "password": "errada99"}).status_code == 401


# --------------------------------------------------------------------- me ----
def test_me_requires_auth_401():
    client, _ = build()
    # sem override e sem Authorization header -> get_current_user real -> 401
    r = client.get("/auth/me")
    assert r.status_code == 401


def test_me_returns_current_user():
    client, db = build(users=[make_user(uid="me-user", email="me@test.com")])
    client.app.dependency_overrides[get_current_user] = lambda: db.get(User, "me-user")
    r = client.get("/auth/me")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["id"] == "me-user"
    assert body["email"] == "me@test.com"
    assert body["role"] == "tutor"
