# backend/tests/test_walker_earning_autovoid_webhook.py
"""
Task 2 (Fase 3): auto-void do ganho do passeador no webhook de refund/chargeback.

Nota sobre REDE: este teste cobre o caso de passeio AVULSO (walk_id no Payment
de gateway). Passeio de REDE pago por crédito tem o refund no Payment da COMPRA do
crédito (que NÃO carrega o walk_id do passeio individual) — esse caso é coberto
pelo void MANUAL via endpoint admin (Task 1).
"""
import os
from datetime import datetime, timezone
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from fastapi import FastAPI
from fastapi.testclient import TestClient

import app.models  # noqa: F401
from app.core.database import Base, get_global_db
from app.models.payment import Payment
from app.models.walker_earning import WalkerEarning, WE_ACCRUED, WE_VOID

_TOKEN = "segredo-test"
_HEADERS = {"asaas-access-token": _TOKEN}


def _client_db():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    db = Session()
    from app.routes import payments as pr
    test_app = FastAPI()
    test_app.include_router(pr.router)
    test_app.dependency_overrides[get_global_db] = lambda: db
    return TestClient(test_app), db


def _seed(db, walk_id="w1", pid="pay-1"):
    db.add(
        Payment(
            id="p1",
            tenant_id="t1",
            tutor_id="tut",
            walk_id=walk_id,
            amount=30,
            status="pagamento_confirmado_sandbox",
            provider="asaas_sandbox",
            provider_payment_id=pid,
            walker_amount=24.6,
        )
    )
    db.add(
        WalkerEarning(
            id="we1",
            walker_id="k1",
            tenant_id="t1",
            walk_id=walk_id,
            gross=30,
            platform_amount=5.4,
            amount=24.6,
            status=WE_ACCRUED,
            accrued_at=datetime(2026, 6, 1, tzinfo=timezone.utc),
            payable_at=datetime(2026, 6, 10, tzinfo=timezone.utc),
        )
    )
    db.commit()


def test_refund_event_voids_earning(monkeypatch):
    """PAYMENT_REFUNDED deve anular o WalkerEarning do walk_id vinculado."""
    monkeypatch.setenv("ASAAS_WEBHOOK_TOKEN", _TOKEN)
    client, db = _client_db()
    _seed(db)
    r = client.post(
        "/payments/webhooks/asaas",
        json={"event": "PAYMENT_REFUNDED", "payment": {"id": "pay-1"}},
        headers=_HEADERS,
    )
    assert r.status_code in (200, 204), r.text
    db.expire_all()
    earning = db.query(WalkerEarning).filter_by(walk_id="w1").one()
    assert earning.status == WE_VOID, f"esperado WE_VOID, obtido {earning.status!r}"
    assert earning.void_reason is not None
    assert "PAYMENT_REFUNDED" in earning.void_reason


def test_chargeback_requested_event_voids_earning(monkeypatch):
    """PAYMENT_CHARGEBACK_REQUESTED também deve anular o ganho."""
    monkeypatch.setenv("ASAAS_WEBHOOK_TOKEN", _TOKEN)
    client, db = _client_db()
    _seed(db, walk_id="w2", pid="pay-2")
    r = client.post(
        "/payments/webhooks/asaas",
        json={"event": "PAYMENT_CHARGEBACK_REQUESTED", "payment": {"id": "pay-2"}},
        headers=_HEADERS,
    )
    assert r.status_code in (200, 204), r.text
    db.expire_all()
    assert db.query(WalkerEarning).filter_by(walk_id="w2").one().status == WE_VOID


def test_confirmed_event_does_not_void(monkeypatch):
    """PAYMENT_RECEIVED (confirmação) NÃO deve alterar o status do ganho."""
    monkeypatch.setenv("ASAAS_WEBHOOK_TOKEN", _TOKEN)
    client, db = _client_db()
    _seed(db)
    client.post(
        "/payments/webhooks/asaas",
        json={"event": "PAYMENT_RECEIVED", "payment": {"id": "pay-1"}},
        headers=_HEADERS,
    )
    db.expire_all()
    assert db.query(WalkerEarning).filter_by(walk_id="w1").one().status == WE_ACCRUED


def test_refund_no_walk_id_does_not_crash(monkeypatch):
    """Refund de Payment sem walk_id (ex.: saque, gorjeta) não deve quebrar."""
    monkeypatch.setenv("ASAAS_WEBHOOK_TOKEN", _TOKEN)
    client, db = _client_db()
    db.add(
        Payment(
            id="p2",
            tenant_id="t1",
            tutor_id="tut",
            walk_id=None,  # sem walk_id — saque ou pagamento de comissão
            amount=50,
            status="pagamento_confirmado_sandbox",
            provider="asaas_sandbox",
            provider_payment_id="pay-99",
        )
    )
    db.commit()
    r = client.post(
        "/payments/webhooks/asaas",
        json={"event": "PAYMENT_REFUNDED", "payment": {"id": "pay-99"}},
        headers=_HEADERS,
    )
    assert r.status_code in (200, 204), r.text


# ---------------------------------------------------------------------------
# Item 2: PAYMENT_REVERSED deve levar Payment a "pagamento_estornado"
#         E anular o WalkerEarning (já estava em _WALKER_EARNING_VOID_EVENTS)
# ---------------------------------------------------------------------------

def test_payment_reversed_voids_earning_and_sets_payment_estornado(monkeypatch):
    """PAYMENT_REVERSED:
    - WalkerEarning do walk_id deve ser void (já estava em _WALKER_EARNING_VOID_EVENTS)
    - Payment deve ficar com status 'pagamento_estornado' (novo: PAYMENT_REVERSED
      adicionado a _PAYMENT_REFUND_EVENTS)
    """
    monkeypatch.setenv("ASAAS_WEBHOOK_TOKEN", _TOKEN)
    client, db = _client_db()
    _seed(db, walk_id="w3", pid="pay-rev-1")
    r = client.post(
        "/payments/webhooks/asaas",
        json={"event": "PAYMENT_REVERSED", "payment": {"id": "pay-rev-1"}},
        headers=_HEADERS,
    )
    assert r.status_code in (200, 204), r.text
    db.expire_all()
    earning = db.query(WalkerEarning).filter_by(walk_id="w3").one()
    assert earning.status == WE_VOID, f"esperado WE_VOID, obtido {earning.status!r}"
    payment = db.query(Payment).filter_by(provider_payment_id="pay-rev-1").one()
    assert payment.status == "pagamento_estornado", (
        f"esperado pagamento_estornado, obtido {payment.status!r}"
    )
