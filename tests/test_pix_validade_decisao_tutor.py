"""Feature PIX validade + decisão do tutor (rigor de dinheiro).

Cobre:
- A: payload PIX com dueDate=hoje + daysAfterDueDateToRegistrationCancellation=0;
     cartão mantém dueDate D+1 e sem o campo de cancelamento.
- B: recriação de cobrança com walk_id cancela a pendente antiga (Asaas DELETE +
     status local `cancelado_regenerado`) antes de criar a nova.
- E: POST /walks/{id}/tutor-decision — reschedule (pago vs não pago; exclusivo mantém
     walker), switch_walker (só exclusivo), refund (aciona Asaas + cancela walk),
     404/409/422.
- G: serialização inclui tutor_decision_required / decision_reason /
     is_exclusive_walker / payment_cutoff_at.

Sem rede real: httpx.AsyncClient e helpers Asaas são mockados.
"""
from __future__ import annotations

import asyncio
from datetime import date, datetime, timedelta

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import app.models  # noqa: F401
from app.core.database import Base, get_db, get_global_db
from app.dependencies.auth import get_current_user
from app.models.payment import Payment
from app.models.pet import Pet
from app.models.tenant import Tenant
from app.models.user import User
from app.models.walk import Walk
from app.routes import payments, walks
from app.routes.payments import create_asaas_payment
from app.schemas.payment import PaymentCreate
from app.services.operational_matching_service import serialize_operational_walk
from app.services.tenant_seed_service import DEFAULT_TENANT_SLUG

TENANT_ID = "t-test"
TUTOR_ID = "tutor-test"


def _future_iso(hours: int = 6) -> str:
    return (datetime.utcnow() + timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%M")


# ─────────────────────────── A: payload PIX/cartão ────────────────────────────

class _FakeResp:
    def __init__(self, status_code=200, payload=None):
        self.status_code = status_code
        self._payload = payload or {}

    def json(self):
        return self._payload

    @property
    def text(self):
        return str(self._payload)


class _FakeAsaasClient:
    """Fake httpx.AsyncClient p/ create_asaas_payment — registra o payload de /payments."""

    def __init__(self, *a, **k):
        self.captured_payment_payload = None

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def post(self, path, json=None):
        if path == "/customers":
            return _FakeResp(200, {"id": "cust-1"})
        if path == "/payments":
            self.captured_payment_payload = json
            return _FakeResp(200, {"id": "prov-new", "status": "PENDING", "invoiceUrl": "https://x"})
        return _FakeResp(200, {})

    async def get(self, path):
        if path.endswith("/pixQrCode"):
            return _FakeResp(200, {"encodedImage": "img", "payload": "copiaecola", "expirationDate": "2026-07-03"})
        return _FakeResp(200, {})


@pytest.fixture
def _capture_client(monkeypatch):
    captured = {}

    def _factory(*a, **k):
        client = _FakeAsaasClient()
        captured["client"] = client
        return client

    monkeypatch.setattr(payments, "PAYMENT_MODE", "asaas_sandbox")
    monkeypatch.setattr(payments.httpx, "AsyncClient", _factory)
    return captured


def test_pix_payload_due_today_and_cancel_zero(_capture_client):
    user = User(id="u1", email="a@b.com", full_name="Tutor")
    asyncio.run(create_asaas_payment(PaymentCreate(amount=50.0, method="pix", walk_id="w1"), user))
    payload = _capture_client["client"].captured_payment_payload
    assert payload["billingType"] == "PIX"
    assert payload["dueDate"] == str(date.today())  # expira no MESMO dia
    assert payload["daysAfterDueDateToRegistrationCancellation"] == 0


def test_card_payload_due_d1_and_no_cancel_field(_capture_client):
    user = User(id="u1", email="a@b.com", full_name="Tutor")
    asyncio.run(create_asaas_payment(PaymentCreate(amount=50.0, method="card", walk_id="w1"), user))
    payload = _capture_client["client"].captured_payment_payload
    assert payload["billingType"] != "PIX"  # UNDEFINED no sandbox
    assert payload["dueDate"] == str(date.today() + timedelta(days=1))  # cartão mantém D+1
    assert "daysAfterDueDateToRegistrationCancellation" not in payload


# ─────────────────────── B: recriação sem duplicar ────────────────────────────

def _build_payments_app():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine)()
    db.add(Tenant(id=TENANT_ID, name="Aumigao", slug=DEFAULT_TENANT_SLUG, status="active", plan="business"))
    db.add(User(id=TUTOR_ID, email="t@x.com", password_hash="x", role="cliente", tenant_id=TENANT_ID))
    db.add(Pet(id="pet-1", tutor_id=TUTOR_ID, tenant_id=TENANT_ID, name="Bolinha"))
    db.commit()
    test_app = FastAPI()
    test_app.include_router(payments.router)
    test_app.dependency_overrides[get_db] = lambda: db
    test_app.dependency_overrides[get_global_db] = lambda: db
    test_app.dependency_overrides[get_current_user] = lambda: db.get(User, TUTOR_ID)
    return test_app, db


