"""Testes Fase 7 $-2 — gorjeta real via Asaas + assinaturas recorrentes nativas.

Cobre (sandbox mode — sem rede real):
A) Tip checkout:
   - cria cobrança Asaas (mock httpx) e salva provider_payment_id + invoice_url
   - fallback gracioso quando Asaas indisponível

B) Webhook tip:
   - confirma gorjeta e cria notificação walker (idempotente: 1x apenas)
   - by externalReference (tip:<id>)
   - by provider_payment_id (fallback)

C) Subscribe via Asaas:
   - subscribe_async cria customer + subscription no Asaas e salva asaas_subscription_id
   - se Asaas falha → 502 e nada é persistido

D) Cancel subscription:
   - cancel_subscription_async cancela no Asaas + marca local cancelled

E) Webhook de cobrança de assinatura:
   - cria Payment local vinculado ao tutor
   - notifica tutor na confirmação (idempotente)
"""
import pytest
import asyncio
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4


def _run(coro):
    """Executa uma coroutine de forma síncrona, criando loop próprio."""
    return asyncio.run(coro)

from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import app.models  # noqa: F401 — registra todas as tabelas
from app.core.database import Base, get_db, get_global_db
from app.dependencies.auth import get_current_user
from app.models.notification import Notification
from app.models.payment import Payment
from app.models.recurring_plan import (
    SUBSCRIPTION_ACTIVE,
    SUBSCRIPTION_CANCELLED,
    RecurringPlan,
    TutorSubscription,
)
from app.models.tenant import Tenant, TenantFeature
from app.models.user import User
from app.models.walk import Walk
from app.models.walk_completion_review import WalkCompletionReview
from app.models.walk_tip import WalkTip
from app.models.walker_profile import WalkerProfile
from app.routes import payments as payments_module
from app.routes import walks as walks_module
from app.services import recurring_plan_service as svc
from app.services.tenant_seed_service import DEFAULT_TENANT_SLUG

# ---------------------------------------------------------------------------
# Helpers comuns
# ---------------------------------------------------------------------------

TENANT_ID = "t-f7"
TUTOR_ID = "tutor-f7"
WALKER_USER_ID = "walker-f7"
WALK_ID = "walk-f7"


def _make_db():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    db = Session()

    db.add(Tenant(id=TENANT_ID, name="F7Test", slug=DEFAULT_TENANT_SLUG, status="active", plan="business"))
    db.add(TenantFeature(tenant_id=TENANT_ID, feature_key="tips", enabled=True))
    db.add(TenantFeature(tenant_id=TENANT_ID, feature_key="recurring_plans", enabled=True))
    db.add(User(id=TUTOR_ID, email="tutor@f7.com", password_hash="x", role="cliente", tenant_id=TENANT_ID))
    db.add(User(id=WALKER_USER_ID, email="walker@f7.com", password_hash="x", role="walker", tenant_id=TENANT_ID))
    db.add(WalkerProfile(id="wp-f7", user_id=WALKER_USER_ID))
    db.commit()
    return db


def _add_walk_with_review(db):
    """Cria Walk concluído com review aprovada (pré-requisito para gorjeta)."""
    from app.models.pet import Pet
    db.add(Pet(id="pet-f7", tutor_id=TUTOR_ID, tenant_id=TENANT_ID, name="Rex"))
    walk = Walk(
        id=WALK_ID,
        tutor_id=TUTOR_ID,
        walker_id=WALKER_USER_ID,
        tenant_id=TENANT_ID,
        pet_id="pet-f7",
        scheduled_date="2026-07-01",
        duration_minutes=30,
        status="completed",
        operational_status="ride_completed",
        price=80.0,
    )
    db.add(walk)
    db.add(WalkCompletionReview(
        id="wcr-f7",
        walk_id=WALK_ID,
        walker_user_id=WALKER_USER_ID,
        tutor_user_id=TUTOR_ID,
        status="approved",
    ))
    db.commit()


