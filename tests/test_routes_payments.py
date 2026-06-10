"""Testes de ROTA (camada HTTP) do modulo app/routes/payments.py.

Padrao do projeto (ver tests/test_routes_onda1.py e tests/test_routes_auth.py):
monta um FastAPI MINIMO com apenas o router de payments, SQLite em memoria
(StaticPool), overrides de get_db / get_current_user. NAO importa app.main (que
conecta no banco de PROD). NENHUMA chamada de rede real ao Asaas: a coroutine
create_asaas_payment e sempre substituida por monkeypatch.

Cobre:
- POST /payments/create: gating de PAYMENT_MODE (400 quando != asaas_sandbox),
  caminho com Asaas "ok" (mock), fallback internal-sandbox quando Asaas cai,
  split de receita (comissao default 20% e config por tenant), 401.
- GET /payments/{id}: happy path do dono, 404 para outro tutor (nao revela),
  admin/super_admin enxergam, 401.
- POST /payments/webhooks/asaas: token ausente/errado -> 401, token correto
  atualiza status pelo evento, payment inexistente nao quebra.
"""
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import app.models  # noqa: F401 - registra todas as tabelas no Base.metadata
from app.core.database import Base, get_db
from app.dependencies.auth import get_current_user
from app.models.payment import Payment
from app.models.tenant import Tenant
from app.models.tenant_payment_config import DEFAULT_COMMISSION_PERCENT, TenantPaymentConfig
from app.models.user import User
from app.routes import payments
from app.services.tenant_seed_service import DEFAULT_TENANT_SLUG

TENANT_ID = "t-test"
TUTOR_ID = "tutor-test"


def build(*, users: list[dict] | None = None, payment_configs: list[dict] | None = None):
    """Monta app minimo com o router de payments e um SQLite em memoria isolado."""
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    db = Session()

    db.add(Tenant(id=TENANT_ID, name="Aumigao", slug=DEFAULT_TENANT_SLUG, status="active", plan="business"))
    default_users = users or [
        dict(id=TUTOR_ID, email="tutor@test.com", password_hash="x", role="cliente", tenant_id=TENANT_ID)
    ]
    for u in default_users:
        db.add(User(**u))
    for cfg in payment_configs or []:
        db.add(TenantPaymentConfig(**cfg))
    db.commit()

    test_app = FastAPI()
    test_app.include_router(payments.router)
    test_app.dependency_overrides[get_db] = lambda: db
    # get_current_user real continua valendo (HTTPBearer auto_error=False -> 401),
    # exceto quando o teste o sobrescreve.
    return test_app, db


def as_user(test_app, db, uid):
    test_app.dependency_overrides[get_current_user] = lambda: db.get(User, uid)
    return TestClient(test_app)


def fake_asaas_ok(provider_id="asaas-pay-1", status="PENDING", invoice="https://inv", pix=None):
    """Retorna uma corotina que imita create_asaas_payment sem tocar a rede."""
    async def _coro(payload, user):
        provider_data = {
            "id": provider_id,
            "status": status,
            "invoiceUrl": invoice,
            "bankSlipUrl": None,
        }
        pix_data = pix or {}
        return provider_data, pix_data, "PIX"

    return _coro


def fake_asaas_down(*_a, **_k):
    """create_asaas_payment que sempre falha -> exercita o fallback interno."""
    async def _coro(payload, user):
        raise RuntimeError("asaas indisponivel (teste)")

    return _coro


@pytest.fixture(autouse=True)
def _force_sandbox_mode(monkeypatch):
    # PAYMENT_MODE e lido em import-time no modulo; garante o valor esperado por teste.
    monkeypatch.setattr(payments, "PAYMENT_MODE", "asaas_sandbox")


# -------------------------------------------------------------- create: gating
def test_create_rejects_when_payment_mode_not_sandbox(monkeypatch):
    monkeypatch.setattr(payments, "PAYMENT_MODE", "production")
    test_app, db = build()
    client = as_user(test_app, db, TUTOR_ID)
    r = client.post("/payments/create", json={"amount": 50.0, "method": "pix"})
    assert r.status_code == 400
    assert "asaas_sandbox" in r.json()["detail"]


def test_create_requires_auth_401():
    test_app, db = build()
    # sem override e sem Authorization header -> get_current_user real -> 401
    client = TestClient(test_app)
    r = client.post("/payments/create", json={"amount": 50.0})
    assert r.status_code == 401


