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
from app.models.tenant_payment_config import TenantPaymentConfig, commission_default_for_plan
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


def test_create_split_uses_plan_default_commission(monkeypatch):
    monkeypatch.setattr(payments, "create_asaas_payment", fake_asaas_ok())
    # sem TenantPaymentConfig -> fallback deriva do PLANO do tenant (business = 8%), não 20%
    test_app, db = build()
    client = as_user(test_app, db, TUTOR_ID)
    r = client.post("/payments/create", json={"amount": 100.0})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["commission_percent"] == commission_default_for_plan("business")  # 8.0
    assert body["platform_amount"] == 8.0
    assert body["walker_amount"] == 92.0


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


# ----------------------------------- R3: idempotência + anti-retrocesso (puro)
from app.routes.payments import (  # noqa: E402
    resolve_payment_webhook_status,
    _PAYMENT_REFUNDED_STATUS,
)

CONFIRMED = "pagamento_confirmado_sandbox"
AGUARDANDO = "aguardando_pagamento"
FALHA = "falha_pagamento"


def test_resolve_status_confirmed_is_sticky_against_overdue():
    # PAYMENT_OVERDUE atrasado após confirmado NÃO regride o status.
    assert resolve_payment_webhook_status(CONFIRMED, "PAYMENT_OVERDUE", FALHA) == CONFIRMED


def test_resolve_status_confirmed_reentry_is_idempotent():
    # Reentrega do mesmo PAYMENT_CONFIRMED mantém o status estável.
    assert resolve_payment_webhook_status(CONFIRMED, "PAYMENT_CONFIRMED", CONFIRMED) == CONFIRMED


def test_resolve_status_refund_goes_to_distinct_estornado_not_falha():
    # Estorno consumado leva a estado de estorno DISTINTO, não 'falha_pagamento'.
    result = resolve_payment_webhook_status(CONFIRMED, "PAYMENT_REFUNDED", FALHA)
    assert result == _PAYMENT_REFUNDED_STATUS
    assert result != FALHA


def test_resolve_status_pending_overdue_still_fails():
    # Pagamento não-confirmado regride normalmente (OVERDUE -> falha).
    assert resolve_payment_webhook_status(AGUARDANDO, "PAYMENT_OVERDUE", FALHA) == FALHA


def test_resolve_status_late_payment_can_confirm_from_failure():
    # Pagamento em falha que depois confirma sobe para confirmado.
    assert resolve_payment_webhook_status(FALHA, "PAYMENT_CONFIRMED", CONFIRMED) == CONFIRMED


# ----------------------------------- R3: idempotência + anti-retrocesso (endpoint)
def _post_webhook(client, event, prov_id="prov-1", status=None):
    pay = {"id": prov_id}
    if status:
        pay["status"] = status
    return client.post(
        "/payments/webhooks/asaas",
        json={"event": event, "payment": pay},
        headers={"asaas-access-token": "segredo"},
    )


def test_webhook_overdue_after_confirmed_does_not_regress(monkeypatch):
    monkeypatch.setenv("ASAAS_WEBHOOK_TOKEN", "segredo")
    test_app, db = build()
    db.add(Payment(id="pay-1", tenant_id=TENANT_ID, tutor_id=TUTOR_ID, amount=10.0,
                   status="pagamento_confirmado_sandbox", provider="asaas_sandbox",
                   provider_payment_id="prov-1"))
    db.commit()
    client = TestClient(test_app)
    r = _post_webhook(client, "PAYMENT_OVERDUE")
    assert r.status_code == 200, r.text
    db.expire_all()
    # confirmado é pegajoso: OVERDUE atrasado não vira falha_pagamento
    assert db.get(Payment, "pay-1").status == "pagamento_confirmado_sandbox"


def test_webhook_refunded_after_confirmed_sets_distinct_state(monkeypatch):
    monkeypatch.setenv("ASAAS_WEBHOOK_TOKEN", "segredo")
    test_app, db = build()
    db.add(Payment(id="pay-2", tenant_id=TENANT_ID, tutor_id=TUTOR_ID, amount=10.0,
                   status="pagamento_confirmado_sandbox", provider="asaas_sandbox",
                   provider_payment_id="prov-2"))
    db.commit()
    client = TestClient(test_app)
    r = _post_webhook(client, "PAYMENT_REFUNDED", prov_id="prov-2")
    assert r.status_code == 200, r.text
    db.expire_all()
    status = db.get(Payment, "pay-2").status
    assert status == _PAYMENT_REFUNDED_STATUS
    assert status != "falha_pagamento"


def test_webhook_confirmed_reentry_keeps_status_stable(monkeypatch):
    monkeypatch.setenv("ASAAS_WEBHOOK_TOKEN", "segredo")
    test_app, db = build()
    db.add(Payment(id="pay-3", tenant_id=TENANT_ID, tutor_id=TUTOR_ID, amount=10.0,
                   status="pagamento_confirmado_sandbox", provider="asaas_sandbox",
                   provider_payment_id="prov-3"))
    db.commit()
    client = TestClient(test_app)
    _post_webhook(client, "PAYMENT_CONFIRMED", prov_id="prov-3", status="CONFIRMED")
    _post_webhook(client, "PAYMENT_CONFIRMED", prov_id="prov-3", status="CONFIRMED")
    db.expire_all()
    assert db.get(Payment, "pay-3").status == "pagamento_confirmado_sandbox"


def test_webhook_orphan_payment_is_logged_not_silent(monkeypatch, caplog):
    import logging
    monkeypatch.setenv("ASAAS_WEBHOOK_TOKEN", "segredo")
    test_app, db = build()
    client = TestClient(test_app)
    with caplog.at_level(logging.WARNING):
        r = _post_webhook(client, "PAYMENT_RECEIVED", prov_id="orfao-999")
    assert r.status_code == 200, r.text
    assert any("orfao" in rec.message.lower() or "órfão" in rec.message.lower()
               or "orphan" in rec.message.lower() for rec in caplog.records)
    assert any("orfao-999" in str(rec.args) + rec.message for rec in caplog.records)


# Regressao do bug de indentacao em create_payment: no sucesso do Asaas os dados
# de PIX (qr code / copia-e-cola) devem vir preenchidos na resposta (nao zerados).
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