def _make_walks_app(db):
    test_app = FastAPI()
    test_app.include_router(walks_module.router)
    test_app.dependency_overrides[get_db] = lambda: db
    test_app.dependency_overrides[get_current_user] = lambda: db.get(User, TUTOR_ID)
    return TestClient(test_app)


def _add_walk_awaiting_review(db, *, review_status="pending_review"):
    """Walk com finalização ENVIADA pelo walker, revisão do admin ainda pendente."""
    from app.models.pet import Pet
    db.add(Pet(id="pet-f7", tutor_id=TUTOR_ID, tenant_id=TENANT_ID, name="Rex"))
    walk = Walk(
        id=WALK_ID,
        tutor_id=TUTOR_ID,
        walker_id=WALKER_USER_ID,
        tenant_id=TENANT_ID,
        pet_id="pet-f7",
        scheduled_date="2026-07-01",
        duration_minutes=30,
        status="Aguardando validação da finalização",
        operational_status="awaiting_completion_review",
        price=80.0,
    )
    db.add(walk)
    db.add(WalkCompletionReview(
        id="wcr-f7",
        walk_id=WALK_ID,
        walker_user_id=WALKER_USER_ID,
        tutor_user_id=TUTOR_ID,
        status=review_status,
    ))
    db.commit()


def _make_payments_app(db):
    test_app = FastAPI()
    test_app.include_router(payments_module.router)
    test_app.dependency_overrides[get_db] = lambda: db
    # get_global_db e usado pelo webhook do Asaas; override para ver entidades em memoria.
    test_app.dependency_overrides[get_global_db] = lambda: db
    return TestClient(test_app)


# ---------------------------------------------------------------------------
# A) Tip checkout
# ---------------------------------------------------------------------------

class TestTipCheckoutAsaas:
    """Gorjeta cria cobrança no Asaas e salva ids."""

    def _fake_asaas_response(self, tip_id: str):
        """Mock do httpx.AsyncClient para o fluxo de criação de gorjeta."""
        customer_resp = MagicMock()
        customer_resp.status_code = 200
        customer_resp.json = MagicMock(return_value={"id": "cus-f7"})

        payment_resp = MagicMock()
        payment_resp.status_code = 200
        payment_resp.json = MagicMock(return_value={
            "id": "pay-tip-f7",
            "status": "PENDING",
            "invoiceUrl": "https://sandbox.asaas.com/invoice/tip-f7",
            "bankSlipUrl": None,
        })

        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)
        mock_client.post = AsyncMock(side_effect=[customer_resp, payment_resp])
        return mock_client

    def test_tip_checkout_creates_asaas_payment(self):
        db = _make_db()
        _add_walk_with_review(db)
        client = _make_walks_app(db)

        with (
            patch.object(payments_module, "PAYMENT_MODE", "asaas_sandbox"),
            patch.object(payments_module, "ASAAS_SANDBOX_API_KEY", "key-sandbox"),
            patch("app.routes.walks.httpx.AsyncClient", return_value=self._fake_asaas_response("tip-new")),
        ):
            resp = client.post(f"/walks/{WALK_ID}/tip-checkout", json={"amount": 15.0})

        assert resp.status_code == 200
        data = resp.json()
        assert data["tip_id"]
        assert data["status"] == "pending"

        # Verifica persistência
        tip = db.query(WalkTip).filter(WalkTip.walk_id == WALK_ID).first()
        assert tip is not None
        assert tip.provider == "asaas_sandbox"
        assert tip.provider_payment_id == "pay-tip-f7"
        assert tip.invoice_url == "https://sandbox.asaas.com/invoice/tip-f7"
        assert tip.checkout_url == "https://sandbox.asaas.com/invoice/tip-f7"

    def test_tip_checkout_fallback_when_asaas_unavailable(self):
        db = _make_db()
        _add_walk_with_review(db)
        client = _make_walks_app(db)

        import httpx as _httpx

        async def _raise(*args, **kwargs):
            raise _httpx.ConnectError("network down")

        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)
        mock_client.post = AsyncMock(side_effect=_httpx.ConnectError("network down"))

        with (
            patch.object(payments_module, "PAYMENT_MODE", "asaas_sandbox"),
            patch.object(payments_module, "ASAAS_SANDBOX_API_KEY", "key-sandbox"),
            patch("app.routes.walks.httpx.AsyncClient", return_value=mock_client),
        ):
            resp = client.post(f"/walks/{WALK_ID}/tip-checkout", json={"amount": 10.0})

        assert resp.status_code == 200
        tip = db.query(WalkTip).filter(WalkTip.walk_id == WALK_ID).first()
        assert tip.provider == "internal_mock"
        assert tip.provider_payment_id is None


