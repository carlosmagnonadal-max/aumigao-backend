# backend/tests/test_commission_billing.py
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
import app.models  # noqa: F401
from app.core.database import Base
from app.models.commission_entry import CommissionEntry, COMM_ACCRUED

def _db():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)()

class _Walk:
    def __init__(self, id, tenant_id, walker_id, price, status="Finalizado"):
        self.id = id; self.tenant_id = tenant_id; self.walker_id = walker_id
        self.assigned_walker_id = None; self.price = price; self.status = status

def test_accrue_creates_entry_for_own_walker():
    from app.services.commission_billing_service import accrue_commission_for_walk
    db = _db()
    walk = _Walk("w1", "t1", "k1", 30.0)
    split = {"commission_percent": 10.0, "platform_amount": 3.0, "walker_amount": 27.0}
    accrue_commission_for_walk(db, walk, split, is_network=False, period="2026-06")
    db.commit()
    e = db.query(CommissionEntry).filter_by(walk_id="w1").one()
    assert e.amount == 3.0 and e.commission_percent == 10.0
    assert e.status == COMM_ACCRUED and e.is_network is False

def test_accrue_is_idempotent():
    from app.services.commission_billing_service import accrue_commission_for_walk
    db = _db()
    walk = _Walk("w1", "t1", "k1", 30.0)
    split = {"commission_percent": 10.0, "platform_amount": 3.0, "walker_amount": 27.0}
    accrue_commission_for_walk(db, walk, split, is_network=False, period="2026-06"); db.commit()
    accrue_commission_for_walk(db, walk, split, is_network=False, period="2026-06"); db.commit()
    assert db.query(CommissionEntry).filter_by(walk_id="w1").count() == 1

def test_accrue_skips_network_walk():
    from app.services.commission_billing_service import accrue_commission_for_walk
    db = _db()
    walk = _Walk("w2", "t1", "k1", 30.0)
    split = {"commission_percent": 18.0, "platform_amount": 5.4, "walker_amount": 24.6}
    accrue_commission_for_walk(db, walk, split, is_network=True, period="2026-06"); db.commit()
    assert db.query(CommissionEntry).filter_by(walk_id="w2").count() == 0

def test_accrue_skips_zero_price():
    from app.services.commission_billing_service import accrue_commission_for_walk
    db = _db()
    walk = _Walk("w3", "t1", "k1", 0.0)
    split = {"commission_percent": 10.0, "platform_amount": 0.0, "walker_amount": 0.0}
    accrue_commission_for_walk(db, walk, split, is_network=False, period="2026-06"); db.commit()
    assert db.query(CommissionEntry).filter_by(walk_id="w3").count() == 0
