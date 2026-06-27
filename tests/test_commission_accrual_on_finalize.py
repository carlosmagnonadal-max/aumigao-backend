# backend/tests/test_commission_accrual_on_finalize.py
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
import app.models  # noqa: F401
from app.core.database import Base
from app.models.commission_entry import CommissionEntry
from app.models.tenant import Tenant
from app.models.walk import Walk
from app.routes.admin import _ensure_internal_walk_payment


def _db():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine)()
    db.add(Tenant(id="t1", name="X", slug="x", status="active", plan="pro"))
    db.commit()
    return db


def test_finalize_accrues_commission_entry():
    db = _db()
    walk = Walk(
        id="w1",
        tenant_id="t1",
        tutor_id="tut1",
        walker_id="k1",
        pet_id="pet-dummy",
        scheduled_date="2026-06-15",
        duration_minutes=30,
        price=40.0,
        status="Finalizado",
    )
    db.add(walk)
    db.commit()
    _ensure_internal_walk_payment(walk, db)
    db.commit()
    e = db.query(CommissionEntry).filter_by(walk_id="w1").one()
    # Pro own-walker fallback = 10% → 40.0 × 10% = 4.0
    assert e.amount == 4.0
    assert e.commission_percent == 10.0
    assert e.is_network is False