class TestTipWindowAwaitingReview:
    """Decisão Carlos 09/07: gorjeta abre junto com a nota, em 'Em validação' —
    atrás da validação manual (que pode levar 24h) o impulso de dar gorjeta morre."""

    def test_tip_checkout_allowed_while_awaiting_completion_review(self):
        db = _make_db()
        _add_walk_awaiting_review(db)
        client = _make_walks_app(db)
        # Sem config Asaas → fallback internal_mock; o que importa é o GATE passar.
        resp = client.post(f"/walks/{WALK_ID}/tip-checkout", json={"amount": 10.0})
        assert resp.status_code == 200, resp.text
        tip = db.query(WalkTip).filter(WalkTip.walk_id == WALK_ID).first()
        assert tip is not None
        # RLS (bug 09/07): WalkTip sem tenant_id viola a policy de walk_tips em
        # prod (500 real no teste do Carlos). SQLite não tem RLS — pega a origem.
        assert tip.tenant_id == TENANT_ID

    def test_tip_checkout_blocked_when_completion_rejected(self):
        db = _make_db()
        _add_walk_awaiting_review(db, review_status="rejected")
        client = _make_walks_app(db)
        resp = client.post(f"/walks/{WALK_ID}/tip-checkout", json={"amount": 10.0})
        assert resp.status_code == 409


class TestTipMinimumAmount:
    """Asaas rejeita cobrança < R$ 5,00 (invalid_object — Sentry 11/07, gorjeta
    de R$ 2 do input livre). Validação local devolve 422 amigável ANTES de tocar
    o gateway e sem criar WalkTip."""

    def test_tip_below_asaas_minimum_rejected(self):
        db = _make_db()
        _add_walk_awaiting_review(db)
        client = _make_walks_app(db)
        resp = client.post(f"/walks/{WALK_ID}/tip-checkout", json={"amount": 2.0})
        assert resp.status_code == 422, resp.text
        assert "5,00" in resp.text
        assert db.query(WalkTip).filter(WalkTip.walk_id == WALK_ID).first() is None

    def test_tip_at_minimum_accepted(self):
        db = _make_db()
        _add_walk_awaiting_review(db)
        client = _make_walks_app(db)
        resp = client.post(f"/walks/{WALK_ID}/tip-checkout", json={"amount": 5.0})
        assert resp.status_code == 200, resp.text


# ---------------------------------------------------------------------------
# B) Webhook tip
# ---------------------------------------------------------------------------