def test_recreation_cancels_stale_pending(monkeypatch):
    monkeypatch.setattr(payments, "PAYMENT_MODE", "asaas_sandbox")
    test_app, db = _build_payments_app()
    # Walk + pendente ANTIGA (>2min) para o mesmo walk.
    db.add(Walk(id="w1", tutor_id=TUTOR_ID, tenant_id=TENANT_ID, pet_id="pet-1",
                scheduled_date=_future_iso(), duration_minutes=30, price=50.0,
                status="aguardando_pagamento", operational_status="awaiting_payment"))
    old = Payment(id="pay-old", tenant_id=TENANT_ID, tutor_id=TUTOR_ID, amount=50.0, walk_id="w1",
                  status="aguardando_pagamento", provider="asaas_sandbox", provider_payment_id="prov-old")
    old.created_at = datetime.utcnow() - timedelta(minutes=10)
    db.add(old)
    db.commit()

    # mocka o create_asaas_payment (nova cobrança) e o cancel_asaas_charge (DELETE).
    cancelled = {}

    async def _fake_create(payload, user):
        return {"id": "prov-new", "status": "PENDING", "invoiceUrl": "https://x"}, {}, "PIX"

    async def _fake_cancel(provider, pid):
        cancelled["pid"] = pid
        return True

    monkeypatch.setattr(payments, "create_asaas_payment", _fake_create)
    monkeypatch.setattr(payments, "cancel_asaas_charge", _fake_cancel)

    client = TestClient(test_app)
    r = client.post("/payments/create", json={"amount": 50.0, "method": "pix", "walk_id": "w1"})
    assert r.status_code == 200, r.text
    # a antiga foi cancelada no Asaas e marcada localmente
    assert cancelled.get("pid") == "prov-old"
    db.expire_all()
    assert db.get(Payment, "pay-old").status == "cancelado_regenerado"
    # uma nova pendente foi criada
    news = db.query(Payment).filter(Payment.walk_id == "w1", Payment.provider_payment_id == "prov-new").all()
    assert len(news) == 1


def test_recent_pending_is_idempotent_not_regenerated(monkeypatch):
    monkeypatch.setattr(payments, "PAYMENT_MODE", "asaas_sandbox")
    test_app, db = _build_payments_app()
    db.add(Walk(id="w1", tutor_id=TUTOR_ID, tenant_id=TENANT_ID, pet_id="pet-1",
                scheduled_date=_future_iso(), duration_minutes=30, price=50.0,
                status="aguardando_pagamento", operational_status="awaiting_payment"))
    recent = Payment(id="pay-recent", tenant_id=TENANT_ID, tutor_id=TUTOR_ID, amount=50.0, walk_id="w1",
                     status="aguardando_pagamento", provider="asaas_sandbox", provider_payment_id="prov-recent")
    db.add(recent)
    db.commit()

    async def _boom_cancel(provider, pid):
        raise AssertionError("não deve cancelar pendente recente")

    monkeypatch.setattr(payments, "cancel_asaas_charge", _boom_cancel)
    client = TestClient(test_app)
    r = client.post("/payments/create", json={"amount": 50.0, "method": "pix", "walk_id": "w1"})
    assert r.status_code == 200, r.text
    # devolveu a existente (idempotência); não regenerou
    assert r.json()["id"] == "pay-recent"


# ─────────────────── E + G: endpoint de decisão do tutor ──────────────────────

def _build_walks_app():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine)()
    db.info["rls_tenant"] = "*"
    db.add(Tenant(id=TENANT_ID, name="Aumigao", slug=DEFAULT_TENANT_SLUG, status="active", plan="business"))
    db.add(User(id=TUTOR_ID, email="t@x.com", password_hash="x", role="cliente", tenant_id=TENANT_ID))
    db.add(User(id="tutor-2", email="o@x.com", password_hash="x", role="cliente", tenant_id=TENANT_ID))
    db.add(Pet(id="pet-1", tutor_id=TUTOR_ID, tenant_id=TENANT_ID, name="Bolinha"))
    db.commit()
    test_app = FastAPI()
    test_app.include_router(walks.router)
    test_app.dependency_overrides[get_db] = lambda: db
    test_app.dependency_overrides[get_current_user] = lambda: db.get(User, TUTOR_ID)
    return test_app, db


