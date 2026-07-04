"""Testes para feat/assinatura-pix-inline.

Cobre:
- payment_status derivado (ativa / aguardando_pagamento) em TutorSubscriptionResponse.
- GET /recurring-plans/subscription/payment:
  - caso feliz: mock Asaas retorna cobrança pendente + PIX QR Code.
  - sem assinatura ativa -> 404.
  - assinatura sem asaas_subscription_id -> 404.
  - sem cobrança pendente (lista vazia) -> 404.
  - Asaas retorna erro 500 -> 502.
  - falha de rede (exceção httpx) -> 502.
  - pix ainda não disponível -> pix_* null, invoice_url presente.
  - espelho /api/... retorna o mesmo resultado.
- Isolamento de tenant: usa rls_tenant (da request), NAO user.tenant_id (nascimento).
"""
import asyncio
from datetime import datetime, timedelta

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import app.models  # registra todas as tabelas no Base
from app.core.database import Base, get_db
from app.dependencies.auth import get_current_user
from app.models.recurring_plan import (
    SUBSCRIPTION_ACTIVE,
    RecurringPlan,
    TutorSubscription,
)
from app.models.tenant import Tenant, TenantFeature
from app.models.user import User
from app.routes import recurring_plans as rp_module
from app.schemas.recurring_plan import TutorSubscriptionResponse
from app.services.recurring_plan_service import subscribe
from app.services.tenant_seed_service import DEFAULT_TENANT_SLUG

# ---------------------------------------------------------------------------
# Constantes de tenant para os testes
# ---------------------------------------------------------------------------

TENANT_ID = "t-pix-inline"
TENANT_ID_B = "t-pix-other"
TUTOR_ID = "tutor-pix-inline"

# URL base do endpoint (com prefixo do router)
_BASE = "/recurring-plans/subscription/payment"
_API_BASE = "/api/recurring-plans/subscription/payment"


# ---------------------------------------------------------------------------
# Helpers de banco
# ---------------------------------------------------------------------------

def _make_db():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    db = Session()
    db.add(Tenant(id=TENANT_ID, name="Aumigao", slug=DEFAULT_TENANT_SLUG, status="active", plan="business"))
    db.add(TenantFeature(tenant_id=TENANT_ID, feature_key="recurring_plans", enabled=True))
    db.add(Tenant(id=TENANT_ID_B, name="Outro", slug="outro", status="active", plan="business"))
    db.add(User(id=TUTOR_ID, email="tutor@pix.com", password_hash="x", role="cliente", tenant_id=TENANT_ID))
    db.commit()
    return db


def _plan(db, tenant_id=TENANT_ID, walks=4, price=99.0):
    plan = RecurringPlan(
        tenant_id=tenant_id, name="Plano Mensal", price=price,
        walks_per_cycle=walks, interval="monthly", active=True,
    )
    db.add(plan)
    db.commit()
    db.refresh(plan)
    return plan


def _make_app(db, *, rls_tenant=TENANT_ID):
    """Monta FastAPI minimo com os dois routers de planos recorrentes."""
    application = FastAPI()
    application.include_router(rp_module.router)
    application.include_router(rp_module.api_router)

    def _get_db_override():
        db.info["rls_tenant"] = rls_tenant
        return db

    application.dependency_overrides[get_db] = _get_db_override
    application.dependency_overrides[get_current_user] = lambda: db.get(User, TUTOR_ID)
    return application


def _sub_with_asaas_id(db, asaas_id="asaas-sub-abc", credits_granted=False):
    """Cria e persiste TutorSubscription com asaas_subscription_id."""
    plan = _plan(db)
    sub = TutorSubscription(
        tenant_id=TENANT_ID,
        plan_id=plan.id,
        tutor_id=TUTOR_ID,
        status=SUBSCRIPTION_ACTIVE,
        price=plan.price,
        walks_per_cycle=plan.walks_per_cycle,
        credits_remaining=0,
        credits_granted=credits_granted,
        current_period_start=datetime.utcnow(),
        current_period_end=datetime.utcnow() + timedelta(days=30),
        asaas_subscription_id=asaas_id,
    )
    db.add(sub)
    db.commit()
    db.refresh(sub)
    return sub


