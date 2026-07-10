"""Mig 0107 — admin cancel pelo motor unico + fila de compensacao do walker.

Cobre:
- PATCH /admin/walks/{id}/status com ride_cancelled -> passa pelo MESMO motor
  (process_tutor_cancellation), notifica tutor E walker, sem duplicacao.
- POST /admin/walk-completions/{id}/approve com kind=cancellation_compensation
  -> cria WalkerEarning, NAO mexe no walk (ja cancelado), SEM commission_entry.
- POST /admin/walk-completions/{id}/reject com kind=cancellation_compensation
  -> so marca a review, NAO sobrescreve o estado do walk cancelado.
"""
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, patch
from uuid import uuid4
from zoneinfo import ZoneInfo

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import app.models  # noqa: F401
from app.core.database import Base, get_db
from app.dependencies.auth import get_current_user
from app.models.notification import Notification
from app.models.payment import Payment
from app.models.tenant import Tenant
from app.models.user import User
from app.models.walk import Walk
from app.models.walk_completion_review import WalkCompletionReview
from app.models.walker_earning import WalkerEarning
from app.routes import admin
from app.services.tenant_seed_service import DEFAULT_TENANT_SLUG

ADMIN_ID = "admin-1"
TENANT_ID = "t-test"
TUTOR_ID = "tutor-1"
WALKER_ID = "walker-1"
PET_ID = "pet-1"


def _local_now() -> datetime:
    return datetime.now(ZoneInfo("America/Bahia")).replace(tzinfo=None)


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M")


def build():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine)()
    db.add(Tenant(id=TENANT_ID, name="Aumigao", slug=DEFAULT_TENANT_SLUG, status="active", plan="business"))
    db.add(User(id=ADMIN_ID, email="admin@test.com", password_hash="x", role="super_admin", full_name="Admin"))
    db.add(User(id=TUTOR_ID, email="tutor@test.com", password_hash="x", role="cliente", tenant_id=TENANT_ID, full_name="Tutor"))
    db.add(User(id=WALKER_ID, email="walker@test.com", password_hash="x", role="walker", tenant_id=TENANT_ID, full_name="Walker"))
    db.commit()
    test_app = FastAPI()
    test_app.include_router(admin.router)
    test_app.dependency_overrides[get_db] = lambda: db
    test_app.dependency_overrides[get_current_user] = lambda: db.get(User, ADMIN_ID)
    return TestClient(test_app), db


def _walk(db, *, scheduled_date, price=100.0, walker_id=WALKER_ID, op_status="ride_scheduled"):
    walk = Walk(
        id=str(uuid4()), tutor_id=TUTOR_ID, tenant_id=TENANT_ID, walker_id=walker_id,
        assigned_walker_id=walker_id, pet_id=PET_ID, scheduled_date=scheduled_date,
        duration_minutes=45, price=price, status="Agendado", operational_status=op_status,
        walker_selection_mode="auto",
    )
    db.add(walk)
    db.commit()
    db.refresh(walk)
    return walk


def _payment(db, walk, *, amount=100.0):
    payment = Payment(
        id=str(uuid4()), tenant_id=TENANT_ID, tutor_id=TUTOR_ID, walk_id=walk.id,
        amount=amount, status="pagamento_confirmado_sandbox", provider="asaas_sandbox",
        provider_payment_id="prov-1",
    )
    db.add(payment)
    db.commit()
    return payment


def _fake_refund(ok=True):
    async def _f(provider, provider_payment_id, value=None):
        return ok
    return AsyncMock(side_effect=_f)


def test_admin_cancel_via_status_patch_uses_motor_and_notifies_both_sides():
    client, db = build()
    scheduled = _local_now() + timedelta(hours=1)  # dentro da janela -> refund parcial
    walk = _walk(db, scheduled_date=_iso(scheduled), price=100.0)
    _payment(db, walk, amount=100.0)

    fake_refund = _fake_refund(ok=True)
    with patch("app.routes.payments.refund_asaas_charge", fake_refund):
        r = client.patch(f"/admin/walks/{walk.id}/status", json={"status": "ride_cancelled"})
    assert r.status_code == 200, r.text

    fake_refund.assert_awaited_once_with("asaas_sandbox", "prov-1", value=50.0)
    db.expire_all()
    updated = db.get(Walk, walk.id)
    assert updated.operational_status == "ride_cancelled"
    assert updated.cancelled_by_role == "admin"

    # Compensacao pendente criada (walker designado, dentro da janela).
    review = db.query(WalkCompletionReview).filter(WalkCompletionReview.walk_id == walk.id).first()
    assert review is not None
    assert review.kind == "cancellation_compensation"
    assert review.compensation_amount == 50.0

    # Notifica TUTOR e WALKER (admin cancel — antes so notificava o tutor).
    tutor_notif = db.query(Notification).filter(Notification.user_id == TUTOR_ID, Notification.type == "walk_status").first()
    walker_notif = db.query(Notification).filter(Notification.user_id == WALKER_ID, Notification.type == "walk_status").first()
    assert tutor_notif is not None
    assert walker_notif is not None


