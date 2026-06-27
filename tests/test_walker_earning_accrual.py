# backend/tests/test_walker_earning_accrual.py
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
import app.models  # noqa: F401
from app.core.database import Base
from app.models.tenant import Tenant
from app.models.walk import Walk
from app.models.payment import Payment
from app.models.walker_earning import WalkerEarning
from app.models.tenant_walker_access import TenantWalkerAccess
from app.routes.admin import _ensure_internal_walk_payment


def _db():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine)()
    db.add(Tenant(id="t1", name="X", slug="x", status="active", plan="pro"))
    db.commit()
    return db


def _mk_walk(db, wid, walker_id, price=30.0):
    w = Walk(id=wid, tenant_id="t1", tutor_id="tut", walker_id=walker_id,
             pet_id="pet-dummy", duration_minutes=30,
             price=price, status="Finalizado", scheduled_date="2026-06-10T10:00")
    db.add(w)
    db.commit()
    return w


def test_network_walk_creates_earning_and_zeroes_payment_walker_amount():
    db = _db()
    db.add(TenantWalkerAccess(id="twa1", tenant_id="t1", walker_user_id="k1",
                              access_type="shared_network", status="active"))
    db.commit()
    w = _mk_walk(db, "w1", "k1")
    _ensure_internal_walk_payment(w, db)
    db.commit()
    earning = db.query(WalkerEarning).filter_by(walk_id="w1").one()
    assert earning.amount > 0 and earning.walker_id == "k1"
    pay = db.query(Payment).filter_by(walk_id="w1").one()
    assert (pay.walker_amount or 0) == 0  # rede NÃO credita walker_amount (evita dupla contagem)


def test_own_walk_unchanged_no_earning():
    db = _db()
    w = _mk_walk(db, "w2", "k2")  # sem TenantWalkerAccess => não-rede
    _ensure_internal_walk_payment(w, db)
    db.commit()
    assert db.query(WalkerEarning).filter_by(walk_id="w2").count() == 0
    pay = db.query(Payment).filter_by(walk_id="w2").one()
    assert (pay.walker_amount or 0) > 0  # próprio: comportamento atual mantido


def test_accrual_idempotent():
    db = _db()
    db.add(TenantWalkerAccess(id="twa2", tenant_id="t1", walker_user_id="k1",
                              access_type="shared_network", status="active"))
    db.commit()
    w = _mk_walk(db, "w3", "k1")
    _ensure_internal_walk_payment(w, db)
    db.commit()
    _ensure_internal_walk_payment(w, db)
    db.commit()
    assert db.query(WalkerEarning).filter_by(walk_id="w3").count() == 1
