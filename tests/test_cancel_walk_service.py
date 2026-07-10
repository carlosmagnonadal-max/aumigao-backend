"""Motor único de cancelamento (mig 0107) — testes de UNIT do service, sem HTTP.

Cobre a matriz da spec (docs/superpowers/specs/2026-07-10-cancelamento-financeiro-design.md):
janela local de fuso, refund total, refund parcial (valor exato), compensação
pendente do walker, assinatura/crédito nos 2 lados da janela, walker
notificado + push whitelist, walk sem pagamento pago, motivo persistido,
tenant_id explícito nas linhas criadas (RLS).
"""
import asyncio
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, patch
from uuid import uuid4
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import app.models  # noqa: F401 - registra todas as tabelas no Base.metadata
from app.core.database import Base
from app.models.notification import Notification
from app.models.payment import Payment
from app.models.pet import Pet
from app.models.push_token import PushToken
from app.models.recurring_plan import RecurringPlan, TutorSubscription, SUBSCRIPTION_ACTIVE
from app.models.tenant import Tenant, TenantSettings
from app.models.user import User
from app.models.walk import Walk
from app.models.walk_completion_review import WalkCompletionReview
from app.models.walker_earning import WalkerEarning
from app.services import cancel_walk_service as svc
from app.services.push_notifications import _should_push
from app.services.tenant_seed_service import DEFAULT_TENANT_SLUG

TENANT_ID = "t-test"
TUTOR_ID = "tutor-test"
WALKER_ID = "walker-test"
PET_ID = "pet-test"


def _run(coro):
    return asyncio.run(coro)


def _local_now() -> datetime:
    return datetime.now(ZoneInfo("America/Bahia")).replace(tzinfo=None)


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M")


def _build(tenant_kwargs: dict | None = None):
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine)()
    db.add(Tenant(id=TENANT_ID, name="Aumigao", slug=DEFAULT_TENANT_SLUG, status="active", plan="business"))
    db.add(User(id=TUTOR_ID, email="tutor@test.com", password_hash="x", role="cliente", tenant_id=TENANT_ID))
    db.add(User(id=WALKER_ID, email="walker@test.com", password_hash="x", role="walker", tenant_id=TENANT_ID, is_active=True))
    db.add(Pet(id=PET_ID, tutor_id=TUTOR_ID, name="Rex", tenant_id=TENANT_ID))
    if tenant_kwargs is not None:
        db.add(TenantSettings(id=str(uuid4()), tenant_id=TENANT_ID, **tenant_kwargs))
    db.commit()
    return db


def _walk(db, *, scheduled_date, price=100.0, walker_id=WALKER_ID, subscription_id=None, status="Agendado", operational_status="ride_scheduled"):
    walk = Walk(
        id=str(uuid4()),
        tutor_id=TUTOR_ID,
        tenant_id=TENANT_ID,
        walker_id=walker_id,
        assigned_walker_id=walker_id,
        pet_id=PET_ID,
        scheduled_date=scheduled_date,
        duration_minutes=45,
        price=price,
        status=status,
        operational_status=operational_status,
        walker_selection_mode="auto",
        subscription_id=subscription_id,
    )
    db.add(walk)
    db.commit()
    db.refresh(walk)
    return walk


def _payment(db, walk, *, amount=100.0, provider="asaas_sandbox", provider_payment_id="prov-1", status="pagamento_confirmado_sandbox"):
    payment = Payment(
        id=str(uuid4()), tenant_id=TENANT_ID, tutor_id=TUTOR_ID, walk_id=walk.id,
        amount=amount, status=status, provider=provider, provider_payment_id=provider_payment_id,
    )
    db.add(payment)
    db.commit()
    db.refresh(payment)
    return payment


def _fake_refund(ok=True):
    async def _f(provider, provider_payment_id, value=None):
        return ok
    return AsyncMock(side_effect=_f)


# ─────────────────────────── janela local de fuso ─────────────────────────

