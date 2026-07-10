"""Mig 0107 — POST /walks/{id}/cancel (caminho NOVO do app tutor).

Reusa o fixture `build()` de tests/test_routes_walks.py (mesmo padrao do
projeto: FastAPI minimo + SQLite em memoria + overrides get_db/get_current_user).
"""
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, patch
from uuid import uuid4
from zoneinfo import ZoneInfo

from app.models.notification import Notification
from app.models.payment import Payment
from app.models.user import User
from app.models.walk import Walk
from app.models.walk_completion_review import WalkCompletionReview
from tests.test_routes_walks import TENANT_ID, TUTOR_ID, WALKER_ID, PET_ID, build


def _local_now() -> datetime:
    return datetime.now(ZoneInfo("America/Bahia")).replace(tzinfo=None)


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M")


def _walk(db, *, scheduled_date, price=100.0, walker_id=None):
    walk = Walk(
        id=str(uuid4()), tutor_id=TUTOR_ID, tenant_id=TENANT_ID, walker_id=walker_id,
        assigned_walker_id=walker_id, pet_id=PET_ID, scheduled_date=scheduled_date,
        duration_minutes=45, price=price, status="Agendado", operational_status="ride_scheduled",
        walker_selection_mode="auto",
    )
    db.add(walk)
    db.commit()
    db.refresh(walk)
    return walk


def _payment(db, walk, *, amount):
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


def test_cancel_outside_window_persists_reason_and_full_refund():
    client, db = build()
    walk = _walk(db, scheduled_date=_iso(_local_now() + timedelta(hours=48)), price=100.0)
    _payment(db, walk, amount=100.0)

    fake_refund = _fake_refund(ok=True)
    with patch("app.routes.payments.refund_asaas_charge", fake_refund):
        r = client.post(f"/walks/{walk.id}/cancel", json={
            "reason_type": "mudanca_de_planos",
            "reason_text": "Viagem cancelada.",
        })
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["operational_status"] == "ride_cancelled"
    assert body["status"] == "Cancelado"
    assert body["cancellation_reason_type"] == "mudanca_de_planos"
    assert body["cancellation_reason"] == "Viagem cancelada."
    assert body["cancelled_by_role"] == "tutor"
    assert body["refund_status"] == "pending"
    assert body["refunded_amount"] == 100.0
    fake_refund.assert_awaited_once_with("asaas_sandbox", "prov-1")


def test_cancel_inside_window_partial_refund_and_walker_compensation():
    client, db = build()
    walk = _walk(db, scheduled_date=_iso(_local_now() + timedelta(hours=1)), price=100.0, walker_id=WALKER_ID)
    _payment(db, walk, amount=100.0)

    fake_refund = _fake_refund(ok=True)
    with patch("app.routes.payments.refund_asaas_charge", fake_refund):
        r = client.post(f"/walks/{walk.id}/cancel", json={})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["refund_status"] == "pending"
    assert body["refunded_amount"] == 50.0  # taxa default 50% sobre 100
    fake_refund.assert_awaited_once_with("asaas_sandbox", "prov-1", value=50.0)

    review = db.query(WalkCompletionReview).filter(WalkCompletionReview.walk_id == walk.id).first()
    assert review is not None
    assert review.kind == "cancellation_compensation"
    assert review.compensation_amount == 50.0

    walker_notif = (
        db.query(Notification)
        .filter(Notification.user_id == WALKER_ID, Notification.type == "walk_status")
        .first()
    )
    assert walker_notif is not None


def test_cancel_404_for_non_owner_tutor():
    client, db = build()
    other_tutor = User(id="other-tutor", email="other@x.com", password_hash="x", role="cliente", tenant_id=TENANT_ID)
    db.add(other_tutor)
    db.commit()
    walk = _walk(db, scheduled_date=_iso(_local_now() + timedelta(hours=48)))

    # troca o usuario autenticado para um tutor que NAO e dono do walk.
    client.app.dependency_overrides.clear()
    from app.core.database import get_db
    from app.dependencies.auth import get_current_user
    from app.routes import walks

    client.app.dependency_overrides[get_db] = lambda: db
    client.app.dependency_overrides[get_current_user] = lambda: db.get(User, "other-tutor")

    r = client.post(f"/walks/{walk.id}/cancel", json={})
    assert r.status_code == 404


def test_cancel_409_when_already_cancelled():
    client, db = build()
    walk = _walk(db, scheduled_date=_iso(_local_now() + timedelta(hours=48)))
    walk.operational_status = "ride_cancelled"
    walk.status = "Cancelado"
    db.commit()

    r = client.post(f"/walks/{walk.id}/cancel", json={})
    assert r.status_code == 409


def test_cancel_free_walk_without_payment_has_no_refund():
    client, db = build()
    walk = _walk(db, scheduled_date=_iso(_local_now() + timedelta(hours=1)), price=0.0)

    fake_refund = _fake_refund(ok=True)
    with patch("app.routes.payments.refund_asaas_charge", fake_refund):
        r = client.post(f"/walks/{walk.id}/cancel", json={})
    assert r.status_code == 200, r.text
    fake_refund.assert_not_awaited()
    body = r.json()
    assert body["refund_status"] is None


def test_put_status_ride_cancelled_from_owner_tutor_routes_through_motor():
    """Compat OTA antiga: PUT /walks/{id}/status {status: ride_cancelled} do
    TUTOR dono passa pelo motor (sem motivo, payload legado nao tem)."""
    client, db = build()
    walk = _walk(db, scheduled_date=_iso(_local_now() + timedelta(hours=1)), price=100.0, walker_id=WALKER_ID)
    _payment(db, walk, amount=100.0)

    fake_refund = _fake_refund(ok=True)
    with patch("app.routes.payments.refund_asaas_charge", fake_refund):
        r = client.put(f"/walks/{walk.id}/status", json={"status": "ride_cancelled"})
    assert r.status_code == 200, r.text
    fake_refund.assert_awaited_once_with("asaas_sandbox", "prov-1", value=50.0)

    review = db.query(WalkCompletionReview).filter(WalkCompletionReview.walk_id == walk.id).first()
    assert review is not None
    assert review.compensation_amount == 50.0
