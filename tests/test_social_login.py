"""Testes de ROTA (camada HTTP) para POST /auth/social (social_login).

Padrao do projeto: monta FastAPI minimo com router de auth, SQLite StaticPool,
override de get_db. Mocka _google_user_info e _decode_apple_jwt_payload via
monkeypatch para evitar chamadas de rede.

Cobre:
- Google: novo usuario criado (role=tutor) com token retornado
- Google: usuario existente retorna sessao sem duplicar
- Google: token invalido → 401
- Provider desconhecido → 400
- Email vazio → 400
- app_target=walker + email novo → 403
- Rate limit por IP → 429
"""
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import app.models  # noqa: F401 – registra todas as tabelas
from app.core.database import Base, get_db
from app.core.security import get_password_hash
from app.models.tenant import Tenant
from app.models.user import User
from app.routes import auth
from app.routes.auth import _social_rate_limiter
from app.services.tenant_seed_service import DEFAULT_TENANT_SLUG

TENANT_ID = "t-social"


def build(*, extra_users: list[dict] | None = None):
    """Monta app minimo com router de auth e SQLite em memoria isolado."""
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    db = Session()

    db.add(Tenant(id=TENANT_ID, name="Aumigao", slug=DEFAULT_TENANT_SLUG, status="active", plan="business"))
    for u in extra_users or []:
        db.add(User(**u))
    db.commit()

    test_app = FastAPI()
    test_app.include_router(auth.router)
    test_app.dependency_overrides[get_db] = lambda: db
    return TestClient(test_app), db


@pytest.fixture(autouse=True)
def _clear_social_limiter():
    _social_rate_limiter._failures.clear()
    yield
    _social_rate_limiter._failures.clear()


# ------------------------------------------------------------------ helpers ---

def _mock_google_ok(email: str, name: str = "Test User"):
    """Retorna corrotina que imita _google_user_info com sucesso."""
    async def _coro(token: str):
        return {"email": email, "name": name}
    return _coro


def _mock_google_fail():
    """Retorna corrotina que imita _google_user_info com token invalido."""
    from fastapi import HTTPException
    async def _coro(token: str):
        raise HTTPException(status_code=401, detail="Token Google invalido.")
    return _coro


def _mock_apple_ok(email: str):
    """Retorna callable que imita _decode_apple_jwt_payload com sucesso."""
    def _decode(token: str):
        return {"email": email, "sub": "apple-sub-123"}
    return _decode


def _mock_apple(sub: str, email: str | None = None):
    """Imita _decode_apple_jwt_payload com sub fixo e email opcional (verificado)."""
    def _decode(token: str):
        data = {"sub": sub}
        if email is not None:
            data["email"] = email
        return data
    return _decode


# ------------------------------------------------------------------ testes ---

def test_google_new_user_creates_tutor_and_returns_tokens(monkeypatch):
    """Novo email via Google cria usuario com role=tutor e retorna access_token."""
    monkeypatch.setattr(auth, "_google_user_info", _mock_google_ok("novo@google.com", "Novo Usuario"))
    client, db = build()

    r = client.post("/auth/social", json={"provider": "google", "token": "gtoken-abc"})

    assert r.status_code == 200, r.text
    body = r.json()
    assert body["access_token"]
    assert body["token_type"] == "bearer"
    assert body["user"]["email"] == "novo@google.com"
    assert body["user"]["role"] == "tutor"
    assert body["user"]["is_active"] is True
    # usuario realmente persistido no banco
    assert db.query(User).filter(User.email == "novo@google.com").count() == 1


def test_google_existing_user_returns_session_without_duplicating(monkeypatch):
    """Email ja cadastrado retorna sessao sem criar segundo registro."""
    monkeypatch.setattr(auth, "_google_user_info", _mock_google_ok("existente@google.com"))
    client, db = build(extra_users=[
        dict(id="u-existing-g", email="existente@google.com", password_hash="",
             role="tutor", tenant_id=TENANT_ID, is_active=True)
    ])

    r = client.post("/auth/social", json={"provider": "google", "token": "gtoken-old"})

    assert r.status_code == 200, r.text
    assert r.json()["user"]["email"] == "existente@google.com"
    # Nao duplicou
    assert db.query(User).filter(User.email == "existente@google.com").count() == 1


def test_google_invalid_token_returns_401(monkeypatch):
    """Token Google invalido levanta HTTPException 401 do _google_user_info."""
    monkeypatch.setattr(auth, "_google_user_info", _mock_google_fail())
    client, _ = build()

    r = client.post("/auth/social", json={"provider": "google", "token": "token-lixo"})

    assert r.status_code == 401


