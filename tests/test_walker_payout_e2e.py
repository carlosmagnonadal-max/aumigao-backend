# backend/tests/test_walker_payout_e2e.py
"""
Task 4 (Fase 3): webhook TRANSFER_FAILED reverte saque para 'pending'.

Shape do evento Asaas: {"event": "TRANSFER_FAILED", "transfer": {"id": "<transfer-id>"}}
O saque é localizado por Payment.provider_payment_id == transfer.id AND provider == "pix".
Outros eventos TRANSFER_* (DONE, CREATED, etc.) são no-op — retornam 200 sem alterar dados.
"""
import os
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from fastapi import FastAPI
from fastapi.testclient import TestClient

import app.models  # noqa: F401
from app.core.database import Base, get_global_db
from app.models.payment import Payment

_TOKEN = "segredo-test"
_HEADERS = {"asaas-access-token": _TOKEN}


def _client_db():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    db = Session()
    from app.routes import payments as pr
    test_app = FastAPI()
    test_app.include_router(pr.router)
    test_app.dependency_overrides[get_global_db] = lambda: db
    return TestClient(test_app), db


def test_transfer_failed_reverts_withdrawal_to_pending(monkeypatch):
    """TRANSFER_FAILED deve reverter o saque (Payment provider='pix') para 'pending'."""
    monkeypatch.setenv("ASAAS_WEBHOOK_TOKEN", _TOKEN)
    client, db = _client_db()
    db.add(Payment(id="wd1", tenant_id="t1", tutor_id="k1", walk_id=None, amount=-50,
                   status="paid", provider="pix", provider_payment_id="tr-9"))
    db.commit()
    r = client.post("/payments/webhooks/asaas",
                    json={"event": "TRANSFER_FAILED", "transfer": {"id": "tr-9"}},
                    headers=_HEADERS)
    assert r.status_code in (200, 204), r.text
    db.expire_all()
    assert db.get(Payment, "wd1").status == "pending"  # revertido p/ nova tentativa


def test_transfer_done_is_noop(monkeypatch):
    """TRANSFER_DONE e outros eventos TRANSFER_* não FAILED devem ser no-op (200, sem crash)."""
    monkeypatch.setenv("ASAAS_WEBHOOK_TOKEN", _TOKEN)
    client, db = _client_db()
    db.add(Payment(id="wd2", tenant_id="t1", tutor_id="k1", walk_id=None, amount=-50,
                   status="paid", provider="pix", provider_payment_id="tr-10"))
    db.commit()
    r = client.post("/payments/webhooks/asaas",
                    json={"event": "TRANSFER_DONE", "transfer": {"id": "tr-10"}},
                    headers=_HEADERS)
    assert r.status_code in (200, 204), r.text
    db.expire_all()
    # status permanece inalterado — no-op
    assert db.get(Payment, "wd2").status == "paid"


def test_transfer_failed_unknown_id_is_noop(monkeypatch):
    """TRANSFER_FAILED com id desconhecido não deve crashar."""
    monkeypatch.setenv("ASAAS_WEBHOOK_TOKEN", _TOKEN)
    client, db = _client_db()
    r = client.post("/payments/webhooks/asaas",
                    json={"event": "TRANSFER_FAILED", "transfer": {"id": "tr-unknown"}},
                    headers=_HEADERS)
    assert r.status_code in (200, 204), r.text