# ---------------------------------------------------------------------------
# Fake httpx para testes sem rede
# ---------------------------------------------------------------------------

class _FakeResp:
    def __init__(self, status_code, json_data=None):
        self.status_code = status_code
        self._json = json_data or {}

    def json(self):
        return self._json


class _FakeAsyncClient:
    """Simula httpx.AsyncClient como async context manager sem rede.

    responses: dict mapeando substring da URL para _FakeResp ou Exception.
    """

    def __init__(self, responses, **kwargs):
        self._responses = responses

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def get(self, url, **kwargs):
        # Resolve pela chave mais longa que aparece na URL (evita que "/payments"
        # engula "/payments/{id}/pixQrCode").
        matched_key = None
        for key in self._responses:
            if key in url:
                if matched_key is None or len(key) > len(matched_key):
                    matched_key = key
        if matched_key is not None:
            resp = self._responses[matched_key]
            if isinstance(resp, Exception):
                raise resp
            return resp
        return _FakeResp(404, {"message": "not found"})


def _patch_httpx(monkeypatch, responses):
    """Substitui httpx.AsyncClient no modulo recurring_plans por _FakeAsyncClient."""
    monkeypatch.setattr(
        rp_module.httpx,
        "AsyncClient",
        lambda **kwargs: _FakeAsyncClient(responses),
    )


def _fake_asaas_config(monkeypatch):
    """Patch _get_asaas_config e asaas_headers no modulo payments para sandbox fake."""
    from app.routes import payments as pay_mod

    monkeypatch.setattr(pay_mod, "_get_asaas_config", lambda: {
        "base_url": "https://sandbox.asaas.fake",
        "api_key": "fake-key",
        "is_live": False,
    })
    monkeypatch.setattr(pay_mod, "asaas_headers", lambda api_key, mode=None: {
        "access_token": api_key,
    })


# ---------------------------------------------------------------------------
# 1. payment_status derivado
# ---------------------------------------------------------------------------

class TestPaymentStatusDerivado:

    def test_payment_status_aguardando_quando_credits_not_granted(self):
        db = _make_db()
        tenant = db.get(Tenant, TENANT_ID)
        plan = _plan(db)
        from app.services.recurring_plan_service import subscribe_async
        sub = asyncio.run(subscribe_async(db, tenant, TUTOR_ID, plan.id, tutor_user=None))
        assert sub.credits_granted is False

        response = rp_module._subscription_response(db, sub)
        assert response.payment_status == "aguardando_pagamento"

    def test_payment_status_ativa_quando_credits_granted(self):
        db = _make_db()
        tenant = db.get(Tenant, TENANT_ID)
        plan = _plan(db)
        sub = subscribe(db, tenant, TUTOR_ID, plan.id)
        assert sub.credits_granted is True

        response = rp_module._subscription_response(db, sub)
        assert response.payment_status == "ativa"

    def test_payment_status_presente_no_schema(self):
        fields = TutorSubscriptionResponse.model_fields
        assert "payment_status" in fields

    def test_schema_default_aguardando_pagamento(self):
        """Valor padrao do campo e 'aguardando_pagamento'."""
        f = TutorSubscriptionResponse.model_fields["payment_status"]
        assert f.default == "aguardando_pagamento"


# ---------------------------------------------------------------------------
# 2. GET /recurring-plans/subscription/payment
# ---------------------------------------------------------------------------

