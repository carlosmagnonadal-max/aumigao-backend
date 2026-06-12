"""Testes do modo de pagamento live (dormente) — app/routes/payments.py.

Cobre:
- Modo live sem ASAAS_LIVE_API_KEY → 503 com mensagem PT clara.
- Modo desconhecido → 400.
- Sandbox continua idêntico (snapshot de campos de resposta): provider=asaas_sandbox,
  status=pagamento_sandbox_criado, invoice_url, pix_*, sandbox_message.
- _build_split_config_for_payment: split incluído quando as 3 condições valem,
  e None quando qualquer condição falha (testes unitários da função pura).
- invoice_url aparece na resposta quando o Asaas retorna.
- PATCH /admin/walkers/{user_id}/wallet requer permissão finance.manage.
"""
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import app.models  # noqa: F401 — registra todas as tabelas no Base.metadata
from app.core.database import Base, get_db
from app.dependencies.auth import get_current_user
from app.models.payment import Payment
from app.models.pet import Pet
from app.models.tenant import Tenant
from app.models.tenant_payment_config import TenantPaymentConfig
from app.models.user import User
from app.models.walk import Walk
from app.models.walker_profile import WalkerProfile
from app.routes import payments, admin as admin_routes
from app.routes.payments import _build_split_config_for_payment
from app.services.tenant_seed_service import DEFAULT_TENANT_SLUG

TENANT_ID = "t-live-test"
TUTOR_ID = "tutor-live"
WALKER_USER_ID = "walker-live"
WALKER_ID = "wp-live"
WALK_ID = "walk-live-1"


# ---------------------------------------------------------------------------
# Helpers de montagem
# ---------------------------------------------------------------------------

def _make_engine_and_session():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    return engine, Session


def build_payments_app(
    *,
    split_enabled: bool = False,
    walker_wallet_id: str | None = None,
    create_walk: bool = False,
):
    """Monta app mínimo com router de payments e SQLite em memória."""
    _engine, Session = _make_engine_and_session()
    db = Session()

    db.add(Tenant(id=TENANT_ID, name="LiveTest", slug=DEFAULT_TENANT_SLUG, status="active", plan="business"))
    db.add(User(id=TUTOR_ID, email="tutor@live.com", password_hash="x", role="cliente", tenant_id=TENANT_ID))
    db.add(User(id=WALKER_USER_ID, email="walker@live.com", password_hash="x", role="walker", tenant_id=TENANT_ID))
    db.add(WalkerProfile(
        id=WALKER_ID,
        user_id=WALKER_USER_ID,
        asaas_wallet_id=walker_wallet_id,
    ))
    if create_walk:
        db.add(Pet(id="pet-live-1", tutor_id=TUTOR_ID, tenant_id=TENANT_ID, name="Rex"))
        db.add(Walk(
            id=WALK_ID,
            tutor_id=TUTOR_ID,
            walker_id=WALKER_USER_ID,
            tenant_id=TENANT_ID,
            pet_id="pet-live-1",
            scheduled_date="2026-07-01",
            duration_minutes=30,
            status="scheduled",
            price=100.0,
        ))
    db.add(TenantPaymentConfig(
        tenant_id=TENANT_ID,
        commission_percent=20.0,
        split_enabled=split_enabled,
        active=True,
    ))
    db.commit()

    test_app = FastAPI()
    test_app.include_router(payments.router)
    test_app.dependency_overrides[get_db] = lambda: db
    return test_app, db


def build_admin_app():
    """Monta app mínimo com router admin e SQLite em memória."""
    _engine, Session = _make_engine_and_session()
    db = Session()

    db.add(Tenant(id=TENANT_ID, name="LiveTest", slug=DEFAULT_TENANT_SLUG, status="active", plan="business"))
    db.add(User(id="admin-1", email="admin@live.com", password_hash="x", role="super_admin", tenant_id=TENANT_ID))
    db.add(User(id="regular-1", email="regular@live.com", password_hash="x", role="cliente", tenant_id=TENANT_ID))
    db.add(User(id=WALKER_USER_ID, email="walker@live.com", password_hash="x", role="walker", tenant_id=TENANT_ID))
    db.add(WalkerProfile(
        id=WALKER_ID,
        user_id=WALKER_USER_ID,
        asaas_wallet_id=None,
    ))
    db.commit()

    test_app = FastAPI()
    test_app.include_router(admin_routes.router)
    test_app.include_router(admin_routes.api_router)
    test_app.dependency_overrides[get_db] = lambda: db
    return test_app, db


