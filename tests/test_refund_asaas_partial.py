"""Mig 0107 — estorno PARCIAL no client Asaas + confirmação via webhook.

refund_asaas_charge(provider, provider_payment_id, value=...) — value=None
preserva o payload {} (estorno total, comportamento histórico inalterado);
value=X manda {"value": X} (estorno parcial, usado pelo motor de cancelamento
tardio). O webhook PAYMENT_PARTIALLY_REFUNDED confirma refund_status="done"
sem flipar Payment.status (Asaas mantém RECEIVED/CONFIRMED em refund parcial).
"""
import asyncio
from unittest.mock import AsyncMock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import app.models  # noqa: F401
from app.core.database import Base, get_db, get_global_db
from app.models.payment import Payment
from app.models.tenant import Tenant
from app.models.user import User
from app.routes import payments as payments_route
from app.services.tenant_seed_service import DEFAULT_TENANT_SLUG

TENANT_ID = "t-test"
TUTOR_ID = "tutor-test"


def _run(coro):
    return asyncio.run(coro)


def _build():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine)()
    db.add(Tenant(id=TENANT_ID, name="Aumigao", slug=DEFAULT_TENANT_SLUG, status="active", plan="business"))
    db.add(User(id=TUTOR_ID, email="t@x.com", password_hash="x", role="cliente", tenant_id=TENANT_ID))
    db.commit()
    test_app = FastAPI()
    test_app.include_router(payments_route.router)
    test_app.dependency_overrides[get_db] = lambda: db
    test_app.dependency_overrides[get_global_db] = lambda: db
    return test_app, db


def _mock_httpx(status_code=200):
    mock_client = AsyncMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=None)
    response = AsyncMock()
    response.status_code = status_code
    mock_client.post = AsyncMock(return_value=response)
    return mock_client


def test_refund_asaas_charge_full_sends_empty_payload():
    mock_client = _mock_httpx()
    with (
        patch.object(payments_route, "_get_asaas_config", return_value={"base_url": "https://sb.asaas.com/v3", "api_key": "k", "is_live": False}),
        patch.object(payments_route, "asaas_headers", return_value={}),
        patch.object(payments_route, "httpx") as mock_httpx_mod,
    ):
        mock_httpx_mod.AsyncClient.return_value = mock_client
        ok = _run(payments_route.refund_asaas_charge("asaas_sandbox", "prov-1"))
    assert ok is True
    mock_client.post.assert_awaited_once_with("/payments/prov-1/refund", json={})


def test_refund_asaas_charge_partial_sends_value_payload():
    mock_client = _mock_httpx()
    with (
        patch.object(payments_route, "_get_asaas_config", return_value={"base_url": "https://sb.asaas.com/v3", "api_key": "k", "is_live": False}),
        patch.object(payments_route, "asaas_headers", return_value={}),
        patch.object(payments_route, "httpx") as mock_httpx_mod,
    ):
        mock_httpx_mod.AsyncClient.return_value = mock_client
        ok = _run(payments_route.refund_asaas_charge("asaas_sandbox", "prov-1", value=23.45))
    assert ok is True
    mock_client.post.assert_awaited_once_with("/payments/prov-1/refund", json={"value": 23.45})


def test_refund_asaas_charge_internal_sandbox_id_skips_call():
    # Cobrança fallback local (sem gateway real) — não deve chamar o Asaas.
    ok = _run(payments_route.refund_asaas_charge("asaas_sandbox", "internal-sandbox-xyz", value=10.0))
    assert ok is False


def test_webhook_partial_refund_confirms_status_done_without_flipping_payment_status(monkeypatch):
    monkeypatch.setenv("ASAAS_WEBHOOK_TOKEN", "segredo")
    test_app, db = _build()
    db.add(Payment(
        id="pay-1", tenant_id=TENANT_ID, tutor_id=TUTOR_ID, amount=100.0, walk_id=None,
        status="pagamento_confirmado_sandbox", provider="asaas_sandbox", provider_payment_id="prov-1",
        refund_status="pending", refunded_amount=20.0,
    ))
    db.commit()
    client = TestClient(test_app)
    r = client.post(
        "/payments/webhooks/asaas",
        json={"event": "PAYMENT_PARTIALLY_REFUNDED", "payment": {"id": "prov-1", "status": "RECEIVED"}},
        headers={"asaas-access-token": "segredo"},
    )
    assert r.status_code == 200, r.text
    db.expire_all()
    payment = db.query(Payment).filter(Payment.id == "pay-1").first()
    assert payment.refund_status == "done"
    # Estorno PARCIAL não tira o payment do estado confirmado (dinheiro majoritariamente recebido).
    assert payment.status == "pagamento_confirmado_sandbox"


def test_webhook_full_refund_confirms_status_done_and_flips_payment_status(monkeypatch):
    monkeypatch.setenv("ASAAS_WEBHOOK_TOKEN", "segredo")
    test_app, db = _build()
    db.add(Payment(
        id="pay-2", tenant_id=TENANT_ID, tutor_id=TUTOR_ID, amount=100.0, walk_id=None,
        status="pagamento_confirmado_sandbox", provider="asaas_sandbox", provider_payment_id="prov-2",
        refund_status="pending", refunded_amount=100.0,
    ))
    db.commit()
    client = TestClient(test_app)
    r = client.post(
        "/payments/webhooks/asaas",
        json={"event": "PAYMENT_REFUNDED", "payment": {"id": "prov-2", "status": "REFUNDED"}},
        headers={"asaas-access-token": "segredo"},
    )
    assert r.status_code == 200, r.text
    db.expire_all()
    payment = db.query(Payment).filter(Payment.id == "pay-2").first()
    assert payment.refund_status == "done"
    assert payment.status == "pagamento_estornado"
