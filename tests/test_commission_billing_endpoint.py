"""Task 6 — Testes do endpoint interno + roteamento de webhook tenant_comm:.

Exercita:
  - POST /payments/webhooks/asaas  com externalReference="tenant_comm:..." marca comissão paga.
  - O adaptador make_asaas_charge_fn() existe e tem assinatura correta (unit smoke).
  - O endpoint interno /payments/internal/commission-billing/run existe e requer token.
"""
import os
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from fastapi import FastAPI
from fastapi.testclient import TestClient
import app.models  # noqa: F401  — registra todos os models no Base
from app.core.database import Base, get_db, get_global_db
from app.models.commission_entry import CommissionEntry, COMM_BILLED, COMM_PAID


# ---------------------------------------------------------------------------
# Helpers de DB in-memory
# ---------------------------------------------------------------------------

def _make_engine():
    return create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )


def _client_and_db():
    """Monta um TestClient isolado com DB in-memory e override de dependências."""
    engine = _make_engine()
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    db = Session()

    # Override das dependências de sessão de banco
    def _override_db():
        return Session()

    from app.routes import payments as payments_routes
    app_test = FastAPI()
    app_test.include_router(payments_routes.router)
    app_test.dependency_overrides[get_db] = _override_db
    app_test.dependency_overrides[get_global_db] = _override_db

    return TestClient(app_test, raise_server_exceptions=False), db


# ---------------------------------------------------------------------------
# Test 1 — webhook marca tenant_comm: como paid
# ---------------------------------------------------------------------------

def test_webhook_marks_commission_paid():
    """PAYMENT_RECEIVED com externalReference=tenant_comm:... deve marcar entradas billed→paid."""
    os.environ["ASAAS_WEBHOOK_TOKEN"] = "wh-secret"
    os.environ["INTERNAL_SWEEP_TOKEN"] = "sweep-secret"

    client, db = _client_and_db()

    # Seed: uma entrada no status billed, vinculada ao provider_payment_id "pay-9"
    db.add(CommissionEntry(
        id="ce1",
        tenant_id="t1",
        walk_id="w1",
        period="2026-06",
        walk_price=30.0,
        commission_percent=10.0,
        amount=3.0,
        is_network=False,
        status=COMM_BILLED,
        asaas_payment_id="pay-9",
    ))
    db.commit()

    # Evento Asaas — PAYMENT_RECEIVED com externalReference tenant_comm:
    payload = {
        "event": "PAYMENT_RECEIVED",
        "payment": {
            "id": "pay-9",
            "externalReference": "tenant_comm:t1:2026-06",
        },
    }
    r = client.post(
        "/payments/webhooks/asaas",
        json=payload,
        headers={"asaas-access-token": "wh-secret"},
    )
    assert r.status_code in (200, 204), f"Esperado 200/204, got {r.status_code}: {r.text}"

    # Verifica que a entrada foi marcada como paid no banco de dados compartilhado
    db.expire_all()
    entry = db.query(CommissionEntry).filter_by(id="ce1").one()
    assert entry.status == COMM_PAID, f"Status esperado 'paid', got '{entry.status}'"


# ---------------------------------------------------------------------------
# Test 2 — webhook com PAYMENT_CONFIRMED também marca paid
# ---------------------------------------------------------------------------

def test_webhook_payment_confirmed_also_marks_paid():
    """PAYMENT_CONFIRMED deve igualmente marcar tenant_comm: como paid."""
    os.environ["ASAAS_WEBHOOK_TOKEN"] = "wh-secret"

    client, db = _client_and_db()

    db.add(CommissionEntry(
        id="ce2",
        tenant_id="t1",
        walk_id="w2",
        period="2026-06",
        walk_price=50.0,
        commission_percent=10.0,
        amount=5.0,
        is_network=False,
        status=COMM_BILLED,
        asaas_payment_id="pay-10",
    ))
    db.commit()

    payload = {
        "event": "PAYMENT_CONFIRMED",
        "payment": {
            "id": "pay-10",
            "externalReference": "tenant_comm:t1:2026-06",
        },
    }
    r = client.post(
        "/payments/webhooks/asaas",
        json=payload,
        headers={"asaas-access-token": "wh-secret"},
    )
    assert r.status_code in (200, 204), f"got {r.status_code}: {r.text}"

    db.expire_all()
    entry = db.query(CommissionEntry).filter_by(id="ce2").one()
    assert entry.status == COMM_PAID