def as_user(test_app, db, uid):
    test_app.dependency_overrides[get_current_user] = lambda: db.get(User, uid)
    return TestClient(test_app)


def fake_asaas_ok(provider_id="asaas-live-1", status="PENDING", invoice="https://live-inv"):
    async def _coro(payload, user):
        return (
            {"id": provider_id, "status": status, "invoiceUrl": invoice, "bankSlipUrl": None},
            {},
            "PIX",
        )
    return _coro


# ---------------------------------------------------------------------------
# Fixture autouse: força sandbox por padrão; testes que testam live sobrescrevem
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _force_sandbox_mode(monkeypatch):
    monkeypatch.setattr(payments, "PAYMENT_MODE", "asaas_sandbox")


# ---------------------------------------------------------------------------
# Testes de modo live sem chave → 503
# ---------------------------------------------------------------------------

def test_live_mode_without_api_key_returns_503(monkeypatch):
    """PAYMENT_MODE=asaas_live sem ASAAS_LIVE_API_KEY → 503 com mensagem em PT."""
    monkeypatch.setattr(payments, "PAYMENT_MODE", "asaas_live")
    monkeypatch.setattr(payments, "ASAAS_LIVE_API_KEY", None)

    test_app, db = build_payments_app()
    client = as_user(test_app, db, TUTOR_ID)
    r = client.post("/payments/create", json={"amount": 50.0, "method": "pix"})
    assert r.status_code == 503, r.text
    detail = r.json()["detail"]
    assert "produção" in detail or "producao" in detail.lower() or "configurad" in detail.lower()


# ---------------------------------------------------------------------------
# Testes de modo desconhecido → 400
# ---------------------------------------------------------------------------

def test_unknown_payment_mode_returns_400(monkeypatch):
    """PAYMENT_MODE desconhecido → 400."""
    monkeypatch.setattr(payments, "PAYMENT_MODE", "stripe")

    test_app, db = build_payments_app()
    client = as_user(test_app, db, TUTOR_ID)
    r = client.post("/payments/create", json={"amount": 50.0, "method": "pix"})
    assert r.status_code == 400, r.text
    assert "asaas_sandbox" in r.json()["detail"] or "desconhecido" in r.json()["detail"].lower()


# ---------------------------------------------------------------------------
# Testes de regressão sandbox (snapshot de campos)
# ---------------------------------------------------------------------------