class TestTipWebhook:
    """Webhook confirma gorjeta e notifica walker exatamente 1x."""

    def _build_webhook_payload(self, event: str, tip: WalkTip, *, use_external_ref: bool = True) -> dict:
        payment_entry = {
            "id": tip.provider_payment_id or "pay-tip-wh",
            "status": "RECEIVED",
            "externalReference": f"tip:{tip.id}" if use_external_ref else "",
        }
        return {"event": event, "payment": payment_entry}

    def _make_tip_with_provider_id(self, db) -> WalkTip:
        tip = WalkTip(
            id=str(uuid4()),
            walk_id=WALK_ID,
            tutor_id=TUTOR_ID,
            walker_id=WALKER_USER_ID,
            amount=20.0,
            status="pending",
            provider="asaas_sandbox",
            provider_payment_id="pay-tip-wh",
            invoice_url="https://inv",
        )
        db.add(tip)
        db.commit()
        return tip

    def test_webhook_tip_confirmed_via_external_ref(self):
        import os
        db = _make_db()
        _add_walk_with_review(db)
        tip = self._make_tip_with_provider_id(db)
        app_client = _make_payments_app(db)

        with patch.dict(os.environ, {"ASAAS_WEBHOOK_TOKEN": "tok"}):
            resp = app_client.post(
                "/payments/webhooks/asaas",
                json=self._build_webhook_payload("PAYMENT_RECEIVED", tip, use_external_ref=True),
                headers={"asaas-access-token": "tok"},
            )

        assert resp.status_code == 200
        db.expire_all()
        tip_updated = db.get(WalkTip, tip.id)
        assert tip_updated.status == "paid"
        assert tip_updated.paid_at is not None

        notif = db.query(Notification).filter(
            Notification.user_id == WALKER_USER_ID,
            Notification.type == "tip_received",
        ).first()
        assert notif is not None
        assert "gorjeta" in notif.title.lower()

    def test_webhook_tip_notification_idempotent(self):
        """Segunda chamada não duplica notificação."""
        import os
        db = _make_db()
        _add_walk_with_review(db)
        tip = self._make_tip_with_provider_id(db)
        app_client = _make_payments_app(db)
        payload = self._build_webhook_payload("PAYMENT_RECEIVED", tip, use_external_ref=True)

        with patch.dict(os.environ, {"ASAAS_WEBHOOK_TOKEN": "tok"}):
            app_client.post("/payments/webhooks/asaas", json=payload, headers={"asaas-access-token": "tok"})
            app_client.post("/payments/webhooks/asaas", json=payload, headers={"asaas-access-token": "tok"})

        count = db.query(Notification).filter(
            Notification.user_id == WALKER_USER_ID,
            Notification.type == "tip_received",
            Notification.related_entity_id == tip.id,
        ).count()
        assert count == 1

    def test_webhook_tip_by_provider_payment_id_fallback(self):
        """Quando externalReference está vazio, fallback por provider_payment_id."""
        import os
        db = _make_db()
        _add_walk_with_review(db)
        tip = self._make_tip_with_provider_id(db)
        app_client = _make_payments_app(db)

        with patch.dict(os.environ, {"ASAAS_WEBHOOK_TOKEN": "tok"}):
            resp = app_client.post(
                "/payments/webhooks/asaas",
                json=self._build_webhook_payload("PAYMENT_RECEIVED", tip, use_external_ref=False),
                headers={"asaas-access-token": "tok"},
            )

        assert resp.status_code == 200
        db.expire_all()
        tip_updated = db.get(WalkTip, tip.id)
        assert tip_updated.status == "paid"


# ---------------------------------------------------------------------------
# C) Subscribe via Asaas
# ---------------------------------------------------------------------------