def test_window_uses_tenant_local_wall_time_not_naive_utc():
    """Passeio às 10h America/Bahia (UTC-3) cancelado 20h antes é FORA da janela de
    24h em horário local — se comparasse com utcnow() puro (bug 08/07) o deslocamento
    de -3h faria parecer 'dentro' da janela incorretamente perto da borda."""
    db = _build()
    scheduled = _local_now() + timedelta(hours=20)
    walk = _walk(db, scheduled_date=_iso(scheduled), walker_id=None)
    _payment(db, walk)

    with patch.object(svc, "get_tenant_cancellation_config", wraps=svc.get_tenant_cancellation_config) as _cfg:
        config = svc.get_tenant_cancellation_config(db, TENANT_ID)
    now = datetime.utcnow()
    is_late = svc._is_late_cancellation(db, walk, config.free_window_minutes, now)
    # 20h de antecedência < janela padrão de 24h → é tardio.
    assert is_late is True

    scheduled_far = _local_now() + timedelta(hours=30)
    walk_far = _walk(db, scheduled_date=_iso(scheduled_far), walker_id=None)
    is_late_far = svc._is_late_cancellation(db, walk_far, config.free_window_minutes, datetime.utcnow())
    assert is_late_far is False


# ─────────────────────────────── refund total ──────────────────────────────

def test_full_refund_outside_window_no_walker_no_compensation():
    db = _build()
    scheduled = _local_now() + timedelta(hours=48)  # bem fora da janela de 24h
    walk = _walk(db, scheduled_date=_iso(scheduled), price=100.0, walker_id=WALKER_ID)
    payment = _payment(db, walk, amount=100.0)

    fake_refund = _fake_refund(ok=True)
    with patch("app.routes.payments.refund_asaas_charge", fake_refund):
        summary = _run(svc.process_tutor_cancellation(db, walk, actor_role="tutor", actor_id=TUTOR_ID))
    db.commit()

    fake_refund.assert_awaited_once_with(payment.provider, payment.provider_payment_id)
    assert summary["refund_kind"] == "total"
    assert summary["refund_status"] == "pending"
    assert summary["refunded_amount"] == 100.0
    assert summary["compensation_amount"] == 0.0
    assert summary["walker_compensated"] is False

    db.refresh(payment)
    db.refresh(walk)
    assert payment.refund_status == "pending"
    assert payment.refunded_amount == 100.0
    assert walk.operational_status == "ride_cancelled"
    assert walk.status == "Cancelado"
    # Fora da janela: sem compensação pendente criada para o walker.
    assert db.query(WalkCompletionReview).filter(WalkCompletionReview.walk_id == walk.id).count() == 0


# ────────────────────────── refund parcial (valor exato) ──────────────────

def test_partial_refund_inside_window_exact_value_and_compensation_created():
    db = _build(tenant_kwargs=dict(
        cancellation_free_window_minutes=1440,
        late_cancellation_fee_percent=50,
        late_fee_walker_share_percent=100,
        auto_refund_on_cancel=True,
    ))
    scheduled = _local_now() + timedelta(hours=3)  # dentro da janela de 24h
    walk = _walk(db, scheduled_date=_iso(scheduled), price=100.0, walker_id=WALKER_ID)
    payment = _payment(db, walk, amount=100.0)

    fake_refund = _fake_refund(ok=True)
    with patch("app.routes.payments.refund_asaas_charge", fake_refund):
        summary = _run(svc.process_tutor_cancellation(db, walk, actor_role="tutor", actor_id=TUTOR_ID))
    db.commit()

    # 50% de taxa sobre R$100 = R$50 retido; refund parcial = R$50.
    fake_refund.assert_awaited_once_with(payment.provider, payment.provider_payment_id, value=50.0)
    assert summary["refund_kind"] == "partial"
    assert summary["retained_amount"] == 50.0
    assert summary["refunded_amount"] == 50.0

    # Compensação = retido(50) x walker_share(100%) = 50 — pendente na fila de finalizações.
    review = db.query(WalkCompletionReview).filter(WalkCompletionReview.walk_id == walk.id).first()
    assert review is not None
    assert review.kind == "cancellation_compensation"
    assert review.status == "pending_review"
    assert review.compensation_amount == 50.0
    assert review.walker_user_id == WALKER_ID
    assert review.tenant_id == TENANT_ID  # RLS: tenant_id explícito