def test_unknown_provider_returns_400():
    """Provider que nao e google/apple retorna 400."""
    client, _ = build()

    r = client.post("/auth/social", json={"provider": "facebook", "token": "fbtoken"})

    assert r.status_code == 400
    assert "provider" in r.json()["detail"].lower() or "invalid" in r.json()["detail"].lower()


def test_google_empty_email_returns_400(monkeypatch):
    """Se o token Google nao carregar email (vazio), retorna 400."""
    async def _no_email(token):
        return {"email": "", "name": "X"}
    monkeypatch.setattr(auth, "_google_user_info", _no_email)
    client, _ = build()

    r = client.post("/auth/social", json={"provider": "google", "token": "gtoken-noemail"})

    assert r.status_code == 400
    assert "email" in r.json()["detail"].lower()


def test_walker_app_target_blocks_new_user_with_403(monkeypatch):
    """app_target=walker com email novo → 403 (passeador precisa do fluxo de cadastro)."""
    monkeypatch.setattr(auth, "_google_user_info", _mock_google_ok("walker-novo@google.com"))
    client, db = build()

    r = client.post("/auth/social", json={
        "provider": "google",
        "token": "gtoken-walker",
        "app_target": "walker",
    })

    assert r.status_code == 403
    detail = r.json()["detail"].lower()
    assert "passead" in detail or "candidatura" in detail or "cadastro" in detail
    # Nao deve ter criado o usuario
    assert db.query(User).filter(User.email == "walker-novo@google.com").count() == 0


def test_walker_app_target_existing_user_can_login(monkeypatch):
    """app_target=walker com usuario JA cadastrado retorna sessao normalmente."""
    monkeypatch.setattr(auth, "_google_user_info", _mock_google_ok("walker-exist@google.com"))
    client, db = build(extra_users=[
        dict(id="u-walker-g", email="walker-exist@google.com", password_hash="",
             role="walker", tenant_id=TENANT_ID, is_active=True)
    ])

    r = client.post("/auth/social", json={
        "provider": "google",
        "token": "gtoken-walker-ok",
        "app_target": "walker",
    })

    # Nao e usuario novo, entao nao cai no bloqueio 403
    assert r.status_code == 200, r.text
    assert r.json()["user"]["role"] == "walker"


# ---------------------------------------------------------- Apple: account-takeover ---

def test_apple_no_email_with_spoofed_payload_email_does_not_takeover(monkeypatch):
    """SEC: token Apple SEM email + payload.email da vítima NÃO autentica como a vítima.

    Cenário do account-takeover: atacante tem token Apple genuíno (sub próprio, sem
    claim de email) e envia payload.email = e-mail da vítima. Como não há match por
    sub e o token não traz email verificado, deve ser rejeitado (401) — nunca virar
    sessão da vítima.
    """
    # Vítima existente, ainda SEM apple_sub vinculado.
    monkeypatch.setattr(auth, "_decode_apple_jwt_payload", _mock_apple("attacker-sub", email=None))
    client, db = build(extra_users=[
        dict(id="u-vitima", email="vitima@x.com", password_hash="hash",
             role="tutor", tenant_id=TENANT_ID, is_active=True)
    ])

    r = client.post("/auth/social", json={
        "provider": "apple",
        "token": "apple-token-genuino-sem-email",
        "email": "vitima@x.com",       # client-supplied — deve ser IGNORADO
        "full_name": "Atacante",
    })

    assert r.status_code == 401, r.text
    # A vítima NÃO deve ter recebido apple_sub do atacante.
    db.expire_all()
    vitima = db.query(User).filter(User.email == "vitima@x.com").one()
    assert vitima.apple_sub is None


def test_apple_linked_sub_authenticates_without_email(monkeypatch):
    """Token Apple com sub JÁ vinculado autentica a conta certa mesmo sem email."""
    monkeypatch.setattr(auth, "_decode_apple_jwt_payload", _mock_apple("linked-sub", email=None))
    client, db = build(extra_users=[
        dict(id="u-apple", email="apple-user@x.com", password_hash="",
             role="tutor", tenant_id=TENANT_ID, is_active=True, apple_sub="linked-sub")
    ])

    r = client.post("/auth/social", json={
        "provider": "apple",
        "token": "apple-token-login-subsequente",
        "email": "outra-coisa@x.com",  # ignorado
    })

    assert r.status_code == 200, r.text
    assert r.json()["user"]["email"] == "apple-user@x.com"