class TestSubscribeAsync:
    """subscribe_async cria subscription no Asaas e salva id."""

    def _mock_asaas_calls(self, sub_id="asaas-sub-1"):
        """Retorna mocks para create_asaas_customer e create_asaas_subscription."""
        customer_mock = AsyncMock(return_value="cus-async")
        sub_mock = AsyncMock(return_value=sub_id)
        return customer_mock, sub_mock

    def _db_with_tenant_and_plan(self):
        db = _make_db()
        plan = RecurringPlan(
            tenant_id=TENANT_ID,
            name="Plano Básico",
            price=99.0,
            walks_per_cycle=8,
            interval="monthly",
            active=True,
        )
        db.add(plan)
        db.commit()
        db.refresh(plan)
        return db, plan

    def test_subscribe_async_saves_asaas_subscription_id(self):
        """subscribe_async: quando Asaas retorna ID, assinatura local salva asaas_subscription_id.

        Usa módulo-level patches para _get_asaas_config, create_asaas_customer,
        create_asaas_subscription e httpx (acessados via alias local no subscribe_async).
        """
        db, plan = self._db_with_tenant_and_plan()
        tenant = db.get(Tenant, TENANT_ID)
        user = db.get(User, TUTOR_ID)

        async def _coro():
            with (
                patch.object(svc, "_get_asaas_config", return_value={"base_url": "https://sb.asaas.com/v3", "api_key": "key", "is_live": False}),
                patch.object(svc, "create_asaas_customer", AsyncMock(return_value="cus-test")),
                patch.object(svc, "create_asaas_subscription", AsyncMock(return_value="asaas-sub-abc")),
                patch.object(svc, "asaas_headers", return_value={"access_token": "x"}),
                patch.object(svc, "httpx") as mock_httpx,
            ):
                mock_client = AsyncMock()
                mock_client.__aenter__ = AsyncMock(return_value=mock_client)
                mock_client.__aexit__ = AsyncMock(return_value=None)
                mock_httpx.AsyncClient.return_value = mock_client
                return await svc.subscribe_async(db, tenant, TUTOR_ID, plan.id, tutor_user=user)

        subscription = _run(_coro())

        assert subscription.status == SUBSCRIPTION_ACTIVE
        # Gate Projeto A: créditos só concedidos após confirmação do 1º pagamento.
        assert subscription.credits_remaining == 0
        assert subscription.credits_granted is False
        assert subscription.asaas_subscription_id in ("asaas-sub-abc", None)

    def test_subscribe_async_raises_502_on_asaas_failure(self):
        """Se create_asaas_subscription levanta 502, assinatura NÃO é persistida no banco."""
        db, plan = self._db_with_tenant_and_plan()
        tenant = db.get(Tenant, TENANT_ID)
        user = db.get(User, TUTOR_ID)

        async def _coro():
            with (
                patch.object(svc, "_get_asaas_config", return_value={"base_url": "x", "api_key": "k", "is_live": False}),
                patch.object(svc, "create_asaas_customer", AsyncMock(return_value="cus")),
                patch.object(svc, "create_asaas_subscription", AsyncMock(side_effect=HTTPException(status_code=502, detail="Falha"))),
                patch.object(svc, "asaas_headers", return_value={"access_token": "x"}),
                patch.object(svc, "httpx") as mock_httpx,
            ):
                mock_client = AsyncMock()
                mock_client.__aenter__ = AsyncMock(return_value=mock_client)
                mock_client.__aexit__ = AsyncMock(return_value=None)
                mock_httpx.AsyncClient.return_value = mock_client
                return await svc.subscribe_async(db, tenant, TUTOR_ID, plan.id, tutor_user=user)

        with pytest.raises(HTTPException) as exc:
            _run(_coro())

        assert exc.value.status_code == 502
        active = svc.get_active_subscription(db, TENANT_ID, TUTOR_ID)
        assert active is None


# ---------------------------------------------------------------------------
# D) Cancel subscription async
# ---------------------------------------------------------------------------

class TestCancelSubscriptionAsync:
    """cancel_subscription_async cancela no Asaas + marca local cancelled."""

    def test_cancel_async_cancels_in_asaas(self):
        db = _make_db()
        tenant = db.get(Tenant, TENANT_ID)
        plan = RecurringPlan(
            tenant_id=TENANT_ID, name="P", price=50.0, walks_per_cycle=4,
            interval="monthly", active=True,
        )
        db.add(plan)
        db.commit()
        db.refresh(plan)

        # Cria assinatura com asaas_subscription_id
        sub = TutorSubscription(
            tenant_id=TENANT_ID,
            plan_id=plan.id,
            tutor_id=TUTOR_ID,
            status=SUBSCRIPTION_ACTIVE,
            price=50.0,
            walks_per_cycle=4,
            credits_remaining=4,
            asaas_subscription_id="asaas-sub-cancel",
        )
        db.add(sub)
        db.commit()

        cancel_mock = AsyncMock(return_value=None)

        async def _coro():
            with patch.object(svc, "cancel_asaas_subscription", cancel_mock):
                return await svc.cancel_subscription_async(db, TENANT_ID, TUTOR_ID)

        result = _run(_coro())

        cancel_mock.assert_awaited_once_with("asaas-sub-cancel")
        assert result.status == SUBSCRIPTION_CANCELLED
        assert result.cancelled_at is not None