def test_partial_refund_respects_custom_fee_and_walker_share():
    db = _build(tenant_kwargs=dict(
        cancellation_free_window_minutes=1440,
        late_cancellation_fee_percent=30,
        late_fee_walker_share_percent=50,
        auto_refund_on_cancel=True,
    ))
    scheduled = _local_now() + timedelta(hours=1)
    walk = _walk(db, scheduled_date=_iso(scheduled), price=200.0, walker_id=WALKER_ID)
    payment = _payment(db, walk, amount=200.0)

    fake_refund = _fake_refund(ok=True)
    with patch("app.routes.payments.refund_asaas_charge", fake_refund):
        summary = _run(svc.process_tutor_cancellation(db, walk, actor_role="tutor", actor_id=TUTOR_ID))
    db.commit()

    # taxa 30% sobre 200 = 60 retido; refund = 140.
    assert summary["retained_amount"] == 60.0
    assert summary["refunded_amount"] == 140.0
    # compensação = 60 x 50% = 30.
    assert summary["compensation_amount"] == 30.0
    review = db.query(WalkCompletionReview).filter(WalkCompletionReview.walk_id == walk.id).first()
    assert review.compensation_amount == 30.0


def test_refund_failure_does_not_block_cancellation():
    db = _build()
    scheduled = _local_now() + timedelta(hours=1)
    walk = _walk(db, scheduled_date=_iso(scheduled), price=100.0, walker_id=None)
    payment = _payment(db, walk, amount=100.0)

    fake_refund = _fake_refund(ok=False)
    with patch("app.routes.payments.refund_asaas_charge", fake_refund):
        summary = _run(svc.process_tutor_cancellation(db, walk, actor_role="tutor", actor_id=TUTOR_ID))
    db.commit()

    assert summary["refund_status"] == "failed"
    db.refresh(walk)
    db.refresh(payment)
    # Walk cancela MESMO com falha no gateway.
    assert walk.operational_status == "ride_cancelled"
    assert payment.refund_status == "failed"
    assert payment.refunded_amount is None


# ───────────────────────── assinatura (crédito) nos 2 lados ────────────────

def _subscription(db):
    plan = RecurringPlan(id=str(uuid4()), tenant_id=TENANT_ID, name="Plano", price=200.0, walks_per_cycle=8, interval="monthly", active=True)
    db.add(plan)
    sub = TutorSubscription(
        id=str(uuid4()), tenant_id=TENANT_ID, tutor_id=TUTOR_ID, plan_id=plan.id,
        status=SUBSCRIPTION_ACTIVE, credits_remaining=3, credits_granted=True,
        current_period_start=datetime.utcnow() - timedelta(days=1),
    )
    db.add(sub)
    db.commit()
    db.refresh(sub)
    return sub


def test_subscription_walk_outside_window_refunds_credit():
    db = _build()
    sub = _subscription(db)
    scheduled = _local_now() + timedelta(hours=48)
    walk = _walk(db, scheduled_date=_iso(scheduled), price=25.0, walker_id=None, subscription_id=sub.id)

    summary = _run(svc.process_tutor_cancellation(db, walk, actor_role="tutor", actor_id=TUTOR_ID))
    db.commit()

    assert summary["refund_kind"] == "credit"
    assert summary["retained_amount"] == 0.0
    db.refresh(sub)
    db.refresh(walk)
    assert sub.credits_remaining == 4  # devolveu 1 crédito
    assert walk.credit_refunded is True


def test_subscription_walk_inside_window_does_not_refund_credit_and_compensates_walker():
    db = _build(tenant_kwargs=dict(
        cancellation_free_window_minutes=1440,
        late_cancellation_fee_percent=50,
        late_fee_walker_share_percent=100,
        auto_refund_on_cancel=True,
    ))
    sub = _subscription(db)
    scheduled = _local_now() + timedelta(hours=2)
    walk = _walk(db, scheduled_date=_iso(scheduled), price=40.0, walker_id=WALKER_ID, subscription_id=sub.id)

    summary = _run(svc.process_tutor_cancellation(db, walk, actor_role="tutor", actor_id=TUTOR_ID))
    db.commit()

    assert summary["refund_kind"] == "credit"
    db.refresh(sub)
    db.refresh(walk)
    # Dentro da janela: crédito NÃO devolvido (é a própria retenção).
    assert sub.credits_remaining == 3
    assert walk.credit_refunded is False
    # Compensação calculada sobre walk.price (40 x 50% = 20).
    assert summary["retained_amount"] == 20.0
    assert summary["compensation_amount"] == 20.0
    review = db.query(WalkCompletionReview).filter(WalkCompletionReview.walk_id == walk.id).first()
    assert review is not None
    assert review.compensation_amount == 20.0


