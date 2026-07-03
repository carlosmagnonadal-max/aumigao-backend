"""R7 — expiração de passeios não pagos (scheduler).

Passeios em 'awaiting_payment' criados há mais de WALK_PAYMENT_TIMEOUT_HOURS
(default 24h) são cancelados e o tutor é notificado. Idempotente: só toca walks
ainda à espera; não afeta os dentro do prazo nem os já em outros estados.
"""
from __future__ import annotations

import app.models  # noqa: F401

from datetime import datetime, timedelta

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.core.database import Base
from app.models.notification import Notification
from app.models.tenant import Tenant
from app.models.user import User
from app.models.walk import Walk
from app.services.operational_scheduler_service import _task_expire_unpaid_walks


def _setup():
    eng = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(eng)
    Factory = sessionmaker(bind=eng)
    db = Factory()
    db.info["rls_tenant"] = "*"
    db.add(Tenant(id="t1", name="T1", slug="t1", status="active", plan="pro"))
    db.add(User(id="tutor1", email="t@x.com", password_hash="x", role="cliente", tenant_id="t1"))
    db.commit()
    return Factory, db


def _walk(db, wid, op_status, age_hours):
    db.add(Walk(
        id=wid, tutor_id="tutor1", tenant_id="t1", pet_id="p1",
        scheduled_date="2026-07-01", duration_minutes=30, price=50.0,
        status="aguardando_pagamento" if op_status == "awaiting_payment" else "Agendado",
        operational_status=op_status,
        created_at=datetime.utcnow() - timedelta(hours=age_hours),
    ))
    db.commit()


def _run(db):
    n = _task_expire_unpaid_walks(db)
    try:
        db.commit()
    except Exception:
        pass
    return n


def test_expires_only_overdue_awaiting_walks():
    Factory, db = _setup()
    _walk(db, "old", "awaiting_payment", age_hours=30)   # vencido (>24h)
    _walk(db, "fresh", "awaiting_payment", age_hours=2)   # dentro do prazo
    _walk(db, "scheduled", "pending_walker_confirmation", age_hours=48)  # já pago/agendado

    count = _run(db)
    assert count == 1

    db2 = Factory()
    db2.info["rls_tenant"] = "*"
    try:
        old = db2.get(Walk, "old")
        assert old.operational_status == "ride_cancelled"
        assert old.status == "Cancelado"
        assert "pagamento" in (old.no_walker_reason or "").lower()
        # não vencido: intacto
        assert db2.get(Walk, "fresh").operational_status == "awaiting_payment"
        # já agendado: intacto
        assert db2.get(Walk, "scheduled").operational_status == "pending_walker_confirmation"
    finally:
        db2.close()


def test_notifies_tutor_on_expiry():
    Factory, db = _setup()
    _walk(db, "old", "awaiting_payment", age_hours=30)
    _run(db)

    db2 = Factory()
    db2.info["rls_tenant"] = "*"
    try:
        notifs = db2.query(Notification).filter(Notification.user_id == "tutor1").all()
        assert len(notifs) == 1
        assert notifs[0].related_entity_id == "old"
    finally:
        db2.close()


def test_idempotent_second_run_is_noop():
    Factory, db = _setup()
    _walk(db, "old", "awaiting_payment", age_hours=30)
    assert _run(db) == 1
    # segunda passada: já cancelado, não toca mais nada
    assert _run(db) == 0