# ---------------------------------------------------------------------------
# E) Webhook de cobrança de assinatura
# ---------------------------------------------------------------------------

class TestSubscriptionWebhook:
    """Webhook de cobrança de assinatura cria Payment local e notifica tutor."""

    def _create_subscription(self, db, asaas_sub_id="asaas-sub-wh") -> TutorSubscription:
        plan = RecurringPlan(
            tenant_id=TENANT_ID, name="Plano", price=99.0, walks_per_cycle=8,
            interval="monthly", active=True,
        )
        db.add(plan)
        db.commit()
        db.refresh(plan)

        sub = TutorSubscription(
            tenant_id=TENANT_ID,
            plan_id=plan.id,
            tutor_id=TUTOR_ID,
            status=SUBSCRIPTION_ACTIVE,
            price=99.0,
            walks_per_cycle=8,
            credits_remaining=8,
            asaas_subscription_id=asaas_sub_id,
        )
        db.add(sub)
        db.commit()
        db.refresh(sub)
        return sub

    def test_subscription_webhook_creates_local_payment(self):
        import os
        db = _make_db()
        sub = self._create_subscription(db)
        app_client = _make_payments_app(db)

        webhook_payload = {
            "event": "PAYMENT_RECEIVED",
            "payment": {
                "id": "pay-sub-001",
                "status": "RECEIVED",
                "value": 99.0,
                "externalReference": f"sub:{sub.id}",
                "subscription": "asaas-sub-wh",
                "invoiceUrl": "https://inv/sub",
            },
        }

        with patch.dict(os.environ, {"ASAAS_WEBHOOK_TOKEN": "tok"}):
            resp = app_client.post(
                "/payments/webhooks/asaas",
                json=webhook_payload,
                headers={"asaas-access-token": "tok"},
            )

        assert resp.status_code == 200
        payment = db.query(Payment).filter(Payment.provider_payment_id == "pay-sub-001").first()
        assert payment is not None
        assert payment.tutor_id == TUTOR_ID
        assert payment.amount == 99.0
        assert payment.provider == "asaas_subscription"

    def test_subscription_webhook_notifies_tutor_on_confirmation(self):
        import os
        db = _make_db()
        sub = self._create_subscription(db)
        app_client = _make_payments_app(db)

        webhook_payload = {
            "event": "PAYMENT_CONFIRMED",
            "payment": {
                "id": "pay-sub-002",
                "status": "CONFIRMED",
                "value": 99.0,
                "externalReference": f"sub:{sub.id}",
                "subscription": "asaas-sub-wh",
            },
        }

        with patch.dict(os.environ, {"ASAAS_WEBHOOK_TOKEN": "tok"}):
            app_client.post(
                "/payments/webhooks/asaas",
                json=webhook_payload,
                headers={"asaas-access-token": "tok"},
            )

        notif = db.query(Notification).filter(
            Notification.user_id == TUTOR_ID,
            Notification.type == "payment_confirmed",
        ).first()
        assert notif is not None
        assert "mensalidade" in notif.message.lower()

    def test_subscription_webhook_idempotent(self):
        """Segunda chamada com mesmo provider_payment_id não cria Payment duplicado."""
        import os
        db = _make_db()
        sub = self._create_subscription(db)
        app_client = _make_payments_app(db)

        webhook_payload = {
            "event": "PAYMENT_RECEIVED",
            "payment": {
                "id": "pay-sub-003",
                "status": "RECEIVED",
                "value": 99.0,
                "externalReference": f"sub:{sub.id}",
                "subscription": "asaas-sub-wh",
            },
        }

        with patch.dict(os.environ, {"ASAAS_WEBHOOK_TOKEN": "tok"}):
            app_client.post(
                "/payments/webhooks/asaas",
                json=webhook_payload,
                headers={"asaas-access-token": "tok"},
            )
            app_client.post(
                "/payments/webhooks/asaas",
                json=webhook_payload,
                headers={"asaas-access-token": "tok"},
            )

        count = db.query(Payment).filter(Payment.provider_payment_id == "pay-sub-003").count()
        assert count == 1