# ---------------------------------------------------------------------------
# Test 3 — webhook não autorizado retorna 401
# ---------------------------------------------------------------------------

def test_webhook_unauthorized_returns_401():
    """Webhook sem token correto deve retornar 401."""
    os.environ["ASAAS_WEBHOOK_TOKEN"] = "wh-secret"

    client, db = _client_and_db()

    payload = {
        "event": "PAYMENT_RECEIVED",
        "payment": {"id": "pay-x", "externalReference": "tenant_comm:t1:2026-06"},
    }
    r = client.post(
        "/payments/webhooks/asaas",
        json=payload,
        headers={"asaas-access-token": "wrong-token"},
    )
    assert r.status_code == 401


# ---------------------------------------------------------------------------
# Test 4 — endpoint interno requer token
# ---------------------------------------------------------------------------

def test_internal_endpoint_requires_token():
    """POST /payments/internal/commission-billing/run sem token correto retorna 401."""
    os.environ["INTERNAL_SWEEP_TOKEN"] = "sweep-secret"

    client, db = _client_and_db()

    r = client.post(
        "/payments/internal/commission-billing/run",
        params={"period": "2026-06"},
        headers={"x-internal-token": "wrong"},
    )
    assert r.status_code == 401


# ---------------------------------------------------------------------------
# Test 5 — endpoint interno aceita token correto (com período sem entradas)
# ---------------------------------------------------------------------------

def test_internal_endpoint_accepts_valid_token():
    """POST com token correto e sem entradas accrued retorna 200 com charges_created=0."""
    os.environ["INTERNAL_SWEEP_TOKEN"] = "sweep-secret"

    client, db = _client_and_db()

    r = client.post(
        "/payments/internal/commission-billing/run",
        params={"period": "2026-05"},
        headers={"x-internal-token": "sweep-secret"},
    )
    assert r.status_code == 200
    data = r.json()
    assert data.get("charges_created") == 0
    assert data.get("period") == "2026-05"


# ---------------------------------------------------------------------------
# Test 6 — make_asaas_charge_fn existe e tem assinatura correta (smoke test)
# ---------------------------------------------------------------------------

def test_make_asaas_charge_fn_exists_and_is_callable():
    """make_asaas_charge_fn() deve existir no serviço e retornar um callable."""
    from app.services.commission_billing_service import make_asaas_charge_fn
    fn = make_asaas_charge_fn()
    assert callable(fn), "make_asaas_charge_fn() deve retornar um callable"


# ---------------------------------------------------------------------------
# Item 2 — Validação de formato de `period` no endpoint
# ---------------------------------------------------------------------------

def test_internal_endpoint_rejects_invalid_period_format():
    """POST com period inválido (não YYYY-MM) deve retornar 422."""
    os.environ["INTERNAL_SWEEP_TOKEN"] = "sweep-secret"

    client, db = _client_and_db()

    for bad_period in ("2026-6", "202606", "26-06", "2026/06", ""):
        r = client.post(
            "/payments/internal/commission-billing/run",
            params={"period": bad_period},
            headers={"x-internal-token": "sweep-secret"},
        )
        assert r.status_code == 422, (
            f"period={bad_period!r}: esperado 422, got {r.status_code}: {r.text}"
        )


def test_internal_endpoint_rejects_missing_period():
    """POST sem parâmetro `period` deve retornar 422."""
    os.environ["INTERNAL_SWEEP_TOKEN"] = "sweep-secret"

    client, db = _client_and_db()

    r = client.post(
        "/payments/internal/commission-billing/run",
        headers={"x-internal-token": "sweep-secret"},
    )
    # FastAPI já devolve 422 para query param obrigatório ausente,
    # ou nossa validação cobre — qualquer 422 é aceitável.
    assert r.status_code == 422, (
        f"period ausente: esperado 422, got {r.status_code}: {r.text}"
    )
