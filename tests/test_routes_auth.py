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
from app.middleware.tenant_resolver import TenantResolverMiddleware
from app.models.tenant import Tenant
from app.models.user import User
from app.routes import auth
from app.routes.auth import _register_rate_limiter, _social_rate_limiter
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


PREMIUM_TENANT_ID = "t-premium"
PREMIUM_TENANT_SLUG = "premium"


def build_multitenant():
    """App de auth COM o TenantResolverMiddleware montado + 2 tenants (default + premium).

    Exercita a resolucao de tenant por X-Tenant-Slug no /auth/register (split Fase 3).
    O `build()` padrao NAO monta o middleware, por isso la o register sempre cai no default.
    """
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    db = Session()
    db.add(Tenant(id=TENANT_ID, name="Aumigao", slug=DEFAULT_TENANT_SLUG, status="active", plan="business"))
    db.add(Tenant(id=PREMIUM_TENANT_ID, name="Premium", slug=PREMIUM_TENANT_SLUG, status="active", plan="business"))
    db.commit()

    test_app = FastAPI()
    test_app.include_router(auth.router)
    # session_factory = sessionmaker: o middleware abre `with Session() as db` por request.
    test_app.add_middleware(TenantResolverMiddleware, session_factory=Session)
    test_app.dependency_overrides[get_db] = lambda: db
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
    # Os limiters são singletons de módulo: zera o estado entre testes.
    login_rate_limiter._failures.clear()
    _register_rate_limiter._failures.clear()
    _social_rate_limiter._failures.clear()
    yield
    login_rate_limiter._failures.clear()
    _register_rate_limiter._failures.clear()
    _social_rate_limiter._failures.clear()


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


def test_register_uses_tenant_from_header_slug():
    """Split Fase 3: com X-Tenant-Slug (build dedicado), o tutor entra no tenant do build."""
    client, db = build_multitenant()
    r = client.post(
        "/auth/register",
        json={"email": "premium@test.com", "password": "senha1234", "full_name": "Tutor Premium", "role": "cliente"},
        headers={"X-Tenant-Slug": PREMIUM_TENANT_SLUG},
    )
    assert r.status_code == 200, r.text
    user = db.query(User).filter(User.email == "premium@test.com").one()
    assert user.tenant_id == PREMIUM_TENANT_ID


def test_register_without_header_falls_back_to_default_tenant():
    """Sem header (combined/walker), preserva o comportamento atual: tenant default."""
    client, db = build_multitenant()
    r = client.post(
        "/auth/register",
        json={"email": "semheader@test.com", "password": "senha1234", "full_name": "Tutor Default", "role": "cliente"},
    )
    assert r.status_code == 200, r.text
    user = db.query(User).filter(User.email == "semheader@test.com").one()
    assert user.tenant_id == TENANT_ID  # default (slug=aumigao)


def test_register_rejects_weak_password_short():
    client, _ = build()
    r = client.post("/auth/register", json={
        "email": "weak@test.com", "password": "abc12", "role": "tutor",
    })
    # Schema validator (min_length=8) returns 422; route validator returns 400.
    # Either status means the short password was correctly rejected.
    assert r.status_code in {400, 422}


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
    # Schema validator (EmailStr) returns 422; route validator returns 400.
    # Either status means the invalid email was correctly rejected.
    assert r.status_code in {400, 422}


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
    from app.core.pii_crypto import blind_index
    from app.models.tutor_profile import TutorProfile
    client, db = build()
    r = client.post("/auth/register", json={
        "email": "tutorcpf@test.com", "password": "senha1234", "role": "tutor",
        "full_name": "Tutor Com CPF", "cpf": VALID_CPF, "phone": VALID_PHONE,
    })
    assert r.status_code == 200, r.text
    # CPF é cifrado no banco — buscar pelo blind index (determinístico).
    profile = db.query(TutorProfile).filter(
        TutorProfile.cpf_bidx == blind_index(VALID_CPF)
    ).first()
    assert profile is not None
    # ORM decifra o CPF ao ler — deve retornar o valor original.
    assert profile.cpf == VALID_CPF
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
    # tenant_id deve estar presente na resposta (plumbing para provisão fiscal)
    assert "tenant_id" in body["user"]
    assert body["user"]["tenant_id"] == TENANT_ID


def test_user_response_includes_tenant_id():
    """UserResponse.model_validate expõe tenant_id do objeto User."""
    from app.schemas.user import UserResponse
    from app.models.user import User as UserModel
    from datetime import timezone

    user = UserModel(
        id="u-schema-test",
        email="schema@test.com",
        full_name="Schema Test",
        role="admin",
        is_active=True,
        tenant_id="t1",
        password_hash="x",
        created_at=__import__("datetime").datetime(2024, 1, 1, tzinfo=timezone.utc),
    )
    result = UserResponse.model_validate(user)
    assert result.tenant_id == "t1"


def test_user_response_tenant_id_none_when_missing():
    """tenant_id deve ser None quando o User não tem tenant_id."""
    from app.schemas.user import UserResponse
    from app.models.user import User as UserModel
    from datetime import timezone

    user = UserModel(
        id="u-no-tenant",
        email="notenant@test.com",
        full_name="No Tenant",
        role="tutor",
        is_active=True,
        tenant_id=None,
        password_hash="x",
        created_at=__import__("datetime").datetime(2024, 1, 1, tzinfo=timezone.utc),
    )
    result = UserResponse.model_validate(user)
    assert result.tenant_id is None


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


# -------------------------------------------------------- B2: must_change_password ----

def test_login_exposes_must_change_password_flag():
    """Login retorna must_change_password no objeto user — o admin-web usa para redirecionar."""
    client, _ = build(users=[make_user(uid="admin-b2", email="admin-b2@test.com",
                                       password="Senha@1234", role="admin",
                                       must_change_password=True)])
    r = client.post("/auth/login", json={"email": "admin-b2@test.com", "password": "Senha@1234"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert "must_change_password" in body["user"], "campo must_change_password ausente no user do login"
    assert body["user"]["must_change_password"] is True


def test_login_must_change_password_false_for_normal_users():
    """Usuarios normais (must_change_password=False) recebem a flag como False no login."""
    client, _ = build(users=[make_user(uid="u-normal", email="normal@test.com",
                                       password="senha1234", must_change_password=False)])
    r = client.post("/auth/login", json={"email": "normal@test.com", "password": "senha1234"})
    assert r.status_code == 200, r.text
    assert r.json()["user"]["must_change_password"] is False


def test_change_password_clears_must_change_password():
    """Apos /auth/change-password, must_change_password deve ser False no banco."""
    from app.core.security import get_password_hash as _hash

    client, db = build(users=[make_user(uid="u-chpwd", email="chpwd@test.com",
                                        password="Senha@1234", must_change_password=True)])
    # Forca o usuario autenticado via override
    client.app.dependency_overrides[get_current_user] = lambda: db.get(User, "u-chpwd")

    r = client.post("/auth/change-password", json={
        "current_password": "Senha@1234",
        "new_password": "NovaSenh@5678",
    })
    assert r.status_code == 200, r.text

    db.expire_all()
    user = db.get(User, "u-chpwd")
    assert user.must_change_password is False, "must_change_password deveria ser False apos troca"