def _make_walk(db, *, op_status="awaiting_tutor_reconfirmation", mode="only_selected",
               reason="pagamento_apos_corte", scheduled=None, walker_id="walker-1"):
    db.add(Walk(id="w1", tutor_id=TUTOR_ID, tenant_id=TENANT_ID, pet_id="pet-1",
                scheduled_date=scheduled or _future_iso(), duration_minutes=30, price=50.0,
                status="Aguardando confirmação do tutor", operational_status=op_status,
                walker_selection_mode=mode, walker_id=walker_id, assigned_walker_id=walker_id,
                no_walker_reason=reason))
    db.commit()


def _confirmed_payment(db):
    db.add(Payment(id="pay-c", tenant_id=TENANT_ID, tutor_id=TUTOR_ID, amount=50.0, walk_id="w1",
                   status="pagamento_confirmado_sandbox", provider="asaas_sandbox", provider_payment_id="prov-c"))
    db.commit()


def test_tutor_decision_404_for_non_owner(monkeypatch):
    test_app, db = _build_walks_app()
    _make_walk(db)
    test_app.dependency_overrides[get_current_user] = lambda: db.get(User, "tutor-2")
    r = TestClient(test_app).post("/walks/w1/tutor-decision", json={"action": "refund"})
    assert r.status_code == 404


def test_tutor_decision_409_wrong_state(monkeypatch):
    test_app, db = _build_walks_app()
    _make_walk(db, op_status="ride_scheduled")
    r = TestClient(test_app).post("/walks/w1/tutor-decision", json={"action": "refund"})
    assert r.status_code == 409


def test_tutor_decision_422_bad_action(monkeypatch):
    test_app, db = _build_walks_app()
    _make_walk(db)
    r = TestClient(test_app).post("/walks/w1/tutor-decision", json={"action": "banana"})
    assert r.status_code == 422


def test_tutor_decision_reschedule_paid_keeps_exclusive_walker(monkeypatch):
    test_app, db = _build_walks_app()
    _make_walk(db, mode="only_selected", walker_id="walker-1")
    _confirmed_payment(db)
    # start_matching é pesado (matching real); stub para focar na transição de estado.
    monkeypatch.setattr(walks, "start_matching", lambda *a, **k: None)
    new_start = (datetime.utcnow() + timedelta(hours=4)).strftime("%Y-%m-%dT%H:%M")
    r = TestClient(test_app).post("/walks/w1/tutor-decision",
                                  json={"action": "reschedule", "scheduled_date": new_start.split("T")[0],
                                        "walk_time": new_start.split("T")[1]})
    assert r.status_code == 200, r.text
    db.expire_all()
    walk = db.get(Walk, "w1")
    assert walk.operational_status == "pending_walker_confirmation"  # pago → confirmação
    assert walk.walker_selection_mode == "only_selected"  # exclusivo mantido
    assert walk.assigned_walker_id == "walker-1"


def test_tutor_decision_reschedule_unpaid_goes_awaiting_payment(monkeypatch):
    test_app, db = _build_walks_app()
    _make_walk(db)  # sem pagamento confirmado
    new_start = (datetime.utcnow() + timedelta(hours=4)).strftime("%Y-%m-%dT%H:%M")
    r = TestClient(test_app).post("/walks/w1/tutor-decision",
                                  json={"action": "reschedule", "scheduled_date": new_start.split("T")[0],
                                        "walk_time": new_start.split("T")[1]})
    assert r.status_code == 200, r.text
    db.expire_all()
    assert db.get(Walk, "w1").operational_status == "awaiting_payment"


def test_tutor_decision_reschedule_rejects_too_soon(monkeypatch):
    test_app, db = _build_walks_app()
    _make_walk(db)
    soon = (datetime.utcnow() + timedelta(minutes=10)).strftime("%Y-%m-%dT%H:%M")
    r = TestClient(test_app).post("/walks/w1/tutor-decision",
                                  json={"action": "reschedule", "scheduled_date": soon.split("T")[0],
                                        "walk_time": soon.split("T")[1]})
    assert r.status_code == 422  # dentro do corte de 45min


def test_tutor_decision_switch_walker_only_exclusive(monkeypatch):
    test_app, db = _build_walks_app()
    _make_walk(db, mode="auto")  # flexível → switch não permitido
    r = TestClient(test_app).post("/walks/w1/tutor-decision", json={"action": "switch_walker"})
    assert r.status_code == 409


def test_tutor_decision_switch_walker_clears_exclusivity(monkeypatch):
    test_app, db = _build_walks_app()
    _make_walk(db, mode="only_selected", walker_id="walker-1")
    _confirmed_payment(db)
    monkeypatch.setattr(walks, "start_matching", lambda *a, **k: None)
    r = TestClient(test_app).post("/walks/w1/tutor-decision", json={"action": "switch_walker"})
    assert r.status_code == 200, r.text
    db.expire_all()
    walk = db.get(Walk, "w1")
    assert walk.walker_selection_mode == "auto"
    assert walk.assigned_walker_id is None
    assert walk.operational_status == "pending_walker_confirmation"