def test_apple_first_time_verified_email_creates_and_links_sub(monkeypatch):
    """1ª vez com email VERIFICADO no token cria a conta e persiste apple_sub."""
    monkeypatch.setattr(auth, "_decode_apple_jwt_payload", _mock_apple("novo-sub", email="novo@apple.com"))
    client, db = build()

    r = client.post("/auth/social", json={
        "provider": "apple",
        "token": "apple-token-primeira-vez",
        "full_name": "Novo Apple",
    })

    assert r.status_code == 200, r.text
    assert r.json()["user"]["email"] == "novo@apple.com"
    user = db.query(User).filter(User.email == "novo@apple.com").one()
    assert user.apple_sub == "novo-sub"


def test_apple_verified_email_links_sub_to_existing_account(monkeypatch):
    """Conta existente (por email verificado) recebe o apple_sub no 1º login Apple."""
    monkeypatch.setattr(auth, "_decode_apple_jwt_payload", _mock_apple("link-sub", email="existe@apple.com"))
    client, db = build(extra_users=[
        dict(id="u-existe", email="existe@apple.com", password_hash="hash",
             role="tutor", tenant_id=TENANT_ID, is_active=True)
    ])

    r = client.post("/auth/social", json={
        "provider": "apple",
        "token": "apple-token-link",
    })

    assert r.status_code == 200, r.text
    db.expire_all()
    user = db.query(User).filter(User.email == "existe@apple.com").one()
    assert user.apple_sub == "link-sub"


# ------------------------------------------------- Google: validação de audience ---

def _mock_tokeninfo(client_id: str):
    async def _coro(access_token: str):
        return {"issued_to": client_id, "azp": client_id, "email": "x@google.com"}
    return _coro


def test_google_audience_outside_allowlist_rejected(monkeypatch):
    """SEC (confused deputy): access_token emitido para OUTRO app (issued_to fora da
    allowlist) é rejeitado com 401, antes mesmo de buscar o userinfo."""
    monkeypatch.setattr(auth, "_google_tokeninfo", _mock_tokeninfo("app-do-atacante.apps.googleusercontent.com"))

    async def _boom_userinfo(token):
        raise AssertionError("userinfo NÃO deveria ser chamado quando o audience é inválido")
    monkeypatch.setattr(auth, "_google_fetch_userinfo", _boom_userinfo)

    client, _ = build()
    r = client.post("/auth/social", json={"provider": "google", "token": "token-de-outro-app"})

    assert r.status_code == 401, r.text
    assert "aplicativo" in r.json()["detail"].lower() or "emitido" in r.json()["detail"].lower()


def test_google_audience_in_allowlist_ok(monkeypatch):
    """access_token com issued_to dentro da allowlist segue normalmente e loga o usuário."""
    from app.routes.auth import _google_allowed_client_ids
    good_id = _google_allowed_client_ids()[0]
    monkeypatch.setattr(auth, "_google_tokeninfo", _mock_tokeninfo(good_id))

    async def _userinfo(token):
        return {"email": "ok@google.com", "name": "OK User"}
    monkeypatch.setattr(auth, "_google_fetch_userinfo", _userinfo)

    client, db = build()
    r = client.post("/auth/social", json={"provider": "google", "token": "token-do-nosso-app"})

    assert r.status_code == 200, r.text
    assert r.json()["user"]["email"] == "ok@google.com"
    assert db.query(User).filter(User.email == "ok@google.com").count() == 1


def test_social_rate_limit_blocks_after_max_failures(monkeypatch):
    """Apos 20 tentativas falhas do mesmo IP, retorna 429.

    O rate limiter usa o IP como chave. Forcamos X-Forwarded-For em TODAS as
    requisicoes para garantir que todas usam o mesmo IP (192.0.2.1), incluindo
    a 21a que deve ser bloqueada antes de qualquer processamento.
    """
    BLOCKED_IP = "192.0.2.1"
    monkeypatch.setattr(auth, "_google_user_info", _mock_google_fail())
    client, _ = build()

    # Esgota as 20 tentativas (max_failures=20 para social) com o mesmo IP
    for _ in range(20):
        client.post(
            "/auth/social",
            json={"provider": "google", "token": "lixo"},
            headers={"X-Forwarded-For": BLOCKED_IP},
        )

    # 21a tentativa com o mesmo IP deve ser bloqueada ANTES do mock ser chamado
    monkeypatch.setattr(auth, "_google_user_info", _mock_google_ok("outro@google.com"))
    r = client.post(
        "/auth/social",
        json={"provider": "google", "token": "bom-token"},
        headers={"X-Forwarded-For": BLOCKED_IP},
    )

    assert r.status_code == 429
    assert "tentativas" in r.json()["detail"].lower() or "muitas" in r.json()["detail"].lower()
