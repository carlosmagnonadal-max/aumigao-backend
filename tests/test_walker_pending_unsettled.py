"""R7 — cobranças NÃO liquidadas do tutor NÃO viram 'pending' do walker.

Payment de walk em status 'aguardando_pagamento'/'pagamento_sandbox_criado' é
expectativa de cobrança alheia — não é saldo do walker. _balance_by_tenant não
deve contá-lo em nenhum bucket. Já 'pending' (genérico Asaas) permanece no bucket
pending (contrato existente).
"""
from __future__ import annotations

import app.models  # noqa: F401

from uuid import uuid4

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.core.database import Base
from app.models.payment import Payment
from app.models.tenant import Tenant
from app.models.user import User
from app.models.walk import Walk
from app.routes.walker import _balance_by_tenant

WALKER = "w1"
TUTOR = "u1"
T1 = "t1"


def _db():
    eng = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(eng)
    db = sessionmaker(bind=eng)()
    db.add(Tenant(id=T1, name="T1", slug="t1", status="active", plan="business"))
    db.add(User(id=WALKER, email="w@x.com", password_hash="x", role="walker", tenant_id=T1))
    db.add(User(id=TUTOR, email="t@x.com", password_hash="x", role="cliente", tenant_id=T1))
    db.commit()
    return db


def _walk_with_payment(db, wid, status):
    db.add(Walk(id=wid, tutor_id=TUTOR, walker_id=WALKER, tenant_id=T1, pet_id="p",
                status="Agendado", price=50.0, scheduled_date="2026-07-01", duration_minutes=30))
    db.add(Payment(id=str(uuid4()), tutor_id=TUTOR, walk_id=wid, tenant_id=T1,
                   amount=50.0, status=status, provider="asaas", walker_amount=40.0))
    db.commit()


def test_unsettled_charge_not_in_pending():
    db = _db()
    _walk_with_payment(db, "w-await", "aguardando_pagamento")
    _walk_with_payment(db, "w-sandbox", "pagamento_sandbox_criado")
    buckets = _balance_by_tenant(db.get(User, WALKER), db)
    entry = buckets.get(T1, {"available": 0, "pending": 0, "processing": 0})
    assert entry["pending"] == 0.0
    assert entry["available"] == 0.0
    assert entry["processing"] == 0.0


def test_generic_pending_still_counts_as_pending():
    db = _db()
    _walk_with_payment(db, "w-pend", "pending")
    buckets = _balance_by_tenant(db.get(User, WALKER), db)
    assert buckets[T1]["pending"] == 40.0


def test_paid_still_available():
    db = _db()
    _walk_with_payment(db, "w-paid", "paid")
    buckets = _balance_by_tenant(db.get(User, WALKER), db)
    assert buckets[T1]["available"] == 40.0