def test_tutor_decision_refund_triggers_asaas_and_cancels(monkeypatch):
    test_app, db = _build_walks_app()
    _make_walk(db)
    _confirmed_payment(db)
    called = {}

    async def _fake_refund(provider, pid):
        called["pid"] = pid
        return True

    monkeypatch.setattr("app.routes.payments.refund_asaas_charge", _fake_refund)
    r = TestClient(test_app).post("/walks/w1/tutor-decision", json={"action": "refund"})
    assert r.status_code == 200, r.text
    assert called.get("pid") == "prov-c"
    db.expire_all()
    assert db.get(Walk, "w1").operational_status == "ride_cancelled"


def test_tutor_decision_refund_502_when_asaas_fails(monkeypatch):
    test_app, db = _build_walks_app()
    _make_walk(db)
    _confirmed_payment(db)

    async def _fail_refund(provider, pid):
        return False

    monkeypatch.setattr("app.routes.payments.refund_asaas_charge", _fail_refund)
    r = TestClient(test_app).post("/walks/w1/tutor-decision", json={"action": "refund"})
    assert r.status_code == 502
    db.expire_all()
    # walk NÃO foi cancelado (estorno falhou)
    assert db.get(Walk, "w1").operational_status == "awaiting_tutor_reconfirmation"


def test_tutor_decision_refund_409_without_confirmed_payment(monkeypatch):
    test_app, db = _build_walks_app()
    _make_walk(db)  # sem pagamento confirmado
    r = TestClient(test_app).post("/walks/w1/tutor-decision", json={"action": "refund"})
    assert r.status_code == 409


# ─────────────────────────────── G: serialização ─────────────────────────────

def test_serialization_flags_tutor_decision():
    test_app, db = _build_walks_app()
    _make_walk(db, op_status="awaiting_tutor_reconfirmation", mode="only_selected",
               reason="pagamento_apos_corte")
    walk = db.get(Walk, "w1")
    data = serialize_operational_walk(walk, db)
    assert data["tutor_decision_required"] is True
    assert data["decision_reason"] == "pagamento_apos_corte"
    assert data["is_exclusive_walker"] is True


def test_serialization_payment_cutoff_at_present_when_awaiting_payment():
    test_app, db = _build_walks_app()
    start = datetime.utcnow() + timedelta(hours=3)
    _make_walk(db, op_status="awaiting_payment", reason=None,
               scheduled=start.strftime("%Y-%m-%dT%H:%M"))
    walk = db.get(Walk, "w1")
    data = serialize_operational_walk(walk, db)
    assert data["payment_cutoff_at"] is not None
    # cutoff = início − 45min
    expected = (start - timedelta(minutes=45)).replace(second=0, microsecond=0)
    assert data["payment_cutoff_at"].startswith(expected.strftime("%Y-%m-%dT%H:%M"))
    assert data["tutor_decision_required"] is False


def test_serialization_no_decision_reason_for_legacy_reconfirmation():
    """Reconfirmação por outros motivos (limite de tentativas) NÃO liga o menu novo."""
    test_app, db = _build_walks_app()
    _make_walk(db, op_status="awaiting_tutor_reconfirmation", reason="Limite de tentativas atingido.")
    walk = db.get(Walk, "w1")
    data = serialize_operational_walk(walk, db)
    assert data["tutor_decision_required"] is False
    assert data["decision_reason"] is None


# ──────────────── F: exclusivo não aceitou → menu de decisão ──────────────────

def test_exclusive_walker_unavailable_routes_to_decision_menu():
    """Item F: passeador EXCLUSIVO indisponível → awaiting_tutor_reconfirmation com
    motivo-máquina 'exclusivo_nao_aceitou' (o serializer liga o menu)."""
    from app.services.operational_matching_service import _selected_walker_unavailable
    test_app, db = _build_walks_app()
    _make_walk(db, op_status="pending_walker_confirmation", mode="only_selected", reason=None)
    walk = db.get(Walk, "w1")
    _selected_walker_unavailable(walk, db, "Passeador escolhido não confirmou.")
    db.commit()
    db.expire_all()
    walk = db.get(Walk, "w1")
    assert walk.operational_status == "awaiting_tutor_reconfirmation"
    assert walk.no_walker_reason == "exclusivo_nao_aceitou"
    data = serialize_operational_walk(walk, db)
    assert data["tutor_decision_required"] is True
    assert data["decision_reason"] == "exclusivo_nao_aceitou"
    assert data["is_exclusive_walker"] is True