class TestGetSubscriptionPayment:

    def test_caso_feliz_retorna_payment_com_pix(self, monkeypatch):
        """Asaas retorna cobranca pendente + PIX QR Code."""
        db = _make_db()
        _sub_with_asaas_id(db)
        _fake_asaas_config(monkeypatch)
        _patch_httpx(monkeypatch, {
            "/pixQrCode": _FakeResp(200, {
                "encodedImage": "data:image/png;base64,abc123",
                "payload": "00020126330014BR.GOV.BCB.PIX",
            }),
            "/payments": _FakeResp(200, {
                "data": [{
                    "id": "pay-001",
                    "value": 99.0,
                    "dueDate": "2026-07-10",
                    "status": "PENDING",
                    "invoiceUrl": "https://asaas.fake/invoice/pay-001",
                }]
            }),
        })

        client = TestClient(_make_app(db))
        resp = client.get(_BASE)

        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["payment_id"] == "pay-001"
        assert body["value"] == 99.0
        assert body["due_date"] == "2026-07-10"
        assert body["status"] == "PENDING"
        assert body["pix_qr_code"] == "data:image/png;base64,abc123"
        assert body["pix_payload"] == "00020126330014BR.GOV.BCB.PIX"
        assert body["invoice_url"] == "https://asaas.fake/invoice/pay-001"

    def test_sem_assinatura_ativa_retorna_404(self, monkeypatch):
        """Nenhuma assinatura ativa -> 404."""
        db = _make_db()
        _fake_asaas_config(monkeypatch)

        client = TestClient(_make_app(db))
        resp = client.get(_BASE)

        assert resp.status_code == 404
        assert "assinatura" in resp.json()["detail"].lower()

    def test_assinatura_sem_asaas_id_retorna_404(self, monkeypatch):
        """Assinatura local sem asaas_subscription_id -> 404."""
        db = _make_db()
        _fake_asaas_config(monkeypatch)

        plan = _plan(db)
        sub_orphan = TutorSubscription(
            tenant_id=TENANT_ID,
            plan_id=plan.id,
            tutor_id=TUTOR_ID,
            status=SUBSCRIPTION_ACTIVE,
            price=plan.price,
            walks_per_cycle=plan.walks_per_cycle,
            credits_remaining=0,
            credits_granted=False,
            current_period_start=datetime.utcnow(),
            current_period_end=datetime.utcnow() + timedelta(days=30),
            asaas_subscription_id=None,
        )
        db.add(sub_orphan)
        db.commit()

        client = TestClient(_make_app(db))
        resp = client.get(_BASE)

        assert resp.status_code == 404
        assert "gateway" in resp.json()["detail"].lower()

    def test_sem_cobranca_pendente_retorna_404(self, monkeypatch):
        """Asaas retorna lista vazia -> 404."""
        db = _make_db()
        _sub_with_asaas_id(db)
        _fake_asaas_config(monkeypatch)
        _patch_httpx(monkeypatch, {
            "/payments": _FakeResp(200, {"data": []}),
        })

        client = TestClient(_make_app(db))
        resp = client.get(_BASE)

        assert resp.status_code == 404
        assert "pendente" in resp.json()["detail"].lower()

    def test_asaas_erro_500_retorna_502(self, monkeypatch):
        """Asaas retorna 500 -> 502."""
        db = _make_db()
        _sub_with_asaas_id(db)
        _fake_asaas_config(monkeypatch)
        _patch_httpx(monkeypatch, {
            "/payments": _FakeResp(500, {"errors": [{"description": "internal error"}]}),
        })

        client = TestClient(_make_app(db))
        resp = client.get(_BASE)

        assert resp.status_code == 502

    def test_asaas_network_error_retorna_502(self, monkeypatch):
        """Falha de rede (Exception no httpx) -> 502."""
        import httpx as real_httpx

        db = _make_db()
        _sub_with_asaas_id(db)
        _fake_asaas_config(monkeypatch)
        _patch_httpx(monkeypatch, {
            "/payments": real_httpx.ConnectTimeout("timeout"),
        })

        client = TestClient(_make_app(db))
        resp = client.get(_BASE)

        assert resp.status_code == 502

    def test_pix_nao_disponivel_retorna_null_mas_invoice_url_presente(self, monkeypatch):
        """QR Code ainda nao gerado pelo Asaas: pix_* null, invoice_url disponivel."""
        db = _make_db()
        _sub_with_asaas_id(db)
        _fake_asaas_config(monkeypatch)
        _patch_httpx(monkeypatch, {
            "/payments": _FakeResp(200, {
                "data": [{
                    "id": "pay-002",
                    "value": 99.0,
                    "dueDate": "2026-07-10",
                    "status": "PENDING",
                    "invoiceUrl": "https://asaas.fake/invoice/pay-002",
                }]
            }),
            "/pixQrCode": _FakeResp(404, {}),
        })

        client = TestClient(_make_app(db))
        resp = client.get(_BASE)

        assert resp.status_code == 200
        body = resp.json()
        assert body["pix_qr_code"] is None
        assert body["pix_payload"] is None
        assert body["invoice_url"] == "https://asaas.fake/invoice/pay-002"

    def test_espelho_api_router_funciona(self, monkeypatch):
        """GET /api/recurring-plans/subscription/payment retorna o mesmo resultado."""
        db = _make_db()
        _sub_with_asaas_id(db)
        _fake_asaas_config(monkeypatch)
        _patch_httpx(monkeypatch, {
            "/payments": _FakeResp(200, {
                "data": [{
                    "id": "pay-003",
                    "value": 49.0,
                    "dueDate": "2026-07-15",
                    "status": "PENDING",
                    "invoiceUrl": "https://asaas.fake/invoice/pay-003",
                }]
            }),
            "/pixQrCode": _FakeResp(200, {
                "encodedImage": "qrcode-data",
                "payload": "pix-payload-string",
            }),
        })

        client = TestClient(_make_app(db))
        resp = client.get(_API_BASE)

        assert resp.status_code == 200
        assert resp.json()["payment_id"] == "pay-003"