def test_admin_cancel_already_cancelled_is_noop_guard():
    client, db = build()
    walk = _walk(db, scheduled_date=_iso(_local_now() + timedelta(hours=48)), op_status="ride_cancelled")
    r = client.patch(f"/admin/walks/{walk.id}/status", json={"status": "ride_cancelled"})
    assert r.status_code == 200, r.text


def _seed_compensation_review(db, *, amount=50.0, walk_op_status="ride_cancelled"):
    walk = _walk(db, scheduled_date=_iso(_local_now() + timedelta(hours=1)), op_status=walk_op_status)
    review = WalkCompletionReview(
        id=str(uuid4()), tenant_id=TENANT_ID, walk_id=walk.id, walker_user_id=WALKER_ID,
        tutor_user_id=TUTOR_ID, status="pending_review", kind="cancellation_compensation",
        compensation_amount=amount, notes="Compensação por cancelamento tardio.",
    )
    db.add(review)
    db.commit()
    db.refresh(walk)
    db.refresh(review)
    return walk, review


def test_approve_cancellation_compensation_creates_walker_earning_without_touching_walk():
    client, db = build()
    walk, review = _seed_compensation_review(db, amount=50.0)

    r = client.post(f"/admin/walk-completions/{review.id}/approve", json={})
    assert r.status_code == 200, r.text

    db.expire_all()
    updated_walk = db.get(Walk, walk.id)
    # Walk continua cancelado — NÃO virou "Finalizado".
    assert updated_walk.operational_status == "ride_cancelled"
    assert updated_walk.status != "Finalizado"

    earning = db.query(WalkerEarning).filter(WalkerEarning.walk_id == walk.id).first()
    assert earning is not None
    assert earning.walker_id == WALKER_ID
    assert earning.amount == 50.0
    assert earning.platform_amount == 0.0

    updated_review = db.get(WalkCompletionReview, review.id)
    assert updated_review.status == "approved"

    # Nenhum Payment "internal" de comissão foi criado (sem commission_entry).
    payments = db.query(Payment).filter(Payment.walk_id == walk.id).all()
    assert all(p.provider != "internal" for p in payments)


def test_approve_cancellation_compensation_idempotent_on_double_approve():
    client, db = build()
    walk, review = _seed_compensation_review(db, amount=50.0)
    r1 = client.post(f"/admin/walk-completions/{review.id}/approve", json={})
    assert r1.status_code == 200
    # Segunda aprovação é bloqueada pela máquina de estados (já aprovada).
    r2 = client.post(f"/admin/walk-completions/{review.id}/approve", json={})
    assert r2.status_code == 409
    db.expire_all()
    earnings = db.query(WalkerEarning).filter(WalkerEarning.walk_id == walk.id).all()
    assert len(earnings) == 1


def test_reject_cancellation_compensation_does_not_overwrite_walk_state():
    client, db = build()
    walk, review = _seed_compensation_review(db, amount=50.0)

    r = client.post(f"/admin/walk-completions/{review.id}/reject", json={"reason": "duplicado"})
    assert r.status_code == 200, r.text

    db.expire_all()
    updated_walk = db.get(Walk, walk.id)
    # NÃO deve virar "completion_rejected" / "Finalização rejeitada" — o walk é
    # um CANCELAMENTO, não uma finalização.
    assert updated_walk.operational_status == "ride_cancelled"
    assert updated_walk.status != "Finalização rejeitada"

    updated_review = db.get(WalkCompletionReview, review.id)
    assert updated_review.status == "rejected"
    assert db.query(WalkerEarning).filter(WalkerEarning.walk_id == walk.id).count() == 0