# --------------------------------------------------- create: happy path (mock)
def test_create_happy_path_with_asaas_ok(monkeypatch):
    monkeypatch.setattr(payments, "create_asaas_payment", fake_asaas_ok(status="PENDING"))
    test_app, db = build()
    client = as_user(test_app, db, TUTOR_ID)
    r = client.post("/payments/create", json={"amount": 100.0, "method": "pix"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["tutor_id"] == TUTOR_ID
    assert body["amount"] == 100.0
    assert body["provider"] == "asaas_sandbox"
    assert body["provider_payment_id"] == "asaas-pay-1"
    # PENDING -> pagamento_sandbox_criado
    assert body["status"] == "pagamento_sandbox_criado"
    assert body["invoice_url"] == "https://inv"
    # persistido no banco
    assert db.query(Payment).count() == 1


def test_create_status_mapping_confirmed(monkeypatch):
    monkeypatch.setattr(payments, "create_asaas_payment", fake_asaas_ok(status="CONFIRMED"))
    test_app, db = build()
    client = as_user(test_app, db, TUTOR_ID)
    r = client.post("/payments/create", json={"amount": 30.0})
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "pagamento_confirmado_sandbox"


def test_create_split_uses_default_commission(monkeypatch):
    monkeypatch.setattr(payments, "create_asaas_payment", fake_asaas_ok())
    test_app, db = build()  # sem TenantPaymentConfig -> usa DEFAULT (20%)
    client = as_user(test_app, db, TUTOR_ID)
    r = client.post("/payments/create", json={"amount": 100.0})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["commission_percent"] == DEFAULT_COMMISSION_PERCENT  # 20.0
    assert body["platform_amount"] == 20.0
    assert body["walker_amount"] == 80.0


def test_create_split_uses_tenant_config_commission(monkeypatch):
    monkeypatch.setattr(payments, "create_asaas_payment", fake_asaas_ok())
    test_app, db = build(payment_configs=[
        dict(tenant_id=TENANT_ID, provider="asaas", commission_percent=10.0, active=True)
    ])
    client = as_user(test_app, db, TUTOR_ID)
    r = client.post("/payments/create", json={"amount": 200.0})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["commission_percent"] == 10.0
    assert body["platform_amount"] == 20.0
    assert body["walker_amount"] == 180.0


# ---------------------------------------------- create: fallback internal-sandbox
def test_create_fallback_when_asaas_down(monkeypatch):
    monkeypatch.setattr(payments, "create_asaas_payment", fake_asaas_down())
    test_app, db = build()
    client = as_user(test_app, db, TUTOR_ID)
    r = client.post("/payments/create", json={"amount": 50.0})
    assert r.status_code == 200, r.text
    body = r.json()
    # provider_payment_id gerado internamente quando Asaas cai
    assert body["provider_payment_id"].startswith("internal-sandbox-")
    # PAYMENT_CREATED nao esta no STATUS_BY_ASAAS_STATUS -> aguardando_pagamento
    assert body["status"] == "aguardando_pagamento"
    assert db.query(Payment).count() == 1


# ---------------------------------------------------------------- get payment
def test_get_payment_owner_happy_path(monkeypatch):
    monkeypatch.setattr(payments, "create_asaas_payment", fake_asaas_ok())
    test_app, db = build()
    client = as_user(test_app, db, TUTOR_ID)
    created = client.post("/payments/create", json={"amount": 40.0}).json()
    pid = created["id"]
    r = client.get(f"/payments/{pid}")
    assert r.status_code == 200, r.text
    assert r.json()["id"] == pid
    assert r.json()["tutor_id"] == TUTOR_ID


def test_get_payment_other_tutor_returns_404():
    test_app, db = build(users=[
        dict(id=TUTOR_ID, email="a@test.com", password_hash="x", role="cliente", tenant_id=TENANT_ID),
        dict(id="other", email="b@test.com", password_hash="x", role="cliente", tenant_id=TENANT_ID),
    ])
    # cria pagamento direto no banco pertencente ao TUTOR_ID
    db.add(Payment(id="pay-x", tenant_id=TENANT_ID, tutor_id=TUTOR_ID, amount=10.0,
                   status="aguardando_pagamento", provider="asaas_sandbox"))
    db.commit()
    client = as_user(test_app, db, "other")
    r = client.get("/payments/pay-x")
    # 404 (nao 403) para nao revelar existencia via enumeracao de ID
    assert r.status_code == 404


def test_get_payment_admin_can_see_any():
    test_app, db = build(users=[
        dict(id=TUTOR_ID, email="a@test.com", password_hash="x", role="cliente", tenant_id=TENANT_ID),
        dict(id="adm", email="adm@test.com", password_hash="x", role="admin", tenant_id=TENANT_ID),
    ])
    db.add(Payment(id="pay-y", tenant_id=TENANT_ID, tutor_id=TUTOR_ID, amount=10.0,
                   status="aguardando_pagamento", provider="asaas_sandbox"))
    db.commit()
    client = as_user(test_app, db, "adm")
    r = client.get("/payments/pay-y")
    assert r.status_code == 200, r.text
    assert r.json()["id"] == "pay-y"


def test_get_payment_missing_returns_404():
    test_app, db = build()
    client = as_user(test_app, db, TUTOR_ID)
    assert client.get("/payments/nao-existe").status_code == 404


def test_get_payment_requires_auth_401():
    test_app, db = build()
    db.add(Payment(id="pay-z", tenant_id=TENANT_ID, tutor_id=TUTOR_ID, amount=10.0,
                   status="aguardando_pagamento", provider="asaas_sandbox"))
    db.commit()
    client = TestClient(test_app)  # sem override -> get_current_user real -> 401
    assert client.get("/payments/pay-z").status_code == 401


# ------------------------------------------------------------------- webhook
def test_webhook_without_token_returns_401(monkeypatch):
    monkeypatch.setenv("ASAAS_WEBHOOK_TOKEN", "segredo")
    test_app, db = build()
    client = TestClient(test_app)
    r = client.post("/payments/webhooks/asaas", json={"event": "PAYMENT_CONFIRMED"})
    assert r.status_code == 401


def test_webhook_wrong_token_returns_401(monkeypatch):
    monkeypatch.setenv("ASAAS_WEBHOOK_TOKEN", "segredo")
    test_app, db = build()
    client = TestClient(test_app)
    r = client.post(
        "/payments/webhooks/asaas",
        json={"event": "PAYMENT_CONFIRMED"},
        headers={"asaas-access-token": "errado"},
    )
    assert r.status_code == 401


def test_webhook_correct_token_updates_status(monkeypatch):
    monkeypatch.setenv("ASAAS_WEBHOOK_TOKEN", "segredo")
    test_app, db = build()
    db.add(Payment(id="pay-w", tenant_id=TENANT_ID, tutor_id=TUTOR_ID, amount=10.0,
                   status="pagamento_sandbox_criado", provider="asaas_sandbox",
                   provider_payment_id="prov-1"))
    db.commit()
    client = TestClient(test_app)
    r = client.post(
        "/payments/webhooks/asaas",
        json={"event": "PAYMENT_CONFIRMED", "payment": {"id": "prov-1", "status": "CONFIRMED"}},
        headers={"asaas-access-token": "segredo"},
    )
    assert r.status_code == 200, r.text
    assert r.json() == {"ok": True, "received": "PAYMENT_CONFIRMED"}
    db.expire_all()
    assert db.get(Payment, "pay-w").status == "pagamento_confirmado_sandbox"


def test_webhook_unknown_payment_does_not_break(monkeypatch):
    monkeypatch.setenv("ASAAS_WEBHOOK_TOKEN", "segredo")
    test_app, db = build()
    client = TestClient(test_app)
    r = client.post(
        "/payments/webhooks/asaas",
        json={"event": "PAYMENT_RECEIVED", "payment": {"id": "nao-existe"}},
        headers={"asaas-access-token": "segredo"},
    )
    assert r.status_code == 200, r.text
    assert r.json()["received"] == "PAYMENT_RECEIVED"


# ------------------------------------------------------------------- BUG REAL
@pytest.mark.xfail(
    reason="BUG: em create_payment, as linhas 'pix_data = {}' e 'provider_status = "
    "provider_data.get(\"status\")' ficam no nivel da funcao (indentacao 4 espacos), "
    "NAO dentro do except. Logo, ate quando o Asaas responde com sucesso, pix_data e "
    "zerado e os dados de PIX (qr code / copia-e-cola) retornados pelo Asaas sao "
    "descartados na resposta.",
    strict=True,
)
def test_create_keeps_pix_data_on_asaas_success(monkeypatch):
    pix = {"encodedImage": "QR==", "payload": "00020126...", "expirationDate": "2026-07-01"}
    monkeypatch.setattr(payments, "create_asaas_payment", fake_asaas_ok(pix=pix))
    test_app, db = build()
    client = as_user(test_app, db, TUTOR_ID)
    r = client.post("/payments/create", json={"amount": 100.0, "method": "pix"})
    assert r.status_code == 200, r.text
    body = r.json()
    # Esperado (se nao houvesse bug): os dados de PIX deveriam vir preenchidos.
    assert body["pix_qr_code"] == "QR=="
    assert body["pix_copy_paste"] == "00020126..."
    assert body["pix_expiration_date"] == "2026-07-01"