# ───────────────────── walker notificado + push whitelist ─────────────────

def test_walker_notified_and_push_eligible():
    db = _build()
    scheduled = _local_now() + timedelta(hours=1)
    walk = _walk(db, scheduled_date=_iso(scheduled), price=0.0, walker_id=WALKER_ID)
    # sem payment confirmado (grátis) — cobre também o caso "sem pagamento".

    _run(svc.process_tutor_cancellation(db, walk, actor_role="tutor", actor_id=TUTOR_ID))
    db.commit()

    notif = (
        db.query(Notification)
        .filter(Notification.user_id == WALKER_ID, Notification.type == "walk_status")
        .order_by(Notification.created_at.desc())
        .first()
    )
    assert notif is not None
    assert _should_push(notif) is True


# ───────────────────── walk sem pagamento pago (grátis/não-pago) ──────────

def test_walk_without_paid_payment_cancels_without_refund():
    db = _build()
    scheduled = _local_now() + timedelta(hours=1)
    walk = _walk(db, scheduled_date=_iso(scheduled), price=50.0, walker_id=WALKER_ID)
    # Payment existe mas NUNCA foi confirmado (ex.: pending) — não deve tentar estornar.
    db.add(Payment(id=str(uuid4()), tenant_id=TENANT_ID, tutor_id=TUTOR_ID, walk_id=walk.id,
                    amount=50.0, status="pagamento_sandbox_criado", provider="asaas_sandbox",
                    provider_payment_id="prov-unpaid"))
    db.commit()

    fake_refund = _fake_refund(ok=True)
    with patch("app.routes.payments.refund_asaas_charge", fake_refund):
        summary = _run(svc.process_tutor_cancellation(db, walk, actor_role="tutor", actor_id=TUTOR_ID))
    db.commit()

    fake_refund.assert_not_awaited()
    assert summary["refund_kind"] is None
    assert summary["compensation_amount"] == 0.0
    db.refresh(walk)
    assert walk.operational_status == "ride_cancelled"


# ───────────────────────────── motivo persistido ───────────────────────────

def test_cancellation_reason_persisted():
    db = _build()
    scheduled = _local_now() + timedelta(hours=1)
    walk = _walk(db, scheduled_date=_iso(scheduled), walker_id=None)

    _run(svc.process_tutor_cancellation(
        db, walk, actor_role="tutor", actor_id=TUTOR_ID,
        reason_type="mudanca_de_planos", reason_text="Pet ficou doente e a viagem foi cancelada.",
    ))
    db.commit()
    db.refresh(walk)

    assert walk.cancellation_reason_type == "mudanca_de_planos"
    assert walk.cancellation_reason == "Pet ficou doente e a viagem foi cancelada."
    assert walk.cancelled_by_role == "tutor"
    assert walk.cancelled_at is not None


# ──────────────────────────── auto_refund OFF ──────────────────────────────

def test_auto_refund_off_does_not_call_gateway_but_still_cancels():
    db = _build(tenant_kwargs=dict(auto_refund_on_cancel=False))
    scheduled = _local_now() + timedelta(hours=48)
    walk = _walk(db, scheduled_date=_iso(scheduled), price=100.0, walker_id=None)
    payment = _payment(db, walk, amount=100.0)

    fake_refund = _fake_refund(ok=True)
    with patch("app.routes.payments.refund_asaas_charge", fake_refund):
        summary = _run(svc.process_tutor_cancellation(db, walk, actor_role="tutor", actor_id=TUTOR_ID))
    db.commit()

    fake_refund.assert_not_awaited()
    db.refresh(walk)
    db.refresh(payment)
    assert walk.operational_status == "ride_cancelled"
    assert payment.refund_status == "pending"