# ---------------------------------------------------------------------------
# 3. Isolamento de tenant (classe de bug recorrente)
# ---------------------------------------------------------------------------

class TestTenantIsolamento:
    """Garante que o endpoint usa o tenant da REQUEST (rls_tenant),
    NAO o tenant de nascimento do usuario (user.tenant_id)."""

    def test_tutor_sem_assinatura_no_tenant_da_request_retorna_404(self, monkeypatch):
        """Tutor tem assinatura em TENANT_ID mas a request e no TENANT_ID_B -> 404."""
        db = _make_db()
        _sub_with_asaas_id(db)  # assinatura em TENANT_ID
        _fake_asaas_config(monkeypatch)

        # App configurado com rls_tenant = TENANT_ID_B (outro tenant)
        client = TestClient(_make_app(db, rls_tenant=TENANT_ID_B))
        resp = client.get(_BASE)

        # Tutor nao tem assinatura em TENANT_ID_B -> 404
        assert resp.status_code == 404

    def test_tutor_com_assinatura_no_tenant_correto_retorna_200(self, monkeypatch):
        """Tutor tem assinatura em TENANT_ID e a request e em TENANT_ID -> 200."""
        db = _make_db()
        _sub_with_asaas_id(db)
        _fake_asaas_config(monkeypatch)
        _patch_httpx(monkeypatch, {
            "/payments": _FakeResp(200, {
                "data": [{
                    "id": "pay-iso-01",
                    "value": 99.0,
                    "dueDate": "2026-07-20",
                    "status": "PENDING",
                    "invoiceUrl": "https://asaas.fake/invoice/pay-iso-01",
                }]
            }),
            "/pixQrCode": _FakeResp(200, {"encodedImage": "img", "payload": "pload"}),
        })

        client = TestClient(_make_app(db, rls_tenant=TENANT_ID))
        resp = client.get(_BASE)

        assert resp.status_code == 200
        assert resp.json()["payment_id"] == "pay-iso-01"