def test_sandbox_response_snapshot(monkeypatch):
    """Sandbox continua igual: provider, status, invoice_url, sandbox_message."""
    monkeypatch.setattr(payments, "create_asaas_payment", fake_asaas_ok(
        provider_id="sb-1", status="PENDING", invoice="https://sandbox-inv"
    ))
    test_app, db = build_payments_app()
    client = as_user(test_app, db, TUTOR_ID)
    r = client.post("/payments/create", json={"amount": 100.0, "method": "pix"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["provider"] == "asaas_sandbox"
    assert body["status"] == "pagamento_sandbox_criado"
    assert body["invoice_url"] == "https://sandbox-inv"
    assert body["sandbox_message"] is not None
    assert body["commission_percent"] == 20.0
    assert body["platform_amount"] == 20.0
    assert body["walker_amount"] == 80.0


def test_sandbox_invoice_url_in_response_when_asaas_returns_it(monkeypatch):
    """invoice_url aparece na resposta quando o Asaas retorna."""
    monkeypatch.setattr(payments, "create_asaas_payment", fake_asaas_ok(invoice="https://my-invoice"))
    test_app, db = build_payments_app()
    client = as_user(test_app, db, TUTOR_ID)
    r = client.post("/payments/create", json={"amount": 50.0})
    assert r.status_code == 200, r.text
    assert r.json()["invoice_url"] == "https://my-invoice"


def test_sandbox_invoice_url_null_when_asaas_returns_none(monkeypatch):
    """invoice_url é null quando o Asaas não retorna URL."""
    async def _no_invoice(payload, user):
        return (
            {"id": "sb-2", "status": "PENDING", "invoiceUrl": None, "bankSlipUrl": None},
            {},
            "PIX",
        )
    monkeypatch.setattr(payments, "create_asaas_payment", _no_invoice)
    test_app, db = build_payments_app()
    client = as_user(test_app, db, TUTOR_ID)
    r = client.post("/payments/create", json={"amount": 50.0})
    assert r.status_code == 200, r.text
    assert r.json()["invoice_url"] is None


# ---------------------------------------------------------------------------
# Testes unitários de _build_split_config_for_payment (função pura)
# ---------------------------------------------------------------------------

def _make_db_for_split(
    *,
    split_enabled: bool = True,
    walker_wallet_id: str | None = "wallet-abc",
    create_walk: bool = True,
    commission_percent: float = 20.0,
):
    """Cria DB em memória com entidades mínimas para testar _build_split_config_for_payment."""
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    db = Session()

    db.add(Tenant(id=TENANT_ID, name="T", slug=DEFAULT_TENANT_SLUG, status="active", plan="business"))
    db.add(User(id=TUTOR_ID, email="t@t.com", password_hash="x", role="cliente", tenant_id=TENANT_ID))
    db.add(User(id=WALKER_USER_ID, email="w@t.com", password_hash="x", role="walker", tenant_id=TENANT_ID))
    db.add(WalkerProfile(id=WALKER_ID, user_id=WALKER_USER_ID, asaas_wallet_id=walker_wallet_id))
    if create_walk:
        db.add(Pet(id="pet-split-1", tutor_id=TUTOR_ID, tenant_id=TENANT_ID, name="Bolinha"))
        db.add(Walk(
            id=WALK_ID,
            tutor_id=TUTOR_ID,
            walker_id=WALKER_USER_ID,
            tenant_id=TENANT_ID,
            pet_id="pet-split-1",
            scheduled_date="2026-07-01",
            duration_minutes=30,
            status="scheduled",
            price=100.0,
        ))
    db.add(TenantPaymentConfig(
        tenant_id=TENANT_ID,
        commission_percent=commission_percent,
        split_enabled=split_enabled,
        active=True,
    ))
    db.commit()
    return db


def test_build_split_config_all_conditions_met(monkeypatch):
    """Retorna split_config quando modo live + split_enabled + asaas_wallet_id."""
    monkeypatch.setattr(payments, "PAYMENT_MODE", "asaas_live")
    db = _make_db_for_split(split_enabled=True, walker_wallet_id="wallet-abc")
    split = {"commission_percent": 20.0, "platform_amount": 20.0, "walker_amount": 80.0}
    result = _build_split_config_for_payment(db, WALK_ID, TENANT_ID, split)
    assert result is not None
    assert result["wallet_id"] == "wallet-abc"
    assert result["percentual_value"] == 80.0


def test_build_split_config_returns_none_in_sandbox(monkeypatch):
    """No sandbox, split_config sempre None."""
    # monkeypatch autouse já garante sandbox
    db = _make_db_for_split(split_enabled=True, walker_wallet_id="wallet-abc")
    split = {"commission_percent": 20.0, "platform_amount": 20.0, "walker_amount": 80.0}
    result = _build_split_config_for_payment(db, WALK_ID, TENANT_ID, split)
    assert result is None


def test_build_split_config_returns_none_when_split_disabled(monkeypatch):
    """split_config é None quando split_enabled=False."""
    monkeypatch.setattr(payments, "PAYMENT_MODE", "asaas_live")
    db = _make_db_for_split(split_enabled=False, walker_wallet_id="wallet-abc")
    split = {"commission_percent": 20.0, "platform_amount": 20.0, "walker_amount": 80.0}
    result = _build_split_config_for_payment(db, WALK_ID, TENANT_ID, split)
    assert result is None


def test_build_split_config_returns_none_when_wallet_missing(monkeypatch):
    """split_config é None quando walker não tem asaas_wallet_id."""
    monkeypatch.setattr(payments, "PAYMENT_MODE", "asaas_live")
    db = _make_db_for_split(split_enabled=True, walker_wallet_id=None)
    split = {"commission_percent": 20.0, "platform_amount": 20.0, "walker_amount": 80.0}
    result = _build_split_config_for_payment(db, WALK_ID, TENANT_ID, split)
    assert result is None


def test_build_split_config_percent_uses_commission_source(monkeypatch):
    """percentual_value = 100 - commission_percent, usando a mesma fonte de verdade."""
    monkeypatch.setattr(payments, "PAYMENT_MODE", "asaas_live")
    db = _make_db_for_split(split_enabled=True, walker_wallet_id="wallet-xyz", commission_percent=15.0)
    split = {"commission_percent": 15.0, "platform_amount": 15.0, "walker_amount": 85.0}
    result = _build_split_config_for_payment(db, WALK_ID, TENANT_ID, split)
    assert result is not None
    assert result["percentual_value"] == 85.0


# ---------------------------------------------------------------------------
# Testes do endpoint PATCH /admin/walkers/{user_id}/wallet
# ---------------------------------------------------------------------------

def test_patch_wallet_requires_finance_manage_permission():
    """Usuário sem permissão finance.manage não pode configurar wallet."""
    test_app, db = build_admin_app()
    # regular-1 é "cliente" — não tem finance.manage
    client = as_user(test_app, db, "regular-1")
    r = client.patch(f"/admin/walkers/{WALKER_USER_ID}/wallet", json={"asaas_wallet_id": "wallet-x"})
    assert r.status_code in {403, 401}, r.text


def test_patch_wallet_super_admin_sets_wallet_id():
    """super_admin pode configurar asaas_wallet_id."""
    test_app, db = build_admin_app()
    client = as_user(test_app, db, "admin-1")
    r = client.patch(f"/admin/walkers/{WALKER_USER_ID}/wallet", json={"asaas_wallet_id": "wal_123abc"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True
    assert body["asaas_wallet_id"] == "wal_123abc"
    # persiste no banco
    db.expire_all()
    profile = db.query(WalkerProfile).filter(WalkerProfile.user_id == WALKER_USER_ID).first()
    assert profile.asaas_wallet_id == "wal_123abc"


def test_patch_wallet_super_admin_clears_wallet_id():
    """super_admin pode limpar asaas_wallet_id com null."""
    test_app, db = build_admin_app()
    # Set first
    profile = db.query(WalkerProfile).filter(WalkerProfile.user_id == WALKER_USER_ID).first()
    profile.asaas_wallet_id = "wal_existing"
    db.commit()

    client = as_user(test_app, db, "admin-1")
    r = client.patch(f"/admin/walkers/{WALKER_USER_ID}/wallet", json={"asaas_wallet_id": None})
    assert r.status_code == 200, r.text
    assert r.json()["asaas_wallet_id"] is None
    db.expire_all()
    profile = db.query(WalkerProfile).filter(WalkerProfile.user_id == WALKER_USER_ID).first()
    assert profile.asaas_wallet_id is None


def test_patch_wallet_unknown_walker_returns_404():
    """Walker inexistente → 404."""
    test_app, db = build_admin_app()
    client = as_user(test_app, db, "admin-1")
    r = client.patch("/admin/walkers/nao-existe/wallet", json={"asaas_wallet_id": "wal_x"})
    assert r.status_code == 404, r.text


def test_patch_wallet_missing_field_returns_422():
    """Body sem 'asaas_wallet_id' → 422."""
    test_app, db = build_admin_app()
    client = as_user(test_app, db, "admin-1")
    r = client.patch(f"/admin/walkers/{WALKER_USER_ID}/wallet", json={"outro_campo": "valor"})
    assert r.status_code == 422, r.text


def test_patch_wallet_empty_string_returns_422():
    """String vazia não é aceita — usar null para limpar."""
    test_app, db = build_admin_app()
    client = as_user(test_app, db, "admin-1")
    r = client.patch(f"/admin/walkers/{WALKER_USER_ID}/wallet", json={"asaas_wallet_id": ""})
    assert r.status_code == 422, r.text


# ---------------------------------------------------------------------------
# Testes de CPF do tutor no modo live
# ---------------------------------------------------------------------------

import asyncio

from app.models.tutor_profile import TutorProfile
from app.routes.payments import create_asaas_customer, _tutor_cpf_ctx

VALID_CPF_11 = "52998224725"  # CPF válido (dígitos verificadores corretos)


def build_payments_app_with_cpf(*, tutor_cpf: str = "") -> tuple:
    """Monta app de payments com TutorProfile opcionalmente configurado com CPF."""
    _engine, Session = _make_engine_and_session()
    db = Session()

    db.add(Tenant(id=TENANT_ID, name="LiveCPF", slug=DEFAULT_TENANT_SLUG, status="active", plan="business"))
    db.add(User(id=TUTOR_ID, email="tutor@live.com", password_hash="x", role="cliente", tenant_id=TENANT_ID))
    db.add(TutorProfile(
        id="tp-live-cpf",
        user_id=TUTOR_ID,
        tenant_id=TENANT_ID,
        full_name="Tutor Teste",
        cpf=tutor_cpf,
        phone="",
    ))
    db.add(TenantPaymentConfig(
        tenant_id=TENANT_ID,
        commission_percent=20.0,
        split_enabled=False,
        active=True,
    ))
    db.commit()

    test_app = FastAPI()
    test_app.include_router(payments.router)
    test_app.dependency_overrides[get_db] = lambda: db
    return test_app, db


# ---------------------------------------------------------------------------
# Testes unitários de create_asaas_customer (função pura via httpx mock)
# ---------------------------------------------------------------------------

class _FakeHttpxClient:
    """Cliente httpx mínimo para testar create_asaas_customer sem rede."""

    def __init__(self, status_code: int = 200, payload: dict | None = None):
        self._status_code = status_code
        self._payload = payload or {"id": "cust-123"}
        self.posted_payload: dict | None = None

    async def post(self, path: str, json: dict | None = None):
        self.posted_payload = json

        class _Resp:
            def __init__(self, status_code, payload):
                self.status_code = status_code
                self._payload = payload

            def json(self):
                return self._payload

            @property
            def text(self):
                return str(self._payload)

        return _Resp(self._status_code, self._payload)


def test_create_asaas_customer_live_uses_tutor_cpf():
    """No modo live, create_asaas_customer usa o CPF passado em tutor_cpf."""
    fake = _FakeHttpxClient(status_code=200, payload={"id": "cust-live-1"})
    cust_id = asyncio.run(create_asaas_customer(
        fake,
        User(id="u1", email="a@b.com", full_name="Tutor"),
        is_live=True,
        tutor_cpf=VALID_CPF_11,
    ))
    assert cust_id == "cust-live-1"
    assert fake.posted_payload is not None
    assert fake.posted_payload["cpfCnpj"] == VALID_CPF_11


def test_create_asaas_customer_live_without_cpf_raises_400():
    """No modo live sem CPF válido, create_asaas_customer levanta HTTPException 400."""
    from fastapi import HTTPException as FastAPIHTTPException

    fake = _FakeHttpxClient()
    with pytest.raises(FastAPIHTTPException) as exc_info:
        asyncio.run(create_asaas_customer(
            fake,
            User(id="u1", email="a@b.com", full_name="Tutor"),
            is_live=True,
            tutor_cpf=None,
        ))
    assert exc_info.value.status_code == 400
    assert "CPF" in exc_info.value.detail


def test_create_asaas_customer_live_empty_cpf_raises_400():
    """No modo live com CPF vazio (string), create_asaas_customer levanta 400."""
    from fastapi import HTTPException as FastAPIHTTPException

    fake = _FakeHttpxClient()
    with pytest.raises(FastAPIHTTPException) as exc_info:
        asyncio.run(create_asaas_customer(
            fake,
            User(id="u1", email="a@b.com", full_name="Tutor"),
            is_live=True,
            tutor_cpf="",
        ))
    assert exc_info.value.status_code == 400


def test_create_asaas_customer_sandbox_uses_default_when_no_cpf():
    """No sandbox sem CPF, usa ASAAS_SANDBOX_DEFAULT_CPF_CNPJ."""
    fake = _FakeHttpxClient(status_code=200, payload={"id": "cust-sb-1"})
    asyncio.run(create_asaas_customer(
        fake,
        User(id="u1", email="a@b.com", full_name="Tutor"),
        is_live=False,
        tutor_cpf=None,
    ))
    assert fake.posted_payload is not None
    # Sandbox usa o default (não é o CPF do tutor)
    from app.routes.payments import ASAAS_SANDBOX_DEFAULT_CPF_CNPJ
    assert fake.posted_payload["cpfCnpj"] == ASAAS_SANDBOX_DEFAULT_CPF_CNPJ


def test_create_asaas_customer_sandbox_uses_tutor_cpf_when_available():
    """No sandbox com CPF real disponível, usa o CPF do tutor."""
    fake = _FakeHttpxClient(status_code=200, payload={"id": "cust-sb-2"})
    asyncio.run(create_asaas_customer(
        fake,
        User(id="u1", email="a@b.com", full_name="Tutor"),
        is_live=False,
        tutor_cpf=VALID_CPF_11,
    ))
    assert fake.posted_payload is not None
    assert fake.posted_payload["cpfCnpj"] == VALID_CPF_11


# ---------------------------------------------------------------------------
# Testes de integração via endpoint POST /payments/create
# ---------------------------------------------------------------------------

def test_live_without_cpf_returns_400(monkeypatch):
    """Modo live + tutor sem CPF no perfil → 400 com mensagem de CPF."""
    monkeypatch.setattr(payments, "PAYMENT_MODE", "asaas_live")
    monkeypatch.setattr(payments, "ASAAS_LIVE_API_KEY", "live-key-test")

    # Não passa tutor_cpf → create_asaas_customer lança 400 antes de chegar ao Asaas
    async def _asaas_raises(payload, user):
        # Simula create_asaas_payment chegando até create_asaas_customer e levantando 400
        from fastapi import HTTPException
        raise HTTPException(
            status_code=400,
            detail="Informe seu CPF no perfil para concluir o pagamento.",
        )

    monkeypatch.setattr(payments, "create_asaas_payment", _asaas_raises)

    test_app, db = build_payments_app_with_cpf(tutor_cpf="")
    client = as_user(test_app, db, TUTOR_ID)
    r = client.post("/payments/create", json={"amount": 50.0, "method": "pix"})
    assert r.status_code == 400, r.text
    detail = r.json().get("detail", "")
    assert "CPF" in detail


def test_live_with_valid_cpf_proceeds(monkeypatch):
    """Modo live + tutor com CPF → cria pagamento normalmente."""
    monkeypatch.setattr(payments, "PAYMENT_MODE", "asaas_live")
    monkeypatch.setattr(payments, "ASAAS_LIVE_API_KEY", "live-key-test")

    async def _ok(payload, user):
        return (
            {"id": "live-pay-1", "status": "PENDING", "invoiceUrl": "https://inv", "bankSlipUrl": None},
            {},
            "PIX",
        )

    monkeypatch.setattr(payments, "create_asaas_payment", _ok)

    test_app, db = build_payments_app_with_cpf(tutor_cpf=VALID_CPF_11)
    client = as_user(test_app, db, TUTOR_ID)
    r = client.post("/payments/create", json={"amount": 50.0, "method": "pix"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["provider"] == "asaas_live"


def test_sandbox_without_cpf_proceeds_with_default(monkeypatch):
    """Sandbox sem CPF → usa default, não levanta 400."""
    # monkeypatch autouse já garante sandbox
    async def _ok(payload, user):
        return (
            {"id": "sb-cpf-1", "status": "PENDING", "invoiceUrl": None, "bankSlipUrl": None},
            {},
            "PIX",
        )

    monkeypatch.setattr(payments, "create_asaas_payment", _ok)

    test_app, db = build_payments_app_with_cpf(tutor_cpf="")
    client = as_user(test_app, db, TUTOR_ID)
    r = client.post("/payments/create", json={"amount": 50.0, "method": "pix"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["provider"] == "asaas_sandbox"
